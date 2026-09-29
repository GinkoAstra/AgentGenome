"""D05: reserved tool retry must execute the original action before new Pi choices."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from sop.authoring import Library, clean_definition
from sop.capabilities import Registry
from sop.common import ExecutionError
from sop.service import application

FIXTURES = Path(__file__).resolve().parents[2]/'tests/fixtures/first-release'


def definition():
    result = clean_definition()
    result['id'] = 'retry-before-agent-choice'
    result['steps'] = [{'id': 'clean', 'kind': 'pi', 'task': 'orders.clean',
                        'inputs': {'source_file': '$input.source_file', 'rule': '$input.rule'}}]
    result['outputs'] = {'cleaned': '$steps.clean.cleaned'}
    result['recovery'] = [{'id': 'retry-original', 'phase': 'execution', 'code': 'temporary_read',
                           'action': 'retry', 'max_attempts': 1}]
    return result


class BeforeDispatchFailure(Registry):
    def __init__(self):
        self.attempts = []

    def execute(self, capability, inputs, work_dir):
        self.attempts.append((capability, dict(inputs)))
        if len(self.attempts) == 1:
            raise ExecutionError('temporary_read', 'Trusted evidence: no dispatch occurred', not_dispatched=True)
        return super().execute(capability, inputs, work_dir)


class ChangeChoiceAgent:
    max_model_calls_per_request = 1
    identity = {'backend': 'scripted', 'purpose': 'retry_admission_protocol_test'}

    def __init__(self, registry):
        self.registry = registry
        self.contexts = []
        self.attempts_at_call = []

    def respond_with_tools(self, context, execute):
        self.contexts.append(deepcopy(context))
        self.attempts_at_call.append(len(self.registry.attempts))
        if len(self.contexts) == 1:
            execute('orders.normalize', {'source_file': context['inputs']['source_file']})
            raise AssertionError('First action must be interrupted before dispatch')
        if context.get('child_result'):
            return {'kind': 'candidate_result', 'outputs': context['child_result']}
        # A fresh model may choose delegation, but cannot use this to skip the
        # controller's previously reserved retry of the exact original action.
        return {'kind': 'need_subtask', 'task': 'orders.clean', 'inputs': context['inputs'],
                'reason': 'A different decomposition is proposed after recovery'}


class ToolRetryAdmissionTests(unittest.TestCase):
    def start(self, directory, registry, agent, hook=None):
        runtime = application(Path(directory), registry=registry, agent=agent,
                              library=Library([clean_definition()]), hook=hook)
        method = definition()
        run = runtime.start(method, {'source_file': FIXTURES/'batch-01/input.csv', 'rule': 'highest_revision'},
                            capabilities=method['capabilities'])
        return runtime, run

    def assert_original_retry_precedes_new_choice(self, runtime, done, registry, agent):
        self.assertEqual(done['state'], 'succeeded', done.get('error'))
        self.assertEqual(agent.attempts_at_call[:2], [0, 2])
        self.assertEqual(registry.attempts[0], registry.attempts[1])
        self.assertEqual(registry.attempts[1][0], 'orders.normalize')
        receipts = agent.contexts[1]['tool_receipts']
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]['capability'], 'orders.normalize')
        operation = done['operations'][receipts[0]['operation']]
        self.assertEqual(operation['status'], 'committed')
        self.assertEqual(operation['origin'], 'agent')
        self.assertEqual(len(done['recoveries']), 1)
        self.assertEqual(done['usage']['model_calls'], 3)
        events = runtime.store.events(done['id'])
        reserved = next(i for i, event in enumerate(events) if event['kind'] == 'recovery_reserved')
        next_agent = next(i for i, event in enumerate(events) if i > reserved and event['kind'] == 'agent_dispatched')
        retried = [event for event in events[reserved+1:next_agent] if event['kind'] == 'operation_committed']
        self.assertEqual(len(retried), 1)
        self.assertEqual(retried[0]['data']['operation'], operation['id'])

    def test_agent_cannot_skip_reserved_retry_by_requesting_a_different_subgraph(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = BeforeDispatchFailure()
            agent = ChangeChoiceAgent(registry)
            runtime, run = self.start(directory, registry, agent)
            done = runtime.advance(run['id'])
            self.assert_original_retry_precedes_new_choice(runtime, done, registry, agent)

    def test_crash_after_recovery_reservation_preserves_original_arguments_and_budget(self):
        class Crash(BaseException):
            pass
        def hook(event, run):
            if event == 'recovery_reserved':
                raise Crash()
        with tempfile.TemporaryDirectory() as directory:
            registry = BeforeDispatchFailure()
            agent = ChangeChoiceAgent(registry)
            runtime, run = self.start(directory, registry, agent, hook)
            with self.assertRaises(Crash):
                runtime.advance(run['id'])
            saved = runtime.store.load(run['id'])
            self.assertEqual(len(agent.contexts), 1)
            self.assertEqual(saved['usage']['model_calls'], 1)
            self.assertEqual(saved['usage']['actions'], 2)  # original intent + reserved recovery
            self.assertEqual(len(saved['recoveries']), 1)
            self.assertEqual(len(registry.attempts), 1)
            original = next(iter(saved['operations'].values()))
            self.assertEqual(original['status'], 'not_dispatched')
            runtime = application(Path(directory), registry=registry, agent=agent,
                                  library=Library([clean_definition()]))
            done = runtime.advance(run['id'])
            self.assert_original_retry_precedes_new_choice(runtime, done, registry, agent)
            receipt = agent.contexts[1]['tool_receipts'][0]
            self.assertEqual(done['operations'][receipt['operation']]['inputs'], original['inputs'])
            self.assertEqual(done['operations'][receipt['operation']]['definition'], original['definition'])
            self.assertEqual(done['tasks'][done['root']]['recovery_counts'], {'retry-original': 1})


if __name__ == '__main__':
    unittest.main()
