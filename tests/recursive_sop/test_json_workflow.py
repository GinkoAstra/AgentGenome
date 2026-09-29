"""JSON preparation -> checked child handoff -> complete order report.

Scripted Pi adapters below are protocol fixtures, not real-model evidence.
"""
import csv
import hashlib
import io
import tempfile
import unittest
from pathlib import Path

from sop.agents import ScriptedAgent, ScriptedPlanner
from sop.authoring import Library, clean_definition, json_report_definition, prepare_definition
from sop.capabilities import Registry
from sop.common import read_json, write_json
from sop.service import application

FIXTURES = Path(__file__).resolve().parents[2] / 'tests/fixtures'
SOURCE = FIXTURES / 'input-handoff/source.json'
EXPECTED_PREPARED = FIXTURES / 'input-handoff/expected.csv'
EXPECTED_REPORT = FIXTURES / 'first-release/batch-01/expected'


class Crash(BaseException):
    pass


class CountingRegistry(Registry):
    def __init__(self):
        self.executed = []

    def execute(self, capability, inputs, work_dir):
        self.executed.append(capability)
        return super().execute(capability, inputs, work_dir)


class ForbiddenAgent(ScriptedAgent):
    def respond(self, context):
        raise AssertionError('A fixed preparation/report must not invoke an agent')


class ForbiddenPlanner(ScriptedPlanner):
    def plan(self, context):
        raise AssertionError('An available fixed method must not invoke a planner')


class JsonWorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='sop-json-workflow-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def app(self, *, generated=False, registry=None, agent=None, planner=None, hook=None, directory='data'):
        definitions = [prepare_definition(), clean_definition('first')]
        if not generated:
            definitions.append(clean_definition())
        return application(self.root / directory, library=Library(definitions),
                           registry=registry or CountingRegistry(), agent=agent or ScriptedAgent(),
                           planner=planner or ScriptedPlanner(), hook=hook)

    def start(self, runtime, *, definition=None, source=SOURCE, rule='highest_revision'):
        definition = definition or json_report_definition()
        inputs = {'source_file': source}
        if rule is not None:
            inputs['rule'] = rule
        return runtime.start(definition, inputs, capabilities=definition['capabilities'])

    def assert_report(self, runtime, run):
        self.assertEqual(run['state'], 'succeeded', run.get('error'))
        root = run['tasks'][run['root']]
        self.assertTrue(root['verification']['passed'])
        self.assertEqual(set(root['outputs']), {'prepared', 'mapping', 'cleaned', 'summary', 'quality'})
        self.assertEqual(runtime.store.path(root['outputs']['prepared']).read_bytes(), EXPECTED_PREPARED.read_bytes())
        for name in ('cleaned', 'summary', 'quality'):
            actual = runtime.store.path(root['outputs'][name])
            expected = EXPECTED_REPORT / (name + ('.json' if name == 'quality' else '.csv'))
            if name == 'quality':
                self.assertEqual(read_json(actual), read_json(expected))
            else:
                self.assertEqual(actual.read_bytes(), expected.read_bytes())
        mapping = runtime.store.json(root['outputs']['mapping'])
        self.assertEqual(mapping['source']['sha256'], root['inputs']['source_file']['hash'])
        self.assertEqual(mapping['prepared']['sha256'], root['outputs']['prepared']['hash'])
        self.assertEqual(mapping['records'], [{'source_index': i, 'csv_row': i + 1} for i in range(7)])
        self.assertEqual(run['publication'], 'unpublished')

    def test_existing_and_generated_clean_follow_independently_accepted_prepare_child(self):
        for generated in (False, True):
            with self.subTest(generated=generated):
                registry, agent, planner = CountingRegistry(), ScriptedAgent(), ScriptedPlanner()
                runtime = self.app(generated=generated, registry=registry, agent=agent, planner=planner,
                                   directory='generated' if generated else 'existing')
                run = runtime.advance(self.start(runtime)['id'])
                self.assert_report(runtime, run)
                prepare = next(task for task in run['tasks'].values() if task['task'] == 'orders.prepare')
                clean = next(task for task in run['tasks'].values() if task['task'] == 'orders.clean')
                self.assertEqual(prepare['state'], 'succeeded')
                self.assertTrue(prepare['verification']['passed'])
                self.assertEqual(prepare['accepted']['origin'], 'existing')
                self.assertEqual(clean['accepted']['origin'], 'generated' if generated else 'existing')
                self.assertEqual(clean['inputs']['source_file'], prepare['outputs']['prepared'])
                self.assertEqual(planner.calls, int(generated))
                self.assertEqual(agent.calls, 2)
                self.assertEqual(registry.executed, ['orders.prepare', 'orders.profile', 'orders.normalize',
                                                     'orders.deduplicate', 'orders.summarize'])
                preparation_operation = next(op for op in run['operations'].values() if op['capability'] == 'orders.prepare')
                self.assertTrue(preparation_operation['verification']['passed'])
                self.assertEqual(preparation_operation['status'], 'committed')
                self.assertTrue(all(call['consumed'] for call in run['calls'].values()))

    def test_fixed_report_uses_same_prepared_content_without_agent_or_planner(self):
        runtime = self.app(agent=ForbiddenAgent(), planner=ForbiddenPlanner())
        run = runtime.advance(self.start(runtime, definition=json_report_definition('fixed'))['id'])
        self.assert_report(runtime, run)
        self.assertEqual(run['usage']['model_calls'], 0)
        self.assertEqual(len(run['tasks']), 2)  # Root plus the fixed preparation child.

    def test_preparation_alone_does_not_request_deduplication_rule(self):
        runtime = self.app(agent=ForbiddenAgent(), planner=ForbiddenPlanner())
        run = runtime.advance(self.start(runtime, definition=prepare_definition(), rule=None)['id'])
        self.assertEqual(run['state'], 'succeeded', run.get('error'))
        self.assertEqual(run['questions'], {})
        self.assertEqual(run['answers'], [])
        self.assertEqual(run['usage']['model_calls'], 0)
        self.assertEqual(runtime.registry.executed, ['orders.prepare'])
        root = run['tasks'][run['root']]
        self.assertEqual(set(root['outputs']), {'prepared', 'mapping'})
        self.assertEqual(runtime.store.path(root['outputs']['prepared']).read_bytes(), EXPECTED_PREPARED.read_bytes())
        self.assertEqual(root['verification']['reports'][0]['metrics']['source_rows'], 7)

    def test_preparation_completes_before_unbound_business_rule_question_and_restores(self):
        runtime = self.app()
        run = runtime.advance(self.start(runtime, rule=None)['id'])
        self.assertEqual(run['state'], 'waiting_info', run.get('error'))
        prepare = next(task for task in run['tasks'].values() if task['task'] == 'orders.prepare')
        self.assertEqual(prepare['state'], 'succeeded')
        question = next(q for q in run['questions'].values() if q['state'] == 'open')
        self.assertEqual(question['field'], 'rule')
        self.assertNotEqual(question['owner'], prepare['id'])
        self.assertEqual(runtime.registry.executed, ['orders.prepare', 'orders.profile'])
        runtime = self.app()
        self.assertEqual(runtime.advance(run['id']), run)
        runtime.answer(run['id'], question['request_id'], 'highest_revision', message_id='json-rule',
                       revision=run['revision'], owner=question['owner'])
        done = runtime.advance(run['id'])
        self.assert_report(runtime, done)
        self.assertNotIn('orders.prepare', runtime.registry.executed)
        self.assertEqual(done['tasks'][done['root']]['inputs']['rule'], 'highest_revision')

    def test_bad_prepared_candidates_never_reach_fixed_consumers(self):
        for filename in ('rejected-dedup.csv', 'rejected-values.csv'):
            with self.subTest(candidate=filename):
                bad_bytes = (FIXTURES / 'input-handoff' / filename).read_bytes()

                class BadPreparation(CountingRegistry):
                    def execute(self, capability, inputs, work_dir):
                        outputs = super().execute(capability, inputs, work_dir)
                        if capability == 'orders.prepare':
                            outputs['prepared'].write_bytes(bad_bytes)
                            mapping = read_json(outputs['mapping'])
                            # A malicious producer may claim a fresh target identity.
                            # Checks must still compare original records and amounts.
                            mapping['prepared']['sha256'] = hashlib.sha256(bad_bytes).hexdigest()
                            mapping['prepared']['rows'] = len(list(csv.reader(io.StringIO(bad_bytes.decode())))) - 1
                            write_json(outputs['mapping'], mapping)
                        return outputs

                registry = BadPreparation()
                runtime = self.app(registry=registry, directory=filename)
                run = runtime.advance(self.start(runtime)['id'])
                self.assertEqual(run['state'], 'failed')
                self.assertEqual(run['error']['code'], 'verification_failed')
                self.assertEqual(registry.executed, ['orders.prepare'])
                self.assertEqual(run['usage']['model_calls'], 0)
                operation = next(iter(run['operations'].values()))
                self.assertFalse(operation['verification']['passed'])
                codes = {item['code'] for item in operation['verification']['diagnostics']}
                self.assertIn('row_count_mismatch' if filename == 'rejected-dedup.csv' else 'prepared_value_mismatch', codes)
                self.assertFalse(any(call['consumed'] for call in run['calls'].values()))
                self.assertEqual(run['tasks'][run['root']]['steps'], {})
                self.assertEqual(run['tasks'][run['root']]['outputs'], {})

    def test_committed_preparation_candidate_is_rechecked_after_crash_without_reexecution(self):
        def hook(kind, run):
            if kind == 'operation_candidate' and any(op['capability'] == 'orders.prepare' for op in run['operations'].values()):
                raise Crash()

        runtime = self.app(hook=hook)
        run = self.start(runtime, definition=json_report_definition('fixed'))
        with self.assertRaises(Crash):
            runtime.advance(run['id'])
        before = runtime.store.load(run['id'])
        operation = next(iter(before['operations'].values()))
        self.assertEqual(operation['capability'], 'orders.prepare')
        self.assertEqual(operation['status'], 'candidate')
        self.assertNotIn('verification', operation)
        candidate_outputs = operation['outputs']

        class NoPreparationReplay(CountingRegistry):
            def execute(self, capability, inputs, work_dir):
                if capability == 'orders.prepare':
                    raise AssertionError('Preparation was replayed after a durable candidate')
                return super().execute(capability, inputs, work_dir)

        resumed_registry = NoPreparationReplay()
        runtime = self.app(registry=resumed_registry, agent=ForbiddenAgent(), planner=ForbiddenPlanner())
        done = runtime.advance(run['id'])
        self.assert_report(runtime, done)
        saved_operation = done['operations'][operation['id']]
        self.assertEqual(saved_operation['outputs'], candidate_outputs)
        self.assertTrue(saved_operation['verification']['passed'])
        self.assertEqual(saved_operation['status'], 'committed')
        self.assertEqual(resumed_registry.executed, ['orders.profile', 'orders.normalize', 'orders.deduplicate', 'orders.summarize'])
        self.assertEqual(len(done['operations']), 5)
        self.assertEqual(runtime.advance(run['id']), done)

    def test_parent_still_rejects_wrong_business_report_after_prepare_and_clean_succeed(self):
        class WrongSummary(CountingRegistry):
            def execute(self, capability, inputs, work_dir):
                outputs = super().execute(capability, inputs, work_dir)
                if capability == 'orders.summarize':
                    quality = read_json(outputs['quality'])
                    quality['total_net_cents'] += 1
                    write_json(outputs['quality'], quality)
                return outputs

        runtime = self.app(registry=WrongSummary())
        run = runtime.advance(self.start(runtime)['id'])
        self.assertEqual(run['state'], 'failed')
        self.assertEqual(run['error']['code'], 'verification_failed')
        root = run['tasks'][run['root']]
        self.assertFalse(root['verification']['passed'])
        self.assertEqual({task['task'] for task in run['tasks'].values() if task['parent_call'] and task['state'] == 'succeeded'},
                         {'orders.prepare', 'orders.clean'})
        self.assertIn('quality_mismatch', {item['code'] for item in root['verification']['reports'][0]['diagnostics']})


if __name__ == '__main__':
    unittest.main()
