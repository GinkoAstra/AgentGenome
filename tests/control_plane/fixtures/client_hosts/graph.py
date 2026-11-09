"""One frozen Graph /2 document shared by every native-host acceptance test."""
from __future__ import annotations

from copy import deepcopy

DEFINITION_ID = "u03-host-native"


def graph_document(definition_id: str = DEFINITION_ID) -> dict:
    from node_runtime.control_plane.tools import TOOL_SPECS

    profile = {"tools": [{"name": spec.name, "version": spec.version,
                          "scope": spec.scope}
                         for spec in TOOL_SPECS.values()],
               "backend_tools": []}

    def agent() -> dict:
        return {"format": "agentgenome-agent-definition/1",
                "backend_ref": "script",
                "instructions": "Execute the assigned task using only the "
                                "committed protocol.",
                "tool_profile": deepcopy(profile)}

    def task(receipt: bool = False) -> dict:
        return {"format": "agentgenome-task-definition/1",
                "goal": ("Review the bound candidate." if receipt
                         else "Complete the assigned step."),
                "agent_ref": "judge" if receipt else "doer",
                "completion_kind": "review_receipt" if receipt else "candidate",
                "contract": {
                    "input_schema": {"type": "array"},
                    "output_schema": {"type": "object" if receipt else "array"},
                    "acceptance": "Satisfy the declared task and report "
                                  "verifiable evidence."}}

    return {
        "format": "agentgenome-graph-definition/2",
        "definition_id": definition_id,
        "base_revision": 0,
        "policy": "adaptive",
        "budget_limits": {"max_nodes": 30, "max_depth": 5, "max_rounds": 5},
        "input_schema": {"required": ["dataset"]},
        "agents": {"doer": agent(), "judge": agent()},
        "tasks": {"work": task(), "check": task(True)},
        "nodes": {
            "first": {"task_ref": "work", "reviewer_ref": "check", "needs": [],
                      "inputs": {"dataset": {"run_input": "dataset"}}},
        },
    }


RUN_INPUTS = {"dataset": ["u03-host"]}
