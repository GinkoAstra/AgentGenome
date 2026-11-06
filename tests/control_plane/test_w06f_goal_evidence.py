"""Focused checks for the evidence-selection Goal and deterministic renderer.

The Pi side is a controlled Script fixture, not a real model: it reads the
frozen material and publishes quotes exactly as a Worker callback would. The
render stage, both reviews and all receipts are the real trusted program in
``examples/agent-workflow/research_workflow.py``.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

from conftest import ISOLATED_ROOT
from helpers import AgentScriptBackend, TERMINAL, operator, rid, toolbox

from node_runtime.control_plane.api import Api
from node_runtime.control_plane.backends.base import WorkerSpec
from node_runtime.control_plane.backends.pi import PiBackend
from node_runtime.control_plane.controller import Controller
from node_runtime.control_plane.scheduler import Scheduler
from node_runtime.control_plane.task_definitions import compile_definition_bundle

EXAMPLES = ISOLATED_ROOT / "examples" / "agent-workflow"
MATERIAL = EXAMPLES / "research-notes.md"
PAGE_FINAL = "Generated from pinned local notes with no web retrieval."


def _workflow():
    if "research_workflow" in globals():
        return globals()["research_workflow"]
    spec = importlib.util.spec_from_file_location(
        "example_research_workflow", EXAMPLES / "research_workflow.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    globals()["research_workflow"] = module
    return module


def _document() -> dict:
    return yaml.safe_load((EXAMPLES / "graph-goal.yaml").read_text(
        encoding="utf-8"))


def _no_external(ref: str):
    raise AssertionError(f"unexpected external definition read: {ref}")


def _parsed():
    return _workflow().parse_material(MATERIAL.read_text(encoding="utf-8"))


def _quote(source_id: str, body: str, length: int = 60) -> dict:
    return {"source_id": source_id,
            "quote": _workflow()._normalize(body)[:length]}


def _complete_quotes() -> dict:
    parsed = _parsed()
    return {"title": parsed["title"],
            "quotes": [_quote(section["source_id"], section["body"])
                       for section in parsed["sections"]]}


def _quote_agent(api, *, only_first: bool = False, wrong_title: bool = False):
    """Controlled Worker fixture standing in for the Pi quotes writer."""

    def run(spec, message, state):
        box = toolbox(api, spec, message)
        refs = spec.context.get("input_refs") or []
        material_ref = next(ref for ref in refs
                            if ref.get("media_type") == "text/markdown")
        parsed = _workflow().parse_material(
            api.read_artifact(box.actor, material_ref).decode("utf-8"))
        sections = parsed["sections"][:1] if only_first else parsed["sections"]
        payload = {
            "title": ("Wrong title" if wrong_title else parsed["title"]),
            "quotes": [_quote(section["source_id"], section["body"])
                       for section in sections],
        }
        round_number = int(message.get("round") or 1)
        call = f"q-{spec.worker_id}-{round_number}"
        ref = box.publish_artifact(
            {"path": "quotes.json", "media_type": "application/json"},
            call_id=f"publish-{call}",
            content=json.dumps(payload).encode("utf-8"))["output"]
        box.invoke("node.submit_candidate", {
            "candidate_id": f"quotes-{spec.node_id}-r{round_number}",
            "round": round_number,
            "output_refs": [ref],
        }, call_id=f"candidate-{call}")

    return run


class FakePiBackend(AgentScriptBackend):
    """Controlled Pi stand-in: same callback protocol, no model call."""

    name = "pi"

    def __init__(self, root: Path, capabilities):
        super().__init__(root, agent=None)
        self._capabilities = capabilities

    def capabilities(self):
        return self._capabilities


class GoalCluster:
    def __init__(self, root: Path, quote_agent) -> None:
        self.root = Path(root)
        self.api = Api(self.root)
        capabilities = PiBackend(self.root / "pi-cap",
                                 data_dir=self.root / "pi-data").capabilities()
        self.pi = FakePiBackend(self.root / "pi-runtime", capabilities)
        self.pi.agent = quote_agent
        self.script = AgentScriptBackend(
            self.root / "script-runtime",
            agent=_workflow().build_agent(self.api))
        self.api.backend_capabilities = {
            "pi": capabilities, "script": self.script.capabilities()}
        self.scheduler = Scheduler(self.api, scheduler_id="scheduler",
                                   controller_id="ctl-1")
        self.controller = Controller(
            self.api, {"pi": self.pi, "script": self.script},
            controller_id="ctl-1", data_dir=self.root, poll_seconds=0.0)

    def start(self, *, material: bytes | None = None) -> str:
        document = _document()
        published = self.api.publish_definition(
            operator(), document, rid("publish"))
        artifact = self.api.put_artifact(
            operator(), rid("artifact"),
            material if material is not None else MATERIAL.read_bytes(),
            "text/markdown")["artifact"]
        run = self.api.create_run(
            operator(), published["definition_ref"], {"material": artifact},
            rid("run"))
        return run["run_id"]

    def drive(self, run_id: str, limit: int = 120) -> dict:
        for _ in range(limit):
            self.scheduler.reconcile(run_id)
            resources = self.api.list_resources(operator(), run_id)
            for worker in resources["workers"]:
                if worker["controller_id"] == "ctl-1":
                    self.controller.reconcile(run_id, worker["worker_id"])
            run = self.api.get_resource(operator(), "run", run_id)
            if run["status"] in TERMINAL:
                resources = self.api.list_resources(operator(), run_id)
                resources["run"] = run
                return resources
        raise AssertionError("the Goal run did not reach a terminal state")


def _stage(resources: dict, name: str) -> dict:
    return next(node for node in resources["nodes"]
                if node["local_child_id"] == name)


# -- frozen material and evidence contract ------------------------------

def test_parse_material_reads_only_s_numbered_sections():
    parsed = _parsed()
    assert parsed["title"] == "Why the Sky Is Blue — pinned operator notes"
    assert [section["source_id"] for section in parsed["sections"]] == [
        "S1", "S2", "S3", "S4"]
    assert "These notes are an illustrative summary" not in " ".join(
        section["body"] for section in parsed["sections"])


def test_complete_quote_selection_scores_the_whole_goal():
    result = _workflow().evaluate_quotes(
        MATERIAL.read_text(encoding="utf-8"), _complete_quotes())
    assert result["complete"] is True
    assert result["comparable"] is True
    assert result["score"] == result["max_score"] == 6
    assert result["title_ok"] is True and result["structure_ok"] is True
    assert result["coverage"] == {"S1": True, "S2": True,
                                  "S3": True, "S4": True}
    assert result["issues"] == []


def test_partial_quote_selection_is_comparable_and_names_missing_sources():
    candidate = _complete_quotes()
    candidate["quotes"] = candidate["quotes"][:1]
    result = _workflow().evaluate_quotes(
        MATERIAL.read_text(encoding="utf-8"), candidate)
    assert result["complete"] is False
    assert result["comparable"] is True
    assert result["coverage"] == {"S1": True, "S2": False,
                                  "S3": False, "S4": False}
    assert result["missing_sources"] == ["S2", "S3", "S4"]
    assert result["score"] == 3


def test_structurally_valid_empty_draft_is_comparable():
    result = _workflow().evaluate_quotes(
        MATERIAL.read_text(encoding="utf-8"),
        {"title": "Why the Sky Is Blue — pinned operator notes", "quotes": []})
    assert result["complete"] is False
    assert result["comparable"] is True
    assert result["coverage"] == {"S1": False, "S2": False,
                                  "S3": False, "S4": False}


@pytest.mark.parametrize("bad", [
    None, [], {"title": "Why the Sky Is Blue — pinned operator notes"},
    {"title": True, "quotes": []},
    {"title": "Why the Sky Is Blue — pinned operator notes", "quotes": [17]},
])
def test_malformed_typed_shape_is_not_comparable(bad):
    result = _workflow().evaluate_quotes(
        MATERIAL.read_text(encoding="utf-8"), bad)
    assert result["complete"] is False
    assert result["comparable"] is False


def test_wrong_title_and_unknown_or_duplicate_sources_are_rejected():
    material = MATERIAL.read_text(encoding="utf-8")
    wrong_title = _complete_quotes()
    wrong_title["title"] = "A different title"
    assert _workflow().evaluate_quotes(material, wrong_title)["title_ok"] is False

    unknown = _complete_quotes()
    unknown["quotes"].append({"source_id": "S9", "quote": "invented text" * 3})
    result = _workflow().evaluate_quotes(material, unknown)
    assert result["complete"] is False
    assert result["unknown_sources"] == ["S9"]

    duplicate = _complete_quotes()
    duplicate["quotes"].append(dict(duplicate["quotes"][0]))
    result = _workflow().evaluate_quotes(material, duplicate)
    assert result["complete"] is False
    assert result["duplicate_sources"] == ["S1"]


def test_non_contiguous_and_short_quotes_are_rejected():
    material = """
# T

## S1. One

The quick brown fox jumps over the lazy dog near the river bank.
"""
    joined = {"title": "T", "quotes": [{
        "source_id": "S1",
        "quote": "The quick brown fox ... near the river bank",
    }]}
    result = _workflow().evaluate_quotes(material, joined)
    assert result["complete"] is False
    assert result["coverage"]["S1"] is False

    short = {"title": "T", "quotes": [{"source_id": "S1", "quote": "The quick"}]}
    result = _workflow().evaluate_quotes(material, short)
    assert result["complete"] is False
    assert result["coverage"]["S1"] is False


def test_unreadable_candidate_is_unknown_not_a_false_zero():
    result = _workflow().evaluate_quotes(
        MATERIAL.read_text(encoding="utf-8"), "not json at all")
    assert result["comparable"] is False
    assert result["complete"] is False
    assert result["score"] == 0


def test_render_lesson_is_deterministic_escaped_and_offline():
    workflow = _workflow()
    material = """
# A <small> title

## S1. Unsafe section

The body contains <script>alert(1)</script> and enough words to quote.
"""
    quotes = {"title": "A <small> title", "quotes": [{
        "source_id": "S1",
        "quote": "The body contains <script>alert(1)</script> and enough words",
    }]}
    first = workflow.render_lesson(material, quotes)
    second = workflow.render_lesson(material, quotes)
    assert first == second
    assert "<script>" not in first
    assert "&lt;script&gt;" in first
    assert "A &lt;small&gt; title" in first
    assert "url(http" not in first.lower()
    assert PAGE_FINAL in first
    assert "<h2>Sources</h2>" in first


def test_first_mismatch_reports_offset_and_lengths():
    workflow = _workflow()
    assert workflow._first_mismatch("abc", "abc") == {
        "offset": 3, "expected_length": 3, "actual_length": 3}
    assert workflow._first_mismatch("abcd", "abXd") == {
        "offset": 2, "expected_length": 4, "actual_length": 4}
    assert workflow._first_mismatch("abcd", "ab") == {
        "offset": 2, "expected_length": 4, "actual_length": 2}


# -- pinned Graph and roles ---------------------------------------------

def test_graph_pins_the_two_work_tasks_and_three_agent_roles():
    document = _document()
    workflow = _workflow()
    bundle = compile_definition_bundle(document, read_document=_no_external)
    assert bundle.document["tasks"]["quotes-work"] == workflow.SUPPORTED_QUOTES_TASK
    assert bundle.document["tasks"]["render-work"] == workflow.SUPPORTED_RENDER_TASK
    assert {name: agent["backend_ref"]
            for name, agent in document["agents"].items()} == {
        "quotes-writer": "pi",
        "render-worker": "script",
        "script-checker": "script",
    }
    assert document["tasks"]["quotes-work"]["progress"] == {
        "no_improvement_limit": 2}
    assert "progress" not in document["tasks"]["render-work"]
    for name in ("quotes-check", "render-check"):
        assert document["tasks"][name]["completion_kind"] == "review_receipt"
    assert document["nodes"]["quotes"]["needs"] == []
    assert document["nodes"]["render"]["needs"] == ["quotes"]
    assert document["nodes"]["render"]["inputs"]["quotes"] == {"from": "quotes"}
    assert document["agents"]["script-checker"]["tool_profile"] == {
        "tools": [{"name": "node.submit_review", "version": "1",
                   "scope": "review:assigned"}],
        "backend_tools": [],
    }


class _FakeNodeApi:
    def __init__(self, node: dict) -> None:
        self.node = node
        self.reads: list[tuple[str, str]] = []

    def get_resource(self, actor, kind, key):
        self.reads.append((kind, key))
        return self.node


def _render_spec(workflow, *, context_template: str | None = None,
                 context_contract: str | None = None) -> WorkerSpec:
    return WorkerSpec(
        worker_id="w1", node_id="n1", epoch=2, intent_seq=1,
        backend_ref="script", agent_ref="sha256:" + "0" * 64,
        desired="active", run_id=rid("run"),
        context={"role": "worker",
                 "template_ref": context_template or workflow.SUPPORTED_RENDER_TASK,
                 "contract_ref": context_contract or workflow.SUPPORTED_RENDER_TASK},
        tool_profile={}, budget={}, usage={}, required_capabilities={})


def test_render_work_requires_actual_node_and_matching_contract(monkeypatch):
    workflow = _workflow()
    executed: list[tuple] = []
    monkeypatch.setattr(workflow, "_run_render_worker",
                        lambda *args: executed.append(args))
    spec = _render_spec(workflow)

    revised = _FakeNodeApi({
        "node_id": "n1", "template_ref": workflow.SUPPORTED_RENDER_TASK,
        "contract_ref": "sha256:" + "f" * 64})
    with pytest.raises(RuntimeError, match="only executes the frozen render-work"):
        workflow.build_agent(revised)(spec, {"role": "worker", "round": 1}, {})
    assert revised.reads == [("node", "n1")] and executed == []

    unsupported = _FakeNodeApi({
        "node_id": "n1", "template_ref": "sha256:" + "1" * 64,
        "contract_ref": "sha256:" + "1" * 64})
    with pytest.raises(RuntimeError, match="only executes the frozen render-work"):
        workflow.build_agent(unsupported)(spec, {"role": "worker", "round": 1}, {})
    assert unsupported.reads == [("node", "n1")]

    agreeing = _FakeNodeApi({
        "node_id": "n1", "template_ref": workflow.SUPPORTED_RENDER_TASK,
        "contract_ref": workflow.SUPPORTED_RENDER_TASK})
    disagreeing = _render_spec(workflow, context_contract="sha256:" + "f" * 64)
    with pytest.raises(RuntimeError, match="only executes the frozen render-work"):
        workflow.build_agent(agreeing)(disagreeing, {"role": "worker", "round": 1}, {})
    assert executed == []

    workflow.build_agent(agreeing)(spec, {"role": "worker", "round": 1}, {})
    assert executed


# -- full controlled run ------------------------------------------------

def test_goal_run_accepts_quotes_then_renders_and_records_receipts(tmp_path):
    cluster = GoalCluster(tmp_path, None)
    cluster.pi.agent = _quote_agent(cluster.api)
    run_id = cluster.start()
    resources = cluster.drive(run_id)
    assert resources["run"]["status"] == "completed"

    quotes_node = _stage(resources, "quotes")
    render_node = _stage(resources, "render")
    assert quotes_node["state"] == "succeeded"
    assert render_node["state"] == "succeeded"

    accepted = {candidate["node_id"]: candidate
                for candidate in resources["candidates"]
                if candidate["decision"] == "accept"}
    quote_ref = accepted[quotes_node["node_id"]]["output_refs"][0]
    render_ref = accepted[render_node["node_id"]]["output_refs"][0]
    assert accepted[render_node["node_id"]]["candidate_id"].startswith(
        f"render-{render_node['node_id']}-e")
    quotes = json.loads(cluster.api.read_artifact(
        operator(), quote_ref).decode("utf-8"))
    expected = _workflow().render_lesson(
        MATERIAL.read_text(encoding="utf-8"), quotes)
    actual = cluster.api.read_artifact(operator(), render_ref).decode("utf-8")
    assert actual == expected
    assert PAGE_FINAL in actual
    assert render_ref["media_type"] == "text/html"

    workers = {worker["node_id"]: worker for worker in resources["workers"]}
    assert workers[render_node["node_id"]]["usage"] == {
        "provider_calls": 0, "tool_calls": 2}
    review_nodes = [node for node in resources["nodes"] if node.get("closure")]
    assert len(review_nodes) == 2
    for node in review_nodes:
        assert workers[node["node_id"]]["usage"] == {
            "provider_calls": 0, "tool_calls": 1}

    reviews = {review["node_id"]: review for review in resources["reviews"]}
    render_review = reviews[render_node["node_id"]]
    assert render_review["verdict"] == "accept"
    feedback = json.loads(cluster.api.read_artifact(
        operator(), render_review["feedback_ref"]).decode("utf-8"))
    assert feedback["quotes_complete"] is True
    assert feedback["expected_sha256"] == render_ref["id"]
    assert feedback["actual_sha256"] == render_ref["sha256"]
    assert feedback["mismatch"] is None


def test_quote_stagnation_stops_after_two_unchanged_checkpoints(tmp_path):
    cluster = GoalCluster(tmp_path, None)
    cluster.pi.agent = _quote_agent(cluster.api, only_first=True)
    run_id = cluster.start()
    resources = cluster.drive(run_id)
    assert resources["run"]["status"] == "failed"
    quotes_node = _stage(resources, "quotes")
    assert quotes_node["state"] == "failed"
    results = [item["result"]
               for item in quotes_node["progress"]["evaluations"]]
    assert results == ["unknown", "not_improved", "not_improved"]
    assert _stage(resources, "render")["state"] != "succeeded"
    assert [candidate["round"] for candidate in resources["candidates"]] == [
        1, 2, 3]
