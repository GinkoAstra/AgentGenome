"""Independent controller boundary tests, using real registered business checks.

The small validator below deliberately does not test B1: these definitions are
known-valid protocol fixtures. Failure assertions concern B2/B4 responsibilities.
No expected fixture is given to an agent; the registered checker recomputes input.
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import subprocess
import sys
import unittest

from sop.agents import ScriptedAgent
from sop.capabilities import Registry
from sop.common import SopError, digest
from sop.runtime import Runtime
from sop.store import Store


SOURCE = Path(__file__).resolve().parents[2] / 'tests/fixtures/first-release/batch-01/input.csv'
CAPABILITIES = ['orders.normalize', 'orders.deduplicate']


def clean_definition():
    return {
        'schema': 'sop/1', 'id': 'audit-clean', 'task': 'orders.clean',
        'params': {'source_file': {'type': 'artifact', 'required': True},
                   'rule': {'type': 'string', 'enum': ['highest_revision', 'first'], 'required': True}},
        'capabilities': CAPABILITIES,
        'steps': [
            {'id': 'normalize', 'kind': 'fixed', 'capability': 'orders.normalize',
             'inputs': {'source_file': '$input.source_file'}},
            {'id': 'deduplicate', 'kind': 'fixed', 'capability': 'orders.deduplicate',
             'inputs': {'normalized': '$steps.normalize.normalized', 'rule': '$input.rule'}},
        ],
        'outputs': {'cleaned': '$steps.deduplicate.cleaned'},
        'checks': ['orders.clean'], 'recovery': [],
    }


def parent_definition(kind='pi', *, highest_only=False):
    definition = clean_definition()
    definition['id'] = 'audit-parent'
    definition['steps'] = [
        {'id': 'clean', 'kind': kind, 'task': 'orders.clean',
         'inputs': {'source_file': '$input.source_file', 'rule': '$input.rule'}},
    ]
    definition['outputs'] = {'cleaned': '$steps.clean.cleaned'}
    if highest_only:
        definition['params']['rule']['enum'] = ['highest_revision']
        definition['supported_rules'] = ['highest_revision']
    return definition


def fixture_validator(definition, registry):
    return {
        'passed': True, 'definition_hash': digest(definition),
        'dependency_hashes': {key: value['version'] for key, value in registry.catalog().items()},
        'checker_versions': registry.checkers(),
    }


def resolver(*args, **kwargs):
    return {'definition': clean_definition(), 'origin': 'existing', 'selection': []}


class InjectedCrash(BaseException):
    """A process exit rather than a handled business/adapter exception."""


class ControllerAdversarialTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix='sop-adversarial-')
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.store = Store(self.base / 'data')

    def runtime(self, *, registry=None, agent=None, hook=None):
        return Runtime(self.store, registry or Registry(), fixture_validator, resolver,
                       agent=agent, hook=hook)

    def start(self, runtime, definition=None, *, rule='highest_revision'):
        inputs = {'source_file': SOURCE}
        if rule is not None:
            inputs['rule'] = rule
        return runtime.start(definition or parent_definition(), inputs, capabilities=CAPABILITIES)

    def test_child_answer_must_not_weaken_parent_rule(self):
        runtime = self.runtime()
        run = self.start(runtime, parent_definition('call', highest_only=True), rule=None)
        run = runtime.advance(run['id'])
        question = next(iter(run['questions'].values()))
        self.assertNotEqual(question['owner'], run['root'])
        before = len(run['operations'])
        try:
            runtime.answer(run['id'], question['request_id'], 'first', message_id='invalid-parent-rule',
                           revision=run['revision'], owner=question['owner'])
        except SopError:
            pass  # A narrower inherited contract may reject at the answer boundary.
        run = runtime.advance(run['id'])
        self.assertNotEqual(run['state'], 'succeeded', 'A highest-only parent completed under first-row rules')
        self.assertNotEqual(run['tasks'][run['root']]['inputs'].get('rule'), 'first')
        self.assertEqual(len(run['operations']), before, 'Incompatible answer dispatched dependent business work')

    def test_pi_candidate_is_saved_before_independent_verification(self):
        class CrashOnPiVerification(Registry):
            def __init__(self):
                self.checks = 0

            def verify(self, task, inputs, outputs):
                self.checks += 1
                if self.checks == 2:  # Child passed; Pi returned that saved child's candidate.
                    raise InjectedCrash('exit while checking Pi candidate')
                return super().verify(task, inputs, outputs)

        registry = CrashOnPiVerification()
        agent = ScriptedAgent()
        runtime = self.runtime(registry=registry, agent=agent)
        run = self.start(runtime)
        with self.assertRaises(InjectedCrash):
            runtime.advance(run['id'])
        calls_before_restart = agent.calls
        completed = self.runtime(registry=registry, agent=agent).advance(run['id'])
        self.assertEqual(completed['state'], 'succeeded')
        self.assertEqual(agent.calls, calls_before_restart,
                         'Verifying a saved Pi candidate after restart must not call the model again')

    def test_pi_candidate_honors_declared_recheck_without_regeneration(self):
        class TransientPiVerifier(Registry):
            def __init__(self):
                self.checks = 0

            def verify(self, task, inputs, outputs):
                self.checks += 1
                if self.checks == 2:
                    raise SopError('checker_temporarily_unavailable', 'trusted verifier did not complete')
                return super().verify(task, inputs, outputs)

        registry = TransientPiVerifier()
        agent = ScriptedAgent()
        runtime = self.runtime(registry=registry, agent=agent)
        definition = parent_definition()
        definition['recovery'] = [
            {'id': 'recheck-original-candidate', 'phase': 'verification',
             'code': 'checker_temporarily_unavailable', 'action': 'recheck', 'max_attempts': 1},
        ]
        run = self.start(runtime, definition)
        completed = runtime.advance(run['id'])
        self.assertEqual(completed['state'], 'succeeded', completed.get('error'))
        self.assertEqual(agent.calls, 2, 'Rechecking a candidate must not ask the agent to regenerate')
        self.assertEqual(len(completed['recoveries']), 1)

    def test_recheck_reservation_crash_keeps_pi_candidate_continuation(self):
        class TransientPiVerifier(Registry):
            def __init__(self):
                self.checks = 0

            def verify(self, task, inputs, outputs):
                self.checks += 1
                if self.checks == 2:
                    raise SopError('checker_temporarily_unavailable', 'trusted verifier did not complete')
                return super().verify(task, inputs, outputs)

        def crash_after_reservation(kind, run):
            if kind == 'recovery_reserved':
                raise InjectedCrash('exit immediately after recovery transaction commits')

        registry, agent = TransientPiVerifier(), ScriptedAgent()
        definition = parent_definition()
        definition['recovery'] = [
            {'id': 'recheck-original-candidate', 'phase': 'verification',
             'code': 'checker_temporarily_unavailable', 'action': 'recheck', 'max_attempts': 1},
        ]
        runtime = self.runtime(registry=registry, agent=agent, hook=crash_after_reservation)
        run = self.start(runtime, definition)
        with self.assertRaises(InjectedCrash):
            runtime.advance(run['id'])
        reserved = self.store.load(run['id'])
        self.assertEqual(len(reserved['recoveries']), 1)
        completed = self.runtime(registry=registry, agent=agent).advance(run['id'])
        self.assertEqual(completed['state'], 'succeeded', completed.get('error'))
        self.assertEqual(agent.calls, 2)
        self.assertEqual(len(completed['recoveries']), 1, 'Restart reserved the same recovery twice')

    def test_lost_planner_reply_keeps_budget_and_does_not_invent_effects(self):
        from sop.authoring import Library
        from sop.service import application

        class LostReplyPlanner:
            max_model_calls_per_request = 2

            def __init__(self):
                self.calls = 0

            def plan(self, context):
                self.calls += 1
                if self.calls == 1:
                    raise InjectedCrash('read-only model reply lost before acceptance')
                return clean_definition()

        planner = LostReplyPlanner()
        runtime = application(self.store.root, library=Library([]), planner=planner)
        run = self.start(runtime, parent_definition('call'))
        with self.assertRaises(InjectedCrash):
            runtime.advance(run['id'])
        saved = self.store.load(run['id'])
        self.assertEqual(saved['usage']['model_calls'], 2, 'Lost reply erased its reserved model budget')
        self.assertEqual(saved['operations'], {}, 'Method planning dispatched a business operation')
        completed = application(self.store.root, library=Library([]), planner=planner).advance(run['id'])
        self.assertEqual(completed['state'], 'succeeded', completed.get('error'))
        self.assertEqual(completed['usage']['model_calls'], 4)
        self.assertEqual(planner.calls, 2)

    def test_accepted_generated_method_is_not_replanned_after_restart(self):
        from sop.authoring import Library
        from sop.agents import ScriptedPlanner
        from sop.service import application

        def crash_after_acceptance(kind, run):
            if kind == 'method_accepted':
                raise InjectedCrash('exit after candidate identity and acceptance commit')

        planner = ScriptedPlanner()
        runtime = application(self.store.root, library=Library([]), planner=planner, hook=crash_after_acceptance)
        run = self.start(runtime, parent_definition('call'))
        with self.assertRaises(InjectedCrash):
            runtime.advance(run['id'])
        saved = self.store.load(run['id'])
        saved_definitions = dict(saved['definitions'])
        completed = application(self.store.root, library=Library([]), planner=None).advance(run['id'])
        self.assertEqual(completed['state'], 'succeeded', completed.get('error'))
        self.assertEqual(planner.calls, 1)
        self.assertEqual(completed['definitions'], saved_definitions)

    def test_process_exit_after_effect_releases_lock_but_never_replays(self):
        runtime = self.runtime()
        run = self.start(runtime, clean_definition())
        marker = self.base / 'observed-effects.txt'
        worker = """
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path.cwd() / 'tests' / 'recursive_sop'))
from test_adversarial import fixture_validator, resolver
from sop.capabilities import Registry
from sop.runtime import Runtime
from sop.store import Store
class ExitAfterEffect(Registry):
    def execute(self, capability, inputs, work_dir):
        result = super().execute(capability, inputs, work_dir)
        with Path(sys.argv[3]).open('a') as output:
            output.write('effect\\n')
            output.flush()
            os.fsync(output.fileno())
        os._exit(73)
Runtime(Store(sys.argv[1]), ExitAfterEffect(), fixture_validator, resolver).advance(sys.argv[2])
"""
        process = subprocess.run([sys.executable, '-c', worker, str(self.store.root), run['id'], str(marker)],
                                 cwd=SOURCE.parents[4], capture_output=True, text=True, timeout=10)
        self.assertEqual(process.returncode, 73, process.stderr)
        self.assertEqual(marker.read_text().splitlines(), ['effect'])
        restored = self.runtime().advance(run['id'])
        self.assertEqual(restored['state'], 'effect_unknown')
        self.assertEqual(len(restored['operations']), 1)
        self.assertEqual(self.runtime().advance(run['id'])['state'], 'effect_unknown')
        self.assertEqual(marker.read_text().splitlines(), ['effect'], 'Unknown effect was replayed')

    def test_malformed_method_reference_fails_closed_without_traceback(self):
        class MalformedReference(ScriptedAgent):
            def respond(self, context):
                return {'kind': 'need_subtask', 'task': context['task'],
                        'inputs': context['inputs'], 'reason': 'invalid optional reference',
                        'definition': ['not-a-content-hash']}

        runtime = self.runtime(agent=MalformedReference())
        run = self.start(runtime)
        stopped = runtime.advance(run['id'])
        self.assertEqual(stopped['state'], 'failed')
        self.assertIn(stopped.get('error', {}).get('code'), {'invalid_response', 'invalid_reference'})
        self.assertEqual(stopped['operations'], {})
        self.assertEqual(stopped['calls'], {})

    def test_cancel_during_model_call_prevents_following_business_actions(self):
        entered, release = threading.Event(), threading.Event()

        class BlockingAgent(ScriptedAgent):
            def respond(self, context):
                if not entered.is_set():
                    entered.set()
                    if not release.wait(5):
                        raise RuntimeError('test synchronization timed out')
                return super().respond(context)

        runtime = self.runtime(agent=BlockingAgent())
        run = self.start(runtime)
        cancellation_error = None
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(runtime.advance, run['id'])
            self.assertTrue(entered.wait(5), 'agent did not start')
            try:
                runtime.cancel(run['id'])
            except SopError as exc:
                cancellation_error = exc.code
            finally:
                release.set()
            pending.result(timeout=10)
        self.assertIsNone(cancellation_error, 'Cancel must persist revocation while the runner owns its lock')
        stopped = self.store.load(run['id'])
        self.assertEqual(stopped['state'], 'cancelled')
        self.assertEqual(stopped['operations'], {}, 'Late model result started business actions after cancellation')

    def test_store_rejects_symlinked_internal_object_directory(self):
        outside = self.base / 'outside'
        outside.mkdir()
        root = self.base / 'redirected-data'
        root.mkdir()
        (root / 'objects').symlink_to(outside, target_is_directory=True)
        try:
            store = Store(root)
            store.put_bytes(b'content must stay inside the configured data root')
        except SopError:
            pass
        self.assertEqual(list(outside.iterdir()), [], 'Content storage escaped via an internal directory symlink')


if __name__ == '__main__':
    unittest.main()
