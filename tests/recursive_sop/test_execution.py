import csv
import json
import tempfile
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from sop.agents import PiAgent, PiPlanner, ScriptedAgent, ScriptedPlanner, _safe_context
from sop.capabilities import Registry, _reference
from sop.common import SopError

FIXTURES = Path(__file__).resolve().parents[2] / 'tests/fixtures/first-release'


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.registry = Registry()

    def tearDown(self):
        self.tmp.cleanup()

    def pipeline(self, source, rule='highest_revision'):
        normalized = self.registry.execute('orders.normalize', {'source_file': source}, self.root/'normalize')
        clean = self.registry.execute('orders.deduplicate', {**normalized, 'rule': rule}, self.root/'dedup')
        report = self.registry.execute('orders.summarize', {**clean, 'source_file': source}, self.root/'summary')
        return {**clean, **report}

    def source(self, body):
        path = self.root/'input.csv'
        path.write_text('order_id,customer_id,revision,gross_cents,refund_cents\n'+body, encoding='utf-8')
        return path

    def test_both_batches_exact_outputs_and_independent_acceptance(self):
        for batch in ('batch-01','batch-02'):
            with self.subTest(batch=batch), tempfile.TemporaryDirectory() as temp:
                self.root = Path(temp)
                source = FIXTURES/batch/'input.csv'
                outputs = self.pipeline(source)
                for name, path in outputs.items():
                    expected = FIXTURES/batch/'expected'/path.name
                    if name == 'quality':
                        self.assertEqual(json.loads(path.read_text()), json.loads(expected.read_text()))
                    else:
                        self.assertEqual(path.read_bytes(), expected.read_bytes())
                self.assertTrue(self.registry.verify('orders.report', {'source_file':source,'rule':'highest_revision'},outputs)['passed'])

    def test_checker_does_not_call_producer_helpers(self):
        source = FIXTURES/'batch-02/input.csv'
        outputs = self.pipeline(source)
        with patch('sop.capabilities._orders', side_effect=AssertionError('producer called')), patch('sop.capabilities._select', side_effect=AssertionError('producer called')):
            self.assertTrue(self.registry.verify('orders.report',{'source_file':source,'rule':'highest_revision'},outputs)['passed'])

    def test_wrong_rule_and_last_row_rejected(self):
        source = FIXTURES/'batch-02/input.csv'
        outputs = self.pipeline(source, 'first')
        result = self.registry.verify('orders.report',{'source_file':source,'rule':'highest_revision'},outputs)
        self.assertFalse(result['passed'])
        outputs['cleaned'].write_text((FIXTURES/'batch-02/expected/cleaned.csv').read_text().replace('O2002,C011,3,9000,2000,7000','O2002,C011,2,8500,1000,7500'))
        self.assertFalse(self.registry.verify('orders.clean',{'source_file':source,'rule':'highest_revision'},{'cleaned':outputs['cleaned']})['passed'])

    def test_conflicting_highest_revision_rejected_by_both_paths(self):
        source = FIXTURES/'conflict-highest-revision/input.csv'
        with self.assertRaises(SopError) as error:
            self.pipeline(source)
        self.assertEqual(error.exception.code,'conflicting_highest_revision')
        with self.assertRaises(SopError) as error:
            _reference(source,'highest_revision')
        self.assertEqual(error.exception.code,'conflicting_highest_revision')
        self.assertFalse((self.root/'dedup/cleaned.csv').exists())

    def test_customer_conflict_checks_all_revisions(self):
        source = self.source('O1,C1,1,100,0\nO1,C2,2,200,0\n')
        for method in (lambda: self.pipeline(source), lambda: _reference(source,'highest_revision')):
            with self.assertRaises(SopError) as error:
                method()
            self.assertEqual(error.exception.code,'customer_conflict')

    def test_identical_ties_and_full_refunds_are_allowed(self):
        source = self.source('O1, C1,2,100,100\nO1,C1 ,2,100,100\n')
        outputs=self.pipeline(source)
        self.assertTrue(self.registry.verify('orders.report',{'source_file':source,'rule':'highest_revision'},outputs)['passed'])
        self.assertEqual(json.loads(outputs['quality'].read_text())['total_net_cents'],0)

    def test_lower_revision_conflicts_do_not_poison_highest(self):
        source = self.source('O1,C1,1,100,0\nO1,C1,1,200,0\nO1,C1,2,300,0\n')
        outputs=self.pipeline(source)
        self.assertTrue(self.registry.verify('orders.report',{'source_file':source,'rule':'highest_revision'},outputs)['passed'])

    def test_nonintegers_rejected_without_rounding(self):
        for value in ('1.0','1e2','NaN','Infinity','',' 2','２'):
            with self.subTest(value=value):
                source=self.source(f'O1,C1,1,{value},0\n')
                with self.assertRaises(SopError) as error:
                    self.registry.execute('orders.normalize',{'source_file':source},self.root/('n'+str(len(value))))
                self.assertEqual(error.exception.code,'invalid_integer')
                with self.assertRaises(SopError):
                    _reference(source,'highest_revision')

    def test_unbounded_integer_arithmetic(self):
        amount=10**80+17
        source=self.source(f'O1,C1,1,{amount},17\n')
        outputs=self.pipeline(source)
        self.assertEqual(json.loads(outputs['quality'].read_text())['total_net_cents'],10**80)

    def test_strict_csv_schema_and_line_endings(self):
        cases = [b'order_id,customer_id,revision,gross_cents,refund_cents\r\nO1,C1,1,1,0\r\n',
                 b'order_id,customer_id,revision,gross_cents,refund_cents\nO1,C1,1,1,0,extra\n',
                 b'order_id,customer_id,revision,gross_cents,refund_cents\n"O1,C1,1,1,0\n',
                 b'order_id,customer_id,revision,gross_cents,refund_cents\nO1,C1,1,1,0']
        for index,data in enumerate(cases):
            source=self.root/f'malformed-{index}.csv'
            source.write_bytes(data)
            with self.assertRaises(SopError):
                self.registry.execute('orders.profile',{'source_file':source},self.root/f'p{index}')

    def test_same_totals_wrong_assignment_rejected(self):
        source=FIXTURES/'batch-01/input.csv'
        outputs=self.pipeline(source)
        text=outputs['cleaned'].read_text().replace('O1003,C001,1,5000,500,4500','O1003,C001,1,6000,500,5500').replace('O1005,C002,1,3000,0,3000','O1005,C002,1,2000,0,2000')
        outputs['cleaned'].write_text(text)
        self.assertFalse(self.registry.verify('orders.report',{'source_file':source,'rule':'highest_revision'},outputs)['passed'])

    def test_bool_quality_values_rejected(self):
        source=self.source('O1,C1,1,1,0\n')
        outputs=self.pipeline(source)
        quality=json.loads(outputs['quality'].read_text());quality['input_rows']=True
        outputs['quality'].write_text(json.dumps(quality))
        self.assertFalse(self.registry.verify('orders.report',{'source_file':source,'rule':'highest_revision'},outputs)['passed'])

    def test_hash_pin_tracks_dependency_changes(self):
        original=self.registry.versions()
        with patch('sop.capabilities.file_digest',return_value='changed'):
            self.assertNotEqual(original,self.registry.versions())

    def test_unknown_capability_and_checker_fail_closed(self):
        with self.assertRaises(SopError):
            self.registry.execute('shell',{},self.root)
        with self.assertRaises(SopError):
            self.registry.verify('no_check',{}, {})

    def test_symlink_and_existing_output_refused(self):
        source=FIXTURES/'batch-01/input.csv'
        linked=self.root/'linked.csv';linked.symlink_to(source)
        with self.assertRaises(SopError):
            self.registry.execute('orders.profile',{'source_file':linked},self.root/'l')
        self.registry.execute('orders.profile',{'source_file':source},self.root/'p')
        with self.assertRaises(SopError):
            self.registry.execute('orders.profile',{'source_file':source},self.root/'p')

    def test_scripted_agent_is_explicit_protocol_fixture(self):
        agent=ScriptedAgent()
        self.assertEqual(agent.identity['purpose'],'protocol_test_double')
        self.assertEqual(agent.respond({'task':'orders.clean','inputs':{}})['kind'],'need_info')
        self.assertEqual(agent.respond({'task':'orders.clean','inputs':{'rule':'highest_revision'}})['kind'],'need_subtask')
        self.assertEqual(agent.respond({'child_result':{'cleaned':{'hash':'a'*64}}})['kind'],'candidate_result')
        proposal=ScriptedPlanner().plan({'task':'orders.clean','catalog':self.registry.catalog()})
        self.assertEqual(proposal['checks'],['orders.clean'])

    def test_pi_context_rejects_paths_and_answer_keys(self):
        for context in ({'inputs':{'source_file':Path('/tmp/input')}}, {'inputs':{'source_path':'/tmp/input'}}, {'facts':{'expected':'secret'}}):
            with self.assertRaises(SopError):
                _safe_context(context)

    def test_pi_adapter_full_runtime_context_with_scripted_transport(self):
        # This exercises the real Python adapter and actual runtime context, while
        # replacing provider transport. It is expressly NOT real-model evidence.
        from sop.authoring import Library, clean_definition, root_definition
        from sop.service import application
        for generated in (False, True):
            with self.subTest(generated=generated), tempfile.TemporaryDirectory() as directory:
                contexts = []
                fixture_agent, fixture_planner = ScriptedAgent(), ScriptedPlanner()
                def transport(command, **kwargs):
                    request = json.loads(kwargs['input'])
                    contexts.append(request)
                    context = request['context']
                    proposal = fixture_planner.plan(context) if request['mode'] == 'planner' else fixture_agent.respond(context)
                    return subprocess.CompletedProcess(command, 0, stdout=json.dumps({
                        'ok': True, 'result': proposal, 'result_payload': json.dumps(proposal),
                        'evidence': {'backend': 'scripted_transport_test', 'model_calls': 1, 'usage': {}}}), stderr='')
                runtime = application(Path(directory), library=Library([clean_definition('first' if generated else None)]),
                                      agent=PiAgent(), planner=PiPlanner())
                def interactive_transport(command, request, execute, strict_loads, **kwargs):
                    # Runtime uses the interactive agent entry; both agent and planner
                    # boundaries must stay offline while exercising actual contexts.
                    return strict_loads(transport(command, input=json.dumps(request)).stdout)
                with patch('sop.agents.subprocess.run', side_effect=transport), \
                     patch('sop.pi_transport.invoke_interactive', side_effect=interactive_transport):
                    run = runtime.start(root_definition(), {'source_file': FIXTURES/'batch-02/input.csv'},
                                        capabilities=list(self.registry.catalog()))
                    waiting = runtime.advance(run['id'])
                    self.assertEqual(waiting['state'], 'waiting_info')
                    question_id = next(key for key, value in waiting['questions'].items() if value['state'] == 'open')
                    runtime.answer(run['id'], question_id, 'highest_revision', message_id='answer', revision=waiting['revision'])
                    done = runtime.advance(run['id'])
                    self.assertEqual(done['state'], 'succeeded')
                self.assertEqual(any(item['mode']=='planner' for item in contexts), generated)
                for request in contexts:
                    serialized=json.dumps(request['context'])
                    self.assertNotIn(str(FIXTURES), serialized)
                    self.assertNotIn('expected/', serialized)
                    self.assertNotIn('answer.json', serialized)
                self.assertTrue(any(request['context'].get('facts') for request in contexts))
                self.assertTrue(any(request['context'].get('child_result') for request in contexts))

    def test_pi_rejects_duplicate_handoff_keys_before_runtime(self):
        payload='{"kind":"cannot_continue","kind":"candidate_result","outputs":{}}'
        result={'ok':True,'result':{'kind':'candidate_result','outputs':{}},'result_payload':payload,'evidence':{}}
        with patch('sop.agents.subprocess.run', return_value=subprocess.CompletedProcess([],0,stdout=json.dumps(result),stderr='')):
            with self.assertRaises(SopError) as error:
                PiAgent().respond({'task':'orders.clean','inputs':{}})
            self.assertEqual(error.exception.code,'backend_protocol')

    def test_pi_failure_is_not_scripted_fallback(self):
        with patch('sop.agents.subprocess.run',side_effect=OSError('missing')):
            with self.assertRaises(SopError) as error:
                PiAgent().respond({'task':'orders.clean','inputs':{}})
            self.assertEqual(error.exception.code,'backend_unavailable')


if __name__ == '__main__':
    unittest.main()
