"""Real Pi Backend: one session, many deliveries, many model/tool rounds.

Each delivery spawns ``pi --print --mode json`` with a fixed session id in an
isolated workspace. Pi runs its own Agent loop (model -> tools -> model) and
exits; the Backend records events, tool receipts, exit status and the session
file so the Controller can observe without driving individual turns.
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
from .spawn import process_start
from .preparation import WorkerPreparation, SYSTEM_TOOLS, REVIEW_KINDS, _sha256_bytes

FORMAT = "agentgenome-pi-backend/1"
SESSION_FORMAT = "agentgenome-pi-session/1"
TOOL_CONTEXT_FORMAT = "agentgenome-worker-tool-context/2"
YIELD_FORMAT = "agentgenome-pi-yield/1"
EXECUTION_LOG_KIND = "pi.delivery.events.jsonl"
EXECUTION_LOG_MAX_BYTES = 512 * 1024
EXECUTION_LOG_CHUNK_BYTES = 64 * 1024
SAFE_EXECUTION_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
ARTIFACT_REF_VALUE = re.compile(r"^sha256:[0-9a-f]{64}$")
SPAWN_LAUNCHER = Path(__file__).with_name("spawn.py")
SPAWN_RECORD_FORMAT = "agentgenome-spawn/1"
DEFAULT_TOOLS = ("write_file", "read_file")
# Context keys already rendered by _prompt or owned by the control plane. Every
# other delegation key is business data and must reach the Agent process.


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


def _read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
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
    current = process_start(pid)
    if current is None:
        return False
    return started is None or current == started


class _RecordedProcess:
    """A pi process recorded by an earlier Controller generation.

    Pi runs with ``start_new_session`` so its process group survives the
    Controller; a resumed Controller only has the durable pid. ``poll`` is
    honest about exit: the adopted process never reports a code, so a missing
    exit record stays unknown instead of being guessed.
    """

    returncode: int | None = None

    def __init__(self, pid: int, started: int | None) -> None:
        self.pid = pid
        self.started = started

    def poll(self) -> int | None:
        return None if _pid_alive(self.pid, self.started) else 0


def _digest_files(root: Path) -> str:
    entries = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            entries.append(f"{path.relative_to(root).as_posix()}  "
                           f"{digest(path.read_bytes())}\n")
    return "sha256:" + hashlib.sha256("".join(entries).encode()).hexdigest()


def _parse_events(path: Path) -> dict[str, Any]:
    provider_calls = 0
    tool_starts = 0
    tool_ends = 0
    final_text = ""
    stop_reason: str | None = None
    error_message: str | None = None
    if not path.exists():
        return {"provider_calls": 0, "tool_starts": 0, "tool_ends": 0,
                "final_text": "", "stop_reason": None, "error_message": None}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        kind = event.get("type")
        if kind == "message_end":
            message = event.get("message") or {}
            if message.get("role") == "assistant" and message.get("stopReason") != "pending":
                provider_calls += 1
                final_text = "".join(
                    part.get("text", "") for part in message.get("content", [])
                    if isinstance(part, dict) and part.get("type") == "text"
                ) or final_text
                # Only the last assistant message decides whether the round
                # ended in a provider failure; an earlier error followed by a
                # successful turn is a transient retry, not a terminal one.
                stop_reason = message.get("stopReason")
                error_message = message.get("errorMessage")
        elif kind == "tool_execution_start":
            tool_starts += 1
        elif kind == "tool_execution_end":
            tool_ends += 1
    return {"provider_calls": provider_calls, "tool_starts": tool_starts,
            "tool_ends": tool_ends, "final_text": final_text,
            "stop_reason": stop_reason, "error_message": error_message}


def _usage_counts(value: Mapping[str, Any] | None) -> dict[str, int]:
    value = value if isinstance(value, Mapping) else {}
    return {
        "provider_calls": max(0, int(value.get("provider_calls", 0) or 0)),
        "tool_calls": max(0, int(value.get("tool_calls", 0) or 0)),
    }


def _numeric_map(value: Any) -> tuple[dict[str, int | float], bool]:
    """Report only usage numbers the Backend actually recorded."""
    if not isinstance(value, Mapping):
        return {}, False
    numbers = {str(key): item for key, item in value.items()
               if isinstance(key, str)
               and isinstance(item, (int, float)) and not isinstance(item, bool)}
    return numbers, bool(numbers)


def _safe_timestamp(event: Mapping[str, Any], message: Mapping[str, Any]) -> int | float | None:
    for source in (event, message):
        value = source.get("timestamp")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
    return None


def _content_summary(message: Mapping[str, Any]) -> dict[str, Any]:
    """Content shape only: no prompt text, thinking text or output text."""
    parts = message.get("content")
    text_parts = text_bytes = thinking_parts = 0
    tool_calls: list[dict[str, Any]] = []
    if isinstance(parts, list):
        for part in parts:
            if not isinstance(part, Mapping):
                continue
            kind = part.get("type")
            if kind == "text" and isinstance(part.get("text"), str):
                text_parts += 1
                text_bytes += len(part["text"].encode("utf-8"))
            elif kind == "thinking":
                thinking_parts += 1
            elif kind == "toolCall":
                tool_calls.append({"call_id": part.get("id"),
                                   "tool": part.get("name")})
    return {"text_parts": text_parts, "text_bytes": text_bytes,
            "thinking_parts": thinking_parts, "tool_calls": tool_calls[:32]}


def _artifact_refs(value: Any, found: list[dict[str, Any]] | None = None,
                   depth: int = 0) -> list[dict[str, Any]]:
    """Collect artifact references from a tool result without its text body."""
    found = found if found is not None else []
    if len(found) >= 20 or depth > 4:
        return found
    if isinstance(value, Mapping):
        identifier = value.get("id")
        if isinstance(identifier, str) and ARTIFACT_REF_VALUE.match(identifier):
            ref = {"id": identifier}
            for key in ("sha256", "size", "media_type"):
                if isinstance(value.get(key), (str, int)) and not isinstance(
                        value.get(key), bool):
                    ref[key] = value[key]
            if ref not in found:
                found.append(ref)
        for key in ("details", "result", "artifacts", "artifact"):
            if key in value:
                _artifact_refs(value[key], found, depth + 1)
    elif isinstance(value, list):
        for item in value[:20]:
            _artifact_refs(item, found, depth + 1)
    elif isinstance(value, str) and len(value) <= 8192 and value.lstrip().startswith(
            ("{", "[")):
        try:
            _artifact_refs(json.loads(value), found, depth + 1)
        except ValueError:
            return found
    return found


def _error_text(result: Any) -> str | None:
    if isinstance(result, Mapping):
        candidate = result.get("error")
        if isinstance(candidate, str) and candidate:
            return candidate[:500]
        for part in result.get("content") or []:
            if isinstance(part, Mapping) and isinstance(part.get("text"), str):
                return part["text"][:500]
    if isinstance(result, str) and result:
        return result[:500]
    return None


def _observation_entry(event: Mapping[str, Any], *, offset: int,
                       line_bytes: int) -> dict[str, Any]:
    """Normalize one Pi JSONL event into a bounded execution observation."""
    kind = event.get("type")
    message = event.get("message") if isinstance(event.get("message"), Mapping) else {}
    record: dict[str, Any] = {"kind": kind, "offset": offset,
                              "line_bytes": line_bytes}
    observed_at = _safe_timestamp(event, message)
    if observed_at is not None:
        record["observed_at"] = observed_at
    if kind == "message_end":
        role = message.get("role")
        record["role"] = role
        record["content"] = _content_summary(message)
        usage, available = _numeric_map(message.get("usage"))
        record["usage"] = usage if available else None
        record["usage_available"] = available
        if "stopReason" in message:
            record["stop_reason"] = message.get("stopReason")
        if message.get("errorMessage"):
            record["error_message"] = str(message["errorMessage"])[:500]
        duration = event.get("durationMs", message.get("durationMs"))
        if isinstance(duration, (int, float)) and not isinstance(duration, bool):
            record["duration_ms"] = duration
        record["provider_call"] = (role == "assistant"
                                   and message.get("stopReason") != "pending")
        return record
    if kind == "tool_execution_start":
        params = event.get("args")
        record["call_id"] = event.get("toolCallId") or event.get("callId")
        record["tool"] = event.get("toolName")
        record["params_keys"] = (sorted(str(key) for key in params)
                                 if isinstance(params, Mapping) else [])
        if isinstance(params, Mapping) and isinstance(params.get("path"), str):
            record["target_path"] = params["path"][:500]
        return record
    if kind == "tool_execution_end":
        result = event.get("result")
        is_error = bool(event.get("isError"))
        record["call_id"] = event.get("toolCallId") or event.get("callId")
        record["tool"] = event.get("toolName")
        record["is_error"] = is_error
        duration = event.get("durationMs")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool):
            record["duration_ms"] = duration
        if is_error:
            record["error"] = {"message": _error_text(result)}
        else:
            record["artifact_refs"] = _artifact_refs(result)
        return record
    if isinstance(event.get("error"), (str, Mapping)):
        record["error"] = ({"message": str(event["error"])[:500]}
                           if isinstance(event["error"], str)
                           else {str(key): str(item)[:200]
                                 for key, item in event["error"].items()
                                 if isinstance(item, (str, int, float))})
    return record


def _execution_missing(reason: str, *, offset: int,
                       detail: Mapping[str, Any] | None = None) -> ExecutionLog:
    return ExecutionLog(source=EXECUTION_LOG_KIND, availability="missing",
                        offset=offset, next_offset=offset,
                        detail={"reason": reason, **dict(detail or {})})


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
        if entry.get("phase") == "start":
            starts.append(entry)
        elif entry.get("phase") == "end":
            ends.add(str(entry.get("call_id")))
    return starts, [entry for entry in starts if str(entry.get("call_id")) not in ends]


class PiBackend(WorkerPreparation, AgentBackend):
    name = "pi"

    def __init__(self, root: str | Path, *, provider: str = "llama-local",
                 model: str = "qwen3.5-2b", executable: str = "pi",
                 max_wall_seconds: float = 240.0, grace_seconds: float = 5.0,
                 tools_module: str | None = None,
                 tools: tuple[str, ...] = DEFAULT_TOOLS,
                 artifact_reader: Callable[[Mapping[str, Any]], bytes] | None = None,
                 max_context_bytes: int | None = None,
                 data_dir: str | Path | None = None,
                 credential_issuer: Callable[[Mapping[str, Any]],
                                             Mapping[str, Any]] | None = None,
                 ) -> None:
        self.root = Path(root) / "backends" / "pi"
        self.root.mkdir(parents=True, exist_ok=True)
        self.provider = provider
        self.model = model
        self.executable = executable
        self.max_wall_seconds = max_wall_seconds
        self.grace_seconds = grace_seconds
        self.tools_module = str(tools_module or Path(__file__).with_name("pi_tools.mjs"))
        self.tools = tuple(tools)
        self.artifact_reader = artifact_reader
        self.max_context_bytes = max_context_bytes
        self.data_dir = str(Path(data_dir).resolve()) if data_dir is not None else None
        self.credential_issuer = credential_issuer
        self._lock = threading.RLock()
        # Cached local handles, keyed by execution, paired with the kernel
        # start time recorded in the durable delivery they belong to.
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
            raise BackendError("not_found", "pi backend state does not exist")
        return state

    def _save(self, execution_key: tuple[str, int], state: dict[str, Any]) -> None:
        state["version"] = int(state.get("version", 0)) + 1
        state["updated_at"] = _now()
        _write_json(self._state_path(execution_key), state)

    @contextmanager
    def _guard(self, execution_key: tuple[str, int]) -> Iterator[None]:
        """Serialise spawn/decide/write for one (worker, epoch) across processes.

        The decision to spawn a delivery must be made under a machine-wide lock;
        two Controllers in different processes both reading an unchanged state
        file would otherwise spawn two model processes for the same delivery.
        """
        with ExecutionLock(self._dir(execution_key) / ".lock"):
            with self._lock:
                yield







    def _tool_context_path(self, execution_key: tuple[str, int],
                           delivery_key: str) -> Path:
        return self._dir(execution_key) / "tool-context" / f"{delivery_key}.json"









    def _exit_path(self, execution_key: tuple[str, int], delivery_key: str) -> Path:
        return self._dir(execution_key) / "exit" / f"{delivery_key}.json"

    def _events_path(self, execution_key: tuple[str, int], delivery_key: str) -> Path:
        return self._dir(execution_key) / "events" / f"{delivery_key}.jsonl"

    def _prompt_path(self, execution_key: tuple[str, int], delivery_key: str) -> Path:
        return self._dir(execution_key) / "prompt" / f"{delivery_key}.txt"

    def _receipt_path(self, execution_key: tuple[str, int]) -> Path:
        return self._dir(execution_key) / "tool-receipts.jsonl"

    def _budget_path(self, execution_key: tuple[str, int], delivery_key: str) -> Path:
        return self._dir(execution_key) / "budget" / f"{delivery_key}.json"

    def _yield_path(self, execution_key: tuple[str, int], delivery_key: str) -> Path:
        return self._dir(execution_key) / "yield" / f"{delivery_key}.json"

    def _unmatched(self, execution_key: tuple[str, int],
                   state: Mapping[str, Any]) -> list[dict[str, Any]]:
        resolved = {str(call) for call in state.get("reconciled_calls", [])}
        _, unmatched = _receipts(self._receipt_path(execution_key))
        return [entry for entry in unmatched
                if str(entry.get("call_id")) not in resolved]

    def _delivery_usage(self, execution_key: tuple[str, int],
                        state: Mapping[str, Any], key: str) -> dict[str, int]:
        record = _read_json(self._budget_path(execution_key, key))
        if isinstance(record, dict) and record.get("extension") is True:
            counts = record.get("counts") or {}
            return {"provider_calls": int(counts.get("provider_calls", 0)),
                    "tool_calls": int(counts.get("tool_calls", 0))}
        events_ref = state["deliveries"].get(key, {}).get("events_ref")
        if not events_ref:
            return {"provider_calls": 0, "tool_calls": 0}
        parsed = _parse_events(Path(events_ref))
        return {"provider_calls": parsed["provider_calls"],
                "tool_calls": parsed["tool_starts"]}

    def _usage(self, execution_key: tuple[str, int],
               state: Mapping[str, Any]) -> dict[str, int]:
        """Cumulative provider/tool calls across every generation and delivery.

        Prior generations live only in the API's committed worker usage; the
        execution directory for a new epoch starts empty, so it must be seeded
        from the Controller-supplied base rather than reset to zero.
        """
        usage = _usage_counts(state.get("usage_base"))
        for key in state.get("order", []):
            delivery = self._delivery_usage(execution_key, state, key)
            usage["provider_calls"] += delivery["provider_calls"]
            usage["tool_calls"] += delivery["tool_calls"]
        return usage

    execution_log_kind = EXECUTION_LOG_KIND

    def execution_log(self, *, run_id: str, worker_id: str, epoch: int,
                      delivery_key: str, after: int = 0,
                      limit: int = 100) -> ExecutionLog:
        """Bounded byte-offset read of one delivery's Pi event stream.

        The Backend resolves the path from its own root; callers never supply
        one. A half-written final line stays unconsumed until it is complete.
        Prompts, thinking text and raw tool output bodies are not returned.
        """
        if (not SAFE_EXECUTION_ID.match(worker_id or "")
                or not SAFE_EXECUTION_ID.match(delivery_key or "")):
            raise BackendError("invalid_argument", "unsafe execution identifier")
        if after < 0:
            raise BackendError("invalid_argument", "execution offset must be >= 0")
        if limit < 1:
            raise BackendError("invalid_argument", "execution limit must be >= 1")
        execution_dir = self.root / worker_id / str(epoch)
        state_path = execution_dir / "state.json"
        if not state_path.is_file():
            return _execution_missing("execution state is missing", offset=after)
        state = _read_json(state_path)
        if not isinstance(state, Mapping) or state.get("run_id") != run_id \
                or state.get("worker_id") != worker_id \
                or int(state.get("epoch", -1)) != epoch:
            return _execution_missing(
                "execution identity does not match the committed binding",
                offset=after)
        delivery = (state.get("deliveries") or {}).get(delivery_key)
        if not isinstance(delivery, Mapping):
            return _execution_missing("delivery is not recorded in this execution",
                                      offset=after)
        path = execution_dir / "events" / f"{delivery_key}.jsonl"
        if not path.is_file():
            return _execution_missing("delivery event log has not been written",
                                      offset=after,
                                      detail={"delivery_state": delivery.get("state")})
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
                # Complete a line even when it crosses several read chunks.
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
                    entries.append(_observation_entry(
                        event, offset=line_offset, line_bytes=len(raw)))
                    continue
                if eof:
                    # Only the unfinished final line is left for the next read.
                    break
                if len(buffer) < EXECUTION_LOG_MAX_BYTES and budget <= 0:
                    # This page's byte budget ran out while a potentially
                    # valid record was still being assembled. Do not consume
                    # it or report a gap: stop at its start and let the next
                    # request reread it with a fresh budget.
                    break
                # No newline within the per-record bound: this individual
                # record is provably larger than one bounded read. Skip
                # forward to its boundary, bounded by this page's budget, and
                # report the gap instead of returning the same cursor.
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
                entries.append({"kind": "gap", "offset": line_offset,
                                "line_bytes": skipped,
                                "reason": "record exceeds bounded execution read",
                                "truncated": consumed < size})
                if consumed >= size:
                    break
            at_eof = handle.tell() >= size or eof
        # Only a trailing line without a newline is an incomplete line. Leftover
        # complete lines after a limit stop are durable data for the next page.
        partial = bool(buffer) and b"\n" not in buffer and at_eof
        has_more = size > consumed and not partial
        return ExecutionLog(
            source=EXECUTION_LOG_KIND,
            availability="available",
            entries=tuple(entries),
            offset=after,
            next_offset=consumed,
            has_more=has_more and not partial,
            partial=partial,
            detail={
                "delivery_state": delivery.get("state"),
                "delivery_started_at": delivery.get("started_at"),
                "delivery_finished_at": delivery.get("finished_at"),
                "size": size,
                "malformed": malformed,
                "gap": gap,
                "tool_receipts": "separate-execution-log",
            },
        )


    @staticmethod
    def _caps(budget: Mapping[str, Any]) -> dict[str, int]:
        caps = {}
        if "max_provider_calls" in budget:
            caps["provider_calls"] = int(budget["max_provider_calls"])
        if "max_tool_calls" in budget:
            caps["tool_calls"] = int(budget["max_tool_calls"])
        return caps

    def _wall_limit(self, state: Mapping[str, Any]) -> float:
        budget = state.get("budget", {})
        limit = budget.get("max_wall_seconds", self.max_wall_seconds)
        try:
            return float(limit)
        except (TypeError, ValueError):
            return float(self.max_wall_seconds)

    # -- lifecycle -------------------------------------------------------
    def capabilities(self) -> Capabilities:
        return Capabilities(
            resumable_session=True,
            live_observation="limited",
            idempotent_delivery=False,
            stop="process",
            checkpoint=True,
            backend_tools=tuple(self.tools),
            internal_activities="none",
        )

    def start(self, spec: WorkerSpec, execution_key: tuple[str, int]) -> Handle:
        with self._guard(execution_key):
            state = _read_json(self._state_path(execution_key))
            stopping = spec.desired == "stopped"
            if state is None:
                state = self._new_state(spec, execution_key)
                state["budget"] = dict(spec.budget)
                if not stopping:
                    self._tools(state)
                    self._system_grants(state)
                    self._materialize(state, spec)
                    state["prepared"] = True
                self._save(execution_key, state)
            else:
                # The persisted agent/context/tool binding is the ownership
                # identity for both paths. Current tool availability, grants
                # and material integrity are active-work requirements only: a
                # committed stop must still reach an execution whose deployment
                # inputs changed after it started.
                self._verify_binding(state, spec)
                changed = False
                if not stopping:
                    if not state.get("prepared", True):
                        # A state created only to record a stop was never
                        # prepared. Active work must materialize and validate
                        # its frozen inputs before it can use this execution.
                        self._tools(state)
                        self._system_grants(state)
                        self._materialize(state, spec)
                        state["prepared"] = True
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
                # Repeated Controller starts of an unchanged execution must not
                # advance the observation version: a version bump would make
                # polling look like a new completed turn.
                if changed:
                    self._save(execution_key, state)
            return self._handle(spec, state)

    def _new_state(self, spec: WorkerSpec, execution_key: tuple[str, int]) -> dict[str, Any]:
        worker_dir = self._dir(execution_key)
        session_id = f"agentgenome-{spec.worker_id}-e{spec.epoch}"
        state = {
            "format": FORMAT,
            "session_format": SESSION_FORMAT,
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
            "session_id": session_id,
            "session_dir": str(worker_dir / "sessions"),
            "workspace": str(worker_dir / "workspace"),
            "deliveries": {},
            "order": [],
            "current": None,
            "stop": None,
            "checkpoint": None,
            "reconciled_calls": [],
            "last_observation_digest": None,
            "usage_base": _usage_counts(spec.usage),
            "version": 0,
            "created_at": _now(),
        }
        for name in ("sessions", "workspace", "events", "exit", "checkpoints"):
            (worker_dir / name).mkdir(parents=True, exist_ok=True)
        return state

    def _spawn_record_path(self, execution_key: tuple[str, int],
                           delivery_key: str) -> Path:
        return self._dir(execution_key) / "spawn" / f"{delivery_key}.json"

    def _identity(self, execution_key: tuple[str, int], delivery_key: str | None,
                  delivery: Mapping[str, Any], *, wait: float = 0.0
                  ) -> tuple[int, int | None] | None:
        """Durable pid/start identity of the current delivery's process.

        The delivery record is authoritative once the Controller saved the
        pid. If the Controller died between Popen and that save, the launcher
        already wrote the same identity to the spawn record; only a matching
        delivery key is accepted so a record from another round cannot answer
        for this one. ``wait`` covers the few milliseconds between Popen and
        the child's record becoming visible.
        """
        pid = delivery.get("pid")
        started = delivery.get("process_start")
        if isinstance(pid, int) and pid > 0:
            return pid, started if isinstance(started, int) else None
        if not isinstance(delivery_key, str) or not delivery_key:
            return None
        path = self._spawn_record_path(execution_key, delivery_key)
        deadline = time.monotonic() + max(wait, 0.0)
        while True:
            record = _read_json(path)
            if (isinstance(record, dict) and
                    record.get("format") == SPAWN_RECORD_FORMAT and
                    record.get("delivery_key") == delivery_key):
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
        """The live process that belongs to the current durable delivery.

        A cached Popen is reused only when both its pid and its spawn-time
        kernel identity match; a handle from an earlier delivery must never
        answer for the current one. Without a recorded start time the cache is
        not trusted. A process recorded by another Controller is adopted by
        pid plus start time; a process whose identity is not known is not
        guessed into existence.
        """
        if not resolved:
            identity = self._identity(execution_key, delivery_key, delivery, wait=wait)
        if identity is None:
            return None
        pid, started = identity
        cached = self._procs.get(execution_key)
        if cached is not None:
            process, cached_start = cached
            if (process.pid == pid and isinstance(started, int) and
                    cached_start == started):
                return process
        if _pid_alive(pid, started):
            return _RecordedProcess(pid, started)
        return None

    def _handle(self, spec: WorkerSpec, state: Mapping[str, Any]) -> Handle:
        current = state.get("current")
        delivery = state["deliveries"].get(current) if current else {}
        process = self._current_process((spec.worker_id, spec.epoch), current,
                                        delivery or {})
        pid = process.pid if process is not None and process.poll() is None else None
        return Handle(worker_id=spec.worker_id, epoch=spec.epoch,
                      workspace=state["workspace"], session_ref=state["session_id"],
                      pid=pid, state="active" if pid else "idle")

    def deliver(self, handle: Handle, message: Mapping[str, Any],
                delivery_key: str) -> DeliveryReceipt:
        if not isinstance(delivery_key, str) or not delivery_key:
            raise BackendError("invalid_argument", "delivery_key is required")
        if not isinstance(message, Mapping) or not isinstance(message.get("brief"), str) \
                or not message["brief"].strip():
            raise BackendError("invalid_argument", "message.brief must be a non-empty string")
        with self._guard(handle.execution_key):
            state = self._state(handle.execution_key)
            existing = state["deliveries"].get(delivery_key)
            if existing is not None:
                return DeliveryReceipt(delivery_key=delivery_key, accepted=True,
                                       outcome="duplicate", delivery_id=existing["delivery_id"],
                                       duplicate=True, error=existing.get("error"))
            if state.get("stop"):
                raise BackendError("stopped", "stop was requested; no new delivery is accepted")
            if state.get("prepared", True) is False:
                # A state created only to record a stop was never materialized
                # or validated. Delivery must not launch work before an active
                # start has prepared the frozen inputs and tools.
                raise BackendError(
                    "capability_unsupported",
                    "the execution was created only to record a stop and is not "
                    "prepared for work; start it active before delivering",
                    prepared=False)
            current = state.get("current")
            if current is not None:
                delivery = state["deliveries"][current]
                proc = self._current_process(handle.execution_key, current, delivery)
                alive = proc is not None and proc.poll() is None
                exit_record = _read_json(self._exit_path(handle.execution_key, current))
                if alive or (delivery.get("state") == "running" and exit_record is None):
                    raise BackendError("busy", "the previous delivery is still running",
                                       delivery_key=current)
            unmatched = self._unmatched(handle.execution_key, state)
            caps = self._caps(state.get("budget", {}))
            usage = self._usage(handle.execution_key, state)
            exhausted = [name for name, cap in caps.items() if usage[name] >= cap]
            if exhausted:
                raise BackendError(
                    "budget_exhausted",
                    "the worker's cumulative budget is exhausted; no new delivery is accepted",
                    budget=exhausted, usage=usage, caps=caps)
            if unmatched:
                raise BackendError(
                    "unknown_effect",
                    "a previous tool effect has an unknown outcome; reconcile before resuming",
                    tools=[entry.get("name") for entry in unmatched])
            # Validate grants, review binding and every granted material before
            # the delivery is recorded, so a rejected delivery never looks like
            # a running one and no model call happens with missing bytes.
            self._system_tools(state, message)
            self._verify_materials(state)
            materials = self._materialize_message(state, delivery_key, message)
            delivery_id = len(state["order"]) + 1
            events = self._events_path(handle.execution_key, delivery_key)
            stderr = self._dir(handle.execution_key) / "events" / f"{delivery_key}.stderr"
            events.parent.mkdir(parents=True, exist_ok=True)
            state["deliveries"][delivery_key] = {
                "delivery_id": delivery_id,
                "delivery_key": delivery_key,
                "message": dict(message),
                "prompt_ref": str(self._prompt_path(handle.execution_key, delivery_key)),
                "state": "running",
                "pid": None,
                "started_at": _now(),
                "started_ts": time.time(),
                "wall_timeout": self._wall_limit(state),
                "finished_at": None,
                "events_ref": str(events),
                "stderr_ref": str(stderr),
                "usage_before": usage,
                "materials": materials,
            }
            state["order"].append(delivery_key)
            state["current"] = delivery_key
            self._save(handle.execution_key, state)
            self._spawn(handle, state, delivery_key, message, events, stderr)
            return DeliveryReceipt(delivery_key=delivery_key, accepted=True,
                                   outcome="running", delivery_id=delivery_id)

    def _spawn(self, handle: Handle, state: dict[str, Any], delivery_key: str,
               message: Mapping[str, Any], events: Path, stderr: Path) -> None:
        execution_key = handle.execution_key
        worker_dir = self._dir(execution_key)
        result_path = message.get("result_path")
        if isinstance(result_path, str) and result_path:
            stale = _safe_child(Path(state["workspace"]), result_path)
            if stale.exists() and stale.is_file():
                stale.unlink()
        backend_tools = self._tools(state)
        system_names, system_specs, binding, decision = self._system_tools(state, message)
        self._verify_materials(state)
        delivered = state["deliveries"][delivery_key].get("materials") or []
        prompt = self._prompt(state, message, backend_tools,
                              system_tools=tuple(system_names), review=binding,
                              decision=decision, delivered=delivered)
        # Keep the exact prompt this delivery launched with, so the committed
        # agent/context instructions can be audited without replaying a model.
        prompt_path = self._prompt_path(execution_key, delivery_key)
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(prompt, encoding="utf-8")
        budget = state.get("budget", {})
        limit = self._wall_limit(state)
        caps = self._caps(budget)
        prior = state["deliveries"][delivery_key].get("usage_before", {})
        budget_path = self._budget_path(execution_key, delivery_key)
        _write_json(budget_path, {
            "format": "agentgenome-pi-budget/1",
            "delivery_key": delivery_key,
            "caps": dict(caps),
            "prior": {"provider_calls": int(prior.get("provider_calls", 0)),
                      "tool_calls": int(prior.get("tool_calls", 0))},
            "counts": {"provider_calls": 0, "tool_calls": 0},
            "budget": [],
            "budget_exceeded": False,
            "extension": False,
        })
        tools = self._tools(state)
        context = state.get("context") or {}
        role = str(message.get("role") or context.get("role") or "worker")
        tool_context = self._tool_context_path(execution_key, delivery_key)
        if system_specs:
            if self.credential_issuer is None:
                raise BackendError(
                    "capability_unsupported",
                    "system tools require the running network service issuer")
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
                raise BackendError("backend_unavailable",
                                   "the service issued no worker endpoint")
            if not isinstance(token, str) or not token:
                raise BackendError("backend_unavailable",
                                   "the service issued no worker credential")
            tool_context.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(tool_context.parent, 0o700)
            _write_json(tool_context, {
                "format": TOOL_CONTEXT_FORMAT,
                "endpoint": endpoint,
                "token": token,
                "run_id": state["run_id"],
                "node_id": state["node_id"],
                "worker_id": state["worker_id"],
                "epoch": state["epoch"],
                "delivery_key": delivery_key,
                "message_kind": message.get("kind"),
                "workspace": state["workspace"],
                "tools": system_specs,
            })
            os.chmod(tool_context, 0o600)
        else:
            tool_context.unlink(missing_ok=True)
        args = [
            self.executable, "--print", "--mode", "json",
            "--provider", self.provider, "--model", self.model,
            "--session-id", state["session_id"], "--session-dir", state["session_dir"],
            "--no-extensions",
        ]
        if tools:
            args += ["--extension", self.tools_module]
        if system_names:
            args += ["--extension", str(Path(__file__).with_name("pi_node_tools.mjs"))]
        args += [
            "--extension", str(Path(__file__).with_name("pi_budget.mjs")),
            "--no-skills", "--no-prompt-templates", "--no-context-files", "--no-themes",
            "--no-builtin-tools",
        ]
        exposed = tuple(tools) + tuple(system_names)
        if exposed:
            args += ["--tools", ",".join(exposed)]
        system_prompt = [
            f"You are agent {state.get('agent_ref')} acting as the {role} "
            f"for node {state['node_id']}. Use only the tools granted to this "
            "Worker and the frozen inputs already placed in the workspace. "
            "Do not modify files outside the worker workspace."
        ]
        if decision is not None:
            system_prompt.append(
                "Answer the bound child decision with node.resolve_decision "
                "before doing anything else, then continue your own task.")
        elif "node.request_decision" in system_names:
            system_prompt.append(
                "Do not ask questions in plain text. If the task truly needs a "
                "parent decision, call node.request_decision once; that ends "
                "this turn until the answer arrives.")
        else:
            system_prompt.append(
                "Do not ask questions and do not wait for further input.")
        args += [
            "--thinking", "off",
            "--system-prompt", " ".join(system_prompt),
            "--", prompt,
        ]
        yield_path = self._yield_path(execution_key, delivery_key)
        yield_path.parent.mkdir(parents=True, exist_ok=True)
        yield_path.unlink(missing_ok=True)
        environment = dict(os.environ)
        environment.update({
            "AGENTGENOME_WORKER_ID": state["worker_id"],
            "AGENTGENOME_NODE_ID": state["node_id"],
            "AGENTGENOME_RUN_ID": state["run_id"],
            "AGENTGENOME_EPOCH": str(state["epoch"]),
            "AGENTGENOME_WORKSPACE": state["workspace"],
            "AGENTGENOME_TOOL_RECEIPT": str(self._receipt_path(execution_key)),
            "AGENTGENOME_EVENTS": str(events),
            "AGENTGENOME_BUDGET_FILE": str(budget_path),
            "AGENTGENOME_YIELD_FILE": str(yield_path),
        })
        if system_specs:
            environment["AGENTGENOME_TOOL_CONTEXT"] = str(tool_context)
        else:
            environment.pop("AGENTGENOME_TOOL_CONTEXT", None)
        spawn_record = self._spawn_record_path(execution_key, delivery_key)
        spawn_record.unlink(missing_ok=True)
        launch = [sys.executable, str(SPAWN_LAUNCHER), str(spawn_record),
                  delivery_key, *args]
        with open(events, "w", encoding="utf-8") as out, open(stderr, "w", encoding="utf-8") as err:
            try:
                process = subprocess.Popen(
                    launch, cwd=state["workspace"], stdout=out, stderr=err,
                    text=True, start_new_session=True, env=environment,
                )
            except OSError as error:
                delivery = state["deliveries"][delivery_key]
                delivery["state"] = "failed"
                delivery["error"] = {"code": "backend_unavailable",
                                     "message": f"could not start pi: {error}"}
                delivery["finished_at"] = _now()
                self._save(execution_key, state)
                raise BackendError("backend_unavailable", "could not start pi") from error
        # The launcher already recorded the same identity before exec; this
        # save is the normal fast path, not the only durable copy.
        started = process_start(process.pid)
        self._procs[execution_key] = (process, started)
        delivery = state["deliveries"][delivery_key]
        delivery["pid"] = process.pid
        delivery["process_start"] = started
        self._save(execution_key, state)
        exit_path = self._exit_path(execution_key, delivery_key)
        threading.Thread(target=self._watch,
                         args=(process, exit_path, events, limit,
                               self.grace_seconds, caps, prior, budget_path),
                         name=f"pi-exit-{delivery_key}", daemon=True).start()

    @classmethod
    def _watch(cls, process: subprocess.Popen[str], exit_path: Path,
               events: Path, timeout: float, grace: float,
               caps: Mapping[str, int], prior: Mapping[str, int],
               budget_path: Path) -> None:
        reason = None
        counts = {"provider_calls": 0, "tool_calls": 0}
        delivery_counts = dict(counts)
        started = time.monotonic()

        def totals(parsed: Mapping[str, Any]) -> dict[str, int]:
            return {"provider_calls": prior.get("provider_calls", 0) + parsed["provider_calls"],
                    "tool_calls": prior.get("tool_calls", 0) + parsed["tool_starts"]}

        try:
            while True:
                try:
                    code = process.wait(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    pass
                parsed = _parse_events(events)
                counts = totals(parsed)
                delivery_counts = {"provider_calls": parsed["provider_calls"],
                                   "tool_calls": parsed["tool_starts"]}
                exceeded = [name for name, cap in caps.items()
                            if counts.get(name, 0) > cap]
                if exceeded:
                    reason = "budget_exceeded"
                    code = cls._kill(process, grace)
                    break
                if time.monotonic() - started > timeout:
                    reason = "wall_timeout"
                    code = cls._kill(process, grace)
                    break
            final = _parse_events(events)
            record = _read_json(budget_path)
            if isinstance(record, dict) and record.get("extension") is True:
                counts_record = record.get("counts") or {}
                delivery_counts = {
                    "provider_calls": int(counts_record.get("provider_calls", 0)),
                    "tool_calls": int(counts_record.get("tool_calls", 0)),
                }
                counts = {
                    "provider_calls": prior.get("provider_calls", 0)
                    + delivery_counts["provider_calls"],
                    "tool_calls": prior.get("tool_calls", 0)
                    + delivery_counts["tool_calls"],
                }
                if record.get("budget_exceeded"):
                    reason = "budget_exceeded"
                budget = list(record.get("budget") or [])
            else:
                counts = totals(final)
                delivery_counts = {"provider_calls": final["provider_calls"],
                                   "tool_calls": final["tool_starts"]}
                budget = [name for name, cap in caps.items()
                          if counts.get(name, 0) > cap]
                if reason is None and any(counts.get(name, 0) > cap
                                          for name, cap in caps.items()):
                    reason = "budget_exceeded"
            _write_json(exit_path, {
                "code": code,
                "signal": -code if code < 0 else None,
                "timed_out": reason == "wall_timeout",
                "budget_exceeded": reason == "budget_exceeded",
                "budget": budget,
                "timeout_seconds": timeout,
                "counts": counts,
                "delivery_counts": delivery_counts,
                "usage_prior": dict(prior),
                "caps": dict(caps),
                "finished_at": _now(),
            })
        finally:
            try:
                process.stdout and process.stdout.close()
            except Exception:
                pass

    @classmethod
    def _kill(cls, process: subprocess.Popen[str], grace: float) -> int:
        cls._terminate(process, signal.SIGTERM)
        try:
            return process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            cls._terminate(process, signal.SIGKILL)
            return process.wait()

    def _expire(self, process: Any) -> int:
        """Stop a process whose wall deadline passed without a live watcher."""
        grace = self.grace_seconds
        self._terminate(process, signal.SIGTERM)
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if process.poll() is not None:
                return -int(signal.SIGTERM)
            time.sleep(0.05)
        self._terminate(process, signal.SIGKILL)
        deadline = time.monotonic() + max(grace, 1.0)
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            time.sleep(0.05)
        return -int(signal.SIGKILL)


    def observe(self, handle: Handle, after: int | None = None) -> Observation:
        with self._guard(handle.execution_key):
            state = self._state(handle.execution_key)
            observation = self._compute(handle, state)
            # This controlled Pi profile registers no native subagent or
            # background-tool entry, so the empty registry is a complete fact.
            # Unknown tool side effects still surface in the main observation.
            observation = Observation(**{**observation.as_dict(),
                                         "activities": (),
                                         "activities_complete": True})
            value = canonical(observation.as_dict())
            if digest(value) != state.get("last_observation_digest"):
                state["last_observation_digest"] = digest(value)
                self._save(handle.execution_key, state)
            observation = Observation(**{**observation.as_dict(),
                                         "observation_id": int(state["version"]),
                                         "activities": tuple(observation.activities)})
            return observation

    def _compute(self, handle: Handle, state: dict[str, Any]) -> Observation:
        execution_key = handle.execution_key
        current = state.get("current")
        stop = state.get("stop")
        provider_calls = tool_calls = 0
        session_ref = state.get("session_id")
        checkpoint_ref = (state.get("checkpoint") or {}).get("digest")
        usage = self._usage(execution_key, state)
        provider_calls = usage["provider_calls"]
        tool_calls = usage["tool_calls"]
        if current is None:
            observed = "stopped" if stop else "idle"
            return Observation(observation_id=0, observed=observed, session_ref=session_ref,
                               checkpoint_ref=checkpoint_ref,
                               error={"code": "stopped_before_result"} if stop else None)
        delivery = state["deliveries"][current]
        events = _parse_events(Path(delivery["events_ref"]))
        provider_calls = usage["provider_calls"]
        tool_calls = usage["tool_calls"]
        # A restarted Controller may find the identity only in the launcher's
        # spawn record; give the child the few milliseconds it needs to write
        # it before calling the process unknown.
        identity = self._identity(execution_key, current, delivery, wait=2.0)
        process = self._current_process(execution_key, current, delivery,
                                        identity=identity, resolved=True)
        exit_record = _read_json(self._exit_path(execution_key, current))
        alive = process is not None and process.poll() is None
        if (process is not None and not alive and exit_record is None and
                not isinstance(process, _RecordedProcess)):
            # The watcher thread writes the exit record right after wait()
            # returns; observation must not call that gap "exit_unknown".
            deadline = time.monotonic() + 1.0
            while exit_record is None and time.monotonic() < deadline:
                time.sleep(0.02)
                exit_record = _read_json(self._exit_path(execution_key, current))
            if exit_record is None and process.returncode is not None:
                exit_record = {"code": process.returncode,
                               "signal": -process.returncode
                               if process.returncode < 0 else None,
                               "timed_out": False}
        timeout = delivery.get("wall_timeout")
        started_ts = delivery.get("started_ts")
        if (alive and not stop and isinstance(timeout, (int, float)) and
                timeout > 0 and isinstance(started_ts, (int, float)) and
                time.time() - started_ts > float(timeout)):
            # The Controller that spawned the process may be gone, so no
            # watcher enforces the wall deadline; observation does it from
            # the durable start time instead of letting the child run forever.
            code = self._expire(process)
            exit_record = {"code": code, "signal": -code,
                           "timed_out": True, "budget_exceeded": False,
                           "timeout_seconds": float(timeout),
                           "counts": dict(usage), "delivery_counts": dict(usage),
                           "finished_at": _now()}
            _write_json(self._exit_path(execution_key, current), exit_record)
            alive = False
        if alive and stop:
            if not stop.get("terminated"):
                self._terminate(process, signal.SIGTERM)
                stop["terminated"] = True
            requested = float(stop.get("requested_ts", 0))
            if time.time() - requested > self.grace_seconds:
                self._terminate(process, signal.SIGKILL)
                stop["escalated"] = True
        if alive:
            return Observation(observation_id=0, observed="active",
                               session_ref=session_ref, checkpoint_ref=checkpoint_ref,
                               delivery_key=current, provider_calls=provider_calls,
                               tool_calls=tool_calls, stop_requested=bool(stop),
                               error={"code": "stop_pending",
                                      "message": "stop was requested; the process is still running"}
                               if stop else None, event_ref=delivery["events_ref"])
        if exit_record is None and delivery.get("state") == "running":
            if identity is None:
                # No pid was committed and no spawn record names this
                # delivery: the process may still be alive, so neither a
                # committed stop nor its absence can be confirmed.
                return Observation(observation_id=0, observed="unknown",
                                   session_ref=session_ref, checkpoint_ref=checkpoint_ref,
                                   delivery_key=current, provider_calls=provider_calls,
                                   tool_calls=tool_calls, stop_requested=bool(stop),
                                   error={"code": "spawn_identity_unknown",
                                          "message": "no durable pid exists for this "
                                                     "delivery; its process cannot be verified",
                                          "delivery_key": current},
                                   event_ref=delivery["events_ref"])
            if not stop:
                return Observation(observation_id=0, observed="unknown",
                                   session_ref=session_ref, checkpoint_ref=checkpoint_ref,
                                   delivery_key=current, provider_calls=provider_calls,
                                   tool_calls=tool_calls, stop_requested=False,
                                   error={"code": "exit_unknown",
                                          "message": "the process disappeared without an exit record"},
                                   event_ref=delivery["events_ref"])
            # The process identity is known, it is gone, and a stop was
            # committed; no watcher exists to record its exit, so record the
            # observation instead of calling it unknown.
            exit_record = {"code": -int(signal.SIGTERM), "signal": signal.SIGTERM,
                           "timed_out": False, "budget_exceeded": False,
                           "counts": dict(usage), "delivery_counts": dict(usage),
                           "imputed": "stop observed after the watching Controller exited",
                           "finished_at": _now()}
            _write_json(self._exit_path(execution_key, current), exit_record)
        if exit_record and exit_record.get("budget_exceeded") and not stop:
            recorded = exit_record.get("counts") or {}
            return Observation(
                observation_id=0, observed="failed", session_ref=session_ref,
                checkpoint_ref=checkpoint_ref, delivery_key=current,
                provider_calls=provider_calls, tool_calls=tool_calls,
                error={"code": "budget_exceeded",
                       "message": "the delivery reached its provider or tool call budget; "
                                  "the next call was refused before execution",
                       "budget": list(exit_record.get("budget") or []),
                       "provider_calls": recorded.get("provider_calls", provider_calls),
                       "tool_calls": recorded.get("tool_calls", tool_calls)},
                event_ref=delivery["events_ref"])
        if exit_record and exit_record.get("timed_out") and not stop:
            return Observation(
                observation_id=0, observed="failed", session_ref=session_ref,
                checkpoint_ref=checkpoint_ref, delivery_key=current,
                provider_calls=provider_calls, tool_calls=tool_calls,
                error={"code": "wall_timeout",
                       "message": "the delivery exceeded its wall-clock budget",
                       "timeout_seconds": exit_record.get("timeout_seconds"),
                       "provider_calls": provider_calls, "tool_calls": tool_calls},
                event_ref=delivery["events_ref"])
        unmatched = self._unmatched(execution_key, state)
        if stop:
            if unmatched:
                return Observation(
                    observation_id=0, observed="unknown", session_ref=session_ref,
                    checkpoint_ref=checkpoint_ref, delivery_key=current,
                    provider_calls=provider_calls, tool_calls=tool_calls,
                    stop_requested=True,
                    error={"code": "tool_effect_unknown",
                           "message": "the worker stopped while a tool effect was in flight",
                           "tools": [entry.get("name") for entry in unmatched]},
                    event_ref=delivery["events_ref"])
            late = events["provider_calls"] > 0 and (exit_record or {}).get("code") == 0
            if late and not stop.get("late_result_ignored"):
                stop["late_result_ignored"] = True
                state["version"] += 1
                _write_json(self._state_path(execution_key), state)
            return Observation(observation_id=0, observed="stopped",
                               session_ref=session_ref, checkpoint_ref=checkpoint_ref,
                               delivery_key=current, provider_calls=provider_calls,
                               tool_calls=tool_calls, stop_requested=True,
                               error={"code": "stopped_before_result",
                                      "message": "a late completion after stop was ignored"}
                               if late else {"code": "stopped_before_result"},
                               event_ref=delivery["events_ref"])
        if unmatched:
            return Observation(observation_id=0, observed="unknown",
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
            # A committed decision request ends this turn deliberately. Pi
            # may exit through an aborted follow-up request, so the durable
            # yield marker is what distinguishes it from a provider failure.
            return Observation(observation_id=0, observed="idle",
                               session_ref=session_ref, checkpoint_ref=checkpoint_ref,
                               delivery_key=current, provider_calls=provider_calls,
                               tool_calls=tool_calls,
                               error={"code": "decision_pending",
                                      "message": "the worker yielded for a parent decision",
                                      "decision_id": yield_record.get("decision_id")},
                               event_ref=delivery["events_ref"])
        code = (exit_record or {}).get("code")
        if code not in (0, None):
            return Observation(observation_id=0, observed="failed",
                               session_ref=session_ref, checkpoint_ref=checkpoint_ref,
                               delivery_key=current, provider_calls=provider_calls,
                               tool_calls=tool_calls,
                               error={"code": "process_failed", "exit_code": code},
                               event_ref=delivery["events_ref"])
        result, error = self._result(state, delivery, events)
        # A declared result is a file the agent committed and can be verified.
        # Prose retained from an earlier turn is only progress reporting, so it
        # must not mask a terminal provider failure on the final turn.
        declared = isinstance(result, dict) and "result" in result
        if events.get("stop_reason") == "error" and not declared:
            # Pi exits 0 after its provider retries are exhausted and marks
            # the final assistant message stopReason=error. Treating that as
            # idle would leave the Node running with nothing left to happen.
            # A valid declared result or a submitted candidate still wins: a
            # trailing provider error does not erase work already delivered.
            return Observation(
                observation_id=0, observed="failed", session_ref=session_ref,
                checkpoint_ref=checkpoint_ref, delivery_key=current,
                result=None,
                error={"code": "provider_error",
                       "message": "the agent's provider failed for the final turn",
                       "stop_reason": "error",
                       "provider_error": events.get("error_message"),
                       "provider_calls": provider_calls,
                       "tool_calls": tool_calls},
                provider_calls=provider_calls, tool_calls=tool_calls,
                event_ref=delivery["events_ref"])
        return Observation(observation_id=0, observed="idle",
                           session_ref=session_ref, checkpoint_ref=checkpoint_ref,
                           delivery_key=current, result=result, error=error,
                           provider_calls=provider_calls, tool_calls=tool_calls,
                           event_ref=delivery["events_ref"])

    def _result(self, state: Mapping[str, Any], delivery: Mapping[str, Any],
                events: Mapping[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
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
        return {"final_text": final_text, "final_digest": digest(final_text)}, None

    # -- checkpoint/restore ---------------------------------------------
    def checkpoint(self, handle: Handle) -> CheckpointRef:
        with self._guard(handle.execution_key):
            state = self._state(handle.execution_key)
            current = state.get("current")
            delivery = state["deliveries"].get(current) if current else {}
            process = self._current_process(handle.execution_key, current,
                                            delivery or {})
            if process is not None and process.poll() is None:
                raise BackendError("busy", "cannot checkpoint while the process is running")
            sessions = Path(state["session_dir"])
            value = _digest_files(sessions) if sessions.exists() else digest({})
            target = self._dir(handle.execution_key) / "checkpoints" / value.split(":", 1)[1]
            (target / "sessions").mkdir(parents=True, exist_ok=True)
            for path in sessions.glob("*"):
                if path.is_file():
                    (target / "sessions" / path.name).write_bytes(path.read_bytes())
            reference = {"kind": "pi-session", "digest": value,
                         "session_id": state["session_id"], "path": str(target)}
            state["checkpoint"] = reference
            self._save(handle.execution_key, state)
            return CheckpointRef(**{key: reference[key] for key in
                                    ("kind", "digest", "session_id", "path")})

    def restore(self, spec: WorkerSpec, checkpoint: Mapping[str, Any],
                execution_key: tuple[str, int]) -> Handle:
        with self._guard(execution_key):
            state = _read_json(self._state_path(execution_key))
            if state is None:
                state = self._new_state(spec, execution_key)
                state["budget"] = dict(spec.budget)
            else:
                self._verify_binding(state, spec)
            self._materialize(state, spec)
            self._tools(state)
            self._system_grants(state)
            state["prepared"] = True
            if checkpoint:
                if checkpoint.get("kind") != "pi-session":
                    raise BackendError("capability_unsupported",
                                       "pi backend cannot restore this checkpoint")
                source = Path(str(checkpoint.get("path", ""))) / "sessions"
                if not source.is_dir():
                    raise BackendError("not_found", "checkpoint sessions are missing")
                target = Path(state["session_dir"])
                target.mkdir(parents=True, exist_ok=True)
                for path in source.glob("*"):
                    if path.is_file() and not (target / path.name).exists():
                        (target / path.name).write_bytes(path.read_bytes())
                state["session_id"] = checkpoint.get("session_id", state["session_id"])
                state["checkpoint"] = dict(checkpoint)
            self._save(execution_key, state)
            return self._handle(spec, state)

    # -- stop ------------------------------------------------------------
    def stop(self, handle: Handle, request_id: str) -> StopReceipt:
        if not isinstance(request_id, str) or not request_id:
            raise BackendError("invalid_argument", "request_id is required")
        with self._guard(handle.execution_key):
            state = self._state(handle.execution_key)
            previous = state.get("stop")
            if previous and previous.get("stopped_at"):
                return StopReceipt(request_id=request_id, accepted=True, already_stopped=True)
            if previous is None:
                state["stop"] = {"request_id": request_id, "requested_at": _now(),
                                 "requested_ts": time.time(), "stopped_at": None}
                self._save(handle.execution_key, state)
            current = state.get("current")
            delivery = state["deliveries"].get(current) if current else {}
            identity = (self._identity(handle.execution_key, current, delivery or {},
                                       wait=2.0) if current else None)
            process = self._current_process(handle.execution_key, current,
                                            delivery or {}, identity=identity,
                                            resolved=True)
            if process is not None:
                self._terminate(process, signal.SIGTERM)
                return StopReceipt(request_id=request_id, accepted=True,
                                   already_stopped=False, pid=process.pid)
            exit_record = (_read_json(self._exit_path(handle.execution_key, current))
                           if current else None)
            if (current and identity is None and exit_record is None and
                    (delivery or {}).get("state") == "running"):
                # No durable identity proves the child either alive or gone.
                # Confirm the stop only when absence is proven; a later
                # observation adopts the launcher record and performs the kill.
                return StopReceipt(request_id=request_id, accepted=True,
                                   already_stopped=False)
            state = self._state(handle.execution_key)
            state["stop"]["stopped_at"] = _now()
            self._save(handle.execution_key, state)
            return StopReceipt(request_id=request_id, accepted=True,
                               already_stopped=True)

    @staticmethod
    def _terminate(process: subprocess.Popen[str], sig: int) -> None:
        try:
            os.killpg(os.getpgid(process.pid), sig)
        except ProcessLookupError:
            pass

    # -- tool reconciliation --------------------------------------------
    def reconcile_tools(self, handle: Handle) -> dict[str, Any]:
        with self._guard(handle.execution_key):
            state = self._state(handle.execution_key)
            unmatched = self._unmatched(handle.execution_key, state)
            resolved: list[dict[str, Any]] = []
            unresolved: list[dict[str, Any]] = []
            workspace = Path(state["workspace"])
            for entry in unmatched:
                effect = entry.get("effect")
                if effect == "read_only":
                    resolved.append({"call_id": entry.get("call_id"), "effect": effect,
                                     "applied": False})
                    continue
                if effect == "idempotent":
                    target = entry.get("params", {}).get("path")
                    expected = entry.get("params", {}).get("text")
                    applied = False
                    if isinstance(target, str) and isinstance(expected, str):
                        try:
                            path = _safe_child(workspace, target)
                            applied = path.exists() and \
                                path.read_text(encoding="utf-8") == expected
                        except BackendError:
                            applied = False
                    resolved.append({"call_id": entry.get("call_id"), "effect": effect,
                                     "applied": applied})
                    continue
                unresolved.append({"call_id": entry.get("call_id"),
                                   "tool": entry.get("name"), "effect": effect})
            reconciled = {str(call) for call in state.get("reconciled_calls", [])}
            reconciled.update(str(item["call_id"]) for item in resolved)
            state["reconciled_calls"] = sorted(reconciled)
            current = state.get("current")
            if current and not unresolved and not state.get("stop"):
                delivery = state["deliveries"][current]
                if delivery.get("state") == "running":
                    delivery["state"] = "failed"
                    delivery["error"] = {"code": "tool_effects_reconciled",
                                         "message": "all in-flight effects are resolved; resume is safe"}
            if resolved:
                self._save(handle.execution_key, state)
            return {"resolved": resolved, "unresolved": unresolved,
                    "unknown_effects": unresolved}
