"""W03b: activity observations, projection guards and lifecycle gates."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from conftest import make_graph
from helpers import (AgentScriptBackend, Cluster, operator, publisher, rid)

from node_runtime.control_plane.api import Api
from node_runtime.control_plane.controller import Controller
from node_runtime.control_plane.execution_facts import (
    execution_quiescent,
    execution_stopped,
)
from node_runtime.control_plane.host import Host
from node_runtime.control_plane.resources import Actor, ControlPlaneError
from node_runtime.control_plane.storage import (
    SCHEMA,
    SCHEMA_VERSION,
    Store,
    _statements,
)


def activity(activity_id="root", observed="active", *, kind="subagent",
             parent=None, usage=None):
    return {"activity_id": activity_id, "parent_activity_id": parent,
            "kind": kind, "observed": observed,
            "usage": dict(usage or {})}


def controller(run_id, epoch):
    return Actor("ctl-1", {"controller"}, run_ids={run_id},
                 controller_id="ctl-1", epoch=epoch)


def start_run(cluster, *, stages=None, budget=None, definition_id="activities"):
    document = make_graph(definition_id=definition_id, stages=stages)
    if budget:
        document["budget_limits"].update(budget)
    published = cluster.publish(document)
    dataset = cluster.artifact(b"dataset", "text/plain")
    run = cluster.create_run(published["definition_ref"], {"dataset": dataset})
    cluster.scheduler.reconcile(run["run_id"])
    return run["run_id"]


def worker_of(cluster, run_id):
    return cluster.state(run_id)["workers"][0]


def observe(cluster, run_id, *, observed, activities=None, complete=None,
            observation_id=1, usage=None, epoch=None):
    worker = cluster.api.get_resource(operator(), "worker",
                                      worker_of(cluster, run_id)["worker_id"])
    fact = {
        "kind": "worker_observed", "run_id": run_id,
        "node_id": worker["node_id"], "worker_id": worker["worker_id"],
        "epoch": worker["epoch"] if epoch is None else epoch,
        "intent_seq": worker["intent_seq"], "observed": observed,
        "observation_id": observation_id,
        "usage": dict(usage or {"provider_calls": 0, "tool_calls": 0}),
    }
    if activities is not None:
        fact["activities"] = activities
        fact["activities_complete"] = complete
    return cluster.api.append_fact(
        controller(run_id, fact["epoch"]), fact, rid("obs"),
        worker["revision"])


def query_worker(cluster, run_id):
    identifier = worker_of(cluster, run_id)["worker_id"]
    return cluster.api.get_resource(operator(), "worker", identifier)


# -- persistence and projection guards ---------------------------------

def test_projection_persists_and_same_observation_cannot_be_rewritten(tmp_path):
    cluster = Cluster(tmp_path / "data", None)
    run_id = start_run(cluster)

    observe(cluster, run_id, observed="idle", observation_id=1,
            activities=[activity(usage={"provider_calls": 2})],
            complete=True, usage={"provider_calls": 10, "tool_calls": 0})
    worker = query_worker(cluster, run_id)
    assert worker["observed"] == "idle"
    assert worker["activities"] == [activity(usage={"provider_calls": 2})]
    assert worker["activities_complete"] is True
    assert worker["usage"] == {"provider_calls": 10, "tool_calls": 0}

    reopened = Api(cluster.root)
    replayed = reopened.get_resource(operator(), "worker", worker["worker_id"])
    assert replayed["activities"] == worker["activities"]
    assert replayed["activities_complete"] is True

    observe(cluster, run_id, observed="idle", observation_id=1,
            activities=[activity(usage={"provider_calls": 2})], complete=True)
    with pytest.raises(ControlPlaneError) as conflict:
        observe(cluster, run_id, observed="idle", observation_id=1,
                activities=[activity(observed="idle",
                                     usage={"provider_calls": 2})],
                complete=True)
    assert conflict.value.code == "idempotency_conflict"

    observe(cluster, run_id, observed="idle", observation_id=2,
            activities=[activity(usage={"provider_calls": 3})], complete=True)
    with pytest.raises(ControlPlaneError) as stale:
        observe(cluster, run_id, observed="idle", observation_id=1,
                activities=[activity(usage={"provider_calls": 3})],
                complete=True)
    assert stale.value.code == "stale_observation"
    with pytest.raises(ControlPlaneError) as epoch:
        observe(cluster, run_id, observed="idle", observation_id=3,
                activities=[activity()], complete=True,
                epoch=worker["epoch"] + 1)
    assert epoch.value.code == "stale_epoch"


def test_incomplete_snapshot_preserves_records_and_failed_merge_rolls_back(
        tmp_path):
    cluster = Cluster(tmp_path / "data", None)
    run_id = start_run(cluster)
    observe(cluster, run_id, observed="idle", observation_id=1,
            activities=[activity(usage={"provider_calls": 2})],
            complete=True)
    observe(cluster, run_id, observed="idle", observation_id=2,
            activities=[activity(usage={"provider_calls": 2}),
                        activity("leaf", observed="starting", parent="root")],
            complete=False)
    worker = query_worker(cluster, run_id)
    assert [item["activity_id"] for item in worker["activities"]] == [
        "root", "leaf"]
    assert worker["activities"][0]["observed"] == "active"
    assert worker["activities_complete"] is False

    before = cluster.api.read_events(operator(), run_id, limit=1000)["events"]
    revision = worker["revision"]
    with pytest.raises(ControlPlaneError) as lost:
        observe(cluster, run_id, observed="idle", observation_id=3,
                activities=[activity("leaf", observed="idle", parent="root")],
                complete=True)
    assert lost.value.code == "invalid_argument"
    after = cluster.api.read_events(operator(), run_id, limit=1000)["events"]
    unchanged = query_worker(cluster, run_id)
    assert len(after) == len(before)
    assert unchanged["revision"] == revision
    assert unchanged["activities"] == worker["activities"]
    assert unchanged["activities_complete"] is False

    # Omitting both fields is an unknown report: records stay, gates close.
    observe(cluster, run_id, observed="idle", observation_id=4)
    omitted = query_worker(cluster, run_id)
    assert omitted["activities"] == worker["activities"]
    assert omitted["activities_complete"] is False


def test_complete_snapshot_cannot_change_identity_or_revive_terminal(tmp_path):
    cluster = Cluster(tmp_path / "data", None)
    run_id = start_run(cluster)
    observe(cluster, run_id, observed="idle", observation_id=1,
            activities=[activity(observed="stopped",
                                 usage={"provider_calls": 2})],
            complete=True)

    for document in (activity(kind="background_tool", observed="stopped"),
                     activity(parent="other", observed="stopped"),
                     activity(observed="active")):
        with pytest.raises(ControlPlaneError) as rejected:
            observe(cluster, run_id, observed="idle", observation_id=2,
                    activities=[document], complete=True)
        assert rejected.value.code == "invalid_argument"

    observe(cluster, run_id, observed="idle", observation_id=3,
            activities=[activity(observed="failed",
                                 usage={"provider_calls": 4})],
            complete=True)
    worker = query_worker(cluster, run_id)
    assert worker["activities"][0]["observed"] == "failed"
    assert worker["usage"]["provider_calls"] == 0

    with pytest.raises(ControlPlaneError):
        observe(cluster, run_id, observed="idle", observation_id=4,
                activities=[activity(observed="failed",
                                     usage={"provider_calls": 3})],
                complete=True)


# -- scheduler lifecycle gates -----------------------------------------

def test_pause_waits_for_complete_internal_quiescence(tmp_path):
    cluster = Cluster(tmp_path / "data", None)
    run_id = start_run(cluster)
    observe(cluster, run_id, observed="idle", observation_id=1,
            activities=[activity()], complete=True)
    run = cluster.api.get_resource(publisher(), "run", run_id)
    cluster.api.set_run_intent(publisher(), run_id, "paused", run["revision"],
                               rid("pause"))

    result = cluster.scheduler.reconcile(run_id)
    assert cluster.api.get_resource(publisher(), "run", run_id)["status"] != "paused"
    assert any(action["action"] == "run_pausing" for action in result["actions"])

    observe(cluster, run_id, observed="idle", observation_id=2,
            activities=[activity(observed="stopped")], complete=True)
    cluster.scheduler.reconcile(run_id)
    assert cluster.api.get_resource(publisher(), "run", run_id)["status"] == "paused"


def test_cancel_waits_for_internal_stop_not_only_the_main_observation(tmp_path):
    cluster = Cluster(tmp_path / "data", None)
    run_id = start_run(cluster)
    observe(cluster, run_id, observed="idle", observation_id=1,
            activities=[activity()], complete=True)
    run = cluster.api.get_resource(publisher(), "run", run_id)
    cluster.api.set_run_intent(publisher(), run_id, "cancelled",
                               run["revision"], rid("cancel"))

    cluster.scheduler.reconcile(run_id)
    assert cluster.api.get_resource(publisher(), "run", run_id)["status"] != "cancelled"
    observe(cluster, run_id, observed="stopped", observation_id=2,
            activities=[activity()], complete=True)
    cluster.scheduler.reconcile(run_id)
    assert cluster.api.get_resource(publisher(), "run", run_id)["status"] != "cancelled"

    observe(cluster, run_id, observed="stopped", observation_id=3,
            activities=[activity(observed="stopped")], complete=True)
    cluster.scheduler.reconcile(run_id)
    assert cluster.api.get_resource(publisher(), "run", run_id)["status"] == "cancelled"


def test_idle_continuation_does_not_race_internal_activity(tmp_path):
    cluster = Cluster(tmp_path / "data", None)
    cluster.backend.agent = lambda spec, message, state: None
    # A turn that emits no candidate leaves the Node running and would
    # normally receive an automatic continuation.
    run_id = start_run(cluster)
    worker = worker_of(cluster, run_id)
    cluster.controller.reconcile(run_id, worker["worker_id"])
    observe(cluster, run_id, observed="idle", observation_id=100,
            activities=[activity()], complete=True)
    result = cluster.scheduler.reconcile(run_id)
    assert not any(action["action"] == "continuation_delivered"
                   for action in result["actions"])
    inbox = cluster.api.list_inbox(operator(), worker_of(cluster, run_id)["worker_id"])
    assert not any("continue:" in message["delivery_key"]
                   for message in inbox["messages"])

    observe(cluster, run_id, observed="idle", observation_id=101,
            activities=[activity(observed="stopped")], complete=True)
    result = cluster.scheduler.reconcile(run_id)
    assert any(action["action"] in ("continuation_delivered",
                                    "worker_waiting")
               for action in result["actions"])


def test_capacity_slot_is_held_until_internal_activities_stop(tmp_path):
    cluster = Cluster(tmp_path / "data", None)
    cluster.backend.agent = lambda spec, message, state: None
    stages = {
        "left": {"task_ref": "work", "reviewer_ref": "check", "needs": [],
                 "inputs": {"dataset": {"run_input": "dataset"}}},
        "right": {"task_ref": "work", "reviewer_ref": "check", "needs": [],
                  "inputs": {"dataset": {"run_input": "dataset"}}},
    }
    run_id = start_run(cluster, stages=stages,
                       budget={"max_inflight_nodes": 1}, definition_id="cap")
    worker = worker_of(cluster, run_id)
    cluster.controller.reconcile(run_id, worker["worker_id"])
    observe(cluster, run_id, observed="idle", observation_id=100,
            activities=[activity()], complete=True)

    cluster.scheduler.reconcile(run_id)
    assert len(cluster.state(run_id)["workers"]) == 1

    observe(cluster, run_id, observed="idle", observation_id=101,
            activities=[activity(observed="stopped")], complete=True)
    cluster.scheduler.reconcile(run_id)
    assert len(cluster.state(run_id)["workers"]) == 2


def test_epoch_replacement_requires_internal_stop_and_clears_projection(tmp_path):
    cluster = Cluster(tmp_path / "data", None)
    cluster.backend.agent = lambda spec, message, state: None
    run_id = start_run(cluster)
    worker = worker_of(cluster, run_id)
    cluster.controller.reconcile(run_id, worker["worker_id"])
    observe(cluster, run_id, observed="idle", observation_id=100,
            activities=[activity()], complete=True)

    current = query_worker(cluster, run_id)
    with pytest.raises(ControlPlaneError) as blocked:
        cluster.scheduler._plan(
            run_id, [("worker", current["worker_id"], current["revision"])],
            [{"kind": "worker", "op": "update", "id": current["worker_id"],
              "fields": {"epoch": current["epoch"] + 1}}], "test-epoch")
    assert blocked.value.code == "epoch_in_flight"

    observe(cluster, run_id, observed="idle", observation_id=101,
            activities=[activity(observed="stopped")], complete=True)
    current = query_worker(cluster, run_id)
    cluster.scheduler._plan(
        run_id, [("worker", current["worker_id"], current["revision"])],
        [{"kind": "worker", "op": "update", "id": current["worker_id"],
          "fields": {"epoch": current["epoch"] + 1}}], "test-epoch")
    replaced = query_worker(cluster, run_id)
    assert replaced["epoch"] == current["epoch"] + 1
    assert replaced["activities"] == []
    assert replaced["activities_complete"] is False


def test_terminal_run_discovery_keeps_unsettled_internal_activity(tmp_path):
    cluster = Cluster(tmp_path / "data", None)
    run_id = start_run(cluster)
    observe(cluster, run_id, observed="stopped", observation_id=1,
            activities=[activity()], complete=True)
    worker = query_worker(cluster, run_id)
    cluster.scheduler._plan(
        run_id, [("worker", worker["worker_id"], worker["revision"])],
        [{"kind": "worker", "op": "update", "id": worker["worker_id"],
          "fields": {"intent_seq": worker["intent_seq"] + 1,
                     "desired": "stopped"}}], "test-stop")
    run = cluster.api.get_resource(publisher(), "run", run_id)
    cluster.scheduler._plan(
        run_id, [("run", run_id, run["revision"])],
        [{"kind": "run", "op": "update", "id": run_id,
          "fields": {"status": "failed"}}], "test-failed")

    discovered = cluster.api.list_reconciliation_runs(operator(), limit=10)
    assert run_id in {item["run_id"] for item in discovered["runs"]}
    assert Host._needs_drain(query_worker(cluster, run_id))

    observe(cluster, run_id, observed="stopped", observation_id=2,
            activities=[activity(observed="stopped")], complete=True)
    discovered = cluster.api.list_reconciliation_runs(operator(), limit=10)
    assert run_id not in {item["run_id"] for item in discovered["runs"]}
    assert not Host._needs_drain(query_worker(cluster, run_id))


def test_terminal_filter_runs_before_limit(tmp_path):
    cluster = Cluster(tmp_path / "data", None)
    clean_run = start_run(cluster)
    observe(cluster, clean_run, observed="stopped", observation_id=1,
            activities=[], complete=True)
    dirty_run = start_run(cluster, definition_id="activities-dirty")
    observe(cluster, dirty_run, observed="stopped", observation_id=1,
            activities=[activity()], complete=True)
    with cluster.api.store.write() as tx:
        tx.execute("UPDATE runs SET created_at = ? WHERE run_id = ?",
                   ("2026-01-01T00:00:00.000Z", clean_run))
        tx.execute("UPDATE runs SET created_at = ? WHERE run_id = ?",
                   ("2026-01-01T00:00:01.000Z", dirty_run))

    for run_id in (clean_run, dirty_run):
        worker = query_worker(cluster, run_id)
        cluster.scheduler._plan(
            run_id, [("worker", worker["worker_id"], worker["revision"])],
            [{"kind": "worker", "op": "update", "id": worker["worker_id"],
              "fields": {"intent_seq": worker["intent_seq"] + 1,
                         "desired": "stopped"}}], "test-stop")
        run = cluster.api.get_resource(publisher(), "run", run_id)
        cluster.scheduler._plan(
            run_id, [("run", run_id, run["revision"])],
            [{"kind": "run", "op": "update", "id": run_id,
              "fields": {"status": "failed"}}], "test-failed")

    # The clean run sorts first by created_at; a post-LIMIT filter would
    # return only that run and hide the dirty one from restart discovery.
    only = cluster.api.list_reconciliation_runs(operator(), limit=1)
    assert [item["run_id"] for item in only["runs"]] == [dirty_run]
    all_runs = cluster.api.list_reconciliation_runs(operator(), limit=10)
    assert dirty_run in {item["run_id"] for item in all_runs["runs"]}
    assert clean_run not in {item["run_id"] for item in all_runs["runs"]}


# -- controlled backend integration ------------------------------------

class ActivityScriptBackend(AgentScriptBackend):
    """Persisted test adapter that owns internal activities."""

    def capabilities(self):
        return replace(super().capabilities(), internal_activities="managed")

    def set_activities(self, execution_key, documents, *, complete):
        with self._guard(execution_key):
            state = self._state(execution_key)
            state["test_activities"] = {
                "activities": [dict(document) for document in documents],
                "activities_complete": bool(complete),
            }
            self._save(execution_key, state)

    def observe(self, handle, after=None):
        base = super().observe(handle, after)
        state = self._state(handle.execution_key)
        entry = state.get("test_activities") or {
            "activities": [], "activities_complete": True}
        return replace(base, activities=tuple(entry["activities"]),
                       activities_complete=entry["activities_complete"])


def _activity_cluster(root):
    cluster = Cluster(root, None)
    backend = ActivityScriptBackend(root, agent=lambda spec, message, state: None)
    cluster.backend = backend
    cluster.api.backend_capabilities = {"script": backend.capabilities()}
    cluster.controller = Controller(cluster.api, {"script": backend},
                                    controller_id="ctl-1", data_dir=root,
                                    poll_seconds=0.0)
    return cluster, backend


def test_backend_rebuild_reports_persisted_activity_and_stop_timeline(tmp_path):
    cluster, backend = _activity_cluster(tmp_path / "data")
    run_id = start_run(cluster)
    worker = worker_of(cluster, run_id)
    key = (worker["worker_id"], worker["epoch"])
    cluster.controller.reconcile(run_id, worker["worker_id"])
    backend.set_activities(key, [activity()], complete=True)

    cluster.controller.reconcile(run_id, worker["worker_id"])
    reported = query_worker(cluster, run_id)
    assert reported["observed"] == "idle"
    assert reported["activities"] == [activity()]
    assert reported["activities_complete"] is True
    assert execution_quiescent(reported) is False

    rebuilt = ActivityScriptBackend(cluster.root,
                                    agent=lambda spec, message, state: None)
    controller = Controller(cluster.api, {"script": rebuilt},
                            controller_id="ctl-1", data_dir=cluster.root,
                            poll_seconds=0.0)
    controller.reconcile(run_id, worker["worker_id"])
    assert query_worker(cluster, run_id)["activities"] == [activity()]

    current = query_worker(cluster, run_id)
    cluster.scheduler._plan(
        run_id, [("worker", current["worker_id"], current["revision"])],
        [{"kind": "worker", "op": "update", "id": current["worker_id"],
          "fields": {"intent_seq": current["intent_seq"] + 1,
                     "desired": "stopped"}}], "test-stop")
    controller.reconcile(run_id, worker["worker_id"])
    stopped = query_worker(cluster, run_id)
    assert stopped["observed"] == "stopped"
    assert stopped["activities"][0]["observed"] == "active"
    assert execution_stopped(stopped) is False
    assert Host._needs_drain(stopped)

    rebuilt.set_activities(key, [activity(observed="stopped")], complete=True)
    controller.reconcile(run_id, worker["worker_id"])
    settled = query_worker(cluster, run_id)
    assert settled["activities"][0]["observed"] == "stopped"
    assert execution_stopped(settled) is True
    assert not Host._needs_drain(settled)


# -- schema /2 additive upgrade ----------------------------------------

def _legacy_schema_2() -> str:
    schema = SCHEMA
    for column in ("    activities_json TEXT NOT NULL DEFAULT '[]',\n",
                   "    activities_complete INTEGER NOT NULL DEFAULT 0,\n"):
        assert column in schema
        schema = schema.replace(column, "")
    return schema


def _create_schema_2(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    for statement in _statements(_legacy_schema_2()):
        connection.execute(statement)
    connection.execute(
        "INSERT INTO meta (key, value) VALUES ('schema_version', '2')")
    connection.commit()
    return connection


def test_schema_2_upgrades_additively_and_keeps_rows(tmp_path):
    root = tmp_path / "legacy"
    root.mkdir()
    database = root / "state.sqlite3"
    connection = _create_schema_2(database)
    try:
        connection.execute(
            "INSERT INTO runs (run_id, definition_ref, definition_digest,"
            " input_refs_json, policy_ref, budget_json, created_by, desired,"
            " status, summary_json, revision, event_seq, created_at, updated_at)"
            " VALUES ('run-1', 'def@1', 'sha256:" + "0" * 64 + "', '[]', 'p',"
            " '{}', 'teacher', 'running', 'running', '{}', 1, 1,"
            " '2026-01-01', '2026-01-01')")
        connection.execute(
            "INSERT INTO nodes (node_id, run_id, template_ref, contract_ref,"
            " input_bindings_json, dependencies_json, reviewer_ref, state,"
            " round, revision, created_at, updated_at)"
            " VALUES ('node-1', 'run-1', 'work', 'contract', '{}', '[]',"
            " 'check', 'running', 1, 1, '2026-01-01', '2026-01-01')")
        connection.execute(
            "INSERT INTO workers (worker_id, run_id, node_id, backend_ref,"
            " agent_ref, controller_id, epoch, intent_seq, desired, observed,"
            " revision, created_at, updated_at)"
            " VALUES ('worker-1', 'run-1', 'node-1', 'script', 'doer', 'ctl',"
            " 1, 1, 'active', 'idle', 3, '2026-01-01', '2026-01-01')")
        connection.execute(
            "INSERT INTO events (event_id, run_id, seq, kind, payload_json,"
            " actor_id, request_id, created_at)"
            " VALUES ('event-1', 'run-1', 1, 'run_created', '{}', 'teacher',"
            " 'req', '2026-01-01')")
        connection.commit()
    finally:
        connection.close()

    store = Store(database)
    with store.read() as tx:
        assert tx.one("SELECT value FROM meta WHERE key = 'schema_version'")[0] == SCHEMA_VERSION
        assert tx.one("SELECT run_id FROM runs")["run_id"] == "run-1"
        worker = tx.one("SELECT * FROM workers WHERE worker_id = 'worker-1'")
        assert worker["activities_json"] == "[]"
        assert worker["activities_complete"] == 0
        assert worker["revision"] == 3
        assert tx.one("SELECT event_id FROM events")["event_id"] == "event-1"
        columns = {row["name"] for row in tx.all("PRAGMA table_info(workers)")}
        assert {"activities_json", "activities_complete"} <= columns

    backups = list(root.glob("state.sqlite3.schema-2-backup-*"))
    assert len(backups) == 1
    snapshot = sqlite3.connect(f"file:{backups[0].as_posix()}?mode=ro", uri=True)
    try:
        assert snapshot.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0] == "2"
        assert snapshot.execute("SELECT run_id FROM runs").fetchone()[0] == "run-1"
        assert snapshot.execute(
            "SELECT revision FROM workers WHERE worker_id = 'worker-1'").fetchone()[0] == 3
        assert snapshot.execute("SELECT event_id FROM events").fetchone()[0] == "event-1"
        backup_columns = {row[1] for row in snapshot.execute("PRAGMA table_info(workers)")}
        assert not {"activities_json", "activities_complete"} & backup_columns
    finally:
        snapshot.close()


def test_incomplete_schema_2_is_refused_without_changes(tmp_path):
    root = tmp_path / "incomplete"
    root.mkdir()
    database = root / "state.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        connection.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', '2')")
        connection.commit()
    finally:
        connection.close()
    before = database.read_bytes()
    with pytest.raises(ControlPlaneError) as refused:
        Store(database)
    assert refused.value.code == "capability_unsupported"
    assert database.read_bytes() == before
    assert not (root / "state.sqlite3-wal").exists()


# -- review regressions: strict evidence, artifact scope, ordering -----

def test_controller_rejects_non_boolean_completeness_without_coercion(tmp_path):
    from node_runtime.control_plane.backends.base import Observation

    cluster = Cluster(tmp_path / "data", None)
    run_id = start_run(cluster)
    worker = query_worker(cluster, run_id)
    invalid = Observation(observation_id=100, observed="idle", activities=(),
                          activities_complete="false")
    with pytest.raises(ControlPlaneError) as rejected:
        cluster.controller._report(worker, invalid)
    assert rejected.value.code == "invalid_argument"
    assert query_worker(cluster, run_id) == worker


@pytest.mark.parametrize("reference_kind", ["unregistered", "foreign"])
def test_activity_evidence_requires_a_visible_registered_artifact(
        tmp_path, reference_kind):
    cluster = Cluster(tmp_path / "data", None)
    run_id = start_run(cluster)
    if reference_kind == "unregistered":
        sha = "f" * 64
        reference = {"id": "sha256:" + sha, "sha256": sha, "size": 9,
                     "media_type": "text/plain"}
    else:
        reference = cluster.artifact(b"private-evidence", name="other-owner")
    document = activity()
    document["evidence_ref"] = reference
    before = query_worker(cluster, run_id)
    with pytest.raises(ControlPlaneError):
        observe(cluster, run_id, observed="idle", observation_id=1,
                activities=[document], complete=True)
    assert query_worker(cluster, run_id) == before


def test_activity_evidence_accepts_an_artifact_inside_the_run_scope(tmp_path):
    cluster = Cluster(tmp_path / "data", None)
    run_id = start_run(cluster)
    reference = cluster.api.get_resource(
        publisher(), "run", run_id)["input_refs"]["dataset"]
    document = activity()
    document["evidence_ref"] = reference
    observe(cluster, run_id, observed="idle", observation_id=1,
            activities=[document], complete=True)
    worker = query_worker(cluster, run_id)
    assert worker["activities"][0]["evidence_ref"] == reference


@pytest.mark.parametrize("stale_sequence", [99, 100])
def test_unnumbered_report_cannot_reset_numbered_high_watermark(
        tmp_path, stale_sequence):
    cluster = Cluster(tmp_path / "data", None)
    run_id = start_run(cluster)
    observe(cluster, run_id, observed="idle", observation_id=100,
            activities=[activity()], complete=True)
    worker = query_worker(cluster, run_id)
    assert worker["activities"] == [activity()]

    unknown = {"kind": "worker_observed", "run_id": run_id,
               "node_id": worker["node_id"], "worker_id": worker["worker_id"],
               "epoch": worker["epoch"], "intent_seq": worker["intent_seq"],
               "observed": "unknown"}
    cluster.api.append_fact(controller(run_id, worker["epoch"]), unknown,
                            rid("unknown"), worker["revision"])
    projected = query_worker(cluster, run_id)
    assert projected["activities"] == [activity()]
    assert projected["activities_complete"] is False

    with pytest.raises(ControlPlaneError):
        observe(cluster, run_id, observed="idle",
                observation_id=stale_sequence,
                activities=[activity(observed="stopped")], complete=True)
    assert query_worker(cluster, run_id) == projected
