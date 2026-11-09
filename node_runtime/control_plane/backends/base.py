"""AgentBackend lifecycle protocol for the control plane.

A Backend owns the complete Agent loop for one (worker_id, epoch): it may call
the model many times and execute tools without asking the Scheduler per turn.
The Controller only starts/restores, delivers, observes, checkpoints and stops.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Mapping


OBSERVED_STATES = frozenset({"starting", "active", "idle", "stopped", "failed", "unknown"})
EFFECTS = frozenset({"read_only", "idempotent", "unknown"})


class BackendError(RuntimeError):
    def __init__(self, code: str, message: str = "", **details: Any) -> None:
        super().__init__(message or code)
        self.code = code
        self.details = details


@dataclass(frozen=True)
class Capabilities:
    resumable_session: bool
    live_observation: str          # "full" | "limited" | "none"
    idempotent_delivery: bool
    stop: str                      # "process" | "cooperative" | "none"
    checkpoint: bool
    # Available native tools, not a grant. None means the adapter cannot
    # describe its defaults; a parent cannot infer unlimited permissions.
    backend_tools: tuple[str, ...] | None = None
    # Descriptive only: "unknown" (default), "none" or "managed". A Backend
    # with "none" reports a complete empty internal activity registry.
    internal_activities: str = "unknown"

    def as_dict(self) -> dict[str, Any]:
        return {
            "resumable_session": self.resumable_session,
            "live_observation": self.live_observation,
            "idempotent_delivery": self.idempotent_delivery,
            "stop": self.stop,
            "checkpoint": self.checkpoint,
            "backend_tools": list(self.backend_tools) if self.backend_tools is not None else None,
            "internal_activities": self.internal_activities,
        }


@dataclass(frozen=True)
class WorkerSpec:
    """Immutable execution description handed to a Backend by the Controller."""

    worker_id: str
    node_id: str
    epoch: int
    intent_seq: int
    backend_ref: str
    agent_ref: str
    desired: str
    run_id: str = ""
    context: Mapping[str, Any] = field(default_factory=dict)
    tool_profile: Mapping[str, Any] = field(default_factory=dict)
    budget: Mapping[str, Any] = field(default_factory=dict)
    checkpoint_ref: Mapping[str, Any] | None = None
    usage: Mapping[str, Any] = field(default_factory=dict)
    # Persisted execution requirements. Controller checks actual adapter
    # support without loading Task/Agent definitions or traversing the graph.
    required_capabilities: Mapping[str, Any] = field(default_factory=dict)

    @property
    def execution_key(self) -> tuple[str, int]:
        return (self.worker_id, self.epoch)

    def as_dict(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "node_id": self.node_id,
            "epoch": self.epoch,
            "intent_seq": self.intent_seq,
            "backend_ref": self.backend_ref,
            "agent_ref": self.agent_ref,
            "desired": self.desired,
            "run_id": self.run_id,
            "context": dict(self.context),
            "tool_profile": dict(self.tool_profile),
            "budget": dict(self.budget),
            "checkpoint_ref": dict(self.checkpoint_ref) if self.checkpoint_ref else None,
            "usage": dict(self.usage),
            "required_capabilities": dict(self.required_capabilities),
        }


@dataclass(frozen=True)
class Handle:
    worker_id: str
    epoch: int
    workspace: str
    session_ref: str | None = None
    pid: int | None = None
    state: str = "starting"

    @property
    def execution_key(self) -> tuple[str, int]:
        return (self.worker_id, self.epoch)


@dataclass(frozen=True)
class DeliveryReceipt:
    delivery_key: str
    accepted: bool
    outcome: str                   # "running" | "delivered" | "duplicate" | "unknown" | "rejected"
    delivery_id: int = 0
    duplicate: bool = False
    error: Mapping[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "delivery_key": self.delivery_key,
            "accepted": self.accepted,
            "outcome": self.outcome,
            "delivery_id": self.delivery_id,
            "duplicate": self.duplicate,
            "error": dict(self.error) if self.error else None,
        }


@dataclass(frozen=True)
class Observation:
    observation_id: int
    observed: str
    session_ref: str | None = None
    checkpoint_ref: str | None = None
    delivery_key: str | None = None
    result: Mapping[str, Any] | None = None
    error: Mapping[str, Any] | None = None
    # None means this source cannot observe the count. Never estimate a model
    # request count from the number of CLI invocations or completed messages.
    provider_calls: int | None = 0
    tool_calls: int | None = 0
    stop_requested: bool = False
    event_ref: str | None = None
    # Empty with complete=False means the Backend cannot enumerate internal
    # activities; it is unknown, not proof that none exist.
    activities: tuple[Mapping[str, Any], ...] = ()
    activities_complete: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "observed": self.observed,
            "session_ref": self.session_ref,
            "checkpoint_ref": self.checkpoint_ref,
            "delivery_key": self.delivery_key,
            "result": dict(self.result) if self.result else None,
            "error": dict(self.error) if self.error else None,
            "provider_calls": self.provider_calls,
            "tool_calls": self.tool_calls,
            "stop_requested": self.stop_requested,
            "event_ref": self.event_ref,
            "activities": [dict(activity) for activity in self.activities],
            "activities_complete": self.activities_complete,
        }


@dataclass(frozen=True)
class CheckpointRef:
    kind: str
    digest: str
    session_id: str
    path: str

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "digest": self.digest,
                "session_id": self.session_id, "path": self.path}


@dataclass(frozen=True)
class ExecutionLog:
    """One bounded page of a Backend-owned execution observation log.

    Availability is explicit: "available", "missing" (no retained log for this
    execution/delivery), "unsupported" (adapter cannot expose one) or "error".
    A missing or unreadable log never changes a business run's outcome.
    """

    source: str = "unsupported"
    availability: str = "unsupported"
    entries: tuple[Mapping[str, Any], ...] = ()
    offset: int = 0
    next_offset: int = 0
    has_more: bool = False
    partial: bool = False
    # Source snapshot version; None for append-only sources (always resumable).
    snapshot: int | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "availability": self.availability,
            "entries": [dict(entry) for entry in self.entries],
            "offset": self.offset,
            "next_offset": self.next_offset,
            "has_more": self.has_more,
            "partial": self.partial,
            "snapshot": self.snapshot,
            "detail": dict(self.detail),
        }


@dataclass(frozen=True)
class StopReceipt:
    request_id: str
    accepted: bool
    already_stopped: bool
    pid: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"request_id": self.request_id, "accepted": self.accepted,
                "already_stopped": self.already_stopped, "pid": self.pid}


class AgentBackend(ABC):
    """Full Agent lifecycle. Implementations own their process/session state."""

    name = "abstract"
    # Identifies the execution-observation source in trace cursors. Only the
    # Backend knows its own log layout; callers never supply a path.
    execution_log_kind = "unsupported"

    @abstractmethod
    def capabilities(self) -> Capabilities: ...

    @abstractmethod
    def start(self, spec: WorkerSpec, execution_key: tuple[str, int]) -> Handle:
        """Create/restore an instance awaiting delivery, without advancing Goal.

        The delivery gate must remain effective: allocating a session cannot
        itself trigger model/tool work before the first allowed message.
        """
        ...

    @abstractmethod
    def deliver(self, handle: Handle, message: Mapping[str, Any],
                delivery_key: str) -> DeliveryReceipt: ...

    @abstractmethod
    def observe(self, handle: Handle, after: int | None = None) -> Observation: ...

    @abstractmethod
    def checkpoint(self, handle: Handle) -> CheckpointRef: ...

    @abstractmethod
    def restore(self, spec: WorkerSpec, checkpoint: Mapping[str, Any],
                execution_key: tuple[str, int]) -> Handle: ...

    @abstractmethod
    def stop(self, handle: Handle, request_id: str) -> StopReceipt: ...

    def reconcile_tools(self, handle: Handle) -> dict[str, Any]:
        """Resolve in-flight tool effects after a crash. Default: nothing known."""
        return {"unknown_effects": [], "resolved": [], "unresolved": True}

    def execution_log(self, *, run_id: str, worker_id: str, epoch: int,
                      delivery_key: str, after: int = 0,
                      limit: int = 100) -> ExecutionLog:
        """Read a bounded page of this execution's own observation log.

        Implementations must resolve their own trusted paths, verify the
        committed run/worker/epoch/delivery binding, read incrementally from
        ``after`` (a byte or entry offset stated in detail), preserve an
        incomplete final record for the next read, and report gaps/truncation
        or missing logs explicitly. Mutable sources must also set ``snapshot``
        so a cursor from another version can be rejected. Never infer token
        usage or completion.
        """
        return ExecutionLog(source=self.execution_log_kind,
                            availability="unsupported", offset=after,
                            next_offset=after)
