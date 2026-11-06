"""Versioned task definitions and Graph /2 compilation scaffold.

Codex architecture framework. Qoder fills W02b1; host cutover is W02b2.
One recursive task model serves stages and internal steps. Definitions are
reusable data, Graph nodes are positions/declarations, existing Node rows are
run instances. This module creates no service, process, database or Agent loop.
"""
from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable, Literal, Mapping

from . import resources as R
from .agent_definitions import AgentDefinition, resolve_agent_definition

TASK_DEFINITION_FORMAT = "agentgenome-task-definition/1"
GRAPH_DEFINITION_FORMAT = "agentgenome-graph-definition/2"

_TASK_FIELDS = frozenset({
    "format", "goal", "agent_ref", "contract", "completion_kind", "expansion",
    "progress",
})
_PROGRESS_FIELDS = frozenset({"no_improvement_limit"})
_CONTRACT_FIELDS = frozenset({"input_schema", "output_schema", "acceptance"})
_COMPLETION_KINDS = frozenset({"candidate", "review_receipt"})
_EXPANSION_FIELDS = frozenset({"children", "join_rule"})
_EXPANSION_CHILD_FIELDS = frozenset({
    "local_child_id", "template_ref", "reviewer_ref", "contract_ref",
    "dependencies", "input_bindings", "delegation",
})
_GRAPH_FIELDS = frozenset({
    "format", "definition_id", "base_revision", "description", "policy",
    "budget_limits", "input_schema", "agents", "tasks", "nodes",
})
_GRAPH_NODE_FIELDS = frozenset({"task_ref", "reviewer_ref", "needs", "inputs",
                                "delegation"})
_DELEGATION_FIELDS = frozenset({"context", "tool_profile", "budget",
                                "runtime_options"})
_INPUT_BINDING_KEYS = frozenset({"id", "sha256", "size", "media_type"})
_BUDGET_LIMITS = {
    "max_nodes": 1,
    "max_depth": 0,
    "max_rounds": 1,
    "run_deadline_seconds": 1,
    "max_inflight_nodes": 1,
    "max_provider_calls": 1,
    "max_tool_calls": 1,
    "max_continuations": 1,
}
_STAGE_ID_DOMAIN = "agentgenome-control-plane/graph-stage/1"


@dataclass(frozen=True)
class TaskDefinition:
    """A reusable SOP step/phase, with an explicit I/O and acceptance contract.

    contract contains input_schema, output_schema and acceptance text. The
    generic runtime validates protocol structure; a parent/reviewer task
    evaluates business acceptance. Initial contract_ref is this task_ref;
    authorized local revisions will be separate versioned references (W05).

    review_receipt is the existing finite checker closure made explicit. It
    completes only with a receipt bound to a specific candidate by the API,
    never just because the Agent ended a model response. Parent review of a
    child does not complete the parent's own candidate-mode task.
    """

    task_ref: str
    definition_digest: str
    goal: str
    agent_ref: str
    contract: Mapping[str, Any]
    completion_kind: Literal["candidate", "review_receipt"] = "candidate"
    expansion: Mapping[str, Any] | None = None
    # W04b frozen Goal policy; evaluator reuses the Node reviewer assignment.
    progress: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class DefinitionBundle:
    """Compiler result, not a second authoritative resource store.

    document is normalized Graph /2 data with content-pinned references.
    agents/tasks are indexed by canonical content refs, not instance IDs.
    artifact_documents holds canonical library documents to register in the
    same API transaction as Graph publication. File-backed immutable blobs
    may precede that commit, but unpublished metadata/grants must roll back.
    API persists the dependency-ref sets, so run-scoped readers can access
    the exact definition library without granting arbitrary artifact access.
    """

    document: Mapping[str, Any]
    agents: Mapping[str, AgentDefinition]
    tasks: Mapping[str, TaskDefinition]
    artifact_documents: Mapping[str, Mapping[str, Any]]


def _content_ref(value: Any, label: str) -> str:
    R.require_text(value, label, max_length=71)
    R.require(R.ARTIFACT_ID.fullmatch(value) is not None, "invalid_argument",
              f"{label} must be a content-addressed sha256 ref", value=value)
    return value


def _json_copy(value: Any, label: str) -> dict[str, Any]:
    R.require(isinstance(value, dict), "invalid_argument",
              f"{label} must be an object")
    R.canonical(value)
    return deepcopy(value)


def _delegation(raw: Mapping[str, Any], label: str) -> dict[str, Any]:
    """Only narrowing fields may be declared per task instance.

    Identity, contract_ref and frozen input refs are never delegation inputs:
    they come from the committed Node state. runtime_options are carried so
    the existing composer can refuse them while no Backend consumes them.
    The same nested ``delegation`` object is the only accepted shape in the
    source Graph, the normalized Graph and an expansion proposal.
    """
    value = raw.get("delegation", {})
    R.require(isinstance(value, dict), "invalid_argument",
              f"{label}.delegation must be an object")
    unknown = sorted(set(value) - _DELEGATION_FIELDS)
    R.require(not unknown, "invalid_argument",
              f"{label}.delegation has unknown fields", unknown=unknown)
    for name, item in value.items():
        R.require(isinstance(item, dict), "invalid_argument",
                  f"{label}.delegation.{name} must be an object")
        R.canonical(item)
    return deepcopy(dict(value))


def validate_contract(raw: Any) -> dict[str, Any]:
    body = R.require_object(raw, "contract", allowed=_CONTRACT_FIELDS,
                            required=_CONTRACT_FIELDS)
    R.validate_input_schema(body["input_schema"], "contract.input_schema")
    R.validate_input_schema(body["output_schema"], "contract.output_schema")
    R.require_text(body["acceptance"], "contract.acceptance", max_length=16384)
    return deepcopy(body)


def _validate_expansion(raw: Any) -> dict[str, Any]:
    body = R.require_object(raw, "expansion", allowed=_EXPANSION_FIELDS,
                            required={"children"})
    result: dict[str, Any] = {}
    if "join_rule" in body:
        R.require(body["join_rule"] in R.JOIN_RULES, "invalid_argument",
                  "expansion.join_rule is unsupported",
                  join_rule=str(body["join_rule"])[:64])
        result["join_rule"] = body["join_rule"]
    children = body["children"]
    if isinstance(children, int) and not isinstance(children, bool):
        R.require_int(children, "expansion.children", minimum=0)
        result["children"] = children
        return result
    R.require(isinstance(children, list) and children, "invalid_argument",
              "expansion.children must be a count or a nonempty list")
    seen: set[str] = set()
    rules: list[dict[str, Any]] = []
    for index, raw_child in enumerate(children):
        label = f"expansion.children[{index}]"
        if isinstance(raw_child, str):
            local = R.require_slug(raw_child, label)
            rule: dict[str, Any] = {"local_child_id": local}
        else:
            child = R.require_object(raw_child, label,
                                     allowed=_EXPANSION_CHILD_FIELDS,
                                     required={"local_child_id"})
            local = R.require_slug(child["local_child_id"],
                                   f"{label}.local_child_id")
            rule = deepcopy(dict(child))
            if "template_ref" in child:
                rule["template_ref"] = _content_ref(
                    child["template_ref"], f"{label}.template_ref")
            if "contract_ref" in child:
                rule["contract_ref"] = _content_ref(
                    child["contract_ref"], f"{label}.contract_ref")
                R.require("template_ref" in rule, "invalid_argument",
                          "expansion contract_ref requires template_ref",
                          local_child_id=local)
                R.require(rule["contract_ref"] == rule["template_ref"],
                          "invalid_argument",
                          "expansion contract_ref must equal the child task ref",
                          local_child_id=local, task_ref=rule["template_ref"],
                          contract_ref=rule["contract_ref"])
            if "reviewer_ref" in child:
                reviewer = child["reviewer_ref"]
                if reviewer != "parent":
                    rule["reviewer_ref"] = _content_ref(
                        reviewer, f"{label}.reviewer_ref")
            dependencies = child.get("dependencies", [])
            R.require(isinstance(dependencies, list) and
                      all(isinstance(dep, str) and dep
                          for dep in dependencies),
                      "invalid_argument",
                      f"{label}.dependencies must be a list of names")
            rule["dependencies"] = list(dependencies)
            if "input_bindings" in child:
                R.require(isinstance(child["input_bindings"], dict),
                          "invalid_argument",
                          f"{label}.input_bindings must be an object")
                rule["input_bindings"] = deepcopy(child["input_bindings"])
            delegation = _delegation(child, label)
            rule.pop("delegation", None)
            if delegation:
                rule["delegation"] = delegation
        R.require(local not in seen, "invalid_argument",
                  "expansion local_child_id must be unique",
                  local_child_id=local)
        seen.add(local)
        rules.append(rule)
    for rule in rules:
        unknown = sorted(set(rule.get("dependencies", [])) - seen)
        R.require(not unknown, "invalid_argument",
                  "expansion dependencies must reference rule children",
                  local_child_id=rule["local_child_id"], unknown=unknown)
    R.require_acyclic({rule["local_child_id"]: list(rule.get("dependencies", []))
                       for rule in rules})
    result["children"] = rules
    return result


def resolve_task_definition(
    document: Mapping[str, Any], *, task_ref: str,
) -> TaskDefinition:
    """W02b1: parse a canonical task document and verify its pinned digest.

    agent_ref must already be a content ref. Validate declared I/O schemas,
    completion protocol and expansion structure, not business correctness.
    No API reads, file access or execution here.
    """
    reference = _content_ref(task_ref, "task_ref")
    body = R.require_object(
        document, "task definition",
        allowed=_TASK_FIELDS,
        required={"format", "goal", "agent_ref", "contract"},
    )
    R.require(body["format"] == TASK_DEFINITION_FORMAT, "invalid_argument",
              "the task definition format is not supported",
              format=str(body["format"])[:128])
    goal = R.require_text(body["goal"], "task goal", max_length=16384)
    agent_ref = _content_ref(body["agent_ref"], "agent_ref")
    contract = validate_contract(body["contract"])
    completion_kind = body.get("completion_kind", "candidate")
    R.require(completion_kind in _COMPLETION_KINDS, "capability_unsupported",
              "the completion protocol is unsupported",
              completion_kind=str(completion_kind)[:64])
    expansion = (None if "expansion" not in body
                 else _validate_expansion(body["expansion"]))
    progress = (None if "progress" not in body
                else _validate_progress(body["progress"]))
    definition_digest = R.digest(body)
    R.require(definition_digest == reference, "artifact_mismatch",
              "the task definition content does not match its task_ref",
              expected=reference, actual=definition_digest)
    return TaskDefinition(
        task_ref=reference,
        definition_digest=definition_digest,
        goal=goal,
        agent_ref=agent_ref,
        contract=contract,
        completion_kind=completion_kind,
        expansion=expansion,
        progress=progress,
    )


def _validate_progress(raw: Any) -> dict[str, Any]:
    """W04b frozen Goal policy. Absent stays absent; never synthesize null."""
    body = R.require_object(raw, "task progress", allowed=_PROGRESS_FIELDS,
                            required={"no_improvement_limit"})
    limit = R.require_int(body["no_improvement_limit"],
                          "task progress.no_improvement_limit", minimum=1)
    return {"no_improvement_limit": limit}


def _validate_budget_limits(raw: Any) -> dict[str, Any]:
    body = R.require_object(raw, "budget_limits", allowed=set(_BUDGET_LIMITS))
    for name, minimum in _BUDGET_LIMITS.items():
        if name in body:
            R.require_int(body[name], f"budget_limits.{name}",
                          minimum=minimum)
    return deepcopy(dict(body))


def _input_binding(value: Any, *, needs: list[str], stage: str, label: str,
                   ids: Mapping[str, str] | None = None) -> Any:
    if isinstance(value, list):
        return [_input_binding(item, needs=needs, stage=stage,
                               label=f"{label}[{index}]", ids=ids)
                for index, item in enumerate(value)]
    R.require(isinstance(value, dict), "invalid_argument",
              f"{label} must be an artifact ref, run_input, from binding or "
              "a list of those")
    keys = set(value)
    if keys == _INPUT_BINDING_KEYS:
        return R.artifact_ref(value, label)
    if "from" in value:
        extra = sorted(keys - {"from", "field"})
        R.require(not extra, "invalid_argument",
                  f"{label} has unknown binding fields", extra=extra)
        source = R.require_text(value["from"], f"{label}.from", max_length=128)
        R.require(source in needs, "unauthorized",
                  f"{label} references an undeclared dependency",
                  source=source, node=stage)
        mapped: dict[str, Any] = {
            "from": source if ids is None else ids[source]}
        if "field" in value:
            mapped["field"] = R.require_text(value["field"], f"{label}.field",
                                             max_length=64)
        return mapped
    if keys == {"run_input"}:
        return {"run_input": R.require_text(value["run_input"],
                                            f"{label}.run_input",
                                            max_length=128)}
    R.fail("invalid_argument", f"{label} is not a supported input binding",
           keys=sorted(keys))


def _stage_instance_ids(run_id: str, names: Any) -> dict[str, str]:
    """Bounded run-scoped node ids, stable for (run_id, declaration name).

    A legal stage name can be 128 characters and a real run id is a 36
    character UUID, so '{run_id}-{name}' would exceed the global 128 character
    node_id limit and the instance could never materialise. The declaration
    name stays in local_child_id; the id is a domain-separated digest prefix.
    """
    return {
        name: "stage-" + hashlib.sha256(
            "\x00".join((_STAGE_ID_DOMAIN, run_id, name)).encode("utf-8")
        ).hexdigest()[:32]
        for name in names
    }


def _topological_order(nodes: Mapping[str, Any]) -> list[str]:
    names = set(nodes)
    remaining: dict[str, set[str]] = {}
    for name, node in nodes.items():
        R.require(isinstance(node, Mapping), "invalid_argument",
                  f"normalized node {name} must be an object")
        needs = node.get("needs") or []
        R.require(isinstance(needs, (list, tuple)) and
                  all(isinstance(need, str) and need for need in needs),
                  "invalid_argument", f"node {name}.needs must be a list")
        unknown = sorted(set(needs) - names)
        R.require(not unknown, "invalid_argument",
                  f"node {name} needs an undeclared node", unknown=unknown)
        remaining[name] = set(needs)
    order: list[str] = []
    while remaining:
        ready = [name for name, needs in remaining.items() if not needs]
        R.require(bool(ready), "dependency_cycle",
                  "the graph dependency set has a cycle",
                  nodes=sorted(remaining))
        for name in ready:
            order.append(name)
            del remaining[name]
        for needs in remaining.values():
            needs.difference_update(ready)
    return order


class _Library:
    """One explicit catalog resolved into content-addressed definitions."""

    def __init__(self, agents: Mapping[str, Any], tasks: Mapping[str, Any],
                 read_document: Callable[[str], Mapping[str, Any]]) -> None:
        self._agents_raw = agents
        self._tasks_raw = tasks
        self._read_document = read_document
        self._agent_alias: dict[str, AgentDefinition] = {}
        self._task_alias: dict[str, TaskDefinition] = {}
        self._visiting: set[str] = set()
        self.agents: dict[str, AgentDefinition] = {}
        self.tasks: dict[str, TaskDefinition] = {}
        self.documents: dict[str, dict[str, Any]] = {}

    # -- agents ---------------------------------------------------------

    def agent(self, alias: str) -> AgentDefinition:
        cached = self._agent_alias.get(alias)
        if cached is not None:
            return cached
        raw = self._agents_raw[alias]
        if isinstance(raw, dict):
            reference = R.digest(raw)
            document = _json_copy(raw, f"agents.{alias}")
            definition = resolve_agent_definition(document,
                                                  agent_ref=reference)
        elif isinstance(raw, str):
            reference = _content_ref(raw, f"agents.{alias}")
            document = self._read(reference, f"agents.{alias}")
            definition = resolve_agent_definition(document,
                                                  agent_ref=reference)
        else:
            R.fail("invalid_argument",
                   "an agent catalog entry must be an object or content ref",
                   agent=alias)
        self._agent_alias[alias] = definition
        self._register_agent(reference, definition, document)
        return definition

    def agent_ref(self, reference: str) -> AgentDefinition:
        cached = self.agents.get(reference)
        if cached is not None:
            return cached
        document = self._read(reference, "agent")
        definition = resolve_agent_definition(document, agent_ref=reference)
        self._register_agent(reference, definition, document)
        return definition

    # -- tasks ----------------------------------------------------------

    def task(self, alias: str) -> TaskDefinition:
        cached = self._task_alias.get(alias)
        if cached is not None:
            return cached
        raw = self._tasks_raw[alias]
        if isinstance(raw, dict):
            R.require(alias not in self._visiting, "dependency_cycle",
                      "the task catalog has a definition cycle", task=alias)
            self._visiting.add(alias)
            try:
                definition = self._inline_task(raw, alias)
            finally:
                self._visiting.discard(alias)
        elif isinstance(raw, str):
            definition = self.task_ref(_content_ref(raw, f"tasks.{alias}"))
        else:
            R.fail("invalid_argument",
                   "a task catalog entry must be an object or content ref",
                   task=alias)
        self._task_alias[alias] = definition
        return definition

    def task_ref(self, reference: str) -> TaskDefinition:
        cached = self.tasks.get(reference)
        if cached is not None:
            return cached
        document = self._read(reference, "task")
        definition = resolve_task_definition(document, task_ref=reference)
        self._register_task(reference, definition, document)
        self.agent_ref(definition.agent_ref)
        self._follow_expansion(definition)
        return definition

    def graph_task(self, value: Any, label: str) -> TaskDefinition:
        R.require(isinstance(value, str) and value, "invalid_argument",
                  f"{label} must be a content ref or a declared task alias")
        if R.ARTIFACT_ID.fullmatch(value):
            return self.task_ref(value)
        if value in self._tasks_raw:
            return self.task(value)
        R.fail("not_found",
               f"{label} is neither a content ref nor a declared task alias",
               value=value[:128])

    def _inline_task(self, raw: dict[str, Any],
                     alias: str) -> TaskDefinition:
        # Alias rewriting applies only to this inline source document. Any
        # document read by content ref is parsed as-is (already normalized).
        body = _json_copy(raw, f"tasks.{alias}")
        if "agent_ref" in body:
            body["agent_ref"] = self._agent_slot(body["agent_ref"], alias)
        if isinstance(body.get("expansion"), dict):
            body["expansion"] = self._expansion_slots(body["expansion"], alias)
        reference = R.digest(body)
        definition = resolve_task_definition(body, task_ref=reference)
        self._register_task(reference, definition, body)
        return definition

    def _agent_slot(self, value: Any, task_alias: str) -> Any:
        if isinstance(value, str):
            if R.ARTIFACT_ID.fullmatch(value):
                self.agent_ref(value)
                return value
            if value in self._agents_raw:
                return self.agent(value).agent_ref
            R.fail("not_found",
                   "the task agent_ref is neither a content ref nor a "
                   "declared agent alias", task=task_alias,
                   value=value[:128])
        return value

    def _task_slot(self, value: Any, label: str) -> Any:
        if isinstance(value, str):
            if R.ARTIFACT_ID.fullmatch(value):
                self.task_ref(value)
                return value
            if value in self._tasks_raw:
                return self.task(value).task_ref
            R.fail("not_found",
                   "the reference is neither a content ref nor a declared "
                   "task alias", label=label, value=value[:128])
        return value

    def _expansion_slots(self, raw: Mapping[str, Any],
                         task_alias: str) -> dict[str, Any]:
        body = deepcopy(dict(raw))
        children = body.get("children")
        if not isinstance(children, list):
            return body
        normalized: list[Any] = []
        for index, raw_child in enumerate(children):
            if not isinstance(raw_child, dict):
                normalized.append(raw_child)
                continue
            label = f"task {task_alias}.expansion.children[{index}]"
            child = dict(raw_child)
            template = child.get("template_ref")
            contract = child.get("contract_ref")
            if template is not None:
                template = self._task_slot(template, f"{label}.template_ref")
                child["template_ref"] = template
            if contract is not None:
                contract = self._task_slot(contract, f"{label}.contract_ref")
                child["contract_ref"] = contract
                # contract_ref attests the selected task's initial ref; it
                # never selects one. Without template_ref the definition
                # validator rejects the child as contract-only.
                if template is not None:
                    R.require(template == contract, "invalid_argument",
                              "expansion contract_ref must equal the child "
                              "task ref",
                              task=task_alias,
                              local_child_id=str(
                                  child.get("local_child_id"))[:64])
            if isinstance(template, str):
                child_task = self.task_ref(template)
                R.require(child_task.completion_kind == "candidate",
                          "invalid_argument",
                          "an expansion child must be a candidate-mode task",
                          task=task_alias, child_ref=template)
            reviewer = child.get("reviewer_ref")
            if reviewer is not None and reviewer != "parent":
                resolved = self._task_slot(reviewer,
                                           f"{label}.reviewer_ref")
                if isinstance(resolved, str):
                    dependency = self.task_ref(resolved)
                    R.require(dependency.completion_kind == "review_receipt",
                              "invalid_argument",
                              "an expansion reviewer must be a review_receipt "
                              "task", task=task_alias, reviewer_ref=resolved)
                child["reviewer_ref"] = resolved
            normalized.append(child)
        body["children"] = normalized
        return body

    def _follow_expansion(self, definition: TaskDefinition) -> None:
        expansion = definition.expansion
        if not isinstance(expansion, Mapping):
            return
        children = expansion.get("children")
        if not isinstance(children, list):
            return
        for child in children:
            if not isinstance(child, Mapping):
                continue
            template = child.get("template_ref")
            if isinstance(template, str):
                task = self.task_ref(template)
                R.require(task.completion_kind == "candidate",
                          "invalid_argument",
                          "an expansion child must be a candidate-mode task",
                          task=definition.task_ref, child_ref=template)
            reviewer = child.get("reviewer_ref")
            if isinstance(reviewer, str) and reviewer != "parent":
                dependency = self.task_ref(reviewer)
                R.require(dependency.completion_kind == "review_receipt",
                          "invalid_argument",
                          "an expansion reviewer must be a review_receipt task",
                          task=definition.task_ref, reviewer_ref=reviewer)

    # -- storage --------------------------------------------------------

    def _read(self, reference: str, label: str) -> dict[str, Any]:
        try:
            document = self._read_document(reference)
        except R.ControlPlaneError:
            raise
        except Exception as error:  # the reader is the only external source
            R.fail("not_found",
                   "the content ref is not available to the reader",
                   ref=reference, label=label,
                   reader_error=type(error).__name__)
        R.require(isinstance(document, dict), "invalid_argument",
                  f"{label} document must be an object", ref=reference)
        R.canonical(document)
        return deepcopy(document)

    def _register_agent(self, reference: str, definition: AgentDefinition,
                        document: Mapping[str, Any]) -> None:
        if reference in self.agents:
            return
        R.require(definition.agent_ref == reference, "artifact_mismatch",
                  "the agent definition does not match its content ref",
                  expected=reference, actual=definition.agent_ref)
        self.agents[reference] = definition
        self.documents[reference] = deepcopy(document)

    def _register_task(self, reference: str, definition: TaskDefinition,
                       document: Mapping[str, Any]) -> None:
        if reference in self.tasks:
            return
        R.require(definition.task_ref == reference, "artifact_mismatch",
                  "the task definition does not match its content ref",
                  expected=reference, actual=definition.task_ref)
        self.tasks[reference] = definition
        self.documents[reference] = deepcopy(document)


def compile_definition_bundle(
    document: Mapping[str, Any], *,
    read_document: Callable[[str], Mapping[str, Any]],
) -> DefinitionBundle:
    """W02b1: resolve explicit catalogs/references and validate Graph /2.

    Catalog entries are inline structured documents or sha256 content refs.
    Local aliases resolve only inside explicit catalogs; no inferred file
    paths, network fetches, latest tags or executable Graph /1 fallback.
    read_document is an API-authorized immutable reader supplied by the
    caller. Every returned document must match its requested digest.

    Graph nodes declare task_ref, reviewer_ref, needs and inputs. A reviewer
    ref resolves to a review_receipt TaskDefinition. Dependencies are a DAG;
    all stage nodes are instantiated, not only a privileged entry/root.
    Library definition cycles are rejected; dynamic recursive task expansion
    is represented by task instances and need not self-reference a hash.
    """
    graph = R.require_object(
        document, "graph definition", allowed=_GRAPH_FIELDS,
        required={"format", "definition_id", "policy", "budget_limits",
                  "agents", "tasks", "nodes"},
    )
    R.require(graph["format"] == GRAPH_DEFINITION_FORMAT, "invalid_argument",
              "the graph definition format is not supported; Graph /1 is not "
              "executed", format=str(graph["format"])[:128])
    definition_id = R.require_slug(graph["definition_id"], "definition_id")
    base_revision = R.require_int(graph.get("base_revision", 0),
                                  "base_revision")
    policy = graph["policy"]
    R.require(policy in R.DEFINITION_POLICIES, "capability_unsupported",
              "the graph requests an unsupported scheduling policy",
              policy=str(policy)[:128])
    budget_limits = _validate_budget_limits(graph["budget_limits"])
    if "description" in graph:
        R.require_text(graph["description"], "description")
    if "input_schema" in graph:
        R.validate_input_schema(graph["input_schema"], "input_schema")
    agents_raw = R.require_object(graph["agents"], "agents")
    tasks_raw = R.require_object(graph["tasks"], "tasks")
    nodes_raw = R.require_object(graph["nodes"], "nodes")
    R.require(bool(agents_raw), "invalid_argument", "agents must be nonempty")
    R.require(bool(tasks_raw), "invalid_argument", "tasks must be nonempty")
    R.require(bool(nodes_raw), "invalid_argument", "nodes must be nonempty")
    for alias in agents_raw:
        R.require_slug(alias, "agent alias")
    for alias in tasks_raw:
        R.require_slug(alias, "task alias")
    for name in nodes_raw:
        R.require_slug(name, "node name")
    R.require(callable(read_document), "invalid_argument",
              "read_document must be callable")

    library = _Library(agents_raw, tasks_raw, read_document)
    for alias in agents_raw:
        library.agent(alias)
    for alias in tasks_raw:
        library.task(alias)

    normalized_nodes: dict[str, dict[str, Any]] = {}
    for name, raw in nodes_raw.items():
        node = R.require_object(raw, f"node {name}",
                                allowed=_GRAPH_NODE_FIELDS,
                                required={"task_ref", "reviewer_ref", "needs"})
        task = library.graph_task(node["task_ref"], f"node {name}.task_ref")
        R.require(task.completion_kind == "candidate", "invalid_argument",
                  "a graph stage task must complete with a candidate",
                  node=name, task_ref=task.task_ref)
        R.require(node["reviewer_ref"] != "parent", "invalid_argument",
                  "a top-level graph stage has no parent reviewer", node=name)
        reviewer = library.graph_task(node["reviewer_ref"],
                                      f"node {name}.reviewer_ref")
        R.require(reviewer.completion_kind == "review_receipt",
                  "invalid_argument",
                  "a graph reviewer must be a review_receipt task",
                  node=name, reviewer_ref=reviewer.task_ref)
        needs = node["needs"]
        R.require(isinstance(needs, list) and
                  all(isinstance(need, str) and need for need in needs),
                  "invalid_argument",
                  f"node {name}.needs must be a list of names")
        R.require(len(set(needs)) == len(needs), "invalid_argument",
                  f"node {name}.needs repeats a stage", node=name)
        unknown = sorted(set(needs) - set(nodes_raw))
        R.require(not unknown, "not_found",
                  "a graph stage needs an undeclared stage",
                  node=name, unknown=unknown)
        inputs = node.get("inputs", {})
        R.require(isinstance(inputs, dict), "invalid_argument",
                  f"node {name}.inputs must be an object")
        normalized_inputs = {
            key: _input_binding(value, needs=list(needs), stage=name,
                                label=f"node {name}.inputs.{key}")
            for key, value in inputs.items()
        }
        normalized_nodes[name] = {
            "task_ref": task.task_ref,
            "reviewer_ref": reviewer.task_ref,
            "needs": list(needs),
            "inputs": normalized_inputs,
            "delegation": _delegation(node, f"node {name}"),
        }
    R.require_acyclic({name: list(node["needs"])
                       for name, node in normalized_nodes.items()})

    normalized: dict[str, Any] = {
        "format": graph["format"],
        "definition_id": definition_id,
        "base_revision": base_revision,
        "policy": policy,
        "budget_limits": budget_limits,
        "agents": {alias: library.agent(alias).agent_ref
                   for alias in agents_raw},
        "tasks": {alias: library.task(alias).task_ref for alias in tasks_raw},
        "nodes": normalized_nodes,
    }
    if "description" in graph:
        normalized["description"] = graph["description"]
    if "input_schema" in graph:
        normalized["input_schema"] = deepcopy(graph["input_schema"])
    return DefinitionBundle(
        document=normalized,
        agents=library.agents,
        tasks=library.tasks,
        artifact_documents=library.documents,
    )


def initial_graph_tasks(
    bundle: DefinitionBundle, *, run_id: str,
) -> list[dict[str, Any]]:
    """W02b1: propose one atomic batch of existing node-create mutations.

    Existing node_id/template_ref/contract_ref/input_bindings/dependencies
    fields are reused. template_ref is now the pinned task definition ref;
    the Graph declaration name identifies position, not reusable behavior.
    Initial contract_ref equals task_ref. Dependencies and 'from' bindings
    are translated to deterministic run-local IDs without accepting undeclared
    edges. All stage instances start pending; existing freeze/ready/binding
    logic runs only when their input versions and dependencies are available.

    No synthetic whole-Graph Agent is inserted. Scheduler submits the batch
    with read versions and never duplicates it after restart.
    """
    R.require(isinstance(bundle, DefinitionBundle), "invalid_argument",
              "bundle must be a DefinitionBundle")
    R.require_text(run_id, "run_id", max_length=96)
    document = bundle.document
    nodes = document.get("nodes") if isinstance(document, Mapping) else None
    R.require(isinstance(nodes, Mapping) and bool(nodes), "invalid_argument",
              "bundle.document must contain graph nodes")
    identifiers = _stage_instance_ids(run_id, nodes)
    for name, node_id in identifiers.items():
        R.require_text(node_id, f"node_id for {name}", max_length=128)
    batch: list[dict[str, Any]] = []
    for name in _topological_order(nodes):
        node = nodes[name]
        R.require(isinstance(node, Mapping), "invalid_argument",
                  f"normalized node {name} must be an object")
        needs = node.get("needs") or []
        R.require(isinstance(needs, (list, tuple)) and
                  all(isinstance(need, str) for need in needs),
                  "invalid_argument", f"node {name}.needs must be a list")
        needs = list(needs)
        unknown = sorted(set(needs) - set(identifiers))
        R.require(not unknown, "invalid_argument",
                  "a normalized stage depends on an undeclared stage",
                  node=name, unknown=unknown)
        requirements = node.get("inputs") or {}
        R.require(isinstance(requirements, Mapping), "invalid_argument",
                  f"node {name}.inputs must be an object")
        task_ref = _content_ref(node.get("task_ref"),
                                f"node {name}.task_ref")
        reviewer_ref = _content_ref(node.get("reviewer_ref"),
                                    f"node {name}.reviewer_ref")
        bindings = {
            key: _input_binding(value, needs=needs, stage=name,
                                label=f"node {name}.inputs.{key}",
                                ids=identifiers)
            for key, value in requirements.items()
        }
        batch.append({"kind": "node", "op": "create", "fields": {
            "node_id": identifiers[name],
            "run_id": run_id,
            "parent_id": None,
            "expansion_id": None,
            "local_child_id": name,
            "template_ref": task_ref,
            "contract_ref": task_ref,
            "input_bindings": bindings,
            "delegation": deepcopy(node.get("delegation") or {}),
            "dependencies": [identifiers[need] for need in needs],
            "reviewer_ref": reviewer_ref,
            "state": "pending",
        }})
    return batch
