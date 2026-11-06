"""Graph /2 definition library: validate, publish, refusal and reuse."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from copy import deepcopy

import pytest

from conftest import make_graph
from helpers import Cluster, accepting_agent, publisher, reader, rid

from node_runtime.control_plane.api import Api
from node_runtime.control_plane.resources import Actor, ControlPlaneError, digest
from node_runtime.control_plane.storage import Tx


def _counts(api: Api) -> dict[str, int]:
    with api.store.read() as tx:
        return {
            table: tx.one(f"SELECT COUNT(*) AS n FROM {table}")["n"]
            for table in ("definitions", "artifacts", "idempotency")
        }


def _file_hash(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_validate_is_read_only_and_matches_publish_identity(cluster):
    document = make_graph()
    before = _counts(cluster.api)
    first = cluster.api.validate_definition(publisher(), document)
    second = cluster.api.validate_definition(publisher(), document)
    after = _counts(cluster.api)
    assert before == after == {"definitions": 0, "artifacts": 0, "idempotency": 0}
    assert first == second
    assert first["document"]["format"] == "agentgenome-graph-definition/2"

    published = cluster.publish(document)
    assert published["definition_ref"] == first["document_digest"]
    assert published["library_refs"] == first["library_refs"]
    assert set(published["library_refs"]) == set(first["agents"]) | set(first["tasks"])


def test_graph_v1_is_refused_without_writing(cluster):
    legacy = {
        "format": "agentgenome-graph-definition/1",
        "definition_id": "legacy",
        "policy": "fixed",
        "budget_limits": {"max_nodes": 1, "max_depth": 0, "max_rounds": 1},
        "agents": {},
        "tasks": {},
        "nodes": {},
    }
    before = _counts(cluster.api)
    with pytest.raises(ControlPlaneError) as rejected:
        cluster.api.validate_definition(publisher(), legacy)
    assert rejected.value.code == "invalid_argument"
    assert "Graph /1" in rejected.value.message
    with pytest.raises(ControlPlaneError):
        cluster.api.publish_definition(publisher(), legacy, rid("publish"))
    assert _counts(cluster.api) == before


def test_schema_v1_is_refused_before_any_write(tmp_path):
    root = tmp_path / "legacy-data"
    root.mkdir()
    database = root / "state.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        connection.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', '1')")
        connection.commit()
    finally:
        connection.close()
    before = _file_hash(database)
    with pytest.raises(ControlPlaneError) as refused:
        Api(root)
    assert refused.value.code == "capability_unsupported"
    assert refused.value.details["schema_version"] == "1"
    assert _file_hash(database) == before
    assert not (root / "state.sqlite3-wal").exists()
    assert not (root / "state.sqlite3-shm").exists()


def test_schema_unknown_tables_are_refused_unchanged(tmp_path):
    root = tmp_path / "unknown-data"
    root.mkdir()
    database = root / "state.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE runs (id TEXT PRIMARY KEY)")
        connection.commit()
    finally:
        connection.close()
    before = _file_hash(database)
    with pytest.raises(ControlPlaneError) as refused:
        Api(root)
    assert refused.value.code == "capability_unsupported"
    assert _file_hash(database) == before


def test_publish_rolls_back_library_metadata_on_failure(cluster, monkeypatch):
    document = make_graph()
    original = Tx.idempotency_store

    def fail_receipt(self, *args, **kwargs):
        raise RuntimeError("injected publish receipt failure")

    monkeypatch.setattr(Tx, "idempotency_store", fail_receipt)
    with pytest.raises(RuntimeError):
        cluster.publish(document, request_id="publish-rollback")
    assert _counts(cluster.api) == {"definitions": 0, "artifacts": 0,
                                    "idempotency": 0}

    monkeypatch.setattr(Tx, "idempotency_store", original)
    published = cluster.publish(document, request_id="publish-rollback")
    assert published["unchanged"] is False
    assert published["revision"] == 1
    counts = _counts(cluster.api)
    assert counts["definitions"] == 1
    assert counts["artifacts"] == len(published["library_refs"])


def test_library_content_is_authorized_and_tamper_evidence_is_checked(cluster):
    document = make_graph()
    agent_document = document["agents"]["doer"]
    canonical = json.dumps(agent_document, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False).encode()
    ref = cluster.artifact(canonical, "application/json")
    assert ref["id"] == digest(agent_document)

    scoped = Actor("scoped-publisher", {"publisher"}, run_ids=frozenset())
    reusing = deepcopy(document)
    reusing["agents"]["doer"] = ref["id"]
    with pytest.raises(ControlPlaneError) as unauthorized:
        cluster.api.validate_definition(scoped, reusing)
    assert unauthorized.value.code == "not_found"

    published = cluster.publish(reusing)
    assert ref["id"] in published["library_refs"]

    path = cluster.api.artifacts._path(ref["sha256"])
    path.write_bytes(b"tampered")
    with pytest.raises(ControlPlaneError) as tampered:
        cluster.api.validate_definition(publisher(), reusing)
    assert tampered.value.code == "artifact_mismatch"


def test_normalized_output_recompiles_to_the_same_identity(cluster):
    document = make_graph()
    normalized = cluster.api.validate_definition(publisher(), document)
    cluster.publish(document)
    again = cluster.api.validate_definition(publisher(), normalized["document"])
    assert again["document_digest"] == normalized["document_digest"]
    assert again["document"] == normalized["document"]


def test_two_sops_reuse_the_same_library_without_sharing_instances(cluster):
    resolved = cluster.api.validate_definition(
        publisher(), make_graph(definition_id="first-sop"))
    first = cluster.publish(make_graph(definition_id="first-sop"))
    # Identical Agent content is one library object even when two aliases
    # name it; the task catalog still has two distinct Task definitions.
    assert len(set(resolved["agents"])) == 1
    assert len(set(resolved["tasks"])) == 2

    second_document = make_graph(definition_id="second-sop")
    second_document["agents"] = resolved["document"]["agents"]
    second_document["tasks"] = resolved["document"]["tasks"]
    second = cluster.publish(second_document)
    assert first["library_refs"] == second["library_refs"]

    artifact = cluster.artifact(b"dataset", "text/plain")
    run_one = cluster.create_run(first["definition_ref"], {"dataset": artifact})
    run_two = cluster.create_run(second["definition_ref"], {"dataset": artifact})
    one = cluster.drive(run_one["run_id"])
    two = cluster.drive(run_two["run_id"])
    assert one["run"]["status"] == two["run"]["status"] == "completed"
    nodes_one = {node["node_id"] for node in one["nodes"]}
    nodes_two = {node["node_id"] for node in two["nodes"]}
    assert nodes_one.isdisjoint(nodes_two)


def test_compile_accepts_only_nested_delegation_fields(cluster):
    document = make_graph()
    document["nodes"]["first"]["delegation"] = {
        "context": {"business_key": "value"},
        "tool_profile": {"tools": ["node.submit_candidate"]},
        "budget": {"max_provider_calls": 2},
        "runtime_options": {},
    }
    normalized = cluster.api.validate_definition(publisher(), document)
    assert normalized["document"]["nodes"]["first"]["delegation"]["context"] == {
        "business_key": "value"}

    broken = deepcopy(document)
    broken["nodes"]["first"]["delegation"]["surprise"] = True
    with pytest.raises(ControlPlaneError) as unknown:
        cluster.api.validate_definition(publisher(), broken)
    assert unknown.value.code == "invalid_argument"

    broken = deepcopy(document)
    broken["nodes"]["first"]["delegation"]["context"] = "not-an-object"
    with pytest.raises(ControlPlaneError):
        cluster.api.validate_definition(publisher(), broken)


@pytest.fixture
def cluster(tmp_path):
    instance = Cluster(tmp_path / "data", None)
    instance.backend.agent = accepting_agent(instance.api)
    return instance
