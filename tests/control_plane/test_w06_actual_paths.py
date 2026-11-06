"""W06 focused checks: YAML file input, previous checkpoint materials, identity."""
from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import make_graph
from helpers import (Cluster, is_review_turn, operator, rid, submit_candidate,
                     submit_review, toolbox)

from node_runtime.control_plane.backends import BackendError, ScriptBackend
from node_runtime.control_plane.backends.base import WorkerSpec
from node_runtime.control_plane.backends.pi import PiBackend
from node_runtime.control_plane.execution_facts import previous_progress_checkpoint
from node_runtime.control_plane.host import (_load_script_agent, _script_identity,
                                            Host)
from node_runtime.control_plane.resources import ControlPlaneError
from node_runtime.control_plane.tools import WorkerToolbox

SHA = "a" * 64


def _ref(tag: str) -> dict:
    return {"id": f"sha256:{tag * 64}", "sha256": tag * 64, "size": 10,
            "media_type": "application/json"}


def _candidate(candidate_id: str, round_number: int, *, node="node-main",
               tag="b") -> dict:
    return {"candidate_id": candidate_id, "node_id": node,
            "round": round_number, "output_refs": [_ref(tag)],
            "summary_ref": _ref("c")}


def _evaluation(checkpoint_id: str, result: str, *, node="node-main",
                comparison="sha256:" + "d" * 64) -> dict:
    return {"node_id": node, "checkpoint_id": checkpoint_id,
            "comparison_ref": comparison, "result": result,
            "evidence_ref": _ref("e")}


# -- W06c selector -------------------------------------------------------

def test_previous_checkpoint_uses_window_and_strictly_earlier_round():
    comparison = "sha256:" + "d" * 64
    status = {
        "enabled": True,
        "node_id": "node-main",
        "comparison_ref": comparison,
        "evaluations": [
            _evaluation("c1", "not_improved"),
            _evaluation("foreign", "improved", node="node-other"),
            _evaluation("other-window", "improved",
                        comparison="sha256:" + "f" * 64),
            _evaluation("c2", "unknown"),
            _evaluation("current", "improved"),
        ],
    }
    candidates = [
        _candidate("c1", 1, tag="1"),
        _candidate("foreign", 2, node="node-other", tag="2"),
        _candidate("other-window", 3, tag="3"),
        _candidate("c2", 2, tag="4"),
        _candidate("current", 3, tag="5"),
        _candidate("future", 4, tag="6"),
    ]

    previous = previous_progress_checkpoint(
        status, candidates, {"candidate_id": "current", "round": 3})

    assert previous["checkpoint_id"] == "c2"
    assert previous["candidate_id"] == "c2"
    assert previous["round"] == 2
    assert previous["result"] == "unknown"
    assert previous["output_refs"] == [_ref("4")]
    assert previous["summary_ref"] == _ref("c")
    assert previous["evidence_ref"] == _ref("e")


def test_previous_checkpoint_is_absent_without_an_enabled_window():
    current = {"candidate_id": "c1", "round": 2}
    assert previous_progress_checkpoint(
        {"enabled": False}, [_candidate("c1", 1)], current) is None
    assert previous_progress_checkpoint(
        {"enabled": True, "node_id": "n", "comparison_ref": "x"},
        [_candidate("c1", 1)], current) is None
    assert previous_progress_checkpoint(
        {"enabled": True, "node_id": "n",
         "comparison_ref": "sha256:" + "d" * 64,
         "evaluations": [_evaluation("c0", "improved")]},
        [_candidate("c0", 2)], current) is None


# -- W06c delivery -------------------------------------------------------

def _pump_until(cluster, run_id, predicate, limit=180):
    for _ in range(limit):
        cluster.scheduler.reconcile(run_id)
        for worker in cluster.state(run_id)["workers"]:
            if worker["controller_id"] == "ctl-1":
                cluster.controller.reconcile(run_id, worker["worker_id"])
        state = cluster.state(run_id)
        if predicate(state):
            return state
    raise AssertionError("the expected intermediate state was not reached")


def _events(cluster, run_id):
    return cluster.api.read_events(operator(), run_id, limit=1000)["events"]


def test_review_receives_previous_evaluated_checkpoint_materials(tmp_path):
    cluster = Cluster(tmp_path / "materials", None)
    reviews: list[dict] = []

    def agent(api):
        def run(spec, message, state):
            box = toolbox(api, spec, message)
            if is_review_turn(message):
                reviews.append(dict(message))
                if len(reviews) == 1:
                    submit_review(box, spec, message, verdict="revise",
                                  progress="not_improved")
                else:
                    submit_review(box, spec, message, verdict="accept",
                                  progress="improved")
                return
            if message.get("kind") in ("revision", "continue"):
                # Round 2 is submitted by the test after the revision
                # feedback delivery is durable.
                return
            submit_candidate(box, spec,
                             round_number=int(message.get("round") or 1))
        return run

    cluster.backend.agent = agent(cluster.api)
    document = make_graph(definition_id="materials")
    document["tasks"]["work"]["progress"] = {"no_improvement_limit": 2}
    published = cluster.publish(document)
    run = cluster.create_run(published["definition_ref"],
                             {"dataset": cluster.artifact(b"dataset")})
    run_id = run["run_id"]

    def revised_and_fed(state):
        stage = next(node for node in state["nodes"]
                     if node["local_child_id"] == "first")
        return (stage["state"] == "running" and stage["round"] == 1
                and any(event["kind"] == "message_delivered"
                        and str((event.get("payload") or {}).get(
                            "delivery_key", "")).startswith("revise:")
                        for event in _events(cluster, run_id)))

    state = _pump_until(cluster, run_id, revised_and_fed)
    stage = next(node for node in state["nodes"]
                 if node["local_child_id"] == "first")
    worker = next(item for item in state["workers"]
                  if item["node_id"] == stage["node_id"])
    round_two = WorkerToolbox(
        cluster.api, run_id=run_id, node_id=stage["node_id"],
        worker_id=worker["worker_id"], epoch=worker["epoch"],
        tool_profile=worker["tool_profile"],
        contract_ref=worker["context"]["contract_ref"])
    submit_candidate(round_two, SimpleNamespace(node_id=stage["node_id"]),
                     round_number=2)

    state = cluster.drive(run_id)
    assert state["run"]["status"] == "completed"
    submitted = [event["payload"]["progress"]["result"]
                 for event in _events(cluster, run_id)
                 if event["kind"] == "review_submitted"]
    assert submitted == ["not_improved", "improved"]

    first = state["candidates"][0]
    second_review = reviews[1]
    previous = second_review["progress_comparison"]["previous"]
    assert previous["candidate_id"] == first["candidate_id"]
    assert previous["round"] == 1
    assert previous["result"] == "not_improved"
    assert previous["output_refs"] == first["output_refs"]

    evidence = next(event["payload"]["progress"]["evidence_ref"] for event
                    in _events(cluster, run_id)
                    if event["kind"] == "review_submitted")
    material_ids = [ref["id"] for ref in second_review["material_refs"]]
    assert first["output_refs"][0]["id"] in material_ids
    assert evidence["id"] in material_ids
    assert len(material_ids) == len(set(material_ids))


def test_pi_materializes_previous_checkpoint_as_progress_history(tmp_path):
    cluster = Cluster(tmp_path / "pi-material", None)
    previous = cluster.artifact(b"previous checkpoint", "application/json")
    message = {"candidate_id": "c2", "material_refs": [previous],
               "progress_comparison": {"previous": {"candidate_id": "c1"}}}
    roles = PiBackend._message_refs(message)
    assert roles == [("progress history", previous)]
    assert PiBackend._message_refs(
        {"material_refs": [previous]}) == [("decision material", previous)]

    backend = PiBackend(tmp_path / "pg", artifact_reader=cluster.api.artifacts.read)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    materials = backend._materialize_message(
        {"workspace": str(workspace), "materials": []}, "delivery-1", message)
    assert len(materials) == 1
    assert materials[0]["id"] == previous["id"]
    assert materials[0]["role"] == "progress history"
    written = workspace / materials[0]["path"]
    assert written.read_bytes() == b"previous checkpoint"


# -- W06d program identity ----------------------------------------------

def _spec() -> WorkerSpec:
    return WorkerSpec(worker_id="w1", node_id="node-main", epoch=1,
                      intent_seq=1, backend_ref="script", agent_ref="checker",
                      desired="active", run_id="run-1", budget={})


def _callback(spec, message, state):
    return None


def test_script_backend_refuses_a_changed_program_identity(tmp_path):
    first = {"path": str(tmp_path / "checker.py"), "factory": "build",
             "sha256": "sha256:" + "1" * 64}
    second = dict(first, sha256="sha256:" + "2" * 64)
    original = ScriptBackend(tmp_path / "data", agent=_callback, identity=first)
    original.start(_spec(), ("w1", 1))

    changed = ScriptBackend(tmp_path / "data", agent=_callback, identity=second)
    with pytest.raises(BackendError) as caught:
        changed.start(_spec(), ("w1", 1))
    assert caught.value.code == "capability_unsupported"
    assert caught.value.details["fields"] == ["agent_identity"]

    # A protocol stub must not silently resume work bound to a program.
    plain = ScriptBackend(tmp_path / "data")
    with pytest.raises(BackendError) as caught:
        plain.start(_spec(), ("w1", 1))
    assert caught.value.code == "capability_unsupported"


def test_host_reads_script_identity_without_executing_the_factory(tmp_path):
    script = tmp_path / "checker.py"
    script.write_text(
        "raise RuntimeError('must not run during status')\n", encoding="utf-8")
    reference = f"{script}:build"
    identity = _script_identity(reference)
    assert identity == {
        "path": str(script.resolve()), "factory": "build",
        "sha256": "sha256:" + hashlib.sha256(script.read_bytes()).hexdigest()}

    host = Host(tmp_path / "data", backend="pi", script_agent=reference,
                load_script_agent=False)
    assert host.script_agent_identity == identity

    with pytest.raises(ControlPlaneError) as caught:
        _load_script_agent(reference, None)
    assert caught.value.code == "invalid_argument"


def test_host_registers_script_only_when_explicit(tmp_path):
    script = tmp_path / "checker.py"
    script.write_text(
        "def build(api):\n"
        "    return lambda spec, message, state: None\n",
        encoding="utf-8")
    host = Host(tmp_path / "both", backend="pi",
                script_agent=f"{script}:build")
    assert set(host.backends) == {"pi", "script"}
    assert host.script_agent_identity["factory"] == "build"

    plain = Host(tmp_path / "plain", backend="pi")
    assert set(plain.backends) == {"pi"}
    assert plain.script_agent_identity is None


def test_host_loads_the_declared_factory(tmp_path):
    script = tmp_path / "checker.py"
    script.write_text(
        "def build(api):\n"
        "    return lambda spec, message, state: (api is None)\n",
        encoding="utf-8")
    agent, identity = _load_script_agent(f"{script}:build", None)
    assert identity["factory"] == "build"
    assert agent(None, {}, {}) is True

    with pytest.raises(ControlPlaneError) as missing:
        _load_script_agent(f"{script}:absent", None)
    assert missing.value.code == "invalid_argument"
