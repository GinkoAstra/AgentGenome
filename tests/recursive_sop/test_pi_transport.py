"""Real local child-process IPC checks; no model/provider requests."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from sop.agents import PiAgent, _strict_loads
from sop.common import SopError
from sop.pi_transport import invoke_interactive


class PiTransportTests(unittest.TestCase):
    def run_worker(self, body, execute=lambda *args: None, timeout=3):
        script = 'import json,sys,time,os\nrequest=json.loads(sys.stdin.readline())\n' + body
        return invoke_interactive([sys.executable, '-u', '-c', script], {'mode': 'agent', 'context': {}, 'config': {}},
                                  execute, _strict_loads, timeout=timeout, env=os.environ.copy())

    def test_bidirectional_strict_tool_call_then_final_candidate(self):
        calls = []
        def execute(name, inputs):
            calls.append((name, inputs))
            return {'operation': 'op-local', 'outputs': {}, 'reused': False}
        result = self.run_worker('''assert request['interactive'] is True
print(json.dumps({'type':'execute','request_id':'one','capability':'orders.normalize','inputs_json':'{"source_file":{"hash":"a"}}'}),flush=True)
reply=json.loads(sys.stdin.readline())
assert reply=={'type':'tool_result','request_id':'one','ok':True,'result':{'operation':'op-local','outputs':{},'reused':False}}
print(json.dumps({'ok':True,'result':{'kind':'cannot_continue','reason':'test'},'result_payload':'{"kind":"cannot_continue","reason":"test"}','evidence':{}}),flush=True)
''', execute)
        self.assertEqual(calls, [('orders.normalize', {'source_file': {'hash': 'a'}})])
        self.assertTrue(result['ok'])

    def test_invalid_frames_never_call_host(self):
        frames = [[], {'type': 'other'}, {'type': 'execute', 'request_id': 'one', 'capability': 'x', 'inputs_json': '{}', 'extra': True},
                  {'type': 'execute', 'request_id': 'one', 'capability': 'x', 'inputs_json': '{"x":1,"x":2}'},
                  {'type': 'execute', 'request_id': 'one', 'capability': 'x', 'inputs_json': '{"x":NaN}'},
                  {'type': 'execute', 'request_id': 'one', 'capability': 'x', 'inputs_json': '[]'}]
        for frame in frames:
            with self.subTest(frame=frame):
                calls = []
                with self.assertRaises(SopError) as error:
                    self.run_worker('print(' + repr(json.dumps(frame)) + ',flush=True)\n', lambda *args: calls.append(args))
                self.assertEqual(error.exception.code, 'backend_protocol')
                self.assertEqual(calls, [])

    def test_duplicate_frame_identity_cannot_execute_twice(self):
        calls = []
        def execute(*args):
            calls.append(args)
            return {'operation': 'op', 'outputs': {}, 'reused': False}
        with self.assertRaises(SopError) as error:
            self.run_worker('''frame={'type':'execute','request_id':'one','capability':'x','inputs_json':'{}'}
print(json.dumps(frame),flush=True)
sys.stdin.readline()
print(json.dumps(frame),flush=True)
''', execute)
        self.assertEqual(error.exception.code, 'backend_protocol')
        self.assertEqual(len(calls), 1)

    def test_host_rejection_propagates_without_model_retry(self):
        def execute(*args):
            raise SopError('effect_unknown', 'Host has an unresolved operation')
        with self.assertRaises(SopError) as error:
            self.run_worker('''print(json.dumps({'type':'execute','request_id':'one','capability':'x','inputs_json':'{}'}),flush=True)
time.sleep(30)
''', execute)
        self.assertEqual(error.exception.code, 'effect_unknown')

    def test_timeout_kills_worker_process(self):
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory)/'pid'
            with self.assertRaises(SopError) as error:
                self.run_worker(f'open({str(pid_file)!r},"w").write(str(os.getpid()))\ntime.sleep(30)\n', timeout=.15)
            self.assertEqual(error.exception.code, 'backend_timeout')
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pid_file.read_text()), 0)

    def test_python_agent_selects_interactive_bridge_and_keeps_evidence(self):
        candidate = {'kind': 'candidate_result', 'outputs': {}}
        response = {'ok': True, 'result': candidate, 'result_payload': json.dumps(candidate),
                    'evidence': {'model_calls': 1, 'usage': {'input': 2}}}
        callback = lambda *args: None
        agent = PiAgent()
        with patch('sop.pi_transport.invoke_interactive', return_value=response) as bridge:
            result = agent.respond_with_tools({'tool_receipts': []}, callback)
        self.assertEqual(bridge.call_args.args[1]['mode'], 'agent')
        self.assertIs(bridge.call_args.args[2], callback)
        self.assertEqual(result['_usage'], {'input': 2})
        self.assertEqual(agent.calls, 1)


    def test_trusted_disable_tools_uses_noninteractive_handoff_path(self):
        import subprocess
        candidate = {'kind': 'need_subtask', 'task': 'orders.clean', 'inputs': {}, 'reason': 'Explicit child path'}
        response = {'ok': True, 'result': candidate, 'result_payload': json.dumps(candidate), 'evidence': {}}
        agent = PiAgent(enable_tools=False)
        with patch('sop.pi_transport.invoke_interactive', side_effect=AssertionError('must remain disabled')) as bridge, \
             patch('sop.agents.subprocess.run', return_value=subprocess.CompletedProcess([], 0, stdout=json.dumps(response), stderr='')) as plain:
            result = agent.respond_with_tools({'tool_receipts': []}, lambda *args: self.fail('No tool execution'))
        self.assertEqual(result['kind'], 'need_subtask')
        self.assertFalse(bridge.called)
        self.assertEqual(plain.call_count, 1)
        self.assertNotIn('interactive', json.loads(plain.call_args.kwargs['input']))
        for value in ('true', 1, None):
            with self.assertRaises(SopError):
                PiAgent(enable_tools=value)


if __name__ == '__main__':
    unittest.main()
