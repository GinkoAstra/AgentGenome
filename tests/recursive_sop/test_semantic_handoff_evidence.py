"""Real authoring storage and recursive runtime; all model ports are program doubles."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.verify_sop_handoff import source_identity, verify
from sop.agents import ScriptedAgent, ScriptedPlanner
from sop.capabilities import Registry
from sop.common import SopError, digest, read_json, write_json
from sop.skill_authoring import SemanticAuthoring


class Author:
    max_model_calls_per_request = 1
    identity = {'backend': 'scripted', 'real_model': False}
    last_evidence = {'backend': 'scripted', 'real_model': False}

    def __init__(self, analysis):
        self.analysis = analysis

    def author(self, context):
        return deepcopy(self.analysis)


class Reviewer:
    max_model_calls_per_request = 1
    identity = {'backend': 'scripted', 'real_model': False}
    last_evidence = {'backend': 'scripted', 'real_model': False}

    def review(self, context):
        return {'schema': 'skill-review/1', 'basis_hash': context['basis_hash'], 'verdict': 'pass', 'findings': []}


class SemanticHandoffEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sop-handoff-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.counter = 0

    def semantic_case(self, *, status='unknown', mode='pi', answer=True):
        self.counter += 1
        case = self.root / f'semantic-{self.counter}'
        case.mkdir()
        rows = [
            ('Integer revision and cents, exact order_id/customer_id/revision/gross_cents/refund_cents columns; no invented values.', ['input']),
            ('Only strip outer customer whitespace.', ['normalize']),
            ('Check normalized customer consistency across every revision.', ['cross_customer']),
            ('One row per order, net=gross-refund, order_id sorted.', ['clean']),
            ('Customer sorted count and gross/refund/net totals.', ['summary']),
            ('Recompute all results from source; input/kept/removed counts and monetary totals.', ['quality']),
            ('Local input read only, run-owned outputs, no upload or deletion.', ['permissions']),
            ('Restricted Pi may delegate checked cleaning methods.' if mode == 'pi' else 'Use fixed scripts only.', [mode]),
        ]
        if status == 'known':
            rows.append(('Keep greatest integer revision, identical highest ties collapse, conflicting ties reject.', ['rule_highest_revision']))
        elif status == 'runtime':
            rows.append(('Choose first or highest integer revision per run; no default. Check all customers; highest ties collapse only if equal.', ['rule_runtime']))
        else:
            rows.append(('Ask which revision retention rule; never guess.', ['ambiguity']))
        source = case / 'SKILL.md'
        source.write_text('\n'.join(text for text, _ in rows) + '\n')
        analysis = {'schema': 'skill-analysis/1', 'task': 'orders.report', 'mode': mode,
                    'rule': {'status': status, 'value': 'highest_revision' if status == 'known' else None},
                    'annotations': [{'source_id': 'SKILL.md', 'start_line': n, 'end_line': n,
                                     'kind': 'requirement', 'clauses': clauses,
                                     'reason': 'Explicitly hand-labelled program fixture'}
                                    for n, (_, clauses) in enumerate(rows, 1)]}
        author = SemanticAuthoring(case / '.data', Registry(), Author(analysis), Reviewer())
        initial = author.submit(source)
        write_json(case / 'initial.json', initial)
        final = initial
        if initial['state'] == 'waiting_info' and answer:
            question = next(q for q in initial['questions'] if q['status'] == 'open')
            final = author.answer(initial['draft_id'], question['request_id'], 'highest_revision',
                                  'accepted-source-answer', initial['revision'])
        write_json(case / 'final.json', final)
        return case, final

    def test_checked_semantic_answer_and_definition_drive_all_four_runs_without_reauthoring(self):
        case, draft = self.semantic_case()
        self.assertEqual(draft['state'], 'delivered', draft['diagnostics'])
        before = source_identity(case)
        planners = []

        def planner_factory():
            planners.append(ScriptedPlanner())
            return planners[-1]

        with patch('scripts.verify_sop_handoff.PiAgent', side_effect=AssertionError('network forbidden')), \
             patch('scripts.verify_sop_handoff.ScriptedPlanner', side_effect=planner_factory):
            output = self.root / 'complete'
            result = verify(output, semantic_case=case)
        self.assertTrue(result['complete'], result)
        self.assertEqual(result['not_run_cases'], [])
        self.assertFalse(result['real_model'])
        self.assertEqual(sum(p.calls for p in planners), 1)
        self.assertEqual([r['method_origins'] for r in result['cases']],
                         [['authored', 'existing'], ['authored', 'existing'],
                          ['authored', 'generated'], ['authored', 'candidate_reuse']])
        generated = result['cases'][2]['child_definitions']
        self.assertEqual(result['cases'][3]['child_definitions'], generated)
        for record in result['cases']:
            run = read_json(output / record['case'] / 'run.json')
            self.assertEqual(run['authoring']['report'], draft['report'])
            self.assertEqual(run['authoring']['answers'], draft['answers'])
            self.assertEqual(run['tasks'][run['root']]['definition']['hash'], digest(draft['definition']))
            self.assertEqual(run['answers'], [])
            self.assertEqual(run['publication'], 'unpublished')
            self.assertEqual(record['expected_comparison'], {'cleaned': True, 'quality': True, 'summary': True})
        self.assertEqual(source_identity(case), before)
        self.assertEqual(read_json(output / 'generated-candidate.json')['publication'], 'unpublished')

    def test_undelivered_fixed_and_unbound_runtime_drafts_never_dispatch(self):
        for params, code in [({'answer': False}, 'draft_not_delivered'),
                             ({'mode': 'fixed'}, 'recursive_mode_required'),
                             ({'status': 'runtime'}, 'bound_rule_required')]:
            with self.subTest(params=params):
                case, _ = self.semantic_case(**params)
                before = source_identity(case)
                with patch('scripts.verify_sop_handoff.application', side_effect=AssertionError('must not start')):
                    result = verify(self.root / f'blocked-{self.counter}', semantic_case=case)
                self.assertEqual(result['error']['code'], code, result)
                self.assertFalse(result['complete'])
                self.assertEqual(result['cases'], [])
                self.assertEqual(len(result['not_run_cases']), 4)
                self.assertEqual(source_identity(case), before)

    def test_snapshot_graph_substitution_and_persisted_answer_tampering_cannot_handoff(self):
        for tamper in ('definition', 'answer'):
            with self.subTest(tamper=tamper):
                case, draft = self.semantic_case()
                if tamper == 'definition':
                    changed = deepcopy(draft)
                    changed['definition']['id'] = 'silently-rebuilt-root'
                    write_json(case / 'final.json', changed)
                else:
                    target = case / '.data' / 'authoring' / 'drafts' / (draft['draft_id'] + '.json')
                    changed = read_json(target)
                    changed['answers']['rule']['value'] = 'first'
                    write_json(target, changed)
                before = source_identity(case)
                with patch('scripts.verify_sop_handoff.application', side_effect=AssertionError('must not start')):
                    result = verify(self.root / f'tamper-{tamper}', semantic_case=case)
                self.assertIn(result['error']['code'], {'draft_identity_mismatch', 'draft_not_delivered'})
                self.assertEqual(result['cases'], [])
                self.assertEqual(len(result['not_run_cases']), 4)
                self.assertEqual(source_identity(case), before)

    def test_failure_keeps_attempted_run_events_and_remaining_denominator_without_fallback(self):
        case, _ = self.semantic_case(status='known')
        before = source_identity(case)
        calls = []

        class FailingAgent(ScriptedAgent):
            def respond(self, context):
                calls.append(context)
                raise SopError('backend_timeout', 'Synthetic program timeout; no real request')

        # Exercise explicit Pi selection with a synthetic port failure. These
        # temporary records are protocol-test output, never real-model evidence.
        with patch('scripts.verify_sop_handoff.PiAgent', return_value=FailingAgent()) as pi, \
             patch('scripts.verify_sop_handoff.PiPlanner', return_value=ScriptedPlanner()), \
             patch('scripts.verify_sop_handoff.ScriptedAgent', side_effect=AssertionError('no scripted fallback')):
            output = self.root / 'failed'
            result = verify(output, semantic_case=case, backend='pi')
        pi.assert_called_once_with(enable_tools=False)
        self.assertFalse(result['complete'])
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(result['cases']), 1)
        self.assertEqual(result['not_run_cases'], ['existing-batch-02', 'generated-batch-01', 'generated-batch-02'])
        record = result['cases'][0]
        self.assertFalse(record['passed'])
        self.assertEqual(record['run_error']['code'], 'backend_timeout')
        self.assertTrue(read_json(output / record['case'] / 'events.json'))
        self.assertEqual(read_json(output / record['case'] / 'run.json')['state'], record['state'])
        self.assertEqual(read_json(output / 'summary.json'), result)
        self.assertEqual(source_identity(case), before)
