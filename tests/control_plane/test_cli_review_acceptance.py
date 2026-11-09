"""Codex independent regressions from actual CLI adapter review findings."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from node_runtime.control_plane.backends.base import BackendError, WorkerSpec
from node_runtime.control_plane.backends.cli import CliBackend
from node_runtime.control_plane.backends.cli_protocol import QoderProtocol, ClaudeProtocol
from node_runtime.control_plane.backends.worker_mcp import BUDGET_FORMAT, CONTEXT_FORMAT
from node_runtime.control_plane.backends.spawn import process_start
from node_runtime.control_plane.tools import TOOL_SPECS
from test_worker_http_p2 import _Service

ROOT = Path(__file__).resolve().parents[2]
BRIDGE = ROOT / 'node_runtime/control_plane/backends/worker_mcp.py'


def test_native_cli_zero_tools_still_loads_only_explicit_empty_mcp(tmp_path):
    for cls in (QoderProtocol, ClaudeProtocol):
        protocol = cls(executable=sys.executable)
        document = protocol.mcp_config(None)
        assert json.loads(document) == {'mcpServers': {}}
        config = tmp_path / 'mcp.json'
        config.write_text(document)
        argv = protocol.argv(workspace=tmp_path, session_id=None, resume=False,
                             exposed=[], mcp_config_path=config,
                             mcp_server=None, settings_path=None)
        assert '--strict-mcp-config' in argv
        assert argv[argv.index('--tools') + 1] == ''


def test_cli_execution_binding_cannot_be_reused_for_other_node_or_run(tmp_path):
    backend = CliBackend(tmp_path, backend='codex', executable=sys.executable)
    base = dict(worker_id='w', node_id='n', run_id='r', epoch=1, intent_seq=1,
                backend_ref='codex', agent_ref='a', desired='active',
                tool_profile={'backend_tools': [], 'tools': []})
    spec = WorkerSpec(**base)
    backend.start(spec, spec.execution_key)
    for changes in ({'run_id': 'other'}, {'node_id': 'other'}, {'backend_ref': 'qoder'}):
        with pytest.raises(BackendError):
            backend.start(WorkerSpec(**{**base, **changes}), spec.execution_key)
    with pytest.raises(BackendError):
        backend.start(spec, ('different-worker', 1))


def test_native_session_reference_does_not_claim_frozen_checkpoint(tmp_path):
    backend = CliBackend(tmp_path, backend='codex', executable=sys.executable)
    assert backend.capabilities().checkpoint is False


def test_cli_stop_cleans_child_even_when_leader_exits_first(tmp_path):
    """Exercise real process group, not a mocked Popen.poll result."""
    child_path = tmp_path / 'child.json'
    child = (
        'import os,signal,time,json;from pathlib import Path;'
        'signal.signal(signal.SIGTERM,signal.SIG_IGN);'
        f'Path({str(child_path)!r}).write_text(json.dumps({{"pid":os.getpid()}}));'
        'time.sleep(30)'
    )
    parent = f'import subprocess,sys,time;subprocess.Popen([sys.executable,"-c",{child!r}]);time.sleep(30)'
    proc = subprocess.Popen([sys.executable, '-c', parent], start_new_session=True)
    pid = None
    try:
        deadline = time.monotonic() + 5
        while not child_path.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        pid = json.loads(child_path.read_text())['pid']
        started = process_start(pid)
        CliBackend._kill(proc, .15)
        deadline = time.monotonic() + 2
        while process_start(pid) == started and time.monotonic() < deadline:
            time.sleep(.02)
        assert process_start(pid) != started, 'MCP child remains after stop returned'
    finally:
        try: os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError: pass
        proc.wait(timeout=5)


def test_real_mcp_upload_has_valid_structured_output_and_candidate_is_not_acceptance(tmp_path):
    service = _Service(tmp_path)
    workspace = tmp_path / 'worker'
    workspace.mkdir()
    budget = tmp_path / 'budget.json'
    budget.write_text(json.dumps({'format': BUDGET_FORMAT, 'caps': {'tool_calls': 10},
                                 'prior': {'tool_calls': 0}, 'counts': {'tool_calls': 0}}))
    context = tmp_path / 'tool-context.json'
    context.write_text(json.dumps({'format': CONTEXT_FORMAT, 'workspace': str(workspace),
        'endpoint': service.server.endpoint, 'token': service.delivery['token'],
        **service.binding, 'workspace': str(workspace),
        'backend_tools': ['read_file', 'write_file'],
        'tools': [{'name': name, 'parameters': dict(TOOL_SPECS[name].input_schema)}
                  for name in ('node.publish_artifact', 'node.submit_candidate')]}))
    os.chmod(context, 0o600)
    env = {**os.environ, 'AGENTGENOME_TOOL_CONTEXT': str(context),
           'AGENTGENOME_BUDGET_FILE': str(budget),
           'AGENTGENOME_TOOL_RECEIPT': str(tmp_path / 'receipts.jsonl')}

    async def exercise():
        params = StdioServerParameters(command=sys.executable, args=[str(BRIDGE)], env=env)
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as client:
                await client.initialize()
                tools = await client.list_tools()
                assert {t.name for t in tools.tools} == {
                    'read_file', 'write_file', 'node.publish_artifact', 'node.submit_candidate'}
                written = await client.call_tool('write_file', {'path': 'new/result.json', 'text': '[2]'})
                assert not written.is_error, written
                assert (workspace / 'new/result.json').read_text() == '[2]'
                # MCP SDK validates output schema here; direct Bridge.call would miss it.
                uploaded = await client.call_tool('node.publish_artifact', {
                    'path': 'new/result.json', 'media_type': 'application/json'})
                assert not uploaded.is_error, uploaded
                ref = uploaded.structured_content
                assert ref['id'].startswith('sha256:') and ref['size'] == 3
                submitted = await client.call_tool('node.submit_candidate', {
                    'candidate_id': 'independent-candidate', 'round': 1, 'output_refs': [ref]})
                assert not submitted.is_error, submitted
                return ref
    try:
        anyio.run(exercise)
        resources = service.client.call('list_resources', {'run_id': service.run_id})
        assert len(resources['candidates']) == 1
        node = next(x for x in resources['nodes'] if x['node_id'] == service.node['node_id'])
        assert node['state'] != 'succeeded'
    finally:
        service.close()


def test_observe_cannot_finish_before_watcher_has_cleaned_mcp_group(tmp_path):
    """Rapid polling must not bypass the watcher's process-group cleanup."""
    child_path = tmp_path / 'orphan-child.json'
    child = (
        'import os,signal,time,json;from pathlib import Path;'
        'signal.signal(signal.SIGTERM,signal.SIG_IGN);'
        f'Path({str(child_path)!r}).write_text(json.dumps({{"pid":os.getpid()}}));'
        'time.sleep(30)'
    )
    executable = tmp_path / 'cli-stub'
    executable.write_text('#!' + sys.executable + '\n' +
        'import subprocess,sys,time,json\nfrom pathlib import Path\n' +
        f'subprocess.Popen([sys.executable,"-c",{child!r}])\n' +
        f'while not Path({str(child_path)!r}).exists():time.sleep(.01)\n' +
        'print(json.dumps({"type":"thread.started","thread_id":"observed-session"}),flush=True)\n' +
        'print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"DONE"}}),flush=True)\n' +
        'print(json.dumps({"type":"turn.completed","usage":{}}),flush=True)\n')
    executable.chmod(0o700)
    backend = CliBackend(tmp_path / 'data', backend='codex', executable=str(executable),
                         grace_seconds=2, max_wall_seconds=10)
    spec = WorkerSpec(worker_id='w', node_id='n', run_id='r', epoch=1, intent_seq=1,
                     backend_ref='codex', agent_ref='a', desired='active',
                     tool_profile={'backend_tools': [], 'tools': []})
    handle = backend.start(spec, spec.execution_key)
    pid = started = None
    try:
        backend.deliver(handle, {'brief': 'finish one turn'}, 'd1')
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if child_path.exists() and pid is None:
                pid = json.loads(child_path.read_text())['pid']
                started = process_start(pid)
            obs = backend.observe(handle)
            if obs.observed in ('idle', 'stopped', 'failed'):
                assert pid is not None, 'child execution was not witnessed'
                assert process_start(pid) != started, 'observation released slot while MCP child still runs'
                return
            time.sleep(.01)
        pytest.fail('watcher never confirmed the cleaned execution')
    finally:
        backend.stop(handle, 'cleanup')
        if pid and process_start(pid) == started:
            try: os.kill(pid, signal.SIGKILL)
            except ProcessLookupError: pass


@pytest.mark.parametrize("delivery_state", ["running", "failed"])
def test_successful_mcp_anchored_stop_is_observable_without_leader_record(tmp_path, monkeypatch, delivery_state):
    """A verified bridge can prove cleanup when the launch record is missing."""
    backend = CliBackend(tmp_path, backend='codex', executable=sys.executable)
    spec = WorkerSpec(worker_id='w', node_id='n', run_id='r', epoch=1, intent_seq=1,
                     backend_ref='codex', agent_ref='a', desired='active',
                     tool_profile={'backend_tools': [], 'tools': []})
    handle = backend.start(spec, spec.execution_key)
    state = backend._state(spec.execution_key)
    events = backend._events_path(spec.execution_key, 'd1')
    events.write_text('')
    state.update(current='d1', order=['d1'])
    state['deliveries']['d1'] = {'delivery_key': 'd1', 'delivery_id': 1,
        'message': {'brief': 'test'}, 'state': delivery_state, 'pid': None,
        'spawned': True, 'events_ref': str(events), 'tools_exposed': False}
    backend._save(spec.execution_key, state)
    # Scope this test to consumption of already-verified cleanup evidence;
    # real bridge identity and group killing have separate process tests.
    monkeypatch.setattr(backend, '_mcp_member', lambda *_: (123, 456, 789))
    monkeypatch.setattr(backend, '_cleanup_group', lambda *_: True)
    receipt = backend.stop(handle, 'verified-stop')
    assert receipt.already_stopped is True
    assert backend.observe(handle).observed == 'stopped'
