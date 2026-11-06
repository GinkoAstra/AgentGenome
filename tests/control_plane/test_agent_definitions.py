"""W02a: pure agent-definition resolution and WorkerSpec composition.

Everything here is a module-level contract: no API, Controller or Backend is
started, and no host wiring consumes the result yet.
"""
from __future__ import annotations

import hashlib
from copy import deepcopy

import pytest

from node_runtime.control_plane import ControlPlaneError, resources as R
from node_runtime.control_plane.agent_definitions import (
    AgentDefinition,
    TaskDelegation,
    build_worker_spec,
    resolve_agent_definition,
)
from node_runtime.control_plane.backends import Capabilities, WorkerSpec

BACKEND = "pi"
INSTRUCTIONS = "Clean the input dataset and report the row count."
SYSTEM_TOOL = {"name": "node.submit_candidate", "version": "1",
               "scope": "node:self"}
PI = Capabilities(
    resumable_session=True,
    live_observation="limited",
    idempotent_delivery=True,
    stop="process",
    checkpoint=True,
)


def artifact(tag: str) -> dict[str, str | int]:
    digest = hashlib.sha256(tag.encode("utf-8")).hexdigest()
    return {"id": f"sha256:{digest}", "sha256": digest,
            "size": len(tag), "media_type": "text/plain"}


def document(**overrides) -> dict:
    body = {
        "format": "agentgenome-agent-definition/1",
        "backend_ref": BACKEND,
        "instructions": INSTRUCTIONS,
        "tool_profile": {
            "tools": [dict(SYSTEM_TOOL)],
            "backend_tools": ["read_file", "write_file"],
        },
    }
    body.update(overrides)
    return body


def resolve(body: dict | None = None) -> AgentDefinition:
    body = document() if body is None else body
    return resolve_agent_definition(body, agent_ref=R.digest(body))


def code_of(call) -> str:
    with pytest.raises(ControlPlaneError) as caught:
        call()
    return caught.value.code


def frozen_context(**extra) -> dict:
    body = {"contract_ref": "contract:clean",
            "input_refs": [artifact("input.csv")]}
    body.update(extra)
    return body


def make_binding(definition: AgentDefinition, **overrides) -> WorkerSpec:
    values = {
        "worker_id": "worker-1",
        "node_id": "clean",
        "epoch": 1,
        "intent_seq": 2,
        "backend_ref": BACKEND,
        "agent_ref": definition.agent_ref,
        "desired": "running",
        "run_id": "run-1",
        "context": frozen_context(candidate_id="candidate-1"),
        "tool_profile": {"tools": [dict(SYSTEM_TOOL)],
                         "backend_tools": ["read_file", "write_file"]},
        "budget": {"max_provider_calls": 10, "max_tool_calls": 20,
                   "max_continuations": 2},
        "usage": {"provider_calls": 2, "tool_calls": 3},
    }
    values.update(overrides)
    return WorkerSpec(**values)


def make_delegation(**overrides) -> TaskDelegation:
    values = {
        "node_id": "clean",
        "contract_ref": "contract:clean",
        "input_refs": [artifact("input.csv")],
        "context": {"notes": "drop blank rows"},
    }
    values.update(overrides)
    return TaskDelegation(**values)


# -- resolve_agent_definition ------------------------------------------------


def test_resolve_pins_the_content_digest_and_normalizes_the_profile():
    body = document()
    first = resolve_agent_definition(body, agent_ref=R.digest(body))
    second = resolve_agent_definition(deepcopy(body), agent_ref=R.digest(body))

    assert first.agent_ref == first.definition_digest == R.digest(body)
    assert first.agent_ref == second.agent_ref
    assert first.backend_ref == BACKEND
    assert first.instructions == INSTRUCTIONS
    assert first.tool_profile == {
        "tools": [dict(SYSTEM_TOOL)],
        "backend_tools": ["read_file", "write_file"],
    }
    assert first.required_capabilities == {}
    assert first.runtime_options == {}
    assert first.configurable_fields == ()


def test_resolve_rejects_tampered_and_unversioned_refs():
    body = document()
    reference = R.digest(body)
    body["instructions"] = "do something else"
    assert code_of(lambda: resolve_agent_definition(body, agent_ref=reference)) == \
        "artifact_mismatch"

    fresh = document()
    assert code_of(lambda: resolve_agent_definition(
        fresh, agent_ref="agent:cleaner")) == "invalid_argument"
    assert code_of(lambda: resolve_agent_definition(
        fresh, agent_ref="sha256:" + "a" * 63)) == "invalid_argument"
    assert code_of(lambda: resolve_agent_definition(
        fresh, agent_ref=R.digest(fresh).upper())) == "invalid_argument"
    # A label never substitutes for content: a different body needs a
    # different ref and cannot be resolved under this one.
    other = document(instructions="other")
    assert code_of(lambda: resolve_agent_definition(
        other, agent_ref=R.digest(fresh))) == "artifact_mismatch"


def test_resolve_rejects_unknown_fields_and_unsupported_format():
    assert code_of(lambda: resolve(document(session_id="chat-1"))) == \
        "invalid_argument"
    assert code_of(lambda: resolve(document(
        format="agentgenome-agent-definition/2"))) == "invalid_argument"

    missing = document()
    del missing["instructions"]
    assert code_of(lambda: resolve(missing)) == "invalid_argument"

    assert code_of(lambda: resolve(document(tool_profile={
        "tools": [dict(SYSTEM_TOOL)], "network": ["*"]}))) == "invalid_argument"
    assert code_of(lambda: resolve(document(tool_profile={
        "tools": [dict(SYSTEM_TOOL), dict(SYSTEM_TOOL)]}))) == "invalid_argument"
    assert code_of(lambda: resolve(document(tool_profile={
        "tools": [{"name": SYSTEM_TOOL["name"], "effect": "read_only"}]}))) == \
        "invalid_argument"


def test_resolve_rejects_unknown_capabilities_and_configurable_paths():
    assert code_of(lambda: resolve(document(
        required_capabilities={"telepathy": True}))) == "capability_unsupported"
    assert code_of(lambda: resolve(document(
        required_capabilities={"live_observation": "partial"}))) == \
        "capability_unsupported"
    assert code_of(lambda: resolve(document(
        required_capabilities={"stop": "graceful"}))) == "capability_unsupported"
    assert code_of(lambda: resolve(document(
        required_capabilities={"checkpoint": "yes"}))) == "invalid_argument"

    assert code_of(lambda: resolve(document(
        configurable_fields=["runtime_options.temperature"]))) == \
        "capability_unsupported"
    assert code_of(lambda: resolve(document(
        configurable_fields=["context.instructions"]))) == \
        "capability_unsupported"


def test_resolve_keeps_declared_capabilities_and_configurable_paths():
    resolved = resolve(document(
        required_capabilities={"resumable_session": True,
                               "live_observation": "limited",
                               "stop": "cooperative"},
        runtime_options={"model": "cheap"},
        configurable_fields=["runtime_options.model"],
    ))
    assert resolved.required_capabilities == {
        "resumable_session": True,
        "live_observation": "limited",
        "stop": "cooperative",
    }
    assert resolved.runtime_options == {"model": "cheap"}
    assert resolved.configurable_fields == ("runtime_options.model",)


# -- build_worker_spec: identity and association -----------------------------


def test_build_preserves_binding_identity_and_definition_instructions():
    definition = resolve()
    binding = make_binding(
        definition,
        checkpoint_ref={"kind": "pi", "digest": "sha256:" + "c" * 64,
                        "session_id": "session-1", "path": "/tmp/session"},
    )
    spec = build_worker_spec(definition, make_delegation(), binding=binding,
                             capabilities=PI)

    assert spec.worker_id == binding.worker_id
    assert spec.node_id == binding.node_id
    assert spec.epoch == binding.epoch
    assert spec.intent_seq == binding.intent_seq
    assert spec.run_id == binding.run_id
    assert spec.desired == binding.desired
    assert spec.backend_ref == binding.backend_ref
    assert spec.agent_ref == definition.agent_ref
    assert spec.context["contract_ref"] == "contract:clean"
    assert spec.context["input_refs"] == [artifact("input.csv")]
    assert spec.context["instructions"] == INSTRUCTIONS
    assert spec.context["candidate_id"] == "candidate-1"
    assert spec.context["notes"] == "drop blank rows"
    assert spec.checkpoint_ref == binding.checkpoint_ref
    assert spec.checkpoint_ref is not binding.checkpoint_ref


def test_build_rejects_a_delegation_for_another_binding():
    definition = resolve()
    binding = make_binding(definition)

    assert code_of(lambda: build_worker_spec(
        definition, make_delegation(node_id="split"),
        binding=binding, capabilities=PI)) == "unauthorized"
    assert code_of(lambda: build_worker_spec(
        definition, make_delegation(),
        binding=make_binding(definition, agent_ref="sha256:" + "0" * 64),
        capabilities=PI)) == "unauthorized"
    assert code_of(lambda: build_worker_spec(
        definition, make_delegation(),
        binding=make_binding(definition, backend_ref="codex"),
        capabilities=PI)) == "capability_unsupported"


def test_build_rejects_a_definition_whose_digest_disagrees():
    definition = resolve()
    forged = AgentDefinition(
        agent_ref="sha256:" + "1" * 64,
        definition_digest="sha256:" + "2" * 64,
        backend_ref=BACKEND,
        instructions=INSTRUCTIONS,
        tool_profile={"tools": [dict(SYSTEM_TOOL)]},
    )
    assert code_of(lambda: build_worker_spec(
        forged, make_delegation(), binding=make_binding(definition),
        capabilities=PI)) == "invalid_argument"

    assert code_of(lambda: build_worker_spec(
        "not-a-definition", make_delegation(), binding=make_binding(definition),
        capabilities=PI)) == "invalid_argument"
    assert code_of(lambda: build_worker_spec(
        definition, make_delegation(), binding={}, capabilities=PI)) == \
        "invalid_argument"


# -- build_worker_spec: frozen association -----------------------------------


def test_frozen_contract_and_inputs_cannot_be_replaced():
    definition = resolve()
    binding = make_binding(definition)
    spec = build_worker_spec(definition, make_delegation(), binding=binding,
                             capabilities=PI)
    assert spec.context["contract_ref"] == "contract:clean"

    assert code_of(lambda: build_worker_spec(
        definition, make_delegation(contract_ref="contract:other"),
        binding=binding, capabilities=PI)) == "unauthorized"
    assert code_of(lambda: build_worker_spec(
        definition, make_delegation(input_refs=[artifact("other.csv")]),
        binding=binding, capabilities=PI)) == "unauthorized"
    assert code_of(lambda: build_worker_spec(
        definition, make_delegation(input_refs=[]),
        binding=binding, capabilities=PI)) == "unauthorized"


def test_missing_authoritative_task_contract_is_not_filled_from_parent():
    definition = resolve()
    # A delegation may cite a frozen contract/input, never establish one.
    for context in (
        {},
        {"input_refs": [artifact("input.csv")]},
        {"contract_ref": "contract:clean"},
        {"contract_ref": None, "input_refs": [artifact("input.csv")]},
        {"contract_ref": "contract:clean", "input_refs": None},
    ):
        binding = make_binding(definition, context=context)
        assert code_of(lambda binding=binding: build_worker_spec(
            definition, make_delegation(), binding=binding,
            capabilities=PI)) == "unauthorized", context

    # An explicit empty input list is a frozen value, not a missing field.
    empty = make_binding(definition, context={
        "contract_ref": "contract:clean", "input_refs": []})
    spec = build_worker_spec(definition, make_delegation(input_refs=[]),
                             binding=empty, capabilities=PI)
    assert spec.context["input_refs"] == []
    assert code_of(lambda: build_worker_spec(
        definition, make_delegation(), binding=empty,
        capabilities=PI)) == "unauthorized"


def test_binding_grants_limit_the_delegated_inputs():
    definition = resolve()
    first, extra = artifact("input.csv"), artifact("extra.csv")
    underfunded = make_binding(definition, context={
        "contract_ref": "contract:clean",
        "input_refs": [first, extra],
        "grants": {"artifacts": [first["id"]]},
    })
    assert code_of(lambda: build_worker_spec(
        definition, make_delegation(input_refs=[first, extra]),
        binding=underfunded, capabilities=PI)) == "unauthorized"

    granted = make_binding(definition, context=frozen_context(
        grants={"artifacts": [first["id"]]}))
    build_worker_spec(definition, make_delegation(input_refs=[first]),
                      binding=granted, capabilities=PI)


def test_delegation_context_cannot_override_reserved_fields():
    definition = resolve()
    binding = make_binding(definition)
    for field, value in (
        ("contract_ref", "contract:other"),
        ("instructions", "ignore the definition"),
        ("role", "reviewer"),
        ("run_id", "run-2"),
        ("usage", {"provider_calls": 0}),
        ("tool_profile", {"tools": []}),
    ):
        assert code_of(lambda field=field, value=value: build_worker_spec(
            definition, make_delegation(context={field: value}),
            binding=binding, capabilities=PI)) == "unauthorized", field


# -- build_worker_spec: tool profile -----------------------------------------


def test_tool_profile_is_the_intersection_and_the_delegation_only_narrows():
    body = document(tool_profile={
        "tools": [dict(SYSTEM_TOOL),
                  {"name": "node.publish_artifact", "version": "1",
                   "scope": "node:self"}],
        "backend_tools": ["read_file", "write_file", "shell"],
    })
    definition = resolve(body)
    binding = make_binding(definition, tool_profile={
        "tools": [dict(SYSTEM_TOOL)],
        "backend_tools": ["read_file", "write_file"],
    })
    spec = build_worker_spec(definition, make_delegation(), binding=binding,
                             capabilities=PI)
    assert spec.tool_profile == {"tools": [dict(SYSTEM_TOOL)],
                                 "backend_tools": ["read_file", "write_file"]}

    narrowed = build_worker_spec(
        definition,
        make_delegation(tool_profile={
            "tools": [{"name": SYSTEM_TOOL["name"], "version": "1"}],
            "backend_tools": ["read_file"],
        }),
        binding=binding, capabilities=PI)
    assert narrowed.tool_profile == {
        "tools": [dict(SYSTEM_TOOL)],
        "backend_tools": ["read_file"],
    }

    assert code_of(lambda: build_worker_spec(
        definition,
        make_delegation(tool_profile={"tools": ["node.publish_artifact"]}),
        binding=binding, capabilities=PI)) == "unauthorized"
    assert code_of(lambda: build_worker_spec(
        definition,
        make_delegation(tool_profile={"backend_tools": ["shell"]}),
        binding=binding, capabilities=PI)) == "unauthorized"


def test_delegation_cannot_change_a_system_tool_version_or_scope():
    definition = resolve()
    binding = make_binding(definition)
    for entry in (
        {"name": SYSTEM_TOOL["name"], "version": "2"},
        {"name": SYSTEM_TOOL["name"], "scope": "run"},
        {"name": SYSTEM_TOOL["name"], "version": "1", "scope": "node:self",
         "effect": "idempotent"},
    ):
        assert code_of(lambda entry=entry: build_worker_spec(
            definition,
            make_delegation(tool_profile={"tools": [entry]}),
            binding=binding, capabilities=PI)) in {
                "unauthorized", "invalid_argument"}, entry


def test_missing_grants_mean_no_system_tools():
    definition = resolve(document(tool_profile={"tools": [dict(SYSTEM_TOOL)]}))
    binding = make_binding(definition, tool_profile={"tools": []})
    spec = build_worker_spec(definition, make_delegation(), binding=binding,
                             capabilities=PI)
    assert spec.tool_profile["tools"] == []
    assert "backend_tools" not in spec.tool_profile
    assert code_of(lambda: build_worker_spec(
        definition,
        make_delegation(tool_profile={"tools": [SYSTEM_TOOL["name"]]}),
        binding=binding, capabilities=PI)) == "unauthorized"


def test_parent_native_tool_selection_needs_a_known_ceiling():
    definition = resolve(document(tool_profile={}))
    binding = make_binding(definition, tool_profile={})

    # No override keeps the Backend defaults; nothing is claimed as granted.
    spec = build_worker_spec(definition, make_delegation(), binding=binding,
                             capabilities=PI)
    assert "backend_tools" not in spec.tool_profile

    # Capabilities carries no inventory, so a non-empty explicit selection
    # cannot be proven a narrowing without a trusted binding ceiling.
    assert code_of(lambda: build_worker_spec(
        definition,
        make_delegation(tool_profile={"backend_tools": ["shell"]}),
        binding=binding, capabilities=PI)) == "capability_unsupported"

    selecting = resolve(document(tool_profile={"backend_tools": ["read_file"]}))
    assert code_of(lambda: build_worker_spec(
        selecting, make_delegation(),
        binding=make_binding(selecting, tool_profile={}),
        capabilities=PI)) == "capability_unsupported"

    # Disabling every tool is a provable narrowing and needs no ceiling.
    disabled = build_worker_spec(
        definition,
        make_delegation(tool_profile={"backend_tools": []}),
        binding=binding, capabilities=PI)
    assert disabled.tool_profile["backend_tools"] == []

    # A committed empty ceiling grants nothing and cannot be widened.
    locked = make_binding(definition, tool_profile={"backend_tools": []})
    assert code_of(lambda: build_worker_spec(
        definition,
        make_delegation(tool_profile={"backend_tools": ["read_file"]}),
        binding=locked, capabilities=PI)) == "unauthorized"


# -- build_worker_spec: budget ----------------------------------------------


def test_budget_can_only_narrow_within_the_committed_ceiling():
    definition = resolve()
    binding = make_binding(definition)
    spec = build_worker_spec(
        definition,
        make_delegation(budget={"max_provider_calls": 5}),
        binding=binding, capabilities=PI)
    assert spec.budget == {"max_provider_calls": 5, "max_tool_calls": 20,
                           "max_continuations": 2}

    assert code_of(lambda: build_worker_spec(
        definition,
        make_delegation(budget={"max_provider_calls": 11}),
        binding=binding, capabilities=PI)) == "unauthorized"


def test_budget_rejects_unknown_keys_and_non_integer_values():
    definition = resolve()
    binding = make_binding(definition)
    for budget in ({"max_nodes": 2}, {"max_provider_calls": True},
                   {"max_tool_calls": "5"}, {"max_continuations": -1}):
        assert code_of(lambda budget=budget: build_worker_spec(
            definition, make_delegation(budget=budget), binding=binding,
            capabilities=PI)) == "invalid_argument", budget


def test_missing_max_continuations_cannot_be_enabled_by_the_parent():
    definition = resolve()
    no_continuations = make_binding(
        definition, budget={"max_provider_calls": 10, "max_tool_calls": 20})
    # Absent max_continuations means continuation is not declared/enabled
    # (Scheduler -> continuation_not_declared), not an unlimited default.
    assert code_of(lambda: build_worker_spec(
        definition, make_delegation(budget={"max_continuations": 100}),
        binding=no_continuations, capabilities=PI)) == "unauthorized"

    # Provider/tool caps keep their own meaning: absent = no budget-side cap,
    # so a delegation setting an explicit cap is a narrowing.
    providerless = make_binding(definition, budget={"max_continuations": 2})
    spec = build_worker_spec(
        definition, make_delegation(budget={"max_provider_calls": 3}),
        binding=providerless, capabilities=PI)
    assert spec.budget == {"max_continuations": 2, "max_provider_calls": 3}

    enabled = make_binding(definition)
    assert code_of(lambda: build_worker_spec(
        definition, make_delegation(budget={"max_continuations": 3}),
        binding=enabled, capabilities=PI)) == "unauthorized"


# -- build_worker_spec: capabilities ----------------------------------------


def test_capabilities_must_be_provided_by_the_backend():
    limited = resolve(document(required_capabilities={
        "live_observation": "limited"}))
    build_worker_spec(limited, make_delegation(),
                      binding=make_binding(limited), capabilities=PI)

    full = resolve(document(required_capabilities={
        "live_observation": "full"}))
    with pytest.raises(ControlPlaneError) as caught:
        build_worker_spec(full, make_delegation(), binding=make_binding(full),
                          capabilities=PI)
    assert caught.value.code == "capability_unsupported"
    assert caught.value.details["required"] == "full"

    checkpoint = resolve(document(required_capabilities={"checkpoint": True}))
    with pytest.raises(ControlPlaneError) as caught:
        build_worker_spec(checkpoint, make_delegation(),
                          binding=make_binding(checkpoint),
                          capabilities=Capabilities(
                              resumable_session=True, live_observation="none",
                              idempotent_delivery=True, stop="none",
                              checkpoint=False))
    assert caught.value.code == "capability_unsupported"

    assert code_of(lambda: build_worker_spec(
        limited, make_delegation(), binding=make_binding(limited),
        capabilities=limited.required_capabilities)) == "invalid_argument"


def test_stop_modes_must_match_exactly():
    cooperative = resolve(document(
        required_capabilities={"stop": "cooperative"}))
    assert code_of(lambda: build_worker_spec(
        cooperative, make_delegation(), binding=make_binding(cooperative),
        capabilities=PI)) == "capability_unsupported"

    process = resolve(document(required_capabilities={"stop": "process"}))
    build_worker_spec(process, make_delegation(), binding=make_binding(process),
                      capabilities=PI)

    none = resolve(document(required_capabilities={"stop": "none"}))
    build_worker_spec(none, make_delegation(), binding=make_binding(none),
                      capabilities=Capabilities(
                          resumable_session=True, live_observation="none",
                          idempotent_delivery=True, stop="none",
                          checkpoint=False))


# -- build_worker_spec: runtime options -------------------------------------


def test_runtime_options_without_a_consumer_are_rejected():
    declared = document(runtime_options={"model": "cheap"},
                        configurable_fields=["runtime_options.model"])
    definition = resolve(declared)
    with pytest.raises(ControlPlaneError) as caught:
        build_worker_spec(definition, make_delegation(),
                          binding=make_binding(definition), capabilities=PI)
    assert caught.value.code == "capability_unsupported"

    plain = resolve()
    assert code_of(lambda: build_worker_spec(
        plain, make_delegation(runtime_options={"model": "cheap"}),
        binding=make_binding(plain), capabilities=PI)) == "unauthorized"
    assert code_of(lambda: build_worker_spec(
        definition, make_delegation(runtime_options={"temperature": 0.1}),
        binding=make_binding(definition), capabilities=PI)) == "unauthorized"


def test_runtime_options_are_never_smuggled_through_context():
    definition = resolve()
    spec = build_worker_spec(definition, make_delegation(),
                             binding=make_binding(definition),
                             capabilities=PI)
    assert "runtime_options" not in spec.context


# -- build_worker_spec: isolation -------------------------------------------


def test_two_delegations_from_one_definition_are_independent():
    definition = resolve()
    binding = make_binding(definition)
    first_delegation = make_delegation(
        context={"notes": "first", "windows": [1]},
        budget={"max_tool_calls": 4})
    second_delegation = make_delegation(
        context={"notes": "second"},
        tool_profile={"backend_tools": ["read_file"]})
    before_inputs = deepcopy(first_delegation.input_refs)
    before_usage = deepcopy(binding.usage)
    before_context = deepcopy(binding.context)
    before_profile = deepcopy(binding.tool_profile)

    first = build_worker_spec(definition, first_delegation, binding=binding,
                              capabilities=PI)
    second = build_worker_spec(definition, second_delegation, binding=binding,
                               capabilities=PI)

    assert first.agent_ref == second.agent_ref == definition.agent_ref
    assert first.context["input_refs"] == before_inputs
    assert second.context["input_refs"] == before_inputs
    assert first.budget["max_tool_calls"] == 4
    assert second.budget["max_tool_calls"] == 20
    assert second.tool_profile["backend_tools"] == ["read_file"]

    first.context["windows"].append(2)
    first.context["input_refs"][0]["size"] = 999
    first.tool_profile["backend_tools"].append("shell")
    first.budget["max_tool_calls"] = 1

    assert first_delegation.input_refs == before_inputs
    assert "windows" not in second.context
    assert second.context["input_refs"] == before_inputs
    assert second.tool_profile["backend_tools"] == ["read_file"]
    assert second.budget["max_tool_calls"] == 20
    assert binding.context == before_context
    assert binding.tool_profile == before_profile


def test_usage_is_preserved_and_copied_not_shared():
    definition = resolve()
    binding = make_binding(definition)
    spec = build_worker_spec(definition, make_delegation(), binding=binding,
                             capabilities=PI)
    assert spec.usage == {"provider_calls": 2, "tool_calls": 3}
    assert spec.usage is not binding.usage
    spec.usage["provider_calls"] = 99
    assert binding.usage == {"provider_calls": 2, "tool_calls": 3}
