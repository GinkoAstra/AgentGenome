"""Thin MCP adapter over the control-plane application API.

Every tool forwards to exactly one `Api` method: no scheduling, validation or
state transition is reimplemented here. The transport is the network service's
MCP Streamable HTTP endpoint; clients connect with any real MCP client and a
publisher bearer credential. Definitions are edited by the Teacher with
ordinary file tools, read back, and passed as structured objects or uploaded
text; every resource check runs once in ``Api``.
"""
from __future__ import annotations

from collections.abc import Mapping
from contextvars import ContextVar
from typing import Any

from mcp.server.mcpserver import MCPServer

from .api import Api
from .http_api import dispatch
from .resources import Actor, ControlPlaneError

SERVER_NAME = "agentgenome"
SERVER_INSTRUCTIONS = (
    "AgentGenome control plane. Validate and publish Graph YAML definitions, "
    "submit runs, then read events, resources and reports. Every tool maps one "
    "public API call; no tool mutates another tool's state."
)

# Single source of truth for the Graph tool surface. The Teacher launcher puts
# these names in Pi's --tools whitelist, which also governs tools registered by
# an extension, so a tool listed here is active without a second enable step.
# A test asserts this stays identical to the server's real tools/list.
TOOL_NAMES = (
    "validate_definition", "publish_definition", "put_artifact",
    "read_artifact", "create_run", "get_resource", "list_runs",
    "list_resources", "read_events", "get_report", "set_run_intent",
    "read_trace", "read_execution", "read_requests",
)

# Declared argument names per tool. The network middleware uses this to turn
# an unknown argument into the same structured refusal the JSON adapter gives
# instead of letting the SDK silently ignore it.
TOOL_ARGUMENTS = {
    "validate_definition": frozenset({"document", "definition_text"}),
    "publish_definition": frozenset(
        {"request_id", "document", "definition_text"}),
    "put_artifact": frozenset({"request_id", "text", "base64", "media_type"}),
    "read_artifact": frozenset({"artifact_id", "sha256", "size", "media_type"}),
    "create_run": frozenset({"definition_ref", "inputs", "request_id"}),
    "get_resource": frozenset({"kind", "identifier"}),
    "list_runs": frozenset({"status", "limit"}),
    "list_resources": frozenset({"run_id"}),
    "read_events": frozenset({"run_id", "after", "limit"}),
    "get_report": frozenset({"run_id"}),
    "set_run_intent": frozenset(
        {"run_id", "desired", "expected_revision", "request_id"}),
    "read_trace": frozenset({"run_id", "node_id", "candidate_id"}),
    "read_execution": frozenset(
        {"run_id", "worker_id", "epoch", "delivery_key", "after", "limit"}),
    "read_requests": frozenset({"request_id", "run_id", "after", "limit"}),
}

# Raw tools/call arguments for the current network request. The SDK applies
# Python defaults before calling a tool, so this is the only place an explicit
# empty string can be distinguished from an absent key.
_CURRENT_ARGUMENTS: ContextVar[Mapping[str, Any] | None] = ContextVar(
    "agentgenome_mcp_arguments", default=None)


def bind_request_arguments(arguments: Mapping[str, Any] | None) -> Any:
    return _CURRENT_ARGUMENTS.set(arguments)


def reset_request_arguments(token: Any) -> None:
    _CURRENT_ARGUMENTS.reset(token)


def current_request_arguments() -> Mapping[str, Any] | None:
    return _CURRENT_ARGUMENTS.get()


def _failed(operation: str, error: ControlPlaneError) -> dict[str, Any]:
    return {"ok": False, "operation": operation,
            "error": {"code": error.code, "message": error.message,
                      "details": error.details}}


def _definition_document(document: dict[str, Any] | None,
                         definition_text: str) -> dict[str, Any]:
    """Transport decoding only; Api remains the authority for Graph semantics.

    Exactly one source. The network service accepts uploaded content only;
    a caller-supplied path is never read here.
    """
    # An empty string is the tool-schema default, meaning "not supplied".
    # Keeping the annotation a plain `str` stops the MCP SDK from JSON-parsing
    # uploaded text into an object before this adapter sees the exact bytes.
    definition_text = definition_text or ""
    text_source: str | None = definition_text if definition_text else None
    sources = [value is not None for value in (document, text_source)]
    if sum(sources) != 1:
        raise ControlPlaneError(
            "invalid_argument",
            "pass exactly one of document or definition_text")
    if document is not None:
        return {"document": document}
    return {"definition_text": text_source}


def build_server(*, api: Api, actor_resolver: Any,
                 middleware: Any = None, trace: Any = None) -> MCPServer:
    """Build the MCP adapter around the running service's Api.

    The network service injects its own Api plus a per-request actor resolver;
    a request-supplied identity is never used. Trace tools are finite reads
    over the same service projection; none of them waits for a run to finish.
    """
    def actor() -> Actor:
        return actor_resolver()

    server = MCPServer(name=SERVER_NAME, version="0.1.0",
                       instructions=SERVER_INSTRUCTIONS,
                       middleware=list(middleware or ()))

    def call(operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Return the same envelope the JSON transport returns.

        Both transports share the explicit operation allowlist and adapter,
        so argument rejection, CAS and idempotency cannot drift apart.
        """
        try:
            return dispatch(api, actor(), operation, arguments, trace=trace)
        except ControlPlaneError as error:  # identity resolution failed
            return _failed(operation, error)

    def definition_arguments(document: dict[str, Any] | None,
                             definition_text: str) -> dict[str, Any]:
        if document is not None:
            arguments = {"document": document}
            if definition_text:
                # Preserve the conflict so the shared adapter rejects it.
                arguments["definition_text"] = definition_text
            return arguments
        if definition_text:
            # An empty default means "not supplied"; omitting the key keeps
            # the adapter's exactly-one-source rule authoritative.
            return {"definition_text": definition_text}
        return {}

    @server.tool()
    def validate_definition(document: dict[str, Any] | None = None,
                            definition_text: str = "") -> dict[str, Any]:
        """Validate a Graph definition and return the normalized body and
        digest. Pass document (object) or definition_text (uploaded YAML).
        Applies the same checks publish applies, without creating a revision."""
        return call("validate_definition",
                    _definition_document(document, definition_text))

    @server.tool()
    def publish_definition(request_id: str,
                           document: dict[str, Any] | None = None,
                           definition_text: str = "") -> dict[str, Any]:
        """Publish an immutable definition revision.

        Pass the definition's base_revision inside the document; a stale base
        revision is rejected. Re-publishing identical content is a no-op.
        Pass document (object) or definition_text (uploaded YAML). Reuse the
        same request_id when retrying one write.
        """
        arguments = _definition_document(document, definition_text)
        arguments["request_id"] = request_id
        return call("publish_definition", arguments)

    @server.tool()
    def put_artifact(request_id: str, text: str = "",
                     base64: str = "",
                     media_type: str = "text/plain") -> dict[str, Any]:
        """Store text or base64 bytes as a content-addressed artifact.

        Exactly one source. An omitted source is an empty string, so a request
        that supplies neither is rejected rather than guessed.
        """
        arguments: dict[str, Any] = {"request_id": request_id,
                                     "media_type": media_type}
        supplied = current_request_arguments()
        if supplied is None:
            if text:
                arguments["text"] = text
            if base64:
                arguments["base64"] = base64
        else:
            if "text" in supplied:
                arguments["text"] = text
            if "base64" in supplied:
                arguments["base64"] = base64
        return call("put_artifact", arguments)

    @server.tool()
    def read_artifact(artifact_id: str, sha256: str, size: int,
                      media_type: str = "text/plain") -> dict[str, Any]:
        """Read an artifact by its exact reference.

        Returns UTF-8 `text` when the bytes decode, otherwise `base64`.
        """
        return call("read_artifact",
                    {"id": artifact_id, "sha256": sha256, "size": size,
                     "media_type": media_type})

    @server.tool()
    def create_run(definition_ref: str, inputs: dict[str, Any],
                   request_id: str) -> dict[str, Any]:
        """Submit a run against a frozen definition revision.

        Inputs must satisfy that Graph's input_schema. For artifact inputs,
        pass each WHOLE object returned by put_artifact: id, sha256, size and
        media_type. Graph bindings such as {run_input: ...} or {from: ...}
        belong inside Graph nodes, never around a run's artifact input.
        """
        return call("create_run",
                    {"definition_ref": definition_ref, "inputs": inputs,
                     "request_id": request_id})

    @server.tool()
    def get_resource(kind: str, identifier: str) -> dict[str, Any]:
        """Read one committed resource view (definition, run, node, worker,
        candidate, expansion_request, expansion, message or artifact)."""
        return call("get_resource",
                    {"kind": kind, "identifier": identifier})

    @server.tool()
    def list_runs(status: str | None = None, limit: int = 200) -> dict[str, Any]:
        """List runs visible to this teacher, newest first, for reconnecting
        after a new conversation starts."""
        arguments: dict[str, Any] = {"limit": limit}
        if status is not None:
            arguments["status"] = status
        return call("list_runs", arguments)

    @server.tool()
    def list_resources(run_id: str) -> dict[str, Any]:
        """Read the committed run snapshot: run, nodes, workers, candidates,
        expansions and reviews."""
        return call("list_resources", {"run_id": run_id})

    @server.tool()
    def read_events(run_id: str, after: int = 0,
                    limit: int = 100) -> dict[str, Any]:
        """Read committed events after a cursor; an expired cursor asks for a
        snapshot re-read instead of silently skipping events."""
        return call("read_events",
                    {"run_id": run_id, "after": after, "limit": limit})

    @server.tool()
    def get_report(run_id: str) -> dict[str, Any]:
        """Read the run report, keeping candidates and accepted results apart."""
        return call("get_report", {"run_id": run_id})

    @server.tool()
    def set_run_intent(run_id: str, desired: str, expected_revision: int,
                       request_id: str) -> dict[str, Any]:
        """Record pause/resume/cancel intent. Intent acceptance is not proof
        that execution has already stopped."""
        return call("set_run_intent",
                    {"run_id": run_id, "desired": desired,
                     "expected_revision": expected_revision,
                     "request_id": request_id})

    @server.tool()
    def read_trace(run_id: str, node_id: str | None = None,
                   candidate_id: str | None = None) -> dict[str, Any]:
        """Read one consistent trace snapshot: task tree, decomposition,
        cross-task dependencies, workers, candidates, reviews, decisions,
        waiting explanations and recent committed events with a cursor."""
        arguments: dict[str, Any] = {"run_id": run_id}
        if node_id is not None:
            arguments["node_id"] = node_id
        if candidate_id is not None:
            arguments["candidate_id"] = candidate_id
        return call("read_trace", arguments)

    @server.tool()
    def read_execution(run_id: str, worker_id: str, epoch: int,
                       delivery_key: str, after: str = "",
                       limit: int = 100) -> dict[str, Any]:
        """Read a bounded page of one delivery's execution log. The cursor is
        the opaque token from the previous page; missing fields stay missing."""
        arguments: dict[str, Any] = {
            "run_id": run_id, "worker_id": worker_id, "epoch": epoch,
            "delivery_key": delivery_key, "limit": limit}
        if after:
            arguments["after"] = after
        return call("read_execution", arguments)

    @server.tool()
    def read_requests(request_id: str | None = None, run_id: str | None = None,
                      after: str | int = "", limit: int = 100) -> dict[str, Any]:
        """Read bounded service request diagnostics, including operations that
        never committed. Pass the previous next_cursor token as `after`.
        Never contains credentials, bodies or model text."""
        arguments: dict[str, Any] = {"limit": limit}
        if after not in ("", 0, None):
            arguments["after"] = after
        if request_id is not None:
            arguments["request_id"] = request_id
        if run_id is not None:
            arguments["run_id"] = run_id
        return call("read_requests", arguments)

    return server
