from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

import node_runtime.control_plane.backends.worker_mcp as worker_mcp
from node_runtime.control_plane.backends.worker_mcp import (
    BUDGET_FORMAT,
    CONTEXT_FORMAT,
    YIELD_FORMAT,
    Bridge,
    Budget,
    ToolFailure,
)


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - http.server contract
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        state = self.server.state
        state["requests"].append({
            "path": self.path,
            "headers": dict(self.headers),
            "body": body,
        })
        mode = state["mode"]
        if mode == "close":
            self.close_connection = True
            return
        if mode == "nonjson":
            data = b"<html>proxy error</html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if mode == "reject":
            self._reply(200, {"ok": False, "error": {
                "code": "unauthorized", "message": "denied"}})
            return
        self._reply(200, {"ok": True, "result": state["result"]})

    def _reply(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: object) -> None:
        return


@contextlib.contextmanager
def fake_worker(mode: str = "ok", result: dict | None = None):
    state = {
        "mode": mode,
        "result": result or {"version": 1, "output": {"id": "sha256:abc"}},
        "requests": [],
    }
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.state = state
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def _context(workspace: Path, *, system: tuple[dict, ...] = (),
             backend: tuple[str, ...] = ("read_file", "write_file"),
             endpoint: str | None = None, token: str | None = None) -> dict:
    return {
        "format": CONTEXT_FORMAT,
        "workspace": str(workspace),
        "tools": list(system),
        "backend_tools": list(backend),
        "endpoint": endpoint,
        "token": token,
        "worker_id": "w1",
        "node_id": "n1",
        "run_id": "r1",
        "epoch": 1,
        "delivery_key": "d1",
    }


def _budget_file(tmp_path: Path, cap: int | None = None) -> Path:
    path = tmp_path / "budget.json"
    path.write_text(json.dumps({
        "format": BUDGET_FORMAT,
        "caps": {} if cap is None else {"tool_calls": cap},
        "prior": {"tool_calls": 0},
        "counts": {"tool_calls": 0},
        "budget": [],
        "budget_exceeded": False,
    }), encoding="utf-8")
    return path


def _bridge(tmp_path: Path, context: dict, *,
            budget: Path | None = None,
            receipts: Path | None = None,
            yield_file: Path | None = None) -> Bridge:
    return Bridge(context, Budget(budget or _budget_file(tmp_path)),
                  receipts if receipts is not None else tmp_path / "receipts.jsonl",
                  yield_file)


def _receipts(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _system(name: str) -> dict:
    return {"name": name, "version": 1, "description": name,
            "parameters": {"type": "object", "properties": {}}}


def test_write_creates_nested_file_and_never_a_directory(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bridge = _bridge(tmp_path, _context(workspace))

    bridge.call("write_file", {"path": "nested/dir/out.txt", "text": "one"})
    target = workspace / "nested" / "dir" / "out.txt"
    assert target.is_file()
    assert target.read_text(encoding="utf-8") == "one"

    bridge.call("write_file", {"path": "nested/dir/out.txt", "text": "two"})
    assert target.is_file()
    assert target.read_text(encoding="utf-8") == "two"
    read = bridge.call("read_file", {"path": "nested/dir/out.txt"})
    assert read["text"] == "two"


def test_write_confines_paths_and_frozen_inputs(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "inputs").mkdir()
    (workspace / "inputs" / "frozen.txt").write_text("frozen")
    (workspace / "blocker").write_text("regular file")
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, workspace / "link")
    bridge = _bridge(tmp_path, _context(workspace))

    cases = [
        ({"path": "inputs/new.txt", "text": "x"}, "path_read_only"),
        ({"path": "messages/x.txt", "text": "x"}, "path_read_only"),
        ({"path": "../escape.txt", "text": "x"}, "path_outside_workspace"),
        ({"path": str(tmp_path / "abs.txt"), "text": "x"},
         "path_outside_workspace"),
        ({"path": "blocker/child.txt", "text": "x"}, "path_not_file"),
        ({"path": "link/out.txt", "text": "x"}, "path_symlink"),
        ({"path": "inputs/new.txt"}, "path_not_found"),
        ({"path": "link"}, "path_symlink"),
        ({"path": "inputs", "text": "x"}, "path_read_only"),
    ]
    for arguments, code in cases:
        name = "read_file" if "text" not in arguments else "write_file"
        with pytest.raises(ToolFailure) as caught:
            bridge.call(name, arguments)
        assert caught.value.code == code, (arguments, caught.value.code)


def test_budget_is_reserved_before_execution(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    budget_path = tmp_path / "budget.json"
    budget_path.write_text(json.dumps({
        "format": BUDGET_FORMAT,
        "caps": {"tool_calls": 0},
        "prior": {"tool_calls": 0},
        "counts": {"tool_calls": 0},
        "budget": [],
        "budget_exceeded": False,
        "extension": False,
    }), encoding="utf-8")
    receipts = tmp_path / "receipts.jsonl"
    bridge = _bridge(tmp_path, _context(workspace), budget=budget_path,
                     receipts=receipts)

    with pytest.raises(ToolFailure) as caught:
        bridge.call("write_file", {"path": "out.txt", "text": "x"})
    assert caught.value.code == "budget_exhausted"
    assert not (workspace / "out.txt").exists()
    assert _receipts(receipts) == []
    state = json.loads(budget_path.read_text(encoding="utf-8"))
    assert state["budget_exceeded"] is True
    assert state["budget"] == ["tool_calls"]
    assert state["counts"]["tool_calls"] == 0


def test_publish_uploads_exact_workspace_bytes(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    content = b"\x00artifact\nbytes\xff"
    (workspace / "result.bin").write_bytes(content)
    receipts = tmp_path / "receipts.jsonl"
    with fake_worker(result={"version": 1,
                             "output": {"id": "sha256:file"}}) as (state, url):
        bridge = _bridge(
            tmp_path,
            _context(workspace, system=(_system("node.publish_artifact"),),
                     endpoint=url, token="delivery-token"),
            receipts=receipts)
        output = bridge.call("node.publish_artifact",
                             {"path": "result.bin",
                              "media_type": "application/octet-stream"})
    assert output["id"] == "sha256:file"
    request = json.loads(state["requests"][0]["body"])
    assert request["tool"] == "node.publish_artifact"
    assert base64.b64decode(request["content_base64"]) == content
    assert state["requests"][0]["headers"]["Authorization"] == \
        "Bearer delivery-token"
    phases = [(item["phase"], item.get("error")) for item in _receipts(receipts)]
    assert phases == [("start", None), ("end", None)]


def test_definite_rejection_records_end_and_keeps_working(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    receipts = tmp_path / "receipts.jsonl"
    with fake_worker(mode="reject") as (_state, url):
        bridge = _bridge(
            tmp_path,
            _context(workspace, system=(_system("node.echo"),),
                     endpoint=url, token="t"),
            receipts=receipts)
        with pytest.raises(ToolFailure) as caught:
            bridge.call("node.echo", {})
        assert caught.value.code == "unauthorized"
        assert caught.value.uncertain is False
        assert bridge.uncertain is False
    entries = _receipts(receipts)
    assert [item["phase"] for item in entries] == ["start", "end"]
    assert entries[1]["error"]["code"] == "unauthorized"


def test_non_json_after_request_is_an_unknown_outcome(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    receipts = tmp_path / "receipts.jsonl"
    with fake_worker(mode="nonjson") as (state, url):
        bridge = _bridge(
            tmp_path,
            _context(workspace, system=(_system("node.echo"),),
                     endpoint=url, token="t"),
            receipts=receipts)
        with pytest.raises(ToolFailure) as caught:
            bridge.call("node.echo", {})
        assert caught.value.code == "tool_outcome_unknown"
        assert caught.value.uncertain is True
        assert bridge.uncertain is True
        with pytest.raises(ToolFailure) as blocked:
            bridge.call("node.echo", {})
        assert blocked.value.code == "tool_outcome_unknown"
    assert len(state["requests"]) == 1
    entries = _receipts(receipts)
    assert [item["phase"] for item in entries] == ["start"]
    assert entries[0]["name"] == "node.echo"


def test_closed_connection_is_an_unknown_outcome(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    receipts = tmp_path / "receipts.jsonl"
    with fake_worker(mode="close") as (_state, url):
        bridge = _bridge(
            tmp_path,
            _context(workspace, system=(_system("node.echo"),),
                     endpoint=url, token="t"),
            receipts=receipts)
        with pytest.raises(ToolFailure) as caught:
            bridge.call("node.echo", {})
        assert caught.value.code == "tool_outcome_unknown"
        assert caught.value.uncertain is True
    assert [item["phase"] for item in _receipts(receipts)] == ["start"]


def test_decision_request_writes_yield_marker_and_stops_the_turn(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    receipts = tmp_path / "receipts.jsonl"
    yield_file = tmp_path / "yield.json"
    with fake_worker(result={
            "version": 1, "output": {"decision_id": "decision-1"}}) as (_s, url):
        bridge = _bridge(
            tmp_path,
            _context(workspace, system=(_system("node.request_decision"),),
                     endpoint=url, token="t"),
            receipts=receipts, yield_file=yield_file)
        bridge.call("node.request_decision", {"question": "which?"})
        marker = json.loads(yield_file.read_text(encoding="utf-8"))
        assert marker["format"] == YIELD_FORMAT
        assert marker["decision_id"] == "decision-1"
        assert marker["delivery_key"] == "d1"
        with pytest.raises(ToolFailure) as caught:
            bridge.call("node.request_decision", {"question": "again?"})
        assert caught.value.code == "decision_pending"


def test_stdio_bridge_lists_and_calls_local_tool(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "note.txt").write_text("hello bridge", encoding="utf-8")
    context = tmp_path / "context.json"
    context.write_text(json.dumps(_context(
        workspace, backend=("read_file",))), encoding="utf-8")
    receipts = tmp_path / "receipts.jsonl"
    budget = _budget_file(tmp_path)
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(Path(worker_mcp.__file__))],
        env={
            "AGENTGENOME_TOOL_CONTEXT": str(context),
            "AGENTGENOME_TOOL_RECEIPT": str(receipts),
            "AGENTGENOME_BUDGET_FILE": str(budget),
        },
    )

    async def run() -> tuple[list[str], dict, dict]:
        async with stdio_client(params) as (
                read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tools = await session.list_tools()
                result = await session.call_tool(
                    "read_file", {"path": "note.txt"})
                assert result.is_error is False
                payload = json.loads(result.content[0].text)
                return ([item.name for item in tools.tools], payload,
                        result.structured_content)

    names, payload, structured = anyio.run(run)
    assert names == ["read_file"]
    assert payload["text"] == "hello bridge"
    assert structured == payload
    assert context.is_file()
    assert [item["phase"] for item in _receipts(receipts)] == ["start", "end"]

def test_missing_or_corrupt_budget_refuses_to_start(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(ToolFailure) as missing:
        Budget(None)
    assert missing.value.code == "budget_unconfigured"
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")
    with pytest.raises(ToolFailure) as bad:
        Budget(corrupt)
    assert bad.value.code == "budget_unavailable"
    assert Budget(_budget_file(tmp_path)).reserve() is True
