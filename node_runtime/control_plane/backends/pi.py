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
_PROMPT_CONTEXT_KEYS = frozenset({
    "role", "run_id", "node_id", "worker_id", "backend_ref", "agent_ref",
    "template_ref", "contract_ref", "input_refs", "reviewer_ref", "grants",
    "brief", "sop_brief", "instructions", "target_node_id", "candidate_id",
    "candidate_round", "review_inputs", "review_candidate_ids",
    "review_children", "progress", "progress_comparison",
})
SYSTEM_TOOLS = {
    "node.submit_candidate": (
        "Submit one candidate for this Node. output_refs is a list of complete "
        "artifact references exactly as returned by node.publish_artifact: "
        "{\"id\": \"sha256:<hex>\", \"sha256\": \"<64 hex>\", \"size\": <int>, "
        "\"media_type\": \"<type>\"}. Pass each returned object whole; a bare id "
        "or path is rejected."),
    "node.request_expansion": (
        "Ask the Scheduler to create child Nodes from an expansion proposal."),
    "node.request_decision": (
        "Ask this Node's parent for a decision and yield the turn until it "
        "answers. Use this only when the task truly depends on a choice you "
        "cannot make. The parent answer arrives as the next delivery."),
    "node.resolve_decision": (
        "Answer the bound child decision exactly once. decision_id comes from "
        "the delivered decision request. Include option only when the request "
        "offered options."),
    "node.submit_review": (
        "Submit exactly one review for the assigned candidate. feedback_ref "
        "accepts an artifact reference or {\"text\": \"...\"} to register "
        "feedback text under this reviewer. When the delivery states a frozen "
        "progress policy, progress={\"result\": improved|not_improved|unknown} "
        "is mandatory and unknown is the honest answer when the candidate is "
        "unusable or not comparable."),
    "node.revise_acceptance": (
        "Propose a versioned local acceptance contract for one direct child "
        "Node. previous_ref is the child's current contract_ref and "
        "criteria_ref is an artifact reference whose JSON object has "
        "input_schema, output_schema and acceptance. The criteria are ordinary "
        "JSON: formatting and key order are yours; only structure and content "
        "are checked. The frozen parent/upper contract is derived by the "
        "control plane; you cannot change it."),
    "node.publish_artifact": (
        "Publish a file from the worker workspace as an immutable artifact. "
        "path is relative to the workspace root and is exactly the path you "
        "passed to write_file (for example result.json, never "
        "workspace/result.json). Returns the artifact reference to pass in "
        "candidate output_refs."),
}
REVIEW_KINDS = ("review_request", "review_start")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _sha256_bytes(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


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


def _process_start(pid: int) -> int | None:
    """Kernel start time of a pid, used to reject a recycled pid."""
    try:
        line = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        return int(line[line.rindex(")") + 2:].split()[19])
    except (ValueError, IndexError):
        return None


def _pid_alive(pid: Any, started: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    current = _process_start(pid)
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


class PiBackend(AgentBackend):
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

    def _verify_binding(self, state: Mapping[str, Any], spec: WorkerSpec) -> None:
        if (state.get("agent_ref") != spec.agent_ref or
                dict(state.get("context") or {}) != dict(spec.context) or
                dict(state.get("tool_profile") or {}) != dict(spec.tool_profile)):
            raise BackendError(
                "capability_unsupported",
                "the committed agent/context/tool profile changed for this execution")

    def _tools(self, state: Mapping[str, Any]) -> tuple[str, ...]:
        profile = state.get("tool_profile") or {}
        if "backend_tools" not in profile:
            return tuple(self.tools)
        raw = profile["backend_tools"]
        if (not isinstance(raw, (list, tuple)) or
                any(not isinstance(name, str) for name in raw)):
            raise BackendError("capability_unsupported",
                               "tool_profile.backend_tools must be a list of tool names")
        unknown = [name for name in raw if name not in self.tools]
        if unknown:
            raise BackendError("capability_unsupported",
                               "the backend does not provide a granted tool",
                               tools=unknown, provided=list(self.tools))
        return tuple(raw)

    def _system_grants(self, state: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        """Validate system-tool grants from the committed Worker profile."""
        profile = state.get("tool_profile") or {}
        raw = profile.get("tools")
        if raw is None:
            return {}
        if isinstance(raw, Mapping):
            raw = list(raw.values())
        if not isinstance(raw, (list, tuple)):
            raise BackendError("capability_unsupported",
                               "tool_profile.tools must be a list or object")
        granted: dict[str, dict[str, Any]] = {}
        for item in raw:
            if isinstance(item, str):
                name, entry = item, {"name": item}
            elif isinstance(item, Mapping) and isinstance(item.get("name"), str):
                name, entry = item["name"], dict(item)
            else:
                raise BackendError("capability_unsupported",
                                   "tool_profile.tools entries need a name")
            if name not in SYSTEM_TOOLS:
                raise BackendError("capability_unsupported",
                                   "the bridge does not provide a granted system tool",
                                   tool=name)
            spec = TOOL_SPECS[name]
            if str(entry.get("version", spec.version)) != spec.version:
                raise BackendError("capability_unsupported",
                                   "the system tool version is not supported",
                                   tool=name, version=entry.get("version"))
            scope = str(entry.get("scope", spec.scope))
            if scope != spec.scope:
                raise BackendError("unauthorized",
                                   "the system tool scope is not granted",
                                   tool=name, scope=scope)
            granted[name] = entry
        if granted and self.credential_issuer is None:
            raise BackendError("capability_unsupported",
                               "system tools require the running network service "
                               "credential issuer")
        return granted

    @staticmethod
    def _review_binding(state: Mapping[str, Any],
                        message: Mapping[str, Any]) -> dict[str, Any] | None:
        if message.get("kind") not in REVIEW_KINDS:
            return None
        target = message.get("target_node_id") or message.get("node_id")
        candidate = message.get("candidate_id")
        round_number = message.get("candidate_round")
        reviewer = message.get("reviewer_ref")
        if not (isinstance(target, str) and target
                and isinstance(candidate, str) and candidate
                and isinstance(round_number, int)
                and isinstance(reviewer, str) and reviewer):
            raise BackendError("capability_unsupported",
                               "the review delivery does not bind a candidate",
                               kind=message.get("kind"))
        return {"target_node_id": target, "candidate_id": candidate,
                "candidate_round": round_number, "reviewer_ref": reviewer}

    @staticmethod
    def _decision_binding(state: Mapping[str, Any],
                          message: Mapping[str, Any]) -> dict[str, Any] | None:
        if message.get("kind") != "decision_request":
            return None
        decision_id = message.get("decision_id")
        child = message.get("child_node_id")
        if not (isinstance(decision_id, str) and decision_id
                and isinstance(child, str) and child):
            raise BackendError("capability_unsupported",
                               "the decision delivery does not bind an id",
                               kind=message.get("kind"))
        return {"decision_id": decision_id, "child_node_id": child}

    def _system_tools(self, state: Mapping[str, Any],
                      message: Mapping[str, Any]
                      ) -> tuple[list[str], list[dict[str, Any]],
                                 dict[str, Any] | None,
                                 dict[str, Any] | None]:
        granted = self._system_grants(state)
        binding = self._review_binding(state, message)
        decision = self._decision_binding(state, message)
        names: list[str] = []
        resources: list[dict[str, Any]] = []
        for name in sorted(granted):
            if name == "node.submit_review" and binding is None:
                # The grant is only live for the delivery that binds a candidate.
                continue
            if name == "node.resolve_decision" and decision is None:
                # The grant is only live for the delivery that binds a decision.
                continue
            names.append(name)
            resources.append({"name": name,
                              "description": SYSTEM_TOOLS[name],
                              "parameters": dict(TOOL_SPECS[name].input_schema)})
        return names, resources, binding, decision

    def _tool_context_path(self, execution_key: tuple[str, int],
                           delivery_key: str) -> Path:
        return self._dir(execution_key) / "tool-context" / f"{delivery_key}.json"

    @staticmethod
    def _frozen_ref(ref: Any) -> tuple[str, str, int]:
        if not isinstance(ref, Mapping) or "id" not in ref or "sha256" not in ref:
            raise BackendError("material_missing",
                               "message/input reference is not a frozen artifact",
                               ref=dict(ref) if isinstance(ref, Mapping) else ref)
        artifact_id = str(ref["id"])
        expected = str(ref["sha256"])
        if artifact_id != f"sha256:{expected}":
            raise BackendError("material_missing",
                               "reference digest is inconsistent",
                               artifact_id=artifact_id)
        size = ref.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise BackendError("material_missing",
                               "reference size is not a byte count",
                               artifact_id=artifact_id)
        return artifact_id, expected, size

    def _read_material(self, ref: Mapping[str, Any]) -> tuple[bytes, str, str, int]:
        artifact_id, expected, size = self._frozen_ref(ref)
        if self.artifact_reader is None:
            raise BackendError("capability_unsupported",
                               "no artifact reader is configured for frozen materials",
                               artifact_id=artifact_id)
        try:
            content = self.artifact_reader(ref)
        except BackendError:
            raise
        except Exception as error:
            raise BackendError("material_missing", "material could not be read",
                               artifact_id=artifact_id) from error
        if not isinstance(content, (bytes, bytearray)):
            raise BackendError("material_missing", "artifact reader must return bytes",
                               artifact_id=artifact_id)
        content = bytes(content)
        if _sha256_bytes(content) != artifact_id:
            raise BackendError("material_missing",
                               "artifact content does not match its frozen digest",
                               artifact_id=artifact_id)
        if len(content) != size:
            raise BackendError("material_missing",
                               "artifact size does not match its frozen reference",
                               artifact_id=artifact_id, expected=size,
                               actual=len(content))
        return content, artifact_id, expected, size

    @staticmethod
    def _material_dir(root: Path, *parts: str) -> Path:
        current = root
        for part in parts:
            current = current / part
            try:
                info = current.lstat()
            except FileNotFoundError:
                current.mkdir()
                continue
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise BackendError("path_symlink",
                                   "a material path component is not a real directory",
                                   path=str(current.relative_to(root)))
        return current

    @staticmethod
    def _write_material(path: Path, content: bytes) -> None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            info = None
        if info is not None and stat.S_ISLNK(info.st_mode):
            raise BackendError("path_symlink",
                               "a material file is a symlink", path=path.name)
        temporary = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        with open(temporary, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    def _materialize(self, state: dict[str, Any], spec: WorkerSpec) -> None:
        refs = list((spec.context or {}).get("input_refs") or [])
        total = sum(int(ref.get("size", 0)) for ref in refs
                    if isinstance(ref, Mapping))
        if self.max_context_bytes is not None and total > self.max_context_bytes:
            raise BackendError("budget_exhausted",
                               "the frozen context exceeds the backend limit",
                               size=total, limit=self.max_context_bytes)
        workspace = Path(state["workspace"])
        target_dir = self._material_dir(workspace, "inputs")
        materials: list[dict[str, Any]] = []
        for ref in refs:
            content, artifact_id, expected, size = self._read_material(ref)
            path = target_dir / expected
            if not path.exists() or _sha256_bytes(path.read_bytes()) != artifact_id:
                self._write_material(path, content)
            materials.append({
                "id": artifact_id, "sha256": expected, "size": len(content),
                "media_type": ref.get("media_type"),
                "path": path.relative_to(workspace).as_posix(),
            })
        state["materials"] = materials

    @staticmethod
    def _message_refs(message: Mapping[str, Any]) -> list[tuple[str, Any]]:
        refs: list[tuple[str, Any]] = []
        for field, role in (("input_refs", "input"),
                            ("output_refs", "candidate output"),
                            ("summary_ref", "candidate summary"),
                            ("feedback_ref", "review feedback"),
                            ("material_refs", "progress history"
                             if message.get("progress_comparison") is not None
                             else "decision material")):
            value = message.get(field)
            if value is None:
                continue
            if field in ("input_refs", "output_refs", "material_refs"):
                if not isinstance(value, list):
                    raise BackendError("material_missing",
                                       f"message {field} must be a list", field=field)
                refs.extend((role, item) for item in value)
            else:
                refs.append((role, value))
        prior = message.get("prior_feedback")
        if isinstance(prior, list):
            # A revised local contract starts a fresh epoch: the past verdicts
            # travel as real materials, not as a summary the model must trust.
            for entry in prior:
                if isinstance(entry, Mapping) and entry.get("feedback_ref") is not None:
                    refs.append(("review feedback", entry["feedback_ref"]))
        return refs

    def _materialize_message(self, state: Mapping[str, Any], delivery_key: str,
                             message: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Copy only the artifacts this delivery was granted into the workspace.

        The initial context stays immutable; message materials are per-delivery
        and fail before any process starts when missing, tampered or over the
        configured material budget.
        """
        initial = {str(item["id"]) for item in state.get("materials") or []}
        unique: list[tuple[str, dict[str, Any], str, str, int]] = []
        seen = set(initial)
        for role, ref in self._message_refs(message):
            artifact_id, expected, size = self._frozen_ref(ref)
            if artifact_id in seen:
                continue
            seen.add(artifact_id)
            unique.append((role, dict(ref), artifact_id, expected, size))
        total = sum(item[4] for item in unique)
        if self.max_context_bytes is not None and total > self.max_context_bytes:
            raise BackendError("budget_exhausted",
                               "the delivered materials exceed the backend limit",
                               kind="delivery_materials", size=total,
                               limit=self.max_context_bytes)
        if not unique:
            return []
        workspace = Path(state["workspace"])
        folder = self._material_dir(
            workspace, "messages",
            hashlib.sha256(delivery_key.encode("utf-8")).hexdigest()[:16])
        materials: list[dict[str, Any]] = []
        for role, ref, artifact_id, expected, size in unique:
            content, checked_id, checked, checked_size = self._read_material(ref)
            path = folder / checked
            if not path.exists() or _sha256_bytes(path.read_bytes()) != checked_id:
                self._write_material(path, content)
            materials.append({
                "id": checked_id, "sha256": checked, "size": checked_size,
                "media_type": ref.get("media_type"), "role": role,
                "path": path.relative_to(workspace).as_posix(),
            })
        return materials

    @staticmethod
    def _verify_materials(state: Mapping[str, Any]) -> None:
        workspace = Path(state["workspace"])
        tracked = list(state.get("materials") or [])
        for delivery in (state.get("deliveries") or {}).values():
            tracked.extend(delivery.get("materials") or [])
        seen: set[str] = set()
        for item in tracked:
            key = f"{item['path']}:{item['sha256']}"
            if key in seen:
                continue
            seen.add(key)
            path = workspace / str(item["path"])
            if not path.is_file() or path.is_symlink() or \
                    _sha256_bytes(path.read_bytes()) != f"sha256:{item['sha256']}":
                raise BackendError("material_missing",
                                   "a granted material was removed or modified",
                                   artifact_id=item["id"], path=item["path"])

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
            if state is None:
                state = self._new_state(spec, execution_key)
                state["budget"] = dict(spec.budget)
                self._tools(state)
                self._system_grants(state)
                self._materialize(state, spec)
                self._save(execution_key, state)
            else:
                self._verify_binding(state, spec)
                self._tools(state)
                self._system_grants(state)
                self._verify_materials(state)
                changed = False
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
        started = _process_start(process.pid)
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

    @staticmethod
    def _prompt(state: Mapping[str, Any], message: Mapping[str, Any],
                tools: tuple[str, ...], *, system_tools: tuple[str, ...] = (),
                review: Mapping[str, Any] | None = None,
                decision: Mapping[str, Any] | None = None,
                delivered: list[Mapping[str, Any]] | None = None) -> str:
        context = state.get("context") or {}
        role = str(message.get("role") or context.get("role") or "worker")
        lines = [
            f"WORKER: {state['worker_id']}",
            f"NODE: {state['node_id']}",
            f"EPOCH: {state['epoch']}",
            f"AGENT: {state.get('agent_ref')}",
            f"ROLE: {role}",
        ]
        for label, key in (("CONTRACT", "contract_ref"), ("TEMPLATE", "template_ref"),
                           ("CONTEXT", "brief"), ("SOP", "sop_brief"),
                           ("INSTRUCTIONS", "instructions")):
            value = context.get(key)
            if isinstance(value, str) and value.strip():
                lines.append(f"{label}: {value.strip()}")
        progress_policy = context.get("progress")
        if isinstance(progress_policy, Mapping) and progress_policy:
            lines.append(
                f"PROGRESS POLICY: {canonical(dict(progress_policy))}. Each "
                "candidate of this Node is checkpointed and judged for "
                "improvement by the bound evaluator.")
        business = {name: value for name, value in sorted(context.items())
                    if name not in _PROMPT_CONTEXT_KEYS}
        if business:
            lines.append("BUSINESS CONTEXT:")
            for name, value in business.items():
                lines.append(f"  {name}: {canonical(value)}")
        materials = list(state.get("materials") or [])
        if materials:
            lines.append("FROZEN INPUTS (read-only, already in the workspace):")
            for item in materials:
                media = item.get("media_type") or "application/octet-stream"
                lines.append(f"  - {item['path']} ({media}, {item['size']} bytes)")
            if review is not None:
                lines.append("Read these frozen candidate materials before deciding.")
        review_inputs = context.get("review_inputs")
        candidate_ids = context.get("review_candidate_ids")
        if review is not None and isinstance(review_inputs, Mapping) and review_inputs:
            lines.append("REVIEW INPUTS (frozen facts declared for this review):")
            for name, artifact_id in sorted(review_inputs.items()):
                sha = str(artifact_id).removeprefix("sha256:")
                lines.append(f"  - {name}: inputs/{sha}")
        if review is not None and isinstance(candidate_ids, list) and candidate_ids:
            lines.append("CANDIDATE OUTPUTS (the artifacts under review):")
            for artifact_id in candidate_ids:
                sha = str(artifact_id).removeprefix("sha256:")
                lines.append(f"  - inputs/{sha}")
        delivered = list(delivered or [])
        accepted_children = (context.get("review_children")
                             if review is not None else None) \
            or (message.get("review_children") if review is not None else None)
        if isinstance(accepted_children, list) and accepted_children:
            paths = {str(item["id"]): item["path"]
                     for item in list(state.get("materials") or []) + delivered}
            lines.append("ACCEPTED CHILDREN (committed results of the reviewed Node):")
            for child in accepted_children:
                if not isinstance(child, Mapping):
                    continue
                lines.append(f"  - node {child.get('node_id')} "
                             f"candidate {child.get('candidate_id')}")
                for artifact_id in child.get("output_ids") or []:
                    path = paths.get(str(artifact_id)) or \
                        f"inputs/{str(artifact_id).removeprefix('sha256:')}"
                    lines.append(f"    - {path}")
        if delivered:
            lines.append("DELIVERED MATERIALS (read-only, this delivery):")
            for item in delivered:
                media = item.get("media_type") or "application/octet-stream"
                lines.append(f"  - {item['path']} ({item.get('role', 'material')}, "
                             f"{media}, {item['size']} bytes)")
            if review is not None and any(
                    item.get("role") == "candidate output" for item in delivered):
                lines.append("Read every candidate output and summary before deciding.")
            elif any(item.get("role") == "review feedback" for item in delivered):
                lines.append("Read the review feedback before continuing.")
        children = message.get("children")
        if isinstance(children, list) and children:
            lines.append("CHILDREN (all accepted):")
            for child in children:
                if isinstance(child, Mapping):
                    lines.append(
                        f"  - {child.get('node_id')}: "
                        f"candidate {child.get('accepted_candidate_ref')}")
        accepted = message.get("accepted_children")
        if isinstance(accepted, list) and accepted:
            lines.append("ACCEPTED CHILDREN (committed; do not redo):")
            for child in accepted:
                if isinstance(child, Mapping):
                    lines.append(
                        f"  - {child.get('node_id')}: "
                        f"candidate {child.get('candidate_id')}")
        open_subtasks = message.get("open_subtasks")
        if isinstance(open_subtasks, list) and open_subtasks:
            lines.append("OPEN SUBTASKS (still owned by this Node):")
            for child in open_subtasks:
                if isinstance(child, Mapping):
                    lines.append(
                        f"  - {child.get('node_id')}: {child.get('state')}")
        reply = message.get("kind") == "decision_resolved"
        if decision is not None:
            lines.append("DECISION REQUEST (from your child):")
            lines.append(f"  decision: {decision['decision_id']}")
            lines.append(f"  child node: {decision['child_node_id']}")
            lines.append(f"  question: {message.get('question')}")
            lines.append(f"  reason: {message.get('reason')}")
            options = message.get("options")
            if isinstance(options, list) and options:
                lines.append(f"  options: {', '.join(map(str, options))}")
        if reply:
            lines.append("DECISION REPLY:")
            lines.append(f"  decision: {message.get('decision_id')}")
            lines.append(f"  answer: {message.get('answer')}")
            if message.get("option") is not None:
                lines.append(f"  option: {message['option']}")
            lines.append("Continue the original task with this answer.")
        if review is not None:
            lines.append(
                f"REVIEW: target node {review['target_node_id']}, "
                f"candidate {review['candidate_id']}, "
                f"round {review['candidate_round']}")
            review_policy = message.get("progress_policy")
            if isinstance(review_policy, Mapping) and review_policy:
                lines.append(
                    "PROGRESS POLICY: "
                    f"{canonical(dict(review_policy))}. Compare this candidate "
                    "with the previous evaluated checkpoint of the same window "
                    "and report one explicit progress result.")
            comparison = message.get("progress_comparison")
            if isinstance(comparison, Mapping):
                previous = comparison.get("previous")
                if isinstance(previous, Mapping):
                    paths = {
                        str(item["id"]): item["path"]
                        for item in (list(state.get("materials") or [])
                                     + list(delivered or []))}
                    lines.append(
                        "PROGRESS COMPARISON: current candidate "
                        f"{review['candidate_id']} (round "
                        f"{review['candidate_round']}) versus previous "
                        f"checkpoint {previous.get('candidate_id')} "
                        f"(round {previous.get('round')}, prior evaluation: "
                        f"{previous.get('result')}). The earlier checkpoint is a "
                        "committed evaluated candidate; it need not be accepted.")
                    for ref in previous.get("output_refs") or []:
                        path = paths.get(str(ref.get("id")))
                        if path is not None:
                            lines.append(f"  previous output: {path}")
                    summary_ref = previous.get("summary_ref")
                    if isinstance(summary_ref, Mapping):
                        path = paths.get(str(summary_ref.get("id")))
                        if path is not None:
                            lines.append(f"  previous summary: {path}")
                    evidence_ref = previous.get("evidence_ref")
                    if isinstance(evidence_ref, Mapping):
                        path = paths.get(str(evidence_ref.get("id")))
                        if path is not None:
                            lines.append(f"  previous review feedback: {path}")
                    lines.append(
                        "Only report improved or not_improved when the current "
                        "and previous materials are genuinely comparable; "
                        "otherwise report unknown.")
                else:
                    lines.append(
                        "PROGRESS COMPARISON: no previous evaluated checkpoint "
                        "exists in this window. Report unknown unless the frozen "
                        "policy declares a valid initial baseline.")
        elif role == "reviewer":
            lines.append(
                f"REVIEW: target node {context.get('target_node_id')}, "
                f"candidate {context.get('candidate_id')}, "
                f"round {context.get('candidate_round')}")
        lines += [
            "",
            "TASK:",
            str(message["brief"]).strip(),
        ]
        goal = message.get("goal")
        if isinstance(goal, str) and goal.strip():
            lines += ["", "GOAL:", goal.strip()]
        acceptance = message.get("acceptance")
        if isinstance(acceptance, str) and acceptance.strip():
            lines += ["", "ACCEPTANCE:", acceptance.strip()]
        if message.get("output_schema") is not None:
            lines += ["", "OUTPUT SCHEMA:", canonical(message["output_schema"])]
        upper_acceptance = message.get("upper_acceptance")
        if isinstance(upper_acceptance, str) and upper_acceptance.strip():
            lines += ["", "UPPER ACCEPTANCE (frozen; the local contract cannot"
                      " relax it):", upper_acceptance.strip()]
        if message.get("upper_output_schema") is not None:
            lines += ["", "UPPER OUTPUT SCHEMA (frozen):",
                      canonical(message["upper_output_schema"])]
        if isinstance(message.get("previous_contract_ref"), str):
            lines += ["", f"PREVIOUS CONTRACT: {message['previous_contract_ref']}"]
        previous_round = message.get("round")
        if isinstance(previous_round, int):
            lines += ["", f"ROUND: {previous_round}"]
        target = message.get("target")
        if review is not None and isinstance(target, Mapping):
            lines += ["", "TARGET TASK (the node under review):",
                      f"  node: {target.get('node_id')}",
                      f"  template: {target.get('template_ref')}",
                      f"  contract: {target.get('contract_ref')}"]
            target_goal = target.get("goal")
            if isinstance(target_goal, str) and target_goal.strip():
                lines.append(f"  goal: {target_goal.strip()}")
            target_acceptance = target.get("acceptance")
            if isinstance(target_acceptance, str) and target_acceptance.strip():
                lines.append(f"  acceptance: {target_acceptance.strip()}")
            if target.get("output_schema") is not None:
                lines.append(f"  output schema: {canonical(target['output_schema'])}")
            target_context = target.get("context")
            if isinstance(target_context, Mapping) and target_context:
                lines.append(f"  business context: {canonical(dict(target_context))}")
        lines += [
            "",
            (f"TOOLS: {', '.join(tuple(tools) + tuple(system_tools))}. "
             "Paths are relative to the workspace root, which is the current "
             "directory: write result.json, not workspace/result.json."
             if tools or system_tools else
             "TOOLS: none. Answer from the frozen inputs and reply."),
        ]
        if system_tools:
            lines.append(
                "SYSTEM TOOLS are recorded by the control plane; your identity, "
                "the review binding and the decision binding are fixed by the "
                "adapter, never by arguments.")
        if review is not None:
            action = (
                f"ACTION: call node.submit_review exactly once with "
                f"candidate_id={review['candidate_id']!r}, "
                f"candidate_round={review['candidate_round']}, your verdict, and "
                'feedback_ref={"text": "..."}')
            if isinstance(message.get("progress_policy"), Mapping):
                action += (
                    ', and progress={"result": "improved" | "not_improved" | '
                    '"unknown"} (required by the frozen progress policy; compare '
                    "against the delivered previous checkpoint and use unknown "
                    "when it is absent, unusable or not comparable)")
            lines.append(action + ".")
        elif decision is not None:
            lines.append(
                f"ACTION: call node.resolve_decision exactly once with "
                f"decision_id={decision['decision_id']!r} and answer=\"...\"; "
                "include option only when the request offered options. Then "
                "continue your own task.")
        elif "node.submit_candidate" in system_tools:
            candidate_round = message.get("round")
            round_hint = (str(candidate_round)
                          if isinstance(candidate_round, int)
                          and not isinstance(candidate_round, bool)
                          else "the candidate round declared in this delivery")
            submission_id = "candidate-" + digest({
                "node_id": state.get("node_id"),
                "worker_id": state.get("worker_id"),
                "epoch": state.get("epoch"),
                "contract_ref": context.get("contract_ref"),
                "round": candidate_round,
            }).removeprefix("sha256:")[:32]
            lines.append(
                "ACTION: write the Task's output file, then call "
                "node.publish_artifact with its relative path and media_type. "
                "Copy the WHOLE returned ArtifactRef, including its sha256: "
                "id, sha256, size and media_type. A write_file digest or file "
                "path is NOT a published artifact. Never guess references. "
                "Then call node.submit_candidate once using exactly "
                f"candidate_id={submission_id!r}, round={round_hint}, "
                "and output_refs=[the returned ArtifactRef]. "
                "This is the technical ID for this delivery's attempt; do not "
                "reuse an earlier candidate_id from session history. "
                "If publication/submission fails, fix that error; do not repeat "
                "the same invalid reference. A text answer is not a candidate.")
            if "node.request_decision" in system_tools:
                lines.append(
                    "If a necessary choice belongs to your parent, use the "
                    "granted node.request_decision tool and yield for its reply.")
        elif "node.request_decision" in system_tools:
            lines.append(
                "ACTION: finish the task when you can. If a choice truly "
                "belongs to your parent, call node.request_decision once and "
                "stop; the answer arrives as the next delivery.")
        result_path = message.get("result_path")
        if isinstance(result_path, str) and result_path:
            fields = message.get("required_fields") or []
            lines.append(
                f"RESULT: when finished, save a JSON object to {result_path!r} in the "
                f"workspace containing these fields: {', '.join(map(str, fields))}. "
                "Use one of the available tools above to write it; do not use a tool "
                "that is not listed. After the result exists, stop calling tools and "
                "reply with one short sentence. Do not write any other file."
            )
        elif review is None and decision is None and "node.submit_candidate" in system_tools:
            lines.append(
                "RESULT: after a successful candidate submission, acknowledge "
                "briefly and end this turn. The Node still awaits its separate "
                "parent/Checker acceptance; do not claim task completion.")
        else:
            lines.append("RESULT: when finished, reply with your final answer and stop.")
        if "node.request_decision" in system_tools or reply:
            lines.append("Do not ask questions in plain text.")
        else:
            lines.append("Do not ask questions. Do not wait for further input.")
        return "\n".join(lines)

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
