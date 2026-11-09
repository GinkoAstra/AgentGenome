"""U02 C4/C5/C7: real stdio adapter over the real HTTP MCP service."""
from __future__ import annotations

import anyio
import asyncio
import httpx2
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
from types import SimpleNamespace
import urllib.request
import uuid
import zipfile

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

from conftest import make_graph
from node_runtime.control_plane.client_access import connect_client
from node_runtime.control_plane.client_mcp import (
    RemoteTransportError, _RemoteMCP)
from node_runtime.control_plane.host import Host
from node_runtime.control_plane.server import ApiServer

ROOT = Path(__file__).resolve().parents[2]


def _rid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _wait_ready(endpoint: str, timeout: float = 10.0) -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            with opener.open(endpoint + "/readyz", timeout=1.0) as response:
                last = json.loads(response.read().decode("utf-8"))
            if last.get("ready") is True:
                return
        except Exception:
            pass
        time.sleep(0.02)
    raise AssertionError(f"service never became ready: {last}")


def _service(path: Path) -> SimpleNamespace:
    host = Host(path, backend="script", scheduler_interval=0.02,
                poll_seconds=0.0)
    server = ApiServer(host, port=0, heartbeat_timeout=5.0)
    endpoint = server.start_background()
    credentials = server.auth.publisher_credentials()
    _wait_ready(endpoint)
    return SimpleNamespace(server=server, host=host,
                           data_dir=Path(host.data_dir), endpoint=endpoint,
                           token=credentials["token"],
                           service_id=credentials["service_id"])


_SERVICE_RUNNER = """
import sys, time
sys.path.insert(0, {root!r})
from node_runtime.control_plane.host import Host
from node_runtime.control_plane.server import ApiServer
host = Host({data!r}, backend="script", scheduler_interval=0.02,
            poll_seconds=0.0)
server = ApiServer(host, port=0, heartbeat_timeout=5.0)
print(server.start_background(), flush=True)
while True:
    time.sleep(0.5)
"""


def _start_service_process(data: Path, scratch: Path) -> tuple[subprocess.Popen, dict]:
    """Start a real service process so kill -9 leaves no graceful drain."""
    log = (scratch / f"service-{time.time_ns()}.log").open("wb")
    proc = subprocess.Popen(
        [sys.executable, "-c",
         _SERVICE_RUNNER.format(root=str(ROOT), data=str(data))],
        stdout=subprocess.DEVNULL, stderr=log, start_new_session=True)
    try:
        record = _wait_service_record(data, proc.pid)
    except BaseException:
        _kill_service_process(proc)
        raise
    return proc, record


def _wait_service_record(data: Path, pid: int, timeout: float = 20.0) -> dict:
    record_path = data / "service.json"
    deadline = time.monotonic() + timeout
    record = None
    while time.monotonic() < deadline:
        try:
            candidate = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            candidate = None
        if isinstance(candidate, dict) and candidate.get("pid") == pid:
            record = candidate
            try:
                _wait_ready(record["endpoint"], timeout=0.5)
                return record
            except Exception:
                pass
        time.sleep(0.02)
    raise AssertionError(f"service process {pid} never became ready: {record}")


def _kill_service_process(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    proc.wait(timeout=10)


def _operation(service: SimpleNamespace, operation: str,
               arguments: dict) -> dict:
    request = urllib.request.Request(
        service.endpoint + f"/v1/operations/{operation}",
        data=json.dumps(arguments).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {service.token}",
                 "Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=10.0) as response:
        body = json.loads(response.read().decode("utf-8"))
    assert body["ok"] is True, body
    return body["result"]


def _stdio_params(data_dir: Path, *, kind: str = "codex",
                  session: str = "c4", workspace: str = "/work/c4"
                  ) -> StdioServerParameters:
    env = dict(os.environ)
    previous = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(ROOT) + (
        os.pathsep + previous if previous else "")
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "node_runtime.control_plane.client_mcp",
              "--client-kind", kind, "--session-id", session,
              "--workspace", workspace, "--data-dir", str(data_dir)],
        env=env, cwd=str(ROOT))


def _unwrap(error: BaseException) -> BaseException:
    while isinstance(error, BaseExceptionGroup) and len(error.exceptions) == 1:
        error = error.exceptions[0]
    return error


def _remote_tools(service: SimpleNamespace):
    async def run():
        client = httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {service.token}"},
            timeout=30.0, trust_env=False)
        try:
            async with streamable_http_client(
                    service.endpoint + "/mcp", http_client=client,
                    terminate_on_close=False) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await session.list_tools()
        finally:
            await client.aclose()

    return anyio.run(run)


def _registrations(service: SimpleNamespace) -> list[dict]:
    access = service.server.service_access
    with access._lock:
        return [dict(record)
                for record in access._registrations.values()]


def test_real_stdio_forwards_remote_tools_and_artifact_results(tmp_path):
    service = _service(tmp_path / "data")
    errlog = tmp_path / "client.err.log"
    try:
        direct = connect_client(data_dir=service.data_dir, client_kind="pi",
                                client_session_id="seed", workspace="/work")
        direct.call("publish_definition", {
            "request_id": _rid("publish"),
            "document": make_graph(definition_id="c4-stdio")})
        artifact = direct.call("put_artifact", {
            "request_id": _rid("artifact"), "text": "seed"})["artifact"]
        direct.close()

        remote = _remote_tools(service)
        observed: dict[str, str] = {}

        async def exercise():
            with errlog.open("w", encoding="utf-8") as log:
                async with stdio_client(_stdio_params(service.data_dir),
                                        errlog=log) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        listed = await session.list_tools()
                        local = {tool.name: tool for tool in listed.tools}
                        expected = {tool.name: tool for tool in remote.tools}
                        assert set(local) == set(expected)
                        for name, tool in expected.items():
                            assert local[name].input_schema == tool.input_schema
                            assert local[name].description == tool.description
                        read_schema = local["read_artifact"].input_schema[
                            "properties"]
                        assert "artifact_id" in read_schema
                        assert "id" not in read_schema

                        put = await session.call_tool("put_artifact", {
                            "request_id": _rid("mcp-put"),
                            "text": "through stdio"})
                        assert put.is_error is False
                        assert put.structured_content["ok"] is True
                        assert put.structured_content["operation"] == "put_artifact"
                        ref = put.structured_content["result"]["artifact"]
                        assert ref["id"].startswith("sha256:")

                        got = await session.call_tool("read_artifact", {
                            "artifact_id": ref["id"], "sha256": ref["sha256"],
                            "size": ref["size"],
                            "media_type": ref["media_type"]})
                        assert got.is_error is False
                        read = got.structured_content["result"]
                        assert read["text"] == "through stdio"
                        assert read["artifact"] == ref

                        missing = await session.call_tool("does_not_exist", {})
                        assert missing.is_error is True
                        assert "Unknown tool: does_not_exist" in json.dumps(
                            [getattr(item, "text", None)
                             for item in missing.content])

                        matching = [
                            record for record in _registrations(service)
                            if record["client_kind"] == "codex"
                            and record["client_session_id"] == "c4"]
                        assert len(matching) == 1
                        registered = await anyio.to_thread.run_sync(
                            lambda: _operation(
                                service, "register_client",
                                {"client_kind": "codex",
                                 "client_session_id": "c4",
                                 "workspace": "/work/c4"}))
                        assert registered["client_id"] == \
                            matching[0]["client_id"]
                        observed["client_id"] = matching[0]["client_id"]
                        assert service.token not in json.dumps(
                            [put.structured_content, got.structured_content,
                             registered])
                        observed["done"] = "yes"

        anyio.run(exercise)
        assert observed.get("done") == "yes"

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            remaining = [
                record for record in _registrations(service)
                if record["client_id"] == observed["client_id"]]
            if not remaining:
                break
            time.sleep(0.02)
        assert not remaining
        assert service.token not in errlog.read_text(encoding="utf-8")
    finally:
        service.server.stop()


def test_stdio_rediscovers_the_same_service_after_restart(tmp_path):
    data = tmp_path / "data"
    first_proc, first = _start_service_process(data, tmp_path)
    restarted: subprocess.Popen | None = None
    errlog = tmp_path / "restart.err.log"

    async def exercise():
        nonlocal restarted
        with errlog.open("w", encoding="utf-8") as log:
            async with stdio_client(_stdio_params(data),
                                    errlog=log) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    assert "get_service_info" in {tool.name
                                                  for tool in listed.tools}

                    # A real crash: SIGKILL leaves no graceful HTTP drain.
                    _kill_service_process(first_proc)
                    (data / "service.json").unlink()
                    second_proc, second = _start_service_process(data, tmp_path)
                    restarted = second_proc
                    assert second["service_id"] == first["service_id"]
                    assert second["instance_id"] != first["instance_id"]
                    assert second["port"] != first["port"]

                    with pytest.raises(BaseException) as failure:
                        await session.list_tools()
                    assert isinstance(_unwrap(failure.value), MCPError)

                    info = await session.call_tool("get_service_info", {})
                    assert info.is_error is False, info
                    result = info.structured_content["result"]
                    assert result["service_id"] == first["service_id"]
                    assert result["instance_id"] == second["instance_id"]

    try:
        anyio.run(exercise)
    finally:
        if restarted is not None:
            _kill_service_process(restarted)
        _kill_service_process(first_proc)


def test_wheel_install_exposes_client_mcp_help(tmp_path):
    uv = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
    if not Path(uv).is_file():
        pytest.skip("uv is required to build a wheel in this environment")

    source = tmp_path / "source"
    source.mkdir()
    shutil.copy2(ROOT / "pyproject.toml", source / "pyproject.toml")
    for name in ("LICENSE", "README.md"):
        if (ROOT / name).is_file():
            shutil.copy2(ROOT / name, source / name)
    shutil.copytree(ROOT / "node_runtime", source / "node_runtime",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    dist = tmp_path / "dist"
    built = subprocess.run(
        [uv, "build", "--wheel", "--out-dir", str(dist), str(source)],
        capture_output=True, text=True, timeout=300)
    assert built.returncode == 0, built.stderr
    wheels = sorted(dist.glob("*.whl"))
    assert len(wheels) == 1, wheels

    target = tmp_path / "install"
    with zipfile.ZipFile(wheels[0]) as archive:
        names = archive.namelist()
        assert "node_runtime/control_plane/client_mcp.py" in names
        archive.extractall(target)

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    env = dict(os.environ)
    previous = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(target) + (
        os.pathsep + previous if previous else "")
    helped = subprocess.run(
        [sys.executable, "-m", "node_runtime.control_plane.client_mcp",
         "--help"], cwd=elsewhere, env=env, capture_output=True, text=True)
    assert helped.returncode == 0, helped.stderr
    assert "--client-kind" in helped.stdout

    origin = subprocess.run(
        [sys.executable, "-c",
         "import node_runtime.control_plane.client_mcp as m; print(m.__file__)"],
        cwd=elsewhere, env=env, capture_output=True, text=True)
    assert origin.returncode == 0, origin.stderr
    assert str(target) in origin.stdout


def test_stdio_accepts_the_claude_client_kind(tmp_path):
    service = _service(tmp_path / "data")
    try:
        async def exercise():
            async with stdio_client(
                    _stdio_params(service.data_dir, kind="claude",
                                  session="claude", workspace="/work/claude")
            ) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    await session.list_tools()
                    deadline = time.monotonic() + 5.0
                    while time.monotonic() < deadline:
                        matching = [
                            record for record in _registrations(service)
                            if record["client_kind"] == "claude"
                            and record["client_session_id"] == "claude"]
                        if matching:
                            assert matching[0]["workspace"] == "/work/claude"
                            return
                        await anyio.sleep(0.02)
                    raise AssertionError("claude stdio client was not registered")

        anyio.run(exercise)
    finally:
        service.server.stop()


def test_retirement_settles_active_and_queued_requests_without_replay(tmp_path):
    async def scenario():
        remote = _RemoteMCP(data_dir=str(tmp_path / "data"),
                            client_kind="codex",
                            client_session_id="b1", workspace="/work/b1")
        entered = asyncio.Event()

        async def blocked():
            entered.set()
            await asyncio.Event().wait()

        remote._ensure_connected = blocked
        active = asyncio.create_task(remote.call_tool("create_run", {}))
        await asyncio.wait_for(entered.wait(), 2)
        queued = asyncio.create_task(remote.list_tools())
        deadline = time.monotonic() + 2
        while remote._queue.qsize() < 1 and time.monotonic() < deadline:
            await asyncio.sleep(0)
        assert remote._queue.qsize() == 1

        owner = remote._task
        assert owner is not None
        owner.cancel()
        results = await asyncio.wait_for(
            asyncio.gather(active, queued, return_exceptions=True), 3)
        assert isinstance(results[0], RemoteTransportError)
        assert results[0].unknown_outcome is True
        assert isinstance(results[1], RemoteTransportError)
        assert results[1].unknown_outcome is False
        assert "not sent" in results[1].message
        assert remote._queue.empty()
        assert remote._retired is True
        await asyncio.wait_for(remote.close(), 3)

    asyncio.run(scenario())


def test_concurrent_requests_replace_one_retired_owner(tmp_path):
    async def scenario():
        remote = _RemoteMCP(data_dir=str(tmp_path / "data"),
                            client_kind="codex",
                            client_session_id="b2", workspace="/work/b2")
        gate = asyncio.Event()

        async def old_owner():
            await gate.wait()

        old = asyncio.create_task(old_owner())
        remote._task = old
        remote._retired = True
        owners = []

        async def fake_run():
            owners.append(asyncio.current_task())
            while True:
                request = await remote._queue.get()
                if request is None:
                    return
                request.future.set_result((request.operation, "ok"))

        remote._run = fake_run
        first = asyncio.create_task(remote.list_tools())
        await asyncio.sleep(0)
        second = asyncio.create_task(remote.call_tool(
            "get_service_info", {}))
        await asyncio.sleep(0)
        assert remote._replace_lock.locked()
        assert not first.done() and not second.done()
        assert owners == []

        gate.set()
        results = await asyncio.wait_for(asyncio.gather(first, second), 3)
        assert sorted(item[0] for item in results) == [
            "call_tool", "list_tools"]
        assert len(owners) == 1
        assert remote._task is owners[0]
        await asyncio.wait_for(remote.close(), 3)

    asyncio.run(scenario())
