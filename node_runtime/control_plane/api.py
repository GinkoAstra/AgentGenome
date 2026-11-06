"""Unified application API for graph runs, facts, plans and reports.

There is exactly one write path: every state change is validated here and
committed in one SQLite transaction together with its event and idempotency
receipt. Transport adapters (CLI, loopback HTTP, MCP) are expected to call this
object, never to duplicate state logic.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .artifacts import ArtifactStore
from .execution_facts import (execution_stopped, merge_activity_snapshot,
                              fold_progress_evaluations,
                              validate_acceptance_revision,
                              validate_progress_evaluation)
from .worker_specs import compose_worker_spec
from .decisions import decision_reply_key
from . import lifecycle as L
from . import resources as R
from .storage import Store, Tx, dump, ensure_directory, load, new_id, now
from .backends.base import Capabilities
from .task_definitions import (DefinitionBundle, compile_definition_bundle,
                               initial_graph_tasks, validate_contract)

RUN_TERMINAL = frozenset({"completed", "failed", "cancelled"})
NODE_TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
PLAN_WORKER_FIELDS = frozenset({"intent_seq", "desired", "epoch"})
ARTIFACT_KEYS = frozenset({"id", "sha256", "size", "media_type"})
# Observed states that prove the previous execution entity has ended. The
# epoch may only advance from one of these *and* with a matching observation
# fact; epoch directories are independent, so an `active`/`starting`/`unknown`
# execution could otherwise keep running beside its replacement.
EPOCH_TERMINAL_OBSERVED = frozenset({"idle", "stopped", "failed"})
USAGE_KEYS = frozenset({"provider_calls", "tool_calls"})
# System-derived progress reassessments own this namespace; ordinary Worker
# decisions may not reserve a future trigger id through it.
PROGRESS_DECISION_PREFIX = "progress-stalled:"
ACCEPTANCE_REVISION_FORMAT = "agentgenome-acceptance-revision/1"
ACCEPTANCE_REQUESTED = "acceptance_revision_requested"
ACCEPTANCE_APPLIED = "acceptance_revision_applied"
MESSAGE_SUPERSEDED = "message_superseded"
REVISION_FIELDS = ("contract_ref", "previous_ref", "upper_contract_ref",
                   "origin", "criteria_ref", "authority_ref")


def _activity_fingerprint(payload: Mapping[str, Any]) -> str:
    records = payload.get("activities")
    if not isinstance(records, list):
        records = []
    ordered = sorted(records, key=lambda record: record.get("activity_id", ""))
    return R.canonical({"activities": ordered,
                        "activities_complete": payload.get("activities_complete") is True})


class Api:
    def __init__(self, data_dir: str | Path,
                 fault_hook: Any = None, *,
                 backend_capabilities: Mapping[str, Capabilities] | None = None) -> None:
        # Trusted composition-root configuration, never supplied by a model
        # in a plan. An API used only for reads/facts needs no adapter registry.
        self.backend_capabilities = dict(backend_capabilities or {})
        self.root = ensure_directory(data_dir)
        self.store = Store(self.root / "state.sqlite3", fault_hook=fault_hook)
        self.artifacts = ArtifactStore(self.root / "artifacts")

    def acceptance_status(self, actor: R.Actor, run_id: str,
                          node_id: str) -> dict[str, Any]:
        """W05b resolve frozen upper/current version and ordered revision history.

        Use existing Task/Artifact/events/applications, not another store.
        Runtime descriptors require this Node/Run's verified revision source;
        JSON shape or artifact ownership alone does not authorize a version.
        Report desired contract and actual execution contract separately.
        """
        with self.store.read() as tx:
            run = self._visible_run(tx, actor, run_id)
            node = self._node(tx, node_id, run_id)
            resolved = self._resolve_acceptance_contract(tx, actor, run, node)
            history = self._acceptance_history(tx, node)
            if run["status"] in RUN_TERMINAL:
                # Terminal projection: an unapplied request stays in history
                # with an explicit state and is never reported as live pending.
                history = [{**item, "state": "not_applicable"}
                           if item["state"] == "pending" else item
                           for item in history]
            pending = next((item for item in history
                            if item["state"] == "pending"), None)
            worker = tx.one(
                "SELECT * FROM workers WHERE node_id = ? ORDER BY created_at LIMIT 1",
                (node["node_id"],))
            execution = None
            if worker is not None:
                context = load(worker["context_json"]) or {}
                execution = {
                    "worker_id": worker["worker_id"],
                    "epoch": worker["epoch"],
                    "desired": worker["desired"],
                    "observed": worker["observed"],
                    "contract_ref": context.get("contract_ref"),
                    "matches_current": context.get("contract_ref")
                    == node["contract_ref"],
                }
            return {
                "node_id": node["node_id"],
                "run_id": node["run_id"],
                "revision": node["revision"],
                "state": node["state"],
                "upper": {
                    "contract_ref": resolved["upper_contract_ref"],
                    "contract": dict(resolved["upper"]),
                },
                "current": {
                    "contract_ref": node["contract_ref"],
                    "contract": dict(resolved["contract"]),
                    "origin": (resolved["descriptor"]["origin"]
                               if resolved["descriptor"] else "frozen"),
                    "previous_ref": (resolved["descriptor"]["previous_ref"]
                                     if resolved["descriptor"] else None),
                    "authority_ref": (resolved["descriptor"]["authority_ref"]
                                      if resolved["descriptor"] else None),
                    "revision_id": (resolved["descriptor"]["contract_ref"]
                                    if resolved["descriptor"] else None),
                    "criteria_ref": (dict(resolved["descriptor"]["criteria_ref"])
                                     if resolved["descriptor"] else None),
                    "source_ref": (dict(resolved["source_ref"])
                                   if resolved["source_ref"] else None),
                },
                "history": history,
                "pending_revision": pending,
                "execution": execution,
            }

    @staticmethod
    def _revision_descriptor(payload: Mapping[str, Any]) -> dict[str, Any]:
        return {name: payload.get(name) for name in REVISION_FIELDS}

    def _acceptance_history(self, tx: Tx, node: Any) -> list[dict[str, Any]]:
        """Ordered revision requests with their application state.

        Identity comes from committed Api events, not from artifact ownership
        or a well-shaped JSON document supplied by a caller.
        """
        requested = tx.all(
            "SELECT * FROM events WHERE run_id = ? AND node_id = ?"
            " AND kind = ? ORDER BY seq", (node["run_id"], node["node_id"],
                                           ACCEPTANCE_REQUESTED))
        applied = tx.all(
            "SELECT * FROM events WHERE run_id = ? AND node_id = ?"
            " AND kind = ? ORDER BY seq", (node["run_id"], node["node_id"],
                                           ACCEPTANCE_APPLIED))
        applied_by_source: dict[str, Any] = {}
        applied_by_contract: dict[str, Any] = {}
        for row in applied:
            payload = load(row["payload_json"]) or {}
            source_event_id = payload.get("source_event_id")
            if isinstance(source_event_id, str):
                applied_by_source[source_event_id] = row
            contract_ref = payload.get("contract_ref")
            if isinstance(contract_ref, str):
                applied_by_contract[contract_ref] = row
        history: list[dict[str, Any]] = []
        for row in requested:
            payload = load(row["payload_json"]) or {}
            descriptor = validate_acceptance_revision(
                self._revision_descriptor(payload)).as_dict()
            applied_row = (applied_by_source.get(row["event_id"])
                           or applied_by_contract.get(descriptor["contract_ref"]))
            if applied_row is not None:
                state = "applied"
            elif node["state"] in NODE_TERMINAL:
                # A cancelled/failed Node keeps the request as history, but no
                # unapplied version can become active work again.
                state = "not_applicable"
            elif node["contract_ref"] == descriptor["previous_ref"]:
                state = "pending"
            else:
                state = "not_applicable"
            history.append({
                **descriptor,
                "state": state,
                "revision_id": descriptor["contract_ref"],
                "source_event_id": row["event_id"],
                "requested_seq": row["seq"],
                "applied_event_id": (applied_row["event_id"]
                                     if applied_row is not None else None),
                "applied_seq": (applied_row["seq"]
                                if applied_row is not None else None),
                "authority": payload.get("authority"),
            })
        return history

    def _read_canonical_artifact(self, tx: Tx, actor: R.Actor,
                                 ref: Any, label: str, *,
                                 canonical: bool = True) -> dict[str, Any]:
        checked = R.artifact_ref(ref, label)
        row = self._artifact_row(tx, checked)
        if not self._artifact_visible(tx, actor, row):
            R.fail("not_found", f"{label} is not visible to this actor",
                   id=checked["id"])
        data = self.artifacts.read(checked)
        try:
            document = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            R.fail("artifact_mismatch", f"{label} is not UTF-8 JSON",
                   id=checked["id"])
        R.require(isinstance(document, dict), "invalid_argument",
                  f"{label} must be a JSON object", id=checked["id"])
        if canonical:
            R.require(R.canonical(document).encode("utf-8") == data,
                      "artifact_mismatch", f"{label} is not canonical JSON",
                      id=checked["id"])
        return document

    def _read_revision_source(self, tx: Tx, actor: R.Actor,
                              descriptor: Mapping[str, Any],
                              source_ref: Any) -> None:
        """Verify that an applied version still has its immutable source."""
        document = self._read_canonical_artifact(
            tx, actor, source_ref, "acceptance revision source")
        R.require(document.get("format") == ACCEPTANCE_REVISION_FORMAT,
                  "artifact_mismatch",
                  "the revision source format is unsupported")
        for name in ("previous_ref", "upper_contract_ref", "origin",
                     "authority_ref"):
            R.require(document.get(name) == descriptor.get(name),
                      "artifact_mismatch",
                      "the revision source disagrees with its descriptor",
                      field=name)
        R.require(R.canonical(document.get("criteria_ref"))
                  == R.canonical(descriptor.get("criteria_ref")),
                  "artifact_mismatch",
                  "the revision source names different criteria")
        R.require(R.digest(document) == descriptor["contract_ref"],
                  "artifact_mismatch",
                  "the revision source digest does not match the version",
                  expected=descriptor["contract_ref"])

    def _criteria_contract(self, tx: Tx, actor: R.Actor,
                           ref: Any) -> dict[str, Any]:
        """Criteria are ordinary JSON: only structure and content are checked.

        A Worker may publish pretty-printed or differently ordered JSON; the
        immutable revision source written by the Api stays canonical, but the
        caller's source document does not have to be.
        """
        document = self._read_canonical_artifact(tx, actor, ref,
                                                 "acceptance criteria",
                                                 canonical=False)
        return validate_contract(document)

    def _resolve_acceptance_contract(self, tx: Tx, actor: R.Actor, run: Any,
                                     node: Any) -> dict[str, Any]:
        """The immutable upper and the verified current local contract.

        A Node whose ``contract_ref`` is neither its frozen Task reference nor
        backed by a committed applied revision is corrupt: a bare content ref
        is never treated as an authorized version.
        """
        bundle = self._definition_bundle_for_run(tx, actor, run)
        task = bundle.tasks.get(node["template_ref"])
        if task is None:
            R.fail("not_found", "the node's task definition is not in the run library",
                   task_ref=node["template_ref"], node_id=node["node_id"])
        upper = dict(task.contract)
        if node["contract_ref"] == node["template_ref"]:
            return {"bundle": bundle, "task": task, "upper": upper,
                    "upper_contract_ref": node["template_ref"],
                    "contract": upper, "descriptor": None,
                    "source_ref": None}
        history = self._acceptance_history(tx, node)
        entry = next((item for item in history
                      if item["state"] == "applied"
                      and item["contract_ref"] == node["contract_ref"]), None)
        if entry is None:
            R.fail("artifact_mismatch",
                   "the node contract has no authorized applied revision",
                   node_id=node["node_id"], contract_ref=node["contract_ref"])
        descriptor = validate_acceptance_revision(
            {name: entry[name] for name in REVISION_FIELDS}).as_dict()
        R.require(descriptor["upper_contract_ref"] == node["template_ref"],
                  "artifact_mismatch",
                  "the local contract names a different frozen upper version",
                  upper_contract_ref=descriptor["upper_contract_ref"],
                  template_ref=node["template_ref"])
        event = tx.one("SELECT * FROM events WHERE event_id = ?",
                       (entry["applied_event_id"],))
        payload = load(event["payload_json"]) if event is not None else {}
        self._read_revision_source(tx, actor, descriptor,
                                   payload.get("source_ref"))
        contract = self._criteria_contract(tx, actor, descriptor["criteria_ref"])
        return {"bundle": bundle, "task": task, "upper": upper,
                "upper_contract_ref": node["template_ref"],
                "contract": contract, "descriptor": descriptor,
                "source_ref": payload.get("source_ref")}

    @staticmethod
    def _worker_tool_grant(worker: Any, name: str, version: str,
                           scope: str) -> bool:
        profile = load(worker["tool_profile_json"]) or {}
        raw = profile.get("tools")
        if isinstance(raw, Mapping):
            raw = list(raw.values())
        for entry in raw or []:
            if isinstance(entry, str) and entry == name:
                return True
            if not isinstance(entry, Mapping) or entry.get("name") != name:
                continue
            if str(entry.get("version", version)) != version:
                continue
            if str(entry.get("scope", scope)) != scope:
                continue
            return True
        return False

    def _append_acceptance_revision(self, tx: Tx, actor: R.Actor, run: Any,
                                    item: dict[str, Any], request_id: str,
                                    expected_revision: int) -> dict[str, Any]:
        """W05b direct-parent proposal; authority/origin/upper are API-derived.

        Validate effective tool grant, actual parent binding/epoch, current
        contract and no undecided candidate; validate and register immutable
        criteria/revision metadata with one existing fact transaction. Bump
        target revision; Scheduler later applies version and execution intent.
        """
        actor.require_role("worker")
        if actor.worker_id is None or actor.epoch is None:
            R.fail("unauthorized",
                   "a worker identity is required to propose a local contract")
        target = self._node(tx, item["target_node_id"], run["run_id"])
        worker = self._worker(tx, actor.worker_id, run["run_id"])
        if actor.epoch != worker["epoch"]:
            R.fail("stale_epoch", "parent epoch is not current",
                   current_epoch=worker["epoch"])
        if worker["desired"] == "stopped":
            R.fail("unauthorized", "the parent execution is stopping")
        if target["parent_id"] is None or target["parent_id"] != worker["node_id"]:
            R.fail("unauthorized",
                   "only the direct parent execution may revise acceptance",
                   target_node_id=target["node_id"],
                   parent_id=target["parent_id"])
        if target["closure_json"] is not None:
            R.fail("unauthorized",
                   "an internal review Node has no local business contract",
                   target_node_id=target["node_id"])
        if not self._worker_tool_grant(worker, "node.revise_acceptance", "1",
                                       "acceptance:children"):
            R.fail("unauthorized",
                   "the parent execution was not granted acceptance revision",
                   worker_id=worker["worker_id"])
        self._check_revision(target, expected_revision, "node", target["node_id"])
        if (target["state"] in ("succeeded", "failed", "cancelled")
                or target["accepted_candidate_ref"] is not None):
            R.fail("candidate_mismatch",
                   "a terminal or accepted node cannot revise acceptance",
                   node_id=target["node_id"], state=target["state"])
        if target["state"] not in ("running", "awaiting_review"):
            R.fail("candidate_mismatch",
                   "the target node has no active business round",
                   node_id=target["node_id"], state=target["state"])
        pending = self._current_undecided_candidate(tx, target)
        if pending is not None:
            R.fail("candidate_mismatch",
                   "an undecided candidate owns the node's review path",
                   candidate_id=pending["candidate_id"])
        history = self._acceptance_history(tx, target)
        open_request = next((entry for entry in history
                             if entry["state"] == "pending"), None)
        if open_request is not None:
            if (open_request["previous_ref"] == item["previous_ref"]
                    and R.canonical(open_request["criteria_ref"])
                    == R.canonical(item["criteria_ref"])):
                event = tx.one("SELECT * FROM events WHERE event_id = ?",
                               (open_request["source_event_id"],))
                return {"target_node_id": target["node_id"],
                        "contract_ref": open_request["contract_ref"],
                        "previous_ref": open_request["previous_ref"],
                        "upper_contract_ref": open_request["upper_contract_ref"],
                        "revision_id": open_request["revision_id"],
                        "event_id": open_request["source_event_id"],
                        "seq": event["seq"] if event is not None else None,
                        "revision": target["revision"], "state": "requested",
                        "applied": False, "duplicate": True}
            R.fail("revision_conflict",
                   "the node already has an unapplied acceptance revision",
                   node_id=target["node_id"],
                   source_event_id=open_request["source_event_id"])
        if item["previous_ref"] != target["contract_ref"]:
            R.fail("revision_conflict",
                   "the proposal targets a different current contract",
                   expected_previous=target["contract_ref"],
                   requested_previous=item["previous_ref"])
        execution = tx.one(
            "SELECT * FROM workers WHERE node_id = ? ORDER BY created_at LIMIT 1",
            (target["node_id"],))
        if execution is not None:
            context = load(execution["context_json"]) or {}
            if context.get("contract_ref") != target["contract_ref"]:
                # A finished application is not yet a rebound execution; a new
                # proposal would target a contract no running Agent holds.
                R.fail("candidate_mismatch",
                       "the target execution has not been rebound to the current contract",
                       node_id=target["node_id"],
                       execution_contract_ref=context.get("contract_ref"),
                       contract_ref=target["contract_ref"])
        resolved = self._resolve_acceptance_contract(tx, actor, run, target)
        task = resolved["task"]
        R.require(task.completion_kind == "candidate", "unauthorized",
                  "a review_receipt task has no local business contract",
                  task_ref=task.task_ref)
        upper = resolved["upper"]
        inputs = load(target["input_refs_json"])
        R.require(inputs is not None, "candidate_mismatch",
                  "the node inputs are not frozen yet",
                  node_id=target["node_id"])
        self._check_refs(tx, actor, [item["criteria_ref"]])
        contract = self._criteria_contract(tx, actor, item["criteria_ref"])
        R.validate_schema_value(upper["input_schema"], inputs,
                                "input_refs")
        R.validate_schema_value(contract["input_schema"], inputs,
                                "input_refs")
        parent = self._node(tx, target["parent_id"], run["run_id"])
        source = {
            "format": ACCEPTANCE_REVISION_FORMAT,
            "previous_ref": target["contract_ref"],
            "upper_contract_ref": target["template_ref"],
            "origin": "runtime_derived",
            "criteria_ref": dict(item["criteria_ref"]),
            "authority_ref": parent["template_ref"],
        }
        data = R.canonical(source).encode("utf-8")
        source_ref = self.artifacts.put(data, "application/json")
        if source_ref["id"] != R.digest(source):
            R.fail("artifact_mismatch",
                   "the revision source content does not match its digest")
        tx.execute(
            "INSERT OR IGNORE INTO artifacts (id, sha256, size, media_type,"
            " created_by, created_at) VALUES (?, ?, ?, 'application/json', ?, ?)",
            (source_ref["id"], source_ref["sha256"], source_ref["size"],
             actor.actor_id, now()),
        )
        descriptor = validate_acceptance_revision({
            "contract_ref": source_ref["id"],
            "previous_ref": source["previous_ref"],
            "upper_contract_ref": source["upper_contract_ref"],
            "origin": source["origin"],
            "criteria_ref": source["criteria_ref"],
            "authority_ref": source["authority_ref"],
        }).as_dict()
        authority = {
            "parent_node_id": parent["node_id"],
            "parent_worker_id": worker["worker_id"],
            "parent_epoch": worker["epoch"],
            "task_ref": parent["template_ref"],
        }
        event_id, seq = tx.append_event(
            run_id=run["run_id"], kind=ACCEPTANCE_REQUESTED,
            node_id=target["node_id"], worker_id=worker["worker_id"],
            epoch=worker["epoch"], actor_id=actor.actor_id,
            request_id=request_id,
            payload={**descriptor, "target_node_id": target["node_id"],
                     "source_ref": source_ref, "authority": authority,
                     "revision_id": descriptor["contract_ref"]},
        )
        new_revision = target["revision"] + 1
        tx.execute(
            "UPDATE nodes SET revision = ?, updated_at = ? WHERE node_id = ?",
            (new_revision, now(), target["node_id"]),
        )
        return {"target_node_id": target["node_id"],
                "contract_ref": descriptor["contract_ref"],
                "previous_ref": descriptor["previous_ref"],
                "upper_contract_ref": descriptor["upper_contract_ref"],
                "revision_id": descriptor["contract_ref"],
                "event_id": event_id, "seq": seq, "revision": new_revision,
                "state": "requested", "applied": False, "duplicate": False}

    def _plan_apply_acceptance_revision(self, tx: Tx, actor: R.Actor, run: Any,
                                        source_event_id: str,
                                        request_id: str) -> dict[str, Any]:
        """W05b apply one verified proposal and stop the superseded execution.

        Expected previous version, Run/Node state and existing parent reply
        receipts remain guards. Atomically update Node contract, application
        marker and old Worker stop intent. Never edit its live frozen context.
        """
        event = tx.one("SELECT * FROM events WHERE event_id = ?",
                       (source_event_id,))
        if (event is None or event["run_id"] != run["run_id"]
                or event["kind"] != ACCEPTANCE_REQUESTED):
            R.fail("not_found",
                   "acceptance revision request does not exist in this run",
                   source_event_id=source_event_id)
        payload = load(event["payload_json"]) or {}
        descriptor = validate_acceptance_revision(
            self._revision_descriptor(payload)).as_dict()
        target = self._node(tx, payload["target_node_id"], run["run_id"])
        applied = None
        for row in tx.all(
                "SELECT * FROM events WHERE run_id = ? AND node_id = ?"
                " AND kind = ? ORDER BY seq",
                (run["run_id"], target["node_id"], ACCEPTANCE_APPLIED)):
            applied_payload = load(row["payload_json"]) or {}
            if applied_payload.get("source_event_id") == source_event_id:
                applied = row
                break
        if applied is not None:
            return {"target_node_id": target["node_id"],
                    "contract_ref": target["contract_ref"],
                    "previous_ref": descriptor["previous_ref"],
                    "source_event_id": source_event_id,
                    "applied_event_id": applied["event_id"],
                    "node": self._node_view(target), "duplicate": True}
        if run["desired"] != "running":
            R.fail("invalid_argument",
                   "the run intent does not allow applying a local contract",
                   desired=run["desired"])
        self._require_open_run(run)
        if target["contract_ref"] != descriptor["previous_ref"]:
            R.fail("revision_conflict",
                   "the node contract changed before the revision applied",
                   current_contract_ref=target["contract_ref"],
                   expected_previous=descriptor["previous_ref"])
        if descriptor["upper_contract_ref"] != target["template_ref"]:
            R.fail("candidate_mismatch",
                   "the revision names a different frozen upper version")
        if (target["state"] in ("succeeded", "failed", "cancelled")
                or target["accepted_candidate_ref"] is not None):
            R.fail("candidate_mismatch",
                   "a terminal or accepted node cannot apply a revision",
                   node_id=target["node_id"], state=target["state"])
        if target["state"] != "running":
            R.fail("candidate_mismatch",
                   "the revision waits until the current review is applied",
                   node_id=target["node_id"], state=target["state"])
        pending = self._current_undecided_candidate(tx, target)
        if pending is not None:
            R.fail("candidate_mismatch",
                   "an undecided candidate owns the node's review path",
                   candidate_id=pending["candidate_id"])
        open_decision = tx.one(
            "SELECT decision_id FROM decision_requests WHERE run_id = ?"
            " AND node_id = ? AND state = 'open' LIMIT 1",
            (run["run_id"], target["node_id"]))
        if open_decision is not None:
            R.fail("candidate_mismatch",
                   "an open parent decision must be answered first",
                   decision_id=open_decision["decision_id"])
        worker = tx.one(
            "SELECT * FROM workers WHERE node_id = ? ORDER BY created_at LIMIT 1",
            (target["node_id"],))
        if worker is not None:
            replies = tx.all(
                "SELECT decision_id, worker_id, epoch FROM decision_requests"
                " WHERE run_id = ? AND node_id = ? AND state = 'resolved'",
                (run["run_id"], target["node_id"]))
            # Consumption belongs to the execution that asked the question, not
            # to whichever epoch is current now. A later revision must not wait
            # for an old reply the replaced execution already consumed.
            for reply in replies:
                consumed = tx.one(
                    "SELECT 1 FROM messages WHERE worker_id = ? AND epoch = ?"
                    " AND delivery_key = ? AND delivery_state = 'delivered'",
                    (reply["worker_id"], reply["epoch"],
                     decision_reply_key(reply["decision_id"])))
                if consumed is None:
                    R.fail("candidate_mismatch",
                           "a parent decision reply has not been consumed",
                           decision_id=reply["decision_id"])
        event_id, seq = tx.append_event(
            run_id=run["run_id"], kind=ACCEPTANCE_APPLIED,
            node_id=target["node_id"], actor_id=actor.actor_id,
            request_id=request_id,
            payload={**descriptor, "target_node_id": target["node_id"],
                     "source_event_id": source_event_id,
                     "source_ref": payload.get("source_ref"),
                     "authority": payload.get("authority"),
                     "revision_id": descriptor["contract_ref"]},
        )
        node_revision = target["revision"] + 1
        tx.execute(
            "UPDATE nodes SET contract_ref = ?, revision = ?, updated_at = ?"
            " WHERE node_id = ?",
            (descriptor["contract_ref"], node_revision, now(),
             target["node_id"]),
        )
        stopped = None
        if worker is not None and worker["desired"] != "stopped":
            stopped = {"worker_id": worker["worker_id"],
                       "epoch": worker["epoch"],
                       "intent_seq": worker["intent_seq"] + 1}
            tx.execute(
                "UPDATE workers SET desired = 'stopped', intent_seq = ?,"
                " revision = ?, updated_at = ? WHERE worker_id = ?",
                (worker["intent_seq"] + 1, worker["revision"] + 1, now(),
                 worker["worker_id"]),
            )
        return {"target_node_id": target["node_id"],
                "contract_ref": descriptor["contract_ref"],
                "previous_ref": descriptor["previous_ref"],
                "source_event_id": source_event_id,
                "applied_event_id": event_id, "seq": seq,
                "node": self._node_view(self._node(tx, target["node_id"])),
                "stop": stopped, "duplicate": False}

    def progress_status(self, actor: R.Actor, run_id: str,
                        node_id: str) -> dict[str, Any]:
        """W04b read committed review evaluations and the frozen Task policy.

        Disabled policy returns enabled=False. Enabled returns policy, fixed
        evaluator_ref/comparison_ref, ordered evaluations, fold summary and
        stable decision_id for its trigger. Reuse existing events/artifacts;
        no business scoring or new authoritative progress database.
        """
        with self.store.read() as tx:
            run = self._visible_run(tx, actor, run_id)
            return self._progress_status_tx(tx, actor, run, node_id)

    def _visible_progress(self, tx: Tx, actor: R.Actor, run: Any,
                          node_id: str) -> dict[str, Any] | None:
        """Attach progress to operator reads; Worker grants cannot read the library."""
        if actor.worker_id is not None:
            return None
        return self._progress_status_tx(tx, actor, run, node_id)

    def _progress_status_tx(self, tx: Tx, actor: R.Actor, run: Any,
                            node_id: str) -> dict[str, Any]:
        """Shared projection for reads and the committed handoff plan."""
        R.require_text(node_id, "node_id", max_length=128)
        node = self._node(tx, node_id, run["run_id"])
        bundle = self._definition_bundle_for_run(tx, actor, run)
        task = bundle.tasks.get(node["template_ref"])
        if task is None:
            R.fail("not_found", "the node's task definition is not in the run library",
                   task_ref=node["template_ref"], node_id=node["node_id"])
        if task.progress is None:
            return {"node_id": node["node_id"], "enabled": False}
        evaluator_ref = self._progress_evaluator_ref(tx, run, node, bundle)
        comparison_ref = self._progress_comparison_ref(node, evaluator_ref)
        threshold = int(task.progress["no_improvement_limit"])
        facts = self._progress_evaluations(tx, run["run_id"], node["node_id"])
        documents = [fact.as_dict() for _, fact in facts]
        summary = fold_progress_evaluations(
            documents, node_id=node["node_id"],
            comparison_ref=comparison_ref, no_improvement_limit=threshold)
        decision_id = None
        decision_state = None
        decision_conflict = None
        if summary["stalled"]:
            decision_id = self._progress_decision_id(
                node["node_id"], comparison_ref,
                summary["trigger_checkpoint_id"])
            row = tx.one(
                "SELECT * FROM decision_requests WHERE decision_id = ?",
                (decision_id,))
            if row is not None:
                if self._progress_request_matches(
                        tx, row, node["node_id"], comparison_ref,
                        summary["trigger_checkpoint_id"]):
                    decision_state = row["state"]
                else:
                    # A foreign request occupies the derived id. Keep the read
                    # usable but expose the collision and never report release.
                    decision_conflict = {
                        "decision_id": decision_id,
                        "reason": "foreign_decision_collision",
                        "state": row["state"],
                    }
        return {
            "node_id": node["node_id"],
            "enabled": True,
            "policy": {"no_improvement_limit": threshold},
            "evaluator_ref": evaluator_ref,
            "comparison_ref": comparison_ref,
            "evaluations": [
                {"seq": seq, **fact.as_dict()} for seq, fact in facts],
            "summary": summary,
            "decision_id": decision_id,
            "decision_state": decision_state,
            "decision_conflict": decision_conflict,
            "released": decision_state == "resolved",
        }

    def _progress_evaluations(self, tx: Tx, run_id: str,
                              node_id: str) -> list[tuple[int, Any]]:
        """Committed review evaluations in API event order, never timestamps."""
        rows = tx.all(
            "SELECT seq, payload_json FROM events WHERE run_id = ?"
            " AND node_id = ? AND kind = 'review_submitted' ORDER BY seq",
            (run_id, node_id))
        facts: list[tuple[int, Any]] = []
        for row in rows:
            payload = load(row["payload_json"])
            document = payload.get("progress") if isinstance(payload, dict) else None
            if isinstance(document, dict):
                facts.append((row["seq"],
                              validate_progress_evaluation(document)))
        return facts

    @staticmethod
    def _progress_decision_id(node_id: str, comparison_ref: str,
                              trigger_checkpoint_id: str) -> str:
        """Bounded stable identity for one trigger point in one window."""
        material = {"node_id": node_id, "comparison_ref": comparison_ref,
                    "trigger_checkpoint_id": trigger_checkpoint_id}
        suffix = R.digest(material).split(":", 1)[1][:32]
        return f"{PROGRESS_DECISION_PREFIX}{suffix}"

    def _progress_request_source(self, tx: Tx, row: Any) -> dict[str, Any] | None:
        """Persisted creation-path provenance of one decision request event."""
        event = tx.one("SELECT * FROM events WHERE event_id = ?",
                       (row["request_event_id"],))
        payload = load(event["payload_json"]) if event is not None else None
        source = payload.get("source") if isinstance(payload, dict) else None
        return source if isinstance(source, dict) else None

    def _progress_request_matches(self, tx: Tx, row: Any, node_id: str,
                                  comparison_ref: str,
                                  trigger_checkpoint_id: str) -> bool:
        """True only for the system request that owns this node/window/trigger.

        The actor who created the event is not authority: an ordinary Worker
        decision can resolve the same predictable id. Provenance is the
        persisted ``source`` written by the derived creation path.
        """
        if row["node_id"] != node_id or row["reason"] != "progress_stalled":
            return False
        source = self._progress_request_source(tx, row)
        return (source is not None
                and source.get("kind") == "progress_stalled"
                and source.get("node_id") == node_id
                and source.get("comparison_ref") == comparison_ref
                and source.get("trigger_checkpoint_id") == trigger_checkpoint_id)

    @staticmethod
    def _progress_evaluation_id(node_id: str, candidate_id: str) -> str:
        material = {"node_id": node_id, "candidate_id": candidate_id}
        suffix = R.digest(material).split(":", 1)[1][:32]
        return f"progress-eval:{suffix}"

    def _progress_evaluator_ref(self, tx: Tx, run: Any, node: Any,
                                bundle: DefinitionBundle) -> str:
        """Fixed Task reference of the actual parent or declared checker.

        Resolve from the persisted Node/Worker relationship, never from whether
        an id merely looks like a sha256 content ref. An expansion child whose
        reviewer is ``parent`` stores the parent Worker id, so the evaluator is
        the parent Task. Anything else must be an approved review_receipt Task
        in this run's library; a Worker id of another Node is rejected.
        """
        reviewer = node["reviewer_ref"]
        parent = (self._node(tx, node["parent_id"], run["run_id"])
                  if node["parent_id"] is not None else None)
        if reviewer == "parent":
            parent_reviewer = True
        else:
            parent_reviewer = (parent is not None
                               and tx.one(
                                   "SELECT 1 FROM workers WHERE run_id = ?"
                                   " AND node_id = ? AND worker_id = ?",
                                   (run["run_id"], parent["node_id"],
                                    reviewer)) is not None)
        if parent_reviewer:
            if parent is None:
                R.fail("capability_unsupported",
                       "the root node has no parent progress evaluator",
                       node_id=node["node_id"])
            return parent["template_ref"]
        task = bundle.tasks.get(reviewer)
        if task is None or task.completion_kind != "review_receipt":
            R.fail("capability_unsupported",
                   "the declared progress evaluator is not an approved review Task",
                   node_id=node["node_id"], evaluator_ref=reviewer)
        return reviewer

    @staticmethod
    def _progress_comparison_ref(node: Any, evaluator_ref: str) -> str:
        """Canonical window identity: frozen inputs + contract + evaluator."""
        inputs = load(node["input_refs_json"])
        if inputs is None:
            inputs = []
        return R.digest({"input_refs": inputs,
                         "contract_ref": node["contract_ref"],
                         "evaluator_ref": evaluator_ref})

    def _plan_progress_replan(self, tx: Tx, actor: R.Actor, run: Any,
                              identifier: str, fields: dict[str, Any],
                              request_id: str) -> dict[str, Any]:
        """W04b authorized derived decision through commit_plan.

        Validate exact current node/window/trigger against committed facts and
        frozen limit; persist one existing decision_request with Scheduler as
        source Actor. Never impersonate the child Worker. Existing parent
        binding, scope, revision, uniqueness, round and reference guards apply.
        This saves a fact; Scheduler applies waiting_decision separately.
        """
        node = self._node(tx, fields["node_id"], run["run_id"])
        status = self._progress_status_tx(tx, actor, run, node["node_id"])
        if not status.get("enabled"):
            R.fail("invalid_argument",
                   "the node has no declared progress policy",
                   node_id=node["node_id"])
        summary = status["summary"]
        R.require(status["comparison_ref"] == fields["comparison_ref"],
                  "candidate_mismatch",
                  "the progress window is not the node's frozen window",
                  comparison_ref=fields["comparison_ref"],
                  expected=status["comparison_ref"])
        R.require(summary["stalled"], "invalid_argument",
                  "the node has not reached its no-improvement limit",
                  node_id=node["node_id"])
        R.require(summary["trigger_checkpoint_id"]
                  == fields["trigger_checkpoint_id"],
                  "candidate_mismatch",
                  "the progress trigger checkpoint does not match the fold",
                  trigger_checkpoint_id=fields["trigger_checkpoint_id"],
                  expected=summary["trigger_checkpoint_id"])
        expected_id = self._progress_decision_id(
            node["node_id"], status["comparison_ref"],
            summary["trigger_checkpoint_id"])
        R.require(identifier == expected_id, "candidate_mismatch",
                  "the progress decision id is not the stable trigger id",
                  decision_id=identifier, expected=expected_id)
        existing = tx.one(
            "SELECT * FROM decision_requests WHERE decision_id = ?",
            (identifier,))
        if existing is not None:
            R.require(existing["run_id"] == run["run_id"]
                      and existing["node_id"] == node["node_id"]
                      and existing["contract_ref"] == node["contract_ref"]
                      and self._progress_request_matches(
                          tx, existing, node["node_id"],
                          status["comparison_ref"],
                          summary["trigger_checkpoint_id"]),
                      "idempotency_conflict",
                      "decision_id is occupied by a foreign request",
                      decision_id=identifier)
            event = tx.one("SELECT * FROM events WHERE event_id = ?",
                           (existing["request_event_id"],))
            return {"decision_id": existing["decision_id"],
                    "node_id": node["node_id"],
                    "parent_id": existing["parent_id"],
                    "state": node["state"],
                    "decision_state": existing["state"],
                    "round": existing["round"], "event_id": existing["request_event_id"],
                    "seq": event["seq"], "revision": node["revision"],
                    "duplicate": True}
        open_request = tx.one(
            "SELECT * FROM decision_requests WHERE node_id = ? AND state = 'open'",
            (node["node_id"],))
        if open_request is not None:
            R.fail("decision_conflict",
                   "node already has an unresolved decision request",
                   decision_id=open_request["decision_id"])
        if node["parent_id"] is None:
            R.fail("capability_unsupported",
                   "the root node has no parent decision authority",
                   node_id=node["node_id"])
        parent = self._node(tx, node["parent_id"], run["run_id"])
        if parent["state"] in ("succeeded", "failed", "cancelled"):
            R.fail("invalid_argument",
                   "the parent node is no longer open for decisions",
                   parent_id=parent["node_id"], parent_state=parent["state"])
        if node["state"] != "running":
            R.fail("decision_conflict",
                   "a progress handoff requires an open running node",
                   state=node["state"])
        worker = tx.one(
            "SELECT * FROM workers WHERE node_id = ? AND run_id = ?"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (node["node_id"], run["run_id"]))
        if worker is None:
            R.fail("decision_conflict",
                   "the child node has no execution identity to wait",
                   node_id=node["node_id"])
        material_refs = self._progress_material_refs(status)
        self._check_refs(tx, actor, material_refs)
        question = (
            f"Node {node['node_id']} reached the declared progress limit "
            f"({status['policy']['no_improvement_limit']} consecutive "
            "not_improved checkpoints). Re-understand the task, adjust the "
            "method, or propose an expansion while keeping the frozen contract.")
        payload = {
            "kind": "progress_stalled",
            "node_id": node["node_id"],
            "comparison_ref": status["comparison_ref"],
            "trigger_checkpoint_id": summary["trigger_checkpoint_id"],
        }
        event_id, seq = tx.append_event(
            run_id=run["run_id"], kind="decision_requested",
            node_id=node["node_id"], worker_id=worker["worker_id"],
            epoch=worker["epoch"], actor_id=actor.actor_id,
            request_id=request_id, payload={
                "decision_id": identifier, "node_id": node["node_id"],
                "worker_id": worker["worker_id"], "epoch": worker["epoch"],
                "round": node["round"] + 1,
                "contract_ref": node["contract_ref"],
                "reason": "progress_stalled", "question": question,
                "options": [], "material_refs": material_refs,
                "source": payload,
            },
        )
        timestamp = now()
        tx.execute(
            "INSERT INTO decision_requests (decision_id, run_id, node_id, parent_id,"
            " worker_id, epoch, round, contract_ref, reason, question, options_json,"
            " material_refs_json, state, request_event_id, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)",
            (identifier, run["run_id"], node["node_id"], parent["node_id"],
             worker["worker_id"], worker["epoch"], node["round"] + 1,
             node["contract_ref"], "progress_stalled", question, dump([]),
             dump(material_refs), event_id, timestamp, timestamp),
        )
        return {"decision_id": identifier, "node_id": node["node_id"],
                "parent_id": parent["node_id"], "state": node["state"],
                "decision_state": "open", "round": node["round"] + 1,
                "event_id": event_id, "seq": seq,
                "revision": node["revision"], "duplicate": False}

    @staticmethod
    def _progress_material_refs(status: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Evidence of the current no-improvement streak, deduplicated.

        Only the current comparison window counts, and only checkpoints after
        the last genuine improvement: an earlier failure belongs to a streak
        that was already cleared. Unknown results neither increment nor reset,
        so they remain part of the current streak. The full history stays in
        ``progress_status``; this helper only selects request material.
        """
        evaluations = [evaluation for evaluation in status["evaluations"]
                       if evaluation["comparison_ref"] == status["comparison_ref"]]
        start = 0
        for index, evaluation in enumerate(evaluations):
            if evaluation["result"] == "improved":
                start = index + 1
        seen: set[str] = set()
        refs: list[dict[str, Any]] = []
        for evaluation in evaluations[start:]:
            ref = evaluation["evidence_ref"]
            if ref["id"] in seen:
                continue
            seen.add(ref["id"])
            refs.append(dict(ref))
        return refs

    def _read_definition_document(self, tx: Tx, actor: R.Actor,
                                  reference: str) -> dict[str, Any]:
        """Read authorized canonical Agent/Task JSON in this transaction.

        Reuse artifact visibility and byte integrity checks. No YAML/path/URL
        guessing. Refuse noncanonical content identity, wrong root type, missing
        data and unauthorized refs; never substitute an empty definition.
        """
        reference = R.require_text(reference, "definition reference",
                                   max_length=71)
        R.require(R.ARTIFACT_ID.fullmatch(reference) is not None,
                  "invalid_argument",
                  "definition content must be a sha256 content ref",
                  ref=reference)
        row = tx.one("SELECT * FROM artifacts WHERE id = ?", (reference,))
        if row is None:
            R.fail("not_found", "definition content is not registered",
                   ref=reference)
        if not self._artifact_visible(tx, actor, row):
            R.fail("not_found", "definition content is not visible to this actor",
                   ref=reference)
        data = self.artifacts.read({"id": row["id"], "sha256": row["sha256"],
                                    "size": row["size"],
                                    "media_type": row["media_type"]})
        try:
            document = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            R.fail("artifact_mismatch", "definition content is not UTF-8 JSON",
                   ref=reference)
        R.require(isinstance(document, dict), "invalid_argument",
                  "definition content must be a JSON object", ref=reference)
        R.require(R.canonical(document).encode("utf-8") == data,
                  "artifact_mismatch",
                  "definition content is not canonical JSON", ref=reference)
        return document

    def _compile_definition_bundle(self, tx: Tx, actor: R.Actor,
                                   document: Mapping[str, Any]) -> DefinitionBundle:
        """One compile path shared by validate and publish; no writes here."""
        return compile_definition_bundle(
            document,
            read_document=lambda reference: self._read_definition_document(
                tx, actor, reference),
        )

    def _register_definition_library(self, tx: Tx, actor: R.Actor,
                                     bundle: DefinitionBundle) -> dict[str, list[str]]:
        """Register library metadata/grants in Graph publication's tx.

        Factor the existing artifact-registration primitive. Do not call the
        public put_artifact and accidentally commit nested publication first.
        Return exact Agent/Task ref sets, not refs discovered in arbitrary text.
        """
        for reference, document in sorted(bundle.artifact_documents.items()):
            data = R.canonical(document).encode("utf-8")
            ref = self.artifacts.put(data, "application/json")
            if ref["id"] != reference:
                R.fail("artifact_mismatch",
                       "definition library content does not match its ref",
                       expected=reference, actual=ref["id"])
            tx.execute(
                "INSERT OR IGNORE INTO artifacts (id, sha256, size, media_type,"
                " created_by, created_at) VALUES (?, ?, ?, 'application/json', ?, ?)",
                (ref["id"], ref["sha256"], ref["size"], actor.actor_id, now()),
            )
            stored = tx.one("SELECT * FROM artifacts WHERE id = ?", (ref["id"],))
            if stored["sha256"] != ref["sha256"] or stored["size"] != ref["size"]:
                R.fail("artifact_mismatch",
                       "stored definition content disagrees with its ref",
                       ref=reference)
        return {"agents": sorted(bundle.agents), "tasks": sorted(bundle.tasks)}

    def _definition_bundle_for_run(self, tx: Tx, actor: R.Actor,
                                    run: Mapping[str, Any]) -> DefinitionBundle:
        """Load the run's frozen Graph and approved library closure.

        Verify the run/definition versions and all content identities. Reads
        are bounded by persisted library refs; no latest/default-label fallback.
        """
        row = self._definition_row(tx, run["definition_ref"])
        document = load(row["document_json"])
        R.require(isinstance(document, dict), "invalid_argument",
                  "stored definition document must be an object",
                  definition_ref=row["definition_ref"])
        R.require(document.get("format") == R.DEFINITION_FORMAT,
                  "invalid_argument",
                  "the stored definition is not Graph /2",
                  definition_ref=row["definition_ref"])
        R.require(R.digest(document) == row["document_digest"],
                  "artifact_mismatch",
                  "stored definition content does not match its digest",
                  definition_ref=row["definition_ref"])
        R.require(row["document_digest"] == run["definition_digest"],
                  "artifact_mismatch",
                  "the run froze a different definition digest",
                  run_id=run["run_id"],
                  run_digest=run["definition_digest"],
                  definition_digest=row["document_digest"])
        library = load(row["library_refs_json"])
        R.require(isinstance(library, list) and
                  all(isinstance(item, str) for item in library),
                  "artifact_mismatch",
                  "stored definition library refs are invalid",
                  definition_ref=row["definition_ref"])
        bundle = self._compile_definition_bundle(tx, actor, document)
        R.require(R.digest(bundle.document) == row["document_digest"],
                  "artifact_mismatch",
                  "recompiling the frozen definition changed its identity",
                  definition_ref=row["definition_ref"])
        expected = sorted(set(bundle.agents) | set(bundle.tasks))
        R.require(sorted(set(library)) == expected, "artifact_mismatch",
                  "the stored definition library closure changed",
                  definition_ref=row["definition_ref"],
                  expected=expected, stored=sorted(set(library)))
        return bundle

    def definition_bundle_for_run(self, actor: R.Actor,
                                  run_id: str) -> DefinitionBundle:
        """Application helper for Scheduler, not a new MCP/CLI service."""
        actor.require_role("scheduler")
        with self.store.read() as tx:
            run = self._visible_run(tx, actor, run_id)
            return self._definition_bundle_for_run(tx, actor, run)

    # ------------------------------------------------------------------
    # helpers

    @staticmethod
    def _fingerprint(actor: R.Actor, operation: str, args: Any) -> str:
        return R.digest({"actor": actor.actor_id, "operation": operation, "args": args})

    @staticmethod
    def _request_id(request_id: Any) -> str:
        return R.require_text(request_id, "request_id", max_length=128)

    @staticmethod
    def _run_row(tx: Tx, run_id: str) -> Any:
        row = tx.one("SELECT * FROM runs WHERE run_id = ?", (run_id,))
        if row is None:
            R.fail("not_found", "run does not exist", run_id=run_id)
        return row

    def _visible_run(self, tx: Tx, actor: R.Actor, run_id: str) -> Any:
        row = self._run_row(tx, run_id)
        actor.require_read(run_id)
        return row

    def _node(self, tx: Tx, node_id: str, run_id: str | None = None) -> Any:
        row = tx.one("SELECT * FROM nodes WHERE node_id = ?", (node_id,))
        if row is None or (run_id is not None and row["run_id"] != run_id):
            R.fail("not_found", "node does not exist in this run", node_id=node_id)
        return row

    def _worker(self, tx: Tx, worker_id: str, run_id: str | None = None) -> Any:
        row = tx.one("SELECT * FROM workers WHERE worker_id = ?", (worker_id,))
        if row is None or (run_id is not None and row["run_id"] != run_id):
            R.fail("not_found", "worker does not exist in this run", worker_id=worker_id)
        return row

    def _candidate(self, tx: Tx, candidate_id: str) -> Any:
        row = tx.one("SELECT * FROM candidates WHERE candidate_id = ?", (candidate_id,))
        if row is None:
            R.fail("candidate_mismatch", "candidate does not exist", candidate_id=candidate_id)
        return row

    @staticmethod
    def _check_revision(row: Any, expected: Any, kind: str, identifier: str) -> None:
        R.require_int(expected, "expected_revision", minimum=1)
        if row["revision"] != expected:
            R.fail("revision_conflict", "resource revision changed",
                   kind=kind, id=identifier, current_revision=row["revision"])

    @staticmethod
    def _artifact_row(tx: Tx, ref: dict[str, Any]) -> Any:
        row = tx.one("SELECT * FROM artifacts WHERE id = ?", (ref["id"],))
        if row is None:
            R.fail("not_found", "artifact is not registered", id=ref["id"])
        if row["sha256"] != ref["sha256"] or row["size"] != ref["size"]:
            R.fail("artifact_mismatch", "artifact metadata does not match", id=ref["id"])
        return row

    @staticmethod
    def _run_uses_artifact(tx: Tx, run_id: str, artifact_id: str) -> bool:
        def uses(raw: Any) -> bool:
            if raw is None:
                return False
            return any(ref["id"] == artifact_id for ref in _artifact_refs(load(raw)))

        run = tx.one("SELECT input_refs_json FROM runs WHERE run_id = ?", (run_id,))
        if run is not None and uses(run["input_refs_json"]):
            return True
        # The frozen definition library is part of the run's authorized
        # closure. Only the exact published Agent/Task refs grant access; a
        # digest that merely appears inside arbitrary text does not.
        definition = tx.one(
            "SELECT d.library_refs_json, d.document_json FROM definitions d"
            " JOIN runs r ON r.definition_ref = d.definition_ref"
            " WHERE r.run_id = ?", (run_id,))
        if definition is not None:
            library = load(definition["library_refs_json"])
            if isinstance(library, list) and artifact_id in library:
                return True
            # A frozen Graph may bind literal artifacts directly in node
            # inputs. They are part of the run closure before node rows exist,
            # otherwise the initial node-create batch cannot pass visibility.
            if any(ref["id"] == artifact_id
                   for ref in _definition_static_refs(
                       load(definition["document_json"]))):
                return True
        for row in tx.all(
            "SELECT input_bindings_json, input_refs_json FROM nodes WHERE run_id = ?",
            (run_id,),
        ):
            if uses(row["input_bindings_json"]) or uses(row["input_refs_json"]):
                return True
        for row in tx.all(
            "SELECT output_refs_json, summary_ref_json FROM candidates WHERE run_id = ?",
            (run_id,),
        ):
            if uses(row["output_refs_json"]) or uses(row["summary_ref_json"]):
                return True
        for row in tx.all("SELECT feedback_ref_json FROM reviews WHERE run_id = ?", (run_id,)):
            if uses(row["feedback_ref_json"]):
                return True
        for row in tx.all(
            "SELECT payload_json FROM expansion_requests WHERE run_id = ?", (run_id,)
        ):
            if uses(row["payload_json"]):
                return True
        for row in tx.all("SELECT payload_json FROM messages WHERE run_id = ?", (run_id,)):
            if uses(row["payload_json"]):
                return True
        for row in tx.all(
                "SELECT payload_json FROM events WHERE run_id = ? AND kind IN (?, ?)",
                (run_id, ACCEPTANCE_REQUESTED, ACCEPTANCE_APPLIED)):
            if uses(row["payload_json"]):
                return True
        return False

    @classmethod
    def _worker_grant_ids(cls, tx: Tx, actor: R.Actor) -> set[str]:
        """Artifacts a Worker may read: its frozen inputs, inbox and own output.

        Workers do not inherit run-wide visibility; a sibling's artifact is
        visible only after the Scheduler delivers it in a committed message.
        """
        worker = tx.one("SELECT * FROM workers WHERE worker_id = ?",
                        (actor.worker_id,))
        if worker is None:
            return set()
        run_id = worker["run_id"]
        node = tx.one("SELECT * FROM nodes WHERE node_id = ?", (worker["node_id"],))
        found: set[str] = set()

        definition = tx.one(
            "SELECT d.library_refs_json FROM definitions d"
            " JOIN runs r ON r.definition_ref = d.definition_ref"
            " WHERE r.run_id = ?", (run_id,))
        if definition is not None:
            # Immutable definition documents are part of the run's frozen
            # library, not another run's business data.
            for reference in load(definition["library_refs_json"]) or []:
                found.add(reference)

        def add(value: Any) -> None:
            for ref in _artifact_refs(value):
                found.add(ref["id"])

        add(load(worker["context_json"]))
        if node is not None:
            add(load(node["input_bindings_json"]))
            if node["input_refs_json"] is not None:
                add(load(node["input_refs_json"]))
            else:
                run = tx.one("SELECT input_refs_json FROM runs WHERE run_id = ?",
                             (run_id,))
                if run is not None:
                    add(load(run["input_refs_json"]))
            if actor.actor_id == node["reviewer_ref"]:
                for candidate in tx.all(
                        "SELECT output_refs_json, summary_ref_json FROM candidates"
                        " WHERE run_id = ? AND node_id = ?",
                        (run_id, node["node_id"])):
                    add(load(candidate["output_refs_json"]))
                    add(load(candidate["summary_ref_json"]))
        for message in tx.all(
                "SELECT payload_json FROM messages WHERE worker_id = ? AND epoch = ?",
                (worker["worker_id"], worker["epoch"])):
            add(load(message["payload_json"]))
        for candidate in tx.all(
                "SELECT output_refs_json, summary_ref_json FROM candidates"
                " WHERE worker_id = ? AND epoch = ?",
                (worker["worker_id"], worker["epoch"])):
            add(load(candidate["output_refs_json"]))
            add(load(candidate["summary_ref_json"]))
        return found

    @staticmethod
    def _artifact_published_by(tx: Tx, actor_id: str, artifact_id: str) -> bool:
        """Whether this actor actually supplied these bytes.

        A ``put_artifact`` idempotency receipt is the durable publication
        fact, so every publisher of identical content keeps its own right
        even though the artifact row stores only the original creator.
        """
        for receipt in tx.all(
                "SELECT operation, response_json FROM idempotency"
                " WHERE actor_id = ? AND operation IN"
                " ('put_artifact', 'publish_definition')",
                (actor_id,)):
            published = load(receipt["response_json"])
            artifact = published.get("artifact") or {}
            if artifact.get("id") == artifact_id:
                return True
            library = published.get("library_refs")
            if isinstance(library, list) and artifact_id in library:
                return True
        return False

    @classmethod
    def _artifact_visible(cls, tx: Tx, actor: R.Actor, row: Any) -> bool:
        # Content-addressed bytes may be supplied by several callers. Each
        # publisher keeps its own durable grant; nobody else inherits it.
        if cls._artifact_published_by(tx, actor.actor_id, row["id"]):
            return True
        if actor.worker_id is not None:
            if row["created_by"] == actor.actor_id:
                return True
            return row["id"] in cls._worker_grant_ids(tx, actor)
        if actor.run_ids is None or row["created_by"] == actor.actor_id:
            return True
        return any(cls._run_uses_artifact(tx, run_id, row["id"])
                   for run_id in sorted(actor.run_ids))

    def _check_refs(self, tx: Tx, actor: R.Actor,
                    refs: list[dict[str, Any]]) -> None:
        for ref in refs:
            row = self._artifact_row(tx, ref)
            if not self._artifact_visible(tx, actor, row):
                R.fail("not_found", "artifact is not visible to this actor", id=ref["id"])

    def _definition_row(self, tx: Tx, definition_ref: str) -> Any:
        row = tx.one("SELECT * FROM definitions WHERE definition_ref = ?", (definition_ref,))
        if row is None:
            R.fail("not_found", "definition version does not exist",
                   definition_ref=definition_ref)
        return row

    @staticmethod
    def _node_depth(tx: Tx, node: Any) -> int:
        depth = 0
        current = node
        seen: set[str] = set()
        while current["parent_id"] is not None:
            if current["parent_id"] in seen:
                R.fail("dependency_cycle", "node parent chain contains a cycle")
            seen.add(current["parent_id"])
            parent = tx.one("SELECT * FROM nodes WHERE node_id = ?", (current["parent_id"],))
            if parent is None:
                R.fail("internal", "node parent is missing", node_id=current["node_id"])
            current = parent
            depth += 1
            if depth > 4096:
                R.fail("dependency_cycle", "node parent chain is too deep")
        return depth

    def _run_budget(self, run: Any) -> dict[str, Any]:
        return load(run["budget_json"])

    def _check_budget_nodes(self, tx: Tx, run: Any, adding: int) -> None:
        limit = self._run_budget(run).get("max_nodes")
        if limit is None:
            return
        count = tx.one("SELECT COUNT(*) AS n FROM nodes WHERE run_id = ?", (run["run_id"],))["n"]
        if count + adding > limit:
            R.fail("budget_exhausted", "node budget would be exceeded",
                   max_nodes=limit, existing=count, requested=adding)

    def _check_budget_depth(self, tx: Tx, run: Any, parent: Any) -> None:
        limit = self._run_budget(run).get("max_depth")
        if limit is None:
            return
        depth = self._node_depth(tx, parent) + 1
        if depth > limit:
            R.fail("budget_exhausted", "node depth would exceed the frozen budget",
                   max_depth=limit, requested_depth=depth)

    def _check_budget_round(self, run: Any, round_number: int) -> None:
        limit = self._run_budget(run).get("max_rounds")
        if limit is not None and round_number > limit:
            R.fail("budget_exhausted", "round budget would be exceeded",
                   max_rounds=limit, requested_round=round_number)

    @staticmethod
    def _require_open_run(run: Any) -> None:
        if run["status"] in RUN_TERMINAL:
            R.fail("invalid_argument", "run is terminal", status=run["status"])

    # ------------------------------------------------------------------
    # read views

    @staticmethod
    def _definition_view(row: Any) -> dict[str, Any]:
        document = load(row["document_json"])
        return {
            "format": R.RESOURCE_FORMAT,
            "kind": "definition",
            "id": row["definition_ref"],
            "definition_id": row["definition_id"],
            "definition_ref": row["definition_ref"],
            "revision": row["revision"],
            "document_digest": row["document_digest"],
            "document": document,
            "library_refs": load(row["library_refs_json"]),
            "policy": document.get("policy"),
            "budget_limits": document.get("budget_limits") or {},
            "created_by": row["created_by"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _run_view(row: Any) -> dict[str, Any]:
        return {
            "format": R.RESOURCE_FORMAT,
            "kind": "run",
            "id": row["run_id"],
            "run_id": row["run_id"],
            "revision": row["revision"],
            "definition_ref": row["definition_ref"],
            "definition_digest": row["definition_digest"],
            "input_refs": load(row["input_refs_json"]),
            "policy_ref": row["policy_ref"],
            "budget": load(row["budget_json"]),
            "created_by": row["created_by"],
            "desired": row["desired"],
            "status": row["status"],
            "summary": load(row["summary_json"]),
            "event_seq": row["event_seq"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _acceptance_brief(self, tx: Tx, node: Any) -> dict[str, Any]:
        """Compact committed version state for dispatch gates and rollout."""
        history = self._acceptance_history(tx, node)
        run = tx.one("SELECT status FROM runs WHERE run_id = ?",
                     (node["run_id"],))
        terminal = (node["state"] in NODE_TERMINAL
                    or (run is not None and run["status"] in RUN_TERMINAL))
        pending = None if terminal else next(
            (entry for entry in history if entry["state"] == "pending"), None)
        applied = next((entry for entry in reversed(history)
                        if entry["state"] == "applied"
                        and entry["contract_ref"] == node["contract_ref"]), None)
        return {
            "contract_ref": node["contract_ref"],
            "upper_contract_ref": node["template_ref"],
            "local": node["contract_ref"] != node["template_ref"],
            "applied": applied,
            "pending": pending,
        }

    @staticmethod
    def _node_view(row: Any) -> dict[str, Any]:
        return {
            "format": R.RESOURCE_FORMAT,
            "kind": "node",
            "id": row["node_id"],
            "node_id": row["node_id"],
            "run_id": row["run_id"],
            "parent_id": row["parent_id"],
            "expansion_id": row["expansion_id"],
            "local_child_id": row["local_child_id"],
            "template_ref": row["template_ref"],
            "contract_ref": row["contract_ref"],
            "input_bindings": load(row["input_bindings_json"]),
            "input_refs": load(row["input_refs_json"]),
            "delegation": load(row["delegation_json"]),
            "dependencies": load(row["dependencies_json"]),
            "reviewer_ref": row["reviewer_ref"],
            "closure": load(row["closure_json"]),
            "state": row["state"],
            "round": row["round"],
            "accepted_candidate_ref": row["accepted_candidate_ref"],
            "revision": row["revision"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _worker_view(row: Any) -> dict[str, Any]:
        return {
            "format": R.RESOURCE_FORMAT,
            "kind": "worker",
            "id": row["worker_id"],
            "worker_id": row["worker_id"],
            "run_id": row["run_id"],
            "node_id": row["node_id"],
            "backend_ref": row["backend_ref"],
            "agent_ref": row["agent_ref"],
            "controller_id": row["controller_id"],
            "epoch": row["epoch"],
            "intent_seq": row["intent_seq"],
            "desired": row["desired"],
            "observed": row["observed"],
            "session_ref": row["session_ref"],
            "checkpoint_ref": row["checkpoint_ref"],
            "context": load(row["context_json"]),
            "tool_profile": load(row["tool_profile_json"]),
            "budget": load(row["budget_json"]),
            "required_capabilities": load(row["required_capabilities_json"]) or {},
            "usage": load(row["usage_json"]) or {"provider_calls": 0, "tool_calls": 0},
            "activities": load(row["activities_json"]) or [],
            "activities_complete": bool(row["activities_complete"]),
            "checkpoint": load(row["checkpoint_json"]),
            "last_event_id": row["last_event_id"],
            "revision": row["revision"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _candidate_view(row: Any) -> dict[str, Any]:
        return {
            "format": R.RESOURCE_FORMAT,
            "kind": "candidate",
            "id": row["candidate_id"],
            "candidate_id": row["candidate_id"],
            "run_id": row["run_id"],
            "node_id": row["node_id"],
            "worker_id": row["worker_id"],
            "epoch": row["epoch"],
            "round": row["round"],
            "output_refs": load(row["output_refs_json"]),
            "contract_ref": row["contract_ref"],
            "summary_ref": load(row["summary_ref_json"]),
            "event_id": row["event_id"],
            "decision": row["decision"],
            "decision_event_id": row["decision_event_id"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _message_view(row: Any) -> dict[str, Any]:
        return {
            "format": R.RESOURCE_FORMAT,
            "kind": "message",
            "id": row["message_id"],
            "message_id": row["message_id"],
            "run_id": row["run_id"],
            "worker_id": row["worker_id"],
            "node_id": row["node_id"],
            "epoch": row["epoch"],
            "delivery_key": row["delivery_key"],
            "payload": load(row["payload_json"]),
            "payload_digest": row["payload_digest"],
            "created_by": row["created_by"],
            "delivery_state": row["delivery_state"],
            "revision": row["revision"],
            "receipt": load(row["delivery_receipt_json"]),
            "enqueue_event_id": row["enqueue_event_id"],
            "delivery_event_id": row["delivery_event_id"],
            "created_at": row["created_at"],
            "delivered_at": row["delivered_at"],
        }

    @staticmethod
    def _decision_view(row: Any) -> dict[str, Any]:
        return {
            "format": R.RESOURCE_FORMAT,
            "kind": "decision_request",
            "id": row["decision_id"],
            "decision_id": row["decision_id"],
            "run_id": row["run_id"],
            "node_id": row["node_id"],
            "parent_id": row["parent_id"],
            "worker_id": row["worker_id"],
            "epoch": row["epoch"],
            "round": row["round"],
            "contract_ref": row["contract_ref"],
            "reason": row["reason"],
            "question": row["question"],
            "options": load(row["options_json"]),
            "material_refs": load(row["material_refs_json"]),
            "state": row["state"],
            "reply": load(row["reply_json"]),
            "resolved_by": row["resolved_by"],
            "resolved_epoch": row["resolved_epoch"],
            "request_event_id": row["request_event_id"],
            "resolved_event_id": row["resolved_event_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _event_view(row: Any) -> dict[str, Any]:
        return {
            "format": R.EVENT_FORMAT,
            "event_id": row["event_id"],
            "run_id": row["run_id"],
            "seq": row["seq"],
            "kind": row["kind"],
            "node_id": row["node_id"],
            "worker_id": row["worker_id"],
            "epoch": row["epoch"],
            "payload": load(row["payload_json"]),
            "actor": row["actor_id"],
            "request_id": row["request_id"],
            "created_at": row["created_at"],
        }

    # ------------------------------------------------------------------
    # artifacts

    def put_artifact(self, actor: R.Actor, request_id: str,
                     content: bytes | str, media_type: str) -> dict[str, Any]:
        self._request_id(request_id)
        R.require_text(media_type, "media_type", max_length=256)
        ref = self.artifacts.put(content, media_type)
        args = {"artifact": ref}
        fingerprint = self._fingerprint(actor, "put_artifact", args)
        with self.store.write() as tx:
            replay = tx.idempotency_lookup(actor.actor_id, "put_artifact", request_id,
                                           fingerprint)
            if replay is not None:
                return replay
            tx.execute(
                "INSERT OR IGNORE INTO artifacts (id, sha256, size, media_type, created_by,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (ref["id"], ref["sha256"], ref["size"], media_type, actor.actor_id, now()),
            )
            stored = tx.one("SELECT * FROM artifacts WHERE id = ?", (ref["id"],))
            response = {"artifact": {"id": stored["id"], "sha256": stored["sha256"],
                                     "size": stored["size"],
                                     "media_type": stored["media_type"]}}
            tx.idempotency_store(actor.actor_id, "put_artifact", request_id, fingerprint,
                                 response)
            return response

    def read_artifact(self, actor: R.Actor, ref: dict[str, Any]) -> bytes:
        checked = R.artifact_ref(ref)
        with self.store.read() as tx:
            row = self._artifact_row(tx, checked)
            if not self._artifact_visible(tx, actor, row):
                R.fail("not_found", "artifact is not visible to this actor", id=checked["id"])
        return self.artifacts.read(checked)

    # ------------------------------------------------------------------
    # definitions and runs

    def validate_definition(self, actor: R.Actor, document: Any) -> dict[str, Any]:
        """Compile Graph /2 with the same reader publish uses, without writing."""
        actor.require_role("publisher")
        with self.store.read() as tx:
            bundle = self._compile_definition_bundle(tx, actor, document)
            normalized = bundle.document
            return {
                "document": normalized,
                "definition_id": normalized["definition_id"],
                "base_revision": normalized["base_revision"],
                "document_digest": R.digest(normalized),
                "library_refs": sorted(set(bundle.agents) | set(bundle.tasks)),
                "agents": sorted(bundle.agents),
                "tasks": sorted(bundle.tasks),
            }

    def publish_definition(self, actor: R.Actor, document: Any,
                           request_id: str) -> dict[str, Any]:
        """Publish one immutable Graph /2 revision and its library in one tx."""
        self._request_id(request_id)
        actor.require_role("publisher")
        with self.store.write() as tx:
            bundle = self._compile_definition_bundle(tx, actor, document)
            body = bundle.document
            definition_ref = R.digest(body)
            definition_id = body["definition_id"]
            base_revision = body["base_revision"]
            args = {"document": body}
            fingerprint = self._fingerprint(actor, "publish_definition", args)
            replay = tx.idempotency_lookup(actor.actor_id, "publish_definition",
                                           request_id, fingerprint)
            if replay is not None:
                return replay
            latest = tx.one(
                "SELECT * FROM definitions WHERE definition_id = ?"
                " ORDER BY revision DESC LIMIT 1", (definition_id,))
            if latest is None:
                if base_revision != 0:
                    R.fail("revision_conflict",
                           "definition does not exist at base_revision",
                           current_revision=0)
                revision = 1
            else:
                if base_revision != latest["revision"]:
                    R.fail("revision_conflict", "definition revision changed",
                           current_revision=latest["revision"])
                if latest["definition_ref"] == definition_ref:
                    library = self._register_definition_library(
                        tx, actor, bundle)
                    refs = sorted(set(library["agents"]) | set(library["tasks"]))
                    response = {
                        "definition_ref": definition_ref,
                        "definition_id": definition_id,
                        "revision": latest["revision"],
                        "document_digest": definition_ref,
                        "library_refs": refs,
                        "unchanged": True,
                    }
                    tx.idempotency_store(actor.actor_id, "publish_definition",
                                         request_id, fingerprint, response)
                    return response
                revision = latest["revision"] + 1
            library = self._register_definition_library(
                tx, actor, bundle)
            refs = sorted(set(library["agents"]) | set(library["tasks"]))
            tx.execute(
                "INSERT INTO definitions (definition_id, revision, definition_ref,"
                " document_digest, document_json, library_refs_json, created_by,"
                " created_at, request_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (definition_id, revision, definition_ref, definition_ref,
                 dump(body), dump(refs), actor.actor_id, now(), request_id),
            )
            response = {
                "definition_ref": definition_ref,
                "definition_id": definition_id,
                "revision": revision,
                "document_digest": definition_ref,
                "library_refs": refs,
                "unchanged": False,
            }
            tx.idempotency_store(actor.actor_id, "publish_definition",
                                 request_id, fingerprint, response)
            return response

    def create_run(self, actor: R.Actor, definition_ref: str, inputs: Any,
                   request_id: str) -> dict[str, Any]:
        """Submit a run against one frozen Graph /2 revision."""
        self._request_id(request_id)
        actor.require_role("publisher")
        R.require_text(definition_ref, "definition_ref", max_length=71)
        R.require(R.ARTIFACT_ID.fullmatch(definition_ref) is not None,
                  "invalid_argument",
                  "definition_ref must be a sha256 content ref",
                  definition_ref=definition_ref)
        R.require(isinstance(inputs, dict), "invalid_argument",
                  "inputs must be an object")
        args = {"definition_ref": definition_ref, "inputs": inputs}
        fingerprint = self._fingerprint(actor, "create_run", args)
        with self.store.write() as tx:
            replay = tx.idempotency_lookup(actor.actor_id, "create_run",
                                           request_id, fingerprint)
            if replay is not None:
                return replay
            definition = self._definition_row(tx, definition_ref)
            body = load(definition["document_json"])
            R.require(body.get("format") == R.DEFINITION_FORMAT,
                      "invalid_argument",
                      "runs require a Graph /2 definition",
                      definition_ref=definition_ref)
            R.require(R.digest(body) == definition["document_digest"],
                      "artifact_mismatch",
                      "the definition content does not match its digest",
                      definition_ref=definition_ref)
            schema = body.get("input_schema")
            if isinstance(schema, dict):
                R.validate_schema_value(schema, inputs, "inputs")
            self._check_refs(tx, actor, _artifact_refs(inputs))
            # Literal refs pinned in the frozen Graph are part of the run's
            # declared inputs; reject a missing or mismatched one atomically
            # instead of failing later when the Scheduler materialises nodes.
            self._check_refs(tx, actor, _definition_static_refs(body))
            run_id = new_id()
            created = now()
            budget = body.get("budget_limits") or {}
            policy = body["policy"]
            tx.execute(
                "INSERT INTO runs (run_id, definition_ref, definition_digest,"
                " input_refs_json, policy_ref, budget_json, created_by, desired,"
                " status, summary_json, revision, event_seq, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'running', 'pending', '{}', 1, 0, ?, ?)",
                (run_id, definition_ref, definition["document_digest"],
                 dump(inputs), policy, dump(budget), actor.actor_id,
                 created, created),
            )
            event_id, seq = tx.append_event(
                run_id=run_id, kind="run_created", actor_id=actor.actor_id,
                request_id=request_id,
                payload={"definition_ref": definition_ref,
                         "document_digest": definition["document_digest"],
                         "input_refs": inputs, "policy_ref": policy},
            )
            response = {
                "run_id": run_id,
                "revision": 1,
                "definition_ref": definition_ref,
                "definition_digest": definition["document_digest"],
                "status": "pending",
                "desired": "running",
                "event_id": event_id,
                "seq": seq,
            }
            tx.idempotency_store(actor.actor_id, "create_run", request_id,
                                 fingerprint, response)
            return response

    def list_runs(self, actor: R.Actor, *, status: str | None = None,
                  limit: int = 200) -> dict[str, Any]:
        """List runs visible to an operator identity, newest first.

        The host uses this to find active runs after a restart; a Teacher uses
        it to reconnect to a run created in an earlier session. It is a pure
        read and never changes run state.
        """
        R.require(not actor.roles.isdisjoint({"publisher", "scheduler"}),
                  "unauthorized", "listing runs requires publisher or scheduler")
        R.require_int(limit, "limit", minimum=1)
        R.require(limit <= 1000, "invalid_argument", "limit must be <= 1000")
        if status is not None:
            R.require(status in R.RUN_STATUSES, "invalid_argument",
                      "unknown run status", status=status)
        with self.store.read() as tx:
            if status is None:
                rows = tx.all("SELECT * FROM runs ORDER BY created_at DESC LIMIT ?",
                              (limit,))
            else:
                rows = tx.all(
                    "SELECT * FROM runs WHERE status = ?"
                    " ORDER BY created_at DESC LIMIT ?", (status, limit))
            return {
                "format": "agentgenome-control-plane-runs/1",
                "runs": [self._run_view(row) for row in rows
                         if actor.can_read(row["run_id"])],
            }

    def list_reconciliation_runs(self, actor: R.Actor, *,
                                 limit: int = 500) -> dict[str, Any]:
        """Runs whose graph or execution may still need work after a restart.

        Non-terminal runs always need reconciliation. Terminal runs are
        included while any Worker still owes something: a stop intent
        (``desired`` is not ``stopped``), a main observation that is not
        stopped/failed, or an incomplete activity enumeration with any
        internal activity not stopped. A restart between ``run.failed`` and
        the stop commit must not hide work that still needs cleanup, and
        neither must an acknowledged-but-unobserved stop. Filtering happens
        in SQL before LIMIT, so a long history of finished runs cannot hide
        an older active one.
        """
        R.require(not actor.roles.isdisjoint({"scheduler", "controller"}),
                  "unauthorized",
                  "listing reconciliation runs requires scheduler or controller")
        R.require_int(limit, "limit", minimum=1)
        R.require(limit <= 1000, "invalid_argument", "limit must be <= 1000")
        with self.store.read() as tx:
            rows = tx.all(
                "SELECT DISTINCT r.* FROM runs r"
                " LEFT JOIN workers w ON w.run_id = r.run_id"
                " WHERE r.status NOT IN ('completed', 'failed', 'cancelled')"
                " OR w.desired <> 'stopped'"
                " OR w.observed NOT IN ('stopped', 'failed')"
                " OR w.activities_complete = 0"
                " OR EXISTS (SELECT 1 FROM json_each(w.activities_json) a"
                "            WHERE json_extract(a.value, '$.observed')"
                "                  NOT IN ('stopped', 'failed'))"
                " ORDER BY r.created_at ASC, r.run_id ASC LIMIT ?",
                (limit,))
            return {
                "format": "agentgenome-control-plane-runs/1",
                "runs": [self._run_view(row) for row in rows
                         if actor.can_read(row["run_id"])],
            }

    def get_resource(self, actor: R.Actor, kind: str, identifier: str) -> dict[str, Any]:
        R.require_text(identifier, "resource id")
        with self.store.read() as tx:
            if kind == "definition":
                return self._definition_view(self._definition_row(tx, identifier))
            if kind == "run":
                return self._run_view(self._visible_run(tx, actor, identifier))
            if kind == "node":
                row = self._node(tx, identifier)
                actor.require_read(row["run_id"])
                view = self._node_view(row)
                view["acceptance"] = self._acceptance_brief(tx, row)
                status = self._visible_progress(
                    tx, actor, self._run_row(tx, row["run_id"]), row["node_id"])
                if status is not None and status.get("enabled"):
                    view["progress"] = status
                return view
            if kind == "worker":
                row = self._worker(tx, identifier)
                actor.require_read(row["run_id"])
                return self._worker_view(row)
            if kind == "candidate":
                row = self._candidate(tx, identifier)
                actor.require_read(row["run_id"])
                return self._candidate_view(row)
            if kind == "expansion_request":
                request = tx.one(
                    "SELECT * FROM expansion_requests WHERE expansion_id = ?",
                    (identifier,),
                )
                if request is None:
                    R.fail("not_found", "expansion request does not exist",
                           expansion_id=identifier)
                actor.require_read(request["run_id"])
                view = {"format": R.RESOURCE_FORMAT, "kind": "expansion_request",
                        "id": request["expansion_id"],
                        **self._expansion_request_view(request)}
                if request["state"] == "applied":
                    children = tx.all(
                        "SELECT local_child_id, child_node_id FROM expansion_children"
                        " WHERE expansion_id = ? ORDER BY local_child_id",
                        (identifier,),
                    )
                    view["children"] = [
                        {"local_child_id": child["local_child_id"],
                         "node_id": child["child_node_id"]} for child in children
                    ]
                return view
            if kind == "expansion":
                row = tx.one("SELECT * FROM expansions WHERE expansion_id = ?", (identifier,))
                if row is None:
                    R.fail("not_found", "expansion does not exist", expansion_id=identifier)
                actor.require_read(row["run_id"])
                children = tx.all(
                    "SELECT local_child_id, child_node_id FROM expansion_children"
                    " WHERE expansion_id = ? ORDER BY local_child_id",
                    (identifier,),
                )
                return {
                    "format": R.RESOURCE_FORMAT,
                    "kind": "expansion",
                    "id": row["expansion_id"],
                    "expansion_id": row["expansion_id"],
                    "run_id": row["run_id"],
                    "parent_node_id": row["parent_node_id"],
                    "join_rule": row["join_rule"],
                    "event_id": row["event_id"],
                    "children": [{"local_child_id": child["local_child_id"],
                                  "node_id": child["child_node_id"]} for child in children],
                    "created_at": row["created_at"],
                }
            if kind == "message":
                row = tx.one("SELECT * FROM messages WHERE message_id = ?", (identifier,))
                if row is None:
                    R.fail("not_found", "message does not exist", message_id=identifier)
                actor.require_read(row["run_id"])
                return self._message_view(row)
            if kind == "decision_request":
                row = tx.one("SELECT * FROM decision_requests WHERE decision_id = ?",
                             (identifier,))
                if row is None:
                    R.fail("not_found", "decision request does not exist",
                           decision_id=identifier)
                actor.require_read(row["run_id"])
                return self._decision_view(row)
            if kind == "artifact":
                row = tx.one("SELECT * FROM artifacts WHERE id = ?", (identifier,))
                if row is None:
                    R.fail("not_found", "artifact does not exist", id=identifier)
                if not self._artifact_visible(tx, actor, row):
                    R.fail("not_found", "artifact is not visible to this actor", id=identifier)
                return {"format": R.RESOURCE_FORMAT, "kind": "artifact", "id": row["id"],
                        "sha256": row["sha256"], "size": row["size"],
                        "media_type": row["media_type"], "created_by": row["created_by"],
                        "created_at": row["created_at"]}
            R.fail("invalid_argument", "unknown resource kind", kind=kind)

    def list_resources(self, actor: R.Actor, run_id: str) -> dict[str, Any]:
        with self.store.read() as tx:
            run = self._visible_run(tx, actor, run_id)
            nodes = tx.all("SELECT * FROM nodes WHERE run_id = ? ORDER BY created_at, node_id",
                           (run_id,))
            workers = tx.all("SELECT * FROM workers WHERE run_id = ? ORDER BY created_at, worker_id",
                             (run_id,))
            candidates = tx.all(
                "SELECT * FROM candidates WHERE run_id = ? ORDER BY created_at, candidate_id",
                (run_id,),
            )
            expansions = tx.all(
                "SELECT expansion_id FROM expansions WHERE run_id = ?"
                " ORDER BY created_at, expansion_id",
                (run_id,),
            )
            expansion_requests = tx.all(
                "SELECT expansion_id, state, reason_json FROM expansion_requests"
                " WHERE run_id = ? ORDER BY created_at, expansion_id",
                (run_id,),
            )
            reviews = tx.all(
                "SELECT r.*, e.worker_id AS reviewer_worker_id,"
                " e.epoch AS reviewer_epoch FROM reviews r"
                " JOIN events e ON e.event_id = r.event_id"
                " WHERE r.run_id = ? ORDER BY r.created_at, r.rowid",
                (run_id,),
            )
            decisions = tx.all(
                # Insertion order, not the model-chosen id, decides which
                # decision is a Node's current wait when timestamps tie.
                "SELECT * FROM decision_requests WHERE run_id = ?"
                " ORDER BY created_at, rowid",
                (run_id,),
            )
            node_views = []
            for node_row in nodes:
                view = self._node_view(node_row)
                view["acceptance"] = self._acceptance_brief(tx, node_row)
                status = self._visible_progress(
                    tx, actor, run, node_row["node_id"])
                if status is not None and status.get("enabled"):
                    view["progress"] = status
                node_views.append(view)
            return {
                "format": R.RESOURCE_FORMAT,
                "run": self._run_view(run),
                "nodes": node_views,
                "workers": [self._worker_view(row) for row in workers],
                "candidates": [self._candidate_view(row) for row in candidates],
                "expansions": [row["expansion_id"] for row in expansions],
                "expansion_requests": [{"expansion_id": row["expansion_id"],
                                        "state": row["state"],
                                        "reason": load(row["reason_json"])}
                                       for row in expansion_requests],
                "reviews": [{
                    "event_id": row["event_id"],
                    "run_id": row["run_id"],
                    "node_id": row["node_id"],
                    "candidate_id": row["candidate_id"],
                    "verdict": row["verdict"],
                    "feedback_ref": load(row["feedback_ref_json"]),
                    "reviewer": row["reviewer"],
                    "worker_id": row["reviewer_worker_id"],
                    "epoch": row["reviewer_epoch"],
                    "created_at": row["created_at"],
                } for row in reviews],
                "decision_requests": [self._decision_view(row) for row in decisions],
            }

    def latest_observation(self, actor: R.Actor, run_id: str,
                           worker_id: str) -> dict[str, Any] | None:
        R.require_text(worker_id, "worker_id", max_length=128)
        with self.store.read() as tx:
            self._visible_run(tx, actor, run_id)
            row = tx.one(
                "SELECT * FROM events WHERE run_id = ? AND worker_id = ?"
                " AND kind = 'worker_observed' ORDER BY seq DESC LIMIT 1",
                (run_id, worker_id),
            )
            if row is None:
                return None
            return {"event_id": row["event_id"], "seq": row["seq"],
                    "request_id": row["request_id"], "payload": load(row["payload_json"])}

    def observation_for(self, actor: R.Actor, run_id: str, worker_id: str,
                        delivery_key: str) -> dict[str, Any] | None:
        """Latest committed observation of one completed delivery.

        The Scheduler needs the evidence that a specific turn ended even after
        later deliveries moved the Worker's latest observation forward.
        """
        R.require_text(worker_id, "worker_id", max_length=128)
        R.require_text(delivery_key, "delivery_key", max_length=128)
        with self.store.read() as tx:
            self._visible_run(tx, actor, run_id)
            rows = tx.all(
                "SELECT * FROM events WHERE run_id = ? AND worker_id = ?"
                " AND kind = 'worker_observed' ORDER BY seq DESC",
                (run_id, worker_id),
            )
            for row in rows:
                payload = load(row["payload_json"])
                if payload.get("delivery_key") == delivery_key:
                    return {"event_id": row["event_id"], "seq": row["seq"],
                            "request_id": row["request_id"], "payload": payload}
            return None

    def read_events(self, actor: R.Actor, run_id: str, after: Any = 0,
                    limit: Any = 100) -> dict[str, Any]:
        cursor = _cursor(after)
        R.require_int(limit, "limit", minimum=1)
        R.require(limit <= 1000, "invalid_argument", "limit must be <= 1000")
        with self.store.read() as tx:
            run = self._visible_run(tx, actor, run_id)
            latest = run["event_seq"]
            if cursor > latest:
                R.fail("cursor_expired", "cursor is ahead of the event log; re-read the snapshot",
                       latest_seq=latest, snapshot_revision=run["revision"],
                       instruction="resnapshot")
            rows = tx.all(
                "SELECT * FROM events WHERE run_id = ? AND seq > ? ORDER BY seq LIMIT ?",
                (run_id, cursor, limit),
            )
            next_cursor = rows[-1]["seq"] if rows else cursor
            return {
                "format": "agentgenome-control-plane-events/1",
                "run_id": run_id,
                "events": [self._event_view(row) for row in rows],
                "next_cursor": next_cursor,
                "latest_seq": latest,
                "snapshot_revision": run["revision"],
            }

    # ------------------------------------------------------------------
    # durable worker inbox

    def enqueue_message(self, actor: R.Actor, worker_id: Any, message: Any,
                        delivery_key: Any, request_id: str) -> dict[str, Any]:
        """Scheduler-written durable inbox item. Delivery is a separate fact."""
        self._request_id(request_id)
        actor.require_role("scheduler")
        R.require_text(worker_id, "worker_id", max_length=128)
        R.require(isinstance(message, dict) and bool(message), "invalid_argument",
                  "message must be a nonempty object")
        R.require(R.is_json(message), "invalid_argument", "message must be valid JSON")
        R.require_text(delivery_key, "delivery_key", max_length=128)
        args = {"worker_id": worker_id, "message": message,
                "delivery_key": delivery_key}
        fingerprint = self._fingerprint(actor, "enqueue_message", args)
        with self.store.write() as tx:
            replay = tx.idempotency_lookup(actor.actor_id, "enqueue_message", request_id,
                                           fingerprint)
            if replay is not None:
                return replay
            worker = self._worker(tx, worker_id)
            run = self._visible_run(tx, actor, worker["run_id"])
            self._require_open_run(run)
            payload_digest = R.digest(message)
            existing = tx.one(
                "SELECT * FROM messages WHERE worker_id = ? AND epoch = ?"
                " AND delivery_key = ?",
                (worker["worker_id"], worker["epoch"], delivery_key),
            )
            if existing is not None:
                if existing["payload_digest"] != payload_digest:
                    R.fail("idempotency_conflict",
                           "delivery_key was reused with a different message",
                           delivery_key=delivery_key)
                event = tx.one("SELECT * FROM events WHERE event_id = ?",
                               (existing["enqueue_event_id"],))
                return {"message": self._message_view(existing), "event_id":
                        existing["enqueue_event_id"], "seq": event["seq"],
                        "duplicate": True}
            self._check_refs(tx, actor, _artifact_refs(message))
            message_id = new_id()
            timestamp = now()
            event_id, seq = tx.append_event(
                run_id=run["run_id"], kind="message_enqueued",
                node_id=worker["node_id"], worker_id=worker["worker_id"],
                epoch=worker["epoch"], actor_id=actor.actor_id, request_id=request_id,
                payload={"message_id": message_id, "delivery_key": delivery_key,
                         "payload_digest": payload_digest},
            )
            tx.execute(
                "INSERT INTO messages (message_id, run_id, worker_id, node_id, epoch,"
                " delivery_key, payload_json, payload_digest, created_by, delivery_state,"
                " revision, enqueue_event_id, delivery_receipt_json, delivery_event_id,"
                " delivered_by, created_at, delivered_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 1, ?, NULL, NULL, NULL,"
                " ?, NULL)",
                (message_id, run["run_id"], worker["worker_id"], worker["node_id"],
                 worker["epoch"], delivery_key, dump(message), payload_digest,
                 actor.actor_id, event_id, timestamp),
            )
            row = tx.one("SELECT * FROM messages WHERE message_id = ?", (message_id,))
            response = {"message": self._message_view(row), "event_id": event_id,
                        "seq": seq, "duplicate": False}
            tx.idempotency_store(actor.actor_id, "enqueue_message", request_id,
                                 fingerprint, response)
            return response

    def list_inbox(self, actor: R.Actor, worker_id: Any) -> dict[str, Any]:
        R.require_text(worker_id, "worker_id", max_length=128)
        with self.store.read() as tx:
            worker = self._worker(tx, worker_id)
            self._visible_run(tx, actor, worker["run_id"])
            rows = tx.all(
                "SELECT * FROM messages WHERE worker_id = ? ORDER BY created_at, rowid",
                (worker["worker_id"],),
            )
            return {
                "format": "agentgenome-control-plane-inbox/1",
                "run_id": worker["run_id"],
                "worker_id": worker["worker_id"],
                "node_id": worker["node_id"],
                "epoch": worker["epoch"],
                "messages": [self._message_view(row) for row in rows],
            }

    def record_message_delivery(self, actor: R.Actor, message_id: Any, receipt: Any,
                                request_id: str) -> dict[str, Any]:
        """Controller commits that a pending inbox item reached the Backend."""
        self._request_id(request_id)
        actor.require_role("controller")
        R.require_text(message_id, "message_id", max_length=128)
        R.require(isinstance(receipt, dict), "invalid_argument", "receipt must be an object")
        R.require(R.is_json(receipt), "invalid_argument", "receipt must be valid JSON")
        delivery_key = receipt.get("delivery_key")
        R.require_text(delivery_key, "receipt.delivery_key", max_length=128)
        args = {"message_id": message_id, "receipt": receipt}
        fingerprint = self._fingerprint(actor, "record_message_delivery", args)
        with self.store.write() as tx:
            replay = tx.idempotency_lookup(actor.actor_id, "record_message_delivery",
                                           request_id, fingerprint)
            if replay is not None:
                return replay
            row = tx.one("SELECT * FROM messages WHERE message_id = ?", (message_id,))
            if row is None:
                R.fail("not_found", "message does not exist", message_id=message_id)
            worker = self._worker(tx, row["worker_id"], row["run_id"])
            self._visible_run(tx, actor, worker["run_id"])
            if (actor.controller_id != worker["controller_id"] or
                    actor.epoch != worker["epoch"] or row["epoch"] != worker["epoch"]):
                R.fail("stale_epoch", "message does not belong to this controller epoch",
                       current_epoch=worker["epoch"], message_epoch=row["epoch"])
            if row["delivery_key"] != delivery_key:
                R.fail("invalid_argument", "receipt delivery_key does not match the message",
                       message_delivery_key=row["delivery_key"])
            if row["delivery_state"] == "superseded":
                R.fail("stale_epoch",
                       "the message belongs to a superseded execution epoch",
                       message_id=row["message_id"],
                       delivery_key=row["delivery_key"])
            if row["delivery_state"] == "delivered":
                event = tx.one("SELECT * FROM events WHERE event_id = ?",
                               (row["delivery_event_id"],))
                response = {"message_id": row["message_id"],
                            "delivery_key": row["delivery_key"],
                            "delivery_state": "delivered",
                            "revision": row["revision"],
                            "event_id": row["delivery_event_id"],
                            "seq": event["seq"], "duplicate": True}
                tx.idempotency_store(actor.actor_id, "record_message_delivery",
                                     request_id, fingerprint, response)
                return response
            event_id, seq = tx.append_event(
                run_id=row["run_id"], kind="message_delivered", node_id=row["node_id"],
                worker_id=row["worker_id"], epoch=row["epoch"],
                actor_id=actor.actor_id, request_id=request_id,
                payload={"message_id": row["message_id"],
                         "delivery_key": row["delivery_key"], "receipt": receipt},
            )
            revision = row["revision"] + 1
            tx.execute(
                "UPDATE messages SET delivery_state = 'delivered', delivered_by = ?,"
                " delivery_receipt_json = ?, delivery_event_id = ?, delivered_at = ?,"
                " revision = ? WHERE message_id = ?",
                (actor.actor_id, dump(receipt), event_id, now(), revision,
                 row["message_id"]),
            )
            response = {"message_id": row["message_id"],
                        "delivery_key": row["delivery_key"],
                        "delivery_state": "delivered", "revision": revision,
                        "event_id": event_id, "seq": seq, "duplicate": False}
            tx.idempotency_store(actor.actor_id, "record_message_delivery", request_id,
                                 fingerprint, response)
            return response

    def supersede_inbox(self, actor: R.Actor, worker_id: Any, epoch: Any,
                        request_id: str) -> dict[str, Any]:
        """Retire undelivered work of an execution that will be replaced.

        This is an explicit, auditable fact, never a fake delivery. Only the
        Scheduler commits it after the old execution is completely stopped and
        its applied local contract is already the Node's current version.
        """
        self._request_id(request_id)
        actor.require_role("scheduler")
        R.require_text(worker_id, "worker_id", max_length=128)
        # Worker epochs are zero-based: the first bound execution is epoch 0.
        R.require_int(epoch, "epoch", minimum=0)
        args = {"worker_id": worker_id, "epoch": epoch}
        fingerprint = self._fingerprint(actor, "supersede_inbox", args)
        with self.store.write() as tx:
            replay = tx.idempotency_lookup(actor.actor_id, "supersede_inbox",
                                           request_id, fingerprint)
            if replay is not None:
                return replay
            worker = self._worker(tx, worker_id)
            run = self._visible_run(tx, actor, worker["run_id"])
            if worker["epoch"] != epoch:
                R.fail("stale_epoch",
                       "only the current execution epoch may be superseded",
                       current_epoch=worker["epoch"], requested_epoch=epoch)
            node = self._node(tx, worker["node_id"], run["run_id"])
            history = self._acceptance_history(tx, node)
            if not any(entry["state"] == "applied"
                       and entry["contract_ref"] == node["contract_ref"]
                       for entry in history):
                R.fail("candidate_mismatch",
                       "the node has no applied local contract to rebind",
                       node_id=node["node_id"])
            if worker["desired"] != "stopped":
                R.fail("invalid_argument",
                       "only an execution already stopping may be superseded",
                       worker_id=worker["worker_id"])
            context = load(worker["context_json"]) or {}
            if context.get("contract_ref") == node["contract_ref"]:
                R.fail("candidate_mismatch",
                       "the execution already holds the current contract",
                       worker_id=worker["worker_id"])
            if not execution_stopped(self._worker_view(worker)):
                R.fail("invalid_argument",
                       "the superseded execution has not been observed stopped",
                       worker_id=worker["worker_id"])
            rows = tx.all(
                "SELECT * FROM messages WHERE worker_id = ? AND epoch = ?"
                " AND delivery_state = 'pending' ORDER BY created_at, rowid",
                (worker["worker_id"], epoch))
            keys: list[str] = []
            for row in rows:
                tx.append_event(
                    run_id=run["run_id"], kind=MESSAGE_SUPERSEDED,
                    node_id=row["node_id"], worker_id=row["worker_id"],
                    epoch=row["epoch"], actor_id=actor.actor_id,
                    request_id=request_id,
                    payload={"message_id": row["message_id"],
                             "delivery_key": row["delivery_key"]},
                )
                tx.execute(
                    "UPDATE messages SET delivery_state = 'superseded',"
                    " revision = revision + 1 WHERE message_id = ?",
                    (row["message_id"],),
                )
                keys.append(row["delivery_key"])
            response = {"worker_id": worker["worker_id"], "epoch": epoch,
                        "superseded": keys, "count": len(keys)}
            tx.idempotency_store(actor.actor_id, "supersede_inbox", request_id,
                                 fingerprint, response)
            return response

    # ------------------------------------------------------------------
    # facts

    def append_fact(self, actor: R.Actor, fact: Any, request_id: str,
                    expected_revision: Any) -> dict[str, Any]:
        self._request_id(request_id)
        item = R.validate_fact(fact)
        R.require_int(expected_revision, "expected_revision", minimum=1)
        role = {"candidate_submitted": "worker", "review_submitted": "reviewer",
                "expansion_requested": "worker", "decision_requested": "worker",
                "decision_resolved": "worker", "worker_observed": "controller",
                "acceptance_revision_requested": "worker"}[item["kind"]]
        actor.require_role(role)
        args = {"fact": item}
        fingerprint = self._fingerprint(actor, "append_fact", args)
        with self.store.write() as tx:
            replay = tx.idempotency_lookup(actor.actor_id, "append_fact", request_id,
                                           fingerprint)
            if replay is not None:
                return replay
            run = self._visible_run(tx, actor, item["run_id"])
            kind = item["kind"]
            # An observation is an execution fact, not business progress: a
            # cancelled or expired Run still needs the stop confirmation to
            # be recorded. Candidates, reviews and expansions stay closed.
            if kind != "worker_observed":
                self._require_open_run(run)
            if kind == "candidate_submitted":
                response = self._append_candidate(tx, actor, run, item, request_id,
                                                  expected_revision)
            elif kind == "review_submitted":
                response = self._append_review(tx, actor, run, item, request_id,
                                               expected_revision)
            elif kind == "expansion_requested":
                response = self._append_expansion(tx, actor, run, item, request_id,
                                                  expected_revision)
            elif kind == "decision_requested":
                response = self._append_decision_request(tx, actor, run, item, request_id,
                                                         expected_revision)
            elif kind == "decision_resolved":
                response = self._append_decision_resolution(tx, actor, run, item,
                                                            request_id, expected_revision)
            elif kind == "acceptance_revision_requested":
                response = self._append_acceptance_revision(
                    tx, actor, run, item, request_id, expected_revision)
            else:
                response = self._append_observation(tx, actor, run, item, request_id,
                                                    expected_revision)
            tx.idempotency_store(actor.actor_id, "append_fact", request_id, fingerprint,
                                 response)
            return response

    def _append_candidate(self, tx: Tx, actor: R.Actor, run: Any, item: dict[str, Any],
                          request_id: str, expected_revision: int) -> dict[str, Any]:
        actor.require_role("worker")
        if actor.worker_id is None or actor.epoch is None:
            R.fail("unauthorized", "worker identity is required for a candidate")
        node = self._node(tx, item["node_id"], run["run_id"])
        worker = self._worker(tx, item["worker_id"], run["run_id"])
        if worker["node_id"] != node["node_id"]:
            R.fail("unauthorized", "worker is not bound to this node")
        if item["epoch"] != worker["epoch"] or actor.epoch != worker["epoch"]:
            R.fail("stale_epoch", "candidate epoch is not current",
                   current_epoch=worker["epoch"])
        if actor.worker_id != worker["worker_id"]:
            R.fail("unauthorized", "worker identity does not match the payload")
        self._check_revision(node, expected_revision, "node", node["node_id"])
        if item["contract_ref"] != node["contract_ref"]:
            R.fail("candidate_mismatch",
                   "candidate contract does not match the frozen node contract",
                   contract_ref=item["contract_ref"],
                   node_contract_ref=node["contract_ref"])
        existing = tx.one("SELECT * FROM candidates WHERE candidate_id = ?",
                          (item["candidate_id"],))
        if existing is not None:
            same = (existing["node_id"] == node["node_id"] and
                    existing["worker_id"] == worker["worker_id"] and
                    existing["epoch"] == item["epoch"] and
                    existing["round"] == item["round"] and
                    load(existing["output_refs_json"]) == item["output_refs"] and
                    existing["contract_ref"] == item["contract_ref"] and
                    load(existing["summary_ref_json"]) == item.get("summary_ref"))
            if not same:
                R.fail("idempotency_conflict", "candidate_id was reused with different content",
                       candidate_id=item["candidate_id"])
            event = tx.one("SELECT * FROM events WHERE event_id = ?", (existing["event_id"],))
            return {"candidate_id": existing["candidate_id"], "event_id": existing["event_id"],
                    "seq": event["seq"], "state": node["state"],
                    "revision": node["revision"], "round": existing["round"],
                    "duplicate": True}
        self._check_refs(tx, actor, item["output_refs"])
        if item.get("summary_ref") is not None:
            self._check_refs(tx, actor, [item["summary_ref"]])
        if node["state"] != "running":
            R.fail("candidate_mismatch", "a candidate requires a running node",
                   state=node["state"])
        if any(entry["state"] == "pending"
               for entry in self._acceptance_history(tx, node)):
            R.fail("candidate_mismatch",
                   "a pending acceptance revision owns the node's next step",
                   node_id=node["node_id"])
        # An "all" join makes every expansion child a required part of this
        # Node's result. A candidate submitted while one is open would become
        # a parent result without that child, so the submission is refused
        # explicitly; the Node stays running and may review or wait for the
        # children, then submit once the join closes.
        self._check_required_children_closed(tx, node, "submit a candidate")
        # The current round's candidate is still an unapplied fact, or its
        # acceptance is unapplied; a second candidate must not bypass it.
        # Node state alone cannot see a fact whose Scheduler application has
        # not run yet, so the fact is checked here as well. The partial unique
        # index on undecided candidates backs this up at the storage layer.
        current = tx.one(
            "SELECT candidate_id, decision, contract_ref FROM candidates"
            " WHERE node_id = ? AND round = ?"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (node["node_id"], node["round"]),
        )
        if current is not None and current["decision"] in (None, "accept"):
            R.fail("candidate_mismatch",
                   "the node's current candidate still owns its review path",
                   candidate_id=current["candidate_id"],
                   decision=current["decision"])
        if (current is not None and current["decision"] in ("revise", "expand")
                and current["contract_ref"] == node["contract_ref"]):
            # The previous round's feedback must reach this execution before a
            # new candidate can bypass it. Enqueueing is not consumption: the
            # Controller's durable delivery fact for this Worker epoch is the
            # boundary. Without it, a queued ordinary turn could submit ahead
            # of the feedback that the next round is supposed to consume. A
            # revised contract replaces that execution; the new turn carries
            # the old feedback in its own acceptance_revised delivery instead.
            feedback_key = L.revision_key(current["candidate_id"],
                                          current["decision"])
            delivered = tx.one(
                "SELECT 1 FROM messages WHERE worker_id = ? AND epoch = ?"
                " AND delivery_key = ? AND delivery_state = 'delivered'",
                (worker["worker_id"], worker["epoch"], feedback_key),
            )
            if delivered is None:
                R.fail("feedback_pending",
                       "the previous review feedback has not been delivered",
                       candidate_id=current["candidate_id"],
                       verdict=current["decision"],
                       delivery_key=feedback_key)
        if item["round"] != node["round"] + 1:
            R.fail("candidate_mismatch", "candidate round must advance by exactly one",
                   current_round=node["round"], requested_round=item["round"])
        self._check_budget_round(run, item["round"])
        resolved = self._resolve_acceptance_contract(tx, actor, run, node)
        upper = resolved["upper"]
        local = resolved["contract"]
        R.validate_schema_value(upper["output_schema"], item["output_refs"],
                                "output_refs")
        R.validate_schema_value(local["output_schema"], item["output_refs"],
                                "output_refs")
        event_id, seq = tx.append_event(
            run_id=run["run_id"], kind="candidate_submitted", node_id=node["node_id"],
            worker_id=worker["worker_id"], epoch=item["epoch"], actor_id=actor.actor_id,
            request_id=request_id, payload=item,
        )
        tx.execute(
            "INSERT INTO candidates (candidate_id, run_id, node_id, worker_id, epoch, round,"
            " output_refs_json, contract_ref, summary_ref_json, event_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item["candidate_id"], run["run_id"], node["node_id"], worker["worker_id"],
             item["epoch"], item["round"], dump(item["output_refs"]), item["contract_ref"],
             dump(item.get("summary_ref")), event_id, now()),
        )
        # The round is reserved in the same transaction so uniqueness survives
        # the fact/state split. The Node state itself is applied by the
        # Scheduler from this committed fact, which also bumps the revision so
        # any plan read before the fact is stale.
        new_revision = node["revision"] + 1
        tx.execute(
            "UPDATE nodes SET round = ?, revision = ?, updated_at = ? WHERE node_id = ?",
            (item["round"], new_revision, now(), node["node_id"]),
        )
        return {"candidate_id": item["candidate_id"], "event_id": event_id, "seq": seq,
                "state": node["state"], "revision": new_revision,
                "round": item["round"], "applied": False}

    def _check_reviewer_binding(self, tx: Tx, actor: R.Actor, run: Any, node: Any,
                                candidate_id: str) -> None:
        """Require the exact Agent execution bound to review this candidate.

        A parent reviews its own child through the parent Worker that accepted
        the child installation. A declared checker reviews only through the
        review node whose closure names this target, candidate and Worker.
        A bare name, a stale epoch or another execution can never review.
        """
        worker = tx.one("SELECT * FROM workers WHERE worker_id = ?",
                        (actor.worker_id or actor.actor_id,))
        if worker is None:
            R.fail("unauthorized",
                   "review requires a bound worker identity",
                   reviewer_ref=node["reviewer_ref"])
        if actor.worker_id is None or actor.worker_id != worker["worker_id"]:
            R.fail("unauthorized",
                   "a Worker reviewer must present its bound worker identity",
                   worker_id=worker["worker_id"])
        if actor.actor_id != worker["worker_id"]:
            # The execution identity is the actual Worker id. A Task/Agent
            # reference may name the role, but it cannot act as the Worker.
            R.fail("unauthorized",
                   "the review Actor must be the actual Worker identity",
                   actor_id=actor.actor_id, worker_id=worker["worker_id"])
        if worker["run_id"] != run["run_id"]:
            R.fail("unauthorized", "reviewer worker belongs to another run",
                   worker_id=worker["worker_id"])
        if actor.epoch != worker["epoch"]:
            R.fail("stale_epoch", "reviewer epoch is not current",
                   current_epoch=worker["epoch"], actor_epoch=actor.epoch)
        is_parent = (node["parent_id"] is not None
                     and worker["node_id"] == node["parent_id"]
                     and node["reviewer_ref"] == worker["worker_id"])
        if is_parent:
            return
        for review in tx.all(
                "SELECT * FROM nodes WHERE run_id = ? AND closure_json IS NOT NULL",
                (run["run_id"],)):
            closure = load(review["closure_json"]) or {}
            if (review["node_id"] == worker["node_id"]
                    and review["template_ref"] == node["reviewer_ref"]
                    and closure.get("kind") == "review_receipt"
                    and closure.get("target_node_id") == node["node_id"]
                    and closure.get("candidate_id") == candidate_id
                    and closure.get("worker_id") == worker["worker_id"]
                    and closure.get("reviewer_ref") == node["reviewer_ref"]):
                return
        R.fail("unauthorized", "reviewer worker is not bound to this candidate",
               worker_id=worker["worker_id"], node_id=node["node_id"])



    @staticmethod
    def _current_undecided_candidate(tx: Tx, node: Any) -> Any:
        """A candidate fact whose Scheduler application may still be pending.

        Only the Node's current contract version counts: a superseded version's
        candidate is history and must not block the replacement execution.
        """
        return tx.one(
            "SELECT candidate_id FROM candidates WHERE node_id = ? AND round = ?"
            " AND contract_ref = ? AND decision IS NULL"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (node["node_id"], node["round"], node["contract_ref"]),
        )

    @staticmethod
    def _check_required_children_closed(tx: Tx, node: Any,
                                       operation: str = "accept") -> None:
        rows = tx.all(
            "SELECT node_id, state FROM nodes WHERE parent_id = ?"
            " AND expansion_id IS NOT NULL AND closure_json IS NULL"
            " ORDER BY node_id", (node["node_id"],))
        open_children = [{"node_id": row["node_id"], "state": row["state"]}
                         for row in rows if row["state"] != "succeeded"]
        if open_children:
            R.fail("required_children_open",
                   f"cannot {operation} while required expansion children are open",
                   node_id=node["node_id"], operation=operation,
                   children=open_children)

    def _append_review(self, tx: Tx, actor: R.Actor, run: Any, item: dict[str, Any],
                       request_id: str, expected_revision: int) -> dict[str, Any]:
        """Record one review decision fact; the Scheduler applies task state."""
        actor.require_role("reviewer")
        node = self._node(tx, item["node_id"], run["run_id"])
        self._check_reviewer_binding(tx, actor, run, node, item["candidate_id"])
        self._check_revision(node, expected_revision, "node", node["node_id"])
        candidate = self._candidate(tx, item["candidate_id"])
        if candidate["node_id"] != node["node_id"] or candidate["run_id"] != run["run_id"]:
            R.fail("candidate_mismatch", "candidate does not belong to this node",
                   candidate_id=item["candidate_id"])
        if candidate["round"] != item["candidate_round"]:
            R.fail("candidate_mismatch", "candidate round does not match",
                   stored_round=candidate["round"],
                   requested_round=item["candidate_round"])
        if candidate["decision"] is not None:
            R.fail("candidate_mismatch", "candidate already has a terminal decision",
                   candidate_id=item["candidate_id"], decision=candidate["decision"])
        if node["state"] != "awaiting_review":
            R.fail("candidate_mismatch", "node is not awaiting review",
                   state=node["state"])
        if candidate["round"] != node["round"]:
            R.fail("candidate_mismatch", "candidate is not the node's current round",
                   candidate_round=candidate["round"], node_round=node["round"])
        self._check_refs(tx, actor, [item["feedback_ref"]])
        resolved = self._resolve_acceptance_contract(tx, actor, run, node)
        bundle = resolved["bundle"]
        task = resolved["task"]
        if node["input_refs_json"] is not None:
            inputs = load(node["input_refs_json"])
            R.validate_schema_value(resolved["upper"]["input_schema"], inputs,
                                    "input_refs")
            R.validate_schema_value(resolved["contract"]["input_schema"], inputs,
                                    "input_refs")
        verdict = item["verdict"]
        if verdict == "accept":
            self._check_required_children_closed(tx, node)
            output_refs = load(candidate["output_refs_json"])
            R.validate_schema_value(resolved["upper"]["output_schema"], output_refs,
                                    "output_refs")
            R.validate_schema_value(resolved["contract"]["output_schema"], output_refs,
                                    "output_refs")
            self._check_refs(tx, actor, output_refs)
        elif verdict == "expand":
            self._check_refs(tx, actor, _artifact_refs(item["proposal"]))
        review_task = bundle.tasks.get(node["reviewer_ref"])
        if review_task is not None:
            R.require(review_task.completion_kind == "review_receipt",
                      "invalid_argument",
                      "the node reviewer is not a review_receipt task",
                      reviewer_ref=node["reviewer_ref"])
            R.validate_schema_value(
                review_task.contract["output_schema"],
                {"verdict": verdict,
                 "candidate_id": item["candidate_id"],
                 "candidate_round": item["candidate_round"],
                 "feedback_ref": item["feedback_ref"]},
                "review_receipt")
        evaluation: dict[str, Any] | None = None
        if task.progress is None:
            R.require("progress" not in item, "invalid_argument",
                      "the node does not declare a progress policy",
                      node_id=node["node_id"])
        else:
            R.require("progress" in item, "invalid_argument",
                      "the node's progress policy requires a review evaluation",
                      node_id=node["node_id"])
            evaluator_ref = self._progress_evaluator_ref(tx, run, node, bundle)
            comparison_ref = self._progress_comparison_ref(node, evaluator_ref)
            evaluation = validate_progress_evaluation({
                "evaluation_id": self._progress_evaluation_id(
                    node["node_id"], item["candidate_id"]),
                "node_id": node["node_id"],
                "checkpoint_id": item["candidate_id"],
                "evaluator_ref": evaluator_ref,
                "contract_ref": node["contract_ref"],
                "comparison_ref": comparison_ref,
                "evidence_ref": item["feedback_ref"],
                "result": item["progress"]["result"],
            }).as_dict()
        event_payload = dict(item)
        if evaluation is not None:
            event_payload["progress"] = evaluation
        event_id, seq = tx.append_event(
            run_id=run["run_id"], kind="review_submitted", node_id=node["node_id"],
            worker_id=actor.worker_id, epoch=actor.epoch, actor_id=actor.actor_id,
            request_id=request_id, payload=event_payload,
        )
        tx.execute(
            "INSERT INTO reviews (event_id, run_id, node_id, candidate_id, verdict,"
            " feedback_ref_json, reviewer, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (event_id, run["run_id"], node["node_id"], item["candidate_id"], verdict,
             dump(item["feedback_ref"]), actor.actor_id, now()),
        )
        tx.execute(
            "UPDATE candidates SET decision = ?, decision_event_id = ?"
            " WHERE candidate_id = ?",
            (verdict, event_id, item["candidate_id"]),
        )
        # The decision is a committed fact. Succeeded/accepted_candidate_ref or
        # the return to running is applied by the Scheduler; the revision bump
        # makes a plan read before this fact stale.
        new_revision = node["revision"] + 1
        tx.execute(
            "UPDATE nodes SET revision = ?, updated_at = ? WHERE node_id = ?",
            (new_revision, now(), node["node_id"]),
        )
        response: dict[str, Any] = {
            "event_id": event_id, "seq": seq, "verdict": verdict,
            "candidate_id": item["candidate_id"], "state": node["state"],
            "revision": new_revision, "applied": False,
        }
        if evaluation is not None:
            response["progress"] = evaluation
        if verdict == "expand":
            request = self._record_expansion_request(
                tx, run, node, item["proposal"], source="reviewer",
                source_event_id=event_id,
            )
            response.update({"expansion_id": request["expansion_id"],
                             "expansion_state": request["state"]})
        return response

    @staticmethod
    def _expansion_payload(proposal: dict[str, Any]) -> dict[str, Any]:
        payload = {key: proposal[key] for key in ("expansion_id", "children", "join_rule")}
        if proposal.get("contract_ref") is not None:
            payload["contract_ref"] = proposal["contract_ref"]
        return payload

    def _append_expansion(self, tx: Tx, actor: R.Actor, run: Any, item: dict[str, Any],
                          request_id: str, expected_revision: int) -> dict[str, Any]:
        actor.require_role("worker")
        if actor.worker_id is None or actor.epoch is None:
            R.fail("unauthorized", "worker identity is required for an expansion")
        node = self._node(tx, item["node_id"], run["run_id"])
        worker = self._worker(tx, actor.worker_id, run["run_id"])
        if worker["node_id"] != node["node_id"]:
            R.fail("unauthorized", "worker is not bound to this node")
        if actor.epoch != worker["epoch"]:
            R.fail("stale_epoch", "expansion epoch is not current",
                   current_epoch=worker["epoch"])
        if worker["desired"] == "stopped":
            R.fail("invalid_argument",
                   "a stopping execution may not expand its task",
                   worker_id=worker["worker_id"])
        context = load(worker["context_json"]) or {}
        bound_contract = context.get("contract_ref")
        requested_contract = item.get("contract_ref")
        if bound_contract is None or requested_contract is None:
            R.fail("invalid_argument",
                   "an expansion requires its bound contract version",
                   worker_id=worker["worker_id"])
        if requested_contract != bound_contract:
            R.fail("candidate_mismatch",
                   "the expansion was bound to a different contract version",
                   execution_contract_ref=bound_contract,
                   requested_contract_ref=requested_contract)
        if bound_contract != node["contract_ref"]:
            R.fail("candidate_mismatch",
                   "the expansion execution does not hold the current contract",
                   execution_contract_ref=bound_contract,
                   node_contract_ref=node["contract_ref"])
        payload = self._expansion_payload(item)
        existing = tx.one("SELECT * FROM expansion_requests WHERE expansion_id = ?",
                          (payload["expansion_id"],))
        if existing is not None:
            if existing["payload_digest"] != R.digest(payload):
                R.fail("idempotency_conflict", "expansion_id was reused with different content",
                       expansion_id=payload["expansion_id"])
            event = tx.one("SELECT * FROM events WHERE event_id = ?",
                           (existing["source_event_id"],))
            return {"event_id": existing["source_event_id"], "seq": event["seq"],
                    "expansion_id": existing["expansion_id"], "state": existing["state"],
                    "applied": existing["state"] == "applied",
                    "revision": node["revision"], "duplicate": True}
        self._check_revision(node, expected_revision, "node", node["node_id"])
        if node["state"] != "running":
            R.fail("candidate_mismatch", "an expansion requires a running node",
                   state=node["state"])
        if any(entry["state"] == "pending"
               for entry in self._acceptance_history(tx, node)):
            R.fail("candidate_mismatch",
                   "a pending acceptance revision owns the node's next step",
                   node_id=node["node_id"])
        pending = self._current_undecided_candidate(tx, node)
        if pending is not None:
            # The old state-only path put the Node in awaiting_review before a
            # following request could arrive. With the fact persisted first,
            # the undecided candidate must itself block the same request.
            R.fail("candidate_mismatch",
                   "an undecided candidate owns the node's next step",
                   candidate_id=pending["candidate_id"])
        self._check_refs(tx, actor, _artifact_refs(payload))
        event_id, seq = tx.append_event(
            run_id=run["run_id"], kind="expansion_requested", node_id=node["node_id"],
            worker_id=worker["worker_id"], epoch=actor.epoch, actor_id=actor.actor_id,
            request_id=request_id, payload=item,
        )
        request = self._record_expansion_request(
            tx, run, node, payload, source="worker", source_event_id=event_id,
        )
        return {"event_id": event_id, "seq": seq,
                "expansion_id": request["expansion_id"], "state": "requested",
                "applied": False, "revision": node["revision"]}

    @staticmethod
    def _expansion_request_view(row: Any) -> dict[str, Any]:
        return {"expansion_id": row["expansion_id"], "run_id": row["run_id"],
                "node_id": row["node_id"], "source": row["source"],
                "payload_digest": row["payload_digest"],
                "payload": load(row["payload_json"]), "state": row["state"],
                "reason": load(row["reason_json"]),
                "source_event_id": row["source_event_id"],
                "applied_at": row["applied_at"], "created_at": row["created_at"],
                "updated_at": row["updated_at"]}

    def _record_expansion_request(self, tx: Tx, run: Any, node: Any,
                                  proposal: dict[str, Any], *, source: str,
                                  source_event_id: str) -> dict[str, Any]:
        payload = self._expansion_payload(proposal)
        digest = R.digest(payload)
        existing = tx.one("SELECT * FROM expansion_requests WHERE expansion_id = ?",
                          (payload["expansion_id"],))
        if existing is not None:
            if (existing["payload_digest"] != digest or
                    existing["node_id"] != node["node_id"] or
                    existing["run_id"] != run["run_id"]):
                R.fail("idempotency_conflict", "expansion_id was reused with different content",
                       expansion_id=payload["expansion_id"])
            return self._expansion_request_view(existing)
        timestamp = now()
        tx.execute(
            "INSERT INTO expansion_requests (expansion_id, run_id, node_id, source,"
            " payload_digest, payload_json, state, source_event_id, applied_at,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'requested', ?, NULL, ?, ?)",
            (payload["expansion_id"], run["run_id"], node["node_id"], source, digest,
             dump(payload), source_event_id, timestamp, timestamp),
        )
        return {"expansion_id": payload["expansion_id"], "run_id": run["run_id"],
                "node_id": node["node_id"], "source": source, "payload_digest": digest,
                "state": "requested", "source_event_id": source_event_id,
                "applied_at": None, "created_at": timestamp, "updated_at": timestamp}

    def _insert_expansion(self, tx: Tx, run: Any, parent: Any,
                          proposal: dict[str, Any],
                          source_event_id: str) -> list[dict[str, str]]:
        """Materialize every approved child of one applied expansion.

        Child task refs must already be part of the run's frozen library;
        identities, contract refs and frozen input refs come from that library
        and committed state, never from the proposal. Only the bounded IDs and
        the declared narrowing fields are taken from the proposal.
        """
        bundle = self._definition_bundle_for_run(
            tx, R.Actor("expansion", {"scheduler"}, run_ids={run["run_id"]}),
            run)
        parent_worker = tx.one(
            "SELECT * FROM workers WHERE node_id = ? AND run_id = ?"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (parent["node_id"], run["run_id"]))
        children = proposal["children"]
        self._check_budget_nodes(tx, run, len(children))
        self._check_budget_depth(tx, run, parent)
        identifiers = {
            child["local_child_id"]: "exp-" + hashlib.sha256(
                "\x00".join(("agentgenome-control-plane/expansion-child/1",
                             run["run_id"], parent["node_id"],
                             proposal["expansion_id"],
                             child["local_child_id"])).encode("utf-8")
            ).hexdigest()[:32]
            for child in children
        }
        timestamp = now()
        tx.execute(
            "INSERT INTO expansions (expansion_id, run_id, parent_node_id, join_rule,"
            " payload_digest, event_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (proposal["expansion_id"], run["run_id"], parent["node_id"],
             proposal["join_rule"], R.digest(self._expansion_payload(proposal)),
             source_event_id, timestamp),
        )
        created: list[dict[str, str]] = []
        for child in children:
            local = child["local_child_id"]
            # A dynamic child may omit the task selection and inherit the
            # parent's approved Task; an explicit selection must still name an
            # approved task and its initial contract equals that task ref.
            task_ref = child.get("template_ref") or parent["template_ref"]
            R.require(child.get("contract_ref") in (None, task_ref),
                      "invalid_argument",
                      "an expansion child contract_ref must equal its task ref",
                      local_child_id=local)
            task = bundle.tasks.get(task_ref)
            if task is None:
                R.fail("not_found",
                       "the expansion child task is not in the run library",
                       local_child_id=local, task_ref=task_ref)
            R.require(task.completion_kind == "candidate", "invalid_argument",
                      "an expansion child must be a candidate-mode task",
                      local_child_id=local, task_ref=task_ref)
            reviewer = child.get("reviewer_ref")
            if reviewer is None or reviewer == "parent":
                if parent_worker is None:
                    R.fail("reviewer_undeclared",
                           "the parent reviewer has no Worker yet",
                           parent_id=parent["node_id"])
                reviewer_ref = parent_worker["worker_id"]
            else:
                reviewer_task = bundle.tasks.get(reviewer)
                if (reviewer_task is None or
                        reviewer_task.completion_kind != "review_receipt"):
                    R.fail("reviewer_undeclared",
                           "an expansion reviewer must be an approved "
                           "review_receipt task",
                           local_child_id=local, reviewer_ref=reviewer)
                reviewer_ref = reviewer
            dependencies = [identifiers[name]
                            for name in child.get("dependencies") or []]
            tx.execute(
                "INSERT INTO nodes (node_id, run_id, parent_id, expansion_id,"
                " local_child_id, template_ref, contract_ref, input_bindings_json,"
                " input_refs_json, delegation_json, dependencies_json, reviewer_ref,"
                " closure_json, state, round, accepted_candidate_ref, revision,"
                " created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, NULL, 'pending',"
                " 0, NULL, 1, ?, ?)",
                (identifiers[local], run["run_id"], parent["node_id"],
                 proposal["expansion_id"], local, task_ref, task_ref,
                 dump(child["input_bindings"]), dump(child.get("delegation") or {}),
                 dump(dependencies), reviewer_ref, timestamp, timestamp),
            )
            tx.execute(
                "INSERT INTO expansion_children (expansion_id, local_child_id,"
                " child_node_id) VALUES (?, ?, ?)",
                (proposal["expansion_id"], local, identifiers[local]),
            )
            created.append({"local_child_id": local,
                            "node_id": identifiers[local]})
        return created

    def _plan_apply_expansion(self, tx: Tx, actor: R.Actor, request: Any,
                              checked: dict[tuple[str, str], Any]) -> dict[str, Any]:
        actor.require_read(request["run_id"])
        run = self._run_row(tx, request["run_id"])
        self._require_open_run(run)
        parent = self._node(tx, request["node_id"], run["run_id"])
        checked_parent = checked.get(("node", parent["node_id"]))
        if checked_parent is None:
            R.fail("revision_conflict", "expansion parent must be covered by read_set",
                   node_id=parent["node_id"])
        if checked_parent["revision"] != parent["revision"]:
            R.fail("revision_conflict", "expansion parent revision changed",
                   node_id=parent["node_id"], current_revision=parent["revision"])
        if request["state"] == "applied":
            children = tx.all(
                "SELECT local_child_id, child_node_id FROM expansion_children"
                " WHERE expansion_id = ? ORDER BY local_child_id",
                (request["expansion_id"],),
            )
            return {"expansion_id": request["expansion_id"],
                    "children": [{"local_child_id": child["local_child_id"],
                                  "node_id": child["child_node_id"]} for child in children],
                    "node": parent, "duplicate": True}
        if request["state"] != "requested":
            R.fail("candidate_mismatch", "expansion request is not pending",
                   expansion_id=request["expansion_id"], state=request["state"])
        if parent["state"] not in {"running", "awaiting_review"}:
            R.fail("candidate_mismatch", "expansion parent is not open for expansion",
                   state=parent["state"])
        proposal = load(request["payload_json"])
        children = self._insert_expansion(tx, run, parent, proposal,
                                          request["source_event_id"])
        timestamp = now()
        tx.execute(
            "UPDATE expansion_requests SET state = 'applied', applied_at = ?, updated_at = ?"
            " WHERE expansion_id = ?",
            (timestamp, timestamp, request["expansion_id"]),
        )
        return {"expansion_id": request["expansion_id"], "children": children,
                "node": self._node(tx, parent["node_id"]), "duplicate": False}

    def _append_observation(self, tx: Tx, actor: R.Actor, run: Any, item: dict[str, Any],
                            request_id: str, expected_revision: int) -> dict[str, Any]:
        actor.require_role("controller")
        worker = self._worker(tx, item["worker_id"], run["run_id"])
        if worker["node_id"] != item["node_id"]:
            R.fail("not_found", "worker is not bound to this node")
        if actor.controller_id is None or actor.epoch is None:
            R.fail("unauthorized", "controller identity is required for an observation")
        if (actor.controller_id != worker["controller_id"] or
                actor.epoch != worker["epoch"] or item["epoch"] != worker["epoch"]):
            R.fail("stale_epoch", "observation does not belong to the current epoch",
                   current_epoch=worker["epoch"])
        self._check_revision(worker, expected_revision, "worker", worker["worker_id"])
        if item["intent_seq"] != worker["intent_seq"]:
            R.fail("revision_conflict", "observation does not match the current intent",
                   current_intent_seq=worker["intent_seq"],
                   requested_intent_seq=item["intent_seq"])
        self._check_refs(tx, actor, [
            activity["evidence_ref"] for activity in item.get("activities") or []
            if activity.get("evidence_ref") is not None])
        # The numbered high-watermark must ignore unnumbered unknown reports:
        # otherwise a permitted report without an observation_id would hide
        # the last numbered fact and let a stale or rewritten observation
        # through the ordering and same-id fingerprint guards.
        previous = tx.one(
            "SELECT seq, payload_json FROM events WHERE run_id = ? AND worker_id = ?"
            " AND kind = 'worker_observed' AND epoch = ?"
            " AND json_extract(payload_json, '$.observation_id') IS NOT NULL"
            " ORDER BY seq DESC LIMIT 1",
            (run["run_id"], worker["worker_id"], item["epoch"]),
        )
        if previous is not None:
            committed = load(previous["payload_json"])
            current_id = item.get("observation_id")
            committed_id = committed.get("observation_id")
            if (isinstance(current_id, int) and isinstance(committed_id, int)
                    and current_id < committed_id):
                # A delayed report must never overwrite a newer actual state:
                # the Backend observation ids are monotonic per generation.
                R.fail("stale_observation",
                       "observation is older than the committed state",
                       observation_id=current_id,
                       committed_observation_id=committed_id,
                       committed_seq=previous["seq"])
            if (isinstance(current_id, int) and current_id == committed_id
                    and _activity_fingerprint(item) != _activity_fingerprint(committed)):
                # One underlying observation cannot carry two different
                # activity/completeness facts, even through a fresh intent.
                R.fail("idempotency_conflict",
                       "the observation id was reused with different activity facts",
                       observation_id=current_id, committed_seq=previous["seq"])
        projection = load(worker["activities_json"]) or []
        if "activities" in item:
            projection = merge_activity_snapshot(
                projection, item["activities"],
                complete=item["activities_complete"])
            projection_complete = item["activities_complete"]
        else:
            # An omitted slice is an unknown report: keep the records already
            # known but do not let completeness release a safety gate.
            projection_complete = False
        event_id, seq = tx.append_event(
            run_id=run["run_id"], kind="worker_observed", node_id=worker["node_id"],
            worker_id=worker["worker_id"], epoch=item["epoch"], actor_id=actor.actor_id,
            request_id=request_id, payload=item,
        )
        new_revision = worker["revision"] + 1
        usage = load(worker["usage_json"]) or {}
        for name, value in (item.get("usage") or {}).items():
            usage[name] = max(int(usage.get(name, 0)), int(value))
        tx.execute(
            "UPDATE workers SET observed = ?, session_ref = ?, checkpoint_ref = ?,"
            " usage_json = ?, activities_json = ?, activities_complete = ?,"
            " last_event_id = ?, revision = ?, updated_at = ?"
            " WHERE worker_id = ?",
            (item["observed"], item.get("session_ref"), item.get("checkpoint_ref"),
             dump(usage), dump(projection), int(projection_complete), event_id,
             new_revision, now(), worker["worker_id"]),
        )
        node = self._node(tx, worker["node_id"], run["run_id"])
        return {"worker_id": worker["worker_id"], "observed": item["observed"],
                "usage": usage, "activities": projection,
                "activities_complete": projection_complete,
                "revision": new_revision, "event_id": event_id,
                "seq": seq, "node_state": node["state"]}

    def _append_decision_request(self, tx: Tx, actor: R.Actor, run: Any,
                                 item: dict[str, Any], request_id: str,
                                 expected_revision: int) -> dict[str, Any]:
        actor.require_role("worker")
        if actor.worker_id is None or actor.epoch is None:
            R.fail("unauthorized", "worker identity is required for a decision request")
        node = self._node(tx, item["node_id"], run["run_id"])
        worker = self._worker(tx, item["worker_id"], run["run_id"])
        if worker["node_id"] != node["node_id"]:
            R.fail("unauthorized", "worker is not bound to this node")
        if item["epoch"] != worker["epoch"] or actor.epoch != worker["epoch"]:
            R.fail("stale_epoch", "decision request epoch is not current",
                   current_epoch=worker["epoch"])
        if actor.worker_id != worker["worker_id"]:
            R.fail("unauthorized", "worker identity does not match the payload")
        R.require(not item["decision_id"].startswith(PROGRESS_DECISION_PREFIX),
                  "invalid_argument",
                  "decision_id uses the reserved progress reassessment namespace",
                  decision_id=item["decision_id"])
        existing = tx.one("SELECT * FROM decision_requests WHERE decision_id = ?",
                          (item["decision_id"],))
        if existing is not None:
            same = (existing["run_id"] == run["run_id"] and
                    existing["node_id"] == node["node_id"] and
                    existing["round"] == item["round"] and
                    existing["contract_ref"] == item["contract_ref"] and
                    existing["reason"] == item["reason"] and
                    existing["question"] == item["question"] and
                    load(existing["options_json"]) == item["options"] and
                    load(existing["material_refs_json"]) == item["material_refs"])
            if not same:
                R.fail("idempotency_conflict",
                       "decision_id was reused with different content",
                       decision_id=item["decision_id"])
            event = tx.one("SELECT * FROM events WHERE event_id = ?",
                           (existing["request_event_id"],))
            return {"decision_id": existing["decision_id"], "node_id": node["node_id"],
                    "parent_id": existing["parent_id"], "state": node["state"],
                    "decision_state": existing["state"], "round": existing["round"],
                    "event_id": existing["request_event_id"], "seq": event["seq"],
                    "revision": node["revision"], "duplicate": True}
        open_request = tx.one(
            "SELECT * FROM decision_requests WHERE node_id = ? AND state = 'open'",
            (node["node_id"],))
        if open_request is not None:
            R.fail("decision_conflict",
                   "node already has an unresolved decision request",
                   decision_id=open_request["decision_id"])
        if node["parent_id"] is None:
            R.fail("capability_unsupported",
                   "the root node has no parent decision authority",
                   node_id=node["node_id"])
        parent = self._node(tx, node["parent_id"], run["run_id"])
        if parent["state"] in ("succeeded", "failed", "cancelled"):
            R.fail("invalid_argument", "the parent node is no longer open for decisions",
                   parent_id=parent["node_id"], parent_state=parent["state"])
        self._check_revision(node, expected_revision, "node", node["node_id"])
        if item["contract_ref"] != node["contract_ref"]:
            R.fail("candidate_mismatch",
                   "decision request contract does not match the frozen node contract",
                   contract_ref=item["contract_ref"],
                   node_contract_ref=node["contract_ref"])
        if node["state"] != "running":
            R.fail("decision_conflict", "a decision request requires a running node",
                   state=node["state"])
        pending = self._current_undecided_candidate(tx, node)
        if pending is not None:
            R.fail("decision_conflict",
                   "an undecided candidate owns the node's next step",
                   candidate_id=pending["candidate_id"])
        if item["round"] != node["round"] + 1:
            R.fail("decision_conflict",
                   "decision request round must be the current attempt",
                   current_round=node["round"], requested_round=item["round"])
        self._check_refs(tx, actor, item["material_refs"])
        # The API commits the open decision fact; the Scheduler consumes it and
        # decides when the Node waits. Anything that must block immediately
        # (Controller delivery, continuation) reads this committed fact.
        event_id, seq = tx.append_event(
            run_id=run["run_id"], kind="decision_requested", node_id=node["node_id"],
            worker_id=worker["worker_id"], epoch=item["epoch"],
            actor_id=actor.actor_id, request_id=request_id, payload=item,
        )
        timestamp = now()
        tx.execute(
            "INSERT INTO decision_requests (decision_id, run_id, node_id, parent_id,"
            " worker_id, epoch, round, contract_ref, reason, question, options_json,"
            " material_refs_json, state, request_event_id, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)",
            (item["decision_id"], run["run_id"], node["node_id"], parent["node_id"],
             worker["worker_id"], item["epoch"], item["round"], item["contract_ref"],
             item["reason"], item["question"], dump(item["options"]),
             dump(item["material_refs"]), event_id, timestamp, timestamp),
        )
        return {"decision_id": item["decision_id"], "node_id": node["node_id"],
                "parent_id": parent["node_id"], "state": node["state"],
                "decision_state": "open", "round": item["round"], "event_id": event_id,
                "seq": seq, "revision": node["revision"], "duplicate": False}

    def _append_decision_resolution(self, tx: Tx, actor: R.Actor, run: Any,
                                    item: dict[str, Any], request_id: str,
                                    expected_revision: int) -> dict[str, Any]:
        actor.require_role("worker")
        if actor.worker_id is None or actor.epoch is None:
            R.fail("unauthorized", "worker identity is required for a decision reply")
        decision = tx.one("SELECT * FROM decision_requests WHERE decision_id = ?",
                          (item["decision_id"],))
        if decision is None:
            R.fail("not_found", "decision request does not exist",
                   decision_id=item["decision_id"])
        if decision["run_id"] != run["run_id"]:
            R.fail("not_found", "decision request belongs to another run",
                   decision_id=item["decision_id"])
        node = self._node(tx, decision["node_id"], run["run_id"])
        if item["node_id"] != node["node_id"]:
            R.fail("candidate_mismatch", "decision reply names the wrong node",
                   decision_node_id=node["node_id"], reply_node_id=item["node_id"])
        worker = self._worker(tx, actor.worker_id, run["run_id"])
        if item["worker_id"] != worker["worker_id"]:
            R.fail("unauthorized", "worker identity does not match the payload")
        if worker["node_id"] != decision["parent_id"]:
            R.fail("unauthorized", "worker is not the parent decision authority",
                   worker_id=worker["worker_id"], parent_id=decision["parent_id"])
        if actor.epoch != worker["epoch"] or item["epoch"] != worker["epoch"]:
            R.fail("stale_epoch", "decision reply epoch is not current",
                   current_epoch=worker["epoch"])
        reply = {"answer": item["answer"]}
        if "option" in item:
            reply["option"] = item["option"]
        if decision["state"] == "resolved":
            if decision["resolved_by"] == worker["worker_id"] and \
                    load(decision["reply_json"]) == reply:
                event = tx.one("SELECT * FROM events WHERE event_id = ?",
                               (decision["resolved_event_id"],))
                return {"decision_id": decision["decision_id"], "node_id": node["node_id"],
                        "parent_id": decision["parent_id"], "state": "resolved",
                        "child_state": node["state"], "reply": reply,
                        "event_id": decision["resolved_event_id"], "seq": event["seq"],
                        "revision": node["revision"], "duplicate": True}
            R.fail("decision_conflict",
                   "decision was already resolved with a different reply",
                   decision_id=decision["decision_id"])
        if decision["state"] != "open":
            R.fail("decision_conflict", "decision is not open",
                   decision_id=decision["decision_id"], state=decision["state"])
        self._check_revision(node, expected_revision, "node", node["node_id"])
        if node["state"] != "waiting_decision":
            R.fail("decision_conflict", "child node is not waiting for a decision",
                   node_id=node["node_id"], state=node["state"])
        if decision["round"] != node["round"] + 1:
            R.fail("decision_conflict", "decision does not belong to the current attempt",
                   decision_round=decision["round"], expected_round=node["round"] + 1)
        if decision["contract_ref"] != node["contract_ref"]:
            R.fail("candidate_mismatch", "decision contract does not match the node",
                   decision_contract_ref=decision["contract_ref"],
                   node_contract_ref=node["contract_ref"])
        options = load(decision["options_json"])
        if "option" in reply and reply["option"] not in options:
            R.fail("invalid_argument", "decision reply names an option that was not offered",
                   option=reply["option"], options=options)
        # The API commits the resolved reply; the Scheduler decides when the
        # child resumes and enqueues the reply before any other delivery.
        event_id, seq = tx.append_event(
            run_id=run["run_id"], kind="decision_resolved", node_id=node["node_id"],
            worker_id=worker["worker_id"], epoch=worker["epoch"],
            actor_id=actor.actor_id, request_id=request_id, payload=item,
        )
        timestamp = now()
        tx.execute(
            "UPDATE decision_requests SET state = 'resolved', reply_json = ?,"
            " resolved_by = ?, resolved_epoch = ?, resolved_event_id = ?, updated_at = ?"
            " WHERE decision_id = ?",
            (dump(reply), worker["worker_id"], worker["epoch"], event_id, timestamp,
             decision["decision_id"]),
        )
        return {"decision_id": decision["decision_id"], "node_id": node["node_id"],
                "parent_id": decision["parent_id"], "state": "resolved",
                "child_state": node["state"], "reply": reply, "event_id": event_id,
                "seq": seq, "revision": node["revision"], "duplicate": False}

    def cancel_decision(self, actor: R.Actor, decision_id: Any, reason: Any,
                        request_id: str) -> dict[str, Any]:
        """Close a decision whose Node can no longer wait (Scheduler only).

        The Node state is changed by the Scheduler's plan; this record close is
        what releases the per-Node open-decision invariant and keeps one
        authoritative waiting row.
        """
        self._request_id(request_id)
        actor.require_role("scheduler")
        decision_id = R.require_text(decision_id, "decision_id", max_length=128)
        R.require(isinstance(reason, dict) and bool(reason), "invalid_argument",
                  "reason must be a nonempty object")
        R.require(R.is_json(reason), "invalid_argument", "reason must be valid JSON")
        args = {"decision_id": decision_id, "reason": reason}
        fingerprint = self._fingerprint(actor, "cancel_decision", args)
        with self.store.write() as tx:
            replay = tx.idempotency_lookup(actor.actor_id, "cancel_decision",
                                           request_id, fingerprint)
            if replay is not None:
                return replay
            row = tx.one("SELECT * FROM decision_requests WHERE decision_id = ?",
                         (decision_id,))
            if row is None:
                R.fail("not_found", "decision request does not exist",
                       decision_id=decision_id)
            actor.require_read(row["run_id"])
            if row["state"] == "resolved":
                R.fail("decision_conflict", "decision was already resolved",
                       decision_id=decision_id)
            if row["state"] == "cancelled":
                response = {"decision_id": decision_id, "node_id": row["node_id"],
                            "state": "cancelled", "duplicate": True}
                tx.idempotency_store(actor.actor_id, "cancel_decision", request_id,
                                     fingerprint, response)
                return response
            event_id, seq = tx.append_event(
                run_id=row["run_id"], kind="decision_cancelled",
                node_id=row["node_id"], worker_id=row["worker_id"],
                epoch=row["epoch"], actor_id=actor.actor_id, request_id=request_id,
                payload={"decision_id": decision_id, "node_id": row["node_id"],
                         "parent_id": row["parent_id"], "reason": reason},
            )
            timestamp = now()
            tx.execute(
                "UPDATE decision_requests SET state = 'cancelled', updated_at = ?"
                " WHERE decision_id = ?",
                (timestamp, decision_id),
            )
            response = {"decision_id": decision_id, "node_id": row["node_id"],
                        "parent_id": row["parent_id"], "state": "cancelled",
                        "event_id": event_id, "seq": seq, "duplicate": False}
            tx.idempotency_store(actor.actor_id, "cancel_decision", request_id,
                                 fingerprint, response)
            return response

    # ------------------------------------------------------------------
    # scheduler plans

    def reject_expansion(self, actor: R.Actor, expansion_id: Any, reason: Any,
                         request_id: str) -> dict[str, Any]:
        """Record a Scheduler policy/budget refusal instead of silently truncating."""
        self._request_id(request_id)
        actor.require_role("scheduler")
        R.require_text(expansion_id, "expansion_id", max_length=128)
        R.require(isinstance(reason, dict) and bool(reason), "invalid_argument",
                  "reason must be a nonempty object")
        R.require(R.is_json(reason), "invalid_argument", "reason must be valid JSON")
        args = {"expansion_id": expansion_id, "reason": reason}
        fingerprint = self._fingerprint(actor, "reject_expansion", args)
        with self.store.write() as tx:
            replay = tx.idempotency_lookup(actor.actor_id, "reject_expansion", request_id,
                                           fingerprint)
            if replay is not None:
                return replay
            request = tx.one("SELECT * FROM expansion_requests WHERE expansion_id = ?",
                             (expansion_id,))
            if request is None:
                R.fail("not_found", "expansion request does not exist",
                       expansion_id=expansion_id)
            actor.require_read(request["run_id"])
            if request["state"] == "rejected":
                response = {"expansion_id": expansion_id, "state": "rejected",
                            "reason": load(request["reason_json"]), "duplicate": True}
                tx.idempotency_store(actor.actor_id, "reject_expansion", request_id,
                                     fingerprint, response)
                return response
            if request["state"] != "requested":
                R.fail("candidate_mismatch", "expansion request is not pending",
                       expansion_id=expansion_id, state=request["state"])
            event_id, seq = tx.append_event(
                run_id=request["run_id"], kind="expansion_rejected",
                node_id=request["node_id"], actor_id=actor.actor_id,
                request_id=request_id,
                payload={"expansion_id": expansion_id, "reason": reason},
            )
            timestamp = now()
            tx.execute(
                "UPDATE expansion_requests SET state = 'rejected', reason_json = ?,"
                " updated_at = ? WHERE expansion_id = ?",
                (dump(reason), timestamp, expansion_id),
            )
            response = {"expansion_id": expansion_id, "state": "rejected",
                        "reason": reason, "event_id": event_id, "seq": seq,
                        "duplicate": False}
            tx.idempotency_store(actor.actor_id, "reject_expansion", request_id,
                                 fingerprint, response)
            return response

    def commit_plan(self, actor: R.Actor, read_set: Any, mutations: Any,
                    request_id: str) -> dict[str, Any]:
        self._request_id(request_id)
        actor.require_role("scheduler")
        reads = R.validate_read_set(read_set)
        changes = R.validate_mutations(mutations)
        # Idempotency binds the semantic command, not the CAS token: a retry
        # after the first commit must replay even though revisions advanced.
        args = {"read_set": [{"kind": item["kind"], "id": item["id"]}
                             for item in reads],
                "mutations": changes}
        fingerprint = self._fingerprint(actor, "commit_plan", args)
        with self.store.write() as tx:
            replay = tx.idempotency_lookup(actor.actor_id, "commit_plan", request_id,
                                           fingerprint)
            if replay is not None:
                return replay
            checked: dict[tuple[str, str], Any] = {}
            for item in reads:
                row = _read_resource(tx, item["kind"], item["id"])
                if row is None:
                    R.fail("not_found", "resource in read_set does not exist",
                           kind=item["kind"], id=item["id"])
                actor.require_read(row["run_id"])
                if row["revision"] != item["revision"]:
                    R.fail("revision_conflict", "read_set revision is stale",
                           kind=item["kind"], id=item["id"],
                           current_revision=row["revision"])
                checked[(item["kind"], item["id"])] = row
            revisions: dict[str, int] = {}
            affected_runs: set[str] = set()
            touched: set[tuple[str, str]] = set()
            expansions: list[dict[str, Any]] = []
            applied = 0

            def mark(kind: str, identifier: str) -> None:
                if (kind, identifier) in touched:
                    R.fail("invalid_argument", "a plan may touch each resource only once",
                           kind=kind, id=identifier)
                touched.add((kind, identifier))

            for mutation in changes:
                if mutation["kind"] == "node" and mutation["op"] == "create":
                    mark("node", mutation["fields"]["node_id"])
                    node, run_id = self._plan_create_node(tx, actor, mutation["fields"])
                    revisions[f"node:{node['node_id']}"] = node["revision"]
                    affected_runs.add(run_id)
                elif mutation["kind"] == "node" and mutation["op"] == "update":
                    mark("node", mutation["id"])
                    key = ("node", mutation["id"])
                    if key not in checked:
                        R.fail("revision_conflict", "node update must be covered by read_set",
                               id=mutation["id"])
                    node = self._plan_update_node(tx, actor, checked[key], mutation["fields"])
                    revisions[f"node:{node['node_id']}"] = node["revision"]
                    affected_runs.add(node["run_id"])
                elif mutation["kind"] == "worker" and mutation["op"] == "create":
                    mark("worker", mutation["fields"]["worker_id"])
                    worker, run_id = self._plan_create_worker(tx, actor, mutation["fields"])
                    revisions[f"worker:{worker['worker_id']}"] = worker["revision"]
                    affected_runs.add(run_id)
                elif mutation["kind"] == "worker" and mutation["op"] == "update":
                    mark("worker", mutation["id"])
                    key = ("worker", mutation["id"])
                    if key not in checked:
                        R.fail("revision_conflict", "worker update must be covered by read_set",
                               id=mutation["id"])
                    worker = self._plan_update_worker(tx, actor, checked[key],
                                                      mutation["fields"])
                    revisions[f"worker:{worker['worker_id']}"] = worker["revision"]
                    affected_runs.add(worker["run_id"])
                elif (mutation["kind"] == "acceptance_revision"
                      and mutation["op"] == "apply"):
                    event = tx.one("SELECT * FROM events WHERE event_id = ?",
                                   (mutation["id"],))
                    if (event is None or event["kind"] != ACCEPTANCE_REQUESTED):
                        R.fail("not_found",
                               "acceptance revision request does not exist",
                               source_event_id=mutation["id"])
                    payload = load(event["payload_json"]) or {}
                    target_id = payload.get("target_node_id")
                    R.require_text(target_id, "acceptance target node",
                                   max_length=128)
                    mark("acceptance_revision", mutation["id"])
                    mark("node", target_id)
                    key = ("node", target_id)
                    if key not in checked:
                        R.fail("revision_conflict",
                               "acceptance revision requires the node in read_set",
                               id=target_id)
                    run_row = self._run_row(tx, event["run_id"])
                    result = self._plan_apply_acceptance_revision(
                        tx, actor, run_row, mutation["id"], request_id)
                    revisions[f"node:{target_id}"] = result["node"]["revision"]
                    if result.get("stop"):
                        worker_row = self._worker(tx, result["stop"]["worker_id"])
                        revisions[f"worker:{worker_row['worker_id']}"] = \
                            worker_row["revision"]
                    affected_runs.add(run_row["run_id"])
                elif mutation["kind"] == "expansion" and mutation["op"] == "apply":
                    request = tx.one(
                        "SELECT * FROM expansion_requests WHERE expansion_id = ?",
                        (mutation["id"],),
                    )
                    if request is None:
                        R.fail("not_found", "expansion request does not exist",
                               expansion_id=mutation["id"])
                    mark("expansion", request["expansion_id"])
                    mark("node", request["node_id"])
                    result = self._plan_apply_expansion(tx, actor, request, checked)
                    expansions.append({"expansion_id": result["expansion_id"],
                                       "children": result["children"],
                                       "duplicate": result["duplicate"]})
                    if not result["duplicate"]:
                        revisions[f"node:{result['node']['node_id']}"] = \
                            result["node"]["revision"]
                        affected_runs.add(request["run_id"])
                elif mutation["kind"] == "decision_request" and mutation["op"] == "create":
                    mark("decision_request", mutation["id"])
                    node_key = ("node", mutation["fields"]["node_id"])
                    if node_key not in checked:
                        R.fail("revision_conflict",
                               "decision_request requires the node in read_set",
                               id=mutation["fields"]["node_id"])
                    node_row = checked[node_key]
                    run_row = self._run_row(tx, node_row["run_id"])
                    self._plan_progress_replan(
                        tx, actor, run_row, mutation["id"], mutation["fields"],
                        request_id)
                    affected_runs.add(run_row["run_id"])
                else:
                    mark("run", mutation["id"])
                    key = ("run", mutation["id"])
                    if key not in checked:
                        R.fail("revision_conflict", "run update must be covered by read_set",
                               id=mutation["id"])
                    run = self._plan_update_run(tx, checked[key], mutation["fields"])
                    revisions[f"run:{run['run_id']}"] = run["revision"]
                    affected_runs.add(run["run_id"])
                applied += 1
                tx.fault("plan_mutation_applied", applied)
            plan_digest = R.digest({"read_set": reads, "mutations": changes})
            events: list[dict[str, Any]] = []
            for run_id in sorted(affected_runs):
                event_id, seq = tx.append_event(
                    run_id=run_id, kind="plan_committed", actor_id=actor.actor_id,
                    request_id=request_id,
                    payload={"plan_digest": plan_digest, "read_set": reads,
                             "mutation_count": len(changes), "expansions": expansions},
                )
                events.append({"run_id": run_id, "event_id": event_id, "seq": seq})
            response = {"committed": True, "events": events, "revisions": revisions,
                        "expansions": expansions}
            tx.idempotency_store(actor.actor_id, "commit_plan", request_id, fingerprint,
                                 response)
            return response

    def _plan_create_node(self, tx: Tx, actor: R.Actor,
                          fields: dict[str, Any]) -> tuple[Any, str]:
        """Admit one frozen stage instance or one bound review_receipt Node.

        There is no caller-invented Node: a top-level Node must equal a stage
        of this run's stored Graph /2 batch, and a review Node must match the
        deterministic identity and closure of an already submitted candidate.
        """
        run_id = fields["run_id"]
        run = self._run_row(tx, run_id)
        self._require_open_run(run)
        actor.require_read(run_id)
        node_id = fields["node_id"]
        if tx.one("SELECT 1 FROM nodes WHERE node_id = ?", (node_id,)) is not None:
            R.fail("invalid_argument", "node already exists", node_id=node_id)
        bundle = self._definition_bundle_for_run(tx, actor, run)
        expected = next(
            (mutation["fields"] for mutation in initial_graph_tasks(bundle, run_id=run_id)
             if mutation["fields"]["node_id"] == node_id), None)
        if expected is not None:
            R.require(R.canonical(fields) == R.canonical(expected),
                      "unauthorized",
                      "a top-level node must match its frozen Graph stage instance",
                      node_id=node_id)
            self._check_refs(tx, actor, _artifact_refs(fields["input_bindings"]))
            self._check_budget_nodes(tx, run, 1)
            return self._insert_node(tx, run_id, fields), run_id

        closure = fields.get("closure")
        R.require(isinstance(closure, dict), "unauthorized",
                  "a top-level node is not declared by this run's frozen Graph",
                  node_id=node_id)
        target = self._node(tx, closure["target_node_id"], run_id)
        candidate = self._candidate(tx, closure["candidate_id"])
        R.require(candidate["run_id"] == run_id and
                  candidate["node_id"] == target["node_id"],
                  "candidate_mismatch",
                  "the review closure does not name this run's candidate",
                  candidate_id=candidate["candidate_id"])
        R.require(candidate["decision"] is None and candidate["round"] == target["round"],
                  "candidate_mismatch",
                  "the review closure candidate is no longer undecided",
                  candidate_id=candidate["candidate_id"],
                  node_round=target["round"])
        R.require(target["state"] == "awaiting_review", "candidate_mismatch",
                  "a review node requires an awaiting target",
                  target_state=target["state"])
        review_task = bundle.tasks.get(closure["reviewer_ref"])
        R.require(review_task is not None and
                  review_task.completion_kind == "review_receipt",
                  "reviewer_undeclared",
                  "the review closure must name an approved review_receipt task",
                  reviewer_ref=closure["reviewer_ref"])
        expected_fields = R.review_node_fields(
            run_id=run_id, target=self._node_view(target),
            candidate=self._candidate_view(candidate),
            reviewer_ref=review_task.task_ref)
        R.require(node_id == expected_fields["node_id"], "unauthorized",
                  "review node identity is deterministic", expected=expected_fields["node_id"])
        R.require(closure["worker_id"] == f"{node_id}-w1", "unauthorized",
                  "the review closure must bind the deterministic review Worker",
                  expected=f"{node_id}-w1")
        R.require(closure["reviewer_ref"] == target["reviewer_ref"],
                  "unauthorized",
                  "the review closure does not match the target's declared reviewer",
                  target_reviewer_ref=target["reviewer_ref"])
        R.require(R.canonical(fields) == R.canonical(expected_fields), "unauthorized",
                  "review node fields do not match its bound candidate",
                  node_id=node_id)
        self._check_refs(tx, actor, _artifact_refs(fields["input_bindings"]))
        self._check_budget_nodes(tx, run, 1)
        self._check_budget_depth(tx, run, target)
        return self._insert_node(tx, run_id, fields), run_id

    def _insert_node(self, tx: Tx, run_id: str, fields: Mapping[str, Any]) -> Any:
        timestamp = now()
        tx.execute(
            "INSERT INTO nodes (node_id, run_id, parent_id, expansion_id,"
            " local_child_id, template_ref, contract_ref, input_bindings_json,"
            " input_refs_json, delegation_json, dependencies_json, reviewer_ref,"
            " closure_json, state, round, accepted_candidate_ref, revision,"
            " created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, 0, NULL, 1, ?, ?)",
            (fields["node_id"], run_id, fields.get("parent_id"),
             fields.get("expansion_id"), fields.get("local_child_id"),
             fields["template_ref"], fields["contract_ref"],
             dump(fields["input_bindings"]),
             dump(fields.get("delegation") or {}),
             dump(fields["dependencies"]), fields["reviewer_ref"],
             dump(fields["closure"]) if fields.get("closure") else None,
             fields.get("state", "pending"), timestamp, timestamp),
        )
        return self._node(tx, fields["node_id"], run_id)

    def _plan_update_node(self, tx: Tx, actor: R.Actor, node: Any,
                          fields: dict[str, Any]) -> Any:
        new_state = fields.get("state", node["state"])
        R.check_node_transition(node["state"], new_state)
        updates: dict[str, Any] = {"state": new_state}
        round_number = fields.get("round", node["round"])
        R.require_int(round_number, "round")
        if round_number < node["round"]:
            R.fail("revision_conflict", "round cannot move backwards",
                   current_round=node["round"])
        updates["round"] = round_number
        accepted = fields.get("accepted_candidate_ref", node["accepted_candidate_ref"])
        closure = load(node["closure_json"])
        closed_by_receipt = (new_state == "succeeded" and closure is not None and
                             self._closure_satisfied(tx, node, closure))
        if accepted is not None:
            candidate = self._candidate(tx, accepted)
            if (candidate["node_id"] != node["node_id"] or
                    (candidate["decision"] != "accept" and not closed_by_receipt)):
                R.fail("candidate_mismatch", "accepted_candidate_ref is not an accepted candidate",
                       candidate_id=accepted)
        if new_state == "succeeded" and accepted is None and not closed_by_receipt:
            R.fail("candidate_mismatch",
                   "a succeeded node requires an accepted candidate or a valid closure")
        updates["accepted_candidate_ref"] = accepted
        if "input_refs" in fields:
            raw = fields["input_refs"]
            R.require(isinstance(raw, list), "invalid_argument", "input_refs must be a list")
            if node["input_refs_json"] is not None and load(node["input_refs_json"]) != raw:
                R.fail("revision_conflict", "input_refs are frozen once bound")
            self._check_refs(tx, actor, _artifact_refs(raw))
            updates["input_refs_json"] = dump(raw)
        else:
            updates["input_refs_json"] = node["input_refs_json"]
        tx.execute(
            "UPDATE nodes SET state = ?, round = ?, accepted_candidate_ref = ?,"
            " input_refs_json = ?, revision = ?, updated_at = ? WHERE node_id = ?",
            (updates["state"], updates["round"], updates["accepted_candidate_ref"],
             updates["input_refs_json"], node["revision"] + 1, now(), node["node_id"]),
        )
        return self._node(tx, node["node_id"])

    def _closure_satisfied(self, tx: Tx, node: Any,
                           closure: dict[str, Any]) -> bool:
        """A review_receipt Node completes from its own bound receipt.

        The closure names one target Node, one candidate and one review
        Worker. The bound Worker's current-epoch receipt for that exact
        candidate discharges this review round for any committed verdict:
        accept, revise and expand all close the Checker's own obligation.
        Whether the target succeeds is the business outcome of that verdict
        and must never be a precondition for the Checker completing. A bare
        reviewer name, another execution, an old epoch or another candidate
        never closes the Node, and the Checker's own required expansion
        children must all be accepted first.
        """
        target = tx.one("SELECT * FROM nodes WHERE node_id = ?",
                        (closure["target_node_id"],))
        if target is None or target["run_id"] != node["run_id"]:
            return False
        if target["reviewer_ref"] != closure["reviewer_ref"]:
            return False
        if node["template_ref"] != target["reviewer_ref"]:
            return False
        candidate = tx.one("SELECT * FROM candidates WHERE candidate_id = ?",
                           (closure["candidate_id"],))
        if candidate is None or candidate["node_id"] != target["node_id"]:
            return False
        worker = tx.one("SELECT * FROM workers WHERE worker_id = ?",
                        (closure["worker_id"],))
        if (worker is None or worker["node_id"] != node["node_id"]
                or worker["run_id"] != node["run_id"]):
            return False
        receipt = tx.one(
            "SELECT r.*, e.worker_id AS receipt_worker, e.epoch AS receipt_epoch"
            " FROM reviews r JOIN events e ON e.event_id = r.event_id"
            " WHERE r.run_id = ? AND r.node_id = ? AND r.candidate_id = ?"
            " ORDER BY r.created_at DESC, r.rowid DESC LIMIT 1",
            (node["run_id"], target["node_id"], closure["candidate_id"]))
        if receipt is None or receipt["verdict"] not in ("accept", "revise", "expand"):
            return False
        if receipt["verdict"] != candidate["decision"]:
            return False
        if (receipt["receipt_worker"] != closure["worker_id"]
                or receipt["receipt_epoch"] != worker["epoch"]):
            return False
        open_children = tx.all(
            "SELECT node_id FROM nodes WHERE parent_id = ? AND expansion_id IS NOT NULL"
            " AND closure_json IS NULL AND state != 'succeeded'",
            (node["node_id"],))
        return not open_children

    def _plan_create_worker(self, tx: Tx, actor: R.Actor,
                            fields: dict[str, Any]) -> tuple[Any, str]:
        """Create the one execution binding of a ready Node.

        The Scheduler submits the full effective context/profile/budget and
        required capabilities it composed from the pinned Task/Agent, frozen
        inputs, trusted capabilities and declared delegation. This method
        recomputes the same pure composition and refuses a missing or
        inconsistent request instead of filling in execution intent.
        """
        node = self._node(tx, fields["node_id"])
        run = self._run_row(tx, node["run_id"])
        self._require_open_run(run)
        actor.require_read(run["run_id"])
        worker_id = fields["worker_id"]
        R.require(worker_id == f"{node['node_id']}-w1", "unauthorized",
                  "worker identity is deterministic per Node",
                  expected=f"{node['node_id']}-w1")
        R.require(node["state"] == "ready", "candidate_mismatch",
                  "a worker requires a ready node", node_state=node["state"])
        if tx.one("SELECT 1 FROM workers WHERE worker_id = ?", (worker_id,)) is not None:
            R.fail("invalid_argument", "worker already exists", worker_id=worker_id)
        if tx.one("SELECT 1 FROM workers WHERE node_id = ?",
                  (node["node_id"],)) is not None:
            R.fail("invalid_argument", "node already has an execution binding",
                   node_id=node["node_id"])
        resolved = self._resolve_acceptance_contract(tx, actor, run, node)
        bundle = resolved["bundle"]
        task = resolved["task"]
        agent = bundle.agents.get(task.agent_ref)
        if agent is None:
            R.fail("not_found", "the task agent is not in the run library",
                   agent_ref=task.agent_ref, node_id=node["node_id"])
        backend_ref = fields["backend_ref"]
        agent_ref = fields["agent_ref"]
        R.require(backend_ref == agent.backend_ref, "capability_unsupported",
                  "the worker backend does not match the task agent definition",
                  agent_backend=agent.backend_ref, worker_backend=backend_ref)
        R.require(agent_ref == agent.agent_ref, "unauthorized",
                  "the worker agent does not match the task definition",
                  task_agent=agent.agent_ref, worker_agent=agent_ref)
        capabilities = self.backend_capabilities.get(backend_ref)
        if capabilities is None:
            R.fail("capability_unsupported",
                   "the host has no trusted capabilities for this backend",
                   backend_ref=backend_ref)
        closure = load(node["closure_json"])
        candidate = None
        if closure is not None:
            candidate = self._candidate_view(
                self._candidate(tx, closure["candidate_id"]))
        spec = compose_worker_spec(
            run_id=run["run_id"],
            node=self._node_view(node),
            worker_id=worker_id,
            task=task,
            agent=agent,
            capabilities=self.backend_capabilities.get(backend_ref),
            candidate=candidate,
            epoch=int(fields.get("epoch", 0)),
            desired=fields.get("desired", "active"),
            run_budget=self._run_budget(run),
            contract=resolved["contract"],
            upper_contract_ref=resolved["upper_contract_ref"],
            revision_descriptor=({**resolved["descriptor"],
                                  "source_ref": resolved["source_ref"]}
                                 if resolved["descriptor"] is not None else None),
        )
        R.require(not fields.get("runtime_options"), "capability_unsupported",
                  "no Backend consumes runtime options in this slice",
                  options=sorted(fields.get("runtime_options") or {}))
        R.require(fields.get("checkpoint") is None, "invalid_argument",
                  "a new worker cannot start from an unbound checkpoint")
        composed = {
            "context": spec.context,
            "tool_profile": spec.tool_profile,
            "budget": spec.budget,
            "required_capabilities": spec.required_capabilities,
        }
        for name, value in composed.items():
            # The Scheduler owns execution intent: the Api recomputes with the
            # same pure rule and never fills in a missing field on its behalf.
            R.require(name in fields and fields[name] is not None,
                      "unauthorized",
                      "the worker request must carry the composed execution spec",
                      field=name)
            R.require(R.canonical(fields[name]) == R.canonical(value),
                      "unauthorized",
                      "the worker request disagrees with the composed execution spec",
                      field=name)
        desired = spec.desired
        epoch = spec.epoch
        timestamp = now()
        tx.execute(
            "INSERT INTO workers (worker_id, run_id, node_id, backend_ref,"
            " agent_ref, required_capabilities_json, controller_id, epoch,"
            " intent_seq, desired, observed, session_ref, checkpoint_ref,"
            " context_json, tool_profile_json, budget_json, checkpoint_json,"
            " usage_json, last_event_id, revision, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 'starting', NULL, NULL,"
            " ?, ?, ?, NULL, ?, NULL, 1, ?, ?)",
            (worker_id, run["run_id"], node["node_id"], backend_ref, agent_ref,
             dump(composed["required_capabilities"]), fields["controller_id"],
             epoch, desired, dump(composed["context"]),
             dump(composed["tool_profile"]), dump(composed["budget"]),
             dump({"provider_calls": 0, "tool_calls": 0}), timestamp, timestamp),
        )
        return self._worker(tx, worker_id, run["run_id"]), run["run_id"]

    def _require_safe_epoch_replacement(self, tx: Tx, worker: Any) -> None:
        """R04-EPOCH-04: only replace an execution the Controller has seen end.

        Each epoch owns an independent execution directory and lock, so the
        generation fence cannot be the process lock. Replacement instead needs
        durable evidence that the old entity is gone *and* that its usage was
        reconciled into this Worker, and that no delivery (or queued delivery)
        of the old epoch can still start a process afterwards.
        """
        details = {"worker_id": worker["worker_id"], "epoch": worker["epoch"],
                   "observed": worker["observed"],
                   "intent_seq": worker["intent_seq"]}
        if worker["observed"] not in EPOCH_TERMINAL_OBSERVED:
            R.fail("epoch_in_flight",
                   "the previous execution is not observed idle/stopped/failed",
                   **details)
        projected = {
            "observed": worker["observed"],
            "activities": load(worker["activities_json"]) or [],
            "activities_complete": bool(worker["activities_complete"]),
        }
        if not execution_stopped(projected, allow_idle_main=True):
            R.fail("epoch_in_flight",
                   "the previous execution still owns internal activity",
                   **details, activities_complete=projected["activities_complete"],
                   activities=projected["activities"])
        event = tx.one(
            "SELECT * FROM events WHERE run_id = ? AND worker_id = ?"
            " AND kind = 'worker_observed' ORDER BY seq DESC LIMIT 1",
            (worker["run_id"], worker["worker_id"]),
        )
        if event is None:
            R.fail("epoch_unreconciled",
                   "no observation was committed for the previous execution",
                   **details)
        payload = load(event["payload_json"])
        if payload.get("epoch") != worker["epoch"]:
            R.fail("epoch_unreconciled",
                   "the latest observation belongs to another epoch",
                   observation_epoch=payload.get("epoch"), **details)
        if payload.get("observed") != worker["observed"]:
            R.fail("epoch_unreconciled",
                   "the worker view disagrees with the latest observation",
                   observation=payload.get("observed"), **details)
        if payload.get("intent_seq") != worker["intent_seq"]:
            R.fail("epoch_unreconciled",
                   "the worker intent changed after the latest observation",
                   observation_intent_seq=payload.get("intent_seq"), **details)
        usage = payload.get("usage")
        if (not isinstance(usage, dict) or set(usage) != USAGE_KEYS
                or any(not isinstance(value, int) or isinstance(value, bool)
                       or value < 0 for value in usage.values())):
            R.fail("epoch_unreconciled",
                   "the terminal observation does not reconcile usage",
                   observation_usage=usage, **details)
        committed = load(worker["usage_json"]) or {}
        for name in sorted(USAGE_KEYS):
            if int(committed.get(name, 0) or 0) < int(usage[name]):
                R.fail("epoch_unreconciled",
                       "the committed usage is behind the last observation",
                       committed=dict(committed), observation_usage=usage,
                       **details)
        latest_delivery = tx.one(
            "SELECT MAX(seq) AS seq FROM events WHERE run_id = ? AND worker_id = ?"
            " AND epoch = ? AND kind = 'message_delivered'",
            (worker["run_id"], worker["worker_id"], worker["epoch"]),
        )
        if latest_delivery is not None and (latest_delivery["seq"] or 0) > event["seq"]:
            R.fail("epoch_in_flight",
                   "a delivery was committed after the last observation",
                   delivery_seq=latest_delivery["seq"], observation_seq=event["seq"],
                   **details)
        pending = tx.one(
            "SELECT message_id FROM messages WHERE worker_id = ? AND epoch = ?"
            " AND delivery_state = 'pending' LIMIT 1",
            (worker["worker_id"], worker["epoch"]),
        )
        if pending is not None:
            R.fail("epoch_in_flight",
                   "a queued message could still start the old epoch",
                   message_id=pending["message_id"], **details)

    SPEC_WORKER_FIELDS = frozenset({"context", "tool_profile", "budget",
                                    "required_capabilities"})

    def _composed_rebind_spec(self, tx: Tx, actor: R.Actor, worker: Any,
                              fields: Mapping[str, Any]) -> dict[str, Any]:
        """Recompute a replacement execution from frozen business state."""
        node = self._node(tx, worker["node_id"])
        run = self._run_row(tx, node["run_id"])
        self._require_open_run(run)
        resolved = self._resolve_acceptance_contract(tx, actor, run, node)
        if resolved["descriptor"] is None:
            R.fail("unauthorized",
                   "a worker context may change only for an applied local contract",
                   node_id=node["node_id"])
        if node["state"] != "running":
            R.fail("candidate_mismatch",
                   "a replacement execution requires a running node",
                   node_id=node["node_id"], state=node["state"])
        task = resolved["task"]
        agent = resolved["bundle"].agents.get(task.agent_ref)
        if agent is None:
            R.fail("not_found", "the task agent is not in the run library",
                   agent_ref=task.agent_ref)
        if agent.backend_ref != worker["backend_ref"]:
            R.fail("capability_unsupported",
                   "the replacement cannot change the committed backend",
                   backend_ref=worker["backend_ref"],
                   agent_backend=agent.backend_ref)
        closure = load(node["closure_json"])
        candidate = None
        if closure is not None:
            candidate = self._candidate_view(
                self._candidate(tx, closure["candidate_id"]))
        spec = compose_worker_spec(
            run_id=run["run_id"], node=self._node_view(node),
            worker_id=worker["worker_id"], task=task, agent=agent,
            capabilities=self.backend_capabilities.get(worker["backend_ref"]),
            candidate=candidate, epoch=int(fields["epoch"]),
            desired=fields.get("desired", "active"),
            run_budget=self._run_budget(run),
            contract=resolved["contract"],
            upper_contract_ref=resolved["upper_contract_ref"],
            revision_descriptor={**resolved["descriptor"],
                                 "source_ref": resolved["source_ref"]},
        )
        composed = {"context": spec.context, "tool_profile": spec.tool_profile,
                    "budget": spec.budget,
                    "required_capabilities": spec.required_capabilities}
        for name, value in composed.items():
            R.require(name in fields and fields[name] is not None,
                      "unauthorized",
                      "a replacement must carry the full composed execution spec",
                      field=name)
            R.require(R.canonical(fields[name]) == R.canonical(value),
                      "unauthorized",
                      "the replacement disagrees with the composed execution spec",
                      field=name)
        return composed

    def _plan_update_worker(self, tx: Tx, actor: R.Actor, worker: Any,
                            fields: dict[str, Any]) -> Any:
        provided = set(fields) & self.SPEC_WORKER_FIELDS
        forbidden = sorted(set(fields) - PLAN_WORKER_FIELDS
                           - self.SPEC_WORKER_FIELDS)
        if forbidden:
            R.fail("unauthorized", "scheduler may only change worker intent and desired state",
                   forbidden=forbidden)
        if provided and set(provided) != self.SPEC_WORKER_FIELDS:
            R.fail("unauthorized",
                   "a replacement must carry the full composed execution spec",
                   missing=sorted(self.SPEC_WORKER_FIELDS - provided))
        intent_seq = worker["intent_seq"]
        if "intent_seq" in fields:
            R.require(fields["intent_seq"] == worker["intent_seq"] + 1, "revision_conflict",
                      "intent_seq must advance by exactly one",
                      current_intent_seq=worker["intent_seq"])
            intent_seq = fields["intent_seq"]
        desired = fields.get("desired", worker["desired"])
        R.require(desired in R.WORKER_DESIRED, "invalid_argument",
                  "unknown worker desired state")
        epoch = worker["epoch"]
        observed = worker["observed"]
        session_ref = worker["session_ref"]
        checkpoint_ref = worker["checkpoint_ref"]
        composed: dict[str, Any] | None = None
        if "epoch" in fields:
            # Replacing the execution entity starts a new generation. The
            # worker id (and its committed usage) stays; session and
            # checkpoint bindings belong to the old epoch and are dropped.
            R.require(fields["epoch"] == worker["epoch"] + 1, "revision_conflict",
                      "epoch must advance by exactly one",
                      current_epoch=worker["epoch"])
            node = self._node(tx, worker["node_id"])
            if node["contract_ref"] != node["template_ref"] and not provided:
                R.fail("unauthorized",
                       "a revised contract requires a fully recomposed execution spec",
                       node_id=node["node_id"])
            self._require_safe_epoch_replacement(tx, worker)
            epoch = fields["epoch"]
            observed = "starting"
            session_ref = None
            checkpoint_ref = None
            # Activity projection is scoped to one execution epoch; a new
            # entity starts from unknown, never merged with the old one.
            activities = []
            activities_complete = False
            if provided:
                composed = self._composed_rebind_spec(tx, actor, worker, fields)
        else:
            if provided:
                R.fail("unauthorized",
                       "worker context may change only with an epoch replacement")
            activities = load(worker["activities_json"]) or []
            activities_complete = bool(worker["activities_complete"])
        if composed is None:
            tx.execute(
                "UPDATE workers SET epoch = ?, intent_seq = ?, desired = ?, observed = ?,"
                " session_ref = ?, checkpoint_ref = ?, activities_json = ?,"
                " activities_complete = ?, revision = ?, updated_at = ?"
                " WHERE worker_id = ?",
                (epoch, intent_seq, desired, observed, session_ref, checkpoint_ref,
                 dump(activities), int(activities_complete),
                 worker["revision"] + 1, now(), worker["worker_id"]),
            )
        else:
            tx.execute(
                "UPDATE workers SET epoch = ?, intent_seq = ?, desired = ?, observed = ?,"
                " session_ref = ?, checkpoint_ref = ?, activities_json = ?,"
                " activities_complete = ?, context_json = ?, tool_profile_json = ?,"
                " budget_json = ?, required_capabilities_json = ?, revision = ?,"
                " updated_at = ? WHERE worker_id = ?",
                (epoch, intent_seq, desired, observed, session_ref, checkpoint_ref,
                 dump(activities), int(activities_complete),
                 dump(composed["context"]), dump(composed["tool_profile"]),
                 dump(composed["budget"]),
                 dump(composed["required_capabilities"]),
                 worker["revision"] + 1, now(), worker["worker_id"]),
            )
        return self._worker(tx, worker["worker_id"])

    def _plan_update_run(self, tx: Tx, run: Any, fields: dict[str, Any]) -> Any:
        status = fields.get("status", run["status"])
        R.check_run_transition(run["status"], status)
        summary = fields.get("summary", load(run["summary_json"]))
        R.require(isinstance(summary, dict), "invalid_argument", "summary must be an object")
        tx.execute(
            "UPDATE runs SET status = ?, summary_json = ?, revision = ?, updated_at = ?"
            " WHERE run_id = ?",
            (status, dump(summary), run["revision"] + 1, now(), run["run_id"]),
        )
        return self._run_row(tx, run["run_id"])

    # ------------------------------------------------------------------
    # intent and report

    def set_run_intent(self, actor: R.Actor, run_id: str, desired: Any,
                       expected_revision: Any, request_id: str) -> dict[str, Any]:
        self._request_id(request_id)
        actor.require_role("publisher")
        R.require(desired in R.RUN_DESIRED, "invalid_argument", "unknown run intent")
        args = {"run_id": run_id, "desired": desired,
                "expected_revision": expected_revision}
        fingerprint = self._fingerprint(actor, "set_run_intent", args)
        with self.store.write() as tx:
            replay = tx.idempotency_lookup(actor.actor_id, "set_run_intent", request_id,
                                           fingerprint)
            if replay is not None:
                return replay
            run = self._visible_run(tx, actor, run_id)
            self._check_revision(run, expected_revision, "run", run_id)
            revision = run["revision"] + 1
            tx.execute(
                "UPDATE runs SET desired = ?, revision = ?, updated_at = ? WHERE run_id = ?",
                (desired, revision, now(), run_id),
            )
            event_id, seq = tx.append_event(
                run_id=run_id, kind="run_intent_set", actor_id=actor.actor_id,
                request_id=request_id, payload={"desired": desired},
            )
            response = {"run_id": run_id, "desired": desired, "revision": revision,
                        "status": run["status"], "event_id": event_id, "seq": seq}
            tx.idempotency_store(actor.actor_id, "set_run_intent", request_id, fingerprint,
                                 response)
            return response

    def get_report(self, actor: R.Actor, run_id: str) -> dict[str, Any]:
        with self.store.read() as tx:
            run = self._visible_run(tx, actor, run_id)
            nodes = tx.all("SELECT * FROM nodes WHERE run_id = ? ORDER BY created_at, node_id",
                           (run_id,))
            candidates = tx.all(
                "SELECT * FROM candidates WHERE run_id = ? ORDER BY created_at, candidate_id",
                (run_id,),
            )
            by_node: dict[str, list[Any]] = {}
            for candidate in candidates:
                by_node.setdefault(candidate["node_id"], []).append(candidate)
            node_reports: list[dict[str, Any]] = []
            accepted_count = 0
            for node in nodes:
                candidate_views = [self._candidate_view(row)
                                   for row in by_node.get(node["node_id"], [])]
                accepted = None
                if node["accepted_candidate_ref"] is not None:
                    accepted_row = self._candidate(tx, node["accepted_candidate_ref"])
                    accepted = self._candidate_view(accepted_row)
                    accepted_count += 1
                    for ref in accepted["output_refs"]:
                        self._artifact_row(tx, ref)
                        self.artifacts.read(ref)
                node_reports.append({
                    "node_id": node["node_id"],
                    "parent_id": node["parent_id"],
                    "template_ref": node["template_ref"],
                    "closure": load(node["closure_json"]),
                    "state": node["state"],
                    "round": node["round"],
                    "candidates": candidate_views,
                    "accepted": accepted,
                })
            return {
                "format": "agentgenome-control-plane-report/1",
                "run": self._run_view(run),
                "definition": {"definition_ref": run["definition_ref"],
                               "document_digest": run["definition_digest"]},
                "nodes": node_reports,
                "counts": {"nodes": len(nodes), "candidates": len(candidates),
                           "accepted": accepted_count},
                "events": {"latest_seq": run["event_seq"]},
            }


def _cursor(after: Any) -> int:
    if after is None:
        return 0
    if isinstance(after, int) and not isinstance(after, bool):
        R.require(after >= 0, "invalid_argument", "cursor must be >= 0")
        return after
    if isinstance(after, str) and after.startswith("seq:"):
        try:
            value = int(after[4:])
        except ValueError:
            R.fail("invalid_argument", "cursor is not a sequence number")
        R.require(value >= 0, "invalid_argument", "cursor must be >= 0")
        return value
    R.fail("invalid_argument", "cursor must be an integer or seq:<n>")


def _read_resource(tx: Tx, kind: str, identifier: str) -> Any:
    if kind == "run":
        return tx.one("SELECT * FROM runs WHERE run_id = ?", (identifier,))
    if kind == "node":
        return tx.one("SELECT * FROM nodes WHERE node_id = ?", (identifier,))
    if kind == "worker":
        return tx.one("SELECT * FROM workers WHERE worker_id = ?", (identifier,))
    return None


def _artifact_refs(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if set(value) == ARTIFACT_KEYS:
            found.append(R.artifact_ref(value))
        else:
            for item in value.values():
                found.extend(_artifact_refs(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_artifact_refs(item))
    return found


def _definition_static_refs(document: Any) -> list[dict[str, Any]]:
    """Literal artifact refs that a frozen Graph declares in node inputs.

    Only the declared ``nodes[*].inputs`` bindings grant run visibility before
    node materialisation; a digest that merely appears in a prompt, context or
    schema is not a grant.
    """
    nodes = document.get("nodes") if isinstance(document, dict) else None
    if not isinstance(nodes, dict):
        return []
    found: list[dict[str, Any]] = []
    for node in nodes.values():
        if isinstance(node, dict):
            found.extend(_artifact_refs(node.get("inputs")))
    return found
