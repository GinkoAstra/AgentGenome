"""Shipped worker + installed Pi + synthetic HTTP stream: zero network/model evidence."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from sop.agents import PiAgent


class PiWorkerIpcTests(unittest.TestCase):
    def test_full_worker_ipc_with_installed_pi_and_synthetic_http_stream(self):
        if not shutil.which('node') or not shutil.which('pi'):
            self.skipTest('Supported Pi installation is required for this installed SDK test')
        source = {'hash': '1'*64, 'bytes': 20, 'kind': 'input'}
        normalized = {'hash': '2'*64, 'bytes': 20, 'kind': 'artifact'}
        cleaned = {'hash': '3'*64, 'bytes': 30, 'kind': 'artifact'}
        calls = []
        def execute(name, inputs):
            calls.append((name, inputs))
            return {'operation': 'op-'+str(len(calls)), 'reused': False,
                    'outputs': {'normalized': normalized} if name == 'orders.normalize' else {'cleaned': cleaned}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'settings.json').write_text(json.dumps({'defaultProvider': 'offline-test', 'defaultModel': 'protocol-model'}))
            (root/'auth.json').write_text('{}')
            (root/'models.json').write_text(json.dumps({'providers': {'offline-test': {
                'baseUrl': 'https://offline-protocol.invalid', 'api': 'anthropic-messages', 'apiKey': 'test-only',
                'headers': {'x-sop-test': 'scoped-test-header'},
                'models': [{'id': 'protocol-model', 'name': 'Synthetic HTTP transport', 'reasoning': False,
                            'input': ['text'], 'contextWindow': 65536, 'maxTokens': 512,
                            'cost': {'input': 0, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0}}]}}}))
            preloader = root/'synthetic-fetch.mjs'
            proposals = [
                {'name': 'capability', 'input': {'capability': 'orders.normalize', 'inputs_json': json.dumps({'source_file': source})}},
                {'name': 'capability', 'input': {'capability': 'orders.deduplicate', 'inputs_json': json.dumps({'normalized': normalized, 'rule': 'highest_revision'})}},
                {'name': 'handoff', 'input': {'payload': json.dumps({'kind': 'candidate_result', 'outputs': {'cleaned': cleaned}})}},
            ]
            assertion_log = root/'transport-assertions.txt'
            preloader.write_text('import {writeFileSync} from "node:fs"; const assertionPath = '+json.dumps(str(assertion_log))+
                                 '; const fail = message => { writeFileSync(assertionPath, message); throw new Error(message); }; const proposals = '+json.dumps(proposals)+r''';
let index = 0;
globalThis.fetch = async (input, init) => {
  const request = input instanceof Request ? input : new Request(input, init);
  const body = await request.clone().json();
  if (new URL(request.url).hostname !== 'offline-protocol.invalid') fail('Unexpected network destination');
  if (request.headers.get('x-api-key') !== 'test-only') fail('Configured test authentication missing');
  if (request.headers.get('x-sop-test') !== 'scoped-test-header') fail('Configured scoped header missing');
  if (body.model !== 'protocol-model') fail('Model selection changed');
  if (body.max_tokens !== 256) fail('Token limit changed');
  if (body.temperature !== 0) fail('Temperature limit changed');
  if (!JSON.stringify(body.system).includes('Failure does not enlarge permissions'))
    fail('Constrained system protocol missing from serialized request');
  if (JSON.stringify(body.tools.map(tool => tool.name)) !== JSON.stringify(['context', 'handoff', 'capability']))
    fail('Serialized request exposed unexpected tools');
  const call = proposals[index++];
  if (!call) throw new Error('Unexpected extra scripted model call');
  const frames = [
    {type:'message_start',message:{id:'message-'+index,type:'message',role:'assistant',model:'protocol-model',content:[],stop_reason:null,stop_sequence:null,usage:{input_tokens:1,output_tokens:0}}},
    {type:'content_block_start',index:0,content_block:{type:'tool_use',id:'tool-'+index,name:call.name,input:{}}},
    {type:'content_block_delta',index:0,delta:{type:'input_json_delta',partial_json:JSON.stringify(call.input)}},
    {type:'content_block_stop',index:0},
    {type:'message_delta',delta:{stop_reason:'tool_use',stop_sequence:null},usage:{output_tokens:1}},
    {type:'message_stop'},
  ];
  return new Response(frames.map(frame=>'event: '+frame.type+'\ndata: '+JSON.stringify(frame)+'\n\n').join(''),
    {status:200,headers:{'content-type':'text/event-stream'}});
};
''')
            context = {'task': 'orders.clean', 'inputs': {'source_file': source, 'rule': 'highest_revision'},
                       'contract': {'capabilities': ['orders.normalize', 'orders.deduplicate']},
                       'catalog': {'orders.normalize': {}, 'orders.deduplicate': {}}, 'tool_receipts': []}
            with patch.dict(os.environ, {'PI_CODING_AGENT_DIR': str(root), 'NODE_OPTIONS': '--import='+str(preloader),
                                         'SOP_PI_PROVIDER': 'offline-test', 'SOP_PI_MODEL': 'protocol-model'}):
                try:
                    result = PiAgent(timeout=10, max_turns=3, max_tokens=256).respond_with_tools(context, execute)
                except Exception:
                    if assertion_log.exists():
                        self.fail(assertion_log.read_text())
                    raise
        self.assertEqual([call[0] for call in calls], ['orders.normalize', 'orders.deduplicate'])
        self.assertEqual(calls[1][1], {'normalized': normalized, 'rule': 'highest_revision'})
        self.assertEqual(result['outputs'], {'cleaned': cleaned})
        self.assertEqual(result['_trace']['model_calls'], 3)
        self.assertEqual(result['_trace']['business_tools'], ['orders.normalize', 'orders.deduplicate'])


if __name__ == '__main__':
    unittest.main()
