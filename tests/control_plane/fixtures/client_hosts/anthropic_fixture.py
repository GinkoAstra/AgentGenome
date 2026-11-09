"""Controlled Anthropic wire-format fixture for native Claude acceptance.

The fixture supplies model ``tool_use`` requests only. Every result it reads is
a real ``tool_result`` produced by the native Claude MCP client against the
installed AgentGenome bridge and the isolated service. It never authors or
fabricates a service result; it only chooses the next request from the actual
returned result.
"""
from __future__ import annotations

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from fixtures.client_hosts.graph import RUN_INPUTS, graph_document

TERMINAL = {"completed", "failed", "cancelled"}


def _flatten(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_flatten(item) for item in value)
    if isinstance(value, dict):
        if "text" in value:
            return _flatten(value["text"])
        if "content" in value:
            return _flatten(value["content"])
        return json.dumps(value, ensure_ascii=False)
    return "" if value is None else str(value)


def _envelope(block: dict | None) -> dict | None:
    if not isinstance(block, dict):
        return None
    text = _flatten(block.get("content")).strip()
    if not text:
        return None
    for candidate in (text, *(line.strip() for line in text.splitlines())):
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _last_exchange(messages: list[dict]) -> tuple[dict | None, dict | None]:
    uses: list[dict] = []
    results: dict[str, dict] = {}
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "tool_use":
                uses.append(part)
            elif part.get("type") == "tool_result" and part.get("tool_use_id"):
                results[str(part["tool_use_id"])] = part
    if not uses:
        return None, None
    last = uses[-1]
    return last, results.get(str(last.get("id")))


def _tool_name(names: list[str], operation: str) -> str:
    for name in names:
        if name == operation or name.endswith(f"__{operation}"):
            return name
    raise RuntimeError(f"native Claude did not expose {operation}; "
                       f"tools={sorted(names)[:40]}")


def _next_request(body: dict, observations: list[dict],
                  observe_run_id: str | None,
                  definition_id: str) -> tuple[dict | None, dict]:
    names = [tool.get("name", "") for tool in body.get("tools", [])
             if isinstance(tool, dict)]
    last, result = _last_exchange(body.get("messages", []))
    if last is None:
        return ({"type": "tool_use", "name": _tool_name(names, "get_service_info"),
                 "input": {}}, {"step": "get_service_info"})
    name = str(last.get("name", ""))
    envelope = _envelope(result)
    operation = name.rsplit("__", 1)[-1]
    if envelope is None:
        raise RuntimeError(f"no readable real tool_result for {name}")
    if envelope.get("ok") is not True:
        raise RuntimeError(f"native MCP call failed: {json.dumps(envelope)[:600]}")
    payload = envelope.get("result")
    observations.append({"tool": operation, "result": payload})
    if operation == "get_service_info":
        return ({"type": "tool_use",
                 "name": _tool_name(names, "list_definitions"), "input": {}},
                {"step": "list_definitions"})
    if operation == "list_definitions":
        if observe_run_id is not None:
            return ({"type": "tool_use",
                     "name": _tool_name(names, "read_trace"),
                     "input": {"run_id": observe_run_id}},
                    {"step": "read_trace"})
        return ({"type": "tool_use",
                 "name": _tool_name(names, "publish_definition"),
                 "input": {"request_id": f"{definition_id}-publish",
                           "document": graph_document(definition_id)}},
                {"step": "publish_definition"})
    if operation == "publish_definition":
        return ({"type": "tool_use", "name": _tool_name(names, "create_run"),
                 "input": {"definition_ref": payload["definition_ref"],
                           "inputs": RUN_INPUTS,
                           "request_id": f"{definition_id}-run"}},
                {"step": "create_run"})
    if operation == "create_run":
        return ({"type": "tool_use", "name": _tool_name(names, "read_trace"),
                 "input": {"run_id": payload["run_id"]}},
                {"step": "read_trace"})
    if operation == "read_trace":
        if payload.get("run", {}).get("status") in TERMINAL:
            return None, {"step": "done"}
        return ({"type": "tool_use", "name": _tool_name(names, "read_trace"),
                 "input": {"run_id": payload["run"]["run_id"]}},
                {"step": "read_trace"})
    raise RuntimeError(f"unexpected fixture step: {name}")


class AnthropicFixture:
    def __init__(self, observe_run_id: str | None = None,
                 definition_id: str = "u03-host-claude-native") -> None:
        self.observe_run_id = observe_run_id
        self.definition_id = definition_id
        self.observations: list[dict] = []
        self.tool_sequences: list[str] = []
        self.errors: list[str] = []
        self.requests: list[dict] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_port}"

    def start(self) -> "AnthropicFixture":
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path.rstrip("/") in ("/v1/models", "/models"):
                    self._json({"data": [], "has_more": False})
                else:
                    self._json({"error": "not_found"}, status=404)

            def do_POST(self):
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length)
                try:
                    body = json.loads(raw)
                except ValueError:
                    self._json({"error": "bad_json"}, status=400)
                    return
                combined = _flatten(body.get("system"))
                for message in body.get("messages", []):
                    combined += "\n" + _flatten(message.get("content"))
                fixture.requests.append({
                    "skill_loaded": ("existing `agentgenome` MCP connection"
                                     in combined),
                    "tool_names": sorted(
                        tool.get("name", "") for tool in body.get("tools", [])
                        if isinstance(tool, dict)),
                })
                if "count_tokens" in self.path:
                    self._json({"input_tokens": 1})
                    return
                try:
                    next_tool, marker = _next_request(
                        body, fixture.observations, fixture.observe_run_id,
                        fixture.definition_id)
                except Exception as error:  # surfaced as a fixture failure
                    fixture.errors.append(str(error))
                    next_tool, marker = None, {"step": f"ERROR: {error}"}
                if next_tool is not None:
                    fixture.tool_sequences.append(next_tool["name"])
                self._message(body, next_tool, marker["step"])

            def _json(self, payload: dict, status: int = 200):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _message(self, body: dict, tool: dict | None, step: str):
                tool_use_id = f"toolu_{uuid.uuid4().hex}"
                if tool is not None:
                    content = {"type": "tool_use", "id": tool_use_id,
                               "name": tool["name"], "input": tool["input"]}
                    stop_reason = "tool_use"
                else:
                    content = {"type": "text", "text": f"FIXTURE-DONE {step}"}
                    stop_reason = "end_turn"
                message = {
                    "id": f"msg_{uuid.uuid4().hex}", "type": "message",
                    "role": "assistant", "model": body.get("model", "fixture"),
                    "content": [content], "stop_reason": stop_reason,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                }
                if not body.get("stream"):
                    self._json(message)
                    return
                empty = {**message, "content": [], "stop_reason": None,
                         "usage": {"input_tokens": 1, "output_tokens": 0}}
                if tool is None:
                    start = {"type": "text", "text": ""}
                    delta = {"type": "text_delta",
                             "text": content["text"]}
                else:
                    start = {"type": "tool_use", "id": tool_use_id,
                             "name": tool["name"], "input": {}}
                    delta = {"type": "input_json_delta",
                             "partial_json": json.dumps(tool["input"])}
                events = [
                    {"type": "message_start", "message": empty},
                    {"type": "content_block_start", "index": 0,
                     "content_block": start},
                    {"type": "content_block_delta", "index": 0,
                     "delta": delta},
                    {"type": "content_block_stop", "index": 0},
                    {"type": "message_delta",
                     "delta": {"stop_reason": stop_reason,
                               "stop_sequence": None},
                     "usage": {"output_tokens": 1}},
                    {"type": "message_stop"},
                ]
                data = "".join(
                    f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
                    for event in events).encode()
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever,
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
