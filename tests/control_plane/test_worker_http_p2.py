"""P2 Worker network path: delivery token, exact bytes, candidate semantics.

The client is the real ``pi_node_tools.mjs`` extension in every test here; the
service is a real listening ApiServer. No model, no installed service and no
client database are touched. The service reads no caller filesystem path, so
the binding workspace is deliberately absent.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import pytest

ROOT = Path(__file__).resolve().parents[2]
NODE = os.environ.get("AGENTGENOME_TEST_NODE", "node")
WORKER_MODULE = ROOT / "node_runtime/control_plane/backends/pi_node_tools.mjs"
sys.path.insert(0, str(ROOT / "tests/control_plane"))

from conftest import make_graph  # noqa: E402
from node_runtime.control_plane.client import ApiClient  # noqa: E402
from node_runtime.control_plane.host import Host  # noqa: E402
from node_runtime.control_plane.server import ApiServer  # noqa: E402


def _post(endpoint: str, path: str, token: str, payload: dict) -> tuple[int, dict]:
    request = Request(
        endpoint + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        with error:
            return error.code, json.loads(error.read().decode("utf-8"))


class _Service:
    def __init__(self, tmp_path: Path):
        self.host = Host(tmp_path / "service", backend="script",
                         scheduler_interval=0.02, poll_seconds=0.0)
        # Keep the control loop idle: this test drives only the Scheduler, so
        # the Worker stays current while the HTTP endpoint is exercised.
        self.host.reconcile_once = lambda: {"actions": []}
        self.server = ApiServer(self.host, port=0, heartbeat_timeout=5.0)
        self.server.start_background()
        publisher = self.server.auth.publisher_credentials()
        self.publisher = publisher
        self.client = ApiClient(self.server.endpoint, publisher["token"])
        document = make_graph(definition_id="worker-http-p2")
        definition = self.client.call(
            "publish_definition",
            {"request_id": "publish", "document": document})
        data = self.client.call("put_artifact", {
            "request_id": "input",
            "base64": base64.b64encode(b"[]").decode("ascii"),
            "media_type": "text/plain"})["artifact"]
        self.run_id = self.client.call("create_run", {
            "request_id": "run", "definition_ref": definition["definition_ref"],
            "inputs": {"dataset": data}})["run_id"]
        for _ in range(3):
            self.host.scheduler.reconcile(self.run_id)
        resources = self.client.call("list_resources", {"run_id": self.run_id})
        self.worker = resources["workers"][0]
        self.node = next(item for item in resources["nodes"]
                         if item["node_id"] == self.worker["node_id"])
        self.binding = {
            "worker_id": self.worker["worker_id"],
            "run_id": self.worker["run_id"],
            "node_id": self.worker["node_id"],
            "epoch": self.worker["epoch"],
            "delivery_key": "p2-delivery",
            "workspace": str(tmp_path / "absent-at-server"),
            "contract_ref": self.node["contract_ref"],
        }
        self.delivery = self.server.auth.issue_delivery(self.binding)

    def close(self) -> None:
        self.server.stop()

    def worker_call(self, request: dict, *, token: str | None = None):
        return _post(self.server.endpoint, "/v1/worker/tool",
                     token or self.delivery["token"], request)


@pytest.fixture
def service(tmp_path):
    instance = _Service(tmp_path)
    try:
        yield instance
    finally:
        instance.close()


def _client_upload(service, tmp_path: Path, data: bytes) -> dict:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "result.bin").write_bytes(data)
    context = tmp_path / "context.json"
    context.write_text(json.dumps({
        "format": "agentgenome-worker-tool-context/2",
        "endpoint": service.server.endpoint,
        "token": service.delivery["token"],
        **{key: service.binding[key] for key in
           ("run_id", "node_id", "worker_id", "epoch", "delivery_key")},
        "workspace": str(workspace),
        "tools": [{"name": "node.publish_artifact",
                   "parameters": {"type": "object"}}],
    }), encoding="utf-8")
    probe = tmp_path / "probe.mjs"
    probe.write_text(
        "const { default: register } = await import(process.argv[2]);\n"
        "const tools = [];\n"
        "register({ on() {}, registerTool(tool) { tools.push(tool); } });\n"
        "const result = await tools[0].execute('call-1', "
        "{path: process.argv[3], media_type: 'application/octet-stream'});\n"
        "console.log(JSON.stringify(result));\n", encoding="utf-8")
    completed = subprocess.run(
        [NODE, str(probe), str(WORKER_MODULE), "result.bin"],
        capture_output=True, text=True, timeout=20,
        env={**os.environ, "AGENTGENOME_TOOL_CONTEXT": str(context)})
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_worker_client_uploads_exact_bytes_and_submits_a_candidate(
        service, tmp_path):
    data = b"\x00\xff\xfe\x80 p2 worker bytes\n"
    uploaded = _client_upload(service, tmp_path, data)
    assert not uploaded.get("isError"), uploaded
    artifact = uploaded["details"]
    assert artifact["size"] == len(data)
    read = service.client.call("read_artifact", {
        key: artifact[key] for key in ("id", "sha256", "size", "media_type")})
    assert base64.b64decode(read["base64"]) == data
    assert not Path(service.binding["workspace"]).exists()

    status, submitted = service.worker_call({
        "call_id": "candidate-1", "tool": "node.submit_candidate",
        "arguments": {"candidate_id": "candidate-1", "round": 1,
                      "output_refs": [artifact]}})
    assert status == 200 and submitted["ok"] is True, submitted
    again = service.worker_call({
        "call_id": "candidate-1", "tool": "node.submit_candidate",
        "arguments": {"candidate_id": "candidate-1", "round": 1,
                      "output_refs": [artifact]}})
    assert again[1]["result"]["output"] == submitted["result"]["output"]
    resources = service.client.call("list_resources",
                                    {"run_id": service.run_id})
    candidates = [item for item in resources["candidates"]
                  if item["candidate_id"] == "candidate-1"]
    assert len(candidates) == 1
    assert candidates[0]["worker_id"] == service.worker["worker_id"]
    assert candidates[0]["epoch"] == service.worker["epoch"]
    assert candidates[0]["contract_ref"] == service.node["contract_ref"]


def test_worker_endpoint_refuses_wrong_identity_bindings(service):
    request = {"call_id": "one", "tool": "node.publish_artifact",
               "arguments": {"path": "x"}, "content_base64": "eA=="}
    publisher = service.publisher
    status, refused = _post(service.server.endpoint, "/v1/worker/tool",
                            publisher["token"], request)
    assert status == 401 and refused["ok"] is False
    status, refused = _post(
        service.server.endpoint, "/v1/operations/list_runs",
        service.delivery["token"], {})
    assert status == 401 and refused["ok"] is False

    stale = service.server.auth.issue_delivery(
        {**service.binding, "epoch": service.binding["epoch"] + 1})
    _, refused = service.worker_call(request, token=stale["token"])
    assert refused["error"]["code"] == "stale_epoch", refused
    wrong_run = service.server.auth.issue_delivery(
        {**service.binding, "run_id": "another-run"})
    _, refused = service.worker_call(request, token=wrong_run["token"])
    assert refused["error"]["code"] in {"unauthorized", "not_found"}, refused
    wrong_node = service.server.auth.issue_delivery(
        {**service.binding, "node_id": "another-node"})
    _, refused = service.worker_call(request, token=wrong_node["token"])
    assert refused["error"]["code"] == "unauthorized", refused

    # No tool, review, decision or identity authority can be added through the
    # model-supplied arguments; these live only in the trusted delivery.
    _, refused = service.worker_call({
        "call_id": "forged", "tool": "node.submit_review",
        "arguments": {"candidate_id": "invented", "candidate_round": 1,
                      "verdict": "accept",
                      "feedback_ref": {"text": "forged"}}})
    assert refused["ok"] is False and refused["error"]["code"] == "unauthorized"
    _, refused = service.worker_call({
        "call_id": "no-bytes", "tool": "node.publish_artifact",
        "arguments": {"path": "x"}})
    assert refused["error"]["code"] == "invalid_arguments"


def test_worker_path_has_no_python_bridge_or_client_database():
    worker = WORKER_MODULE.read_text(encoding="utf-8")
    for forbidden in ("child_process", "spawnSync", "spawn(", "tool_bridge"):
        assert forbidden not in worker
    api = (ROOT / "node_runtime/control_plane/worker_api.py").read_text(
        encoding="utf-8")
    assert "open(" not in api and "read_bytes" not in api
    client = (ROOT / "node_runtime/control_plane/client.py").read_text(
        encoding="utf-8")
    for forbidden in ("sqlite", "from .api", "from .storage", "import api"):
        assert forbidden not in client
    assert "tool_bridge" not in (
        ROOT / "node_runtime/control_plane/backends/pi.py").read_text(
        encoding="utf-8")
