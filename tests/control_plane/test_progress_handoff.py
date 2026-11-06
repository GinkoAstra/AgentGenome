"""W04b: frozen Goal progress policy, review checkpoints and parent hand-off."""
from __future__ import annotations

import json
import time
from copy import deepcopy
from pathlib import Path

import pytest

from conftest import make_graph
from helpers import (Cluster, is_review_turn, operator, publisher, rid,
                     submit_candidate, submit_review, toolbox)

from node_runtime.control_plane.api import Api
from node_runtime.control_plane.backends.base import WorkerSpec
from node_runtime.control_plane.backends.pi import PiBackend
from node_runtime.control_plane.decisions import decision_reply_key
from node_runtime.control_plane.lifecycle import delivery_gate, revision_key
from node_runtime.control_plane.resources import Actor, ControlPlaneError, digest
from node_runtime.control_plane.scheduler import Scheduler

SCHEDULER = Actor("scheduler", {"scheduler"})


def _progress_document(definition_id, *, limit=2):
    document = make_graph(definition_id=definition_id)
    document["tasks"]["work"]["progress"] = {"no_improvement_limit": limit}
    return document


def _artifact_bytes(api, ref):
    info = api.get_resource(publisher(), "artifact", ref)
    return api.artifacts.read({key: info[key] for key in
                               ("id", "sha256", "size", "media_type")})


def _stage(state, name="first"):
    return next(node for node in state["nodes"]
                if node["local_child_id"] == name)


def _pump(cluster, run_id, predicate, limit=120):
    for _ in range(limit):
        cluster.scheduler.reconcile(run_id)
        state = cluster.state(run_id)
        if predicate(state):
            return state
        for worker in state["workers"]:
            if worker["controller_id"] == "ctl-1":
                cluster.controller.reconcile(run_id, worker["worker_id"])
    raise AssertionError("the expected intermediate state was not reached")


def _drive(cluster, run_id, limit=160):
    """Reconcile once, then hand every committed delivery to its backend."""
    actions = []
    for _ in range(limit):
        actions.extend(cluster.scheduler.reconcile(run_id)["actions"])
        state = cluster.state(run_id)
        for worker in state["workers"]:
            if worker["controller_id"] == "ctl-1":
                cluster.controller.reconcile(run_id, worker["worker_id"])
        state = cluster.state(run_id)
        if state["run"]["status"] != "running" and \
                state["run"]["status"] not in ("pending",):
            return state, actions
    raise AssertionError("the run did not reach a terminal state")


def _review_events(api, run_id, node_id):
    events = api.read_events(operator(), run_id, limit=1000)["events"]
    return [event for event in events
            if event["kind"] == "review_submitted"
            and event["node_id"] == node_id]


# ---------------------------------------------------------------------------
# Frozen policy


def test_progress_policy_is_frozen_only_when_declared(tmp_path):
    cluster = Cluster(tmp_path / "policy", None)
    published = cluster.publish(_progress_document("policy"))
    dataset = cluster.artifact(b"dataset")
    run = cluster.create_run(published["definition_ref"], {"dataset": dataset})
    bundle = cluster.api.definition_bundle_for_run(operator(), run["run_id"])
    work_ref = bundle.document["tasks"]["work"]
    check_ref = bundle.document["tasks"]["check"]

    assert bundle.tasks[work_ref].progress == {"no_improvement_limit": 2}
    assert bundle.tasks[check_ref].progress is None
    stored = json.loads(_artifact_bytes(cluster.api, work_ref))
    assert stored["progress"] == {"no_improvement_limit": 2}

    state = _pump(cluster, run["run_id"], lambda state: bool(state["workers"]))
    node = _stage(state)
    assert node["progress"]["enabled"] is True
    assert node["progress"]["policy"] == {"no_improvement_limit": 2}
    worker = state["workers"][0]
    # The frozen policy travels with the committed execution context.
    assert worker["context"]["progress"] == {"no_improvement_limit": 2}
    worker_actor = Actor(worker["worker_id"], {"worker"},
                         run_ids={run["run_id"]},
                         worker_id=worker["worker_id"], epoch=worker["epoch"])
    worker_view = cluster.api.get_resource(worker_actor, "node", node["node_id"])
    assert "progress" not in worker_view

    # A Task without the optional policy keeps its exact stored document: no
    # progress:null is synthesized and no identity changes.
    other = Cluster(tmp_path / "policy-absent", None)
    published = other.publish(make_graph(definition_id="policy-absent"))
    dataset = other.artifact(b"dataset")
    run = other.create_run(published["definition_ref"], {"dataset": dataset})
    bundle = other.api.definition_bundle_for_run(operator(), run["run_id"])
    for reference, task in bundle.tasks.items():
        assert task.progress is None
        assert "progress" not in json.loads(_artifact_bytes(other.api, reference))


# ---------------------------------------------------------------------------
# Review evidence


def test_review_persists_derived_evaluation_and_survives_reopen(tmp_path):
    cluster = Cluster(tmp_path / "evidence", None)
    captured = {}

    def agent(api):
        def run(spec, message, state):
            box = toolbox(api, spec, message)
            if is_review_turn(message):
                captured["review"] = (spec, dict(message))
                return
            submit_candidate(box, spec, round_number=1)
        return run

    cluster.backend.agent = agent(cluster.api)
    published = cluster.publish(_progress_document("evidence"))
    dataset = cluster.artifact(b"dataset")
    run = cluster.create_run(published["definition_ref"], {"dataset": dataset})
    run_id = run["run_id"]
    state = _pump(cluster, run_id, lambda state: "review" in captured)
    node = _stage(state)
    candidate = state["candidates"][0]

    spec, message = captured["review"]
    assert message["progress_policy"] == {"no_improvement_limit": 2,
                                          "required": True}
    box = toolbox(cluster.api, spec, message)
    feedback = box.put_feedback("first attempt is not comparable",
                                call_id="feedback-eval")
    arguments = {
        "candidate_id": message["candidate_id"],
        "candidate_round": message["candidate_round"],
        "verdict": "revise",
        "feedback_ref": feedback,
    }
    with pytest.raises(ControlPlaneError) as missing:
        box.invoke("node.submit_review", dict(arguments),
                   call_id="review-missing-progress")
    assert missing.value.code == "invalid_argument"

    result = box.invoke("node.submit_review",
                        dict(arguments, progress={"result": "not_improved"}),
                        call_id="review-with-progress")
    assert result["output"]["progress"]["result"] == "not_improved"

    events = _review_events(cluster.api, run_id, node["node_id"])
    assert len(events) == 1
    evaluation = events[0]["payload"]["progress"]
    bundle = cluster.api.definition_bundle_for_run(operator(), run_id)
    check_ref = bundle.document["tasks"]["check"]
    assert evaluation["node_id"] == node["node_id"]
    assert evaluation["checkpoint_id"] == candidate["candidate_id"]
    assert evaluation["evaluator_ref"] == check_ref
    assert evaluation["contract_ref"] == node["contract_ref"]
    assert evaluation["evidence_ref"]["id"] == feedback["id"]
    assert evaluation["comparison_ref"] == digest({
        "input_refs": node["input_refs"],
        "contract_ref": node["contract_ref"],
        "evaluator_ref": check_ref,
    })

    status = cluster.api.progress_status(SCHEDULER, run_id, node["node_id"])
    assert status["enabled"] is True
    assert status["policy"] == {"no_improvement_limit": 2}
    assert status["evaluator_ref"] == check_ref
    assert [item["checkpoint_id"] for item in status["evaluations"]] == [
        candidate["candidate_id"]]
    assert status["evaluations"][0]["evaluation_id"].startswith("progress-eval:")
    assert status["summary"]["consecutive_no_improvement"] == 1
    assert status["summary"]["stalled"] is False
    assert status["decision_id"] is None

    # The fold is a projection of committed events, so a fresh API computes
    # exactly the same status and a stale Scheduler cannot count twice.
    reopened = Api(cluster.root)
    again = reopened.progress_status(SCHEDULER, run_id, node["node_id"])
    assert again == status
    reopened.backend_capabilities = dict(cluster.api.backend_capabilities)
    Scheduler(reopened, scheduler_id="scheduler", controller_id="ctl-1") \
        .reconcile(run_id)
    assert len(_review_events(reopened, run_id, node["node_id"])) == 1
    assert reopened.list_resources(operator(), run_id)["decision_requests"] == []


def test_review_progress_is_refused_when_the_policy_is_absent(tmp_path):
    cluster = Cluster(tmp_path / "absent", None)
    captured = {}

    def agent(api):
        def run(spec, message, state):
            box = toolbox(api, spec, message)
            if is_review_turn(message):
                captured["review"] = (spec, dict(message))
                return
            submit_candidate(box, spec, round_number=1)
        return run

    cluster.backend.agent = agent(cluster.api)
    published = cluster.publish(make_graph(definition_id="absent"))
    dataset = cluster.artifact(b"dataset")
    run = cluster.create_run(published["definition_ref"], {"dataset": dataset})
    _pump(cluster, run["run_id"], lambda state: "review" in captured)

    spec, message = captured["review"]
    assert "progress_policy" not in message
    box = toolbox(cluster.api, spec, message)
    with pytest.raises(ControlPlaneError) as refused:
        submit_review(box, spec, message, verdict="revise",
                      progress="not_improved")
    assert refused.value.code == "invalid_argument"
    accepted = submit_review(box, spec, message, verdict="accept")
    assert accepted["output"]["verdict"] == "accept"
    assert "progress" not in accepted["output"]


def test_review_progress_identity_is_derived_and_cannot_be_forged(tmp_path):
    cluster = Cluster(tmp_path / "forge", None)
    captured = {}

    def agent(api):
        def run(spec, message, state):
            box = toolbox(api, spec, message)
            if is_review_turn(message):
                captured["review"] = (spec, dict(message))
                return
            submit_candidate(box, spec, round_number=1)
        return run

    cluster.backend.agent = agent(cluster.api)
    published = cluster.publish(_progress_document("forge"))
    dataset = cluster.artifact(b"dataset")
    run = cluster.create_run(published["definition_ref"], {"dataset": dataset})
    run_id = run["run_id"]
    state = _pump(cluster, run_id, lambda state: "review" in captured)
    node = _stage(state)
    spec, message = captured["review"]
    box = toolbox(cluster.api, spec, message)

    for forged in (
            {"result": "improved", "comparison_ref": "sha256:" + "0" * 64},
            {"result": "improved", "evaluation_id": "progress-eval:" + "0" * 32},
            {"result": "maybe"},
            {"result": "improved", "checkpoint_id": "other-checkpoint"}):
        with pytest.raises(ControlPlaneError) as rejected:
            box.invoke("node.submit_review", {
                "candidate_id": message["candidate_id"],
                "candidate_round": message["candidate_round"],
                "verdict": "revise",
                "feedback_ref": box.put_feedback("forged", call_id="forged-ref"),
                "progress": forged,
            }, call_id=f"forged-{sorted(forged)}")
        assert rejected.value.code in {"invalid_argument", "capability_unsupported"}
    assert _review_events(cluster.api, run_id, node["node_id"]) == []

    # The Scheduler-side hand-off mutation is also verified against committed
    # facts: a valid-looking id with a non-trigger checkpoint is refused.
    status = cluster.api.progress_status(SCHEDULER, run_id, node["node_id"])
    current_node = cluster.api.get_resource(SCHEDULER, "node", node["node_id"])
    current_run = cluster.api.get_resource(SCHEDULER, "run", run_id)
    reads = [
        {"kind": "node", "id": node["node_id"],
         "revision": current_node["revision"]},
        {"kind": "run", "id": run_id, "revision": current_run["revision"]},
    ]
    forged = {
        "kind": "decision_request", "op": "create",
        "id": "progress-stalled:" + "f" * 32,
        "fields": {"source": "progress_stalled", "node_id": node["node_id"],
                   "comparison_ref": status["comparison_ref"],
                   "trigger_checkpoint_id": message["candidate_id"]},
    }
    with pytest.raises(ControlPlaneError) as not_stalled:
        cluster.api.commit_plan(cluster.scheduler.actor(run_id), reads,
                                [forged], rid("forged-request"))
    assert not_stalled.value.code in {"invalid_argument", "candidate_mismatch"}

    forged["fields"]["comparison_ref"] = "sha256:" + "0" * 64
    with pytest.raises(ControlPlaneError) as wrong_window:
        cluster.api.commit_plan(cluster.scheduler.actor(run_id), reads,
                                [forged], rid("forged-window"))
    assert wrong_window.value.code == "candidate_mismatch"


# ---------------------------------------------------------------------------
# Delivery priority


def _gate_node(state="running"):
    return {"node_id": "n1", "run_id": "r1", "state": state, "round": 1,
            "epoch": 1}


def _gate_candidate(decision="revise"):
    return {"node_id": "n1", "run_id": "r1", "round": 1,
            "candidate_id": "c1", "decision": decision}


def _gate_worker():
    return {"worker_id": "w1", "node_id": "n1", "run_id": "r1", "epoch": 2}


def _gate_messages(*, reply="pending", revision="pending"):
    return [
        {"delivery_key": decision_reply_key("d1"), "delivery_state": reply,
         "epoch": 2, "payload": {"kind": "decision_resolved"}},
        {"delivery_key": revision_key("c1", "revise"),
         "delivery_state": revision, "epoch": 2, "payload": {"kind": "revision"}},
        {"delivery_key": "ordinary", "delivery_state": "pending",
         "epoch": 2, "payload": {"kind": "continue"}},
    ]


STALLED = {"enabled": True, "released": False,
           "summary": {"stalled": True}}
RELEASED = {"enabled": True, "released": True,
            "summary": {"stalled": True}}


def test_delivery_priority_is_reply_then_revision_then_ordinary():
    worker = _gate_worker()
    node = _gate_node()
    candidate = _gate_candidate()
    resolved = [{"node_id": "n1", "decision_id": "d1", "state": "resolved"}]
    opened = [{"node_id": "n1", "decision_id": "d1", "state": "open"}]

    # An open parent request blocks everything, including the stalled gate.
    assert delivery_gate(worker, _gate_messages(), node, [candidate], opened,
                         progress=STALLED) == set()

    # Once resolved, only the reply may cross the unresolved trigger.
    messages = _gate_messages()
    assert delivery_gate(worker, messages, node, [candidate], resolved,
                         progress=STALLED) == {decision_reply_key("d1")}
    assert delivery_gate(worker, messages, node, [candidate], resolved,
                         progress=RELEASED) == {decision_reply_key("d1")}

    # Reply delivered: the still-owed revision is next, never ordinary work.
    messages = _gate_messages(reply="delivered")
    assert delivery_gate(worker, messages, node, [candidate], resolved,
                         progress=RELEASED) == {revision_key("c1", "revise")}
    # Unreleased trigger keeps ordinary work blocked even after the reply.
    assert delivery_gate(worker, messages, node, [candidate], resolved,
                         progress=STALLED) == set()

    # Both consumed: ordinary continuation may proceed.
    messages = _gate_messages(reply="delivered", revision="delivered")
    assert delivery_gate(worker, messages, node, [candidate], resolved,
                         progress=RELEASED) is None

    # Without progress the same priority order still holds.
    messages = _gate_messages()
    assert delivery_gate(worker, messages, node, [candidate], resolved) == {
        decision_reply_key("d1")}

    # The existing hard gates are not weakened by the progress rule.
    undecided = _gate_candidate(decision=None)
    assert delivery_gate(worker, _gate_messages(), _gate_node("awaiting_review"),
                         [undecided], resolved, progress=RELEASED) == set()


# ---------------------------------------------------------------------------
# No parent authority


def test_root_progress_stall_fails_instead_of_faking_a_handoff(tmp_path):
    cluster = Cluster(tmp_path / "root", None)

    def agent(api):
        def run(spec, message, state):
            box = toolbox(api, spec, message)
            if is_review_turn(message):
                submit_review(box, spec, message, verdict="revise",
                              progress="not_improved")
                return
            submit_candidate(box, spec, round_number=1)
        return run

    cluster.backend.agent = agent(cluster.api)
    published = cluster.publish(_progress_document("root", limit=1))
    dataset = cluster.artifact(b"dataset")
    run = cluster.create_run(published["definition_ref"], {"dataset": dataset})
    state, actions = _drive(cluster, run["run_id"])

    assert state["run"]["status"] == "failed"
    node = _stage(state)
    assert node["state"] == "failed"
    assert state["decision_requests"] == []
    failure = next(action for action in actions
                   if action["action"] == "node_failed")
    assert failure["reason"] == "progress_handoff_unavailable"
    assert failure["code"] == "capability_unsupported"
    # No synthetic evaluator/decision replaced the missing parent authority.
    assert node["progress"]["summary"]["stalled"] is True
    assert node["progress"]["decision_state"] is None
    assert node["progress"]["released"] is False


# ---------------------------------------------------------------------------
# Real parent hand-off, child re-expansion and restart


def _handoff_graph(definition_id, *, limit=2):
    document = make_graph(definition_id=definition_id)
    document["tasks"]["segment"] = deepcopy(document["tasks"]["work"])
    document["tasks"]["segment"]["goal"] = "Produce one refined segment."
    document["tasks"]["segment"]["progress"] = {"no_improvement_limit": limit}
    document["tasks"]["splitter"] = deepcopy(document["tasks"]["work"])
    document["tasks"]["splitter"]["goal"] = ("Split the task, consume the "
                                             "accepted segment, and finish.")
    document["tasks"]["splitter"]["expansion"] = {
        "join_rule": "all",
        "children": [{"local_child_id": "seg", "template_ref": "segment"}],
    }
    document["budget_limits"].update({"max_nodes": 10, "max_rounds": 6,
                                      "max_continuations": 6})
    document["nodes"] = {
        "phase": {"task_ref": "splitter", "reviewer_ref": "check",
                  "needs": [], "inputs": {"dataset": {"run_input": "dataset"}}},
    }
    return document


def _handoff_agent(api, segment_ref):
    replanned: set[str] = set()
    deferred: set[str] = set()

    def node_view(node_id):
        return api.get_resource(SCHEDULER, "node", node_id)

    def expand(box, spec, prefix):
        box.invoke("node.request_expansion", {
            "expansion_id": f"{prefix}-{spec.node_id}",
            "join_rule": "all",
            "children": [{"local_child_id": "seg",
                          "template_ref": segment_ref}],
        }, call_id=f"{prefix}-{spec.node_id}")

    def run(spec, message, state):
        box = toolbox(api, spec, message)
        kind = message.get("kind")
        if is_review_turn(message):
            target = message.get("target_node_id") or message.get("node_id")
            if message.get("progress_policy") is not None:
                target_node = node_view(target)
                settled = (target in replanned
                           or target_node.get("parent_id") in replanned)
                if settled:
                    submit_review(box, spec, message, verdict="accept",
                                  progress="improved")
                else:
                    submit_review(box, spec, message, verdict="revise",
                                  progress="not_improved")
            else:
                submit_review(box, spec, message)
            return
        if kind == "decision_request":
            replanned.add(message.get("child_node_id"))
            box.invoke("node.resolve_decision", {
                "decision_id": message["decision_id"],
                "answer": ("Re-understand the task and create one smaller "
                           "segment while keeping the frozen contract."),
            }, call_id=f"resolve-{message['decision_id']}")
            return
        if kind == "decision_resolved":
            replanned.add(spec.node_id)
            expand(box, spec, "replan")
            return
        if kind == "revision":
            if spec.node_id not in replanned:
                # A turn running inside the revision delivery cannot submit
                # ahead of that delivery fact; rework starts on the next turn.
                deferred.add(spec.node_id)
            return
        if kind == "continue":
            if spec.node_id in deferred and spec.node_id not in replanned:
                deferred.discard(spec.node_id)
                node = node_view(spec.node_id)
                if node["state"] == "running":
                    submit_candidate(
                        box, spec, round_number=int(node["round"]) + 1)
            return
        if kind == "children_joined":
            submit_candidate(box, spec,
                             round_number=int(node_view(spec.node_id)["round"]) + 1)
            return
        if kind == "accepted":
            return
        if kind == "start":
            if node_view(spec.node_id).get("parent_id") is None:
                expand(box, spec, "split")
            else:
                submit_candidate(box, spec, round_number=1)
            return
    return run


def _handoff_run(tmp_path, name):
    cluster = Cluster(tmp_path / name, None)
    document = _handoff_graph(name)
    refs = cluster.api.validate_definition(publisher(), document)["document"]["tasks"]
    cluster.backend.agent = _handoff_agent(cluster.api, refs["segment"])
    published = cluster.publish(document)
    dataset = cluster.artifact(b"dataset")
    run = cluster.create_run(published["definition_ref"], {"dataset": dataset})
    return cluster, refs["segment"], run["run_id"]


def test_parent_replans_then_child_expands_and_the_run_recovers(tmp_path):
    cluster, segment_ref, run_id = _handoff_run(tmp_path, "handoff")
    state = cluster.drive(run_id)

    assert state["run"]["status"] == "completed"
    segments = sorted(
        (node for node in state["nodes"]
         if node.get("expansion_id") is not None),
        key=lambda node: node["node_id"])
    parent = _stage(state, "phase")
    # The first segment of the phase and the child created after the parent
    # re-plan are both real Nodes with their own committed results.
    assert len(segments) == 2
    first = next(node for node in segments
                 if node["parent_id"] == parent["node_id"]
                 and any(other["parent_id"] == node["node_id"]
                         for other in segments))
    child = next(node for node in segments
                 if node["parent_id"] == first["node_id"])
    assert first["state"] == "succeeded"
    assert child["state"] == "succeeded"
    assert first["template_ref"] == child["template_ref"] == segment_ref
    assert child["accepted_candidate_ref"]

    # The parent did not succeed because a child was accepted: it still had to
    # submit and pass its own candidate after the join.
    assert parent["state"] == "succeeded"
    parent_candidate = next(item for item in state["candidates"]
                            if item["candidate_id"]
                            == parent["accepted_candidate_ref"])
    assert parent_candidate["round"] == 1

    # One stable request for the stalled window, answered by the real parent.
    stalled_requests = [item for item in state["decision_requests"]
                        if item["node_id"] == first["node_id"]]
    assert len(stalled_requests) == 1
    request = stalled_requests[0]
    assert request["reason"] == "progress_stalled"
    assert request["parent_id"] == parent["node_id"]
    assert request["state"] == "resolved"
    assert request["decision_id"].startswith("progress-stalled:")
    assert request["options"] == []
    # Evidence of every checkpoint that did not improve, deduplicated by
    # content because two identical feedback reports are one artifact.
    expected_refs = list(dict.fromkeys(
        evaluation["evidence_ref"]["id"]
        for evaluation in first["progress"]["evaluations"]
        if evaluation["result"] != "improved"))
    assert [ref["id"] for ref in request["material_refs"]] == expected_refs
    assert request["reply"]["answer"].startswith("Re-understand")

    # Round 1/2 evidence belongs to the old window; the accepted round 3 is a
    # real improvement and closes the streak without clearing history.
    evaluations = first["progress"]["evaluations"]
    assert [item["result"] for item in evaluations] == [
        "not_improved", "not_improved", "improved"]
    assert first["progress"]["summary"]["consecutive_no_improvement"] == 0
    assert first["progress"]["summary"]["stalled"] is False
    # A healthy window gates nothing; the resolved request is still the one
    # durable answer evidenced above and is not re-created after recovery.
    assert first["progress"]["released"] is False

    # The child's own re-plan used the declared expansion channel: two
    # expansions exist (phase split + stalled child re-plan).
    assert len(state["expansions"]) == 2
    parent_worker = next(worker for worker in state["workers"]
                         if worker["node_id"] == parent["node_id"])
    child_worker = next(worker for worker in state["workers"]
                        if worker["node_id"] == first["node_id"])
    # The parent authority is the real parent Worker, never an impersonation.
    assert request["worker_id"] == child_worker["worker_id"]
    assert request["resolved_by"] == parent_worker["worker_id"]


def test_open_handoff_and_progress_evidence_survive_a_real_restart(tmp_path):
    cluster, segment_ref, run_id = _handoff_run(tmp_path, "restart")
    state = None
    for _ in range(160):
        cluster.scheduler.reconcile(run_id)
        state = cluster.state(run_id)
        if any(item["state"] == "open" for item in state["decision_requests"]):
            break
        for worker in state["workers"]:
            if worker["controller_id"] == "ctl-1":
                cluster.controller.reconcile(run_id, worker["worker_id"])
    assert state is not None
    open_requests = [item for item in state["decision_requests"]
                     if item["state"] == "open"]
    assert len(open_requests) == 1
    request = open_requests[0]
    stalled = next(node for node in state["nodes"]
                   if node["node_id"] == request["node_id"])
    assert stalled["state"] == "waiting_decision"
    before = cluster.api.progress_status(SCHEDULER, run_id, request["node_id"])
    assert before["summary"]["stalled"] is True
    assert before["decision_state"] == "open"
    evaluations = len(before["evaluations"])

    # A fresh Api/Scheduler/Backend on the same durable state must reuse the
    # same trigger instead of recounting or creating a second request.
    restarted = Cluster(cluster.root, None)
    restarted.backend.agent = _handoff_agent(restarted.api, segment_ref)
    restarted.scheduler.reconcile(run_id)
    after = restarted.api.progress_status(SCHEDULER, run_id, request["node_id"])
    current = restarted.state(run_id)
    assert after["decision_id"] == before["decision_id"]
    assert len(after["evaluations"]) == evaluations
    assert [item["decision_id"] for item in current["decision_requests"]] == [
        before["decision_id"]]

    completed = restarted.drive(run_id)
    assert completed["run"]["status"] == "completed"
    assert len([item for item in completed["decision_requests"]
                if item["node_id"] == request["node_id"]]) == 1


# ---------------------------------------------------------------------------
# Actual Pi bridge/prompt


def test_pi_prompt_and_dynamic_tool_schema_carry_the_progress_policy(tmp_path):
    capture = tmp_path / "captured-argv"
    capture.mkdir()
    fake = tmp_path / "capture-pi"
    fake.write_text(
        "#!/bin/sh\n"
        "for last; do :; done\n"
        f"out=$(mktemp '{capture}/argv.XXXXXX')\n"
        "printf '%s' \"$last\" > \"$out\"\n"
        "exit 0\n", encoding="utf-8")
    fake.chmod(0o755)
    backend = PiBackend(
        tmp_path / "data", executable=str(fake), max_wall_seconds=30,
        data_dir=tmp_path / "data",
        credential_issuer=lambda binding: {
            "endpoint": "http://127.0.0.1:1", "token": "delivery-token",
        })
    checker = WorkerSpec(
        worker_id="w2", node_id="n2", epoch=0, intent_seq=1,
        backend_ref="pi", agent_ref="checker@v1", desired="active", run_id="r1",
        context={"role": "reviewer", "brief": "Review the bound candidate."},
        tool_profile={"tools": ["node.submit_review"],
                      "backend_tools": ["write_file"]})
    message = {
        "kind": "review_start", "brief": "Review the candidate.",
        "role": "reviewer", "goal": "Review the bound candidate.",
        "acceptance": "The verdict cites the target contract.",
        "output_schema": {"type": "object"},
        "target_node_id": "n1", "candidate_id": "candidate-1",
        "candidate_round": 1, "reviewer_ref": "check",
        "progress_policy": {"no_improvement_limit": 2, "required": True},
        "target": {"node_id": "n1", "template_ref": "work",
                   "contract_ref": "clean"},
    }
    handle = backend.start(checker, ("w2", 0))
    backend.deliver(handle, message, "checker-start")

    deadline = time.monotonic() + 10
    while not list(capture.glob("argv.*")) and time.monotonic() < deadline:
        time.sleep(0.02)
    prompt = next(capture.glob("argv.*")).read_text(encoding="utf-8")
    assert "PROGRESS POLICY:" in prompt
    assert '"no_improvement_limit":2' in prompt.replace(" ", "")
    assert "progress=" in prompt
    assert '"not_improved"' in prompt

    state = json.loads(backend._state_path(("w2", 0)).read_text(encoding="utf-8"))
    names, specs, binding, decision = backend._system_tools(state, message)
    assert names == ["node.submit_review"]
    review = next(item for item in specs if item["name"] == "node.submit_review")
    assert review["parameters"]["properties"]["progress"]["required"] == [
        "result"]
    assert set(review["parameters"]["properties"]["progress"]["properties"][
        "result"]["enum"]) == {"improved", "not_improved", "unknown"}
