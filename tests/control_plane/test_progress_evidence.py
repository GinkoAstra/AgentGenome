"""W04a component tests for structured progress evidence and the no-improvement fold."""

import copy

import pytest

from node_runtime.control_plane.execution_facts import (
    ProgressEvaluation,
    fold_progress_evaluations,
    validate_progress_evaluation,
)
from node_runtime.control_plane.resources import ControlPlaneError

SHA = "a" * 64
EVIDENCE = {"id": f"sha256:{SHA}", "sha256": SHA, "size": 12,
            "media_type": "application/json"}

NODE = "node-main"
OTHER_NODE = "node-other"
EVALUATOR = "sha256:" + "1" * 64
CONTRACT = "sha256:" + "2" * 64
COMPARISON = "sha256:" + "3" * 64
OTHER_EVALUATOR = "sha256:" + "6" * 64
OTHER_CONTRACT = "sha256:" + "5" * 64
OTHER_COMPARISON = "sha256:" + "4" * 64


def evaluation(evaluation_id, checkpoint_id, result, *, node_id=NODE,
               evaluator=EVALUATOR, contract=CONTRACT,
               comparison=COMPARISON, evidence=None):
    return {
        "evaluation_id": evaluation_id,
        "node_id": node_id,
        "checkpoint_id": checkpoint_id,
        "evaluator_ref": evaluator,
        "contract_ref": contract,
        "comparison_ref": comparison,
        "evidence_ref": dict(EVIDENCE) if evidence is None else evidence,
        "result": result,
    }


def fold(documents, *, node_id=NODE, comparison=COMPARISON, limit=3):
    return fold_progress_evaluations(
        documents, node_id=node_id, comparison_ref=comparison,
        no_improvement_limit=limit)


def fold_error(documents, *, node_id=NODE, comparison=COMPARISON, limit=3):
    with pytest.raises(ControlPlaneError) as raised:
        fold(documents, node_id=node_id, comparison=comparison, limit=limit)
    assert raised.value.code == "invalid_argument"
    return raised.value


# ---------------------------------------------------------------------------
# Parsing and serialization


def test_valid_evaluation_round_trips_as_detached_json():
    document = evaluation("ev-1", "cp-1", "not_improved")

    fact = validate_progress_evaluation(document)

    assert isinstance(fact, ProgressEvaluation)
    assert fact.as_dict() == document
    serialized = fact.as_dict()
    serialized["evidence_ref"]["size"] = 999
    serialized["node_id"] = "mutated"
    assert fact.evidence_ref == EVIDENCE
    assert fact.node_id == NODE
    document["evidence_ref"]["size"] = 7
    assert fact.evidence_ref == EVIDENCE


def test_parser_detaches_caller_evidence_ref():
    evidence = dict(EVIDENCE)
    fact = validate_progress_evaluation(
        evaluation("ev-1", "cp-1", "improved", evidence=evidence))
    evidence["size"] = 0
    assert fact.evidence_ref["size"] == 12


@pytest.mark.parametrize("mutate", [
    lambda doc: doc.update({"score": 0.9}),
    lambda doc: doc.pop("checkpoint_id"),
    lambda doc: doc.update({"result": "maybe"}),
    lambda doc: doc.update({"evaluation_id": ""}),
    lambda doc: doc.update({"evaluation_id": "x" * 129}),
    lambda doc: doc.update({"node_id": "x" * 129}),
    lambda doc: doc.update({"evaluator_ref": "sha256:" + "A" * 64}),
    lambda doc: doc.update({"contract_ref": "sha256:" + "a" * 63}),
    lambda doc: doc.update({"comparison_ref": "md5:" + "a" * 32}),
    lambda doc: doc.update({"evaluator_ref": EVALUATOR + "\n"}),
    lambda doc: doc.update({"contract_ref": CONTRACT + "\n"}),
    lambda doc: doc.update({"comparison_ref": COMPARISON + "\n"}),
    lambda doc: doc.update({"evidence_ref": {"id": "nope"}}),
])
def test_parser_rejects_unknown_fields_bad_ids_and_content_refs(mutate):
    document = evaluation("ev-1", "cp-1", "unknown")
    mutate(document)
    with pytest.raises(ControlPlaneError) as raised:
        validate_progress_evaluation(document)
    assert raised.value.code == "invalid_argument"


# ---------------------------------------------------------------------------
# Fold semantics


def test_fold_interleaves_improvement_unknown_and_not_improved():
    documents = [
        evaluation("e1", "c1", "not_improved"),
        evaluation("e2", "c2", "unknown"),
        evaluation("e3", "c3", "not_improved"),
        evaluation("e4", "c4", "not_improved"),
        evaluation("e5", "c5", "unknown"),
        evaluation("e6", "c6", "not_improved"),
        evaluation("e7", "c7", "improved"),
        evaluation("e8", "c8", "not_improved"),
        evaluation("e9", "c9", "not_improved"),
        evaluation("e10", "c10", "not_improved"),
    ]

    result = fold(documents, limit=3)

    assert result["consecutive_no_improvement"] == 3
    assert result["last_effective_checkpoint_id"] == "c10"
    assert result["trigger_checkpoint_id"] == "c10"
    assert result["stalled"] is True
    assert result["node_id"] == NODE
    assert result["comparison_ref"] == COMPARISON


def test_unknown_neither_counts_nor_clears_and_effective_updates_last():
    documents = [
        evaluation("e1", "c1", "not_improved"),
        evaluation("e2", "c2", "unknown"),
        evaluation("e3", "c3", "unknown"),
    ]

    result = fold(documents, limit=5)

    assert result["consecutive_no_improvement"] == 1
    assert result["last_effective_checkpoint_id"] == "c1"
    assert result["trigger_checkpoint_id"] is None
    assert result["stalled"] is False


def test_first_k_fixes_trigger_until_real_improvement():
    documents = [
        evaluation("e1", "c1", "not_improved"),
        evaluation("e2", "c2", "not_improved"),
        evaluation("e3", "c3", "not_improved"),
        evaluation("e4", "c4", "unknown"),
        evaluation("e5", "c5", "not_improved"),
    ]

    result = fold(documents, limit=3)

    assert result["consecutive_no_improvement"] == 4
    assert result["trigger_checkpoint_id"] == "c3"
    assert result["last_effective_checkpoint_id"] == "c5"
    assert result["stalled"] is True


def test_improvement_resets_streak_and_starts_a_new_trigger_interval():
    documents = [
        evaluation("e1", "c1", "not_improved"),
        evaluation("e2", "c2", "not_improved"),
        evaluation("e3", "c3", "not_improved"),
        evaluation("e4", "c4", "improved"),
    ]

    result = fold(documents, limit=3)

    assert result["consecutive_no_improvement"] == 0
    assert result["last_effective_checkpoint_id"] == "c4"
    assert result["trigger_checkpoint_id"] is None
    assert result["stalled"] is False


def test_empty_documents_mean_no_observations():
    result = fold([], limit=2)

    assert result == {
        "node_id": NODE,
        "comparison_ref": COMPARISON,
        "consecutive_no_improvement": 0,
        "last_effective_checkpoint_id": None,
        "trigger_checkpoint_id": None,
        "stalled": False,
    }


# ---------------------------------------------------------------------------
# Replay, conflict and isolation


def test_exact_replay_is_one_fact_but_changed_facts_reject():
    first = evaluation("e1", "c1", "not_improved")

    replay = fold([first, copy.deepcopy(first)], limit=1)

    assert replay["consecutive_no_improvement"] == 1
    assert replay["trigger_checkpoint_id"] == "c1"

    fold_error([first, {**first, "result": "improved"}])


def test_same_checkpoint_cannot_become_another_evaluation_fact():
    first = evaluation("e1", "c1", "not_improved")
    second = evaluation("e2", "c1", "not_improved")

    error = fold_error([first, second])

    assert error.details["checkpoint_id"] == "c1"


def test_node_and_window_isolation_ignores_other_facts():
    documents = [
        evaluation("e1", "c1", "not_improved"),
        evaluation("e2", "c2", "not_improved", node_id=OTHER_NODE),
        evaluation("e3", "c3", "not_improved", comparison=OTHER_COMPARISON),
        evaluation("e4", "c4", "not_improved"),
    ]

    result = fold(documents, limit=10)

    assert result["consecutive_no_improvement"] == 2
    assert result["last_effective_checkpoint_id"] == "c4"
    other = fold(documents, node_id=OTHER_NODE, limit=10)
    assert other["consecutive_no_improvement"] == 1
    assert other["last_effective_checkpoint_id"] == "c2"


def test_window_rejects_contract_or_evaluator_mismatch():
    fold_error([
        evaluation("e1", "c1", "not_improved"),
        evaluation("e2", "c2", "not_improved", contract=OTHER_CONTRACT),
    ])
    fold_error([
        evaluation("e1", "c1", "not_improved"),
        evaluation("e2", "c2", "not_improved", evaluator=OTHER_EVALUATOR),
    ])


def test_all_documents_parse_even_outside_the_target_window():
    documents = [
        evaluation("e1", "c1", "not_improved"),
        {**evaluation("e2", "c2", "not_improved", node_id=OTHER_NODE),
         "unexpected": True},
    ]

    fold_error(documents)


# ---------------------------------------------------------------------------
# Determinism, input immutability and selector validation


def test_recovery_recomputes_same_result_from_persisted_facts():
    documents = [
        evaluation("e1", "c1", "not_improved"),
        evaluation("e2", "c2", "unknown"),
        evaluation("e3", "c3", "not_improved"),
        evaluation("e4", "c4", "not_improved"),
    ]
    before = copy.deepcopy(documents)

    first = fold(documents, limit=3)
    second = fold(documents, limit=3)

    assert first == second
    assert documents == before
    assert first["trigger_checkpoint_id"] == "c4"


@pytest.mark.parametrize("kwargs", [
    {"limit": 0},
    {"limit": True},
    {"limit": "3"},
    {"node_id": ""},
    {"comparison": "sha256:" + "Z" * 64},
    {"comparison": COMPARISON + "\n"},
])
def test_fold_rejects_invalid_selectors(kwargs):
    fold_error([], **kwargs)


def test_fold_requires_a_json_array():
    fold_error({"evaluation_id": "e1"})
    fold_error(("e1",))
