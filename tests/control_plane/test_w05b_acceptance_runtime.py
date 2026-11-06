"""W05b: parent-granted local acceptance versions and the real rebind chain."""
from __future__ import annotations

from copy import deepcopy

import pytest

from conftest import make_graph
from helpers import (Cluster, is_review_turn, operator, publisher, rid,
                     submit_candidate, submit_review, toolbox)

from node_runtime.control_plane.resources import (Actor, ControlPlaneError,
                                                  canonical, validate_schema_value)
from node_runtime.control_plane.backends.pi import PiBackend
from node_runtime.control_plane.tools import WorkerToolbox

CRITERIA = {
    "input_schema": {"type": "array"},
    "output_schema": {"type": "array"},
    "acceptance": "The revised local criteria require the documented evidence.",
}


def _publish_criteria(box, call_id="criteria", document=None,
                      canonical_bytes=True):
    """A Worker may only propose criteria it can itself read."""
    import json
    document = document or CRITERIA
    content = (canonical(document) if canonical_bytes
               else json.dumps(document)).encode("utf-8")
    return box.publish_artifact(
        {"path": f"{call_id}.json", "media_type": "application/json"},
        call_id=call_id, content=content)["output"]


def _node(state, *, prefix=None, local_child_id=None):
    for node in state["nodes"]:
        if prefix is not None and node["node_id"].startswith(prefix):
            return node
        if local_child_id is not None and node["local_child_id"] == local_child_id:
            return node
    raise AssertionError("node not found")


def _worker(state, node_id):
    return next(worker for worker in state["workers"]
                if worker["node_id"] == node_id)


def _worker_box(cluster, worker):
    return WorkerToolbox(
        cluster.api, run_id=worker["run_id"], node_id=worker["node_id"],
        worker_id=worker["worker_id"], epoch=worker["epoch"],
        tool_profile=worker["tool_profile"],
        contract_ref=worker["context"].get("contract_ref"))


def _pump(cluster, run_id, predicate, limit=160):
    for _ in range(limit):
        cluster.scheduler.reconcile(run_id)
        state = cluster.state(run_id)
        if predicate(state):
            return state
        for worker in state["workers"]:
            if worker["controller_id"] == "ctl-1":
                cluster.controller.reconcile(run_id, worker["worker_id"])
    raise AssertionError("the expected intermediate state was not reached")


def _split(api, spec, box, children, call_id="split"):
    return box.invoke("node.request_expansion", {
        "expansion_id": f"split-{spec.node_id}",
        "join_rule": "all",
        "children": children,
    }, call_id=call_id)


def _revise(box, target_node_id, criteria_ref, call_id="revise"):
    node = box.api.get_resource(box.actor, "node", target_node_id)
    return box.invoke("node.revise_acceptance", {
        "target_node_id": target_node_id,
        "previous_ref": node["contract_ref"],
        "criteria_ref": criteria_ref,
    }, call_id=call_id)


# ---------------------------------------------------------------------------
# Full version replacement


def _revision_agent(api, *, captured, criteria_document=None,
                    criteria_canonical=True, ask_decision=False,
                    delay_revised=False):
    """Parent splits once and revises the child after its first revise verdict."""
    criteria_document = criteria_document or CRITERIA

    def run(spec, message, state):
        box = toolbox(api, spec, message)
        kind = message.get("kind")
        node_id = spec.node_id
        if is_review_turn(message):
            target = message.get("target_node_id")
            if target and target.startswith("exp-"):
                if message.get("candidate_round") == 1:
                    submit_review(box, spec, message, verdict="revise")
                    captured["first_review"] = dict(message)
                    criteria = _publish_criteria(
                        box, document=criteria_document,
                        canonical_bytes=criteria_canonical)
                    captured["criteria_ref"] = criteria
                    box.invoke("node.revise_acceptance", {
                        "target_node_id": target,
                        # The reviewed execution still holds the old contract.
                        "previous_ref": message["target"]["contract_ref"],
                        "criteria_ref": criteria,
                    }, call_id="revise-child")
                else:
                    submit_review(box, spec, message, verdict="accept")
                return
            submit_review(box, spec, message, verdict="accept")
            return
        if kind == "decision_request":
            box.invoke("node.resolve_decision", {
                "decision_id": message["decision_id"],
                "answer": "Keep the existing format."}, call_id="decision-answer")
            return
        if kind == "decision_resolved":
            return
        if kind == "acceptance_revised":
            captured.setdefault("revised", []).append(dict(message))
            if delay_revised:
                return
            node = api.get_resource(box.actor, "node", node_id)
            submit_candidate(box, spec, round_number=int(node["round"]) + 1)
            return
        if kind == "children_joined":
            submit_candidate(box, spec, round_number=int(message.get("round") or 1))
            return
        if node_id.startswith("exp-"):
            captured.setdefault("child", []).append(dict(message))
            if ask_decision and kind == "start":
                box.invoke("node.request_decision", {
                    "decision_id": "format-before-version",
                    "reason": "format",
                    "question": "Which format should be used?"}, call_id="format")
                return
            node = api.get_resource(box.actor, "node", node_id)
            submit_candidate(box, spec, round_number=int(node["round"]) + 1)
            return
        _split(api, spec, box, [{"local_child_id": "left"}])
    return run


def test_parent_revision_rebinds_child_epoch_and_keeps_both_bounds(tmp_path):
    cluster = Cluster(tmp_path / "w05b", None)
    captured: dict = {}
    document = make_graph(definition_id="w05b")
    document["tasks"]["work"]["contract"]["acceptance"] = (
        "Frozen upper acceptance: every output is verifiable.")
    cluster.backend.agent = _revision_agent(cluster.api, captured=captured)
    published = cluster.publish(document)
    dataset = cluster.artifact(b"dataset", "text/plain")
    run = cluster.create_run(published["definition_ref"], {"dataset": dataset})
    run_id = run["run_id"]

    state = cluster.drive(run_id, limit=220)

    assert state["run"]["status"] == "completed"
    child = _node(state, prefix="exp-")
    parent = _node(state, local_child_id="first")
    # Two immutable versions ran in sequence; the first candidate is never
    # relabeled with the parent's later contract.
    status = cluster.api.acceptance_status(operator(), run_id, child["node_id"])
    assert status["upper"]["contract_ref"] == child["template_ref"]
    assert status["current"]["origin"] == "runtime_derived"
    assert status["current"]["contract_ref"] == child["contract_ref"]
    assert status["current"]["previous_ref"] == child["template_ref"]
    assert status["current"]["authority_ref"] == parent["template_ref"]
    assert status["current"]["criteria_ref"] == captured["criteria_ref"]
    assert [entry["state"] for entry in status["history"]] == ["applied"]
    assert status["pending_revision"] is None
    assert status["execution"]["epoch"] == 1
    assert status["execution"]["matches_current"] is True

    child_workers = [worker for worker in state["workers"]
                     if worker["node_id"] == child["node_id"]]
    assert len(child_workers) == 1
    assert child_workers[0]["epoch"] == 1

    candidates = [item for item in state["candidates"]
                  if item["node_id"] == child["node_id"]]
    assert [item["round"] for item in candidates] == [1, 2]
    assert candidates[0]["contract_ref"] == child["template_ref"]
    assert candidates[1]["contract_ref"] == child["contract_ref"]
    assert candidates[0]["decision"] == "revise"
    assert candidates[1]["decision"] == "accept"

    # The fresh epoch received both bounds, the prior verdict and the round.
    revised = captured["revised"][0]
    assert revised["contract_ref"] == child["contract_ref"]
    assert revised["previous_contract_ref"] == child["template_ref"]
    assert revised["upper_contract_ref"] == child["template_ref"]
    assert revised["acceptance"] == CRITERIA["acceptance"]
    assert revised["upper_acceptance"] == document["tasks"]["work"][
        "contract"]["acceptance"]
    assert revised["criteria_ref"] == captured["criteria_ref"]
    assert revised["round"] == 2
    assert revised["prior_feedback"][0]["verdict"] == "revise"

    # The old epoch never consumed the revise feedback: the new epoch carried
    # it in the acceptance_revised hand-off instead.
    inbox = cluster.api.list_inbox(operator(),
                                   child_workers[0]["worker_id"])["messages"]
    old_epoch = [message for message in inbox if message["epoch"] == 0]
    assert not any(message["payload"].get("kind") == "revision"
                   and message["delivery_state"] == "delivered"
                   for message in old_epoch)


def test_rebuild_from_scratch_reuses_the_applied_revision(tmp_path):
    """Reopening the data directory keeps version identity and execution bind."""
    cluster = Cluster(tmp_path / "w05b-reopen", None)
    captured: dict = {}
    cluster.backend.agent = _revision_agent(cluster.api, captured=captured)
    published = cluster.publish(make_graph(definition_id="w05b-reopen"))
    dataset = cluster.artifact(b"dataset", "text/plain")
    run = cluster.create_run(published["definition_ref"], {"dataset": dataset})
    run_id = run["run_id"]
    state = _pump(cluster, run_id, lambda state: bool(captured.get("revised")))
    child = _node(state, prefix="exp-")
    before = cluster.api.acceptance_status(operator(), run_id, child["node_id"])

    reopened = Cluster(tmp_path / "w05b-reopen", None)
    after = reopened.api.acceptance_status(operator(), run_id, child["node_id"])
    assert after == before
    assert after["current"]["origin"] == "runtime_derived"
    assert after["execution"]["epoch"] == 1

    # A late old-epoch tool context cannot keep working or relabel a candidate.
    worker = _worker(state, child["node_id"])
    old_box = WorkerToolbox(
        reopened.api, run_id=run_id, node_id=child["node_id"],
        worker_id=worker["worker_id"], epoch=0,
        tool_profile=worker["tool_profile"],
        contract_ref=child["template_ref"])
    with pytest.raises(ControlPlaneError) as stale_execution:
        submit_candidate(
            old_box,
            type("S", (), {"node_id": child["node_id"], "run_id": run_id})(),
            round_number=2)
    assert stale_execution.value.code == "stale_epoch", stale_execution.value.message


# ---------------------------------------------------------------------------
# Authority, provenance and version guards


def _single_stage_run(cluster):
    published = cluster.publish(make_graph(definition_id="w05b-authority"))
    dataset = cluster.artifact(b"dataset", "text/plain")
    run = cluster.create_run(published["definition_ref"], {"dataset": dataset})
    run_id = run["run_id"]
    state = _pump(cluster, run_id, lambda state: bool(state["workers"]))
    return run_id, state


def test_revision_requires_direct_child_and_api_derived_authority(tmp_path):
    cluster = Cluster(tmp_path / "w05b-authority", None)
    run_id, state = _single_stage_run(cluster)
    stage = _node(state, local_child_id="first")
    worker = _worker(state, stage["node_id"])
    box = _worker_box(cluster, worker)
    criteria_ref = _publish_criteria(box)

    with pytest.raises(ControlPlaneError) as missing_parent:
        _revise(box, stage["node_id"], criteria_ref, call_id="self-revision")
    assert missing_parent.value.code == "unauthorized"

    # A model-supplied authority/origin never reaches the fact validator.
    node = cluster.api.get_resource(operator(), "node", stage["node_id"])
    with pytest.raises(ControlPlaneError) as forged:
        cluster.api.append_fact(Actor(
            worker["worker_id"], {"worker"}, run_ids={run_id},
            worker_id=worker["worker_id"], epoch=worker["epoch"]), {
            "kind": "acceptance_revision_requested",
            "run_id": run_id,
            "target_node_id": stage["node_id"],
            "previous_ref": stage["contract_ref"],
            "criteria_ref": criteria_ref,
            "origin": "human_confirmed",
            "authority_ref": stage["template_ref"],
        }, rid("forged"), node["revision"])
    assert forged.value.code == "invalid_argument"

    stale = Actor(worker["worker_id"], {"worker"}, run_ids={run_id},
                  worker_id=worker["worker_id"], epoch=worker["epoch"] + 1)
    with pytest.raises(ControlPlaneError) as stale_epoch:
        cluster.api.append_fact(stale, {
            "kind": "acceptance_revision_requested",
            "run_id": run_id,
            "target_node_id": stage["node_id"],
            "previous_ref": stage["contract_ref"],
            "criteria_ref": criteria_ref,
        }, rid("stale"), node["revision"])
    assert stale_epoch.value.code == "stale_epoch"


def test_request_rejects_wrong_previous_and_second_version(tmp_path):
    cluster = Cluster(tmp_path / "w05b-guards", None)

    def agent(api):
        def run(spec, message, state):
            box = toolbox(api, spec, message)
            if is_review_turn(message):
                submit_review(box, spec, message)
                return
            if spec.node_id.startswith("exp-"):
                return
            _split(api, spec, box, [{"local_child_id": "left"}])
        return run

    cluster.backend.agent = agent(cluster.api)
    published = cluster.publish(make_graph(definition_id="w05b-guards"))
    dataset = cluster.artifact(b"dataset", "text/plain")
    run = cluster.create_run(published["definition_ref"], {"dataset": dataset})
    run_id = run["run_id"]
    state = _pump(cluster, run_id,
                  lambda state: any(node["node_id"].startswith("exp-")
                                    for node in state["nodes"]))
    parent = _node(state, local_child_id="first")
    child = _node(state, prefix="exp-")
    parent_box = _worker_box(cluster, _worker(state, parent["node_id"]))
    criteria_ref = _publish_criteria(parent_box)

    with pytest.raises(ControlPlaneError) as wrong_previous:
        parent_box.invoke("node.revise_acceptance", {
            "target_node_id": child["node_id"],
            "previous_ref": "sha256:" + "0" * 64,
            "criteria_ref": criteria_ref,
        }, call_id="wrong-previous")
    assert wrong_previous.value.code == "revision_conflict"

    first = _revise(parent_box, child["node_id"], criteria_ref, call_id="first")
    assert first["output"]["state"] == "requested"

    # The same proposal replays; another criteria version conflicts while one
    # request is still unapplied.
    duplicate = _revise(parent_box, child["node_id"], criteria_ref,
                        call_id="duplicate")
    assert duplicate["output"]["state"] == "requested"
    assert duplicate["output"]["contract_ref"] == first["output"]["contract_ref"]
    other_ref = _publish_criteria(
        parent_box, call_id="other-criteria",
        document={**CRITERIA, "acceptance": "Another one."})
    with pytest.raises(ControlPlaneError) as second:
        _revise(parent_box, child["node_id"], other_ref, call_id="second")
    assert second.value.code == "revision_conflict"

    child_worker = _worker(cluster.state(run_id), child["node_id"])
    child_box = _worker_box(cluster, child_worker)
    with pytest.raises(ControlPlaneError) as candidate_blocked:
        submit_candidate(child_box, type("S", (), {
            "node_id": child["node_id"], "run_id": run_id,
        })(), round_number=1)
    assert candidate_blocked.value.code == "candidate_mismatch"

    with pytest.raises(ControlPlaneError) as expansion_blocked:
        _split(cluster.api, type("S", (), {"node_id": child["node_id"]})(),
               child_box, [{"local_child_id": "left"}], call_id="nested")
    assert expansion_blocked.value.code == "candidate_mismatch"

    applied = _pump(cluster, run_id,
                    lambda state: _node(state, prefix="exp-")["contract_ref"]
                    != child["contract_ref"])
    rebound = _node(applied, prefix="exp-")
    status = cluster.api.acceptance_status(operator(), run_id,
                                           rebound["node_id"])
    assert status["current"]["contract_ref"] == first["output"]["contract_ref"]
    assert status["pending_revision"] is None


# ---------------------------------------------------------------------------
# Upper bound


def test_local_loosening_cannot_bypass_the_frozen_upper_bound(tmp_path):
    cluster = Cluster(tmp_path / "w05b-upper", None)
    captured: dict = {}

    def agent(api):
        def run(spec, message, state):
            box = toolbox(api, spec, message)
            if is_review_turn(message):
                submit_review(box, spec, message)
                return
            if spec.node_id.startswith("exp-"):
                if message.get("kind") == "acceptance_revised":
                    captured["revised"] = dict(message)
                    ref = box.publish_artifact(
                        {"path": "child.json", "media_type": "application/json"},
                        call_id="child-output", content=b"{}")["output"]
                    captured["ref"] = ref
                    try:
                        box.invoke("node.submit_candidate", {
                            "candidate_id": f"upper-{spec.node_id}",
                            "round": int(message["round"]),
                            "output_refs": [ref],
                        }, call_id="upper-candidate")
                    except ControlPlaneError as error:
                        captured["error"] = error.code
                        captured["message"] = error.message
                return
            _split(api, spec, box, [{
                "local_child_id": "left",
                "template_ref": captured["other_ref"],
            }])
        return run

    document = make_graph(definition_id="w05b-upper")
    other = deepcopy(document["tasks"]["work"])
    other["goal"] = "Produce the constrained child result."
    other["contract"] = {
        "input_schema": {"type": "array"},
        "output_schema": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["media_type"],
                "properties": {"media_type": {"type": "string",
                                              "maxLength": 10}},
            },
        },
        "acceptance": "Frozen upper constraint: a very short media type.",
    }
    document["tasks"]["other"] = other
    captured["other_ref"] = cluster.api.validate_definition(
        publisher(), document)["document"]["tasks"]["other"]
    cluster.backend.agent = agent(cluster.api)
    published = cluster.publish(document)
    dataset = cluster.artifact(b"dataset", "text/plain")
    run = cluster.create_run(published["definition_ref"], {"dataset": dataset})
    run_id = run["run_id"]
    state = _pump(cluster, run_id,
                  lambda state: any(node["node_id"].startswith("exp-")
                                    for node in state["nodes"]))
    parent = _node(state, local_child_id="first")
    child = _node(state, prefix="exp-")
    parent_box = _worker_box(cluster, _worker(state, parent["node_id"]))
    # The local criteria accepts any array, i.e. loses the upper restriction.
    _revise(parent_box, child["node_id"], _publish_criteria(parent_box),
            call_id="revise")

    _pump(cluster, run_id, lambda state: "error" in captured)

    assert captured["error"] == "invalid_argument"
    assert "output_refs" in captured["message"]
    validate_schema_value(CRITERIA["output_schema"], [captured["ref"]],
                          "output_refs")

    current = cluster.api.get_resource(publisher(), "run", run_id)
    cluster.api.set_run_intent(publisher(), run_id, "cancelled",
                               current["revision"], rid("cancel"))


# ---------------------------------------------------------------------------
# Recovery


def test_crash_between_rebind_and_revised_delivery_reenqueues(tmp_path):
    cluster = Cluster(tmp_path / "w05b-recovery", None)

    def agent(api):
        def run(spec, message, state):
            box = toolbox(api, spec, message)
            if is_review_turn(message):
                submit_review(box, spec, message)
                return
            if spec.node_id.startswith("exp-"):
                return
            _split(api, spec, box, [{"local_child_id": "left"}])
        return run

    cluster.backend.agent = agent(cluster.api)
    published = cluster.publish(make_graph(definition_id="w05b-recovery"))
    dataset = cluster.artifact(b"dataset", "text/plain")
    run = cluster.create_run(published["definition_ref"], {"dataset": dataset})
    run_id = run["run_id"]
    state = _pump(cluster, run_id,
                  lambda state: any(node["node_id"].startswith("exp-")
                                    for node in state["nodes"]))
    parent = _node(state, local_child_id="first")
    child = _node(state, prefix="exp-")
    parent_box = _worker_box(cluster, _worker(state, parent["node_id"]))
    _revise(parent_box, child["node_id"], _publish_criteria(parent_box),
            call_id="revise")

    # Let the Scheduler apply the version and rebind the epoch, but pretend the
    # process died before the fresh hand-off was enqueued.
    original = cluster.scheduler._enqueue

    def drop_revised(run_id, worker, tag, payload, actions, *, key=None):
        if payload.get("kind") == "acceptance_revised":
            return False
        return original(run_id, worker, tag, payload, actions, key=key)

    cluster.scheduler._enqueue = drop_revised
    try:
        _pump(cluster, run_id,
              lambda state: _worker(state, child["node_id"])["epoch"] == 1)
    finally:
        cluster.scheduler._enqueue = original

    rebound = _worker(cluster.state(run_id), child["node_id"])
    child_now = _node(cluster.state(run_id), prefix="exp-")
    assert rebound["context"]["contract_ref"] == child_now["contract_ref"]
    key = f"start:{rebound['worker_id']}:1"
    inbox = cluster.api.list_inbox(operator(), rebound["worker_id"])["messages"]
    assert not any(message["delivery_key"] == key for message in inbox)

    _pump(cluster, run_id, lambda state: any(
        message["delivery_key"] == key
        for message in cluster.api.list_inbox(
            operator(), rebound["worker_id"])["messages"]))
    inbox = cluster.api.list_inbox(operator(), rebound["worker_id"])["messages"]
    revised = [message for message in inbox
               if message["payload"].get("kind") == "acceptance_revised"]
    assert revised and revised[0]["epoch"] == 1


def test_supersede_inbox_retires_pending_work_of_the_stopped_superseded_epoch(tmp_path):
    """Only a stopped, already-replaced execution may retire pending work."""
    cluster = Cluster(tmp_path / "w05b-supersede", None)
    captured: dict = {}
    cluster.backend.agent = _revision_agent(cluster.api, captured=captured)
    published = cluster.publish(make_graph(definition_id="w05b-supersede"))
    dataset = cluster.artifact(b"dataset", "text/plain")
    run = cluster.create_run(published["definition_ref"], {"dataset": dataset})
    run_id = run["run_id"]
    actor = cluster.scheduler.actor(run_id)

    def pending_state(state):
        children = [node for node in state["nodes"]
                    if node["node_id"].startswith("exp-")]
        return (children and cluster.api.acceptance_status(
            operator(), run_id, children[0]["node_id"])["pending_revision"])

    state = None
    for _ in range(160):
        cluster.scheduler.reconcile(run_id)
        state = cluster.state(run_id)
        if pending_state(state):
            break
        for item in state["workers"]:
            if item["controller_id"] == "ctl-1":
                cluster.controller.reconcile(run_id, item["worker_id"])
        state = cluster.state(run_id)
        if pending_state(state):
            break
    assert state is not None and pending_state(state)

    child = _node(state, prefix="exp-")
    worker = _worker(state, child["node_id"])
    parent = _node(state, local_child_id="first")
    parent_worker = _worker(state, parent["node_id"])
    assert worker["epoch"] == 0

    # Parent without an applied local contract is rejected first.
    with pytest.raises(ControlPlaneError) as frozen:
        cluster.api.supersede_inbox(actor, parent_worker["worker_id"],
                                    parent_worker["epoch"], rid("parent"))
    assert frozen.value.code == "candidate_mismatch"

    # Before the version applies there is nothing to rebind: reject and leave
    # the execution's pending work untouched.
    cluster.api.enqueue_message(actor, worker["worker_id"],
                                {"kind": "revise", "node_id": child["node_id"]},
                                "probe:revision", rid("probe"))
    with pytest.raises(ControlPlaneError) as active:
        cluster.api.supersede_inbox(actor, worker["worker_id"],
                                    worker["epoch"], rid("active"))
    assert active.value.code == "candidate_mismatch"
    inbox = cluster.api.list_inbox(operator(), worker["worker_id"])["messages"]
    assert next(message for message in inbox
                if message["delivery_key"] == "probe:revision")[
                    "delivery_state"] == "pending"

    # Apply the version and observe the old execution fully stopped.
    cluster.scheduler._apply_reviews(run_id, [])
    assert cluster.scheduler._reconcile_acceptance_revisions(run_id, [])
    state = cluster.state(run_id)
    worker = _worker(state, child["node_id"])
    assert worker["epoch"] == 0 and worker["desired"] == "stopped"
    for _ in range(6):
        cluster.controller.reconcile(run_id, worker["worker_id"])
        state = cluster.state(run_id)
        worker = _worker(state, child["node_id"])
        if worker["observed"] in ("stopped", "failed"):
            break
    assert worker["observed"] in ("stopped", "failed")

    request_id = rid("supersede")
    response = cluster.api.supersede_inbox(actor, worker["worker_id"],
                                           worker["epoch"], request_id)
    assert response["superseded"] == ["probe:revision"]
    assert response["count"] == 1
    inbox = cluster.api.list_inbox(operator(), worker["worker_id"])["messages"]
    probe = next(message for message in inbox
                 if message["delivery_key"] == "probe:revision")
    assert probe["delivery_state"] == "superseded"

    with pytest.raises(ControlPlaneError) as stale:
        cluster.api.supersede_inbox(actor, worker["worker_id"],
                                    worker["epoch"] + 1, rid("stale"))
    assert stale.value.code == "stale_epoch"

    replay = cluster.api.supersede_inbox(actor, worker["worker_id"],
                                         worker["epoch"], request_id)
    assert replay == response

    # The Scheduler now binds the fresh epoch; the retired probe never returns.
    cluster.scheduler.reconcile(run_id)
    rebound = _worker(cluster.state(run_id), child["node_id"])
    assert rebound["epoch"] == 1
    assert rebound["context"]["contract_ref"] != child["contract_ref"]

    # The replacement execution already holds the current contract: its
    # legitimate pending work must stay pending, never be retired.
    cluster.api.enqueue_message(actor, rebound["worker_id"],
                                {"kind": "continue", "brief": "current work"},
                                "probe:current", rid("current"))
    with pytest.raises(ControlPlaneError) as current:
        cluster.api.supersede_inbox(actor, rebound["worker_id"],
                                    rebound["epoch"], rid("current-supersede"))
    assert current.value.code == "invalid_argument"
    inbox = cluster.api.list_inbox(operator(), rebound["worker_id"])["messages"]
    assert next(message for message in inbox
                if message["delivery_key"] == "probe:current")[
                    "delivery_state"] == "pending"


# ---------------------------------------------------------------------------
# W05b review-fix regressions


def _until_pending_revision(cluster, run_id):
    """Stop between Controller delivery and the next Scheduler pass."""
    for _ in range(160):
        cluster.scheduler.reconcile(run_id)
        state = cluster.state(run_id)
        children = [node for node in state["nodes"]
                    if node["node_id"].startswith("exp-")]
        if children and cluster.api.acceptance_status(
                operator(), run_id, children[0]["node_id"])["pending_revision"]:
            return state
        for item in state["workers"]:
            if item["controller_id"] == "ctl-1":
                cluster.controller.reconcile(run_id, item["worker_id"])
        state = cluster.state(run_id)
        children = [node for node in state["nodes"]
                    if node["node_id"].startswith("exp-")]
        if children and cluster.api.acceptance_status(
                operator(), run_id, children[0]["node_id"])["pending_revision"]:
            return state
    raise AssertionError("the pending acceptance revision was not reached")


def test_ordinary_json_criteria_is_accepted(tmp_path):
    cluster = Cluster(tmp_path / "w05b-ordinary-json", None)
    captured: dict = {}
    cluster.backend.agent = _revision_agent(
        cluster.api, captured=captured, criteria_canonical=False)
    published = cluster.publish(make_graph(definition_id="w05b-ordinary-json"))
    run = cluster.create_run(published["definition_ref"],
                             {"dataset": cluster.artifact(b"dataset")})
    state = cluster.drive(run["run_id"])
    assert state["run"]["status"] == "completed"
    child = _node(state, prefix="exp-")
    status = cluster.api.acceptance_status(operator(), run["run_id"],
                                           child["node_id"])
    assert [entry["state"] for entry in status["history"]] == ["applied"]
    assert status["current"]["contract"] == CRITERIA


def test_fresh_epoch_continues_after_one_idle_turn(tmp_path):
    cluster = Cluster(tmp_path / "w05b-idle", None)
    captured: dict = {}
    cluster.backend.agent = _revision_agent(
        cluster.api, captured=captured, delay_revised=True)
    document = make_graph(definition_id="w05b-idle")
    document["budget_limits"]["max_continuations"] = 4
    published = cluster.publish(document)
    run = cluster.create_run(published["definition_ref"],
                             {"dataset": cluster.artifact(b"dataset")})
    state = cluster.drive(run["run_id"])
    assert state["run"]["status"] == "completed"
    child = _node(state, prefix="exp-")
    worker = _worker(state, child["node_id"])
    assert worker["epoch"] == 1
    rounds = [item["round"] for item in state["candidates"]
              if item["node_id"] == child["node_id"]]
    assert rounds == [1, 2]


def test_second_revision_does_not_wait_for_old_epoch_reply(tmp_path):
    cluster = Cluster(tmp_path / "w05b-second", None)
    captured: dict = {}
    cluster.backend.agent = _revision_agent(
        cluster.api, captured=captured, ask_decision=True)
    document = make_graph(definition_id="w05b-second")
    document["budget_limits"]["max_continuations"] = 4
    published = cluster.publish(document)
    run = cluster.create_run(published["definition_ref"],
                             {"dataset": cluster.artifact(b"dataset")})
    run_id = run["run_id"]
    state = _pump(cluster, run_id,
                  lambda state: any(worker["epoch"] == 1
                                    for worker in state["workers"]))
    child = _node(state, prefix="exp-")
    current_ref = child["contract_ref"]
    assert current_ref != child["template_ref"]
    parent = _node(state, local_child_id="first")
    parent_box = _worker_box(cluster, _worker(state, parent["node_id"]))
    second = {**CRITERIA, "acceptance": CRITERIA["acceptance"] + " Again."}
    criteria_ref = _publish_criteria(parent_box, call_id="second",
                                     document=second)
    parent_box.invoke("node.revise_acceptance", {
        "target_node_id": child["node_id"],
        "previous_ref": current_ref,
        "criteria_ref": criteria_ref,
    }, call_id="second-revision")
    cluster.scheduler.reconcile(run_id)
    after = cluster.api.get_resource(operator(), "node", child["node_id"])
    assert after["contract_ref"] != current_ref


def test_old_epoch_cannot_expand_under_the_replaced_contract(tmp_path):
    cluster = Cluster(tmp_path / "w05b-expand", None)
    captured: dict = {}
    cluster.backend.agent = _revision_agent(cluster.api, captured=captured)
    published = cluster.publish(make_graph(definition_id="w05b-expand"))
    dataset = cluster.artifact(b"dataset")
    run = cluster.create_run(published["definition_ref"], {"dataset": dataset})
    run_id = run["run_id"]
    state = _until_pending_revision(cluster, run_id)
    child = _node(state, prefix="exp-")
    old_contract = child["contract_ref"]
    worker = _worker(state, child["node_id"])
    cluster.scheduler._apply_reviews(run_id, [])
    assert cluster.scheduler._reconcile_acceptance_revisions(run_id, [])
    stopped = _worker(cluster.state(run_id), child["node_id"])
    assert stopped["epoch"] == worker["epoch"] and stopped["desired"] == "stopped"
    old_box = WorkerToolbox(
        cluster.api, run_id=run_id, node_id=child["node_id"],
        worker_id=worker["worker_id"], epoch=worker["epoch"],
        tool_profile=worker["tool_profile"], contract_ref=old_contract)
    expansion_id = "late-expansion"
    with pytest.raises(ControlPlaneError):
        old_box.invoke("node.request_expansion", {
            "expansion_id": expansion_id, "join_rule": "all",
            "children": [{"local_child_id": "stale",
                          "input_bindings": {"dataset": dataset},
                          "reviewer_ref": "parent"}]}, call_id="late")
    assert all(item["expansion_id"] != expansion_id
               for item in cluster.state(run_id)["expansion_requests"])


def test_cancelled_unapplied_revision_is_not_a_live_pending_task(tmp_path):
    cluster = Cluster(tmp_path / "w05b-cancel", None)
    captured: dict = {}
    cluster.backend.agent = _revision_agent(cluster.api, captured=captured)
    published = cluster.publish(make_graph(definition_id="w05b-cancel"))
    run = cluster.create_run(published["definition_ref"],
                             {"dataset": cluster.artifact(b"dataset")})
    run_id = run["run_id"]
    state = _until_pending_revision(cluster, run_id)
    child = _node(state, prefix="exp-")
    current = cluster.api.get_resource(publisher(), "run", run_id)
    cluster.api.set_run_intent(publisher(), run_id, "cancelled",
                               current["revision"], rid("cancel"))
    final = cluster.drive(run_id)
    assert final["run"]["status"] == "cancelled"
    status = cluster.api.acceptance_status(operator(), run_id, child["node_id"])
    assert status["pending_revision"] is None
    assert status["history"] and status["history"][0]["state"] == "not_applicable"


# ---------------------------------------------------------------------------
# Real Pi delivery


def test_pi_revised_delivery_exposes_both_bounds_and_prior_feedback(tmp_path):
    backend = PiBackend(tmp_path / "pi")
    state = {"worker_id": "w1", "node_id": "n1", "epoch": 1,
             "agent_ref": "agent", "context": {"role": "worker",
                                               "contract_ref": "local",
                                               "template_ref": "upper"}}
    feedback_ref = {"id": "sha256:" + "a" * 64, "sha256": "a" * 64,
                    "size": 5, "media_type": "text/plain"}
    message = {
        "kind": "acceptance_revised",
        "brief": "continue under the revised version",
        "acceptance": "local acceptance",
        "output_schema": {"type": "array"},
        "upper_acceptance": "frozen acceptance",
        "upper_output_schema": {"type": "array"},
        "previous_contract_ref": "upper",
        "round": 2,
        "prior_feedback": [{"candidate_id": "c1", "verdict": "revise",
                            "feedback_ref": feedback_ref}],
        "accepted_children": [{"node_id": "exp-a", "candidate_id": "cand-a"}],
        "open_subtasks": [{"node_id": "exp-b", "state": "running"}],
    }
    prompt = backend._prompt(state, message, ("node.submit_candidate",))
    assert "ACCEPTANCE:\nlocal acceptance" in prompt
    assert "UPPER ACCEPTANCE (frozen" in prompt
    assert "frozen acceptance" in prompt
    assert "PREVIOUS CONTRACT: upper" in prompt
    assert "ROUND: 2" in prompt
    assert "ACCEPTED CHILDREN" in prompt and "exp-a" in prompt
    assert "OPEN SUBTASKS" in prompt and "exp-b" in prompt
    assert ("review feedback", feedback_ref) in backend._message_refs(message)
