"""P1 acceptance: the real network service over an isolated data directory.

Every test below uses a real listening socket on loopback, the SDK's
Streamable HTTP MCP client, and a temporary Host composed with the script
backend. No model, installed Host, user data directory or global config is
touched.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import socket
import stat
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import httpx2
import pytest
import yaml
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS

from conftest import make_graph
from node_runtime.control_plane.client import (ApiClient, ApiTransportError,
                                               TRANSPORT_PROTOCOL,
                                               TRANSPORT_TIMEOUT,
                                               TRANSPORT_UNAVAILABLE)
from node_runtime.control_plane.host import Host
from node_runtime.control_plane.http_api import PUBLIC_OPERATIONS
from node_runtime.control_plane.resources import ControlPlaneError
from node_runtime.control_plane.server import (ApiServer, ServiceStartError,
                                               StopIncomplete)

MCP_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "p1-acceptance", "version": "0"},
    },
}


def _rid(prefix: str = "p1") -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _service(tmp_path: Path, name: str = "data", **host_kwargs) -> SimpleNamespace:
    options = {"backend": "script", "scheduler_interval": 0.02,
               "poll_seconds": 0.0}
    options.update(host_kwargs)
    host = Host(tmp_path / name, **options)
    server = ApiServer(host, port=0, heartbeat_timeout=5.0)
    endpoint = server.start_background()
    credentials = server.auth.publisher_credentials()
    instance = SimpleNamespace(server=server, host=host, endpoint=endpoint,
                               token=credentials["token"],
                               service_id=credentials["service_id"])
    _wait_ready(endpoint)
    return instance


@pytest.fixture
def isolated(tmp_path):
    instance = _service(tmp_path)
    try:
        yield instance
    finally:
        instance.server.stop()


def _http(endpoint: str, path: str, *, method: str = "GET",
          payload: dict | None = None, token: str | None = None,
          timeout: float = 5.0) -> tuple[int, dict]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(endpoint + path, data=data,
                                     method=method, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        with error:
            return error.code, json.loads(error.read().decode("utf-8"))


def _wait_ready(endpoint: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    last: tuple[int, dict] | None = None
    while time.monotonic() < deadline:
        last = _http(endpoint, "/readyz")
        if last[0] == 200 and last[1].get("ready") is True:
            return last[1]
        time.sleep(0.02)
    raise AssertionError(f"service never became ready: {last}")


def _mcp(endpoint: str, token: str, action) -> object:
    """Run one real Streamable HTTP MCP session and return the action result."""

    async def run():
        client = httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {token}"}, timeout=30.0,
            trust_env=False)
        try:
            async with streamable_http_client(
                    endpoint + "/mcp", http_client=client,
                    terminate_on_close=False) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await action(session)
        finally:
            await client.aclose()

    return asyncio.run(run())


def _mcp_envelope(endpoint: str, token: str, tool: str,
                  arguments: dict) -> dict:
    async def call(session):
        return await session.call_tool(tool, arguments)

    result = _mcp(endpoint, token, call)
    assert result.is_error is False, result.content
    assert isinstance(result.structured_content, dict)
    return result.structured_content


def _mcp_error(call) -> MCPError:
    """The SDK wraps request errors in anyio task groups; unwrap one root."""
    try:
        call()
    except BaseException as error:  # intentional: any transport-level refusal
        while isinstance(error, BaseExceptionGroup):
            if len(error.exceptions) != 1:
                raise AssertionError(f"multiple MCP failures: {error!r}")
            error = error.exceptions[0]
        assert isinstance(error, MCPError), error
        return error
    raise AssertionError("expected the MCP server to reject this request")


def _publish(client: ApiClient, definition_id: str = "p1-graph") -> tuple[dict, dict]:
    document = make_graph(definition_id=definition_id)
    result = client.call("publish_definition",
                         {"request_id": _rid("publish"),
                          "document": document})
    return document, result


def _artifact(client: ApiClient, payload: bytes,
              media_type: str = "application/octet-stream") -> dict:
    return client.call("put_artifact", {
        "request_id": _rid("artifact"),
        "base64": base64.b64encode(payload).decode("ascii"),
        "media_type": media_type,
    })["artifact"]


def _run(client: ApiClient, definition_ref: str,
         artifact: dict) -> dict:
    return client.call("create_run", {
        "definition_ref": definition_ref,
        "inputs": {"dataset": artifact},
        "request_id": _rid("run"),
    })


def _artifact_args(ref: dict, *, id_key: str = "id") -> dict:
    return {id_key: ref["id"], "sha256": ref["sha256"],
            "size": ref["size"], "media_type": ref["media_type"]}


# -- P1-A --------------------------------------------------------------

def test_p1a_http_and_mcp_share_one_service_instance(isolated):
    endpoint, token = isolated.endpoint, isolated.token
    status, health = _http(endpoint, "/healthz")
    assert status == 200 and health["ok"] is True
    assert health["service"] == "agentgenome"
    ready = _wait_ready(endpoint)
    assert ready["checks"] == {"storage": True, "control_loop": True}

    client = ApiClient(endpoint, token)
    document = make_graph(definition_id="p1-a")
    validated = client.call("validate_definition", {"document": document})
    assert validated["definition_id"] == "p1-a"

    published = client.call("publish_definition", {
        "request_id": _rid("publish"), "document": document})
    assert published["definition_ref"]
    assert published["revision"] == 1

    payload = b"\x00\x01\xffP1 exact bytes\xfe\x00"
    artifact = _artifact(client, payload)
    fetched = client.call("read_artifact", _artifact_args(artifact))
    assert base64.b64decode(fetched["base64"]) == payload

    run = _run(client, published["definition_ref"], artifact)
    view = client.call("get_resource", {"kind": "run",
                                        "identifier": run["run_id"]})
    assert view["run_id"] == run["run_id"]
    listed = client.call("list_runs", {})
    assert [item["run_id"] for item in listed["runs"]] == [run["run_id"]]
    events = client.call("read_events", {"run_id": run["run_id"]})
    assert events["events"][0]["kind"] == "run_created"
    report = client.call("get_report", {"run_id": run["run_id"]})
    assert report["run"]["run_id"] == run["run_id"]

    for _ in range(10):
        fresh = client.call("get_resource", {"kind": "run",
                                             "identifier": run["run_id"]})
        try:
            intent = client.call("set_run_intent", {
                "run_id": run["run_id"], "desired": "paused",
                "expected_revision": fresh["revision"],
                "request_id": _rid("intent")})
            break
        except ControlPlaneError as error:
            if error.code != "revision_conflict":
                raise
            time.sleep(0.02)
    else:
        raise AssertionError("could not commit a run intent")
    assert intent["desired"] == "paused"

    async def listing(session):
        return await session.list_tools()

    tools = _mcp(endpoint, token, listing)
    assert len(tools.tools) == len(PUBLIC_OPERATIONS)
    assert {tool.name for tool in tools.tools} == set(PUBLIC_OPERATIONS)

    uploaded = _mcp_envelope(endpoint, token, "validate_definition",
                             {"definition_text": yaml.safe_dump(document)})
    assert uploaded["ok"] is True
    assert uploaded["result"]["definition_id"] == "p1-a"

    # Cross-protocol: the HTTP-published definition and run are visible to MCP.
    mcp_definition = _mcp_envelope(
        endpoint, token, "get_resource",
        {"kind": "definition", "identifier": published["definition_ref"]})
    assert mcp_definition["ok"] is True
    mcp_report = _mcp_envelope(endpoint, token, "get_report",
                               {"run_id": run["run_id"]})
    assert mcp_report["ok"] is True
    assert mcp_report["result"]["run"]["run_id"] == run["run_id"]

    mcp_artifact = _mcp_envelope(
        endpoint, token, "read_artifact", _artifact_args(artifact,
                                                         id_key="artifact_id"))
    assert mcp_artifact["ok"] is True
    assert base64.b64decode(mcp_artifact["result"]["base64"]) == payload

    # Cross-protocol: an MCP write is visible to the HTTP client.
    mcp_text = _mcp_envelope(
        endpoint, token, "put_artifact",
        {"request_id": _rid("mcp-artifact"), "text": "from-mcp",
         "media_type": "text/plain"})
    assert mcp_text["ok"] is True
    mcp_ref = mcp_text["result"]["artifact"]
    read_back = client.call("read_artifact", _artifact_args(mcp_ref))
    assert read_back["text"] == "from-mcp"


# -- P1-B --------------------------------------------------------------

def test_p1b_credentials_are_private_and_never_echoed(isolated):
    data_dir = Path(isolated.host.data_dir)
    auth_path = data_dir / "service-auth.json"
    assert stat.S_IMODE(auth_path.stat().st_mode) == 0o600
    service_record = json.loads((data_dir / "service.json").read_text())
    assert isolated.token not in json.dumps(service_record)
    for path in ("/healthz", "/readyz"):
        status, body = _http(isolated.endpoint, path)
        assert status == 200
        assert isolated.token not in json.dumps(body)
    log_path = data_dir / "service.log"
    if log_path.exists():
        assert isolated.token not in log_path.read_text()


def test_p1b_missing_and_wrong_credentials_are_rejected(isolated):
    endpoint, token = isolated.endpoint, isolated.token
    for presented in (None, "Bearer not-the-token"):
        status, body = _http(endpoint, "/v1/operations/list_runs",
                             method="POST", payload={}, token=presented)
        assert status == 401
        assert body["ok"] is False
        assert body["error"]["code"] == "unauthorized"
        assert "not-the-token" not in json.dumps(body)
    status, body = _http(endpoint, "/mcp", method="POST",
                         payload=MCP_INITIALIZE, token=None)
    assert status == 401
    assert body["ok"] is False and body["error"]["code"] == "unauthorized"
    assert token not in json.dumps(body)


def test_p1b_unknown_operations_and_extra_arguments_are_structured(isolated):
    endpoint, token = isolated.endpoint, isolated.token
    status, body = _http(endpoint, "/v1/operations/delete_everything",
                         method="POST", payload={}, token=token)
    assert status == 400
    assert body["ok"] is False
    assert body["error"]["code"] == "invalid_argument"
    assert body["error"]["details"]["operation"] == "delete_everything"

    status, body = _http(endpoint, "/v1/operations/validate_definition",
                         method="POST",
                         payload={"document": make_graph(),
                                  "actor_id": "someone-else"},
                         token=token)
    assert status == 400
    assert body["error"]["code"] == "invalid_argument"
    assert "actor_id" in body["error"]["details"]["fields"]
    assert "bearer token" in body["error"]["details"]["reason"]

    identity = _mcp_error(lambda: _mcp_envelope(
        endpoint, token, "validate_definition",
        {"document": make_graph(), "roles": ["publisher"]}))
    assert identity.code == INVALID_PARAMS
    assert identity.data["fields"] == ["roles"]

    extra = _mcp_error(lambda: _mcp_envelope(
        endpoint, token, "validate_definition",
        {"document": make_graph(), "surprise": 1}))
    assert extra.code == INVALID_PARAMS
    assert extra.data["fields"] == ["surprise"]

    async def unknown_tool(session):
        return await session.call_tool("no_such_tool", {})

    rejected = _mcp(endpoint, token, unknown_tool)
    assert rejected.is_error is True
    assert "Unknown tool" in rejected.content[0].text


def test_p1b_cas_and_idempotency_match_across_protocols(isolated):
    endpoint, token = isolated.endpoint, isolated.token
    client = ApiClient(endpoint, token)
    _, published = _publish(client, "p1-b")
    artifact = _artifact(client, b"dataset")
    run = _run(client, published["definition_ref"], artifact)

    with pytest.raises(ControlPlaneError) as conflict:
        client.call("set_run_intent", {
            "run_id": run["run_id"], "desired": "paused",
            "expected_revision": 999, "request_id": _rid("cas")})
    assert conflict.value.code == "revision_conflict"

    mcp_conflict = _mcp_envelope(endpoint, token, "set_run_intent", {
        "run_id": run["run_id"], "desired": "paused",
        "expected_revision": 999, "request_id": _rid("mcp-cas")})
    assert mcp_conflict["ok"] is False
    assert mcp_conflict["error"]["code"] == "revision_conflict"
    assert int(mcp_conflict["error"]["details"]["current_revision"]) >= 1

    request_id = _rid("idempotent")
    argument = {"definition_ref": published["definition_ref"],
                "inputs": {"dataset": artifact}, "request_id": request_id}
    first = client.call("create_run", argument)
    replay = client.call("create_run", argument)
    assert replay["run_id"] == first["run_id"]
    runs = client.call("list_runs", {})["runs"]
    assert len([item for item in runs
                if item["run_id"] == first["run_id"]]) == 1


def test_p1b_parallel_requests_use_independent_store_connections(isolated):
    client = ApiClient(isolated.endpoint, isolated.token)
    results: list[dict] = []
    failures: list[BaseException] = []
    barrier = threading.Barrier(8)

    def worker(index: int) -> None:
        try:
            barrier.wait(5)
            client.call("put_artifact", {
                "request_id": _rid(f"parallel-{index}"),
                "text": f"body-{index}", "media_type": "text/plain"})
            results.append(client.call(
                "validate_definition",
                {"document": make_graph(definition_id=f"parallel-{index}")}))
        except BaseException as error:  # surfaced by the assertion below
            failures.append(error)

    threads = [threading.Thread(target=worker, args=(index,))
               for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert failures == []
    assert len(results) == 8
    assert {item["definition_id"] for item in results} == {
        f"parallel-{index}" for index in range(8)}


# -- P1-C --------------------------------------------------------------

def test_p1c_health_and_reads_respond_while_control_loop_is_busy(isolated):
    entered = threading.Event()
    release = threading.Event()

    def blocked() -> dict:
        entered.set()
        release.wait(10)
        return {"actions": [], "runs": 0}

    try:
        isolated.host.reconcile_once = blocked
        assert entered.wait(5), "control loop never entered the slow tick"
        started = time.monotonic()
        status, body = _http(isolated.endpoint, "/healthz", timeout=2)
        assert status == 200 and body["ok"] is True
        client = ApiClient(isolated.endpoint, isolated.token, timeout=2)
        assert client.call("list_runs", {})["runs"] == []
        assert time.monotonic() - started < 2
    finally:
        release.set()
    _wait_ready(isolated.endpoint)


def test_p1c_readiness_follows_control_loop_failure_and_recovery(isolated):
    original = isolated.host.reconcile_once

    def failing() -> dict:
        raise RuntimeError("synthetic reconciliation failure")

    isolated.host.reconcile_once = failing
    try:
        deadline = time.monotonic() + 10
        while True:
            status, body = _http(isolated.endpoint, "/readyz")
            if (status == 503
                    and body.get("control_loop_error") == "RuntimeError"):
                break
            if time.monotonic() > deadline:
                raise AssertionError(f"readiness did not report failure: {body}")
            time.sleep(0.02)
        assert body["checks"]["control_loop"] is False
        assert body["ready"] is False
    finally:
        isolated.host.reconcile_once = original

    ready = _wait_ready(isolated.endpoint)
    assert ready["checks"]["control_loop"] is True
    assert "control_loop_error" not in ready


# -- P1-D --------------------------------------------------------------

def test_p1d_duplicate_start_is_refused_and_restart_sees_same_state(
        isolated, tmp_path):
    client = ApiClient(isolated.endpoint, isolated.token)
    document, published = _publish(client, "p1-d")
    artifact = _artifact(client, b"persisted")
    run = _run(client, published["definition_ref"], artifact)

    duplicate = ApiServer(
        Host(isolated.host.data_dir, backend="script",
             scheduler_interval=0.02, poll_seconds=0.0),
        port=0, heartbeat_timeout=5.0)
    with pytest.raises(ServiceStartError) as refused:
        duplicate.start_background()
    assert refused.value.reason == "host_already_running"
    assert client.call("get_resource",
                       {"kind": "run", "identifier": run["run_id"]})["run_id"]

    isolated.server.stop()

    restarted = _service(tmp_path, name=Path(isolated.host.data_dir).name)
    try:
        assert restarted.service_id == isolated.service_id
        resumed = ApiClient(restarted.endpoint, restarted.token)
        definition = resumed.call("get_resource", {
            "kind": "definition",
            "identifier": published["definition_ref"]})
        assert definition["definition_id"] == "p1-d"
        reloaded = resumed.call("get_resource", {
            "kind": "run", "identifier": run["run_id"]})
        assert reloaded["run_id"] == run["run_id"]
        assert resumed.call("read_events",
                            {"run_id": run["run_id"]})["events"]
    finally:
        restarted.server.stop()


def test_p1d_stopping_one_service_leaves_another_running(tmp_path):
    first = _service(tmp_path, "first")
    second = _service(tmp_path, "second")
    try:
        first.server.stop()
        client = ApiClient(second.endpoint, second.token)
        assert client.call("list_runs", {})["runs"] == []
        status, body = _http(second.endpoint, "/healthz")
        assert status == 200 and body["ok"] is True
        _wait_ready(second.endpoint)
    finally:
        second.server.stop()


# -- P1-E --------------------------------------------------------------

def test_p1e_client_reports_a_stopped_service(tmp_path):
    instance = _service(tmp_path)
    client = ApiClient(instance.endpoint, instance.token)
    assert client.call("list_runs", {})["runs"] == []
    instance.server.stop()
    with pytest.raises(ApiTransportError) as failure:
        client.call("list_runs", {})
    assert failure.value.code == TRANSPORT_UNAVAILABLE
    assert failure.value.unknown_outcome is False


def test_p1e_timeout_is_unknown_outcome_and_reuses_the_request_id(isolated):
    client = ApiClient(isolated.endpoint, isolated.token)
    _, published = _publish(client, "p1-e")
    artifact = _artifact(client, b"timeout-dataset")
    arguments = {"definition_ref": published["definition_ref"],
                 "inputs": {"dataset": artifact},
                 "request_id": _rid("timeout-run")}
    original = isolated.server._dispatch

    def slow(actor, operation, request_arguments):
        time.sleep(0.6)
        return original(actor, operation, request_arguments)

    try:
        isolated.server._dispatch = slow
        fast = ApiClient(isolated.endpoint, isolated.token, timeout=0.15)
        with pytest.raises(ApiTransportError) as failure:
            fast.call("create_run", arguments)
        assert failure.value.code == TRANSPORT_TIMEOUT
        assert failure.value.unknown_outcome is True
    finally:
        isolated.server._dispatch = original

    deadline = time.monotonic() + 10
    while True:
        runs = client.call("list_runs", {})["runs"]
        if runs:
            break
        if time.monotonic() > deadline:
            raise AssertionError("timed-out write never committed")
        time.sleep(0.02)
    assert len(runs) == 1
    retried = client.call("create_run", arguments)
    assert retried["run_id"] == runs[0]["run_id"]
    assert len(client.call("list_runs", {})["runs"]) == 1


# -- P1 corrections (independent review, 2026-11-04) -------------------

def _fake_service(handler) -> ThreadingHTTPServer:
    http = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    http._test_thread = thread
    return http


def _close_fake_service(http: ThreadingHTTPServer) -> None:
    http.shutdown()
    http.server_close()
    http._test_thread.join(timeout=3)


def test_lost_response_after_write_is_unknown_outcome_and_not_retried():
    received: list[dict] = []

    class DropResponse(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            received.append(json.loads(self.rfile.read(length)))
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()

        def log_message(self, *args):
            pass

    http = _fake_service(DropResponse)
    try:
        endpoint = f"http://127.0.0.1:{http.server_port}"
        client = ApiClient(endpoint, "test-token")
        with pytest.raises(ApiTransportError) as failure:
            client.call("create_run", {"request_id": "keep-this-id"})
        assert failure.value.unknown_outcome is True, (
            "a dropped response cannot prove the write was not committed")
        assert len(received) == 1, "client must not replay a write implicitly"
        assert received[0]["request_id"] == "keep-this-id"
    finally:
        _close_fake_service(http)


def test_malformed_response_after_write_is_unknown_outcome():
    received: list[bytes] = []

    class BadJSON(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            received.append(self.rfile.read(length))
            body = b"<html>not a control-plane envelope</html>"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    http = _fake_service(BadJSON)
    try:
        endpoint = f"http://127.0.0.1:{http.server_port}"
        client = ApiClient(endpoint, "test-token")
        with pytest.raises(ApiTransportError) as failure:
            client.call("put_artifact", {"request_id": "keep", "text": "x"})
        assert failure.value.code == TRANSPORT_PROTOCOL
        assert failure.value.unknown_outcome is True
        assert len(received) == 1, "a parse failure must not trigger a retry"
    finally:
        _close_fake_service(http)


def test_stopping_blocked_control_loop_keeps_directory_ownership(tmp_path):
    entered, release = threading.Event(), threading.Event()
    host = Host(tmp_path / "ownership", backend="script",
                scheduler_interval=0.01, poll_seconds=0.0)

    def slow_tick() -> dict:
        entered.set()
        release.wait(20)
        return {"actions": []}

    host.reconcile_once = slow_tick
    service = ApiServer(host, port=0, heartbeat_timeout=5.0)
    service.start_background()
    assert entered.wait(5), "control loop never entered the slow tick"
    try:
        with pytest.raises(StopIncomplete) as incomplete:
            service.stop(timeout=0.5)
        assert "control_loop" in incomplete.value.pending
        assert host.record()["pid"] == os.getpid(), (
            "a live control thread lost its ownership")
        challenger = Host(host.data_dir, backend="script", poll_seconds=0.0)
        refusal = challenger.claim()
        assert refusal and refusal["error"] == "host_already_running"
    finally:
        release.set()
        if service._control_thread is not None:
            service._control_thread.join(timeout=3)
        service.stop()

    assert host.record()["pid"] is None, "completed stop must release"


def test_stopping_with_inflight_http_request_keeps_ownership(tmp_path):
    instance = _service(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = instance.server._dispatch

    def slow(actor, operation, request_arguments):
        entered.set()
        release.wait(20)
        return original(actor, operation, request_arguments)

    instance.server._dispatch = slow
    result: dict[str, object] = {}

    def request() -> None:
        try:
            client = ApiClient(instance.endpoint, instance.token)
            result["value"] = client.call("list_runs", {})
        except BaseException as error:  # surfaced by the assertions below
            result["error"] = error

    worker = threading.Thread(target=request)
    worker.start()
    assert entered.wait(5), "HTTP request never reached the blocked dispatch"
    try:
        with pytest.raises(StopIncomplete) as incomplete:
            instance.server.stop(timeout=0.5)
        assert "http" in incomplete.value.pending
        assert instance.host.record()["pid"] == os.getpid(), (
            "in-flight HTTP work must keep the claim")
        challenger = Host(instance.host.data_dir, backend="script",
                          poll_seconds=0.0)
        refusal = challenger.claim()
        assert refusal and refusal["error"] == "host_already_running"
    finally:
        release.set()
        worker.join(timeout=10)
        instance.server._dispatch = original
    assert "error" not in result, result
    assert result["value"]["runs"] == [], (
        "graceful stop must finish the in-flight response")
    instance.server.stop()
    assert instance.host.record()["pid"] is None


def test_mcp_put_artifact_preserves_explicit_empty_sources(isolated):
    endpoint, token = isolated.endpoint, isolated.token

    def invoke(arguments: dict) -> object:
        async def call(session):
            return await session.call_tool("put_artifact", arguments)

        return _mcp(endpoint, token, call)

    empty_text = invoke({"request_id": _rid("empty-text"), "text": ""})
    assert empty_text.is_error is False
    body = empty_text.structured_content
    assert body["ok"] is True, body
    reference = body["result"]["artifact"]
    assert reference["size"] == 0
    fetched = ApiClient(endpoint, token).call(
        "read_artifact", _artifact_args(reference))
    assert fetched["text"] == ""

    empty_base64 = invoke({"request_id": _rid("empty-b64"), "base64": ""})
    assert empty_base64.structured_content["ok"] is True
    assert empty_base64.structured_content["result"]["artifact"]["size"] == 0

    both = invoke({"request_id": _rid("both"), "text": "",
                   "base64": "eA=="})
    assert both.structured_content["ok"] is False
    assert both.structured_content["error"]["code"] == "invalid_argument"
    status, http_body = _http(
        endpoint, "/v1/operations/put_artifact", method="POST",
        payload={"request_id": _rid("http-both"), "text": "",
                 "base64": "eA=="}, token=token)
    assert status == 400
    assert http_body["error"]["code"] == both.structured_content["error"]["code"]
    assert (http_body["error"]["message"]
            == both.structured_content["error"]["message"])

    neither = invoke({"request_id": _rid("neither")})
    assert neither.structured_content["ok"] is False
    assert neither.structured_content["error"]["code"] == "invalid_argument"
    assert "pass exactly one" in neither.structured_content["error"]["message"]


