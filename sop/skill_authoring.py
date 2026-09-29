"""Model-assisted Skill interpretation with a separate semantic review boundary.

Mechanical coverage is necessary, not a proof of natural-language accuracy.
Neither the generator nor reviewer receives execution tools or graph state rights.
"""
from contextlib import contextmanager
from copy import deepcopy
import fcntl
from pathlib import Path
import uuid

from .authoring import Authoring, RULES, _problem
from .common import SopError, canonical, digest, file_digest, read_json, write_json
from .sources import collect_sources

CLAUSES = {
    'deliverables': 'Deliver cleaned order detail, per-customer summary and quality totals.',
    'input': 'Input columns order_id/customer_id/revision/gross_cents/refund_cents; revision and amounts are integers, monetary unit cents; no floating arithmetic or guessed missing values.',
    'normalize': 'Trim customer_id leading/trailing whitespace only; preserve order IDs, revisions and monetary values.',
    'cross_customer': 'Reject an order with different normalized customers across ANY revisions.',
    'clean': 'Exactly one record per order under the explicit retention rule; net=gross-refund; order_id ascending.',
    'summary': 'Group by normalized customer; order count, gross/refund/net totals; customer_id ascending.',
    'quality': 'Input/retained/removed row counts and gross/refund/net totals; independently reconcile all outputs with original source.',
    'permissions': 'Read local input without modification; write run-owned artifacts only; no upload/network/delete or other external effects.',
    'ambiguity': 'Retention rule is unknown and must be asked, not guessed or borrowed from a method default.',
    'reuse': 'Declare source_file as a run parameter; each input batch is independent.',
    'fixed': 'All business steps use the declared registered fixed implementations; the model cannot replace code.',
    'pi': 'The cleaning task may use constrained Pi proposals and checked child methods.',
    'rule_highest_revision': 'Keep greatest integer revision, not last line; identical highest ties collapse; conflicting highest ties reject.',
    'rule_first': 'Keep first input record per order, after checking customer consistency across all revisions.',
    'rule_runtime': 'Both first and highest_revision are explicitly defined choices; require an explicit per-run selection, no default.',
}
REQUIRED = {'input', 'normalize', 'cross_customer', 'clean', 'summary', 'quality', 'permissions'}


def public_bundle(bundle):
    """Native paths are only for host integrity checks, never model context."""
    return {'root_id': bundle['root_id'], 'files': [
        {key: file[key] for key in ('id', 'relative_path', 'sha256', 'text', 'lines')}
        for file in bundle['files']]}


def bundle_hash(bundle):
    return digest(public_bundle(bundle))


def validate_analysis(analysis, bundle):
    """Validate exhaustive line coverage and retain every claim for review."""
    if not isinstance(analysis, dict) or set(analysis) - {'schema', 'task', 'mode', 'annotations', 'rule', 'definition'}:
        raise SopError('analysis_schema', 'unknown analysis fields')
    if analysis.get('schema') != 'skill-analysis/1' or analysis.get('task') != 'orders.report':
        raise SopError('analysis_schema', 'expected skill-analysis/1 and a declared orders.report task')
    if analysis.get('mode') not in ('fixed', 'pi'):
        raise SopError('analysis_schema', 'mode must be explicit fixed or pi')
    rule = analysis.get('rule')
    if not isinstance(rule, dict) or set(rule) != {'status', 'value'}:
        raise SopError('analysis_schema', 'rule requires status and value')
    if rule['status'] not in ('known', 'unknown', 'runtime') or (
            rule['status'] == 'known' and rule['value'] not in RULES) or (
            rule['status'] != 'known' and rule['value'] is not None):
        raise SopError('analysis_schema', 'invalid explicit rule status/value')
    annotations = analysis.get('annotations')
    if not isinstance(annotations, list):
        raise SopError('analysis_schema', 'annotations must be a list')
    sources = {file['id']: file for file in bundle['files']}
    covered, requirements, lines, categories = set(), [], [], set()
    for index, annotation in enumerate(annotations):
        if not isinstance(annotation, dict) or set(annotation) != {'source_id', 'start_line', 'end_line', 'kind', 'clauses', 'reason'}:
            raise SopError('analysis_schema', f'annotation {index} has undeclared or missing fields')
        source_id = annotation['source_id']
        if not isinstance(source_id, str) or source_id not in sources:
            raise SopError('analysis_source', 'annotation references unknown source')
        first, last = annotation['start_line'], annotation['end_line']
        source_lines = sources[source_id]['text'].splitlines()
        if type(first) is not int or type(last) is not int or not 1 <= first <= last <= len(source_lines):
            raise SopError('analysis_span', 'source span is out of bounds')
        kind, clauses = annotation['kind'], annotation['clauses']
        if kind not in ('requirement', 'background', 'example', 'uninterpreted') or not isinstance(annotation['reason'], str):
            raise SopError('analysis_schema', 'invalid source classification')
        if not isinstance(clauses, list) or any(not isinstance(c, str) or c not in CLAUSES for c in clauses) or len(set(clauses)) != len(clauses):
            raise SopError('analysis_clause', 'only declared precise clauses can be proposed')
        if (kind == 'requirement' and not clauses) or (kind != 'requirement' and clauses):
            raise SopError('analysis_clause', 'requirements require clauses; other classifications cannot hide claims')
        ids = []
        for number in range(first, last + 1):
            key = (source_id, number)
            if key in covered:
                raise SopError('analysis_overlap', 'source span interpreted more than once')
            covered.add(key)
        text = '\n'.join(source_lines[first-1:last])
        for clause in clauses or (['unsupported'] if kind == 'uninterpreted' else []):
            requirement_id = f'R{len(requirements)+1:03d}'
            ids.append(requirement_id)
            requirements.append({'id': requirement_id,
                                 'source': {'file': source_id, 'line': first, 'end_line': last, 'text': text},
                                 'meaning': clause, 'interpretation': CLAUSES.get(clause, annotation['reason']),
                                 'status': 'unsupported' if clause == 'unsupported' else 'pending',
                                 'targets': [], 'checks': ['orders.report'] if clause != 'unsupported' else [],
                                 'revision_source': 'source'})
            categories.add(clause)
        for number in range(first, last+1):
            lines.append({'file': source_id, 'line': number, 'text': source_lines[number-1],
                          'classification': kind, 'requirement_ids': ids})
    expected = {(file['id'], n) for file in bundle['files']
                for n, line in enumerate(file['text'].splitlines(), 1) if line.strip()}
    if not expected <= covered:
        raise SopError('analysis_uncovered_source', 'every nonempty source line must be classified, including attachments')
    if 'unsupported' in categories:
        raise SopError('unsupported_requirement', 'uninterpreted source requirements remain')
    if not REQUIRED <= categories:
        raise SopError('analysis_missing_contract', 'required declared semantics missing: ' + ', '.join(sorted(REQUIRED-categories)))
    if analysis['mode'] == 'pi' and 'fixed' in categories or analysis['mode'] == 'fixed' and 'pi' in categories:
        raise SopError('analysis_conflict', 'mode contradicts a source requirement')
    explicit = {c for c in categories if c.startswith('rule_')}
    expected_rule = {'known': {'rule_'+str(rule['value'])}, 'unknown': set(), 'runtime': {'rule_runtime'}}[rule['status']]
    if explicit != expected_rule:
        raise SopError('analysis_conflict', 'rule status contradicts source clauses')
    return requirements, lines


def partial_analysis_audit(analysis, bundle):
    """Preserve located, explicitly unverified claims even when validation fails."""
    sources = {file['id']: file for file in bundle['files']}
    audit = {'status': 'incomplete', 'annotations': [], 'uncovered': [], 'overlaps': [],
             'uninterpreted': [], 'invalid_annotations': [], 'requirements': [], 'source_lines': []}
    covered = {}
    annotations = analysis.get('annotations', []) if isinstance(analysis, dict) else []
    if not isinstance(annotations, list):
        annotations = []
    for index, item in enumerate(annotations):
        if not isinstance(item, dict):
            audit['invalid_annotations'].append({'annotation_index': index})
            continue
        source_id, first, last = item.get('source_id'), item.get('start_line'), item.get('end_line')
        if (not isinstance(source_id, str) or source_id not in sources or type(first) is not int or
                type(last) is not int or not 1 <= first <= last <= len(sources[source_id]['text'].splitlines())):
            audit['invalid_annotations'].append({'annotation_index': index, 'source_id': source_id,
                'start_line': first, 'end_line': last})
            continue
        source_lines = sources[source_id]['text'].splitlines()
        span = {'annotation_index': index, 'source_id': source_id, 'start_line': first, 'end_line': last,
                'text': '\n'.join(source_lines[first-1:last]), 'kind': item.get('kind'),
                'clauses': deepcopy(item.get('clauses'))}
        audit['annotations'].append(span)
        if item.get('kind') == 'uninterpreted':
            audit['uninterpreted'].append(deepcopy(span))
        clauses = item.get('clauses')
        clauses = clauses if isinstance(clauses, list) else []
        ids = []
        for clause in clauses or (['unsupported'] if item.get('kind') == 'uninterpreted' else []):
            if not isinstance(clause, str):
                continue
            requirement_id = f'R{len(audit["requirements"])+1:03d}'
            ids.append(requirement_id)
            audit['requirements'].append({'id': requirement_id,
                'source': {'file': source_id, 'line': first, 'end_line': last, 'text': span['text']},
                'meaning': clause, 'interpretation': CLAUSES.get(clause, str(item.get('reason', ''))),
                'status': 'unverified' if clause in CLAUSES else 'unsupported', 'targets': [], 'checks': [],
                'revision_source': 'source'})
        for number in range(first, last+1):
            key = (source_id, number)
            if key in covered:
                audit['overlaps'].append({'source_id': source_id, 'start_line': number,
                    'end_line': number, 'annotation_indices': [covered[key]['annotation_index'], index]})
            covered[key] = {'classification': item.get('kind', 'uninterpreted'),
                            'requirement_ids': ids, 'annotation_index': index}
    for source_id, file in sources.items():
        for number, text in enumerate(file['text'].splitlines(), 1):
            location = {'source_id': source_id, 'start_line': number, 'end_line': number}
            entry = covered.get((source_id, number))
            if text.strip() and entry is None:
                audit['uncovered'].append(location)
            audit['source_lines'].append({'file': source_id, 'line': number, 'text': text,
                'classification': entry['classification'] if entry else 'uninterpreted',
                'requirement_ids': entry['requirement_ids'] if entry else []})
    return audit


class SemanticAuthoring(Authoring):
    def __init__(self, data_dir, registry, model=None, reviewer=None, max_model_calls=12):
        super().__init__(data_dir, registry, model=model, max_model_calls=max_model_calls)
        self.reviewer = reviewer

    def _control_path(self, draft_id):
        self._path(draft_id)  # Validate identity before forming a second path.
        return self.base / 'controls' / (draft_id + '.json')

    @contextmanager
    def _control_lock(self, draft_id):
        directory = self._control_path(draft_id).parent
        try:
            directory.mkdir(parents=True, exist_ok=True)
            with (directory / (draft_id + '.lock')).open('a') as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            raise SopError('storage_failure', str(exc)) from exc

    def _control(self, draft_id):
        path = self._control_path(draft_id)
        return read_json(path) if path.exists() else {'requested': False, 'receipts': {}}

    def _apply_cancel(self, draft):
        control = self._control(draft['draft_id'])
        if not control.get('requested'):
            return False
        changed = draft.get('stop_reason') != 'cancelled'
        if changed:
            draft['revision'] += 1
            draft['cancellation'] = deepcopy(control['request'])
            if draft.get('report'):
                draft['report_history'].append(deepcopy(draft['report']))
                draft['report'] = {**draft['report'], 'passed': False, 'applicable': False,
                                   'invalidated_by': ['authoring_cancelled']}
            for question in draft['questions']:
                if question['status'] == 'open':
                    question['status'] = 'cancelled'
        draft['state'], draft['stop_reason'] = 'stopped', 'cancelled'
        return changed

    def _save(self, draft):
        # Cancel acceptance and the final delivery commit serialize on a small
        # per-draft lock, which is never held over a model request.
        with self._control_lock(draft['draft_id']):
            if set(draft.get('receipts', {})) & set(self._control(draft['draft_id'])['receipts']):
                raise SopError('message_conflict', 'message identity was concurrently used by a cancellation')
            self._apply_cancel(draft)
            super()._save(draft)

    def _check_cancel(self, draft):
        if self._control(draft['draft_id']).get('requested'):
            self._apply_cancel(draft)
            raise SopError('draft_cancelled', 'authoring was cancelled; no further model requests or delivery')

    def cancel(self, draft_id, *, message_id, revision):
        """Persist cancellation without waiting for the busy model/global lock."""
        if not isinstance(message_id, str) or not message_id or type(revision) is not int:
            raise SopError('invalid_message', 'message identity and integer revision required')
        payload_hash = digest({'kind': 'cancel', 'draft_id': draft_id, 'revision': revision})
        with self._control_lock(draft_id):
            control = self._control(draft_id)
            receipt = control['receipts'].get(message_id)
            if receipt:
                if receipt['payload_hash'] != payload_hash:
                    raise SopError('message_conflict', 'message content differs')
                return deepcopy(receipt['response'])
            draft = self._read(draft_id)
            if message_id in draft['receipts']:
                raise SopError('message_conflict', 'message identity belongs to another authoring command')
            if revision != draft['revision']:
                raise SopError('stale_revision', 'draft changed')
            requested = draft['state'] != 'delivered'
            response = {'draft_id': draft_id, 'revision': revision, 'message_id': message_id,
                        'requested': requested, 'state': 'cancel_requested' if requested else 'delivered'}
            if requested:
                control['requested'] = True
                control.setdefault('request', {'message_id': message_id, 'revision': revision})
            control['receipts'][message_id] = {'payload_hash': payload_hash, 'response': response}
            try:
                write_json(self._control_path(draft_id), control)
            except OSError as exc:
                raise SopError('storage_failure', str(exc)) from exc
            return deepcopy(response)

    def get(self, draft_id):
        with self._lock():
            draft = self._read(draft_id)
            self._invalidate(draft)
            self._save(draft)
            return deepcopy(draft)

    @staticmethod
    def _answer_alignment(draft, analysis):
        effective = draft['answers'].get('rule')
        if not effective or not isinstance(analysis, dict):
            return
        rule = analysis.get('rule', {})
        if not isinstance(rule, dict):
            return  # Structural validation supplies the diagnostic.
        if (rule.get('status') == 'runtime' or
                rule.get('status') == 'known' and rule.get('value') != effective['value'] or
                effective.get('source') == 'source' and rule.get('status') != 'known'):
            raise SopError('answer_conflict', 'revision cannot replace an effective source/answer rule')

    def _call(self, draft, role, port, method, context):
        self._check_cancel(draft)
        if port is None:
            raise SopError('semantic_checker_missing' if role == 'review' else 'backend_unavailable', f'{role} backend is required')
        maximum = getattr(port, 'max_model_calls_per_request', 1)
        if type(maximum) is not int or maximum < 1:
            raise SopError('invalid_backend_config', 'model request bound must be a positive integer')
        if draft['usage']['model_calls'] + maximum > draft['limits']['max_model_calls']:
            raise SopError('budget_exhausted', f'no remaining budget for {role}')
        draft['usage']['model_calls'] += maximum
        attempt = {'role': role, 'state': 'reserved', 'context_hash': digest(context),
                   'reservation': maximum, 'number': len(draft['model_attempts'])+1}
        draft['model_attempts'].append(attempt)
        self._save(draft)
        self._check_cancel(draft)
        try:
            result = getattr(port, method)(deepcopy(context))
            canonical(result)
        except Exception as exc:
            attempt['state'] = 'failed'
            attempt['evidence'] = deepcopy(getattr(port, 'last_evidence', None))
            self._save(draft)
            if isinstance(exc, SopError):
                raise
            raise SopError('model_failure', f'{role} backend did not complete') from exc
        attempt['state'] = 'completed'
        attempt['evidence'] = deepcopy(getattr(port, 'last_evidence', None))
        if self._control(draft['draft_id']).get('requested'):
            attempt['late_result'] = deepcopy(result)
            self._save(draft)
            self._check_cancel(draft)
        return result

    def submit(self, source_path, proposal=None):
        bundle = collect_sources(source_path)
        if not bundle['files']:
            raise SopError('source_invalid', canonical(bundle['diagnostics']))
        root = next(file for file in bundle['files'] if file['id']==bundle['root_id'])
        source_hash = root['sha256']
        snapshot = self.base/'sources'/(source_hash+'.json')
        with self._lock():
            try:
                write_json(snapshot, {'source_hash': source_hash, 'text': root['text']})
            except OSError as exc:
                raise SopError('storage_failure', str(exc)) from exc
            draft = {'schema':'draft/1', 'authoring_mode':'semantic', 'draft_id':'draft-'+uuid.uuid4().hex,
                     'revision':1, 'state':'authoring', 'source':{'path':root['path'],'snapshot':str(snapshot),'hash':source_hash},
                     'source_hash':source_hash, 'source_bundle':bundle, 'bundle_hash':bundle_hash(bundle),
                     'source_lines':[], 'requirements':[], 'requirement_map':{'forward':{},'reverse':{}},
                     'questions':[], 'answers':{}, 'definition':None, 'analysis':deepcopy(proposal), 'proposal':None,
                     'report':None,'report_history':[], 'diagnostics':deepcopy(bundle['diagnostics']),
                     'publication':'unpublished','trials':'not_run','business_calls':0,
                     'usage':{'model_calls':0},'limits':{'max_model_calls':self.max_model_calls},
                     'model_attempts':[], 'semantic_reviews':[], 'receipts':{}, 'mode':'pi',
                     'revision_history':[], 'analysis_audit':None,
                     'semantic_evidence':{'method':'model_proposal_and_independent_review',
                                          'general_nl_validation':'not_run','business_semantics':'independent_runtime_checker_required'}}
            self._save(draft)
            return self._continue(draft)

    def _review(self, draft):
        basis = {'bundle_hash':draft['bundle_hash'], 'analysis':draft['analysis'], 'answers':draft['answers']}
        basis_hash = digest(basis)
        context = {'sources':public_bundle(draft['source_bundle']), 'analysis':draft['analysis'],
                   'answers':draft['answers'], 'clauses':CLAUSES, 'basis_hash':basis_hash,
                   'instruction':'Independently compare EVERY source span with exact declared clauses. Reject omissions, extra constraints, incorrect rule interpretation, unsupported scripts, and requirements hidden as background/examples. Review from original sources, not generator self-assessment.'}
        result = self._call(draft, 'review', self.reviewer, 'review', context)
        if (not isinstance(result, dict) or set(result) != {'schema','basis_hash','verdict','findings'} or
                result.get('schema')!='skill-review/1' or result.get('basis_hash')!=basis_hash or
                result.get('verdict') not in ('pass','reject') or not isinstance(result.get('findings'),list)):
            raise SopError('semantic_review_protocol', 'review must bind the exact source/analysis/answer basis')
        if result['verdict']=='pass' and result['findings']:
            raise SopError('semantic_review_protocol', 'passing review cannot contain unresolved findings')
        draft['semantic_reviews'].append(deepcopy(result))
        if result['verdict']!='pass':
            raise SopError('semantic_review_rejected', canonical(result['findings']))
        draft['semantic_evidence']['review_basis_hash'] = basis_hash
        draft['semantic_evidence']['reviewer'] = deepcopy(getattr(self.reviewer,'identity',{'backend':'injected_review_port'}))

    def _continue(self, draft):
        try:
            self._check_cancel(draft)
            if draft['diagnostics']:
                draft['state']='stopped'
                self._save(draft)
                return deepcopy(draft)
            if not self._sources_current(draft):
                raise SopError('source_changed','source or an attachment changed; resubmit materials')
            if draft['analysis'] is None:
                context = {'sources':public_bundle(draft['source_bundle']), 'clauses':CLAUSES,
                           'catalog':self.registry.catalog(), 'scope':'orders.report', 'limits':draft['limits'],
                           'answers':deepcopy(draft['answers'])}
                if draft.get('revision_history'):
                    previous = draft['revision_history'][-1]
                    previous_diagnostics = deepcopy(previous['diagnostics'])
                    previous_diagnostics.extend(deepcopy((previous.get('report') or {}).get('diagnostics', [])))
                    for diagnostic in previous_diagnostics:
                        if 'path' in diagnostic and not diagnostic['path'].startswith('$.'):
                            diagnostic['selector'] = diagnostic.pop('path')
                    context.update(analysis=deepcopy(previous['analysis']),
                        instruction='Revise the previous analysis against unchanged original sources and effective answers. Fix recorded errors without dropping requirements or altering permissions.',
                        requirements={'previous_diagnostics':previous_diagnostics})
                draft['analysis'] = self._call(draft,'author',self.model,'author',context)
            audit = partial_analysis_audit(draft['analysis'],draft['source_bundle'])
            draft['analysis_audit'] = audit
            draft['requirements'], draft['source_lines'] = deepcopy(audit['requirements']), deepcopy(audit['source_lines'])
            requirements, lines = validate_analysis(draft['analysis'],draft['source_bundle'])
            self._answer_alignment(draft, draft['analysis'])
            audit['status'] = 'mechanically_valid'
            draft['requirements'],draft['source_lines']=requirements,lines
            draft['mode']=draft['analysis']['mode']
            draft['proposal']=deepcopy(draft['analysis'].get('definition'))
            rule=draft['analysis']['rule']
            if rule['status']=='known' and not draft['answers']:
                draft['answers']['rule']={'value':rule['value'],'source':'source','source_hash':draft['source_hash']}
            draft['rule_at_runtime'] = rule['status']=='runtime'
            draft['state']='checking'
            self._review(draft)
            if not self._sources_current(draft):
                raise SopError('source_changed','source or attachment changed during semantic review')
            if rule['status']=='unknown' and not draft['answers']:
                if not any(question['status']=='open' for question in draft['questions']):
                    request_id = 'dedup_policy' if not draft['questions'] else f'dedup_policy-r{draft["revision"]}'
                    draft['questions'].append({'request_id':request_id,'field':'rule','status':'open',
                        'reason':'原始资料未明确订单修订保留规则；请选择保留首次或最大整数 revision。',
                        'schema':{'type':'string','enum':RULES[:]},
                        'options':{'highest_revision':CLAUSES['rule_highest_revision'],'first':CLAUSES['rule_first']},
                        'requirements':[r['id'] for r in requirements if r['meaning'] in ('ambiguity','clean')]})
                draft['state']='waiting_info'
            else:
                self._check_cancel(draft)
                draft['semantic_evidence']['review_status']='passed'
                self._compile(draft)
                draft['report'].update({'bundle_hash':draft['bundle_hash'],
                     'analysis_hash':digest(draft['analysis']), 'semantic_reviews_hash':digest(draft['semantic_reviews']),
                     'semantic_authoring_version':file_digest(Path(__file__))})
                if not self._sources_current(draft):
                    draft['report']['passed']=False
                    draft['report']['applicable']=False
                    raise SopError('source_changed','source or attachment changed before delivery')
        except SopError as exc:
            diagnostic = _problem(exc.code,'$.semantic_authoring',str(exc))
            audit = draft.get('analysis_audit') or {}
            locations = {'analysis_uncovered_source':'uncovered', 'analysis_overlap':'overlaps',
                         'unsupported_requirement':'uninterpreted', 'analysis_span':'invalid_annotations',
                         'analysis_source':'invalid_annotations'}.get(exc.code)
            if locations:
                diagnostic['source_spans'] = deepcopy(audit.get(locations, []))
            elif exc.code.startswith('analysis_'):
                diagnostic['source_spans'] = deepcopy(audit.get('annotations', []))
            draft['diagnostics'].append(diagnostic)
            draft['state']='stopped'
        self._save(draft)
        return deepcopy(draft)

    @staticmethod
    def _sources_current(draft):
        for file in draft['source_bundle']['files']:
            path=Path(file['path'])
            try:
                if path.resolve()!=path or path.stat().st_nlink!=1 or file_digest(path)!=file['sha256']:
                    return False
            except OSError:
                return False
        return bundle_hash(draft['source_bundle'])==draft['bundle_hash']

    def _invalidate(self,draft):
        if not draft.get('report') or not draft['report'].get('applicable'):
            return False
        changed = super()._invalidate(draft)
        report=draft['report']
        reasons=[]
        if not self._sources_current(draft): reasons.append('source_bundle_changed')
        if digest(draft['analysis'])!=report.get('analysis_hash'): reasons.append('analysis_changed')
        if digest(draft['semantic_reviews'])!=report.get('semantic_reviews_hash'): reasons.append('semantic_review_changed')
        if file_digest(Path(__file__))!=report.get('semantic_authoring_version'): reasons.append('semantic_policy_changed')
        if reasons:
            if not changed:
                draft['report_history'].append(deepcopy(report))
                draft['revision']+=1
            draft['report']={**report,'passed':False,'applicable':False,
                             'invalidated_by':sorted(set(report.get('invalidated_by',[])+reasons))}
            draft['state']='stopped'
            return True
        return changed

    def revise(self, draft_id, *, message_id, revision, proposal=None, request_model=False):
        """Explicit bounded revision; sources, effective answers and run pins survive."""
        if not isinstance(message_id, str) or not message_id or type(revision) is not int:
            raise SopError('invalid_message', 'message identity and integer revision required')
        if type(request_model) is not bool or (proposal is None) == (not request_model):
            raise SopError('invalid_revision', 'provide exactly one analysis proposal or request_model=true')
        canonical(proposal)
        payload_hash = digest({'kind':'revise', 'draft_id':draft_id, 'revision':revision,
                               'proposal':proposal, 'request_model':request_model})
        with self._lock():
            draft = self._read(draft_id)
            receipt = draft['receipts'].get(message_id)
            if receipt:
                if receipt['payload_hash'] != payload_hash:
                    raise SopError('message_conflict', 'message content differs')
                return deepcopy(receipt['response'])
            if message_id in self._control(draft_id)['receipts']:
                raise SopError('message_conflict', 'message identity belongs to a cancellation')
            self._check_cancel(draft)
            if revision != draft['revision']:
                raise SopError('stale_revision', 'draft changed')
            if not self._sources_current(draft) or draft['source_bundle']['diagnostics']:
                raise SopError('source_changed', 'source materials changed or remain incomplete; resubmit instead of revising')
            if proposal is not None:
                self._answer_alignment(draft, proposal)
            if self.reviewer is None:
                raise SopError('semantic_checker_missing', 'revised candidates require independent semantic review')
            previous = {key:deepcopy(draft.get(key)) for key in (
                'revision', 'state', 'analysis', 'analysis_audit', 'definition', 'requirements',
                'source_lines', 'requirement_map', 'questions', 'answers', 'report', 'diagnostics',
                'semantic_reviews', 'semantic_evidence', 'usage', 'bundle_hash')}
            draft.setdefault('revision_history', []).append(previous)
            if draft.get('report'):
                draft['report_history'].append(deepcopy(draft['report']))
            draft['revision'] += 1
            draft['state'] = 'authoring'
            draft.pop('stop_reason', None)
            draft['analysis'] = deepcopy(proposal)
            draft['analysis_audit'] = None
            draft['proposal'], draft['definition'], draft['report'] = None, None, None
            draft['diagnostics'], draft['requirements'], draft['source_lines'] = [], [], []
            draft['requirement_map'] = {'forward':{}, 'reverse':{}}
            draft['semantic_reviews'] = []
            draft['semantic_evidence'] = {'method':'model_proposal_and_independent_review',
                'general_nl_validation':'not_run', 'business_semantics':'independent_runtime_checker_required'}
            for question in draft['questions']:
                if question['status'] == 'open':
                    question['status'] = 'superseded'
            accepted = deepcopy(draft)
            accepted['receipts'] = {}
            draft['receipts'][message_id] = {'payload_hash':payload_hash, 'response':accepted}
            self._save(draft)
            result = self._continue(draft)
            response = deepcopy(result)
            response['receipts'] = {}
            draft['receipts'][message_id]['response'] = response
            self._save(draft)
            return response

    def answer(self,draft_id,request_id,value,message_id,revision):
        if not isinstance(message_id,str) or not message_id or type(revision) is not int:
            raise SopError('invalid_message','message identity and integer revision required')
        payload_hash=digest({'draft_id':draft_id,'request_id':request_id,'value':value,'revision':revision})
        with self._lock():
            draft=self._read(draft_id)
            receipt=draft['receipts'].get(message_id)
            if receipt:
                if receipt['payload_hash']!=payload_hash: raise SopError('message_conflict','message content differs')
                return deepcopy(receipt['response'])
            if message_id in self._control(draft_id)['receipts']:
                raise SopError('message_conflict','message identity belongs to a cancellation')
            self._check_cancel(draft)
            if revision!=draft['revision']: raise SopError('stale_revision','draft changed')
            question=next((q for q in draft['questions'] if q['request_id']==request_id and q['status']=='open'),None)
            if draft['state']!='waiting_info' or question is None: raise SopError('question_mismatch','no matching open question')
            if not self._sources_current(draft): raise SopError('source_changed','source or attachment changed')
            if self.reviewer is None:
                raise SopError('semantic_checker_missing','answer requires configured independent semantic review; question stays open')
            rule=self._answer_rule(value)
            draft['answers']['rule']={'value':rule,'source':'answer','request_id':request_id,
                                      'message_id':message_id,'answer':deepcopy(value),'revision':revision}
            question['status']='answered'
            draft['revision']+=1
            draft['state']='checking'
            accepted=deepcopy(draft)
            accepted['receipts']={}
            draft['receipts'][message_id]={'payload_hash':payload_hash,'response':accepted}
            self._save(draft) # answer/closure/receipt/budget basis survive a lost review response
            result=self._continue(draft)
            response=deepcopy(result);response['receipts']={}
            draft['receipts'][message_id]['response']=response
            self._save(draft)
            return response

    def resume(self,draft_id):
        with self._lock():
            draft=self._read(draft_id)
            if draft['state'] not in ('authoring','checking'):
                self._save(draft)
                return deepcopy(draft)
            return self._continue(draft)
