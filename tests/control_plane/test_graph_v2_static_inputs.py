"""Frozen Graph literal inputs grant run visibility before nodes exist.

A Graph may pin an uploaded artifact directly in ``node.inputs``. The run must
accept that declaration atomically at ``create_run`` and the Scheduler must be
able to materialise the first node without a bootstrap visibility deadlock.
Context-only refs are not grants, and Workers keep their own narrow closure.
"""
from __future__ import annotations

import time

import pytest

from conftest import make_graph
from helpers import operator, publisher
from node_runtime.control_plane.client import ApiClient
from node_runtime.control_plane.host import Host
from node_runtime.control_plane.resources import Actor, ControlPlaneError
from node_runtime.control_plane.server import ApiServer


def _static_graph(definition_id: str, literal: dict) -> dict:
    document = make_graph(definition_id=definition_id)
    document["nodes"]["first"]["inputs"] = {"dataset": literal}
    return document


def _scheduler(run_id: str) -> Actor:
    return Actor("static-scheduler", {"scheduler"}, run_ids={run_id})


def _ghost_ref() -> dict:
    return {"id": "sha256:" + "a" * 64, "sha256": "a" * 64,
            "size": 1, "media_type": "text/plain"}


def test_literal_graph_input_is_visible_before_node_materialisation(cluster):
    static = cluster.artifact(b"static-bytes", "text/plain")
    published = cluster.publish(_static_graph("static-boot", static))
    run_input = cluster.artifact(b"run-input", "text/plain")
    run = cluster.create_run(published["definition_ref"], {"dataset": run_input})

    assert cluster.api.read_artifact(_scheduler(run["run_id"]),
                                    static) == b"static-bytes"
    state = cluster.drive(run["run_id"])
    assert state["run"]["status"] == "completed"
    node = next(item for item in state["nodes"]
                if item["local_child_id"] == "first")
    assert [ref["id"] for ref in node["input_refs"]] == [static["id"]]


def test_missing_literal_graph_input_is_rejected_atomically(cluster):
    published = cluster.publish(_static_graph("static-missing", _ghost_ref()))
    run_input = cluster.artifact(b"run-input", "text/plain")
    with pytest.raises(ControlPlaneError) as error:
        cluster.create_run(published["definition_ref"], {"dataset": run_input})
    assert error.value.code == "not_found"
    assert cluster.api.list_runs(publisher())["runs"] == []


def test_mismatched_literal_graph_input_is_rejected_atomically(cluster):
    real = cluster.artifact(b"static-bytes", "text/plain")
    forged = {**real, "size": real["size"] + 1}
    published = cluster.publish(_static_graph("static-mismatch", forged))
    run_input = cluster.artifact(b"run-input", "text/plain")
    with pytest.raises(ControlPlaneError) as error:
        cluster.create_run(published["definition_ref"], {"dataset": run_input})
    assert error.value.code == "artifact_mismatch"
    assert cluster.api.list_runs(publisher())["runs"] == []


def test_context_only_artifact_is_not_granted(cluster):
    secret = cluster.artifact(b"secret-context", "text/plain")
    document = make_graph(definition_id="context-not-grant")
    document["nodes"]["first"]["delegation"] = {"context": {"hint": secret}}
    published = cluster.publish(document)
    run_input = cluster.artifact(b"run-input", "text/plain")
    run = cluster.create_run(published["definition_ref"], {"dataset": run_input})

    with pytest.raises(ControlPlaneError) as error:
        cluster.api.read_artifact(_scheduler(run["run_id"]), secret)
    assert error.value.code == "not_found"


def test_worker_closure_keeps_its_own_literal_and_rejects_siblings(cluster):
    static = cluster.artifact(b"static-bytes", "text/plain")
    published = cluster.publish(_static_graph("static-worker", static))
    run_input = cluster.artifact(b"run-input", "text/plain")
    run = cluster.create_run(published["definition_ref"], {"dataset": run_input})
    state = cluster.drive(run["run_id"])

    node = next(item for item in state["nodes"]
                if item["local_child_id"] == "first")
    worker = next(item for item in state["workers"]
                  if item["node_id"] == node["node_id"])
    actor = Actor(worker["worker_id"], {"worker"}, run_ids={run["run_id"]},
                  worker_id=worker["worker_id"], epoch=worker["epoch"])
    assert cluster.api.read_artifact(actor, static) == b"static-bytes"

    sibling = cluster.artifact(b"sibling-only", "text/plain")
    with pytest.raises(ControlPlaneError) as error:
        cluster.api.read_artifact(actor, sibling)
    assert error.value.code == "not_found"


def test_network_create_run_with_literal_graph_input_boots(tmp_path):
    host = Host(tmp_path / "data", backend="script",
                scheduler_interval=0.02, poll_seconds=0.0)
    server = ApiServer(host, port=0, heartbeat_timeout=5.0,
                       sse_poll_interval=0.05,
                       sse_heartbeat_interval=0.2)
    endpoint = server.start_background()
    try:
        client = ApiClient(endpoint,
                           server.auth.publisher_credentials()["token"])
        static = client.call("put_artifact", {
            "text": "static-over-network",
            "request_id": "static-network-artifact"})["artifact"]
        published = client.call("publish_definition", {
            "document": _static_graph("static-network", static),
            "request_id": "static-network-publish"})
        run_input = client.call("put_artifact", {
            "text": "run-input", "request_id": "static-network-input"})["artifact"]
        run = client.call("create_run", {
            "definition_ref": published["definition_ref"],
            "inputs": {"dataset": run_input},
            "request_id": "static-network-run"})

        # The live Host scheduler materialises the node and binds the frozen
        # literal on the following tick; poll briefly instead of assuming one.
        deadline = time.monotonic() + 3.0
        resources = None
        while time.monotonic() < deadline:
            resources = host.api.list_resources(operator(), run["run_id"])
            if any(ref["id"] == static["id"]
                   for node in resources["nodes"]
                   for ref in node.get("input_refs") or []):
                break
            time.sleep(0.05)
        assert resources is not None
        assert resources["run"]["status"] != "failed"
        assert any(ref["id"] == static["id"]
                   for node in resources["nodes"]
                   for ref in node.get("input_refs") or [])
        events = host.api.read_events(operator(), run["run_id"],
                                      limit=200)["events"]
        assert not [event for event in events
                    if event["kind"] == "scheduler_error"]
    finally:
        server.stop()
