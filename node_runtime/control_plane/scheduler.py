"""Scheduler: graph readiness, allocations, wake-ups and finite closure.

The Scheduler is the only component that decides which Nodes exist, which
dependencies are ready, how many Workers may run and who is woken. It commits
plans through the API and enqueues durable inbox items. It never starts a
Backend, chooses a tool call or judges review content.
"""
from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import bindings as B
from . import lifecycle as L
from . import policies as P
from . import task_definitions as T
from .backends.locks import ExecutionLock
from .decisions import (decision_continue_prefix, decision_delivery_gate,
                        decision_key, decision_reply_key)
from .execution_facts import (execution_quiescent, execution_stopped,
                              previous_progress_checkpoint)
from .resources import Actor, ControlPlaneError, digest, review_node_fields
from .worker_specs import compose_worker_spec

# Composition refusals that no retry can resolve: the frozen Task/Agent,
# delegation, trusted capabilities or frozen inputs cannot form a legal
# execution binding. They fail the Node instead of looping the Host forever.
_UNEXECUTABLE_BINDING_CODES = frozenset({
    "capability_unsupported", "unauthorized", "invalid_argument",
    "not_found", "candidate_mismatch",
})


class Scheduler:
    def __init__(self, api: Any, *, scheduler_id: str = "scheduler",
                 controller_id: str = "ctl-1",
                 policies: Mapping[str, P.ExpansionPolicy] | None = None,
                 max_inflight: int | None = None,
                 max_context_bytes: int | None = None,
                 request_prefix: str = "sched", lock_timeout: float = 60.0) -> None:
        """W02b2: graph decisions use registered capabilities and pinned defs.

        No global agent_ref, prompt or tool-profile defaults. Host configures
        trusted Api.backend_capabilities from its actual adapter instances.
        """
        if not scheduler_id or not controller_id:
            raise ValueError("scheduler_id and controller_id are required")
        self.api = api
        self.scheduler_id = scheduler_id
        self.controller_id = controller_id
        self.policies = dict(policies or {})
        self.max_inflight = max_inflight
        self.max_context_bytes = max_context_bytes
        self.request_prefix = request_prefix
        self.lock_timeout = lock_timeout


    # -- identity --------------------------------------------------------
    def actor(self, run_id: str) -> Actor:
        return Actor(self.scheduler_id, {"scheduler"}, run_ids={run_id})

    def _rid(self, run_id: str, tag: str, body: Any = None) -> str:
        """Content-addressed request identity that survives a Scheduler restart.

        The same logical plan/wake-up reads the same id in a new process and
        replays its durable receipt; a different body can never borrow it.
        """
        material = {"run_id": run_id, "tag": tag, "body": body}
        return f"{self.request_prefix}:{tag}:{digest(material).split(':', 1)[1][:32]}"

    # -- reads -----------------------------------------------------------
    def _snapshot(self, run_id: str) -> dict[str, Any]:
        actor = self.actor(run_id)
        data = self.api.list_resources(actor, run_id)
        run = self.api.get_resource(actor, "run", run_id)
        definition = self.api.get_resource(actor, "definition", run["definition_ref"])
        data["run"] = run
        data["definition"] = definition
        data["by_id"] = {node["node_id"]: node for node in data["nodes"]}
        data["worker_by_id"] = {worker["worker_id"]: worker
                               for worker in data["workers"]}
        data["worker_by_node"] = {}
        for worker in data["workers"]:
            data["worker_by_node"].setdefault(worker["node_id"], []).append(worker)
        return data

    # -- plan helpers ----------------------------------------------------
    def _plan(self, run_id: str, reads: Iterable[tuple[str, str, int]],
              mutations: list[dict[str, Any]], tag: str) -> dict[str, Any]:
        reads = list(reads)
        # Revisions are part of the request identity: two occurrences of the
        # same tag/mutations on the same resource (e.g. a parent leaving
        # waiting_children twice) are different plans and must not replay
        # each other's receipt. The API fingerprint still ignores CAS
        # metadata, so an exact replan with the same revisions replays.
        body = {"reads": [{"kind": kind, "id": identifier, "revision": revision}
                          for kind, identifier, revision in reads],
                "mutations": mutations}
        return self.api.commit_plan(
            self.actor(run_id),
            [{"kind": kind, "id": identifier, "revision": revision}
             for kind, identifier, revision in reads],
            mutations, self._rid(run_id, tag, body))

    def _update_run_status(self, snap: Mapping[str, Any], status: str,
                           run_id: str) -> None:
        run = snap["run"]
        if run["status"] == status:
            return
        self._plan(run_id, [("run", run_id, run["revision"])], [{
            "kind": "run", "op": "update", "id": run_id,
            "fields": {"status": status,
                       "summary": {"source": "scheduler",
                                   "nodes": len(snap["nodes"])}},
        }], "run-status")

    # -- public ----------------------------------------------------------
    def _reconcile_lock(self, run_id: str):
        """Serialise the whole public reconcile per Run across processes.

        Capacity is a property of the Run, not of the Node a plan happens to
        read: two Schedulers with disjoint Node snapshots can each pass the
        quota check and commit a Worker without any per-Node CAS conflict
        (R04-CAPACITY-07). An advisory lock keyed by Run in the shared data
        directory makes the read-decide-commit sequence atomic for both new
        Workers and idle-Worker wake-ups. Without a filesystem data directory
        (unit helpers) there is nothing to share, so no lock is taken.
        """
        root = getattr(self.api, "root", None)
        if root is None:
            return nullcontext()
        name = digest(run_id).split(":", 1)[1][:32]
        return ExecutionLock(Path(root) / "locks" / "scheduler" / f"{name}.lock",
                             timeout=self.lock_timeout)

    def reconcile(self, run_id: str, *, max_steps: int = 200) -> dict[str, Any]:
        with self._reconcile_lock(run_id):
            return self._reconcile_locked(run_id, max_steps=max_steps)

    def _reconcile_locked(self, run_id: str, *,
                          max_steps: int = 200) -> dict[str, Any]:
        actions: list[dict[str, Any]] = []
        for _ in range(max_steps):
            changed = False
            try:
                blocked, controlled = self._run_control(run_id, actions)
                changed |= controlled
                if not blocked:
                    # Committed candidate/review facts become Node state before
                    # failure attribution or any new dispatch. The plan reads
                    # both the Node and Run revisions so a pause/cancel makes
                    # an older application plan stale instead of overwriting it.
                    changed |= self._apply_candidates(run_id, actions)
                    changed |= self._apply_reviews(run_id, actions)
                    # Goal progress is consumed before any revision or
                    # ordinary continuation can be dispatched to the Node.
                    changed |= self._reconcile_goal_progress(run_id, actions)
                    # Execution facts become business state before any new
                    # dispatch: a failed process or an unresolved effect must
                    # never leave the logical task waiting forever.
                    changed |= self._apply_execution_failures(run_id, actions)
                    changed |= self._propagate_failures(run_id, actions)
                    changed |= self._apply_expansions(run_id, actions)
                    # Decision facts become Node waits/resumes before any
                    # ordinary wake-up can dispatch work to the same Worker.
                    changed |= self._apply_decisions(run_id, actions)
                    # Acceptance revisions commit the new contract, stop the
                    # old execution and bind the fresh epoch before ordinary
                    # wake-ups can dispatch under either version.
                    changed |= self._reconcile_acceptance_revisions(run_id, actions)
                    changed |= self._bootstrap(run_id, actions)
                    changed |= self._close_review_nodes(run_id, actions)
                    changed |= self._freeze_inputs(run_id, actions)
                    changed |= self._promote_ready(run_id, actions)
                    changed |= self._start_workers(run_id, actions)
                    changed |= self._wake_reviewers(run_id, actions)
                    changed |= self._wake_revisions(run_id, actions)
                    changed |= self._wake_parents(run_id, actions)
                    changed |= self._wake_decision_parents(run_id, actions)
                    changed |= self._wake_idle_workers(run_id, actions)
                changed |= self._close_decisions(run_id, actions)
                changed |= self._stop_terminal_workers(run_id, actions)
                # A Run that already failed can never complete, so every
                # independent execution still bound to it receives the same
                # durable stop intent as a cancelled Run. The observation (and
                # the drain of it) stays with the Controller.
                changed |= self._stop_failed_run_workers(run_id, actions)
                changed |= self._finish_run(run_id, actions)
            except ControlPlaneError as error:
                if error.code != "revision_conflict":
                    raise
                # Another Scheduler (or a restart overlap) won the CAS. The
                # next pass re-reads durable state, so quota and dispatch
                # decisions are made from what was actually committed.
                changed = True
                continue
            if not changed:
                break
        snap = self._snapshot(run_id)
        return {"run_id": run_id, "actions": actions,
                "run": snap["run"],
                "nodes": {node["node_id"]: node["state"] for node in snap["nodes"]},
                "workers": {worker["worker_id"]: worker["observed"]
                            for worker in snap["workers"]}}

    # -- run control -----------------------------------------------------
    def _run_control(self, run_id: str,
                     actions: list[dict[str, Any]]) -> tuple[bool, bool]:
        """Apply a committed pause/cancel/deadline before any new dispatch.

        Returns (blocked, changed). A paused Run accepts no new delivery but
        lets in-flight work finish; cancel and deadline commit a stop intent
        to every Worker first, and cancellation only becomes terminal after
        the stop facts are observed. Accepted artifacts are never touched.
        """
        snap = self._snapshot(run_id)
        run = snap["run"]
        if run["status"] in ("completed", "failed", "cancelled"):
            return True, False
        deadline = self._deadline_seconds(run)
        expired = deadline is not None and self._deadline_passed(run, deadline)
        if run["desired"] == "running" and not expired:
            if run["status"] == "paused":
                self._update_run_status(snap, "running", run_id)
                actions.append({"action": "run_resumed"})
                return True, True
            return False, False

        if run["desired"] == "paused" and not expired:
            # No new dispatch while paused; running executions finish on
            # their own, so no stop is committed and the session survives
            # for the resume. Accepting the intent is not the quiescence
            # fact: the Run only reports paused once every execution is
            # quiescent with a complete activity enumeration (A18,
            # R04-PAUSE-06).
            busy = [worker for worker in snap["workers"]
                    if not execution_quiescent(worker)]
            if busy:
                # A Controller that read the pre-pause Run can still deliver
                # one already-queued message and report active afterwards
                # (R04-PAUSE-06 late variant). An earlier `paused` is then no
                # longer an observed fact: reopen it until the execution
                # actually settles, then confirm again.
                if run["status"] == "paused":
                    self._update_run_status(snap, "running", run_id)
                    actions.append({"action": "run_pausing", "reopened": True,
                                    "workers": [worker["worker_id"]
                                                for worker in busy]})
                    return True, True
                actions.append({"action": "run_pausing",
                                "workers": [worker["worker_id"] for worker in busy]})
                return True, False
            if run["status"] != "paused":
                self._update_run_status(snap, "paused", run_id)
                actions.append({"action": "run_paused"})
                return True, True
            return True, False

        changed = False
        for worker in snap["workers"]:
            if worker["desired"] == "stopped":
                continue
            self._plan(run_id, [("worker", worker["worker_id"], worker["revision"])], [{
                "kind": "worker", "op": "update", "id": worker["worker_id"],
                "fields": {"intent_seq": worker["intent_seq"] + 1,
                           "desired": "stopped"},
            }], "run-control-stop")
            actions.append({"action": "worker_stopping",
                            "worker_id": worker["worker_id"],
                            "reason": "deadline" if expired else run["desired"]})
            changed = True
        if expired:
            if changed:
                return True, True
            self._terminalize_nodes(run_id, snap["nodes"], "failed", actions)
            self._update_run_status(snap, "failed", run_id)
            actions.append({"action": "run_deadline_exceeded",
                            "deadline_seconds": deadline})
            return True, True
        busy = [worker for worker in snap["workers"]
                if not execution_stopped(worker)]
        if busy:
            # Accepted artifacts stay as they are; cancellation is not
            # reported complete until every main and internal execution has
            # actually stopped, not merely acknowledged the stop.
            return True, changed
        if changed:
            return True, True
        self._terminalize_nodes(run_id, snap["nodes"], "cancelled", actions)
        self._update_run_status(snap, "cancelled", run_id)
        actions.append({"action": "run_cancelled"})
        return True, True

    def _terminalize_nodes(self, run_id: str, nodes: Iterable[Mapping[str, Any]],
                           state: str, actions: list[dict[str, Any]]) -> None:
        for node in nodes:
            if node["state"] in ("succeeded", "failed", "cancelled"):
                continue
            # An unresolved side-effect keeps its unknown outcome; a deadline
            # failure of the Run does not turn "unknown" into "failed".
            if state == "failed" and node["state"] == "blocked":
                continue
            self._plan(run_id, [("node", node["node_id"], node["revision"])], [{
                "kind": "node", "op": "update", "id": node["node_id"],
                "fields": {"state": state},
            }], f"run-control-node-{state}")
            actions.append({"action": "node_terminal", "node_id": node["node_id"],
                            "state": state})

    @staticmethod
    def _deadline_seconds(run: Mapping[str, Any]) -> int | None:
        value = (run.get("budget") or {}).get("run_deadline_seconds")
        return int(value) if isinstance(value, (int, float)) and value > 0 else None

    @staticmethod
    def _deadline_passed(run: Mapping[str, Any], seconds: int) -> bool:
        created = run.get("created_at")
        if not isinstance(created, str):
            return False
        try:
            started = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except ValueError:
            return False
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - started).total_seconds() > seconds

    # -- failure propagation ---------------------------------------------
    def _observation_error(self, run_id: str,
                           worker: Mapping[str, Any]) -> dict[str, Any] | None:
        record = self.api.latest_observation(self.actor(run_id), worker["run_id"],
                                             worker["worker_id"])
        if record is None:
            return None
        error = (record.get("payload") or {}).get("error")
        return dict(error) if isinstance(error, Mapping) else None

    def _application_plan(self, run_id: str, snap: Mapping[str, Any],
                          node: Mapping[str, Any], mutations: list[dict[str, Any]],
                          tag: str) -> None:
        # The Run is read even though only the Node changes: pause/cancel bumps
        # its revision, so a plan decided before that intent can never apply.
        self._plan(run_id,
                   [("node", node["node_id"], node["revision"]),
                    ("run", run_id, snap["run"]["revision"])],
                   mutations, tag)

    def _apply_candidates(self, run_id: str,
                          actions: list[dict[str, Any]]) -> bool:
        """Apply a committed candidate fact to its Node's review state.

        Only an undecided candidate is applied: a decision arriving after the
        candidate remains a review fact to apply, never a second candidate
        transition. Applying is idempotent by Node state, so a restart can
        replay it without an in-memory seen set.
        """
        snap = self._snapshot(run_id)
        changed = False
        for node in snap["nodes"]:
            candidate = L.undecided_candidate(node, snap["candidates"])
            if candidate is None:
                continue
            try:
                mutations = L.candidate_transition(
                    node, candidate, run_desired=snap["run"]["desired"])
            except ValueError as error:
                self._fail_node(run_id, node, "candidate_fact_mismatch", actions,
                                candidate_id=candidate["candidate_id"],
                                detail=str(error))
                changed = True
                snap = self._snapshot(run_id)
                continue
            if not mutations:
                continue
            self._application_plan(run_id, snap, node, mutations, "candidate-applied")
            actions.append({"action": "candidate_applied",
                            "node_id": node["node_id"],
                            "candidate_id": candidate["candidate_id"]})
            changed = True
            snap = self._snapshot(run_id)
        return changed

    def _apply_reviews(self, run_id: str,
                       actions: list[dict[str, Any]]) -> bool:
        """Apply a committed review decision to its Node's task state.

        Accept sets succeeded and the accepted candidate; revise returns an
        awaiting Node to running. An already-applied decision is a no-op by
        Node state, so exactly-once holds across Scheduler restarts.
        """
        snap = self._snapshot(run_id)
        changed = False
        for node in snap["nodes"]:
            candidate = L.current_candidate(node, snap["candidates"])
            if candidate is None or candidate.get("decision") is None:
                continue
            if candidate.get("contract_ref") != node.get("contract_ref"):
                # An applied acceptance revision superseded this candidate
                # version. Its verdict is already history; re-applying it under
                # the new frozen local contract would be a false mismatch.
                continue
            review = next(
                (item for item in reversed(snap["reviews"])
                 if item["node_id"] == node["node_id"]
                 and item["candidate_id"] == candidate["candidate_id"]), None)
            if review is None:
                continue
            try:
                mutations = L.review_transition(
                    node, candidate, review, run_desired=snap["run"]["desired"])
            except ValueError as error:
                self._fail_node(run_id, node, "review_fact_mismatch", actions,
                                candidate_id=candidate["candidate_id"],
                                event_id=review.get("event_id"), detail=str(error))
                changed = True
                snap = self._snapshot(run_id)
                continue
            if not mutations:
                continue
            self._application_plan(run_id, snap, node, mutations, "review-applied")
            actions.append({"action": "review_applied",
                            "node_id": node["node_id"],
                            "candidate_id": candidate["candidate_id"],
                            "verdict": review["verdict"]})
            changed = True
            snap = self._snapshot(run_id)
        return changed

    def _fail_node(self, run_id: str, node: Mapping[str, Any], reason: str,
                   actions: list[dict[str, Any]], **details: Any) -> None:
        self._plan(run_id, [("node", node["node_id"], node["revision"])], [{
            "kind": "node", "op": "update", "id": node["node_id"],
            "fields": {"state": "failed"},
        }], f"node-failed-{reason}")
        actions.append({"action": "node_failed", "node_id": node["node_id"],
                        "reason": reason, **details})

    def _block_node(self, run_id: str, node: Mapping[str, Any], reason: str,
                    actions: list[dict[str, Any]], **details: Any) -> None:
        self._plan(run_id, [("node", node["node_id"], node["revision"])], [{
            "kind": "node", "op": "update", "id": node["node_id"],
            "fields": {"state": "blocked"},
        }], f"node-blocked-{reason}")
        actions.append({"action": "node_blocked", "node_id": node["node_id"],
                        "reason": reason, **details})

    def _cancel_node(self, run_id: str, node: Mapping[str, Any], reason: str,
                     actions: list[dict[str, Any]]) -> None:
        self._plan(run_id, [("node", node["node_id"], node["revision"])], [{
            "kind": "node", "op": "update", "id": node["node_id"],
            "fields": {"state": "cancelled"},
        }], f"node-cancelled-{reason}")
        actions.append({"action": "node_cancelled", "node_id": node["node_id"],
                        "reason": reason})

    def _apply_execution_failures(self, run_id: str,
                                  actions: list[dict[str, Any]]) -> bool:
        """Turn committed execution facts into explicit Node state.

        A Node is a business task, so a failed process or an unresolved
        side-effect cannot leave it "running" forever. A submitted candidate
        still owns its review path; only a Worker failure without one fails
        the Node. Unknown effects are never replayed: the Node is blocked for
        reconciliation and keeps its Worker observation.
        """
        snap = self._snapshot(run_id)
        changed = False
        for worker in snap["workers"]:
            node = snap["by_id"].get(worker["node_id"])
            if node is None or node["state"] in ("succeeded", "failed", "cancelled"):
                continue
            observed = worker["observed"]
            if observed == "failed":
                # A committed candidate owns its review path. The fact, not the
                # Node state, decides this: the process may die in the window
                # before the candidate fact has been applied.
                if L.undecided_candidate(node, snap["candidates"]) is not None:
                    continue
                self._fail_node(run_id, node, "worker_failed", actions,
                                worker_id=worker["worker_id"],
                                error=self._observation_error(run_id, worker))
                changed = True
            elif observed == "unknown" and node["state"] != "blocked":
                self._block_node(run_id, node, "unknown_effect", actions,
                                 worker_id=worker["worker_id"],
                                 error=self._observation_error(run_id, worker))
                changed = True
        return changed

    def _propagate_failures(self, run_id: str,
                            actions: list[dict[str, Any]]) -> bool:
        """A required group cannot succeed once one member failed or blocked.

        The default fail policy from the execution spec applies to declared
        dependencies, expansion children (``all`` join) and review closures.
        Siblings of a dead group are cancelled - not failed - so accepted
        artifacts are kept and their executors receive a stop intent through
        ``_stop_terminal_workers``.
        """
        snap = self._snapshot(run_id)
        terminal = ("succeeded", "failed", "cancelled")
        changed = False
        planned: set[str] = set()

        for node in snap["nodes"]:
            closure = node.get("closure")
            if not closure or node["state"] not in ("failed", "cancelled", "blocked"):
                continue
            target = snap["by_id"].get(closure["target_node_id"])
            if (target is None or target["node_id"] in planned
                    or target["state"] in terminal or target["state"] == "blocked"):
                continue
            if node["state"] == "blocked":
                self._block_node(run_id, target, "review_blocked", actions,
                                 review_node_id=node["node_id"])
            else:
                self._fail_node(run_id, target, "review_failed", actions,
                                review_node_id=node["node_id"],
                                review_state=node["state"])
            planned.add(target["node_id"])
            changed = True

        for node in snap["nodes"]:
            if (node["node_id"] in planned or node["state"] in terminal
                    or node["state"] == "blocked"):
                continue
            failed = [dep for dep in node["dependencies"]
                      if snap["by_id"][dep]["state"] in ("failed", "cancelled")]
            blocked = [dep for dep in node["dependencies"]
                       if snap["by_id"][dep]["state"] == "blocked"]
            if failed:
                self._fail_node(run_id, node, "dependency_failed", actions,
                                dependencies=failed)
            elif blocked:
                self._block_node(run_id, node, "dependency_blocked", actions,
                                 dependencies=blocked)
            else:
                continue
            planned.add(node["node_id"])
            changed = True

        for expansion_id in snap["expansions"]:
            children = [node for node in snap["nodes"]
                        if node.get("expansion_id") == expansion_id]
            if not children:
                continue
            parent = snap["by_id"].get(children[0]["parent_id"])
            if parent is None or parent["state"] in terminal or \
                    parent["state"] == "blocked":
                if parent is not None and parent["state"] in ("failed", "cancelled"):
                    changed |= self._cancel_orphans(run_id, children, planned, actions)
                continue
            failed = [child for child in children
                      if child["state"] in ("failed", "cancelled")]
            blocked = [child for child in children
                       if child["state"] == "blocked"]
            if failed:
                if parent["node_id"] not in planned:
                    self._fail_node(run_id, parent, "required_child_failed", actions,
                                    expansion_id=expansion_id,
                                    children=[child["node_id"] for child in failed])
                    planned.add(parent["node_id"])
                changed |= self._cancel_orphans(run_id, children, planned, actions)
                changed = True
            elif blocked and parent["node_id"] not in planned:
                self._block_node(run_id, parent, "required_child_blocked", actions,
                                 expansion_id=expansion_id,
                                 children=[child["node_id"] for child in blocked])
                planned.add(parent["node_id"])
                changed = True
        return changed

    def _cancel_orphans(self, run_id: str, children: Iterable[Mapping[str, Any]],
                        planned: set[str],
                        actions: list[dict[str, Any]]) -> bool:
        changed = False
        for child in children:
            if child["node_id"] in planned or child["state"] not in (
                    "pending", "ready", "running", "waiting_decision",
                    "waiting_children", "awaiting_review"):
                continue
            self._cancel_node(run_id, child, "required_group_failed", actions)
            planned.add(child["node_id"])
            changed = True
        return changed

    # -- expansions ------------------------------------------------------
    def _apply_expansions(self, run_id: str, actions: list[dict[str, Any]]) -> bool:
        snap = self._snapshot(run_id)
        pending = [item for item in snap["expansion_requests"]
                   if item["state"] == "requested"]
        if not pending:
            return False
        policy = P.policy_for(snap["run"]["policy_ref"], self.policies)
        bundle = self.api.definition_bundle_for_run(self.actor(run_id), run_id)
        definition = dict(snap["definition"])
        # Policies read the approved pinned TaskDefinitions, never a Graph
        # position alias or an old Graph /1 reviewer map.
        definition["task_definitions"] = bundle.tasks
        definition["agent_definitions"] = bundle.agents
        changed = False
        for item in pending:
            snap = self._snapshot(run_id)
            request = self.api.get_resource(self.actor(run_id), "expansion_request",
                                            item["expansion_id"])
            if request["state"] != "requested":
                continue
            parent = snap["by_id"].get(request["node_id"])
            if parent is None or parent["state"] in ("succeeded", "failed",
                                                     "cancelled", "blocked"):
                continue
            try:
                policy.validate_expansion(definition, parent, request["payload"])
                self._plan(run_id, [("node", parent["node_id"], parent["revision"])], [{
                    "kind": "expansion", "op": "apply",
                    "id": request["expansion_id"],
                }], "apply-expansion")
                actions.append({"action": "expansion_applied",
                                "expansion_id": request["expansion_id"]})
            except ControlPlaneError as error:
                if error.code == "revision_conflict":
                    # CAS contention is retryable; the next pass reads fresh state.
                    changed = True
                    continue
                self.api.reject_expansion(
                    self.actor(run_id), request["expansion_id"],
                    {"code": error.code, "message": error.message,
                     "details": dict(error.details)},
                    self._rid(run_id, "reject-expansion",
                              {"expansion_id": request["expansion_id"],
                               "reason": {"code": error.code,
                                          "message": error.message,
                                          "details": dict(error.details)}}))
                actions.append({"action": "expansion_rejected",
                                "expansion_id": request["expansion_id"],
                                "code": error.code})
            changed = True
        return changed

    # -- worker/parent decisions -----------------------------------------
    def _reconcile_acceptance_revisions(self, run_id: str,
                                         actions: list[dict[str, Any]]) -> bool:
        """W05b apply criteria intent, drain old execution, bind fresh epoch.

        Graph state/usage/history stay. Reuse existing stop observation and
        safe epoch fence; all owned work must really stop before rebinding.
        A fresh immutable effective context contains frozen upper plus local
        criteria. Ensure a committed restart delivery after crashes; preserve
        child control work and cumulative budgets, never fake safe replay.
        """
        snap = self._snapshot(run_id)
        changed = False
        for node in list(snap["nodes"]):
            acceptance = node.get("acceptance") or {}
            pending = acceptance.get("pending")
            if pending is not None:
                if not self._revision_apply_ready(snap, node):
                    continue
                try:
                    self._plan(
                        run_id,
                        [("node", node["node_id"], node["revision"]),
                         ("run", run_id, snap["run"]["revision"])],
                        [{"kind": "acceptance_revision", "op": "apply",
                          "id": pending["source_event_id"]}],
                        "acceptance-apply")
                except ControlPlaneError as error:
                    if error.code in ("candidate_mismatch", "revision_conflict"):
                        changed |= error.code == "revision_conflict"
                        continue
                    raise
                actions.append({"action": "acceptance_revision_applied",
                                "node_id": node["node_id"],
                                "contract_ref": pending["contract_ref"],
                                "previous_ref": pending["previous_ref"],
                                "source_event_id": pending["source_event_id"]})
                changed = True
                snap = self._snapshot(run_id)
                continue
            if not acceptance.get("local"):
                continue
            worker = (snap["worker_by_node"].get(node["node_id"]) or [None])[0]
            if worker is None:
                continue
            context = worker.get("context") or {}
            if context.get("contract_ref") == node["contract_ref"]:
                key = L.acceptance_key(worker["worker_id"], worker["epoch"])
                if key in self._seen_keys(run_id, worker):
                    continue
                bundle = self.api.definition_bundle_for_run(
                    self.actor(run_id), run_id)
                payload = self._acceptance_revised_payload(
                    snap, node, worker, bundle=bundle)
                if self._enqueue(run_id, worker, "acceptance-revised", payload,
                                 actions, key=key):
                    actions.append({"action": "acceptance_revision_delivered",
                                    "node_id": node["node_id"],
                                    "worker_id": worker["worker_id"],
                                    "epoch": worker["epoch"],
                                    "delivery_key": key})
                    changed = True
                    snap = self._snapshot(run_id)
                continue
            if worker["desired"] != "stopped":
                self._plan(
                    run_id, [("worker", worker["worker_id"], worker["revision"])],
                    [{"kind": "worker", "op": "update", "id": worker["worker_id"],
                      "fields": {"intent_seq": worker["intent_seq"] + 1,
                                 "desired": "stopped"}}],
                    "acceptance-stop-old")
                actions.append({"action": "acceptance_execution_stopping",
                                "node_id": node["node_id"],
                                "worker_id": worker["worker_id"],
                                "epoch": worker["epoch"]})
                changed = True
                snap = self._snapshot(run_id)
                continue
            if (worker["observed"] not in ("stopped", "failed")
                    or not execution_stopped(worker)):
                # The main process may be gone while a tick or nested activity
                # is still active; replacement waits for complete ownership.
                continue
            pending_keys = self._pending_keys(run_id, worker)
            if pending_keys:
                self.api.supersede_inbox(
                    self.actor(run_id), worker["worker_id"], worker["epoch"],
                    self._rid(run_id, "acceptance-supersede",
                              {"worker_id": worker["worker_id"],
                               "epoch": worker["epoch"],
                               "keys": sorted(pending_keys)}))
                actions.append({"action": "acceptance_inbox_superseded",
                                "node_id": node["node_id"],
                                "worker_id": worker["worker_id"],
                                "epoch": worker["epoch"],
                                "delivery_keys": sorted(pending_keys)})
                changed = True
                snap = self._snapshot(run_id)
                continue
            new_epoch = worker["epoch"] + 1
            status = self.api.acceptance_status(
                self.actor(run_id), run_id, node["node_id"])
            bundle = self.api.definition_bundle_for_run(
                self.actor(run_id), run_id)
            task = bundle.tasks.get(node["template_ref"])
            agent = (bundle.agents.get(task.agent_ref)
                     if task is not None else None)
            if task is None or agent is None:
                self._fail_node(run_id, node, "definition_missing", actions,
                                template_ref=node["template_ref"])
                changed = True
                snap = self._snapshot(run_id)
                continue
            closure = node.get("closure")
            candidate = None
            if closure is not None:
                candidate = next(
                    (item for item in snap["candidates"]
                     if item["candidate_id"] == closure["candidate_id"]), None)
            try:
                spec = compose_worker_spec(
                    run_id=run_id, node=node,
                    worker_id=worker["worker_id"], task=task, agent=agent,
                    capabilities=self.api.backend_capabilities.get(
                        agent.backend_ref),
                    candidate=candidate, epoch=new_epoch, desired="active",
                    run_budget=snap["run"]["budget"] or {},
                    contract=status["current"]["contract"],
                    upper_contract_ref=status["upper"]["contract_ref"],
                    revision_descriptor=status["current"],
                )
            except ControlPlaneError as error:
                if error.code not in _UNEXECUTABLE_BINDING_CODES:
                    raise
                self._fail_node(run_id, node, "binding_unexecutable", actions,
                                error_code=error.code, error=error.details)
                changed = True
                snap = self._snapshot(run_id)
                continue
            fields = {
                "intent_seq": worker["intent_seq"] + 1,
                "desired": "active",
                "epoch": new_epoch,
                "context": dict(spec.context),
                "tool_profile": dict(spec.tool_profile),
                "budget": dict(spec.budget),
                "required_capabilities": dict(spec.required_capabilities),
            }
            try:
                self._plan(
                    run_id,
                    [("worker", worker["worker_id"], worker["revision"]),
                     ("node", node["node_id"], node["revision"])],
                    [{"kind": "worker", "op": "update",
                      "id": worker["worker_id"], "fields": fields}],
                    "acceptance-rebind")
            except ControlPlaneError as error:
                if error.code == "revision_conflict":
                    changed = True
                    continue
                raise
            actions.append({"action": "acceptance_execution_rebound",
                            "node_id": node["node_id"],
                            "worker_id": worker["worker_id"],
                            "epoch": new_epoch,
                            "contract_ref": node["contract_ref"]})
            changed = True
            snap = self._snapshot(run_id)
        return changed

    @staticmethod
    def _revision_apply_ready(snap: Mapping[str, Any],
                              node: Mapping[str, Any]) -> bool:
        if node["state"] != "running":
            return False
        if L.undecided_candidate(node, snap["candidates"]) is not None:
            return False
        if any(decision["node_id"] == node["node_id"]
               and decision["state"] == "open"
               for decision in snap["decision_requests"]):
            return False
        current = L.current_candidate(node, snap["candidates"])
        if current is not None and current.get("decision") == "accept":
            return False
        return True

    def _acceptance_revised_payload(self, snap: Mapping[str, Any],
                                    node: Mapping[str, Any],
                                    worker: Mapping[str, Any],
                                    *, bundle: T.DefinitionBundle) -> dict[str, Any]:
        """Fresh-epoch hand-off: new contract, both bounds and past feedback."""
        status = self.api.acceptance_status(
            self.actor(node["run_id"]), node["run_id"], node["node_id"])
        feedback = [
            {"candidate_id": review["candidate_id"],
             "verdict": review["verdict"],
             "feedback_ref": dict(review["feedback_ref"])}
            for review in snap["reviews"]
            if review["node_id"] == node["node_id"]
            and review["verdict"] in ("revise", "expand")]
        children = [child for child in snap["nodes"]
                    if child.get("parent_id") == node["node_id"]
                    and child.get("expansion_id") is not None
                    and not child.get("closure")]
        accepted = [
            {"node_id": child["node_id"],
             "candidate_id": child["accepted_candidate_ref"]}
            for child in children if child["state"] == "succeeded"]
        open_subtasks = [
            {"node_id": child["node_id"], "state": child["state"]}
            for child in children
            if child["state"] not in ("succeeded", "failed", "cancelled")]
        return {
            "kind": "acceptance_revised",
            "role": "worker",
            "brief": (f"The acceptance contract for node {node['node_id']} was "
                      "revised by its parent. Continue this task under the new "
                      "version; the frozen upper bound still applies."),
            "node_id": node["node_id"],
            "template_ref": node["template_ref"],
            "contract_ref": node["contract_ref"],
            "previous_contract_ref": status["current"]["previous_ref"],
            "upper_contract_ref": status["upper"]["contract_ref"],
            "acceptance": status["current"]["contract"]["acceptance"],
            "upper_acceptance": status["upper"]["contract"]["acceptance"],
            "output_schema": status["current"]["contract"]["output_schema"],
            "upper_output_schema": status["upper"]["contract"]["output_schema"],
            "criteria_ref": status["current"].get("criteria_ref"),
            "source_ref": status["current"].get("source_ref"),
            "input_refs": list(node.get("input_refs") or []),
            "round": int(node["round"]) + 1,
            "prior_feedback": feedback,
            "accepted_children": accepted,
            "open_subtasks": open_subtasks,
            "worker_id": worker["worker_id"],
            "epoch": worker["epoch"],
        }

    def _reconcile_goal_progress(self, run_id: str,
                                 actions: list[dict[str, Any]]) -> bool:
        """W04b before ordinary rework: consume evaluator facts, propose handoff.

        Use API progress_status/shared fold, committed run/node versions and
        frozen limit. Same trigger proposes one parent decision via existing
        commit_plan; parent answers/redecomposes through existing tools. No
        business score, Controller routing or reset on process restart.
        """
        snap = self._snapshot(run_id)
        changed = False
        for node in snap["nodes"]:
            if node["state"] != "running":
                continue
            status = self.api.progress_status(
                self.actor(run_id), run_id, node["node_id"])
            if not status.get("enabled"):
                continue
            summary = status["summary"]
            if not summary["stalled"]:
                continue
            decision_id = status["decision_id"]
            if status.get("decision_conflict") is not None:
                # A foreign request occupies this trigger's derived id. Report
                # it explicitly instead of treating it as the system handoff.
                self._fail_node(
                    run_id, node, "progress_handoff_unavailable", actions,
                    code="idempotency_conflict",
                    detail="a foreign decision request occupies the progress trigger id",
                    comparison_ref=status["comparison_ref"],
                    trigger_checkpoint_id=summary["trigger_checkpoint_id"])
                changed = True
                snap = self._snapshot(run_id)
                continue
            # One trigger point owns exactly one system request across restart,
            # re-reporting, ordinary continues and epoch replacement. Only an
            # already-provided system request has a decision_state.
            if status.get("decision_state") is not None:
                continue
            if any(item["node_id"] == node["node_id"]
                   and item["state"] == "open"
                   for item in snap["decision_requests"]):
                continue
            mutation = {
                "kind": "decision_request", "op": "create",
                "id": decision_id,
                "fields": {
                    "source": "progress_stalled",
                    "node_id": node["node_id"],
                    "comparison_ref": status["comparison_ref"],
                    "trigger_checkpoint_id": summary["trigger_checkpoint_id"],
                },
            }
            try:
                self._plan(run_id,
                           [("node", node["node_id"], node["revision"]),
                            ("run", run_id, snap["run"]["revision"])],
                           [mutation], "progress-stalled")
            except ControlPlaneError as error:
                if error.code == "revision_conflict":
                    changed = True
                    continue
                if error.code == "decision_conflict":
                    # A concurrent handoff already owns this Node; the next
                    # pass re-reads the committed request.
                    changed = True
                    continue
                if error.code in _UNEXECUTABLE_BINDING_CODES:
                    # No parent/authority or a broken frozen window: report
                    # explicitly and fail instead of faking a handoff.
                    self._fail_node(
                        run_id, node, "progress_handoff_unavailable", actions,
                        code=error.code, detail=error.message,
                        comparison_ref=status["comparison_ref"],
                        trigger_checkpoint_id=summary["trigger_checkpoint_id"])
                    changed = True
                    snap = self._snapshot(run_id)
                    continue
                raise
            actions.append({"action": "progress_stalled_requested",
                            "node_id": node["node_id"],
                            "decision_id": decision_id,
                            "trigger_checkpoint_id":
                                summary["trigger_checkpoint_id"]})
            changed = True
            snap = self._snapshot(run_id)
        return changed

    def _apply_decisions(self, run_id: str,
                         actions: list[dict[str, Any]]) -> bool:
        """Consume committed decision facts into Node waits and resumes.

        The API records an open request and a resolved reply; only the
        Scheduler decides that the child waits, that it resumes, and in which
        order the reply is delivered. Keeping those transitions here is what
        prevents a reply from being preempted by an older queued delivery.
        """
        snap = self._snapshot(run_id)
        changed = False
        # A Node may request a decision more than once. Only its latest row
        # describes the current wait; an older resolved row must not resume a
        # Node that is now waiting for a different answer.
        latest: dict[str, str] = {}
        for decision in snap["decision_requests"]:
            latest[decision["node_id"]] = decision["decision_id"]
        for decision in snap["decision_requests"]:
            if decision["state"] != "open":
                continue
            node = snap["by_id"].get(decision["node_id"])
            if node is None or node["state"] != "running":
                continue
            self._plan(run_id, [("node", node["node_id"], node["revision"])], [{
                "kind": "node", "op": "update", "id": node["node_id"],
                "fields": {"state": "waiting_decision"},
            }], "decision-wait")
            actions.append({"action": "node_waiting_decision",
                            "node_id": node["node_id"],
                            "decision_id": decision["decision_id"]})
            changed = True
            snap = self._snapshot(run_id)
        for decision in snap["decision_requests"]:
            if decision["state"] != "resolved":
                continue
            if latest.get(decision["node_id"]) != decision["decision_id"]:
                continue
            node = snap["by_id"].get(decision["node_id"])
            if node is None or node["state"] != "waiting_decision":
                continue
            worker = (snap["worker_by_node"].get(node["node_id"]) or [None])[0]
            if worker is None:
                self._fail_node(run_id, node, "decision_worker_missing", actions,
                                decision_id=decision["decision_id"])
                changed = True
                snap = self._snapshot(run_id)
                continue
            if worker["desired"] == "stopped":
                continue
            if worker["observed"] == "failed":
                self._fail_node(run_id, node, "decision_worker_failed", actions,
                                decision_id=decision["decision_id"],
                                worker_id=worker["worker_id"],
                                error=self._observation_error(run_id, worker))
                changed = True
                snap = self._snapshot(run_id)
                continue
            key = decision_reply_key(decision["decision_id"])
            if key not in self._seen_keys(run_id, worker):
                if not self._dispatch_allowed(run_id, snap, worker, key):
                    continue
                reply = decision.get("reply") or {}
                payload = {
                    "kind": "decision_resolved",
                    "role": "worker",
                    "brief": (f"Parent node {decision['parent_id']} answered decision "
                              f"{decision['decision_id']}. Continue your task."),
                    "node_id": node["node_id"],
                    "decision_id": decision["decision_id"],
                    "parent_id": decision["parent_id"],
                    "child_node_id": node["node_id"],
                    "question": decision["question"],
                    "answer": reply.get("answer"),
                    "option": reply.get("option"),
                }
                if not self._enqueue(run_id, worker, "decision-reply", payload,
                                     actions, key=key):
                    continue
                changed = True
                snap = self._snapshot(run_id)
                node = snap["by_id"][decision["node_id"]]
            if node["state"] != "waiting_decision":
                continue
            self._plan(run_id, [("node", node["node_id"], node["revision"])], [{
                "kind": "node", "op": "update", "id": node["node_id"],
                "fields": {"state": "running"},
            }], "decision-resume")
            actions.append({"action": "node_resumed",
                            "node_id": node["node_id"],
                            "decision_id": decision["decision_id"],
                            "delivery_key": key})
            changed = True
            snap = self._snapshot(run_id)
        return changed

    def _close_decisions(self, run_id: str,
                         actions: list[dict[str, Any]]) -> bool:
        """Cancel an open decision once its Node can no longer wait.

        Closing is a durable Scheduler decision; the API only records the
        cancellation event and releases the one-open-decision invariant.
        """
        snap = self._snapshot(run_id)
        changed = False
        for decision in snap["decision_requests"]:
            if decision["state"] != "open":
                continue
            node = snap["by_id"].get(decision["node_id"])
            if node is not None and node["state"] not in (
                    "succeeded", "failed", "cancelled", "blocked"):
                continue
            reason = {"code": "node_not_waiting",
                      "node_state": node["state"] if node is not None else "missing"}
            try:
                self.api.cancel_decision(
                    self.actor(run_id), decision["decision_id"], reason,
                    self._rid(run_id, "cancel-decision",
                              {"decision_id": decision["decision_id"],
                               "reason": reason}))
            except ControlPlaneError as error:
                if error.code not in ("decision_conflict", "not_found"):
                    raise
                changed = True
                continue
            actions.append({"action": "decision_cancelled",
                            "node_id": decision["node_id"],
                            "decision_id": decision["decision_id"],
                            "reason": reason["code"]})
            changed = True
        return changed

    # -- bootstrap -------------------------------------------------------
    def _bootstrap(self, run_id: str, actions: list[dict[str, Any]]) -> bool:
        """Materialize every stage of the frozen Graph /2 once.

        There is no synthetic whole-graph root Agent. The stored, content-pinned
        Graph declares the complete stage set, so one atomic plan creates all
        declared stages (``pending``) and starts the Run. A restart replays the
        same request identity instead of duplicating instances.
        """
        snap = self._snapshot(run_id)
        run = snap["run"]
        if run["status"] in ("completed", "failed", "cancelled"):
            return False
        if snap["nodes"]:
            if run["status"] == "pending":
                self._update_run_status(snap, "running", run_id)
                actions.append({"action": "run_started"})
                return True
            return False
        bundle = self.api.definition_bundle_for_run(self.actor(run_id), run_id)
        mutations = T.initial_graph_tasks(bundle, run_id=run_id)
        mutations.append({
            "kind": "run", "op": "update", "id": run_id,
            "fields": {
                "status": "running",
                "summary": {"source": "scheduler",
                            "stages": [mutation["fields"]["local_child_id"]
                                       for mutation in mutations
                                       if mutation["kind"] == "node"]},
            },
        })
        self._plan(run_id, [("run", run_id, run["revision"])], mutations,
                   "bootstrap")
        actions.append({"action": "run_bootstrapped",
                        "stages": [mutation["fields"]["local_child_id"]
                                   for mutation in mutations
                                   if mutation["kind"] == "node"]})
        return True

    # -- review nodes and closure ---------------------------------------
    def _ensure_review_node(self, run_id: str, target: Mapping[str, Any],
                            candidate: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        """Instantiate the declared review_receipt Task for one candidate.

        The Node identity, its Worker identity, literal input bindings and the
        closure all derive from the committed target/candidate; none is chosen
        by a model or by Graph position. A repeated scan returns the existing
        instance and never creates a second reviewer.
        """
        snap = self._snapshot(run_id)
        fields = review_node_fields(run_id=run_id, target=target,
                                    candidate=candidate,
                                    reviewer_ref=target["reviewer_ref"])
        node_id = fields["node_id"]
        existing = snap["by_id"].get(node_id)
        if existing is not None:
            return existing, False
        self._plan(run_id,
                   [("node", target["node_id"], target["revision"])],
                   [{"kind": "node", "op": "create", "fields": fields}],
                   "ensure-review-node")
        created = self._snapshot(run_id)["by_id"].get(node_id)
        if created is None:  # pragma: no cover - the plan committed it
            raise ControlPlaneError("not_found", "review node was not committed",
                                    node_id=node_id)
        return created, True

    def _close_review_nodes(self, run_id: str,
                            actions: list[dict[str, Any]]) -> bool:
        snap = self._snapshot(run_id)
        changed = False
        for node in snap["nodes"]:
            closure = node.get("closure")
            if not closure or node["state"] in ("succeeded", "failed",
                                                "cancelled", "blocked"):
                continue
            target = snap["by_id"].get(closure["target_node_id"])
            if target is None:
                continue
            reviewer = snap["worker_by_id"].get(closure["worker_id"])
            if reviewer is None:
                continue
            candidate_id = closure.get("candidate_id") or target["accepted_candidate_ref"]
            # A declared checker receipt is identified by the executing Worker
            # and the exact bound candidate. The obligation is the verdict
            # itself: an accept closes the target, while revise/expand closes
            # this review round and leaves the target to rework. Without this a
            # revised candidate's review Node could never become terminal and
            # the Run would stay deferred forever. The receipt must also come
            # from the reviewer's current epoch, so an old execution cannot
            # close a resumed one.
            receipt = next((review for review in reversed(snap["reviews"])
                            if review["node_id"] == target["node_id"]
                            and review["candidate_id"] == candidate_id
                            and review.get("worker_id") == closure["worker_id"]
                            and review.get("epoch") == reviewer["epoch"]), None)
            if receipt is None:
                continue
            self._plan(run_id, [("node", node["node_id"], node["revision"])], [{
                "kind": "node", "op": "update", "id": node["node_id"],
                "fields": {"state": "succeeded"},
            }], "close-review")
            actions.append({"action": "review_closed", "node_id": node["node_id"],
                            "candidate_id": candidate_id})
            changed = True
        return changed

    # -- inputs ----------------------------------------------------------
    def _freeze_inputs(self, run_id: str, actions: list[dict[str, Any]]) -> bool:
        snap = self._snapshot(run_id)
        changed = False
        for node in snap["nodes"]:
            if node["input_refs"] is not None:
                continue
            if node["state"] in ("succeeded", "failed", "cancelled", "blocked"):
                continue
            dependencies = node["dependencies"]
            if any(snap["by_id"][dep]["state"] != "succeeded" for dep in dependencies):
                continue
            try:
                refs = self._resolve_refs(snap, node)
                if (self.max_context_bytes is not None and
                        sum(int(ref["size"]) for ref in refs) > self.max_context_bytes):
                    raise ControlPlaneError(
                        "budget_exhausted", "context exceeds the configured limit",
                        size=sum(int(ref["size"]) for ref in refs),
                        limit=self.max_context_bytes)
            except ControlPlaneError as error:
                self._plan(run_id, [("node", node["node_id"], node["revision"])], [{
                    "kind": "node", "op": "update", "id": node["node_id"],
                    "fields": {"state": "failed"},
                }], "input-failed")
                actions.append({"action": "input_failed", "node_id": node["node_id"],
                                "code": error.code})
                changed = True
                continue
            self._plan(run_id, [("node", node["node_id"], node["revision"])], [{
                "kind": "node", "op": "update", "id": node["node_id"],
                "fields": {"input_refs": refs},
            }], "freeze-inputs")
            actions.append({"action": "input_frozen", "node_id": node["node_id"],
                            "count": len(refs)})
            changed = True
        return changed

    def _resolve_refs(self, snap: Mapping[str, Any],
                      node: Mapping[str, Any]) -> list[dict[str, Any]]:
        return B.resolve_binding_refs(snap, node, node["input_bindings"])

    # -- readiness / workers ---------------------------------------------
    def _seen_keys(self, run_id: str, worker: Mapping[str, Any]) -> set[str]:
        """Delivery keys already durable for this Worker epoch.

        Binding a wake-up to its committed notification (pending or delivered)
        is what makes repeated scans of old facts idempotent, independent of
        how a backend or receipt reports ``duplicate``.
        """
        inbox = self.api.list_inbox(self.actor(run_id), worker["worker_id"])
        return {message["delivery_key"] for message in inbox["messages"]
                if message["epoch"] == worker["epoch"]}

    def _pending_keys(self, run_id: str, worker: Mapping[str, Any]) -> set[str]:
        inbox = self.api.list_inbox(self.actor(run_id), worker["worker_id"])
        return {message["delivery_key"] for message in inbox["messages"]
                if message["delivery_state"] == "pending"
                and message["epoch"] == worker["epoch"]}

    def _continuations_used(self, run_id: str,
                            worker: Mapping[str, Any]) -> int:
        """Durable continuation deliveries for this logical task.

        Only messages produced by an idle-turn continuation carry the previous
        turn identity; the initial start and ordinary review deliveries do not
        count against ``max_continuations``. A version rebind starts a new
        execution epoch but must not reset the cumulative hard budget.
        """
        inbox = self.api.list_inbox(self.actor(run_id), worker["worker_id"])
        return sum(1 for message in inbox["messages"]
                   if isinstance(message.get("payload"), Mapping)
                   and "previous_turn" in message["payload"])

    def _occupies_slot(self, run_id: str, worker: Mapping[str, Any],
                       snap: Mapping[str, Any],
                       pending: set[str] | None = None) -> bool:
        """A Worker keeps its dispatch slot until proven quiescent.

        A committed but undelivered message already reserves capacity, so the
        Scheduler cannot start extra Workers before the Controller observes it.
        Idle main observation is not enough when internal activities are still
        active or the enumeration is unknown. A yielded Worker whose queued
        messages are all held by the delivery gate is not occupying anything:
        the same composed gate the Controller uses would refuse to deliver
        them, so counting the Worker would starve a parent or checker that
        still needs a capacity slot.
        """
        if pending is None:
            pending = self._pending_keys(run_id, worker)
        if not execution_quiescent(worker):
            return True
        if worker["observed"] != "idle" or not pending:
            return False
        inbox = self.api.list_inbox(self.actor(run_id), worker["worker_id"])
        progress = self.api.progress_status(
            self.actor(run_id), run_id, worker["node_id"])
        node = snap["by_id"].get(worker["node_id"])
        allowed = L.delivery_gate(worker, inbox["messages"], node,
                                  snap["candidates"], snap["decision_requests"],
                                  progress=progress,
                                  acceptance=(node or {}).get("acceptance"))
        return allowed is None or bool(pending & allowed)

    def _inflight(self, run_id: str, snap: Mapping[str, Any]) -> int:
        return sum(1 for worker in snap["workers"]
                   if self._occupies_slot(run_id, worker, snap))

    def _dispatch_allowed(self, run_id: str, snap: Mapping[str, Any],
                          worker: Mapping[str, Any], key: str) -> bool:
        pending = self._pending_keys(run_id, worker)
        if key in pending:
            return True
        node = snap["by_id"].get(worker["node_id"])
        acceptance = (node or {}).get("acceptance") or {}
        if acceptance.get("pending"):
            # The superseded execution may only stop. A decision reply it is
            # still owed must cross, exactly as the Controller gate allows;
            # blocking it here would deadlock the open decision that also
            # holds the revision application.
            inbox = self.api.list_inbox(self.actor(run_id), worker["worker_id"])
            allowed = L.decision_delivery_gate(
                worker, inbox["messages"], snap["decision_requests"])
            return allowed is not None and key in allowed
        if acceptance.get("local"):
            execution_ref = (worker.get("context") or {}).get("contract_ref")
            if execution_ref != acceptance.get("contract_ref"):
                return False
        # Caps are measured on durable usage accumulated across rework,
        # restart and epoch replacement; once met, no new delivery is
        # dispatched to this Worker.
        if self._usage_exhausted(worker, snap["run"]):
            return False
        if self._occupies_slot(run_id, worker, snap, pending):
            return True
        return self._inflight(run_id, snap) < self._capacity(snap)

    def _capacity(self, snap: Mapping[str, Any]) -> int:
        if self.max_inflight is not None:
            return self.max_inflight
        budget = snap["run"].get("budget") or {}
        if "max_inflight_nodes" in budget:
            return int(budget["max_inflight_nodes"])
        return int(budget.get("max_nodes", len(snap["nodes"]) + 1))

    @staticmethod
    def _usage_exhausted(worker: Mapping[str, Any],
                         run: Mapping[str, Any] | None = None) -> list[str]:
        """Caps already met by committed usage; no new delivery is dispatched.

        The Controller binds WorkerSpec.budget to ``worker.budget or run.budget``;
        the gate reads the same value so a Worker without its own budget is
        measured against the effective Run budget.
        """
        budget = worker.get("budget") or (run or {}).get("budget") or {}
        usage = worker.get("usage_lower_bound") or worker.get("usage") or {}
        caps = {"provider_calls": budget.get("max_provider_calls"),
                "tool_calls": budget.get("max_tool_calls")}
        return [name for name, cap in caps.items()
                if isinstance(cap, (int, float))
                and int(usage.get(name, 0) or 0) >= int(cap)]

    def _promote_ready(self, run_id: str, actions: list[dict[str, Any]]) -> bool:
        snap = self._snapshot(run_id)
        changed = False
        for node in snap["nodes"]:
            if node["state"] != "pending":
                continue
            if node["input_refs"] is None:
                continue
            if any(snap["by_id"][dep]["state"] != "succeeded"
                   for dep in node["dependencies"]):
                continue
            self._plan(run_id, [("node", node["node_id"], node["revision"])], [{
                "kind": "node", "op": "update", "id": node["node_id"],
                "fields": {"state": "ready"},
            }], "promote")
            actions.append({"action": "node_ready", "node_id": node["node_id"]})
            changed = True
        return changed

    def _start_workers(self, run_id: str, actions: list[dict[str, Any]]) -> bool:
        """Bind a ready Node to its approved Task/Agent execution.

        The Scheduler composes the effective WorkerSpec from the pinned
        Task/Agent, frozen Node material, declared delegation and the trusted
        Backend capabilities, then submits that full configuration. The Api
        recomputes it with the same rule and refuses a missing or inconsistent
        request. The start delivery carries the frozen goal and contract so no
        global worker prompt is used.
        """
        snap = self._snapshot(run_id)
        changed = False
        bundle = None
        for node in list(snap["nodes"]):
            if node["state"] not in ("ready", "running"):
                continue
            worker = (snap["worker_by_node"].get(node["node_id"]) or [None])[0]
            if worker is None:
                if node["state"] != "ready":
                    continue
                if self._inflight(run_id, snap) >= self._capacity(snap):
                    continue
                if bundle is None:
                    bundle = self.api.definition_bundle_for_run(
                        self.actor(run_id), run_id)
                task = bundle.tasks.get(node["template_ref"])
                agent = (bundle.agents.get(task.agent_ref)
                         if task is not None else None)
                if task is None or agent is None:
                    self._fail_node(run_id, node, "definition_missing", actions,
                                    template_ref=node["template_ref"])
                    changed = True
                    snap = self._snapshot(run_id)
                    continue
                worker_id = f"{node['node_id']}-w1"
                closure = node.get("closure")
                candidate = None
                if closure is not None:
                    candidate = next(
                        (item for item in snap["candidates"]
                         if item["candidate_id"] == closure["candidate_id"]),
                        None)
                acceptance = self.api.acceptance_status(
                    self.actor(run_id), run_id, node["node_id"])
                try:
                    spec = compose_worker_spec(
                        run_id=run_id,
                        node=node,
                        worker_id=worker_id,
                        task=task,
                        agent=agent,
                        capabilities=self.api.backend_capabilities.get(
                            agent.backend_ref),
                        candidate=candidate,
                        epoch=0,
                        desired="active",
                        run_budget=snap["run"]["budget"] or {},
                        contract=acceptance["current"]["contract"],
                        upper_contract_ref=acceptance["upper"]["contract_ref"],
                        revision_descriptor=(None
                                             if acceptance["current"]["origin"]
                                             == "frozen"
                                             else acceptance["current"]),
                    )
                except ControlPlaneError as error:
                    if error.code not in _UNEXECUTABLE_BINDING_CODES:
                        raise
                    self._fail_node(run_id, node, "binding_unexecutable",
                                    actions, error_code=error.code,
                                    error=error.details)
                    changed = True
                    snap = self._snapshot(run_id)
                    continue
                fields = {
                    "worker_id": worker_id,
                    "node_id": node["node_id"],
                    "backend_ref": spec.backend_ref,
                    "agent_ref": spec.agent_ref,
                    "controller_id": self.controller_id,
                    "epoch": spec.epoch,
                    "desired": spec.desired,
                    "context": dict(spec.context),
                    "tool_profile": dict(spec.tool_profile),
                    "budget": dict(spec.budget),
                    "required_capabilities": dict(spec.required_capabilities),
                }
                self._plan(
                    run_id,
                    [("node", node["node_id"], node["revision"]),
                     ("run", run_id, snap["run"]["revision"])],
                    [{"kind": "worker", "op": "create", "fields": fields},
                     {"kind": "node", "op": "update", "id": node["node_id"],
                      "fields": {"state": "running"}}],
                    "start-worker")
                actions.append({"action": "worker_created",
                                "node_id": node["node_id"],
                                "worker_id": worker_id})
                changed = True
                snap = self._snapshot(run_id)
                node = snap["by_id"][node["node_id"]]
                worker = snap["worker_by_id"][worker_id]
            if node["state"] != "running":
                continue
            start_key = f"start:{worker['worker_id']}:{worker['epoch']}"
            if start_key in self._seen_keys(run_id, worker):
                continue
            if bundle is None:
                bundle = self.api.definition_bundle_for_run(
                    self.actor(run_id), run_id)
            acceptance = self.api.acceptance_status(
                self.actor(run_id), run_id, node["node_id"])
            context = worker.get("context") or {}
            revised = (acceptance["current"]["origin"] != "frozen"
                       and context.get("contract_ref")
                       == acceptance["current"]["contract_ref"])
            if revised:
                # A crash between the epoch rebind and its hand-off must not
                # degrade into a bare start: the fresh version still owes the
                # prior feedback and the revised round.
                payload = self._acceptance_revised_payload(
                    snap, node, worker, bundle=bundle)
            else:
                payload = self._start_payload(snap, node, worker, bundle=bundle)
            if self._enqueue(run_id, worker, "acceptance-revised" if revised
                             else "start", payload, actions,
                             key=start_key):
                actions.append({"action": ("acceptance_revision_delivered"
                                           if revised else "worker_started"),
                                "node_id": node["node_id"],
                                "worker_id": worker["worker_id"],
                                "epoch": worker["epoch"],
                                "kind": payload["kind"]})
                changed = True
                snap = self._snapshot(run_id)
        return changed

    def _start_payload(self, snap: Mapping[str, Any], node: Mapping[str, Any],
                       worker: Mapping[str, Any],
                       *, bundle: T.DefinitionBundle | None = None) -> dict[str, Any]:
        """Deliver the frozen Task goal, contract and authorized material refs."""
        if bundle is None:
            bundle = self.api.definition_bundle_for_run(
                self.actor(node["run_id"]), node["run_id"])
        task = bundle.tasks.get(node["template_ref"])
        if task is None:
            raise ControlPlaneError("not_found",
                                    "the node's task definition is not in the run library",
                                    task_ref=node["template_ref"],
                                    node_id=node["node_id"])
        closure = node.get("closure")
        if closure:
            target = snap["by_id"].get(closure["target_node_id"])
            candidate = next(
                (item for item in snap["candidates"]
                 if item["candidate_id"] == closure["candidate_id"]), None)
            if target is None or candidate is None:
                raise ControlPlaneError(
                    "candidate_mismatch",
                    "the review closure no longer names a committed candidate",
                    node_id=node["node_id"])
            target_acceptance = self.api.acceptance_status(
                self.actor(node["run_id"]), node["run_id"], target["node_id"])
            payload = self._review_payload(
                target, candidate, node["reviewer_ref"],
                review_children=self._review_child_materials(snap, target),
                target_task=bundle.tasks.get(target["template_ref"]),
                target_acceptance=target_acceptance, snap=snap)
            payload.update({
                "kind": "review_start",
                "role": "reviewer",
                "goal": task.goal,
                "acceptance": task.contract["acceptance"],
                "output_schema": task.contract["output_schema"],
                "node_id": node["node_id"],
                "contract_ref": node["contract_ref"],
                "template_ref": node["template_ref"],
                "input_refs": list(node.get("input_refs") or []),
                "worker_id": worker["worker_id"],
                "epoch": worker["epoch"],
            })
            return payload
        acceptance = self.api.acceptance_status(
            self.actor(node["run_id"]), node["run_id"], node["node_id"])
        return {
            "kind": "start",
            "role": "worker",
            "round": int(node["round"]) + 1,
            "brief": task.goal,
            "node_id": node["node_id"],
            "template_ref": node["template_ref"],
            "contract_ref": node["contract_ref"],
            "upper_contract_ref": acceptance["upper"]["contract_ref"],
            "goal": task.goal,
            "acceptance": acceptance["current"]["contract"]["acceptance"],
            "upper_acceptance": acceptance["upper"]["contract"]["acceptance"],
            "output_schema": acceptance["current"]["contract"]["output_schema"],
            "upper_output_schema": acceptance["upper"]["contract"]["output_schema"],
            "criteria_ref": acceptance["current"].get("criteria_ref"),
            "input_refs": list(node.get("input_refs") or []),
            "worker_id": worker["worker_id"],
            "epoch": worker["epoch"],
        }

    # -- wake-ups --------------------------------------------------------
    def _wake_reviewers(self, run_id: str,
                        actions: list[dict[str, Any]]) -> bool:
        """Wake the actual reviewer of an undecided candidate.

        A declared review_receipt Task becomes its own bound review Node; a
        dynamic child's parent reviews through the parent Worker that owns the
        execution. Both paths use the shared delivery and capacity gates.
        """
        snap = self._snapshot(run_id)
        if not any(node["state"] == "awaiting_review" for node in snap["nodes"]):
            return False
        bundle = self.api.definition_bundle_for_run(self.actor(run_id), run_id)
        changed = False
        for node in list(snap["nodes"]):
            if node["state"] != "awaiting_review":
                continue
            candidate = L.undecided_candidate(node, snap["candidates"])
            if candidate is None:
                continue
            reviewer_ref = node["reviewer_ref"]
            review_task = bundle.tasks.get(reviewer_ref)
            if (review_task is not None
                    and review_task.completion_kind == "review_receipt"):
                review_node, created = self._ensure_review_node(
                    run_id, node, candidate)
                if created:
                    actions.append({"action": "review_node_created",
                                    "node_id": review_node["node_id"],
                                    "target_node_id": node["node_id"],
                                    "candidate_id": candidate["candidate_id"]})
                    changed = True
                    snap = self._snapshot(run_id)
                continue
            parent = (snap["by_id"].get(node["parent_id"])
                      if node["parent_id"] is not None else None)
            if parent is None or parent["state"] in (
                    "succeeded", "failed", "cancelled", "blocked"):
                # Failure propagation owns a lost review authority.
                continue
            worker = (snap["worker_by_node"].get(parent["node_id"])
                      or [None])[0]
            if worker is None:
                # The parent has no execution to review with yet; this pass
                # cannot deliver, and the next pass re-reads durable state.
                continue
            key = f"review:{candidate['candidate_id']}"
            if worker["observed"] == "failed":
                self._fail_node(run_id, node, "reviewer_failed", actions,
                                reviewer_worker_id=worker["worker_id"],
                                candidate_id=candidate["candidate_id"])
                changed = True
                snap = self._snapshot(run_id)
                continue
            exhausted = self._usage_exhausted(worker, snap["run"])
            if exhausted and key not in self._seen_keys(run_id, worker):
                self._fail_node(run_id, node, "review_budget_exhausted", actions,
                                reviewer_worker_id=worker["worker_id"],
                                candidate_id=candidate["candidate_id"],
                                budget=exhausted,
                                usage=dict(worker.get("usage_lower_bound") or worker.get("usage") or {}))
                changed = True
                snap = self._snapshot(run_id)
                continue
            if not self._dispatch_allowed(run_id, snap, worker, key):
                continue
            target_acceptance = self.api.acceptance_status(
                self.actor(run_id), run_id, node["node_id"])
            payload = self._review_payload(
                node, candidate, reviewer_ref,
                review_children=self._review_child_materials(snap, node),
                target_task=bundle.tasks.get(node["template_ref"]),
                target_acceptance=target_acceptance, snap=snap)
            if self._enqueue(run_id, worker, "review", payload, actions,
                             key=key):
                changed = True
                snap = self._snapshot(run_id)
                parent = snap["by_id"].get(node["parent_id"])
                if parent is not None and parent["state"] == "waiting_children":
                    self._plan(run_id,
                               [("node", parent["node_id"],
                                 parent["revision"])],
                               [{"kind": "node", "op": "update",
                                 "id": parent["node_id"],
                                 "fields": {"state": "running"}}],
                               "parent-wake")
                    changed = True
        return changed

    def _review_child_materials(self, snap: Mapping[str, Any],
                                node: Mapping[str, Any] | None
                                ) -> list[dict[str, Any]]:
        """Accepted expansion children of the Node under review.

        Only committed, accepted results are shown; an unfinished sibling never
        appears as review material.
        """
        if node is None:
            return []
        results: list[dict[str, Any]] = []
        for expansion_id in snap["expansions"]:
            children = [child for child in snap["nodes"]
                        if child.get("expansion_id") == expansion_id
                        and child.get("parent_id") == node["node_id"]
                        and child["state"] == "succeeded"]
            for child in sorted(children, key=lambda item: item["node_id"]):
                candidate = next(
                    (item for item in snap["candidates"]
                     if item["candidate_id"] == child.get(
                         "accepted_candidate_ref")), None)
                if candidate is None:
                    continue
                results.append({
                    "node_id": child["node_id"],
                    "candidate_id": candidate["candidate_id"],
                    "output_ids": [ref["id"] for ref in candidate["output_refs"]],
                    "output_refs": candidate["output_refs"],
                })
        return results

    def _declared_order(self, run_id: str, expansion_id: str,
                        children: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Order joined children by the frozen expansion declaration.

        The request payload is the immutable declaration. Listing applied
        expansions alphabetically or by row order would silently change the
        order the parent sees at the join.
        """
        request = self.api.get_resource(self.actor(run_id), "expansion_request",
                                        expansion_id)
        payload = request.get("payload") or {}
        declared = [child.get("local_child_id")
                    for child in payload.get("children") or []
                    if isinstance(child, Mapping)]
        rank = {name: index for index, name in enumerate(declared)
                if isinstance(name, str)}
        unknown = len(rank)
        return sorted(children, key=lambda node: (
            rank.get(node.get("local_child_id"), unknown), node["node_id"]))

    @staticmethod
    def _open_required_children(snap: Mapping[str, Any],
                                node: Mapping[str, Any]) -> list[dict[str, Any]]:
        return [{"node_id": child["node_id"], "state": child["state"]}
                for child in sorted(snap["nodes"], key=lambda item: item["node_id"])
                if child.get("parent_id") == node["node_id"]
                and child.get("expansion_id") is not None
                and not child.get("closure")
                and child["state"] != "succeeded"]



    def _review_payload(self, node: Mapping[str, Any], candidate: Mapping[str, Any],
                        reviewer_ref: str,
                        review_inputs: Mapping[str, Mapping[str, Any]] | None = None,
                        review_children: list[Mapping[str, Any]] | None = None,
                        target_task: T.TaskDefinition | None = None,
                        target_acceptance: Mapping[str, Any] | None = None,
                        snap: Mapping[str, Any] | None = None
                        ) -> dict[str, Any]:
        """Describe one exact candidate version and its authorized materials.

        The reviewed Node's own Task goal and contract travel with the review so
        the reviewer judges the committed objective rather than a global brief.
        """
        extra_refs = [dict(ref) for ref in (review_inputs or {}).values()]
        children = list(review_children or [])
        for child in children:
            extra_refs.extend(dict(ref) for ref in child.get("output_refs") or [])
        payload = {
            "kind": "review_request",
            "role": "reviewer",
            "brief": (f"Review candidate {candidate['candidate_id']} "
                      f"(round {candidate['round']}) for node {node['node_id']}. "
                      "Read every candidate output and submit exactly one "
                      "structured review."),
            "node_id": node["node_id"],
            "target_node_id": node["node_id"],
            "candidate_id": candidate["candidate_id"],
            "candidate_round": candidate["round"],
            "output_refs": [dict(ref) for ref in candidate["output_refs"]],
            "summary_ref": candidate.get("summary_ref"),
            "input_refs": extra_refs,
            "review_children": [
                {"node_id": child["node_id"],
                 "candidate_id": child["candidate_id"],
                 "output_ids": list(child.get("output_ids") or [])}
                for child in children],
            "reviewer_ref": reviewer_ref,
        }
        if target_task is not None:
            current = (target_acceptance or {}).get("current") or {}
            upper = (target_acceptance or {}).get("upper") or {}
            payload["target"] = {
                "node_id": node["node_id"],
                "template_ref": node["template_ref"],
                "contract_ref": node["contract_ref"],
                "upper_contract_ref": upper.get("contract_ref",
                                                node["template_ref"]),
                "goal": target_task.goal,
                "acceptance": (current.get("contract") or target_task.contract)[
                    "acceptance"],
                "upper_acceptance": (upper.get("contract")
                                     or target_task.contract)["acceptance"],
                "output_schema": (current.get("contract")
                                  or target_task.contract)["output_schema"],
                "upper_output_schema": (upper.get("contract")
                                        or target_task.contract)["output_schema"],
                "criteria_ref": current.get("criteria_ref"),
                "context": dict((node.get("delegation") or {}).get("context") or {}),
            }
            if target_task.progress is not None:
                # The frozen Goal policy is a review obligation, not business
                # data: the reviewer must emit one structured result per
                # candidate while the policy is enabled.
                payload["progress_policy"] = {
                    "no_improvement_limit":
                        int(target_task.progress["no_improvement_limit"]),
                    "required": True,
                }
                if snap is not None:
                    status = self.api.progress_status(
                        self.actor(node["run_id"]), node["run_id"],
                        node["node_id"])
                    previous = previous_progress_checkpoint(
                        status, list(snap["candidates"]), dict(candidate))
                    payload["progress_comparison"] = {
                        "comparison_ref": status.get("comparison_ref"),
                        "previous": previous,
                    }
                    # Historical material uses the same per-message grant and
                    # Pi materializer as every other delivery; no run-level
                    # permission is widened and no new table is added.
                    material_refs: list[dict[str, Any]] = []
                    if previous is not None:
                        material_refs.extend(previous["output_refs"])
                        if previous.get("summary_ref") is not None:
                            material_refs.append(previous["summary_ref"])
                        material_refs.append(previous["evidence_ref"])
                    seen_ids: set[str] = set()
                    unique_refs: list[dict[str, Any]] = []
                    for ref in material_refs:
                        if ref["id"] in seen_ids:
                            continue
                        seen_ids.add(ref["id"])
                        unique_refs.append(dict(ref))
                    payload["material_refs"] = unique_refs
        return payload

    def _wake_revisions(self, run_id: str,
                        actions: list[dict[str, Any]]) -> bool:
        snap = self._snapshot(run_id)
        changed = False
        for node in snap["nodes"]:
            if node["state"] != "running":
                continue
            # Only the current candidate's own review may drive the next round.
            candidate = next(
                (item for item in snap["candidates"]
                 if item["node_id"] == node["node_id"]
                 and item["round"] == node["round"]
                 and item["decision"] in ("revise", "expand")), None)
            if candidate is None:
                continue
            if candidate.get("contract_ref") != node.get("contract_ref"):
                # The applied acceptance revision superseded this round; the
                # fresh epoch receives it through the revised hand-off instead.
                continue
            review = next(
                (item for item in reversed(snap["reviews"])
                 if item["node_id"] == node["node_id"]
                 and item["candidate_id"] == candidate["candidate_id"]), None)
            if review is None or review["verdict"] != candidate["decision"]:
                continue
            worker = (snap["worker_by_node"].get(node["node_id"]) or [None])[0]
            if worker is None:
                continue
            acceptance = node.get("acceptance") or {}
            if acceptance.get("pending"):
                # A superseded execution is stopping; its old round must not
                # be failed for budget while the revision is being applied.
                continue
            if (acceptance.get("local")
                    and (worker.get("context") or {}).get("contract_ref")
                    != acceptance.get("contract_ref")):
                continue
            key = L.revision_key(candidate["candidate_id"], review["verdict"])
            exhausted = self._usage_exhausted(worker, snap["run"])
            if exhausted and key not in self._seen_keys(run_id, worker):
                # The next round would be a new delivery. Durable usage has
                # already met the cap, so the rework cannot continue; the
                # Node fails instead of waiting for a delivery that must not
                # be dispatched.
                self._plan(run_id, [("node", node["node_id"], node["revision"])], [{
                    "kind": "node", "op": "update", "id": node["node_id"],
                    "fields": {"state": "failed"},
                }], "budget-exhausted-revision")
                actions.append({"action": "budget_exhausted",
                                "node_id": node["node_id"],
                                "worker_id": worker["worker_id"],
                                "budget": exhausted,
                                "usage": dict(worker.get("usage_lower_bound") or worker.get("usage") or {}),
                                "phase": "revision"})
                changed = True
                continue
            payload = {
                "kind": "revision",
                "role": "worker",
                "brief": (f"Your candidate {candidate['candidate_id']} for node "
                          f"{node['node_id']} received verdict {review['verdict']}. "
                          "Continue in this session and submit the next round."),
                "node_id": node["node_id"],
                "candidate_id": candidate["candidate_id"],
                "verdict": review["verdict"],
                "feedback_ref": review["feedback_ref"],
                "round": int(candidate["round"]) + 1,
            }
            if not self._dispatch_allowed(run_id, snap, worker, key):
                continue
            if self._enqueue(run_id, worker, key, payload, actions, key=key):
                changed = True
                snap = self._snapshot(run_id)
        return changed

    def _wake_parents(self, run_id: str,
                      actions: list[dict[str, Any]]) -> bool:
        snap = self._snapshot(run_id)
        changed = False
        for review in snap["reviews"]:
            if review["verdict"] != "accept":
                continue
            child = snap["by_id"].get(review["node_id"])
            if child is None or child["parent_id"] is None:
                continue
            # A committed acceptance is consumable only after the Scheduler has
            # applied it to the child. An unapplied accept must not let the
            # parent run ahead of the decision fact it depends on.
            if (child["state"] != "succeeded"
                    or child.get("accepted_candidate_ref") != review["candidate_id"]):
                continue
            parent = snap["by_id"][child["parent_id"]]
            if parent["state"] in ("succeeded", "failed", "cancelled", "blocked"):
                continue
            worker = (snap["worker_by_node"].get(parent["node_id"]) or [None])[0]
            if worker is None:
                continue
            key = f"accepted:{review['candidate_id']}"
            exhausted = self._usage_exhausted(worker, snap["run"])
            if exhausted and key not in self._seen_keys(run_id, worker):
                self._fail_node(run_id, parent, "wake_budget_exhausted", actions,
                                phase="child_accepted",
                                child_node_id=child["node_id"],
                                budget=exhausted,
                                usage=dict(worker.get("usage_lower_bound") or worker.get("usage") or {}))
                changed = True
                continue
            candidate = next((item for item in snap["candidates"]
                              if item["candidate_id"] == review["candidate_id"]), None)
            payload = {
                "kind": "child_accepted",
                "role": "worker",
                "brief": (f"Child node {child['node_id']} candidate "
                          f"{review['candidate_id']} was accepted. Continue your task."),
                "node_id": parent["node_id"],
                "child_node_id": child["node_id"],
                "candidate_id": review["candidate_id"],
                "output_refs": candidate["output_refs"] if candidate else [],
            }
            if not self._dispatch_allowed(run_id, snap, worker, key):
                continue
            if self._enqueue(run_id, worker, "accepted",
                             payload, actions, key=key):
                changed = True
                snap = self._snapshot(run_id)
                parent = snap["by_id"][child["parent_id"]]
                if parent["state"] == "waiting_children":
                    self._plan(run_id, [("node", parent["node_id"],
                                         parent["revision"])], [{
                        "kind": "node", "op": "update", "id": parent["node_id"],
                        "fields": {"state": "running"},
                    }], "parent-continue")
        for expansion_id in snap["expansions"]:
            children = [node for node in snap["nodes"]
                        if node.get("expansion_id") == expansion_id]
            if not children or any(node["state"] != "succeeded" for node in children):
                continue
            children = self._declared_order(run_id, expansion_id, children)
            parent_id = children[0]["parent_id"]
            parent = snap["by_id"].get(parent_id)
            if parent is None or parent["state"] in ("succeeded", "failed",
                                                     "cancelled", "blocked"):
                continue
            worker = (snap["worker_by_node"].get(parent_id) or [None])[0]
            if worker is None:
                continue
            join_key = f"join:{expansion_id}"
            exhausted = self._usage_exhausted(worker, snap["run"])
            if exhausted and join_key not in self._seen_keys(run_id, worker):
                # The join cannot be delivered again; the parent fails instead
                # of staying in waiting_children forever.
                self._fail_node(run_id, parent, "join_budget_exhausted", actions,
                                expansion_id=expansion_id,
                                budget=exhausted,
                                usage=dict(worker.get("usage_lower_bound") or worker.get("usage") or {}))
                changed = True
                continue
            payload = {
                "kind": "children_joined",
                "role": "worker",
                "round": int(parent["round"]) + 1,
                "brief": (f"All children of expansion {expansion_id} succeeded. "
                          "Continue your task."),
                "node_id": parent_id,
                "expansion_id": expansion_id,
                "children": [{"node_id": node["node_id"],
                              "accepted_candidate_ref": node["accepted_candidate_ref"]}
                             for node in children],
            }
            if not self._dispatch_allowed(run_id, snap, worker, join_key):
                continue
            if self._enqueue(run_id, worker, "join", payload, actions,
                             key=join_key):
                changed = True
                snap = self._snapshot(run_id)
                parent = snap["by_id"].get(parent_id)
                if parent is None or parent["state"] in ("succeeded", "failed",
                                                         "cancelled"):
                    continue
                if parent["state"] == "waiting_children":
                    self._plan(run_id, [("node", parent_id, parent["revision"])], [{
                        "kind": "node", "op": "update", "id": parent_id,
                        "fields": {"state": "running"},
                    }], "parent-join")
        return changed

    def _wake_decision_parents(self, run_id: str,
                               actions: list[dict[str, Any]]) -> bool:
        """Deliver an open child decision to the real parent Worker.

        The decision authority is the parent Node's existing Worker; the
        message is the only way it learns the bound decision id. A parent that
        can no longer act fails the child explicitly instead of leaving the
        wait durable forever.
        """
        snap = self._snapshot(run_id)
        changed = False
        for decision in snap["decision_requests"]:
            if decision["state"] != "open":
                continue
            child = snap["by_id"].get(decision["node_id"])
            if child is None or child["state"] != "waiting_decision":
                continue
            parent = snap["by_id"].get(decision["parent_id"])
            if parent is None or parent["state"] in ("succeeded", "failed",
                                                     "cancelled", "blocked"):
                # Failure propagation owns terminal parents; a parent that
                # cannot decide must not keep the child waiting.
                self._fail_node(run_id, child, "decision_authority_lost", actions,
                                decision_id=decision["decision_id"],
                                parent_id=decision["parent_id"],
                                parent_state=(parent["state"] if parent else "missing"))
                changed = True
                snap = self._snapshot(run_id)
                continue
            worker = (snap["worker_by_node"].get(parent["node_id"]) or [None])[0]
            if worker is None:
                self._fail_node(run_id, child, "decision_authority_missing", actions,
                                decision_id=decision["decision_id"],
                                parent_id=parent["node_id"])
                changed = True
                snap = self._snapshot(run_id)
                continue
            if worker["desired"] == "stopped":
                continue
            if worker["observed"] == "failed":
                self._fail_node(run_id, child, "decision_authority_failed", actions,
                                decision_id=decision["decision_id"],
                                parent_id=parent["node_id"],
                                worker_id=worker["worker_id"],
                                error=self._observation_error(run_id, worker))
                changed = True
                snap = self._snapshot(run_id)
                continue
            key = decision_key(decision["decision_id"])
            if key in self._seen_keys(run_id, worker):
                # The request was delivered once; a parent turn that ended
                # without resolving it gets the same budgeted continuation as
                # any other idle turn, still bound to this decision.
                if self._continue_decision_parent(run_id, snap, decision,
                                                  worker, actions):
                    changed = True
                    snap = self._snapshot(run_id)
                continue
            exhausted = self._usage_exhausted(worker, snap["run"])
            if exhausted:
                self._fail_node(run_id, parent, "decision_budget_exhausted", actions,
                                decision_id=decision["decision_id"],
                                child_node_id=child["node_id"],
                                worker_id=worker["worker_id"],
                                budget=exhausted,
                                usage=dict(worker.get("usage_lower_bound") or worker.get("usage") or {}))
                changed = True
                snap = self._snapshot(run_id)
                continue
            if not self._dispatch_allowed(run_id, snap, worker, key):
                continue
            payload = {
                "kind": "decision_request",
                "role": "worker",
                "brief": (f"Child node {child['node_id']} is waiting for your decision "
                          f"{decision['decision_id']}. Answer it, then continue your task."),
                "node_id": parent["node_id"],
                "child_node_id": child["node_id"],
                "decision_id": decision["decision_id"],
                "question": decision["question"],
                "reason": decision["reason"],
                "options": list(decision.get("options") or []),
                "contract_ref": decision["contract_ref"],
                "round": decision["round"],
                "material_refs": list(decision.get("material_refs") or []),
            }
            if self._enqueue(run_id, worker, "decision-request", payload,
                             actions, key=key):
                actions.append({"action": "decision_request_delivered",
                                "decision_id": decision["decision_id"],
                                "parent_node_id": parent["node_id"],
                                "worker_id": worker["worker_id"]})
                changed = True
                snap = self._snapshot(run_id)
        return changed

    def _continue_decision_parent(self, run_id: str,
                                  snap: Mapping[str, Any],
                                  decision: Mapping[str, Any],
                                  worker: Mapping[str, Any],
                                  actions: list[dict[str, Any]]) -> bool:
        """Give a parent another turn while a decision stays unanswered.

        The request is delivered once. A parent in ``waiting_children`` is
        outside the ordinary idle-continuation path, so without this its first
        turn could end without calling ``node.resolve_decision`` and the child
        would wait forever. The continuation keeps the decision binding and
        the same durable caps as every other idle-turn continuation.
        """
        if worker["observed"] != "idle" or not execution_quiescent(worker):
            return False
        observation = self.api.latest_observation(
            self.actor(run_id), run_id, worker["worker_id"])
        if (observation is None
                or observation["payload"].get("observed") != "idle"
                or observation["payload"].get("epoch") != worker["epoch"]):
            return False
        turn = observation["payload"].get("delivery_key")
        if not isinstance(turn, str):
            return False
        request_key = decision_key(decision["decision_id"])
        continuations = decision_continue_prefix(decision["decision_id"])
        if any(key.startswith(continuations)
               for key in self._pending_keys(run_id, worker)):
            # A continuation for this decision is already queued; later turns
            # (another decision, a review) must not queue a second one.
            return False
        if not (turn == request_key or turn.startswith(continuations)):
            # A later delivery may have completed after this decision's turn
            # ended without an answer. That turn's committed idle observation
            # is the durable evidence; the latest turn alone is not.
            completed = self.api.observation_for(
                self.actor(run_id), run_id, worker["worker_id"], request_key)
            if (completed is None
                    or completed["payload"].get("observed") != "idle"
                    or completed["payload"].get("epoch") != worker["epoch"]):
                return False
        summary = digest({"worker_id": worker["worker_id"],
                          "epoch": worker["epoch"], "turn": turn})[7:39]
        key = f"{continuations}{summary}"
        if key in self._seen_keys(run_id, worker):
            return False
        exhausted = self._usage_exhausted(worker, snap["run"])
        parent = snap["by_id"].get(decision["parent_id"])
        if parent is None:
            return False
        if exhausted:
            self._fail_node(run_id, parent, "decision_budget_exhausted", actions,
                            decision_id=decision["decision_id"],
                            child_node_id=decision["node_id"],
                            worker_id=worker["worker_id"],
                            budget=exhausted, usage=dict(worker.get("usage_lower_bound") or worker.get("usage") or {}))
            return True
        limit = self._budget(snap, parent).get("max_continuations")
        if limit is None:
            actions.append({"action": "worker_waiting",
                            "node_id": parent["node_id"],
                            "worker_id": worker["worker_id"],
                            "reason": "continuation_not_declared"})
            return False
        used = self._continuations_used(run_id, worker)
        if used >= int(limit):
            self._fail_node(run_id, parent, "continuation_exhausted", actions,
                            phase="decision", worker_id=worker["worker_id"],
                            limit=int(limit), used=used)
            return True
        if not self._dispatch_allowed(run_id, snap, worker, key):
            return False
        payload = {
            "kind": "decision_request",
            "role": "worker",
            "brief": (f"You have not answered decision {decision['decision_id']} "
                      f"for child node {decision['node_id']} yet. Continue in this "
                      "session and call node.resolve_decision."),
            "node_id": parent["node_id"],
            "child_node_id": decision["node_id"],
            "decision_id": decision["decision_id"],
            "question": decision["question"],
            "reason": decision["reason"],
            "options": list(decision.get("options") or []),
            "contract_ref": decision["contract_ref"],
            "round": decision["round"],
            "material_refs": list(decision.get("material_refs") or []),
            "previous_turn": turn,
        }
        if not self._enqueue(run_id, worker, "decision-continue", payload,
                             actions, key=key):
            return False
        actions.append({"action": "decision_request_resumed",
                        "decision_id": decision["decision_id"],
                        "worker_id": worker["worker_id"],
                        "delivery_key": key, "previous_turn": turn})
        return True

    def _wake_idle_workers(self, run_id: str,
                           actions: list[dict[str, Any]]) -> bool:
        """Continue a Worker whose turn ended without submitting a result.

        A Node is the logical task; an Agent turn that produced no candidate
        leaves it unfinished (20-execution "keep the task incomplete and
        continue within budget"). Without a continuation fact the Worker has
        nothing to do and the Node would sit "running" until the run deadline.
        Durable usage bounds the loop: once a cap is met the Node fails with an
        explicit reason instead of dispatching a delivery the Backend refuses.
        """
        snap = self._snapshot(run_id)
        changed = False
        for node in snap["nodes"]:
            if node["state"] != "running":
                continue
            worker = (snap["worker_by_node"].get(node["node_id"]) or [None])[0]
            if (worker is None or worker["observed"] != "idle"
                    or not execution_quiescent(worker)):
                continue
            acceptance = node.get("acceptance") or {}
            if acceptance.get("pending"):
                # The superseded execution may only stop; continuation and
                # budget accounting restart with the rebound epoch.
                continue
            if (acceptance.get("local")
                    and (worker.get("context") or {}).get("contract_ref")
                    != acceptance.get("contract_ref")):
                continue
            if any(decision["node_id"] == node["node_id"]
                   and decision["state"] == "open"
                   for decision in snap["decision_requests"]):
                # An open decision owns this turn's continuation: the Worker
                # deliberately yielded and must wait for the parent reply.
                continue
            if self._open_required_children(snap, node):
                # The parent was woken only to inspect/review a child. Its own
                # turn is not the missing result; the join wake-up drives it
                # once every required child is accepted.
                continue
            if L.continuation_blocker(node, snap["candidates"]) is not None:
                # A committed candidate fact (undecided, or accepted but not
                # yet applied) owns this turn; an ordinary continuation would
                # run around the review path.
                continue
            if self._pending_keys(run_id, worker):
                continue
            observation = self.api.latest_observation(
                self.actor(run_id), run_id, worker["worker_id"])
            if (observation is None
                    or observation["payload"].get("observed") != "idle"
                    or observation["payload"].get("epoch") != worker["epoch"]):
                continue
            # One advance per completed turn. The turn's delivery identity is
            # stable across polling, Controller restarts and observation
            # version bumps that carry no new model/tool work; a genuinely new
            # turn has a new delivery key, so an old idle fact cannot spawn
            # repeated continues. This must be decided before any exhaustion
            # accounting: a continuation already dispatched for this turn has
            # not finished executing, so it cannot prove the Node spent its
            # last allowed turn.
            turn = observation["payload"].get("delivery_key")
            if not isinstance(turn, str) or not turn:
                turn = f"observation:{observation['event_id']}"
            # The delivery key becomes an events/exit/spawn/budget file name in
            # the Backend, so a new turn must not append the previous key: the
            # identity is summarised at a fixed length and the full previous
            # turn stays in the payload for audit.
            summary = digest({"worker_id": worker["worker_id"],
                              "epoch": worker["epoch"], "turn": turn})[7:39]
            key = f"continue:{summary}"
            if key in self._seen_keys(run_id, worker):
                continue
            # Durable usage caps come next: once a cap is met a delivery would
            # be refused by the Backend, so fail explicitly regardless of
            # continuation policy.
            exhausted = self._usage_exhausted(worker, snap["run"])
            if exhausted:
                self._fail_node(run_id, node, "budget_exhausted", actions,
                                phase="continuation",
                                worker_id=worker["worker_id"],
                                budget=exhausted,
                                usage=dict(worker.get("usage_lower_bound") or worker.get("usage") or {}))
                changed = True
                continue
            # A zero-usage Backend can end turns forever without ever meeting a
            # provider/tool cap. Continuation therefore needs its own frozen,
            # finite limit; without one the Node explicitly waits for external
            # input instead of spinning model deliveries.
            limit = self._budget(snap, node).get("max_continuations")
            if limit is None:
                actions.append({"action": "worker_waiting",
                                "node_id": node["node_id"],
                                "worker_id": worker["worker_id"],
                                "reason": "continuation_not_declared"})
                continue
            used = self._continuations_used(run_id, worker)
            if used >= int(limit):
                self._fail_node(run_id, node, "continuation_exhausted", actions,
                                phase="continuation",
                                worker_id=worker["worker_id"],
                                limit=int(limit), used=used)
                changed = True
                continue
            if not self._dispatch_allowed(run_id, snap, worker, key):
                continue
            if self._enqueue(run_id, worker, "continue",
                             self._continue_payload(node, worker, turn),
                             actions, key=key):
                actions.append({"action": "worker_continued",
                                "node_id": node["node_id"],
                                "worker_id": worker["worker_id"],
                                "delivery_key": key,
                                "previous_turn": turn})
                changed = True
                snap = self._snapshot(run_id)
        return changed

    @staticmethod
    def _continue_payload(node: Mapping[str, Any],
                          worker: Mapping[str, Any],
                          previous_turn: str) -> dict[str, Any]:
        if node.get("closure"):
            context = worker.get("context") or {}
            return {
                "kind": "review_request",
                "role": "reviewer",
                "brief": (f"Your previous turn ended without submitting a review. "
                          f"Continue in this session and submit exactly one "
                          f"structured review for candidate "
                          f"{context.get('candidate_id')}."),
                "node_id": node["node_id"],
                "target_node_id": context.get("target_node_id"),
                "candidate_id": context.get("candidate_id"),
                "candidate_round": context.get("candidate_round"),
                "reviewer_ref": context.get("reviewer_ref"),
                "previous_turn": previous_turn,
            }
        return {
            "kind": "continue",
            "role": "worker",
            "round": int(node["round"]) + 1,
            "brief": (f"Your previous turn ended without submitting a candidate "
                      f"for node {node['node_id']}. Continue in this session and "
                      "submit the candidate when the result is ready."),
            "node_id": node["node_id"],
            "contract_ref": node["contract_ref"],
            "worker_id": worker["worker_id"],
            "epoch": worker["epoch"],
            "previous_turn": previous_turn,
        }

    def _stop_terminal_workers(self, run_id: str,
                               actions: list[dict[str, Any]]) -> bool:
        snap = self._snapshot(run_id)
        changed = False
        terminal = {"succeeded", "failed", "cancelled"}
        for worker in snap["workers"]:
            node = snap["by_id"].get(worker["node_id"])
            if node is None or node["state"] not in terminal:
                continue
            if worker["desired"] == "stopped":
                continue
            self._plan(run_id, [("worker", worker["worker_id"], worker["revision"])], [{
                "kind": "worker", "op": "update", "id": worker["worker_id"],
                "fields": {"intent_seq": worker["intent_seq"] + 1,
                           "desired": "stopped"},
            }], "stop-worker")
            actions.append({"action": "worker_stopping",
                            "worker_id": worker["worker_id"]})
            changed = True
        return changed

    def _stop_failed_run_workers(self, run_id: str,
                                 actions: list[dict[str, Any]]) -> bool:
        """A failed Run stops every remaining execution, not only dead Nodes.

        Stage failure fails the Run while independent stages may still be
        active, idle or holding a queued start. Those Workers can never deliver
        a result the Run accepts, so the Scheduler commits the same
        ``desired="stopped"`` intent used for cancel. Only the intent is
        declared here: the observed outcome stays a Controller fact, and an
        unresolved effect keeps its ``unknown`` observation.
        """
        snap = self._snapshot(run_id)
        if snap["run"]["status"] != "failed":
            return False
        changed = False
        for worker in snap["workers"]:
            if worker["desired"] == "stopped":
                continue
            self._plan(run_id, [("worker", worker["worker_id"], worker["revision"])], [{
                "kind": "worker", "op": "update", "id": worker["worker_id"],
                "fields": {"intent_seq": worker["intent_seq"] + 1,
                           "desired": "stopped"},
            }], "stop-failed-run-worker")
            actions.append({"action": "worker_stopping",
                            "worker_id": worker["worker_id"],
                            "reason": "run_failed"})
            changed = True
        return changed

    # -- completion ------------------------------------------------------
    @staticmethod
    def _declared_stages(snap: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        """The frozen Graph /2 stage set: no synthetic whole-graph root.

        A declared stage has no parent, no expansion and no review closure.
        Expansion children and review Nodes are completion obligations, not
        stages, so they can never substitute for stage success.
        """
        return [node for node in snap["nodes"]
                if node.get("parent_id") is None
                and node.get("expansion_id") is None
                and not node.get("closure")]

    def _finish_run(self, run_id: str, actions: list[dict[str, Any]]) -> bool:
        """Aggregate the frozen declared stage set into the Run status.

        The Run completes only when every declared stage succeeded and no
        required expansion child or review Node is still open. One blocked
        stage blocks the Run (an unresolved external effect must not be
        reported as success, failure or silent waiting); reconciliation lets
        the same Run continue. A failed or cancelled stage fails the Run.
        """
        snap = self._snapshot(run_id)
        run = snap["run"]
        if run["status"] in ("completed", "failed", "cancelled"):
            return False
        if run["desired"] != "running":
            return False
        stages = self._declared_stages(snap)
        if not stages:
            return False
        states = {node["node_id"]: node["state"] for node in stages}
        run_status = run["status"]
        blocked = sorted(node["node_id"] for node in stages
                         if node["state"] == "blocked")
        if blocked:
            if run_status == "blocked":
                return False
            self._plan(run_id, [("run", run_id, run["revision"])], [{
                "kind": "run", "op": "update", "id": run_id,
                "fields": {"status": "blocked",
                           "summary": {"source": "scheduler",
                                       "reason": "unknown_effect",
                                       "stages": states,
                                       "blocked_stages": blocked,
                                       "nodes": len(snap["nodes"])}},
            }], "finish-blocked")
            actions.append({"action": "run_blocked", "reason": "unknown_effect",
                            "stages": blocked})
            return True
        if run_status == "blocked":
            # A reconciled stage lets the same Run continue from durable facts.
            self._update_run_status(snap, "running", run_id)
            actions.append({"action": "run_unblocked", "stages": sorted(states)})
            return True
        if all(node["state"] == "succeeded" for node in stages):
            # A stage's own success never implies its declared expansion or
            # review closure is done. Check the whole graph before completing.
            open_required = [{"node_id": node["node_id"], "state": node["state"]}
                             for node in snap["nodes"]
                             if node.get("expansion_id") is not None
                             and not node.get("closure")
                             and node["state"] != "succeeded"]
            dead_required = [node for node in open_required
                             if node["state"] in ("failed", "cancelled")]
            if dead_required:
                status = "failed"
                reason = "required_child_failed"
            else:
                if open_required:
                    actions.append({"action": "run_completion_deferred",
                                    "reason": "required_children_open",
                                    "children": open_required})
                    return False
                trailing = [node["node_id"] for node in snap["nodes"]
                            if node.get("closure")
                            and node["state"] not in
                            ("succeeded", "failed", "cancelled")]
                if trailing:
                    actions.append({"action": "run_completion_deferred",
                                    "reason": "review_open",
                                    "reviews": trailing})
                    return False
                status, reason = "completed", None
        elif any(node["state"] in ("failed", "cancelled") for node in stages):
            status, reason = "failed", self._stage_termination_reason(snap, stages)
        else:
            return False
        if run_status == status:
            return False
        self._plan(run_id, [("run", run_id, run["revision"])], [{
            "kind": "run", "op": "update", "id": run_id,
            "fields": {"status": status,
                       "summary": {"source": "scheduler",
                                   "reason": reason,
                                   "stages": states,
                                   "nodes": len(snap["nodes"])}},
        }], "finish")
        actions.append({"action": "run_finished", "status": status,
                        "reason": reason})
        return True

    def _stage_termination_reason(self, snap: Mapping[str, Any],
                                  stages: Iterable[Mapping[str, Any]]) -> str:
        """Name the first durable cause of stage termination, not business judgement."""
        for node in stages:
            if node["state"] not in ("failed", "cancelled"):
                continue
            for worker in snap["worker_by_node"].get(node["node_id"], []):
                if worker["observed"] == "failed":
                    return "worker_failed"
            for dependency in node["dependencies"]:
                state = snap["by_id"][dependency]["state"]
                if state in ("failed", "cancelled"):
                    return "dependency_failed"
                if state == "blocked":
                    return "dependency_blocked"
            return "stage_cancelled" if node["state"] == "cancelled" else "stage_failed"
        for expansion_id in snap["expansions"]:
            for child in snap["nodes"]:
                if child.get("expansion_id") != expansion_id:
                    continue
                if child["state"] in ("failed", "cancelled"):
                    return "required_child_failed"
                if child["state"] == "blocked":
                    return "required_child_blocked"
        for node in snap["nodes"]:
            closure = node.get("closure") or {}
            if not closure:
                continue
            if node["state"] in ("failed", "cancelled"):
                return "review_failed"
            if node["state"] == "blocked":
                return "review_blocked"
        return "failed"

    # -- budgets ---------------------------------------------------------

    def _budget(self, snap: Mapping[str, Any],
                node: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """The effective worker budget, narrowed by the committed delegation.

        ``_plan_create_worker`` composed this value with ``build_worker_spec``
        and persisted it on the Worker; re-deriving it here would be a second
        composition path. Before a Worker exists only the frozen Run budget is
        in force.
        """
        if node is not None:
            for worker in snap["worker_by_node"].get(node["node_id"], []):
                if worker.get("budget"):
                    return dict(worker["budget"])
        return dict(snap["run"].get("budget") or {})


    # -- messages --------------------------------------------------------
    def _enqueue(self, run_id: str, worker: Mapping[str, Any], tag: str,
                 payload: dict[str, Any],
                 actions: list[dict[str, Any]], *,
                 key: str | None = None) -> bool:
        delivery_key = key or f"{tag}:{worker['worker_id']}:{worker['epoch']}"
        if delivery_key in self._seen_keys(run_id, worker):
            return False
        # Execution identity is authoritative scheduler state, not something a
        # payload author may choose; every delivery carries it for the Backend.
        payload = dict(payload)
        payload.setdefault("run_id", run_id)
        payload.setdefault("worker_id", worker["worker_id"])
        payload.setdefault("epoch", worker["epoch"])
        response = self.api.enqueue_message(
            self.actor(run_id), worker["worker_id"], payload, delivery_key,
            self._rid(run_id, f"message:{tag}",
                      {"worker_id": worker["worker_id"],
                       "epoch": worker["epoch"],
                       "delivery_key": delivery_key,
                       "payload": payload}))
        if response.get("duplicate"):
            return False
        actions.append({"action": "message_enqueued", "kind": payload["kind"],
                        "worker_id": worker["worker_id"],
                        "delivery_key": delivery_key})
        return True
