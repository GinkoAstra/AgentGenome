"""Semantic-authoring protocol evidence using explicitly scripted ports.

These hand-labelled paraphrases test source handling and review gates. They do
not measure a real model's language understanding or independent review quality.
"""
from copy import deepcopy
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from sop.authoring import Authoring, RULES
from sop.capabilities import Registry
from sop.common import SopError, canonical, digest, read_json
from sop.skill_authoring import SemanticAuthoring


class GuardedRegistry(Registry):
    def __init__(self):
        self.business_calls = []

    def execute(self, *args, **kwargs):
        self.business_calls.append('execute')
        raise AssertionError('Skill authoring must not execute business steps')

    def verify(self, *args, **kwargs):
        self.business_calls.append('verify')
        raise AssertionError('Definition checking must not run business acceptance')


class ScriptedAuthor:
    """Hand-labelled source interpretation; not an actual language model."""
    max_model_calls_per_request = 1
    identity = {'backend': 'scripted', 'purpose': 'authoring_protocol_test_double'}

    def __init__(self, analysis, response=None):
        self.analysis = deepcopy(analysis)
        self.response = response
        self.contexts = []
        self.last_evidence = {'backend': 'scripted', 'real_model': False}

    def author(self, context):
        self.contexts.append(deepcopy(context))
        if self.response:
            return self.response(context)
        return deepcopy(self.analysis)


class ScriptedReviewer:
    """Scripted independent role invocation, not semantic-quality evidence."""
    max_model_calls_per_request = 1
    identity = {'backend': 'scripted', 'purpose': 'review_protocol_test_double'}

    def __init__(self, response=None):
        self.response = response
        self.contexts = []
        self.last_evidence = {'backend': 'scripted', 'real_model': False}

    def review(self, context):
        self.contexts.append(deepcopy(context))
        if self.response:
            return self.response(context)
        return {'schema': 'skill-review/1', 'basis_hash': context['basis_hash'],
                'verdict': 'pass', 'findings': []}


class SimulatedProcessLoss(BaseException):
    pass


def lose_process(context):
    raise SimulatedProcessLoss()


class SemanticAuthoringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sop-semantic-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.materials = self.root / 'materials'
        self.materials.mkdir()
        self.data = self.root / 'data'
        self.registry = GuardedRegistry()

    def fixture(self, status='known', value='highest_revision', mode='fixed'):
        rule_text = {
            'known': ('每单选整数修订号最大的记录，不看行位置；最高修订完全一样可合并，内容冲突即拒绝。'
                      if value == 'highest_revision' else '每个订单取输入中的第一条记录，但仍检查全部修订的客户一致性。'),
            'unknown': '尚未说明同单多条修订选哪条；必须先询问，不得猜测或借用方法默认值。',
            'runtime': '允许每次显式选首次记录或最大整数revision，绝不默认。首次仍检查所有修订客户；最大值并列相同合并、冲突拒绝。',
        }[status]
        root_rows = [
            ('# 每批独立的本地订单报告', []),
            ('接口细节见[随附数据说明](interface.md)。', []),
            ('交付订单明细、客户汇总、质量统计三份材料。', ['deliverables']),
            ('客户编号只删两端空白，其余字符、订单编号、修订号及金额保持原值。', ['normalize']),
            ('一个订单的任何修订若落到不同规范化客户，就停止拒绝该数据。', ['cross_customer']),
            ('每个订单只保留一条，净额用总额减退款；明细按订单编号递增。', ['clean']),
            ('按规范化客户统计单数、总额、退款及净额；客户编号递增输出。', ['summary']),
            ('报告输入、留下和去掉的行数及三种金额总计；从原始输入独立复算所有输出。', ['quality']),
            ('只读本地输入，结果只写本次运行目录；禁止修改输入、删除、上传及网络业务操作。', ['permissions']),
            ('文件在每次运行提供；批次独立，定义不得保存某次路径。', ['reuse']),
            ('业务操作固定执行已注册实现，模型不得替换程序。' if mode == 'fixed' else
             '清洗任务可使用受限制Pi提案，并调用通过检查的子方法。', [mode]),
            (rule_text, ['rule_' + value] if status == 'known' else ['ambiguity' if status == 'unknown' else 'rule_runtime']),
        ]
        doc_rows = [
            ('# 数据约定', []),
            ('每行是一条订单修订；字段依次为order_id、customer_id、revision、gross_cents、refund_cents。修订与金额均为整数，金额单位分，禁用浮点或补造缺值。', ['input']),
        ]
        source = self.materials / 'SKILL.md'
        source.write_text('\n'.join(text for text, _ in root_rows) + '\n', encoding='utf-8')
        (self.materials / 'interface.md').write_text('\n'.join(text for text, _ in doc_rows) + '\n', encoding='utf-8')
        annotations = []
        for name, rows in [('SKILL.md', root_rows), ('interface.md', doc_rows)]:
            for number, (_, clauses) in enumerate(rows, 1):
                annotations.append({'source_id': name, 'start_line': number, 'end_line': number,
                                    'kind': 'requirement' if clauses else 'background', 'clauses': clauses,
                                    'reason': 'Hand-labelled requirement' if clauses else 'Heading or reference to the attached specification'})
        analysis = {'schema': 'skill-analysis/1', 'task': 'orders.report', 'mode': mode,
                    'annotations': annotations,
                    'rule': {'status': status, 'value': value if status == 'known' else None}}
        return source, analysis

    def service(self, analysis, *, author=None, reviewer=None, budget=12):
        author = author if author is not None else ScriptedAuthor(analysis)
        reviewer = reviewer if reviewer is not None else ScriptedReviewer()
        return SemanticAuthoring(self.data, self.registry, author, reviewer, budget), author, reviewer

    def codes(self, draft):
        return {diagnostic['code'] for diagnostic in draft['diagnostics']}

    def assertCode(self, code, callback):
        with self.assertRaises(SopError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)

    def only_saved_draft(self):
        paths = list((self.data / 'authoring' / 'drafts').glob('*.json'))
        self.assertEqual(len(paths), 1)
        return read_json(paths[0])

    def test_paraphrase_and_referenced_document_deliver_checked_parameterized_definition(self):
        source, analysis = self.fixture()
        app, author, reviewer = self.service(analysis)
        draft = app.submit(source)
        self.assertEqual(draft['state'], 'delivered', draft['diagnostics'])
        self.assertTrue(draft['report']['passed'])
        self.assertEqual(draft['definition']['params']['rule']['enum'], ['highest_revision'])
        self.assertEqual(draft['answers']['rule']['source'], 'source')
        self.assertEqual({row['file'] for row in draft['source_lines']}, {'SKILL.md', 'interface.md'})
        self.assertTrue(all(requirement['targets'] for requirement in draft['requirements']))
        self.assertEqual(draft['report']['bundle_hash'], draft['bundle_hash'])
        self.assertEqual(draft['report']['analysis_hash'], digest(analysis))
        self.assertEqual(draft['report']['semantic_reviews_hash'], digest(draft['semantic_reviews']))
        self.assertEqual(len(author.contexts), 1)
        self.assertEqual(len(reviewer.contexts), 1)
        self.assertEqual(author.contexts[0]['sources'], reviewer.contexts[0]['sources'])
        for context in author.contexts + reviewer.contexts:
            self.assertNotIn(str(self.root), canonical(context))
            self.assertTrue(all('path' not in file for file in context['sources']['files']))
        self.assertEqual(draft['business_calls'], 0)
        self.assertEqual(self.registry.business_calls, [])
        self.assertEqual(draft['trials'], 'not_run')
        self.assertEqual(draft['publication'], 'unpublished')
        self.assertFalse(draft['report']['execution_authorized'])
        self.assertFalse(draft['report']['result_verified'])
        self.assertEqual(draft['semantic_evidence']['general_nl_validation'], 'not_run')

    def test_known_first_and_runtime_choice_have_distinct_answer_and_definition_semantics(self):
        for status, value, mode in [('known', 'first', 'fixed'), ('runtime', None, 'pi')]:
            with self.subTest(status=status):
                source, analysis = self.fixture(status, value, mode)
                app, _, _ = self.service(analysis)
                draft = app.submit(source)
                self.assertEqual(draft['state'], 'delivered', draft['diagnostics'])
                self.assertEqual(draft['questions'], [])
                if status == 'known':
                    self.assertEqual(draft['definition']['params']['rule']['enum'], ['first'])
                    self.assertEqual(draft['answers']['rule']['value'], 'first')
                else:
                    self.assertEqual(draft['definition']['params']['rule']['enum'], RULES)
                    self.assertEqual(draft['answers'], {})
                    self.assertTrue(draft['rule_at_runtime'])
                    self.assertTrue(any(step['kind'] == 'pi' for step in draft['definition']['steps']))

    def test_omitted_rule_is_a_question_without_fabricating_source_ambiguity(self):
        source, analysis = self.fixture('unknown')
        source.write_text('\n'.join(source.read_text().splitlines()[:-1])+'\n')
        analysis['annotations'] = [a for a in analysis['annotations'] if a['clauses'] != ['ambiguity']]
        app, _, _ = self.service(analysis)
        waiting = app.submit(source)
        self.assertEqual(waiting['state'], 'waiting_info', waiting['diagnostics'])
        self.assertFalse(any(r['meaning']=='ambiguity' for r in waiting['requirements']))
        clean_id = next(r['id'] for r in waiting['requirements'] if r['meaning']=='clean')
        self.assertIn(clean_id, waiting['questions'][0]['requirements'])
        delivered = app.answer(waiting['draft_id'], 'dedup_policy', 'highest_revision', 'rule-1', waiting['revision'])
        self.assertEqual(delivered['state'], 'delivered', delivered['diagnostics'])

    def test_unknown_rule_waits_persists_and_answer_rechecks_new_basis_without_reauthoring(self):
        source, analysis = self.fixture('unknown')
        app, author, reviewer = self.service(analysis)
        waiting = app.submit(source)
        self.assertEqual(waiting['state'], 'waiting_info')
        self.assertIsNone(waiting['definition'])
        self.assertEqual(waiting['questions'][0]['schema']['enum'], RULES)
        self.assertTrue(waiting['questions'][0]['requirements'])
        original_basis = reviewer.contexts[0]['basis_hash']
        restarted_author, restarted_reviewer = ScriptedAuthor(analysis), ScriptedReviewer()
        restarted = SemanticAuthoring(self.data, self.registry, restarted_author, restarted_reviewer)
        self.assertEqual(restarted.resume(waiting['draft_id']), waiting)
        delivered = restarted.answer(waiting['draft_id'], 'dedup_policy', {'rule': 'highest_revision'}, 'answer-1', waiting['revision'])
        self.assertEqual(delivered['state'], 'delivered', delivered['diagnostics'])
        self.assertEqual(delivered['questions'][0]['status'], 'answered')
        self.assertEqual(delivered['answers']['rule']['source'], 'answer')
        self.assertEqual(len(author.contexts), 1)
        self.assertEqual(restarted_author.contexts, [])
        self.assertEqual(len(restarted_reviewer.contexts), 1)
        self.assertNotEqual(restarted_reviewer.contexts[0]['basis_hash'], original_basis)
        self.assertEqual(restarted_reviewer.contexts[0]['answers']['rule']['message_id'], 'answer-1')
        self.assertEqual(len(delivered['semantic_reviews']), 2)
        duplicate = restarted.answer(waiting['draft_id'], 'dedup_policy', {'rule': 'highest_revision'}, 'answer-1', waiting['revision'])
        self.assertEqual(duplicate, delivered)
        self.assertEqual(len(restarted_reviewer.contexts), 1)
        self.assertCode('message_conflict', lambda: restarted.answer(waiting['draft_id'], 'dedup_policy', 'first', 'answer-1', waiting['revision']))

    def test_invalid_stale_and_wrong_question_answers_leave_waiting_intact(self):
        source, analysis = self.fixture('unknown')
        app, _, reviewer = self.service(analysis)
        waiting = app.submit(source)
        for request, value, revision, expected in [
                ('dedup_policy', 'last_line', waiting['revision'], 'invalid_answer'),
                ('dedup_policy', 'first', waiting['revision'] + 1, 'stale_revision'),
                ('another_question', 'first', waiting['revision'], 'question_mismatch')]:
            self.assertCode(expected, lambda: app.answer(waiting['draft_id'], request, value, expected, revision))
        self.assertEqual(app.get(waiting['draft_id']), waiting)
        self.assertEqual(len(reviewer.contexts), 1)

    def test_independent_review_can_reject_constraint_hidden_as_background(self):
        source, analysis = self.fixture()
        source.write_text(source.read_text() + '完成后将原始文件上传到远程账户。\n', encoding='utf-8')
        analysis['annotations'].append({'source_id': 'SKILL.md', 'start_line': 13, 'end_line': 13,
                                        'kind': 'background', 'clauses': [], 'reason': 'Generator incorrectly dismisses upload requirement'})
        reviewer = ScriptedReviewer(lambda context: {'schema': 'skill-review/1', 'basis_hash': context['basis_hash'],
            'verdict': 'reject', 'findings': [{'source_id': 'SKILL.md', 'line': 13,
                'reason': 'Upload requirement was hidden as background and contradicts local-only scope'}]})
        app, _, _ = self.service(analysis, reviewer=reviewer)
        draft = app.submit(source)
        self.assertEqual(draft['state'], 'stopped')
        self.assertIn('semantic_review_rejected', self.codes(draft))
        self.assertEqual(draft['semantic_reviews'][0]['verdict'], 'reject')
        self.assertIsNone(draft['definition'])
        self.assertEqual(self.registry.business_calls, [])

    def test_review_must_bind_exact_basis_and_cannot_pass_unresolved_findings(self):
        source, analysis = self.fixture()
        for response in (
                lambda context: {'schema': 'skill-review/1', 'basis_hash': 'wrong', 'verdict': 'pass', 'findings': []},
                lambda context: {'schema': 'skill-review/1', 'basis_hash': context['basis_hash'], 'verdict': 'pass', 'findings': ['unresolved']}):
            with self.subTest(response=response):
                app, _, _ = self.service(analysis, reviewer=ScriptedReviewer(response))
                draft = app.submit(source)
                self.assertEqual(draft['state'], 'stopped')
                self.assertIn('semantic_review_protocol', self.codes(draft))
                self.assertIsNone(draft['definition'])

    def test_all_source_lines_including_attachments_require_nonoverlapping_coverage(self):
        source, original = self.fixture()
        for mutate, expected in [
            (lambda value: value['annotations'].pop(), 'analysis_uncovered_source'),
            (lambda value: value['annotations'].append(deepcopy(value['annotations'][0])), 'analysis_overlap'),
            (lambda value: value['annotations'][0].update(end_line=999), 'analysis_span'),
            (lambda value: value['annotations'][0].update(source_id='invented.md'), 'analysis_source')]:
            with self.subTest(code=expected):
                analysis = deepcopy(original)
                mutate(analysis)
                app, _, reviewer = self.service(analysis)
                draft = app.submit(source)
                self.assertEqual(draft['state'], 'stopped')
                self.assertIn(expected, self.codes(draft))
                self.assertEqual(reviewer.contexts, [])
                self.assertIsNone(draft['definition'])

    def test_uninterpreted_or_undeclared_clause_cannot_be_delivered(self):
        source, original = self.fixture()
        source.write_text(source.read_text() + '还需要一种当前没有实现的业务操作。\n', encoding='utf-8')
        for kind, clauses, code in [('uninterpreted', [], 'unsupported_requirement'),
                                     ('requirement', ['unknown_upload'], 'analysis_clause')]:
            with self.subTest(kind=kind):
                analysis = deepcopy(original)
                analysis['annotations'].append({'source_id': 'SKILL.md', 'start_line': 13, 'end_line': 13,
                                                'kind': kind, 'clauses': clauses, 'reason': 'Unimplemented extra operation'})
                app, _, reviewer = self.service(analysis)
                draft = app.submit(source)
                self.assertEqual(draft['state'], 'stopped')
                self.assertIn(code, self.codes(draft))
                self.assertIn('当前没有实现', draft['source_bundle']['files'][0]['text'])
                self.assertEqual(reviewer.contexts, [])

    def test_rule_and_mode_analysis_conflicts_stop_before_review(self):
        source, original = self.fixture()
        for field, value in [('mode', 'pi'), ('rule', {'status': 'known', 'value': 'first'})]:
            with self.subTest(field=field):
                analysis = deepcopy(original)
                analysis[field] = value
                app, _, reviewer = self.service(analysis)
                draft = app.submit(source)
                self.assertIn('analysis_conflict', self.codes(draft))
                self.assertEqual(draft['state'], 'stopped')
                self.assertEqual(reviewer.contexts, [])

    def test_missing_reviewer_never_delivers_and_cannot_consume_waiting_answer(self):
        source, analysis = self.fixture()
        app = SemanticAuthoring(self.data, self.registry, ScriptedAuthor(analysis), None)
        draft = app.submit(source)
        self.assertEqual(draft['state'], 'stopped')
        self.assertIn('semantic_checker_missing', self.codes(draft))
        source, analysis = self.fixture('unknown')
        app, _, _ = self.service(analysis)
        waiting = app.submit(source)
        without_reviewer = SemanticAuthoring(self.data, self.registry)
        self.assertCode('semantic_checker_missing', lambda: without_reviewer.answer(waiting['draft_id'], 'dedup_policy', 'first', 'no-review', waiting['revision']))
        self.assertEqual(app.get(waiting['draft_id']), waiting)

    def test_referenced_source_change_invalidates_report_and_preserves_previous_evidence(self):
        source, analysis = self.fixture()
        app, _, reviewer = self.service(analysis)
        delivered = app.submit(source)
        document = self.materials / 'interface.md'
        document.write_text(document.read_text() + '金额现在改为浮点元。\n', encoding='utf-8')
        current = app.get(delivered['draft_id'])
        self.assertEqual(current['state'], 'stopped')
        self.assertFalse(current['report']['passed'])
        self.assertFalse(current['report']['applicable'])
        self.assertIn('source_bundle_changed', current['report']['invalidated_by'])
        self.assertEqual(current['report_history'][-1], delivered['report'])
        self.assertEqual(len(reviewer.contexts), 1)
        # Legacy query dispatch must apply the same semantic freshness checks.
        self.assertEqual(Authoring(self.data, self.registry).get(delivered['draft_id'])['state'], 'stopped')

    def test_source_change_during_review_cannot_return_a_fresh_delivery(self):
        source, analysis = self.fixture()
        document = self.materials / 'interface.md'
        def change_source(context):
            document.write_text(document.read_text() + '新要求：改变金额单位。\n', encoding='utf-8')
            return {'schema': 'skill-review/1', 'basis_hash': context['basis_hash'],
                    'verdict': 'pass', 'findings': []}
        app, _, _ = self.service(analysis, reviewer=ScriptedReviewer(change_source))
        result = app.submit(source)
        self.assertEqual(result['state'], 'stopped')
        self.assertIn('source_changed', self.codes(result))
        self.assertFalse(result.get('report', {}).get('passed', False) if result.get('report') else False)
        self.assertEqual(self.registry.business_calls, [])

    def test_referenced_source_change_while_waiting_does_not_accept_answer(self):
        source, analysis = self.fixture('unknown')
        app, _, reviewer = self.service(analysis)
        waiting = app.submit(source)
        document = self.materials / 'interface.md'
        document.write_text('Changed meaning\n', encoding='utf-8')
        self.assertCode('source_changed', lambda: app.answer(waiting['draft_id'], 'dedup_policy', 'first', 'drift', waiting['revision']))
        stored = app.get(waiting['draft_id'])
        self.assertEqual(stored['questions'][0]['status'], 'open')
        self.assertEqual(stored['answers'], {})
        self.assertEqual(len(reviewer.contexts), 1)

    def test_missing_reference_blocks_before_any_model_request(self):
        source, analysis = self.fixture()
        (self.materials / 'interface.md').unlink()
        app, author, reviewer = self.service(analysis)
        draft = app.submit(source)
        self.assertEqual(draft['state'], 'stopped')
        self.assertIn('missing_source', self.codes(draft))
        self.assertEqual(author.contexts, [])
        self.assertEqual(reviewer.contexts, [])

    def test_budget_accounts_for_author_and_reviewer_and_never_silently_retries(self):
        source, analysis = self.fixture()
        for budget, expected_author in [(0, 0), (1, 1)]:
            with self.subTest(budget=budget):
                app, author, reviewer = self.service(analysis, budget=budget)
                draft = app.submit(source)
                self.assertEqual(draft['state'], 'stopped')
                self.assertIn('budget_exhausted', self.codes(draft))
                self.assertEqual(draft['usage']['model_calls'], budget)
                self.assertEqual(len(author.contexts), expected_author)
                self.assertEqual(reviewer.contexts, [])
                self.assertEqual(app.resume(draft['draft_id']), draft)
                self.assertEqual(len(author.contexts), expected_author)

    def test_backend_error_is_not_a_business_information_question(self):
        source, analysis = self.fixture('unknown')
        def timeout(context):
            raise SopError('backend_timeout', 'Scripted timeout')
        app, _, _ = self.service(analysis, author=ScriptedAuthor(analysis, timeout))
        draft = app.submit(source)
        self.assertEqual(draft['state'], 'stopped')
        self.assertIn('backend_timeout', self.codes(draft))
        self.assertEqual(draft['questions'], [])
        self.assertEqual(draft['model_attempts'][0]['state'], 'failed')
        self.assertEqual(draft['usage']['model_calls'], 1)

    def test_interrupted_author_reserves_budget_and_resume_does_not_reset_it(self):
        source, analysis = self.fixture()
        app, _, _ = self.service(analysis, author=ScriptedAuthor(analysis, lose_process), budget=3)
        with self.assertRaises(SimulatedProcessLoss):
            app.submit(source)
        saved = self.only_saved_draft()
        self.assertEqual(saved['state'], 'authoring')
        self.assertEqual(saved['usage']['model_calls'], 1)
        self.assertEqual(saved['model_attempts'][0]['state'], 'reserved')
        resumed, author, reviewer = self.service(analysis, budget=100)
        result = resumed.resume(saved['draft_id'])
        self.assertEqual(result['state'], 'delivered', result['diagnostics'])
        self.assertEqual(result['limits']['max_model_calls'], 3)
        self.assertEqual(result['usage']['model_calls'], 3)
        self.assertEqual(len(author.contexts), 1)
        self.assertEqual(len(reviewer.contexts), 1)
        self.assertEqual(result['model_attempts'][0]['state'], 'reserved')

    def test_interrupted_post_answer_review_keeps_answer_receipt_and_resumes_review_only(self):
        source, analysis = self.fixture('unknown')
        app, _, _ = self.service(analysis, budget=4)
        waiting = app.submit(source)
        crashing = SemanticAuthoring(self.data, self.registry, ScriptedAuthor(analysis), ScriptedReviewer(lose_process))
        with self.assertRaises(SimulatedProcessLoss):
            crashing.answer(waiting['draft_id'], 'dedup_policy', 'highest_revision', 'durable-answer', waiting['revision'])
        saved = self.only_saved_draft()
        self.assertEqual(saved['state'], 'checking')
        self.assertEqual(saved['questions'][0]['status'], 'answered')
        self.assertEqual(saved['answers']['rule']['value'], 'highest_revision')
        self.assertEqual(saved['usage']['model_calls'], 3)
        resumed, author, reviewer = self.service(analysis)
        duplicate = resumed.answer(waiting['draft_id'], 'dedup_policy', 'highest_revision', 'durable-answer', waiting['revision'])
        self.assertEqual(duplicate['state'], 'checking')
        self.assertEqual(author.contexts, [])
        self.assertEqual(reviewer.contexts, [])
        result = resumed.resume(waiting['draft_id'])
        self.assertEqual(result['state'], 'delivered', result['diagnostics'])
        self.assertEqual(result['usage']['model_calls'], 4)
        self.assertEqual(author.contexts, [])
        self.assertEqual(len(reviewer.contexts), 1)
        self.assertEqual(len(result['semantic_reviews']), 2)
        self.assertEqual(result['questions'][0]['status'], 'answered')
        self.assertEqual(resumed.resume(waiting['draft_id']), result)
        self.assertEqual(len(reviewer.contexts), 1)


    def test_cancel_during_author_call_is_durable_without_waiting_for_model_lock(self):
        source, analysis = self.fixture()
        entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
        results, errors = {}, []
        def blocked_author(context):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('test did not release author')
            return deepcopy(analysis)
        app, _, reviewer = self.service(analysis, author=ScriptedAuthor(analysis, blocked_author))
        def submit():
            try:
                results['draft'] = app.submit(source)
            except BaseException as error:
                errors.append(error)
        worker = threading.Thread(target=submit, daemon=True)
        worker.start()
        self.assertTrue(entered.wait(2))
        saved = self.only_saved_draft()
        def cancel():
            try:
                results['ack'] = SemanticAuthoring(self.data, self.registry).cancel(
                    saved['draft_id'], message_id='cancel-in-flight', revision=saved['revision'])
            except BaseException as error:
                errors.append(error)
            finally:
                cancelled.set()
        controller = threading.Thread(target=cancel, daemon=True)
        controller.start()
        try:
            self.assertTrue(cancelled.wait(1), 'cancel must not wait for the model/global lock')
            self.assertTrue(results['ack']['requested'])
        finally:
            release.set()
            worker.join(3)
            controller.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        stopped = results['draft']
        self.assertEqual(stopped['state'], 'stopped')
        self.assertEqual(stopped['stop_reason'], 'cancelled')
        self.assertEqual(stopped['usage']['model_calls'], 1)
        self.assertEqual(stopped['model_attempts'][0]['late_result'], analysis)
        self.assertEqual(reviewer.contexts, [])
        self.assertIsNone(stopped['definition'])
        self.assertEqual(app.resume(stopped['draft_id'])['state'], 'stopped')
        self.assertEqual(reviewer.contexts, [])
        self.assertCode('draft_cancelled', lambda: app.revise(stopped['draft_id'],
            message_id='revive', revision=stopped['revision'], proposal=analysis))
        self.assertEqual(app.cancel(saved['draft_id'], message_id='cancel-in-flight', revision=saved['revision']), results['ack'])
        self.assertCode('message_conflict', lambda: app.cancel(saved['draft_id'],
            message_id='cancel-in-flight', revision=stopped['revision']))

    def test_cancel_during_review_preserves_late_evidence_without_delivery(self):
        source, analysis = self.fixture()
        def cancel_in_review(context):
            saved = self.only_saved_draft()
            SemanticAuthoring(self.data, self.registry).cancel(saved['draft_id'],
                message_id='cancel-review', revision=saved['revision'])
            return {'schema':'skill-review/1', 'basis_hash':context['basis_hash'], 'verdict':'pass', 'findings':[]}
        app, _, reviewer = self.service(analysis, reviewer=ScriptedReviewer(cancel_in_review))
        stopped = app.submit(source)
        self.assertEqual(stopped['stop_reason'], 'cancelled')
        self.assertIsNone(stopped['report'])
        self.assertEqual(stopped['semantic_reviews'], [])
        self.assertEqual(stopped['model_attempts'][-1]['late_result']['verdict'], 'pass')
        self.assertEqual(stopped['usage']['model_calls'], 2)
        self.assertEqual(len(reviewer.contexts), 1)

    def test_cancel_at_delivery_boundary_cannot_leave_a_passed_report(self):
        source, analysis = self.fixture()
        app, _, _ = self.service(analysis)
        compile_original = app._compile
        def compile_then_cancel(draft):
            compile_original(draft)
            app.cancel(draft['draft_id'], message_id='cancel-delivery', revision=draft['revision'])
        with patch.object(app, '_compile', side_effect=compile_then_cancel):
            stopped = app.submit(source)
        self.assertEqual(stopped['state'], 'stopped')
        self.assertEqual(stopped['stop_reason'], 'cancelled')
        self.assertFalse(stopped['report']['passed'])
        self.assertFalse(stopped['report']['applicable'])
        self.assertEqual(app.get(stopped['draft_id'])['state'], 'stopped')

    def test_cancel_waiting_closes_question_and_cannot_be_reused_as_answer_identity(self):
        source, analysis = self.fixture('unknown')
        app, _, reviewer = self.service(analysis)
        waiting = app.submit(source)
        self.assertCode('stale_revision', lambda: app.cancel(waiting['draft_id'],
            message_id='bad-revision', revision=waiting['revision']+1))
        ack = app.cancel(waiting['draft_id'], message_id='stop-waiting', revision=waiting['revision'])
        self.assertEqual(ack['state'], 'cancel_requested')
        self.assertCode('message_conflict', lambda: app.answer(waiting['draft_id'], 'dedup_policy',
            'first', 'stop-waiting', waiting['revision']))
        stopped = app.get(waiting['draft_id'])
        self.assertEqual(stopped['questions'][0]['status'], 'cancelled')
        self.assertEqual(stopped['stop_reason'], 'cancelled')
        self.assertCode('draft_cancelled', lambda: app.answer(waiting['draft_id'], 'dedup_policy',
            'first', 'too-late', stopped['revision']))
        self.assertEqual(len(reviewer.contexts), 1)

    def test_cancel_already_delivered_is_noop_and_does_not_retract_candidate(self):
        source, analysis = self.fixture()
        app, _, reviewer = self.service(analysis)
        delivered = app.submit(source)
        ack = app.cancel(delivered['draft_id'], message_id='after-delivery', revision=delivered['revision'])
        self.assertFalse(ack['requested'])
        self.assertEqual(ack['state'], 'delivered')
        self.assertEqual(app.get(delivered['draft_id']), delivered)
        self.assertEqual(len(reviewer.contexts), 1)

    def test_revise_invalid_analysis_preserves_partial_audit_history_and_budget(self):
        source, valid = self.fixture()
        invalid = deepcopy(valid)
        invalid['annotations'].pop()
        app, author, reviewer = self.service(invalid, budget=3)
        stopped = app.submit(source)
        self.assertEqual(stopped['state'], 'stopped')
        self.assertTrue(stopped['requirements'])
        self.assertEqual(stopped['analysis_audit']['status'], 'incomplete')
        missing = stopped['diagnostics'][0]['source_spans']
        self.assertIn({'source_id':'interface.md', 'start_line':2, 'end_line':2}, missing)
        self.assertEqual(stopped['analysis'], invalid)
        repaired = app.revise(stopped['draft_id'], message_id='repair-1', revision=stopped['revision'], proposal=valid)
        self.assertEqual(repaired['state'], 'delivered', repaired['diagnostics'])
        self.assertEqual(repaired['revision'], stopped['revision']+1)
        self.assertEqual(repaired['revision_history'][0]['analysis'], invalid)
        self.assertEqual(repaired['revision_history'][0]['diagnostics'], stopped['diagnostics'])
        self.assertEqual(repaired['usage']['model_calls'], 2)
        self.assertEqual(repaired['limits'], stopped['limits'])
        self.assertEqual(repaired['source_bundle'], stopped['source_bundle'])
        self.assertEqual(len(author.contexts), 1)
        self.assertEqual(len(reviewer.contexts), 1)
        duplicate = app.revise(stopped['draft_id'], message_id='repair-1', revision=stopped['revision'], proposal=valid)
        self.assertEqual(duplicate, repaired)
        self.assertEqual(len(reviewer.contexts), 1)
        self.assertCode('message_conflict', lambda: app.revise(stopped['draft_id'],
            message_id='repair-1', revision=stopped['revision'], request_model=True))
        self.assertCode('stale_revision', lambda: app.revise(stopped['draft_id'],
            message_id='stale-repair', revision=stopped['revision'], proposal=valid))

    def test_revise_review_rejection_requires_new_review_and_keeps_old_findings(self):
        source, analysis = self.fixture()
        reject = ScriptedReviewer(lambda context: {'schema':'skill-review/1', 'basis_hash':context['basis_hash'],
            'verdict':'reject', 'findings':[{'source_id':'SKILL.md','line':12,'reason':'Manual test review rejection'}]})
        app, _, _ = self.service(analysis, reviewer=reject)
        stopped = app.submit(source)
        fresh_reviewer = ScriptedReviewer()
        resumed = SemanticAuthoring(self.data, self.registry, ScriptedAuthor(analysis), fresh_reviewer)
        repaired = resumed.revise(stopped['draft_id'], message_id='review-repair', revision=stopped['revision'], proposal=analysis)
        self.assertEqual(repaired['state'], 'delivered')
        self.assertEqual(repaired['revision_history'][0]['semantic_reviews'][0]['verdict'], 'reject')
        self.assertEqual(repaired['semantic_reviews'][0]['verdict'], 'pass')
        self.assertEqual(repaired['usage']['model_calls'], 3)
        self.assertEqual(len(fresh_reviewer.contexts), 1)

    def test_requested_model_revision_sees_previous_errors_and_uses_original_remaining_budget(self):
        source, valid = self.fixture()
        invalid = deepcopy(valid)
        invalid['annotations'].pop()
        app, _, _ = self.service(invalid, budget=3)
        stopped = app.submit(source)
        reauthor, review = ScriptedAuthor(valid), ScriptedReviewer()
        restarted = SemanticAuthoring(self.data, self.registry, reauthor, review, max_model_calls=100)
        result = restarted.revise(stopped['draft_id'], message_id='request-model', revision=stopped['revision'], request_model=True)
        self.assertEqual(result['state'], 'delivered', result['diagnostics'])
        self.assertEqual(result['limits']['max_model_calls'], 3)
        self.assertEqual(result['usage']['model_calls'], 3)
        self.assertEqual(reauthor.contexts[0]['analysis'], invalid)
        self.assertIn('analysis_uncovered_source', canonical(reauthor.contexts[0]['requirements']))
        self.assertEqual(len(review.contexts), 1)
        exhausted = restarted.revise(result['draft_id'], message_id='no-budget', revision=result['revision'], proposal=valid)
        self.assertEqual(exhausted['state'], 'stopped')
        self.assertIn('budget_exhausted', self.codes(exhausted))
        self.assertEqual(exhausted['usage']['model_calls'], 3)
        self.assertEqual(len(review.contexts), 1)
        self.assertEqual(exhausted['report_history'][-1], result['report'])
        self.assertEqual(result['definition']['params']['rule']['enum'], ['highest_revision'])

    def test_revision_preserves_effective_user_answer_and_rejects_source_or_rule_replacement(self):
        source, analysis = self.fixture('unknown')
        app, _, reviewer = self.service(analysis)
        waiting = app.submit(source)
        delivered = app.answer(waiting['draft_id'], 'dedup_policy', 'highest_revision', 'bound-rule', waiting['revision'])
        for status, value in [('runtime',None), ('known','first')]:
            replacement = deepcopy(analysis)
            replacement['rule'] = {'status':status,'value':value}
            self.assertCode('answer_conflict', lambda: app.revise(delivered['draft_id'],
                message_id='change-'+status, revision=delivered['revision'], proposal=replacement))
        revised = app.revise(delivered['draft_id'], message_id='keep-answer', revision=delivered['revision'], proposal=analysis)
        self.assertEqual(revised['state'], 'delivered')
        self.assertEqual(revised['answers'], delivered['answers'])
        self.assertEqual(revised['definition']['params']['rule']['enum'], ['highest_revision'])
        self.assertEqual(revised['source_bundle'], delivered['source_bundle'])
        self.assertEqual(revised['questions'][0]['status'], 'answered')
        self.assertEqual(len(reviewer.contexts), 3)
        (self.materials/'interface.md').write_text('Changed original input contract\n')
        self.assertCode('source_changed', lambda: app.revise(revised['draft_id'],
            message_id='changed-source', revision=revised['revision'], proposal=analysis))

    def test_revision_replaces_unanswered_question_identity_without_accepting_old_answer(self):
        source, analysis = self.fixture('unknown')
        app, _, _ = self.service(analysis)
        waiting = app.submit(source)
        revised = app.revise(waiting['draft_id'], message_id='question-revision', revision=waiting['revision'], proposal=analysis)
        self.assertEqual(revised['state'], 'waiting_info')
        self.assertEqual(revised['questions'][0]['status'], 'superseded')
        self.assertEqual(revised['questions'][1]['status'], 'open')
        self.assertNotEqual(revised['questions'][0]['request_id'], revised['questions'][1]['request_id'])
        self.assertCode('question_mismatch', lambda: app.answer(revised['draft_id'],
            revised['questions'][0]['request_id'], 'first', 'old-question', revised['revision']))
        delivered = app.answer(revised['draft_id'], revised['questions'][1]['request_id'],
            'first', 'current-question', revised['revision'])
        self.assertEqual(delivered['state'], 'delivered')

    def test_interrupted_revision_review_keeps_history_receipt_and_original_candidate(self):
        source, analysis = self.fixture()
        app, _, _ = self.service(analysis, budget=4)
        delivered = app.submit(source)
        original_definition = deepcopy(delivered['definition'])
        crashing = SemanticAuthoring(self.data, self.registry, ScriptedAuthor(analysis), ScriptedReviewer(lose_process))
        with self.assertRaises(SimulatedProcessLoss):
            crashing.revise(delivered['draft_id'], message_id='durable-revision',
                            revision=delivered['revision'], proposal=analysis)
        saved = self.only_saved_draft()
        self.assertEqual(saved['state'], 'checking')
        self.assertEqual(saved['usage']['model_calls'], 3)
        self.assertEqual(saved['revision_history'][0]['definition'], original_definition)
        author, reviewer = ScriptedAuthor(analysis), ScriptedReviewer()
        restarted = SemanticAuthoring(self.data, self.registry, author, reviewer)
        receipt = restarted.revise(delivered['draft_id'], message_id='durable-revision',
                                   revision=delivered['revision'], proposal=analysis)
        self.assertEqual(receipt['state'], 'authoring')
        self.assertEqual(reviewer.contexts, [])
        result = restarted.resume(delivered['draft_id'])
        self.assertEqual(result['state'], 'delivered')
        self.assertEqual(result['usage']['model_calls'], 4)
        self.assertEqual(author.contexts, [])
        self.assertEqual(len(reviewer.contexts), 1)
        self.assertEqual(result['revision_history'][0]['definition'], original_definition)
        self.assertEqual(delivered['definition'], original_definition)

    def test_revision_requires_exactly_one_strategy_and_available_reviewer(self):
        source, analysis = self.fixture()
        app, _, _ = self.service(analysis)
        delivered = app.submit(source)
        for kwargs in ({}, {'proposal':analysis,'request_model':True}, {'request_model':1}):
            self.assertCode('invalid_revision', lambda: app.revise(delivered['draft_id'],
                message_id='invalid-strategy', revision=delivered['revision'], **kwargs))
        missing = SemanticAuthoring(self.data,self.registry,ScriptedAuthor(analysis))
        self.assertCode('semantic_checker_missing', lambda: missing.revise(delivered['draft_id'],
            message_id='missing-review', revision=delivered['revision'], proposal=analysis))
        self.assertEqual(app.get(delivered['draft_id']), delivered)

    def test_unsupported_clause_keeps_located_unverified_audit_in_stopped_draft(self):
        source, analysis = self.fixture()
        source.write_text(source.read_text()+'另需未实现的外部上传。\n')
        analysis['annotations'].append({'source_id':'SKILL.md','start_line':13,'end_line':13,
            'kind':'uninterpreted','clauses':[],'reason':'Missing upload capability'})
        app, _, reviewer = self.service(analysis)
        stopped = app.submit(source)
        self.assertEqual(stopped['state'], 'stopped')
        unsupported = [requirement for requirement in stopped['requirements'] if requirement['status']=='unsupported']
        self.assertEqual(unsupported[0]['source']['line'],13)
        self.assertEqual(unsupported[0]['source']['text'],'另需未实现的外部上传。')
        self.assertEqual(stopped['diagnostics'][0]['source_spans'][0]['source_id'],'SKILL.md')
        self.assertEqual(stopped['analysis_audit']['status'],'incomplete')
        self.assertEqual(reviewer.contexts,[])
        self.assertIsNone(stopped['report'])

    def test_source_snapshot_storage_failure_has_protocol_error_code(self):
        source, analysis = self.fixture()
        app, author, reviewer = self.service(analysis)
        with patch('sop.skill_authoring.write_json', side_effect=OSError('Simulated full disk')):
            self.assertCode('storage_failure', lambda: app.submit(source))
        self.assertEqual(author.contexts, [])
        self.assertEqual(reviewer.contexts, [])
        self.assertEqual(self.registry.business_calls, [])


if __name__ == '__main__':
    unittest.main()
