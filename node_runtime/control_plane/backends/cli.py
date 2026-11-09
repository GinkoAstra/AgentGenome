"""Lifecycle adapter for externally installed Agent CLIs.

The deployment selects the executable and model; Graph data never supplies argv
or code. Each Worker owns a durable execution directory. CLI-specific launch
and event formats live in cli_protocol.py; graph transitions remain in the
Scheduler.

One delivery starts one native CLI turn. The CLI owns its model/tool loop, but
every tool reaches the service through the temporary MCP bridge (worker_mcp.py).
A CLI exit is not Node completion: the Controller still needs a committed
candidate and its separate acceptance.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from ..resources import ControlPlaneError, canonical, digest
from ..tools import TOOL_SPECS
from .base import (AgentBackend, BackendError, Capabilities, CheckpointRef,
                   DeliveryReceipt, ExecutionLog, Handle, Observation,
                   StopReceipt, WorkerSpec)
from .cli_protocol import (MCP_SERVER_NAME, WORKER_MCP_MODULE, CliProtocol,
                           make_protocol)
from .locks import ExecutionLock
from .preparation import WorkerPreparation
from .spawn import process_start

FORMAT = "agentgenome-cli-backend/1"
TOOL_CONTEXT_FORMAT = "agentgenome-cli-tool-context/1"
BUDGET_FORMAT = "agentgenome-cli-budget/1"
MCP_RECORD_FORMAT = "agentgenome-worker-mcp/1"
YIELD_FORMAT = "agentgenome-cli-yield/1"
SPAWN_RECORD_FORMAT = "agentgenome-spawn/1"
SPAWN_LAUNCHER = Path(__file__).with_name("spawn.py")
SAFE_EXECUTION_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
EXECUTION_LOG_MAX_BYTES = 512 * 1024
EXECUTION_LOG_CHUNK_BYTES = 64 * 1024
LOCAL_TOOL_NAMES = ("read_file", "write_file")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _safe_child(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or os.path.isabs(relative):
        raise BackendError("invalid_argument", "result path must be relative")
    candidate = (root / relative).resolve()
    if candidate != root.resolve() and root.resolve() not in candidate.parents:
        raise BackendError("invalid_argument", "result path escapes the workspace")
    return candidate


def _pid_alive(pid: Any, started: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if not isinstance(started, int) or isinstance(started, bool):
        return False
    current = process_start(pid)
    if current is None:
        return False
    return current == started


class _RecordedProcess:
    """A CLI process recorded by an earlier Controller generation."""

    returncode: int | None = None

    def __init__(self, pid: int, started: int | None) -> None:
        self.pid = pid
        self.started = started

    def poll(self) -> int | None:
        return None if _pid_alive(self.pid, self.started) else 0


def _usage_counts(value: Mapping[str, Any] | None) -> dict[str, int | None]:
    value = value if isinstance(value, Mapping) else {}
    tool = value.get("tool_calls")
    if not isinstance(tool, int) or isinstance(tool, bool) or tool < 0:
        tool = 0
    return {"provider_calls": None, "tool_calls": tool}


def _receipts(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    starts: list[dict[str, Any]] = []
    ends: set[str] = set()
    if not path.exists():
        return starts, []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, Mapping):
            continue
        if entry.get("phase") == "start":
            starts.append(dict(entry))
        elif entry.get("phase") == "end":
            ends.add(str(entry.get("call_id")))
    return starts, [entry for entry in starts if str(entry.get("call_id")) not in ends]


class CliBackend(WorkerPreparation, AgentBackend):
    """One common lifecycle, explicit protocol differences per CLI.

    Model and configuration are deployment inputs. ``config_dir`` references an
    existing CLI configuration; it is read, never modified.
    """

    def __init__(self, root: str | Path, *, backend: str,
                 executable: str, model: str | None = None,
                 config_dir: str | Path | None = None,
                 max_wall_seconds: float = 240.0,
                 grace_seconds: float = 5.0,
                 yield_grace_seconds: float = 1.0,
                 artifact_reader: Callable[[Mapping[str, Any]], bytes] | None = None,
                 credential_issuer: Callable[
                     [Mapping[str, Any]], Mapping[str, Any]] | None = None) -> None:
        if backend == "kimi":
            raise BackendError(
                "capability_unsupported",
                "the Kimi CLI adapter is not implemented: the installed "
                "protocol has not been verified")
        self.name = backend
        self.root = Path(root) / "backends" / backend
        self.root.mkdir(parents=True, exist_ok=True)
        self.executable = executable
        self.model = model
        self.config_dir = Path(config_dir).resolve() if config_dir else None
        self.max_wall_seconds = max_wall_seconds
        self.grace_seconds = grace_seconds
        self.yield_grace_seconds = yield_grace_seconds
        self.artifact_reader = artifact_reader
        self.credential_issuer = credential_issuer
        self.max_context_bytes = 8 * 1024 * 1024
        self.tools = LOCAL_TOOL_NAMES
        self.protocol: CliProtocol = make_protocol(
            backend, executable=executable, model=model,
            config_dir=config_dir)
        self.execution_log_kind = f"cli.{backend}.events.jsonl"
        self._lock = threading.RLock()
        self._procs: dict[tuple[str, int],
                          tuple[subprocess.Popen[str], int | None]] = {}

    def set_credential_issuer(self, issuer: Callable[
            [Mapping[str, Any]], Mapping[str, Any]]) -> None:
        self.credential_issuer = issuer

    # -- paths/state -----------------------------------------------------
    def _dir(self, execution_key: tuple[str, int]) -> Path:
        worker_id, epoch = execution_key
        path = self.root / worker_id / str(epoch)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _state_path(self, execution_key: tuple[str, int]) -> Path:
        return self._dir(execution_key) / "state.json"

    def _state(self, execution_key: tuple[str, int]) -> dict[str, Any]:
        state = _read_json(self._state_path(execution_key))
        if state is None:
            raise BackendError("not_found",
                               "CLI backend state does not exist")
        return state

    def _save(self, execution_key: tuple[str, int], state: dict[str, Any]) -> None:
        state["version"] = int(state.get("version", 0)) + 1
        state["updated_at"] = _now()
        _write_json(self._state_path(execution_key), state)

    @contextmanager
    def _guard(self, execution_key: tuple[str, int]) -> Iterator[None]:
        with ExecutionLock(self._dir(execution_key) / ".lock"):
            with self._lock:
                yield

    def _events_path(self, execution_key: tuple[str, int],
                     delivery_key: str) -> Path:
        return self._dir(execution_key) / "events" / f"{delivery_key}.jsonl"

    def _stderr_path(self, execution_key: tuple[str, int],
                     delivery_key: str) -> Path:
        return self._dir(execution_key) / "events" / f"{delivery_key}.stderr"

    def _prompt_path(self, execution_key: tuple[str, int],
                     delivery_key: str) -> Path:
        return self._dir(execution_key) / "prompt" / f"{delivery_key}.txt"

    def _exit_path(self, execution_key: tuple[str, int],
                   delivery_key: str) -> Path:
        return self._dir(execution_key) / "exit" / f"{delivery_key}.json"

    def _spawn_record_path(self, execution_key: tuple[str, int],
                           delivery_key: str) -> Path:
        return self._dir(execution_key) / "spawn" / f"{delivery_key}.json"

    def _budget_path(self, execution_key: tuple[str, int],
                     delivery_key: str) -> Path:
        return self._dir(execution_key) / "budget" / f"{delivery_key}.json"

    def _yield_path(self, execution_key: tuple[str, int],
                    delivery_key: str) -> Path:
        return self._dir(execution_key) / "yield" / f"{delivery_key}.json"

    def _tool_context_path(self, execution_key: tuple[str, int],
                           delivery_key: str) -> Path:
        return self._dir(execution_key) / "tool-context" / f"{delivery_key}.json"

    def _mcp_record_path(self, execution_key: tuple[str, int],
                         delivery_key: str) -> Path:
        return self._dir(execution_key) / "mcp" / f"{delivery_key}.json"

    def _mcp_config_path(self, execution_key: tuple[str, int],
                         delivery_key: str) -> Path:
        return self._dir(execution_key) / "tool-context" / f"{delivery_key}.mcp.json"

    def _receipt_path(self, execution_key: tuple[str, int]) -> Path:
        return self._dir(execution_key) / "tool-receipts.jsonl"

    def _claude_settings_path(self, execution_key: tuple[str, int]) -> Path:
        return self._dir(execution_key) / "config" / "claude-settings.json"

    # -- process identity ------------------------------------------------
    def _identity(self, execution_key: tuple[str, int], delivery_key: str | None,
                  delivery: Mapping[str, Any], *, wait: float = 0.0
                  ) -> tuple[int, int | None] | None:
        pid = delivery.get("pid")
        started = delivery.get("process_start")
        if isinstance(pid, int) and pid > 0 and isinstance(started, int):
            return pid, started
        if not isinstance(delivery_key, str) or not delivery_key:
            return None
        path = self._spawn_record_path(execution_key, delivery_key)
        deadline = time.monotonic() + max(wait, 0.0)
        while True:
            record = _read_json(path)
            if (isinstance(record, dict)
                    and record.get("format") == SPAWN_RECORD_FORMAT
                    and record.get("delivery_key") == delivery_key):
                pid = record.get("pid")
                started = record.get("process_start")
                if isinstance(pid, int) and pid > 0 and isinstance(started, int):
                    return pid, started
                return None
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.02)

    def _current_process(self, execution_key: tuple[str, int],
                         delivery_key: str | None, delivery: Mapping[str, Any],
                         *, wait: float = 0.0,
                         identity: tuple[int, int | None] | None = None,
                         resolved: bool = False) -> Any:
        if not resolved:
            identity = self._identity(execution_key, delivery_key, delivery,
                                      wait=wait)
        if identity is None:
            return None
        pid, started = identity
        cached = self._procs.get(execution_key)
        if cached is not None:
            process, cached_start = cached
            if (process.pid == pid and isinstance(started, int)
                    and cached_start == started and _pid_alive(pid, started)):
                return process
            # The cached Popen is only a convenience: the kernel start time is
            # the identity that decides whether this is still our process.
            self._procs.pop(execution_key, None)
        if _pid_alive(pid, started):
            return _RecordedProcess(pid, started)
        return None

    def _alive(self, process: Any, started: int | None = None) -> bool:
        """Observe a process without reaping it.

        Only the exit watcher may reap the CLI leader: it does so after the
        whole process group is verified clean. Every other path uses /proc so
        a zombie leader still counts as present until that verification.
        """
        if process is None:
            return False
        if isinstance(process, _RecordedProcess):
            return _pid_alive(process.pid, process.started)
        if not isinstance(started, int) or isinstance(started, bool):
            for cached, cached_start in self._procs.values():
                if cached is process:
                    started = cached_start
                    break
        if isinstance(started, int) and not isinstance(started, bool):
            return _pid_alive(process.pid, started)
        # Without a recorded start time a live pid is not evidence that this
        # is still the same process; presence stays unknown.
        return False

    def _mcp_member(self, execution_key: tuple[str, int],
                    delivery_key: str | None,
                    state: Mapping[str, Any]) -> tuple[int, int, int] | None:
        """Exact identity of the MCP bridge that is still in the group.

        The bridge records its own pid, kernel start and pgid at startup. That
        record is the only safe anchor when the CLI leader is already gone:
        a bare pgid may have been reused by an unrelated process.
        """
        if not isinstance(delivery_key, str) or not delivery_key:
            return None
        record = _read_json(self._mcp_record_path(execution_key, delivery_key))
        if not isinstance(record, dict) or record.get("format") != MCP_RECORD_FORMAT:
            return None
        pid, started, pgid = (record.get("pid"), record.get("process_start"),
                              record.get("pgid"))
        if not isinstance(pid, int) or not isinstance(started, int) \
                or not isinstance(pgid, int):
            return None
        for field in ("run_id", "node_id", "worker_id", "epoch"):
            if record.get(field) != state.get(field):
                return None
        if record.get("delivery_key") != delivery_key:
            return None
        if not _pid_alive(pid, started):
            return None
        if self._group_of(pid) != pgid:
            return None
        return pid, started, pgid

    def _handle(self, spec: WorkerSpec, state: Mapping[str, Any]) -> Handle:
        current = state.get("current")
        delivery = state["deliveries"].get(current) if current else {}
        process = self._current_process((spec.worker_id, spec.epoch), current,
                                        delivery or {})
        pid = (process.pid if self._alive(
            process, (delivery or {}).get("process_start")) else None)
        return Handle(worker_id=spec.worker_id, epoch=spec.epoch,
                      workspace=state["workspace"],
                      session_ref=state.get("session_id"),
                      pid=pid, state="active" if pid else "idle")

    # -- budget/capabilities ---------------------------------------------
    @staticmethod
    def _caps(budget: Mapping[str, Any]) -> dict[str, int]:
        if "max_provider_calls" in budget:
            raise BackendError(
                "capability_unsupported",
                "a CLI backend cannot truncate a provider request before it is "
                "sent; max_provider_calls is not supported",
                budget="max_provider_calls")
        caps: dict[str, int] = {}
        if "max_tool_calls" in budget:
            value = budget["max_tool_calls"]
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise BackendError("invalid_argument",
                                   "max_tool_calls must be a non-negative integer")
            caps["tool_calls"] = value
        return caps

    def _wall_limit(self, state: Mapping[str, Any]) -> float:
        budget = state.get("budget", {})
        limit = budget.get("max_wall_seconds", self.max_wall_seconds)
        try:
            return float(limit)
        except (TypeError, ValueError):
            return float(self.max_wall_seconds)

    def capabilities(self) -> Capabilities:
        return Capabilities(
            resumable_session=True,
            live_observation="limited",
            idempotent_delivery=False,
            stop="process",
            # Only the live native session can be continued. Historical
            # checkpoints would claim a state the CLI cannot reproduce.
            checkpoint=False,
            backend_tools=tuple(self.tools),
            # Native shell/subagent/background work is disabled per protocol
            # and every tool reaches the service through the one MCP bridge,
            # so the activity projection is complete and empty.
            internal_activities="none",
        )

    # -- lifecycle -------------------------------------------------------
    def _verify_identity(self, state: Mapping[str, Any], spec: WorkerSpec,
                         execution_key: tuple[str, int]) -> None:
        """The directory key and persisted state must be the same execution.

        ``_verify_binding`` compares agent/context/profile only; a reused
        directory could otherwise carry state across run/node boundaries when
        those fields happen to match.
        """
        self._verify_key(spec, execution_key)
        if spec.backend_ref != self.name:
            raise BackendError(
                "capability_unsupported",
                "the committed execution names another backend",
                field="backend_ref", persisted=state.get("backend"),
                committed=spec.backend_ref)
        expected = {
            "backend": self.name,
            "worker_id": spec.worker_id,
            "node_id": spec.node_id,
            "run_id": spec.run_id,
            "epoch": spec.epoch,
        }
        for field, value in expected.items():
            if state.get(field) != value:
                raise BackendError(
                    "capability_unsupported",
                    "the persisted execution identity does not match the "
                    "committed worker",
                    field=field, persisted=state.get(field), committed=value)

    @staticmethod
    def _verify_key(spec: WorkerSpec, execution_key: tuple[str, int]) -> None:
        if (spec.worker_id, spec.epoch) != execution_key:
            raise BackendError(
                "invalid_argument",
                "execution_key does not match the committed worker epoch",
                execution_key=list(execution_key),
                worker_id=spec.worker_id, epoch=spec.epoch)

    def start(self, spec: WorkerSpec, execution_key: tuple[str, int]) -> Handle:
        self._verify_key(spec, execution_key)
        with self._guard(execution_key):
            state = _read_json(self._state_path(execution_key))
            stopping = spec.desired == "stopped"
            if state is None:
                if not stopping:
                    # A hard provider-call budget cannot be enforced before a
                    # CLI request; refuse it before preparing active work.
                    self._caps(spec.budget)
                    self.protocol.check()
                state = self._new_state(spec, execution_key)
                state["budget"] = dict(spec.budget)
                if not stopping:
                    self._prepare_active(state, spec)
                self._save(execution_key, state)
            else:
                self._verify_binding(state, spec)
                self._verify_identity(state, spec, execution_key)
                changed = False
                if not stopping:
                    self._caps(spec.budget)
                    if not state.get("prepared", True):
                        self._prepare_active(state, spec)
                        changed = True
                    else:
                        self._tools(state)
                        self._system_grants(state)
                        self._verify_materials(state)
                    if "usage_base" not in state:
                        state["usage_base"] = _usage_counts(spec.usage)
                        changed = True
                    if state.get("budget") != dict(spec.budget):
                        state["budget"] = dict(spec.budget)
                        changed = True
                if changed:
                    self._save(execution_key, state)
            return self._handle(spec, state)

    def _new_state(self, spec: WorkerSpec,
                   execution_key: tuple[str, int]) -> dict[str, Any]:
        worker_dir = self._dir(execution_key)
        state = {
            "format": FORMAT,
            "backend": self.name,
            "worker_id": spec.worker_id,
            "node_id": spec.node_id,
            "run_id": spec.run_id,
            "epoch": spec.epoch,
            "intent_seq": spec.intent_seq,
            "agent_ref": spec.agent_ref,
            "context": dict(spec.context),
            "tool_profile": dict(spec.tool_profile),
            "materials": [],
            "prepared": False,
            "workspace": str(worker_dir / "workspace"),
            "session_id": None,
            "session_requested": None,
            "session_evidence": None,
            "deliveries": {},
            "order": [],
            "current": None,
            "stop": None,
            "reconciled_calls": [],
            "last_observation_digest": None,
            "usage_base": _usage_counts(spec.usage),
            "budget": {},
            "version": 0,
            "created_at": _now(),
        }
        for name in ("workspace", "events", "exit", "prompt", "spawn",
                     "tool-context", "budget", "yield", "config", "mcp"):
            (worker_dir / name).mkdir(parents=True, exist_ok=True)
        return state

    def _prepare_active(self, state: dict[str, Any], spec: WorkerSpec) -> None:
        self.protocol.check()
        self._tools(state)
        self._system_grants(state)
        self._materialize(state, spec)
        state["prepared"] = True

    # -- native session evidence -----------------------------------------
    def _native_session(self, state: dict[str, Any]) -> str | None:
        current = state.get("session_id")
        if isinstance(current, str) and current:
            return current
        for key in state.get("order", []):
            delivery = state["deliveries"].get(key)
            if not isinstance(delivery, Mapping):
                continue
            ref = delivery.get("events_ref")
            if not isinstance(ref, str):
                continue
            found = self.protocol.summarize(Path(ref))["session_id"]
            if found:
                state["session_id"] = found
                if state.get("session_evidence") is None:
                    state["session_evidence"] = "native"
                return found
        return None

    # -- delivery --------------------------------------------------------
    def deliver(self, handle: Handle, message: Mapping[str, Any],
                delivery_key: str) -> DeliveryReceipt:
        if not isinstance(delivery_key, str) or not delivery_key:
            raise BackendError("invalid_argument", "delivery_key is required")
        if (not isinstance(message, Mapping)
                or not isinstance(message.get("brief"), str)
                or not message["brief"].strip()):
            raise BackendError("invalid_argument",
                               "message.brief must be a non-empty string")
        with self._guard(handle.execution_key):
            state = self._state(handle.execution_key)
            existing = state["deliveries"].get(delivery_key)
            if existing is not None:
                return DeliveryReceipt(
                    delivery_key=delivery_key, accepted=True,
                    outcome="duplicate", delivery_id=existing["delivery_id"],
                    duplicate=True, error=existing.get("error"))
            if state.get("stop"):
                raise BackendError(
                    "stopped", "stop was requested; no new delivery is accepted")
            if state.get("prepared", True) is False:
                raise BackendError(
                    "capability_unsupported",
                    "the execution was created only to record a stop and is not "
                    "prepared for work; start it active before delivering",
                    prepared=False)
            current = state.get("current")
            if current is not None:
                delivery = state["deliveries"][current]
                process = self._current_process(
                    handle.execution_key, current, delivery)
                alive = self._alive(process, delivery.get("process_start"))
                exit_record = _read_json(
                    self._exit_path(handle.execution_key, current))
                if alive or (delivery.get("state") == "running"
                             and exit_record is None):
                    raise BackendError(
                        "busy", "the previous delivery is still running",
                        delivery_key=current)
                if exit_record and exit_record.get("group_cleaned") is False:
                    raise BackendError(
                        "unknown_effect",
                        "the previous process group could not be verified "
                        "clean; reconcile it before starting another turn",
                        delivery_key=current)
            unmatched = self._unmatched(handle.execution_key, state)
            if unmatched:
                raise BackendError(
                    "unknown_effect",
                    "a previous tool effect has an unknown outcome; reconcile "
                    "before resuming",
                    tools=[entry.get("name") for entry in unmatched])
            caps = self._caps(state.get("budget", {}))
            usage = self._usage(handle.execution_key, state)
            if usage["tool_calls"] is not None \
                    and "tool_calls" in caps \
                    and usage["tool_calls"] >= caps["tool_calls"]:
                raise BackendError(
                    "budget_exhausted",
                    "the worker's tool-call budget is exhausted; no new "
                    "delivery is accepted",
                    budget=["tool_calls"], usage=usage, caps=caps)
            # Resolve or reject session continuity before writing a running
            # record, so a rejected resume never looks like an attempt.
            evidence = self._native_session(state)
            if evidence is None and any(
                    delivery.get("spawned") for delivery in
                    state["deliveries"].values()):
                raise BackendError(
                    "capability_unsupported",
                    "a previous CLI attempt produced no native session id; "
                    "continuing that session cannot be guaranteed",
                    session_evidence="missing")
            system_names, system_specs, binding, decision = \
                self._system_tools(state, message)
            self._verify_materials(state)
            materials = self._materialize_message(
                state, delivery_key, message)
            if not evidence:
                requested = state.get("session_requested")
                if not isinstance(requested, str) or not requested:
                    requested = self.protocol.requested_session()
                state["session_requested"] = requested
                session_id = requested
                resume = False
            else:
                session_id = evidence
                resume = True
            delivery_id = len(state["order"]) + 1
            events = self._events_path(handle.execution_key, delivery_key)
            stderr = self._stderr_path(handle.execution_key, delivery_key)
            events.parent.mkdir(parents=True, exist_ok=True)
            state["deliveries"][delivery_key] = {
                "delivery_id": delivery_id,
                "delivery_key": delivery_key,
                "message": dict(message),
                "prompt_ref": str(self._prompt_path(
                    handle.execution_key, delivery_key)),
                "state": "running",
                "pid": None,
                "spawned": False,
                "session_id": session_id,
                "session_resume": resume,
                "started_at": _now(),
                "started_ts": time.time(),
                "wall_timeout": self._wall_limit(state),
                "finished_at": None,
                "events_ref": str(events),
                "stderr_ref": str(stderr),
                "usage_before": usage,
                "materials": materials,
                "system_tools": list(system_names),
                "tools_exposed": bool(system_specs or self._tools(state)),
            }
            state["order"].append(delivery_key)
            state["current"] = delivery_key
            self._save(handle.execution_key, state)
            self._spawn(handle, state, delivery_key, message, events, stderr,
                        system_names, system_specs, binding, decision)
            return DeliveryReceipt(delivery_key=delivery_key, accepted=True,
                                   outcome="running", delivery_id=delivery_id)

    def _spawn(self, handle: Handle, state: dict[str, Any], delivery_key: str,
               message: Mapping[str, Any], events: Path, stderr: Path,
               system_names: list[str], system_specs: list[dict[str, Any]],
               binding: Mapping[str, Any] | None,
               decision: Mapping[str, Any] | None) -> None:
        execution_key = handle.execution_key
        worker_dir = self._dir(execution_key)
        result_path = message.get("result_path")
        if isinstance(result_path, str) and result_path:
            stale = _safe_child(Path(state["workspace"]), result_path)
            if stale.exists() and stale.is_file():
                stale.unlink()
        backend_tools = self._tools(state)
        delivered = state["deliveries"][delivery_key].get("materials") or []
        prompt = self._prompt(state, message, backend_tools,
                              system_tools=tuple(system_names),
                              review=binding, decision=decision,
                              delivered=delivered)
        prompt_path = self._prompt_path(execution_key, delivery_key)
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(prompt, encoding="utf-8")
        budget = state.get("budget", {})
        limit = self._wall_limit(state)
        caps = self._caps(budget)
        prior = state["deliveries"][delivery_key].get("usage_before", {})
        prior_tools = prior.get("tool_calls")
        budget_path = self._budget_path(execution_key, delivery_key)
        _write_json(budget_path, {
            "format": BUDGET_FORMAT,
            "delivery_key": delivery_key,
            "caps": dict(caps),
            "prior": {"tool_calls": int(prior_tools or 0)},
            "counts": {"tool_calls": 0},
            "budget": [],
            "budget_exceeded": False,
            "extension": False,
        })
        tool_context = self._tool_context_path(execution_key, delivery_key)
        exposed = tuple(backend_tools) + tuple(system_names)
        if exposed:
            credential: Mapping[str, Any] = {}
            if system_specs:
                if self.credential_issuer is None:
                    raise BackendError(
                        "capability_unsupported",
                        "system tools require the running network service issuer")
                context = state.get("context") or {}
                credential = self.credential_issuer({
                    "run_id": state["run_id"],
                    "node_id": state["node_id"],
                    "worker_id": state["worker_id"],
                    "epoch": state["epoch"],
                    "delivery_key": delivery_key,
                    "workspace": state["workspace"],
                    "contract_ref": context.get("contract_ref"),
                    "review": binding,
                    "decision": decision,
                })
                endpoint = credential.get("endpoint")
                token = credential.get("token")
                if not isinstance(endpoint, str) or not endpoint:
                    raise BackendError(
                        "backend_unavailable",
                        "the service issued no worker endpoint")
                if not isinstance(token, str) or not token:
                    raise BackendError(
                        "backend_unavailable",
                        "the service issued no worker credential")
            tool_context.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(tool_context.parent, 0o700)
            _write_json(tool_context, {
                "format": TOOL_CONTEXT_FORMAT,
                "endpoint": credential.get("endpoint"),
                "token": credential.get("token"),
                "run_id": state["run_id"],
                "node_id": state["node_id"],
                "worker_id": state["worker_id"],
                "epoch": state["epoch"],
                "delivery_key": delivery_key,
                "workspace": state["workspace"],
                "tools": system_specs,
                "backend_tools": list(backend_tools),
            })
            os.chmod(tool_context, 0o600)
        else:
            tool_context.unlink(missing_ok=True)
        yield_path = self._yield_path(execution_key, delivery_key)
        yield_path.parent.mkdir(parents=True, exist_ok=True)
        yield_path.unlink(missing_ok=True)
        mcp_config_path = None
        mcp_server = None
        mcp_record = self._mcp_record_path(execution_key, delivery_key)
        mcp_record.unlink(missing_ok=True)
        mcp_environment = {
            "AGENTGENOME_CLI_BACKEND": self.name,
            "AGENTGENOME_WORKER_ID": state["worker_id"],
            "AGENTGENOME_NODE_ID": state["node_id"],
            "AGENTGENOME_RUN_ID": state["run_id"],
            "AGENTGENOME_EPOCH": str(state["epoch"]),
            "AGENTGENOME_WORKSPACE": state["workspace"],
            "AGENTGENOME_TOOL_RECEIPT": str(self._receipt_path(execution_key)),
            "AGENTGENOME_BUDGET_FILE": str(budget_path),
            "AGENTGENOME_YIELD_FILE": str(yield_path),
            "AGENTGENOME_MCP_RECORD": str(mcp_record),
        }
        if exposed:
            mcp_environment["AGENTGENOME_TOOL_CONTEXT"] = str(tool_context)
            mcp_server = {"command": sys.executable,
                          "args": [str(WORKER_MCP_MODULE)],
                          "env": mcp_environment}
        # Qoder/Claude still get an explicit empty MCP document under
        # --strict-mcp-config when this Worker has no granted tools; no grants
        # must not mean "inherit whatever the user configured".
        document = self.protocol.mcp_config(mcp_server)
        if document is not None:
            mcp_config_path = self._mcp_config_path(
                execution_key, delivery_key)
            mcp_config_path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(mcp_config_path.parent, 0o700)
            mcp_config_path.write_text(document, encoding="utf-8")
            os.chmod(mcp_config_path, 0o600)
        settings_path = None
        if self.name == "claude":
            settings_path = self._claude_settings(execution_key)
        delivery = state["deliveries"][delivery_key]
        session_id = delivery.get("session_id")
        resume = bool(delivery.get("session_resume"))
        args = self.protocol.argv(
            workspace=Path(state["workspace"]),
            session_id=session_id if isinstance(session_id, str) else None,
            resume=resume, exposed=exposed,
            mcp_config_path=mcp_config_path, mcp_server=mcp_server,
            settings_path=settings_path)
        environment = dict(os.environ)
        environment.update({
            "AGENTGENOME_CLI_BACKEND": self.name,
            "AGENTGENOME_WORKER_ID": state["worker_id"],
            "AGENTGENOME_NODE_ID": state["node_id"],
            "AGENTGENOME_RUN_ID": state["run_id"],
            "AGENTGENOME_EPOCH": str(state["epoch"]),
            "AGENTGENOME_WORKSPACE": state["workspace"],
            "AGENTGENOME_TOOL_RECEIPT": str(self._receipt_path(execution_key)),
            "AGENTGENOME_BUDGET_FILE": str(budget_path),
            "AGENTGENOME_YIELD_FILE": str(yield_path),
        })
        if exposed:
            environment["AGENTGENOME_TOOL_CONTEXT"] = str(tool_context)
        else:
            environment.pop("AGENTGENOME_TOOL_CONTEXT", None)
        environment.update(self.protocol.environment())
        spawn_record = self._spawn_record_path(execution_key, delivery_key)
        spawn_record.unlink(missing_ok=True)
        launch = [sys.executable, str(SPAWN_LAUNCHER), str(spawn_record),
                  delivery_key, *args]
        prompt_stream = open(prompt_path, "r", encoding="utf-8")
        try:
            with open(events, "w", encoding="utf-8") as out, \
                    open(stderr, "w", encoding="utf-8") as err:
                try:
                    process = subprocess.Popen(
                        launch, cwd=state["workspace"], stdin=prompt_stream,
                        stdout=out, stderr=err, text=True,
                        start_new_session=True, env=environment)
                except OSError as error:
                    delivery["state"] = "failed"
                    delivery["error"] = {
                        "code": "backend_unavailable",
                        "message": f"could not start {self.name}: {error}"}
                    delivery["finished_at"] = _now()
                    self._save(execution_key, state)
                    raise BackendError(
                        "backend_unavailable",
                        f"could not start {self.name}") from error
        finally:
            prompt_stream.close()
        started = process_start(process.pid)
        self._procs[execution_key] = (process, started)
        delivery["pid"] = process.pid
        delivery["process_start"] = started
        delivery["spawned"] = True
        self._save(execution_key, state)
        threading.Thread(
            target=self._watch,
            args=(process, self._exit_path(execution_key, delivery_key),
                  events, limit, self.grace_seconds, caps, prior,
                  budget_path, yield_path, self.yield_grace_seconds),
            name=f"cli-exit-{self.name}-{delivery_key}", daemon=True).start()

    @staticmethod
    def _watch(process: subprocess.Popen[str], exit_path: Path,
               events: Path, timeout: float, grace: float,
               caps: Mapping[str, int], prior: Mapping[str, Any],
               budget_path: Path, yield_path: Path,
               yield_grace: float) -> None:
        reason = None
        counts = {"tool_calls": int((prior or {}).get("tool_calls") or 0)}
        delivery_counts = {"tool_calls": 0}
        started = time.monotonic()
        yield_seen = None
        try:
            while True:
                if CliBackend._leader_state(process.pid) in (
                        "Z", "X", "x", "gone"):
                    # The leader may be a zombie: keep it unreaped so its pid
                    # cannot be reused while the group is cleaned below.
                    code = None
                    break
                record = _read_json(budget_path)
                if isinstance(record, dict) and record.get("extension") is True:
                    current = int((record.get("counts") or {}).get(
                        "tool_calls", 0) or 0)
                    delivery_counts = {"tool_calls": current}
                    counts = {"tool_calls": int(
                        (prior or {}).get("tool_calls") or 0) + current}
                    if record.get("budget_exceeded"):
                        reason = "budget_exceeded"
                        code, cleaned = CliBackend._kill(process, grace)
                        break
                if yield_seen is None and yield_path.exists():
                    # A committed decision request ends the turn. Give the CLI
                    # a short moment to return the tool result and exit, then
                    # stop a model that keeps going.
                    yield_seen = time.monotonic()
                if (yield_seen is not None
                        and time.monotonic() - yield_seen > yield_grace):
                    reason = "yield"
                    code, cleaned = CliBackend._kill(process, grace)
                    break
                if time.monotonic() - started > timeout:
                    reason = "wall_timeout"
                    code, cleaned = CliBackend._kill(process, grace)
                    break
                time.sleep(0.05)
            if code is None:
                # A normally exiting CLI may leave its MCP bridge (or any
                # other child) running in the same session group. Kill the
                # group before the pid is reaped and before reporting idle.
                cleaned = CliBackend._cleanup_group(process.pid, grace)
                code = process.wait()
            record = _read_json(budget_path)
            if isinstance(record, dict) and record.get("extension") is True:
                current = int((record.get("counts") or {}).get(
                    "tool_calls", 0) or 0)
                delivery_counts = {"tool_calls": current}
                counts = {"tool_calls": int(
                    (prior or {}).get("tool_calls") or 0) + current}
                if record.get("budget_exceeded"):
                    reason = "budget_exceeded"
                budget = list(record.get("budget") or [])
            else:
                budget = []
            _write_json(exit_path, {
                "code": code,
                "signal": -code if code < 0 else None,
                "timed_out": reason == "wall_timeout",
                "budget_exceeded": reason == "budget_exceeded",
                "yielded": reason == "yield" or yield_path.exists(),
                # Terminal observations are only allowed after this scan.
                "group_cleaned": cleaned,
                "budget": budget,
                "timeout_seconds": timeout,
                "provider_calls": None,
                "counts": counts,
                "delivery_counts": delivery_counts,
                "usage_prior": dict(prior or {}),
                "caps": dict(caps),
                "finished_at": _now(),
            })
        finally:
            try:
                process.stdout and process.stdout.close()
            except Exception:
                pass

    @staticmethod
    def _leader_state(pid: int) -> str:
        """Kernel state of the leader; never reap it just to observe it."""
        try:
            text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        except OSError:
            return "gone"
        try:
            return text[text.rindex(")") + 2]
        except (ValueError, IndexError):
            return "gone"

    @staticmethod
    def _group_of(pid: int) -> int | None:
        try:
            text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            return int(text[text.rindex(")") + 2:].split()[2])
        except (ValueError, IndexError):
            return None

    @staticmethod
    def _group_active(pgid: int) -> bool:
        """True while a non-zombie member of the process group exists.

        The CLI leader is deliberately left unreaped so the pgid cannot be
        recycled while the group is being torn down. Zombie members are
        ignored: they cannot run and only wait for their parent to reap them.
        """
        try:
            entries = os.listdir("/proc")
        except OSError:
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                return False
            except PermissionError:
                return True
            return True
        for entry in entries:
            if not entry.isdigit():
                continue
            try:
                text = Path(f"/proc/{entry}/stat").read_text(encoding="utf-8")
            except OSError:
                continue
            try:
                fields = text[text.rindex(")") + 2:].split()
                state, group = fields[0], int(fields[2])
            except (ValueError, IndexError):
                continue
            if group == pgid and state not in ("Z", "X", "x"):
                return True
        return False

    @staticmethod
    def _signal_group(pgid: int, sig: int) -> None:
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            pass

    @classmethod
    def _cleanup_group(cls, pgid: int, grace: float) -> bool:
        """Terminate every remaining member and return cleanup evidence.

        A normally exiting CLI can leave its MCP bridge (or any other child)
        running; the leader alone is not evidence that the turn is over. The
        final scan decides whether a terminal observation is allowed at all.
        """
        if not cls._group_active(pgid):
            return True
        cls._signal_group(pgid, signal.SIGTERM)
        deadline = time.monotonic() + max(float(grace), 0.0)
        while time.monotonic() < deadline:
            if not cls._group_active(pgid):
                return True
            time.sleep(0.02)
        cls._signal_group(pgid, signal.SIGKILL)
        deadline = time.monotonic() + max(float(grace), 1.0)
        while time.monotonic() < deadline:
            if not cls._group_active(pgid):
                return True
            time.sleep(0.02)
        return not cls._group_active(pgid)

    @classmethod
    def _kill(cls, process: subprocess.Popen[str],
              grace: float) -> tuple[int, bool]:
        """Stop the leader and the whole group, then reap the leader."""
        pid = process.pid
        cls._signal_group(pid, signal.SIGTERM)
        deadline = time.monotonic() + max(float(grace), 0.0)
        while time.monotonic() < deadline:
            if cls._leader_state(pid) in ("Z", "X", "x", "gone"):
                break
            time.sleep(0.02)
        else:
            cls._signal_group(pid, signal.SIGKILL)
        # Keep the leader unreaped until the group is gone: its pid is the
        # process-group id, and reaping it would let the kernel reuse both.
        cleaned = cls._cleanup_group(pid, grace)
        try:
            code = process.wait(timeout=max(float(grace), 1.0))
        except subprocess.TimeoutExpired:
            cls._signal_group(pid, signal.SIGKILL)
            code = process.wait()
        return code, cleaned

    def _expire(self, process: Any) -> tuple[int, bool]:
        if isinstance(process, _RecordedProcess):
            # Not our child: only signal while the exact recorded identity is
            # still alive; once it is gone its pgid may belong to someone else.
            if not _pid_alive(process.pid, process.started):
                return -int(signal.SIGTERM), not self._group_active(process.pid)
            self._signal_group(process.pid, signal.SIGTERM)
            deadline = time.monotonic() + self.grace_seconds
            while time.monotonic() < deadline:
                if not _pid_alive(process.pid, process.started) \
                        and not self._group_active(process.pid):
                    return -int(signal.SIGTERM), True
                time.sleep(0.05)
            cleaned = self._cleanup_group(process.pid, self.grace_seconds)
            return -int(signal.SIGTERM), cleaned
        return self._kill(process, self.grace_seconds)

    @classmethod
    def _terminate(cls, process: Any, sig: int) -> None:
        cls._signal_group(process.pid, sig)

    # -- usage -----------------------------------------------------------
    def _delivery_tools(self, execution_key: tuple[str, int],
                        delivery: Mapping[str, Any]) -> int | None:
        record = _read_json(self._budget_path(
            execution_key, str(delivery.get("delivery_key"))))
        if isinstance(record, dict) and record.get("extension") is True:
            value = (record.get("counts") or {}).get("tool_calls", 0)
            return int(value or 0)
        if delivery.get("tools_exposed") is False:
            return 0
        # The bridge file is written before spawn; a missing record for an
        # exposed delivery means the count cannot be known.
        return None

    def _usage(self, execution_key: tuple[str, int],
               state: Mapping[str, Any]) -> dict[str, int | None]:
        base = state.get("usage_base") or {}
        total = int(base.get("tool_calls") or 0) if isinstance(
            base.get("tool_calls"), int) else 0
        unknown = False
        for key in state.get("order", []):
            delivery = state["deliveries"].get(key)
            if not isinstance(delivery, Mapping):
                continue
            count = self._delivery_tools(
                execution_key, {**delivery, "delivery_key": key})
            if count is None:
                unknown = True
            else:
                total += count
        return {"provider_calls": None,
                "tool_calls": None if unknown else total}

    def _unmatched(self, execution_key: tuple[str, int],
                   state: Mapping[str, Any]) -> list[dict[str, Any]]:
        resolved = {str(call) for call in state.get("reconciled_calls", [])}
        _, unmatched = _receipts(self._receipt_path(execution_key))
        return [entry for entry in unmatched
                if str(entry.get("call_id")) not in resolved]

    # -- Claude settings isolation ---------------------------------------
    def _claude_settings(self, execution_key: tuple[str, int]) -> Path:
        path = self._claude_settings_path(execution_key)
        if path.is_file():
            return path
        source = None
        candidates = []
        if self.config_dir is not None:
            candidates.append(self.config_dir / "settings.json")
        candidates.append(Path.home() / ".claude" / "settings.json")
        for candidate in candidates:
            if not candidate.is_file():
                continue
            try:
                source = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                raise BackendError(
                    "backend_unavailable",
                    "the Claude settings file is unreadable",
                    path=str(candidate)) from error
            break
        settings: dict[str, Any] = {
            "disableAllHooks": True,
            "enabledPlugins": {},
        }
        if isinstance(source, Mapping):
            env = source.get("env")
            if isinstance(env, Mapping):
                settings["env"] = {str(key): value for key, value in env.items()
                                   if isinstance(value, str)}
            helper = source.get("apiKeyHelper")
            if isinstance(helper, str) and helper:
                settings["apiKeyHelper"] = helper
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        _write_json(path, settings)
        os.chmod(path, 0o600)
        return path

    # -- observation -----------------------------------------------------
    def observe(self, handle: Handle, after: int | None = None) -> Observation:
        with self._guard(handle.execution_key):
            state = self._state(handle.execution_key)
            before = state.get("session_id")
            self._native_session(state)
            observation = self._compute(handle, state)
            if state.get("session_id") != before:
                self._save(handle.execution_key, state)
            observation = Observation(**{
                **observation.as_dict(),
                "activities": (),
                "activities_complete": True,
            })
            value = canonical(observation.as_dict())
            if digest(value) != state.get("last_observation_digest"):
                state["last_observation_digest"] = digest(value)
                self._save(handle.execution_key, state)
            observation = Observation(**{
                **observation.as_dict(),
                "observation_id": int(state["version"]),
                "activities": tuple(observation.activities),
            })
            return observation

    def _compute(self, handle: Handle, state: dict[str, Any]) -> Observation:
        execution_key = handle.execution_key
        current = state.get("current")
        stop = state.get("stop")
        usage = self._usage(execution_key, state)
        provider_calls = usage["provider_calls"]
        tool_calls = usage["tool_calls"]
        session_ref = state.get("session_id")
        checkpoint_ref = None  # checkpoints are not supported
        if current is None:
            observed = "stopped" if stop else "idle"
            return Observation(
                observation_id=0, observed=observed,
                session_ref=session_ref, checkpoint_ref=checkpoint_ref,
                error={"code": "stopped_before_result"} if stop else None,
                provider_calls=provider_calls, tool_calls=tool_calls)
        delivery = state["deliveries"][current]
        events = self.protocol.summarize(Path(delivery["events_ref"]))
        identity = self._identity(execution_key, current, delivery, wait=2.0)
        process = self._current_process(execution_key, current, delivery,
                                        identity=identity, resolved=True)
        exit_record = _read_json(self._exit_path(execution_key, current))
        alive = self._alive(process, identity[1] if identity else None)
        if (process is not None and not alive and exit_record is None
                and not isinstance(process, _RecordedProcess)):
            # Never synthesize a terminal exit from the leader's return code:
            # only the watcher writes one, and only after it proves the whole
            # group is clean.
            deadline = time.monotonic() + 1.0
            while exit_record is None and time.monotonic() < deadline:
                time.sleep(0.02)
                exit_record = _read_json(
                    self._exit_path(execution_key, current))
        timeout = delivery.get("wall_timeout")
        started_ts = delivery.get("started_ts")
        if (alive and not stop and isinstance(timeout, (int, float))
                and timeout > 0 and isinstance(started_ts, (int, float))
                and time.time() - started_ts > float(timeout)):
            code, cleaned = self._expire(process)
            if not cleaned:
                return Observation(
                    observation_id=0, observed="unknown",
                    session_ref=session_ref, checkpoint_ref=checkpoint_ref,
                    delivery_key=current, provider_calls=provider_calls,
                    tool_calls=tool_calls,
                    error={"code": "group_cleanup_incomplete",
                           "message": "the wall-clock limit expired, but the "
                                      "process group could not be verified "
                                      "clean",
                           "timeout_seconds": float(timeout)},
                    event_ref=delivery["events_ref"])
            exit_record = {
                "code": code, "signal": -code, "timed_out": True,
                "budget_exceeded": False, "timeout_seconds": float(timeout),
                "provider_calls": None, "counts": dict(usage),
                "delivery_counts": dict(usage), "group_cleaned": True,
                "finished_at": _now()}
            _write_json(self._exit_path(execution_key, current), exit_record)
            alive = False
        if alive and stop:
            if not stop.get("terminated"):
                self._terminate(process, signal.SIGTERM)
                stop["terminated"] = True
            requested = float(stop.get("requested_ts", 0))
            if time.time() - requested > self.grace_seconds:
                if (not isinstance(process, _RecordedProcess)
                        or _pid_alive(process.pid, process.started)):
                    self._terminate(process, signal.SIGKILL)
                stop["escalated"] = True
            # The leader can exit before its MCP child. Keep the turn active
            # until no member of the recorded group is still running.
            if self._group_active(process.pid):
                alive = True
        if alive:
            return Observation(
                observation_id=0, observed="active",
                session_ref=session_ref, checkpoint_ref=checkpoint_ref,
                delivery_key=current, provider_calls=provider_calls,
                tool_calls=tool_calls, stop_requested=bool(stop),
                error={"code": "stop_pending",
                       "message": "stop was requested; the process is still running"}
                if stop else None,
                event_ref=delivery["events_ref"])
        if (not alive and stop and identity is not None
                and self._group_active(identity[0])):
            # The exact leader is gone but group members survive. Their pgid
            # cannot be proven ours once the leader identity is dead, so never
            # signal it and never report the stop as complete.
            return Observation(
                observation_id=0, observed="active",
                session_ref=session_ref, checkpoint_ref=checkpoint_ref,
                delivery_key=current, provider_calls=provider_calls,
                tool_calls=tool_calls, stop_requested=True,
                error={"code": "stop_pending",
                       "message": "stop was requested; process-group members "
                                  "are still running", "pid": identity[0]},
                event_ref=delivery["events_ref"])
        if exit_record is None and delivery.get("state") == "running":
            if identity is None:
                return Observation(
                    observation_id=0, observed="unknown",
                    session_ref=session_ref, checkpoint_ref=checkpoint_ref,
                    delivery_key=current, provider_calls=provider_calls,
                    tool_calls=tool_calls, stop_requested=bool(stop),
                    error={"code": "spawn_identity_unknown",
                           "message": "no durable pid exists for this delivery; "
                                      "its process cannot be verified",
                           "delivery_key": current},
                    event_ref=delivery["events_ref"])
            if not stop:
                return Observation(
                    observation_id=0, observed="unknown",
                    session_ref=session_ref, checkpoint_ref=checkpoint_ref,
                    delivery_key=current, provider_calls=provider_calls,
                    tool_calls=tool_calls, stop_requested=False,
                    error={"code": "exit_unknown",
                           "message": "the process disappeared without an exit record"},
                    event_ref=delivery["events_ref"])
            exit_record = {
                "code": -int(signal.SIGTERM), "signal": signal.SIGTERM,
                "timed_out": False, "budget_exceeded": False,
                "counts": dict(usage), "delivery_counts": dict(usage),
                "imputed": "stop observed after the watching Controller exited",
                "group_cleaned": True,
                "finished_at": _now()}
            _write_json(self._exit_path(execution_key, current), exit_record)
        unmatched = self._unmatched(execution_key, state)
        if (exit_record and not stop and unmatched
                and (exit_record.get("budget_exceeded")
                     or exit_record.get("timed_out"))):
            # The process was killed while an HTTP tool request was in flight.
            # The service may have committed it, so keep the unknown outcome
            # and only attach the budget/timeout as a diagnostic.
            return Observation(
                observation_id=0, observed="unknown",
                session_ref=session_ref, checkpoint_ref=checkpoint_ref,
                delivery_key=current, provider_calls=provider_calls,
                tool_calls=tool_calls,
                error={"code": "tool_effect_unknown",
                       "message": "the delivery was stopped while a tool effect "
                                  "was in flight; its outcome is unknown",
                       "reason": ("budget_exceeded"
                                  if exit_record.get("budget_exceeded")
                                  else "wall_timeout"),
                       "timeout_seconds": exit_record.get("timeout_seconds"),
                       "budget": list(exit_record.get("budget") or []),
                       "tools": [entry.get("name") for entry in unmatched]},
                event_ref=delivery["events_ref"])
        if exit_record and exit_record.get("group_cleaned") is False:
            return Observation(
                observation_id=0, observed="unknown",
                session_ref=session_ref, checkpoint_ref=checkpoint_ref,
                delivery_key=current, provider_calls=provider_calls,
                tool_calls=tool_calls, stop_requested=bool(stop),
                error={"code": "group_cleanup_incomplete",
                       "message": "the CLI leader exited, but its process "
                                  "group could not be verified clean"},
                event_ref=delivery["events_ref"])
        if exit_record and exit_record.get("budget_exceeded") and not stop:
            return Observation(
                observation_id=0, observed="failed",
                session_ref=session_ref, checkpoint_ref=checkpoint_ref,
                delivery_key=current, provider_calls=provider_calls,
                tool_calls=tool_calls,
                error={"code": "budget_exceeded",
                       "message": "the delivery reached its tool-call budget; "
                                  "the next call was refused before execution",
                       "budget": list(exit_record.get("budget") or []),
                       "tool_calls": tool_calls},
                event_ref=delivery["events_ref"])
        if exit_record and exit_record.get("timed_out") and not stop:
            return Observation(
                observation_id=0, observed="failed",
                session_ref=session_ref, checkpoint_ref=checkpoint_ref,
                delivery_key=current, provider_calls=provider_calls,
                tool_calls=tool_calls,
                error={"code": "wall_timeout",
                       "message": "the delivery exceeded its wall-clock budget",
                       "timeout_seconds": exit_record.get("timeout_seconds"),
                       "tool_calls": tool_calls},
                event_ref=delivery["events_ref"])
        if stop:
            if unmatched:
                return Observation(
                    observation_id=0, observed="unknown",
                    session_ref=session_ref, checkpoint_ref=checkpoint_ref,
                    delivery_key=current, provider_calls=provider_calls,
                    tool_calls=tool_calls, stop_requested=True,
                    error={"code": "tool_effect_unknown",
                           "message": "the worker stopped while a tool effect "
                                      "was in flight",
                           "tools": [entry.get("name") for entry in unmatched]},
                    event_ref=delivery["events_ref"])
            late = events["native_tool_starts"] > 0 and (
                exit_record or {}).get("code") == 0
            if late and not stop.get("late_result_ignored"):
                stop["late_result_ignored"] = True
                state["version"] += 1
                _write_json(self._state_path(execution_key), state)
            return Observation(
                observation_id=0, observed="stopped",
                session_ref=session_ref, checkpoint_ref=checkpoint_ref,
                delivery_key=current, provider_calls=provider_calls,
                tool_calls=tool_calls, stop_requested=True,
                error={"code": "stopped_before_result",
                       "message": "a late completion after stop was ignored"}
                if late else {"code": "stopped_before_result"},
                event_ref=delivery["events_ref"])
        if unmatched:
            return Observation(
                observation_id=0, observed="unknown",
                session_ref=session_ref, checkpoint_ref=checkpoint_ref,
                delivery_key=current, provider_calls=provider_calls,
                tool_calls=tool_calls,
                error={"code": "tool_effect_unknown",
                       "message": "a tool started but no end receipt exists",
                       "tools": [entry.get("name") for entry in unmatched]},
                event_ref=delivery["events_ref"])
        yield_record = _read_json(self._yield_path(execution_key, current))
        if (isinstance(yield_record, dict)
                and yield_record.get("format") == YIELD_FORMAT
                and yield_record.get("delivery_key") == current
                and yield_record.get("worker_id") == state.get("worker_id")
                and yield_record.get("epoch") == state.get("epoch")):
            return Observation(
                observation_id=0, observed="idle",
                session_ref=session_ref, checkpoint_ref=checkpoint_ref,
                delivery_key=current, provider_calls=provider_calls,
                tool_calls=tool_calls,
                error={"code": "decision_pending",
                       "message": "the worker yielded for a parent decision",
                       "decision_id": yield_record.get("decision_id")},
                event_ref=delivery["events_ref"])
        code = (exit_record or {}).get("code")
        if code not in (0, None):
            return Observation(
                observation_id=0, observed="failed",
                session_ref=session_ref, checkpoint_ref=checkpoint_ref,
                delivery_key=current, provider_calls=provider_calls,
                tool_calls=tool_calls,
                error={"code": "process_failed", "exit_code": code},
                event_ref=delivery["events_ref"])
        result, error = self._result(state, delivery, events)
        declared = isinstance(result, dict) and "result" in result
        if events.get("failed") and not declared:
            return Observation(
                observation_id=0, observed="failed",
                session_ref=session_ref, checkpoint_ref=checkpoint_ref,
                delivery_key=current, provider_calls=provider_calls,
                tool_calls=tool_calls,
                error={"code": "provider_error",
                       "message": "the agent CLI reported a failed final turn",
                       "provider_error": events.get("error_message")},
                event_ref=delivery["events_ref"])
        return Observation(
            observation_id=0, observed="idle",
            session_ref=session_ref, checkpoint_ref=checkpoint_ref,
            delivery_key=current, result=result, error=error,
            provider_calls=provider_calls, tool_calls=tool_calls,
            event_ref=delivery["events_ref"])

    def _result(self, state: Mapping[str, Any], delivery: Mapping[str, Any],
                events: Mapping[str, Any]) -> tuple[
                    dict[str, Any] | None, dict[str, Any] | None]:
        message = delivery["message"]
        result_path = message.get("result_path")
        final_text = events.get("final_text", "")
        if isinstance(result_path, str) and result_path:
            path = _safe_child(Path(state["workspace"]), result_path)
            if not path.exists():
                return None, {"code": "no_valid_result",
                              "message": "the result file is missing",
                              "result_path": result_path}
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                return None, {"code": "no_valid_result",
                              "message": "the result file is not valid JSON",
                              "result_path": result_path}
            if not isinstance(value, dict):
                return None, {"code": "no_valid_result",
                              "message": "the result must be a JSON object",
                              "result_path": result_path}
            missing = [field for field in message.get("required_fields", [])
                       if field not in value]
            if missing:
                return None, {"code": "no_valid_result",
                              "message": "the result misses required fields",
                              "missing": missing}
            return {"result": value, "result_digest": digest(value),
                    "result_path": result_path, "final_text": final_text}, None
        if not final_text.strip():
            return None, {"code": "no_valid_result",
                          "message": "the round ended without assistant text"}
        return {"final_text": final_text,
                "final_digest": digest(final_text)}, None

    # -- checkpoint/restore ----------------------------------------------
    def checkpoint(self, handle: Handle) -> CheckpointRef:
        # Only the live native session can be continued. Snapshotting a CLI
        # session plus event hashes would claim a historical state that this
        # backend cannot actually reproduce.
        raise BackendError(
            "capability_unsupported",
            f"{self.name} supports same-execution session resume, not "
            "historical checkpoints")

    def restore(self, spec: WorkerSpec, checkpoint: Mapping[str, Any],
                execution_key: tuple[str, int]) -> Handle:
        raise BackendError(
            "capability_unsupported",
            f"{self.name} cannot restore a historical checkpoint")

    # -- stop ------------------------------------------------------------
    def stop(self, handle: Handle, request_id: str) -> StopReceipt:
        if not isinstance(request_id, str) or not request_id:
            raise BackendError("invalid_argument", "request_id is required")
        with self._guard(handle.execution_key):
            state = self._state(handle.execution_key)
            previous = state.get("stop")
            if previous and previous.get("stopped_at"):
                return StopReceipt(request_id=request_id, accepted=True,
                                   already_stopped=True)
            if previous is None:
                state["stop"] = {
                    "request_id": request_id, "requested_at": _now(),
                    "requested_ts": time.time(), "stopped_at": None}
                self._save(handle.execution_key, state)
            current = state.get("current")
            delivery = state["deliveries"].get(current) if current else {}
            identity = (self._identity(handle.execution_key, current,
                                       delivery or {}, wait=2.0)
                        if current else None)
            process = self._current_process(handle.execution_key, current,
                                            delivery or {}, identity=identity,
                                            resolved=True)
            terminal = False
            if process is not None:
                # Terminate the recorded process group and verify that no
                # member (the MCP bridge included) survives the leader.
                _code, cleaned = self._expire(process)
                if not cleaned:
                    return StopReceipt(request_id=request_id, accepted=True,
                                       already_stopped=False, pid=process.pid)
                terminal = True
            elif identity is not None and self._group_active(identity[0]):
                # The leader is gone. A bare pgid may be reused, so anchor the
                # group on the live MCP bridge's own recorded pid/start/pgid
                # (written by the bridge itself, private to this delivery).
                member = self._mcp_member(
                    handle.execution_key, current, state)
                if member is None:
                    raise BackendError(
                        "capability_unsupported",
                        "the CLI leader is gone and no verified MCP member "
                        "anchor exists; this restart-stop cannot be proven "
                        "safe and is refused rather than killing a reused pid",
                        pgid=identity[0])
                member_pid, _member_start, member_pgid = member
                if member_pgid != identity[0]:
                    raise BackendError(
                        "capability_unsupported",
                        "the recorded MCP member belongs to another process "
                        "group; refusing to signal an unverified pgid",
                        pgid=identity[0], member_pgid=member_pgid)
                if not self._cleanup_group(member_pgid, self.grace_seconds):
                    return StopReceipt(request_id=request_id, accepted=True,
                                       already_stopped=False,
                                       pid=member_pid)
                terminal = True
            elif identity is not None:
                # The exact leader is gone and its group has no live member.
                terminal = True
            else:
                # No durable leader identity at all. The private MCP record is
                # the only possible anchor left; otherwise the outcome stays
                # unknown rather than claiming a stop that cannot be proven.
                member = self._mcp_member(
                    handle.execution_key, current, state)
                if member is not None:
                    member_pid, _member_start, member_pgid = member
                    if not self._cleanup_group(member_pgid,
                                               self.grace_seconds):
                        return StopReceipt(
                            request_id=request_id, accepted=True,
                            already_stopped=False, pid=member_pid)
                    terminal = True
            exit_record = (_read_json(
                self._exit_path(handle.execution_key, current))
                if current else None)
            if (not terminal and current and exit_record is None
                    and (delivery or {}).get("state") == "running"):
                return StopReceipt(request_id=request_id, accepted=True,
                                   already_stopped=False)
            if terminal and current:
                exit_path = self._exit_path(handle.execution_key, current)
                if exit_record is None:
                    # No watcher survived to write an exit record (restart or a
                    # recorded process that was already gone). This stop has
                    # nevertheless verified the whole group clean, so persist
                    # that proof for later observations.
                    usage = self._usage(handle.execution_key, state)
                    exit_record = {
                        "code": -int(signal.SIGTERM),
                        "signal": int(signal.SIGTERM),
                        "timed_out": False, "budget_exceeded": False,
                        "yielded": False,
                        "group_cleaned": True,
                        "group_cleaned_by": "stop",
                        "group_cleaned_at": _now(),
                        "imputed": "stop verified the group clean without a "
                                   "live watcher",
                        "provider_calls": None,
                        "counts": dict(usage),
                        "delivery_counts": {"tool_calls": None},
                        "finished_at": _now(),
                    }
                    _write_json(exit_path, exit_record)
                elif exit_record.get("group_cleaned") is False:
                    # Stale evidence from before the cleanup finished: this
                    # stop now proves the group clean, so later observations
                    # (and a restarted Controller) must consume that proof
                    # instead of reporting unknown forever.
                    exit_record["group_cleaned"] = True
                    exit_record["group_cleaned_by"] = "stop"
                    exit_record["group_cleaned_at"] = _now()
                    _write_json(exit_path, exit_record)
            state = self._state(handle.execution_key)
            state["stop"]["stopped_at"] = _now()
            self._save(handle.execution_key, state)
            return StopReceipt(request_id=request_id, accepted=True,
                               already_stopped=True)

    # -- tool reconciliation --------------------------------------------
    def reconcile_tools(self, handle: Handle) -> dict[str, Any]:
        with self._guard(handle.execution_key):
            state = self._state(handle.execution_key)
            unmatched = self._unmatched(handle.execution_key, state)
            resolved: list[dict[str, Any]] = []
            unresolved: list[dict[str, Any]] = []
            workspace = Path(state["workspace"])
            for entry in unmatched:
                name = entry.get("name")
                effect = entry.get("effect")
                if name == "read_file" and effect == "read_only":
                    resolved.append({"call_id": entry.get("call_id"),
                                     "effect": effect, "applied": False})
                    continue
                if name == "write_file" and effect == "idempotent":
                    target = (entry.get("params") or {}).get("path")
                    expected = (entry.get("params") or {}).get("text")
                    applied = False
                    if isinstance(target, str) and isinstance(expected, str):
                        try:
                            path = _safe_child(workspace, target)
                            applied = path.exists() and \
                                path.read_text(encoding="utf-8") == expected
                        except BackendError:
                            applied = False
                    resolved.append({"call_id": entry.get("call_id"),
                                     "effect": effect, "applied": applied})
                    continue
                # System tools change service state through an HTTP request
                # whose response may have been lost. Their outcome is unknown
                # until the API state proves it, so never guess an answer.
                unresolved.append({"call_id": entry.get("call_id"),
                                   "tool": name, "effect": effect})
            reconciled = {str(call) for call in state.get("reconciled_calls", [])}
            reconciled.update(str(item["call_id"]) for item in resolved)
            state["reconciled_calls"] = sorted(reconciled)
            current = state.get("current")
            if current and not unresolved and not state.get("stop"):
                delivery = state["deliveries"][current]
                if delivery.get("state") == "running":
                    delivery["state"] = "failed"
                    delivery["error"] = {
                        "code": "tool_effects_reconciled",
                        "message": "all in-flight effects are resolved; "
                                   "resume is safe"}
            if resolved:
                self._save(handle.execution_key, state)
            return {"resolved": resolved, "unresolved": unresolved,
                    "unknown_effects": unresolved}

    # -- bounded execution trace -----------------------------------------
    def execution_log(self, *, run_id: str, worker_id: str, epoch: int,
                      delivery_key: str, after: int = 0,
                      limit: int = 100) -> ExecutionLog:
        if (not SAFE_EXECUTION_ID.match(worker_id or "")
                or not SAFE_EXECUTION_ID.match(delivery_key or "")):
            raise BackendError("invalid_argument", "unsafe execution identifier")
        if after < 0:
            raise BackendError("invalid_argument",
                               "execution offset must be >= 0")
        if limit < 1:
            raise BackendError("invalid_argument",
                               "execution limit must be >= 1")
        execution_dir = self.root / worker_id / str(epoch)
        state_path = execution_dir / "state.json"
        source = self.execution_log_kind
        if not state_path.is_file():
            return ExecutionLog(
                source=source, availability="missing", offset=after,
                next_offset=after,
                detail={"reason": "execution state is missing"})
        state = _read_json(state_path)
        if not isinstance(state, Mapping) or state.get("run_id") != run_id \
                or state.get("worker_id") != worker_id \
                or int(state.get("epoch", -1)) != epoch:
            return ExecutionLog(
                source=source, availability="missing", offset=after,
                next_offset=after,
                detail={"reason": "execution identity does not match the "
                                  "committed binding"})
        delivery = (state.get("deliveries") or {}).get(delivery_key)
        if not isinstance(delivery, Mapping):
            return ExecutionLog(
                source=source, availability="missing", offset=after,
                next_offset=after,
                detail={"reason": "delivery is not recorded in this execution"})
        path = execution_dir / "events" / f"{delivery_key}.jsonl"
        if not path.is_file():
            return ExecutionLog(
                source=source, availability="missing", offset=after,
                next_offset=after,
                detail={"reason": "delivery event log has not been written",
                        "delivery_state": delivery.get("state")})
        size = path.stat().st_size
        if after > size:
            raise ControlPlaneError(
                "cursor_expired", "execution cursor is ahead of the event log",
                instruction="resnapshot", size=size)
        entries: list[dict[str, Any]] = []
        malformed = 0
        gap = False
        consumed = after
        buffer = b""
        eof = False
        budget = EXECUTION_LOG_MAX_BYTES
        with open(path, "rb") as handle:
            handle.seek(after)
            while len(entries) < limit:
                while (b"\n" not in buffer and not eof
                       and len(buffer) < EXECUTION_LOG_MAX_BYTES
                       and budget > 0):
                    chunk = handle.read(min(EXECUTION_LOG_CHUNK_BYTES, budget,
                                            EXECUTION_LOG_MAX_BYTES - len(buffer)))
                    if not chunk:
                        eof = True
                        break
                    budget -= len(chunk)
                    buffer += chunk
                newline = buffer.find(b"\n")
                if newline >= 0:
                    raw, buffer = buffer[:newline], buffer[newline + 1:]
                    line_offset = consumed
                    consumed += newline + 1
                    if not raw.strip():
                        continue
                    try:
                        event = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, ValueError):
                        malformed += 1
                        entries.append({"kind": "unparsed",
                                        "offset": line_offset,
                                        "line_bytes": len(raw)})
                        continue
                    if not isinstance(event, Mapping):
                        malformed += 1
                        entries.append({"kind": "unparsed",
                                        "offset": line_offset,
                                        "line_bytes": len(raw)})
                        continue
                    entries.append(self.protocol.entry(
                        event, offset=line_offset, line_bytes=len(raw)))
                    continue
                if eof:
                    break
                if len(buffer) < EXECUTION_LOG_MAX_BYTES and budget <= 0:
                    break
                gap = True
                malformed += 1
                skipped = len(buffer)
                line_offset = consumed
                buffer = b""
                while skipped < EXECUTION_LOG_MAX_BYTES and budget > 0:
                    chunk = handle.read(min(EXECUTION_LOG_CHUNK_BYTES, budget,
                                            EXECUTION_LOG_MAX_BYTES - skipped))
                    if not chunk:
                        eof = True
                        break
                    budget -= len(chunk)
                    index = chunk.find(b"\n")
                    if index >= 0:
                        skipped += index + 1
                        buffer = chunk[index + 1:]
                        break
                    skipped += len(chunk)
                consumed += skipped
                entries.append({
                    "kind": "gap", "offset": line_offset,
                    "line_bytes": skipped,
                    "reason": "record exceeds bounded execution read",
                    "truncated": consumed < size})
                if consumed >= size:
                    break
            at_eof = handle.tell() >= size or eof
        partial = bool(buffer) and b"\n" not in buffer and at_eof
        has_more = size > consumed and not partial
        return ExecutionLog(
            source=source, availability="available",
            entries=tuple(entries), offset=after, next_offset=consumed,
            has_more=has_more and not partial, partial=partial,
            detail={
                "delivery_state": delivery.get("state"),
                "delivery_started_at": delivery.get("started_at"),
                "delivery_finished_at": delivery.get("finished_at"),
                "size": size, "malformed": malformed, "gap": gap,
                "usage_semantics": "tool_calls counts granted MCP tool "
                                   "executions; provider_calls is unknown",
            })
