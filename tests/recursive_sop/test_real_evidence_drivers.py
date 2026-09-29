"""Evidence-driver regression tests; every model port is disabled or a declared double."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import verify_recursive_sop as recursive
from scripts import verify_sop_extensions as extensions
from sop.common import SopError, digest, read_json


class EvidenceDriverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.baseline = tempfile.TemporaryDirectory()
        cls.base = Path(cls.baseline.name)
        # Exercise real state, artifacts and independent business checkers. Any
        # accidental provider construction is a test failure, never a network call.
        with patch.object(recursive, 'PiAgent', side_effect=AssertionError('No network')), \
             patch.object(recursive, 'PiPlanner', side_effect=AssertionError('No network')), \
             patch.object(extensions, 'PiAgent', side_effect=AssertionError('No network')):
            cls.recursive_result = recursive.verify(cls.base/'recursive')
            cls.extension_result = extensions.verify(cls.base/'extensions')

    @classmethod
    def tearDownClass(cls):
        cls.baseline.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.output = Path(self.tmp.name)/'evidence'

    def archived(self, name):
        case = self.base/'recursive'/name
        return read_json(case/'run.json'), read_json(case/'events.json')

    def assert_failure_archived(self, result, directory, expected_remaining):
        self.assertFalse(result['complete'])
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(len(result['cases']), 1)
        self.assertEqual(len(result['not_run_cases']), expected_remaining)
        self.assertEqual(read_json(self.output/'summary.json'), result)
        self.assertEqual(read_json(directory/'assessment.json'), result['cases'][0])
        self.assertTrue((directory/'run.json').is_file())
        self.assertTrue((directory/'events.json').is_file())
        self.assertFalse(result['cases'][0]['passed'])

    def test_default_paths_keep_six_recursive_and_five_extension_cases(self):
        for result, count in ((self.recursive_result, 6), (self.extension_result, 5)):
            self.assertTrue(result['complete'], result)
            self.assertEqual(len(result['cases']), count)
            self.assertEqual(result['not_run_cases'], [])
            self.assertTrue(all(c['passed'] and all(c['path_checks'].values()) for c in result['cases']))
        records = self.recursive_result['cases']
        self.assertCountEqual(records[0]['method_origins'], ['explicit','existing'])
        self.assertCountEqual(records[2]['method_origins'], ['explicit','generated'])
        self.assertCountEqual(records[3]['method_origins'], ['explicit','candidate_reuse'])
        saved = read_json(self.base/'recursive/generated-candidate.json')
        run, _ = self.archived('generated-batch-02')
        children = [t for t in run['tasks'].values() if t['parent_call']]
        self.assertEqual(children[0]['definition']['hash'], digest(saved))

    def test_recursive_expected_mismatch_is_a_saved_failure_not_an_exception(self):
        original = recursive.read_json
        def wrong_expected(path):
            if Path(path).name == 'quality.json' and Path(path).parent.name == 'expected':
                return {'wrong_expected_value': True}
            return original(path)
        with patch.object(recursive, 'read_json', side_effect=wrong_expected):
            result = recursive.verify(self.output)
        case = self.output/'existing-batch-01'
        self.assert_failure_archived(result, case, 5)
        self.assertEqual(result['cases'][0]['state'], 'succeeded')  # business vs external oracle
        self.assertFalse(result['cases'][0]['expected_comparison']['quality'])
        self.assertIn('expected_mismatch', result['cases'][0]['validation_errors'])
        self.assertTrue(read_json(case/'events.json'))

    def test_recursive_read_error_retains_prior_comparisons_run_events_and_not_run(self):
        original = recursive.read_json
        def unreadable_expected(path):
            if Path(path).name == 'quality.json' and Path(path).parent.name == 'expected':
                raise OSError('Synthetic expected-file read failure')
            return original(path)
        with patch.object(recursive, 'read_json', side_effect=unreadable_expected):
            result = recursive.verify(self.output)
        case = self.output/'existing-batch-01'
        self.assert_failure_archived(result, case, 5)
        self.assertEqual(result['cases'][0]['error']['code'], 'io_error')
        self.assertEqual(read_json(case/'run.json')['state'], 'succeeded')
        self.assertIn('cleaned', result['cases'][0]['expected_comparison'])
        self.assertTrue(read_json(case/'events.json'))

    def test_advance_exception_archives_latest_durable_state(self):
        application = recursive.application
        def interrupted(*args, **kwargs):
            rt = application(*args, **kwargs)
            advance = rt.advance
            def fail_after_commit(*args, **kwargs):
                advance(*args, **kwargs)
                raise SopError('synthetic_transport_failure', 'No model or network request')
            rt.advance = fail_after_commit
            return rt
        with patch.object(recursive, 'application', side_effect=interrupted):
            result = recursive.verify(self.output)
        case = self.output/'existing-batch-01'
        self.assert_failure_archived(result, case, 5)
        run = read_json(case/'run.json')
        self.assertEqual(run['state'], 'waiting_info')
        self.assertEqual(run['usage'], result['cases'][0]['usage'])
        self.assertGreater(run['revision'], 0)
        self.assertEqual(result['cases'][0]['error']['code'], 'synthetic_transport_failure')
        self.assertTrue(any(e['kind']=='waiting_info' for e in read_json(case/'events.json')))

    def test_waiting_after_answer_attempt_stops_batch_as_unfinished(self):
        application = recursive.application
        def unanswered(*args, **kwargs):
            rt = application(*args, **kwargs)
            rt.answer = lambda *args, **kwargs: None  # Simulated undelivered answer; durable question stays open.
            return rt
        with patch.object(recursive, 'application', side_effect=unanswered):
            result = recursive.verify(self.output)
        self.assert_failure_archived(result, self.output/'existing-batch-01', 5)
        self.assertEqual(result['cases'][0]['state'], 'waiting_info')
        self.assertIn('unexpected_state:waiting_info', result['cases'][0]['validation_errors'])

    def test_generated_path_requires_planner_returned_child_and_exact_reuse(self):
        run, events = self.archived('generated-batch-01')
        checks, _ = recursive._recursive_checks(run, [e for e in events if e['kind']!='planner_dispatched'], 'generated','batch-01',None)
        self.assertFalse(checks['current_planner_generated'])
        child = next(t for t in run['tasks'].values() if t['parent_call'])
        child['accepted']['origin'] = 'existing'
        run['calls'][child['parent_call']]['consumed'] = False
        run['tasks'][run['root']]['verification']['passed'] = False
        checks, _ = recursive._recursive_checks(run,events,'generated','batch-01',None)
        self.assertFalse(checks['child_method_origin'])
        self.assertFalse(checks['child_return_consumed'])
        self.assertFalse(checks['parent_verified'])
        run, events = self.archived('generated-batch-02')
        saved = read_json(self.base/'recursive/generated-candidate.json')
        wrong = deepcopy(saved)
        wrong['id'] += '-different'
        checks, _ = recursive._recursive_checks(run,events,'generated','batch-02',wrong)
        self.assertFalse(checks['same_saved_candidate'])

    def test_extension_exception_is_preserved_before_remaining_cases(self):
        original = extensions.read_json
        def unreadable_expected(path):
            if Path(path).name == 'quality.json' and Path(path).parent.name == 'expected':
                raise SopError('expected_unavailable', 'Synthetic oracle read error')
            return original(path)
        with patch.object(extensions, 'read_json', side_effect=unreadable_expected):
            result = extensions.verify(self.output)
        case = self.output/'json-existing'
        self.assert_failure_archived(result, case, 4)
        self.assertEqual(result['cases'][0]['error']['code'], 'expected_unavailable')
        self.assertEqual(read_json(case/'run.json')['state'], 'succeeded')
        self.assertTrue(read_json(case/'events.json'))

    def test_direct_path_requires_committed_tools_and_no_child_graph(self):
        case = self.base/'extensions/direct-batch-01'
        run, events = read_json(case/'run.json'), read_json(case/'events.json')
        for op in run['operations'].values():
            if op.get('origin') == 'agent':
                op['status'] = 'candidate'
        checks = extensions._extension_checks(run,events,'direct')
        self.assertFalse(checks['direct_tools_committed'])
        run['calls']['synthetic-child'] = {}
        checks = extensions._extension_checks(run,events,'direct')
        self.assertFalse(checks['no_child_graph'])

    def test_pi_branch_failure_is_recorded_without_a_real_provider(self):
        class FailedPort:
            max_model_calls_per_request = 1
            identity = {'backend':'scripted','purpose':'driver_failure_test'}
            last_evidence = {'backend':'scripted','status':'backend_unavailable'}
            def respond_with_tools(self, context, execute):
                raise SopError('backend_unavailable','Synthetic unavailable provider')
        # Exercise the explicit pi branch with a clearly declared injected port.
        with patch.object(extensions, 'PiAgent', return_value=FailedPort()) as factory:
            result = extensions.verify(self.output, backend='pi')
        self.assertEqual(factory.call_count, 1)
        self.assert_failure_archived(result, self.output/'direct-batch-01', 1)
        self.assertEqual(result['cases'][0]['state'], 'failed')
        self.assertEqual(result['cases'][0]['error']['code'], 'backend_unavailable')
        self.assertEqual(result['not_run_cases'], ['direct-batch-02'])


if __name__ == '__main__':
    unittest.main()
