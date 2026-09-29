"""Acceptance-driver integrity; all injected ports are explicit program doubles."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.verify_sop_semantics import verify
from sop.common import SopError, file_digest, read_json, write_json
from sop.skill_authoring import bundle_hash
from sop.sources import collect_sources


class Author:
    max_model_calls_per_request = 1
    identity = {'backend': 'scripted', 'real_model': False}
    last_evidence = {'backend': 'scripted', 'real_model': False}

    def __init__(self, analysis=None, error=None):
        self.analysis, self.error, self.contexts = analysis, error, []

    def author(self, context):
        self.contexts.append(deepcopy(context))
        if self.error:
            raise SopError(self.error, 'Synthetic failure, no provider request')
        return deepcopy(self.analysis)


class Reviewer:
    max_model_calls_per_request = 1
    identity = {'backend': 'scripted', 'real_model': False}
    last_evidence = {'backend': 'scripted', 'real_model': False}

    def __init__(self):
        self.contexts = []

    def review(self, context):
        self.contexts.append(deepcopy(context))
        return {'schema': 'skill-review/1', 'basis_hash': context['basis_hash'], 'verdict': 'pass', 'findings': []}


class SemanticEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.suite = self.root / 'suite'
        self.labels = []
        self.analyses = []

    def add_case(self, status='known', reject=False):
        name = f'case-{len(self.labels)+1:02d}'
        case = self.suite / 'cases' / name
        case.mkdir(parents=True)
        # Compact hand-labelled protocol fixtures, never semantic-quality claims.
        rows = [
            ('Columns order_id/customer_id/revision/gross_cents/refund_cents, integer revision and integer cents, no float or guessed missing values.', ['input']),
            ('Trim customer_id ends only; retain other fields and characters.', ['normalize']),
            ('Reject differing normalized customers across any revisions of an order.', ['cross_customer']),
            ('One row per order under the accepted rule; net=gross-refund; ascending order_id.', ['clean']),
            ('Group normalized customer, count orders and gross/refund/net totals; ascending customer_id.', ['summary']),
            ('Input/retained/removed counts and monetary totals; independently recalculate outputs from original source.', ['quality']),
            ('Local read-only input, run-owned outputs; no upload/network/delete or external effects.', ['permissions']),
            ('Use only registered fixed business implementations.', ['fixed']),
        ]
        if status == 'known':
            rows.append(('Keep maximum integer revision, not last row; identical maximum ties collapse, conflicting ties reject.', ['rule_highest_revision']))
        if status == 'runtime':
            rows.append(('Each run must explicitly choose first record or maximum integer revision, no default. Check all customer revisions, identical maximum ties collapse, conflicting maximum ties reject.', ['rule_runtime']))
        if reject:
            rows.append(('Additionally discard any order with net below 100 cents.', []))
        source = case / 'SKILL.md'
        source.write_text('\n'.join(row[0] for row in rows)+'\n')
        analysis = {'schema': 'skill-analysis/1', 'task': 'orders.report', 'mode': 'fixed',
                    'rule': {'status': status, 'value': 'highest_revision' if status == 'known' else None},
                    'annotations': [{'source_id': 'SKILL.md', 'start_line': n, 'end_line': n,
                                     'kind': 'requirement' if clauses else 'uninterpreted', 'clauses': clauses,
                                     'reason': 'Program test fixture, not model interpretation'}
                                    for n, (_, clauses) in enumerate(rows, 1)]}
        self.analyses.append(analysis)
        self.labels.append({'id': name, 'initial_state': 'stopped' if reject else 'waiting_info' if status == 'unknown' else 'delivered',
                            'mode': 'fixed', 'rule_status': status,
                            'rule_value': 'highest_revision' if status == 'known' else None,
                            'answer': 'highest_revision' if status == 'unknown' else None,
                            'reject_spans': [{'file': 'SKILL.md', 'line': len(rows)}] if reject else [],
                            'rationale': 'HOST-ONLY-ANSWER-KEY-SENTINEL'})
        return source

    def freeze(self):
        labels = self.suite / 'labels.json'
        write_json(labels, {'schema': 'semantic-authoring-labels/1', 'cases': self.labels})
        cases = {}
        for label in self.labels:
            bundle = collect_sources(self.suite / 'cases' / label['id'] / 'SKILL.md')
            cases[label['id']] = {'files': {f['relative_path']: f['sha256'] for f in bundle['files']},
                                  'bundle_hash': bundle_hash(bundle)}
        write_json(self.suite / 'manifest.json', {'schema': 'semantic-authoring-manifest/1',
                   'labels_sha256': file_digest(labels), 'cases': cases})

    def run_double(self, author, reviewer=None, **kwargs):
        return verify(self.root / 'output', suite=self.suite, backend='test_double',
                      ports_factory=lambda: (author, reviewer or Reviewer()), **kwargs)

    def test_default_plan_does_not_construct_pi_or_claim_model_success(self):
        self.add_case()
        self.freeze()
        with patch('scripts.verify_sop_semantics.PiAuthor', side_effect=AssertionError('Network boundary')):
            result = verify(self.root / 'output', suite=self.suite)
        self.assertTrue(result['preparation_valid'])
        self.assertFalse(result['complete'])
        self.assertFalse(result['real_model'])
        self.assertEqual(result['cases'], [])

    def test_source_or_label_drift_rejected_before_dispatch(self):
        source = self.add_case()
        self.freeze()
        author = Author(self.analyses[0])
        source.write_text(source.read_text()+'Extra requirement\n')
        with self.assertRaises(SopError) as caught:
            self.run_double(author)
        self.assertEqual(caught.exception.code, 'evaluation_changed')
        self.assertEqual(author.contexts, [])
        self.freeze()
        with (self.suite / 'labels.json').open('a') as handle:
            handle.write(' ')
        with self.assertRaises(SopError) as caught:
            self.run_double(author)
        self.assertEqual(caught.exception.code, 'evaluation_changed')
        self.assertEqual(author.contexts, [])

    def test_provider_failure_stops_batch_and_preserves_failure_denominator(self):
        self.add_case(reject=True)
        self.add_case()
        self.freeze()
        author = Author(error='backend_error')
        result = self.run_double(author)
        self.assertEqual(len(author.contexts), 1)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['not_run_cases'], ['case-02'])
        self.assertFalse(result['cases'][0]['passed'])
        self.assertEqual(read_json(self.root / 'output/case-01/final.json')['state'], 'stopped')
        self.assertEqual(result['cases'][0]['usage']['model_calls'], 1)

    def test_answer_key_isolated_until_scoped_question_and_answer_rechecked(self):
        self.add_case(status='unknown')
        self.freeze()
        author, reviewer = Author(self.analyses[0]), Reviewer()
        result = self.run_double(author, reviewer)
        self.assertTrue(result['complete'], result)
        self.assertFalse(result['real_model'])
        self.assertEqual(author.contexts[0]['answers'], {})
        self.assertEqual(reviewer.contexts[0]['answers'], {})
        self.assertEqual(len(reviewer.contexts), 2)
        self.assertEqual(reviewer.contexts[1]['answers']['rule']['value'], 'highest_revision')
        for context in author.contexts + reviewer.contexts:
            self.assertNotIn('HOST-ONLY-ANSWER-KEY-SENTINEL', str(context))
            self.assertNotIn('labels.json', str(context))
        self.assertEqual(result['cases'][0]['usage']['model_calls'], 3)
        self.assertTrue((self.root/'output/case-01/answer-message.json').exists())

    def test_oracle_detects_wrong_rule_even_if_scripted_reviewer_passes(self):
        self.add_case()
        self.freeze()
        analysis = self.analyses[0]
        analysis['rule']['value'] = 'first'
        analysis['annotations'][-1]['clauses'] = ['rule_first']
        result = self.run_double(Author(analysis))
        self.assertEqual(result['cases'][0]['final_state'], 'delivered')
        self.assertIn('retention_interpretation_mismatch', result['cases'][0]['validation_errors'])
        self.assertIn('compiled_rule_domain_mismatch', result['cases'][0]['validation_errors'])
        self.assertFalse(result['complete'])

    def test_runtime_choice_stays_parameterized_without_authoring_answer(self):
        self.add_case(status='runtime')
        self.freeze()
        result = self.run_double(Author(self.analyses[0]))
        self.assertTrue(result['complete'], result)
        self.assertFalse((self.root/'output/case-01/answer-message.json').exists())

    def test_correct_rejection_requires_the_extra_constraint_source_span(self):
        self.add_case(reject=True)
        self.freeze()
        result = self.run_double(Author(self.analyses[0]))
        self.assertTrue(result['complete'], result)
        self.assertEqual(result['cases'][0]['diagnostic_codes'], ['unsupported_requirement'])

    def test_unrelated_parse_failure_does_not_count_as_correct_rejection(self):
        self.add_case(reject=True)
        self.freeze()
        result = self.run_double(Author({'schema': 'broken'}))
        self.assertFalse(result['complete'])
        self.assertIn('missing_located_semantic_rejection', result['cases'][0]['validation_errors'])

    def test_review_budget_exhaustion_is_retained_without_reset_or_retry(self):
        self.add_case()
        self.freeze()
        author, reviewer = Author(self.analyses[0]), Reviewer()
        result = self.run_double(author, reviewer, max_model_calls=1)
        self.assertFalse(result['complete'])
        self.assertEqual(result['cases'][0]['usage']['model_calls'], 1)
        self.assertEqual(len(author.contexts), 1)
        self.assertEqual(reviewer.contexts, [])
        self.assertIn('budget_exhausted', result['cases'][0]['diagnostic_codes'])


if __name__ == '__main__':
    unittest.main()
