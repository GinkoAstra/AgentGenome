"""HTTP Worker tool protocol for one delivery credential.

The service authenticates a delivery token, resolves the trusted binding
written by the Backend (run/node/worker/epoch/delivery, review, decision,
workspace), re-checks the live Worker epoch against the committed API state and
then routes the request through the same ``WorkerToolbox`` used by tests and
scripted Agents.

Artifact bytes are uploaded by the Pi client from its own bound workspace; the
service decodes the supplied base64 and stores those exact bytes. No Api, data
directory, bridge argv or executable path comes from the request or from the
Agent process, and the service never reads a client-supplied filesystem path.
The model may only choose a granted tool name and its schema-validated
arguments; identity, workspace and review bindings are fixed server-side.
"""
from __future__ import annotations

import base64
import binascii
from typing import Any, Mapping

from . import resources as R
from .api import Api
from .tools import ToolError, WorkerToolbox

MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
CONTEXT_FORMAT = "agentgenome-worker-tool-context/2"


def _worker(api: Api, binding: Mapping[str, Any]) -> dict[str, Any]:
    actor = R.Actor(binding["worker_id"], {"worker"},
                    run_ids={binding["run_id"]},
                    worker_id=binding["worker_id"], epoch=binding["epoch"])
    worker = api.get_resource(actor, "worker", binding["worker_id"])
    if worker["run_id"] != binding["run_id"]:
        raise ToolError("unauthorized", "worker belongs to another run")
    if worker["node_id"] != binding["node_id"]:
        raise ToolError("unauthorized", "worker is not bound to this node")
    if worker["epoch"] != binding["epoch"]:
        raise ToolError("stale_epoch", "worker epoch is not current",
                        current_epoch=worker["epoch"],
                        context_epoch=binding["epoch"])
    return worker


def _toolbox(api: Api, binding: Mapping[str, Any],
             worker: Mapping[str, Any]) -> WorkerToolbox:
    review = binding.get("review") or {}
    decision = binding.get("decision") or {}
    decision_ids = decision.get("decision_ids")
    if decision_ids is None and isinstance(decision.get("decision_id"), str):
        decision_ids = [decision["decision_id"]]
    if decision_ids is not None and (
            not isinstance(decision_ids, list) or
            not all(isinstance(item, str) and item for item in decision_ids)):
        raise ToolError("unauthorized", "delivery decision binding is invalid")
    prefix = "worker:" + R.digest({
        "worker_id": binding["worker_id"], "epoch": binding["epoch"],
        "delivery_key": binding["delivery_key"],
    }).split(":", 1)[1][:20]
    return WorkerToolbox(
        api,
        run_id=binding["run_id"], node_id=binding["node_id"],
        worker_id=binding["worker_id"], epoch=binding["epoch"],
        tool_profile=worker["tool_profile"],
        reviewer_ref=review.get("reviewer_ref"),
        review_target=review.get("target_node_id"),
        review_candidate=review.get("candidate_id"),
        review_round=review.get("candidate_round"),
        decision_ids=decision_ids,
        request_prefix=prefix,
        contract_ref=binding.get("contract_ref"),
    )


def _uploaded_content(request: Mapping[str, Any]) -> bytes:
    """Decode artifact bytes sent by the Worker client.

    The path in ``arguments`` is only a label for the model and media type; the
    service never opens it. Base64 is the single delivery form so arbitrary
    bytes survive the JSON boundary exactly.
    """
    if "content_base64" not in request:
        raise ToolError("invalid_arguments",
                        "publish_artifact requires content_base64")
    encoded = request.get("content_base64")
    if not isinstance(encoded, str):
        raise ToolError("invalid_arguments",
                        "content_base64 must be a base64 string")
    try:
        content = base64.b64decode(encoded, validate=True) if encoded else b""
    except (binascii.Error, ValueError) as error:
        raise ToolError("invalid_arguments",
                        "content_base64 is not valid base64") from error
    if len(content) > MAX_ARTIFACT_BYTES:
        raise ToolError("material_too_large",
                        f"artifact exceeds {MAX_ARTIFACT_BYTES} bytes")
    return content


def _feedback_ref(toolbox: WorkerToolbox, arguments: Mapping[str, Any],
                  *, call_id: str) -> dict[str, Any]:
    feedback = arguments.get("feedback_ref")
    if not isinstance(feedback, Mapping) or "text" not in feedback:
        return dict(arguments)
    if set(feedback) - {"text", "media_type"}:
        raise ToolError("invalid_arguments",
                        "feedback_ref must be an artifact reference or feedback text")
    text = feedback["text"]
    if not isinstance(text, str):
        raise ToolError("invalid_arguments", "feedback text must be a string")
    media_type = feedback.get("media_type", "text/plain")
    if not isinstance(media_type, str):
        raise ToolError("invalid_arguments", "feedback media_type must be a string")
    artifact = toolbox.put_feedback(text, media_type, call_id=call_id)
    return {**dict(arguments), "feedback_ref": artifact}


def execute(api: Api, binding: Mapping[str, Any],
            request: Any) -> dict[str, Any]:
    """Run one Worker tool call for an already-authenticated delivery."""
    if not isinstance(request, Mapping):
        raise ToolError("invalid_arguments", "request must be a JSON object")
    tool = request.get("tool")
    call_id = request.get("call_id")
    arguments = request.get("arguments", {})
    if not isinstance(tool, str) or not tool:
        raise ToolError("invalid_arguments", "tool is required")
    if not isinstance(call_id, str) or not call_id:
        raise ToolError("invalid_arguments", "call_id is required")
    if not isinstance(arguments, Mapping):
        raise ToolError("invalid_arguments", "arguments must be a JSON object")
    worker = _worker(api, binding)
    toolbox = _toolbox(api, binding, worker)
    granted = {item["name"] for item in toolbox.schemas()}
    if tool not in granted:
        raise ToolError("unauthorized", "tool is not granted for this delivery",
                        tool=tool)
    if tool == "node.publish_artifact":
        result = toolbox.publish_artifact(
            arguments, call_id=call_id, content=_uploaded_content(request))
    elif tool == "node.submit_review":
        toolbox.preflight(tool, arguments, call_id=call_id)
        prepared = _feedback_ref(toolbox, arguments, call_id=call_id)
        result = toolbox.invoke(tool, prepared, call_id=call_id)
    else:
        result = toolbox.invoke(tool, dict(arguments), call_id=call_id)
    return {"tool": tool, "version": result["version"],
            "output": result["output"]}
