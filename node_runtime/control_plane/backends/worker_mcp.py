"""Thin stdio MCP bridge for one CLI delivery.

The CLI starts this module as its only MCP server. It registers exactly the
tools granted to the Worker (plus the same workspace-confined read_file and
write_file the Pi backend offers), so a model cannot widen its own authority.

System tools are posted to the running AgentGenome service over HTTP with the
delivery credential from a private context file. The model only chooses a tool
name and schema-validated arguments; run/node/worker/epoch identity, workspace,
review binding and decision binding are fixed here and verified server-side.

Every execution reserves a tool-call budget slot before it can run and leaves
an fsynced start/end receipt. A killed bridge therefore leaves honest evidence
for the Backend's observation and reconciliation.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import threading
import uuid
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

if __package__ in (None, ""):
    # Launched as a script from the CLI's MCP config. Import the package from
    # the checkout that owns this file, not from whatever cwd the model has.
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import anyio
import mcp_types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from node_runtime.control_plane.tools import TOOL_SPECS

CONTEXT_FORMAT = "agentgenome-cli-tool-context/1"
BUDGET_FORMAT = "agentgenome-cli-budget/1"
YIELD_FORMAT = "agentgenome-cli-yield/1"
MCP_RECORD_FORMAT = "agentgenome-worker-mcp/1"
ENDPOINT_PATH = "/v1/worker/tool"
TIMEOUT_SECONDS = 60
MAX_READ_BYTES = 512 * 1024
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
RESERVED_WRITE_ROOTS = ("inputs", "messages")
REQUEST_TOOL = "node.request_decision"

_LOCAL_TOOLS: dict[str, dict[str, Any]] = {
    "read_file": {
        "description": ("Read a UTF-8 text file. path is relative to the "
                        "workspace root, for example inputs/page.json."),
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        "effect": "read_only",
    },
    "write_file": {
        "description": ("Write UTF-8 text to a file. path is relative to the "
                        "workspace root. Frozen inputs and delivered messages "
                        "are read-only. Same content produces the same bytes."),
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"},
                           "text": {"type": "string"}},
            "required": ["path", "text"],
            "additionalProperties": False,
        },
        "effect": "idempotent",
    },
}


class ToolFailure(RuntimeError):
    def __init__(self, code: str, message: str = "", *,
                 uncertain: bool = False, **details: Any) -> None:
        super().__init__(message or code)
        self.code = code
        self.details = details
        # The service may have committed the effect but no definite answer
        # reached this bridge. The unmatched start receipt must stay unmatched.
        self.uncertain = uncertain


class Budget:
    """Per-delivery tool-call reservation shared with the Backend's watcher."""

    def __init__(self, path: Path | None) -> None:
        if path is None:
            raise ToolFailure(
                "budget_unconfigured",
                "the MCP bridge has no tool-call budget file")
        self.path = path
        self.lock = threading.Lock()
        self.state = self._load()
        if self.state is None:
            # A missing or corrupt budget must never mean "unlimited": every
            # delivery that starts this bridge is expected to carry one.
            raise ToolFailure(
                "budget_unavailable",
                f"the tool-call budget file {path} is missing or corrupt")

    def _load(self) -> dict[str, Any] | None:
        if self.path is None:
            return None
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(state, dict) or state.get("format") != BUDGET_FORMAT:
            return None
        state["caps"] = state.get("caps") or {}
        state["prior"] = {"tool_calls": int(
            (state.get("prior") or {}).get("tool_calls", 0) or 0)}
        state["counts"] = {"tool_calls": int(
            (state.get("counts") or {}).get("tool_calls", 0) or 0)}
        state["budget"] = list(state.get("budget") or [])
        state["budget_exceeded"] = state.get("budget_exceeded") is True
        return state

    def _save(self) -> None:
        self.state["extension"] = True
        payload = json.dumps(self.state, sort_keys=True)
        temporary = self.path.with_name(
            f"{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        descriptor = os.open(temporary,
                             os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(descriptor, payload.encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, self.path)

    def reserve(self) -> bool:
        with self.lock:
            cap = self.state["caps"].get("tool_calls")
            total = (self.state["prior"]["tool_calls"]
                     + self.state["counts"]["tool_calls"])
            if isinstance(cap, int) and total >= cap:
                if "tool_calls" not in self.state["budget"]:
                    self.state["budget"].append("tool_calls")
                self.state["budget_exceeded"] = True
                self._save()
                return False
            self.state["counts"]["tool_calls"] += 1
            self._save()
            return True


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _process_start(pid: int) -> int | None:
    """Kernel start time; the exact half of a pid identity."""
    try:
        line = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        return int(line[line.rindex(")") + 2:].split()[19])
    except (ValueError, IndexError):
        return None


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    descriptor = os.open(temporary,
                         os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(descriptor, json.dumps(
            payload, sort_keys=True).encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)


def _record(path: Path | None, entry: Mapping[str, Any]) -> None:
    if path is None:
        raise ToolFailure("tool_receipt_unconfigured",
                          "the tool receipt path is not configured")
    line = json.dumps({**entry, "at": _now()}, sort_keys=True) + "\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, line.encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _load_context() -> dict[str, Any]:
    value = os.environ.pop("AGENTGENOME_TOOL_CONTEXT", None)
    if not value:
        raise SystemExit("worker MCP bridge has no tool context")
    path = Path(value)
    try:
        context = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SystemExit(f"worker MCP context is unreadable: {error}")
    # Keep the private file (0600, inside a 0700 directory): an MCP server may
    # be restarted inside the same CLI turn and must still find its binding.
    if not isinstance(context, dict) or context.get("format") != CONTEXT_FORMAT:
        raise SystemExit("worker MCP context has an unknown format")
    if not isinstance(context.get("workspace"), str) or not context["workspace"]:
        raise SystemExit("worker MCP context has no workspace")
    if not isinstance(context.get("tools"), list):
        raise SystemExit("worker MCP context has no tool list")
    if context["tools"]:
        if not isinstance(context.get("endpoint"), str) or not context["endpoint"]:
            raise SystemExit("worker MCP context has no service endpoint")
        if not isinstance(context.get("token"), str) or not context["token"]:
            raise SystemExit("worker MCP context has no delivery credential")
    backend_tools = context.get("backend_tools") or []
    if not isinstance(backend_tools, list) or any(
            name not in _LOCAL_TOOLS for name in backend_tools):
        raise SystemExit("worker MCP context has an unknown backend tool")
    return context


class Bridge:
    def __init__(self, context: Mapping[str, Any], budget: Budget,
                 receipts: Path | None, yield_file: Path | None) -> None:
        self.context = dict(context)
        self.budget = budget
        self.receipts = receipts
        self.yield_file = yield_file
        self.yielded = False
        self.uncertain = False
        self.workspace = Path(str(context["workspace"]))
        self.system: dict[str, dict[str, Any]] = {
            item["name"]: dict(item) for item in context["tools"]
            if isinstance(item, Mapping) and isinstance(item.get("name"), str)}
        self.backend_tools = tuple(context.get("backend_tools") or [])
        self.by_name: dict[str, str] = {}
        for name in list(self.system) + list(self.backend_tools):
            self.by_name[name] = name
            self.by_name[name.replace(".", "_")] = name

    def tools(self) -> list[types.Tool]:
        exposed: list[types.Tool] = []
        for name in sorted(self.system):
            spec = TOOL_SPECS.get(name)
            exposed.append(types.Tool(
                name=name,
                description=self.system[name].get("description")
                or (spec.name if spec else name),
                input_schema=dict(self.system[name].get("parameters")
                                  or (spec.input_schema if spec else
                                      {"type": "object", "properties": {}})),
                output_schema=dict(spec.output_schema) if spec else None,
            ))
        for name in self.backend_tools:
            local = _LOCAL_TOOLS[name]
            exposed.append(types.Tool(
                name=name, description=local["description"],
                input_schema=dict(local["parameters"])))
        return exposed

    def call(self, requested: str, arguments: Mapping[str, Any] | None) -> dict[str, Any]:
        if self.uncertain:
            raise ToolFailure(
                "tool_outcome_unknown",
                "an earlier tool call may have committed without a verified "
                "result; this turn cannot issue another call")
        name = self.by_name.get(requested)
        if name is None:
            raise ToolFailure("capability_unsupported", "unknown tool",
                              tool=requested)
        if self.yielded:
            raise ToolFailure("decision_pending",
                              "a decision request already ended this turn")
        params = dict(arguments or {})
        if name in self.system:
            return self._system_call(name, params)
        return self._local_call(name, params)

    def _reserve(self) -> None:
        if not self.budget.reserve():
            raise ToolFailure("budget_exhausted",
                              "the tool-call budget is exhausted")

    def _local_call(self, name: str, params: Mapping[str, Any]) -> dict[str, Any]:
        local = _LOCAL_TOOLS[name]
        self._reserve()
        call_id = "mcp-" + uuid.uuid4().hex
        target = params.get("path")
        _record(self.receipts, {
            "phase": "start", "call_id": call_id, "name": name,
            "effect": local["effect"], "params": params,
            **self._identity(),
        })
        try:
            if name == "read_file":
                result = self._read_file(target)
            else:
                result = self._write_file(target, params.get("text"))
        except ToolFailure as error:
            _record(self.receipts, {
                "phase": "end", "call_id": call_id, "name": name,
                "effect": local["effect"], "params": params,
                "error": {"code": error.code, "message": str(error)},
                **self._identity(),
            })
            raise
        _record(self.receipts, {
            "phase": "end", "call_id": call_id, "name": name,
            "effect": local["effect"], "params": params, "result": result,
            **self._identity(),
        })
        return result

    def _system_call(self, name: str, params: Mapping[str, Any]) -> dict[str, Any]:
        self._reserve()
        call_id = "mcp-" + uuid.uuid4().hex
        _record(self.receipts, {
            "phase": "start", "call_id": call_id, "name": name,
            "effect": "idempotent", "params": params, **self._identity()})
        try:
            request: dict[str, Any] = {
                "call_id": call_id, "tool": name, "arguments": params}
            if name == "node.publish_artifact":
                request["content_base64"] = self._artifact_bytes(
                    params.get("path")).decode("ascii")
            result = self._post(request)
        except ToolFailure as error:
            if not error.uncertain:
                _record(self.receipts, {
                    "phase": "end", "call_id": call_id, "name": name,
                    "effect": "idempotent", "params": params,
                    "error": {"code": error.code, "message": str(error)},
                    **self._identity()})
            raise
        _record(self.receipts, {
            "phase": "end", "call_id": call_id, "name": name,
            "effect": "idempotent", "params": params, "result": result,
            **self._identity()})
        output = result.get("output")
        if not isinstance(output, Mapping):
            raise ToolFailure(
                "invalid_response",
                "the worker service returned no structured tool output")
        if name == REQUEST_TOOL:
            self._yield(output)
        return dict(output)

    def _identity(self) -> dict[str, Any]:
        return {key: self.context.get(key) for key in
                ("worker_id", "node_id", "epoch", "run_id", "delivery_key")}

    def _uncertain(self, message: str, **details: Any) -> None:
        """The request may have reached and committed; never report success.

        The start receipt stays unmatched so the Backend's reconciliation sees
        an unknown effect instead of a completed call, and later calls in this
        turn are blocked rather than retried under a fresh call id.
        """
        self.uncertain = True
        raise ToolFailure("tool_outcome_unknown", message, uncertain=True,
                          **details)

    def _post(self, request: Mapping[str, Any]) -> dict[str, Any]:
        payload = json.dumps(request).encode("utf-8")
        http_request = urllib.request.Request(
            str(self.context["endpoint"]).rstrip("/") + ENDPOINT_PATH,
            data=payload,
            headers={"Authorization": f"Bearer {self.context['token']}",
                     "Content-Type": "application/json",
                     "Accept": "application/json"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(http_request, timeout=TIMEOUT_SECONDS) as response:
                status = response.status
                try:
                    body = response.read().decode("utf-8")
                except (OSError, UnicodeDecodeError):
                    self._uncertain(
                        "worker service closed the response without a readable "
                        "body", status=status)
        except urllib.error.HTTPError as error:
            status = error.code
            try:
                body = error.read().decode("utf-8")
            except (OSError, UnicodeDecodeError, ValueError):
                self._uncertain(
                    "worker service returned an unreadable error body",
                    status=status)
        except (urllib.error.URLError, OSError) as error:
            self._uncertain(
                f"worker service could not be reached: {error}")
        try:
            envelope = json.loads(body)
        except ValueError:
            self._uncertain(
                f"worker service returned no JSON (HTTP {status})",
                status=status)
        if not isinstance(envelope, Mapping):
            self._uncertain(
                "worker service returned a non-object response", status=status)
        outcome = envelope.get("ok")
        if outcome is not True:
            if outcome is not False:
                self._uncertain(
                    "worker service response has no definite outcome",
                    status=status)
            # An explicit ok=false envelope is a definite service rejection:
            # no effect is claimed and the call can be reported as failed.
            failure = envelope.get("error")
            failure = failure if isinstance(failure, Mapping) else {}
            raise ToolFailure(
                str(failure.get("code") or "tool_failed"),
                str(failure.get("message")
                    or f"worker service returned HTTP {status}"),
                **(failure.get("details") if isinstance(
                    failure.get("details"), Mapping) else {}))
        result = envelope.get("result")
        if not isinstance(result, Mapping):
            self._uncertain(
                "worker service returned no tool result", status=status)
        return dict(result)

    def _yield(self, output: Any) -> None:
        decision_id = output.get("decision_id") if isinstance(output, Mapping) else None
        if not isinstance(decision_id, str) or not decision_id:
            raise ToolFailure("invalid_response",
                              "decision request did not return a decision id")
        self.yielded = True
        if self.yield_file is None:
            return
        _write_json(self.yield_file, {
            "format": YIELD_FORMAT,
            "delivery_key": self.context.get("delivery_key"),
            "run_id": self.context.get("run_id"),
            "worker_id": self.context.get("worker_id"),
            "node_id": self.context.get("node_id"),
            "epoch": self.context.get("epoch"),
            "decision_id": decision_id,
            "at": _now(),
        })

    # -- workspace-confined local tools ----------------------------------
    def _parts(self, requested: Any, *, write: bool) -> list[str]:
        if not isinstance(requested, str) or not requested:
            raise ToolFailure("invalid_arguments", "path is required")
        text = requested.replace("\\", "/")
        if os.path.isabs(text):
            raise ToolFailure("path_outside_workspace",
                              "path must stay inside the worker workspace")
        parts = [part for part in text.split("/") if part not in ("", ".")]
        if not parts or any(part == ".." for part in parts):
            raise ToolFailure("path_outside_workspace",
                              "path must stay inside the worker workspace")
        if write and parts[0] in RESERVED_WRITE_ROOTS:
            raise ToolFailure("path_read_only",
                              f"{parts[0]} is frozen and cannot be modified")
        return parts

    def _root(self) -> Path:
        try:
            return self.workspace.resolve(strict=True)
        except OSError as error:
            raise ToolFailure("workspace_unconfigured",
                              "worker workspace does not exist") from error

    def _existing(self, requested: Any, *, write: bool) -> tuple[Path, Path]:
        root = self._root()
        parts = self._parts(requested, write=write)
        current = root
        for index, part in enumerate(parts):
            final = index == len(parts) - 1
            current = current / part
            try:
                current.lstat()
            except FileNotFoundError:
                if not write:
                    raise ToolFailure("path_not_found",
                                      f"file {requested} does not exist")
                if final:
                    # Only the final name may be absent; never turn it into a
                    # directory. Its parents were created on earlier rounds.
                    break
                current.mkdir()
                continue
            if current.is_symlink():
                raise ToolFailure("path_symlink",
                                  "symlinks are not allowed in the workspace")
            if final:
                if not current.is_file():
                    raise ToolFailure("path_not_file",
                                      "path must name a regular file")
            elif not current.is_dir():
                raise ToolFailure(
                    "path_not_file",
                    f"{part} is not a directory in the workspace")
        real = current.resolve(strict=False)
        if real != root and root not in real.parents:
            raise ToolFailure("path_outside_workspace",
                              "path must stay inside the worker workspace")
        return root, current

    def _read_file(self, requested: Any) -> dict[str, Any]:
        _, path = self._existing(requested, write=False)
        if not path.is_file():
            raise ToolFailure("path_not_file", "path must name a regular file")
        content = path.read_bytes()
        if len(content) > MAX_READ_BYTES:
            raise ToolFailure("material_too_large",
                              f"file exceeds {MAX_READ_BYTES} bytes")
        return {"path": requested, "text": content.decode("utf-8", errors="replace"),
                "sha256": "sha256:" + hashlib.sha256(content).hexdigest(),
                "bytes": len(content)}

    def _write_file(self, requested: Any, text: Any) -> dict[str, Any]:
        if not isinstance(text, str):
            raise ToolFailure("invalid_arguments", "text must be a string")
        _, path = self._existing(requested, write=True)
        content = text.encode("utf-8")
        temporary = path.with_name(
            f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except OSError as error:
            raise ToolFailure("path_symlink",
                              "symlinks are not allowed in the workspace") from error
        try:
            os.write(descriptor, content)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        return {"path": requested,
                "sha256": "sha256:" + hashlib.sha256(content).hexdigest(),
                "bytes": len(content)}

    def _artifact_bytes(self, requested: Any) -> bytes:
        _, path = self._existing(requested, write=False)
        if not path.is_file():
            raise ToolFailure("path_not_file", "path must name a regular file")
        content = path.read_bytes()
        if len(content) > MAX_ARTIFACT_BYTES:
            raise ToolFailure("material_too_large",
                              f"artifact exceeds {MAX_ARTIFACT_BYTES} bytes")
        return base64.b64encode(content)


def _result(payload: Mapping[str, Any]) -> types.CallToolResult:
    # The structured content is the tool's real output (the ToolSpec output
    # object), never the HTTP envelope, and the text says the same thing.
    text = json.dumps(payload, sort_keys=True)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        structured_content=dict(payload), is_error=False)


def _failure(error: ToolFailure) -> types.CallToolResult:
    payload = {"ok": False, "code": error.code, "message": str(error)}
    details = getattr(error, "details", None)
    if isinstance(details, Mapping):
        payload.update(details)
    return types.CallToolResult(
        content=[types.TextContent(type="text",
                                   text=json.dumps(payload, sort_keys=True))],
        is_error=True)


def main() -> int:
    context = _load_context()
    try:
        budget = Budget(Path(os.environ["AGENTGENOME_BUDGET_FILE"])
                        if os.environ.get("AGENTGENOME_BUDGET_FILE") else None)
    except ToolFailure as error:
        raise SystemExit(
            f"worker MCP bridge refuses to start: {error.code}: {error}")
    record = os.environ.pop("AGENTGENOME_MCP_RECORD", None)
    if record:
        # Durable, private identity for Host-restart cleanup. Without it a
        # later stop cannot prove which process group belongs to this delivery.
        _write_json(Path(record), {
            "format": MCP_RECORD_FORMAT,
            "pid": os.getpid(),
            "process_start": _process_start(os.getpid()),
            "pgid": os.getpgrp(),
            "delivery_key": context.get("delivery_key"),
            "run_id": context.get("run_id"),
            "node_id": context.get("node_id"),
            "worker_id": context.get("worker_id"),
            "epoch": context.get("epoch"),
        })
    receipts = (Path(os.environ["AGENTGENOME_TOOL_RECEIPT"])
                if os.environ.get("AGENTGENOME_TOOL_RECEIPT") else None)
    yield_file = (Path(os.environ["AGENTGENOME_YIELD_FILE"])
                  if os.environ.get("AGENTGENOME_YIELD_FILE") else None)
    bridge = Bridge(context, budget, receipts, yield_file)
    exposed = bridge.tools()

    async def list_tools(_ctx: Any, _params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(tools=exposed)

    async def call_tool(_ctx: Any, params: types.CallToolRequestParams
                        ) -> types.CallToolResult:
        arguments = params.arguments if isinstance(params.arguments, Mapping) else {}
        try:
            output = await anyio.to_thread.run_sync(
                lambda: bridge.call(params.name, arguments))
        except ToolFailure as error:
            return _failure(error)
        except Exception as error:  # never print a traceback to the model
            return _failure(ToolFailure("tool_error", str(error)))
        return _result(output)

    server = Server("agentgenome", version="1",
                    on_list_tools=list_tools, on_call_tool=call_tool)

    async def run() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream,
                             server.create_initialization_options())

    anyio.run(run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
