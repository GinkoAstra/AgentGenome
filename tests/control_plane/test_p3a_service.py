"""P3a live service: heartbeat, slow subscriber, MCP parity and CLI reads.

Real loopback sockets and the SDK's Streamable HTTP MCP client over one
isolated script-backed Host. No model, installed service or user directory is
touched. Root's independent files are never imported or modified.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from conftest import make_graph
from helpers import operator
from node_runtime.control_plane.client import ApiClient
from node_runtime.control_plane.host import Host
from node_runtime.control_plane.server import ApiServer


ROOT = Path(__file__).resolve().parents[2]


def _get(server: ApiServer, path: str, params: dict | None = None):
    query = "?" + urlencode(params) if params else ""
    request = urllib.request.Request(
        server.endpoint + path + query,
        headers={"Authorization":
                 "Bearer " + server.auth.publisher_credentials()["token"]})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(request, timeout=5)


def _mcp(endpoint: str, token: str, action):
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


@pytest.fixture
def service(tmp_path):
    host = Host(tmp_path / "data", backend="script",
                scheduler_interval=0.02, poll_seconds=0.0)
    host.reconcile_once = lambda: {"actions": []}
    server = ApiServer(host, port=0, heartbeat_timeout=5.0,
                       sse_poll_interval=0.05,
                       sse_heartbeat_interval=0.2)
    endpoint = server.start_background()
    token = server.auth.publisher_credentials()["token"]
    client = ApiClient(endpoint, token)
    published = client.call("publish_definition", {
        "document": make_graph(definition_id="p3a-service"),
        "request_id": "p3a-publish"})
    artifact = client.call("put_artifact", {
        "text": "[]", "request_id": "p3a-input"})["artifact"]
    run = client.call("create_run", {
        "definition_ref": published["definition_ref"],
        "inputs": {"dataset": artifact},
        "request_id": "p3a-run"})
    try:
        yield server, client, token, run["run_id"]
    finally:
        server.stop()


def _snapshot(server, run_id):
    with _get(server, f"/v1/runs/{run_id}/trace") as response:
        return json.load(response)["result"]


def _seed_execution(server, run_id):
    server.host.scheduler.reconcile(run_id)
    resources = server.host.api.list_resources(operator(), run_id)
    worker = resources["workers"][0]
    inbox = server.host.api.list_inbox(operator(),
                                       worker["worker_id"])["messages"]
    delivery = inbox[0]
    path = (server.host.backends["script"].root / worker["worker_id"]
            / str(worker["epoch"]) / "state.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "worker_id": worker["worker_id"],
        "epoch": worker["epoch"],
        "spec": {"run_id": run_id},
        "deliveries": {delivery["delivery_key"]: {
            "state": "running",
            "started_at": "2026-11-04T00:00:00Z",
            "finished_at": None,
            "tools": [{"call_id": "call-1", "name": "read",
                       "effect": "read", "state": "completed"}],
        }},
    }), encoding="utf-8")
    return worker, delivery


def test_heartbeat_reaches_idle_subscriber_without_blocking_reads(service):
    server, client, _, run_id = service
    cursor = _snapshot(server, run_id)["cursor"]["token"]
    response = _get(server, f"/v1/runs/{run_id}/watch", {"after": cursor})
    try:
        # An open, unconsumed stream must not block a finite read.
        started = time.monotonic()
        client.call("read_trace", {"run_id": run_id})
        assert time.monotonic() - started < 2.0
        event = None
        data = None
        for _ in range(200):
            line = response.readline().decode("utf-8").strip()
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data = json.loads(line.split(":", 1)[1].strip())
            if event == "heartbeat" and data is not None:
                break
        assert event == "heartbeat", (event, data)
        assert data["run_id"] == run_id and "latest_seq" in data
    finally:
        response.close()


def test_mcp_finite_tools_match_the_http_projection(service):
    server, client, token, run_id = service
    worker, delivery = _seed_execution(server, run_id)
    http_trace = client.call("read_trace", {"run_id": run_id})
    http_execution = client.call("read_execution", {
        "run_id": run_id, "worker_id": worker["worker_id"],
        "epoch": worker["epoch"],
        "delivery_key": delivery["delivery_key"]})

    async def calls(session):
        return (
            await session.call_tool("read_trace", {"run_id": run_id}),
            await session.call_tool("read_execution", {
                "run_id": run_id, "worker_id": worker["worker_id"],
                "epoch": worker["epoch"],
                "delivery_key": delivery["delivery_key"]}),
            await session.call_tool("read_requests", {"limit": 50}),
        )

    mcp_trace, mcp_execution, mcp_requests = _mcp(server.endpoint, token, calls)
    for result in (mcp_trace, mcp_execution, mcp_requests):
        assert result.is_error is False, result.content
    assert mcp_trace.structured_content["result"] == http_trace
    assert mcp_execution.structured_content["result"] == http_execution
    assert mcp_execution.structured_content["result"]["entries"]
    diagnostics = mcp_requests.structured_content["result"]
    assert diagnostics["format"] == "agentgenome-trace-requests/1"
    assert token not in json.dumps(diagnostics)


def test_cli_trace_execution_and_requests_use_the_service(service, tmp_path):
    server, client, token, run_id = service
    worker, delivery = _seed_execution(server, run_id)
    with pytest.raises(Exception):
        client.call("set_run_intent", {
            "run_id": run_id, "desired": "paused",
            "expected_revision": -1, "request_id": "cli-rejected"})
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    base = [sys.executable, "-m", "node_runtime.control_plane.teacher_cli",
            "--data-dir", str(server.host.data_dir)]

    trace = subprocess.run(base + ["trace", run_id], cwd=str(tmp_path),
                           env=env, capture_output=True, text=True,
                           timeout=30)
    assert trace.returncode == 0, trace.stdout + trace.stderr
    assert json.loads(trace.stdout)["result"]["cursor"]

    events = tmp_path / "execution.jsonl"
    execution = subprocess.run(
        base + ["execution", run_id, worker["worker_id"],
                str(worker["epoch"]), delivery["delivery_key"],
                "--all", "--jsonl", str(events)],
        cwd=str(tmp_path), env=env, capture_output=True, text=True,
        timeout=30)
    assert execution.returncode == 0, execution.stdout + execution.stderr
    entries = [json.loads(line) for line in events.read_text().splitlines()]
    assert [entry["kind"] for entry in entries] == ["delivery", "tool"]

    requests_path = tmp_path / "requests.jsonl"
    diagnostics = subprocess.run(
        base + ["requests", "--run-id", run_id, "--limit", "1000",
                "--jsonl", str(requests_path)],
        cwd=str(tmp_path), env=env, capture_output=True, text=True,
        timeout=30)
    assert diagnostics.returncode == 0, (
        diagnostics.stdout + diagnostics.stderr)
    report = json.loads(diagnostics.stdout)["result"]
    records = [json.loads(line)
               for line in requests_path.read_text().splitlines()]
    assert records and report["records"] == records
    assert any(record["operation"] == "set_run_intent"
               for record in records)
    assert token not in json.dumps(records)


def test_production_serve_exits_with_an_open_idle_watch(tmp_path):
    from node_runtime.control_plane import discovery
    from node_runtime.control_plane.resources import ControlPlaneError

    data = tmp_path / "data"
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    process = subprocess.Popen(
        [sys.executable, "-m", "node_runtime.control_plane.host", "serve",
         "--data-dir", str(data), "--backend", "script",
         "--interval", "0.05", "--poll", "0"],
        cwd=str(tmp_path), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    response = None
    try:
        connection = None
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            assert process.poll() is None, process.stderr.read().decode()
            try:
                connection = discovery.connection(data)
                break
            except ControlPlaneError:
                time.sleep(0.05)
        assert connection is not None, "production service never became ready"
        client = ApiClient(connection.endpoint, connection.token)
        published = client.call("publish_definition", {
            "document": make_graph(definition_id="p3a-prod-stop"),
            "request_id": "p3a-prod-publish"})
        artifact = client.call("put_artifact", {
            "text": "[]", "request_id": "p3a-prod-input"})["artifact"]
        run = client.call("create_run", {
            "definition_ref": published["definition_ref"],
            "inputs": {"dataset": artifact},
            "request_id": "p3a-prod-run"})
        cursor = client.call("read_trace", {
            "run_id": run["run_id"]})["cursor"]["token"]
        response = urllib.request.build_opener(
            urllib.request.ProxyHandler({})).open(urllib.request.Request(
                connection.endpoint + f"/v1/runs/{run['run_id']}/watch?"
                + urlencode({"after": cursor}),
                headers={"Authorization": f"Bearer {connection.token}"}),
            timeout=5)
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=10) == 0, process.stderr.read().decode()
        record = json.loads((data / "service.json").read_text())
        assert record["stopped_at"]
    finally:
        if response is not None:
            response.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
