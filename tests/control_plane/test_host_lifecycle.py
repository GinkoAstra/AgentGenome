"""Host lifecycle regressions: BackendError isolation and ownership-based stop.

F1: a persistent Backend identity refusal must not starve other Runs.
F2: a foreground or console launch must stop through the persisted ownership
records, never through argv shape, and must not signal an unconfirmed process.

All fixtures are temporary; no installed service or user data directory is
touched.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import make_graph
from helpers import operator, publisher, rid
from node_runtime.control_plane import discovery
from node_runtime.control_plane import host as host_module
from node_runtime.control_plane.backends.spawn import process_start
from node_runtime.control_plane.host import Host
from node_runtime.control_plane.resources import ControlPlaneError

ROOT = Path(__file__).resolve().parents[2]
PYTHON = sys.executable
TERMINAL = ("completed", "failed", "cancelled")
SERVICE_FORMAT = "agentgenome-api-service/1"
FACTORY_SOURCE = """\
def factory(api):
    def agent(spec, message, state):
        return None
    return agent
"""
MARKER_FACTORY_SOURCE = """\
def factory(api):
    def agent(spec, message, state):
        with open({marker!r}, "a", encoding="utf-8") as handle:
            handle.write("called\\n")
    return agent
"""


# -- F1: BackendError isolation ----------------------------------------

def _host_with_factory(data_dir: Path, factory: Path) -> Host:
    return Host(data_dir, backend="script",
                script_agent=f"{factory}:factory",
                scheduler_interval=0.01, poll_seconds=0.0)


def _publish_and_create(host: Host, *, definition_id: str,
                        deadline: int | None = None):
    artifact = host.api.put_artifact(
        publisher(), rid("input"), b"[]", "text/plain")["artifact"]
    document = make_graph(definition_id=definition_id)
    if deadline is not None:
        document["budget_limits"]["run_deadline_seconds"] = deadline
    published = host.api.publish_definition(
        publisher(), document, rid("publish"))
    run = host.api.create_run(
        publisher(), published["definition_ref"],
        {"dataset": artifact}, rid("run"))
    return published["definition_ref"], artifact, run["run_id"]


def _worker_state(host: Host, worker: dict) -> dict:
    path = (host.backends["script"].root / worker["worker_id"]
            / str(worker["epoch"]) / "state.json")
    return json.loads(path.read_text(encoding="utf-8"))


def _assert_new_run_advanced(host: Host, run_id: str) -> dict:
    resources = host.api.list_resources(operator(), run_id)
    assert resources["nodes"], "the new Run never materialised a Node"
    assert resources["workers"], "the new Run never materialised a Worker"
    assert resources["run"]["status"] not in TERMINAL
    return resources


def _change_factory(factory: Path) -> None:
    factory.write_text(factory.read_text(encoding="utf-8") + "\n# changed\n",
                       encoding="utf-8")


def _identity_errors(data_dir: Path, worker_id: str) -> list[dict]:
    path = data_dir / "host.log"
    if not path.exists():
        return []
    entries = [json.loads(line) for line in
               path.read_text(encoding="utf-8").splitlines()
               if line.strip()]
    return [entry for entry in entries
            if entry.get("event") == "controller_error"
            and entry.get("worker_id") == worker_id
            and entry.get("code") == "capability_unsupported"]


def test_script_identity_refusal_does_not_starve_a_later_run(tmp_path):
    factory = tmp_path / "checker.py"
    factory.write_text(FACTORY_SOURCE, encoding="utf-8")
    data = tmp_path / "data"

    first = _host_with_factory(data, factory)
    _, _, old_run = _publish_and_create(first, definition_id="f1-old")
    first.reconcile_once()
    old_workers = first.api.list_resources(operator(), old_run)["workers"]
    assert old_workers, "the first Run never started its Worker"
    old_worker = old_workers[0]
    old_identity = first.script_agent_identity
    assert _worker_state(first, old_worker)["agent_identity"] == old_identity

    _change_factory(factory)
    second = _host_with_factory(data, factory)
    assert second.script_agent_identity != old_identity
    _, _, new_run = _publish_and_create(second, definition_id="f1-new")
    before = second.api.list_resources(operator(), new_run)
    assert before["nodes"] == [] and before["workers"] == []

    # The old Worker refuses on every tick; the new Run must still advance.
    second.reconcile_once()
    second.reconcile_once()
    _assert_new_run_advanced(second, new_run)

    assert _worker_state(second, old_worker)["agent_identity"] == old_identity, (
        "the refused binding must stay untouched, not be rewritten")
    assert _identity_errors(data, old_worker["worker_id"]), (
        "the real BackendError was not logged with its identity code")


def test_terminal_drain_stops_the_old_execution_and_a_later_run_advances(tmp_path):
    factory = tmp_path / "checker.py"
    factory.write_text(FACTORY_SOURCE, encoding="utf-8")
    data = tmp_path / "data"

    first = _host_with_factory(data, factory)
    _, _, old_run = _publish_and_create(
        first, definition_id="f1-drain-old", deadline=1)
    first.reconcile_once()
    old_worker = first.api.list_resources(operator(), old_run)["workers"][0]
    old_identity = first.script_agent_identity

    # Let the run deadline pass, then commit the stop and the terminal state
    # without any Controller drain, so the next Host finds a terminal Run whose
    # Worker still needs draining.
    time.sleep(1.1)
    first.scheduler.reconcile(old_run)
    first.scheduler.reconcile(old_run)
    terminal = first.api.list_resources(operator(), old_run)
    assert terminal["run"]["status"] == "failed"
    assert terminal["workers"][0]["desired"] == "stopped"

    _change_factory(factory)
    second = _host_with_factory(data, factory)
    _, _, new_run = _publish_and_create(second, definition_id="f1-drain-new")
    second.reconcile_once()
    second.reconcile_once()
    _assert_new_run_advanced(second, new_run)

    drained = second.api.list_resources(operator(), old_run)
    worker = next(item for item in drained["workers"]
                  if item["worker_id"] == old_worker["worker_id"])
    assert drained["run"]["status"] == "failed"
    assert worker["observed"] == "stopped"
    state = _worker_state(second, old_worker)
    assert state["agent_identity"] == old_identity, (
        "the stop path rewrote the persisted program identity")
    assert state["stop"]["stopped_at"]
    assert not _identity_errors(data, old_worker["worker_id"]), (
        "a committed stop was blocked by the changed program identity")


def test_cancelled_run_stops_after_the_program_identity_changed(tmp_path):
    marker = tmp_path / "calls.log"
    factory = tmp_path / "checker.py"
    factory.write_text(MARKER_FACTORY_SOURCE.format(marker=str(marker)),
                       encoding="utf-8")
    data = tmp_path / "data"

    first = _host_with_factory(data, factory)
    _, _, run_id = _publish_and_create(first, definition_id="f4-cancel")
    first.reconcile_once()
    worker = first.api.list_resources(operator(), run_id)["workers"][0]
    old_identity = first.script_agent_identity
    before_calls = marker.read_text(encoding="utf-8").count("called")

    run = first.api.get_resource(publisher(), "run", run_id)
    first.api.set_run_intent(publisher(), run_id, "cancelled",
                             run["revision"], rid("cancel"))

    _change_factory(factory)
    second = _host_with_factory(data, factory)
    assert second.script_agent_identity != old_identity
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        second.reconcile_once()
        state = second.api.list_resources(operator(), run_id)
        if state["run"]["status"] == "cancelled":
            break
        time.sleep(0.05)
    state = second.api.list_resources(operator(), run_id)
    stopped = next(item for item in state["workers"]
                   if item["worker_id"] == worker["worker_id"])
    assert state["run"]["status"] == "cancelled"
    assert stopped["observed"] == "stopped"
    assert _worker_state(second, worker)["agent_identity"] == old_identity
    assert marker.read_text(encoding="utf-8").count("called") == before_calls, (
        "the changed program ran under the old execution identity")
    assert not _identity_errors(data, worker["worker_id"])


# -- F2: ownership-based stop ------------------------------------------

def _env(**extra: str) -> dict[str, str]:
    environment = {**os.environ, "PYTHONPATH": str(ROOT),
                   "PYTHONDONTWRITEBYTECODE": "1"}
    environment.update(extra)
    return environment


def _host_command(*args: str) -> list[str]:
    return [PYTHON, "-m", "node_runtime.control_plane.host", *args]


def _serve(tmp_path: Path, args: list[str], *, env=None):
    log = (tmp_path / "serve.log").open("wb")
    process = subprocess.Popen(
        _host_command("serve", *args), cwd=str(tmp_path),
        env=env or _env(), stdout=subprocess.DEVNULL, stderr=log,
        start_new_session=True)
    return process, log


def _wait_ready(data: Path, process: subprocess.Popen | None = None,
                timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise AssertionError(f"serve exited with {process.returncode}")
        status = discovery.status(data)
        if status.get("running") and status.get("ready"):
            return status
        time.sleep(0.05)
    raise AssertionError("service never became ready")


def _stop(tmp_path: Path, args: list[str], *, env=None) -> subprocess.CompletedProcess:
    return subprocess.run(
        _host_command("stop", *args), cwd=str(tmp_path), env=env or _env(),
        capture_output=True, text=True, timeout=30)


def _stop_in_process(data: Path, timeout: float = 10.0):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = host_module.main(
            ["stop", "--data-dir", str(data), "--timeout", str(timeout)])
    return code, out.getvalue(), err.getvalue()


def _cleanup(process: subprocess.Popen | None, log=None) -> None:
    if process is not None and process.poll() is None:
        process.kill()
        process.wait(timeout=5)
    if log is not None:
        log.close()


def _assert_stopped_records(data: Path) -> None:
    host_record = json.loads((data / "host.json").read_text(encoding="utf-8"))
    assert host_record["pid"] is None
    assert host_record["stopped_at"]
    service_record = json.loads(
        (data / "service.json").read_text(encoding="utf-8"))
    assert service_record["stopped_at"]


def test_module_serve_stops_through_persisted_ownership(tmp_path):
    data = tmp_path / "data"
    process, log = _serve(tmp_path, ["--data-dir", str(data),
                                     "--backend", "script",
                                     "--interval", "0.05", "--poll", "0"])
    try:
        status = _wait_ready(data, process)
        assert status["pid"] == process.pid
        result = _stop(tmp_path, ["--data-dir", str(data), "--timeout", "10"])
        assert result.returncode == 0, result.stdout + result.stderr
        assert process.wait(timeout=10) == 0
        _assert_stopped_records(data)
    finally:
        _cleanup(process, log)


def test_console_entry_stops_without_module_name_in_argv(tmp_path):
    data = tmp_path / "data"
    entry = tmp_path / "agentgenome_console.py"
    entry.write_text(
        "from node_runtime.control_plane.host import main\n"
        "raise SystemExit(main())\n", encoding="utf-8")
    log = (tmp_path / "serve.log").open("wb")
    process = subprocess.Popen(
        [PYTHON, str(entry), "serve", "--data-dir", str(data),
         "--backend", "script", "--interval", "0.05", "--poll", "0"],
        cwd=str(tmp_path), env=_env(), stdout=subprocess.DEVNULL,
        stderr=log, start_new_session=True)
    try:
        _wait_ready(data, process)
        result = _stop(tmp_path, ["--data-dir", str(data), "--timeout", "10"])
        assert result.returncode == 0, result.stdout + result.stderr
        assert process.wait(timeout=10) == 0
        _assert_stopped_records(data)
    finally:
        _cleanup(process, log)


def test_detached_start_and_stop_share_the_same_identity(tmp_path):
    data = tmp_path / "data"
    started = subprocess.run(
        _host_command("start", "--data-dir", str(data), "--backend", "script",
                      "--interval", "0.05", "--poll", "0",
                      "--start-timeout", "20"),
        cwd=str(tmp_path), env=_env(), capture_output=True, text=True,
        timeout=40)
    assert started.returncode == 0, started.stdout + started.stderr
    try:
        result = _stop(tmp_path, ["--data-dir", str(data), "--timeout", "10"])
        assert result.returncode == 0, result.stdout + result.stderr
        _assert_stopped_records(data)
        assert discovery.status(data)["running"] is False
    finally:
        record = host_module._read_json(data / "host.json") or {}
        pid = record.get("pid")
        if isinstance(pid, int) and pid > 0:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


@pytest.mark.parametrize("mode", ["relative", "default"])
def test_serve_stop_resolves_relative_and_default_data_dirs(tmp_path, mode):
    if mode == "relative":
        data_arg = "relative-data"
        data = tmp_path / data_arg
        serve_args = ["--data-dir", data_arg]
        stop_args = ["--data-dir", data_arg]
        environment = _env()
    else:
        data = tmp_path / "state" / "agentgenome" / "default"
        serve_args = []
        stop_args = []
        environment = _env(XDG_STATE_HOME=str(tmp_path / "state"))
    process, log = _serve(
        tmp_path, serve_args + ["--backend", "script",
                                "--interval", "0.05", "--poll", "0"],
        env=environment)
    try:
        _wait_ready(data, process)
        result = _stop(tmp_path, stop_args + ["--timeout", "10"],
                       env=environment)
        assert result.returncode == 0, result.stdout + result.stderr
        assert process.wait(timeout=10) == 0
        _assert_stopped_records(data)
    finally:
        _cleanup(process, log)


def _write_host_record(data: Path, *, pid: int, started: int,
                       instance_id: str = "owner-1",
                       data_dir: Path | None = None,
                       include_data_dir: bool = True,
                       record_format: str = "agentgenome-host/1") -> None:
    record = {
        "format": record_format,
        "instance_id": instance_id,
        "pid": pid,
        "process_start": started,
        "stopped_at": None,
    }
    if include_data_dir:
        record["data_dir"] = str((data_dir or data).resolve())
    data.mkdir(parents=True, exist_ok=True)
    (data / "host.json").write_text(json.dumps(record), encoding="utf-8")


def _write_service_record(data: Path, *, pid: int,
                          instance_id: str = "owner-1") -> None:
    data.mkdir(parents=True, exist_ok=True)
    (data / "service.json").write_text(json.dumps({
        "format": SERVICE_FORMAT,
        "service_id": "service-1",
        "instance_id": instance_id,
        "pid": pid,
        "endpoint": "http://127.0.0.1:9",
        "data_dir": str(data.resolve()),
        "stopped_at": None,
    }), encoding="utf-8")


def _sleeping_child() -> subprocess.Popen:
    return subprocess.Popen([PYTHON, "-c",
                             "import time; time.sleep(30)"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def test_stop_works_while_http_is_unreachable(tmp_path):
    data = tmp_path / "data"
    child = _sleeping_child()
    try:
        _write_host_record(data, pid=child.pid,
                           started=process_start(child.pid))
        code, out, err = _stop_in_process(data)
        assert code == 0, out + err
        assert child.wait(timeout=5) == -signal.SIGTERM
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


@pytest.mark.parametrize("case", ["recycled", "conflict_instance",
                                  "conflict_directory", "missing_host",
                                  "missing_directory", "wrong_format"])
def test_stop_rejects_unconfirmed_identity_without_signalling(tmp_path, case):
    data = tmp_path / "data"
    child = _sleeping_child()
    try:
        started = process_start(child.pid)
        if case == "recycled":
            _write_host_record(data, pid=child.pid, started=0)
            expected = 0
        elif case == "conflict_instance":
            _write_host_record(data, pid=child.pid, started=started,
                               instance_id="host-instance")
            _write_service_record(data, pid=child.pid,
                                  instance_id="service-instance")
            expected = 1
        elif case == "conflict_directory":
            _write_host_record(data, pid=child.pid, started=started,
                               data_dir=tmp_path / "elsewhere")
            expected = 1
        elif case == "missing_directory":
            _write_host_record(data, pid=child.pid, started=started,
                               include_data_dir=False)
            expected = 1
        elif case == "wrong_format":
            _write_host_record(data, pid=child.pid, started=started,
                               record_format="agentgenome-host/0")
            _write_service_record(data, pid=child.pid)
            expected = 1
        else:
            _write_service_record(data, pid=child.pid)
            expected = 1
        code, out, err = _stop_in_process(data)
        assert code == expected, out + err
        assert child.poll() is None, "an unconfirmed identity was signalled"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_stop_rejects_unreadable_live_identity_without_signalling(
        tmp_path, monkeypatch):
    # A live pid whose kernel identity cannot be read is unknown, not stale:
    # the stop path must refuse without sending SIGTERM.
    data = tmp_path / "data"
    child = _sleeping_child()
    signals: list[int] = []

    def guarded_kill(pid: int, signum: int) -> None:
        signals.append(signum)
        raise PermissionError("pid identity is not readable")

    try:
        _write_host_record(data, pid=child.pid,
                           started=process_start(child.pid))
        with monkeypatch.context() as patch:
            patch.setattr(host_module, "process_start", lambda _pid: None)
            patch.setattr(host_module.os, "kill", guarded_kill)
            code, out, err = _stop_in_process(data)
        assert code == 1, out + err
        assert json.loads(err)["error"] == "ownership_identity_unverified"
        assert signals == [0], "an unverified identity was signalled"
        assert child.poll() is None, "the live process was terminated"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_record_dir_matches_rejects_absent_or_nontext_directory(tmp_path):
    # A missing key used to resolve as the literal relative path "None".
    assert not host_module._record_dir_matches({}, Path("None").resolve())
    assert not host_module._record_dir_matches(
        {"data_dir": ""}, Path.cwd().resolve())
    assert not host_module._record_dir_matches(
        {"data_dir": 123}, Path("123").resolve())
    assert host_module._record_dir_matches(
        {"data_dir": str(tmp_path)}, tmp_path.resolve())


def test_finished_poll_requires_positive_evidence(monkeypatch):
    pid = os.getpid()
    started = process_start(pid)
    with monkeypatch.context() as patch:
        patch.setattr(host_module, "process_start", lambda _pid: None)
        assert not host_module._process_finished(pid, started)
    with monkeypatch.context() as patch:
        patch.setattr(host_module, "_process_state", lambda _pid: None)
        assert not host_module._process_finished(pid, started)


def test_finished_poll_treats_unreadable_proc_as_unknown(monkeypatch):
    # procfs unreadable: only ESRCH proves the owner is gone. A permission or
    # transient failure must keep polling rather than claim completion.
    pid = os.getpid()
    started = process_start(pid)

    def denied(_pid: int, _signum: int) -> None:
        raise PermissionError("procfs is not readable")

    def gone(_pid: int, _signum: int) -> None:
        raise ProcessLookupError("no such process")

    with monkeypatch.context() as patch:
        patch.setattr(host_module, "process_start", lambda _pid: None)
        patch.setattr(host_module.os, "kill", denied)
        assert not host_module._process_finished(pid, started)
    with monkeypatch.context() as patch:
        patch.setattr(host_module, "process_start", lambda _pid: None)
        patch.setattr(host_module.os, "kill", gone)
        assert host_module._process_finished(pid, started)


def test_stop_never_signals_its_own_process(tmp_path):
    data = tmp_path / "data"
    _write_host_record(data, pid=os.getpid(),
                       started=process_start(os.getpid()))
    received: list[int] = []
    previous = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, lambda signum, _frame: received.append(signum))
    try:
        code, out, err = _stop_in_process(data)
    finally:
        signal.signal(signal.SIGTERM, previous)
    assert code == 1, out + err
    assert received == []


def test_stop_treats_unreaped_zombie_as_stopped(tmp_path):
    data = tmp_path / "data"
    child = _sleeping_child()
    child.kill()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and _proc_state(child.pid) != "Z":
        time.sleep(0.02)
    try:
        # A zombie still answers kill(pid, 0); poll() would reap it, so the
        # unreaped evidence is the kernel state itself.
        assert _proc_state(child.pid) == "Z"
        _write_host_record(data, pid=child.pid,
                           started=process_start(child.pid))
        started_at = time.monotonic()
        code, out, err = _stop_in_process(data, timeout=5)
        assert code == 0, out + err
        assert time.monotonic() - started_at < 3
    finally:
        child.wait(timeout=5)


def test_stop_reports_timeout_while_process_still_runs(tmp_path):
    data = tmp_path / "data"
    child = subprocess.Popen(
        [PYTHON, "-c",
         "import signal,time\n"
         "signal.signal(signal.SIGTERM, lambda *a: None)\n"
         "time.sleep(30)\n"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(0.2)
        _write_host_record(data, pid=child.pid,
                           started=process_start(child.pid))
        code, out, err = _stop_in_process(data, timeout=0.5)
        assert code == 1, out + err
        assert child.poll() is None, "stop claimed success while work remained"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def _proc_state(pid: int) -> str | None:
    try:
        line = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        return line[line.rindex(")") + 2]
    except (ValueError, IndexError):
        return None
