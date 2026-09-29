"""Original SPEC §§13-15: parameter proposals, correction, and frozen execution.

These tests use the real configured preparation capability and durable runtime.
No model proposes the test values, and no passing self-reported metrics are used.
"""
from copy import deepcopy
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from sop.capabilities import Registry
from sop.common import SopError
from sop.service import application

FIXTURES = Path(__file__).resolve().parents[2] / 'tests/fixtures/input-handoff'
COLUMNS = ['order_id', 'customer_id', 'revision', 'gross_cents', 'refund_cents']
IDENTITY_MAP = {field: field for field in COLUMNS}
CORRECTION = {'id': 'correct-chunk-before-execution', 'phase': 'parameter_validation',
              'code': 'invalid_value', 'action': 'request_parameter_proposal',
              'fields': ['chunk_size'], 'max_attempts': 2}


class Crash(BaseException):
    pass


class RecordingRegistry(Registry):
    def __init__(self):
        self.calls = []

    def execute(self, capability, inputs, work_dir):
        self.calls.append((capability, deepcopy(inputs)))
        return super().execute(capability, inputs, work_dir)


class ParameterWorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='sop-parameter-workflow-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def app(self, *, registry=None, hook=None, directory='data'):
        return application(self.root / directory, registry=registry or RecordingRegistry(), hook=hook)

    def start(self, runtime, *, correction=True, policy=None, limits=None):
        from sop.authoring import configured_prepare_definition
        definition = configured_prepare_definition()
        definition['recovery'] = [deepcopy(CORRECTION)] if correction else []
        options = {}
        if policy is not None:
            options['parameter_policy'] = policy
        if limits is not None:
            options['limits'] = limits
        return runtime.start(definition, {'source_file': FIXTURES / 'source.json'},
                             capabilities=definition['capabilities'], **options)

    def task(self, runtime, run_id):
        run = runtime.store.load(run_id)
        return run['tasks'][run['root']]

    def propose(self, runtime, run_id, patch, *, message_id, revision=None, task_id=None):
        task = self.task(runtime, run_id)
        return runtime.propose_parameters(run_id, task_id or task['id'], patch,
                                          message_id=message_id,
                                          revision=task['parameter_revision'] if revision is None else revision)

    def wait(self, runtime, **start_options):
        run = runtime.advance(self.start(runtime, **start_options)['id'])
        self.assertEqual(run['state'], 'waiting_info', run.get('error'))
        task = run['tasks'][run['root']]
        self.assertEqual(task['state'], 'waiting_parameters')
        self.assertEqual(task['parameter_revision'], 0)
        self.assertEqual(run['operations'], {})
        self.assertEqual(runtime.registry.calls, [])
        questions = [q for q in run['questions'].values() if q['state'] == 'open']
        self.assertEqual(len(questions), 1)
        self.assertEqual(questions[0]['field'], 'parameters')
        self.assertEqual(set(questions[0]['missing']), {'column_map', 'chunk_size'})
        return run

    def assert_prepared(self, runtime, run):
        self.assertEqual(run['state'], 'succeeded', run.get('error'))
        task = run['tasks'][run['root']]
        self.assertTrue(task['verification']['passed'])
        self.assertEqual(runtime.store.path(task['outputs']['prepared']).read_bytes(),
                         (FIXTURES / 'expected.csv').read_bytes())
        self.assertEqual(run['usage']['model_calls'], 0)
        self.assertEqual(run['publication'], 'unpublished')

    def test_partial_values_persist_waiting_then_complete_without_automatic_execution(self):
        runtime = self.app()
        waiting = self.wait(runtime)
        question_id = next(iter(waiting['questions']))
        partial = self.propose(runtime, waiting['id'], {'column_map': IDENTITY_MAP}, message_id='map-only')
        self.assertTrue(partial['accepted'], partial)
        self.assertEqual(partial['parameter_revision'], 1)
        self.assertEqual(partial['missing'], ['chunk_size'])
        saved = runtime.store.load(waiting['id'])
        self.assertEqual(saved['state'], 'waiting_info')
        self.assertEqual(saved['tasks'][saved['root']]['state'], 'waiting_parameters')
        self.assertEqual(saved['questions'][question_id]['state'], 'open')
        self.assertEqual(runtime.registry.calls, [])
        runtime = self.app()
        resumed = runtime.advance(waiting['id'])
        self.assertEqual(resumed['questions'][question_id]['state'], 'open')
        full = self.propose(runtime, waiting['id'], {'chunk_size': 2}, message_id='chunk-only')
        self.assertTrue(full['accepted'], full)
        self.assertEqual(full['parameter_revision'], 2)
        self.assertEqual(full['missing'], [])
        ready = runtime.store.load(waiting['id'])
        self.assertEqual(ready['state'], 'ready')
        self.assertEqual(ready['tasks'][ready['root']]['state'], 'ready')
        self.assertEqual(runtime.registry.calls, [])
        self.assertNotEqual(ready['questions'][question_id]['state'], 'open')
        done = runtime.advance(waiting['id'])
        self.assert_prepared(runtime, done)
        self.assertEqual(len(runtime.registry.calls), 1)
        self.assertEqual(runtime.registry.calls[0][1]['column_map'], IDENTITY_MAP)
        self.assertEqual(runtime.registry.calls[0][1]['chunk_size'], 2)
        operation = next(iter(done['operations'].values()))
        self.assertEqual(operation['parameter_revision'], 2)
        self.assertEqual(len(done['tasks'][done['root']]['parameter_history']), 2)

    def test_same_message_returns_original_receipt_before_revision_check_without_rollback(self):
        runtime = self.app()
        waiting = self.wait(runtime)
        partial = self.propose(runtime, waiting['id'], {'column_map': IDENTITY_MAP}, message_id='map', revision=0)
        self.propose(runtime, waiting['id'], {'chunk_size': 3}, message_id='chunk')
        before = runtime.store.load(waiting['id'])
        repeated = self.propose(runtime, waiting['id'], {'column_map': dict(reversed(list(IDENTITY_MAP.items())))},
                                message_id='map', revision=0)
        self.assertEqual(repeated, partial)
        self.assertEqual(runtime.store.load(waiting['id']), before)
        self.assertEqual(self.task(runtime, waiting['id'])['inputs']['chunk_size'], 3)
        self.assertEqual(self.task(runtime, waiting['id'])['parameter_revision'], 2)
        self.assertEqual(len(self.task(runtime, waiting['id'])['parameter_history']), 2)
        with self.assertRaises(SopError):
            self.propose(runtime, waiting['id'], {'chunk_size': 4}, message_id='map', revision=0)
        with self.assertRaises(SopError):
            self.propose(runtime, waiting['id'], {'chunk_size': 4}, message_id='new-stale', revision=0)
        self.assertEqual(runtime.store.load(waiting['id']), before)
        done = runtime.advance(waiting['id'])
        self.assert_prepared(runtime, done)
        self.assertEqual(self.propose(runtime, waiting['id'], {'column_map': IDENTITY_MAP}, message_id='map', revision=0), partial)
        self.assertEqual(runtime.store.load(waiting['id']), done)

    def test_parameter_revision_is_not_the_run_revision_or_question_identity(self):
        runtime = self.app()
        waiting = self.wait(runtime)
        task = waiting['tasks'][waiting['root']]
        self.assertNotEqual(waiting['revision'], task['parameter_revision'])
        with self.assertRaises(SopError):
            self.propose(runtime, waiting['id'], {'column_map': IDENTITY_MAP}, message_id='wrong-revision', revision=waiting['revision'])
        accepted = self.propose(runtime, waiting['id'], {'column_map': IDENTITY_MAP}, message_id='correct-revision', revision=0)
        self.assertTrue(accepted['accepted'])
        self.assertNotEqual(accepted['run_revision'], accepted['parameter_revision'])
        self.assertEqual(runtime.registry.calls, [])

    def test_invalid_chunk_reserves_declared_correction_once_and_restarts_with_same_scope(self):
        runtime = self.app()
        waiting = self.wait(runtime)
        self.propose(runtime, waiting['id'], {'column_map': IDENTITY_MAP}, message_id='map')
        before = runtime.store.load(waiting['id'])
        invalid = self.propose(runtime, waiting['id'], {'chunk_size': 0}, message_id='invalid-chunk')
        self.assertFalse(invalid['accepted'])
        self.assertEqual(invalid['parameter_revision'], 1)
        saved = runtime.store.load(waiting['id'])
        task = saved['tasks'][saved['root']]
        self.assertEqual(task['inputs']['column_map'], IDENTITY_MAP)
        self.assertNotIn('chunk_size', task['inputs'])
        self.assertEqual(task['recovery_counts'], {CORRECTION['id']: 1})
        self.assertEqual(saved['usage']['actions'], before['usage']['actions'] + 2)
        question = next(q for q in saved['questions'].values() if q['state'] == 'open')
        self.assertEqual(question['allowed_fields'], ['chunk_size'])
        self.assertEqual(runtime.registry.calls, [])
        runtime = self.app()
        duplicate = self.propose(runtime, waiting['id'], {'chunk_size': 0}, message_id='invalid-chunk', revision=1)
        self.assertEqual(duplicate, invalid)
        self.assertEqual(runtime.store.load(waiting['id']), saved)
        corrected = self.propose(runtime, waiting['id'], {'chunk_size': 2}, message_id='corrected')
        self.assertTrue(corrected['accepted'])
        self.assertEqual(corrected['parameter_revision'], 2)
        done = runtime.advance(waiting['id'])
        self.assert_prepared(runtime, done)
        self.assertEqual(len(done['recoveries']), 1)
        self.assertEqual(done['tasks'][done['root']]['recovery_counts'], {CORRECTION['id']: 1})

    def test_unauthorized_fields_reject_entire_correction_without_spending_another_retry(self):
        runtime = self.app()
        waiting = self.wait(runtime)
        self.propose(runtime, waiting['id'], {'column_map': IDENTITY_MAP}, message_id='map')
        self.propose(runtime, waiting['id'], {'chunk_size': 0}, message_id='bad')
        original = self.task(runtime, waiting['id'])
        for index, extra in enumerate((
            {'source_ref': 'other-input'}, {'template_ref': 'other-script'},
            {'source_file': original['inputs']['source_file']}, {'column_map': IDENTITY_MAP},
            {'max_chunk_size': 99999}, {'node_status': 'succeeded'},
            {'record_values_and_order_preserved': True}, {'source_rows': 7},
            {'capabilities': ['shell']}, {'target_format': 'csv'},
        )):
            with self.subTest(extra=extra):
                receipt = self.propose(runtime, waiting['id'], {'chunk_size': 2, **extra}, message_id=f'forbidden-{index}')
                self.assertFalse(receipt['accepted'], receipt)
                current = self.task(runtime, waiting['id'])
                self.assertEqual(current['inputs'], original['inputs'])
                self.assertEqual(current['parameter_revision'], original['parameter_revision'])
                self.assertEqual(current['parameter_history'], original['parameter_history'])
                self.assertEqual(current['recovery_counts'], {CORRECTION['id']: 1})
                self.assertEqual(current['state'], 'waiting_parameters')
        self.assertEqual(runtime.registry.calls, [])
        self.assertTrue(self.propose(runtime, waiting['id'], {'chunk_size': 2}, message_id='allowed')['accepted'])
        self.assert_prepared(runtime, runtime.advance(waiting['id']))

    def test_unknown_fields_before_failure_leave_waiting_and_do_not_partially_accept_map(self):
        runtime = self.app()
        waiting = self.wait(runtime, correction=False)
        bad = self.propose(runtime, waiting['id'], {'column_map': IDENTITY_MAP, 'status': 'succeeded'}, message_id='status-injection')
        self.assertFalse(bad['accepted'])
        task = self.task(runtime, waiting['id'])
        self.assertEqual(task['state'], 'waiting_parameters')
        self.assertEqual(task['parameter_revision'], 0)
        self.assertNotIn('column_map', task['inputs'])
        self.assertEqual(task['recovery_counts'], {})
        self.assertEqual(runtime.registry.calls, [])
        good = self.propose(runtime, waiting['id'], {'column_map': IDENTITY_MAP, 'chunk_size': 1}, message_id='valid')
        self.assertTrue(good['accepted'])
        self.assert_prepared(runtime, runtime.advance(waiting['id']))

    def test_invalid_value_without_recovery_stops_without_dispatch(self):
        runtime = self.app()
        waiting = self.wait(runtime, correction=False)
        receipt = self.propose(runtime, waiting['id'], {'column_map': IDENTITY_MAP, 'chunk_size': 0}, message_id='bad')
        self.assertFalse(receipt['accepted'])
        stopped = runtime.store.load(waiting['id'])
        self.assertEqual(stopped['state'], 'failed')
        self.assertEqual(stopped['tasks'][stopped['root']]['parameter_revision'], 0)
        self.assertNotIn('column_map', stopped['tasks'][stopped['root']]['inputs'])
        self.assertEqual(stopped['operations'], {})
        with self.assertRaises(SopError):
            self.propose(runtime, waiting['id'], {'column_map': IDENTITY_MAP, 'chunk_size': 2}, message_id='unauthorized-repair')
        self.assertEqual(runtime.registry.calls, [])

    def test_strict_integer_type_and_host_policy_limit_cannot_be_relaxed_by_proposals(self):
        for index, value in enumerate((True, False, 1.0, '1', 0, -1, 5)):
            with self.subTest(value=value):
                runtime = self.app(directory=f'value-{index}')
                waiting = self.wait(runtime, correction=False, policy={'max_chunk_size': 4})
                receipt = self.propose(runtime, waiting['id'], {'column_map': IDENTITY_MAP, 'chunk_size': value}, message_id='bad-type-or-range')
                self.assertFalse(receipt['accepted'])
                self.assertEqual(self.task(runtime, waiting['id'])['parameter_revision'], 0)
                self.assertEqual(runtime.registry.calls, [])
        runtime = self.app(directory='policy-maximum')
        waiting = self.wait(runtime, policy={'max_chunk_size': 4})
        receipt = self.propose(runtime, waiting['id'], {'column_map': IDENTITY_MAP, 'chunk_size': 4}, message_id='maximum')
        self.assertTrue(receipt['accepted'])
        self.assert_prepared(runtime, runtime.advance(waiting['id']))

    def test_type_correct_but_wrong_field_meaning_is_not_an_accepted_column_map(self):
        wrong = dict(IDENTITY_MAP, gross_cents='refund_cents', refund_cents='gross_cents')
        runtime = self.app()
        waiting = self.wait(runtime, correction=False)
        receipt = self.propose(runtime, waiting['id'], {'column_map': wrong, 'chunk_size': 2}, message_id='swapped-money')
        self.assertFalse(receipt['accepted'])
        task = self.task(runtime, waiting['id'])
        self.assertEqual(task['parameter_revision'], 0)
        self.assertNotIn('chunk_size', task['inputs'])
        self.assertEqual(runtime.registry.calls, [])

    def test_repeated_new_invalid_values_exhaust_correction_limit_across_restart(self):
        runtime = self.app()
        waiting = self.wait(runtime)
        self.propose(runtime, waiting['id'], {'column_map': IDENTITY_MAP}, message_id='map')
        for index in range(2):
            receipt = self.propose(runtime, waiting['id'], {'chunk_size': 0}, message_id=f'invalid-{index}')
            self.assertFalse(receipt['accepted'])
            saved = runtime.store.load(waiting['id'])
            self.assertEqual(saved['tasks'][saved['root']]['recovery_counts'][CORRECTION['id']], index + 1)
            self.assertEqual(saved['state'], 'waiting_info')
            runtime = self.app()
        receipt = self.propose(runtime, waiting['id'], {'chunk_size': 0}, message_id='exhausted')
        self.assertFalse(receipt['accepted'])
        stopped = runtime.store.load(waiting['id'])
        self.assertEqual(stopped['state'], 'failed')
        self.assertEqual(stopped['tasks'][stopped['root']]['recovery_counts'], {CORRECTION['id']: 2})
        self.assertEqual(len(stopped['recoveries']), 2)
        self.assertEqual(stopped['tasks'][stopped['root']]['parameter_revision'], 1)
        self.assertEqual(runtime.registry.calls, [])

    def test_root_action_budget_is_not_reset_by_proposals_or_restart(self):
        runtime = self.app()
        waiting = self.wait(runtime, limits={'max_actions': 3, 'max_model_calls': 1, 'max_depth': 2})
        self.propose(runtime, waiting['id'], {'column_map': IDENTITY_MAP}, message_id='map')
        self.propose(runtime, waiting['id'], {'chunk_size': 0}, message_id='reserve-last-actions')
        saved = runtime.store.load(waiting['id'])
        self.assertEqual(saved['usage']['actions'], 3)
        runtime = self.app()
        try:
            self.propose(runtime, waiting['id'], {'chunk_size': 2}, message_id='beyond-budget')
        except SopError as error:
            self.assertEqual(error.code, 'budget_exhausted')
        stopped = runtime.store.load(waiting['id'])
        self.assertEqual(stopped['state'], 'budget_exhausted')
        self.assertEqual(stopped['usage']['actions'], 3)
        self.assertEqual(stopped['tasks'][stopped['root']]['recovery_counts'], {CORRECTION['id']: 1})
        self.assertNotIn('chunk_size', stopped['tasks'][stopped['root']]['inputs'])
        self.assertEqual(runtime.registry.calls, [])

    def test_ready_values_can_be_revised_until_execution_and_history_is_not_overwritten(self):
        runtime = self.app()
        waiting = self.wait(runtime)
        first = self.propose(runtime, waiting['id'], {'column_map': IDENTITY_MAP, 'chunk_size': 1}, message_id='first')
        first_history = deepcopy(self.task(runtime, waiting['id'])['parameter_history'])
        second = self.propose(runtime, waiting['id'], {'chunk_size': 3}, message_id='update-ready')
        self.assertTrue(first['accepted'])
        self.assertTrue(second['accepted'])
        self.assertEqual(second['parameter_revision'], first['parameter_revision'] + 1)
        self.assertEqual(self.task(runtime, waiting['id'])['parameter_history'][:1], first_history)
        done = runtime.advance(waiting['id'])
        self.assert_prepared(runtime, done)
        operation = next(iter(done['operations'].values()))
        self.assertEqual(operation['inputs']['chunk_size'], 3)
        self.assertEqual(operation['parameter_revision'], second['parameter_revision'])
        with self.assertRaises(SopError):
            self.propose(runtime, waiting['id'], {'chunk_size': 4}, message_id='after-consumption')
        self.assertEqual(runtime.store.load(waiting['id']), done)

    def test_execution_start_and_ready_parameter_update_are_mutually_exclusive(self):
        entered, release = threading.Event(), threading.Event()

        class BlockingRegistry(RecordingRegistry):
            def execute(self, capability, inputs, work_dir):
                entered.set()
                if not release.wait(5):
                    raise AssertionError('Test did not release fixed capability')
                return super().execute(capability, inputs, work_dir)

        registry = BlockingRegistry()
        runtime = self.app(registry=registry)
        waiting = self.wait(runtime)
        accepted = self.propose(runtime, waiting['id'], {'column_map': IDENTITY_MAP, 'chunk_size': 2}, message_id='ready')
        results, errors = [], []

        def advance():
            try:
                results.append(runtime.advance(waiting['id']))
            except BaseException as error:
                errors.append(error)

        worker = threading.Thread(target=advance, daemon=True)
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            with self.assertRaises(SopError):
                self.propose(runtime, waiting['id'], {'chunk_size': 4}, message_id='concurrent-update', revision=accepted['parameter_revision'])
        finally:
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assert_prepared(runtime, results[0])
        self.assertEqual(registry.calls[0][1]['chunk_size'], 2)
        self.assertEqual(next(iter(results[0]['operations'].values()))['parameter_revision'], accepted['parameter_revision'])
        self.assertEqual(results[0]['tasks'][results[0]['root']]['parameter_revision'], accepted['parameter_revision'])

    def test_candidate_restart_uses_frozen_parameter_version_without_tool_reexecution(self):
        def hook(kind, run):
            if kind == 'operation_candidate':
                raise Crash()

        registry = RecordingRegistry()
        runtime = self.app(registry=registry, hook=hook)
        waiting = self.wait(runtime)
        accepted = self.propose(runtime, waiting['id'], {'column_map': IDENTITY_MAP, 'chunk_size': 2}, message_id='configured')
        with self.assertRaises(Crash):
            runtime.advance(waiting['id'])
        saved = runtime.store.load(waiting['id'])
        operation = next(iter(saved['operations'].values()))
        self.assertEqual(operation['parameter_revision'], accepted['parameter_revision'])
        self.assertEqual(operation['status'], 'candidate')
        self.assertEqual(len(registry.calls), 1)
        with self.assertRaises(SopError):
            self.propose(runtime, waiting['id'], {'chunk_size': 3}, message_id='replace-consumed')

        class NoReplay(Registry):
            def execute(self, *args, **kwargs):
                raise AssertionError('A saved configured candidate must not repeat conversion')

        runtime = self.app(registry=NoReplay())
        done = runtime.advance(waiting['id'])
        self.assert_prepared(runtime, done)
        self.assertEqual(done['operations'][operation['id']]['inputs'], operation['inputs'])
        self.assertEqual(done['operations'][operation['id']]['parameter_revision'], accepted['parameter_revision'])

    def test_cancel_precedes_parameter_proposal_and_cannot_reopen_wait(self):
        runtime = self.app()
        waiting = self.wait(runtime)
        partial = self.propose(runtime, waiting['id'], {'column_map': IDENTITY_MAP}, message_id='map')
        runtime.store.request_cancel(waiting['id'])
        with self.assertRaises(SopError):
            self.propose(runtime, waiting['id'], {'chunk_size': 2}, message_id='late-fill')
        stopped = runtime.store.load(waiting['id'])
        self.assertEqual(stopped['state'], 'cancelled')
        self.assertEqual(stopped['tasks'][stopped['root']]['parameter_revision'], partial['parameter_revision'])
        self.assertNotIn('chunk_size', stopped['tasks'][stopped['root']]['inputs'])
        self.assertEqual(runtime.registry.calls, [])
        self.assertEqual(self.propose(runtime, waiting['id'], {'column_map': IDENTITY_MAP}, message_id='map', revision=0), partial)
        self.assertEqual(runtime.store.load(waiting['id']), stopped)

    def test_empty_partial_proposal_cannot_clear_unresolved_correction_scope(self):
        runtime = self.app()
        waiting = self.wait(runtime)
        self.propose(runtime, waiting['id'], {'column_map': IDENTITY_MAP}, message_id='map')
        self.propose(runtime, waiting['id'], {'chunk_size': 0}, message_id='invalid')
        self.propose(runtime, waiting['id'], {}, message_id='empty-correction')
        saved = runtime.store.load(waiting['id'])
        question = next(q for q in saved['questions'].values() if q['state'] == 'open')
        self.assertEqual(question['allowed_fields'], ['chunk_size'])
        forbidden = self.propose(runtime, waiting['id'], {'column_map': IDENTITY_MAP, 'chunk_size': 2},
                                 message_id='try-reopened-map')
        self.assertFalse(forbidden['accepted'])
        self.assertEqual(self.task(runtime, waiting['id'])['recovery_counts'], {CORRECTION['id']: 1})
        self.assertEqual(runtime.registry.calls, [])
        self.assertTrue(self.propose(runtime, waiting['id'], {'chunk_size': 2}, message_id='correct')['accepted'])
        self.assert_prepared(runtime, runtime.advance(waiting['id']))

    def test_lost_parameter_receipt_is_durable_and_not_reaccepted_after_restart(self):
        def hook(kind, run):
            if kind == 'parameters_accepted':
                raise Crash()

        runtime = self.app(hook=hook)
        waiting = self.wait(runtime)
        patch = {'column_map': IDENTITY_MAP, 'chunk_size': 2}
        with self.assertRaises(Crash):
            self.propose(runtime, waiting['id'], patch, message_id='lost-acceptance', revision=0)
        saved = runtime.store.load(waiting['id'])
        self.assertEqual(saved['tasks'][saved['root']]['parameter_revision'], 1)
        self.assertEqual(saved['usage']['actions'], 1)
        self.assertEqual(runtime.registry.calls, [])
        runtime = self.app()
        receipt = self.propose(runtime, waiting['id'], patch, message_id='lost-acceptance', revision=0)
        self.assertTrue(receipt['accepted'])
        self.assertEqual(receipt['parameter_revision'], 1)
        self.assertEqual(runtime.store.load(waiting['id']), saved)
        self.assertEqual(len(self.task(runtime, waiting['id'])['parameter_history']), 1)
        self.assert_prepared(runtime, runtime.advance(waiting['id']))

    def test_rejection_receipt_and_correction_reservation_survive_same_commit_crash(self):
        def hook(kind, run):
            if kind == 'parameters_rejected':
                raise Crash()

        runtime = self.app(hook=hook)
        waiting = self.wait(runtime)
        self.propose(runtime, waiting['id'], {'column_map': IDENTITY_MAP}, message_id='map')
        with self.assertRaises(Crash):
            self.propose(runtime, waiting['id'], {'chunk_size': 0}, message_id='lost-rejection', revision=1)
        saved = runtime.store.load(waiting['id'])
        self.assertEqual(saved['tasks'][saved['root']]['recovery_counts'], {CORRECTION['id']: 1})
        self.assertEqual(saved['usage']['actions'], 3)
        runtime = self.app()
        receipt = self.propose(runtime, waiting['id'], {'chunk_size': 0}, message_id='lost-rejection', revision=1)
        self.assertFalse(receipt['accepted'])
        self.assertEqual(runtime.store.load(waiting['id']), saved)
        self.assertEqual(runtime.registry.calls, [])
        self.assertTrue(self.propose(runtime, waiting['id'], {'chunk_size': 2}, message_id='fixed')['accepted'])
        self.assert_prepared(runtime, runtime.advance(waiting['id']))

    def test_parameter_question_cannot_be_answered_through_business_rule_endpoint(self):
        runtime = self.app()
        waiting = self.wait(runtime)
        question = next(q for q in waiting['questions'].values() if q['state'] == 'open')
        with self.assertRaises(SopError) as caught:
            runtime.answer(waiting['id'], question['request_id'], 'first', message_id='wrong-endpoint',
                           revision=waiting['revision'], owner=waiting['root'])
        self.assertEqual(caught.exception.code, 'unsupported_question')
        self.assertEqual(runtime.store.load(waiting['id']), waiting)
        self.assertEqual(runtime.registry.calls, [])

    def test_changed_registry_blocks_new_parameter_acceptance_without_consuming_budget(self):
        runtime = self.app()
        waiting = self.wait(runtime)
        before = runtime.store.load(waiting['id'])
        with patch.object(Registry, '_version', return_value='changed-implementation'):
            with self.assertRaises(SopError) as caught:
                self.propose(runtime, waiting['id'], {'column_map': IDENTITY_MAP, 'chunk_size': 2},
                             message_id='changed-registry')
        self.assertEqual(caught.exception.code, 'dependency_drift')
        self.assertEqual(runtime.store.load(waiting['id']), before)
        self.assertEqual(runtime.registry.calls, [])

    def test_changed_source_snapshot_blocks_new_parameter_acceptance(self):
        runtime = self.app()
        waiting = self.wait(runtime)
        before = runtime.store.load(waiting['id'])
        source = runtime.store.path(before['tasks'][before['root']]['inputs']['source_file'])
        # Deliberate trusted-test corruption of this run's copy, never the fixture.
        source.chmod(0o600)
        source.write_bytes(source.read_bytes() + b'\n')
        with self.assertRaises(SopError) as caught:
            self.propose(runtime, waiting['id'], {'column_map': IDENTITY_MAP, 'chunk_size': 2},
                         message_id='changed-source')
        self.assertEqual(caught.exception.code, 'integrity')
        self.assertEqual(runtime.store.load(waiting['id']), before)
        self.assertEqual(runtime.registry.calls, [])

    def test_unknown_task_and_wrong_run_proposal_never_mutate_target(self):
        runtime = self.app()
        first = self.wait(runtime)
        second = self.wait(runtime)
        before = runtime.store.load(first['id'])
        for task_id in ('task-not-found', second['root']):
            with self.subTest(task=task_id), self.assertRaises(SopError):
                self.propose(runtime, first['id'], {'column_map': IDENTITY_MAP, 'chunk_size': 2},
                             message_id='wrong-scope-' + task_id, task_id=task_id, revision=0)
        self.assertEqual(runtime.store.load(first['id']), before)
        self.assertEqual(runtime.registry.calls, [])


if __name__ == '__main__':
    unittest.main()
