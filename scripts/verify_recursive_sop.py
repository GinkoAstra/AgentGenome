#!/usr/bin/env python3
"""Repeatable release evidence. Expected files and answers stay in this driver.

Usage: python3 scripts/verify_recursive_sop.py --output /tmp/sop-evidence
Real backend: add --backend pi. No scripted fallback is performed.
"""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sop.agents import PiAgent, PiPlanner, ScriptedAgent, ScriptedPlanner
from sop.authoring import Authoring, Library, clean_definition, root_definition
from sop.capabilities import Registry
from sop.common import SopError, digest, file_digest, read_json, write_json
from sop.service import application

FIXTURE = ROOT / 'tests/fixtures/first-release'


def _exception(exc):
    return {'code': getattr(exc, 'code', 'io_error' if isinstance(exc, OSError) else 'driver_error'),
            'message': str(exc)}


def _archive_run(runtime, run, directory):
    """Salvage the durable state even if advance or output comparison raised."""
    errors, events = [], []
    if runtime is not None and run is not None:
        try:
            run = runtime.store.load(run['id'])
        except Exception as exc:
            errors.append({'stage': 'load_run', **_exception(exc)})
        try:
            events = runtime.store.events(run['id'])
        except Exception as exc:
            errors.append({'stage': 'load_events', **_exception(exc)})
    for name, value in (('run.json', run), ('events.json', events)):
        try:
            write_json(directory/name, value)
        except Exception as exc:
            errors.append({'stage': name, **_exception(exc)})
    return run, events, errors


def _progress(output, result, names, *, finished=False):
    result['not_run_cases'] = names[len(result['cases']):]
    result['complete'] = len(result['cases']) == len(names) and all(c['passed'] for c in result['cases'])
    result['status'] = ('passed' if result['complete'] else 'failed') if finished else 'running'
    write_json(output/'summary.json', result)


def _parent_checks(run):
    root = run['tasks'][run['root']]
    return {'parent_verified': (root.get('verification') or {}).get('passed') is True,
            'candidate_unpublished': run.get('publication') == 'unpublished'}


def _recursive_checks(run, events, path, batch, saved_candidate):
    checks = _parent_checks(run)
    calls = [c for c in run['calls'].values() if c['parent'] == run['root']]
    children = [run['tasks'][c['child']] for c in calls]
    planner_events = [e for e in events if e['kind'] == 'planner_dispatched']
    if path == 'fixed':
        checks.update(no_child_graph=not run['calls'], no_planner=not planner_events,
                      no_model_calls=run['usage']['model_calls'] == 0)
        return checks, children
    expected_origin = 'candidate_reuse' if path == 'generated' and batch == 'batch-02' else path
    checks.update(child_graph_called=bool(children),
                  child_contract=bool(children) and all(c['task'] == 'orders.clean' for c in children),
                  child_method_origin=bool(children) and all((c.get('accepted') or {}).get('origin') == expected_origin for c in children),
                  child_return_consumed=bool(calls) and all(c.get('consumed') for c in calls),
                  child_verified=bool(children) and all((c.get('verification') or {}).get('passed') is True for c in children),
                  model_dispatched=any(e['kind'] == 'agent_dispatched' for e in events),
                  no_direct_tools=not any(op.get('origin') == 'agent' for op in run['operations'].values()))
    if path == 'generated' and batch == 'batch-01':
        checks['current_planner_generated'] = bool(planner_events)
    else:
        checks['no_planner'] = not planner_events
    if path == 'generated' and batch == 'batch-02':
        checks['same_saved_candidate'] = saved_candidate is not None and bool(children) and all(
            c['definition']['hash'] == digest(saved_candidate) for c in children)
    for child in children:
        accepted = [e['seq'] for e in events if e['kind'] == 'method_accepted' and e['data']['task'] == child['id']
                    and e['data']['definition'] == child['definition']]
        intents = [e['seq'] for e in events if e['kind'] == 'operation_intent' and e['data']['task'] == child['id']]
        checks['accepted_before_execution:'+child['id']] = bool(accepted) and all(accepted[0] < n for n in intents)
        checks['definition_saved:'+child['id']] = child['definition']['hash'] in run['definitions']
    return checks, children


def verify(output, backend='scripted'):
    if backend not in ('scripted', 'pi'):
        raise SopError('evaluation_backend', 'backend must be scripted or pi')
    output = Path(output).absolute()
    if output.exists() and any(output.iterdir()):
        raise SopError('output_exists', 'evidence directory must be empty; preserve earlier evidence')
    output.mkdir(parents=True, exist_ok=True)
    registry = Registry()
    configurations = [(path, batch) for path in ('existing', 'generated', 'fixed') for batch in ('batch-01', 'batch-02')]
    names = [path+'-'+batch for path, batch in configurations]
    result = {'schema':'recursive-sop-evidence/1','created_at':datetime.now(timezone.utc).isoformat(),
              'backend':backend,'program_tests':'see tests.txt; this command records integration cases',
              'model_validation':'real requests' if backend=='pi' else 'not performed; deterministic protocol substitutes',
              'algorithm_benefit':'not measured; timings are not a controlled algorithm comparison',
              'reserved_model_calls_note':'usage.model_calls reserves max provider requests per adapter call; actual calls are in model_usage evidence',
              'cases':[], 'case_count':len(names),
              'implementation_hashes':{str(p.relative_to(ROOT)):file_digest(p) for p in sorted((ROOT/'sop').glob('*')) if p.is_file()},
              'driver_sha256':file_digest(Path(__file__))}
    _progress(output, result, names)
    saved_candidate = None
    for path, batch in configurations:
        case_dir = output/(path+'-'+batch)
        case_dir.mkdir()
        started = time.monotonic()
        rt = run = None
        comparisons, checks, validation_errors = {}, {}, []
        error = None
        try:
            agent, planner = (PiAgent(enable_tools=False), PiPlanner()) if backend == 'pi' else (ScriptedAgent(), ScriptedPlanner())
            definitions = [clean_definition('first')]
            if path == 'existing':
                definitions.append(clean_definition())
            library = Library(definitions)
            if path == 'generated' and batch == 'batch-02' and saved_candidate is not None:
                library.add(saved_candidate, origin='candidate_reuse')
            rt = application(case_dir/'.data', registry=registry, library=library,
                             agent=None if path=='fixed' else agent, planner=None if path=='fixed' else planner)
            # Fixture authoring and runtime are separate checks; this does not
            # claim that a real semantic-authoring candidate was executed here.
            author = Authoring(case_dir/'.data', registry)
            draft = author.submit(FIXTURE/'source-request.zh.md')
            write_json(case_dir/'draft-before-answer.json', draft)
            if draft['state'] == 'waiting_info':
                q = next(q for q in draft['questions'] if q['status']=='open')
                draft = author.answer(draft['draft_id'], q['request_id'], read_json(FIXTURE/'answer.json'),
                                      'author-answer', draft['revision'])
            write_json(case_dir/'draft.json', draft)
            if draft['state'] != 'delivered':
                raise SopError('authoring_failed', 'fixture Skill was not delivered')
            definition = root_definition('fixed' if path=='fixed' else 'pi')
            write_json(case_dir/'definition.json', definition)
            run = rt.start(definition, {'source_file': FIXTURE/batch/'input.csv'}, capabilities=definition['capabilities'])
            run = rt.advance(run['id'])
            if run['state'] == 'waiting_info':
                write_json(case_dir/'waiting.json', run)
                q = next(q for q in run['questions'].values() if q['state']=='open')
                answer = read_json(FIXTURE/'answer.json')['answer']['keep']
                rt.answer(run['id'], q['request_id'], answer, message_id='fixture-answer', revision=run['revision'])
                rt = application(case_dir/'.data', registry=registry, library=library,
                                 agent=None if path=='fixed' else agent, planner=None if path=='fixed' else planner)
                run = rt.advance(run['id'])
            if run['state'] == 'succeeded':
                for name, ref in run['tasks'][run['root']]['outputs'].items():
                    ext = '.json' if name=='quality' else '.csv'
                    actual = rt.store.path(ref)
                    expected = FIXTURE/batch/'expected'/(name+ext)
                    (case_dir/(name+ext)).write_bytes(actual.read_bytes())
                    comparisons[name] = read_json(actual)==read_json(expected) if ext=='.json' else actual.read_bytes()==expected.read_bytes()
                if set(comparisons) != {'cleaned','summary','quality'} or not all(comparisons.values()):
                    validation_errors.append('expected_mismatch')
                checks, children = _recursive_checks(run, rt.store.events(run['id']), path, batch, saved_candidate)
                validation_errors.extend('path_check:'+name for name, passed in checks.items() if not passed)
                if path=='generated' and batch=='batch-01' and not validation_errors:
                    saved_candidate = rt.store.json(children[0]['definition'])
                    write_json(output/'generated-candidate.json', saved_candidate)
            else:
                validation_errors.append('unexpected_state:'+run['state'])
        except Exception as exc:
            error = _exception(exc)
            validation_errors.append('driver_failure')
        run, events, archive_errors = _archive_run(rt, run, case_dir)
        if archive_errors:
            validation_errors.append('evidence_incomplete')
        record = {'path':path,'batch':batch,'run_id':run['id'] if run else None,
                  'state':run['state'] if run else 'driver_failed', 'expected_comparison':comparisons,
                  'error':error or (run.get('error') if run else None),'usage':run['usage'] if run else None,
                  'elapsed_seconds':round(time.monotonic()-started,6),
                  'method_origins':[t['accepted']['origin'] for t in run['tasks'].values() if t.get('accepted')] if run else [],
                  'publication':run['publication'] if run else None, 'evidence':str(case_dir.relative_to(output)),
                  'path_checks':checks, 'validation_errors':validation_errors, 'archive_errors':archive_errors,
                  'passed':run is not None and run['state']=='succeeded' and not validation_errors}
        write_json(case_dir/'assessment.json',record)
        result['cases'].append(record)
        _progress(output, result, names, finished=not record['passed'])
        if not record['passed']:
            break
    _progress(output, result, names, finished=True)
    return result


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',required=True)
    parser.add_argument('--backend',choices=['scripted','pi'],default='scripted')
    args=parser.parse_args()
    try:
        result=verify(args.output,args.backend)
        print(f"{len(result['cases'])} cases; complete={result['complete']}; evidence={Path(args.output).absolute()}")
        return 0 if result['complete'] else 1
    except (SopError,OSError) as exc:
        print(f'{getattr(exc,"code","io_error")}: {exc}',file=sys.stderr)
        return 2

if __name__=='__main__':
    raise SystemExit(main())
