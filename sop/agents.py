"""D04 protocol fixtures and real, restricted Pi adapters.

Scripted* objects are deterministic test doubles, never model validation evidence.
Pi exposes logical context, a handoff and optional host-mediated capabilities, never native tools.
"""
import json
import os
import subprocess
from pathlib import Path

from .common import SopError, canonical


def _strict_loads(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate JSON key')
            result[key] = value
        return result
    value = json.loads(text, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite JSON number')))
    canonical(value)
    return value


class ScriptedAgent:
    max_model_calls_per_request = 1
    identity = {'backend': 'scripted', 'model': None, 'purpose': 'protocol_test_double'}

    def __init__(self, responses=None):
        self.responses = list(responses) if responses is not None else None
        self.calls = 0

    def respond(self, context):
        self.calls += 1
        if self.responses is not None:
            if not self.responses:
                return {'kind': 'cannot_continue', 'reason': 'Scripted protocol responses exhausted'}
            result = self.responses.pop(0)
            return result(context) if callable(result) else result
        if context.get('child_result') is not None:
            child = context['child_result']
            return {'kind': 'candidate_result', 'outputs': child.get('outputs', child)}
        inputs = context.get('inputs', {})
        if not inputs.get('rule'):
            return {'kind': 'need_info', 'field': 'rule', 'reason': 'The deduplication rule is not bound'}
        if context.get('task') != 'orders.clean':
            return {'kind': 'cannot_continue', 'reason': 'Protocol fixture only handles orders.clean'}
        return {'kind': 'need_subtask', 'task': 'orders.clean', 'inputs': dict(inputs),
                'reason': 'Delegate cleaning to an applicable checked capability composition'}


class ScriptedPlanner:
    max_model_calls_per_request = 1
    identity = {'backend': 'scripted', 'model': None, 'purpose': 'protocol_test_double'}

    def __init__(self):
        self.calls = 0

    def plan(self, context):
        self.calls += 1
        if context.get('task') != 'orders.clean':
            raise SopError('capability_missing', 'Protocol planner only composes orders.clean')
        catalog = context.get('catalog', {})
        if not {'orders.normalize', 'orders.deduplicate'}.issubset(catalog):
            raise SopError('capability_missing', 'Normalization and deduplication must be declared')
        return {'schema': 'sop/1', 'id': 'generated-orders-clean', 'task': 'orders.clean',
                'params': {'source_file': {'type': 'artifact', 'required': True},
                           'rule': {'type': 'string', 'enum': ['highest_revision', 'first'], 'required': True}},
                'capabilities': ['orders.normalize', 'orders.deduplicate'],
                'steps': [{'id': 'normalize', 'kind': 'fixed', 'capability': 'orders.normalize',
                           'inputs': {'source_file': '$input.source_file'}},
                          {'id': 'deduplicate', 'kind': 'fixed', 'capability': 'orders.deduplicate',
                           'inputs': {'normalized': '$steps.normalize.normalized', 'rule': '$input.rule'}}],
                'outputs': {'cleaned': '$steps.deduplicate.cleaned'}, 'checks': ['orders.clean'], 'recovery': []}


_CONTEXT_FIELDS = {'task', 'inputs', 'facts', 'child_result', 'catalog', 'continuation', 'limits',
                   'contract', 'information_query', 'rule', 'required_outputs', 'checks', 'requirements',
                   'source', 'answers', 'scope', 'selection', 'sources', 'clauses', 'analysis',
                   'basis_hash', 'instruction', 'tool_receipts'}


def _safe_context(context):
    if not isinstance(context, dict) or set(context) - _CONTEXT_FIELDS:
        raise SopError('invalid_agent_context', 'Only declared logical context fields may reach Pi')
    # Fail on native Path objects and non-JSON values rather than stringifying paths.
    canonical(context)
    if 'sources' in context:
        sources = context['sources']
        if (not isinstance(sources, dict) or set(sources) != {'root_id', 'files'} or
                not isinstance(sources['files'], list)):
            raise SopError('invalid_agent_context', 'Sources must be a public Skill bundle')
        for source in sources['files']:
            if (not isinstance(source, dict) or
                    set(source) != {'id', 'relative_path', 'sha256', 'text', 'lines'}):
                raise SopError('invalid_agent_context', 'Source snapshots cannot expose native path metadata')
            for key in ('id', 'relative_path'):
                value = source[key]
                if (not isinstance(value, str) or not value or value.startswith(('/', chr(92))) or
                        chr(92) in value or ':' in value or any(part in ('', '.', '..') for part in value.split('/'))):
                    raise SopError('invalid_agent_context', 'Source identities must be confined relative paths')
        if sources['root_id'] not in [source['id'] for source in sources['files']]:
            raise SopError('invalid_agent_context', 'Source root must refer to a supplied snapshot')
    def walk(value):
        if isinstance(value, dict):
            if value.get('kind') in ('artifact', 'input'):
                if set(value) != {'hash', 'bytes', 'kind'} or not isinstance(value['hash'], str) or len(value['hash']) != 64:
                    raise SopError('invalid_agent_context', 'Artifacts must be logical content handles')
            for key, child in value.items():
                if key == 'path' and isinstance(child, str) and child.startswith('$.'):
                    continue  # Definition diagnostic JSON selector, never a filesystem path.
                if key in ('path', 'expected', 'expected_path', 'answer_file', 'source_path', 'work_dir', 'data_dir'):
                    raise SopError('invalid_agent_context', 'Filesystem or answer-key references are not model context')
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(context)
    return context


class _Pi:
    mode = 'agent'

    def __init__(self, *, provider=None, model=None, timeout=60, max_turns=3, max_tokens=4096, node='node'):
        if not 1 <= timeout <= 300 or not 1 <= max_turns <= 10 or not 128 <= max_tokens <= 16384:
            raise SopError('invalid_backend_config', 'Pi limits are outside supported bounds')
        self.provider, self.model = provider, model
        self.timeout, self.max_turns, self.max_tokens, self.node = timeout, max_turns, max_tokens, node
        self.max_model_calls_per_request = max_turns
        self.calls = 0
        self.last_evidence = None
        self.identity = {'backend': 'pi', 'provider': provider, 'model': model, 'purpose': 'real_model'}

    def _invoke(self, context, execute=None):
        context = _safe_context(context)
        request = {'mode': self.mode, 'context': context,
                   'config': {'provider': self.provider, 'model': self.model, 'timeout_ms': int(self.timeout*1000),
                              'max_turns': self.max_turns, 'max_tokens': self.max_tokens}}
        self.calls += 1
        if execute is not None:
            from .pi_transport import invoke_interactive
            try:
                response = invoke_interactive([self.node, str(Path(__file__).with_name('pi_worker.mjs'))],
                    request, execute, _strict_loads, timeout=self.timeout + 5, env=os.environ.copy())
            except SopError as exc:
                self.last_evidence = {'backend': 'pi', 'status': exc.code, 'calls': self.calls}
                raise
        else:
            try:
                result = subprocess.run([self.node, str(Path(__file__).with_name('pi_worker.mjs'))],
                                        input=canonical(request), capture_output=True, text=True,
                                        timeout=self.timeout + 5, cwd='/tmp', env=os.environ.copy())
            except subprocess.TimeoutExpired as exc:
                self.last_evidence = {'backend': 'pi', 'status': 'timeout', 'calls': self.calls}
                raise SopError('backend_timeout', 'Pi worker exceeded its bounded deadline') from exc
            except OSError as exc:
                self.last_evidence = {'backend': 'pi', 'status': 'unavailable', 'calls': self.calls}
                raise SopError('backend_unavailable', 'Cannot start the Pi worker') from exc
            try:
                response = _strict_loads(result.stdout)
                if not isinstance(response, dict):
                    raise ValueError('Worker response must be an object')
            except (ValueError, TypeError, SopError) as exc:
                self.last_evidence = {'backend': 'pi', 'status': 'worker_error', 'exit_code': result.returncode}
                raise SopError('backend_protocol', 'Pi worker did not return a valid structured result; raw transport diagnostics withheld') from exc
        self.last_evidence = response.get('evidence', {})
        if not response.get('ok'):
            error = response.get('error', {})
            raise SopError(error.get('code', 'backend_error'), error.get('message', 'Pi backend failed'))
        self.identity = {'backend': 'pi', **{key: self.last_evidence.get(key) for key in ('provider', 'model', 'pi_version')}, 'purpose': 'real_model'}
        try:
            proposal = _strict_loads(response['result_payload'])
            if not isinstance(proposal, dict) or proposal != response['result']:
                raise ValueError('Handoff payload differs from result')
        except (KeyError, TypeError, ValueError, SopError) as exc:
            raise SopError('backend_protocol', 'Pi handoff must be a strict JSON object with no duplicate keys') from exc
        return proposal


class PiAgent(_Pi):
    def __init__(self, *, enable_tools=True, **kwargs):
        if type(enable_tools) is not bool:
            raise SopError('invalid_backend_config', 'enable_tools must be a trusted boolean setting')
        super().__init__(**kwargs)
        self.enable_tools = enable_tools

    def respond(self, context):
        response = self._invoke(context)
        response['_usage'] = self.last_evidence.get('usage', {})
        response['_trace'] = self.last_evidence
        return response

    def respond_with_tools(self, context, execute):
        response = self._invoke(context, execute if self.enable_tools else None)
        response['_usage'] = self.last_evidence.get('usage', {})
        response['_trace'] = self.last_evidence
        return response


class PiPlanner(_Pi):
    mode = 'planner'

    def plan(self, context):
        return self._invoke(context)


class PiAuthor(_Pi):
    mode = 'author'

    def author(self, context):
        proposal = self._invoke(context)
        if proposal.get('schema') != 'skill-analysis/1':
            raise SopError('backend_protocol', 'Pi author must return skill-analysis/1')
        return proposal


class PiSemanticReviewer(_Pi):
    mode = 'review'

    def review(self, context):
        review = self._invoke(context)
        if review.get('schema') != 'skill-review/1':
            raise SopError('backend_protocol', 'Pi reviewer must return skill-review/1')
        return review
