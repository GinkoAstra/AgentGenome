"""W06 examples: deterministic oracle, Graph /2 publication and Pi bindings.

These checks do not start a model. They prove the example SOP, the independent
oracle and the trusted WorkerSpec composition. Real Pi/Teacher acceptance is
run separately by the root integrator.
"""
from __future__ import annotations

import importlib.util
import json
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from conftest import ISOLATED_ROOT
from helpers import operator, rid, submit_candidate, toolbox

from node_runtime.control_plane.api import Api
from node_runtime.control_plane.backends.base import WorkerSpec
from node_runtime.control_plane.backends.pi import PiBackend
from node_runtime.control_plane.backends.script import ScriptBackend
from node_runtime.control_plane.scheduler import Scheduler
from node_runtime.control_plane.worker_specs import compose_worker_spec

EXAMPLES = ISOLATED_ROOT / "examples" / "agent-workflow"

EXPECTED_CLEAN = {
    "rows": [
        {"name": "Alice", "email": "alice@example.com"},
        {"name": "Bob", "email": "bob@example.com"},
        {"name": "Carol", "email": "carol@example.com"},
    ],
    "audit": {
        "input_rows": 6,
        "output_rows": 3,
        "invalid_rows": 2,
        "duplicate_rows": 1,
    },
}
EXPECTED_PROFILE = {"rows": 3, "domains": {"example.com": 3}}


def _document(name: str) -> dict:
    return yaml.safe_load((EXAMPLES / name).read_text(encoding="utf-8"))


def _fixture(name: str):
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


def _business_checks():
    spec = importlib.util.spec_from_file_location(
        "example_business_checks", EXAMPLES / "business_checks.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _published_api(tmp_path: Path, document: dict):
    backend = PiBackend(tmp_path / "runtime", data_dir=tmp_path / "data")
    script = ScriptBackend(tmp_path / "script")
    api = Api(tmp_path / "state",
              backend_capabilities={"pi": backend.capabilities(),
                                    "script": script.capabilities()})
    published = api.publish_definition(operator(), document, rid("publish"))
    return api, published


def _run_once(api: Api, published: dict, name: str, content: bytes,
              media_type: str) -> tuple[str, dict]:
    artifact = api.put_artifact(operator(), rid("artifact"), content,
                                media_type)["artifact"]
    run = api.create_run(operator(), published["definition_ref"],
                         {name: artifact}, rid("run"))
    Scheduler(api).reconcile(run["run_id"], max_steps=50)
    return run["run_id"], api.list_resources(operator(), run["run_id"])


def _stage(resources: dict, name: str) -> dict:
    return next(node for node in resources["nodes"]
                if node["local_child_id"] == name)


def _worker_spec(worker: dict) -> WorkerSpec:
    return WorkerSpec(
        worker_id=worker["worker_id"], node_id=worker["node_id"],
        epoch=worker["epoch"], intent_seq=worker["intent_seq"],
        backend_ref=worker["backend_ref"], agent_ref=worker["agent_ref"],
        desired=worker["desired"], run_id=worker["run_id"],
        context=worker["context"], tool_profile=worker["tool_profile"],
        budget=worker["budget"], usage=worker["usage"],
        required_capabilities=worker["required_capabilities"])


# -- independent oracle --------------------------------------------------

def test_business_oracle_recomputes_the_fixture_exactly():
    checks = _business_checks()
    source = _fixture("clean-input.json")
    clean = checks.expected_clean(source)
    assert clean == EXPECTED_CLEAN
    assert checks.expected_profile(clean) == EXPECTED_PROFILE
    checks.verify_clean(source, clean, EXPECTED_PROFILE)


def test_business_oracle_rejects_a_wrong_row_or_counter():
    checks = _business_checks()
    source = _fixture("clean-input.json")

    wrong_rows = deepcopy(EXPECTED_CLEAN)
    wrong_rows["rows"][0]["name"] = "ALICE"
    with pytest.raises(AssertionError, match="cleaning output"):
        checks.verify_clean(source, wrong_rows, EXPECTED_PROFILE)

    wrong_audit = deepcopy(EXPECTED_CLEAN)
    wrong_audit["audit"]["duplicate_rows"] = 0
    with pytest.raises(AssertionError, match="cleaning output"):
        checks.verify_clean(source, wrong_audit, EXPECTED_PROFILE)


def test_business_oracle_rejects_a_wrong_profile():
    checks = _business_checks()
    source = _fixture("clean-input.json")
    wrong_profile = {"rows": 3, "domains": {"example.com": 2}}
    with pytest.raises(AssertionError, match="profile"):
        checks.verify_clean(source, EXPECTED_CLEAN, wrong_profile)


# -- clean business example ---------------------------------------------

def test_clean_example_is_graph_v2_with_pi_workers_and_program_checkers():
    document = _document("graph-clean.yaml")
    assert document["format"] == "agentgenome-graph-definition/2"
    assert document["policy"] == "fixed"
    assert {name: agent["backend_ref"]
            for name, agent in document["agents"].items()} == {
        "cleaner": "pi", "profiler": "pi",
        "clean-checker": "script", "profile-checker": "script"}
    assert set(document["tasks"]) == {
        "clean-work", "clean-check", "profile-work", "profile-check"}
    assert document["nodes"]["clean"]["reviewer_ref"] == "clean-check"
    assert document["nodes"]["profile"]["reviewer_ref"] == "profile-check"
    for name in ("clean-work", "profile-work"):
        assert document["tasks"][name]["completion_kind"] == "candidate"
    for name in ("clean-check", "profile-check"):
        assert document["tasks"][name]["completion_kind"] == "review_receipt"


def test_clean_example_binds_only_granted_pi_tools(tmp_path):
    document = _document("graph-clean.yaml")
    api, published = _published_api(tmp_path, document)
    _, resources = _run_once(
        api, published, "dataset", (EXAMPLES / "clean-input.json").read_bytes(),
        "application/json")

    clean = _stage(resources, "clean")
    profile = _stage(resources, "profile")
    assert clean["state"] == "running", clean.get("error_json")
    assert profile["state"] == "pending"
    assert resources["run"]["status"] == "running"

    worker = next(item for item in resources["workers"]
                  if item["node_id"] == clean["node_id"])
    assert worker["backend_ref"] == "pi"
    assert worker["tool_profile"]["backend_tools"] == ["read_file", "write_file"]
    assert {entry["name"] for entry in worker["tool_profile"]["tools"]} == {
        "node.publish_artifact", "node.submit_candidate"}


# -- bounded Goal example ------------------------------------------------

def test_goal_example_declares_the_frozen_progress_policy():
    document = _document("graph-goal.yaml")
    assert document["format"] == "agentgenome-graph-definition/2"
    assert {name: agent["backend_ref"]
            for name, agent in document["agents"].items()} == {
        "quotes-writer": "pi", "render-worker": "script",
        "script-checker": "script"}
    assert document["agents"]["script-checker"]["tool_profile"] == {
        "tools": [{"name": "node.submit_review", "version": "1",
                   "scope": "review:assigned"}],
        "backend_tools": [],
    }
    assert document["tasks"]["quotes-work"]["progress"] == {
        "no_improvement_limit": 2}
    assert "progress" not in document["tasks"]["render-work"]
    assert document["nodes"]["quotes"]["reviewer_ref"] == "quotes-check"
    assert document["nodes"]["render"]["reviewer_ref"] == "render-check"
    for name in ("quotes-check", "render-check"):
        assert document["tasks"][name]["completion_kind"] == "review_receipt"


def test_goal_example_binds_pi_tools_from_pinned_material(tmp_path):
    document = _document("graph-goal.yaml")
    api, published = _published_api(tmp_path, document)
    _, resources = _run_once(
        api, published, "material", (EXAMPLES / "research-notes.md").read_bytes(),
        "text/markdown")

    quotes = _stage(resources, "quotes")
    render = _stage(resources, "render")
    assert quotes["state"] == "running", quotes.get("error_json")
    assert render["state"] == "pending"
    worker = next(item for item in resources["workers"]
                  if item["node_id"] == quotes["node_id"])
    assert worker["backend_ref"] == "pi"
    assert worker["tool_profile"]["backend_tools"] == ["read_file", "write_file"]
    assert {entry["name"] for entry in worker["tool_profile"]["tools"]} == {
        "node.publish_artifact", "node.submit_candidate"}


# -- declared Checker bindings ------------------------------------------

def test_clean_example_checker_gets_only_review_tools(tmp_path):
    document = _document("graph-clean.yaml")
    api, published = _published_api(tmp_path, document)
    run_id, resources = _run_once(
        api, published, "dataset", (EXAMPLES / "clean-input.json").read_bytes(),
        "application/json")

    clean = _stage(resources, "clean")
    worker = next(item for item in resources["workers"]
                  if item["node_id"] == clean["node_id"])
    spec = _worker_spec(worker)
    submit_candidate(toolbox(api, spec, {"role": "worker", "kind": "start"}),
                     spec)
    Scheduler(api).reconcile(run_id, max_steps=50)

    resources = api.list_resources(operator(), run_id)
    review = next(node for node in resources["nodes"] if node.get("closure"))
    bundle = api.definition_bundle_for_run(operator(), run_id)
    task = bundle.tasks[review["template_ref"]]
    agent = bundle.agents[task.agent_ref]
    candidate = next(item for item in resources["candidates"]
                     if item["candidate_id"]
                     == review["closure"]["candidate_id"])
    checker = compose_worker_spec(
        run_id=run_id, node=review, worker_id=review["closure"]["worker_id"],
        task=task, agent=agent,
        capabilities=api.backend_capabilities.get(agent.backend_ref),
        candidate=candidate, epoch=0, desired="active",
        run_budget=api.get_resource(operator(), "run", run_id)["budget"])

    assert agent.backend_ref == "script"
    assert task.completion_kind == "review_receipt"
    assert checker.tool_profile == {
        "tools": [{"name": "node.submit_review", "version": "1",
                   "scope": "review:assigned"}],
        "backend_tools": [],
    }
    assert checker.context["role"] == "reviewer"
    assert checker.context["candidate_id"] == candidate["candidate_id"]


def test_goal_example_freezes_the_quotes_progress_policy(tmp_path):
    document = _document("graph-goal.yaml")
    api, published = _published_api(tmp_path, document)
    run_id, resources = _run_once(
        api, published, "material", (EXAMPLES / "research-notes.md").read_bytes(),
        "text/markdown")
    bundle = api.definition_bundle_for_run(operator(), run_id)

    quotes = _stage(resources, "quotes")
    render = _stage(resources, "render")
    assert bundle.tasks[quotes["template_ref"]].progress == {
        "no_improvement_limit": 2}
    assert bundle.tasks[render["template_ref"]].progress is None
    review_task = bundle.tasks[quotes["reviewer_ref"]]
    assert review_task.completion_kind == "review_receipt"
