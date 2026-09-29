"""Mechanical B1 acceptance. These tests make no model-quality claim."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sop.authoring import Authoring, Library, clean_definition, prepare_method, root_definition, validate_definition
from sop.capabilities import Registry
from sop.common import SopError, digest, read_json, write_json

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'tests/fixtures/first-release/source-request.zh.md'
ANSWER = ROOT / 'tests/fixtures/first-release/answer.json'


class GuardedRegistry(Registry):
    """Compilation may inspect interfaces but must never invoke business work."""
    def __init__(self):
        self.calls = []
        self.version = None

    def catalog(self):
        catalog = super().catalog()
        if self.version:
            for schema in catalog.values():
                schema['version'] = self.version
        return catalog

    def execute(self, *args, **kwargs):
        self.calls.append('execute')
        raise AssertionError('authoring must not execute a business capability')

    def verify(self, *args, **kwargs):
        self.calls.append('verify')
        raise AssertionError('authoring must not execute the business verifier')


class RecordingPlanner:
    def __init__(self, definition=None):
        self.calls = []
        self.definition = definition

    def plan(self, context):
        self.calls.append(deepcopy(context))
        return deepcopy(self.definition or clean_definition())


class CompilerTests(unittest.TestCase):
    def setUp(self):
        self.registry = GuardedRegistry()

    def assertInvalid(self, definition, code=None):
        report = validate_definition(definition, self.registry)
        self.assertFalse(report['passed'], report)
        if code:
            self.assertIn(code, [item['code'] for item in report['diagnostics']], report)
        return report

    def test_builtin_modes_and_dependencies(self):
        for definition in (root_definition(), root_definition('fixed'), clean_definition(), clean_definition('first')):
            report = validate_definition(definition, self.registry)
            self.assertTrue(report['passed'], report)
            self.assertEqual(report['definition_hash'], digest(definition))
            self.assertEqual(set(report['dependency_hashes']), set(definition['capabilities']))
            self.assertEqual(set(report['checker_versions']), set(definition['checks']))
        self.assertTrue(all(s['kind'] == 'fixed' for s in root_definition('fixed')['steps']))
        self.assertEqual(self.registry.calls, [])

    def test_unknown_fields_and_fixed_script_replacement(self):
        for extra in ({'shell': 'rm -rf input'}, {'code': 'print(1)'}, {'permission': 'network'}):
            definition = clean_definition()
            definition['steps'][0].update(extra)
            self.assertInvalid(definition, 'unknown_field')
        definition = clean_definition()
        definition['unexpected'] = True
        self.assertInvalid(definition, 'unknown_field')

    def test_unknown_capabilities_and_checker_fail_closed(self):
        definition = clean_definition()
        definition['capabilities'].append('network.upload')
        self.assertInvalid(definition, 'unknown_capability')
        definition = clean_definition()
        definition['checks'] = ['future.checker']
        self.assertInvalid(definition, 'unknown_checker')
        definition = clean_definition()
        definition['checks'] = []
        self.assertInvalid(definition, 'missing_checker')
        definition = clean_definition()
        definition['checks'].append('orders.report')
        self.assertInvalid(definition, 'task_contract')

    def test_bindings_cannot_use_future_unknown_or_literal_artifacts(self):
        for value in ('$steps.later.normalized', '$input.missing', '$input.source_file.extra', '/tmp/input.csv'):
            definition = clean_definition()
            definition['steps'][0]['inputs']['source_file'] = value
            self.assertInvalid(definition)
        definition = clean_definition()
        definition['steps'].reverse()
        self.assertInvalid(definition, 'invalid_binding')

    def test_required_dependencies_and_rule_identity(self):
        definition = root_definition('fixed')
        definition['params']['rule']['required'] = False
        self.assertInvalid(definition, 'missing_dependency')
        definition = clean_definition()
        definition['steps'][1]['inputs'].pop('rule')
        self.assertInvalid(definition, 'missing_dependency')
        definition = clean_definition()
        definition['steps'][1]['inputs']['rule'] = 'first'
        self.assertInvalid(definition, 'contract_binding')
        # A task's Pi information-request protocol may resolve this optional rule.
        self.assertTrue(validate_definition(root_definition(), self.registry)['passed'])

    def test_artifact_roles_and_parent_outputs(self):
        definition = clean_definition()
        definition['outputs']['cleaned'] = '$steps.normalize.normalized'
        self.assertInvalid(definition, 'binding_role')
        definition = root_definition()
        definition['outputs'].pop('cleaned')
        self.assertInvalid(definition, 'task_contract')
        definition = root_definition('fixed')
        definition['steps'][-1]['inputs']['cleaned'] = '$steps.normalize.normalized'
        self.assertInvalid(definition, 'binding_role')

    def test_transitive_permissions_and_invalid_mode(self):
        definition = root_definition()
        definition['capabilities'].remove('orders.normalize')
        self.assertInvalid(definition, 'permission_denied')
        definition = clean_definition()
        definition['steps'][0]['kind'] = 'shell'
        self.assertInvalid(definition, 'invalid_mode')
        definition = clean_definition()
        definition['capabilities'].append('orders.profile')
        self.assertInvalid(definition, 'permission_denied')

    def test_valid_recursive_call_and_immutable_reference(self):
        definition = clean_definition()
        definition['steps'] = [{'id': 'child', 'kind': 'call', 'task': 'orders.clean',
                                'definition': digest(clean_definition()),
                                'inputs': {'source_file': '$input.source_file', 'rule': '$input.rule'}}]
        definition['outputs'] = {'cleaned': '$steps.child.cleaned'}
        self.assertTrue(validate_definition(definition, self.registry)['passed'])
        definition['steps'][0]['definition'] = 'current-latest'
        self.assertInvalid(definition, 'invalid_reference')

    def test_recovery_is_bounded_unique_and_cannot_lower_checks(self):
        rule = {'id': 'retryOnce', 'phase': 'execution', 'code': 'executor_unavailable', 'action': 'retry', 'max_attempts': 1}
        definition = clean_definition()
        definition['recovery'] = [rule]
        self.assertTrue(validate_definition(definition, self.registry)['passed'])
        mutations = [{'max_attempts': True}, {'max_attempts': 0}, {'max_attempts': 101}, {'action': 'replan'},
                     {'code': 'effect_unknown'}, {'code': 'verification_failed'}, {'phase': 'verification'}, {'script': 'replacement'}]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                definition['recovery'] = [{**rule, **mutation}]
                self.assertInvalid(definition)
        definition['recovery'] = [rule, {**rule, 'id': 'another'}]
        self.assertInvalid(definition, 'ambiguous_recovery')

    def test_malformed_json_never_bypasses_or_crashes_compiler(self):
        bad_values = [None, 1, True, [], {'x': float('nan')}, {'x': (1, 2)}, {1: 'bad'}]
        for value in bad_values:
            with self.subTest(value=value):
                self.assertInvalid(value)
        for field in root_definition():
            for value in (None, [], {}, 4, False):
                definition = root_definition()
                definition[field] = value
                with self.subTest(field=field, value=value):
                    # Some empty fields are legal (recovery only).
                    report = validate_definition(definition, self.registry)
                    self.assertIn('passed', report)
                    if field != 'recovery' or value != []:
                        self.assertFalse(report['passed'], report)
        for field, value in [('required', 1), ('type', []), ('enum', [True])]:
            definition = clean_definition()
            definition['params']['rule'][field] = value
            self.assertInvalid(definition)


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.registry = GuardedRegistry()
        self.inputs = {'source_file': 'artifact:logical-handle', 'rule': 'highest_revision'}

    def test_existing_wins_without_calling_planner(self):
        planner = RecordingPlanner()
        selected = prepare_method('orders.clean', self.inputs, Library([clean_definition()]), self.registry, planner)
        self.assertEqual(selected['origin'], 'existing')
        self.assertEqual(planner.calls, [])

    def test_first_only_is_rejected_and_current_planner_is_used(self):
        planner = RecordingPlanner()
        selected = prepare_method('orders.clean', self.inputs, Library([clean_definition('first')]), self.registry, planner)
        self.assertEqual(selected['origin'], 'generated')
        self.assertEqual(selected['selection'][0]['reason'], 'rule_not_supported')
        self.assertEqual(len(planner.calls), 1)
        self.assertNotIn('source_file', planner.calls[0])
        self.assertNotIn('expected', str(planner.calls[0]))
        self.assertEqual(planner.calls[0]['rule'], 'highest_revision')

    def test_no_silent_generation_or_default_answer(self):
        with self.assertRaises(SopError) as error:
            prepare_method('orders.clean', self.inputs, Library([]), self.registry)
        self.assertEqual(error.exception.code, 'capability_missing')
        with self.assertRaises(SopError) as error:
            prepare_method('orders.clean', {}, Library([clean_definition()]), self.registry, RecordingPlanner())
        self.assertEqual(error.exception.code, 'missing_information')

    def test_planner_candidate_must_satisfy_parent(self):
        for proposed in (root_definition(), clean_definition('first')):
            with self.assertRaises(SopError) as error:
                prepare_method('orders.clean', self.inputs, Library([]), self.registry, RecordingPlanner(proposed))
            self.assertEqual(error.exception.code, 'contract_mismatch')
        invalid = clean_definition()
        invalid['checks'] = []
        with self.assertRaises(SopError) as error:
            prepare_method('orders.clean', self.inputs, Library([]), self.registry, RecordingPlanner(invalid))
        self.assertEqual(error.exception.code, 'definition_invalid')

    def test_candidate_reuse_does_not_publish_and_library_is_copying(self):
        definition = clean_definition()
        library = Library()
        identity = library.add(definition, origin='generated')
        definition['checks'] = []
        self.assertEqual(library.get(identity)['checks'], ['orders.clean'])
        selected = prepare_method('orders.clean', self.inputs, library, self.registry)
        self.assertEqual(selected['origin'], 'candidate_reuse')
        self.assertNotIn('published', selected)


class AuthoringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / 'Skill.md'
        self.source.write_text(SOURCE.read_text())
        self.registry = GuardedRegistry()
        self.author = Authoring(self.base / 'data', self.registry)

    def deliver(self, proposal=None):
        draft = self.author.submit(self.source, proposal)
        self.assertEqual(draft['state'], 'waiting_info', draft['diagnostics'])
        return self.author.answer(draft['draft_id'], 'dedup_policy', read_json(ANSWER), 'answer-1', draft['revision'])

    def test_wait_resume_answer_full_provenance_and_zero_business_calls(self):
        draft = self.author.submit(self.source)
        self.assertEqual(draft['state'], 'waiting_info')
        restored = Authoring(self.base / 'data', self.registry).get(draft['draft_id'])
        self.assertEqual(restored['questions'], draft['questions'])
        delivered = self.author.answer(draft['draft_id'], 'dedup_policy', 'highest_revision', 'm1', draft['revision'])
        self.assertEqual(delivered['state'], 'delivered')
        self.assertTrue(delivered['report']['passed'])
        self.assertEqual(delivered['definition']['params']['rule']['enum'], ['highest_revision'])
        self.assertEqual(len(delivered['source_lines']), len(self.source.read_text().splitlines()))
        for requirement in delivered['requirements']:
            self.assertTrue(requirement['targets'])
            self.assertEqual(delivered['source_lines'][requirement['source']['line'] - 1]['text'], requirement['source']['text'])
            for target in requirement['targets']:
                self.assertIn(requirement['id'], delivered['requirement_map']['reverse'][target])
        for step in delivered['definition']['steps']:
            self.assertIn('steps.' + step['id'], delivered['requirement_map']['reverse'])
        self.assertEqual(delivered['publication'], 'unpublished')
        self.assertEqual(delivered['trials'], 'not_run')
        self.assertEqual(delivered['business_calls'], 0)
        self.assertFalse(delivered['report']['result_verified'])
        self.assertEqual(self.registry.calls, [])

    def test_answer_idempotence_precedes_revision_check_and_conflicts_rejected(self):
        draft = self.author.submit(self.source)
        args = (draft['draft_id'], 'dedup_policy', 'highest_revision', 'm1', draft['revision'])
        response = self.author.answer(*args)
        self.assertEqual(self.author.answer(*args), response)
        with self.assertRaises(SopError) as error:
            self.author.answer(draft['draft_id'], 'dedup_policy', 'first', 'm1', draft['revision'])
        self.assertEqual(error.exception.code, 'message_conflict')
        with self.assertRaises(SopError) as error:
            self.author.answer(draft['draft_id'], 'dedup_policy', 'first', 'm2', draft['revision'])
        self.assertEqual(error.exception.code, 'stale_revision')

    def test_invalid_and_foreign_answers_do_not_close_question(self):
        draft = self.author.submit(self.source)
        for request, value, revision, code in [('foreign', 'highest_revision', 1, 'question_mismatch'),
                                               ('dedup_policy', 'last_line', 1, 'invalid_answer'),
                                               ('dedup_policy', 'highest_revision', 0, 'stale_revision'),
                                               ('dedup_policy', 'highest_revision', True, 'stale_revision')]:
            with self.subTest(code=code):
                with self.assertRaises(SopError) as error:
                    self.author.answer(draft['draft_id'], request, value, 'invalid', revision)
                self.assertEqual(error.exception.code, code)
                current = self.author.get(draft['draft_id'])
                self.assertEqual(current['state'], 'waiting_info')
                self.assertEqual(current['questions'][0]['status'], 'open')
                self.assertEqual(current['revision'], 1)

    def test_explicit_rules_deliver_without_run_paths(self):
        self.source.write_text(self.source.read_text() + '\n去重规则：highest_revision。\n')
        draft = self.author.submit(self.source)
        self.assertEqual(draft['state'], 'delivered')
        self.assertEqual(draft['questions'], [])
        self.assertNotIn(str(self.source), str(draft['definition']))
        self.assertTrue(draft['definition']['params']['source_file']['required'])
        self.assertEqual(draft['definition']['params']['source_file']['type'], 'artifact')

    def test_unknown_requirement_is_preserved_even_with_valid_proposal(self):
        self.source.write_text(self.source.read_text() + '\n- 将结果上传 OSS 并删除原文件。\n')
        draft = self.author.submit(self.source, {'definition': root_definition()})
        self.assertEqual(draft['state'], 'stopped')
        unsupported = [r for r in draft['requirements'] if r['status'] == 'unsupported']
        self.assertEqual(len(unsupported), 1)
        self.assertIn('删除原文件', unsupported[0]['source']['text'])
        self.assertIsNone(draft['report'])
        self.assertEqual(self.registry.calls, [])

    def test_arbitrary_nl_cannot_pass_scoped_recognizer(self):
        self.source.write_text('帮我智能处理订单。\n')
        draft = self.author.submit(self.source)
        self.assertEqual(draft['state'], 'stopped')
        self.assertIn('unsupported_requirement', [d['code'] for d in draft['diagnostics']])
        self.assertEqual(draft['semantic_evidence']['general_nl_validation'], 'not_run')

    def test_model_failure_is_not_presented_as_missing_user_information(self):
        self.source.write_text('帮我智能处理订单。\n')
        class Unavailable:
            def author(self, context):
                raise SopError('model_timeout', 'provider timed out')
        draft = Authoring(self.base / 'model', self.registry, Unavailable()).submit(self.source)
        self.assertEqual(draft['state'], 'stopped')
        self.assertIn('model_timeout', [d['code'] for d in draft['diagnostics']])
        self.assertEqual(draft['questions'], [])

    def test_authoring_model_budget_validation_and_zero_budget_stops(self):
        for invalid in (-1, True, 1.0, '1', None):
            with self.subTest(invalid=invalid):
                with self.assertRaises(SopError) as error:
                    Authoring(self.base / 'invalid-budget', self.registry, max_model_calls=invalid)
                self.assertEqual(error.exception.code, 'invalid_budget')
        self.source.write_text('帮我智能处理订单。\n')
        class ForbiddenModel:
            def author(self, context):
                raise AssertionError('zero model budget must prevent backend invocation')
        author = Authoring(self.base / 'budget-zero', self.registry, ForbiddenModel(), max_model_calls=0)
        draft = author.submit(self.source)
        self.assertEqual(draft['state'], 'stopped')
        self.assertEqual(draft['usage'], {'model_calls': 0})
        self.assertEqual(draft['limits'], {'max_model_calls': 0})
        self.assertIn('budget_exhausted', [d['code'] for d in draft['diagnostics']])
        self.assertNotIn('model_failure', [d['code'] for d in draft['diagnostics']])
        self.assertEqual(author.get(draft['draft_id'])['usage'], {'model_calls': 0})

    def test_authoring_default_budget_reserves_before_one_model_call(self):
        self.source.write_text('帮我智能处理订单。\n')
        saved = []
        data = self.base / 'budget-default'
        class CountingModel:
            calls = 0
            def author(self, context):
                self.calls += 1
                saved.append(read_json(next((data / 'authoring' / 'drafts').glob('*.json'))))
                return {'definition': root_definition(), 'semantics': {'self_review': 'passed'}}
        model = CountingModel()
        author = Authoring(data, self.registry, model)
        draft = author.submit(self.source)
        self.assertEqual(model.calls, 1)
        self.assertEqual(saved[0]['usage'], {'model_calls': 1})
        self.assertEqual(saved[0]['limits'], {'max_model_calls': 1})
        self.assertEqual(saved[0]['model_attempt']['state'], 'reserved')
        self.assertEqual(draft['usage'], {'model_calls': 1})
        self.assertEqual(draft['model_attempt']['state'], 'completed')
        self.assertEqual(draft['state'], 'stopped')
        self.assertTrue(any(r['status'] == 'unsupported' for r in draft['requirements']))
        self.assertEqual(author.get(draft['draft_id'])['usage'], {'model_calls': 1})
        self.assertEqual(model.calls, 1)
        self.assertEqual(self.registry.calls, [])

    def test_interrupted_authoring_model_attempt_keeps_reserved_budget(self):
        self.source.write_text('帮我智能处理订单。\n')
        class Crash(BaseException):
            pass
        class InterruptedModel:
            calls = 0
            def author(self, context):
                self.calls += 1
                raise Crash()
        model = InterruptedModel()
        data = self.base / 'budget-interrupted'
        author = Authoring(data, self.registry, model)
        with self.assertRaises(Crash):
            author.submit(self.source)
        saved = read_json(next((data / 'authoring' / 'drafts').glob('*.json')))
        self.assertEqual(saved['usage'], {'model_calls': 1})
        self.assertEqual(saved['model_attempt']['state'], 'reserved')
        restored = Authoring(data, self.registry, model).get(saved['draft_id'])
        self.assertEqual(restored['usage'], {'model_calls': 1})
        self.assertEqual(model.calls, 1)
        self.assertNotEqual(restored['state'], 'delivered')

    def test_fixed_mode_rejects_pi_replacement_and_does_not_call_model(self):
        self.source.write_text(self.source.read_text() + '\n执行模式：fixed。\n')
        class ForbiddenModel:
            def author(self, context):
                raise AssertionError('known fixed Skill does not require a model')
        self.author = Authoring(self.base / 'data', self.registry, ForbiddenModel())
        delivered = self.deliver()
        self.assertEqual(delivered['state'], 'delivered')
        self.assertTrue(all(step['kind'] == 'fixed' for step in delivered['definition']['steps']))
        proposed = root_definition()
        proposed['params']['rule']['enum'] = ['highest_revision']
        rejected = self.deliver({'definition': proposed})
        self.assertEqual(rejected['state'], 'stopped')
        self.assertIn('fixed_mode_violation', [d['code'] for d in rejected['report']['diagnostics']])

    def test_missing_output_and_wrong_rule_are_rejected_with_source_trace(self):
        proposed = root_definition()
        proposed['params']['rule']['enum'] = ['highest_revision']
        proposed['outputs'].pop('cleaned')
        draft = self.deliver({'definition': proposed})
        self.assertEqual(draft['state'], 'stopped')
        self.assertFalse(draft['report']['passed'])
        self.assertTrue(any(r['meaning'] == 'clean' for r in draft['requirements']))
        output_error = next(d for d in draft['report']['diagnostics'] if d['path'] == '$.outputs')
        self.assertTrue(output_error['source_lines'])
        self.assertTrue(output_error['requirement_ids'])
        proposed = root_definition()
        proposed['params']['rule']['enum'] = ['first']
        draft = self.deliver({'definition': proposed})
        self.assertEqual(draft['state'], 'stopped')
        self.assertIn('requirement_mismatch', [d['code'] for d in draft['report']['diagnostics']])

    def test_structured_proposal_validates_bidirectional_source_metadata(self):
        baseline = self.deliver()
        proposal = {'definition': baseline['definition'], 'requirements': baseline['requirements'],
                    'source_annotations': baseline['source_lines'], 'semantics': {'note': 'protocol fixture'}}
        self.assertEqual(self.deliver(proposal)['state'], 'delivered')
        proposal['requirements'] = proposal['requirements'][:-1]
        draft = self.deliver(proposal)
        self.assertEqual(draft['state'], 'stopped')
        self.assertIn('provenance_mismatch', [d['code'] for d in draft['report']['diagnostics']])

    def test_dependency_source_and_answers_changes_invalidate_but_keep_report(self):
        for changed in ('dependency', 'source', 'answers', 'mapping'):
            with self.subTest(changed=changed):
                self.registry.version = None
                self.source.write_text(SOURCE.read_text())
                delivered = self.deliver()
                if changed == 'dependency':
                    self.registry.version = 'f' * 64
                elif changed == 'source':
                    self.source.write_text(self.source.read_text() + '\nChanged requirement')
                else:
                    path = self.author._path(delivered['draft_id'])
                    stored = read_json(path)
                    if changed == 'answers':
                        stored['answers']['rule']['value'] = 'first'
                    else:
                        stored['requirement_map']['reverse'] = {}
                    write_json(path, stored)
                invalidated = self.author.get(delivered['draft_id'])
                self.assertEqual(invalidated['state'], 'stopped')
                self.assertFalse(invalidated['report']['passed'])
                self.assertFalse(invalidated['report']['applicable'])
                self.assertEqual(invalidated['report_history'], [delivered['report']])
                self.assertEqual(self.author.get(delivered['draft_id'])['revision'], invalidated['revision'])

    def test_source_change_during_question_cannot_deliver_old_requirements(self):
        draft = self.author.submit(self.source)
        self.source.write_text(self.source.read_text() + '\nnew restriction')
        with self.assertRaises(SopError) as error:
            self.author.answer(draft['draft_id'], 'dedup_policy', 'highest_revision', 'm1', 1)
        self.assertEqual(error.exception.code, 'source_changed')
        self.assertEqual(self.author.get(draft['draft_id'])['state'], 'stopped')

    def test_missing_checker_and_storage_failure_cannot_deliver(self):
        with patch.object(self.registry, 'checkers', return_value={}):
            draft = self.deliver()
        self.assertEqual(draft['state'], 'stopped')
        self.assertIn('unknown_checker', [d['code'] for d in draft['report']['diagnostics']])
        with patch('sop.authoring.write_json', side_effect=OSError('disk unavailable')):
            with self.assertRaises(SopError) as error:
                self.author.submit(self.source)
        self.assertEqual(error.exception.code, 'storage_failure')

    def test_new_source_paths_and_answer_messages_do_not_enter_reusable_definition(self):
        first = self.deliver()
        other = self.base / 'other-Skill.md'
        other.write_text(SOURCE.read_text())
        second = self.author.submit(other)
        second = self.author.answer(second['draft_id'], 'dedup_policy', 'highest_revision', 'different-message', second['revision'])
        self.assertEqual(first['definition'], second['definition'])
        self.assertNotEqual(first['draft_id'], second['draft_id'])
        self.assertNotEqual(first['report']['answers_hash'], second['report']['answers_hash'])


if __name__ == '__main__':
    unittest.main()
