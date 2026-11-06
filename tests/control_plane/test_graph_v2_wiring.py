"""Graph /2 wiring: delegation composition, capabilities, Host, MCP and example."""
from __future__ import annotations

import asyncio
import json
import time
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from conftest import ISOLATED_ROOT, make_graph
from helpers import (Cluster, accepting_agent, is_review_turn, operator,
                     publisher, reader, rid, submit_candidate, submit_review,
                     toolbox)

from node_runtime.control_plane.api import Api
from node_runtime.control_plane.backends.base import (BackendError, Capabilities,
                                                      WorkerSpec)
from node_runtime.control_plane.backends.pi import PiBackend
from node_runtime.control_plane.backends.script import ScriptBackend
from node_runtime.control_plane.controller import Controller
from node_runtime.control_plane.host import Host
from node_runtime.control_plane.resources import Actor, ControlPlaneError
from node_runtime.control_plane.scheduler import Scheduler


def _stage(state, name):
    return next(node for node in state["nodes"]
                if node["local_child_id"] == name)


def _pump(cluster, run_id, predicate, limit=80):
    for _ in range(limit):
        cluster.scheduler.reconcile(run_id)
        for worker in cluster.state(run_id)["workers"]:
            if worker["controller_id"] == "ctl-1":
                cluster.controller.reconcile(run_id, worker["worker_id"])
        state = cluster.state(run_id)
        if predicate(state):
            return state
    raise AssertionError("the expected intermediate state was not reached")


def _binding_failure(cluster, run_id, limit=6):
    """Reconcile until the unexecutable binding is a durable Node failure."""
    actions = []
    for _ in range(limit):
        actions.extend(cluster.scheduler.reconcile(run_id)["actions"])
        state = cluster.state(run_id)
        if _stage(state, "first")["state"] in ("failed", "blocked", "cancelled"):
            return state, actions
    raise AssertionError("worker composition did not fail")


def _failure_action(actions):
    return next(action for action in actions if action["action"] == "node_failed")


# -- delegation ---------------------------------------------------------

def test_authorized_delegation_narrows_the_worker_spec(tmp_path):
    captured: dict[str, object] = {}

    def capture(spec, message, state):
        captured.setdefault("spec", spec)

    cluster = Cluster(tmp_path / "delegation", None)
    cluster.backend.agent = capture
    document = make_graph(definition_id="delegation")
    document["budget_limits"]["max_continuations"] = 3
    document["nodes"]["first"]["delegation"] = {
        "context": {"business_key": "narrowed"},
        "tool_profile": {"tools": ["node.submit_candidate"]},
        "budget": {"max_continuations": 1},
    }
    published = cluster.publish(document)
    dataset = cluster.artifact(b"dataset", "text/plain")
    run = cluster.create_run(published["definition_ref"], {"dataset": dataset})

    state = _pump(cluster, run["run_id"],
                  lambda state: bool(state["workers"]))
    worker = state["workers"][0]
    spec = captured["spec"]

    assert spec.context["business_key"] == "narrowed"
    assert spec.context["role"] == "worker"
    assert spec.context["instructions"] == document["agents"]["doer"]["instructions"]
    assert {entry["name"] for entry in spec.tool_profile["tools"]} == {
        "node.submit_candidate"}
    assert not spec.tool_profile.get("backend_tools")

    # The persisted Worker is the same composition, not a second one.
    assert worker["context"] == spec.context
    assert worker["tool_profile"] == spec.tool_profile
    assert worker["budget"]["max_continuations"] == 1
    assert worker["budget"]["max_nodes"] == 30


def test_scheduler_submits_the_full_configuration_and_api_refuses_tampering(
        tmp_path, monkeypatch):
    cluster = Cluster(tmp_path / "binding-submitted", None)
    published = cluster.publish(make_graph(definition_id="binding-submitted"))
    dataset = cluster.artifact(b"dataset", "text/plain")
    run = cluster.create_run(published["definition_ref"], {"dataset": dataset})

    submitted: list[dict[str, object]] = []
    original = cluster.api.commit_plan

    def spy(actor, read_set, mutations, request_id):
        for mutation in mutations:
            if mutation.get("kind") == "worker" and mutation.get("op") == "create":
                submitted.append(deepcopy(mutation["fields"]))
        return original(actor, read_set, mutations, request_id)

    monkeypatch.setattr(cluster.api, "commit_plan", spy)
    cluster.scheduler.reconcile(run["run_id"])
    state = cluster.state(run["run_id"])
    assert len(submitted) == 1
    fields = submitted[0]
    assert {"context", "tool_profile", "budget", "required_capabilities"} <= set(fields)
    assert fields["desired"] == "active"
    worker = next(worker for worker in state["workers"]
                  if worker["node_id"] == _stage(state, "first")["node_id"])
    assert fields["context"] == worker["context"]
    assert fields["tool_profile"] == worker["tool_profile"]
    assert fields["budget"] == worker["budget"]
    assert fields["required_capabilities"] == worker["required_capabilities"]

    # A direct plan that omits or disagrees with the composed execution config
    # is refused; the Api does not fill in intent on the Scheduler's behalf.
    other = Cluster(tmp_path / "binding-refused", None)
    published = other.publish(make_graph(definition_id="binding-refused"))
    dataset = other.artifact(b"dataset", "text/plain")
    run = other.create_run(published["definition_ref"], {"dataset": dataset})
    monkeypatch.setattr(other.scheduler, "_start_workers",
                        lambda run_id, actions: False)
    other.scheduler.reconcile(run["run_id"])
    state = other.state(run["run_id"])
    node = _stage(state, "first")
    assert node["state"] == "ready"
    bundle = other.api.definition_bundle_for_run(
        other.scheduler.actor(run["run_id"]), run["run_id"])
    task = bundle.tasks[node["template_ref"]]
    agent = bundle.agents[task.agent_ref]
    reads = [
        {"kind": "node", "id": node["node_id"], "revision": node["revision"]},
        {"kind": "run", "id": run["run_id"], "revision": state["run"]["revision"]},
    ]
    identity = {
        "worker_id": f"{node['node_id']}-w1",
        "node_id": node["node_id"],
        "backend_ref": agent.backend_ref,
        "agent_ref": agent.agent_ref,
        "controller_id": "ctl-1",
    }

    with pytest.raises(ControlPlaneError) as missing:
        other.api.commit_plan(other.scheduler.actor(run["run_id"]), reads, [{
            "kind": "worker", "op": "create", "fields": dict(identity),
        }], rid("missing-config"))
    assert missing.value.code == "unauthorized"
    assert missing.value.details["field"] == "context"

    with pytest.raises(ControlPlaneError) as inconsistent:
        other.api.commit_plan(other.scheduler.actor(run["run_id"]), reads, [{
            "kind": "worker", "op": "create",
            "fields": dict(identity, context={"role": "worker"},
                           tool_profile={"tools": []}, budget={},
                           required_capabilities={}),
        }], rid("inconsistent-config"))
    assert inconsistent.value.code == "unauthorized"
    assert inconsistent.value.details["field"] == "context"
    assert other.state(run["run_id"])["workers"] == []


@pytest.mark.parametrize("delegation,code", [
    ({"budget": {"max_continuations": 5}}, "unauthorized"),
    ({"tool_profile": {"tools": ["node.escalate"]}}, "unauthorized"),
    ({"context": {"worker_id": "spoofed"}}, "unauthorized"),
])
def test_over_broad_delegation_is_refused_before_binding(tmp_path, delegation, code):
    cluster = Cluster(tmp_path / "over-broad", None)
    cluster.backend.agent = accepting_agent(cluster.api)
    document = make_graph(definition_id="over-broad")
    document["budget_limits"]["max_continuations"] = 3
    document["nodes"]["first"]["delegation"] = delegation
    published = cluster.publish(document)
    dataset = cluster.artifact(b"dataset", "text/plain")
    run = cluster.create_run(published["definition_ref"], {"dataset": dataset})

    state, actions = _binding_failure(cluster, run["run_id"])
    failure = _failure_action(actions)
    assert failure["reason"] == "binding_unexecutable"
    assert failure["error_code"] == code
    assert state["run"]["status"] == "failed"
    assert state["workers"] == []
    assert _stage(state, "first")["state"] == "failed"


def test_unsupported_agent_requirement_stops_task_without_endless_binding_retry(tmp_path):
    cluster = Cluster(tmp_path / "requirements", None)
    cluster.backend.agent = accepting_agent(cluster.api)
    # The trusted host registry says this backend cannot resume a session,
    # even though the Agent definition requires it.
    cluster.api.backend_capabilities = {"script": Capabilities(
        resumable_session=False, live_observation="full",
        idempotent_delivery=True, stop="cooperative", checkpoint=True,
        backend_tools=())}
    document = make_graph(definition_id="requirements")
    document["agents"]["doer"]["required_capabilities"] = {"resumable_session": True}
    published = cluster.publish(document)
    dataset = cluster.artifact(b"dataset", "text/plain")
    run = cluster.create_run(published["definition_ref"], {"dataset": dataset})

    state, actions = _binding_failure(cluster, run["run_id"])
    failure = _failure_action(actions)
    assert failure["error_code"] == "capability_unsupported"
    assert state["run"]["status"] == "failed"
    assert state["workers"] == []

    # The durable failure is not a retry loop: a later reconcile neither
    # binds a Worker nor emits the same failure again.
    before = len(cluster.api.read_events(operator(), run["run_id"],
                                         limit=1000)["events"])
    again = cluster.scheduler.reconcile(run["run_id"])
    after = cluster.api.read_events(operator(), run["run_id"], limit=1000)
    assert not [action for action in again["actions"]
                if action["action"] in ("worker_created", "node_failed")]
    assert len(after["events"]) == before
    assert cluster.state(run["run_id"])["workers"] == []


def test_runtime_options_are_refused_while_no_backend_consumes_them(tmp_path):
    cluster = Cluster(tmp_path / "runtime-options", None)
    cluster.backend.agent = accepting_agent(cluster.api)
    document = make_graph(definition_id="runtime-options")
    document["agents"]["doer"]["runtime_options"] = {"model": "tiny"}
    document["agents"]["doer"]["configurable_fields"] = ["runtime_options.model"]
    published = cluster.publish(document)
    dataset = cluster.artifact(b"dataset", "text/plain")
    run = cluster.create_run(published["definition_ref"], {"dataset": dataset})

    state, actions = _binding_failure(cluster, run["run_id"])
    failure = _failure_action(actions)
    assert failure["error_code"] == "capability_unsupported"
    assert failure["error"]["options"] == ["model"]
    assert state["run"]["status"] == "failed"
    assert state["workers"] == []


# -- execution binding ---------------------------------------------------

def test_script_backend_restores_the_committed_spec_not_the_message(tmp_path):
    captured = []

    def agent(spec, message, state):
        captured.append(spec)

    backend = ScriptBackend(tmp_path / "script")
    backend.agent = agent
    committed = WorkerSpec(
        worker_id="node-w1", node_id="node", epoch=0, intent_seq=1,
        backend_ref="script", agent_ref="agent-ref", desired="active",
        run_id="run-committed",
        context={"role": "worker", "run_id": "run-committed"},
        tool_profile={"tools": []},
        budget={"max_tool_calls": 3},
        required_capabilities={"resumable_session": True},
    )
    handle = backend.start(committed, ("node-w1", 0))
    backend.deliver(handle, {"kind": "start", "script": {"mode": "agent"}},
                    "no-run")
    backend.deliver(handle, {"kind": "continue", "run_id": "run-forged",
                             "script": {"mode": "agent"}}, "forged-run")

    # A mutable message can neither supply nor change the execution identity.
    assert [spec.run_id for spec in captured] == ["run-committed",
                                                  "run-committed"]
    assert captured[0].budget == {"max_tool_calls": 3}
    assert captured[0].required_capabilities == {"resumable_session": True}
    assert captured[0].backend_ref == "script"
    assert captured[0].agent_ref == "agent-ref"

    restarted = ScriptBackend(tmp_path / "script")
    restarted.agent = agent
    restarted.start(committed, ("node-w1", 0))
    with pytest.raises(BackendError) as changed:
        restarted.start(
            WorkerSpec(
                worker_id="node-w1", node_id="node", epoch=0, intent_seq=1,
                backend_ref="script", agent_ref="agent-ref", desired="active",
                run_id="other-run",
                context={"role": "worker", "run_id": "run-committed"},
                tool_profile={"tools": []}, budget={"max_tool_calls": 3},
                required_capabilities={"resumable_session": True}),
            ("node-w1", 0))
    assert changed.value.code == "capability_unsupported"
    assert changed.value.details["fields"] == ["run_id"]


def test_script_backend_refuses_a_state_without_a_committed_spec(tmp_path):
    backend = ScriptBackend(tmp_path / "script")
    committed = WorkerSpec(
        worker_id="node-w1", node_id="node", epoch=0, intent_seq=1,
        backend_ref="script", agent_ref="agent-ref", desired="active",
        run_id="run-committed", context={"role": "worker"},
        tool_profile={"tools": []}, budget={"max_tool_calls": 3})
    backend.start(committed, ("node-w1", 0))
    path = backend._state_path(("node-w1", 0))
    state = json.loads(path.read_text(encoding="utf-8"))
    del state["spec"]
    path.write_text(json.dumps(state), encoding="utf-8")
    before = path.read_bytes()

    # A missing committed spec is unrecoverable. The current request must not
    # be written back as if it had been the historical execution identity.
    with pytest.raises(BackendError) as missing:
        backend.start(committed, ("node-w1", 0))
    assert missing.value.code == "capability_unsupported"
    assert missing.value.details["fields"] == ["spec"]
    assert path.read_bytes() == before


def test_controller_delivers_committed_contract_to_workers_and_checkers(tmp_path):
    captured = {"workers": [], "checkers": []}

    def agent(api):
        def run(spec, message, state):
            box = toolbox(api, spec, message)
            if is_review_turn(message):
                captured["checkers"].append((spec, dict(message)))
                submit_review(box, spec, message)
                return
            captured["workers"].append((spec, dict(message)))
            submit_candidate(box, spec, round_number=int(
                message.get("round") or 1))
        return run

    cluster = Cluster(tmp_path / "contract", None)
    cluster.backend.agent = agent(cluster.api)
    stages = {
        "extract": {"task_ref": "work", "reviewer_ref": "check", "needs": [],
                    "inputs": {"dataset": {"run_input": "dataset"}}},
        "transform": {"task_ref": "work", "reviewer_ref": "check",
                      "needs": ["extract"],
                      "inputs": {"upstream": {"from": "extract"}}},
    }
    document = make_graph(definition_id="contract", stages=stages)
    published = cluster.publish(document)
    dataset = cluster.artifact(b"dataset", "text/plain")
    run = cluster.create_run(published["definition_ref"], {"dataset": dataset})

    state = cluster.drive(run["run_id"])
    assert state["run"]["status"] == "completed"
    assert captured["workers"] and captured["checkers"]

    work_contract = document["tasks"]["work"]["contract"]
    check_contract = document["tasks"]["check"]["contract"]
    worker_spec, worker_message = captured["workers"][0]
    assert worker_spec.run_id == run["run_id"]
    assert worker_spec.budget == state["run"]["budget"]
    assert worker_message["goal"] == document["tasks"]["work"]["goal"]
    assert worker_message["acceptance"] == work_contract["acceptance"]
    assert worker_message["output_schema"] == work_contract["output_schema"]

    checker_spec, checker_message = captured["checkers"][0]
    assert checker_spec.run_id == run["run_id"]
    assert checker_message["goal"] == document["tasks"]["check"]["goal"]
    assert checker_message["acceptance"] == check_contract["acceptance"]
    assert checker_message["output_schema"] == check_contract["output_schema"]
    extract = _stage(state, "extract")
    assert checker_message["target"]["node_id"] == extract["node_id"]
    assert checker_message["target"]["contract_ref"] == extract["contract_ref"]
    assert checker_message["target"]["goal"] == document["tasks"]["work"]["goal"]
    assert checker_message["target"]["acceptance"] == work_contract["acceptance"]
    assert checker_message["target"]["output_schema"] == work_contract["output_schema"]


def test_actual_pi_adapter_delivers_frozen_goal_and_acceptance_to_process(tmp_path):
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
    backend = PiBackend(tmp_path / "data", executable=str(fake),
                        max_wall_seconds=30, data_dir=tmp_path / "data")
    backend.set_credential_issuer(
        lambda binding: {"endpoint": "http://127.0.0.1:1",
                         "token": "delivery-token"})

    worker = WorkerSpec(
        worker_id="w1", node_id="n1", epoch=0, intent_seq=1,
        backend_ref="pi", agent_ref="worker@v1", desired="active", run_id="r1",
        context={"role": "worker", "brief": "Clean one page."},
        tool_profile={"tools": [], "backend_tools": ["write_file"]})
    checker = WorkerSpec(
        worker_id="w2", node_id="n2", epoch=0, intent_seq=1,
        backend_ref="pi", agent_ref="checker@v1", desired="active", run_id="r1",
        context={"role": "reviewer", "brief": "Review the bound candidate."},
        tool_profile={"tools": ["node.submit_review"],
                      "backend_tools": ["write_file"]})
    target = {"node_id": "n1", "template_ref": "work", "contract_ref": "clean",
              "goal": "Clean the seeded table.",
              "acceptance": "Every row has a valid id.",
              "output_schema": {"type": "array"}}
    worker_handle = backend.start(worker, ("w1", 0))
    backend.deliver(worker_handle, {
        "kind": "start", "brief": "Clean one page.",
        "goal": "Clean the seeded table.",
        "acceptance": "Every row has a valid id.",
        "output_schema": {"type": "array"}}, "worker-start")
    checker_handle = backend.start(checker, ("w2", 0))
    backend.deliver(checker_handle, {
        "kind": "review_start", "brief": "Review the candidate.",
        "role": "reviewer",
        "goal": "Review the bound candidate.",
        "acceptance": "The verdict cites the target contract.",
        "output_schema": {"type": "object"},
        "target_node_id": "n1", "candidate_id": "candidate-1",
        "candidate_round": 1, "reviewer_ref": "check",
        "target": target}, "checker-start")

    deadline = time.monotonic() + 10
    while len(list(capture.glob("argv.*"))) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    prompts = [path.read_text(encoding="utf-8")
               for path in capture.glob("argv.*")]
    assert len(prompts) == 2
    worker_prompt = next(prompt for prompt in prompts
                         if "ROLE: worker" in prompt)
    checker_prompt = next(prompt for prompt in prompts
                          if "ROLE: reviewer" in prompt)

    # The prompt captured from the real child argv is the same bytes the
    # Backend recorded for this delivery.
    state = json.loads(backend._state_path(("w1", 0)).read_text(encoding="utf-8"))
    assert Path(state["deliveries"]["worker-start"]["prompt_ref"]).read_text(
        encoding="utf-8") == worker_prompt
    assert "TASK:\nClean one page." in worker_prompt
    assert "GOAL:\nClean the seeded table." in worker_prompt
    assert "ACCEPTANCE:\nEvery row has a valid id." in worker_prompt
    assert 'OUTPUT SCHEMA:\n{"type":"array"}' in worker_prompt

    assert "TASK:\nReview the candidate." in checker_prompt
    assert "GOAL:\nReview the bound candidate." in checker_prompt
    assert "ACCEPTANCE:\nThe verdict cites the target contract." in checker_prompt
    assert 'OUTPUT SCHEMA:\n{"type":"object"}' in checker_prompt
    assert "TARGET TASK (the node under review):" in checker_prompt
    assert "  node: n1" in checker_prompt
    assert "  template: work" in checker_prompt
    assert "  contract: clean" in checker_prompt
    assert "  goal: Clean the seeded table." in checker_prompt
    assert "  acceptance: Every row has a valid id." in checker_prompt
    assert '  output schema: {"type":"array"}' in checker_prompt

    capabilities = backend.capabilities()
    assert capabilities.internal_activities == "none"
    observation = backend.observe(worker_handle)
    assert observation.activities == ()
    assert observation.activities_complete is True


# -- cross-epoch --------------------------------------------------------

def test_candidate_from_an_old_epoch_is_rejected(tmp_path):
    def producer(api):
        def run(spec, message, state):
            box = toolbox(api, spec, message)
            if message.get("kind") == "start":
                from helpers import submit_candidate
                submit_candidate(box, spec, round_number=1)

        return run

    cluster = Cluster(tmp_path / "epoch", None)
    cluster.backend.agent = producer(cluster.api)
    published = cluster.publish(make_graph(definition_id="epoch"))
    dataset = cluster.artifact(b"dataset", "text/plain")
    run = cluster.create_run(published["definition_ref"], {"dataset": dataset})
    state = _pump(cluster, run["run_id"],
                  lambda s: any(item["decision"] is None for item in s["candidates"]))
    stage = _stage(state, "first")
    worker = next(item for item in state["workers"]
                  if item["node_id"] == stage["node_id"])
    stale = Actor(worker["worker_id"], {"worker"},
                  run_ids={run["run_id"]}, worker_id=worker["worker_id"],
                  epoch=worker["epoch"] + 1)
    output = cluster.artifact(b"late", "text/plain")
    with pytest.raises(ControlPlaneError) as rejected:
        cluster.api.append_fact(stale, {
            "kind": "candidate_submitted",
            "run_id": run["run_id"],
            "node_id": stage["node_id"],
            "worker_id": worker["worker_id"],
            "epoch": worker["epoch"] + 1,
            "candidate_id": "candidate-stale-epoch",
            "round": 2,
            "contract_ref": stage["contract_ref"],
            "output_refs": [output],
        }, rid("stale-candidate"), stage["revision"])
    assert rejected.value.code == "stale_epoch"


# -- host ---------------------------------------------------------------

def test_host_registers_actual_capabilities_and_completes_a_run(tmp_path):
    host = Host(tmp_path / "host", backend="script", poll_seconds=0.0)
    assert host.api.backend_capabilities == {
        "script": host.backends["script"].capabilities()}
    assert host.controller.backends["script"] is host.backends["script"]
    # The deterministic stand-in runs the Agent callback on every delivery the
    # same way a real Backend session would, without any model call.
    backend = host.backends["script"]
    execute = backend._execute

    def execute_as_agent(execution_key, delivery_key):
        state = backend._state(execution_key)
        state["deliveries"][delivery_key]["message"].setdefault(
            "script", {"mode": "agent"})
        backend._save(execution_key, state)
        execute(execution_key, delivery_key)

    backend._execute = execute_as_agent
    backend.agent = accepting_agent(host.api)

    teacher = Actor("teacher", {"publisher"})
    document = make_graph(definition_id="host-run")
    published = host.api.publish_definition(teacher, document, rid("publish"))
    dataset = host.api.put_artifact(teacher, rid("artifact"), b"dataset",
                                    "text/plain")["artifact"]
    run = host.api.create_run(teacher, published["definition_ref"],
                              {"dataset": dataset}, rid("run"))
    run_id = run["run_id"]

    status = None
    for _ in range(60):
        host.reconcile_once()
        status = host.api.get_resource(reader(), "run", run_id)["status"]
        if status in ("completed", "failed", "cancelled"):
            break
    assert status == "completed", host._log_path.read_text() if host._log_path.exists() else ""
    report = host.api.get_report(reader(), run_id)
    assert report["run"]["status"] == "completed"


# -- MCP + Teacher ------------------------------------------------------

def test_teacher_mcp_surface_matches_the_shipped_tool_list(tmp_path):
    from node_runtime.control_plane import mcp_server, teacher_entry

    cluster = Cluster(tmp_path / "mcp", None)
    server = mcp_server.build_server(
        api=cluster.api, actor_resolver=lambda: publisher())
    tools = asyncio.run(server.list_tools())
    assert {tool.name for tool in tools} == set(mcp_server.TOOL_NAMES)
    assert set(mcp_server.TOOL_NAMES) <= set(teacher_entry.teacher_tools())

    document = make_graph(definition_id="mcp-validate")
    result = asyncio.run(server.call_tool(
        "validate_definition", {"document": document}))
    assert result.structured_content["ok"] is True
    assert (result.structured_content["result"]["document"]["format"]
            == "agentgenome-graph-definition/2")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    paths = teacher_entry.prepare(tmp_path / "teacher-data", workspace)
    assert paths["prompt"].read_text(encoding="utf-8")
    assert paths["guide"].exists()
    assert paths["template"].exists()
    config = json.loads(teacher_entry.mcp_config(
        paths, endpoint="http://127.0.0.1:9", token="test-token"))
    assert config["format"] == teacher_entry.MCP_CONFIG_FORMAT
    assert config["endpoint"] == "http://127.0.0.1:9"
    assert config["token"] == "test-token"
    assert config["workspace"] == str(workspace)
    assert "command" not in config and "args" not in config
    argv = teacher_entry.command(paths, executable="pi")
    assert "--extension" in argv
    assert str(teacher_entry.MCP_EXTENSION).endswith("pi_teacher_tools.mjs")
    assert "--tools" in argv


# -- shipped example ----------------------------------------------------

def test_shipped_two_stage_example_is_a_runnable_graph_v2(tmp_path):
    path = ISOLATED_ROOT / "examples" / "agent-workflow" / "graph-two-stage.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert document["format"] == "agentgenome-graph-definition/2"

    cluster = Cluster(tmp_path / "example", None)
    cluster.backend.agent = accepting_agent(cluster.api)
    normalized = cluster.api.validate_definition(publisher(), document)
    assert normalized["document_digest"] == cluster.publish(document)["definition_ref"]

    dataset = cluster.artifact(b"dataset", "text/plain")
    run = cluster.create_run(normalized["document_digest"], {"dataset": dataset})
    state = cluster.drive(run["run_id"])
    assert state["run"]["status"] == "completed"
    assert {_stage(state, "extract")["state"],
            _stage(state, "transform")["state"]} == {"succeeded"}


# -- Controller preflight -----------------------------------------------

class _NativeBackend(ScriptBackend):
    """Script Backend that claims one native tool, like a real adapter."""

    def capabilities(self) -> Capabilities:
        from dataclasses import replace
        return replace(super().capabilities(), backend_tools=("read_file",))


def test_controller_preflight_checks_the_actual_adapter_and_keeps_stop(tmp_path):
    cluster = Cluster(tmp_path / "preflight", None)
    actual = _NativeBackend(cluster.root)
    trusted = _NativeCapabilities = Capabilities(
        resumable_session=True, live_observation="full",
        idempotent_delivery=True, stop="cooperative", checkpoint=True,
        backend_tools=("grep", "read_file"))
    cluster.api.backend_capabilities = {"script": trusted}
    cluster.controller.backends["script"] = actual

    document = make_graph(definition_id="preflight")
    document["agents"]["doer"]["tool_profile"]["backend_tools"] = ["grep"]
    published = cluster.publish(document)
    dataset = cluster.artifact(b"dataset", "text/plain")
    run = cluster.create_run(published["definition_ref"], {"dataset": dataset})
    run_id = run["run_id"]

    cluster.scheduler.reconcile(run_id)
    state = cluster.state(run_id)
    worker = state["workers"][0]
    assert worker["tool_profile"]["backend_tools"] == ["grep"]

    with pytest.raises(ControlPlaneError) as unsupported:
        cluster.controller.reconcile(run_id, worker["worker_id"])
    assert unsupported.value.code == "capability_unsupported"
    assert unsupported.value.details["missing"] == ["grep"]
    # No delivery reached the backend and no observation was committed.
    execution = actual._state((worker["worker_id"], worker["epoch"]))
    assert execution["order"] == []
    assert cluster.api.latest_observation(
        Actor("scheduler", {"scheduler"}), run_id,
        worker["worker_id"]) is None

    current = cluster.api.get_resource(publisher(), "run", run_id)
    cluster.api.set_run_intent(publisher(), run_id, "cancelled",
                               current["revision"], rid("cancel"))
    cluster.scheduler.reconcile(run_id)
    stopped = cluster.controller.reconcile(run_id, worker["worker_id"])
    assert stopped["status"] == "stopped_requested"
    assert stopped["observed"] == "stopped"


# -- parallel stages ----------------------------------------------------

def _two_independent_stages():
    return {
        "first": {"task_ref": "work", "reviewer_ref": "check", "needs": [],
                  "inputs": {"dataset": {"run_input": "dataset"}}},
        "second": {"task_ref": "work", "reviewer_ref": "check", "needs": [],
                   "inputs": {"dataset": {"run_input": "dataset"}}},
    }


def _worker_for(state, name):
    node = _stage(state, name)
    return next(worker for worker in state["workers"]
                if worker["node_id"] == node["node_id"])


def _fail_first_worker(api, controller, run_id, worker):
    controller.reconcile(run_id, worker["worker_id"])
    api.enqueue_message(Actor("scheduler", {"scheduler"}), worker["worker_id"],
                        {"kind": "scripted_fail", "script": {"mode": "fail"}},
                        "manual-fail", rid("message"))
    controller.reconcile(run_id, worker["worker_id"])


def test_parallel_stage_failure_stops_and_drains_the_queued_sibling(tmp_path):
    api, scheduler, controller = _plain_control(tmp_path / "parallel-queued")
    run = _plain_run(api, make_graph(definition_id="parallel-queued",
                                     stages=_two_independent_stages()),
                     definition_id="parallel-queued")
    run_id = run["run_id"]
    scheduler.reconcile(run_id)
    state = api.list_resources(Actor("scheduler", {"scheduler"}), run_id)
    assert len(state["workers"]) == 2
    first, second = _worker_for(state, "first"), _worker_for(state, "second")

    # Only the first start is delivered; the second start stays queued.
    _fail_first_worker(api, controller, run_id, first)
    scheduler.reconcile(run_id)
    state = api.list_resources(Actor("scheduler", {"scheduler"}), run_id)
    run_now = api.get_resource(reader(), "run", run_id)
    assert run_now["status"] == "failed"
    assert run_now["summary"]["reason"] == "worker_failed"
    assert _stage(state, "first")["state"] == "failed"

    # The independent stage receives the same durable stop intent as cancel.
    assert _worker_for(state, "second")["desired"] == "stopped"

    # The queued start must never reach the Backend after the Run failed; the
    # committed stop drains the Worker to an observed fact instead.
    for _ in range(3):
        controller.reconcile(run_id, second["worker_id"])
    execution = controller.backends["script"]._state(
        (second["worker_id"], second["epoch"]))
    assert execution["order"] == []
    assert execution["stop"]["stopped_at"]
    drained = api.get_resource(Actor("scheduler", {"scheduler"}),
                               "worker", second["worker_id"])
    assert drained["observed"] == "stopped"
    inbox = api.list_inbox(Actor("scheduler", {"scheduler"}),
                           second["worker_id"])
    assert inbox["messages"]
    assert all(message["delivery_state"] == "pending"
               for message in inbox["messages"])


def test_parallel_stage_failure_stops_and_drains_the_active_sibling(tmp_path):
    from types import SimpleNamespace

    api, scheduler, controller = _plain_control(tmp_path / "parallel-active")
    run = _plain_run(api, make_graph(definition_id="parallel-active",
                                     stages=_two_independent_stages()),
                     definition_id="parallel-active")
    run_id = run["run_id"]
    scheduler.reconcile(run_id)
    state = api.list_resources(Actor("scheduler", {"scheduler"}), run_id)
    first, second = _worker_for(state, "first"), _worker_for(state, "second")

    backend = controller.backends["script"]
    execute = backend._execute

    def hang_second(execution_key, delivery_key):
        stored = backend._state(execution_key)
        message = stored["deliveries"][delivery_key]["message"]
        if (stored["node_id"] == second["node_id"]
                and message.get("kind") == "start"):
            message.setdefault("script", {})["mode"] = "hang"
            backend._save(execution_key, stored)
        execute(execution_key, delivery_key)

    backend._execute = hang_second
    controller.reconcile(run_id, second["worker_id"])
    observed = api.get_resource(Actor("scheduler", {"scheduler"}),
                                "worker", second["worker_id"])
    assert observed["observed"] == "active"

    _fail_first_worker(api, controller, run_id, first)
    scheduler.reconcile(run_id)
    state = api.list_resources(Actor("scheduler", {"scheduler"}), run_id)
    assert api.get_resource(reader(), "run", run_id)["status"] == "failed"
    assert _worker_for(state, "second")["desired"] == "stopped"

    # A cooperative stop is requested but the execution is still observed
    # active: the control plane never reports stopped before the process ends.
    controller.reconcile(run_id, second["worker_id"])
    execution = backend._state((second["worker_id"], second["epoch"]))
    assert execution["stop"]["requested_at"]
    assert execution["stop"]["stopped_at"] is None
    still = api.get_resource(Actor("scheduler", {"scheduler"}),
                             "worker", second["worker_id"])
    assert still["observed"] == "active"

    backend.complete_stop(SimpleNamespace(
        execution_key=(second["worker_id"], second["epoch"])))
    controller.reconcile(run_id, second["worker_id"])
    drained = api.get_resource(Actor("scheduler", {"scheduler"}),
                               "worker", second["worker_id"])
    assert drained["observed"] == "stopped"


def test_terminal_run_refuses_a_new_delivery_even_without_a_stop_intent(
        tmp_path, monkeypatch):
    api, scheduler, controller = _plain_control(tmp_path / "terminal-delivery")
    # Isolate the Controller guarantee: no stop intent is ever committed.
    monkeypatch.setattr(scheduler, "_stop_failed_run_workers",
                        lambda run_id, actions: False)
    run = _plain_run(api, make_graph(definition_id="terminal-delivery",
                                     stages=_two_independent_stages()),
                     definition_id="terminal-delivery")
    run_id = run["run_id"]
    scheduler.reconcile(run_id)
    state = api.list_resources(Actor("scheduler", {"scheduler"}), run_id)
    first, second = _worker_for(state, "first"), _worker_for(state, "second")
    _fail_first_worker(api, controller, run_id, first)
    scheduler.reconcile(run_id)

    state = api.list_resources(Actor("scheduler", {"scheduler"}), run_id)
    assert api.get_resource(reader(), "run", run_id)["status"] == "failed"
    assert _worker_for(state, "second")["desired"] == "active"

    for _ in range(3):
        controller.reconcile(run_id, second["worker_id"])
    execution = controller.backends["script"]._state(
        (second["worker_id"], second["epoch"]))
    assert execution["order"] == []


def test_host_recovers_a_failed_run_whose_stop_was_never_committed(tmp_path):
    host = Host(tmp_path / "failed-window", backend="script", poll_seconds=0.0)
    document = make_graph(definition_id="failed-window",
                          stages=_two_independent_stages())
    published = host.api.publish_definition(publisher(), document,
                                            rid("publish"))
    dataset = host.api.put_artifact(publisher(), rid("artifact"), b"dataset",
                                    "text/plain")["artifact"]
    run = host.api.create_run(publisher(), published["definition_ref"],
                              {"dataset": dataset}, rid("run"))
    run_id = run["run_id"]
    host.scheduler.reconcile(run_id)
    state = host.api.list_resources(operator(), run_id)
    first, second = _worker_for(state, "first"), _worker_for(state, "second")

    class FailFirst(ScriptBackend):
        def deliver(self, handle, message, delivery_key):
            mode = ("fail" if handle.worker_id == first["worker_id"]
                    else "finish")
            return super().deliver(
                handle, {**message, "script": {"mode": mode}}, delivery_key)

    backend = FailFirst(host.data_dir)
    host.backends["script"] = backend
    host.controller.backends["script"] = backend
    host.api.backend_capabilities = {"script": backend.capabilities()}
    host.controller.reconcile(run_id, second["worker_id"])
    host.controller.reconcile(run_id, first["worker_id"])

    # A restart between the failed-Run commit and the cleanup pass must not
    # lose the still-unstopped sibling.
    host.scheduler.reconcile(run_id, max_steps=1)
    assert host.api.get_resource(reader(), "run", run_id)["status"] == "failed"
    assert host.api.get_resource(operator(), "worker",
                                 second["worker_id"])["observed"] == "idle"
    assert run_id in {item["run_id"] for item in host.reconciliation_runs()}

    for _ in range(3):
        host.reconcile_once()
    drained = host.api.get_resource(operator(), "worker", second["worker_id"])
    assert drained["desired"] == "stopped"
    assert drained["observed"] == "stopped"


# -- Task input contracts before binding --------------------------------

_JSON_ONLY = {
    "type": "array",
    "items": {"type": "object",
              "properties": {"media_type": {"enum": ["application/json"]}}},
}


def test_task_input_schema_rejects_an_incompatible_frozen_input(tmp_path):
    cluster = Cluster(tmp_path / "input-schema", None)
    document = make_graph(definition_id="input-schema")
    document["tasks"]["work"]["contract"]["input_schema"] = deepcopy(_JSON_ONLY)
    published = cluster.publish(document)
    dataset = cluster.artifact(b"dataset", "text/plain")
    run = cluster.create_run(published["definition_ref"], {"dataset": dataset})

    state, actions = _binding_failure(cluster, run["run_id"])
    failure = _failure_action(actions)
    assert failure["reason"] == "binding_unexecutable"
    assert failure["error_code"] == "invalid_argument"
    assert state["run"]["status"] == "failed"
    assert state["workers"] == []
    assert _stage(state, "first")["state"] == "failed"


def test_api_refuses_a_direct_binding_with_incompatible_task_inputs(
        tmp_path, monkeypatch):
    cluster = Cluster(tmp_path / "input-schema-api", None)
    document = make_graph(definition_id="input-schema-api")
    document["tasks"]["work"]["contract"]["input_schema"] = deepcopy(_JSON_ONLY)
    published = cluster.publish(document)
    dataset = cluster.artifact(b"dataset", "text/plain")
    run = cluster.create_run(published["definition_ref"], {"dataset": dataset})
    monkeypatch.setattr(cluster.scheduler, "_start_workers",
                        lambda run_id, actions: False)
    cluster.scheduler.reconcile(run["run_id"])
    state = cluster.state(run["run_id"])
    node = _stage(state, "first")
    assert node["state"] == "ready"
    bundle = cluster.api.definition_bundle_for_run(
        cluster.scheduler.actor(run["run_id"]), run["run_id"])
    task = bundle.tasks[node["template_ref"]]
    agent = bundle.agents[task.agent_ref]

    with pytest.raises(ControlPlaneError) as refused:
        cluster.api.commit_plan(cluster.scheduler.actor(run["run_id"]), [
            {"kind": "node", "id": node["node_id"],
             "revision": node["revision"]},
            {"kind": "run", "id": run["run_id"],
             "revision": state["run"]["revision"]},
        ], [{
            "kind": "worker", "op": "create",
            "fields": {"worker_id": f"{node['node_id']}-w1",
                       "node_id": node["node_id"],
                       "backend_ref": agent.backend_ref,
                       "agent_ref": agent.agent_ref,
                       "controller_id": "ctl-1"},
        }], rid("direct-binding"))
    assert refused.value.code == "invalid_argument"
    assert cluster.state(run["run_id"])["workers"] == []


def test_checker_input_schema_rejects_incompatible_inputs_before_binding(tmp_path):
    cluster = Cluster(tmp_path / "checker-schema", None)
    cluster.backend.agent = accepting_agent(cluster.api)
    document = make_graph(definition_id="checker-schema")
    # The Checker may only consume application/json. Its frozen inputs are the
    # candidate refs plus the target's text/plain run input.
    document["tasks"]["check"]["contract"]["input_schema"] = deepcopy(_JSON_ONLY)
    published = cluster.publish(document)
    dataset = cluster.artifact(b"dataset", "text/plain")
    run = cluster.create_run(published["definition_ref"], {"dataset": dataset})
    run_id = run["run_id"]

    actions = []
    for _ in range(60):
        actions.extend(cluster.scheduler.reconcile(run_id)["actions"])
        state = cluster.state(run_id)
        for worker in state["workers"]:
            if worker["controller_id"] == "ctl-1":
                cluster.controller.reconcile(run_id, worker["worker_id"])
        if cluster.api.get_resource(reader(), "run", run_id)["status"] in (
                "completed", "failed", "cancelled"):
            break
    state = cluster.state(run_id)
    assert state["run"]["status"] == "failed"
    assert state["candidates"], "the ordinary stage did run and submit"

    reviews = [node for node in state["nodes"] if node.get("closure")]
    assert len(reviews) == 1
    review = reviews[0]
    failure = next(action for action in actions
                   if action["action"] == "node_failed"
                   and action["node_id"] == review["node_id"])
    assert failure["reason"] == "binding_unexecutable"
    assert failure["error_code"] == "invalid_argument"
    assert review["state"] == "failed"
    # Inputs were frozen first: the rejection is before any execution binding.
    assert review["input_refs"]
    assert not [worker for worker in state["workers"]
                if worker["node_id"] == review["node_id"]]
    assert _stage(state, "first")["state"] == "failed"


# -- execution failures and unknown effects -----------------------------

def _plain_control(root):
    api = Api(root)
    backend = ScriptBackend(root)
    api.backend_capabilities = {"script": backend.capabilities()}
    scheduler = Scheduler(api, scheduler_id="scheduler", controller_id="ctl-1")
    controller = Controller(api, {"script": backend}, controller_id="ctl-1",
                            data_dir=root, poll_seconds=0.0)
    return api, scheduler, controller


def _plain_pump(api, scheduler, controller, run_id, predicate, limit=40):
    scheduler_actor = Actor("scheduler", {"scheduler"})
    for _ in range(limit):
        scheduler.reconcile(run_id)
        state = api.list_resources(scheduler_actor, run_id)
        for worker in state["workers"]:
            controller.reconcile(run_id, worker["worker_id"])
        run = api.get_resource(reader(), "run", run_id)
        state = api.list_resources(scheduler_actor, run_id)
        if predicate(run, state):
            return run, state
    raise AssertionError("the plain control plane did not reach the expected state")


def _plain_run(api, document, *, definition_id):
    published = api.publish_definition(publisher(), document, rid("publish"))
    dataset = api.put_artifact(publisher(), rid("artifact"), b"dataset",
                               "text/plain")["artifact"]
    return api.create_run(publisher(), published["definition_ref"],
                          {"dataset": dataset}, rid("run"))


def test_worker_process_failure_fails_the_node(tmp_path):
    api, scheduler, controller = _plain_control(tmp_path / "wpd")
    run = _plain_run(api, make_graph(definition_id="wpd"),
                     definition_id="wpd")
    run_id = run["run_id"]
    _, state = _plain_pump(api, scheduler, controller, run_id,
                           lambda run, state: bool(state["workers"]))
    worker = state["workers"][0]
    assert worker["desired"] == "active"

    scheduler_actor = Actor("scheduler", {"scheduler"})
    api.enqueue_message(scheduler_actor, worker["worker_id"],
                        {"kind": "scripted_fail", "script": {"mode": "fail"}},
                        "manual-fail", rid("message"))
    run, state = _plain_pump(
        api, scheduler, controller, run_id,
        lambda run, state: run["status"] != "running")
    assert run["status"] == "failed"
    assert run["summary"]["reason"] == "worker_failed"
    node = next(node for node in state["nodes"]
                if node["local_child_id"] == "first")
    assert node["state"] == "failed"
    assert state["candidates"] == []


def test_unknown_effect_blocks_without_replay(tmp_path):
    api, scheduler, controller = _plain_control(tmp_path / "effect")
    run = _plain_run(api, make_graph(definition_id="effect"),
                     definition_id="effect")
    run_id = run["run_id"]
    _, state = _plain_pump(api, scheduler, controller, run_id,
                           lambda run, state: bool(state["workers"]))
    worker = state["workers"][0]

    scheduler_actor = Actor("scheduler", {"scheduler"})
    api.enqueue_message(scheduler_actor, worker["worker_id"], {
        "kind": "scripted_unknown",
        "script": {
            "tool_calls": [{"name": "write", "effect": "unknown",
                            "target": "out.txt", "content": "x"}],
            "crash_after_tool_start": True,
        },
    }, "manual-unknown", rid("message"))
    run, state = _plain_pump(
        api, scheduler, controller, run_id,
        lambda run, state: run["status"] != "running")
    assert run["status"] == "blocked"
    assert run["summary"]["reason"] == "unknown_effect"
    node = next(node for node in state["nodes"]
                if node["local_child_id"] == "first")
    assert node["state"] == "blocked"

    backend = controller.backends["script"]
    execution = backend._state((worker["worker_id"], worker["epoch"]))
    assert execution["order"].count("manual-unknown") == 1
    # A blocked execution is never replayed into another delivery.
    for _ in range(3):
        controller.reconcile(run_id, worker["worker_id"])
    execution = backend._state((worker["worker_id"], worker["epoch"]))
    assert execution["order"].count("manual-unknown") == 1
    assert execution["deliveries"]["manual-unknown"]["state"] == "unknown"
