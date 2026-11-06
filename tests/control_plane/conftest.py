"""Codex test framework: enforce isolated imports and build explicit Graph /2 data.

Qoder may add runtime fixtures below. Do not relax the source-path check or
replace these definitions with Graph /1 compatibility data.
"""
from pathlib import Path
from copy import deepcopy
import pytest
import node_runtime

ISOLATED_ROOT = Path(__file__).resolve().parents[2]
assert Path(node_runtime.__file__).resolve().parent.parent == ISOLATED_ROOT, (
    'Tests imported the main/installed runtime instead of the isolated Graph /2 implementation',
    node_runtime.__file__, str(ISOLATED_ROOT))


def make_graph(*, definition_id='test-graph', policy='adaptive', stages=None):
    from node_runtime.control_plane.tools import TOOL_SPECS
    profile = {'tools': [{'name': s.name, 'version': s.version, 'scope': s.scope}
                         for s in TOOL_SPECS.values()], 'backend_tools': []}
    def agent():
        return {'format': 'agentgenome-agent-definition/1', 'backend_ref': 'script',
                'instructions': 'Execute the assigned task using only the committed protocol.',
                'tool_profile': deepcopy(profile)}
    def task(receipt=False):
        return {'format': 'agentgenome-task-definition/1',
                'goal': 'Review the bound candidate.' if receipt else 'Complete the assigned step.',
                'agent_ref': 'judge' if receipt else 'doer',
                'completion_kind': 'review_receipt' if receipt else 'candidate',
                'contract': {'input_schema': {'type': 'array'},
                             'output_schema': {'type': 'object' if receipt else 'array'},
                             'acceptance': 'Satisfy the declared task and report verifiable evidence.'}}
    return {'format': 'agentgenome-graph-definition/2', 'definition_id': definition_id,
            'base_revision': 0, 'policy': policy,
            'budget_limits': {'max_nodes': 30, 'max_depth': 5, 'max_rounds': 5},
            'input_schema': {'required': ['dataset']},
            'agents': {'doer': agent(), 'judge': agent()},
            'tasks': {'work': task(), 'check': task(True)},
            'nodes': deepcopy(stages) if stages is not None else {
                'first': {'task_ref': 'work', 'reviewer_ref': 'check', 'needs': [],
                          'inputs': {'dataset': {'run_input': 'dataset'}}}}}


# Qoder runtime fixture: real Api/Scheduler/Controller + scripted Agent.
@pytest.fixture
def cluster(tmp_path):
    from helpers import Cluster, accepting_agent
    instance = Cluster(tmp_path / 'data', None)
    instance.backend.agent = accepting_agent(instance.api)
    return instance
