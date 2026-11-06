"""W02b1: pure task-definition resolution and Graph /2 compilation.

Everything here is a module-level contract: no API, Scheduler, Controller or
Backend is started. Graph /1 documents are rejected, not executed.
"""
from __future__ import annotations

import hashlib
import uuid
from copy import deepcopy

import pytest

from node_runtime.control_plane import ControlPlaneError, resources as R
from node_runtime.control_plane.policies import FixedPolicy
from node_runtime.control_plane.task_definitions import (
    GRAPH_DEFINITION_FORMAT,
    TASK_DEFINITION_FORMAT,
    DefinitionBundle,
    compile_definition_bundle,
    initial_graph_tasks,
    resolve_task_definition,
)

BACKEND = "pi"


def artifact(tag: str) -> dict[str, str | int]:
    digest = hashlib.sha256(tag.encode("utf-8")).hexdigest()
    return {"id": f"sha256:{digest}", "sha256": digest,
            "size": len(tag), "media_type": "text/plain"}


def agent_document(instructions: str = "Do the task.", **overrides) -> dict:
    body = {
        "format": "agentgenome-agent-definition/1",
        "backend_ref": BACKEND,
        "instructions": instructions,
        "tool_profile": {},
    }
    body.update(overrides)
    return body


def contract(**overrides) -> dict:
    body = {
        "input_schema": {"type": "array", "items": {"type": "object"}},
        "output_schema": {"type": "array"},
        "acceptance": "Every row follows the declared SOP.",
    }
    body.update(overrides)
    return body


def task_document(agent_ref: str, **overrides) -> dict:
    body = {
        "format": TASK_DEFINITION_FORMAT,
        "goal": "Do the work.",
        "agent_ref": agent_ref,
        "contract": contract(),
    }
    body.update(overrides)
    return body


def graph_document(agents: dict, tasks: dict, nodes: dict,
                   **overrides) -> dict:
    body = {
        "format": GRAPH_DEFINITION_FORMAT,
        "definition_id": "example",
        "base_revision": 0,
        "policy": "fixed",
        "budget_limits": {"max_nodes": 20, "max_depth": 4, "max_rounds": 4},
        "agents": agents,
        "tasks": tasks,
        "nodes": nodes,
    }
    body.update(overrides)
    return body


class Library:
    """Content-addressed reader double; never a file or network reader."""

    def __init__(self, *documents: dict) -> None:
        self.documents = {R.digest(item): item for item in documents}
        self.calls: list[str] = []

    def __call__(self, reference: str) -> dict:
        self.calls.append(reference)
        return self.documents[reference]


def code_of(call) -> str:
    with pytest.raises(ControlPlaneError) as caught:
        call()
    return caught.value.code


def standard() -> tuple[dict, dict, dict, dict, dict, dict]:
    """Inline catalogs: aliases are rewritten only inside this source doc."""
    worker_doc = agent_document("Clean the dataset.")
    reviewer_doc = agent_document("Review the candidate.")
    work = task_document("worker", goal="Produce rows.")
    check = task_document("reviewer", goal="Check rows.",
                          completion_kind="review_receipt")
    agents = {"worker": worker_doc, "reviewer": reviewer_doc}
    tasks = {"work": work, "check": check}
    nodes = {
        "phase_a": {"task_ref": "work", "reviewer_ref": "check", "needs": [],
                    "inputs": {"dataset": {"run_input": "dataset"}}},
        "phase_b": {"task_ref": "work", "reviewer_ref": "check",
                    "needs": ["phase_a"],
                    "inputs": {"dataset": {"from": "phase_a"}}},
    }
    return graph_document(agents, tasks, nodes), worker_doc, reviewer_doc, work, check, agents


def with_nodes(graph: dict, nodes: dict) -> dict:
    body = deepcopy(graph)
    body["nodes"] = nodes
    return body


# -- resolve_task_definition ------------------------------------------------


def test_resolve_task_pins_content_and_contract():
    worker_ref = R.digest(agent_document())
    body = task_document(worker_ref)
    first = resolve_task_definition(body, task_ref=R.digest(body))
    second = resolve_task_definition(deepcopy(body), task_ref=R.digest(body))

    assert first.task_ref == first.definition_digest == R.digest(body)
    assert second.task_ref == first.task_ref
    assert first.goal == "Do the work."
    assert first.agent_ref == worker_ref
    assert first.contract == contract()
    assert first.completion_kind == "candidate"
    assert first.expansion is None


def test_resolve_task_rejects_tampered_unversioned_and_unknown_fields():
    worker_ref = R.digest(agent_document())
    body = task_document(worker_ref)
    reference = R.digest(body)

    tampered = deepcopy(body)
    tampered["goal"] = "Something else"
    assert code_of(lambda: resolve_task_definition(
        tampered, task_ref=reference)) == "artifact_mismatch"
    assert code_of(lambda: resolve_task_definition(
        task_document(worker_ref, goal="other"),
        task_ref=reference)) == "artifact_mismatch"

    # A label, an unversioned name or a malformed digest never substitutes.
    assert code_of(lambda: resolve_task_definition(
        body, task_ref="task:work")) == "invalid_argument"
    assert code_of(lambda: resolve_task_definition(
        body, task_ref=reference.upper())) == "invalid_argument"
    assert code_of(lambda: resolve_task_definition(
        body, task_ref="sha256:" + "a" * 63)) == "invalid_argument"

    # agent_ref must already be a canonical content ref inside a definition.
    assert code_of(lambda: resolve_task_definition(
        task_document("worker"), task_ref=R.digest(task_document("worker")))) == \
        "invalid_argument"

    assert code_of(lambda: resolve_task_definition(
        task_document(worker_ref, session_id="chat-1"),
        task_ref=reference)) == "invalid_argument"
    assert code_of(lambda: resolve_task_definition(
        task_document(worker_ref, format="agentgenome-task-definition/2"),
        task_ref=reference)) == "invalid_argument"
    missing = task_document(worker_ref)
    del missing["contract"]
    assert code_of(lambda: resolve_task_definition(
        missing, task_ref=R.digest(missing))) == "invalid_argument"


def test_task_contract_uses_the_existing_schema_subset():
    worker_ref = R.digest(agent_document())

    def resolve_with(**contract_overrides) -> str:
        body = task_document(worker_ref, contract=contract(**contract_overrides))
        return code_of(lambda: resolve_task_definition(
            body, task_ref=R.digest(body)))

    assert resolve_with(input_schema={"$ref": "#/definitions/x"}) == \
        "capability_unsupported"
    assert resolve_with(output_schema={"type": "tuple"}) == "invalid_argument"
    assert resolve_with(input_schema={"type": "array", "items": []}) == \
        "invalid_argument"
    assert resolve_with(acceptance=3) == "invalid_argument"
    assert resolve_with(acceptance="") == "invalid_argument"
    assert resolve_with(timeout=30) == "invalid_argument"

    body = task_document(worker_ref, contract={"input_schema": {}})
    assert code_of(lambda: resolve_task_definition(
        body, task_ref=R.digest(body))) == "invalid_argument"


def test_completion_protocol_is_explicit():
    worker_ref = R.digest(agent_document())
    receipt = task_document(worker_ref, completion_kind="review_receipt")
    assert resolve_task_definition(
        receipt, task_ref=R.digest(receipt)).completion_kind == "review_receipt"

    for kind in ("receipt", "candidate_or_receipt", 1):
        body = task_document(worker_ref, completion_kind=kind)
        assert code_of(lambda body=body: resolve_task_definition(
            body, task_ref=R.digest(body))) == "capability_unsupported", kind


def test_expansion_structure_is_validated_at_resolve_time():
    worker_ref = R.digest(agent_document())
    work_ref = R.digest(task_document(worker_ref, goal="Child work."))
    reviewer_ref = R.digest(task_document(
        worker_ref, goal="Review.", completion_kind="review_receipt"))

    def resolve_with(expansion: dict) -> str:
        body = task_document(worker_ref, expansion=expansion)
        return code_of(lambda: resolve_task_definition(
            body, task_ref=R.digest(body)))

    def resolve_ok(expansion: dict):
        body = task_document(worker_ref, expansion=expansion)
        return resolve_task_definition(body, task_ref=R.digest(body))

    assert resolve_ok({"children": 2}).expansion == {"children": 2}
    assert resolve_ok({"children": 0, "join_rule": "all"}).expansion == {
        "join_rule": "all", "children": 0}
    assert resolve_ok({"children": ["one", "two"]}).expansion["children"] == [
        {"local_child_id": "one"}, {"local_child_id": "two"}]
    assert resolve_with({"children": [], "join_rule": "all"}) == \
        "invalid_argument"
    assert resolve_with({"children": 2, "join_rule": "any"}) == \
        "invalid_argument"
    assert resolve_with({"children": 1, "extra": True}) == "invalid_argument"
    assert resolve_with({"children": [{"local_child_id": "_bad"}]}) == \
        "invalid_argument"
    assert resolve_with({"children": [{"local_child_id": "one", "x": 1}]}) == \
        "invalid_argument"
    assert resolve_with({"children": [
        {"local_child_id": "one"}, {"local_child_id": "one"}]}) == \
        "invalid_argument"
    assert resolve_with({"children": [
        {"local_child_id": "one", "dependencies": ["ghost"]}]}) == \
        "invalid_argument"
    assert resolve_with({"children": [
        {"local_child_id": "one", "dependencies": ["two"]},
        {"local_child_id": "two", "dependencies": ["one"]}]}) == \
        "dependency_cycle"
    assert resolve_with({"children": [
        {"local_child_id": "one", "contract_ref": work_ref}]}) == \
        "invalid_argument"
    assert resolve_with({"children": [
        {"local_child_id": "one", "template_ref": work_ref,
         "contract_ref": reviewer_ref}]}) == "invalid_argument"
    assert resolve_with({"children": [
        {"local_child_id": "one", "reviewer_ref": "checker"}]}) == \
        "invalid_argument"

    explicit = {"children": [{"local_child_id": "one",
                              "template_ref": work_ref,
                              "contract_ref": work_ref,
                              "reviewer_ref": reviewer_ref,
                              "dependencies": []}]}
    body = task_document(worker_ref, expansion=explicit)
    assert resolve_task_definition(
        body, task_ref=R.digest(body)).expansion["children"][0]["template_ref"] \
        == work_ref
    parent = task_document(
        worker_ref, expansion={"children": [
            {"local_child_id": "one", "reviewer_ref": "parent"}]})
    assert resolve_task_definition(
        parent, task_ref=R.digest(parent)).expansion["children"][0][
            "reviewer_ref"] == "parent"


# -- compile_definition_bundle: catalogs and identity -----------------------


def test_compile_resolves_catalog_aliases_without_mutating_sources():
    graph, worker_doc, reviewer_doc, work, check, agents = standard()
    before = deepcopy(graph)
    bundle = compile_definition_bundle(graph, read_document=Library())

    expected_work_doc = task_document(R.digest(worker_doc), goal="Produce rows.")
    expected_check_doc = task_document(
        R.digest(reviewer_doc), goal="Check rows.",
        completion_kind="review_receipt")
    work_ref = R.digest(expected_work_doc)
    check_ref = R.digest(expected_check_doc)

    assert graph == before
    assert bundle.document["agents"] == {
        "worker": R.digest(worker_doc), "reviewer": R.digest(reviewer_doc)}
    assert bundle.document["tasks"] == {"work": work_ref, "check": check_ref}
    assert bundle.document["nodes"]["phase_a"] == {
        "task_ref": work_ref, "reviewer_ref": check_ref, "needs": [],
        "inputs": {"dataset": {"run_input": "dataset"}}, "delegation": {}}
    assert bundle.document["nodes"]["phase_b"]["needs"] == ["phase_a"]
    assert bundle.document["nodes"]["phase_b"]["inputs"] == {
        "dataset": {"from": "phase_a"}}
    assert set(bundle.agents) == {R.digest(worker_doc),
                                  R.digest(reviewer_doc)}
    assert set(bundle.tasks) == {work_ref, check_ref}
    assert set(bundle.artifact_documents) == set(bundle.agents) | set(bundle.tasks)

    # Inline sources and catalog entries are copied, never shared or edited.
    assert agents["worker"] == worker_doc
    assert work == task_document("worker", goal="Produce rows.")
    bundle.document["nodes"]["phase_a"]["needs"].append("phase_b")
    bundle.artifact_documents[work_ref]["goal"] = "mutated"
    assert work["goal"] == "Produce rows."
    assert bundle.tasks[work_ref].goal == "Produce rows."


def test_alias_and_content_ref_resolve_to_the_same_identity():
    graph, worker_doc, reviewer_doc, work, check, agents = standard()
    first = compile_definition_bundle(graph, read_document=Library())
    second = compile_definition_bundle(deepcopy(graph), read_document=Library())
    assert first.document == second.document

    expected_work_doc = task_document(R.digest(worker_doc), goal="Produce rows.")
    expected_check_doc = task_document(
        R.digest(reviewer_doc), goal="Check rows.",
        completion_kind="review_receipt")
    work_ref = R.digest(expected_work_doc)
    check_ref = R.digest(expected_check_doc)
    library = Library(expected_work_doc, expected_check_doc)
    pinned = compile_definition_bundle(
        graph_document(agents,
                       {"work": work_ref, "check": check_ref},
                       graph["nodes"]),
        read_document=library)

    assert pinned.document == first.document
    assert set(library.calls) == {work_ref, check_ref}
    assert pinned.tasks[work_ref].task_ref == first.tasks[work_ref].task_ref
    assert pinned.tasks[work_ref].agent_ref == R.digest(worker_doc)
    assert pinned.tasks[work_ref].completion_kind == "candidate"


def test_external_documents_are_never_alias_rewritten():
    worker_doc = agent_document()
    work_doc = task_document(R.digest(worker_doc))
    work_ref = R.digest(work_doc)
    reviewer_doc = agent_document("Review.")
    check_doc = task_document(R.digest(reviewer_doc), goal="Check.",
                              completion_kind="review_receipt")
    check_ref = R.digest(check_doc)
    before = deepcopy(work_doc)
    graph = graph_document(
        {"worker": worker_doc, "reviewer": reviewer_doc},
        {"work": work_ref, "check": check_ref},
        {"phase": {"task_ref": work_ref, "reviewer_ref": check_ref,
                   "needs": []}})

    library = Library(work_doc, check_doc)
    bundle = compile_definition_bundle(graph, read_document=library)
    assert bundle.document["tasks"] == {"work": work_ref, "check": check_ref}
    assert library.documents[work_ref] == before
    assert bundle.artifact_documents[work_ref] == before

    # A reader that returns different content under a pinned ref must fail,
    # not silently re-pin (or alias-rewrite) the document.
    swapped = Library(check_doc)
    swapped.documents[work_ref] = task_document(
        R.digest(worker_doc), goal="Swapped")
    assert code_of(lambda: compile_definition_bundle(
        graph, read_document=swapped)) == "artifact_mismatch"

    aliased = Library(check_doc)
    aliased.documents[work_ref] = task_document("worker")
    assert code_of(lambda: compile_definition_bundle(
        graph, read_document=aliased)) == "invalid_argument"


def test_reader_is_only_used_for_content_refs():
    graph, *_ = standard()
    calls: list[str] = []

    def reader(reference: str) -> dict:
        calls.append(reference)
        raise AssertionError("an inline graph must not read a document")

    bundle = compile_definition_bundle(graph, read_document=reader)
    assert calls == []
    assert set(bundle.tasks) == set(bundle.document["tasks"].values())


def test_unknown_alias_missing_ref_and_wrong_types_fail():
    worker_doc = agent_document()
    work = task_document(R.digest(worker_doc))
    node = {"phase": {"task_ref": "work", "reviewer_ref": "check", "needs": []}}

    assert code_of(lambda: compile_definition_bundle(
        graph_document({"worker": worker_doc},
                       {"work": task_document("ghost")}, node),
        read_document=Library())) == "not_found"
    assert code_of(lambda: compile_definition_bundle(
        graph_document({"worker": worker_doc},
                       {"work": work, "check": work},
                       {"phase": {"task_ref": "ghost",
                                  "reviewer_ref": "check", "needs": []}}),
        read_document=Library())) == "not_found"
    assert code_of(lambda: compile_definition_bundle(
        graph_document({"worker": 5}, {"work": work, "check": work}, node),
        read_document=Library())) == "invalid_argument"
    assert code_of(lambda: compile_definition_bundle(
        graph_document({"worker": worker_doc}, {"work": [work]}, node),
        read_document=Library())) == "invalid_argument"
    assert code_of(lambda: compile_definition_bundle(
        graph_document({"worker": R.digest(worker_doc)},
                       {"work": work}, node),
        read_document=Library())) == "not_found"
    assert code_of(lambda: compile_definition_bundle(
        graph_document({}, {"work": work}, node),
        read_document=Library())) == "invalid_argument"
    assert code_of(lambda: compile_definition_bundle(
        graph_document({"worker": worker_doc}, {}, node),
        read_document=Library())) == "invalid_argument"
    assert code_of(lambda: compile_definition_bundle(
        graph_document({"worker": worker_doc}, {"work": work}, {}),
        read_document=Library())) == "invalid_argument"


def test_catalog_definition_cycle_is_rejected():
    worker_doc = agent_document()
    worker_ref = R.digest(worker_doc)
    first = task_document(worker_ref, goal="First.", expansion={
        "children": [{"local_child_id": "next", "template_ref": "second"}]})
    second = task_document(worker_ref, goal="Second.", expansion={
        "children": [{"local_child_id": "next", "template_ref": "first"}]})
    review = task_document(worker_ref, goal="Review.",
                           completion_kind="review_receipt")
    graph = graph_document(
        {"worker": worker_doc},
        {"first": first, "second": second, "review": review},
        {"phase": {"task_ref": "first", "reviewer_ref": "review",
                   "needs": []}})
    assert code_of(lambda: compile_definition_bundle(
        graph, read_document=Library())) == "dependency_cycle"
    assert code_of(lambda: compile_definition_bundle(
        graph_document({"worker": worker_doc},
                       {"a": task_document(worker_ref, expansion={
                           "children": [{"local_child_id": "x",
                                         "template_ref": "a"}]}),
                        "check": review},
                       {"phase": {"task_ref": "a", "reviewer_ref": "check",
                                  "needs": []}}),
        read_document=Library())) == "dependency_cycle"


# -- compile_definition_bundle: graph structure -----------------------------


def test_graph_dependency_edges_are_validated():
    graph, *_ = standard()

    repeated = deepcopy(graph)
    repeated["nodes"]["phase_b"]["needs"] = ["phase_a", "phase_a"]
    assert code_of(lambda: compile_definition_bundle(
        repeated, read_document=Library())) == "invalid_argument"

    unknown = deepcopy(graph)
    unknown["nodes"]["phase_b"]["needs"] = ["ghost"]
    assert code_of(lambda: compile_definition_bundle(
        unknown, read_document=Library())) == "not_found"

    self_need = deepcopy(graph)
    self_need["nodes"]["phase_a"]["needs"] = ["phase_a"]
    assert code_of(lambda: compile_definition_bundle(
        self_need, read_document=Library())) == "dependency_cycle"

    cycle = deepcopy(graph)
    cycle["nodes"]["phase_a"]["needs"] = ["phase_b"]
    assert code_of(lambda: compile_definition_bundle(
        cycle, read_document=Library())) == "dependency_cycle"

    bad_needs = deepcopy(graph)
    bad_needs["nodes"]["phase_a"]["needs"] = "phase_b"
    assert code_of(lambda: compile_definition_bundle(
        bad_needs, read_document=Library())) == "invalid_argument"

    missing = deepcopy(graph)
    del missing["nodes"]["phase_a"]["reviewer_ref"]
    assert code_of(lambda: compile_definition_bundle(
        missing, read_document=Library())) == "invalid_argument"

    extra = deepcopy(graph)
    extra["nodes"]["phase_a"]["entry"] = True
    assert code_of(lambda: compile_definition_bundle(
        extra, read_document=Library())) == "invalid_argument"


def test_from_bindings_must_reference_declared_dependencies():
    graph, *_ = standard()

    no_need = deepcopy(graph)
    no_need["nodes"]["phase_b"]["needs"] = []
    assert code_of(lambda: compile_definition_bundle(
        no_need, read_document=Library())) == "unauthorized"

    ghost = deepcopy(graph)
    ghost["nodes"]["phase_b"]["inputs"] = {"dataset": {"from": "ghost"}}
    assert code_of(lambda: compile_definition_bundle(
        ghost, read_document=Library())) == "unauthorized"

    unsupported = deepcopy(graph)
    unsupported["nodes"]["phase_b"]["inputs"] = {"dataset": "literal"}
    assert code_of(lambda: compile_definition_bundle(
        unsupported, read_document=Library())) == "invalid_argument"

    malformed = deepcopy(graph)
    malformed["nodes"]["phase_b"]["inputs"] = {
        "dataset": {"id": "sha256:" + "a" * 64, "sha256": "a" * 64,
                    "size": 1}}
    assert code_of(lambda: compile_definition_bundle(
        malformed, read_document=Library())) == "invalid_argument"

    listed = deepcopy(graph)
    listed["nodes"]["phase_b"]["inputs"] = {"parts": [
        {"from": "phase_a"},
        {"from": "phase_a", "field": "rows"},
        {"run_input": "dataset"},
        artifact("data.csv"),
    ]}
    bundle = compile_definition_bundle(listed, read_document=Library())
    assert bundle.document["nodes"]["phase_b"]["inputs"]["parts"] == [
        {"from": "phase_a"},
        {"from": "phase_a", "field": "rows"},
        {"run_input": "dataset"},
        artifact("data.csv"),
    ]


def test_stage_and_reviewer_completion_modes_are_enforced():
    graph, *_ = standard()

    receipt_stage = deepcopy(graph)
    receipt_stage["nodes"]["phase_a"]["task_ref"] = "check"
    assert code_of(lambda: compile_definition_bundle(
        receipt_stage, read_document=Library())) == "invalid_argument"

    candidate_reviewer = deepcopy(graph)
    candidate_reviewer["nodes"]["phase_a"]["reviewer_ref"] = "work"
    assert code_of(lambda: compile_definition_bundle(
        candidate_reviewer, read_document=Library())) == "invalid_argument"

    parent_reviewer = deepcopy(graph)
    parent_reviewer["nodes"]["phase_a"]["reviewer_ref"] = "parent"
    assert code_of(lambda: compile_definition_bundle(
        parent_reviewer, read_document=Library())) == "invalid_argument"


def test_graph_policy_budget_and_top_level_fields_are_validated():
    graph, *_ = standard()

    for overrides, expected in (
        ({"policy": "elastic"}, "capability_unsupported"),
        ({"budget_limits": {"max_tasks": 1}}, "invalid_argument"),
        ({"budget_limits": {"max_nodes": -1}}, "invalid_argument"),
        ({"budget_limits": {"max_nodes": True}}, "invalid_argument"),
        ({"definition_id": "bad id"}, "invalid_argument"),
        ({"base_revision": -1}, "invalid_argument"),
        ({"description": 5}, "invalid_argument"),
        ({"input_schema": {"$ref": "#/x"}}, "capability_unsupported"),
        ({"unknown": 1}, "invalid_argument"),
    ):
        assert code_of(lambda overrides=overrides: compile_definition_bundle(
            graph_document(**{**_graph_parts(graph), **overrides}),
            read_document=Library())) == expected, overrides

    assert code_of(lambda: compile_definition_bundle(
        graph_document(**_graph_parts(graph)), read_document=None)) == \
        "invalid_argument"


def _graph_parts(graph: dict) -> dict:
    return {key: deepcopy(value) for key, value in graph.items()}


def test_graph_one_is_not_executed():
    graph, *_ = standard()
    old = deepcopy(graph)
    old["format"] = "agentgenome-graph-definition/1"
    assert code_of(lambda: compile_definition_bundle(
        old, read_document=Library())) == "invalid_argument"

    legacy = {
        "format": "agentgenome-graph-definition/1",
        "definition_id": "legacy",
        "entry_node": "root",
        "root_reviewer": "checker",
        "reviewers": {"checker": {"kind": "checker"}},
        "nodes": {"root": {"contract_ref": "clean", "reviewer": "parent",
                           "needs": []}},
        "policy": "fixed",
        "budget_limits": {"max_nodes": 2, "max_depth": 2, "max_rounds": 2},
    }
    assert code_of(lambda: compile_definition_bundle(
        legacy, read_document=Library())) == "invalid_argument"


# -- fixed expansion reference slots ----------------------------------------


def test_fixed_expansion_three_reference_slots_resolve_into_the_closure():
    worker_doc = agent_document()
    reviewer_doc = agent_document("Review.")
    work = task_document("worker", goal="Child work.")
    check = task_document("reviewer", goal="Child check.",
                          completion_kind="review_receipt")
    split = task_document("worker", goal="Split.", expansion={
        "join_rule": "all",
        "children": [
            {"local_child_id": "one", "template_ref": "work",
             "reviewer_ref": "check", "contract_ref": "work"},
            {"local_child_id": "two", "reviewer_ref": "parent"},
            {"local_child_id": "three", "template_ref": "work"},
            {"local_child_id": "four", "dependencies": ["one"]},
        ],
    })
    graph = graph_document(
        {"worker": worker_doc, "reviewer": reviewer_doc},
        {"work": work, "check": check, "split": split},
        {"phase": {"task_ref": "split", "reviewer_ref": "check",
                   "needs": []}})
    bundle = compile_definition_bundle(graph, read_document=Library())

    work_ref = R.digest(task_document(R.digest(worker_doc),
                                      goal="Child work."))
    check_ref = R.digest(task_document(R.digest(reviewer_doc),
                                       goal="Child check.",
                                       completion_kind="review_receipt"))
    split_def = next(item for item in bundle.tasks.values()
                     if item.goal == "Split.")
    children = split_def.expansion["children"]
    assert children[0]["template_ref"] == children[0]["contract_ref"] == work_ref
    assert children[0]["reviewer_ref"] == check_ref
    assert children[1]["reviewer_ref"] == "parent"
    assert "template_ref" not in children[1]
    assert "contract_ref" not in children[1]
    assert children[2]["template_ref"] == work_ref
    assert "contract_ref" not in children[2]
    assert children[3]["dependencies"] == ["one"]
    assert {work_ref, check_ref} <= set(bundle.tasks)
    assert bundle.document["tasks"]["split"] == split_def.task_ref
    assert split_def.task_ref not in R.canonical(split_def.expansion)


def test_expansion_contract_ref_never_selects_or_switches_the_task():
    worker_doc = agent_document()
    reviewer_doc = agent_document("Review.")
    other = task_document("worker", goal="Other work.")
    check = task_document("reviewer", goal="Child check.",
                          completion_kind="review_receipt")
    split = task_document("worker", goal="Split.", expansion={
        "join_rule": "all",
        "children": [{"local_child_id": "one", "contract_ref": "other"}],
    })
    graph = graph_document(
        {"worker": worker_doc, "reviewer": reviewer_doc},
        {"work": task_document("worker", goal="Child work."),
         "other": other, "check": check, "split": split},
        {"phase": {"task_ref": "split", "reviewer_ref": "check",
                   "needs": []}})

    with pytest.raises(ControlPlaneError) as failure:
        compile_definition_bundle(graph, read_document=Library())
    assert failure.value.code == "invalid_argument"
    assert "template_ref" in failure.value.message


def test_expansion_explicit_template_ref_selects_the_named_task():
    worker_doc = agent_document()
    reviewer_doc = agent_document("Review.")
    check = task_document("reviewer", goal="Child check.",
                          completion_kind="review_receipt")
    split = task_document("worker", goal="Split.", expansion={
        "join_rule": "all",
        "children": [{"local_child_id": "one", "template_ref": "other",
                      "contract_ref": "other"}],
    })
    graph = graph_document(
        {"worker": worker_doc, "reviewer": reviewer_doc},
        {"work": task_document("worker", goal="Child work."),
         "other": task_document("worker", goal="Other work."),
         "check": check, "split": split},
        {"phase": {"task_ref": "split", "reviewer_ref": "check",
                   "needs": []}})
    bundle = compile_definition_bundle(graph, read_document=Library())

    worker_ref = R.digest(worker_doc)
    work_ref = R.digest(task_document(worker_ref, goal="Child work."))
    other_ref = R.digest(task_document(worker_ref, goal="Other work."))
    split_def = next(item for item in bundle.tasks.values()
                     if item.goal == "Split.")
    child = split_def.expansion["children"][0]
    assert child["template_ref"] == child["contract_ref"] == other_ref
    assert child["template_ref"] != work_ref
    assert {work_ref, other_ref} <= set(bundle.tasks)


def test_fixed_policy_reads_template_ref_as_the_only_selector():
    worker_ref = R.digest(agent_document())
    other_ref = R.digest(task_document(worker_ref, goal="Other work."))
    split_doc = task_document(worker_ref, goal="Split.", expansion={
        "join_rule": "all",
        "children": [
            {"local_child_id": "one", "template_ref": other_ref},
            {"local_child_id": "two"},
        ],
    })
    split_ref = R.digest(split_doc)
    definition = {"task_definitions": {
        split_ref: resolve_task_definition(split_doc, task_ref=split_ref)}}
    parent = {"node_id": "n1", "template_ref": split_ref}
    policy = FixedPolicy()

    policy.validate_expansion(definition, parent, {
        "join_rule": "all",
        "children": [
            {"local_child_id": "one", "template_ref": other_ref,
             "contract_ref": other_ref},
            {"local_child_id": "two"},
        ],
    })

    with pytest.raises(ControlPlaneError) as switched:
        policy.validate_expansion(definition, parent, {
            "join_rule": "all",
            "children": [
                {"local_child_id": "one", "template_ref": other_ref,
                 "contract_ref": other_ref},
                {"local_child_id": "two", "contract_ref": other_ref},
            ],
        })
    assert switched.value.code == "unauthorized"


def test_external_task_expansion_pulls_the_closure():
    worker_doc = agent_document()
    reviewer_doc = agent_document("Review.")
    work = task_document(R.digest(worker_doc), goal="Child work.")
    check = task_document(R.digest(reviewer_doc), goal="Child check.",
                          completion_kind="review_receipt")
    split = task_document(R.digest(worker_doc), goal="Split.", expansion={
        "children": [
            {"local_child_id": "one", "template_ref": R.digest(work),
             "reviewer_ref": R.digest(check)},
        ]})
    library = Library(worker_doc, reviewer_doc, work, check, split)
    graph = graph_document(
        {"worker": R.digest(worker_doc)},
        {"split": R.digest(split)},
        {"phase": {"task_ref": R.digest(split),
                   "reviewer_ref": R.digest(check), "needs": []}})
    bundle = compile_definition_bundle(graph, read_document=library)
    assert set(bundle.tasks) == {R.digest(work), R.digest(check),
                                 R.digest(split)}
    assert set(bundle.agents) == {R.digest(worker_doc),
                                  R.digest(reviewer_doc)}
    assert set(library.calls) == set(library.documents)

    wrong_reviewer = task_document(R.digest(worker_doc), goal="Bad split.",
                                   expansion={"children": [
                                       {"local_child_id": "one",
                                        "reviewer_ref": R.digest(work)}]})
    graph = graph_document(
        {"worker": R.digest(worker_doc)},
        {"split": R.digest(wrong_reviewer)},
        {"phase": {"task_ref": R.digest(wrong_reviewer),
                   "reviewer_ref": R.digest(check), "needs": []}})
    library = Library(worker_doc, work, check, wrong_reviewer)
    assert code_of(lambda: compile_definition_bundle(
        graph, read_document=library)) == "invalid_argument"


# -- initial_graph_tasks ----------------------------------------------------


def test_initial_tasks_are_one_atomic_pending_batch():
    graph, worker_doc, reviewer_doc, work, check, agents = standard()
    custom = with_nodes(graph, {
        "phase_a": {"task_ref": "work", "reviewer_ref": "check", "needs": [],
                    "inputs": {"dataset": {"run_input": "dataset"}}},
        "phase_b": {"task_ref": "work", "reviewer_ref": "check",
                    "needs": ["phase_a"],
                    "inputs": {"dataset": {"from": "phase_a"}}},
        "phase_c": {"task_ref": "work", "reviewer_ref": "check",
                    "needs": ["phase_a"], "inputs": {
                        "raw": artifact("data.csv"),
                        "parts": [{"from": "phase_a"},
                                  {"run_input": "dataset"}]}},
    })
    bundle = compile_definition_bundle(custom, read_document=Library())
    before = deepcopy(bundle.document)
    batch = initial_graph_tasks(bundle, run_id="run-1")

    assert [item["fields"]["local_child_id"] for item in batch] == [
        "phase_a", "phase_b", "phase_c"]
    ids = {item["fields"]["local_child_id"]: item["fields"]["node_id"]
           for item in batch}
    assert len(set(ids.values())) == len(ids)
    for item in batch:
        assert item["kind"] == "node" and item["op"] == "create"
        fields = item["fields"]
        assert fields["node_id"] == ids[fields["local_child_id"]]
        assert len(fields["node_id"]) <= 128
        assert fields["run_id"] == "run-1"
        assert fields["state"] == "pending"
        assert fields["parent_id"] is None
        assert fields["expansion_id"] is None
        assert fields["template_ref"] == fields["contract_ref"]
        assert fields["reviewer_ref"] == \
            bundle.document["nodes"][fields["local_child_id"]]["reviewer_ref"]
    assert batch[1]["fields"]["dependencies"] == [ids["phase_a"]]
    assert batch[2]["fields"]["dependencies"] == [ids["phase_a"]]
    assert batch[0]["fields"]["input_bindings"] == {
        "dataset": {"run_input": "dataset"}}
    assert batch[1]["fields"]["input_bindings"] == {
        "dataset": {"from": ids["phase_a"]}}
    assert batch[2]["fields"]["input_bindings"] == {
        "raw": artifact("data.csv"),
        "parts": [{"from": ids["phase_a"]},
                  {"run_input": "dataset"}],
    }
    R.validate_mutations(batch)
    assert bundle.document == before


def test_initial_tasks_are_deterministic_and_run_scoped():
    graph, worker_doc, reviewer_doc, work, check, agents = standard()
    bundle = compile_definition_bundle(graph, read_document=Library())
    first = initial_graph_tasks(bundle, run_id="run-1")
    again = initial_graph_tasks(bundle, run_id="run-1")
    other = initial_graph_tasks(bundle, run_id="run-2")

    assert first == again
    assert [item["fields"]["template_ref"] for item in first] == \
        [item["fields"]["template_ref"] for item in other]
    assert {item["fields"]["node_id"] for item in first}.isdisjoint(
        {item["fields"]["node_id"] for item in other})
    assert all(item["fields"]["run_id"] == "run-2" for item in other)

    expected_work = task_document(R.digest(worker_doc), goal="Produce rows.")
    expected_check = task_document(
        R.digest(reviewer_doc), goal="Check rows.",
        completion_kind="review_receipt")
    reuse = compile_definition_bundle(
        graph_document(agents,
                       {"work": R.digest(expected_work),
                        "check": R.digest(expected_check)},
                       {"later": {"task_ref": "work",
                                  "reviewer_ref": "check", "needs": []}}),
        read_document=Library(expected_work, expected_check))
    later = initial_graph_tasks(reuse, run_id="run-3")
    assert later[0]["fields"]["template_ref"] == \
        first[0]["fields"]["template_ref"]
    assert later[0]["fields"]["local_child_id"] == "later"
    assert later[0]["fields"]["node_id"] != first[0]["fields"]["node_id"]


def test_long_stage_names_get_one_bounded_id_mapping():
    graph, *_ = standard()
    long_name = "a" + "b" * 127
    assert len(long_name) == 128
    bundle = compile_definition_bundle(with_nodes(graph, {
        long_name: {"task_ref": "work", "reviewer_ref": "check", "needs": [],
                    "inputs": {"dataset": {"run_input": "dataset"}}},
        "finish": {"task_ref": "work", "reviewer_ref": "check",
                   "needs": [long_name],
                   "inputs": {"dataset": {"from": long_name}}},
    }), read_document=Library())
    run_id = str(uuid.uuid4())

    batch = initial_graph_tasks(bundle, run_id=run_id)

    fields = {item["fields"]["local_child_id"]: item["fields"]
              for item in batch}
    assert set(fields) == {long_name, "finish"}
    ids = {name: item["node_id"] for name, item in fields.items()}
    assert len(set(ids.values())) == 2
    assert all(len(node_id) <= 128 for node_id in ids.values())
    assert fields[long_name]["dependencies"] == []
    assert fields["finish"]["dependencies"] == [ids[long_name]]
    assert fields["finish"]["input_bindings"] == {
        "dataset": {"from": ids[long_name]}}
    R.validate_mutations(batch)


def test_long_stage_ids_are_stable_per_run_and_disjoint_across_runs():
    graph, *_ = standard()
    long_name = "phase-" + "x" * 122
    assert len(long_name) == 128
    bundle = compile_definition_bundle(with_nodes(graph, {
        long_name: {"task_ref": "work", "reviewer_ref": "check", "needs": []},
        "tail": {"task_ref": "work", "reviewer_ref": "check",
                 "needs": [long_name],
                 "inputs": {"dataset": {"from": long_name}}},
    }), read_document=Library())
    run_a, run_b = str(uuid.uuid4()), str(uuid.uuid4())

    first = initial_graph_tasks(bundle, run_id=run_a)
    again = initial_graph_tasks(bundle, run_id=run_a)
    other = initial_graph_tasks(bundle, run_id=run_b)

    assert first == again
    ids_a = {item["fields"]["local_child_id"]: item["fields"]["node_id"]
             for item in first}
    ids_b = {item["fields"]["local_child_id"]: item["fields"]["node_id"]
             for item in other}
    assert set(ids_a) == set(ids_b) == {long_name, "tail"}
    assert all(ids_a[name] != ids_b[name] for name in ids_a)
    assert first[1]["fields"]["dependencies"] == [ids_a[long_name]]
    assert other[1]["fields"]["dependencies"] == [ids_b[long_name]]
    assert first[1]["fields"]["input_bindings"] == {
        "dataset": {"from": ids_a[long_name]}}
    assert other[1]["fields"]["input_bindings"] == {
        "dataset": {"from": ids_b[long_name]}}


def test_initial_rejects_bad_bundles():
    graph, *_ = standard()
    bundle = compile_definition_bundle(graph, read_document=Library())

    assert code_of(lambda: initial_graph_tasks({}, run_id="run-1")) == \
        "invalid_argument"
    assert code_of(lambda: initial_graph_tasks(bundle, run_id="")) == \
        "invalid_argument"

    document = deepcopy(bundle.document)
    document["nodes"]["phase_a"]["needs"] = ["ghost"]
    broken = DefinitionBundle(document=document, agents=bundle.agents,
                              tasks=bundle.tasks,
                              artifact_documents=bundle.artifact_documents)
    assert code_of(lambda: initial_graph_tasks(
        broken, run_id="run-1")) == "invalid_argument"

    document = deepcopy(bundle.document)
    document["nodes"]["phase_a"] = ["not-a-node"]
    broken = DefinitionBundle(document=document, agents=bundle.agents,
                              tasks=bundle.tasks,
                              artifact_documents=bundle.artifact_documents)
    assert code_of(lambda: initial_graph_tasks(
        broken, run_id="run-1")) == "invalid_argument"
