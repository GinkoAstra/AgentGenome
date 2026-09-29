"""End-to-end, fault-injection and independent acceptance of the public runtime."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sop.agents import ScriptedAgent, ScriptedPlanner
from sop.authoring import Authoring, Library, clean_definition, root_definition
from sop.capabilities import Registry
from sop.common import ExecutionError, SopError, digest, read_json, write_json
from sop.service import application
from sop.store import Store

FIXTURE = Path(__file__).resolve().parents[2] / 'tests/fixtures/first-release'

class Crash(BaseException):
    pass

class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def app(self, mode='existing', hook=None, registry=None, agent=None, planner=None, definitions=None):
        library = Library(definitions if definitions is not None else [clean_definition('first')] + ([clean_definition()] if mode == 'existing' else []))
        return application(self.root / '.data', library=library, registry=registry,
                           agent=agent or ScriptedAgent(), planner=planner or ScriptedPlanner(), hook=hook)

    def start(self, rt, batch='batch-01', mode='pi', rule=None, definition=None, limits=None):
        definition = definition or root_definition(mode)
        inputs = {'source_file': FIXTURE / batch / 'input.csv'}
        if rule is not None:
            inputs['rule'] = rule
        return rt.start(definition, inputs, capabilities=definition['capabilities'], limits=limits)

    def answer(self, rt, run, value='highest_revision'):
        question = next(q for q in run['questions'].values() if q['state'] == 'open')
        return rt.answer(run['id'], question['request_id'], value, message_id='answer', revision=run['revision'])

    def finish(self, rt, run):
        run = rt.advance(run['id'])
        if run['state'] == 'waiting_info':
            self.answer(rt, run)
            run = rt.advance(run['id'])
        return run

    def assert_outputs(self, rt, run, batch):
        self.assertEqual(run['state'], 'succeeded', run.get('error'))
        for key, ref in run['tasks'][run['root']]['outputs'].items():
            expected = FIXTURE / batch / 'expected' / (key + ('.json' if key == 'quality' else '.csv'))
            if key == 'quality':
                self.assertEqual(read_json(rt.store.path(ref)), read_json(expected))
            else:
                self.assertEqual(rt.store.path(ref).read_bytes(), expected.read_bytes())

    def test_existing_and_runtime_generated_both_batches(self):
        for mode in ('existing', 'generated'):
            rt = self.app(mode)
            definition = root_definition()
            hashes = []
            for batch in ('batch-01', 'batch-02'):
                run = self.finish(rt, self.start(rt, batch, definition=definition))
                self.assert_outputs(rt, run, batch)
                child = next(t for t in run['tasks'].values() if t['parent_call'])
                self.assertEqual(child['accepted']['origin'], 'existing' if mode == 'existing' else 'generated')
                self.assertTrue(any(not x['accepted'] for x in child['accepted']['selection']))
                self.assertEqual(run['publication'], 'unpublished')
                hashes.append(run['tasks'][run['root']]['definition']['hash'])
                self.assertEqual(len(run['answers']), 1)
            self.assertEqual(hashes[0], hashes[1])

    def test_fixed_no_agent_or_planner_invocations(self):
        class Forbidden(ScriptedAgent):
            def respond(self, context):
                raise AssertionError('fixed execution called a model')
        rt = self.app(agent=Forbidden())
        for batch in ('batch-01', 'batch-02'):
            run = self.finish(rt, self.start(rt, batch, mode='fixed'))
            self.assert_outputs(rt, run, batch)
            self.assertEqual(run['usage']['model_calls'], 0)

    def test_saved_generated_candidate_reused_with_new_input(self):
        rt = self.app('generated')
        first = self.finish(rt, self.start(rt))
        child = next(t for t in first['tasks'].values() if t['parent_call'])
        candidate = rt.store.json(child['definition'])
        class NoPlanning(ScriptedPlanner):
            def plan(self, context):
                raise AssertionError('saved candidate was regenerated')
        rt = self.app('generated', definitions=[candidate], planner=NoPlanning())
        second = self.finish(rt, self.start(rt, 'batch-02'))
        self.assert_outputs(rt, second, 'batch-02')
        other = next(t for t in second['tasks'].values() if t['parent_call'])
        self.assertEqual(child['definition'], other['definition'])
        self.assertNotEqual(first['id'], second['id'])
        self.assertNotEqual(child['inputs']['source_file'], other['inputs']['source_file'])

    def test_wait_restart_invalid_duplicate_conflicting_stale_answers(self):
        rt = self.app()
        run = rt.advance(self.start(rt)['id'])
        self.assertEqual(run['state'], 'waiting_info')
        question = next(iter(run['questions'].values()))
        rt = self.app()
        self.assertEqual(rt.advance(run['id']), run)
        with self.assertRaisesRegex(SopError, 'rule must'):
            self.answer(rt, run, {'rule': 'highest_revision', 'script': 'evil'})
        self.assertEqual(rt.store.load(run['id'])['questions'][question['request_id']]['state'], 'open')
        accepted = self.answer(rt, run)
        self.assertEqual(self.answer(rt, run), accepted)
        with self.assertRaises(SopError) as conflict:
            self.answer(rt, run, 'first')
        self.assertEqual(conflict.exception.code, 'message_conflict')
        with self.assertRaises(SopError) as stale:
            rt.answer(run['id'], question['request_id'], 'first', message_id='new', revision=run['revision'])
        self.assertEqual(stale.exception.code, 'stale_revision')
        self.assert_outputs(rt, rt.advance(run['id']), 'batch-01')

    def test_cross_instance_answer_rejected(self):
        rt = self.app()
        run = rt.advance(self.start(rt)['id'])
        q = next(iter(run['questions'].values()))
        with self.assertRaises(SopError) as wrong:
            rt.answer(run['id'], q['request_id'], 'first', message_id='wrong', revision=run['revision'], owner='other')
        self.assertEqual(wrong.exception.code, 'wrong_scope')

    def test_accepted_dynamic_definition_not_replanned_after_crash(self):
        def hook(kind, run):
            if kind == 'method_accepted':
                raise Crash()
        rt = self.app('generated', hook=hook)
        run = self.start(rt, rule='highest_revision')
        with self.assertRaises(Crash):
            rt.advance(run['id'])
        class Forbidden(ScriptedPlanner):
            def plan(self, context):
                raise AssertionError('restore invoked planner')
        rt = self.app('generated', planner=Forbidden())
        self.assert_outputs(rt, rt.advance(run['id']), 'batch-01')
        self.assertEqual(sum(e['kind'] == 'method_accepted' for e in rt.store.events(run['id'])), 1)

    def test_committed_child_return_consumed_once(self):
        def hook(kind, run):
            if kind == 'task_succeeded' and any(t['state']=='succeeded' and t['parent_call'] for t in run['tasks'].values()):
                raise Crash()
        rt = self.app(hook=hook)
        run = self.start(rt, rule='highest_revision')
        with self.assertRaises(Crash):
            rt.advance(run['id'])
        before = rt.store.load(run['id'])
        count = len(before['operations'])
        rt = self.app()
        done = rt.advance(run['id'])
        self.assert_outputs(rt, done, 'batch-01')
        self.assertEqual(len(done['operations']), count + 1) # only parent summary remains
        self.assertEqual(sum(e['kind']=='child_returned' for e in rt.store.events(run['id'])), 1)
        self.assertEqual(rt.advance(run['id']), done)

    def test_unknown_intent_not_replayed(self):
        def hook(kind, run):
            if kind == 'operation_intent':
                raise Crash()
        rt = self.app(hook=hook)
        run = self.start(rt, mode='fixed', rule='highest_revision')
        with self.assertRaises(Crash):
            rt.advance(run['id'])
        rt = self.app()
        resumed = rt.advance(run['id'])
        self.assertEqual(resumed['state'], 'effect_unknown')
        self.assertEqual(len(resumed['operations']), 1)
        self.assertEqual(rt.advance(run['id']), resumed)

    def test_effect_without_candidate_commit_not_replayed(self):
        class InterruptedRegistry(Registry):
            def execute(self, cap, inputs, work_dir):
                result = super().execute(cap, inputs, work_dir)
                raise Crash()
        rt = self.app(registry=InterruptedRegistry())
        run = self.start(rt, mode='fixed', rule='highest_revision')
        with self.assertRaises(Crash):
            rt.advance(run['id'])
        self.assertTrue(list((self.root/'.data/work').rglob('profile.json')))
        rt = self.app()
        resumed = rt.advance(run['id'])
        self.assertEqual(resumed['state'], 'effect_unknown')
        self.assertEqual(len(resumed['operations']), 1)

    def test_saved_fixed_candidate_does_not_repeat_tool(self):
        def hook(kind, run):
            if kind == 'operation_candidate':
                raise Crash()
        rt = self.app(hook=hook)
        run = self.start(rt, mode='fixed', rule='highest_revision')
        with self.assertRaises(Crash):
            rt.advance(run['id'])
        rt = self.app()
        done = rt.advance(run['id'])
        self.assert_outputs(rt, done, 'batch-01')
        self.assertEqual(len(done['operations']), 4)

    def test_task_verification_restart_uses_same_candidate(self):
        def hook(kind, run):
            if kind == 'verification_started':
                raise Crash()
        rt = self.app(hook=hook)
        run = self.start(rt, mode='fixed', rule='highest_revision')
        with self.assertRaises(Crash):
            rt.advance(run['id'])
        rt = self.app()
        done = rt.advance(run['id'])
        self.assert_outputs(rt, done, 'batch-01')
        self.assertEqual(len(done['operations']), 4)

    def test_conflict_stops_no_implicit_ai_repair(self):
        rt = self.app()
        done = self.finish(rt, self.start(rt, 'conflict-highest-revision', mode='fixed'))
        self.assertEqual(done['state'], 'failed')
        self.assertEqual(done['usage']['model_calls'], 0)
        self.assertFalse(any(x['capability']=='orders.summarize' for x in done['operations'].values()))

    def test_child_success_not_parent_success(self):
        class WrongSummary(Registry):
            def execute(self, cap, inputs, work_dir):
                outputs = super().execute(cap, inputs, work_dir)
                if cap == 'orders.summarize':
                    data = read_json(outputs['quality'])
                    data['total_net_cents'] += 1
                    write_json(outputs['quality'], data)
                return outputs
        rt = self.app(registry=WrongSummary())
        done = self.finish(rt, self.start(rt))
        self.assertEqual(done['state'], 'failed')
        self.assertEqual(done['error']['code'], 'verification_failed')
        self.assertTrue(any(t['parent_call'] and t['state']=='succeeded' for t in done['tasks'].values()))
        self.assertFalse(done['tasks'][done['root']]['verification']['passed'])

    def test_false_model_completion_rejected(self):
        agent = ScriptedAgent([{'kind':'candidate_result','outputs':{}}])
        rt = self.app(agent=agent)
        done = self.finish(rt, self.start(rt, rule='highest_revision'))
        self.assertEqual(done['state'], 'failed')
        self.assertEqual(done['error']['code'], 'invalid_candidate')

    def test_generated_permission_escalation_rejected_before_child_effect(self):
        class EvilPlanner(ScriptedPlanner):
            def plan(self, context):
                definition = super().plan(context)
                definition['capabilities'].append('files.delete')
                return definition
        rt = self.app('generated', planner=EvilPlanner())
        done = self.finish(rt, self.start(rt))
        self.assertEqual(done['state'], 'failed')
        self.assertEqual(len(done['operations']), 1) # profile only

    def test_subtask_cannot_replace_parent_input(self):
        class EvilAgent(ScriptedAgent):
            def respond(self, context):
                result = super().respond(context)
                if result['kind']=='need_subtask':
                    result['inputs']['source_file']='/etc/passwd'
                return result
        rt = self.app(agent=EvilAgent())
        done = self.finish(rt, self.start(rt))
        self.assertEqual(done['state'], 'failed')
        self.assertEqual(done['error']['code'], 'parent_contract')

    def test_root_budget_persists_and_cannot_be_bypassed_by_restart(self):
        rt = self.app()
        run = self.start(rt, limits={'max_actions':100,'max_model_calls':1,'max_depth':8})
        run = rt.advance(run['id'])
        self.answer(rt, run)
        done = self.app().advance(run['id'])
        self.assertEqual(done['state'], 'budget_exhausted')
        self.assertEqual(done['usage']['model_calls'], 1)
        self.assertEqual(self.app().advance(run['id']), done)

    def test_true_model_request_upper_bound_is_reserved(self):
        class MultiCall(ScriptedAgent):
            max_model_calls_per_request = 3
            def respond(self, context):
                raise AssertionError('insufficient budget must prevent model request')
        rt = self.app(agent=MultiCall())
        run = self.start(rt, limits={'max_actions':100,'max_model_calls':2,'max_depth':8})
        done = rt.advance(run['id'])
        self.assertEqual(done['state'], 'budget_exhausted')
        self.assertEqual(done['usage']['model_calls'], 0)

    def test_declared_transient_retry_is_durable_and_fixed(self):
        class Transient(Registry):
            calls = 0
            def execute(self, cap, inputs, work_dir):
                if cap=='orders.profile':
                    self.calls += 1
                    if self.calls==1:
                        raise ExecutionError('temporary_read', 'reader unavailable', not_dispatched=True)
                return super().execute(cap, inputs, work_dir)
        definition = root_definition('fixed')
        definition['recovery'] = [{'id':'read_retry','phase':'execution','code':'temporary_read','action':'retry','max_attempts':1}]
        def hook(kind, run):
            if kind == 'recovery_reserved':
                raise Crash()
        registry = Transient()
        rt = self.app(registry=registry, hook=hook)
        run = self.start(rt, definition=definition, rule='highest_revision')
        with self.assertRaises(Crash):
            rt.advance(run['id'])
        rt = self.app(registry=registry)
        done = rt.advance(run['id'])
        self.assert_outputs(rt, done, 'batch-01')
        self.assertEqual(len(done['recoveries']), 1)
        self.assertEqual(done['tasks'][done['root']]['recovery_counts']['read_retry'], 1)
        self.assertEqual(done['usage']['model_calls'], 0)

    def test_untrusted_failure_does_not_match_safe_retry(self):
        class Untrusted(Registry):
            def execute(self, cap, inputs, work_dir):
                raise SopError('temporary_read', 'model claims temporary; no trusted dispatch evidence')
        definition = root_definition('fixed')
        definition['recovery'] = [{'id':'read_retry','phase':'execution','code':'temporary_read','action':'retry','max_attempts':2}]
        rt = self.app(registry=Untrusted())
        done = rt.advance(self.start(rt, definition=definition, rule='highest_revision')['id'])
        self.assertEqual(done['state'], 'failed')
        self.assertEqual(done['recoveries'], [])
        self.assertEqual(len(done['operations']), 1)

    def test_retry_limit_survives_new_attempt_and_restart(self):
        class Broken(Registry):
            def execute(self, cap, inputs, work_dir):
                raise ExecutionError('temporary_read', 'unavailable', not_dispatched=True)
        definition = root_definition('fixed')
        definition['recovery'] = [{'id':'read_retry','phase':'execution','code':'temporary_read','action':'retry','max_attempts':1}]
        rt = self.app(registry=Broken())
        done = rt.advance(self.start(rt, definition=definition, rule='highest_revision')['id'])
        self.assertEqual(done['state'], 'failed')
        self.assertEqual(len(done['operations']), 2)
        self.assertEqual(len(done['recoveries']), 1)

    def test_artifact_tampering_blocks_restore(self):
        rt = self.app()
        run = rt.advance(self.start(rt)['id'])
        ref = run['tasks'][run['root']]['inputs']['source_file']
        path = rt.store.path(ref)
        path.chmod(0o600)
        path.write_text('changed')
        with self.assertRaises(SopError) as failure:
            self.app().advance(run['id'])
        self.assertEqual(failure.exception.code, 'integrity')

    def test_dependency_change_blocks_restore(self):
        rt = self.app()
        run = rt.advance(self.start(rt)['id'])
        class Changed(Registry):
            def checkers(self):
                return {k:'changed' for k in super().checkers()}
        with self.assertRaises(SopError) as failure:
            self.app(registry=Changed()).advance(run['id'])
        self.assertEqual(failure.exception.code, 'dependency_drift')

    def test_cancelled_run_does_not_accept_late_answer(self):
        rt = self.app()
        run = rt.advance(self.start(rt)['id'])
        cancelled = rt.cancel(run['id'])
        q = next(iter(run['questions'].values()))
        with self.assertRaises(SopError):
            rt.answer(run['id'], q['request_id'], 'first', message_id='late', revision=cancelled['revision'])
        self.assertEqual(rt.advance(run['id'])['state'], 'cancelled')

    def test_store_single_writer_and_atomic_stale_commit(self):
        rt = self.app()
        run = self.start(rt)
        with rt.store.lock(run['id']):
            with self.assertRaises(SopError) as busy:
                self.app().advance(run['id'])
            self.assertEqual(busy.exception.code, 'run_busy')
        stale = deepcopy(run)
        rt.store.commit(run, 'one')
        with self.assertRaises(SopError):
            rt.store.commit(stale, 'two')
        self.assertFalse(any(e['kind']=='two' for e in rt.store.events(run['id'])))

    def test_definition_has_no_instance_paths_or_answers(self):
        rt = self.app()
        definition = root_definition()
        original = digest(definition)
        for batch in ('batch-01','batch-02'):
            run = self.finish(rt, self.start(rt, batch, definition=definition))
            self.assertEqual(run['tasks'][run['root']]['definition']['hash'], original)
        self.assertEqual(digest(definition), original)
        self.assertNotIn(str(self.root), json.dumps(definition))

    def test_child_method_waits_for_answer_then_maps_fact_to_parent(self):
        definition = root_definition()
        definition['params']['rule']['required'] = True
        definition['steps'][1]['kind'] = 'call'
        rt = self.app()
        run = rt.advance(self.start(rt, definition=definition)['id'])
        self.assertEqual(run['state'], 'waiting_info')
        question = next(iter(run['questions'].values()))
        self.assertNotEqual(question['owner'], run['root'])
        self.assertEqual(len(run['operations']), 1)
        self.answer(rt, run)
        done = self.app().advance(run['id'])
        self.assert_outputs(rt, done, 'batch-01')
        self.assertEqual(done['tasks'][done['root']]['inputs']['rule'], 'highest_revision')
        self.assertTrue(any(a['source']=='child_return' for a in done['answers']))
        self.assertEqual(done['usage']['model_calls'], 0)

    @staticmethod
    def wrapper(base=None, kind='call'):
        definition = clean_definition()
        definition['id'] = 'nested-clean'
        step = {'id':'nested', 'kind':kind, 'task':'orders.clean',
                'inputs':{'source_file':'$input.source_file','rule':'$input.rule'}}
        if base is not None:
            step['definition'] = digest(base)
        definition['steps'] = [step]
        definition['outputs'] = {'cleaned':'$steps.nested.cleaned'}
        return definition

    def test_multiple_recursive_levels_use_same_controller(self):
        base = clean_definition()
        wrapper = self.wrapper(base)
        rt = self.app(definitions=[wrapper,base])
        done = self.finish(rt, self.start(rt))
        self.assert_outputs(rt, done, 'batch-01')
        self.assertEqual(max(t['depth'] for t in done['tasks'].values()), 2)
        self.assertEqual(len(done['calls']), 2)
        self.assertTrue(all(c['consumed'] for c in done['calls'].values()))
        self.assertTrue(all(t['verification']['passed'] for t in done['tasks'].values()))

    def test_repeated_equivalent_expansion_stops_without_side_effects(self):
        recursive = self.wrapper(kind='pi')
        rt = self.app(definitions=[recursive])
        done = self.finish(rt, self.start(rt))
        self.assertEqual(done['state'], 'failed')
        self.assertEqual(done['error']['code'], 'stagnation')
        self.assertEqual(len(done['operations']), 1)
        self.assertEqual(len(done['calls']), 1)

    def test_depth_budget_limits_expansion(self):
        base=clean_definition()
        rt=self.app(definitions=[self.wrapper(base),base])
        run=self.start(rt, rule='highest_revision', limits={'max_actions':100,'max_model_calls':20,'max_depth':1})
        done=rt.advance(run['id'])
        self.assertEqual(done['state'],'budget_exhausted')
        self.assertEqual(done['error']['message'],'max_depth')
        self.assertEqual(len(done['operations']),1)

    def test_context_excludes_unrelated_facts_and_sibling_artifacts(self):
        rt=self.app()
        run=rt.advance(self.start(rt)['id'])
        self.answer(rt,run)
        run=rt.store.load(run['id'])
        run['answers'].append({'owner':'unrelated-sibling','field':'secret','value':'do-not-propagate'})
        rt.store.commit(run,'unrelated_fixture_fact')
        class ContextAudit(ScriptedAgent):
            def respond(inner,context):
                self.assertNotIn('do-not-propagate',json.dumps(context))
                self.assertNotIn('profile',context['inputs'])
                self.assertEqual(context['contract']['checks'],['orders.clean'])
                self.assertIn('modify_inputs',context['contract']['forbidden'])
                self.assertEqual(context['information_query'],'context_only')
                self.assertEqual(context['inputs']['rule'],'highest_revision')
                return super(ContextAudit,inner).respond(context)
        done=self.app(agent=ContextAudit()).advance(run['id'])
        self.assert_outputs(rt,done,'batch-01')
        self.assertEqual(len(done['questions']),1)

    def test_existing_fact_answers_agent_without_user_question(self):
        class AskOnce(ScriptedAgent):
            asked=False
            def respond(inner,context):
                if not inner.asked:
                    inner.asked=True
                    return {'kind':'need_info','field':'rule','reason':'retrieve bound rule'}
                return super(AskOnce,inner).respond(context)
        rt=self.app(agent=AskOnce())
        done=rt.advance(self.start(rt,rule='highest_revision')['id'])
        self.assert_outputs(rt,done,'batch-01')
        self.assertEqual(done['questions'],{})
        self.assertEqual(sum(e['kind']=='information_satisfied' for e in rt.store.events(done['id'])),1)

    def test_strict_json_duplicate_keys_and_nonfinite_rejected(self):
        for data in ('{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}'):
            path = self.root / 'invalid.json'
            path.write_text(data)
            with self.assertRaises(SopError):
                read_json(path)

if __name__ == '__main__':
    unittest.main()
