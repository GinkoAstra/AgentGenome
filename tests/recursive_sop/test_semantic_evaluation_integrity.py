"""Regression cases for frozen evaluation identity and located rejection.

All ports are explicit program doubles. Temporary suites are mutated only to
exercise the evaluator's boundaries; no production case or provider is used.
"""
from copy import deepcopy
import unittest
from unittest.mock import patch

import test_semantic_evaluation as fixtures
from scripts.verify_sop_semantics import load_suite, verify
from sop.common import file_digest, read_json
from sop.skill_authoring import bundle_hash
from sop.sources import collect_sources


class RejectingReviewer(fixtures.Reviewer):
    def __init__(self, finding):
        super().__init__()
        self.finding = finding

    def review(self, context):
        self.contexts.append(deepcopy(context))
        return {'schema': 'skill-review/1', 'basis_hash': context['basis_hash'],
                'verdict': 'reject', 'findings': [deepcopy(self.finding)]}


class SemanticEvaluationIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SemanticEvaluationTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        for port in ('PiAuthor', 'PiSemanticReviewer'):
            guard = patch('scripts.verify_sop_semantics.' + port,
                          side_effect=AssertionError('Real provider construction forbidden'))
            guard.start()
            self.addCleanup(guard.stop)

    def test_missing_reason_and_out_of_bounds_rejection_cannot_pass(self):
        fixture = self.fixture
        fixture.add_case(reject=True)
        fixture.freeze()
        target_line = fixture.labels[0]['reject_spans'][0]['line']
        # Make the author omit the extra constraint so the independent review
        # is the only source of a rejection, then inspect its actual evidence.
        analysis = deepcopy(fixture.analyses[0])
        analysis['annotations'][-1]['kind'] = 'background'
        findings = [
            {'source_id': 'SKILL.md', 'start_line': target_line, 'end_line': target_line},
            {'source_id': 'SKILL.md', 'start_line': target_line, 'end_line': target_line,
             'reason': '   '},
            {'source_id': 'SKILL.md', 'start_line': -99, 'end_line': 999999,
             'reason': 'The source contains an unsupported constraint.'},
        ]
        for index, finding in enumerate(findings):
            with self.subTest(finding=finding):
                author, reviewer = fixtures.Author(analysis), RejectingReviewer(finding)
                result = verify(fixture.root / f'output-{index}', suite=fixture.suite,
                                backend='test_double', ports_factory=lambda: (author, reviewer))
                entry = result['cases'][0]
                self.assertEqual(result['status'], 'failed', result)
                self.assertFalse(result['complete'])
                self.assertFalse(entry['passed'])
                self.assertEqual(entry['diagnostic_codes'], ['semantic_review_rejected'])
                self.assertIn('missing_located_semantic_rejection', entry['validation_errors'])
                self.assertEqual(entry['infrastructure_errors'], [])
                self.assertEqual(len(reviewer.contexts), 1)

    def test_in_bounds_whole_source_rejection_cannot_pass(self):
        fixture = self.fixture
        source = fixture.add_case(reject=True)
        fixture.freeze()
        line_count = len(source.read_text().splitlines())
        analysis = deepcopy(fixture.analyses[0])
        analysis['annotations'] = [
            {'source_id': 'SKILL.md', 'start_line': 1, 'end_line': line_count,
             'kind': 'uninterpreted', 'clauses': [],
             'reason': 'The document includes something unsupported.'},
        ]
        author, reviewer = fixtures.Author(analysis), fixtures.Reviewer()
        result = fixture.run_double(author, reviewer)
        entry = result['cases'][0]
        self.assertEqual(result['status'], 'failed', result)
        self.assertFalse(result['complete'])
        self.assertFalse(entry['passed'])
        self.assertEqual(entry['diagnostic_codes'], ['unsupported_requirement'])
        self.assertIn('missing_located_semantic_rejection', entry['validation_errors'])
        final = read_json(fixture.root / 'output/case-01/final.json')
        span = final['diagnostics'][0]['source_spans'][0]
        self.assertEqual((span['start_line'], span['end_line']), (1, line_count))
        self.assertEqual(reviewer.contexts, [])

    def test_consistent_manifest_and_source_replacement_stops_batch(self):
        fixture = self.fixture
        fixture.add_case()
        next_source = fixture.add_case()
        fixture.freeze()
        frozen_manifest_sha = file_digest(fixture.suite / 'manifest.json')
        author, reviewer = fixtures.Author(fixture.analyses[0]), fixtures.Reviewer()
        dispatches = []

        def replace_suite_after_validation():
            dispatches.append(True)
            next_source.write_text(next_source.read_text().replace('Columns ', 'Input columns ', 1))
            fixture.freeze()
            return author, reviewer

        result = verify(fixture.root / 'output', suite=fixture.suite, backend='test_double',
                        ports_factory=replace_suite_after_validation)
        # The replacement is internally consistent and would pass a fresh load;
        # the original batch must retain its original frozen identity anyway.
        load_suite(fixture.suite)
        self.assertNotEqual(file_digest(fixture.suite / 'manifest.json'), frozen_manifest_sha)
        self.assertEqual(result['suite_manifest_sha256'], frozen_manifest_sha)
        self.assertEqual(file_digest(fixture.root / 'output/suite-manifest.json'), frozen_manifest_sha)
        self.assertEqual(result['status'], 'blocked', result)
        self.assertFalse(result['complete'])
        self.assertEqual(len(result['cases']), 1)
        self.assertFalse(result['cases'][0]['passed'])
        self.assertEqual(result['cases'][0]['final_state'], 'delivered')
        self.assertIn('evaluation_changed', result['cases'][0]['infrastructure_errors'])
        self.assertEqual(result['not_run_cases'], ['case-02'])
        self.assertEqual(len(dispatches), 1)
        self.assertEqual((len(author.contexts), len(reviewer.contexts)), (1, 1))

    def test_source_change_in_submission_window_blocks_bundle_mismatch(self):
        fixture = self.fixture
        source = fixture.add_case()
        fixture.add_case()
        fixture.freeze()
        original_bundle_hash = bundle_hash(collect_sources(source))
        original_manifest_sha = file_digest(fixture.suite / 'manifest.json')
        author, reviewer = fixtures.Author(fixture.analyses[0]), fixtures.Reviewer()
        dispatches = []

        def change_source_before_submit():
            dispatches.append(True)
            source.write_text(source.read_text().replace('Columns ', 'Input columns ', 1))
            return author, reviewer

        result = verify(fixture.root / 'output', suite=fixture.suite, backend='test_double',
                        ports_factory=change_source_before_submit)
        initial = read_json(fixture.root / 'output/case-01/initial.json')
        final = read_json(fixture.root / 'output/case-01/final.json')
        changed_bundle_hash = bundle_hash(collect_sources(source))
        self.assertNotEqual(changed_bundle_hash, original_bundle_hash)
        self.assertEqual(initial['bundle_hash'], changed_bundle_hash)
        self.assertEqual(final['bundle_hash'], changed_bundle_hash)
        self.assertEqual(file_digest(fixture.suite / 'manifest.json'), original_manifest_sha)
        self.assertEqual(result['status'], 'blocked', result)
        self.assertFalse(result['complete'])
        self.assertEqual(len(result['cases']), 1)
        entry = result['cases'][0]
        self.assertEqual(entry['final_state'], 'delivered')
        self.assertFalse(entry['passed'])
        self.assertIn('frozen_source_identity_mismatch', entry['validation_errors'])
        self.assertIn('evaluation_changed', entry['infrastructure_errors'])
        self.assertEqual(result['not_run_cases'], ['case-02'])
        self.assertEqual(len(dispatches), 1)
        self.assertEqual((len(author.contexts), len(reviewer.contexts)), (1, 1))


if __name__ == '__main__':
    unittest.main()
