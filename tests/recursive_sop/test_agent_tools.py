"""Host-controlled Pi business tool tests using deterministic protocol adapters.

These exercise actual capabilities, durable operations, and independent checkers.
They do not constitute real-model or algorithm-benefit evidence.
"""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from sop.authoring import Library, clean_definition, root_definition
from sop.capabilities import Registry
from sop.common import ExecutionError, SopError, read_json
from sop.service import application

FIXTURES = Path(__file__).resolve().parents[2] / 'tests/fixtures/first-release'


class Crash(BaseException):
    pass


def direct_clean_definition():
    definition = clean_definition()
    definition['id'] = 'direct-tool-clean'
    definition['steps'] = [{'id': 'clean', 'kind': 'pi', 'task': 'orders.clean',
                            'inputs': {'source_file': '$input.source_file', 'rule': '$input.rule'}}]
    definition['outputs'] = {'cleaned': '$steps.clean.cleaned'}
    return definition


class RecordingRegistry(Registry):
    def __init__(self):
        self.executed = []

    def execute(self, capability, inputs, work_dir):
        self.executed.append(capability)
        return super().execute(capability, inputs, work_dir)


class DirectAgent:
    max_model_calls_per_request = 1
    identity = {'backend': 'scripted-tools', 'purpose': 'protocol_test_double'}

    def __init__(self, action=None):
        self.action = action
        self.contexts = []
        self.receipts = []

    def respond_with_tools(self, context, execute):
        self.contexts.append(deepcopy(context))
        if self.action:
            return self.action(context, execute)
        normalized = execute('orders.normalize', {'source_file': context['inputs']['source_file']})
        cleaned = execute('orders.deduplicate', {'normalized': normalized['outputs']['normalized'],
                                                 'rule': context['inputs']['rule']})
        self.receipts.extend([normalized, cleaned])
        return {'kind': 'candidate_result', 'outputs': cleaned['outputs']}


class AgentToolTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='sop-agent-tools-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def app(self, *, agent=None, registry=None, hook=None, directory='data'):
        # An empty library proves the direct tool route needs no existing/new child.
        return application(self.root / directory, library=Library([]), agent=agent or DirectAgent(),
                           registry=registry or RecordingRegistry(), hook=hook)

    def start(self, runtime, *, batch='batch-01', definition=None):
        definition = definition or direct_clean_definition()
        return runtime.start(definition, {'source_file': FIXTURES / batch / 'input.csv', 'rule': 'highest_revision'},
                             capabilities=definition['capabilities'])

    def assert_clean(self, runtime, run, batch='batch-01'):
        self.assertEqual(run['state'], 'succeeded', run.get('error'))
        root = run['tasks'][run['root']]
        self.assertTrue(root['verification']['passed'])
        self.assertEqual(runtime.store.path(root['outputs']['cleaned']).read_bytes(),
                         (FIXTURES / batch / 'expected/cleaned.csv').read_bytes())
        self.assertEqual(run['calls'], {})
        self.assertEqual(len(run['tasks']), 1)
        self.assertEqual(run['publication'], 'unpublished')

    def test_both_batches_run_direct_tools_and_parent_report_without_child_graphs(self):
        for batch in ('batch-01', 'batch-02'):
            with self.subTest(batch=batch):
                agent, registry = DirectAgent(), RecordingRegistry()
                runtime = self.app(agent=agent, registry=registry, directory=batch)
                run = runtime.advance(self.start(runtime, batch=batch, definition=root_definition())['id'])
                self.assert_clean(runtime, run, batch)
                root = run['tasks'][run['root']]
                for name in ('summary', 'quality'):
                    actual = runtime.store.path(root['outputs'][name])
                    expected = FIXTURES / batch / 'expected' / (name + ('.json' if name == 'quality' else '.csv'))
                    self.assertEqual(read_json(actual), read_json(expected)) if name == 'quality' else self.assertEqual(actual.read_bytes(), expected.read_bytes())
                self.assertEqual(registry.executed, ['orders.profile', 'orders.normalize', 'orders.deduplicate', 'orders.summarize'])
                self.assertEqual(len(agent.contexts), 1)
                self.assertEqual(set(agent.contexts[0]['catalog']), {'orders.normalize', 'orders.deduplicate'})
                self.assertEqual(agent.contexts[0]['contract']['capabilities'], ['orders.deduplicate', 'orders.normalize'])
                self.assertEqual([op['origin'] for op in run['operations'].values()], ['fixed', 'agent', 'agent', 'fixed'])

    def test_task_scope_denies_profile_despite_parent_run_grant(self):
        def action(context, execute):
            return execute('orders.profile', {'source_file': context['inputs']['source_file']})

        registry = RecordingRegistry()
        runtime = self.app(agent=DirectAgent(action), registry=registry)
        run = runtime.advance(self.start(runtime, definition=root_definition())['id'])
        self.assertEqual(run['state'], 'failed')
        self.assertEqual(run['error']['code'], 'permission_denied')
        self.assertIn('orders.profile', run['capabilities'])
        self.assertEqual(registry.executed, ['orders.profile'])  # Only the declared parent step.
        self.assertEqual(len(run['operations']), 1)

    def test_another_run_artifact_cannot_replace_bound_source(self):
        runtime = self.app()
        other = self.start(runtime, batch='batch-02')
        foreign = other['tasks'][other['root']]['inputs']['source_file']
        runtime.agent = DirectAgent(lambda context, execute: execute('orders.normalize', {'source_file': foreign}))
        run = runtime.advance(self.start(runtime)['id'])
        self.assertEqual(run['state'], 'failed')
        self.assertEqual(run['error']['code'], 'invalid_artifact')
        self.assertEqual(run['operations'], {})
        self.assertEqual(runtime.registry.executed, [])
        self.assertEqual(runtime.store.load(other['id'])['state'], 'ready')

    def test_wrong_role_artifact_and_bound_rule_substitution_are_rejected(self):
        for case in ('wrong-role', 'wrong-rule'):
            with self.subTest(case=case):
                def action(context, execute):
                    if case == 'wrong-role':
                        normalized = context['inputs']['source_file']
                    else:
                        normalized = execute('orders.normalize', {'source_file': context['inputs']['source_file']})['outputs']['normalized']
                    return execute('orders.deduplicate', {'normalized': normalized,
                                                           'rule': 'first' if case == 'wrong-rule' else context['inputs']['rule']})

                runtime = self.app(agent=DirectAgent(action), directory=case)
                run = runtime.advance(self.start(runtime)['id'])
                self.assertEqual(run['state'], 'failed')
                self.assertEqual(run['error']['code'], 'invalid_artifact' if case == 'wrong-role' else 'invalid_rule')
                self.assertNotIn('orders.deduplicate', runtime.registry.executed)
                self.assertEqual(run['tasks'][run['root']]['inputs']['rule'], 'highest_revision')

    def test_same_tool_arguments_return_same_receipt_without_repeating_execution(self):
        receipts = []

        def action(context, execute):
            args = {'source_file': context['inputs']['source_file']}
            first = execute('orders.normalize', args)
            duplicate = execute('orders.normalize', deepcopy(args))
            dedup_args = {'normalized': first['outputs']['normalized'], 'rule': context['inputs']['rule']}
            clean = execute('orders.deduplicate', dedup_args)
            clean_duplicate = execute('orders.deduplicate', deepcopy(dedup_args))
            receipts.extend([first, duplicate, clean, clean_duplicate])
            return {'kind': 'candidate_result', 'outputs': clean_duplicate['outputs']}

        runtime = self.app(agent=DirectAgent(action))
        run = runtime.advance(self.start(runtime)['id'])
        self.assert_clean(runtime, run)
        self.assertEqual(runtime.registry.executed, ['orders.normalize', 'orders.deduplicate'])
        self.assertEqual(len(run['operations']), 2)
        for first, second in ((receipts[0], receipts[1]), (receipts[2], receipts[3])):
            self.assertFalse(first['reused'])
            self.assertTrue(second['reused'])
            self.assertEqual(first['operation'], second['operation'])
            self.assertEqual(first['outputs'], second['outputs'])

    def test_tool_candidate_crash_keeps_pi_program_counter_and_resumes_without_tool_replay(self):
        def hook(kind, run):
            if kind == 'operation_candidate':
                raise Crash()

        runtime = self.app(hook=hook)
        run = self.start(runtime)
        with self.assertRaises(Crash):
            runtime.advance(run['id'])
        saved = runtime.store.load(run['id'])
        root = saved['tasks'][saved['root']]
        self.assertEqual(root['pc'], 0)
        self.assertEqual(root['state'], 'committing_step')
        self.assertEqual(root['steps'], {})
        operation = next(iter(saved['operations'].values()))
        self.assertEqual(operation['origin'], 'agent')
        self.assertEqual(operation['status'], 'candidate')

        class NoNormalizeReplay(RecordingRegistry):
            def execute(self, capability, inputs, work_dir):
                if capability == 'orders.normalize':
                    raise AssertionError('Durable normalization candidate was executed twice')
                return super().execute(capability, inputs, work_dir)

        agent = DirectAgent()
        runtime = self.app(agent=agent, registry=NoNormalizeReplay())
        done = runtime.advance(run['id'])
        self.assert_clean(runtime, done)
        self.assertEqual(runtime.registry.executed, ['orders.deduplicate'])
        self.assertEqual(len(agent.contexts[0]['tool_receipts']), 1)
        self.assertTrue(agent.receipts[0]['reused'])
        self.assertEqual(agent.receipts[0]['operation'], operation['id'])
        self.assertEqual(done['tasks'][done['root']]['pc'], 1)
        self.assertEqual(len(done['operations']), 2)

    def test_intent_crash_has_unknown_effect_and_never_replays_tool(self):
        def hook(kind, run):
            if kind == 'operation_intent':
                raise Crash()

        runtime = self.app(hook=hook)
        run = self.start(runtime)
        with self.assertRaises(Crash):
            runtime.advance(run['id'])
        self.assertEqual(runtime.registry.executed, [])
        runtime = self.app()
        resumed = runtime.advance(run['id'])
        self.assertEqual(resumed['state'], 'effect_unknown')
        self.assertEqual(len(resumed['operations']), 1)
        self.assertEqual(runtime.registry.executed, [])
        self.assertEqual(runtime.agent.contexts, [])
        self.assertEqual(runtime.advance(run['id']), resumed)

    def test_cancellation_between_callbacks_prevents_following_dispatch(self):
        runtime = self.app()
        run = self.start(runtime)

        def action(context, execute):
            normalized = execute('orders.normalize', {'source_file': context['inputs']['source_file']})
            runtime.store.request_cancel(run['id'])
            return execute('orders.deduplicate', {'normalized': normalized['outputs']['normalized'],
                                                   'rule': context['inputs']['rule']})

        runtime.agent = DirectAgent(action)
        done = runtime.advance(run['id'])
        self.assertEqual(done['state'], 'cancelled')
        self.assertEqual(runtime.registry.executed, ['orders.normalize'])
        self.assertEqual(len(done['operations']), 1)
        self.assertEqual(done['tasks'][done['root']]['pc'], 0)

    def test_owned_but_wrong_final_candidate_is_rejected_by_independent_checker(self):
        def action(context, execute):
            normalized = execute('orders.normalize', {'source_file': context['inputs']['source_file']})
            return {'kind': 'candidate_result', 'outputs': {'cleaned': normalized['outputs']['normalized']}}

        runtime = self.app(agent=DirectAgent(action))
        run = runtime.advance(self.start(runtime)['id'])
        self.assertEqual(run['state'], 'failed')
        self.assertEqual(run['error']['code'], 'verification_failed')
        self.assertFalse(run['tasks'][run['root']]['verification']['passed'])
        self.assertEqual(runtime.registry.executed, ['orders.normalize'])
        self.assertEqual(run['tasks'][run['root']]['pc'], 0)

    def test_bad_deduplication_tool_output_fails_source_based_final_verification(self):
        class BadDeduplication(RecordingRegistry):
            def execute(self, capability, inputs, work_dir):
                outputs = super().execute(capability, inputs, work_dir)
                if capability == 'orders.deduplicate':
                    path = outputs['cleaned']
                    path.write_text(path.read_text().replace('12000,1000,11000', '12100,1000,11100'))
                return outputs

        runtime = self.app(registry=BadDeduplication())
        run = runtime.advance(self.start(runtime)['id'])
        self.assertEqual(run['state'], 'failed')
        self.assertEqual(run['error']['code'], 'verification_failed')
        self.assertIn('cleaned_mismatch', {d['code'] for d in run['tasks'][run['root']]['verification']['diagnostics']})
        self.assertEqual(runtime.registry.executed, ['orders.normalize', 'orders.deduplicate'])

    def test_late_saved_callback_is_revoked_after_handoff(self):
        callbacks = []
        agent = DirectAgent()
        original = agent.respond_with_tools

        def respond(context, execute):
            callbacks.append((execute, context['inputs']['source_file']))
            return original(context, execute)

        agent.respond_with_tools = respond
        runtime = self.app(agent=agent)
        run = runtime.advance(self.start(runtime)['id'])
        self.assert_clean(runtime, run)
        execute, source = callbacks[0]
        with self.assertRaises(SopError) as caught:
            execute('orders.normalize', {'source_file': source})
        self.assertEqual(caught.exception.code, 'stale_token')
        self.assertEqual(len(runtime.registry.executed), 2)

    def test_declared_retry_revokes_current_callback_before_controller_resumes(self):
        class TransientNormalize(RecordingRegistry):
            attempts = 0

            def execute(self, capability, inputs, work_dir):
                if capability == 'orders.normalize':
                    self.attempts += 1
                    if self.attempts == 1:
                        raise ExecutionError('temporary_read', 'trusted dispatch did not happen', not_dispatched=True)
                return super().execute(capability, inputs, work_dir)

        callback_denials = []
        calls = []

        def action(context, execute):
            calls.append(context)
            args = {'source_file': context['inputs']['source_file']}
            try:
                normalized = execute('orders.normalize', args)
            except SopError as interrupted:
                # Model/tool adapters can catch an error. It cannot renew authority.
                try:
                    execute('orders.normalize', args)
                except SopError as denied:
                    callback_denials.append(denied.code)
                else:
                    callback_denials.append('ILLEGAL_DISPATCH_IN_INTERRUPTED_SESSION')
                raise interrupted
            cleaned = execute('orders.deduplicate', {'normalized': normalized['outputs']['normalized'],
                                                     'rule': context['inputs']['rule']})
            return {'kind': 'candidate_result', 'outputs': cleaned['outputs']}

        definition = direct_clean_definition()
        definition['recovery'] = [{'id': 'read-retry', 'phase': 'execution', 'code': 'temporary_read',
                                   'action': 'retry', 'max_attempts': 1}]
        registry = TransientNormalize()
        runtime = self.app(agent=DirectAgent(action), registry=registry)
        run = runtime.advance(self.start(runtime, definition=definition)['id'])
        self.assert_clean(runtime, run)
        self.assertEqual(callback_denials, ['stale_token'])
        self.assertEqual(len(calls), 2)
        self.assertEqual(registry.attempts, 2)
        self.assertEqual(len(run['recoveries']), 1)

    def test_caught_tool_recovery_error_cannot_replace_controller_state_with_late_handoff(self):
        class TransientNormalize(RecordingRegistry):
            attempts = 0

            def execute(self, capability, inputs, work_dir):
                if capability == 'orders.normalize':
                    self.attempts += 1
                    if self.attempts == 1:
                        raise ExecutionError('temporary_read', 'trusted nondispatch', not_dispatched=True)
                return super().execute(capability, inputs, work_dir)

        late_handoffs = []

        def action(context, execute):
            try:
                normalized = execute('orders.normalize', {'source_file': context['inputs']['source_file']})
            except SopError as interrupted:
                late_handoffs.append(interrupted.code)
                return {'kind': 'cannot_continue', 'reason': 'Late model handoff must not overwrite reserved recovery'}
            cleaned = execute('orders.deduplicate', {'normalized': normalized['outputs']['normalized'],
                                                     'rule': context['inputs']['rule']})
            return {'kind': 'candidate_result', 'outputs': cleaned['outputs']}

        definition = direct_clean_definition()
        definition['recovery'] = [{'id': 'read-retry', 'phase': 'execution', 'code': 'temporary_read',
                                   'action': 'retry', 'max_attempts': 1}]
        runtime = self.app(agent=DirectAgent(action), registry=TransientNormalize())
        done = runtime.advance(self.start(runtime, definition=definition)['id'])
        self.assert_clean(runtime, done)
        self.assertEqual(late_handoffs, ['tool_interrupted'])
        self.assertEqual(len(runtime.agent.contexts), 2)
        self.assertEqual(len(done['recoveries']), 1)
        self.assertEqual(runtime.registry.attempts, 2)

    def test_declared_tool_recheck_keeps_candidate_and_ends_interrupted_session(self):
        class TransientToolCheck(RecordingRegistry):
            checks = 0

            def catalog(self):
                catalog = super().catalog()
                catalog['orders.normalize']['result_check'] = 'normalize-test-check'
                return catalog

            def verify(self, task, inputs, outputs):
                if task == 'normalize-test-check':
                    self.checks += 1
                    if self.checks == 1:
                        raise SopError('temporary_check', 'checker transport temporarily unavailable')
                    return {'passed': True, 'diagnostics': [], 'metrics': {}}
                return super().verify(task, inputs, outputs)

        denials = []
        calls = []

        def action(context, execute):
            calls.append(context)
            args = {'source_file': context['inputs']['source_file']}
            try:
                normalized = execute('orders.normalize', args)
            except SopError as interrupted:
                try:
                    execute('orders.normalize', args)
                except SopError as denied:
                    denials.append(denied.code)
                else:
                    denials.append('ILLEGAL_DISPATCH_IN_INTERRUPTED_SESSION')
                raise interrupted
            cleaned = execute('orders.deduplicate', {'normalized': normalized['outputs']['normalized'],
                                                     'rule': context['inputs']['rule']})
            return {'kind': 'candidate_result', 'outputs': cleaned['outputs']}

        definition = direct_clean_definition()
        definition['recovery'] = [{'id': 'tool-recheck', 'phase': 'verification', 'code': 'temporary_check',
                                   'action': 'recheck', 'max_attempts': 1}]
        registry = TransientToolCheck()
        runtime = self.app(agent=DirectAgent(action), registry=registry)
        run = runtime.advance(self.start(runtime, definition=definition)['id'])
        self.assert_clean(runtime, run)
        self.assertEqual(denials, ['stale_token'])
        self.assertEqual(len(calls), 2)
        self.assertEqual(registry.executed, ['orders.normalize', 'orders.deduplicate'])
        self.assertEqual(registry.checks, 2)
        self.assertEqual(len(run['operations']), 2)
        self.assertEqual(len(run['recoveries']), 1)
        self.assertEqual(len(calls[1]['tool_receipts']), 1)


if __name__ == '__main__':
    unittest.main()
