#!/usr/bin/env python3
"""Continue one delivered semantic case through both recursive paths and batches.

Default execution uses explicit protocol doubles. --backend pi uses Pi with no
fallback. The source authoring evidence is never rewritten, and no answer key
is used to supply missing business information.
"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import shutil
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sop.agents import PiAgent, PiPlanner, ScriptedAgent, ScriptedPlanner
from sop.authoring import Authoring, Library, clean_definition
from sop.capabilities import Registry
from sop.common import SopError, digest, file_digest, read_json, write_json
from sop.service import application

FIXTURE = ROOT / 'tests/fixtures/first-release'
CASES = [(method, batch) for method in ('existing', 'generated') for batch in ('batch-01', 'batch-02')]
IDENTITY_FIELDS = ('draft_id', 'revision', 'authoring_mode', 'mode', 'state', 'definition', 'report',
                   'answers', 'source_hash', 'bundle_hash', 'publication')


def source_identity(case):
    """Fingerprint only the input records; never instantiate storage in them."""
    authoring = case / '.data' / 'authoring'
    if not authoring.is_dir() or not (case / 'final.json').is_file():
        raise SopError('semantic_case_missing', 'final.json and persisted .data/authoring are required')
    files = [case / 'final.json', *sorted(p for p in authoring.rglob('*') if p.is_file())]
    if any(p.is_symlink() for p in [case / '.data', case / 'final.json', authoring, *authoring.rglob('*')]):
        raise SopError('unsafe_path', 'semantic authoring evidence cannot contain symbolic links')
    return {str(p.relative_to(case)): file_digest(p) for p in files}


def checked_draft(service, snapshot):
    # Exactly the trusted entry used by CLI run --draft; it dispatches semantic
    # drafts to SemanticAuthoring and invalidates stale sources/reports/answers.
    draft = service.get(snapshot['draft_id'])
    if (draft.get('state') != 'delivered' or draft.get('authoring_mode') != 'semantic' or
            not (draft.get('report') or {}).get('passed') or not draft['report'].get('applicable')):
        raise SopError('draft_not_delivered', 'persisted semantic draft does not have a current passing report')
    if any(draft.get(key) != snapshot.get(key) for key in IDENTITY_FIELDS):
        raise SopError('draft_identity_mismatch', 'final snapshot differs from persisted definition, report or answers')
    if draft.get('publication') != 'unpublished':
        raise SopError('publication_mismatch', 'handoff evidence must remain unpublished')
    if draft.get('mode') != 'pi':
        raise SopError('recursive_mode_required', 'fixed drafts cannot evidence Pi recursive paths; supply a pi draft')
    rule = draft.get('answers', {}).get('rule', {}).get('value')
    if rule is None:
        raise SopError('bound_rule_required', 'source draft has no effective rule; no label answer will be injected')
    if rule != 'highest_revision':
        raise SopError('fixture_rule_unsupported', 'these independent batch expectations require highest_revision')
    return draft


def provenance(draft):
    return deepcopy({'draft_id': draft['draft_id'], 'revision': draft['revision'],
                     'report': draft['report'], 'answers': draft['answers']})


def check_run(rt, run, draft, expected_origin, batch, case_dir):
    """Host-only acceptance, separate from model claims and runtime checks."""
    errors, comparisons = [], {}
    root = run['tasks'][run['root']]
    if root['definition']['hash'] != digest(draft['definition']) or rt.store.json(root['definition']) != draft['definition']:
        errors.append('authored_definition_changed')
    if run['authoring'] != provenance(draft) or root['inputs'].get('rule') != draft['answers']['rule']['value']:
        errors.append('authored_provenance_or_answer_changed')
    if root['inputs']['source_file']['hash'] != file_digest(FIXTURE / batch / 'input.csv'):
        errors.append('bound_input_changed')
    if run['answers'] or run['questions']:
        errors.append('unexpected_runtime_information_request')
    if run['publication'] != 'unpublished':
        errors.append('unexpected_publication')
    children = [t for t in run['tasks'].values() if t['parent_call']]
    if len(children) != 1 or [t['accepted']['origin'] for t in children] != [expected_origin]:
        errors.append('recursive_method_origin_mismatch')
    if any(t['state'] != 'succeeded' or not (t.get('verification') or {}).get('passed') for t in run['tasks'].values()):
        errors.append('parent_or_child_not_verified')
    if len(run['calls']) != len(children) or any(not c['consumed'] for c in run['calls'].values()):
        errors.append('child_return_not_consumed')
    if run['state'] != 'succeeded':
        errors.append('run_not_succeeded')
    if set(root['outputs']) != {'cleaned', 'summary', 'quality'}:
        errors.append('missing_parent_outputs')
    for name in ('cleaned', 'summary', 'quality'):
        if name not in root['outputs']:
            continue
        ext = '.json' if name == 'quality' else '.csv'
        actual = rt.store.path(root['outputs'][name])
        expected = FIXTURE / batch / 'expected' / (name + ext)
        comparisons[name] = (read_json(actual) == read_json(expected) if ext == '.json'
                             else actual.read_bytes() == expected.read_bytes())
        (case_dir / (name + ext)).write_bytes(actual.read_bytes())
    if set(comparisons) != {'cleaned', 'summary', 'quality'} or not all(comparisons.values()):
        errors.append('independent_expected_mismatch')
    return errors, comparisons, children


def verify(output, *, semantic_case, backend='scripted'):
    if backend not in ('scripted', 'pi'):
        raise SopError('evaluation_backend', 'choose scripted or explicit pi; no fallback is permitted')
    source = Path(semantic_case).absolute()
    output = Path(output).absolute()
    if source.resolve() != source or output.resolve() != output:
        raise SopError('unsafe_path', 'evidence paths must not contain symbolic links')
    if output == source or source in output.parents:
        raise SopError('output_overlap', 'output cannot overwrite or extend the source semantic case')
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise SopError('output_exists', 'use a new empty directory and retain earlier evidence')
    output.mkdir(parents=True, exist_ok=True)
    result = {'schema': 'semantic-handoff-evidence/1', 'created_at': datetime.now(timezone.utc).isoformat(),
              'backend': backend, 'real_model': backend == 'pi', 'complete': False, 'status': 'preparing',
              'semantic_case': str(source), 'case_count': len(CASES), 'cases': [],
              'not_run_cases': [f'{method}-{batch}' for method, batch in CASES],
              'model_validation': 'explicit real Pi requests; see saved usage' if backend == 'pi' else 'program protocol doubles only',
              'semantic_validation': 'uses prior delivered source evidence; does not re-score language understanding',
              'algorithm_benefit': 'not_measured', 'publication': 'unpublished',
              'limits': {'max_actions': 100, 'max_model_calls': 20, 'max_depth': 8},
              'model_reservation_note': 'usage.model_calls reserves adapter maxima; actual requests and tokens are in model_usage',
              'implementation_hashes': {str(p.relative_to(ROOT)): file_digest(p) for p in sorted((ROOT / 'sop').glob('*')) if p.is_file()},
              'fixture_hashes': {str(p.relative_to(FIXTURE)): file_digest(p)
                                 for batch in ('batch-01', 'batch-02')
                                 for p in [FIXTURE / batch / 'input.csv', *sorted((FIXTURE / batch / 'expected').glob('*'))]},
              'driver_sha256': file_digest(Path(__file__))}
    write_json(output / 'summary.json', result)
    try:
        pinned = source_identity(source)
        snapshot = read_json(source / 'final.json')
        write_json(output / 'source-identity.json', pinned)
        write_json(output / 'source-final.json', snapshot)
        # get() may persist invalidation, so run it on an exact private copy.
        # Snapshot paths still refer to original evidence and are checked read-only.
        copied_data = output / 'input-authoring' / '.data'
        shutil.copytree(source / '.data' / 'authoring', copied_data / 'authoring')
        registry = Registry()
        author = Authoring(copied_data, registry)
        draft = checked_draft(author, snapshot)
        if source_identity(source) != pinned:
            raise SopError('source_changed', 'source evidence changed while copying authoring state')
        write_json(output / 'checked-draft.json', draft)
        result['input_draft'] = {'draft_id': draft['draft_id'], 'revision': draft['revision'],
                                 'definition_hash': digest(draft['definition']), 'report_hash': digest(draft['report']),
                                 'answers_hash': digest(draft['answers']), 'bundle_hash': draft['bundle_hash'],
                                 'authoring_usage': draft['usage'], 'semantic_evidence': draft.get('semantic_evidence', {})}
    except Exception as exc:
        result.update(status='blocked', error={'code': getattr(exc, 'code', 'preparation_error'), 'message': str(exc)})
        write_json(output / 'summary.json', result)
        return result

    saved_candidate_hash = None
    for method, batch in CASES:
        name = f'{method}-{batch}'
        case_dir = output / name
        case_dir.mkdir()
        started = time.monotonic()
        rt = run = None
        record = {'case': name, 'method': method, 'batch': batch, 'passed': False,
                  'state': 'not_started', 'expected_comparison': {}, 'validation_errors': []}
        try:
            if source_identity(source) != pinned:
                raise SopError('source_changed', 'source authoring records changed between runs')
            draft = checked_draft(author, snapshot)
            definitions = [clean_definition('first')]
            if method == 'existing':
                definitions.append(clean_definition())
            library = Library(definitions)
            expected_origin = 'existing' if method == 'existing' else 'generated'
            if method == 'generated' and batch == 'batch-02':
                saved = read_json(output / 'generated-candidate.json')
                if saved['definition_hash'] != saved_candidate_hash or digest(saved['definition']) != saved_candidate_hash:
                    raise SopError('candidate_changed', 'saved generated candidate identity changed')
                if saved['publication'] != 'unpublished':
                    raise SopError('publication_mismatch', 'candidate must remain unpublished')
                library.add(saved['definition'], origin='candidate_reuse')
                expected_origin = 'candidate_reuse'
            agent, planner = ((PiAgent(enable_tools=False), PiPlanner()) if backend == 'pi'
                              else (ScriptedAgent(), ScriptedPlanner()))
            rt = application(case_dir / '.data', registry=registry, library=library, agent=agent, planner=planner)
            definition = draft['definition']
            run = rt.start(definition, {'source_file': FIXTURE / batch / 'input.csv',
                                       'rule': draft['answers']['rule']['value']},
                           capabilities=definition['capabilities'], limits=result['limits'], authoring=provenance(draft))
            record['run_id'] = run['id']
            write_json(case_dir / 'started-run.json', run)
            run = rt.advance(run['id'])
            record['validation_errors'], record['expected_comparison'], children = check_run(
                rt, run, draft, expected_origin, batch, case_dir)
            events = rt.store.events(run['id'])
            record['planner_dispatches'] = sum(e['kind'] == 'planner_dispatched' for e in events)
            if method == 'generated' and batch == 'batch-01' and record['planner_dispatches'] != 1:
                record['validation_errors'].append('expected_one_generation')
            if expected_origin != 'generated' and record['planner_dispatches'] != 0:
                record['validation_errors'].append('unexpected_replanning')
            if expected_origin == 'candidate_reuse' and (len(children) != 1 or
                    children[0]['definition']['hash'] != saved_candidate_hash):
                record['validation_errors'].append('saved_candidate_not_reused')
            if source_identity(source) != pinned:
                record['validation_errors'].append('source_evidence_changed_during_run')
            checked_draft(author, snapshot)
            record['passed'] = not record['validation_errors']
            if record['passed'] and method == 'generated' and batch == 'batch-01':
                saved_candidate = rt.store.json(children[0]['definition'])
                saved_candidate_hash = digest(saved_candidate)
                write_json(output / 'generated-candidate.json', {'definition': saved_candidate,
                           'definition_hash': saved_candidate_hash, 'publication': 'unpublished', 'source_run': run['id']})
        except Exception as exc:
            record['passed'] = False
            record['error'] = {'code': getattr(exc, 'code', 'driver_error'), 'message': str(exc)}
        finally:
            if rt is not None and run is not None:
                try:
                    run = rt.store.load(run['id'])
                    write_json(case_dir / 'run.json', run)
                    write_json(case_dir / 'events.json', rt.store.events(run['id']))
                    record.update(state=run['state'], usage=run['usage'], run_error=run.get('error'),
                                  publication=run['publication'],
                                  method_origins=[run['tasks'][run['root']]['accepted']['origin']] +
                                      [t['accepted']['origin'] for t in run['tasks'].values() if t['parent_call'] and t.get('accepted')],
                                  child_definitions=[t['definition']['hash'] for t in run['tasks'].values() if t['parent_call'] and t['definition']])
                except Exception as exc:
                    record['passed'] = False
                    record['evidence_error'] = {'code': getattr(exc, 'code', 'storage_failure'), 'message': str(exc)}
            record['elapsed_seconds'] = round(time.monotonic() - started, 6)
            write_json(case_dir / 'assessment.json', record)
            result['cases'].append(record)
            result['not_run_cases'] = [f'{m}-{b}' for m, b in CASES[len(result['cases']):]]
            result['status'] = 'running' if record['passed'] else 'failed'
            write_json(output / 'summary.json', result)
        if not record['passed']:
            break
    result['complete'] = len(result['cases']) == len(CASES) and all(r['passed'] for r in result['cases'])
    result['status'] = 'passed' if result['complete'] else 'failed'
    write_json(output / 'summary.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--semantic-case', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--backend', choices=['scripted', 'pi'], default='scripted')
    args = parser.parse_args()
    try:
        result = verify(args.output, semantic_case=args.semantic_case, backend=args.backend)
        print(f"status={result['status']}; cases={len(result['cases'])}/{result['case_count']}; evidence={args.output.absolute()}")
        return 0 if result['complete'] else 1
    except (SopError, OSError) as exc:
        print(f'{getattr(exc, "code", "io_error")}: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
