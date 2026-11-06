"""Structured observations and contract revisions for W03-W05.

Scaffold data contracts, not a second database or a new event service.
API persists/validates these through its existing fact transaction. Backend
reports activities, evaluator reports progress, authorized parent proposes
local contract revisions. Scheduler applies task/binding transitions.

Dataclass 'frozen' prevents attribute reassignment only; API validation and
canonical persisted values establish immutability of nested payloads.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping

from .resources import (ARTIFACT_ID, WORKER_OBSERVED, artifact_ref,
                        optional_artifact_ref, require, require_acyclic,
                        require_int, require_object, require_text)

_ACTIVITY_FIELDS = frozenset({"activity_id", "parent_activity_id", "kind",
                              "observed", "usage", "evidence_ref"})
_ACTIVITY_KINDS = frozenset({"subagent", "background_tool"})
_ACTIVITY_USAGE_FIELDS = frozenset({"provider_calls", "tool_calls"})

_PROGRESS_FIELDS = frozenset({
    "evaluation_id", "node_id", "checkpoint_id", "evaluator_ref",
    "contract_ref", "comparison_ref", "evidence_ref", "result"})
_PROGRESS_RESULTS = frozenset({"improved", "not_improved", "unknown"})

_REVISION_FIELDS = frozenset({
    "contract_ref", "previous_ref", "upper_contract_ref", "origin",
    "criteria_ref", "authority_ref"})
_REVISION_ORIGINS = frozenset({"human_confirmed", "runtime_derived"})


def _detach(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _detach(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_detach(item) for item in value]
    return value


def _opaque_id(value: Any, label: str) -> str:
    return require_text(value, label, max_length=128)


def _content_ref(value: Any, label: str) -> str:
    require(isinstance(value, str) and ARTIFACT_ID.fullmatch(value) is not None,
            "invalid_argument",
            f"{label} must be sha256:<64 lowercase hex>")
    return value


@dataclass(frozen=True)
class ExecutionIdentity:
    """Trusted binding identity; epoch is not an Agent interaction count."""

    run_id: str
    node_id: str
    worker_id: str
    epoch: int


@dataclass(frozen=True)
class ActivityFact:
    """One owned internal execution in an observation, not a logical Node.

    Scope and ordering come from the existing worker_observed envelope:
    (run_id, node_id, worker_id, epoch, observation_id). Do not duplicate them
    per activity or let a model supply another binding. activity_id is stable
    within that execution epoch; parent_activity_id is another activity there.
    Usage contains known cumulative attribution only. Missing counts remain
    unknown; the API must not add these counts to inclusive Worker totals.
    """

    activity_id: str
    parent_activity_id: str | None
    kind: Literal["subagent", "background_tool"]
    observed: Literal["starting", "active", "idle", "stopped", "failed", "unknown"]
    usage: Mapping[str, Any]
    evidence_ref: Mapping[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        """W03a: detached JSON object, including explicit unknown parent."""
        document = {
            "activity_id": self.activity_id,
            "parent_activity_id": self.parent_activity_id,
            "kind": self.kind,
            "observed": self.observed,
            "usage": _detach(self.usage),
        }
        if self.evidence_ref is not None:
            document["evidence_ref"] = _detach(self.evidence_ref)
        return document


@dataclass(frozen=True)
class ProgressEvaluation:
    """Evaluator fact, not a score computed by Scheduler.

    comparison_ref identifies a compatible input/contract/evaluator window.
    API verifies evidence ownership and prevents checkpoint replay counting.
    Failed/uncomparable evaluation is unknown, never fake non-improvement.
    """

    evaluation_id: str
    node_id: str
    checkpoint_id: str
    evaluator_ref: str
    contract_ref: str
    comparison_ref: str
    evidence_ref: Mapping[str, Any]
    result: Literal["improved", "not_improved", "unknown"]

    def as_dict(self) -> dict[str, Any]:
        """W04a detached fact; identity comes from committed evaluation context."""
        return {
            "evaluation_id": self.evaluation_id,
            "node_id": self.node_id,
            "checkpoint_id": self.checkpoint_id,
            "evaluator_ref": self.evaluator_ref,
            "contract_ref": self.contract_ref,
            "comparison_ref": self.comparison_ref,
            "evidence_ref": _detach(self.evidence_ref),
            "result": self.result,
        }


@dataclass(frozen=True)
class AcceptanceRevision:
    """Versioned local revision under an immutable confirmed upper contract.

    human_confirmed contracts are not Agent-editable. Runtime-derived local
    criteria can change only within declared authority. Existing candidates
    keep their original contract_ref; a revision never rewrites their history.
    """

    contract_ref: str
    previous_ref: str | None
    upper_contract_ref: str | None
    origin: Literal["human_confirmed", "runtime_derived"]
    criteria_ref: Mapping[str, Any]
    authority_ref: str

    def as_dict(self) -> dict[str, Any]:
        """W05a detached version descriptor; API resolves/authenticates refs."""
        return {
            "contract_ref": self.contract_ref,
            "previous_ref": self.previous_ref,
            "upper_contract_ref": self.upper_contract_ref,
            "origin": self.origin,
            "criteria_ref": _detach(self.criteria_ref),
            "authority_ref": self.authority_ref,
        }


def validate_activity_fact(document: Mapping[str, Any]) -> ActivityFact:
    """W03a pure parser; reuse resources errors/canonical/artifact_ref.

    Exactly activity_id, parent_activity_id, kind, observed, usage, optional
    evidence_ref. IDs are opaque nonempty strings <=128 characters. A missing parent means the owning Agent root; self-parent rejects.
    Known usage keys: provider_calls/tool_calls; each supplied value is a
    nonnegative integer (not bool). Omitted counters stay omitted, not zero.
    Unknown fields (especially binding/epoch/scope fields) reject. Detach all
    nested data; this parser does not authorize callers or write state.
    """
    item = require_object(
        document, "activity",
        allowed=_ACTIVITY_FIELDS,
        required={"activity_id", "kind", "observed", "usage"})
    activity_id = _opaque_id(item["activity_id"], "activity.activity_id")
    parent_value = item.get("parent_activity_id")
    parent_activity_id = (
        None if parent_value is None
        else _opaque_id(parent_value, "activity.parent_activity_id"))
    require(parent_activity_id != activity_id, "invalid_argument",
            "activity.parent_activity_id must not reference itself")
    kind = item["kind"]
    require(isinstance(kind, str) and kind in _ACTIVITY_KINDS, "invalid_argument",
            "activity.kind is unsupported", kind=str(kind)[:64])
    observed = item["observed"]
    require(isinstance(observed, str) and observed in WORKER_OBSERVED,
            "invalid_argument", "activity.observed is unsupported",
            observed=str(observed)[:64])
    usage_document = require_object(item["usage"], "activity.usage",
                                    allowed=_ACTIVITY_USAGE_FIELDS)
    usage: dict[str, int] = {}
    for name in ("provider_calls", "tool_calls"):
        if name in usage_document:
            usage[name] = require_int(usage_document[name],
                                      f"activity.usage.{name}")
    evidence_ref = optional_artifact_ref(item.get("evidence_ref"),
                                         "activity.evidence_ref")
    return ActivityFact(
        activity_id=activity_id,
        parent_activity_id=parent_activity_id,
        kind=kind,
        observed=observed,
        usage=usage,
        evidence_ref=evidence_ref,
    )


def validate_activity_snapshot(
    documents: Any,
) -> tuple[ActivityFact, ...]:
    """W03a complete known-record batch, with one consistent parent tree.

    A JSON array only. Parse with validate_activity_fact. IDs must be unique;
    every nonnull parent must be in the same batch, and the forest is acyclic.
    No Node creation, scope reassignment, state merge or usage summation here.
    Backend integration will carry completeness separately from this array.
    """
    require(isinstance(documents, list), "invalid_argument",
            "activity snapshot must be a JSON array")
    facts = tuple(validate_activity_fact(document) for document in documents)
    ids = [fact.activity_id for fact in facts]
    require(len(ids) == len(set(ids)), "invalid_argument",
            "activity snapshot has duplicate activity ids")
    known = set(ids)
    parents = {fact.activity_id: fact.parent_activity_id for fact in facts}
    for activity_id, parent_id in parents.items():
        require(parent_id is None or parent_id in known, "invalid_argument",
                "activity parent is not in the same snapshot",
                activity_id=activity_id, parent_activity_id=parent_id)
    require_acyclic({
        activity_id: [] if parent_id is None else [parent_id]
        for activity_id, parent_id in parents.items()
    })
    return facts


def validate_progress_evaluation(document: Mapping[str, Any]) -> ProgressEvaluation:
    """W04: parse structured evidence; no natural-language score inference.

    Exactly evaluation_id, node_id, checkpoint_id, evaluator_ref, contract_ref,
    comparison_ref, evidence_ref and result. IDs are opaque nonempty strings
    <=128 characters with no path semantics. Content refs are strict
    sha256:<64 lowercase hex>. evidence_ref reuses ArtifactRef. result is
    improved/not_improved/unknown; unknown covers failures and incomparable
    windows. Unknown fields reject; this parser authorizes and writes nothing.
    """
    item = require_object(
        document, "progress evaluation",
        allowed=_PROGRESS_FIELDS, required=_PROGRESS_FIELDS)
    evaluation_id = _opaque_id(item["evaluation_id"], "progress.evaluation_id")
    node_id = _opaque_id(item["node_id"], "progress.node_id")
    checkpoint_id = _opaque_id(item["checkpoint_id"], "progress.checkpoint_id")
    evaluator_ref = _content_ref(item["evaluator_ref"], "progress.evaluator_ref")
    contract_ref = _content_ref(item["contract_ref"], "progress.contract_ref")
    comparison_ref = _content_ref(item["comparison_ref"], "progress.comparison_ref")
    evidence_ref = artifact_ref(item["evidence_ref"], "progress.evidence_ref")
    result = item["result"]
    require(isinstance(result, str) and result in _PROGRESS_RESULTS,
            "invalid_argument", "progress.result is unsupported",
            result=str(result)[:64])
    return ProgressEvaluation(
        evaluation_id=evaluation_id,
        node_id=node_id,
        checkpoint_id=checkpoint_id,
        evaluator_ref=evaluator_ref,
        contract_ref=contract_ref,
        comparison_ref=comparison_ref,
        evidence_ref=evidence_ref,
        result=result,
    )


def validate_acceptance_revision(document: Mapping[str, Any]) -> AcceptanceRevision:
    """W05a parse the existing six-field AcceptanceRevision descriptor.

    contract_ref and authority_ref are strict content refs; previous_ref and
    upper_contract_ref are null or strict content refs. origin is one of the
    two declared sources; runtime_derived requires a frozen upper reference.
    A version cannot point to itself as previous or upper; previous==upper is
    valid for the first runtime version. criteria_ref reuses ArtifactRef and
    detaches caller data. All six fields required, unknown fields rejected.
    This parser does NOT approve an Actor, load criteria or prove semantic
    implication. API checks reference content, actual parent authority, frozen
    upper and expected current version in its existing transaction.
    """
    item = require_object(
        document, "acceptance revision",
        allowed=_REVISION_FIELDS, required=_REVISION_FIELDS)
    contract_ref = _content_ref(item["contract_ref"], "revision.contract_ref")
    previous_ref = (
        None if item["previous_ref"] is None
        else _content_ref(item["previous_ref"], "revision.previous_ref"))
    upper_contract_ref = (
        None if item["upper_contract_ref"] is None
        else _content_ref(item["upper_contract_ref"], "revision.upper_contract_ref"))
    origin = item["origin"]
    require(isinstance(origin, str) and origin in _REVISION_ORIGINS,
            "invalid_argument", "revision.origin is unsupported",
            origin=str(origin)[:64])
    require(origin != "runtime_derived" or upper_contract_ref is not None,
            "invalid_argument",
            "runtime_derived revision requires a frozen upper contract")
    require(previous_ref != contract_ref, "invalid_argument",
            "revision.previous_ref must not reference itself")
    require(upper_contract_ref != contract_ref, "invalid_argument",
            "revision.upper_contract_ref must not reference itself")
    criteria_ref = artifact_ref(item["criteria_ref"], "revision.criteria_ref")
    authority_ref = _content_ref(item["authority_ref"], "revision.authority_ref")
    return AcceptanceRevision(
        contract_ref=contract_ref,
        previous_ref=previous_ref,
        upper_contract_ref=upper_contract_ref,
        origin=origin,
        criteria_ref=criteria_ref,
        authority_ref=authority_ref,
    )


_QUIESCENT_OBSERVED = frozenset({"idle", "stopped", "failed"})
_STOPPED_OBSERVED = frozenset({"stopped", "failed"})


def _activity_documents(records: Any, label: str) -> list[dict[str, Any]]:
    return [fact.as_dict() for fact in validate_activity_snapshot(records)]


def merge_activity_snapshot(
    previous: Any, incoming: Any, *, complete: bool,
) -> list[dict[str, Any]]:
    """W03b API projection within ONE execution epoch.

    Both arrays use validate_activity_snapshot. Preserve previously known
    records on incomplete reports; complete reports enumerate all known
    records (including terminal ones), so omission cannot mean 'stopped'.
    Stable activity_id cannot change kind/parent or restart after stopped/
    failed. Keep known cumulative counters; reject supplied decreases.
    No usage addition, authorization, cross-epoch merge or state writes here.
    Returned documents are detached; API commits with the outer observation.
    """
    require(isinstance(complete, bool), "invalid_argument",
            "activity completeness must be a boolean")
    documents = _activity_documents(
        [] if previous is None else previous, "previous activity snapshot")
    position = {document["activity_id"]: index
                for index, document in enumerate(documents)}
    known = set(position)
    incoming_ids: set[str] = set()
    for document in _activity_documents(incoming, "incoming activity snapshot"):
        activity_id = document["activity_id"]
        index = position.get(activity_id)
        if index is None:
            position[activity_id] = len(documents)
            documents.append(document)
            incoming_ids.add(activity_id)
            known.add(activity_id)
            continue
        incoming_ids.add(activity_id)
        current = documents[index]
        require(current["kind"] == document["kind"],
                "invalid_argument", "activity kind cannot change",
                activity_id=activity_id, previous_kind=current["kind"],
                kind=document["kind"])
        require(current["parent_activity_id"] == document["parent_activity_id"],
                "invalid_argument", "activity parent cannot change",
                activity_id=activity_id,
                previous_parent_activity_id=current["parent_activity_id"],
                parent_activity_id=document["parent_activity_id"])
        if current["observed"] in _STOPPED_OBSERVED:
            require(document["observed"] in _STOPPED_OBSERVED,
                    "invalid_argument",
                    "a stopped/failed activity cannot be revived",
                    activity_id=activity_id, previous_observed=current["observed"],
                    observed=document["observed"])
        usage = dict(current["usage"])
        for name, value in document["usage"].items():
            if name in usage:
                require(value >= usage[name], "invalid_argument",
                        "activity usage cannot decrease",
                        activity_id=activity_id, counter=name,
                        previous=usage[name], actual=value)
            usage[name] = value
        merged = {
            "activity_id": activity_id,
            "parent_activity_id": current["parent_activity_id"],
            "kind": current["kind"],
            "observed": document["observed"],
            "usage": usage,
        }
        evidence_ref = (document["evidence_ref"] if "evidence_ref" in document
                        else current.get("evidence_ref"))
        if evidence_ref is not None:
            merged["evidence_ref"] = _detach(evidence_ref)
        documents[index] = merged
    if complete:
        missing = sorted(known - incoming_ids)
        require(not missing, "invalid_argument",
                "a complete activity snapshot must include every known record",
                missing=missing)
    return _detach(documents)


def _complete_activities(worker: Mapping[str, Any]) -> list[Any] | None:
    if worker.get("activities_complete") is not True:
        return None
    records = worker.get("activities")
    if records is None:
        records = []
    return records if isinstance(records, list) else None


def _activity_observations(records: list[Any]) -> list[Any] | None:
    observations = []
    for record in records:
        if not isinstance(record, Mapping):
            return None
        observations.append(record.get("observed"))
    return observations


def execution_quiescent(worker: Mapping[str, Any]) -> bool:
    """W03b observed main AND owned internals have no work in progress.

    Complete enumeration is required; absent/false is unknown, never empty.
    Main/internal idle, stopped or failed are quiescent. Idle still owns a
    resumable execution; this predicate only releases active capacity/pause.
    Pure projection predicate, not a task-completion or delivery decision.
    """
    if worker.get("observed") not in _QUIESCENT_OBSERVED:
        return False
    records = _complete_activities(worker)
    if records is None:
        return False
    observations = _activity_observations(records)
    return (observations is not None
            and all(observed in _QUIESCENT_OBSERVED for observed in observations))


def execution_stopped(
    worker: Mapping[str, Any], *, allow_idle_main: bool = False,
) -> bool:
    """W03b all execution entities are observed stopped/failed, not just idle.

    Require complete enumeration. Use for cancellation/drain/recovery and
    epoch safety; stop acknowledgement alone is not evidence. Epoch may
    replace an idle MAIN only after all its internal entities have stopped.
    """
    main = worker.get("observed")
    allowed_main = _STOPPED_OBSERVED
    if allow_idle_main:
        allowed_main = allowed_main | {"idle"}
    if main not in allowed_main:
        return False
    records = _complete_activities(worker)
    if records is None:
        return False
    observations = _activity_observations(records)
    return (observations is not None
            and all(observed in _STOPPED_OBSERVED for observed in observations))


def fold_progress_evaluations(
    documents: Any, *, node_id: str, comparison_ref: str,
    no_improvement_limit: int,
) -> dict[str, Any]:
    """W04a pure projection from API-ordered, committed evaluation documents.

    Validate documents with validate_progress_evaluation. Keep only requested
    node/comparison window; same window must bind the same evaluator/contract.
    Exact replay is one fact; reusing evaluation/checkpoint identity for
    different facts rejects instead of counting a second checkpoint.
    improved clears the current streak and trigger, not_improved increments,
    unknown neither increments nor resets. First K within an uninterrupted
    streak fixes trigger_checkpoint_id; later reports retain it until genuine
    improvement. No inference from LLM turn count, restart, text or tool count.

    Return node_id, comparison_ref, consecutive_no_improvement,
    last_effective_checkpoint_id, trigger_checkpoint_id, stalled. Input is
    ordered by committed API event sequence, never client timestamp. This
    helper does not authenticate evidence, create decisions, or execute work.
    """
    target_node = _opaque_id(node_id, "fold.node_id")
    target_window = _content_ref(comparison_ref, "fold.comparison_ref")
    limit = require_int(no_improvement_limit, "fold.no_improvement_limit",
                        minimum=1)
    require(isinstance(documents, list), "invalid_argument",
            "progress evaluations must be a JSON array")
    facts = tuple(validate_progress_evaluation(document)
                  for document in documents)

    consecutive = 0
    last_effective: str | None = None
    trigger: str | None = None
    window_contract: str | None = None
    window_evaluator: str | None = None
    seen_evaluation: dict[str, ProgressEvaluation] = {}
    checkpoint_owner: dict[str, str] = {}
    for fact in facts:
        if fact.node_id != target_node or fact.comparison_ref != target_window:
            continue
        if window_contract is None:
            window_contract = fact.contract_ref
            window_evaluator = fact.evaluator_ref
        else:
            require(fact.contract_ref == window_contract
                    and fact.evaluator_ref == window_evaluator,
                    "invalid_argument",
                    "progress window binds one contract and evaluator",
                    evaluation_id=fact.evaluation_id,
                    contract_ref=fact.contract_ref,
                    evaluator_ref=fact.evaluator_ref)
        previous = seen_evaluation.get(fact.evaluation_id)
        if previous is not None:
            require(previous == fact, "invalid_argument",
                    "the same evaluation id cannot change facts",
                    evaluation_id=fact.evaluation_id)
            continue
        owner = checkpoint_owner.get(fact.checkpoint_id)
        require(owner is None or owner == fact.evaluation_id,
                "invalid_argument",
                "a checkpoint cannot become another evaluation fact",
                checkpoint_id=fact.checkpoint_id,
                evaluation_id=fact.evaluation_id,
                first_evaluation_id=owner)
        seen_evaluation[fact.evaluation_id] = fact
        checkpoint_owner[fact.checkpoint_id] = fact.evaluation_id
        if fact.result == "improved":
            consecutive = 0
            trigger = None
            last_effective = fact.checkpoint_id
        elif fact.result == "not_improved":
            consecutive += 1
            last_effective = fact.checkpoint_id
            if consecutive == limit and trigger is None:
                trigger = fact.checkpoint_id
    return {
        "node_id": target_node,
        "comparison_ref": target_window,
        "consecutive_no_improvement": consecutive,
        "last_effective_checkpoint_id": last_effective,
        "trigger_checkpoint_id": trigger,
        "stalled": trigger is not None,
    }


def previous_progress_checkpoint(status: Mapping[str, Any],
                                 candidates: list[Mapping[str, Any]],
                                 current: Mapping[str, Any]) -> dict[str, Any] | None:
    """Select the previous evaluated candidate in this exact comparison window.

    Consume Api event-sequenced status, never model-authored comparison claims.
    Exclude foreign Node/window, current/future rounds and absent candidate
    records. Return checkpoint identity/round/result/evidence_ref/output_refs/
    summary_ref as detached JSON data. No previous checkpoint returns None.
    Selection does not decide whether business evidence is comparable.
    """
    if not isinstance(status, Mapping) or status.get("enabled") is not True:
        return None
    node_id = status.get("node_id")
    comparison_ref = status.get("comparison_ref")
    evaluations = status.get("evaluations")
    if (not isinstance(node_id, str) or not isinstance(comparison_ref, str)
            or not isinstance(evaluations, list)):
        return None
    current_id = current.get("candidate_id")
    current_round = current.get("round")
    current_contract = current.get("contract_ref")
    if (not isinstance(current_id, str)
            or not isinstance(current_round, int)
            or isinstance(current_round, bool)):
        return None
    by_id: dict[str, Mapping[str, Any]] = {}
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        candidate_id = candidate.get("candidate_id")
        candidate_node = candidate.get("node_id")
        candidate_contract = candidate.get("contract_ref")
        round_number = candidate.get("round")
        if not isinstance(candidate_id, str):
            continue
        if candidate_node is not None and candidate_node != node_id:
            continue
        if candidate_contract is not None and candidate_contract != current_contract:
            continue
        if not isinstance(round_number, int) or isinstance(round_number, bool):
            continue
        by_id[candidate_id] = candidate
    selected: tuple[Mapping[str, Any], Mapping[str, Any]] | None = None
    for evaluation in evaluations:
        if not isinstance(evaluation, Mapping):
            continue
        if (evaluation.get("node_id") != node_id
                or evaluation.get("comparison_ref") != comparison_ref):
            continue
        evaluation_contract = evaluation.get("contract_ref")
        if (evaluation_contract is not None
                and evaluation_contract != current_contract):
            continue
        checkpoint_id = evaluation.get("checkpoint_id")
        if not isinstance(checkpoint_id, str) or checkpoint_id == current_id:
            continue
        candidate = by_id.get(checkpoint_id)
        if candidate is None:
            continue
        if candidate["round"] >= current_round:
            continue
        selected = (evaluation, candidate)
    if selected is None:
        return None
    evaluation, candidate = selected
    summary_ref = candidate.get("summary_ref")
    return {
        "checkpoint_id": evaluation["checkpoint_id"],
        "candidate_id": candidate["candidate_id"],
        "round": candidate["round"],
        "result": evaluation.get("result"),
        "evidence_ref": _detach(evaluation.get("evidence_ref")),
        "output_refs": [_detach(ref) for ref in candidate.get("output_refs") or []],
        "summary_ref": _detach(summary_ref) if summary_ref is not None else None,
    }
