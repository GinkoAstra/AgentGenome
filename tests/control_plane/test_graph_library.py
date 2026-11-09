"""U02 C3: local graph library scanning and explicit publication."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import time
import urllib.request
import uuid

import pytest
import yaml

from conftest import make_graph
from node_runtime.control_plane.client_access import connect_client
from node_runtime.control_plane.graph_library import (
    publish_entry, scan_library)
from node_runtime.control_plane.host import Host
from node_runtime.control_plane.resources import ControlPlaneError
from node_runtime.control_plane.server import ApiServer


def _rid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _graph(definition_id: str = "library-graph") -> dict:
    return make_graph(definition_id=definition_id)


def _write_graph(path: Path, document: dict | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document or _graph(), sort_keys=False),
                    encoding="utf-8")
    return path


def _manifest(entry: Path, **overrides) -> Path:
    body = {"format": "agentgenome-library-entry/1", "name": entry.name,
            "graph": "graph.yaml", "description": "library entry",
            "usage": "README.md"}
    body.update(overrides)
    (entry / "agentgenome.json").write_text(json.dumps(body),
                                            encoding="utf-8")
    return entry / "agentgenome.json"


def _wait_ready(endpoint: str, timeout: float = 10.0) -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            with opener.open(endpoint + "/readyz", timeout=1.0) as response:
                last = json.loads(response.read().decode("utf-8"))
            if last.get("ready") is True:
                return
        except Exception:
            pass
        time.sleep(0.02)
    raise AssertionError(f"service never became ready: {last}")


def _service(path: Path) -> SimpleNamespace:
    host = Host(path, backend="script", scheduler_interval=0.02,
                poll_seconds=0.0)
    server = ApiServer(host, port=0, heartbeat_timeout=5.0)
    endpoint = server.start_background()
    credentials = server.auth.publisher_credentials()
    _wait_ready(endpoint)
    return SimpleNamespace(server=server, host=host,
                           data_dir=Path(host.data_dir), endpoint=endpoint,
                           token=credentials["token"],
                           service_id=credentials["service_id"])


def test_scan_reads_manifestless_and_manifest_entries(tmp_path):
    root = tmp_path / "library"
    alpha = root / "alpha"
    _write_graph(alpha / "graph.yaml", _graph("alpha"))
    (alpha / "program.py").write_text("raise SystemExit('never imported')",
                                      encoding="utf-8")
    (alpha / "deployment.json").write_text("{}", encoding="utf-8")
    (alpha / ".local").mkdir()
    (alpha / ".local" / "secret").write_text("x", encoding="utf-8")

    beta = root / "beta"
    _write_graph(beta / "graph.yaml", _graph("beta"))
    (beta / "README.md").write_text("usage", encoding="utf-8")
    _manifest(beta, name="beta-name", description="with manifest",
              usage="README.md")

    hidden = root / ".hidden"
    _write_graph(hidden / "graph.yaml", _graph("hidden"))
    noise = root / "noise"
    noise.mkdir(parents=True)
    (noise / "notes.txt").write_text("not a graph", encoding="utf-8")

    entries = scan_library(root)
    assert [entry.name for entry in entries] == ["alpha", "beta-name"]

    alpha_entry, beta_entry = entries
    assert alpha_entry.root == alpha.resolve()
    assert alpha_entry.graph_path == (alpha / "graph.yaml").resolve()
    assert alpha_entry.description == ""
    assert alpha_entry.usage_path is None

    assert beta_entry.root == beta.resolve()
    assert beta_entry.description == "with manifest"
    assert beta_entry.usage_path == (beta / "README.md").resolve()


@pytest.mark.parametrize("field,value", [
    ("format", "agentgenome-library-entry/2"),
    ("name", 5),
    ("graph", 5),
    ("description", 5),
    ("usage", 5),
])
def test_manifest_fields_are_strictly_typed(tmp_path, field, value):
    root = tmp_path / "library"
    entry = root / "entry"
    _write_graph(entry / "graph.yaml")
    (entry / "README.md").write_text("usage", encoding="utf-8")
    _manifest(entry, **{field: value})
    with pytest.raises(ControlPlaneError) as failure:
        scan_library(root)
    assert failure.value.code == "invalid_argument"
    assert failure.value.details["entry"] == "entry"


@pytest.mark.parametrize("declared", [
    "../outside.yaml", "/tmp/outside.yaml", "link.yaml", "missing.yaml",
])
def test_manifest_graph_must_be_an_inside_regular_file(tmp_path, declared):
    root = tmp_path / "library"
    entry = root / "entry"
    entry.mkdir(parents=True)
    outside = tmp_path / "outside.yaml"
    _write_graph(outside)
    (entry / "link.yaml").symlink_to(outside)
    _manifest(entry, graph=declared)
    with pytest.raises(ControlPlaneError) as failure:
        scan_library(root)
    assert failure.value.code in ("invalid_argument", "not_found")
    assert failure.value.details["entry"] == "entry"


def test_manifest_usage_cannot_escape_the_entry(tmp_path):
    root = tmp_path / "library"
    entry = root / "entry"
    _write_graph(entry / "graph.yaml")
    (tmp_path / "usage.md").write_text("outside", encoding="utf-8")
    _manifest(entry, usage="../usage.md")
    with pytest.raises(ControlPlaneError) as failure:
        scan_library(root)
    assert failure.value.code == "invalid_argument"
    assert failure.value.details["field"] == "usage"


def test_manifest_symlink_escaping_the_entry_is_rejected(tmp_path):
    root = tmp_path / "library"
    entry = root / "entry"
    _write_graph(entry / "graph.yaml")
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"format": "agentgenome-library-entry/1",
                                   "name": "outside", "graph": "graph.yaml",
                                   "description": ""}), encoding="utf-8")
    (entry / "agentgenome.json").symlink_to(outside)
    with pytest.raises(ControlPlaneError) as failure:
        scan_library(root)
    assert failure.value.code == "invalid_argument"


def test_symlinked_entry_escaping_the_library_root_is_rejected(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    outside = tmp_path / "outside"
    _write_graph(outside / "graph.yaml")
    (root / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ControlPlaneError) as failure:
        scan_library(root)
    assert failure.value.code == "invalid_argument"
    assert failure.value.details["entry"] == "linked"


def test_duplicate_entry_names_are_rejected(tmp_path):
    root = tmp_path / "library"
    for directory in ("one", "two"):
        entry = root / directory
        _write_graph(entry / "graph.yaml")
        _manifest(entry, name="same-name", usage=None)
    with pytest.raises(ControlPlaneError) as failure:
        scan_library(root)
    assert failure.value.code == "invalid_argument"
    assert failure.value.details["name"] == "same-name"


def test_missing_library_root_is_an_explicit_error(tmp_path):
    with pytest.raises(ControlPlaneError) as failure:
        scan_library(tmp_path / "absent")
    assert failure.value.code == "not_found"


class _RecordingClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def call(self, operation: str, arguments: dict) -> dict:
        self.calls.append((operation, arguments))
        if operation == "validate_definition":
            return {"definition_id": arguments["document"]["definition_id"]}
        return {"definition_ref": "sha256:" + "0" * 64, "revision": 1}


def test_publish_entry_keeps_author_base_revision_and_validates_first(tmp_path):
    root = tmp_path / "library"
    _write_graph(root / "entry" / "graph.yaml", _graph("author-base"))
    entry = scan_library(root)[0]
    client = _RecordingClient()

    result = publish_entry(client, entry, request_id="import-1")
    assert result["revision"] == 1
    assert [operation for operation, _ in client.calls] == [
        "validate_definition", "publish_definition"]
    validated = client.calls[0][1]["document"]
    published = client.calls[1][1]
    assert validated["base_revision"] == 0
    assert published["request_id"] == "import-1"
    assert published["document"]["base_revision"] == 0

    client.calls.clear()
    publish_entry(client, entry, request_id="import-2", base_revision=4)
    assert client.calls[0][1]["document"]["base_revision"] == 4
    assert client.calls[1][1]["document"]["base_revision"] == 4

    for invalid in (True, -1, "1", 1.5):
        with pytest.raises(ControlPlaneError) as failure:
            publish_entry(client, entry, request_id="bad",
                          base_revision=invalid)  # type: ignore[arg-type]
        assert failure.value.code == "invalid_argument"


def test_publish_rejects_graph_replaced_by_outside_symlink_after_scan(tmp_path):
    root = tmp_path / "library"
    entry_dir = root / "entry"
    _write_graph(entry_dir / "graph.yaml", _graph("inside"))
    outside = tmp_path / "outside.yaml"
    _write_graph(outside, _graph("outside"))
    entry = scan_library(root)[0]

    (entry_dir / "graph.yaml").unlink()
    (entry_dir / "graph.yaml").symlink_to(outside)

    client = _RecordingClient()
    with pytest.raises(ControlPlaneError) as failure:
        publish_entry(client, entry, request_id="swapped")
    assert failure.value.code == "invalid_argument"
    assert client.calls == []


def test_real_publish_is_idempotent_and_reports_version_conflict(tmp_path):
    service = _service(tmp_path / "data")
    client = connect_client(data_dir=service.data_dir, client_kind="script",
                            client_session_id="library",
                            workspace="/work/library")
    try:
        root = tmp_path / "library"
        entry_dir = root / "entry"
        document = _graph("c3-real")
        document["description"] = "first"
        _write_graph(entry_dir / "graph.yaml", document)
        (entry_dir / "program.py").write_text("PROGRAM_MARKER",
                                              encoding="utf-8")
        (entry_dir / "deployment.json").write_text("DEPLOY_MARKER",
                                                   encoding="utf-8")
        entry = scan_library(root)[0]

        first = publish_entry(client, entry, request_id="import-1")
        assert first["revision"] == 1
        replay = publish_entry(client, entry, request_id="import-1")
        assert replay == first

        changed = _graph("c3-real")
        changed["description"] = "second"
        _write_graph(entry_dir / "graph.yaml", changed)
        with pytest.raises(ControlPlaneError) as conflict:
            publish_entry(client, entry, request_id="import-2")
        assert conflict.value.code == "revision_conflict"
        assert conflict.value.details["current_revision"] == 1

        changed["base_revision"] = 1
        _write_graph(entry_dir / "graph.yaml", changed)
        second = publish_entry(client, entry, request_id="import-3")
        assert second["revision"] == 2

        definition = client.call("get_resource", {
            "kind": "definition", "identifier": second["definition_ref"]})
        serialized = json.dumps(definition)
        assert "PROGRAM_MARKER" not in serialized
        assert "DEPLOY_MARKER" not in serialized
    finally:
        client.close()
        service.server.stop()
