"""U02 C1/C2/C5/C6: client attachment, discovery, reconnect and no replay."""
from __future__ import annotations

import json
from pathlib import Path
import time
from types import SimpleNamespace
import urllib.request
import uuid

import pytest

from conftest import make_graph
from node_runtime.control_plane.client import ApiClient, ApiTransportError
from node_runtime.control_plane.client_access import (
    ClientConnection, ServiceChangedError, connect_client)
from node_runtime.control_plane.discovery import Connection
from node_runtime.control_plane.host import Host
from node_runtime.control_plane.resources import ControlPlaneError
from node_runtime.control_plane.server import ApiServer

ROOT = Path(__file__).resolve().parents[2]


def _rid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


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


def _stop(instance: SimpleNamespace) -> None:
    try:
        instance.server.stop()
    except Exception:
        pass


def test_two_workspaces_share_default_service_and_visible_catalog(
        tmp_path, monkeypatch):
    state = tmp_path / "state"
    service = _service(state / "agentgenome" / "default")
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    workspace_a = tmp_path / "work-a"
    workspace_b = tmp_path / "work-b"
    workspace_a.mkdir()
    workspace_b.mkdir()
    clients = []
    try:
        monkeypatch.chdir(workspace_a)
        first = connect_client(data_dir=None, client_kind="pi",
                               client_session_id="c1-a",
                               workspace=workspace_a)
        clients.append(first)
        monkeypatch.chdir(workspace_b)
        second = connect_client(data_dir=None, client_kind="qoder",
                                client_session_id="c1-b",
                                workspace=workspace_b)
        clients.append(second)

        assert first.connection.data_dir == service.data_dir
        assert second.connection.data_dir == service.data_dir
        assert first.connection.service_id == service.service_id
        assert second.connection.service_id == service.service_id

        first.call("publish_definition",
                   {"request_id": _rid("publish"),
                    "document": make_graph(definition_id="c1-shared")})
        page_a = first.call("list_definitions", {})
        page_b = second.call("list_definitions", {})
        assert page_a == page_b
        assert [item["definition_id"] for item in page_a["items"]] == [
            "c1-shared"]

        fields = {"client_id", "service_id", "instance_id", "client_kind",
                  "client_session_id", "workspace", "registered_at"}
        for client in (first, second):
            assert set(client.registration) == fields
            assert client.registration["client_id"].startswith("client-")
            assert client.registration["service_id"] == service.service_id
            assert "worker_id" not in client.registration
            assert "run_id" not in client.registration
        assert (first.registration["client_kind"]
                != second.registration["client_kind"])
        assert first.registration["client_id"] != second.registration["client_id"]
    finally:
        for client in reversed(clients):
            client.close()
        _stop(service)


def test_missing_service_does_not_create_data_dir_or_database(
        tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    with pytest.raises(ControlPlaneError) as failure:
        connect_client(data_dir=None, client_kind="codex",
                       client_session_id="no-service", workspace="/work")
    assert failure.value.code == "service_unavailable"
    assert failure.value.details["data_dir"] == str(
        (state / "agentgenome" / "default").resolve())
    assert not (state / "agentgenome").exists()
    assert list(tmp_path.rglob("*.db")) == []
    assert list(tmp_path.rglob("*.sqlite*")) == []


def test_client_import_closure_has_no_control_plane_business_objects():
    script = (
        "import sys\n"
        "import node_runtime.control_plane.client_access\n"
        "import node_runtime.control_plane.graph_library\n"
        "import node_runtime.control_plane.client_mcp\n"
        "bad = [name for name in ("
        "'node_runtime.control_plane.api',"
        "'node_runtime.control_plane.storage',"
        "'node_runtime.control_plane.host'"
        ") if name in sys.modules]\n"
        "assert not bad, bad\n"
    )
    import subprocess
    import sys
    done = subprocess.run([sys.executable, "-c", script], cwd=ROOT,
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def test_package_public_exports_still_import_api_and_controller():
    script = (
        "from node_runtime.control_plane import Api, Controller\n"
        "from node_runtime.control_plane.api import Api as DirectApi\n"
        "from node_runtime.control_plane.controller import Controller as DirectController\n"
        "assert Api is DirectApi\n"
        "assert Controller is DirectController\n"
    )
    import subprocess
    import sys
    done = subprocess.run([sys.executable, "-c", script], cwd=ROOT,
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def test_reconnect_accepts_same_service_after_restart_with_new_instance(
        tmp_path):
    service = _service(tmp_path / "data")
    client = connect_client(data_dir=service.data_dir, client_kind="codex",
                            client_session_id="restart",
                            workspace="/work/restart")
    old_service_id = client.connection.service_id
    old_instance_id = client.connection.instance_id
    old_endpoint = client.connection.endpoint
    restarted = None
    try:
        _stop(service)
        (service.data_dir / "service.json").unlink()
        restarted = _service(tmp_path / "data")

        assert restarted.service_id == old_service_id
        assert restarted.host.instance_id != old_instance_id
        registration = client.reconnect()

        assert registration["service_id"] == old_service_id
        assert registration["instance_id"] == restarted.host.instance_id
        assert client.connection.service_id == old_service_id
        assert client.connection.instance_id == restarted.host.instance_id
        assert client.connection.endpoint != old_endpoint
        assert client.call("list_definitions", {}) == {
            "items": [], "next_cursor": None}
    finally:
        client.close()
        if restarted is not None:
            _stop(restarted)


def test_reconnect_rejects_a_changed_service_id(tmp_path):
    service = _service(tmp_path / "data")
    client = connect_client(data_dir=service.data_dir, client_kind="qoder",
                            client_session_id="changed", workspace="/work")
    old_service_id = client.connection.service_id
    old_endpoint = client.connection.endpoint
    restarted = None
    try:
        _stop(service)
        (service.data_dir / "service.json").unlink()
        (service.data_dir / "service-auth.json").unlink()
        restarted = _service(tmp_path / "data")
        assert restarted.service_id != old_service_id

        with pytest.raises(ServiceChangedError) as failure:
            client.reconnect()
        assert failure.value.code == "service_changed"
        assert failure.value.details["expected_service_id"] == old_service_id
        assert failure.value.details["observed_service_id"] == restarted.service_id

        # The original call is not migrated: the old endpoint is gone and the
        # new service never receives this request.
        with pytest.raises(ApiTransportError):
            client.call("list_runs", {})
        assert client.connection.endpoint == old_endpoint
        requests_log = restarted.data_dir / "requests.jsonl"
        assert "list_runs" not in (requests_log.read_text(encoding="utf-8")
                                   if requests_log.exists() else "")
    finally:
        client.close()
        if restarted is not None:
            _stop(restarted)


def test_close_only_unregisters_and_leaves_the_run_readable(tmp_path):
    service = _service(tmp_path / "data")
    first = connect_client(data_dir=service.data_dir, client_kind="pi",
                           client_session_id="closer", workspace="/work/a")
    reader = None
    try:
        published = first.call("publish_definition", {
            "request_id": _rid("publish"),
            "document": make_graph(definition_id="c6-close")})
        artifact = first.call("put_artifact", {
            "request_id": _rid("artifact"), "text": "dataset"})["artifact"]
        run = first.call("create_run", {
            "definition_ref": published["definition_ref"],
            "inputs": {"dataset": artifact},
            "request_id": _rid("run")})
        first.close()

        reader = connect_client(data_dir=service.data_dir, client_kind="codex",
                                client_session_id="reader",
                                workspace="/work/b")
        view = reader.call("get_resource", {
            "kind": "run", "identifier": run["run_id"]})
        assert view["run_id"] == run["run_id"]
        assert "client_id" not in view
        again = first.call("unregister_client", {
            "client_id": first.registration["client_id"]})
        assert again == {"client_id": first.registration["client_id"],
                         "removed": False}
    finally:
        if reader is not None:
            reader.close()
        _stop(service)


class _DropAfterRequest:
    """Accept exactly one POST, then close without a response."""

    def __init__(self) -> None:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        self.requests: list[bytes] = []
        recorder = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                recorder.requests.append(self.rfile.read(length))
                self.close_connection = True

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        import threading
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


def test_a_failed_create_run_is_sent_once_and_never_replayed(tmp_path):
    transport = _DropAfterRequest()
    try:
        connection = Connection(
            data_dir=tmp_path, endpoint=transport.endpoint, token="token",
            service_id="service", instance_id="instance", pid=None,
            ready=True, health={})
        registration = {
            "client_id": "client-test", "service_id": "service",
            "instance_id": "instance", "client_kind": "codex",
            "client_session_id": "no-replay", "workspace": "/work",
            "registered_at": "2026-11-09T00:00:00+00:00"}
        client = ClientConnection(
            connection=connection, registration=registration,
            api=ApiClient(connection.endpoint, connection.token))
        with pytest.raises(ApiTransportError) as failure:
            client.call("create_run", {
                "definition_ref": "sha256:" + "0" * 64,
                "inputs": {}, "request_id": "one-shot"})
        assert failure.value.unknown_outcome is True
        assert len(transport.requests) == 1
        body = json.loads(transport.requests[0].decode("utf-8"))
        assert body["request_id"] == "one-shot"
    finally:
        transport.close()
