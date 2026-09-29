#!/usr/bin/env python3
"""Persist first-release parameter preparation/correction evidence; no model calls."""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from sop.authoring import configured_prepare_definition
from sop.capabilities import SOURCE_COLUMNS
from sop.common import SopError, file_digest, read_json, write_json
from sop.service import application

SOURCE=ROOT/'tests/fixtures/input-handoff/source.json'
EXPECTED=ROOT/'tests/fixtures/input-handoff/expected.csv'


def verify(output):
    output=Path(output).absolute()
    if output.exists() and any(output.iterdir()):
        raise SopError('output_exists','evidence directory must be new or empty')
    output.mkdir(parents=True,exist_ok=True)
    records=[]
    for case_name in ('correct-then-execute','invalid-without-branch','correction-limit'):
        case=output/case_name;case.mkdir()
        definition=configured_prepare_definition()
        if case_name!='invalid-without-branch':
            definition['recovery']=[{'id':'correct-size','phase':'parameter_validation','code':'invalid_value',
                'action':'request_parameter_proposal','fields':['chunk_size'],'max_attempts':1}]
        write_json(case/'definition.json',definition)
        rt=application(case/'.data')
        run=rt.start(definition,{'source_file':SOURCE},capabilities=definition['capabilities'],
                     parameter_policy={'max_chunk_size':3})
        run=rt.advance(run['id'])
        assert run['state']=='waiting_info' and not run['operations']
        write_json(case/'waiting.json',run)
        task_id=run['root'];receipts=[]
        def propose(values,message):
            current=rt.store.load(run['id'])
            response=rt.propose_parameters(run['id'],task_id,values,message_id=message,
                                          revision=current['tasks'][task_id]['parameter_revision'])
            receipts.append({'message_id':message,'patch':values,'response':response})
            return response
        mapping={name:name for name in SOURCE_COLUMNS}
        assert propose({'column_map':mapping},'map')['accepted']
        bad=propose({'chunk_size':0},'bad-size')
        assert bad['accepted'] is False
        write_json(case/'after-invalid.json',rt.store.load(run['id']))
        if case_name=='correct-then-execute':
            state=rt.store.load(run['id'])
            unchanged=state['tasks'][task_id]['parameter_revision']
            injected=propose({'chunk_size':2,'template_ref':'unregistered-replacement'},'bad-envelope')
            assert not injected['accepted'] and injected['parameter_revision']==unchanged
            before=rt.store.load(run['id'])
            valid=propose({'chunk_size':2},'good-size')
            assert valid['accepted'] and valid['missing']==[]
            assert rt.propose_parameters(run['id'],task_id,{'chunk_size':2},message_id='good-size',
                                        revision=before['tasks'][task_id]['parameter_revision'])==valid
            rt=application(case/'.data')
            done=rt.advance(run['id'])
            expected_state='succeeded'
            checks={'expected_state':done['state']==expected_state,'one_conversion':len(done['operations'])==1,
                    'one_correction':len(done['recoveries'])==1,'no_model_calls':done['usage']['model_calls']==0}
            if done['state']=='succeeded':
                outputs=done['tasks'][task_id]['outputs']
                checks['prepared_exact']=rt.store.path(outputs['prepared']).read_bytes()==EXPECTED.read_bytes()
                for name,ref in outputs.items():
                    (case/(name+('.json' if name=='mapping' else '.csv'))).write_bytes(rt.store.path(ref).read_bytes())
        else:
            if case_name=='correction-limit':
                rt=application(case/'.data')
                assert propose({'chunk_size':-1},'bad-size-again')['accepted'] is False
            done=rt.advance(run['id']);expected_state='failed'
            checks={'expected_state':done['state']==expected_state,'zero_conversion':not done['operations'],
                    'no_model_calls':done['usage']['model_calls']==0}
        write_json(case/'receipts.json',receipts)
        write_json(case/'run.json',done)
        write_json(case/'events.json',rt.store.events(run['id']))
        records.append({'case':case_name,'run_id':done['id'],'state':done['state'],'expected_state':expected_state,
                        'checks':checks,'passed':all(checks.values()),'error':done.get('error'),
                        'usage':done['usage'],'publication':done['publication']})
    result={'schema':'recursive-sop-parameter-evidence/1','created_at':datetime.now(timezone.utc).isoformat(),
            'backend':'none','cases':records,'complete':all(case['passed'] for case in records),
            'model_validation':'not performed; deterministic data proposals','algorithm_benefit':'not measured',
            'implementation_hashes':{str(p.relative_to(ROOT)):file_digest(p) for p in sorted((ROOT/'sop').glob('*')) if p.is_file()}}
    write_json(output/'summary.json',result)
    return result


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);args=parser.parse_args()
    try:
        result=verify(args.output)
        print(f"{len(result['cases'])} cases; complete={result['complete']}; evidence={Path(args.output).absolute()}")
        return 0 if result['complete'] else 1
    except (SopError,OSError) as exc:
        print(f'{getattr(exc,"code","io_error")}: {exc}',file=sys.stderr);return 2

if __name__=='__main__':raise SystemExit(main())
