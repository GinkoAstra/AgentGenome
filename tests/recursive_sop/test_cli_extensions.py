"""CLI composition checks. Pi ports are explicit protocol doubles; no network."""
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sop.cli import main
from sop.skill_authoring import CLAUSES

ROOT=Path(__file__).resolve().parents[2]
FIXTURE=ROOT/'tests/fixtures/first-release'

class Port:
    max_model_calls_per_request=1
    identity={'backend':'scripted','purpose':'cli_protocol_test'}
    def __init__(self,analysis): self.analysis=analysis
    def author(self,context): return deepcopy(self.analysis)
    def review(self,context):
        return {'schema':'skill-review/1','basis_hash':context['basis_hash'],'verdict':'pass','findings':[]}

class CliExtensionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.data=self.root/'data'

    def invoke(self,*args):
        stdout,stderr=io.StringIO(),io.StringIO()
        with redirect_stdout(stdout),redirect_stderr(stderr):
            code=main(['--data',str(self.data),*map(str,args)])
        return code,json.loads(stdout.getvalue() or stderr.getvalue())

    def skill(self,unknown=False):
        clauses=['input','normalize','cross_customer','clean','summary','quality','permissions','fixed',
                 'ambiguity' if unknown else 'rule_highest_revision']
        source=self.root/'SKILL.md'
        source.write_text('\n'.join(CLAUSES[c] for c in clauses)+'\n')
        analysis={'schema':'skill-analysis/1','task':'orders.report','mode':'fixed',
                  'annotations':[{'source_id':'SKILL.md','start_line':n,'end_line':n,'kind':'requirement',
                                  'clauses':[c],'reason':'Hand labelled CLI protocol fixture'} for n,c in enumerate(clauses,1)],
                  'rule':{'status':'unknown' if unknown else 'known','value':None if unknown else 'highest_revision'}}
        return source,analysis

    def test_semantic_author_cli_hands_same_report_and_answer_into_fixed_run(self):
        source,analysis=self.skill()
        port=Port(analysis)
        with patch('sop.agents.PiAuthor',return_value=port),patch('sop.agents.PiSemanticReviewer',return_value=port):
            code,draft=self.invoke('author',source,'--backend','pi')
        self.assertEqual((code,draft['state']),(0,'delivered'),draft)
        code,run=self.invoke('run','--draft',draft['draft_id'],'--input',FIXTURE/'batch-02/input.csv',
                            '--backend','none','--grant',*draft['definition']['capabilities'])
        self.assertEqual((code,run['state']),(0,'succeeded'),run.get('error'))
        self.assertEqual(run['authoring']['report'],draft['report'])
        self.assertEqual(run['authoring']['answers'],draft['answers'])
        self.assertEqual(run['usage']['model_calls'],0)

    def test_semantic_cancel_cli_needs_no_backend_and_blocks_delivery(self):
        source,analysis=self.skill(unknown=True)
        port=Port(analysis)
        with patch('sop.agents.PiAuthor',return_value=port),patch('sop.agents.PiSemanticReviewer',return_value=port):
            code,draft=self.invoke('author',source,'--backend','pi')
        self.assertEqual(draft['state'],'waiting_info')
        code,receipt=self.invoke('draft-cancel',draft['draft_id'],'--message-id','cancel-1','--revision',draft['revision'])
        self.assertEqual(code,0,receipt)
        code,current=self.invoke('draft',draft['draft_id'])
        self.assertEqual((code,current['state']),(1,'stopped'))
        self.assertEqual(current['stop_reason'],'cancelled')
        with patch('sop.agents.PiSemanticReviewer',side_effect=AssertionError('No model on cancelled draft')):
            code,again=self.invoke('draft-cancel',draft['draft_id'],'--message-id','cancel-1','--revision',draft['revision'])
        self.assertEqual(again,receipt)

    def test_semantic_revision_cli_preserves_budget_and_requires_review(self):
        source,analysis=self.skill()
        invalid=deepcopy(analysis)
        invalid['annotations']=invalid['annotations'][:-1]
        port=Port(invalid)
        with patch('sop.agents.PiAuthor',return_value=port),patch('sop.agents.PiSemanticReviewer',return_value=port):
            code,draft=self.invoke('author',source,'--backend','pi')
        self.assertEqual((code,draft['state']),(1,'stopped'))
        proposal=self.root/'analysis.json';proposal.write_text(json.dumps(analysis))
        code,error=self.invoke('draft-revise',draft['draft_id'],'--proposal',proposal,'--message-id','fix-1','--revision',draft['revision'])
        self.assertEqual(code,2,error)
        self.assertEqual(error['error'],'semantic_checker_missing')
        with patch('sop.agents.PiAuthor',return_value=Port(analysis)),patch('sop.agents.PiSemanticReviewer',return_value=Port(analysis)):
            code,revised=self.invoke('draft-revise',draft['draft_id'],'--proposal',proposal,'--message-id','fix-1',
                                     '--revision',draft['revision'],'--backend','pi')
        self.assertEqual((code,revised['state']),(0,'delivered'),revised)
        self.assertGreater(revised['usage']['model_calls'],draft['usage']['model_calls'])
        self.assertEqual(revised['limits'],draft['limits'])

    def test_configured_prepare_parameters_cli_waits_then_runs_exact_revision(self):
        definition=self.root/'configured.json'
        code,created=self.invoke('definition','--mode','prepare-configured','--output',definition)
        self.assertEqual(code,0,created)
        code,waiting=self.invoke('run','--definition',definition,
            '--input',ROOT/'tests/fixtures/input-handoff/source.json','--backend','none',
            '--max-chunk-size',3,'--grant','orders.prepare_configured')
        self.assertEqual((code,waiting['state']),(0,'waiting_info'))
        self.assertEqual(waiting['operations'],{})
        task=waiting['tasks'][waiting['root']]
        columns=['order_id','customer_id','revision','gross_cents','refund_cents']
        values=self.root/'fields.json'
        values.write_text(json.dumps({'column_map':{c:c for c in columns},'chunk_size':2}))
        code,receipt=self.invoke('parameters',waiting['id'],task['id'],'--values',values,
            '--message-id','fields-1','--revision',task['parameter_revision'])
        self.assertEqual(code,0,receipt)
        self.assertTrue(receipt['accepted'])
        code,done=self.invoke('resume',waiting['id'],'--backend','none')
        self.assertEqual((code,done['state']),(0,'succeeded'),done.get('error'))
        operation=next(iter(done['operations'].values()))
        self.assertEqual(operation['parameter_revision'],receipt['parameter_revision'])
        self.assertEqual(operation['inputs']['chunk_size'],2)
        code,duplicate=self.invoke('parameters',waiting['id'],task['id'],'--values',values,
            '--message-id','fields-1','--revision',task['parameter_revision'])
        self.assertEqual(duplicate,receipt)

    def test_json_definition_run_and_export_cli(self):
        definition=self.root/'json-fixed.json'
        code,created=self.invoke('definition','--mode','json-fixed','--output',definition)
        self.assertEqual(code,0,created)
        self.assertTrue(created['check']['passed'])
        body=json.loads(definition.read_text())
        code,run=self.invoke('run','--definition',definition,'--input',ROOT/'tests/fixtures/input-handoff/source.json',
                            '--rule','highest_revision','--backend','none','--grant',*body['capabilities'])
        self.assertEqual((code,run['state']),(0,'succeeded'),run.get('error'))
        code,exports=self.invoke('export',run['id'],'--output',self.root/'out')
        self.assertEqual(code,0,exports)
        self.assertEqual(set(exports),{'prepared','mapping','cleaned','summary','quality'})
        self.assertEqual(Path(exports['cleaned']).read_bytes(),(FIXTURE/'batch-01/expected/cleaned.csv').read_bytes())
        self.assertEqual(Path(exports['mapping']).suffix,'.json')

if __name__=='__main__': unittest.main()
