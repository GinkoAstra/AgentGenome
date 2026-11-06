"""P3a: trace projection, scoped cursors and Backend execution observation.

All fixtures are isolated temporary directories. The Pi case is a controlled
on-disk Pi event fixture: it exercises the real Backend adapter without any
model process.
"""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import make_graph
from helpers import Cluster, accepting_agent, operator
from test_backends import spec

from node_runtime.control_plane.backends.pi import PiBackend
from node_runtime.control_plane.backends.script import ScriptBackend
from node_runtime.control_plane.resources import ControlPlaneError
from node_runtime.control_plane.trace import (ControlCursor, TraceService)


def _chain_stages():
    return {
        "extract": {"task_ref": "work", "reviewer_ref": "check", "needs": [],
                    "inputs": {"dataset": {"run_input": "dataset"}}},
        "transform": {"task_ref": "work", "reviewer_ref": "check",
                      "needs": ["extract"],
                      "inputs": {"upstream": {"from": "extract"}}},
    }


def _service(api, backend, *, service_id: str = "svc-p3a") -> TraceService:
    return TraceService(api, service_id=service_id,
                        backends={"script": backend})


def _run_chain(tmp_path: Path) -> tuple[Cluster, TraceService, str]:
    cluster = Cluster(tmp_path / "data", None)
    cluster.backend.agent = accepting_agent(cluster.api)
    document = make_graph(definition_id="p3a-chain", stages=_chain_stages())
    published = cluster.publish(document)
    dataset = cluster.artifact(b"dataset", "text/plain")
    run = cluster.create_run(published["definition_ref"], {"dataset": dataset})
    state = cluster.drive(run["run_id"])
    assert state["run"]["status"] == "completed"
    return cluster, _service(cluster.api, cluster.backend), run["run_id"]


# -- snapshot projection ------------------------------------------------

def test_snapshot_shares_one_transaction_cursor_and_separates_graph(tmp_path):
    cluster, trace, run_id = _run_chain(tmp_path)
    actor = operator()

    resources = cluster.api.list_resources(actor, run_id)
    snapshot = trace.snapshot(actor, run_id)
    cursor = snapshot["cursor"]
    assert cursor["seq"] == resources["run"]["event_seq"]
    assert cursor["service_id"] == "svc-p3a"
    assert cursor["run_id"] == run_id

    stages = {node["label"]: node for node in snapshot["tree"]
              if node["local_child_id"] is not None}
    assert set(stages) == {"extract", "transform"}
    review_nodes = [node for node in snapshot["tree"]
                    if node["local_child_id"] is None]
    assert {node["parent_id"] for node in review_nodes} == {
        stage["node_id"] for stage in stages.values()}
    assert snapshot["decomposition"] == [{
        "parent_id": node["parent_id"],
        "child_id": node["node_id"],
        "expansion_id": None,
        "local_child_id": None,
    } for node in review_nodes]
    assert snapshot["dependencies"] == [{
        "node_id": stages["transform"]["node_id"],
        "depends_on": stages["extract"]["node_id"],
    }]

    # The timeline ends exactly at the snapshot cursor and never runs ahead.
    timeline = snapshot["timeline"]["events"]
    assert timeline
    assert timeline[-1]["seq"] == cursor["seq"]
    assert all(event["seq"] <= cursor["seq"] for event in timeline)
    assert [event["seq"] for event in timeline] == list(
        range(timeline[0]["seq"], timeline[-1]["seq"] + 1))

    # A snapshot cursor is already caught up: subscribing loses nothing.
    page = trace.events(actor, run_id, after=cursor["token"])
    assert page["events"] == []
    assert page["latest_seq"] == cursor["seq"]

    assert snapshot["executions"]
    assert all(execution["delivery_keys"]
               for execution in snapshot["executions"])
    assert snapshot["availability"]["control_events"] == "committed"
    assert snapshot["availability"]["request_diagnostics"] == "unconfigured"


def test_snapshot_filters_do_not_change_cursor_meaning(tmp_path):
    cluster, trace, run_id = _run_chain(tmp_path)
    actor = operator()
    full = trace.snapshot(actor, run_id)
    transform = next(node for node in full["tree"]
                     if node["label"] == "transform")

    filtered = trace.snapshot(actor, run_id, node_id=transform["node_id"])
    assert filtered["cursor"]["seq"] == full["cursor"]["seq"]
    kept = {transform["node_id"]} | {
        node["node_id"] for node in full["tree"]
        if node["parent_id"] == transform["node_id"]}
    assert {node["node_id"] for node in filtered["tree"]} == kept
    assert all(item["child_id"] in kept for item in filtered["decomposition"])
    assert all(item["node_id"] in kept for item in filtered["dependencies"])

    candidate_id = full["candidates"][0]["candidate_id"]
    selected = trace.snapshot(actor, run_id, candidate_id=candidate_id)
    assert selected["cursor"]["seq"] == full["cursor"]["seq"]
    assert [item["candidate_id"] for item in selected["candidates"]] == [
        candidate_id]
    assert all(item["candidate_id"] == candidate_id
               for item in selected["reviews"])
    assert all(item["node_id"] == selected["candidates"][0]["node_id"]
               for item in selected["tree"])

    with pytest.raises(ControlPlaneError) as missing:
        trace.snapshot(actor, run_id, node_id="no-such-node")
    assert missing.value.code == "not_found"


def test_filtered_waiting_evidence_uses_full_snapshot(tmp_path, monkeypatch):
    cluster, trace, run_id = _run_chain(tmp_path)
    actor = operator()
    resources = cluster.api.list_resources(actor, run_id)
    transform = next(node for node in resources["nodes"]
                     if node["local_child_id"] == "transform")
    transform["state"] = "pending"
    resources["run"]["status"] = "running"
    monkeypatch.setattr(cluster.api, "list_resources",
                        lambda actor, identifier: deepcopy(resources))

    full = trace.snapshot(actor, run_id)
    focused = trace.snapshot(actor, run_id, node_id=transform["node_id"])
    expected = next(item for item in full["waiting"]
                    if item["node_id"] == transform["node_id"])
    got = next(item for item in focused["waiting"]
               if item["node_id"] == transform["node_id"])
    assert expected["reason"] == "awaiting_schedule"
    assert got == expected


# -- scoped control cursors --------------------------------------------

def test_events_pages_resume_without_gaps_or_duplicates(tmp_path):
    cluster, trace, run_id = _run_chain(tmp_path)
    actor = operator()
    latest = cluster.api.list_resources(actor, run_id)["run"]["event_seq"]

    cursor = None
    seen = []
    while True:
        page = trace.events(actor, run_id, after=cursor, limit=3)
        assert page["format"] == "agentgenome-trace-events/1"
        assert page["service_id"] == "svc-p3a"
        for event in page["events"]:
            seen.append(event["seq"])
        if not page["has_more"]:
            assert page["next_cursor"]["seq"] == latest
            break
        cursor = page["next_cursor"]["token"]
    assert seen == list(range(1, latest + 1))

    resumed = trace.events(actor, run_id,
                           after=ControlCursor("svc-p3a", run_id,
                                               latest - 1).encode())
    assert [event["seq"] for event in resumed["events"]] == [latest]

    for foreign in (ControlCursor("other-service", run_id, 0).encode(),
                    ControlCursor("svc-p3a", "other-run", 0).encode()):
        with pytest.raises(ControlPlaneError) as expired:
            trace.events(actor, run_id, after=foreign)
        assert expired.value.code == "cursor_expired"
        assert expired.value.details.get("instruction") == "resnapshot"

    with pytest.raises(ControlPlaneError) as ahead:
        trace.events(actor, run_id, after=latest + 1)
    assert ahead.value.code == "cursor_expired"

    with pytest.raises(ControlPlaneError) as malformed:
        trace.events(actor, run_id, after="{not-json")
    assert malformed.value.code == "invalid_argument"


# -- execution detail ---------------------------------------------------

def _execution_identity(cluster: Cluster, run_id: str):
    resources = cluster.api.list_resources(operator(), run_id)
    worker = resources["workers"][0]
    inbox = cluster.api.list_inbox(operator(), worker["worker_id"])
    delivery = inbox["messages"][0]
    return worker, delivery


def test_execution_detail_preserves_identity_and_pages(tmp_path):
    cluster, trace, run_id = _run_chain(tmp_path)
    worker, delivery = _execution_identity(cluster, run_id)
    actor = operator()
    epoch = worker["epoch"]
    key = delivery["delivery_key"]

    first = trace.execution(actor, run_id, worker["worker_id"], epoch, key,
                            limit=1)
    assert first["availability"] == "available"
    assert first["source"] == "script.delivery.state"
    assert first["service_id"] == "svc-p3a"
    assert first["run_id"] == run_id
    assert first["worker_id"] == worker["worker_id"]
    assert first["epoch"] == epoch
    assert first["delivery_key"] == key
    assert first["delivery"]["message_id"] == delivery["message_id"]
    assert first["entries"] and len(first["entries"]) == 1
    assert first["has_more"] is True
    assert first["partial"] is False
    for entry in first["entries"]:
        assert entry["worker_id"] == worker["worker_id"]
        assert entry["epoch"] == epoch
        assert entry["delivery_key"] == key
        assert entry["source"] == "script.delivery.state"

    rest = []
    cursor = first["next_cursor"]["token"]
    offset = first["next_cursor"]["offset"]
    while True:
        page = trace.execution(actor, run_id, worker["worker_id"], epoch, key,
                               after=cursor, limit=2)
        assert page["cursor"]["offset"] == offset
        rest.extend(page["entries"])
        offset = page["next_cursor"]["offset"]
        if not page["has_more"]:
            break
        cursor = page["next_cursor"]["token"]
    assert [entry["index"] for entry in first["entries"] + rest] == list(
        range(len(first["entries"]) + len(rest)))
    assert {entry["kind"] for entry in first["entries"] + rest} >= {
        "delivery", "result"}

    # A cursor belongs to exactly one execution; another worker cannot reuse it.
    foreign = {"service_id": "svc-p3a", "run_id": run_id,
               "worker_id": "other-worker", "epoch": epoch,
               "delivery_key": key, "source": "script.delivery.state",
               "offset": 0}
    with pytest.raises(ControlPlaneError) as expired:
        trace.execution(actor, run_id, worker["worker_id"], epoch, key,
                        after=json.dumps(foreign))
    assert expired.value.code == "cursor_expired"

    with pytest.raises(ControlPlaneError) as bad_key:
        trace.execution(actor, run_id, worker["worker_id"], epoch, "../escape")
    assert bad_key.value.code == "invalid_argument"


def test_execution_rejects_another_epoch_or_run(tmp_path):
    cluster, trace, run_id = _run_chain(tmp_path)
    worker, delivery = _execution_identity(cluster, run_id)
    actor = operator()

    with pytest.raises(ControlPlaneError) as epoch_mismatch:
        trace.execution(actor, run_id, worker["worker_id"],
                        worker["epoch"] + 1, delivery["delivery_key"])
    assert epoch_mismatch.value.code == "not_found"
    assert epoch_mismatch.value.details.get("epoch") == worker["epoch"] + 1

    with pytest.raises(ControlPlaneError) as run_mismatch:
        trace.execution(actor, "run-does-not-match", worker["worker_id"],
                        worker["epoch"], delivery["delivery_key"])
    assert run_mismatch.value.code == "not_found"

    other = TraceService(cluster.api, service_id="svc-p3a", backends={})
    unsupported = other.execution(actor, run_id, worker["worker_id"],
                                  worker["epoch"], delivery["delivery_key"])
    assert unsupported["availability"] == "unsupported"
    assert unsupported["entries"] == []
    assert unsupported["next_cursor"]["offset"] == 0


def test_execution_missing_state_stays_explicit(tmp_path):
    cluster, trace, run_id = _run_chain(tmp_path)
    worker, delivery = _execution_identity(cluster, run_id)
    state = (Path(cluster.root) / "backends" / "script"
             / worker["worker_id"] / str(worker["epoch"]) / "state.json")
    assert state.is_file()
    state.unlink()

    page = trace.execution(operator(), run_id, worker["worker_id"],
                           worker["epoch"], delivery["delivery_key"])
    assert page["availability"] == "missing"
    assert page["entries"] == []
    assert page["detail"]["reason"] == "execution state is missing"
    assert page["next_cursor"]["offset"] == 0


def test_script_snapshot_change_expires_execution_cursor(tmp_path):
    backend = ScriptBackend(tmp_path / "script-data")
    handle = backend.start(spec(), ("w1", 1))
    backend.deliver(handle, {
        "brief": "controlled crash",
        "script": {
            "tool_calls": [{
                "name": "write_file", "effect": "idempotent",
                "target": "out.txt",
                "params": {"path": "out.txt", "text": "abc"},
                "content": "abc"}],
            "crash_after_tool_start": True}}, "d1")
    api = SimpleNamespace(
        get_resource=lambda actor, kind, key: {
            "run_id": "r1", "node_id": "n1", "backend_ref": "script"},
        list_inbox=lambda actor, worker: {"messages": [{
            "message_id": "m1", "delivery_key": "d1", "epoch": 1,
            "delivery_state": "delivered", "created_at": None,
            "delivered_at": None, "receipt": {}}]})
    trace = TraceService(api, service_id="svc-p3a",
                         backends={"script": backend})
    actor = operator()

    before = trace.execution(actor, "r1", "w1", 1, "d1")
    tool = next(entry for entry in before["entries"]
                if entry["kind"] == "tool")
    assert tool["state"] == "start"
    assert isinstance(before["snapshot"], int)
    assert json.loads(before["next_cursor"]["token"])["snapshot"] == \
        before["snapshot"]

    backend.reconcile_tools(handle)
    fresh = trace.execution(actor, "r1", "w1", 1, "d1")
    changed = next(entry for entry in fresh["entries"]
                   if entry["kind"] == "tool")
    assert changed["state"] != "start"
    assert fresh["snapshot"] > before["snapshot"]

    with pytest.raises(ControlPlaneError) as expired:
        trace.execution(actor, "r1", "w1", 1, "d1",
                        after=before["next_cursor"]["token"])
    assert expired.value.code == "cursor_expired"
    assert expired.value.details.get("instruction") == "resnapshot"

    # A cursor from the current snapshot still resumes normally.
    resumed = trace.execution(actor, "r1", "w1", 1, "d1",
                              after=fresh["next_cursor"]["token"])
    assert resumed["snapshot"] == fresh["snapshot"]


# -- Pi Backend execution log ------------------------------------------

def _pi_fixture(tmp_path: Path) -> tuple[PiBackend, str, str, str, int]:
    backend = PiBackend(tmp_path / "pi-data")
    run_id, worker_id, key, epoch = "run-pi", "worker-pi", "delivery-1", 1
    directory = backend.root / worker_id / str(epoch)
    (directory / "events").mkdir(parents=True)
    (directory / "state.json").write_text(json.dumps({
        "run_id": run_id, "worker_id": worker_id, "epoch": epoch,
        "deliveries": {key: {"state": "running",
                             "started_at": "2026-11-04T00:00:00.000000Z",
                             "finished_at": None}},
    }), encoding="utf-8")
    digest = "sha256:" + "a" * 64
    events = [
        {"type": "message_end", "timestamp": 1700000000, "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "PROMPT-SECRET"},
                {"type": "thinking", "thinking": "HIDDEN-REASONING"},
                {"type": "toolCall", "id": "call-1", "name": "write_file"},
            ],
            "usage": {"input": 3, "output": 4, "totalTokens": 7},
            "stopReason": "toolUse"}},
        {"type": "tool_execution_start", "toolCallId": "call-1",
         "toolName": "write_file",
         "args": {"path": "result.json", "body": "TOOL-SECRET"}},
        {"type": "tool_execution_end", "toolCallId": "call-1",
         "toolName": "write_file", "isError": False, "durationMs": 9,
         "result": {"content": "RAW-TOOL-OUTPUT",
                    "artifact": {"id": digest, "sha256": "a" * 64,
                                 "size": 3, "media_type": "text/plain"}}},
    ]
    path = directory / "events" / f"{key}.jsonl"
    complete = "".join(json.dumps(event) + "\n" for event in events)
    partial = '{"type":"message_end","message":{"role":"assistant"'
    path.write_text(complete + partial, encoding="utf-8")
    return backend, run_id, worker_id, key, epoch


def test_pi_execution_log_is_bounded_partial_and_redacted(tmp_path):
    backend, run_id, worker_id, key, epoch = _pi_fixture(tmp_path)

    page = backend.execution_log(run_id=run_id, worker_id=worker_id,
                                 epoch=epoch, delivery_key=key, limit=10)
    assert page.availability == "available"
    assert page.source == "pi.delivery.events.jsonl"
    assert page.partial is True
    assert page.has_more is False
    assert len(page.entries) == 3
    assert [entry["kind"] for entry in page.entries] == [
        "message_end", "tool_execution_start", "tool_execution_end"]

    rendered = json.dumps(page.as_dict())
    for secret in ("PROMPT-SECRET", "HIDDEN-REASONING", "TOOL-SECRET",
                   "RAW-TOOL-OUTPUT"):
        assert secret not in rendered

    message, started, ended = page.entries
    assert message["usage"] == {"input": 3, "output": 4, "totalTokens": 7}
    assert message["usage_available"] is True
    assert message["content"]["tool_calls"] == [
        {"call_id": "call-1", "tool": "write_file"}]
    assert "observed_at" not in started
    assert started["call_id"] == "call-1"
    assert started["params_keys"] == ["body", "path"]
    assert started["target_path"] == "result.json"
    assert ended["call_id"] == "call-1"
    assert ended["artifact_refs"][0]["id"].startswith("sha256:")

    # The incomplete line is preserved until its newline arrives.
    directory = backend.root / worker_id / str(epoch)
    path = directory / "events" / f"{key}.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(',"usage":5,"stopReason":"end"}}\n')
    completed = backend.execution_log(run_id=run_id, worker_id=worker_id,
                                      epoch=epoch, delivery_key=key,
                                      after=page.next_offset, limit=10)
    assert completed.partial is False
    assert [entry["kind"] for entry in completed.entries] == ["message_end"]
    assert completed.entries[0]["usage"] is None
    assert completed.entries[0]["usage_available"] is False
    assert completed.entries[0]["stop_reason"] == "end"

    # limit is a hard page bound; the next offset continues exactly.
    first = backend.execution_log(run_id=run_id, worker_id=worker_id,
                                  epoch=epoch, delivery_key=key, limit=1)
    assert [entry["kind"] for entry in first.entries] == ["message_end"]
    assert first.has_more is True
    second = backend.execution_log(run_id=run_id, worker_id=worker_id,
                                   epoch=epoch, delivery_key=key,
                                   after=first.next_offset, limit=1)
    assert [entry["kind"] for entry in second.entries] == [
        "tool_execution_start"]

    with pytest.raises(ControlPlaneError) as ahead:
        backend.execution_log(run_id=run_id, worker_id=worker_id, epoch=epoch,
                              delivery_key=key, after=10_000)
    assert ahead.value.code == "cursor_expired"


def test_pi_execution_log_identity_and_missing_are_explicit(tmp_path):
    backend, run_id, worker_id, key, epoch = _pi_fixture(tmp_path)

    wrong = backend.execution_log(run_id="another-run", worker_id=worker_id,
                                  epoch=epoch, delivery_key=key)
    assert wrong.availability == "missing"
    assert "identity" in wrong.detail["reason"]

    wrong_epoch = backend.execution_log(run_id=run_id, worker_id=worker_id,
                                        epoch=epoch + 1, delivery_key=key)
    assert wrong_epoch.availability == "missing"

    missing = backend.execution_log(run_id=run_id, worker_id=worker_id,
                                    epoch=epoch, delivery_key="no-delivery")
    assert missing.availability == "missing"
    assert "delivery" in missing.detail["reason"]

    state = backend.root / worker_id / str(epoch) / "state.json"
    state.unlink()
    absent = backend.execution_log(run_id=run_id, worker_id=worker_id,
                                   epoch=epoch, delivery_key=key)
    assert absent.availability == "missing"
    assert "state" in absent.detail["reason"]


# -- retained delivery epochs -------------------------------------------

def test_snapshot_keeps_retained_delivery_under_its_own_epoch(tmp_path):
    from test_observation_activities import (
        start_run, worker_of, observe, query_worker)

    cluster = Cluster(tmp_path / "state", lambda spec, message, state: None)
    run_id = start_run(cluster)
    worker = worker_of(cluster, run_id)
    cluster.controller.reconcile(run_id, worker["worker_id"])
    observe(cluster, run_id, observed="idle", observation_id=100,
            activities=[], complete=True)
    before = query_worker(cluster, run_id)
    inbox = cluster.api.list_inbox(operator(), before["worker_id"])["messages"]
    assert inbox
    cluster.scheduler._plan(
        run_id, [("worker", before["worker_id"], before["revision"])],
        [{"kind": "worker", "op": "update", "id": before["worker_id"],
          "fields": {"epoch": before["epoch"] + 1}}],
        "trace-test-rebind")
    trace = _service(cluster.api, cluster.backend)
    snapshot = trace.snapshot(operator(), run_id)
    identity = {(entry["worker_id"], entry["epoch"], key)
                for entry in snapshot["executions"]
                for key in entry["delivery_keys"]}
    for message in inbox:
        assert (before["worker_id"], message["epoch"],
                message["delivery_key"]) in identity
        assert (before["worker_id"], before["epoch"] + 1,
                message["delivery_key"]) not in identity
    current = [entry for entry in snapshot["executions"]
               if entry["current"]]
    assert current and all(entry["epoch"] == before["epoch"] + 1
                           for entry in current)


# -- request diagnostics ------------------------------------------------

def test_request_diagnostics_scan_one_page_and_resume_by_token(tmp_path):
    from node_runtime.control_plane.trace import (
        DIAGNOSTIC_PAGE_BYTES, RequestDiagnostics)

    log = tmp_path / "requests.jsonl"
    filler = json.dumps({"operation": "other", "padding": "x" * 800}) + "\n"
    lines = DIAGNOSTIC_PAGE_BYTES // len(filler.encode()) + 5
    target = json.dumps({"operation": "target",
                         "request_id": "wanted"}) + "\n"
    log.write_text(filler * lines + target, encoding="utf-8")
    diagnostics = RequestDiagnostics(log, service_id="svc-p3a")

    first = diagnostics.read(request_id="wanted", limit=100)
    assert first["records"] == []
    assert first["has_more"] is True
    # One bounded page per read: a filter that matches nothing must not make
    # the server scan the whole log before answering.
    assert first["next_cursor"]["offset"] <= DIAGNOSTIC_PAGE_BYTES
    token = first["next_cursor"]["token"]
    second = diagnostics.read(request_id="wanted", after=token, limit=100)
    assert [record["request_id"] for record in second["records"]] == [
        "wanted"]
    assert second["cursor"]["generation"] == diagnostics.generation()


def test_request_diagnostics_reject_rotated_or_raw_offsets(tmp_path):
    from node_runtime.control_plane.trace import RequestDiagnostics

    log = tmp_path / "requests.jsonl"
    diagnostics = RequestDiagnostics(log, service_id="svc-p3a")
    diagnostics.record(operation="first", outcome="ok", request_id="one")
    token = diagnostics.read()["next_cursor"]["token"]

    os.replace(log, log.with_name(log.name + ".1"))
    diagnostics.record(operation="second", outcome="ok", request_id="two")
    with pytest.raises(ControlPlaneError) as expired:
        diagnostics.read(after=token)
    assert expired.value.code == "cursor_expired"
    assert expired.value.details["instruction"] == "resnapshot"

    with pytest.raises(ControlPlaneError) as raw:
        diagnostics.read(after=10)
    assert raw.value.code == "invalid_argument"

    fresh = diagnostics.read()
    assert [record["request_id"] for record in fresh["records"]] == ["two"]


def test_request_diagnostics_unconfigured_still_has_a_cursor(tmp_path):
    cluster = Cluster(tmp_path / "data", None)
    trace = _service(cluster.api, cluster.backend)
    report = trace.requests(operator())
    assert report["availability"] == "unconfigured"
    assert report["records"] == []
    assert report["cursor"]["generation"] is None
    assert report["cursor"]["token"]
