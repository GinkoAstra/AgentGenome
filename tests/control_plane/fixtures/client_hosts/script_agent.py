"""Controlled Script-backend Agent for native-host acceptance.

This is a protocol fixture, not a second execution engine. It accepts one
candidate and one review per delivery using only the committed
WorkerToolbox operations, exactly like a real Agent using the same tools.
"""
from __future__ import annotations

import hashlib
import json

from node_runtime.control_plane.tools import WorkerToolbox


def _short(tag: str, value: str) -> str:
    return f"{tag}-{hashlib.sha256(value.encode()).hexdigest()[:32]}"


def _review_turn(message: dict) -> bool:
    return (message.get("role") == "reviewer"
            or message.get("kind") in ("review_start", "review_request"))


def _submit_candidate(box: WorkerToolbox, spec, round_number: int) -> None:
    body = json.dumps({"node_id": spec.node_id, "round": round_number},
                      sort_keys=True).encode()
    ref = box.publish_artifact(
        {"path": f"{spec.node_id}-r{round_number}.json",
         "media_type": "application/json"},
        call_id=f"publish-{spec.node_id}-{round_number}",
        content=body)["output"]
    box.invoke(
        "node.submit_candidate",
        {"candidate_id": f"candidate-{spec.node_id}-r{round_number}",
         "round": round_number, "output_refs": [ref]},
        call_id=f"candidate-{spec.node_id}-{round_number}")


def _submit_review(box: WorkerToolbox, spec, message: dict) -> None:
    candidate_id = message.get("candidate_id")
    candidate_round = message.get("candidate_round")
    turn = f"{spec.node_id}:{candidate_id}:accept"
    feedback = box.put_feedback(f"accept by {spec.worker_id}",
                                call_id=_short("feedback", turn))
    box.invoke(
        "node.submit_review",
        {"candidate_id": candidate_id, "candidate_round": candidate_round,
         "verdict": "accept", "feedback_ref": feedback},
        call_id=_short("review", turn))


def factory(api):
    def agent(spec, message, state):
        context = spec.context or {}
        if _review_turn(message):
            box = WorkerToolbox(
                api, run_id=spec.run_id, node_id=spec.node_id,
                worker_id=spec.worker_id, epoch=spec.epoch,
                tool_profile=spec.tool_profile,
                reviewer_ref=(message.get("reviewer_ref")
                              or context.get("reviewer_ref")),
                review_target=(message.get("target_node_id")
                               or context.get("target_node_id")),
                review_candidate=(message.get("candidate_id")
                                  or context.get("candidate_id")),
                review_round=(message.get("candidate_round")
                              or context.get("candidate_round")),
                contract_ref=context.get("contract_ref"))
            _submit_review(box, spec, message)
            return
        box = WorkerToolbox(
            api, run_id=spec.run_id, node_id=spec.node_id,
            worker_id=spec.worker_id, epoch=spec.epoch,
            tool_profile=spec.tool_profile,
            decision_ids=([message["decision_id"]]
                          if message.get("decision_id") else None),
            contract_ref=context.get("contract_ref"))
        _submit_candidate(box, spec, int(message.get("round") or 1))
    return agent
