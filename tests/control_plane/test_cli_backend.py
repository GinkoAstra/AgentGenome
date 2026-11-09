"""Lifecycle tests that really spawn a CLI process through CliBackend.deliver.

The fake CLI speaks the same JSONL vocabulary as Codex/Qoder but never calls a
model; this exercises argv construction, the MCP launch, the exit watcher and
process-group teardown end to end.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import time

import pytest

from node_runtime.control_plane.backends.base import BackendError, WorkerSpec
from node_runtime.control_plane.backends.cli import CliBackend
from node_runtime.control_plane.backends.spawn import process_start

CODEX_EVENTS = [
    {"type": "thread.started", "thread_id": "thread-1"},
    {"type": "turn.started"},
    {"type": "item.completed",
     "item": {"type": "agent_message", "text": "codex done"}},
    {"type": "turn.completed", "usage": {"input_tokens": 3}},
]

QODER_EVENTS = [
    {"type": "system", "subtype": "init", "session_id": "session-q",
     "tools": [], "mcp_servers": [], "plugins": []},
    {"type": "assistant",
     "message": {"id": "m1", "role": "assistant",
                 "content": [{"type": "text", "text": "qoder done"}]}},
    {"type": "result", "subtype": "success", "is_error": False,
     "result": "qoder done", "session_id": "session-q"},
]


def _script(tmp_path: Path, name: str, events: list[dict], *,
            sleep: float = 0.0) -> Path:
    path = tmp_path / name
    lines = "\n".join(json.dumps(event) for event in events)
    path.write_text(
        f"#!{sys.executable}\n"
        "import sys, time\n"
        "sys.stdin.read()\n"
        f"print({lines!r}, flush=True)\n"
        f"time.sleep({sleep})\n",
        encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _spec(*, backend_tools: list[str] | None = None,
          budget: dict | None = None) -> WorkerSpec:
    return WorkerSpec(
        worker_id="w1", node_id="n1", run_id="r1", epoch=1, intent_seq=1,
        backend_ref="", agent_ref="agent", desired="active",
        tool_profile={"backend_tools": backend_tools or [], "tools": []},
        budget=budget or {})


def _observe(backend: CliBackend, handle, *, wanted: set[str],
             timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    observation = None
    while time.monotonic() < deadline:
        observation = backend.observe(handle)
        if observation.observed in wanted:
            return observation
        time.sleep(0.05)
    raise AssertionError(
        f"observation stayed {observation.observed if observation else None}; "
        f"last={observation}")


def _backend(tmp_path: Path, backend: str, executable: Path) -> CliBackend:
    return CliBackend(tmp_path, backend=backend, executable=str(executable),
                      max_wall_seconds=10, grace_seconds=0.5,
                      yield_grace_seconds=0.5)


@pytest.mark.parametrize(("backend", "events", "session", "text"), [
    ("codex", CODEX_EVENTS, "thread-1", "codex done"),
    ("qoder", QODER_EVENTS, "session-q", "qoder done"),
])
def test_deliver_runs_native_turn_and_records_session(
        tmp_path, backend, events, session, text):
    executable = _script(tmp_path, f"fake-{backend}", events)
    instance = _backend(tmp_path, backend, executable)
    spec = _spec(backend_tools=["write_file"])
    spec = WorkerSpec(**{**spec.as_dict(), "backend_ref": backend,
                         "tool_profile": spec.tool_profile})
    handle = instance.start(spec, spec.execution_key)

    receipt = instance.deliver(handle, {"brief": "do the thing"}, "d1")
    assert receipt.accepted and receipt.outcome == "running"
    observation = _observe(instance, handle, wanted={"idle"})
    assert observation.observed == "idle"
    assert observation.session_ref == session
    assert observation.checkpoint_ref is None
    assert observation.result["final_text"] == text
    assert observation.activities == ()
    assert observation.activities_complete is True

    duplicate = instance.deliver(handle, {"brief": "again"}, "d1")
    assert duplicate.duplicate is True
    assert duplicate.delivery_id == receipt.delivery_id

    resumed = instance.deliver(handle, {"brief": "next round"}, "d2")
    assert resumed.accepted and resumed.outcome == "running"
    assert _observe(instance, handle, wanted={"idle"}).observed == "idle"
    state = json.loads((instance._state_path(spec.execution_key))
                       .read_text(encoding="utf-8"))
    assert state["deliveries"]["d2"]["session_resume"] is True
    assert state["deliveries"]["d1"]["tools_exposed"] is True
    # The private tool context is written for the exposed MCP bridge.
    context = Path(state["deliveries"]["d1"]["events_ref"]).parent.parent \
        / "tool-context" / "d1.json"
    assert stat.S_IMODE(context.stat().st_mode) == 0o600

    trace = instance.execution_log(run_id="r1", worker_id="w1", epoch=1,
                                   delivery_key="d1", after=0, limit=100)
    assert trace.availability == "available"
    assert trace.entries and trace.next_offset > 0
    assert any(entry.get("session_id") == session for entry in trace.entries)


def test_delivery_without_native_session_cannot_be_resumed(tmp_path):
    events = [{"type": "turn.started"},
              {"type": "item.completed",
               "item": {"type": "agent_message", "text": "no id"}},
              {"type": "turn.completed"}]
    executable = _script(tmp_path, "fake-codex", events)
    instance = _backend(tmp_path, "codex", executable)
    spec = _spec()
    spec = WorkerSpec(**{**spec.as_dict(), "backend_ref": "codex"})
    handle = instance.start(spec, spec.execution_key)
    instance.deliver(handle, {"brief": "first"}, "d1")
    assert _observe(instance, handle, wanted={"idle"}).observed == "idle"
    with pytest.raises(BackendError) as caught:
        instance.deliver(handle, {"brief": "second"}, "d2")
    assert caught.value.code == "capability_unsupported"


def test_stop_terminates_the_whole_process_group(tmp_path):
    executable = _script(tmp_path, "fake-codex", CODEX_EVENTS, sleep=30)
    instance = _backend(tmp_path, "codex", executable)
    spec = _spec()
    spec = WorkerSpec(**{**spec.as_dict(), "backend_ref": "codex"})
    handle = instance.start(spec, spec.execution_key)
    instance.deliver(handle, {"brief": "hang"}, "d1")
    deadline = time.monotonic() + 5
    state = instance._state(spec.execution_key)
    while state["deliveries"]["d1"]["pid"] is None \
            and time.monotonic() < deadline:
        time.sleep(0.02)
        state = instance._state(spec.execution_key)
    pid = state["deliveries"]["d1"]["pid"]
    assert isinstance(pid, int) and pid > 0

    receipt = instance.stop(handle, "stop-1")
    assert receipt.accepted is True
    assert instance._group_active(pid) is False
    observation = _observe(instance, handle, wanted={"stopped"})
    assert observation.observed == "stopped"
    assert observation.activities_complete is True


def test_cached_process_is_rejected_when_kernel_start_differs(tmp_path):
    backend = _backend(tmp_path, "codex", Path(sys.executable))
    process = subprocess.Popen(
        [sys.executable, "-c", "import time;time.sleep(30)"])
    started = process_start(process.pid)
    key = ("w1", 1)
    backend._procs[key] = (process, started)
    try:
        delivery = {"pid": process.pid, "process_start": started}
        assert backend._current_process(key, None, delivery) is process
        assert backend._alive(process, started) is True

        # A live pid alone is not the recorded process: adoption must compare
        # the kernel start time and drop the stale cache entry.
        recycled = dict(delivery, process_start=started + 1)
        assert backend._current_process(key, None, recycled) is None
        assert key not in backend._procs
        assert backend._alive(process, started + 1) is False
        assert backend._alive(process) is False
    finally:
        process.kill()
        process.wait(timeout=5)


def test_checkpoint_and_restore_are_explicitly_unsupported(tmp_path):
    executable = _script(tmp_path, "fake-codex", CODEX_EVENTS)
    instance = _backend(tmp_path, "codex", executable)
    spec = _spec()
    spec = WorkerSpec(**{**spec.as_dict(), "backend_ref": "codex"})
    handle = instance.start(spec, spec.execution_key)
    assert instance.capabilities().checkpoint is False
    with pytest.raises(BackendError) as checkpoint:
        instance.checkpoint(handle)
    assert checkpoint.value.code == "capability_unsupported"
    with pytest.raises(BackendError) as restore:
        instance.restore(spec, {"kind": "cli-session"}, spec.execution_key)
    assert restore.value.code == "capability_unsupported"


def test_provider_call_budget_is_refused_before_preparation(tmp_path):
    executable = _script(tmp_path, "fake-codex", CODEX_EVENTS)
    instance = _backend(tmp_path, "codex", executable)
    spec = _spec(budget={"max_provider_calls": 1})
    spec = WorkerSpec(**{**spec.as_dict(), "backend_ref": "codex"})
    with pytest.raises(BackendError) as caught:
        instance.start(spec, spec.execution_key)
    assert caught.value.code == "capability_unsupported"


@pytest.mark.parametrize(("field", "reason"), [
    ("budget_exceeded", "budget_exceeded"),
    ("timed_out", "wall_timeout"),
])
def test_inflight_tool_keeps_unknown_before_budget_or_timeout(
        tmp_path, field, reason):
    executable = _script(tmp_path, "fake-codex", CODEX_EVENTS)
    instance = _backend(tmp_path, "codex", executable)
    spec = _spec()
    spec = WorkerSpec(**{**spec.as_dict(), "backend_ref": "codex"})
    handle = instance.start(spec, spec.execution_key)
    instance.deliver(handle, {"brief": "turn"}, "d1")
    assert _observe(instance, handle, wanted={"idle"}).observed == "idle"

    # Model a tool request that reached the bridge and never returned before
    # the watcher killed the process for budget/timeout reasons.
    receipt = instance._receipt_path(spec.execution_key)
    with open(receipt, "a", encoding="utf-8") as stream:
        stream.write(json.dumps({
            "phase": "start", "call_id": "mcp-inflight",
            "name": "node.submit_candidate", "effect": "idempotent",
            "params": {}}) + "\n")
    exit_path = instance._exit_path(spec.execution_key, "d1")
    record = json.loads(exit_path.read_text(encoding="utf-8"))
    record[field] = True
    record["code"] = -15
    if field == "timed_out":
        record["timeout_seconds"] = 1
    exit_path.write_text(json.dumps(record), encoding="utf-8")
    state = json.loads(instance._state_path(spec.execution_key)
                       .read_text(encoding="utf-8"))
    state["deliveries"]["d1"]["state"] = "running"
    instance._save(spec.execution_key, state)

    observation = _observe(instance, handle, wanted={"unknown"})
    assert observation.observed == "unknown"
    assert observation.error["code"] == "tool_effect_unknown"
    assert observation.error["reason"] == reason
    assert observation.error["tools"] == ["node.submit_candidate"]


def _orphan_group(tmp_path: Path, record: Path | None, *,
                  ignore_term: bool = False
                  ) -> tuple[subprocess.Popen, int, int]:
    """A dead leader whose child (the MCP bridge stand-in) stays in its group."""
    child_path = tmp_path / "mcp-child.json"
    signal_setup = ("signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                    if ignore_term else "")
    record_write = ""
    if record is not None:
        record_write = (
            "record=Path(" + repr(str(record)) + "); "
            "record.parent.mkdir(parents=True, exist_ok=True); "
            "text=Path('/proc/%d/stat'%os.getpid()).read_text(); "
            "start=int(text[text.rindex(')')+2:].split()[19]); "
            "record.write_text(json.dumps({"
            "'format':'agentgenome-worker-mcp/1','pid':os.getpid(),"
            "'process_start':start,'pgid':os.getpgrp(),"
            "'delivery_key':'d1','run_id':'r1','node_id':'n1',"
            "'worker_id':'w1','epoch':1})); ")
    child = ("import os,time,json,signal; from pathlib import Path; "
             f"{signal_setup}{record_write}"
             f"Path({str(child_path)!r}).write_text(str(os.getpid())); "
             "time.sleep(30)")
    leader = (f"import subprocess,sys; "
              f"subprocess.Popen([sys.executable,'-c',{child!r}]);")
    process = subprocess.Popen([sys.executable, "-c", leader],
                               start_new_session=True)
    leader_started = process_start(process.pid)
    process.wait(timeout=5)
    deadline = time.monotonic() + 5
    while not child_path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    child_pid = int(child_path.read_text())
    return process, child_pid, leader_started


def test_restart_stop_cleans_group_through_verified_mcp_identity(tmp_path):
    executable = _script(tmp_path, "fake-codex", CODEX_EVENTS)
    instance = _backend(tmp_path, "codex", executable)
    spec = _spec()
    spec = WorkerSpec(**{**spec.as_dict(), "backend_ref": "codex"})
    key = spec.execution_key
    instance.start(spec, key)
    record = instance._mcp_record_path(key, "d1")
    leader, child_pid, leader_started = _orphan_group(tmp_path, record)
    started = process_start(child_pid)
    state = instance._new_state(spec, key)
    state["prepared"] = True
    state["order"] = ["d1"]
    state["current"] = "d1"
    state["deliveries"] = {"d1": {
        "delivery_id": 1, "delivery_key": "d1",
        "message": {"brief": "restart"},
        "state": "running", "pid": leader.pid, "spawned": True,
        "process_start": leader_started,
        "started_ts": time.time(), "wall_timeout": 10,
        "events_ref": str(instance._events_path(key, "d1")),
        "stderr_ref": str(instance._stderr_path(key, "d1")),
        "materials": [], "system_tools": [], "tools_exposed": True}}
    instance._save(key, state)
    # Stale evidence from before the cleanup: the group is still reported
    # unclean even after a later stop proves the whole group gone.
    exit_path = instance._exit_path(key, "d1")
    exit_path.parent.mkdir(parents=True, exist_ok=True)
    exit_path.write_text(json.dumps({
        "code": 0, "timed_out": False, "budget_exceeded": False,
        "group_cleaned": False, "finished_at": "earlier"}), encoding="utf-8")
    handle = instance._handle(spec, state)
    try:
        receipt = instance.stop(handle, "restart-stop")
        assert receipt.already_stopped is True
        deadline = time.monotonic() + 2
        while process_start(child_pid) == started \
                and time.monotonic() < deadline:
            time.sleep(0.02)
        assert process_start(child_pid) != started
        healed = json.loads(exit_path.read_text(encoding="utf-8"))
        assert healed["group_cleaned"] is True
        assert instance.observe(handle).observed == "stopped"
    finally:
        try:
            os.killpg(leader.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_restart_stop_without_verified_member_is_refused(tmp_path):
    executable = _script(tmp_path, "fake-codex", CODEX_EVENTS)
    instance = _backend(tmp_path, "codex", executable)
    spec = _spec()
    spec = WorkerSpec(**{**spec.as_dict(), "backend_ref": "codex"})
    key = spec.execution_key
    instance.start(spec, key)
    leader, child_pid, leader_started = _orphan_group(tmp_path, None)
    state = instance._new_state(spec, key)
    state["prepared"] = True
    state["order"] = ["d1"]
    state["current"] = "d1"
    state["deliveries"] = {"d1": {
        "delivery_id": 1, "delivery_key": "d1",
        "message": {"brief": "restart"},
        "state": "running", "pid": leader.pid, "spawned": True,
        "process_start": leader_started,
        "started_ts": time.time(), "wall_timeout": 10,
        "events_ref": str(instance._events_path(key, "d1")),
        "stderr_ref": str(instance._stderr_path(key, "d1")),
        "materials": [], "system_tools": [], "tools_exposed": True}}
    instance._save(key, state)
    handle = instance._handle(spec, state)
    try:
        with pytest.raises(BackendError) as caught:
            instance.stop(handle, "restart-stop")
        assert caught.value.code == "capability_unsupported"
    finally:
        try:
            os.killpg(leader.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass