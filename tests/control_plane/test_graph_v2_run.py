"""Graph /2 run semantics: declared stages, acceptance, restart and cancel."""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from conftest import make_graph
from helpers import (Cluster, accepting_agent, is_review_turn, operator,
                     publisher, reader, rid, submit_candidate, submit_review,
                     toolbox)

from node_runtime.control_plane.resources import Actor, ControlPlaneError
from node_runtime.control_plane.tools import WorkerToolbox


def _stage(state, name):
    return next(node for node in state["nodes"]
                if node["local_child_id"] == name)


def _review_nodes(state):
    return [node for node in state["nodes"] if node.get("closure")]


def _candidate(state, candidate_id):
    return next(item for item in state["candidates"]
                if item["candidate_id"] == candidate_id)


def _events(cluster, run_id):
    return cluster.api.read_events(operator(), run_id, limit=1000)["events"]


def _chain_stages():
    return {
        "extract": {"task_ref": "work", "reviewer_ref": "check", "needs": [],
                    "inputs": {"dataset": {"run_input": "dataset"}}},
        "transform": {"task_ref": "work", "reviewer_ref": "check",
                      "needs": ["extract"],
                      "inputs": {"upstream": {"from": "extract"}}},
    }


def _merge_stages():
    return {
        "left": {"task_ref": "work", "reviewer_ref": "check", "needs": [],
                 "inputs": {"dataset": {"run_input": "dataset"}}},
        "right": {"task_ref": "work", "reviewer_ref": "check", "needs": [],
                  "inputs": {"dataset": {"run_input": "dataset"}}},
        "merge": {"task_ref": "work", "reviewer_ref": "check",
                  "needs": ["left", "right"],
                  "inputs": {"left": {"from": "left"},
                             "right": {"from": "right"}}},
    }


def test_two_stage_chain_freezes_upstream_accepted_outputs(cluster):
    document = make_graph(definition_id="chain", stages=_chain_stages())
    published = cluster.publish(document)
    dataset = cluster.artifact(b"dataset", "text/plain")
    run = cluster.create_run(published["definition_ref"], {"dataset": dataset})

    state = cluster.drive(run["run_id"])
    assert state["run"]["status"] == "completed"
    assert state["run"]["summary"]["stages"] == {
        node["node_id"]: "succeeded" for node in state["nodes"]
        if node["local_child_id"] is not None}
    assert {node["local_child_id"] for node in state["nodes"]
            if node["local_child_id"] is not None} == {"extract", "transform"}

    extract = _stage(state, "extract")
    transform = _stage(state, "transform")
    assert extract["state"] == transform["state"] == "succeeded"
    assert [ref["id"] for ref in extract["input_refs"]] == [dataset["id"]]

    upstream = _candidate(state, extract["accepted_candidate_ref"])
    assert transform["input_refs"] == upstream["output_refs"]
    assert (transform["input_refs"][0]["id"] != dataset["id"])

    # The downstream turn could only start after the upstream acceptance fact.
    events = _events(cluster, run["run_id"])
    accepted_seq = next(event["seq"] for event in events
                        if event["kind"] == "review_submitted"
                        and event["node_id"] == extract["node_id"])
    transform_worker = next(worker for worker in state["workers"]
                            if worker["node_id"] == transform["node_id"])
    start_seq = next(event["seq"] for event in events
                     if event["kind"] == "message_enqueued"
                     and event["worker_id"] == transform_worker["worker_id"])
    assert accepted_seq < start_seq

    for stage in (extract, transform):
        review = next(node for node in _review_nodes(state)
                      if node["closure"]["target_node_id"] == stage["node_id"])
        assert review["closure"]["candidate_id"] == stage["accepted_candidate_ref"]
        assert review["state"] == "succeeded"
        assert review["closure"]["worker_id"] != stage["node_id"] + "-w1"


def test_parallel_branches_merge_only_after_both_accept(cluster):
    document = make_graph(definition_id="merge", stages=_merge_stages())
    published = cluster.publish(document)
    dataset = cluster.artifact(b"dataset", "text/plain")
    run = cluster.create_run(published["definition_ref"], {"dataset": dataset})

    state = cluster.drive(run["run_id"])
    assert state["run"]["status"] == "completed"
    left, right, merge = (_stage(state, name)
                          for name in ("left", "right", "merge"))
    assert left["state"] == right["state"] == merge["state"] == "succeeded"
    assert {ref["id"] for ref in merge["input_refs"]} == {
        ref["id"]
        for stage in (left, right)
        for ref in _candidate(state, stage["accepted_candidate_ref"])["output_refs"]
    }

    events = _events(cluster, run["run_id"])
    merge_worker = next(worker for worker in state["workers"]
                        if worker["node_id"] == merge["node_id"])
    merge_seq = next(event["seq"] for event in events
                     if event["kind"] == "message_enqueued"
                     and event["worker_id"] == merge_worker["worker_id"])
    for stage in (left, right):
        accepted_seq = next(event["seq"] for event in events
                            if event["kind"] == "review_submitted"
                            and event["node_id"] == stage["node_id"])
        assert accepted_seq < merge_seq


def test_review_receipt_is_a_bound_independent_checker(cluster):
    document = make_graph(definition_id="receipt")
    published = cluster.publish(document)
    dataset = cluster.artifact(b"dataset", "text/plain")
    run = cluster.create_run(published["definition_ref"], {"dataset": dataset})

    state = cluster.drive(run["run_id"])
    stage = _stage(state, "first")
    review = next(node for node in _review_nodes(state)
                  if node["closure"]["target_node_id"] == stage["node_id"])
    assert review["parent_id"] == stage["node_id"]
    assert review["closure"] == {
        "kind": "review_receipt",
        "target_node_id": stage["node_id"],
        "candidate_id": stage["accepted_candidate_ref"],
        "reviewer_ref": stage["reviewer_ref"],
        "worker_id": review["node_id"] + "-w1",
    }
    assert review["template_ref"] == stage["reviewer_ref"]
    assert review["state"] == "succeeded"

    target_worker = next(worker for worker in state["workers"]
                         if worker["node_id"] == stage["node_id"])
    checker = next(worker for worker in state["workers"]
                   if worker["worker_id"] == review["closure"]["worker_id"])
    assert checker["node_id"] != target_worker["node_id"]
    assert checker["worker_id"] != target_worker["worker_id"]
    receipt = next(item for item in state["reviews"]
                   if item["node_id"] == stage["node_id"])
    assert receipt["worker_id"] == checker["worker_id"]
    assert receipt["worker_id"] != target_worker["worker_id"]


def _chain_cluster(cluster):
    published = cluster.publish(make_graph(definition_id="isolated",
                                           stages=_chain_stages()))
    dataset = cluster.artifact(b"dataset", "text/plain")
    run = cluster.create_run(published["definition_ref"], {"dataset": dataset})
    return run, cluster.drive(run["run_id"])


def _producer_only(api):
    def run(spec, message, state):
        box = toolbox(api, spec, message)
        if is_review_turn(message):
            return
        submit_candidate(box, spec, round_number=int(message.get("round") or 1))
    return run


def _pump_until(cluster, run_id, predicate, limit=80):
    for _ in range(limit):
        cluster.scheduler.reconcile(run_id)
        for worker in cluster.state(run_id)["workers"]:
            if worker["controller_id"] == "ctl-1":
                cluster.controller.reconcile(run_id, worker["worker_id"])
        state = cluster.state(run_id)
        if predicate(state):
            return state
    raise AssertionError("the expected intermediate state was not reached")


def test_reviewer_identity_is_bound_to_one_candidate(tmp_path):
    cluster = Cluster(tmp_path / "identity", None)
    cluster.backend.agent = _producer_only(cluster.api)
    document = make_graph(definition_id="identity", stages=_merge_stages())
    published = cluster.publish(document)
    dataset = cluster.artifact(b"dataset", "text/plain")
    run = cluster.create_run(published["definition_ref"], {"dataset": dataset})

    def both_candidates_waiting(state):
        closures = [node for node in state["nodes"] if node.get("closure")]
        if len(closures) != 2:
            return False
        workers = {worker["worker_id"]: worker for worker in state["workers"]}
        return all(closure["closure"]["worker_id"] in workers
                   and workers[closure["closure"]["worker_id"]]["observed"] == "idle"
                   for closure in closures)

    state = _pump_until(cluster, run["run_id"], both_candidates_waiting)
    nodes = {node["node_id"]: node for node in state["nodes"]}
    closures = {node["closure"]["target_node_id"]: node
                for node in state["nodes"] if node.get("closure")}
    left_id, right_id = _stage(state, "left")["node_id"], _stage(state, "right")["node_id"]
    candidates = {item["node_id"]: item for item in state["candidates"]}
    left_closure, right_closure = closures[left_id], closures[right_id]
    left_checker = next(worker for worker in state["workers"]
                        if worker["worker_id"] == left_closure["closure"]["worker_id"])
    left_target_worker = next(worker for worker in state["workers"]
                              if worker["node_id"] == left_id)

    def checker_actor(epoch):
        return Actor(left_checker["worker_id"], {"reviewer"},
                     run_ids={run["run_id"]},
                     worker_id=left_checker["worker_id"], epoch=epoch)

    feedback = cluster.api.put_artifact(
        checker_actor(left_checker["epoch"]), rid("feedback"),
        b"left accepted", "text/plain")["artifact"]

    def attempt(actor, node_id, candidate_id):
        return cluster.api.append_fact(actor, {
            "kind": "review_submitted",
            "run_id": run["run_id"],
            "node_id": node_id,
            "candidate_id": candidate_id,
            "candidate_round": 1,
            "verdict": "accept",
            "feedback_ref": feedback,
        }, rid("review-attempt"), nodes[node_id]["revision"])

    with pytest.raises(ControlPlaneError) as wrong_candidate:
        attempt(checker_actor(left_checker["epoch"]), left_id,
                candidates[right_id]["candidate_id"])
    assert wrong_candidate.value.code == "unauthorized"

    with pytest.raises(ControlPlaneError) as stale:
        attempt(checker_actor(left_checker["epoch"] + 1), left_id,
                candidates[left_id]["candidate_id"])
    assert stale.value.code == "stale_epoch"

    with pytest.raises(ControlPlaneError) as own_review:
        attempt(Actor(left_target_worker["worker_id"], {"reviewer"},
                      run_ids={run["run_id"]},
                      worker_id=left_target_worker["worker_id"],
                      epoch=left_target_worker["epoch"]),
                left_id, candidates[left_id]["candidate_id"])
    assert own_review.value.code == "unauthorized"

    # A Task/Agent reference may name the reviewer role, but only the actual
    # Worker execution identity can submit the receipt.
    impersonated = Actor("task-ref-as-actor", {"reviewer"},
                         run_ids={run["run_id"]},
                         worker_id=left_checker["worker_id"],
                         epoch=left_checker["epoch"])
    with pytest.raises(ControlPlaneError) as not_the_worker:
        attempt(impersonated, left_id, candidates[left_id]["candidate_id"])
    assert not_the_worker.value.code == "unauthorized"

    accepted = attempt(checker_actor(left_checker["epoch"]), left_id,
                       candidates[left_id]["candidate_id"])
    assert accepted["verdict"] == "accept"
    cluster.scheduler.reconcile(run["run_id"])
    state = cluster.state(run["run_id"])
    nodes = {node["node_id"]: node for node in state["nodes"]}
    assert nodes[left_id]["state"] == "succeeded"
    assert nodes[left_closure["node_id"]]["state"] == "succeeded"
    assert nodes[right_id]["state"] == "awaiting_review"


def test_restart_and_idempotent_replay_do_not_duplicate_instances(cluster):
    document = make_graph(definition_id="restart")
    published = cluster.publish(document, request_id="publish-fixed")
    dataset = cluster.artifact(b"dataset", "text/plain")
    run = cluster.create_run(published["definition_ref"], {"dataset": dataset},
                             request_id="run-fixed")
    first = cluster.drive(run["run_id"])
    node_ids = {node["node_id"] for node in first["nodes"]}
    candidate_ids = {item["candidate_id"] for item in first["candidates"]}
    review_ids = {item["event_id"] for item in first["reviews"]}

    restarted = Cluster(cluster.root, None)
    restarted.backend.agent = accepting_agent(restarted.api)
    replay = restarted.publish(document, request_id="publish-fixed")
    assert replay["definition_ref"] == published["definition_ref"]
    replay_run = restarted.create_run(replay["definition_ref"],
                                      {"dataset": dataset},
                                      request_id="run-fixed")
    assert replay_run["run_id"] == run["run_id"]

    second = restarted.drive(run["run_id"])
    assert second["run"]["status"] == "completed"
    assert {node["node_id"] for node in second["nodes"]} == node_ids
    assert {item["candidate_id"] for item in second["candidates"]} == candidate_ids
    assert {item["event_id"] for item in second["reviews"]} == review_ids


def test_request_id_reuse_and_revision_checks_are_enforced(cluster):
    document = make_graph(definition_id="cas")
    published = cluster.publish(document, request_id="publish-cas")
    dataset = cluster.artifact(b"dataset", "text/plain")
    run = cluster.create_run(published["definition_ref"], {"dataset": dataset},
                             request_id="run-cas")

    with pytest.raises(ControlPlaneError) as publish_conflict:
        cluster.publish(make_graph(definition_id="other"),
                        request_id="publish-cas")
    assert publish_conflict.value.code == "idempotency_conflict"

    other = cluster.artifact(b"other", "text/plain")
    with pytest.raises(ControlPlaneError) as run_conflict:
        cluster.create_run(published["definition_ref"], {"dataset": other},
                           request_id="run-cas")
    assert run_conflict.value.code == "idempotency_conflict"

    paused = cluster.api.set_run_intent(publisher(), run["run_id"], "paused", 1,
                                        rid("intent"))
    assert paused["revision"] == 2
    with pytest.raises(ControlPlaneError) as stale:
        cluster.api.set_run_intent(publisher(), run["run_id"], "paused", 1,
                                   rid("intent"))
    assert stale.value.code == "revision_conflict"


def test_cancel_intent_terminates_declared_stages(cluster):
    published = cluster.publish(make_graph(definition_id="cancel",
                                           stages=_chain_stages()))
    dataset = cluster.artifact(b"dataset", "text/plain")
    run = cluster.create_run(published["definition_ref"], {"dataset": dataset})
    cluster.scheduler.reconcile(run["run_id"])
    current = cluster.api.get_resource(publisher(), "run", run["run_id"])

    cluster.api.set_run_intent(publisher(), run["run_id"], "cancelled",
                               current["revision"], rid("intent"))
    state = cluster.drive(run["run_id"])
    assert state["run"]["status"] == "cancelled"
    assert all(node["state"] in ("cancelled", "succeeded")
               for node in state["nodes"])


def test_declared_stage_budget_is_checked_by_the_bootstrap_plan(cluster):
    stages = {
        f"stage{index}": {"task_ref": "work", "reviewer_ref": "check",
                          "needs": [], "inputs": {}}
        for index in range(3)
    }
    document = make_graph(definition_id="budget", stages=stages)
    document["budget_limits"]["max_nodes"] = 2
    published = cluster.publish(document)
    dataset = cluster.artifact(b"dataset", "text/plain")
    run = cluster.create_run(published["definition_ref"], {"dataset": dataset})

    with pytest.raises(ControlPlaneError) as exceeded:
        cluster.scheduler.reconcile(run["run_id"])
    assert exceeded.value.code == "budget_exhausted"
    state = cluster.state(run["run_id"])
    assert state["run"]["status"] == "pending"
    assert state["nodes"] == []


# -- dynamic split ------------------------------------------------------

def _split_agent(api):
    def run(spec, message, state):
        box = toolbox(api, spec, message)
        if is_review_turn(message):
            submit_review(box, spec, message)
            return
        if message.get("kind") == "children_joined":
            submit_candidate(box, spec, round_number=1)
            return
        if message.get("kind") == "accepted":
            return
        if message["node_id"].startswith("stage-"):
            box.invoke("node.request_expansion", {
                "expansion_id": f"split-{spec.node_id}",
                "join_rule": "all",
                "children": [{"local_child_id": name}
                             for name in ("left", "right")],
            }, call_id="split")
            return
        submit_candidate(box, spec, round_number=1)
    return run


def test_dynamic_split_runs_children_then_parent_candidate(tmp_path):
    cluster = Cluster(tmp_path / "split", None)
    cluster.backend.agent = _split_agent(cluster.api)
    document = make_graph(definition_id="split")
    published = cluster.publish(document)
    dataset = cluster.artifact(b"dataset", "text/plain")
    run = cluster.create_run(published["definition_ref"], {"dataset": dataset})

    state = cluster.drive(run["run_id"])
    assert state["run"]["status"] == "completed"
    parent = _stage(state, "first")
    children = [node for node in state["nodes"]
                if node.get("expansion_id") is not None]
    assert {node["local_child_id"] for node in children} == {"left", "right"}
    assert all(node["state"] == "succeeded" for node in children)

    # Accepting both children never completes the parent task by itself: the
    # parent still had to submit and pass its own candidate.
    assert parent["state"] == "succeeded"
    parent_candidate = _candidate(state, parent["accepted_candidate_ref"])
    assert parent_candidate["round"] == 1
    assert state["expansions"]

    child_reviews = [item for item in state["reviews"]
                     if item["node_id"] in {node["node_id"] for node in children}]
    assert len(child_reviews) == len(children)
    parent_worker = next(worker for worker in state["workers"]
                         if worker["node_id"] == parent["node_id"])
    assert {item["worker_id"] for item in child_reviews} == {
        parent_worker["worker_id"]}
    parent_review = next(item for item in state["reviews"]
                         if item["node_id"] == parent["node_id"])
    assert parent_review["worker_id"] != parent_worker["worker_id"]


def _selection_graph(definition_id):
    document = make_graph(definition_id=definition_id)
    other = deepcopy(document["tasks"]["work"])
    other["goal"] = "Complete the other assigned step."
    document["tasks"]["other"] = other
    return document


def _selection_agent(api, refs, *, contract_only=False):
    def run(spec, message, state):
        box = toolbox(api, spec, message)
        if is_review_turn(message):
            submit_review(box, spec, message)
            return
        if message.get("kind") == "children_joined":
            submit_candidate(box, spec, round_number=1)
            return
        if message.get("kind") == "accepted":
            return
        if message["node_id"].startswith("stage-"):
            right = {"local_child_id": "right"}
            if contract_only:
                right["contract_ref"] = refs["other"]
            else:
                right["template_ref"] = refs["other"]
                right["contract_ref"] = refs["other"]
            box.invoke("node.request_expansion", {
                "expansion_id": f"split-{spec.node_id}",
                "join_rule": "all",
                "children": [{"local_child_id": "left"}, right],
            }, call_id="split")
            return
        submit_candidate(box, spec, round_number=1)
    return run


def test_expansion_explicit_template_ref_selects_another_approved_task(tmp_path):
    cluster = Cluster(tmp_path / "select-task", None)
    document = _selection_graph("select-task")
    refs = cluster.api.validate_definition(
        publisher(), document)["document"]["tasks"]
    cluster.backend.agent = _selection_agent(cluster.api, refs)
    published = cluster.publish(document)
    dataset = cluster.artifact(b"dataset", "text/plain")
    run = cluster.create_run(published["definition_ref"], {"dataset": dataset})

    state = cluster.drive(run["run_id"])
    assert state["run"]["status"] == "completed"
    left, right = _stage(state, "left"), _stage(state, "right")
    assert right["template_ref"] == right["contract_ref"] == refs["other"]
    assert left["template_ref"] == left["contract_ref"] == refs["work"]


def test_expansion_contract_ref_cannot_switch_the_inherited_task(tmp_path):
    cluster = Cluster(tmp_path / "switch-task", None)
    document = _selection_graph("switch-task")
    refs = cluster.api.validate_definition(
        publisher(), document)["document"]["tasks"]
    cluster.backend.agent = _selection_agent(
        cluster.api, refs, contract_only=True)
    published = cluster.publish(document)
    dataset = cluster.artifact(b"dataset", "text/plain")
    run = cluster.create_run(published["definition_ref"], {"dataset": dataset})
    run_id = run["run_id"]

    cluster.scheduler.reconcile(run_id)
    state = cluster.state(run_id)
    parent = _stage(state, "first")
    worker = next(item for item in state["workers"]
                  if item["node_id"] == parent["node_id"])
    cluster.controller.reconcile(run_id, worker["worker_id"])
    request_id = cluster.state(run_id)["expansion_requests"][0]["expansion_id"]
    requested = cluster.api.get_resource(operator(), "expansion_request",
                                         request_id)
    assert requested["payload"]["children"][1]["contract_ref"] == refs["other"]
    assert requested["payload"]["children"][1].get("template_ref") is None

    cluster.scheduler.reconcile(run_id)
    rejected = cluster.api.get_resource(operator(), "expansion_request",
                                        request_id)
    assert rejected["state"] == "rejected"
    assert rejected["reason"]["code"] == "invalid_argument"
    assert not [node for node in cluster.state(run_id)["nodes"]
                if node["expansion_id"] is not None]


def test_one_inflight_slot_is_released_by_waiting_parent_and_children(tmp_path):
    cluster = Cluster(tmp_path / "quota", None)
    cluster.backend.agent = _split_agent(cluster.api)
    document = make_graph(definition_id="quota")
    document["budget_limits"]["max_inflight_nodes"] = 1
    published = cluster.publish(document)
    dataset = cluster.artifact(b"dataset", "text/plain")
    run = cluster.create_run(published["definition_ref"], {"dataset": dataset})

    # A parent that yielded for its children, and children that yielded for
    # their own checkers, must not keep the only dispatch slot: otherwise the
    # declared checkers and the resumed parent could never run.
    state = cluster.drive(run["run_id"], limit=300)
    assert state["run"]["status"] == "completed"
    assert all(node["state"] == "succeeded"
               for node in state["nodes"] if node.get("local_child_id"))


# -- worker/parent decision (WPD) ---------------------------------------

def _decision_agent(api, decisions, checks):
    def run(spec, message, state):
        box = toolbox(api, spec, message)
        if is_review_turn(message):
            submit_review(box, spec, message)
            return
        kind = message.get("kind")
        if kind == "decision_request":
            decision_id = message["decision_id"]
            child_node_id = message["child_node_id"]
            child_worker_id = f"{child_node_id}-w1"
            answer = "continue with the parent routing"

            def attempt(worker_id, epoch, text, tag):
                actor = Actor(worker_id, {"worker"}, run_ids={spec.run_id},
                              worker_id=worker_id, epoch=epoch)
                try:
                    api.append_fact(actor, {
                        "kind": "decision_resolved", "run_id": spec.run_id,
                        "node_id": child_node_id, "worker_id": worker_id,
                        "epoch": epoch, "decision_id": decision_id,
                        "answer": text,
                    }, rid(tag), 1)
                except ControlPlaneError as error:
                    checks.append((tag, error.code))
                else:
                    checks.append((tag, "accepted"))

            if not checks:
                attempt(child_worker_id, 0, answer, "wrong-authority")
                attempt(spec.worker_id, spec.epoch + 1, answer, "stale-epoch")
            box.invoke("node.resolve_decision", {
                "decision_id": decision_id, "answer": answer,
            }, call_id=f"resolve-{decision_id}")
            if len(checks) == 2:
                duplicate = api.append_fact(
                    Actor(spec.worker_id, {"worker"}, run_ids={spec.run_id},
                          worker_id=spec.worker_id, epoch=spec.epoch),
                    {"kind": "decision_resolved", "run_id": spec.run_id,
                     "node_id": child_node_id, "worker_id": spec.worker_id,
                     "epoch": spec.epoch, "decision_id": decision_id,
                     "answer": answer},
                    rid("reply-repeat"), 1)
                checks.append(("duplicate", duplicate["duplicate"]))
            decisions.append((decision_id, spec.worker_id, spec.epoch))
            return
        if kind == "decision_resolved":
            submit_candidate(box, spec, round_number=1)
            return
        if kind == "children_joined":
            submit_candidate(box, spec, round_number=1)
            return
        if kind == "accepted":
            return
        if spec.node_id.startswith("stage-"):
            box.invoke("node.request_expansion", {
                "expansion_id": f"split-{spec.node_id}",
                "join_rule": "all",
                "children": [{"local_child_id": name}
                             for name in ("left", "right")],
            }, call_id="split")
            return
        box.invoke("node.request_decision", {
            "decision_id": f"ask-{spec.node_id}",
            "reason": "the child needs its parent's routing decision",
            "question": "Should the child continue?",
        }, call_id=f"ask-{spec.node_id}")
    return run


def test_child_waits_for_parent_decision_and_resumes(tmp_path):
    cluster = Cluster(tmp_path / "decision", None)
    decisions, checks = [], []
    cluster.backend.agent = _decision_agent(cluster.api, decisions, checks)
    published = cluster.publish(make_graph(definition_id="decision"))
    dataset = cluster.artifact(b"dataset", "text/plain")
    run = cluster.create_run(published["definition_ref"], {"dataset": dataset})

    state = cluster.drive(run["run_id"], limit=200)
    assert state["run"]["status"] == "completed"
    children = [node for node in state["nodes"]
                if node.get("expansion_id") is not None]
    assert len(children) == 2
    assert all(node["state"] == "succeeded" for node in children)
    assert len(decisions) == 2
    assert all(item["state"] == "resolved" for item in state["decision_requests"])
    assert {item["resolved_by"] for item in state["decision_requests"]} == {
        worker["worker_id"] for worker in state["workers"]
        if worker["node_id"] == _stage(state, "first")["node_id"]}

    # The answer is committed as a fact by the parent's real worker identity.
    events = _events(cluster, run["run_id"])
    decision_id, parent_worker_id, parent_epoch = decisions[0]
    decision = cluster.api.get_resource(publisher(), "decision_request", decision_id)
    child_worker = next(worker for worker in state["workers"]
                        if worker["node_id"] == decision["node_id"])
    requested_seq = next(event["seq"] for event in events
                         if event["kind"] == "decision_requested"
                         and event["payload"]["decision_id"] == decision_id)
    resolved = next(event for event in events
                    if event["kind"] == "decision_resolved"
                    and event["payload"]["decision_id"] == decision_id)
    reply_enqueued_seq = next(event["seq"] for event in events
                              if event["kind"] == "message_enqueued"
                              and event["worker_id"] == child_worker["worker_id"]
                              and event["payload"]["delivery_key"].startswith(
                                  "decision-reply:"))
    candidate_seq = next(event["seq"] for event in events
                         if event["kind"] == "candidate_submitted"
                         and event["node_id"] == decision["node_id"])
    assert requested_seq < resolved["seq"] < reply_enqueued_seq < candidate_seq
    next(event for event in events
         if event["kind"] == "message_delivered"
         and event["worker_id"] == child_worker["worker_id"]
         and event["payload"]["delivery_key"].startswith("decision-reply:"))
    assert resolved["worker_id"] == parent_worker_id
    assert resolved["epoch"] == parent_epoch

    # The child's own Worker cannot answer, a stale parent epoch cannot answer,
    # and replaying the same answer is a duplicate rather than a second reply.
    assert checks == [("wrong-authority", "unauthorized"),
                      ("stale-epoch", "stale_epoch"),
                      ("duplicate", True)]


# -- rework -------------------------------------------------------------

def _revise_once_agent(api):
    def run(spec, message, state):
        box = toolbox(api, spec, message)
        if is_review_turn(message):
            verdict = "revise" if message.get("candidate_round") == 1 else "accept"
            submit_review(box, spec, message, verdict=verdict)
            return
        if message.get("kind") in ("revision", "continue"):
            # Round 2 is started by the test only after the revision
            # feedback delivery is durable; a turn running inside that
            # delivery may not submit ahead of its own gate.
            return
        submit_candidate(box, spec, round_number=int(message.get("round") or 1))
    return run


def test_rework_reuses_one_worker_and_consumes_feedback(tmp_path):
    cluster = Cluster(tmp_path / "rework", None)
    cluster.backend.agent = _revise_once_agent(cluster.api)
    published = cluster.publish(make_graph(definition_id="rework"))
    dataset = cluster.artifact(b"dataset", "text/plain")
    run = cluster.create_run(published["definition_ref"], {"dataset": dataset})

    def revised_and_fed(state):
        stage = _stage(state, "first")
        return (stage["state"] == "running" and stage["round"] == 1
                and any(event["kind"] == "message_delivered"
                        and str((event.get("payload") or {}).get(
                            "delivery_key", "")).startswith("revise:")
                        for event in _events(cluster, run["run_id"])))

    state = _pump_until(cluster, run["run_id"], revised_and_fed)
    stage = _stage(state, "first")
    assert stage["accepted_candidate_ref"] is None
    target_workers = [worker for worker in state["workers"]
                      if worker["node_id"] == stage["node_id"]]
    assert len(target_workers) == 1

    # The same Node and Worker session continue round 2 of the same Node.
    spec = SimpleNamespace(node_id=stage["node_id"])
    toolbox_for_worker = WorkerToolbox(
        cluster.api, run_id=run["run_id"], node_id=stage["node_id"],
        worker_id=target_workers[0]["worker_id"],
        epoch=target_workers[0]["epoch"],
        tool_profile=target_workers[0]["tool_profile"],
        contract_ref=target_workers[0]["context"]["contract_ref"])
    submit_candidate(toolbox_for_worker, spec, round_number=2)

    state = cluster.drive(run["run_id"])
    assert state["run"]["status"] == "completed"
    stage = _stage(state, "first")
    assert stage["round"] == 2
    assert _candidate(state, stage["accepted_candidate_ref"])["round"] == 2
    assert [item["round"] for item in state["candidates"]] == [1, 2]
    assert [item["verdict"] for item in state["reviews"]] == ["revise", "accept"]

    target_workers = [worker for worker in state["workers"]
                      if worker["node_id"] == stage["node_id"]]
    assert len(target_workers) == 1
    assert target_workers[0]["worker_id"] == toolbox_for_worker.worker_id
    assert target_workers[0]["epoch"] == toolbox_for_worker.epoch
    # Each review round's declared Checker node discharged its own bound
    # receipt, including the revised round that did not accept the target.
    reviews = sorted(_review_nodes(state), key=lambda node: node["round"])
    assert [node["state"] for node in reviews] == ["succeeded", "succeeded"]
    events = _events(cluster, run["run_id"])
    feedback_key = "revise:{}:revise".format(state["candidates"][0]["candidate_id"])
    feedback_seq = next(event["seq"] for event in events
                        if event["kind"] == "message_delivered"
                        and event["worker_id"] == target_workers[0]["worker_id"]
                        and event["payload"]["delivery_key"] == feedback_key)
    second_seq = next(event["seq"] for event in events
                      if event["kind"] == "candidate_submitted"
                      and event["node_id"] == stage["node_id"]
                      and event["payload"]["round"] == 2)
    assert feedback_seq < second_seq
