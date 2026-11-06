"""Pure composition of the effective WorkerSpec.

The Scheduler proposes the binding and the Api recomputes it from the same
authoritative inputs (frozen Node material, pinned Task/Agent, declared
delegation and trusted Backend capabilities). Keeping that construction in one
pure function means the two call sites cannot drift and the Api never invents
execution intent the Scheduler did not submit.
"""
from __future__ import annotations

from typing import Any, Mapping

from . import resources as R
from .agent_definitions import (AgentDefinition, Capabilities, TaskDelegation,
                                build_worker_spec)
from .backends.base import WorkerSpec
from .task_definitions import TaskDefinition


def compose_worker_spec(
    *,
    run_id: str,
    node: Mapping[str, Any],
    worker_id: str,
    task: TaskDefinition,
    agent: AgentDefinition,
    capabilities: Capabilities | None,
    candidate: Mapping[str, Any] | None,
    epoch: int,
    desired: str,
    run_budget: Mapping[str, Any],
    contract: Mapping[str, Any] | None = None,
    upper_contract_ref: str | None = None,
    revision_descriptor: Mapping[str, Any] | None = None,
) -> WorkerSpec:
    """Build the deterministic execution spec both callers must agree on.

    The current Task's ``contract.input_schema`` describes the frozen Node
    inputs, so an incompatible input set is refused here - after freezing and
    before any execution binding exists. Ordinary and review_receipt Tasks use
    the same check; the Scheduler fails the Node as unexecutable and the Api
    refuses a directly submitted binding.
    """
    R.require(isinstance(task, TaskDefinition), "invalid_argument",
              "task must be a resolved TaskDefinition")
    R.require(isinstance(agent, AgentDefinition), "invalid_argument",
              "agent must be a resolved AgentDefinition")
    R.require(task.task_ref == node["template_ref"], "invalid_argument",
              "the task definition does not match the node",
              task_ref=task.task_ref, template_ref=node["template_ref"])
    R.require(isinstance(capabilities, Capabilities), "capability_unsupported",
              "the host has no trusted capabilities for this backend",
              backend_ref=getattr(agent, "backend_ref", None))
    R.require(desired in R.WORKER_DESIRED, "invalid_argument",
              "unknown worker desired state")
    R.require_int(epoch, "epoch", minimum=0)
    closure = node.get("closure")
    role = "reviewer" if closure else "worker"
    input_refs = node.get("input_refs")
    if input_refs is None:
        R.fail("candidate_mismatch", "the node inputs are not frozen",
               node_id=node["node_id"])
    effective_contract = dict(contract if contract is not None else task.contract)
    R.validate_schema_value(effective_contract["input_schema"], input_refs,
                            "input_refs")
    grants = sorted({ref["id"] for ref in input_refs})
    upper_ref = upper_contract_ref or node["template_ref"]
    context: dict[str, Any] = {
        "role": role,
        "run_id": run_id,
        "node_id": node["node_id"],
        "worker_id": worker_id,
        "backend_ref": agent.backend_ref,
        "agent_ref": agent.agent_ref,
        "template_ref": node["template_ref"],
        "contract_ref": node["contract_ref"],
        "upper_contract_ref": upper_ref,
        "input_refs": input_refs,
        "reviewer_ref": node["reviewer_ref"],
        "grants": {"artifacts": grants},
    }
    if revision_descriptor is not None:
        context["acceptance"] = {
            "contract_ref": node["contract_ref"],
            "upper_contract_ref": upper_ref,
            "criteria_ref": dict(revision_descriptor["criteria_ref"]),
        }
        source_ref = revision_descriptor.get("source_ref")
        if source_ref is not None:
            # The verified revision source is part of the frozen version; a
            # fresh execution must be able to re-resolve its own contract.
            context["acceptance"]["source_ref"] = dict(source_ref)
    if closure:
        if candidate is None:
            R.fail("candidate_mismatch",
                   "the review closure no longer names a committed candidate",
                   node_id=node["node_id"])
        candidate_ids = [ref["id"] for ref in (candidate.get("output_refs") or [])]
        summary_ref = candidate.get("summary_ref")
        if summary_ref is not None:
            candidate_ids.append(summary_ref["id"])
        context.update({
            "target_node_id": closure["target_node_id"],
            "candidate_id": closure["candidate_id"],
            "candidate_round": candidate["round"],
            "review_candidate_ids": candidate_ids,
        })
    if task.progress is not None:
        # Frozen Goal policy travels with the effective spec so a real model
        # sees when a structured progress evaluation is mandatory.
        context["progress"] = {
            "no_improvement_limit": int(task.progress["no_improvement_limit"]),
        }
    ceiling_tools = R.REVIEWER_TOOLS if closure else R.WORKER_TOOLS
    ceiling: dict[str, Any] = {"tools": [dict(entry) for entry in ceiling_tools]}
    if capabilities.backend_tools is not None:
        ceiling["backend_tools"] = list(capabilities.backend_tools)
    binding = WorkerSpec(
        worker_id=worker_id,
        node_id=node["node_id"],
        epoch=epoch,
        intent_seq=1,
        backend_ref=agent.backend_ref,
        agent_ref=agent.agent_ref,
        desired=desired,
        run_id=run_id,
        context=context,
        tool_profile=ceiling,
        budget=dict(run_budget),
        usage={"provider_calls": 0, "tool_calls": 0},
        required_capabilities={},
    )
    declared = node.get("delegation") or {}
    delegation = TaskDelegation(
        node_id=node["node_id"],
        contract_ref=node["contract_ref"],
        input_refs=input_refs,
        context=declared.get("context", {}),
        tool_profile=declared.get("tool_profile"),
        budget=declared.get("budget", {}),
        runtime_options=declared.get("runtime_options", {}),
    )
    return build_worker_spec(agent, delegation, binding=binding,
                             capabilities=capabilities)
