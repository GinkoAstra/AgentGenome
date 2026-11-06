"""Delivery ordering rule for a Node with parent decisions.

The Scheduler owns decision facts and reply messages; the Controller owns
delivery. Both must apply exactly the same rule, otherwise a queued ordinary
message can preempt a parent's answer, or a consumed answer can hold a Worker
that is no longer waiting.
"""
from __future__ import annotations

from typing import Any, Mapping

from .resources import digest

# ``decision_id`` accepts up to 128 characters and has no path-safe character
# restriction, so derived delivery keys never embed it verbatim: they carry a
# bounded stable digest while the payload and facts keep the original id.
_SUFFIX_LENGTH = 24


def _decision_suffix(decision_id: str) -> str:
    return digest(decision_id).split(":", 1)[1][:_SUFFIX_LENGTH]


def decision_key(decision_id: str) -> str:
    return f"decision:{_decision_suffix(decision_id)}"


def decision_reply_key(decision_id: str) -> str:
    return f"decision-reply:{_decision_suffix(decision_id)}"


def decision_continue_prefix(decision_id: str) -> str:
    return f"decision-continue:{_decision_suffix(decision_id)}:"


def decision_delivery_gate(worker: Mapping[str, Any],
                           messages: list[Mapping[str, Any]],
                           decisions: list[Mapping[str, Any]]
                           ) -> set[str] | None:
    """Delivery keys a Worker may receive, or ``None`` when ungated.

    - An open decision blocks every ordinary delivery.
    - A resolved reply blocks them until the reply itself is delivered.
    - A reply is owed until a durable delivery of its key exists. That covers
      the window where the request was made in an earlier epoch: the reply was
      never consumed, so ordinary messages still may not run ahead of it.
    """
    node_decisions = [item for item in decisions
                      if item.get("node_id") == worker["node_id"]]
    if not node_decisions:
        return None
    if any(item.get("state") == "open" for item in node_decisions):
        return set()
    delivered = {message["delivery_key"] for message in messages
                 if message.get("delivery_state") == "delivered"}
    replies = {decision_reply_key(item["decision_id"])
               for item in node_decisions
               if item.get("state") == "resolved"
               and decision_reply_key(item["decision_id"]) not in delivered}
    return replies or None
