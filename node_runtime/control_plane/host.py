"""Formal control-plane host: Scheduler + Controller in one service process.

The host is a thin composition root. It discovers non-terminal runs through the
public API, asks the Scheduler to reconcile each graph, then asks the
Controller to reconcile that run's Workers. It owns no graph or business
logic, so every SOP uses the same process and the same Controller code.

Commands:
    agentgenome serve      run the network service in the foreground
    agentgenome start      start/reuse the detached service and wait for ready
    agentgenome status     report the service record and visible runs (HTTP)
    agentgenome stop       verify and terminate the service process
    agentgenome teacher    open the ordinary Teacher Pi session for a service

Launchers never construct Host, Api or Store: status/start/stop read the
durable service record, probe loopback HTTP and (when connected) use ApiClient.
Only ``serve`` opens the database, in the one process that owns the directory.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import types
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from . import discovery
from .api import Api
from .backends.base import BackendError
from .backends.cli import CliBackend
from .backends.pi import PiBackend
from .backends.script import ScriptBackend
from .backends.spawn import process_start
from .client import ApiClient, ApiTransportError
from .controller import Controller
from .execution_facts import execution_stopped
from .resources import Actor, ControlPlaneError
from .scheduler import Scheduler

HOST_FORMAT = "agentgenome-host/1"
STATUS_FORMAT = "agentgenome-host-status/1"
TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "cancelled"})
CLI_BACKENDS = ("codex", "qoder", "claude")
HOST_BACKENDS = ("pi", "script", *CLI_BACKENDS, "kimi")
CLI_EXECUTABLES = {"codex": "codex", "qoder": "qodercli", "claude": "claude"}


def _resolve_runtime(backend: str, provider: str | None,
                     model: str | None, executable: str | None
                     ) -> tuple[str | None, str | None, str | None]:
    """Fill deployment defaults per backend; never leak Pi's model into a CLI."""
    if backend == "kimi":
        raise ControlPlaneError(
            "capability_unsupported",
            "the Kimi CLI adapter is not implemented: the installed protocol "
            "has not been verified (MoonshotAI/kimi-code differs from the old "
            "Python kimi-cli)")
    if backend not in HOST_BACKENDS:
        raise ControlPlaneError("capability_unsupported",
                                "unknown host backend", backend=backend)
    if backend == "pi":
        return (provider or "llama-local", model or "qwen3.5-2b",
                executable or "pi")
    if backend in CLI_BACKENDS:
        if provider not in (None, ""):
            raise ControlPlaneError(
                "invalid_argument",
                "--provider is only valid for the Pi backend; CLI backends "
                "use the executable's own provider configuration",
                backend=backend)
        return (None, model, executable or CLI_EXECUTABLES[backend])
    return (provider, model, executable)


def _resolved_config_dir(value: str | Path | None) -> Path | None:
    if value in (None, ""):
        return None
    try:
        return Path(value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ControlPlaneError(
            "not_found", "the CLI configuration directory does not exist",
            config_dir=str(value)) from error


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True),
                         encoding="utf-8")
    os.replace(temporary, path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int, started: int | None = None) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return started is None or process_start(pid) == started


def _process_state(pid: int) -> str | None:
    try:
        line = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        return line[line.rindex(")") + 2]
    except (ValueError, IndexError):
        return None


def _process_gone(pid: int) -> bool:
    """True only when the kernel reports that the pid no longer exists."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def _process_finished(pid: int, started: int) -> bool:
    """True when the recorded owner exited or is an unreaped zombie.

    A zombie still answers kill(pid, 0) until its parent reaps it; it has no
    control work left, so stop must not wait for the reaper. When procfs
    cannot be read, only ESRCH proves the owner is gone; a permission or
    transient read failure stays unknown and keeps polling.
    """
    current = process_start(pid)
    if current is None:
        return _process_gone(pid)
    if current != started:
        return True
    return _process_state(pid) in ("Z", "X", "x")


def _record_dir_matches(record: dict[str, Any], data_dir: Path) -> bool:
    value = record.get("data_dir")
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        return Path(value).resolve() == data_dir
    except (OSError, RuntimeError):
        return False


def _ended(record: dict[str, Any] | None) -> bool:
    return (record is None or record.get("stopped_at") is not None
            or not record.get("pid"))


@contextmanager
def _bootstrap_lock(data_dir: Path, timeout: float) -> Iterator[None]:
    """Serialise host bootstrap across processes.

    Database initialisation, liveness checks, spawning and publishing the
    host record all happen inside this lock, so two `start` processes cannot
    both believe they own the data directory. The lock is released once the
    running host has published its record; it is not held for the service
    lifetime.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    handle = open(data_dir / "host.lock", "a+", encoding="utf-8")
    deadline = time.monotonic() + max(0.0, timeout)
    try:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise ControlPlaneError(
                        "host_busy",
                        "another host operation holds the data directory lock")
                time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _script_reference(reference: str) -> tuple[Path, str]:
    """Parse <file.py>:<factory> and confine it to one existing local file."""
    if not isinstance(reference, str) or not reference.strip():
        raise ControlPlaneError(
            "invalid_argument", "--script-agent must be <file.py>:<factory>")
    file_part, separator, factory = reference.rpartition(":")
    if not separator or not file_part or not factory:
        raise ControlPlaneError(
            "invalid_argument", "--script-agent must be <file.py>:<factory>",
            reference=reference)
    if not factory.isidentifier():
        raise ControlPlaneError(
            "invalid_argument",
            "--script-agent factory must be a Python identifier",
            factory=factory)
    try:
        path = Path(file_part).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ControlPlaneError(
            "not_found", "script agent file is missing", path=file_part
        ) from error
    if not path.is_file():
        raise ControlPlaneError(
            "invalid_argument", "script agent path is not a regular file",
            path=str(path))
    return path, factory


def _script_identity(reference: str) -> dict[str, str]:
    """Deployment identity without importing or executing the module."""
    path, factory = _script_reference(reference)
    digest_value = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"path": str(path), "factory": factory,
            "sha256": f"sha256:{digest_value}"}


def _load_script_agent(reference: str, api: Api) -> tuple[Any, dict[str, str]]:
    """Load one explicit trusted local factory, not a Graph-selected plugin.

    Return (factory(api) callback, identity with absolute path/factory/SHA).
    Invalid/missing/noncallable configuration fails before starting Workers.
    Existing ScriptBackend callback protocol remains unchanged.
    """
    identity = _script_identity(reference)
    path = Path(identity["path"])
    module_name = ("agentgenome_script_agent_"
                   + identity["sha256"].split(":", 1)[1][:16])
    try:
        source = path.read_bytes()
    except OSError as error:
        raise ControlPlaneError(
            "invalid_argument", "script agent file cannot be read",
            path=str(path)) from error
    if "sha256:" + hashlib.sha256(source).hexdigest() != identity["sha256"]:
        raise ControlPlaneError(
            "invalid_argument", "script agent file changed while loading",
            path=str(path))
    # Compile the exact bytes whose digest is the identity. Normal module
    # loading can reuse a same-size/same-mtime .pyc and silently execute old
    # review logic under the new digest.
    try:
        code = compile(source, str(path), "exec")
    except (SyntaxError, ValueError) as error:
        raise ControlPlaneError(
            "invalid_argument", "script agent file cannot be compiled",
            path=str(path), error=repr(error)) from error
    module = types.ModuleType(module_name)
    module.__file__ = str(path)
    try:
        exec(code, module.__dict__)
    except Exception as error:
        raise ControlPlaneError(
            "invalid_argument", "script agent module failed to import",
            path=str(path), error=repr(error)) from error
    factory = getattr(module, identity["factory"], None)
    if not callable(factory):
        raise ControlPlaneError(
            "invalid_argument",
            "script agent factory is missing or not callable",
            path=str(path), factory=identity["factory"])
    try:
        agent = factory(api)
    except Exception as error:
        raise ControlPlaneError(
            "invalid_argument", "script agent factory failed",
            path=str(path), factory=identity["factory"],
            error=repr(error)) from error
    if not callable(agent):
        raise ControlPlaneError(
            "invalid_argument",
            "script agent factory did not return a callable agent",
            path=str(path), factory=identity["factory"])
    return agent, identity


class Host:
    """Composition root for one Scheduler and one Controller."""

    def __init__(self, data_dir: str | Path, *, backend: str = "pi",
                 provider: str | None = None, model: str | None = None,
                 executable: str | None = None, controller_id: str = "ctl-1",
                 scheduler_id: str = "scheduler",
                 scheduler_interval: float = 1.0,
                 poll_seconds: float = 0.2,
                 max_wall_seconds: float = 240.0,
                 instance_id: str | None = None,
                 lock_timeout: float = 10.0,
                 script_agent: str | None = None,
                 config_dir: str | Path | None = None,
                 load_script_agent: bool = True) -> None:
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.api = Api(self.data_dir)
        self.instance_id = instance_id or uuid.uuid4().hex
        self.lock_timeout = lock_timeout
        self.backend_ref = backend
        provider, model, executable = _resolve_runtime(
            backend, provider, model, executable)
        self.provider = provider
        self.model = model
        self.executable = executable
        self.config_dir = _resolved_config_dir(config_dir)
        if self.config_dir is not None and backend not in CLI_BACKENDS:
            raise ControlPlaneError(
                "invalid_argument",
                "--config-dir applies only to a CLI backend",
                backend=backend)
        self.controller_id = controller_id
        self.scheduler_id = scheduler_id
        self.scheduler_interval = scheduler_interval
        self.poll_seconds = poll_seconds
        agent = None
        identity = None
        if script_agent is not None:
            if load_script_agent:
                # The explicit deployment flag is the only way a program
                # callback enters the host; Graph/model data cannot name code.
                agent, identity = _load_script_agent(script_agent, self.api)
            else:
                # Status/stop may inspect an existing service without running
                # arbitrary local code; only the deployment identity is read.
                identity = _script_identity(script_agent)
        self.script_agent_identity = identity
        self.credential_issuer = None
        backends: dict[str, Any] = {}
        if backend == "pi":
            backends["pi"] = PiBackend(
                self.data_dir, provider=provider, model=model,
                executable=executable, max_wall_seconds=max_wall_seconds,
                artifact_reader=self.api.artifacts.read,
                data_dir=self.data_dir,
            )
            if identity is not None:
                # A Pi host keeps Pi for Workers and optionally registers the
                # strict program Checker for Agents whose backend_ref is script.
                backends["script"] = ScriptBackend(
                    self.data_dir, agent=agent, identity=identity)
        elif backend == "script":
            backends["script"] = ScriptBackend(
                self.data_dir, agent=agent, identity=identity)
        elif backend in CLI_BACKENDS:
            instance = CliBackend(
                self.data_dir, backend=backend,
                executable=self.executable or CLI_EXECUTABLES[backend],
                model=self.model, config_dir=self.config_dir,
                max_wall_seconds=max_wall_seconds,
                artifact_reader=self.api.artifacts.read,
            )
            # Fail the service startup, not a later Controller tick, when the
            # deployment's executable or configuration reference is missing.
            instance.protocol.check()
            backends[backend] = instance
            if identity is not None:
                # A CLI host still attaches the trusted local program Checker;
                # only an explicit deployment flag can name that code.
                backends["script"] = ScriptBackend(
                    self.data_dir, agent=agent, identity=identity)
        else:
            raise ControlPlaneError("capability_unsupported",
                                    "unknown host backend", backend=backend)
        self.backends = backends
        # The adapter inventory is trusted composition-root configuration. No
        # plan or model output can widen it, and the API refuses a Worker whose
        # Agent requires a capability this registry does not provide.
        self.api.backend_capabilities = {
            name: instance.capabilities() for name, instance in backends.items()}
        self.scheduler = Scheduler(
            self.api, scheduler_id=scheduler_id, controller_id=controller_id)
        self.controller = Controller(self.api, backends,
                                     controller_id=controller_id,
                                     data_dir=self.data_dir,
                                     poll_seconds=poll_seconds)
        self._host_record = self.data_dir / "host.json"
        self._launch_record = self.data_dir / "host.launch.json"
        self._log_path = self.data_dir / "host.log"

    # -- service attachment ---------------------------------------------
    def attach_credential_issuer(self, issuer: Any) -> None:
        """Give Backends the trusted delivery-credential issuer.

        Called by ApiServer after the loopback endpoint is bound, so no Worker
        can receive system tools without a verified network route.
        """
        self.credential_issuer = issuer
        for backend in self.backends.values():
            setter = getattr(backend, "set_credential_issuer", None)
            if callable(setter):
                setter(issuer)

    # -- status ----------------------------------------------------------
    def record(self) -> dict[str, Any] | None:
        return _read_json(self._host_record)

    def running(self) -> bool:
        record = self.record()
        if not record or not record.get("pid"):
            return False
        return _pid_alive(int(record["pid"]), record.get("process_start"))

    def _operator(self) -> Actor:
        return Actor("host", {"scheduler", "publisher"})

    def status(self) -> dict[str, Any]:
        record = self.record()
        alive = self.running()
        runs: list[dict[str, Any]] = []
        if (self.data_dir / "state.sqlite3").exists():
            for run in self.api.list_runs(self._operator(), limit=1000)["runs"]:
                runs.append({
                    "run_id": run["run_id"], "status": run["status"],
                    "desired": run["desired"],
                    "definition_ref": run["definition_ref"],
                    "created_at": run["created_at"],
                    "updated_at": run["updated_at"],
                    "event_seq": run["event_seq"],
                })
        return {
            "format": STATUS_FORMAT,
            "data_dir": str(self.data_dir),
            "running": alive,
            "pid": record.get("pid") if alive else None,
            "backend": (record or {}).get("backend"),
            "script_agent": (record or {}).get("script_agent"),
            "controller_id": (record or {}).get("controller_id"),
            "scheduler_id": (record or {}).get("scheduler_id"),
            "started_at": (record or {}).get("started_at"),
            "stopped_at": (record or {}).get("stopped_at"),
            "log": str(self._log_path),
            "runs": runs,
        }

    def _log(self, event: str, **detail: Any) -> None:
        entry = {"at": _now(), "event": event, **detail}
        with self._log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
            handle.flush()

    # -- reconciliation --------------------------------------------------
    def reconciliation_runs(self) -> list[dict[str, Any]]:
        return self.api.list_reconciliation_runs(self._operator(),
                                                 limit=1000)["runs"]

    @staticmethod
    def _needs_drain(worker: dict[str, Any]) -> bool:
        return (worker["desired"] == "stopped"
                and not execution_stopped(worker))

    def _drain(self, run_id: str, worker: dict[str, Any],
               actions: list[dict[str, Any]]) -> None:
        """Drive a committed stop to an observed fact, without redelivering.

        Some Backends acknowledge a stop before the process has exited. The
        first reconcile reports the still-active execution; a second stop is
        harmless and lets the backend finish the transition. Bounded here so a
        genuinely stuck process cannot block other runs; the service loop
        retries on its next pass.
        """
        for _ in range(3):
            result = self.controller.reconcile(run_id, worker["worker_id"])
            if result.get("delivered"):
                actions.append({"action": "delivered", "run_id": run_id,
                                "worker_id": worker["worker_id"],
                                "delivery_key": result["delivered"].get(
                                    "delivery_key")})
            if result.get("status") == "controller_mismatch":
                return
            current = self.api.get_resource(self._operator(), "worker",
                                            worker["worker_id"])
            if not self._needs_drain(current):
                return
            time.sleep(min(self.poll_seconds, 0.2))

    def reconcile_once(self) -> dict[str, Any]:
        runs = self.reconciliation_runs()
        actions: list[dict[str, Any]] = []
        for run in runs:
            run_id = run["run_id"]
            try:
                result = self.scheduler.reconcile(run_id)
                actions.extend(result.get("actions") or [])
            except ControlPlaneError as error:
                if error.code != "revision_conflict":
                    self._log("scheduler_error", run_id=run_id,
                              code=error.code, message=error.message)
            snapshot = self.api.list_resources(self._operator(), run_id)
            # The Scheduler may have made this Run terminal in this very tick;
            # only the fresh status decides between dispatch and drain.
            terminal = snapshot["run"]["status"] in TERMINAL_RUN_STATUSES
            for worker in snapshot["workers"]:
                if worker["controller_id"] != self.controller_id:
                    continue
                if terminal:
                    # A terminal Run must never receive new work; only a stop
                    # already committed by the Scheduler is still executable.
                    if not self._needs_drain(worker):
                        continue
                    try:
                        self._drain(run_id, worker, actions)
                    except ControlPlaneError as error:
                        self._log("controller_error", run_id=run_id,
                                  worker_id=worker["worker_id"],
                                  code=error.code, message=error.message)
                    except BackendError as error:
                        self._log("controller_error", run_id=run_id,
                                  worker_id=worker["worker_id"],
                                  code=error.code, message=str(error),
                                  details=error.details)
                    continue
                try:
                    result = self.controller.reconcile(
                        run_id, worker["worker_id"])
                    if result.get("delivered"):
                        actions.append({"action": "delivered",
                                        "run_id": run_id,
                                        "worker_id": worker["worker_id"],
                                        "delivery_key": result["delivered"].get(
                                            "delivery_key")})
                except ControlPlaneError as error:
                    self._log("controller_error", run_id=run_id,
                              worker_id=worker["worker_id"],
                              code=error.code, message=error.message)
                except BackendError as error:
                    self._log("controller_error", run_id=run_id,
                              worker_id=worker["worker_id"],
                              code=error.code, message=str(error),
                              details=error.details)
        return {"actions": actions, "runs": len(runs), "status": self.status()}

    def _claim(self) -> dict[str, Any] | None:
        """Publish this process as the host, or report who already owns it.

        The caller holds the bootstrap lock, so the liveness check and the
        record write are one atomic decision across processes.
        """
        existing = self.record()
        if existing and existing.get("pid") and _pid_alive(
                int(existing["pid"]), existing.get("process_start")):
            self._log("host_refused", pid=existing["pid"],
                      reason="already_running")
            return {"error": "host_already_running", "pid": existing["pid"],
                    "log": str(self._log_path)}
        launch = _read_json(self._launch_record)
        if (launch and launch.get("instance_id") != self.instance_id
                and int(launch.get("pid") or 0) != os.getpid()
                and _pid_alive(int(launch.get("pid") or 0),
                               launch.get("process_start"))):
            return {"error": "host_start_in_progress",
                    "pid": launch["pid"], "log": str(self._log_path)}
        record = {
            "format": HOST_FORMAT,
            "instance_id": self.instance_id,
            "pid": os.getpid(),
            "process_start": process_start(os.getpid()),
            "data_dir": str(self.data_dir),
            "backend": self.backend_ref,
            "provider": self.provider,
            "model": self.model,
            "executable": self.executable,
            "config_dir": str(self.config_dir) if self.config_dir else None,
            "script_agent": self.script_agent_identity,
            "controller_id": self.controller_id,
            "scheduler_id": self.scheduler_id,
            "started_at": _now(),
            "stopped_at": None,
        }
        _write_json(self._host_record, record)
        if launch and launch.get("instance_id") == self.instance_id:
            try:
                self._launch_record.unlink()
            except OSError:
                pass
        self._log("host_started", pid=os.getpid(), backend=self.backend_ref,
                  instance_id=self.instance_id)
        return None

    def claim(self) -> dict[str, Any] | None:
        """Atomically publish this process as the directory owner.

        ApiServer calls this once at startup before binding its socket, so a
        data directory has exactly one execution host. Returns a refusal
        record, or None on claim.
        """
        with _bootstrap_lock(self.data_dir, self.lock_timeout):
            return self._claim()

    def release(self) -> None:
        """Mark this process's claim stopped; never touches another process.

        Clearing the owning pid lets a later process (or a test's in-process
        restart) claim the directory again. The instance id and stopped time
        stay for traceability; host.log keeps the event history.
        """
        record = self.record()
        if (record and record.get("pid") == os.getpid()
                and record.get("instance_id") == self.instance_id):
            record["stopped_at"] = _now()
            record["pid"] = None
            record["process_start"] = None
            _write_json(self._host_record, record)


def _host_from_args(args: argparse.Namespace, *,
                    load_script_agent: bool = True) -> Host:
    return Host(
        discovery.resolve_data_dir(getattr(args, "data_dir", None)),
        backend=args.backend, provider=args.provider,
        model=args.model, executable=args.executable,
        controller_id=args.controller_id, scheduler_id=args.scheduler_id,
        scheduler_interval=args.interval, poll_seconds=args.poll,
        max_wall_seconds=args.max_wall_seconds,
        instance_id=getattr(args, "instance_id", None),
        lock_timeout=args.lock_timeout,
        script_agent=getattr(args, "script_agent", None),
        config_dir=getattr(args, "config_dir", None),
        load_script_agent=load_script_agent,
    )


def _add_host_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--data-dir", default=None,
                        help="service data directory "
                             "(default: $XDG_STATE_HOME/agentgenome/default)")
    parser.add_argument("--backend", choices=HOST_BACKENDS, default="pi")
    parser.add_argument("--provider", default=None,
                        help="Pi provider (rejected for CLI backends)")
    parser.add_argument("--model", default=None,
                        help="deployment model; Pi falls back to its local "
                             "default, a CLI uses its own configured default "
                             "when omitted")
    parser.add_argument("--executable", default=None,
                        help="CLI executable (default: codex/qodercli/claude "
                             "per backend; pi for the Pi backend)")
    parser.add_argument("--config-dir", default=None,
                        help="read-only CLI configuration directory "
                             "(CODEX_HOME/CLAUDE_CONFIG_DIR/qoder --config-dir)")
    parser.add_argument("--script-agent", default=None,
                        metavar="FILE.py:FACTORY",
                        help="trusted local program-Checker factory "
                             "(module:factory returning agent(spec, message, state))")
    parser.add_argument("--controller-id", default="ctl-1")
    parser.add_argument("--scheduler-id", default="scheduler")
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--poll", type=float, default=0.2)
    parser.add_argument("--max-wall-seconds", type=float, default=240.0)
    parser.add_argument("--lock-timeout", type=float, default=10.0)


def _command_serve(args: argparse.Namespace) -> int:
    """Own the database and network lifecycle in this one process."""
    from .server import ApiServer

    try:
        host = _host_from_args(args)
    except (ControlPlaneError, BackendError) as error:
        print(json.dumps({
            "error": getattr(error, "code", "backend_unavailable"),
            "message": getattr(error, "message", str(error)),
            **getattr(error, "details", {}),
        }, indent=2), file=sys.stderr)
        return 2
    server = ApiServer(host, bind=args.bind, port=args.port)
    return server.serve()


def _runs_via_http(data_dir: Path) -> tuple[list[dict[str, Any]], str | None]:
    """Read visible runs through the connected service; never open storage."""
    try:
        connection = discovery.connection(data_dir)
    except ControlPlaneError as error:
        return [], error.code
    try:
        client = ApiClient(connection.endpoint, connection.token, timeout=5.0)
        return client.call("list_runs", {})["runs"], None
    except (ApiTransportError, ControlPlaneError) as error:
        return [], getattr(error, "code", "service_unavailable")


def _command_status(args: argparse.Namespace) -> int:
    data_dir = discovery.resolve_data_dir(args.data_dir)
    status = discovery.status(args.data_dir)
    runs, error = _runs_via_http(data_dir)
    status["format"] = STATUS_FORMAT
    status["runs"] = runs
    if error is not None:
        status["runs_error"] = error
    print(json.dumps(status, indent=2, sort_keys=True))
    return 0


def _config_mismatch(record: dict[str, Any],
                     args: argparse.Namespace) -> dict[str, Any] | None:
    mismatch: dict[str, Any] = {}
    provider, model, executable = _resolve_runtime(
        args.backend, args.provider, args.model, args.executable)
    config_dir = _resolved_config_dir(getattr(args, "config_dir", None))
    requested = {
        "backend": args.backend,
        "provider": provider,
        "model": model,
        "executable": executable,
        "config_dir": str(config_dir) if config_dir is not None else None,
        "controller_id": args.controller_id,
        "scheduler_id": args.scheduler_id,
    }
    for field, value in requested.items():
        current = record.get(field)
        if current != value:
            mismatch[field] = {"running": current, "requested": value}
    requested_script = None
    if getattr(args, "script_agent", None):
        requested_script = _script_identity(args.script_agent)
    current_script = record.get("script_agent")
    if current_script != requested_script and (
            current_script is not None or requested_script is not None):
        mismatch["script_agent"] = {"running": current_script,
                                    "requested": requested_script}
    return mismatch or None


def _terminate(process: subprocess.Popen, timeout: float = 5.0) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout)


def _command_start(args: argparse.Namespace) -> int:
    data_dir = discovery.resolve_data_dir(args.data_dir)
    try:
        provider, model, executable = _resolve_runtime(
            args.backend, args.provider, args.model, args.executable)
        config_dir = _resolved_config_dir(getattr(args, "config_dir", None))
    except ControlPlaneError as error:
        print(json.dumps({"error": error.code, "message": error.message,
                          **error.details}, indent=2), file=sys.stderr)
        return 2
    pending: dict[str, Any]
    with _bootstrap_lock(data_dir, args.start_timeout):
        # Bootstrap never constructs Host/Api: only the detached serve process
        # opens the database. This command reads records and probes HTTP.
        status = discovery.status(data_dir)
        config = _read_json(data_dir / "host.json") or {}
        if status["running"]:
            mismatch = _config_mismatch(config, args)
            if mismatch:
                print(json.dumps({
                    "error": "host_config_mismatch",
                    "pid": status["pid"], "mismatch": mismatch,
                    "hint": "stop the running service or start with its configuration",
                }, indent=2), file=sys.stderr)
                return 1
            if not status.get("ready"):
                # Keep the healthy HTTP surface for inspection but never claim
                # an existing service is usable before readiness is observed.
                deadline = time.monotonic() + args.start_timeout
                observed = status
                while time.monotonic() < deadline:
                    observed = discovery.status(data_dir)
                    if not observed["running"]:
                        break
                    if observed.get("ready"):
                        print(json.dumps({**observed, "format": STATUS_FORMAT},
                                         indent=2, sort_keys=True))
                        return 0
                    time.sleep(0.05)
                print(json.dumps({
                    "error": "service_not_ready",
                    "pid": observed.get("pid"),
                    "ready": bool(observed.get("ready")),
                    "data_dir": str(data_dir),
                    "log": str(data_dir / "host.log"),
                    "hint": "inspect the running service; it was not restarted",
                }, indent=2), file=sys.stderr)
                return 1
            print(json.dumps({**status, "format": STATUS_FORMAT},
                             indent=2, sort_keys=True))
            return 0
        launch = _read_json(data_dir / "host.launch.json")
        if launch and _pid_alive(int(launch.get("pid") or 0),
                                 launch.get("process_start")):
            pending = {"kind": "foreign", "pid": int(launch["pid"]),
                       "instance_id": launch.get("instance_id")}
        else:
            token = uuid.uuid4().hex
            command = [sys.executable, "-m",
                       "node_runtime.control_plane.host", "serve",
                       "--data-dir", str(data_dir),
                       "--backend", args.backend,
                       "--controller-id", args.controller_id,
                       "--scheduler-id", args.scheduler_id,
                       "--interval", str(args.interval),
                       "--poll", str(args.poll),
                       "--max-wall-seconds", str(args.max_wall_seconds),
                       "--lock-timeout", str(args.lock_timeout),
                       "--bind", args.bind, "--port", str(args.port),
                       "--instance-id", token]
            if provider is not None:
                command += ["--provider", provider]
            if model is not None:
                command += ["--model", model]
            if executable is not None:
                command += ["--executable", executable]
            if config_dir is not None:
                command += ["--config-dir", str(config_dir)]
            if getattr(args, "script_agent", None):
                identity = _script_identity(args.script_agent)
                command += [
                    "--script-agent",
                    f"{identity['path']}:{identity['factory']}",
                ]
            environment = os.environ.copy()
            package_root = str(Path(__file__).resolve().parents[2])
            existing = environment.get("PYTHONPATH")
            environment["PYTHONPATH"] = (
                package_root if not existing
                else package_root + os.pathsep + existing)
            data_dir.mkdir(parents=True, exist_ok=True)
            with (data_dir / "host.log").open("ab") as log:
                process = subprocess.Popen(
                    command, cwd=package_root, env=environment,
                    stdin=subprocess.DEVNULL, stdout=log,
                    stderr=subprocess.STDOUT, start_new_session=True)
            _write_json(data_dir / "host.launch.json", {
                "instance_id": token, "pid": process.pid,
                "process_start": process_start(process.pid),
                "started_at": _now(),
            })
            pending = {"kind": "own", "process": process, "token": token}

    deadline = time.monotonic() + args.start_timeout
    while time.monotonic() < deadline:
        status = discovery.status(data_dir)
        if pending["kind"] == "foreign":
            current = _read_json(data_dir / "host.json") or {}
            if int(current.get("pid") or 0) == pending["pid"]:
                mismatch = _config_mismatch(current, args)
                if mismatch:
                    print(json.dumps({
                        "error": "host_config_mismatch",
                        "pid": pending["pid"], "mismatch": mismatch,
                        "hint": "the pending service was started with another configuration",
                    }, indent=2), file=sys.stderr)
                    return 1
        if status["running"] and status["ready"]:
            expected = (pending["token"] if pending["kind"] == "own"
                        else pending.get("instance_id"))
            if expected is None or status["instance_id"] == expected:
                print(json.dumps({**status, "format": STATUS_FORMAT},
                                 indent=2, sort_keys=True))
                return 0
        if pending["kind"] == "own" and pending["process"].poll() is not None:
            break
        if pending["kind"] == "foreign" and not _pid_alive(pending["pid"]):
            break
        time.sleep(0.05)

    if pending["kind"] == "own":
        _terminate(pending["process"])
        launch_record = _read_json(data_dir / "host.launch.json")
        if launch_record and launch_record.get("instance_id") == pending["token"]:
            try:
                (data_dir / "host.launch.json").unlink()
            except OSError:
                pass
    print(json.dumps({"error": "service did not become ready",
                      "pid": (pending["process"].pid
                              if pending["kind"] == "own" else pending["pid"]),
                      "log": str(data_dir / "host.log")}, indent=2),
          file=sys.stderr)
    return 1


def _command_stop(args: argparse.Namespace) -> int:
    """Stop the process that owns this directory, without trusting argv.

    The durable host record (pid + kernel start time + instance + resolved
    directory) is the ownership identity. The service record corroborates it
    when live. A foreground or console launch therefore stops the same way as
    a detached one, and an unconfirmed identity is never signalled.
    """
    data_dir = discovery.resolve_data_dir(args.data_dir)
    with _bootstrap_lock(data_dir, args.timeout):
        service = discovery.read_record(data_dir)
        host = _read_json(data_dir / "host.json")
        if not isinstance(host, dict) or host.get("format") != HOST_FORMAT:
            host = None
        service_live = not _ended(service)
        host_live = not _ended(host)

        if not host_live:
            if service_live:
                print(json.dumps({
                    "error": "ownership_identity_incomplete",
                    "detail": "a live service record has no host ownership record",
                    "pid": service.get("pid"),
                }, indent=2), file=sys.stderr)
                return 1
            stale = (host or service or {}).get("pid")
            print(json.dumps({"running": False,
                              **({"stale_pid": stale} if stale else {})},
                             indent=2))
            return 0

        pid = host.get("pid")
        started = host.get("process_start")
        if (not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0
                or not isinstance(started, int) or isinstance(started, bool)):
            print(json.dumps({
                "error": "ownership_identity_incomplete",
                "detail": "host record lacks a pid or kernel start time",
                "pid": pid, "process_start": started,
            }, indent=2), file=sys.stderr)
            return 1
        instance_id = host.get("instance_id")
        if (pid == os.getpid()
                or not _record_dir_matches(host, data_dir)
                or not isinstance(instance_id, str) or not instance_id):
            print(json.dumps({
                "error": "ownership_identity_conflict",
                "detail": "host record does not name this directory and instance",
                "pid": pid, "data_dir": host.get("data_dir"),
                "instance_id": instance_id,
            }, indent=2), file=sys.stderr)
            return 1
        if service_live:
            if (service.get("pid") != pid
                    or service.get("instance_id") != instance_id
                    or not _record_dir_matches(service, data_dir)):
                print(json.dumps({
                    "error": "ownership_identity_conflict",
                    "detail": "service and host records disagree",
                    "pid": pid, "service_pid": service.get("pid"),
                    "service_instance_id": service.get("instance_id"),
                    "host_instance_id": instance_id,
                }, indent=2), file=sys.stderr)
                return 1

        current = process_start(pid)
        if current is None:
            if _process_gone(pid):
                print(json.dumps({"running": False, "stale_pid": pid},
                                 indent=2))
                return 0
            print(json.dumps({
                "error": "ownership_identity_unverified",
                "detail": "the recorded pid is alive but its kernel identity "
                          "could not be read",
                "pid": pid,
            }, indent=2), file=sys.stderr)
            return 1
        if current != started:
            print(json.dumps({"running": False, "recycled_pid": pid},
                             indent=2))
            return 0
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as error:
            print(json.dumps({"error": "stop_not_permitted", "pid": pid,
                              "detail": str(error)}, indent=2),
                  file=sys.stderr)
            return 1

        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            if _process_finished(pid, started):
                print(json.dumps({"running": False, "stopped_pid": pid},
                                 indent=2))
                return 0
            time.sleep(0.1)
        print(json.dumps({"error": "service did not stop before timeout",
                          "pid": pid}, indent=2), file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentgenome", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, function in (("serve", _command_serve),
                           ("start", _command_start),
                           ("status", _command_status),
                           ("stop", _command_stop)):
        item = sub.add_parser(name)
        _add_host_args(item)
        if name in ("serve", "start"):
            item.add_argument("--bind", default="127.0.0.1",
                              help="loopback bind address")
            item.add_argument("--port", type=int, default=0)
        if name == "serve":
            item.add_argument("--instance-id", default=None)
        if name == "start":
            item.add_argument("--start-timeout", type=float, default=10.0)
        if name == "stop":
            item.add_argument("--timeout", type=float, default=15.0)
        item.set_defaults(function=function)
    teacher = sub.add_parser(
        "teacher", help="open the ordinary Teacher Pi session")
    teacher.add_argument("--data-dir", default=None,
                         help="service data directory (default per-user)")
    teacher.add_argument("--workspace", default=None,
                         help="local Teacher workspace (default: current cwd)")
    teacher.add_argument("--provider", default=None)
    teacher.add_argument("--model", default=None)
    teacher.add_argument("--executable", default=os.environ.get("AGENTGENOME_PI", "pi"))
    teacher.add_argument("--session-id", default="agentgenome-teacher")
    teacher.add_argument("message", nargs=argparse.REMAINDER)
    def run_teacher(args: argparse.Namespace) -> int:
        from .teacher_entry import launch
        message = tuple(args.message)
        if message[:1] == ("--",):
            message = message[1:]
        return launch(args.data_dir, workspace=args.workspace,
                      executable=args.executable,
                      provider=args.provider, model=args.model,
                      session_id=args.session_id, message=message)
    teacher.set_defaults(function=run_teacher)
    args = parser.parse_args(argv)
    return args.function(args)


if __name__ == "__main__":
    raise SystemExit(main())
