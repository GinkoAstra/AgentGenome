"""Deterministic Graph /2 execution helpers for the isolated test suite.

The helpers build real Api/Scheduler/Controller objects on a temporary data
directory and a scripted Backend whose Agent callback drives the same
WorkerToolbox protocol a real backend uses. They never import the main
installation and never touch a user data directory.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from node_runtime.control_plane.api import Api
from node_runtime.control_plane.backends.script import ScriptBackend
from node_runtime.control_plane.controller import Controller
from node_runtime.control_plane.resources import Actor
from node_runtime.control_plane.scheduler import Scheduler
from node_runtime.control_plane.tools import WorkerToolbox

TERMINAL = frozenset({"completed", "failed", "cancelled"})


def rid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def publisher(name: str = "teacher") -> Actor:
    return Actor(name, {"publisher"})


def reader(name: str = "reader") -> Actor:
    return Actor(name, {"publisher"})


def operator(name: str = "operator") -> Actor:
    return Actor(name, {"publisher", "scheduler"})


class AgentScriptBackend(ScriptBackend):
    """ScriptBackend that always executes the configured Agent callback."""

    def _execute(self, execution_key, delivery_key):
        state = self._state(execution_key)
        state["deliveries"][delivery_key]["message"].setdefault(
            "script", {"mode": "agent"})
        self._save(execution_key, state)
        super()._execute(execution_key, delivery_key)


class Cluster:
    """One in-process control plane with deterministic scripted Agents."""

    def __init__(self, root: str | Path, agent, *,
                 fault_hook=None, backend_capabilities=None):
        self.root = Path(root)
        self.api = Api(self.root, fault_hook=fault_hook)
        self.backend = AgentScriptBackend(self.root, agent=agent)
        registered = {"script": self.backend.capabilities()}
        if backend_capabilities is not None:
            registered = backend_capabilities
        self.api.backend_capabilities = registered
        self.scheduler = Scheduler(self.api, scheduler_id="scheduler",
                                   controller_id="ctl-1")
        self.controller = Controller(
            self.api, {"script": self.backend}, controller_id="ctl-1",
            data_dir=self.root, poll_seconds=0.0)

    # -- inputs ----------------------------------------------------------
    def artifact(self, content: bytes | str, media_type: str = "text/plain",
                 name: str = "teacher") -> dict:
        if isinstance(content, str):
            content = content.encode("utf-8")
        return self.api.put_artifact(publisher(name), rid("artifact"),
                                     content, media_type)["artifact"]

    def publish(self, document, *, name: str = "teacher",
                request_id: str | None = None) -> dict:
        return self.api.publish_definition(
            publisher(name), document, request_id or rid("publish"))

    def create_run(self, definition_ref: str, inputs: dict, *,
                   name: str = "teacher",
                   request_id: str | None = None) -> dict:
        return self.api.create_run(publisher(name), definition_ref, inputs,
                                   request_id or rid("run"))

    # -- driving ---------------------------------------------------------
    def state(self, run_id: str) -> dict:
        resources = self.api.list_resources(operator(), run_id)
        run = self.api.get_resource(reader(), "run", run_id)
        resources["run"] = run
        return resources

    def drive(self, run_id: str, limit: int = 120) -> dict:
        for _ in range(limit):
            self.scheduler.reconcile(run_id)
            resources = self.state(run_id)
            for worker in resources["workers"]:
                if worker["controller_id"] != "ctl-1":
                    continue
                self.controller.reconcile(run_id, worker["worker_id"])
            run = self.api.get_resource(reader(), "run", run_id)
            if run["status"] in TERMINAL:
                return self.state(run_id)
        raise AssertionError(f"run {run_id} did not reach a terminal state")


# -- Agent callback primitives ------------------------------------------

def is_review_turn(message: dict) -> bool:
    return (message.get("role") == "reviewer"
            or message.get("kind") in ("review_start", "review_request"))


def toolbox(api, spec, message: dict) -> WorkerToolbox:
    """Build the toolbox from the trusted binding plus delivery identity."""
    context = spec.context or {}
    if is_review_turn(message):
        return WorkerToolbox(
            api, run_id=spec.run_id, node_id=spec.node_id,
            worker_id=spec.worker_id, epoch=spec.epoch,
            tool_profile=spec.tool_profile,
            reviewer_ref=(message.get("reviewer_ref")
                          or context.get("reviewer_ref")),
            review_target=(message.get("target_node_id")
                           or context.get("target_node_id")),
            review_candidate=(message.get("candidate_id")
                              or context.get("candidate_id")),
            review_round=(message.get("candidate_round")
                          or context.get("candidate_round")),
            contract_ref=context.get("contract_ref"),
        )
    decision_ids = ([message["decision_id"]]
                    if message.get("decision_id") else None)
    return WorkerToolbox(
        api, run_id=spec.run_id, node_id=spec.node_id,
        worker_id=spec.worker_id, epoch=spec.epoch,
        tool_profile=spec.tool_profile,
        decision_ids=decision_ids,
        contract_ref=context.get("contract_ref"),
    )


def submit_candidate(box: WorkerToolbox, spec, *, round_number: int = 1,
                     extra: dict | None = None) -> dict:
    body = json.dumps({"node_id": spec.node_id,
                       "round": round_number}, sort_keys=True).encode()
    ref = box.publish_artifact(
        {"path": f"{spec.node_id}-r{round_number}.json",
         "media_type": "application/json"},
        call_id=f"publish-{spec.node_id}-{round_number}",
        content=body)["output"]
    arguments = {
        "candidate_id": f"candidate-{spec.node_id}-r{round_number}",
        "round": round_number,
        "output_refs": [ref],
    }
    if extra:
        arguments.update(extra)
    return box.invoke("node.submit_candidate", arguments,
                      call_id=f"candidate-{spec.node_id}-{round_number}")


def _short(tag: str, value: str) -> str:
    import hashlib
    return f"{tag}-{hashlib.sha256(value.encode()).hexdigest()[:32]}"


def submit_review(box: WorkerToolbox, spec, message: dict, *,
                  verdict: str = "accept", progress: str | None = None) -> dict:
    candidate_id = message.get("candidate_id")
    candidate_round = message.get("candidate_round")
    turn = f"{spec.node_id}:{candidate_id}:{verdict}"
    feedback = box.put_feedback(
        f"{verdict} by {spec.worker_id}",
        call_id=_short("feedback", turn))
    arguments = {
        "candidate_id": candidate_id,
        "candidate_round": candidate_round,
        "verdict": verdict,
        "feedback_ref": feedback,
    }
    if progress is not None:
        arguments["progress"] = {"result": progress}
    return box.invoke("node.submit_review", arguments,
                      call_id=_short("review", f"{turn}:{progress}"))


def accepting_agent(api, *, verdict=lambda message: "accept"):
    """Worker/Checker callback that accepts every candidate."""

    def run(spec, message, state):
        box = toolbox(api, spec, message)
        if is_review_turn(message):
            submit_review(box, spec, message, verdict=verdict(message))
            return
        submit_candidate(box, spec,
                         round_number=int(message.get("round") or 1))
    return run
