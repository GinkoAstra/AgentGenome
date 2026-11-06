"""Agent definition + task delegation -> existing Backend WorkerSpec.

Codex architecture scaffold; implementation is assigned to Qoder in W02.
These are data objects, not services. Identity comes from a committed binding,
never from Agent output. No second execution model or tool-grant vocabulary.
"""
from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from . import resources as R
from .backends.base import Capabilities, WorkerSpec

DEFINITION_FORMAT = "agentgenome-agent-definition/1"

_DEFINITION_FIELDS = frozenset({
    "format", "backend_ref", "instructions", "tool_profile",
    "required_capabilities", "runtime_options", "configurable_fields",
})
_PROFILE_FIELDS = frozenset({"tools", "backend_tools"})
_TOOL_FIELDS = frozenset({"name", "version", "scope"})
_CAPABILITY_FIELDS = frozenset({
    "resumable_session", "live_observation", "idempotent_delivery",
    "stop", "checkpoint",
})
_BOOLEAN_CAPABILITIES = frozenset({
    "resumable_session", "idempotent_delivery", "checkpoint",
})
_OBSERVATION_LEVELS = {"none": 0, "limited": 1, "full": 2}
_STOP_MODES = frozenset({"none", "cooperative", "process"})
# First-round semantic whitelist: no reflection into arbitrary object paths.
_CONFIGURABLE_PATHS = frozenset({"runtime_options.model"})
_WORKER_BUDGET_KEYS = frozenset({
    "max_provider_calls", "max_tool_calls", "max_continuations",
})
# Absent max_continuations means continuations are not enabled at all
# (Scheduler._wake_idle_workers -> continuation_not_declared). Provider/tool
# caps keep their own meaning: absent means no budget-side cap.
_ENABLE_REQUIRED_BUDGET_KEYS = frozenset({"max_continuations"})
_RESERVED_CONTEXT_KEYS = frozenset({
    "role", "brief", "sop_brief", "instructions", "contract_ref",
    "upper_contract_ref", "acceptance", "template_ref", "input_refs",
    "grants", "reviewer_ref",
    "target_node_id", "candidate_id", "candidate_round",
    "review_candidate_ids", "review_inputs", "review_children",
    "run_id", "node_id", "worker_id", "epoch", "intent_seq",
    "agent_ref", "backend_ref", "desired", "checkpoint_ref", "usage",
    "budget", "tool_profile", "runtime_options", "message_kind",
    "review", "decision", "materials", "workspace", "data_dir",
    "delivery_key",
})
_CONTENT_REF = re.compile(r"sha256:[0-9a-f]{64}")


@dataclass(frozen=True)
class AgentDefinition:
    """Resolved, version-pinned definition; agent_ref is not a session ID."""

    agent_ref: str
    definition_digest: str
    backend_ref: str
    instructions: str
    tool_profile: Mapping[str, Any]
    required_capabilities: Mapping[str, Any] = field(default_factory=dict)
    runtime_options: Mapping[str, Any] = field(default_factory=dict)
    configurable_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class TaskDelegation:
    """Per-task inputs and requested configuration, within frozen bounds.

    The logical Worker is a task role, represented by the existing node_id.
    Reusing a definition creates new task/binding identities, not shared chat.
    Permission/budget limits still come from committed API state.
    """

    node_id: str
    contract_ref: str
    input_refs: Sequence[Mapping[str, Any]]
    context: Mapping[str, Any] = field(default_factory=dict)
    tool_profile: Mapping[str, Any] | None = None
    budget: Mapping[str, Any] = field(default_factory=dict)
    runtime_options: Mapping[str, Any] = field(default_factory=dict)


def _content_ref(value: Any, label: str) -> str:
    R.require_text(value, label, max_length=71)
    R.require(_CONTENT_REF.fullmatch(value) is not None, "invalid_argument",
              f"{label} must be a content-addressed sha256 ref", value=value)
    return value


def _json_mapping(value: Any, label: str) -> dict[str, Any]:
    R.require(isinstance(value, Mapping), "invalid_argument",
              f"{label} must be an object")
    body = dict(value)
    R.canonical(body)
    return body


def _tool_entries(raw: Any, label: str) -> list[dict[str, Any]]:
    if isinstance(raw, Mapping):
        raw = list(raw.values())
    R.require(isinstance(raw, (list, tuple)), "invalid_argument",
              f"{label} must be a list or object")
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            entry: dict[str, Any] = {"name": item}
        elif isinstance(item, Mapping):
            extra = sorted(set(item) - _TOOL_FIELDS)
            R.require(not extra, "invalid_argument",
                      f"{label} entry has unknown fields", extra=extra)
            entry = dict(item)
        else:
            R.fail("invalid_argument",
                   f"{label} entries must be names or objects",
                   got=type(item).__name__)
        name = R.require_text(entry.get("name"), f"{label} entry name")
        R.require(name not in seen, "invalid_argument",
                  f"{label} repeats a tool", tool=name)
        seen.add(name)
        for field_name in ("version", "scope"):
            if field_name in entry:
                R.require_text(entry[field_name],
                               f"{label}.{name}.{field_name}")
        entries.append(entry)
    return entries


def _name_list(raw: Any, label: str) -> list[str]:
    R.require(isinstance(raw, (list, tuple)), "invalid_argument",
              f"{label} must be a list of names")
    names: list[str] = []
    for item in raw:
        name = R.require_text(item, f"{label} item")
        R.require(name not in names, "invalid_argument",
                  f"{label} repeats a name", name=name)
        names.append(name)
    return names


def _profile_parts(profile: Any, label: str
                   ) -> tuple[list[dict[str, Any]] | None,
                              list[str] | None]:
    body = _json_mapping(profile, label)
    extra = sorted(set(body) - _PROFILE_FIELDS)
    R.require(not extra, "invalid_argument",
              f"{label} has unknown fields", extra=extra)
    tools = (_tool_entries(body["tools"], f"{label}.tools")
             if "tools" in body else None)
    backend_tools = (_name_list(body["backend_tools"],
                                f"{label}.backend_tools")
                     if "backend_tools" in body else None)
    return tools, backend_tools


def _validate_required_capabilities(raw: Any) -> dict[str, Any]:
    body = _json_mapping(raw, "required_capabilities")
    for name, value in body.items():
        R.require(name in _CAPABILITY_FIELDS, "capability_unsupported",
                  "the definition requires an unknown capability",
                  capability=name)
        if name in _BOOLEAN_CAPABILITIES:
            R.require(isinstance(value, bool), "invalid_argument",
                      f"required_capabilities.{name} must be a boolean")
        elif name == "live_observation":
            R.require(isinstance(value, str) and value in _OBSERVATION_LEVELS,
                      "capability_unsupported",
                      "the required observation level is unsupported",
                      level=value)
        elif name == "stop":
            R.require(isinstance(value, str) and value in _STOP_MODES,
                      "capability_unsupported",
                      "the required stop mode is unsupported", mode=value)
    return body


def _validate_runtime_options(raw: Any) -> dict[str, Any]:
    return _json_mapping(raw, "runtime_options")


def _validate_configurable_fields(raw: Any) -> tuple[str, ...]:
    R.require(isinstance(raw, list), "invalid_argument",
              "configurable_fields must be a list of field paths")
    fields: list[str] = []
    for item in raw:
        path = R.require_text(item, "configurable_fields item")
        R.require(path not in fields, "invalid_argument",
                  "configurable_fields repeats a path", path=path)
        if path not in _CONFIGURABLE_PATHS:
            R.fail("capability_unsupported",
                   "the configurable field path is not supported",
                   path=path)
        fields.append(path)
    return tuple(fields)


def resolve_agent_definition(
    document: Mapping[str, Any], *, agent_ref: str,
) -> AgentDefinition:
    """W02: validate a local definition and resolve its immutable version.

    Reject unresolved/mismatched refs. Do not silently use latest or invent
    instructions from an agent_ref label. API publication owns persisted refs.
    """
    reference = _content_ref(agent_ref, "agent_ref")
    body = R.require_object(
        document, "agent definition",
        allowed=_DEFINITION_FIELDS,
        required={"format", "backend_ref", "instructions", "tool_profile"},
    )
    R.require(body["format"] == DEFINITION_FORMAT, "invalid_argument",
              "the agent definition format is not supported",
              format=body["format"])
    backend_ref = R.require_text(body["backend_ref"], "backend_ref",
                                 max_length=256)
    instructions = R.require_text(body["instructions"],
                                  "definition instructions", max_length=65536)
    tools, backend_tools = _profile_parts(body["tool_profile"], "tool_profile")
    tool_profile: dict[str, Any] = {}
    if tools is not None:
        tool_profile["tools"] = tools
    if backend_tools is not None:
        tool_profile["backend_tools"] = backend_tools
    required = _validate_required_capabilities(
        body.get("required_capabilities", {}))
    runtime_options = _validate_runtime_options(
        body.get("runtime_options", {}))
    configurable = _validate_configurable_fields(
        body.get("configurable_fields", []))

    definition_digest = R.digest(body)
    R.require(definition_digest == reference, "artifact_mismatch",
              "the definition content does not match its agent_ref",
              expected=reference, actual=definition_digest)
    return AgentDefinition(
        agent_ref=reference,
        definition_digest=definition_digest,
        backend_ref=backend_ref,
        instructions=instructions,
        tool_profile=tool_profile,
        required_capabilities=required,
        runtime_options=runtime_options,
        configurable_fields=configurable,
    )


def _trusted_binding(binding: WorkerSpec) -> tuple[dict[str, Any],
                                                   dict[str, Any],
                                                   dict[str, Any] | None]:
    for name in ("worker_id", "node_id", "run_id", "backend_ref",
                 "agent_ref", "desired"):
        R.require_text(getattr(binding, name), f"binding.{name}")
    R.require_int(binding.epoch, "binding.epoch", minimum=0)
    R.require_int(binding.intent_seq, "binding.intent_seq", minimum=0)
    context = _json_mapping(binding.context, "binding.context")
    budget = _json_mapping(binding.budget, "binding.budget")
    for name in _WORKER_BUDGET_KEYS:
        if name in budget:
            R.require_int(budget[name], f"binding.budget.{name}", minimum=0)
    usage = _json_mapping(binding.usage, "binding.usage")
    checkpoint = (None if binding.checkpoint_ref is None else
                  _json_mapping(binding.checkpoint_ref,
                                "binding.checkpoint_ref"))
    return context, usage, checkpoint


def _frozen_association(context: Mapping[str, Any],
                        contract_ref: str, input_refs: list[dict[str, Any]],
                        ) -> None:
    # The committed Task already froze contract and inputs. A delegation may
    # only cite them; it never supplies the authoritative value.
    R.require("contract_ref" in context and context["contract_ref"] is not None,
              "unauthorized",
              "the trusted binding has no frozen contract_ref")
    frozen_contract = R.require_text(context["contract_ref"],
                                     "binding.context.contract_ref")
    R.require(frozen_contract == contract_ref, "unauthorized",
              "the delegation would replace the frozen contract",
              frozen=frozen_contract, requested=contract_ref)
    # An explicit empty list is a frozen value; a missing/None field is not.
    R.require("input_refs" in context and context["input_refs"] is not None,
              "unauthorized",
              "the trusted binding has no frozen input_refs")
    frozen_inputs = context["input_refs"]
    R.require(isinstance(frozen_inputs, (list, tuple)), "invalid_argument",
              "binding.context.input_refs must be a list")
    frozen = [R.artifact_ref(ref, "binding.context.input_refs item")
              for ref in frozen_inputs]
    R.require(R.canonical(frozen) == R.canonical(input_refs),
              "unauthorized",
              "the delegation would replace the frozen input refs",
              frozen=frozen, requested=input_refs)
    grants = context.get("grants")
    if grants is not None:
        body = _json_mapping(grants, "binding.context.grants")
        allowed = body.get("artifacts")
        if allowed is not None:
            R.require(isinstance(allowed, (list, tuple)) and
                      all(isinstance(item, str) for item in allowed),
                      "invalid_argument",
                      "binding.context.grants.artifacts must be a list")
            extras = sorted({ref["id"] for ref in input_refs}
                            - set(allowed))
            R.require(not extras, "unauthorized",
                      "the delegation input refs are not granted by the binding",
                      artifacts=extras)


def _intersect_tools(definition: list[dict[str, Any]] | None,
                     binding: list[dict[str, Any]] | None,
                     ) -> list[dict[str, Any]] | None:
    if definition is None and binding is None:
        return None
    by_binding = {entry["name"]: entry for entry in binding or []}
    result: list[dict[str, Any]] = []
    for entry in definition or []:
        other = by_binding.get(entry["name"])
        if other is None:
            continue
        merged = dict(entry)
        compatible = True
        for key in ("version", "scope"):
            if key in entry and key in other and entry[key] != other[key]:
                compatible = False
                break
            if key not in merged and key in other:
                merged[key] = other[key]
        if compatible:
            result.append(merged)
    return result


def _narrow_tools(effective: list[dict[str, Any]] | None,
                  requested: list[dict[str, Any]] | None,
                  ) -> list[dict[str, Any]] | None:
    if requested is None:
        return effective
    by_name = {entry["name"]: entry for entry in effective or []}
    narrowed: list[dict[str, Any]] = []
    for entry in requested:
        base = by_name.get(entry["name"])
        R.require(base is not None, "unauthorized",
                  "the delegation requests an ungranted system tool",
                  tool=entry["name"])
        for key in ("version", "scope"):
            if key in entry:
                R.require(key in base and base[key] == entry[key],
                          "unauthorized",
                          "the delegation cannot change a system tool grant",
                          tool=entry["name"], field=key,
                          requested=entry[key])
        narrowed.append(dict(base))
    return narrowed


def _compose_backend_tools(definition: list[str] | None,
                           binding: list[str] | None,
                           requested: list[str] | None,
                           ) -> list[str] | None:
    """Backend tool names, constrained by a trusted ceiling.

    Absent backend_tools means the Backend's own defaults at that layer.
    Capabilities carries no tool inventory, so a non-empty explicit selection
    can only be trusted when the committed binding states the ceiling; an
    explicit empty selection is a provable narrowing (disable all tools).
    """
    if binding is None:
        if requested is None:
            if definition is None:
                return None
            if not definition:
                return []
            R.fail("capability_unsupported",
                   "the definition selects backend tools without a trusted "
                   "binding ceiling", tools=list(definition))
        if not requested:
            return []
        R.fail("capability_unsupported",
               "the delegation selects backend tools without a trusted "
               "binding ceiling", tools=list(requested))
    effective = (list(binding) if definition is None else
                 [name for name in definition if name in set(binding)])
    if requested is None:
        return effective
    if not requested:
        return []
    allowed = set(effective)
    extras = [name for name in requested if name not in allowed]
    R.require(not extras, "unauthorized",
              "the delegation requests an ungranted backend tool",
              tools=extras)
    return list(requested)


def _compose_tool_profile(definition: AgentDefinition,
                          binding: WorkerSpec,
                          delegation: TaskDelegation) -> dict[str, Any]:
    definition_tools, definition_backend = _profile_parts(
        definition.tool_profile, "definition.tool_profile")
    binding_tools, binding_backend = _profile_parts(
        binding.tool_profile, "binding.tool_profile")
    if delegation.tool_profile is None:
        requested_tools = requested_backend = None
    else:
        requested_tools, requested_backend = _profile_parts(
            delegation.tool_profile, "delegation.tool_profile")
    tools = _narrow_tools(_intersect_tools(definition_tools, binding_tools),
                          requested_tools)
    backend_tools = _compose_backend_tools(definition_backend, binding_backend,
                                           requested_backend)
    profile: dict[str, Any] = {}
    if tools is not None:
        profile["tools"] = tools
    if backend_tools is not None:
        profile["backend_tools"] = backend_tools
    return profile


def _compose_budget(binding: WorkerSpec,
                    delegation: TaskDelegation) -> dict[str, Any]:
    budget = deepcopy(dict(binding.budget))
    requested = (_json_mapping(delegation.budget, "delegation.budget")
                 if delegation.budget is not None else {})
    for name, value in requested.items():
        R.require(name in _WORKER_BUDGET_KEYS, "invalid_argument",
                  "the delegation requests an unsupported worker budget key",
                  key=name)
        R.require_int(value, f"delegation.budget.{name}", minimum=0)
        ceiling = budget.get(name)
        if name in _ENABLE_REQUIRED_BUDGET_KEYS:
            R.require(ceiling is not None, "unauthorized",
                      "the binding does not enable this budget limit",
                      key=name)
        if ceiling is not None:
            R.require_int(ceiling, f"binding.budget.{name}", minimum=0)
            R.require(value <= ceiling, "unauthorized",
                      "the delegation would exceed the committed budget",
                      key=name, requested=value, ceiling=ceiling)
        budget[name] = value
    return budget


def _compose_runtime_options(definition: AgentDefinition,
                             delegation: TaskDelegation) -> None:
    effective = dict(_json_mapping(definition.runtime_options,
                                   "definition.runtime_options"))
    requested = _json_mapping(delegation.runtime_options,
                              "delegation.runtime_options")
    for name, value in requested.items():
        path = f"runtime_options.{name}"
        R.require(path in definition.configurable_fields, "unauthorized",
                  "the delegation overrides an unconfigured runtime option",
                  path=path)
        effective[name] = value
    if effective:
        # W02a has no Backend field path for these options. Passing them
        # through context would pretend they are effective.
        R.fail("capability_unsupported",
               "no Backend consumes runtime options in this slice",
               options=sorted(effective))


def check_backend_capabilities(required: Mapping[str, Any],
                        actual: Capabilities) -> None:
    R.require(isinstance(actual, Capabilities), "invalid_argument",
              "capabilities must be the Backend declaration")
    for name, wanted in required.items():
        gotten = getattr(actual, name, None)
        if name in _BOOLEAN_CAPABILITIES:
            R.require(isinstance(wanted, bool), "invalid_argument",
                      f"required_capabilities.{name} must be a boolean")
            if wanted:
                R.require(gotten is True, "capability_unsupported",
                          "the Backend does not provide a required capability",
                          capability=name, provided=gotten)
        elif name == "live_observation":
            R.require(isinstance(wanted, str) and wanted in _OBSERVATION_LEVELS,
                      "capability_unsupported",
                      "the required observation level is unsupported",
                      level=wanted)
            level = _OBSERVATION_LEVELS.get(gotten) if isinstance(gotten, str) else None
            if level is None or level < _OBSERVATION_LEVELS[wanted]:
                R.fail("capability_unsupported",
                       "the Backend observation level is too weak",
                       required=wanted, provided=gotten)
        elif name == "stop":
            R.require(isinstance(wanted, str) and wanted in _STOP_MODES,
                      "capability_unsupported",
                      "the required stop mode is unsupported", mode=wanted)
            if wanted != "none":
                R.require(gotten == wanted, "capability_unsupported",
                          "the Backend stop mode does not match",
                          required=wanted, provided=gotten)
        else:  # pragma: no cover - resolve already rejects unknown names
            R.fail("capability_unsupported",
                   "the definition requires an unknown capability",
                   capability=name)


def build_worker_spec(
    definition: AgentDefinition,
    delegation: TaskDelegation,
    *, binding: WorkerSpec,
    capabilities: Capabilities,
) -> WorkerSpec:
    """W02: pure configuration composition before Scheduler commits binding.

    Preserve trusted execution identity. Verify node/backend/agent association,
    required capabilities and allowed overrides. Parent may narrow grants and
    budgets, not expand committed ceilings or replace frozen contract/inputs.
    The Controller consumes this projection without assembling prompts/grants.
    Runtime options must use a declared Backend path, not be silently ignored.
    """
    R.require(isinstance(definition, AgentDefinition), "invalid_argument",
              "definition must be a resolved AgentDefinition")
    R.require(isinstance(delegation, TaskDelegation), "invalid_argument",
              "delegation must be a TaskDelegation")
    R.require(isinstance(binding, WorkerSpec), "invalid_argument",
              "binding must be the committed WorkerSpec")
    R.require(definition.agent_ref == definition.definition_digest,
              "invalid_argument",
              "the definition digest and agent_ref disagree")
    context, bound_usage, checkpoint = _trusted_binding(binding)
    R.require_text(delegation.node_id, "delegation.node_id")
    R.require(delegation.node_id == binding.node_id, "unauthorized",
              "the delegation does not belong to this binding",
              node_id=delegation.node_id, binding_node_id=binding.node_id)
    R.require(definition.agent_ref == binding.agent_ref, "unauthorized",
              "the definition ref does not match the committed binding",
              definition=definition.agent_ref, binding=binding.agent_ref)
    R.require(definition.backend_ref == binding.backend_ref,
              "capability_unsupported",
              "the definition requires a different backend",
              definition=definition.backend_ref, binding=binding.backend_ref)

    contract_ref = R.require_text(delegation.contract_ref,
                                  "delegation.contract_ref")
    R.require(isinstance(delegation.input_refs, (list, tuple)),
              "invalid_argument", "delegation.input_refs must be a list")
    input_refs = [R.artifact_ref(ref, "delegation.input_refs item")
                  for ref in delegation.input_refs]
    _frozen_association(context, contract_ref, input_refs)

    effective_context = deepcopy(context)
    requested_context = _json_mapping(delegation.context,
                                      "delegation.context")
    for name, value in requested_context.items():
        R.require(name not in _RESERVED_CONTEXT_KEYS, "unauthorized",
                  "the delegation context cannot override a system field",
                  field=name)
        effective_context[name] = deepcopy(value)
    effective_context["contract_ref"] = contract_ref
    effective_context["input_refs"] = deepcopy(input_refs)
    effective_context["instructions"] = definition.instructions

    check_backend_capabilities(definition.required_capabilities, capabilities)
    _compose_runtime_options(definition, delegation)
    tool_profile = _compose_tool_profile(definition, binding, delegation)
    budget = _compose_budget(binding, delegation)
    return WorkerSpec(
        worker_id=binding.worker_id,
        node_id=binding.node_id,
        epoch=binding.epoch,
        intent_seq=binding.intent_seq,
        backend_ref=binding.backend_ref,
        agent_ref=definition.agent_ref,
        desired=binding.desired,
        run_id=binding.run_id,
        context=effective_context,
        tool_profile=tool_profile,
        budget=budget,
        checkpoint_ref=(None if checkpoint is None else deepcopy(checkpoint)),
        usage=deepcopy(bound_usage),
        required_capabilities=deepcopy(dict(definition.required_capabilities)),
    )
