"""Pure task-state proposals; authority stays in Api.commit_plan.

Codex scaffold for W01. Scheduler supplies committed facts/read versions and
submits returned mutations with its full read_set. This module does not call
Backend, SQL, MCP, tools or model APIs. API is still responsible for atomic
uniqueness, permissions, revision checks and idempotent application receipts.

The functions here only translate an already committed candidate/review fact
into the next existing Node state. A mismatch raises ``ValueError`` instead of
returning an empty proposal, so corruption cannot masquerade as "nothing to do".
An empty list means the fact is already applied, cancelled, or not applicable
under the current Run intent.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from .decisions import decision_delivery_gate

_TERMINAL_NODE_STATES = frozenset({"succeeded", "failed", "cancelled"})
_REVIEWABLE_NODE_STATES = frozenset({"running", "awaiting_review"})


def _association(node: Mapping[str, Any], candidate: Mapping[str, Any]) -> None:
    """Reject a candidate that is not the Node's exact committed fact."""
    if candidate.get("run_id") != node.get("run_id"):
        raise ValueError("candidate belongs to another run")
    if candidate.get("node_id") != node.get("node_id"):
        raise ValueError("candidate belongs to another node")
    if candidate.get("round") != node.get("round"):
        raise ValueError("candidate round is not the Node's current round")
    if candidate.get("contract_ref") != node.get("contract_ref"):
        raise ValueError("candidate contract does not match the frozen Node contract")


def candidate_transition(
    node: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *, run_desired: str,
) -> list[dict[str, Any]]:
    """W01: propose awaiting_review for an unapplied persisted candidate.

    Check exact task/round/contract association and nonterminal state. No new
    execution on paused/cancelled run. Return [] only for a genuine no-op;
    mismatch must not masquerade as success. Output uses current plan mutation
    format; Scheduler records application with state in one API transaction.
    """
    _association(node, candidate)
    if run_desired != "running":
        return []
    if node.get("state") in _TERMINAL_NODE_STATES:
        return []
    if node.get("state") == "awaiting_review":
        return []
    if node.get("state") != "running":
        return []
    return [{"kind": "node", "op": "update", "id": node["node_id"],
             "fields": {"state": "awaiting_review"}}]


def review_transition(
    node: Mapping[str, Any],
    candidate: Mapping[str, Any],
    review: Mapping[str, Any],
    *, run_desired: str,
) -> list[dict[str, Any]]:
    """W01: propose accept/revise state application from an exact decision.

    Accept permits successors only after committed application. Revise reuses
    task and current execution generation. Expand uses existing persisted
    expansion flow. Cancellation wins over an unapplied old decision; an
    already-applied result is retained as history. No business scoring here.
    """
    _association(node, candidate)
    if review.get("run_id") != node.get("run_id"):
        raise ValueError("review belongs to another run")
    if review.get("node_id") != node.get("node_id"):
        raise ValueError("review belongs to another node")
    if review.get("candidate_id") != candidate.get("candidate_id"):
        raise ValueError("review belongs to another candidate")
    verdict = review.get("verdict")
    if verdict != candidate.get("decision"):
        raise ValueError("review verdict is not the candidate's committed decision")
    if verdict not in ("accept", "revise", "expand"):
        raise ValueError("review has no applicable verdict")
    if run_desired != "running":
        # Pause/cancel wins over an unapplied decision. The fact stays durable
        # and is re-evaluated after a resume; a cancelled Node keeps history.
        return []
    if node.get("state") in _TERMINAL_NODE_STATES:
        return []
    if verdict == "expand":
        # The existing expansion-request channel applies the plan; review
        # application adds no second state path.
        return []
    if node.get("state") != "awaiting_review":
        # Either the candidate application is still pending (running) or a
        # concurring transition already consumed it. Re-evaluate next pass.
        return []
    if verdict == "accept":
        return [{"kind": "node", "op": "update", "id": node["node_id"],
                 "fields": {"state": "succeeded",
                            "accepted_candidate_ref": candidate["candidate_id"]}}]
    return [{"kind": "node", "op": "update", "id": node["node_id"],
             "fields": {"state": "running"}}]


def current_candidate(
    node: Mapping[str, Any],
    candidates: Iterable[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    """The candidate fact for a Node's current round and contract version.

    A superseded version's candidate keeps its history but no longer owns the
    Node's review or continuation path: the replacement execution works under
    the applied local contract and must not be gated by the old verdict.
    """
    for candidate in candidates:
        if (candidate.get("node_id") == node.get("node_id")
                and candidate.get("round") == node.get("round")
                and candidate.get("contract_ref") == node.get("contract_ref")):
            return candidate
    return None


def undecided_candidate(
    node: Mapping[str, Any],
    candidates: Iterable[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    """An undecided candidate fact still owns this Node's review path."""
    candidate = current_candidate(node, candidates)
    if candidate is not None and candidate.get("decision") is None:
        return candidate
    return None


def continuation_blocker(
    node: Mapping[str, Any],
    candidates: Iterable[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    """A candidate fact that still forbids an ordinary idle continuation.

    An undecided candidate owns the review path. An accepted candidate whose
    state application has not run yet must not be bypassed by a new turn
    either. A revise/expand decision has already been applied by the time the
    Node is running again, so rework may continue normally.
    """
    candidate = current_candidate(node, candidates)
    if candidate is None:
        return None
    if candidate.get("decision") is None:
        return candidate
    if candidate.get("decision") == "accept":
        return candidate
    return None


def candidate_delivery_gate(
    node: Mapping[str, Any] | None,
    candidates: Iterable[Mapping[str, Any]],
) -> set[str] | None:
    """Delivery keys an ordinary Worker turn may receive, or ``None``.

    While a candidate fact is undecided, or its decision has not been applied
    to the Node state yet, no queued ordinary continuation may reach the
    Worker. A terminal Node receives nothing new. ``None`` means ungated.
    """
    if node is None:
        return None
    if node.get("state") in _TERMINAL_NODE_STATES:
        return set()
    candidate = current_candidate(node, candidates)
    if candidate is None:
        return None
    if candidate.get("decision") is None:
        return set()
    if node.get("state") == "awaiting_review":
        # accept/revise fact is durable but the Scheduler has not applied it.
        return set()
    return None


def revision_key(candidate_id: str, verdict: str) -> str:
    """Delivery key of the revision feedback for one reviewed candidate."""
    return f"revise:{candidate_id}:{verdict}"


def acceptance_key(worker_id: str, epoch: int) -> str:
    """Delivery key of the fresh execution's local-contract hand-off.

    It deliberately reuses the Worker start key: a replacement epoch must
    receive exactly one start, and a normal start payload must never follow
    the revised contract into the same execution.
    """
    return f"start:{worker_id}:{epoch}"


def revision_delivery_gate(
    node: Mapping[str, Any] | None,
    candidates: Iterable[Mapping[str, Any]],
    messages: Iterable[Mapping[str, Any]],
    *,
    epoch: int | None = None,
) -> set[str] | None:
    """Allow only the owed revision feedback, or ``None`` when consumed.

    A revise/expand decision is applied to the Node before its feedback is
    enqueued and delivered. Until that message has a durable delivery for the
    Worker's current epoch, ordinary messages may not run ahead of it; if it
    has not been enqueued yet the gate allows only its key, so nothing else is
    delivered either. Enqueue alone is not consumption: only a delivered
    message satisfies the gate. ``None`` means no feedback is owed.
    """
    if node is None:
        return None
    if node.get("state") in _TERMINAL_NODE_STATES:
        return None
    candidate = current_candidate(node, candidates)
    if candidate is None:
        return None
    verdict = candidate.get("decision")
    if verdict not in ("revise", "expand"):
        return None
    key = revision_key(candidate["candidate_id"], verdict)
    delivered = {message.get("delivery_key") for message in messages
                 if message.get("delivery_state") == "delivered"
                 and (epoch is None or message.get("epoch") == epoch)}
    if key in delivered:
        return None
    return {key}


def delivery_gate(
    worker: Mapping[str, Any],
    messages: Iterable[Mapping[str, Any]],
    node: Mapping[str, Any] | None,
    candidates: Iterable[Mapping[str, Any]],
    decisions: Iterable[Mapping[str, Any]],
    *,
    progress: Mapping[str, Any] | None = None,
    acceptance: Mapping[str, Any] | None = None,
) -> set[str] | None:
    """One effective delivery rule for Controller dispatch and Scheduler slots.

    Priority is explicit, never an intersection of disjoint sets: a resolved
    parent decision reply is delivered first, then the candidate's owed
    revision feedback, then ordinary continuation. An unstalled Worker stays
    ungated. A stalled Goal progress window with no resolved trigger request
    blocks ordinary work and revision feedback immediately (before the
    Scheduler has created the parent request); the resolved trigger releases
    only that gate and never clears the evidence count. An empty set is a hard
    block; ``None`` means the Worker is ungated. Both callers must use this
    function so a Worker the Controller cannot feed is never counted as
    occupying a capacity slot.
    """
    messages = list(messages)
    decision_gate = decision_delivery_gate(worker, messages, list(decisions))
    candidate_gate = candidate_delivery_gate(node, candidates)
    revision_gate = revision_delivery_gate(node, candidates, messages,
                                           epoch=worker.get("epoch"))
    if candidate_gate == set() or decision_gate == set():
        return set()
    # A pending local contract is not dispatchable work: the superseded
    # execution is stopping and the Node's review path is held. An owed parent
    # reply still crosses, exactly as it does for a revision decision.
    pending_revision = bool(acceptance and acceptance.get("pending"))
    local = bool(acceptance and acceptance.get("local"))
    execution_ref = (worker.get("context") or {}).get("contract_ref")
    if pending_revision or (local and execution_ref
                            != acceptance.get("contract_ref")):
        return decision_gate if decision_gate is not None else set()
    if local:
        key = acceptance_key(worker.get("worker_id"), worker.get("epoch"))
        delivered = any(
            message.get("delivery_key") == key
            and message.get("delivery_state") == "delivered"
            and (message.get("payload") or {}).get("kind") == "acceptance_revised"
            for message in messages)
        if not delivered:
            # The new epoch must receive its full local contract and prior
            # feedback before any ordinary continuation.
            return {key}
    stalled = bool(progress and progress.get("enabled")
                   and progress.get("summary", {}).get("stalled"))
    if stalled and not progress.get("released"):
        # Only an owed decision reply may cross an unresolved trigger. The
        # request may not have been committed yet; ordinary work and revision
        # feedback still wait for that parent answer.
        return decision_gate if decision_gate is not None else set()
    if decision_gate is not None:
        return decision_gate
    if revision_gate is not None:
        return revision_gate
    return None
