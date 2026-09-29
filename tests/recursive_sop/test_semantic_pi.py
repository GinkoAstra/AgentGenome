"""Python adapter boundary evidence only; subprocess transport is mocked."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from sop.agents import PiAuthor, PiSemanticReviewer, _safe_context
from sop.common import SopError
from sop.skill_authoring import CLAUSES, public_bundle
from sop.sources import collect_sources


class SemanticPiAdapterTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root/'SKILL.md').write_text('# Orders\nSee [details](details.md).\n')
        (self.root/'details.md').write_text('The retention rule must be asked.\n')
        self.sources = public_bundle(collect_sources(self.root/'SKILL.md'))
        self.analysis = {'schema': 'skill-analysis/1', 'task': 'orders.report', 'mode': 'pi',
                         'annotations': [], 'rule': {'status': 'unknown', 'value': None}}
        self.judgment = {'schema': 'skill-review/1', 'basis_hash': 'c'*64,
                         'verdict': 'reject', 'findings': ['The test interpretation lacks coverage.']}

    def response(self, result):
        return subprocess.CompletedProcess([], 0, stdout=json.dumps({
            'ok': True, 'result': result, 'result_payload': json.dumps(result),
            'evidence': {'backend': 'scripted_transport_test', 'model_calls': 1,
                         'provider': 'test', 'model': 'test', 'pi_version': '0.80.6'}}), stderr='')

    def test_author_and_reviewer_receive_public_sources_in_separate_requests(self):
        contexts = []
        def transport(command, **kwargs):
            request = json.loads(kwargs['input'])
            contexts.append(request)
            self.assertEqual(kwargs['cwd'], '/tmp')
            self.assertNotIn(str(self.root), kwargs['input'])
            return self.response(self.analysis if request['mode'] == 'author' else self.judgment)
        author, reviewer = PiAuthor(), PiSemanticReviewer()
        with patch('sop.agents.subprocess.run', side_effect=transport):
            result = author.author({'sources': self.sources, 'clauses': CLAUSES,
                                    'catalog': {}, 'scope': 'orders.report', 'limits': {'max_model_calls': 12}})
            judgment = reviewer.review({'sources': self.sources, 'clauses': CLAUSES, 'analysis': result,
                                        'answers': {}, 'basis_hash': 'c'*64, 'instruction': 'Review all original source spans.'})
        self.assertEqual(result, self.analysis)
        self.assertEqual(judgment, self.judgment)
        self.assertEqual([item['mode'] for item in contexts], ['author', 'review'])
        self.assertEqual(author.calls, 1)
        self.assertEqual(reviewer.calls, 1)
        self.assertEqual(contexts[0]['context']['sources'], contexts[1]['context']['sources'])
        self.assertNotIn('analysis', contexts[0]['context'])
        self.assertEqual({file['id'] for file in self.sources['files']}, {'SKILL.md', 'details.md'})
        self.assertNotIn('_usage', result)
        self.assertNotIn('_trace', judgment)

    def test_public_bundle_rejects_native_paths_and_unsafe_relative_identities(self):
        bundle = collect_sources(self.root/'SKILL.md')
        with self.assertRaises(SopError):
            _safe_context({'sources': bundle})
        for value in ('/etc/passwd', '../outside.md', 'docs/../../outside.md', 'C:/secret.md', 'docs\\secret.md'):
            for key in ('id', 'relative_path'):
                with self.subTest(value=value, key=key):
                    sources = deepcopy(self.sources)
                    sources['files'][0][key] = value
                    with self.assertRaises(SopError) as error:
                        _safe_context({'sources': sources})
                    self.assertEqual(error.exception.code, 'invalid_agent_context')
        self.assertEqual(_safe_context({'sources': self.sources})['sources'], self.sources)

    def test_semantic_context_does_not_enable_answer_keys_or_native_paths(self):
        for field in ('expected', 'expected_path', 'answer_file', 'source_path', 'work_dir', 'data_dir'):
            with self.subTest(field=field), self.assertRaises(SopError):
                _safe_context({'sources': self.sources, 'analysis': {field: '/tmp/secret'}})

    def test_semantic_adapters_reject_wrong_result_protocol_and_duplicate_keys(self):
        for port, method in ((PiAuthor(), 'author'), (PiSemanticReviewer(), 'review')):
            with self.subTest(mode=port.mode):
                with patch('sop.agents.subprocess.run', return_value=self.response({'kind': 'candidate_result', 'outputs': {}})):
                    with self.assertRaises(SopError) as error:
                        getattr(port, method)({'sources': self.sources})
                    self.assertEqual(error.exception.code, 'backend_protocol')
                duplicate = '{"schema":"skill-analysis/1","schema":"skill-review/1"}'
                returned = {'ok': True, 'result': {'schema': 'skill-review/1'}, 'result_payload': duplicate, 'evidence': {}}
                with patch('sop.agents.subprocess.run', return_value=subprocess.CompletedProcess([], 0, stdout=json.dumps(returned), stderr='')):
                    with self.assertRaises(SopError) as error:
                        getattr(port, method)({'sources': self.sources})
                    self.assertEqual(error.exception.code, 'backend_protocol')

    def test_backend_failures_are_preserved_without_fallback(self):
        failure = {'ok': False, 'error': {'code': 'backend_error', 'message': 'Provider rejected request'},
                   'evidence': {'backend': 'pi', 'provider_error': {'http_status': 403}, 'model_calls': 1}}
        for port, method in ((PiAuthor(), 'author'), (PiSemanticReviewer(), 'review')):
            with self.subTest(mode=port.mode):
                with patch('sop.agents.subprocess.run', return_value=subprocess.CompletedProcess([], 1, stdout=json.dumps(failure), stderr='')) as transport:
                    with self.assertRaises(SopError) as error:
                        getattr(port, method)({'sources': self.sources})
                    self.assertEqual(error.exception.code, 'backend_error')
                    self.assertEqual(port.last_evidence['provider_error']['http_status'], 403)
                    self.assertEqual(transport.call_count, 1)


if __name__ == '__main__':
    unittest.main()
