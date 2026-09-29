#!/usr/bin/env python3
"""Repeatable JSON preparation and task-internal tool evidence (no language-model claims)."""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sop.agents import PiAgent, ScriptedAgent, ScriptedPlanner
from sop.authoring import Library, clean_definition, json_report_definition, prepare_definition, root_definition
from sop.capabilities import Registry
from sop.common import SopError, file_digest, read_json, write_json
from sop.service import application
from scripts.verify_recursive_sop import _archive_run, _exception, _parent_checks, _progress

FIXTURE = ROOT / 'tests/fixtures/first-release'
JSON_INPUT = ROOT / 'tests/fixtures/input-handoff/source.json'


class ToolProtocolAgent:
    """Explicit protocol double: selects known operations; no semantic/model evidence."""
    max_model_calls_per_request = 1
    identity = {'backend':'scripted','purpose':'tool_protocol_test_double'}

    def respond_with_tools(self, context, execute):
        if not context['inputs'].get('rule'):
            return {'kind':'need_info','field':'rule','reason':'Explicit retention rule required'}
        normalized = execute('orders.normalize', {'source_file':context['inputs']['source_file']})
        # Exercise persisted identical-request replay suppression within this session.
        repeated = execute('orders.normalize', {'source_file':context['inputs']['source_file']})
        if not repeated['reused'] or repeated['operation'] != normalized['operation']:
            raise SopError('receipt_reuse_failed','identical committed request was dispatched again')
        cleaned = execute('orders.deduplicate', {'normalized':normalized['outputs']['normalized'],
                                               'rule':context['inputs']['rule']})
        return {'kind':'candidate_result','outputs':cleaned['outputs']}


def _extension_checks(run, events, mode):
    checks = _parent_checks(run)
    children = [t for t in run['tasks'].values() if t['parent_call']]
    tools = [op for op in run['operations'].values() if op.get('origin') == 'agent']
    planner = any(e['kind'] == 'planner_dispatched' for e in events)
    if mode == 'direct':
        checks.update(no_child_graph=not run['calls'], no_planner=not planner,
                      direct_tools_committed=bool(tools) and all(op['status']=='committed' for op in tools),
                      cleaning_tools_used={'orders.normalize','orders.deduplicate'} <= {op['capability'] for op in tools},
                      model_dispatched=any(e['kind']=='agent_dispatched' for e in events))
    else:
        prepare = [t for t in children if t['task']=='orders.prepare']
        checks.update(preparation_child=bool(prepare),
                      child_returns_consumed=bool(run['calls']) and all(c['consumed'] for c in run['calls'].values()),
                      children_verified=bool(children) and all((t.get('verification') or {}).get('passed') is True for t in children))
        clean = [t for t in children if t['task']=='orders.clean']
        if mode == 'fixed':
            checks.update(no_clean_child=not clean, no_planner=not planner, no_model_calls=run['usage']['model_calls']==0)
        else:
            checks['clean_method_origin'] = bool(clean) and all((t.get('accepted') or {}).get('origin')==mode for t in clean)
            checks['planner_policy'] = planner if mode=='generated' else not planner
    return checks


def verify(output, backend='scripted'):
    if backend not in ('scripted','pi'):
        raise SopError('evaluation_backend','backend must be scripted or pi')
    output=Path(output).absolute()
    if output.exists() and any(output.iterdir()):
        raise SopError('output_exists','use a new empty evidence directory')
    output.mkdir(parents=True,exist_ok=True)
    registry=Registry()
    configurations=[('json-existing','json','existing'),('json-generated','json','generated'),
                    ('json-fixed','json','fixed'),('direct-batch-01','csv','direct'),('direct-batch-02','csv','direct')]
    if backend=='pi':
        configurations=[item for item in configurations if item[2]=='direct']
    names=[item[0] for item in configurations]
    result={'schema':'recursive-sop-extension-evidence/1','created_at':datetime.now(timezone.utc).isoformat(),
            'backend':backend,'cases':[], 'case_count':len(names),
            'model_validation':'real requests, direct tools only' if backend=='pi' else 'not performed; scripted protocol ports',
            'algorithm_benefit':'not measured',
            'implementation_hashes':{str(p.relative_to(ROOT)):file_digest(p) for p in sorted((ROOT/'sop').glob('*')) if p.is_file()},
            'driver_sha256':file_digest(Path(__file__)),
            'shared_driver_sha256':file_digest(ROOT/'scripts/verify_recursive_sop.py')}
    _progress(output,result,names)
    for name,source_format,mode in configurations:
        case=output/name
        case.mkdir()
        rt=run=None
        comparisons,checks,validation_errors={},{},[]
        error=None
        try:
            definitions=[prepare_definition(),clean_definition('first')]
            if mode=='existing': definitions.append(clean_definition())
            agent=(PiAgent() if backend=='pi' else ToolProtocolAgent()) if mode=='direct' else ScriptedAgent()
            rt=application(case/'.data',registry=registry,library=Library([] if backend=='pi' else definitions),
                           agent=agent,planner=ScriptedPlanner() if backend=='scripted' else None)
            batch='batch-02' if name.endswith('02') else 'batch-01'
            definition=json_report_definition('fixed' if mode=='fixed' else 'pi') if source_format=='json' else root_definition()
            source=JSON_INPUT if source_format=='json' else FIXTURE/batch/'input.csv'
            write_json(case/'definition.json',definition)
            run=rt.start(definition,{'source_file':source},capabilities=definition['capabilities'])
            run=rt.advance(run['id'])
            if run['state']=='waiting_info':
                write_json(case/'waiting.json',run)
                question=next(q for q in run['questions'].values() if q['state']=='open')
                rt.answer(run['id'],question['request_id'],'highest_revision',message_id='accepted-rule',revision=run['revision'])
                run=rt.advance(run['id'])
            if run['state']=='succeeded':
                for key,ref in run['tasks'][run['root']]['outputs'].items():
                    path=rt.store.path(ref)
                    suffix='.json' if key in ('quality','mapping') else '.csv'
                    (case/(key+suffix)).write_bytes(path.read_bytes())
                    if key not in ('prepared','mapping'):
                        expected=FIXTURE/batch/'expected'/(key+suffix)
                        comparisons[key]=read_json(path)==read_json(expected) if suffix=='.json' else path.read_bytes()==expected.read_bytes()
                if set(comparisons)!={'cleaned','summary','quality'} or not all(comparisons.values()):
                    validation_errors.append('expected_mismatch')
                checks=_extension_checks(run,rt.store.events(run['id']),mode)
                validation_errors.extend('path_check:'+key for key,passed in checks.items() if not passed)
            else:
                validation_errors.append('unexpected_state:'+run['state'])
        except Exception as exc:
            error=_exception(exc)
            validation_errors.append('driver_failure')
        run,events,archive_errors=_archive_run(rt,run,case)
        if archive_errors: validation_errors.append('evidence_incomplete')
        record={'case':name,'state':run['state'] if run else 'driver_failed','run_id':run['id'] if run else None,
                'expected_comparison':comparisons,
                'method_origins':[t['accepted']['origin'] for t in run['tasks'].values() if t.get('accepted')] if run else [],
                'agent_tool_operations':[op['id'] for op in run['operations'].values() if op.get('origin')=='agent'] if run else [],
                'publication':run['publication'] if run else None,'error':error or (run.get('error') if run else None),
                'usage':run['usage'] if run else None,'path_checks':checks,'validation_errors':validation_errors,
                'archive_errors':archive_errors,'passed':run is not None and run['state']=='succeeded' and not validation_errors}
        write_json(case/'assessment.json',record)
        result['cases'].append(record)
        _progress(output,result,names,finished=not record['passed'])
        if not record['passed']: break
    _progress(output,result,names,finished=True)
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
