"""Pure scheduling policies: fixed and adaptive expansion planning.

Policies see frozen definition data, the current Node snapshot and a proposed
Expansion. They decide whether the Scheduler may commit that expansion; they
never call the model, read artifacts or execute graph work. The Controller is
unaware of which policy produced a plan.
"""
from __future__ import annotations

from typing import Any, Mapping

from .resources import ControlPlaneError, fail, require


class ExpansionPolicy:
    name = "abstract"

    def validate_expansion(self, definition: Mapping[str, Any],
                           parent: Mapping[str, Any],
                           proposal: Mapping[str, Any]) -> None:
        raise NotImplementedError


class FixedPolicy(ExpansionPolicy):
    """Only the expansion rule frozen in the definition node is allowed."""

    name = "fixed"

    def validate_expansion(self, definition: Mapping[str, Any],
                           parent: Mapping[str, Any],
                           proposal: Mapping[str, Any]) -> None:
        """Compare one proposed Expansion with its pinned TaskDefinition.

        The rule lives in the approved Task content version that the Node's
        ``template_ref`` names, never in a Graph position alias. A count rule
        fixes only the child count; an explicit rule fixes every child's
        identity, task selection, reviewer, dependencies and declared
        narrowing. Input bindings stay run data and are frozen by the API.
        """
        tasks = definition.get("task_definitions") or {}
        task = tasks.get(parent["template_ref"])
        if task is None:
            fail("not_found", "the expansion parent task is not pinned",
                 task_ref=parent["template_ref"], node_id=parent["node_id"])
        rule = task.expansion
        if rule is None:
            fail("capability_unsupported",
                 "the pinned task declares no fixed expansion",
                 task_ref=parent["template_ref"], node_id=parent["node_id"])
        require(proposal["join_rule"] == rule.get("join_rule", "all"),
                "unauthorized",
                "the expansion join_rule differs from the pinned task",
                expected=rule.get("join_rule", "all"),
                actual=proposal["join_rule"])
        declared = rule["children"]
        children = proposal["children"]
        if isinstance(declared, int):
            require(len(children) == declared, "unauthorized",
                    "the fixed expansion declares a different child count",
                    expected=declared, actual=len(children))
            return
        expected = {child["local_child_id"]: child for child in declared}
        actual = {child["local_child_id"]: child for child in children}
        require(set(actual) == set(expected), "unauthorized",
                "the fixed expansion children differ from the pinned task",
                missing=sorted(set(expected) - set(actual)),
                extra=sorted(set(actual) - set(expected)))
        for local, declaration in expected.items():
            child = actual[local]
            expected_task = (declaration.get("template_ref")
                             or parent["template_ref"])
            selected_task = (child.get("template_ref")
                             or parent["template_ref"])
            require(selected_task == expected_task, "unauthorized",
                    "the fixed expansion child selects a different task",
                    local_child_id=local, expected=expected_task,
                    actual=selected_task)
            require(child.get("contract_ref") in (None, selected_task),
                    "unauthorized",
                    "the fixed expansion child contract_ref must equal its task",
                    local_child_id=local, task_ref=selected_task,
                    contract_ref=child.get("contract_ref"))
            expected_reviewer = declaration.get("reviewer_ref", "parent")
            selected_reviewer = child.get("reviewer_ref") or "parent"
            require(selected_reviewer == expected_reviewer, "unauthorized",
                    "the fixed expansion child selects a different reviewer",
                    local_child_id=local, expected=expected_reviewer,
                    actual=selected_reviewer)
            require(sorted(child.get("dependencies") or []) ==
                    sorted(declaration.get("dependencies") or []),
                    "unauthorized",
                    "the fixed expansion child has different dependencies",
                    local_child_id=local)
            require((child.get("delegation") or {}) ==
                    (declaration.get("delegation") or {}), "unauthorized",
                    "the fixed expansion child has different delegation",
                    local_child_id=local)
            declared_bindings = declaration.get("input_bindings") or {}
            missing = sorted(set(declared_bindings) -
                             set(child.get("input_bindings") or {}))
            require(not missing, "unauthorized",
                    "the fixed expansion child omits a declared input binding",
                    local_child_id=local, missing=missing)



class AdaptivePolicy(ExpansionPolicy):
    """Accept the Agent proposal; budgets and graph validation stay in the API."""

    name = "adaptive"

    def validate_expansion(self, definition: Mapping[str, Any],
                           parent: Mapping[str, Any],
                           proposal: Mapping[str, Any]) -> None:
        return None


DEFAULT_POLICIES: dict[str, ExpansionPolicy] = {
    "fixed": FixedPolicy(),
    "adaptive": AdaptivePolicy(),
}


def policy_for(name: str, policies: Mapping[str, ExpansionPolicy] | None = None) -> ExpansionPolicy:
    table = dict(DEFAULT_POLICIES)
    if policies:
        table.update(policies)
    policy = table.get(name)
    if policy is None:
        raise ControlPlaneError("capability_unsupported",
                                f"no scheduling policy is registered as {name!r}",
                                policy=str(name)[:64])
    return policy


