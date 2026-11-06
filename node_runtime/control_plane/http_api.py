"""HTTP/MCP operation adapter; all resource rules remain in Api.

The explicit operation allowlist is shared by the two transports. No generic
getattr(Api, user_operation), client-selected Actor, or local filesystem reads.
"""
from __future__ import annotations

import base64
import binascii
from typing import Any, Mapping

import yaml

from .api import Api
from .resources import Actor, ControlPlaneError


PUBLIC_OPERATIONS = (
    "validate_definition", "publish_definition", "put_artifact",
    "read_artifact", "create_run", "get_resource", "list_runs",
    "list_resources", "read_events", "get_report", "set_run_intent",
    "read_trace", "read_execution", "read_requests",
)

IDENTITY_FIELDS = frozenset({"actor", "actor_id", "role", "roles"})

_ERROR_STATUS = {
    "unauthorized": 401,
    "not_found": 404,
    "revision_conflict": 409,
    "idempotency_conflict": 409,
    "decision_conflict": 409,
    "candidate_mismatch": 409,
    "required_children_open": 409,
    "dependency_cycle": 409,
    "feedback_pending": 409,
    "epoch_in_flight": 409,
    "epoch_unreconciled": 409,
    "artifact_mismatch": 409,
    "cursor_expired": 409,
    "invalid_argument": 400,
    "stale_epoch": 400,
    "stale_observation": 400,
    "reviewer_undeclared": 400,
    "budget_exhausted": 400,
    "capability_unsupported": 400,
    "service_unavailable": 503,
    "invalid_arguments": 400,
    "path_outside_workspace": 400,
    "path_not_found": 404,
    "path_symlink": 400,
    "path_not_file": 400,
    "workspace_unconfigured": 400,
    "material_too_large": 400,
}


def ok_envelope(operation: str, result: Any) -> dict[str, Any]:
    return {"ok": True, "operation": operation, "result": result}


def error_envelope(operation: str,
                   error: ControlPlaneError) -> dict[str, Any]:
    return {"ok": False, "operation": operation,
            "error": {"code": error.code, "message": error.message,
                      "details": error.details}}


def status_for_error(code: str) -> int:
    return _ERROR_STATUS.get(code, 500)


def decode_definition(document: Any = None,
                      definition_text: Any = None) -> dict[str, Any]:
    """Adapter-layer YAML decoding; Graph semantics stay in Api."""
    if document is not None and definition_text is not None:
        raise ControlPlaneError(
            "invalid_argument",
            "pass exactly one of document or definition_text")
    if definition_text is not None:
        if not isinstance(definition_text, str) or not definition_text.strip():
            raise ControlPlaneError(
                "invalid_argument",
                "definition_text must be nonempty YAML text")
        try:
            loaded = yaml.safe_load(definition_text)
        except yaml.YAMLError as error:
            raise ControlPlaneError(
                "invalid_argument",
                "definition_text is not valid YAML") from error
        if not isinstance(loaded, dict):
            raise ControlPlaneError(
                "invalid_argument",
                "definition_text must contain a YAML/JSON object")
        return loaded
    if document is None:
        raise ControlPlaneError(
            "invalid_argument",
            "pass exactly one of document or definition_text")
    if not isinstance(document, dict):
        raise ControlPlaneError("invalid_argument",
                                "document must be a JSON object")
    return document


def dispatch(api: Api, actor: Actor, operation: str,
             arguments: Mapping[str, Any],
             *, trace: Any = None) -> dict[str, Any]:
    """Decode a public operation and return the existing ok/error envelope.

    Definition bodies are content (document or explicit YAML text), never a
    client path to open on the server. Artifact transport supports exact bytes.
    Preserve request_id and expected_revision; reject extra identity fields.
    Leave scheduling/worker-only mutations out of the public allowlist.
    Trace reads are the same authorized projection the HTTP watch route uses.
    """
    try:
        if operation not in PUBLIC_OPERATIONS:
            raise ControlPlaneError(
                "invalid_argument", "unknown operation",
                operation=str(operation)[:128])
        if not isinstance(arguments, Mapping):
            raise ControlPlaneError("invalid_argument",
                                    "request body must be a JSON object")
        result = _run(api, actor, operation, arguments, trace)
        return ok_envelope(operation, result)
    except ControlPlaneError as error:
        return error_envelope(operation, error)


def _trace_service(trace: Any) -> Any:
    if trace is None:
        raise ControlPlaneError(
            "internal", "trace service is not configured for this transport")
    return trace


def _run(api: Api, actor: Actor, operation: str,
         arguments: Mapping[str, Any], trace: Any = None) -> Any:
    if operation == "validate_definition":
        _only(arguments, operation, {"document", "definition_text"})
        document = decode_definition(arguments.get("document"),
                                     arguments.get("definition_text"))
        return api.validate_definition(actor, document)

    if operation == "publish_definition":
        _only(arguments, operation, {"request_id", "document", "definition_text"})
        request_id = _required(arguments, "request_id")
        document = decode_definition(arguments.get("document"),
                                     arguments.get("definition_text"))
        return api.publish_definition(actor, document, request_id)

    if operation == "put_artifact":
        _only(arguments, operation, {"request_id", "text", "base64", "media_type"})
        request_id = _required(arguments, "request_id")
        content = artifact_content(arguments)
        media_type = arguments.get("media_type", "text/plain")
        return api.put_artifact(actor, request_id, content, media_type)

    if operation == "read_artifact":
        _only(arguments, operation, {"id", "sha256", "size", "media_type"})
        ref = {"id": _required(arguments, "id"),
               "sha256": _required(arguments, "sha256"),
               "size": _required(arguments, "size"),
               "media_type": arguments.get("media_type", "text/plain")}
        content = api.read_artifact(actor, ref)
        try:
            return {"artifact": ref, "text": content.decode("utf-8")}
        except UnicodeDecodeError:
            return {"artifact": ref,
                    "base64": base64.b64encode(content).decode("ascii")}

    if operation == "create_run":
        _only(arguments, operation, {"definition_ref", "inputs", "request_id"})
        return api.create_run(actor, _required(arguments, "definition_ref"),
                              _required(arguments, "inputs"),
                              _required(arguments, "request_id"))

    if operation == "get_resource":
        _only(arguments, operation, {"kind", "identifier"})
        return api.get_resource(actor, _required(arguments, "kind"),
                                _required(arguments, "identifier"))

    if operation == "list_runs":
        _only(arguments, operation, {"status", "limit"})
        return api.list_runs(actor, status=arguments.get("status"),
                             limit=arguments.get("limit", 200))

    if operation == "list_resources":
        _only(arguments, operation, {"run_id"})
        return api.list_resources(actor, _required(arguments, "run_id"))

    if operation == "read_events":
        _only(arguments, operation, {"run_id", "after", "limit"})
        return api.read_events(actor, _required(arguments, "run_id"),
                               after=arguments.get("after", 0),
                               limit=arguments.get("limit", 100))

    if operation == "get_report":
        _only(arguments, operation, {"run_id"})
        return api.get_report(actor, _required(arguments, "run_id"))

    if operation == "set_run_intent":
        _only(arguments, operation,
              {"run_id", "desired", "expected_revision", "request_id"})
        return api.set_run_intent(actor, _required(arguments, "run_id"),
                                  _required(arguments, "desired"),
                                  _required(arguments, "expected_revision"),
                                  _required(arguments, "request_id"))

    if operation == "read_trace":
        _only(arguments, operation, {"run_id", "node_id", "candidate_id"})
        return _trace_service(trace).snapshot(
            actor, _required(arguments, "run_id"),
            node_id=arguments.get("node_id"),
            candidate_id=arguments.get("candidate_id"))

    if operation == "read_execution":
        _only(arguments, operation,
              {"run_id", "worker_id", "epoch", "delivery_key", "after", "limit"})
        return _trace_service(trace).execution(
            actor, _required(arguments, "run_id"),
            _required(arguments, "worker_id"),
            _required(arguments, "epoch"),
            _required(arguments, "delivery_key"),
            after=arguments.get("after"),
            limit=arguments.get("limit", 100))

    if operation == "read_requests":
        _only(arguments, operation,
              {"request_id", "run_id", "after", "limit"})
        return _trace_service(trace).requests(
            actor, request_id=arguments.get("request_id"),
            run_id=arguments.get("run_id"),
            after=arguments.get("after", 0),
            limit=arguments.get("limit", 100))

    raise ControlPlaneError("invalid_argument", "unknown operation",
                            operation=str(operation)[:128])


def _only(arguments: Mapping[str, Any], operation: str,
          allowed: set[str]) -> None:
    extra = sorted(set(arguments) - allowed)
    if extra:
        detail: dict[str, Any] = {"operation": operation, "fields": extra}
        if set(extra) & IDENTITY_FIELDS:
            detail["reason"] = "identity comes from the bearer token"
        raise ControlPlaneError(
            "invalid_argument", "unknown argument for operation", **detail)


def _required(arguments: Mapping[str, Any], name: str) -> Any:
    if name not in arguments or arguments[name] is None:
        raise ControlPlaneError("invalid_argument",
                                f"missing required argument {name}",
                                field=name)
    return arguments[name]


def artifact_content(arguments: Mapping[str, Any]) -> str | bytes:
    text = arguments.get("text")
    encoded = arguments.get("base64")
    if text is not None and encoded is not None:
        raise ControlPlaneError("invalid_argument",
                                "pass exactly one of text or base64")
    if text is not None:
        if not isinstance(text, str):
            raise ControlPlaneError("invalid_argument", "text must be a string")
        return text
    if encoded is not None:
        if not isinstance(encoded, str):
            raise ControlPlaneError("invalid_argument", "base64 must be a string")
        try:
            return base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ControlPlaneError("invalid_argument",
                                    "base64 is not valid base64") from error
    raise ControlPlaneError("invalid_argument",
                            "pass exactly one of text or base64")
