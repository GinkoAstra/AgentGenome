"""Generic Worker Controller.

The Controller turns committed Worker intent into Backend actions and reports
actual observations. It never schedules graph work, chooses child tasks, selects
tools or marks a Node successful. Durable inbox items and delivery facts live in
the API; the Controller keeps no authoritative local state.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Mapping

from .agent_definitions import check_backend_capabilities
from .execution_facts import execution_stopped
from .backends.base import AgentBackend, BackendError, Observation, WorkerSpec
from .lifecycle import delivery_gate
from .resources import Actor, ControlPlaneError, digest

FORMAT = "agentgenome-control-plane-controller/1"


class Controller:
    def __init__(self, api: Any, backends: Mapping[str, AgentBackend], *,
                 controller_id: str, data_dir: str | Path | None = None,
                 poll_seconds: float = 0.1) -> None:
        if not controller_id:
            raise ValueError("controller_id is required")
        self.api = api
        self.backends = dict(backends)
        self.controller_id = controller_id
        self.root = Path(data_dir) if data_dir is not None else None
        self.poll_seconds = poll_seconds
        self._lock = threading.RLock()

    # -- identity --------------------------------------------------------
    def actor(self, run_id: str, epoch: int) -> Actor:
        return Actor(self.controller_id, {"controller"}, run_ids={run_id},
                     controller_id=self.controller_id, epoch=epoch)

    def _backend(self, worker: Mapping[str, Any]) -> AgentBackend:
        name = str(worker["backend_ref"]).split("@", 1)[0]
        backend = self.backends.get(name)
        if backend is None:
            raise ControlPlaneError("capability_unsupported",
                                    f"no backend is registered as {name!r}",
                                    backend_ref=worker["backend_ref"])
        return backend

    def _owns(self, worker: Mapping[str, Any]) -> bool:
        return worker["controller_id"] == self.controller_id

    def _spec(self, worker: Mapping[str, Any], run: Mapping[str, Any], *,
              desired: str | None = None) -> WorkerSpec:
        return WorkerSpec(
            worker_id=worker["worker_id"], node_id=worker["node_id"],
            epoch=worker["epoch"], intent_seq=worker["intent_seq"],
            backend_ref=worker["backend_ref"], agent_ref=worker["agent_ref"],
            desired=worker["desired"] if desired is None else desired,
            run_id=worker["run_id"],
            context=worker["context"], tool_profile=worker["tool_profile"],
            budget=worker["budget"] or run["budget"],
            checkpoint_ref=worker["checkpoint"],
            usage=worker.get("usage_lower_bound") or worker.get("usage") or {},
            required_capabilities=worker.get("required_capabilities") or {},
        )

    def _preflight_spec(self, spec: WorkerSpec, backend: AgentBackend) -> None:
        """W02b2 generic execution check; no definitions or business decisions.

        Scheduler/API already verify against the trusted registered registry.
        Check actual adapter requirements/native inventory again before any
        delivery. Never turn an unsupported configuration into fake stopped,
        succeeded or safe-to-replay observations. Cancellation/stop remains
        available even when a desired active spec cannot be fulfilled.
        """
        if spec.backend_ref.split("@", 1)[0] != backend.name:
            raise ControlPlaneError(
                "capability_unsupported",
                "the committed worker names a different backend",
                backend_ref=spec.backend_ref, backend=backend.name)
        capabilities = backend.capabilities()
        check_backend_capabilities(spec.required_capabilities, capabilities)
        selected = list((spec.tool_profile or {}).get("backend_tools") or [])
        if not selected:
            return
        available = capabilities.backend_tools
        if available is None:
            raise ControlPlaneError(
                "capability_unsupported",
                "the backend cannot describe its native tool inventory",
                backend=backend.name, requested=selected)
        missing = sorted(set(selected) - set(available))
        if missing:
            raise ControlPlaneError(
                "capability_unsupported",
                "the backend does not expose the selected native tools",
                backend=backend.name, missing=missing)


    def _worker_run(self, worker: Mapping[str, Any]) -> Mapping[str, Any]:
        return self.api.get_resource(self.actor(worker["run_id"], worker["epoch"]),
                                     "run", worker["run_id"])

    # -- worker reconciliation ------------------------------------------
    def reconcile(self, run_id: str, worker_id: str, *,
                  wait_seconds: float = 0.0) -> dict[str, Any]:
        with self._lock:
            worker = self._worker(run_id, worker_id)
            if not self._owns(worker):
                return {"worker_id": worker_id, "status": "controller_mismatch",
                        "controller_id": worker["controller_id"]}
            run = self._worker_run(worker)
            backend = self._backend(worker)
            spec = self._spec(worker, run)
            handle = backend.start(spec, (worker["worker_id"], worker["epoch"]))
            refresh = lambda: self._observe(handle, backend, 0.0)  # noqa: E731
            if worker["desired"] == "stopped":
                backend.stop(handle, f"stop:{worker['worker_id']}:{worker['epoch']}")
                observation = self._observe(handle, backend, wait_seconds)
                reported = self._report(worker, observation, refresh=refresh)
                return {"worker_id": worker_id, "status": "stopped_requested",
                        "observed": observation.observed, "reported": reported}

            self._preflight_spec(spec, backend)
            delivered = self._deliver_next(worker, run, backend, handle)
            observation = self._observe(handle, backend, wait_seconds)
            reported = self._report(worker, observation, refresh=refresh)
            return {"worker_id": worker_id,
                    "status": ("blocked" if delivered and delivered.get("blocked")
                               else "running" if delivered else "idle"),
                    "delivered": delivered,
                    "observed": observation.observed,
                    "observation": observation.as_dict(),
                    "reported": reported}

    def _worker(self, run_id: str, worker_id: str) -> dict[str, Any]:
        return self.api.get_resource(self.actor(run_id, 1), "worker", worker_id)

    def _delivery_gate(self, actor: Actor, worker: Mapping[str, Any],
                       messages: list[Mapping[str, Any]]) -> set[str] | None:
        """The shared decision/candidate/revision delivery rule.

        An open decision blocks every ordinary delivery; a resolved reply and
        an owed revise/expand feedback block them until each is durably
        delivered. An undecided candidate or an acceptance not yet applied
        blocks ordinary continuations too. The Scheduler uses the same rule for
        slot occupancy, so a Worker the Controller cannot feed does not hold a
        capacity slot. The Controller itself only executes the committed facts.
        """
        data = self.api.list_resources(actor, worker["run_id"])
        node = next((item for item in data.get("nodes") or []
                     if item["node_id"] == worker["node_id"]), None)
        progress = self.api.progress_status(actor, worker["run_id"],
                                            worker["node_id"])
        return delivery_gate(worker, messages, node,
                             data.get("candidates") or [],
                             data.get("decision_requests") or [],
                             progress=progress,
                             acceptance=(node or {}).get("acceptance"))

    def _deliver_next(self, worker: Mapping[str, Any], run: Mapping[str, Any],
                      backend: AgentBackend, handle: Any) -> dict[str, Any] | None:
        if run["desired"] != "running":
            # A committed pause/cancel intent stops new dispatch at the
            # controller too; queued messages stay pending for resume.
            return None
        if run["status"] in ("completed", "failed", "cancelled"):
            # A terminal Run accepts no new start or continuation even if its
            # desired intent has not been reset yet: a queued start must stay
            # queued (and is drained by the committed stop intent), never
            # delivered to an execution whose result the Run cannot accept.
            return None
        actor = self.actor(worker["run_id"], worker["epoch"])
        inbox = self.api.list_inbox(actor, worker["worker_id"])
        allowed = self._delivery_gate(actor, worker, inbox["messages"])
        if allowed == set():
            return None
        for message in inbox["messages"]:
            if message["epoch"] != worker["epoch"]:
                continue
            if message["delivery_state"] != "pending":
                continue
            if allowed is not None and message["delivery_key"] not in allowed:
                continue
            try:
                receipt = backend.deliver(handle, message["payload"],
                                          message["delivery_key"])
            except BackendError as error:
                if error.code in ("busy", "unknown_effect", "stopped"):
                    return None
                if error.code == "budget_exhausted":
                    return {"blocked": "budget_exhausted",
                            "message_id": message["message_id"],
                            "budget": error.details.get("budget"),
                            "usage": error.details.get("usage"),
                            "caps": error.details.get("caps")}
                raise
            committed = receipt.as_dict()
            # Two Controllers may observe the same execution: one gets the
            # original receipt, the other the Backend's duplicate receipt.
            # Request identity must follow the exact receipt so both attempts
            # commit (the API keeps one delivered event); a constant
            # delivery:{message_id} id would make the loser conflict on
            # content it can never change.
            self.api.record_message_delivery(
                actor, message["message_id"], committed,
                "delivery:" + digest({"message_id": message["message_id"],
                                      "receipt": committed})[7:39],
            )
            if receipt.duplicate:
                continue
            return {"delivery_key": message["delivery_key"],
                    "message_id": message["message_id"],
                    "receipt": receipt.as_dict()}
        return None

    def _observe(self, handle: Any, backend: AgentBackend,
                 wait_seconds: float) -> Observation:
        deadline = time.monotonic() + max(0.0, wait_seconds)
        observation = backend.observe(handle)
        while observation.observed in ("starting", "active") and time.monotonic() < deadline:
            time.sleep(self.poll_seconds)
            observation = backend.observe(handle)
        return observation

    def _observation_fact(self, worker: Mapping[str, Any],
                          observation: Observation) -> dict[str, Any]:
        fact: dict[str, Any] = {
            "kind": "worker_observed",
            "run_id": worker["run_id"],
            "node_id": worker["node_id"],
            "worker_id": worker["worker_id"],
            "epoch": worker["epoch"],
            "intent_seq": worker["intent_seq"],
            "observed": observation.observed,
            "observation_id": int(observation.observation_id),
            "usage": {name: int(value) for name, value in (
                ("provider_calls", observation.provider_calls),
                ("tool_calls", observation.tool_calls),
            ) if value is not None},
            "activities": [dict(activity) for activity in observation.activities],
            # Backend evidence is copied verbatim so the API's strict boolean
            # check can reject an invalid completeness value instead of a
            # truthy coercion turning "false" into a released safety gate.
            "activities_complete": observation.activities_complete,
        }
        if observation.session_ref:
            fact["session_ref"] = observation.session_ref
        if observation.checkpoint_ref:
            fact["checkpoint_ref"] = observation.checkpoint_ref
        if observation.delivery_key:
            # The completed turn's delivery identity is stable across polling,
            # restarts and unrelated observation version bumps; Scheduler
            # continuation keys use it instead of a poll-generated event id.
            fact["delivery_key"] = observation.delivery_key
        if observation.error:
            fact["error"] = dict(observation.error)
        return fact

    def _report(self, worker: Mapping[str, Any], observation: Observation, *,
                refresh: Any = None) -> dict[str, Any]:
        """Commit an observation; never rebase a stale one onto a new revision.

        A CAS conflict means another Controller committed something newer. The
        only safe recovery is to re-observe the Backend and report that state;
        replaying the old payload would overwrite a newer actual state and
        release capacity while the Worker still runs (R04-OBSERVATION-05).
        """
        fact = self._observation_fact(worker, observation)
        previous = self.api.latest_observation(
            self.actor(worker["run_id"], worker["epoch"]),
            worker["run_id"], worker["worker_id"])
        if previous is not None and previous["payload"] == fact:
            return {"worker_id": worker["worker_id"], "observed": observation.observed,
                    "revision": worker["revision"], "event_id": previous["event_id"],
                    "seq": previous["seq"], "replayed": True}
        request_id = (f"obs:{worker['worker_id']}:{worker['epoch']}:"
                      f"{observation.observation_id}:{digest(fact)[:16]}")
        try:
            return self.api.append_fact(self.actor(worker["run_id"], worker["epoch"]),
                                        fact, request_id, worker["revision"])
        except ControlPlaneError as error:
            if error.code not in ("revision_conflict", "stale_observation") or refresh is None:
                raise
            current = self._worker(worker["run_id"], worker["worker_id"])
            if current["epoch"] != worker["epoch"]:
                raise
            observation = refresh()
            fact = self._observation_fact(current, observation)
            previous = self.api.latest_observation(
                self.actor(current["run_id"], current["epoch"]),
                current["run_id"], current["worker_id"])
            if previous is not None and previous["payload"] == fact:
                return {"worker_id": current["worker_id"],
                        "observed": observation.observed,
                        "revision": current["revision"], "event_id": previous["event_id"],
                        "seq": previous["seq"], "replayed": True}
            request_id = (f"obs:{current['worker_id']}:{current['epoch']}:"
                          f"{observation.observation_id}:{digest(fact)[:16]}")
            return self.api.append_fact(
                self.actor(current["run_id"], current["epoch"]), fact, request_id,
                current["revision"])

    # -- run intent ------------------------------------------------------
    def apply_run_intent(self, run_id: str, *,
                         wait_seconds: float = 5.0) -> dict[str, Any]:
        """Stop only workers this Controller owns and the Scheduler committed to stop."""
        with self._lock:
            reader = self.actor(run_id, 1)
            run = self.api.get_resource(reader, "run", run_id)
            workers = self.api.list_resources(reader, run_id)["workers"]
            summary = {"run_id": run_id, "desired": run["desired"],
                       "controller_id": self.controller_id,
                       "stopped": [], "pending": [], "skipped": []}
            if run["desired"] not in ("paused", "cancelled"):
                return summary
            for worker in workers:
                if not self._owns(worker):
                    summary["skipped"].append({"worker_id": worker["worker_id"],
                                               "controller_id": worker["controller_id"],
                                               "reason": "controller_mismatch"})
                    continue
                if execution_stopped(worker):
                    summary["stopped"].append(worker["worker_id"])
                    continue
                if worker["desired"] != "stopped":
                    summary["pending"].append({"worker_id": worker["worker_id"],
                                               "observed": worker["observed"],
                                               "reason": "intent_not_committed"})
                    continue
                backend = self._backend(worker)
                if backend.capabilities().stop == "none":
                    summary["pending"].append({"worker_id": worker["worker_id"],
                                               "observed": worker["observed"],
                                               "reason": "capability_unsupported"})
                    continue
                handle = backend.start(self._spec(worker, run),
                                       (worker["worker_id"], worker["epoch"]))
                backend.stop(handle, f"stop:{worker['worker_id']}:{worker['epoch']}")
                observation = self._observe(handle, backend, wait_seconds=wait_seconds)
                self._report(worker, observation,
                             refresh=lambda: self._observe(handle, backend, 0.0))
                current = self._worker(run_id, worker["worker_id"])
                if execution_stopped(current):
                    summary["stopped"].append(worker["worker_id"])
                else:
                    summary["pending"].append({"worker_id": worker["worker_id"],
                                               "observed": observation.observed,
                                               "reason": "stop_not_observed"})
            summary["cancelled"] = (run["desired"] == "cancelled"
                                    and not summary["pending"]
                                    and not summary["skipped"])
            return summary
