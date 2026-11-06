"""Read-only trace projection for the single AgentGenome service.

Codex P3 framework. Qoder fills the projection and Backend integration before
any route imports it. This module must not schedule tasks, infer business
acceptance, or keep another copy of authoritative run state.

Control events and execution observations have separate cursors. A snapshot
cursor comes from the same Api read transaction as its resources; advancing
the cursor in a later transaction would lose events between snapshot/watch.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Mapping

from .api import Api
from .resources import Actor, ControlPlaneError, require, require_int, require_text


TRACE_FORMAT = "agentgenome-trace/1"
EXECUTION_TRACE_FORMAT = "agentgenome-execution-trace/1"
EVENTS_FORMAT = "agentgenome-trace-events/1"
REQUESTS_FORMAT = "agentgenome-trace-requests/1"
CURSOR_FORMAT = "agentgenome-trace-cursor/1"
EXECUTION_CURSOR_FORMAT = "agentgenome-execution-cursor/1"
REQUEST_RECORD_FORMAT = "agentgenome-request-diagnostic/1"

MAX_LIMIT = 1000
EXECUTION_LIMIT = 500
TIMELINE_LIMIT = 200

DIAGNOSTIC_MAX_BYTES = 2 * 1024 * 1024
DIAGNOSTIC_PAGE_BYTES = 256 * 1024
DIAGNOSTIC_MAX_RECORD_BYTES = 8 * 1024

SAFE_DELIVERY_KEY = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")

EXECUTION_CURSOR_KEYS = frozenset(
    {"service_id", "run_id", "worker_id", "epoch", "delivery_key", "source",
     "offset", "snapshot"})
EXECUTION_CURSOR_REQUIRED_KEYS = EXECUTION_CURSOR_KEYS - {"snapshot"}


@dataclass(frozen=True)
class ControlCursor:
    """Namespace a durable sequence by service and run, including in SSE IDs."""

    service_id: str
    run_id: str
    seq: int

    def as_dict(self) -> dict[str, Any]:
        return {"service_id": self.service_id, "run_id": self.run_id,
                "seq": self.seq}

    def encode(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def decode(cls, value: str | Mapping[str, Any] | None, *,
               service_id: str, run_id: str) -> ControlCursor:
        if value is None:
            return cls(service_id, run_id, 0)
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (ValueError, TypeError) as error:
                raise ControlPlaneError(
                    "invalid_argument", "trace cursor must be a JSON object") from error
        if (not isinstance(value, Mapping)
                or set(value) != {"service_id", "run_id", "seq"}
                or type(value.get("seq")) is not int
                or value["seq"] < 0):
            raise ControlPlaneError("invalid_argument", "invalid trace cursor")
        if value["service_id"] != service_id or value["run_id"] != run_id:
            raise ControlPlaneError(
                "cursor_expired", "trace cursor belongs to another service or run",
                instruction="resnapshot")
        return cls(service_id, run_id, value["seq"])


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _truncate(value: Any, limit: int = 1024, depth: int = 0) -> Any:
    """Bound a diagnostic field without changing its JSON type."""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "...[truncated]"
    if depth > 3:
        return "[truncated]"
    if isinstance(value, list):
        return [_truncate(item, limit, depth + 1) for item in value[:50]]
    if isinstance(value, Mapping):
        return {str(key)[:128]: _truncate(item, limit, depth + 1)
                for key, item in list(value.items())[:50]}
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:limit]


class RequestDiagnostics:
    """Bounded append-only JSONL diagnostics owned by the service.

    Records operations, request ids and available business associations even
    when a request never committed. Never records credentials, raw bodies or
    hidden model text. This is not a second task store: nothing here is an
    authority on run state.
    """

    def __init__(self, path: Path | str, *, service_id: str,
                 max_bytes: int = DIAGNOSTIC_MAX_BYTES):
        self.path = Path(path)
        self.service_id = service_id
        self.max_bytes = max_bytes
        self._lock = threading.Lock()

    @property
    def rotated_path(self) -> Path:
        return self.path.with_name(self.path.name + ".1")

    def record(self, *, operation: str, outcome: str, **fields: Any) -> None:
        record: dict[str, Any] = {
            "format": REQUEST_RECORD_FORMAT,
            "service_id": self.service_id,
            "at": _iso_now(),
            "operation": _truncate(operation, 200),
            "outcome": _truncate(outcome, 40),
        }
        for key, value in fields.items():
            if value is None or key in (
                    "at", "format", "service_id", "operation", "outcome"):
                continue
            record[key] = _truncate(value)
        line = json.dumps(record, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False)
        if len(line.encode("utf-8")) > DIAGNOSTIC_MAX_RECORD_BYTES:
            record = {
                "format": REQUEST_RECORD_FORMAT,
                "service_id": self.service_id,
                "at": record["at"],
                "operation": record["operation"],
                "outcome": record["outcome"],
                "request_id": record.get("request_id"),
                "truncated": True,
            }
            line = json.dumps(record, sort_keys=True, separators=(",", ":"))
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                size = self.path.stat().st_size if self.path.exists() else 0
                if size and size + len(line) + 1 > self.max_bytes:
                    if self.rotated_path.exists():
                        self.rotated_path.unlink()
                    os.replace(self.path, self.rotated_path)
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:
                # Diagnostics must never fail the business request they describe.
                return

    def _parse(self, raw: bytes) -> dict[str, Any] | None:
        if not raw.strip():
            return None
        try:
            entry = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return {}
        if not isinstance(entry, dict):
            return {}
        return entry

    def generation(self) -> str | None:
        """Stable identity of the current log file, or None when absent.

        A rotation or replacement changes the inode, so a cursor issued
        against the previous generation can be rejected instead of silently
        reading a different file at the same byte offset.
        """
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return None
        # Append-stable identity. ctime/mtime change on every write and would
        # invalidate a cursor as soon as the next request is recorded; only a
        # replacement/rotation (new inode) makes the old byte offset unsafe.
        return f"{stat.st_dev:x}:{stat.st_ino:x}"

    def _cursor(self, offset: int, generation: str | None) -> dict[str, Any]:
        cursor = {"offset": offset, "generation": generation}
        cursor["token"] = json.dumps(cursor, sort_keys=True,
                                     separators=(",", ":"))
        return cursor

    def _resume(self, after: Any, generation: str | None) -> int:
        if after is None or after == "" or after == 0:
            return 0
        if isinstance(after, bool):
            raise ControlPlaneError("invalid_argument",
                                    "invalid request diagnostic cursor")
        if isinstance(after, int):
            if after < 0:
                raise ControlPlaneError("invalid_argument",
                                        "invalid request diagnostic cursor")
            raise ControlPlaneError(
                "invalid_argument",
                "request diagnostic offset requires its cursor token",
                instruction="use the next_cursor token from a read")
        if isinstance(after, Mapping):
            cursor = dict(after)
        elif isinstance(after, str):
            try:
                cursor = json.loads(after)
            except ValueError as error:
                raise ControlPlaneError(
                    "invalid_argument",
                    "invalid request diagnostic cursor") from error
        else:
            raise ControlPlaneError("invalid_argument",
                                    "invalid request diagnostic cursor")
        if not isinstance(cursor, dict):
            raise ControlPlaneError("invalid_argument",
                                    "invalid request diagnostic cursor")
        offset = cursor.get("offset")
        issued = cursor.get("generation")
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise ControlPlaneError("invalid_argument",
                                    "invalid request diagnostic cursor")
        if offset == 0 and issued is None:
            return 0
        if issued != generation:
            raise ControlPlaneError(
                "cursor_expired",
                "request diagnostic log rotated since this cursor was issued",
                instruction="resnapshot", generation=generation)
        return offset

    def read(self, *, request_id: str | None = None, run_id: str | None = None,
             after: Any = 0, limit: int = 100) -> dict[str, Any]:
        require_int(limit, "limit", minimum=1)
        require(limit <= MAX_LIMIT, "invalid_argument", "limit must be <= 1000")
        generation = self.generation()
        offset = self._resume(after, generation)
        base = {
            "format": REQUESTS_FORMAT,
            "service_id": self.service_id,
            "availability": {"kind": "bounded-jsonl",
                             "max_bytes": self.max_bytes,
                             "rotation": self.rotated_path.name},
        }
        if generation is None:
            cursor = self._cursor(0, None)
            return {**base, "records": [], "cursor": cursor,
                    "next_cursor": dict(cursor), "has_more": False,
                    "partial": False, "malformed": 0, "limit": limit}
        size = self.path.stat().st_size
        if offset > size:
            raise ControlPlaneError(
                "cursor_expired", "request diagnostic cursor is ahead of the log",
                instruction="resnapshot", size=size)
        records: list[dict[str, Any]] = []
        malformed = 0
        consumed = offset
        partial = False
        with self._lock:
            with open(self.path, "rb") as handle:
                handle.seek(offset)
                # Exactly one bounded page per read: a filter that matches
                # nothing returns an empty page with has_more instead of
                # scanning the whole log.
                chunk = handle.read(DIAGNOSTIC_PAGE_BYTES)
                while True:
                    newline = chunk.find(b"\n")
                    if newline < 0:
                        break
                    raw, chunk = chunk[:newline], chunk[newline + 1:]
                    line_start = consumed
                    consumed += newline + 1
                    if len(records) >= limit:
                        consumed = line_start
                        break
                    entry = self._parse(raw)
                    if entry is None:
                        continue
                    if entry == {}:
                        malformed += 1
                        continue
                    if request_id is not None and entry.get("request_id") != request_id:
                        continue
                    if run_id is not None and entry.get("run_id") != run_id:
                        continue
                    records.append(entry)
                if offset + DIAGNOSTIC_PAGE_BYTES >= size:
                    # Complete unread lines after a limit stop are durable
                    # data for the next page. Only a newline-free EOF tail is
                    # an incomplete record.
                    partial = bool(chunk) and b"\n" not in chunk
        cursor = self._cursor(offset, generation)
        next_cursor = self._cursor(consumed, generation)
        return {
            **base,
            "records": records,
            "cursor": cursor,
            "next_cursor": next_cursor,
            "has_more": size > consumed and not partial,
            "partial": partial,
            "malformed": malformed,
            "limit": limit,
        }


class TraceService:
    """One authorized projection shared by HTTP, finite MCP tools and CLI.

    Backends own their execution-log format and trusted paths. Execution
    readers must be incremental/bounded and preserve incomplete final lines;
    no caller can choose a server file path. Unavailable observation fields
    remain explicitly unavailable rather than becoming fabricated values.
    """

    def __init__(self, api: Api, *, service_id: str,
                 backends: Mapping[str, Any], request_log: Path | None = None):
        self.api = api
        self.service_id = service_id
        self.backends = backends
        self.request_log = request_log
        if request_log is None or isinstance(request_log, RequestDiagnostics):
            self.diagnostics: RequestDiagnostics | None = request_log
        else:
            self.diagnostics = RequestDiagnostics(
                request_log, service_id=service_id)

    # ------------------------------------------------------------------
    # cursors

    def _control_cursor(self, run_id: str, value: Any) -> ControlCursor:
        if value is None or value == "":
            return ControlCursor(self.service_id, run_id, 0)
        if isinstance(value, bool):
            raise ControlPlaneError("invalid_argument", "invalid trace cursor")
        if isinstance(value, int):
            if value < 0:
                raise ControlPlaneError("invalid_argument", "invalid trace cursor")
            return ControlCursor(self.service_id, run_id, value)
        return ControlCursor.decode(value, service_id=self.service_id, run_id=run_id)

    def _execution_cursor(self, value: Any, *, run_id: str, worker_id: str,
                          epoch: int, delivery_key: str,
                          source: str) -> tuple[int, int | None]:
        identity = {"service_id": self.service_id, "run_id": run_id,
                    "worker_id": worker_id, "epoch": epoch,
                    "delivery_key": delivery_key, "source": source}
        if value is None or value == "":
            return 0, None
        if isinstance(value, bool):
            raise ControlPlaneError("invalid_argument", "invalid execution cursor")
        if isinstance(value, int):
            if value < 0:
                raise ControlPlaneError("invalid_argument", "invalid execution cursor")
            return value, None
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (ValueError, TypeError) as error:
                raise ControlPlaneError(
                    "invalid_argument",
                    "execution cursor must be a JSON object") from error
        if not isinstance(value, Mapping):
            raise ControlPlaneError("invalid_argument", "invalid execution cursor")
        keys = set(value)
        if (not EXECUTION_CURSOR_REQUIRED_KEYS <= keys <= EXECUTION_CURSOR_KEYS
                or type(value.get("offset")) is not int
                or value["offset"] < 0
                or ("snapshot" in value
                    and value["snapshot"] is not None
                    and (type(value["snapshot"]) is not int
                         or value["snapshot"] < 0))):
            raise ControlPlaneError("invalid_argument", "invalid execution cursor")
        for key, expected in identity.items():
            if value.get(key) != expected:
                raise ControlPlaneError(
                    "cursor_expired",
                    "execution cursor belongs to another service, run or execution",
                    instruction="resnapshot")
        return value["offset"], value.get("snapshot")

    def _execution_cursor_dict(self, *, run_id: str, worker_id: str, epoch: int,
                               delivery_key: str, source: str,
                               offset: int, snapshot: int | None) -> dict[str, Any]:
        value = {"service_id": self.service_id, "run_id": run_id,
                 "worker_id": worker_id, "epoch": epoch,
                 "delivery_key": delivery_key, "source": source,
                 "offset": offset}
        if snapshot is not None:
            value["snapshot"] = snapshot
        return value

    # ------------------------------------------------------------------
    # snapshot

    def snapshot(self, actor: Actor, run_id: str, *,
                 node_id: str | None = None,
                 candidate_id: str | None = None) -> dict[str, Any]:
        """Consistent resources + cursor; decomposition and dependencies separate.

        Include committed candidate/review/decision/output associations and
        execution identities. Waiting explanations cite actual resource facts.
        Filtering must not change the underlying run cursor's meaning.
        """
        require_text(run_id, "run_id", max_length=128)
        if node_id is not None:
            require_text(node_id, "node_id", max_length=128)
        if candidate_id is not None:
            require_text(candidate_id, "candidate_id", max_length=128)
        resources = self.api.list_resources(actor, run_id)
        run = resources["run"]
        cursor = ControlCursor(self.service_id, run_id, int(run["event_seq"]))
        all_nodes = {node["node_id"]: node for node in resources["nodes"]}
        all_candidates = resources["candidates"]
        all_decisions = resources["decision_requests"]
        nodes = dict(all_nodes)
        workers = resources["workers"]
        candidates = all_candidates
        reviews = resources["reviews"]
        decisions = all_decisions

        if node_id is not None:
            if node_id not in nodes:
                raise ControlPlaneError("not_found", "node does not exist",
                                        node_id=node_id)
            keep = self._subtree(nodes, node_id)
            nodes = {key: value for key, value in nodes.items() if key in keep}
            workers = [worker for worker in workers if worker["node_id"] in nodes]
            candidates = [item for item in candidates if item["node_id"] in nodes]
            reviews = [item for item in reviews if item["node_id"] in nodes]
            decisions = [item for item in decisions if item["node_id"] in nodes]
        if candidate_id is not None:
            match = next((item for item in candidates
                          if item["candidate_id"] == candidate_id), None)
            if match is None:
                raise ControlPlaneError("not_found", "candidate does not exist",
                                        candidate_id=candidate_id)
            candidates = [match]
            reviews = [item for item in reviews
                       if item["candidate_id"] == candidate_id]
            target = nodes.get(match["node_id"])
            if target is not None:
                nodes = {target["node_id"]: target}
                decisions = [item for item in decisions
                             if item["node_id"] == target["node_id"]]
                workers = [worker for worker in workers
                           if worker["node_id"] == target["node_id"]]

        inbox: dict[str, list[dict[str, Any]]] = {}
        for worker in workers:
            page = self.api.list_inbox(actor, worker["worker_id"])
            inbox[worker["worker_id"]] = page["messages"]

        decomposition = []
        dependencies = []
        tree = []
        executions = []
        waiting = []
        workers_by_node: dict[str, list[dict[str, Any]]] = {}
        for worker in workers:
            workers_by_node.setdefault(worker["node_id"], []).append(worker)
            # Committed deliveries retain the epoch they were sent under.
            # Grouping them onto the worker's current epoch would relabel
            # history after a rebind, so each epoch is its own execution row.
            grouped: dict[int, list[dict[str, Any]]] = {}
            for message in inbox.get(worker["worker_id"], []):
                grouped.setdefault(int(message["epoch"]), []).append(message)
            current_epoch = int(worker["epoch"])
            for epoch in sorted(set(grouped) | {current_epoch}):
                current = epoch == current_epoch
                deliveries = [{
                    "message_id": message["message_id"],
                    "delivery_key": message["delivery_key"],
                    "epoch": epoch,
                    "delivery_state": message["delivery_state"],
                    "created_at": message["created_at"],
                    "delivered_at": message["delivered_at"],
                } for message in grouped.get(epoch, [])]
                executions.append({
                    "worker_id": worker["worker_id"],
                    "node_id": worker["node_id"],
                    "epoch": epoch,
                    "current": current,
                    "backend_ref": worker["backend_ref"],
                    "agent_ref": worker["agent_ref"],
                    "observed": worker["observed"] if current else None,
                    "desired": worker["desired"] if current else None,
                    "last_event_id": (worker["last_event_id"]
                                      if current else None),
                    "delivery_keys": [item["delivery_key"]
                                      for item in deliveries],
                    "deliveries": deliveries,
                    "usage": worker["usage"] if current else None,
                    "activities_complete": (worker["activities_complete"]
                                            if current else None),
                })
        for node in nodes.values():
            parent = node["parent_id"]
            if parent is not None:
                decomposition.append({
                    "parent_id": parent,
                    "child_id": node["node_id"],
                    "expansion_id": node["expansion_id"],
                    "local_child_id": node["local_child_id"],
                })
            for dependency in node["dependencies"] or []:
                dependencies.append({"node_id": node["node_id"],
                                     "depends_on": dependency})
            node_workers = workers_by_node.get(node["node_id"], [])
            current = max(node_workers, key=lambda item: item["epoch"], default=None)
            node_candidates = [item for item in candidates
                               if item["node_id"] == node["node_id"]]
            latest_candidate = node_candidates[-1] if node_candidates else None
            tree.append({
                "node_id": node["node_id"],
                "label": node["local_child_id"] or node["node_id"],
                "parent_id": parent,
                "expansion_id": node["expansion_id"],
                "local_child_id": node["local_child_id"],
                "template_ref": node["template_ref"],
                "reviewer_ref": node["reviewer_ref"],
                "state": node["state"],
                "round": node["round"],
                "dependencies": list(node["dependencies"] or []),
                "accepted_candidate_ref": node["accepted_candidate_ref"],
                "latest_candidate_id": (latest_candidate["candidate_id"]
                                        if latest_candidate else None),
                "current_execution": ({
                    "worker_id": current["worker_id"],
                    "epoch": current["epoch"],
                    "backend_ref": current["backend_ref"],
                    "observed": current["observed"],
                } if current else None),
                "revision": node["revision"],
                "updated_at": node["updated_at"],
            })

        # Waiting evidence uses the full transaction; filters only shape the view.
        for view_node_id in nodes:
            reason = self._waiting_reason(run, nodes[view_node_id], all_nodes,
                                          all_decisions, all_candidates)
            if reason is not None:
                waiting.append(reason)

        if node_id is not None:
            node_ids = set(nodes)
            decomposition = [item for item in decomposition
                             if item["child_id"] in node_ids]
            dependencies = [item for item in dependencies
                            if item["node_id"] in node_ids]
            executions = [item for item in executions
                          if item["node_id"] in node_ids]

        timeline_limit = min(TIMELINE_LIMIT, max(1, cursor.seq))
        timeline_start = max(0, cursor.seq - timeline_limit)
        try:
            page = self.api.read_events(actor, run_id, timeline_start,
                                        timeline_limit + 1)
        except ControlPlaneError as error:
            if error.code != "cursor_expired":
                raise
            page = self.api.read_events(actor, run_id, 0, timeline_limit + 1)
        timeline_events = [dict(item) for item in page["events"]
                           if item["seq"] <= cursor.seq][:timeline_limit]

        return {
            "format": TRACE_FORMAT,
            "service_id": self.service_id,
            "run": {
                "run_id": run["run_id"],
                "status": run["status"],
                "desired": run["desired"],
                "revision": run["revision"],
                "definition_ref": run["definition_ref"],
                "created_at": run["created_at"],
                "updated_at": run["updated_at"],
                "summary": run["summary"],
            },
            "cursor": {**cursor.as_dict(), "format": CURSOR_FORMAT,
                       "token": cursor.encode()},
            "filters": {"node_id": node_id, "candidate_id": candidate_id},
            "tree": tree,
            "decomposition": decomposition,
            "dependencies": dependencies,
            "workers": [{
                "worker_id": worker["worker_id"],
                "node_id": worker["node_id"],
                "epoch": worker["epoch"],
                "backend_ref": worker["backend_ref"],
                "observed": worker["observed"],
                "desired": worker["desired"],
                "session_ref": worker["session_ref"],
                "checkpoint_ref": worker["checkpoint_ref"],
                "usage": worker["usage"],
                "last_event_id": worker["last_event_id"],
            } for worker in workers],
            "executions": executions,
            "candidates": [{
                "candidate_id": item["candidate_id"],
                "node_id": item["node_id"],
                "worker_id": item["worker_id"],
                "epoch": item["epoch"],
                "round": item["round"],
                "decision": item["decision"],
                "event_id": item["event_id"],
                "decision_event_id": item["decision_event_id"],
                "output_refs": item["output_refs"],
                "created_at": item["created_at"],
            } for item in candidates],
            "reviews": [{
                "event_id": item["event_id"],
                "node_id": item["node_id"],
                "candidate_id": item["candidate_id"],
                "verdict": item["verdict"],
                "feedback_ref": item["feedback_ref"],
                "reviewer": item["reviewer"],
                "worker_id": item["worker_id"],
                "epoch": item["epoch"],
                "created_at": item["created_at"],
            } for item in reviews],
            "decisions": [{
                "decision_id": item["decision_id"],
                "node_id": item["node_id"],
                "parent_id": item["parent_id"],
                "worker_id": item["worker_id"],
                "epoch": item["epoch"],
                "reason": item["reason"],
                "question": item["question"],
                "state": item["state"],
                "resolved_by": item["resolved_by"],
                "request_event_id": item["request_event_id"],
                "resolved_event_id": item["resolved_event_id"],
                "created_at": item["created_at"],
            } for item in decisions],
            "waiting": waiting,
            "timeline": {
                "events": timeline_events,
                "cursor": {**cursor.as_dict(), "token": cursor.encode()},
                "next_cursor": (timeline_events[-1]["seq"]
                                if timeline_events else timeline_start),
                "oldest_seq": (timeline_events[0]["seq"]
                               if timeline_events else None),
                "has_earlier": bool(timeline_events
                                    and timeline_events[0]["seq"] > 1),
                "limit": timeline_limit,
                "truncated": cursor.seq - timeline_start > timeline_limit,
            },
            "availability": {
                "control_events": "committed",
                "execution_logs": "backend-owned",
                "request_diagnostics": ("configured" if self.diagnostics is not None
                                        else "unconfigured"),
            },
        }

    @staticmethod
    def _subtree(nodes: Mapping[str, Mapping[str, Any]], root: str) -> set[str]:
        keep = {root}
        changed = True
        while changed:
            changed = False
            for key, node in nodes.items():
                if key not in keep and node["parent_id"] in keep:
                    keep.add(key)
                    changed = True
        return keep

    @staticmethod
    def _waiting_reason(run: Mapping[str, Any], node: Mapping[str, Any],
                        nodes: Mapping[str, Mapping[str, Any]],
                        decisions: list[Mapping[str, Any]],
                        candidates: list[Mapping[str, Any]]) -> dict[str, Any] | None:
        state = node["state"]
        base = {"node_id": node["node_id"], "state": state}
        if run["status"] == "paused":
            return {**base, "reason": "run_paused",
                    "evidence": {"run_status": run["status"],
                                 "run_desired": run["desired"]}}
        if state in ("succeeded", "failed", "cancelled"):
            return None
        if state == "waiting_decision":
            open_decision = next(
                (item for item in reversed(decisions)
                 if item["node_id"] == node["node_id"] and item["state"] == "open"),
                None)
            return {**base, "reason": "awaiting_parent_decision",
                    "evidence": ({"decision_id": open_decision["decision_id"],
                                  "parent_id": open_decision["parent_id"],
                                  "question": open_decision["question"]}
                                 if open_decision else {"decision": "not_found"})}
        if state == "waiting_children":
            children = [item for key, item in nodes.items()
                        if item["parent_id"] == node["node_id"]]
            open_children = [{"node_id": item["node_id"], "state": item["state"]}
                             for item in children
                             if item["state"] not in ("succeeded", "cancelled")]
            return {**base, "reason": "awaiting_children",
                    "evidence": {"open_children": open_children}}
        if state == "awaiting_review":
            pending = next(
                (item for item in reversed(candidates)
                 if item["node_id"] == node["node_id"]
                 and item["decision"] is None
                 and item["round"] == node["round"]), None)
            return {**base, "reason": "awaiting_review",
                    "evidence": ({"candidate_id": pending["candidate_id"],
                                  "reviewer_ref": node["reviewer_ref"],
                                  "round": pending["round"]}
                                 if pending else {"candidate": "not_found"})}
        if state == "pending":
            unmet = [{"node_id": dependency,
                      "state": (nodes[dependency]["state"]
                                if dependency in nodes else "missing")}
                     for dependency in (node["dependencies"] or [])
                     if dependency not in nodes
                     or nodes[dependency]["state"] != "succeeded"]
            if unmet:
                return {**base, "reason": "dependencies_unmet",
                        "evidence": {"dependencies": unmet}}
            return {**base, "reason": "awaiting_schedule",
                    "evidence": {"node_revision": node["revision"]}}
        if state == "ready":
            return {**base, "reason": "awaiting_dispatch",
                    "evidence": {"node_revision": node["revision"]}}
        if state == "blocked":
            return {**base, "reason": "blocked",
                    "evidence": {"node_revision": node["revision"]}}
        return {**base, "reason": "execution_in_progress",
                "evidence": {"node_revision": node["revision"]}}

    # ------------------------------------------------------------------
    # events

    def events(self, actor: Actor, run_id: str, *,
               after: str | Mapping[str, Any] | None = None,
               limit: int = 100) -> dict[str, Any]:
        """Read committed events after a scoped cursor, for polling or SSE.

        Delegate event sequence/read authorization to the existing Api. The
        HTTP generator handles heartbeat/backpressure/disconnection; it never
        advances a cursor past unconsumed durable events.
        """
        require_text(run_id, "run_id", max_length=128)
        require_int(limit, "limit", minimum=1)
        require(limit <= MAX_LIMIT, "invalid_argument", "limit must be <= 1000")
        cursor = self._control_cursor(run_id, after)
        page = self.api.read_events(actor, run_id, cursor.seq, limit)
        next_cursor = ControlCursor(self.service_id, run_id, page["next_cursor"])
        return {
            "format": EVENTS_FORMAT,
            "service_id": self.service_id,
            "run_id": run_id,
            "events": page["events"],
            "cursor": {**cursor.as_dict(), "token": cursor.encode()},
            "next_cursor": {**next_cursor.as_dict(),
                            "token": next_cursor.encode()},
            "latest_seq": page["latest_seq"],
            "snapshot_revision": page["snapshot_revision"],
            "has_more": page["next_cursor"] < page["latest_seq"],
            "limit": limit,
        }

    # ------------------------------------------------------------------
    # execution details

    def execution(self, actor: Actor, run_id: str, worker_id: str,
                  epoch: int, delivery_key: str, *,
                  after: str | Mapping[str, Any] | None = None,
                  limit: int = 100) -> dict[str, Any]:
        """Authorized execution detail, identified independently of control seq.

        Preserve run/worker/epoch/delivery and source on every record. Select a
        Backend from committed binding, never a caller-supplied backend/path.
        Old epochs may be read explicitly if retained; never combine their logs
        with a current execution. Do not expose prompts or model reasoning.
        """
        require_text(run_id, "run_id", max_length=128)
        require_text(worker_id, "worker_id", max_length=128)
        require_int(epoch, "epoch")
        require(isinstance(delivery_key, str) and
                SAFE_DELIVERY_KEY.match(delivery_key) is not None,
                "invalid_argument", "delivery_key must be a safe identifier")
        require_int(limit, "limit", minimum=1)
        require(limit <= EXECUTION_LIMIT, "invalid_argument",
                f"limit must be <= {EXECUTION_LIMIT}")

        worker = self.api.get_resource(actor, "worker", worker_id)
        if worker["run_id"] != run_id:
            raise ControlPlaneError("not_found",
                                    "worker does not belong to this run",
                                    worker_id=worker_id, run_id=run_id)
        inbox = self.api.list_inbox(actor, worker_id)
        message = next((item for item in inbox["messages"]
                        if item["delivery_key"] == delivery_key), None)
        if message is None:
            raise ControlPlaneError(
                "not_found", "delivery is not committed for this worker",
                worker_id=worker_id, delivery_key=delivery_key)
        if int(message["epoch"]) != epoch:
            raise ControlPlaneError(
                "not_found", "delivery does not belong to this epoch",
                worker_id=worker_id, delivery_key=delivery_key, epoch=epoch)

        backend_ref = worker["backend_ref"]
        backend = self.backends.get(backend_ref)
        source = str(getattr(backend, "execution_log_kind", "unsupported")
                     if backend is not None else "unsupported")
        offset, cursor_snapshot = self._execution_cursor(
            after, run_id=run_id, worker_id=worker_id, epoch=epoch,
            delivery_key=delivery_key, source=source)

        identity = {"service_id": self.service_id, "run_id": run_id,
                    "worker_id": worker_id, "epoch": epoch,
                    "delivery_key": delivery_key, "node_id": worker["node_id"],
                    "backend_ref": backend_ref, "source": source}
        if backend is None:
            result: dict[str, Any] = {
                "availability": "unsupported",
                "entries": (),
                "next_offset": offset,
                "has_more": False,
                "partial": False,
                "detail": {"reason": "backend has no execution log adapter",
                           "backend_ref": backend_ref},
            }
        else:
            try:
                observed = backend.execution_log(
                    run_id=run_id, worker_id=worker_id, epoch=epoch,
                    delivery_key=delivery_key, after=offset, limit=limit)
                result = observed.as_dict()
            except ControlPlaneError:
                raise
            except Exception as error:  # Log failure is not a business verdict.
                result = {
                    "availability": "error",
                    "entries": (),
                    "next_offset": offset,
                    "has_more": False,
                    "partial": False,
                    "detail": {"code": getattr(error, "code",
                                               "execution_log_error"),
                               "message": str(error)[:1024]},
                }
        snapshot = result.get("snapshot")
        if (cursor_snapshot is not None and snapshot is not None
                and cursor_snapshot != snapshot):
            raise ControlPlaneError(
                "cursor_expired",
                "execution log source changed since this cursor was issued; "
                "read the execution again from the start",
                instruction="resnapshot",
                cursor_snapshot=cursor_snapshot, snapshot=snapshot)
        entries = [dict(entry, **identity) for entry in result["entries"]]
        cursor = self._execution_cursor_dict(
            run_id=run_id, worker_id=worker_id, epoch=epoch,
            delivery_key=delivery_key, source=source, offset=offset,
            snapshot=snapshot)
        next_cursor = self._execution_cursor_dict(
            run_id=run_id, worker_id=worker_id, epoch=epoch,
            delivery_key=delivery_key, source=source,
            offset=int(result["next_offset"]), snapshot=snapshot)
        return {
            "format": EXECUTION_TRACE_FORMAT,
            **identity,
            "delivery": {
                "message_id": message["message_id"],
                "delivery_key": delivery_key,
                "delivery_state": message["delivery_state"],
                "created_at": message["created_at"],
                "delivered_at": message["delivered_at"],
                "receipt": message["receipt"],
            },
            "availability": result["availability"],
            "entries": entries,
            "snapshot": snapshot,
            "partial": bool(result.get("partial")),
            "has_more": bool(result.get("has_more")),
            "detail": result.get("detail") or {},
            "cursor": {**cursor, "format": EXECUTION_CURSOR_FORMAT,
                       "token": json.dumps(cursor, sort_keys=True,
                                           separators=(",", ":"))},
            "next_cursor": {**next_cursor, "format": EXECUTION_CURSOR_FORMAT,
                            "token": json.dumps(next_cursor, sort_keys=True,
                                                separators=(",", ":"))},
            "limit": limit,
        }

    # ------------------------------------------------------------------
    # request diagnostics

    def requests(self, actor: Actor, *, request_id: str | None = None,
                 run_id: str | None = None, after: Any = 0,
                 limit: int = 100) -> dict[str, Any]:
        """Bounded diagnostic request records, including uncommitted failures.

        Read only the service's configured diagnostic log. Exclude credentials,
        arbitrary request bodies and hidden model text. These are diagnostics,
        not task completion facts, and cannot mutate run status.
        """
        require(not actor.roles.isdisjoint({"publisher", "scheduler"}),
                "unauthorized",
                "request diagnostics require publisher or scheduler")
        if run_id is not None:
            require_text(run_id, "run_id", max_length=128)
            actor.require_read(run_id)
        if request_id is not None:
            require_text(request_id, "request_id", max_length=128)
        require_int(limit, "limit", minimum=1)
        require(limit <= MAX_LIMIT, "invalid_argument", "limit must be <= 1000")
        if self.diagnostics is None:
            cursor = {"offset": 0, "generation": None}
            cursor["token"] = json.dumps(cursor, sort_keys=True,
                                         separators=(",", ":"))
            return {
                "format": REQUESTS_FORMAT,
                "service_id": self.service_id,
                "records": [],
                "cursor": cursor,
                "next_cursor": dict(cursor),
                "has_more": False,
                "partial": False,
                "malformed": 0,
                "limit": limit,
                "availability": "unconfigured",
            }
        return self.diagnostics.read(request_id=request_id, run_id=run_id,
                                     after=after, limit=limit)
