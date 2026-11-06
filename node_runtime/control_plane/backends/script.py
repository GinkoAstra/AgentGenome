"""Deterministic scripted Backend.

It implements the same execution boundary as the Pi Backend without any model
call: a delivery is executed inside the Backend (tool effects included) and the
Controller only sees durable observations. Used for protocol and failure tests.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from ..resources import ControlPlaneError, digest
from .locks import ExecutionLock
from .base import (
    AgentBackend,
    BackendError,
    Capabilities,
    CheckpointRef,
    DeliveryReceipt,
    ExecutionLog,
    Handle,
    Observation,
    StopReceipt,
    WorkerSpec,
)

FORMAT = "agentgenome-script-backend/1"
EXECUTION_LOG_KIND = "script.delivery.state"
SAFE_EXECUTION_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

# The execution identity and configuration committed by the Scheduler. A
# delivery message may not redefine any of these, and a restart must find the
# same values.
_SPEC_IDENTITY = (
    "worker_id", "node_id", "epoch", "backend_ref", "agent_ref", "run_id",
    "context", "tool_profile", "budget", "required_capabilities",
)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


class ScriptBackend(AgentBackend):
    name = "script"

    def __init__(self, root: str | Path,
                 agent: Callable[[WorkerSpec, Mapping[str, Any],
                                  Mapping[str, Any]], None] | None = None,
                 *, identity: Mapping[str, str] | None = None) -> None:
        self.root = Path(root) / "backends" / "script"
        self.root.mkdir(parents=True, exist_ok=True)
        self.agent = agent
        self.identity = dict(identity) if identity is not None else None
        self._lock = threading.RLock()

    # -- paths and state -------------------------------------------------
    def _dir(self, execution_key: tuple[str, int]) -> Path:
        worker_id, epoch = execution_key
        path = self.root / worker_id / str(epoch)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _state_path(self, execution_key: tuple[str, int]) -> Path:
        return self._dir(execution_key) / "state.json"

    def _state(self, execution_key: tuple[str, int]) -> dict[str, Any]:
        path = self._state_path(execution_key)
        if not path.exists():
            raise BackendError("not_found", "script backend state does not exist")
        return _read_json(path)

    def _save(self, execution_key: tuple[str, int], state: dict[str, Any]) -> None:
        state["version"] = int(state.get("version", 0)) + 1
        state["updated_at"] = _now()
        _write_json(self._state_path(execution_key), state)

    @contextmanager
    def _guard(self, execution_key: tuple[str, int]) -> Iterator[None]:
        """Serialise effects for one execution across processes.

        The lock file lives in the execution directory, so different Workers
        still run concurrently while a resumed Controller cannot decide from a
        stale read and repeat a committed effect.
        """
        with ExecutionLock(self._dir(execution_key) / ".lock"):
            with self._lock:
                yield

    # -- lifecycle -------------------------------------------------------
    def _require_identity(self, state: Mapping[str, Any]) -> None:
        """Refuse to run changed program code against an existing binding.

        A legacy no-callback state that never stored an identity keeps its
        original protocol behavior. A state persisted by a configured program
        (or a mixed pairing in either direction) cannot be resumed under a
        different configured program.
        """
        stored = state.get("agent_identity")
        configured = dict(self.identity) if self.identity is not None else None
        if _canonical(stored) != _canonical(configured):
            raise BackendError(
                "capability_unsupported",
                "the execution was bound to a different program-checker identity",
                fields=["agent_identity"],
                stored_identity=stored, configured_identity=configured)

    def capabilities(self) -> Capabilities:
        return Capabilities(
            resumable_session=True,
            live_observation="full",
            idempotent_delivery=True,
            stop="cooperative",
            checkpoint=True,
            backend_tools=(),
            internal_activities="none",
        )

    def start(self, spec: WorkerSpec, execution_key: tuple[str, int]) -> Handle:
        with self._guard(execution_key):
            snapshot = spec.as_dict()
            path = self._state_path(execution_key)
            if path.exists():
                state = _read_json(path)
                stored = state.get("spec")
                if not isinstance(stored, Mapping):
                    raise BackendError(
                        "capability_unsupported",
                        "the execution has no committed spec and cannot be resumed",
                        fields=["spec"])
                changed = [name for name in _SPEC_IDENTITY
                           if _canonical(stored.get(name)) != _canonical(snapshot.get(name))]
                if changed:
                    raise BackendError(
                        "capability_unsupported",
                        "the committed execution spec changed for this execution",
                        fields=changed)
                self._require_identity(state)
            else:
                state = {
                    "format": FORMAT,
                    "worker_id": spec.worker_id,
                    "node_id": spec.node_id,
                    "epoch": spec.epoch,
                    "intent_seq": spec.intent_seq,
                    "agent_ref": spec.agent_ref,
                    "context": dict(spec.context),
                    "tool_profile": dict(spec.tool_profile),
                    "spec": snapshot,
                    "agent_identity": (dict(self.identity)
                                       if self.identity is not None else None),
                    "workspace": str(self._dir(execution_key) / "workspace"),
                    "deliveries": {},
                    "order": [],
                    "stop": None,
                    "checkpoint": None,
                    "version": 0,
                    "created_at": _now(),
                }
                (self._dir(execution_key) / "workspace").mkdir(parents=True, exist_ok=True)
                self._save(execution_key, state)
            return Handle(worker_id=spec.worker_id, epoch=spec.epoch,
                          workspace=state["workspace"],
                          session_ref=f"script:{spec.worker_id}:{spec.epoch}",
                          state=self._handle_state(state))

    @staticmethod
    def _handle_state(state: Mapping[str, Any]) -> str:
        stop = state.get("stop")
        if stop and stop.get("stopped_at"):
            return "stopped"
        for key in reversed(state.get("order", [])):
            delivery = state["deliveries"][key]
            if delivery["state"] in ("active", "stopping"):
                return "active"
            if delivery["state"] == "failed":
                return "failed"
            if delivery["state"] == "unknown":
                return "unknown"
        return "idle"

    def deliver(self, handle: Handle, message: Mapping[str, Any],
                delivery_key: str) -> DeliveryReceipt:
        if not isinstance(delivery_key, str) or not delivery_key:
            raise BackendError("invalid_argument", "delivery_key is required")
        if not isinstance(message, Mapping):
            raise BackendError("invalid_argument", "message must be an object")
        with self._guard(handle.execution_key):
            state = self._state(handle.execution_key)
            existing = state["deliveries"].get(delivery_key)
            if existing is not None:
                return DeliveryReceipt(
                    delivery_key=delivery_key, accepted=True, outcome="duplicate",
                    delivery_id=existing["delivery_id"], duplicate=True,
                )
            if state.get("stop") and state["stop"].get("requested_at"):
                raise BackendError("stopped", "stop was requested; no new delivery is accepted")
            unknown = [key for key in state["order"]
                       if state["deliveries"][key]["state"] == "unknown"]
            if unknown:
                raise BackendError(
                    "unknown_effect",
                    "a previous tool effect has an unknown outcome; reconcile before resuming",
                    delivery_key=unknown[0])
            active = [key for key in state["order"]
                      if state["deliveries"][key]["state"] == "active"]
            if active:
                raise BackendError("busy", "another delivery is still running",
                                   delivery_key=active[0])
            delivery_id = len(state["order"]) + 1
            delivery = {
                "delivery_id": delivery_id,
                "delivery_key": delivery_key,
                "message": dict(message),
                "state": "active",
                "tools": [],
                "result": None,
                "error": None,
                "started_at": _now(),
                "finished_at": None,
            }
            state["deliveries"][delivery_key] = delivery
            state["order"].append(delivery_key)
            self._save(handle.execution_key, state)
            self._execute(handle.execution_key, delivery_key)
            return DeliveryReceipt(delivery_key=delivery_key, accepted=True,
                                   outcome="delivered", delivery_id=delivery_id)

    def _execute(self, execution_key: tuple[str, int], delivery_key: str) -> None:
        state = self._state(execution_key)
        delivery = state["deliveries"][delivery_key]
        script = delivery["message"].get("script", {})
        if not isinstance(script, Mapping):
            raise BackendError("invalid_argument", "message.script must be an object")
        # A configured callback is the deployment's program Checker, so it
        # executes every ordinary delivery unless the message explicitly
        # selects another scripted mode. Without a callback the original
        # protocol-simulation default stays in force.
        mode = script.get("mode", "agent" if self.agent is not None else "finish")
        if mode == "hang":
            return  # stays active until stop
        if mode == "agent":
            committed = state.get("spec")
            if not isinstance(committed, Mapping):
                delivery["state"] = "failed"
                delivery["error"] = {
                    "code": "invalid_argument",
                    "message": "the committed WorkerSpec was not persisted at start",
                }
                delivery["finished_at"] = _now()
                self._save(execution_key, state)
                return
            if self.agent is None:
                delivery["state"] = "failed"
                delivery["error"] = {"code": "capability_unsupported",
                                     "message": "no Agent tool executor is configured"}
                delivery["finished_at"] = _now()
                self._save(execution_key, state)
                return
            # Identity/config come from the committed spec, never from the
            # mutable delivery message.
            spec = WorkerSpec(
                worker_id=committed["worker_id"], node_id=committed["node_id"],
                epoch=int(committed["epoch"]),
                intent_seq=int(committed.get("intent_seq") or 1),
                backend_ref=committed["backend_ref"],
                agent_ref=committed["agent_ref"],
                desired=committed.get("desired") or "active",
                run_id=committed["run_id"],
                context=committed.get("context") or {},
                tool_profile=committed.get("tool_profile") or {},
                budget=committed.get("budget") or {},
                checkpoint_ref=committed.get("checkpoint_ref"),
                usage=committed.get("usage") or {},
                required_capabilities=committed.get("required_capabilities") or {},
            )
            try:
                self.agent(spec, delivery["message"], state)
            except Exception as error:  # Agent/tool failure is an honest result
                delivery["state"] = "idle"
                delivery["error"] = {"code": getattr(error, "code", "agent_failed"),
                                     "message": str(error)}
                delivery["finished_at"] = _now()
                self._save(execution_key, state)
                return
            delivery["state"] = "idle"
            delivery["result"] = {"result": {"status": "submitted"},
                                  "result_digest": digest({"status": "submitted"})}
            delivery["finished_at"] = _now()
            self._save(execution_key, state)
            return
        if mode == "fail":
            delivery["state"] = "failed"
            delivery["error"] = {"code": "scripted_failure", "message": "scripted process failure"}
            delivery["finished_at"] = _now()
            self._save(execution_key, state)
            return
        workspace = Path(state["workspace"])
        for tool in script.get("tool_calls", []):
            if not isinstance(tool, Mapping) or "name" not in tool:
                raise BackendError("invalid_argument", "tool call needs a name")
            effect = tool.get("effect", "unknown")
            receipt = {
                "name": tool["name"], "effect": effect,
                "params": dict(tool.get("params", {})),
                "state": "start", "started_at": _now(),
            }
            expected_content: str | None = None
            if "target" in tool:
                receipt["target"] = tool["target"]
                expected_content = str(tool.get("content", ""))
                receipt["expected_content"] = expected_content
                (workspace / tool["target"]).parent.mkdir(parents=True, exist_ok=True)
            delivery["tools"].append(receipt)
            state["version"] += 1
            _write_json(self._state_path(execution_key), state)
            if script.get("crash_after_tool_start"):
                delivery["state"] = "unknown"
                delivery["error"] = {
                    "code": "tool_effect_unknown",
                    "message": "process stopped after a tool started; outcome is unknown",
                    "tool": tool["name"], "effect": effect,
                }
                self._save(execution_key, state)
                return
            if "target" in tool:
                content = expected_content or ""
                (workspace / tool["target"]).write_text(content, encoding="utf-8")
                receipt["result"] = {"path": tool["target"],
                                     "sha256": hashlib.sha256(content.encode()).hexdigest(),
                                     "bytes": len(content.encode())}
            else:
                receipt["result"] = {"ok": True}
            receipt["state"] = "end"
            receipt["finished_at"] = _now()
            state["version"] += 1
            _write_json(self._state_path(execution_key), state)
        result_path = script.get("result_path")
        required = script.get("required_fields", [])
        if result_path:
            path = workspace / result_path
            if not path.exists():
                delivery["state"] = "idle"
                delivery["error"] = {"code": "no_valid_result",
                                     "message": "the required result file is missing",
                                     "result_path": result_path}
            else:
                try:
                    result = json.loads(path.read_text(encoding="utf-8"))
                except ValueError:
                    result = None
                if not isinstance(result, dict):
                    delivery["state"] = "idle"
                    delivery["error"] = {"code": "no_valid_result",
                                         "message": "the result file is not a JSON object",
                                         "result_path": result_path}
                elif any(field not in result for field in required):
                    delivery["state"] = "idle"
                    delivery["error"] = {"code": "no_valid_result",
                                         "message": "the result file misses required fields",
                                         "required_fields": list(required)}
                else:
                    delivery["state"] = "idle"
                    delivery["result"] = {"result": result,
                                          "result_digest": digest(result),
                                          "result_path": result_path}
        elif script.get("result") is not None:
            delivery["state"] = "idle"
            delivery["result"] = {"result": dict(script["result"]),
                                  "result_digest": digest(script["result"])}
        else:
            delivery["state"] = "idle"
            delivery["error"] = {"code": "no_valid_result",
                                 "message": "the round ended without a result"}
        delivery["finished_at"] = _now()
        self._save(execution_key, state)

    execution_log_kind = EXECUTION_LOG_KIND

    def execution_log(self, *, run_id: str, worker_id: str, epoch: int,
                      delivery_key: str, after: int = 0,
                      limit: int = 100) -> ExecutionLog:
        """Index-offset view of one delivery's recorded state and tool effects."""
        if (not SAFE_EXECUTION_ID.match(worker_id or "")
                or not SAFE_EXECUTION_ID.match(delivery_key or "")):
            raise BackendError("invalid_argument", "unsafe execution identifier")
        if after < 0:
            raise BackendError("invalid_argument", "execution offset must be >= 0")
        if limit < 1:
            raise BackendError("invalid_argument", "execution limit must be >= 1")
        path = self.root / worker_id / str(epoch) / "state.json"
        if not path.is_file():
            return ExecutionLog(source=EXECUTION_LOG_KIND, availability="missing",
                                offset=after, next_offset=after,
                                detail={"reason": "execution state is missing"})
        state = _read_json(path)
        spec = state.get("spec") if isinstance(state.get("spec"), Mapping) else {}
        if (state.get("worker_id") != worker_id
                or int(state.get("epoch", -1)) != epoch
                or spec.get("run_id") != run_id):
            return ExecutionLog(
                source=EXECUTION_LOG_KIND, availability="missing",
                offset=after, next_offset=after,
                detail={"reason": "execution identity does not match the "
                                  "committed binding"})
        delivery = (state.get("deliveries") or {}).get(delivery_key)
        if not isinstance(delivery, Mapping):
            return ExecutionLog(
                source=EXECUTION_LOG_KIND, availability="missing",
                offset=after, next_offset=after,
                detail={"reason": "delivery is not recorded in this execution"})
        entries: list[dict[str, Any]] = []
        started = delivery.get("started_at")
        finished = delivery.get("finished_at")
        duration = None
        if isinstance(started, str) and isinstance(finished, str):
            try:
                duration = int((datetime.fromisoformat(finished.replace("Z", "+00:00"))
                                - datetime.fromisoformat(
                                    started.replace("Z", "+00:00"))).total_seconds() * 1000)
            except ValueError:
                duration = None
        delivery_entry: dict[str, Any] = {
            "kind": "delivery", "index": 0, "state": delivery.get("state"),
            "started_at": started, "finished_at": finished,
        }
        if duration is not None:
            delivery_entry["duration_ms"] = duration
        entries.append(delivery_entry)
        for tool in delivery.get("tools") or []:
            if not isinstance(tool, Mapping):
                continue
            entry: dict[str, Any] = {
                "kind": "tool", "index": len(entries),
                "call_id": tool.get("call_id"), "tool": tool.get("name"),
                "effect": tool.get("effect"), "state": tool.get("state"),
                "started_at": tool.get("started_at"),
                "finished_at": tool.get("finished_at"),
            }
            if isinstance(tool.get("result"), Mapping):
                entry["result_summary"] = {
                    "keys": sorted(str(key) for key in tool["result"])[:32]}
            if isinstance(tool.get("error"), Mapping):
                entry["error"] = {
                    "code": tool["error"].get("code"),
                    "message": str(tool["error"].get("message", ""))[:500]}
            entries.append(entry)
        if isinstance(delivery.get("error"), Mapping):
            entries.append({
                "kind": "error", "index": len(entries),
                "error": {"code": delivery["error"].get("code"),
                          "message": str(delivery["error"].get("message", ""))[:500]},
            })
        result = delivery.get("result")
        if isinstance(result, Mapping):
            entries.append({
                "kind": "result", "index": len(entries),
                "result_digest": result.get("result_digest"),
                "result_path": result.get("result_path"),
            })
        page = entries[after:after + limit]
        next_offset = after + len(page)
        return ExecutionLog(
            source=EXECUTION_LOG_KIND, availability="available",
            entries=tuple(page), offset=after, next_offset=next_offset,
            has_more=next_offset < len(entries), partial=False,
            snapshot=int(state.get("version", 0)),
            detail={"delivery_state": delivery.get("state"),
                    "delivery_started_at": started,
                    "delivery_finished_at": finished,
                    "entries": len(entries)})

    def observe(self, handle: Handle, after: int | None = None) -> Observation:
        with self._lock:
            state = self._state(handle.execution_key)
            delivery = state["deliveries"][state["order"][-1]] if state["order"] else None
            observed = self._handle_state(state)
            stop = state.get("stop") or {}
            error = delivery.get("error") if delivery else None
            result = delivery.get("result") if delivery else None
            if delivery and delivery["state"] == "unknown":
                observed = "unknown"
            if delivery and delivery["state"] == "stopping" and error is None:
                error = {"code": "stop_pending",
                         "message": "stop was requested; the worker has not stopped yet"}
            tools = [tool for key in state["order"]
                     for tool in (state["deliveries"][key].get("tools") or [])]
            return Observation(
                observation_id=int(state["version"]),
                observed=observed,
                session_ref=f"script:{handle.worker_id}:{handle.epoch}",
                checkpoint_ref=(state.get("checkpoint") or {}).get("digest"),
                delivery_key=delivery["delivery_key"] if delivery else None,
                result=result,
                error=error,
                provider_calls=0,
                tool_calls=len(tools),
                stop_requested=bool(stop.get("requested_at")),
                event_ref=str(self._state_path(handle.execution_key)),
                activities=(),
                activities_complete=True,
            )

    def checkpoint(self, handle: Handle) -> CheckpointRef:
        with self._guard(handle.execution_key):
            state = self._state(handle.execution_key)
            payload = {key: state[key] for key in
                       ("format", "worker_id", "node_id", "epoch", "deliveries", "order", "stop")}
            value = digest(payload)
            path = self._dir(handle.execution_key) / "checkpoints" / value.split(":", 1)[1]
            _write_json(path / "state.json", state)
            state["checkpoint"] = {"kind": "script-state", "digest": value,
                                   "session_id": f"script:{handle.worker_id}:{handle.epoch}",
                                   "path": str(path)}
            self._save(handle.execution_key, state)
            return CheckpointRef(kind="script-state", digest=value,
                                 session_id=f"script:{handle.worker_id}:{handle.epoch}",
                                 path=str(path))

    def restore(self, spec: WorkerSpec, checkpoint: Mapping[str, Any],
                execution_key: tuple[str, int]) -> Handle:
        with self._guard(execution_key):
            if checkpoint.get("kind") != "script-state":
                raise BackendError("capability_unsupported",
                                   "script backend cannot restore this checkpoint")
            source = Path(str(checkpoint.get("path", ""))) / "state.json"
            if not source.exists():
                raise BackendError("not_found", "checkpoint state is missing")
            restored = _read_json(source)
            snapshot = spec.as_dict()
            stored = restored.get("spec")
            if not isinstance(stored, Mapping):
                raise BackendError(
                    "capability_unsupported",
                    "the checkpoint has no committed spec and cannot be restored",
                    fields=["spec"])
            changed = [name for name in _SPEC_IDENTITY
                       if _canonical(stored.get(name)) != _canonical(snapshot.get(name))]
            if changed:
                raise BackendError(
                    "capability_unsupported",
                    "the checkpoint belongs to a different committed spec",
                    fields=changed)
            self._require_identity(restored)
            self._save(execution_key, restored)
            state = self._state(execution_key)
            return Handle(worker_id=spec.worker_id, epoch=spec.epoch,
                          workspace=state["workspace"],
                          session_ref=f"script:{spec.worker_id}:{spec.epoch}",
                          state=self._handle_state(state))

    def stop(self, handle: Handle, request_id: str) -> StopReceipt:
        with self._guard(handle.execution_key):
            state = self._state(handle.execution_key)
            stop = state.get("stop")
            if stop and stop.get("stopped_at"):
                return StopReceipt(request_id=request_id, accepted=True,
                                   already_stopped=True)
            active = [key for key in state["order"]
                      if state["deliveries"][key]["state"] == "active"]
            if stop is None:
                state["stop"] = {"request_id": request_id, "requested_at": _now(),
                                 "stopped_at": None}
            for key in active:
                state["deliveries"][key]["state"] = "stopping"
            if not active:
                state["stop"]["stopped_at"] = _now()
            self._save(handle.execution_key, state)
            return StopReceipt(request_id=request_id, accepted=True,
                               already_stopped=not active)

    def complete_stop(self, handle: Handle) -> None:
        """Simulate the worker actually exiting after a stop request."""
        with self._guard(handle.execution_key):
            state = self._state(handle.execution_key)
            stop = state.get("stop") or {}
            if not stop.get("requested_at"):
                raise BackendError("invalid_argument", "no stop was requested")
            for key in state["order"]:
                delivery = state["deliveries"][key]
                if delivery["state"] == "stopping":
                    delivery["state"] = "stopped"
                    delivery["finished_at"] = _now()
                    delivery["error"] = {"code": "stopped_before_result",
                                         "message": "stop completed before a result"}
            stop["stopped_at"] = _now()
            state["stop"] = stop
            self._save(handle.execution_key, state)

    # -- test helpers ----------------------------------------------------
    def simulate_late_result(self, handle: Handle, delivery_key: str,
                             result: Mapping[str, Any]) -> None:
        """A late completion after stop must not revive the worker or a node."""
        with self._guard(handle.execution_key):
            state = self._state(handle.execution_key)
            delivery = state["deliveries"][delivery_key]
            if delivery["state"] in ("stopping", "stopped"):
                delivery["late_result_ignored"] = dict(result)
                state["version"] += 1
                _write_json(self._state_path(handle.execution_key), state)
                return
            delivery["state"] = "idle"
            delivery["result"] = {"result": dict(result), "result_digest": digest(result)}
            self._save(handle.execution_key, state)

    def reconcile_tools(self, handle: Handle) -> dict[str, Any]:
        with self._guard(handle.execution_key):
            state = self._state(handle.execution_key)
            workspace = Path(state["workspace"])
            resolved: list[dict[str, Any]] = []
            unresolved: list[dict[str, Any]] = []
            for key in state["order"]:
                delivery = state["deliveries"][key]
                if delivery["state"] != "unknown":
                    continue
                blocked = False
                for receipt in delivery["tools"]:
                    if receipt["state"] != "start":
                        continue
                    effect = receipt["effect"]
                    if effect == "unknown":
                        blocked = True
                        unresolved.append({"delivery_key": key, "tool": receipt["name"],
                                           "effect": effect})
                        continue
                    target = receipt.get("target")
                    applied = False
                    if effect == "idempotent" and target:
                        path = workspace / target
                        if "expected_content" in receipt:
                            expected = receipt["expected_content"]
                        else:
                            expected = receipt["params"].get(
                                "content", receipt["params"].get("text"))
                        applied = path.exists() and expected is not None and \
                            path.read_text(encoding="utf-8") == str(expected)
                    receipt["state"] = "reconciled_applied" if applied else "reconciled_not_applied"
                    resolved.append({"delivery_key": key, "tool": receipt["name"],
                                     "effect": effect, "applied": applied})
                if not blocked:
                    delivery["state"] = "failed"
                    delivery["error"] = {"code": "tool_effects_reconciled",
                                         "message": "all in-flight effects are resolved; resume is safe"}
                    delivery["finished_at"] = _now()
                    state["version"] += 1
                    _write_json(self._state_path(handle.execution_key), state)
            return {"resolved": resolved, "unresolved": unresolved,
                    "unknown_effects": unresolved}
