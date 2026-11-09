"""U01 acceptance: one service, two transports, shared catalog and metadata.

Every test uses a real loopback service over a temporary data directory and
the SDK's Streamable HTTP MCP client. No user data directory, model or global
configuration is touched.
"""
from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS

from conftest import make_graph
from node_runtime.control_plane.host import Host
from node_runtime.control_plane.http_api import PUBLIC_OPERATIONS
from node_runtime.control_plane.server import ApiServer


def _rid(prefix: str = "u01") -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _service(tmp_path: Path, name: str = "data") -> SimpleNamespace:
    host = Host(tmp_path / name, backend="script", scheduler_interval=0.02,
                poll_seconds=0.0)
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


def _call(endpoint: str, token: str, operation: str,
          payload: dict | None = None) -> dict:
    status, body = _http(endpoint, f"/v1/operations/{operation}",
                         method="POST", payload=payload or {}, token=token)
    assert status == 200, body
    assert body["ok"] is True and body["operation"] == operation, body
    return body["result"]


def _mcp(endpoint: str, token: str, action) -> object:
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


def _mcp_raw(endpoint: str, token: str, tool: str,
             arguments: dict) -> dict:
    """Return the envelope without asserting success, for negative parity."""
    async def call(session):
        return await session.call_tool(tool, arguments)

    result = _mcp(endpoint, token, call)
    assert result.is_error is False, result.content
    assert isinstance(result.structured_content, dict)
    return result.structured_content


def _mcp_error(call) -> MCPError:
    try:
        call()
    except BaseException as error:  # unwrap the SDK's task group
        while isinstance(error, BaseExceptionGroup):
            assert len(error.exceptions) == 1, error
            error = error.exceptions[0]
        assert isinstance(error, MCPError), error
        return error
    raise AssertionError("expected the MCP server to reject this request")


def _publish(endpoint: str, token: str, definition_id: str,
             description: str | None = None) -> dict:
    document = make_graph(definition_id=definition_id)
    if description is not None:
        document["description"] = description
    return _call(endpoint, token, "publish_definition",
                 {"request_id": _rid("publish"), "document": document})


def _artifact(endpoint: str, token: str, text: str = "dataset") -> dict:
    return _call(endpoint, token, "put_artifact",
                 {"request_id": _rid("artifact"), "text": text})["artifact"]


def test_service_info_is_identical_over_http_and_mcp(isolated):
    endpoint, token = isolated.endpoint, isolated.token
    http_info = _call(endpoint, token, "get_service_info")
    mcp_info = _mcp_envelope(endpoint, token, "get_service_info", {})

    assert mcp_info["ok"] is True
    assert mcp_info["result"] == http_info
    assert http_info["format"] == "agentgenome-service-info/1"
    assert http_info["protocol"] == "agentgenome-control-plane/1"
    assert http_info["service_id"] == isolated.service_id
    assert http_info["instance_id"] == isolated.host.instance_id
    assert http_info["operations"] == list(PUBLIC_OPERATIONS)
    refs = [backend["backend_ref"] for backend in http_info["backends"]]
    assert "script" in refs
    capabilities = {backend["backend_ref"]: backend["capabilities"]
                    for backend in http_info["backends"]}
    assert capabilities["script"]["live_observation"] == "full"
    assert token not in json.dumps(http_info)


def test_bearer_is_required_and_identity_arguments_are_rejected(isolated):
    endpoint, token = isolated.endpoint, isolated.token
    for presented in (None, "Bearer not-the-token"):
        status, body = _http(endpoint, "/v1/operations/get_service_info",
                             method="POST", payload={}, token=presented)
        assert status == 401
        assert body["ok"] is False and body["error"]["code"] == "unauthorized"
        assert "not-the-token" not in json.dumps(body)

    status, body = _http(endpoint, "/v1/operations/register_client",
                         method="POST",
                         payload={"client_kind": "pi",
                                  "client_session_id": "s",
                                  "workspace": "/w", "actor": "someone"},
                         token=token)
    assert status == 400
    assert body["error"]["code"] == "invalid_argument"
    assert body["error"]["details"]["fields"] == ["actor"]

    identity = _mcp_error(lambda: _mcp_envelope(
        endpoint, token, "register_client",
        {"client_kind": "pi", "client_session_id": "s",
         "workspace": "/w", "roles": ["publisher"]}))
    assert identity.code == INVALID_PARAMS
    assert identity.data["fields"] == ["roles"]


def test_registration_is_idempotent_across_http_and_mcp(isolated):
    endpoint, token = isolated.endpoint, isolated.token
    first = _call(endpoint, token, "register_client",
                  {"client_kind": "codex", "client_session_id": "parity",
                   "workspace": "/first"})
    second = _mcp_envelope(
        endpoint, token, "register_client",
        {"client_kind": "codex", "client_session_id": "parity",
         "workspace": "/second"})["result"]

    assert second["client_id"] == first["client_id"]
    assert second["registered_at"] == first["registered_at"]
    assert second["workspace"] == "/second"
    assert second["service_id"] == isolated.service_id

    other = _call(endpoint, token, "register_client",
                  {"client_kind": "pi", "client_session_id": "parity",
                   "workspace": "/second"})
    assert other["client_id"] != first["client_id"]

    removed = _mcp_envelope(endpoint, token, "unregister_client",
                            {"client_id": first["client_id"]})["result"]
    assert removed == {"client_id": first["client_id"], "removed": True}
    again = _call(endpoint, token, "unregister_client",
                  {"client_id": first["client_id"]})
    assert again == {"client_id": first["client_id"], "removed": False}


def test_claude_client_kind_is_accepted_over_http_and_mcp(isolated):
    endpoint, token = isolated.endpoint, isolated.token
    over_http = _call(endpoint, token, "register_client",
                      {"client_kind": "claude", "client_session_id": "claude",
                       "workspace": "/work/claude"})
    over_mcp = _mcp_envelope(
        endpoint, token, "register_client",
        {"client_kind": "claude", "client_session_id": "claude",
         "workspace": "/work/claude"})["result"]
    assert over_http["client_kind"] == "claude"
    assert over_mcp["client_id"] == over_http["client_id"]
    assert over_mcp["service_id"] == isolated.service_id


def test_catalog_pages_match_over_http_and_mcp(isolated):
    endpoint, token = isolated.endpoint, isolated.token
    for definition_id in ("u01-beta", "u01-alpha", "u01-gamma"):
        _publish(endpoint, token, definition_id,
                 description=f"graph {definition_id}")

    first = _call(endpoint, token, "list_definitions", {"limit": 2})
    assert [item["definition_id"] for item in first["items"]] == [
        "u01-alpha", "u01-beta"]
    assert first["next_cursor"]

    http_second = _call(endpoint, token, "list_definitions",
                        {"limit": 2, "after": first["next_cursor"]})
    mcp_second = _mcp_envelope(
        endpoint, token, "list_definitions",
        {"limit": 2, "after": first["next_cursor"]})["result"]
    assert mcp_second == http_second
    assert [item["definition_id"] for item in http_second["items"]] == [
        "u01-gamma"]
    assert http_second["next_cursor"] is None

    full = _mcp_envelope(endpoint, token, "list_definitions",
                         {"limit": 100})["result"]
    assert [item["definition_id"] for item in full["items"]] == [
        "u01-alpha", "u01-beta", "u01-gamma"]
    assert full["items"][0]["description"] == "graph u01-alpha"

    filtered = _mcp_envelope(endpoint, token, "list_definitions",
                             {"query": "gamma"})["result"]
    assert [item["definition_id"] for item in filtered["items"]] == [
        "u01-gamma"]


def test_list_definitions_explicit_argument_errors_match_http_and_mcp(isolated):
    endpoint, token = isolated.endpoint, isolated.token
    _publish(endpoint, token, "u01-parity", description="parity graph")

    rejected = [{"after": ""}, {"limit": True}, {"limit": "2"},
                {"query": 5}, {"limit": None}]
    for payload in rejected:
        status, http_body = _http(endpoint, "/v1/operations/list_definitions",
                                  method="POST", payload=payload, token=token)
        mcp_body = _mcp_raw(endpoint, token, "list_definitions", payload)
        assert status == 400, (payload, http_body)
        assert http_body["ok"] is False
        assert mcp_body["ok"] is False
        assert mcp_body["error"] == http_body["error"]

    accepted = [{}, {"query": None}, {"after": None}, {"limit": 1},
                {"query": "parity", "limit": 1}]
    for payload in accepted:
        status, http_body = _http(endpoint, "/v1/operations/list_definitions",
                                  method="POST", payload=payload, token=token)
        mcp_body = _mcp_raw(endpoint, token, "list_definitions", payload)
        assert status == 200, (payload, http_body)
        assert http_body["ok"] is True
        assert mcp_body == http_body
        assert [item["definition_id"] for item in http_body["result"]["items"]] \
            == ["u01-parity"]


def test_list_definitions_tool_schema_declares_types_and_bounds(isolated):
    endpoint, token = isolated.endpoint, isolated.token

    async def list_tools(session):
        return await session.list_tools()

    tools = _mcp(endpoint, token, list_tools)
    tool = next(tool for tool in tools.tools if tool.name == "list_definitions")
    properties = tool.input_schema["properties"]

    assert properties["query"]["type"] == ["string", "null"]
    assert properties["query"]["maxLength"] == 256
    assert properties["limit"]["type"] == "integer"
    assert properties["limit"]["minimum"] == 1
    assert properties["limit"]["maximum"] == 100
    assert properties["limit"]["default"] == 50
    assert properties["after"]["type"] == ["string", "null"]
    assert properties["after"]["maxLength"] == 4096
    assert tool.input_schema.get("required", []) == []


def test_two_client_sessions_share_catalog_run_and_unregister_is_metadata_only(
        isolated):
    endpoint, token = isolated.endpoint, isolated.token
    session_a = _call(endpoint, token, "register_client",
                      {"client_kind": "codex", "client_session_id": "a",
                       "workspace": "/work/a"})
    session_b = _call(endpoint, token, "register_client",
                      {"client_kind": "qoder", "client_session_id": "b",
                       "workspace": "/work/b"})
    assert session_a["client_id"] != session_b["client_id"]
    assert session_a["service_id"] == session_b["service_id"]

    published = _publish(endpoint, token, "u01-shared")
    artifact = _artifact(endpoint, token, "shared dataset")
    run = _call(endpoint, token, "create_run",
                {"definition_ref": published["definition_ref"],
                 "inputs": {"dataset": artifact},
                 "request_id": _rid("run")})

    for client in (session_a, session_b):
        assert client["client_kind"] in ("codex", "qoder")
        catalog = _call(endpoint, token, "list_definitions")
        assert "u01-shared" in [item["definition_id"]
                                for item in catalog["items"]]
        definition = _call(endpoint, token, "get_resource",
                           {"kind": "definition",
                            "identifier": published["definition_ref"]})
        assert definition["definition_id"] == "u01-shared"
        run_view = _call(endpoint, token, "get_resource",
                         {"kind": "run", "identifier": run["run_id"]})
        assert run_view["run_id"] == run["run_id"]
        assert "client_id" not in run_view

    # Removing one registration cannot cancel the run or the other window.
    _call(endpoint, token, "unregister_client",
          {"client_id": session_a["client_id"]})
    still_visible = _call(endpoint, token, "get_resource",
                          {"kind": "run", "identifier": run["run_id"]})
    assert still_visible["run_id"] == run["run_id"]
    reused = _call(endpoint, token, "register_client",
                   {"client_kind": "qoder", "client_session_id": "b",
                    "workspace": "/work/b"})
    assert reused["client_id"] == session_b["client_id"]


def test_restart_keeps_catalog_and_run_but_clears_window_registrations(
        isolated, tmp_path):
    endpoint, token = isolated.endpoint, isolated.token
    published = _publish(endpoint, token, "u01-restart")
    artifact = _artifact(endpoint, token)
    run = _call(endpoint, token, "create_run",
                {"definition_ref": published["definition_ref"],
                 "inputs": {"dataset": artifact},
                 "request_id": _rid("run")})
    registered = _call(endpoint, token, "register_client",
                       {"client_kind": "codex", "client_session_id": "restart",
                        "workspace": "/work/restart"})
    before = _call(endpoint, token, "list_definitions")

    isolated.server.stop()
    restarted = _service(tmp_path,
                         name=Path(isolated.host.data_dir).name)
    try:
        assert restarted.service_id == isolated.service_id
        assert restarted.token == token
        endpoint = restarted.endpoint

        after = _call(endpoint, restarted.token, "list_definitions")
        assert after == before

        resumed = _call(endpoint, restarted.token, "get_resource",
                        {"kind": "run", "identifier": run["run_id"]})
        assert resumed["run_id"] == run["run_id"]
        definition = _call(endpoint, restarted.token, "get_resource",
                           {"kind": "definition",
                            "identifier": published["definition_ref"]})
        assert definition["definition_id"] == "u01-restart"

        fresh = _call(endpoint, restarted.token, "register_client",
                      {"client_kind": "codex",
                       "client_session_id": "restart",
                       "workspace": "/work/restart"})
        assert fresh["client_id"] != registered["client_id"]
        assert fresh["service_id"] == isolated.service_id

        stale = _call(endpoint, restarted.token, "unregister_client",
                      {"client_id": registered["client_id"]})
        assert stale == {"client_id": registered["client_id"],
                         "removed": False}
    finally:
        restarted.server.stop()
