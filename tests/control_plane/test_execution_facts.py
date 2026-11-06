"""W03a component tests for ActivityFact parsing and snapshot structure."""

import pytest

from node_runtime.control_plane.execution_facts import (
    ActivityFact,
    execution_quiescent,
    execution_stopped,
    merge_activity_snapshot,
    validate_activity_fact,
    validate_activity_snapshot,
)
from node_runtime.control_plane.resources import ControlPlaneError, digest

SHA = "a" * 64
EVIDENCE = {"id": f"sha256:{SHA}", "sha256": SHA, "size": 12,
            "media_type": "application/json"}


def child(activity_id="native-1", parent=None, **overrides):
    document = {
        "activity_id": activity_id,
        "parent_activity_id": parent,
        "kind": "subagent",
        "observed": "active",
        "usage": {"provider_calls": 2},
    }
    document.update(overrides)
    return document


def test_valid_forest_round_trips_as_detached_json():
    root = {"activity_id": "root", "kind": "subagent",
            "observed": "active", "usage": {}}
    leaf = child("leaf", "root", usage={"provider_calls": 2, "tool_calls": 1},
                 evidence_ref=dict(EVIDENCE))

    facts = validate_activity_snapshot([root, leaf])

    assert [fact.activity_id for fact in facts] == ["root", "leaf"]
    root_dict = facts[0].as_dict()
    assert root_dict["parent_activity_id"] is None
    assert "evidence_ref" not in root_dict
    assert facts[1].as_dict()["evidence_ref"] == EVIDENCE

    serialized = facts[1].as_dict()
    serialized["usage"]["provider_calls"] = 99
    serialized["evidence_ref"]["size"] = 0
    assert facts[1].usage == {"provider_calls": 2, "tool_calls": 1}
    assert facts[1].evidence_ref == EVIDENCE
    digest(facts[1].as_dict())


def test_parser_detaches_caller_nested_data():
    usage = {"provider_calls": 2}
    evidence = dict(EVIDENCE)
    fact = validate_activity_fact(
        child(usage=usage, evidence_ref=evidence))
    usage["provider_calls"] = 7
    evidence["size"] = 999

    assert fact.usage == {"provider_calls": 2}
    assert fact.evidence_ref == EVIDENCE


def test_unknown_usage_counters_stay_unknown_not_zero():
    fact = validate_activity_fact(child(usage={"tool_calls": 0}))

    assert fact.usage == {"tool_calls": 0}
    assert fact.as_dict()["usage"] == {"tool_calls": 0}
    assert "provider_calls" not in fact.as_dict()["usage"]


def test_explicit_null_evidence_is_no_evidence():
    fact = validate_activity_fact(child(evidence_ref=None))

    assert fact.evidence_ref is None
    assert "evidence_ref" not in fact.as_dict()


@pytest.mark.parametrize("field,value", [
    ("run_id", "run-1"),
    ("worker_id", "worker-1"),
    ("epoch", 1),
    ("observation_id", "obs-1"),
    ("event_id", "event-1"),
    ("format", "agentgenome-activity/1"),
])
def test_binding_and_scope_fields_reject(field, value):
    with pytest.raises(ControlPlaneError) as error:
        validate_activity_fact(child(**{field: value}))
    assert error.value.code == "invalid_argument"
    assert error.value.details["extra"] == [field]


@pytest.mark.parametrize("usage", [
    {"provider_calls": True},
    {"tool_calls": -1},
    {"provider_calls": 1.5},
    {"provider_calls": "2"},
    {"provider_calls": None},
    {"input_tokens": 3},
])
def test_invalid_usage_counters_reject(usage):
    with pytest.raises(ControlPlaneError) as error:
        validate_activity_fact(child(usage=usage))
    assert error.value.code == "invalid_argument"


@pytest.mark.parametrize("field,value", [
    ("kind", "task"),
    ("observed", "done"),
    ("activity_id", ""),
    ("activity_id", "a" * 129),
])
def test_invalid_identity_or_literal_fields_reject(field, value):
    with pytest.raises(ControlPlaneError) as error:
        validate_activity_fact(child(**{field: value}))
    assert error.value.code == "invalid_argument"


def test_opaque_ids_may_contain_slashes_and_parent_matches_exactly():
    facts = validate_activity_snapshot([
        {"activity_id": "agent/root", "kind": "subagent",
         "observed": "active", "usage": {}},
        child("agent/native-1", "agent/root"),
        child("agent/native-1/child", "agent/native-1"),
    ])

    assert [fact.parent_activity_id for fact in facts] == [
        None, "agent/root", "agent/native-1"]
    assert facts[2].as_dict()["activity_id"] == "agent/native-1/child"

    with pytest.raises(ControlPlaneError):
        validate_activity_snapshot([child("agent/native-1", "agent")])


def test_self_parent_rejects():
    with pytest.raises(ControlPlaneError) as error:
        validate_activity_fact(child("native-1", "native-1"))
    assert error.value.code == "invalid_argument"


def test_duplicate_ids_reject():
    with pytest.raises(ControlPlaneError):
        validate_activity_snapshot([child("same"), child("same")])


def test_cycle_and_dangling_parent_reject():
    with pytest.raises(ControlPlaneError):
        validate_activity_snapshot([child("a", "b"), child("b", "a")])
    with pytest.raises(ControlPlaneError):
        validate_activity_snapshot([child("a", "missing")])


def test_snapshot_requires_array_but_empty_is_valid():
    assert validate_activity_snapshot([]) == ()
    for value in ({}, None, "activities", (child(),)):
        with pytest.raises(ControlPlaneError):
            validate_activity_snapshot(value)


def test_activity_fact_is_frozen():
    fact = validate_activity_fact(child())
    with pytest.raises(Exception):
        fact.observed = "idle"
    assert isinstance(fact, ActivityFact)


# -- W03b projection and lifecycle predicates ---------------------------

def worker(observed="idle", activities=None, complete=True):
    return {"observed": observed,
            "activities": [] if activities is None else activities,
            "activities_complete": complete}


def test_incomplete_snapshot_keeps_known_records_and_merges_usage():
    previous = [child("root", None, usage={"provider_calls": 2}),
                child("leaf", "root", observed="stopped",
                      usage={"tool_calls": 1})]
    merged = merge_activity_snapshot(
        previous, [child("root", None, observed="idle",
                         usage={"provider_calls": 3})], complete=False)

    assert [record["activity_id"] for record in merged] == ["root", "leaf"]
    assert merged[0]["observed"] == "idle"
    assert merged[0]["usage"] == {"provider_calls": 3}
    assert merged[1]["observed"] == "stopped"
    assert merged[1]["usage"] == {"tool_calls": 1}


def test_complete_snapshot_must_include_every_known_record():
    previous = [child("root", None), child("leaf", "root")]
    with pytest.raises(ControlPlaneError) as error:
        merge_activity_snapshot(previous, [child("root", None, observed="idle")],
                                complete=True)
    assert error.value.code == "invalid_argument"
    assert error.value.details["missing"] == ["leaf"]


def test_identity_and_terminal_state_cannot_be_rewritten():
    previous = [child("root", None, observed="stopped",
                      usage={"provider_calls": 1})]
    with pytest.raises(ControlPlaneError):
        merge_activity_snapshot(
            previous, [child("root", None, kind="background_tool")],
            complete=False)
    with pytest.raises(ControlPlaneError):
        merge_activity_snapshot(
            previous, [child("root", "other")], complete=False)
    with pytest.raises(ControlPlaneError):
        merge_activity_snapshot(
            previous, [child("root", None, observed="active")], complete=False)
    revived = merge_activity_snapshot(
        previous, [child("root", None, observed="failed")], complete=False)
    assert revived[0]["observed"] == "failed"


def test_usage_decrease_rejects_and_omitted_counter_is_preserved():
    previous = [child("root", None,
                      usage={"provider_calls": 2, "tool_calls": 4})]
    with pytest.raises(ControlPlaneError) as error:
        merge_activity_snapshot(
            previous, [child("root", None, observed="idle",
                             usage={"provider_calls": 1})], complete=False)
    assert error.value.code == "invalid_argument"
    merged = merge_activity_snapshot(
        previous, [child("root", None, observed="idle", usage={})],
        complete=False)
    assert merged[0]["usage"] == {"provider_calls": 2, "tool_calls": 4}


def test_quiescent_requires_complete_enumeration():
    assert execution_quiescent(worker())
    assert execution_quiescent(worker("failed", [
        child("a", None, observed="idle"),
        child("b", "a", observed="stopped")]))
    assert not execution_quiescent(worker(complete=False))
    assert not execution_quiescent(worker(activities=[child("a", None)]))
    assert not execution_quiescent(worker("active"))
    assert not execution_quiescent(worker(activities=[
        child("a", None, observed="unknown")]))
    assert not execution_quiescent(worker("failed", [child("a", None)]))
    assert not execution_quiescent({"observed": "idle"})


def test_stopped_is_stricter_than_quiescent():
    idle_internal = worker(activities=[child("a", None, observed="idle")])
    assert execution_quiescent(idle_internal)
    assert not execution_stopped(idle_internal)
    assert not execution_stopped(idle_internal, allow_idle_main=True)

    idle_main = worker(observed="idle", activities=[
        child("a", None, observed="stopped")])
    assert not execution_stopped(idle_main)
    assert execution_stopped(idle_main, allow_idle_main=True)

    assert execution_stopped(worker("stopped", [
        child("a", None, observed="failed")]))
    assert not execution_stopped(worker("stopped", complete=False))
