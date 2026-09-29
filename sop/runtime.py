"""B2: one durable authority for calls, questions, effects, and acceptance.

Adapters receive values and return proposals; none are given this store or state.
"""
from copy import deepcopy
from pathlib import Path
import time
import uuid

from .common import ExecutionError, SopError, canonical, digest

TERMINAL = {'succeeded', 'failed', 'cancelled', 'effect_unknown', 'budget_exhausted'}


def identity(prefix):
    return prefix + '_' + uuid.uuid4().hex[:16]


def artifact(value):
    return isinstance(value, dict) and set(value) == {'hash', 'bytes', 'kind'}


class Runtime:
    def __init__(self, store, registry, validator, resolver, *, agent=None, hook=None):
        self.store, self.registry = store, registry
        self.validator, self.resolver, self.agent = validator, resolver, agent
        self.hook = hook

    def _save(self, run, kind, data=None, receipt=None):
        self.store.commit(run, kind, data, receipt)
        if self.hook:
            self.hook(kind, deepcopy(run))

    def _outputs(self, task):
        contracts = self.registry.task_contracts()
        if task not in contracts:
            raise SopError('unknown_task', 'task contract is not registered')
        return set(contracts[task]['outputs'])

    def _check_definition(self, definition, grant, expected_task=None):
        report = self.validator(definition, self.registry)
        if not report.get('passed'):
            raise SopError('definition_rejected', canonical(report))
        if not set(definition['capabilities']) <= set(grant):
            raise SopError('permission_denied', 'definition exceeds original capability grant')
        if expected_task and definition['task'] != expected_task:
            raise SopError('parent_contract', 'child task differs from requested contract')
        if not self._outputs(definition['task']) <= set(definition['outputs']):
            raise SopError('parent_contract', 'child lacks required outputs')
        if definition['task'] not in definition['checks']:
            raise SopError('parent_contract', 'independent task checker is required')
        return report

    def _parameters(self, definition, inputs, allow_missing_rule=False):
        params = definition['params']
        if set(inputs) - set(params):
            raise SopError('invalid_parameter', 'undeclared input field')
        for name, spec in params.items():
            value = inputs.get(name)
            if value is None:
                form_fields = getattr(self.registry, 'parameter_forms', lambda: {})().get(definition['task'], {}).get('fields', {})
                if spec.get('required') and not (allow_missing_rule and (name == 'rule' or name in form_fields)):
                    raise SopError('missing_parameter', name)
                continue
            if spec.get('type') == 'artifact':
                self.store.path(value)
            elif spec.get('type') == 'string' and not isinstance(value, str):
                raise SopError('invalid_parameter', name + ' must be string')
            elif spec.get('type') == 'integer' and type(value) is not int:
                raise SopError('invalid_parameter', name + ' must be integer')
            elif spec.get('type') == 'object' and not isinstance(value, dict):
                raise SopError('invalid_parameter', name + ' must be an object')
            if 'enum' in spec and value not in spec['enum']:
                raise SopError('invalid_parameter', name + ' outside enum')
        if definition.get('supported_rules') and inputs.get('rule') and inputs['rule'] not in definition['supported_rules']:
            raise SopError('incompatible_rule', 'method does not support bound rule')

    def start(self, definition, inputs, *, capabilities, limits=None, authoring=None, parameter_policy=None):
        limits = dict(limits or {'max_actions': 100, 'max_model_calls': 20, 'max_depth': 8})
        if set(limits) != {'max_actions', 'max_model_calls', 'max_depth'} or any(type(v) is not int or v < 1 for v in limits.values()):
            raise SopError('invalid_budget', 'positive integer limits are required')
        if not isinstance(capabilities, list) or any(not isinstance(x, str) for x in capabilities):
            raise SopError('permission_denied', 'explicit capability grant required')
        parameter_policy = deepcopy(parameter_policy if parameter_policy is not None else {'max_chunk_size':1024})
        if (not isinstance(parameter_policy, dict) or set(parameter_policy) != {'max_chunk_size'} or
                type(parameter_policy['max_chunk_size']) is not int or parameter_policy['max_chunk_size'] < 1):
            raise SopError('invalid_parameter_policy', 'trusted max_chunk_size must be a positive integer')
        report = self._check_definition(definition, capabilities)
        bindings = deepcopy(inputs)
        for key, spec in definition['params'].items():
            if spec.get('type') == 'artifact' and key in bindings and isinstance(bindings[key], (str, Path)):
                bindings[key] = self.store.import_file(bindings[key], 'input')
        self._parameters(definition, bindings, allow_missing_rule=True)
        if definition['task'] in getattr(self.registry, 'parameter_forms', lambda: {})():
            from .parameter_forms import validate_form
            initial = validate_form(definition['task'], bindings, {}, parameter_policy)
            if not initial['passed']:
                raise SopError('invalid_parameter', canonical(initial['diagnostics']))
        run_id, root_id = identity('run'), identity('task')
        ref = self.store.put_json(definition)
        task = self._task(root_id, definition['task'], bindings, None, 0)
        task.update(definition=ref, accepted={'report': report, 'origin': 'authored' if authoring else 'explicit', 'selection': []})
        run = {'schema': 'run/1', 'id': run_id, 'root': root_id, 'state': 'ready', 'tasks': {root_id: task},
               'calls': {}, 'questions': {}, 'answers': [], 'operations': {}, 'capabilities': sorted(set(capabilities)),
               'limits': limits, 'parameter_policy': parameter_policy, 'usage': {'actions': 0, 'model_calls': 0, 'model_usage': []},
               'definitions': {ref['hash']: ref}, 'recoveries': [], 'authoring': authoring,
               'publication': 'unpublished', 'created': time.time()}
        self.store.create(run)
        return run

    @staticmethod
    def _task(task_id, task, inputs, parent, depth):
        return {'id': task_id, 'task': task, 'inputs': inputs, 'parent_call': parent, 'depth': depth,
                'state': 'ready', 'definition': None, 'accepted': None, 'pc': 0, 'steps': {},
                'outputs': {}, 'agent': {}, 'recovery_counts': {}, 'verification': None,
                'parameter_revision': 0, 'parameter_history': []}

    def _definition(self, task):
        return self.store.json(task['definition'])

    def _validate_saved(self, run):
        for task in run['tasks'].values():
            for value in task['inputs'].values():
                if artifact(value):
                    self.store.path(value)
            for outputs in [task['outputs'], *task['steps'].values()]:
                for value in outputs.values():
                    if artifact(value):
                        self.store.path(value)
            if task['definition']:
                report = self._check_definition(self._definition(task), run['capabilities'], task['task'])
                saved = task['accepted']['report']
                for field in ('definition_hash', 'dependency_hashes', 'checker_versions'):
                    if report.get(field) != saved.get(field):
                        raise SopError('dependency_drift', 'accepted definition/checker implementation changed')
        for operation in run['operations'].values():
            for value in operation.get('outputs', {}).values():
                if artifact(value):
                    self.store.path(value)

    def _budget(self, run, kind, amount=1):
        field, limit = ('model_calls', 'max_model_calls') if kind == 'model' else ('actions', 'max_actions')
        if run['usage'][field] + amount > run['limits'][limit]:
            raise SopError('budget_exhausted', field)
        run['usage'][field] += amount

    def _resolve(self, value, task):
        if isinstance(value, str) and value.startswith('$input.'):
            return deepcopy(task['inputs'].get(value[7:]))
        if isinstance(value, str) and value.startswith('$steps.'):
            parts = value.split('.')
            try:
                return deepcopy(task['steps'][parts[1]][parts[2]])
            except (KeyError, IndexError):
                raise SopError('binding_missing', value)
        return deepcopy(value)

    def _bindings(self, step, task):
        return {k: self._resolve(v, task) for k, v in step['inputs'].items()}

    def _paths(self, inputs):
        return {k: self.store.path(v) if artifact(v) else v for k, v in inputs.items()}

    def _fail(self, run, task, code, message, *, state='failed'):
        task.update(state=state, error={'code': code, 'message': str(message)})
        run['state'] = state
        run['error'] = {'task': task['id'], 'code': code, 'message': str(message)}
        self._save(run, 'task_stopped', run['error'])

    def _question(self, run, task, field, reason, step):
        if field != 'rule':
            raise SopError('unsupported_question', 'only declared rule choice can be filled by runtime answer')
        if task['inputs'].get(field) is not None:
            raise SopError('already_known', 'agent asked for an already bound fact')
        question = {'request_id': identity('question'), 'owner': task['id'], 'field': field,
                    'reason': reason, 'schema': {'type': 'string', 'enum': self._rule_options(run, task)},
                    'state': 'open', 'continuation': {'pc': task['pc'], 'step': step['id']},
                    'revision': run['revision'] + 1, 'source': 'runtime', 'scope': run['id']}
        run['questions'][question['request_id']] = question
        task['state'] = 'waiting_info'
        run['state'] = 'waiting_info'
        task['agent']['token'] = None
        self._save(run, 'waiting_info', question)

    def _rule_options(self, run, task):
        options = {'highest_revision', 'first'}
        current = task
        while current:
            if current['definition']:
                definition = self._definition(current)
                options &= set(definition['params'].get('rule', {}).get('enum', options))
                options &= set(definition.get('supported_rules', options))
            if current['inputs'].get('rule') is not None:
                options &= {current['inputs']['rule']}
            call = run['calls'].get(current.get('parent_call'))
            current = run['tasks'][call['parent']] if call and 'rule' in call.get('fact_bindings', {}) else None
        return sorted(options)

    def answer(self, run_id, request_id, value, *, message_id, revision, owner=None):
        if not isinstance(message_id, str) or not message_id or type(revision) is not int:
            raise SopError('invalid_message', 'message ID and integer revision required')
        payload = {'request_id': request_id, 'value': value, 'revision': revision, 'owner': owner}
        with self.store.lock(run_id):
            old = self.store.receipt(run_id, message_id, payload)
            if old is not None:
                return old
            run = self.store.load(run_id)
            if run['revision'] != revision:
                raise SopError('stale_revision', 'answer targets an old run revision')
            question = run['questions'].get(request_id)
            if not question or question['state'] != 'open' or run['state'] in TERMINAL:
                raise SopError('stale_question', 'question closed, missing, or run stopped')
            if question['field'] != 'rule':
                raise SopError('unsupported_question', 'parameter-form questions require propose_parameters')
            if owner is not None and question['owner'] != owner:
                raise SopError('wrong_scope', 'answer targets another instance')
            if not isinstance(value, str) or value not in question['schema']['enum']:
                raise SopError('invalid_answer', 'rule must be highest_revision or first')
            task = run['tasks'][question['owner']]
            inputs = dict(task['inputs'], **{question['field']: value})
            if task['definition']:
                self._parameters(self._definition(task), inputs, allow_missing_rule=True)
            binding = {'request_id': request_id, 'owner': task['id'], 'field': question['field'],
                       'value': value, 'source': 'answer', 'message_id': message_id, 'revision': revision + 1}
            task['inputs'] = inputs
            task['state'] = 'ready'
            task['agent']['continuation'] = 'answer:' + request_id
            question['state'] = 'answered'
            run['answers'].append(binding)
            run['state'] = 'ready'
            response = {'accepted': True, 'revision': revision + 1, 'binding': binding}
            self._save(run, 'answer_accepted', binding, (message_id, payload, response))
            return response

    def _form(self, task):
        return getattr(self.registry, 'parameter_forms', lambda: {})().get(task['task'])

    def _wait_parameters(self, run, task, missing, allowed_fields=None):
        form = self._form(task)
        question = next((q for q in run['questions'].values() if q['owner']==task['id'] and
                         q['field']=='parameters' and q['state']=='open'), None)
        if question is None:
            question = {'request_id':identity('question'), 'owner':task['id'], 'field':'parameters',
                        'state':'open', 'source':'parameter_form', 'scope':run['id'],
                        'schema':deepcopy(form['fields']), 'form_version':form['version']}
            run['questions'][question['request_id']] = question
        from .capabilities import SOURCE_COLUMNS
        question['constraints'] = {'column_map': {name:name for name in SOURCE_COLUMNS},
                                   'chunk_size': {'type':'integer','minimum':1,
                                                  'maximum':run['parameter_policy']['max_chunk_size']}}
        if task['definition']:
            chunk_schema = self._definition(task)['params'].get('chunk_size', {})
            if 'enum' in chunk_schema:
                question['constraints']['chunk_size']['enum'] = [value for value in chunk_schema['enum']
                    if 1 <= value <= run['parameter_policy']['max_chunk_size']]
        question.update(missing=list(missing), allowed_fields=list(allowed_fields or form['fields']),
                        parameter_revision=task['parameter_revision'], revision=run['revision']+1,
                        reason='Provide declared data fields; source, implementation and limits are immutable')
        task['state'], run['state'] = 'waiting_parameters', 'waiting_info'
        task['agent']['token'] = None
        return question

    def _ensure_parameters(self, run, task):
        if not self._form(task):
            return True
        from .parameter_forms import validate_form
        check = validate_form(task['task'], task['inputs'], {}, run['parameter_policy'])
        if not check['passed']:
            raise SopError('invalid_parameter', canonical(check['diagnostics']))
        if task['definition']:
            self._parameters(self._definition(task), task['inputs'], allow_missing_rule=True)
        if check['missing']:
            question = self._wait_parameters(run, task, check['missing'])
            self._save(run, 'waiting_parameters', question)
            return False
        return True

    def propose_parameters(self, run_id, task_id, patch, *, message_id, revision):
        """Accept data only, under the same run lock used to start effects."""
        if not isinstance(message_id, str) or not message_id or type(revision) is not int:
            raise SopError('invalid_message', 'message identity and parameter revision required')
        payload = {'kind':'parameter_proposal', 'task_id':task_id, 'patch':patch, 'revision':revision}
        canonical(payload)
        with self.store.lock(run_id):
            old = self.store.receipt(run_id, message_id, payload)
            if old is not None:
                return old
            run = self.store.load(run_id)
            self._validate_saved(run)
            if self._apply_cancel(run) or run['state'] in TERMINAL:
                raise SopError('task_stopped', 'stopped run cannot accept parameters')
            task = run['tasks'].get(task_id)
            if task is None or not self._form(task):
                raise SopError('wrong_scope', 'target task has no declared parameter form')
            if revision != task['parameter_revision']:
                raise SopError('stale_revision', 'parameter proposal targets an old binding revision')
            if (task['state'] not in ('ready','waiting_parameters') or task['pc'] != 0 or
                    any(op['task']==task_id for op in run['operations'].values())):
                raise SopError('parameters_locked', 'parameters cannot change after operation start')
            from .parameter_forms import validate_form
            report = validate_form(task['task'], task['inputs'], patch, run['parameter_policy'],
                                   allowed_fields=task.get('parameter_correction_fields'))
            if report['passed'] and task['definition']:
                definition = self._definition(task)
                try:
                    self._parameters(definition, report['bindings'], allow_missing_rule=True)
                except SopError as exc:
                    fields = [name for name, schema in definition['params'].items() if 'enum' in schema
                              and report['bindings'].get(name) is not None
                              and report['bindings'][name] not in schema['enum']]
                    report.update(passed=False, bindings=deepcopy(task['inputs']),
                                  missing=validate_form(task['task'], task['inputs'], {}, run['parameter_policy'])['missing'],
                                  invalid_fields=fields or list(patch),
                                  diagnostics=[{'code':'invalid_value','field':name,'message':str(exc)} for name in (fields or list(patch))])
            try:
                self._budget(run, 'action')
            except SopError as exc:
                self._fail(run, task, exc.code, str(exc), state='budget_exhausted')
                raise
            if report['passed']:
                task['inputs'] = deepcopy(report['bindings'])
                task['parameter_revision'] += 1
                task['parameter_history'].append({'revision':task['parameter_revision'],
                    'message_id':message_id, 'bindings':deepcopy(task['inputs'])})
                correction_fields = task.get('parameter_correction_fields', [])
                pending_correction = [field for field in correction_fields if field not in patch]
                if not pending_correction:
                    task.pop('parameter_correction_fields', None)
                else:
                    report['missing'] = sorted(set(report['missing']) | set(pending_correction))
                if report['missing']:
                    self._wait_parameters(run, task, report['missing'], task.get('parameter_correction_fields'))
                else:
                    for question in run['questions'].values():
                        if question['owner']==task_id and question['field']=='parameters' and question['state']=='open':
                            question['state']='answered'
                    task['state'], run['state'] = 'ready', 'ready'
            else:
                invalid = set(report['invalid_fields'])
                codes = {d['code'] for d in report['diagnostics']}
                # A rejected extra-field envelope never grants a recovery action.
                if 'invalid_value' in codes and not (codes & {'unknown_field','forbidden_field','field_not_editable'}):
                    definition = self._definition(task) if task['definition'] else None
                    rules = [r for r in (definition or {}).get('recovery', []) if r['phase']=='parameter_validation'
                             and r['code']=='invalid_value' and r['action']=='request_parameter_proposal'
                             and invalid <= set(r['fields'])]
                    rule = rules[0] if len(rules)==1 else None
                    count = task['recovery_counts'].get(rule['id'],0) if rule else 0
                    if rule and count < rule['max_attempts']:
                        try:
                            self._budget(run, 'action')
                        except SopError as exc:
                            self._fail(run, task, exc.code, str(exc), state='budget_exhausted')
                            raise
                        task['recovery_counts'][rule['id']] = count+1
                        decision = {'failure_id':identity('failure'), 'task':task_id, 'phase':'parameter_validation',
                            'code':'invalid_value', 'evidence_from':'form_validator', 'execution_started':False,
                            'rule':deepcopy(rule), 'count':count+1, 'bindings_hash':digest(task['inputs']),
                            'definition':task['definition']['hash'], 'parameter_revision':task['parameter_revision']}
                        run['recoveries'].append(decision)
                        task['parameter_correction_fields'] = rule['fields'][:]
                        self._wait_parameters(run, task, report['missing'], rule['fields'])
                    else:
                        task['state'], run['state'] = 'failed','failed'
                        run['error'] = task['error'] = {'code':'parameter_recovery_exhausted' if rule else 'invalid_value',
                                                       'message':'No available declared parameter correction'}
            response = {'accepted':report['passed'], 'parameter_revision':task['parameter_revision'],
                        'missing':report['missing'], 'diagnostics':report['diagnostics'], 'run_revision':run['revision']+1}
            self._save(run, 'parameters_accepted' if report['passed'] else 'parameters_rejected',
                       {'task':task_id, 'response':response}, (message_id,payload,response))
            return response

    def _apply_cancel(self, run):
        if not self.store.cancellation_requested(run['id']):
            return False
        run['state'] = 'cancelled'
        for task in run['tasks'].values():
            if task['state'] not in TERMINAL:
                task['state'] = 'cancelled'
                task['agent']['token'] = None
        self._save(run, 'cancelled', {'effects_reverted': False})
        return True

    def cancel(self, run_id):
        run = self.store.load(run_id)
        if run['state'] in TERMINAL:
            return run
        self.store.request_cancel(run_id)
        try:
            with self.store.lock(run_id):
                run = self.store.load(run_id)
                if run['state'] not in TERMINAL:
                    self._apply_cancel(run)
                return run
        except SopError as exc:
            if exc.code != 'run_busy':
                raise
            return {'id': run_id, 'state': 'cancellation_requested', 'effects_reverted': False}

    def _recover(self, run, task, phase, code, message, *, not_dispatched=False, operation=None):
        definition = self._definition(task)
        failure = {'failure_id': identity('failure'), 'task': task['id'], 'phase': phase, 'code': code,
                   'message': str(message), 'not_dispatched': not_dispatched, 'operation': operation}
        matches = [r for r in definition.get('recovery', []) if r['phase'] == phase and r['code'] == code]
        if len(matches) > 1:
            self._fail(run, task, 'recovery_conflict', 'multiple recovery branches matched')
            return
        rule = matches[0] if matches else None
        if rule:
            count = task['recovery_counts'].get(rule['id'], 0)
            safe = (rule['action'] == 'retry' and phase == 'execution' and not_dispatched) or (
                rule['action'] == 'recheck' and phase == 'verification' and code != 'verification_failed')
            if safe and count < rule['max_attempts']:
                self._budget(run, 'action')
                task['recovery_counts'][rule['id']] = count + 1
                decision = dict(failure, rule=deepcopy(rule), count=count + 1,
                                bindings_hash=digest(task['inputs']), definition=task['definition']['hash'])
                run['recoveries'].append(decision)
                task['state'] = ('committing_step' if task.get('operation') and run['operations'][task['operation']]['status']=='candidate' else ('verifying_agent' if task['agent'].get('candidate') else 'verifying')) if rule['action'] == 'recheck' else 'ready'
                if rule['action'] == 'retry':
                    if operation and run['operations'][operation].get('origin') == 'agent':
                        task['state'] = 'retrying_agent_tool'
                        task['tool_retry'] = operation
                    task['operation'] = None
                self._save(run, 'recovery_reserved', decision)
                return
        task['failure'] = failure
        self._fail(run, task, code, message)

    def _accept_method(self, run, task):
        if 'rule' in self.registry.task_contracts()[task['task']]['inputs'] and task['inputs'].get('rule') is None:
            self._question(run, task, 'rule', 'method selection requires an explicit business rule', {'id': 'method'})
            return
        # Resolver receives only this instance's scoped inputs and one budgeted planning callback.
        self._budget(run, 'model' if getattr(self.resolver, 'requires_model', False) else 'action')
        self._save(run, 'method_preparation', {'task': task['id']})
        proposal = self.resolver(task['task'], deepcopy(task['inputs']), run)
        definition = proposal['definition']
        report = self._check_definition(definition, run['capabilities'], task['task'])
        self._parameters(definition, task['inputs'], allow_missing_rule=True)
        ref = self.store.put_json(definition)
        run['definitions'][ref['hash']] = ref
        task['definition'] = ref
        task['accepted'] = {'report': report, 'origin': proposal['origin'], 'selection': proposal.get('selection', [])}
        task['state'] = 'ready'
        self._save(run, 'method_accepted', {'task': task['id'], 'definition': ref, **task['accepted']})

    def _child(self, run, parent, step, task_name, inputs, *, definition_hash=None, reason='declared call'):
        expected = self._bindings(step, parent)
        if task_name != step['task'] or inputs != expected:
            raise SopError('parent_contract', 'child must preserve requested task and exact input bindings')
        if parent['depth'] + 1 > run['limits']['max_depth']:
            raise SopError('budget_exhausted', 'max_depth')
        signature = digest({'task': task_name, 'inputs': inputs, 'definition': definition_hash})
        ancestor = parent
        while ancestor:
            if ancestor.get('call_signature') == signature:
                raise SopError('stagnation', 'equivalent ancestor task repeated without progress')
            call = run['calls'].get(ancestor.get('parent_call'))
            ancestor = run['tasks'][call['parent']] if call else None
        self._budget(run, 'action')
        call_id, child_id = identity('call'), identity('task')
        child = self._task(child_id, task_name, deepcopy(inputs), call_id, parent['depth'] + 1)
        child['call_signature'] = signature
        if definition_hash:
            ref = run['definitions'].get(definition_hash)
            if not ref:
                # Exact library lookup is the resolver's explicit-ref path.
                proposal = self.resolver(task_name, deepcopy(inputs), run, definition_hash=definition_hash)
                definition = proposal['definition']
                if digest(definition) != definition_hash:
                    raise SopError('definition_identity', 'explicit definition does not match hash')
                ref = self.store.put_json(definition)
                run['definitions'][ref['hash']] = ref
            definition = self.store.json(ref)
            report = self._check_definition(definition, run['capabilities'], task_name)
            self._parameters(definition, inputs, allow_missing_rule=True)
            child['definition'] = ref
            child['accepted'] = {'report': report, 'origin': 'candidate_reuse', 'selection': []}
        run['tasks'][child_id] = child
        run['calls'][call_id] = {'id': call_id, 'parent': parent['id'], 'child': child_id,
                                'step': step['id'], 'pc': parent['pc'], 'kind': step['kind'],
                                'consumed': False, 'reason': reason,
                                'fact_bindings': {'rule': 'rule'} if step['inputs'].get('rule') == '$input.rule' else {},
                                'required_outputs': sorted(self._outputs(task_name))}
        parent['state'] = 'waiting_child'
        parent['agent']['token'] = None
        self._save(run, 'child_created', run['calls'][call_id])

    def _return_child(self, run, child):
        call = run['calls'][child['parent_call']]
        if call['consumed']:
            return
        parent = run['tasks'][call['parent']]
        if parent['state'] != 'waiting_child' or parent['pc'] != call['pc']:
            raise SopError('stale_child_result', 'parent no longer awaits this call')
        if not set(call['required_outputs']) <= set(child['outputs']):
            raise SopError('parent_contract', 'missing child output')
        # Only a declared inherited unknown rule is mapped back for parent whole-result verification.
        if ('rule' in call.get('fact_bindings', {}) and parent['inputs'].get('rule') is None
                and child['inputs'].get('rule') is not None):
            self._parameters(self._definition(parent), dict(parent['inputs'], rule=child['inputs']['rule']), allow_missing_rule=True)
            parent['inputs']['rule'] = child['inputs']['rule']
            run['answers'].append({'owner': parent['id'], 'field': 'rule', 'value': child['inputs']['rule'],
                                   'source': 'child_return', 'call_id': call['id']})
        if call['kind'] == 'pi':
            parent['agent'].update(child_result=deepcopy(child['outputs']), continuation='child:' + call['id'])
        else:
            parent['steps'][call['step']] = deepcopy(child['outputs'])
            parent['pc'] += 1
        parent['state'] = 'ready'
        call['consumed'] = True
        self._save(run, 'child_returned', {'call_id': call['id'], 'outputs': child['outputs']})

    def _fixed(self, run, task, step, *, supplied_inputs=None, origin='fixed'):
        inputs = self._bindings(step, task) if supplied_inputs is None else deepcopy(supplied_inputs)
        if 'rule' in inputs and inputs['rule'] is None:
            self._question(run, task, 'rule', 'fixed operation requires an explicit rule', step)
            return
        if step['capability'] not in run['capabilities']:
            raise SopError('permission_denied', 'capability not granted')
        self._budget(run, 'action')
        operation_id = identity('op')
        operation = {'id': operation_id, 'task': task['id'], 'step': step['id'], 'capability': step['capability'],
                     'inputs': inputs, 'status': 'intent', 'definition': task['definition']['hash'], 'origin': origin,
                     'parameter_revision':task.get('parameter_revision',0)}
        run['operations'][operation_id] = operation
        task.update(state='running', operation=operation_id)
        self._save(run, 'operation_intent', operation)
        if self.store.cancellation_requested(run['id']):
            operation['status'] = 'not_dispatched'
            self._apply_cancel(run)
            return
        work = self.store.root / 'work' / operation_id
        work.mkdir()
        try:
            outputs = self.registry.execute(step['capability'], self._paths(inputs), work)
        except ExecutionError as exc:
            operation['status'] = 'not_dispatched' if exc.not_dispatched else 'failed'
            self._recover(run, task, 'execution', exc.code, str(exc), not_dispatched=exc.not_dispatched, operation=operation_id)
            return
        except SopError as exc:
            operation['status'] = 'failed'
            self._recover(run, task, 'execution', exc.code, str(exc), operation=operation_id)
            return
        # Any non-protocol exception after dispatch leaves intent intact for conservative recovery.
        allowed = set(self.registry.catalog()[step['capability']]['outputs'])
        if not isinstance(outputs, dict) or set(outputs) != allowed:
            raise SopError('invalid_output', 'fixed capability output shape differs from declaration')
        refs = {}
        for name, path in outputs.items():
            path = Path(path).absolute()
            if not path.is_relative_to(work) or path.resolve() != path:
                raise SopError('unsafe_output', 'adapter output outside operation directory')
            refs[name] = self.store.import_file(path)
        operation.update(status='candidate', outputs=refs)
        task['state'] = 'committing_step'
        self._save(run, 'operation_candidate', {'operation': operation_id, 'outputs': refs})

    def _commit_fixed(self, run, task):
        operation = run['operations'][task['operation']]
        checker = self.registry.catalog()[operation['capability']].get('result_check')
        if checker:
            self._budget(run, 'action')
            self._save(run, 'capability_verification_started', {'operation':operation['id'],'checker':checker})
            try:
                report = self.registry.verify(checker,self._paths(operation['inputs']),self._paths(operation['outputs']))
            except (SopError,OSError,TimeoutError) as exc:
                self._recover(run,task,'verification',getattr(exc,'code','check_incomplete'),str(exc),operation=operation['id'])
                return
            operation['verification']=report
            if report.get('passed') is not True:
                self._recover(run,task,'verification','verification_failed',canonical(report),operation=operation['id'])
                return
        if operation.get('origin') == 'agent':
            task.pop('tool_retry', None)
            task['agent'].setdefault('tool_receipts', []).append({
                'operation': operation['id'], 'capability': operation['capability'],
                'inputs': deepcopy(operation['inputs']), 'outputs': deepcopy(operation['outputs'])})
        else:
            task['steps'][operation['step']] = deepcopy(operation['outputs'])
            task['pc'] += 1
        task['state'] = 'ready'
        operation['status'] = 'committed'
        task['operation'] = None
        self._save(run, 'operation_committed', {'operation': operation['id']})

    def _agent_grant(self, run, task, step):
        return sorted(set(run['capabilities']) & set(self._definition(task)['capabilities']) &
                      set(self.registry.task_contracts()[step['task']]['capabilities']))

    def _agent_tool(self, run, task, step, token, capability, inputs):
        try:
            return self._dispatch_agent_tool(run, task, step, token, capability, inputs)
        except SopError as exc:
            task['agent']['token'] = None
            task['agent'].setdefault('tool_failure', {'code': exc.code, 'message': str(exc)})
            self._save(run, 'agent_tool_rejected', {'task': task['id'], 'code': exc.code})
            raise

    def _dispatch_agent_tool(self, run, task, step, token, capability, inputs):
        """A synchronous host gateway, never exposed to the model as Python state."""
        session = task['agent']
        if session.get('token') != token or task['state'] != 'ready' or run['state'] in TERMINAL:
            raise SopError('stale_token', 'Pi tool session is no longer active')
        if self._apply_cancel(run):
            raise SopError('cancelled', 'run cancellation stops tool dispatch')
        grant = self._agent_grant(run, task, step)
        if not isinstance(capability, str) or capability not in grant:
            raise SopError('permission_denied', 'tool is outside the accepted task scope')
        signature = self.registry.catalog()[capability]
        if not isinstance(inputs, dict) or set(inputs) != set(signature['inputs']):
            raise SopError('invalid_parameter', 'tool inputs differ from its declared signature')
        canonical(inputs)
        bound = self._bindings(step, task)
        contract = self.registry.task_contracts()[step['task']]
        owned = []
        for name, value in bound.items():
            if artifact(value):
                owned.append((value, contract['inputs'][name].get('role', name)))
        for name, value in (session.get('child_result') or {}).items():
            owned.append((value, contract.get('output_roles', {}).get(name, name)))
        for receipt in session.get('tool_receipts', []):
            producer = self.registry.catalog()[receipt['capability']]
            for name, value in receipt['outputs'].items():
                owned.append((value, producer.get('output_roles', {}).get(name, name)))
        for name, schema in signature['inputs'].items():
            value = inputs[name]
            if schema['type'] == 'artifact':
                role = schema.get('role', name)
                if not artifact(value) or (value, role) not in owned:
                    raise SopError('invalid_artifact', 'tool artifact is not owned with the required role')
                self.store.path(value)
            elif name == 'rule':
                if not bound.get('rule') or value != bound['rule'] or value not in schema['enum']:
                    raise SopError('invalid_rule', 'tool cannot replace or guess the bound rule')
            elif name not in bound or value != bound[name]:
                raise SopError('invalid_parameter', 'tool cannot change bound structured parameters')
        for receipt in session.get('tool_receipts', []):
            if receipt['capability'] == capability and receipt['inputs'] == inputs:
                return {'operation': receipt['operation'], 'outputs': deepcopy(receipt['outputs']), 'reused': True}
        self._fixed(run, task, {'id': step['id'], 'capability': capability},
                    supplied_inputs=inputs, origin='agent')
        if task['state'] == 'committing_step':
            self._commit_fixed(run, task)
        # Declared recovery resumes in B2, never inside this model call.
        if task['state'] != 'ready' or task.get('operation'):
            raise SopError('tool_interrupted', 'tool did not commit an accepted result')
        receipts = session.get('tool_receipts', [])
        if not receipts or receipts[-1]['capability'] != capability or receipts[-1]['inputs'] != inputs:
            raise SopError('tool_interrupted', 'tool execution requires controller recovery')
        receipt = receipts[-1]
        if self._apply_cancel(run):
            raise SopError('cancelled', 'run cancelled after committed tool result')
        return {'operation': receipt['operation'], 'outputs': deepcopy(receipt['outputs']), 'reused': False}

    def _agent_step(self, run, task, step):
        if self.agent is None:
            raise SopError('backend_unavailable', 'no agent runtime configured')
        self._budget(run, 'model', getattr(self.agent, 'max_model_calls_per_request', 1))
        session = task['agent']
        if session.get('step') != step['id']:
            task['agent'] = session = {'step': step['id'], 'continuation': 'start', 'child_result': None}
        token = identity('token')
        session['token'] = token
        session.pop('tool_failure', None)
        context = {'task': step['task'], 'inputs': self._bindings(step, task),
                   'facts': [deepcopy(a) for a in run['answers'] if a['owner'] == task['id']],
                   'child_result': session.get('child_result'), 'continuation': session.get('continuation'),
                   'contract': {'task': step['task'], 'required_outputs': sorted(self._outputs(step['task'])),
                                'checks': [step['task']], 'capabilities': self._agent_grant(run, task, step),
                                'forbidden': ['modify_inputs', 'change_scripts', 'change_checks', 'publish', 'external_effects']},
                   'information_query': 'context_only',
                   'catalog': {name: self.registry.catalog()[name] for name in self._agent_grant(run, task, step)},
                   'tool_receipts': deepcopy(session.get('tool_receipts', [])), 'limits': run['limits']}
        session['context_hash'] = digest(context)
        self._save(run, 'agent_dispatched', {'task': task['id'], 'step': step['id'], 'token': token, 'context': context})
        try:
            response = (self.agent.respond_with_tools(context, lambda cap, args: self._agent_tool(run, task, step, token, cap, args))
                        if hasattr(self.agent, 'respond_with_tools') else self.agent.respond(context))
        except SopError as exc:
            evidence = getattr(self.agent, 'last_evidence', None)
            if evidence:
                run['usage']['model_usage'].append(evidence)
            failure = session.get('tool_failure', {'code':exc.code, 'message':str(exc)})
            if run['state'] in TERMINAL or failure['code'] == 'tool_interrupted':
                session['token'] = None
                self._save(run, 'agent_tool_session_stopped', {'task': task['id'], 'code': exc.code})
                return
            raise SopError(failure['code'], failure['message']) from exc
        if session.get('tool_failure'):
            failure = session['tool_failure']
            if run['state'] in TERMINAL or failure['code'] == 'tool_interrupted':
                return
            raise SopError(failure['code'], failure['message'])
        if not isinstance(response, dict):
            raise SopError('invalid_response', 'agent must return an object')
        canonical(response)
        if self.store.cancellation_requested(run['id']):
            session['late_response'] = response
            self._apply_cancel(run)
            return
        usage = response.pop('_usage', None)
        trace = response.pop('_trace', None)
        if usage is not None:
            run['usage']['model_usage'].append(usage)
        if trace is not None:
            session['trace'] = trace
        # This synchronous worker cannot commit state or keep a valid token after handoff.
        if session['token'] != token:
            raise SopError('stale_token', 'execution token revoked')
        session['token'] = None
        kind = response.get('kind')
        allowed = {
            'need_info': {'kind', 'field', 'reason'},
            'need_subtask': {'kind', 'task', 'inputs', 'reason', 'definition'},
            'candidate_result': {'kind', 'outputs'},
            'cannot_continue': {'kind', 'reason'},
        }
        if not isinstance(kind, str) or kind not in allowed or set(response) - allowed[kind]:
            raise SopError('invalid_response', 'unknown handoff fields or kind')
        for field in ('task', 'reason', 'field'):
            if field in response and not isinstance(response[field], str):
                raise SopError('invalid_response', field + ' must be a string')
        if kind == 'need_subtask':
            if not isinstance(response.get('inputs'), dict):
                raise SopError('invalid_response', 'subtask inputs must be an object')
            if 'definition' in response:
                ref = response['definition']
                if not isinstance(ref, str) or len(ref) != 64 or any(c not in '0123456789abcdef' for c in ref):
                    raise SopError('invalid_response', 'definition must be a content digest')
        session['last_response'] = response
        if kind == 'need_info':
            if response.get('field') in task['inputs'] and task['inputs'][response['field']] is not None:
                field = response['field']
                consumed = session.setdefault('information_consumed', [])
                if field in consumed:
                    raise SopError('stagnation', 'same known fact repeatedly requested without progress')
                consumed.append(field)
                session['continuation'] = 'known_fact:' + field
                self._save(run, 'information_satisfied', {'task': task['id'], 'field': field, 'source': 'bound_input'})
                return
            self._question(run, task, response.get('field'), response.get('reason', ''), step)
        elif kind == 'need_subtask':
            self._child(run, task, step, response.get('task'), response.get('inputs'),
                        definition_hash=response.get('definition'), reason=response.get('reason', ''))
        elif kind == 'candidate_result':
            outputs = response.get('outputs')
            if not isinstance(outputs, dict) or not self._outputs(step['task']) <= set(outputs):
                raise SopError('invalid_candidate', 'required output missing')
            permitted = list(self._bindings(step, task).values()) + list((session.get('child_result') or {}).values())
            permitted += [value for receipt in session.get('tool_receipts', []) for value in receipt['outputs'].values()]
            for value in outputs.values():
                if not artifact(value) or value not in permitted:
                    raise SopError('invalid_candidate', 'candidate is not an owned artifact')
                self.store.path(value)
            session['candidate'] = deepcopy(outputs)
            session['candidate_task'] = step['task']
            session['candidate_inputs'] = self._bindings(step, task)
            task['state'] = 'verifying_agent'
            self._save(run, 'agent_candidate', {'task': task['id'], 'step': step['id'], 'outputs': outputs})
        else:
            self._fail(run, task, 'cannot_continue', response.get('reason', 'unspecified'))

    def _verify_agent(self, run, task):
        session = task['agent']
        self._budget(run, 'action')
        self._save(run, 'agent_verification_started', {'task': task['id']})
        try:
            report = self.registry.verify(session['candidate_task'], self._paths(session['candidate_inputs']),
                                          self._paths(session['candidate']))
        except (SopError, OSError, TimeoutError) as exc:
            self._recover(run, task, 'verification', getattr(exc, 'code', 'check_incomplete'), str(exc))
            if task['state'] == 'verifying':
                task['state'] = 'verifying_agent'
                self._save(run, 'agent_recheck_ready', {'task': task['id']})
            return
        if not report.get('passed'):
            task['verification'] = report
            self._recover(run, task, 'verification', 'verification_failed', canonical(report))
            return
        outputs = session['candidate']
        task['steps'][session['step']] = deepcopy(outputs)
        task['pc'] += 1
        task['agent'] = {}
        task['state'] = 'ready'
        self._save(run, 'agent_result_accepted', {'task': task['id'], 'report': report, 'outputs': outputs})

    def _verify(self, run, task):
        self._budget(run, 'action')
        self._save(run, 'verification_started', {'task': task['id'], 'outputs': task['outputs']})
        try:
            reports = []
            for checker in self._definition(task)['checks']:
                reports.append(self.registry.verify(checker, self._paths(task['inputs']), self._paths(task['outputs'])))
        except (SopError, OSError, TimeoutError) as exc:
            self._recover(run, task, 'verification', getattr(exc, 'code', 'check_incomplete'), str(exc))
            return
        task['verification'] = {'passed': all(r.get('passed') is True for r in reports), 'reports': reports}
        if not task['verification']['passed']:
            self._recover(run, task, 'verification', 'verification_failed', canonical(task['verification']))
            return
        task['state'] = 'succeeded'
        self._save(run, 'task_succeeded', {'task': task['id'], 'outputs': task['outputs'], 'verification': task['verification']})

    def advance(self, run_id, *, max_transitions=None):
        with self.store.lock(run_id):
            run = self.store.load(run_id)
            if run['state'] in TERMINAL:
                return run
            self._validate_saved(run)
            # A persisted intent says nothing about dispatch; never infer safety from absent files.
            for task in run['tasks'].values():
                if task['state'] == 'running':
                    self._fail(run, task, 'effect_unknown', 'operation intent has no committed candidate', state='effect_unknown')
                    return run
            transitions = 0
            while run['state'] not in TERMINAL:
                if max_transitions is not None and transitions >= max_transitions:
                    break
                transitions += 1
                if self._apply_cancel(run):
                    break
                # Child completion and parent consumption are separate commits, each restartable.
                returned = False
                for child in list(run['tasks'].values()):
                    if child['state'] == 'succeeded' and child['parent_call'] and not run['calls'][child['parent_call']]['consumed']:
                        self._return_child(run, child)
                        returned = True
                        break
                if returned:
                    continue
                root = run['tasks'][run['root']]
                if root['state'] == 'succeeded':
                    run['state'] = 'succeeded'
                    self._save(run, 'run_succeeded', {'outputs': root['outputs']})
                    break
                runnable = [t for t in run['tasks'].values() if t['state'] in {'ready', 'verifying', 'verifying_agent', 'committing_step', 'retrying_agent_tool'}]
                if not runnable:
                    run['state'] = 'waiting_info' if any(t['state'] in ('waiting_info','waiting_parameters') for t in run['tasks'].values()) else 'waiting_child'
                    break
                task = max(runnable, key=lambda t: t['depth'])
                try:
                    if task['state']=='ready' and not self._ensure_parameters(run, task):
                        continue
                    if task['definition'] is None:
                        self._accept_method(run, task)
                        continue
                    if task['state'] == 'retrying_agent_tool':
                        original = run['operations'][task['tool_retry']]
                        self._fixed(run, task, {'id': original['step'], 'capability': original['capability']},
                                    supplied_inputs=original['inputs'], origin='agent')
                        continue
                    if task['state'] == 'committing_step':
                        self._commit_fixed(run, task)
                        continue
                    if task['state'] == 'verifying_agent':
                        self._verify_agent(run, task)
                        continue
                    if task['state'] == 'verifying':
                        self._verify(run, task)
                        continue
                    definition = self._definition(task)
                    if task['pc'] >= len(definition['steps']):
                        task['outputs'] = {k: self._resolve(v, task) for k, v in definition['outputs'].items()}
                        task['state'] = 'verifying'
                        self._save(run, 'task_candidate', {'task': task['id'], 'outputs': task['outputs']})
                        continue
                    step = definition['steps'][task['pc']]
                    if step['kind'] == 'fixed':
                        self._fixed(run, task, step)
                    elif step['kind'] == 'pi':
                        self._agent_step(run, task, step)
                    elif step['kind'] == 'call':
                        self._child(run, task, step, step['task'], self._bindings(step, task), definition_hash=step.get('definition'))
                    else:
                        raise SopError('invalid_step', 'unknown execution mode')
                except SopError as exc:
                    # If a tool already has an unresolved intent, its effect uncertainty dominates errors.
                    operation = run['operations'].get(task.get('operation'))
                    if operation and operation['status'] == 'intent':
                        self._fail(run, task, 'effect_unknown', str(exc), state='effect_unknown')
                    else:
                        self._fail(run, task, exc.code, str(exc), state='budget_exhausted' if exc.code == 'budget_exhausted' else 'failed')
                    break
            return run
