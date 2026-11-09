"""U03-host: native Pi/Codex/Qoder/Claude acceptance on one isolated service.

Every host process is launched with a temporary HOME, an explicit config root,
an explicit service data directory and an explicit cwd. Codex uses its official
app-server control protocol; Claude uses its real CLI with a controlled
Anthropic wire-format fixture that supplies tool_use requests only. Every
service value asserted here came back through the host's own MCP client.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from fixtures.client_hosts.anthropic_fixture import AnthropicFixture
from fixtures.client_hosts.graph import RUN_INPUTS, graph_document
from node_runtime.control_plane import client_install, pi_integration

ROOT = Path(__file__).resolve().parents[2]
HOST_FIXTURES = ROOT / "tests" / "control_plane" / "fixtures" / "client_hosts"
SCRIPT_AGENT = f"{HOST_FIXTURES / 'script_agent.py'}:factory"
TERMINAL = {"completed", "failed", "cancelled"}
CODEX_DEFINITION = "u03-host-codex-native"
CROSS_DEFINITION = "u03-host-cross-native"
EVIDENCE_PATH = os.environ.get("AGENTGENOME_HOST_EVIDENCE")
EVIDENCE: dict = {"hosts": {}}


def _record(host: str, **values) -> None:
    EVIDENCE["hosts"].setdefault(host, {}).update(values)


@pytest.fixture(scope="module", autouse=True)
def _write_evidence():
    yield
    if EVIDENCE_PATH:
        Path(EVIDENCE_PATH).write_text(
            json.dumps(EVIDENCE, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n", encoding="utf-8")


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    # Native CLIs change cwd, so every derived path must already be absolute.
    if not Path(tmp_path).is_absolute():
        raise ValueError("native host env requires an absolute base path")
    base = Path(tmp_path).resolve()
    env = dict(os.environ)
    for name in ("PI_CODING_AGENT_DIR", "CODEX_HOME", "CLAUDE_CONFIG_DIR",
                 "QODER_CONFIG_DIR", "ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY"):
        env.pop(name, None)
    home = base / "home"
    home.mkdir()
    env.update({
        "HOME": str(home),
        "XDG_STATE_HOME": str(base / "xdg-state"),
        "XDG_CONFIG_HOME": str(base / "xdg-config"),
        "XDG_CACHE_HOME": str(base / "xdg-cache"),
        "PYTHONPATH": str(ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    })
    env.update(extra)
    return env


def _run(command: list[str], *, env: dict[str, str], cwd: Path,
         timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(command, env=env, cwd=str(cwd), capture_output=True,
                          text=True, timeout=timeout)


def _version(executable: str, env: dict[str, str], cwd: Path) -> str:
    done = _run([executable, "--version"], env=env, cwd=cwd, timeout=60)
    text = (done.stdout or done.stderr).strip().splitlines()
    return text[0] if text else "unknown"


def _install(kind: str, root: Path, data: Path,
             env: dict[str, str]) -> dict:
    done = _run(
        [sys.executable, "-m", "node_runtime.control_plane.client_install",
         "install", "--client-kind", kind, "--config-root", str(root),
         "--data-dir", str(data)], env=env, cwd=ROOT)
    assert done.returncode == 0, done.stdout + done.stderr
    return json.loads(done.stdout)


def _start_service(data: Path, env: dict[str, str]) -> None:
    done = _run(
        [sys.executable, "-m", "node_runtime.control_plane.host", "start",
         "--backend", "script", "--data-dir", str(data),
         "--script-agent", SCRIPT_AGENT, "--interval", "0.01", "--poll", "0"],
        env=env, cwd=ROOT)
    assert done.returncode == 0, done.stdout + done.stderr


def _stop_service(data: Path, env: dict[str, str]) -> None:
    subprocess.run(
        [sys.executable, "-m", "node_runtime.control_plane.host", "stop",
         "--data-dir", str(data)], env=env, cwd=str(ROOT), capture_output=True,
        text=True, timeout=30)


@pytest.fixture(scope="module")
def service(tmp_path_factory):
    # One actual service for the whole module: every host must observe the same
    # service and catalog, not a look-alike service per test.
    base = tmp_path_factory.mktemp("hosts")
    env = _env(base)
    data = base / "service"
    _start_service(data, env)
    try:
        yield env, data
    finally:
        _stop_service(data, env)


def _canonical_service_id(data: Path) -> str:
    record = json.loads((data / "service.json").read_text(encoding="utf-8"))
    return record["service_id"]


def _walk(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _payload(response: dict) -> dict:
    result = response.get("result")
    assert isinstance(result, dict), response
    if isinstance(result.get("structuredContent"), dict):
        return result["structuredContent"]
    for block in result.get("content", []):
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            try:
                value = json.loads(block["text"])
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    raise AssertionError(f"no MCP payload in {json.dumps(response)[:1000]}")


class CodexAppServer:
    def __init__(self, env: dict[str, str], cwd: Path) -> None:
        self.process = subprocess.Popen(
            ["codex", "app-server", "--stdio"], env=env, cwd=str(cwd),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1)
        assert self.process.stdin and self.process.stdout and self.process.stderr
        self.records: "queue.Queue[dict]" = queue.Queue()
        self.stderr: list[str] = []
        self.next_id = 1
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()

    def _read(self) -> None:
        for line in self.process.stdout:
            try:
                self.records.put(json.loads(line))
            except ValueError:
                pass

    def _read_err(self) -> None:
        for line in self.process.stderr:
            self.stderr.append(line)

    def _send(self, record: dict) -> None:
        assert self.process.stdin
        self.process.stdin.write(json.dumps(record) + "\n")
        self.process.stdin.flush()

    def _response(self, request_id: int, timeout: float = 60) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                record = self.records.get(
                    timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                break
            if record.get("id") == request_id:
                return record
        raise AssertionError(
            f"no app-server response {request_id}: {''.join(self.stderr)[-2000:]}")

    def call(self, method: str, params: dict | None = None,
             timeout: float = 60) -> dict:
        request_id = self.next_id
        self.next_id += 1
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method,
                    "params": params or {}})
        return self._response(request_id, timeout)

    def notify(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def initialize(self) -> None:
        response = self.call("initialize", {
            "clientInfo": {"name": "u03-host", "version": "0"},
            "capabilities": {"experimentalApi": True}})
        assert "result" in response, response
        self.notify("initialized")

    def tool(self, thread_id: str, name: str, arguments: dict) -> dict:
        response = self.call("mcpServer/tool/call", {
            "server": "agentgenome", "threadId": thread_id,
            "tool": name, "arguments": arguments})
        return _payload(response)

    def close(self) -> None:
        try:
            self.process.stdin.close()  # type: ignore[union-attr]
        finally:
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()


def _codex_publish_run(env: dict[str, str], data: Path, base: Path,
                       tag: str, definition_id: str) -> dict:
    """Publish and start one run through the native Codex MCP client."""
    codex_home = base / "codex"
    project = base / "project"
    project.mkdir(parents=True)
    _install("codex", codex_home, data, env)
    child = dict(env)
    child["CODEX_HOME"] = str(codex_home)
    app = CodexAppServer(child, project)
    try:
        app.initialize()
        thread = app.call("thread/start", {"cwd": str(project),
                                           "ephemeral": True})
        thread_id = thread["result"]["thread"]["id"]
        info = app.tool(thread_id, "get_service_info", {})
        assert info["ok"] is True
        published = app.tool(thread_id, "publish_definition", {
            "request_id": f"{tag}-publish",
            "document": graph_document(definition_id)})
        assert published["ok"] is True
        assert published["result"]["definition_id"] == definition_id
        created = app.tool(thread_id, "create_run", {
            "definition_ref": published["result"]["definition_ref"],
            "inputs": RUN_INPUTS, "request_id": f"{tag}-run"})
        assert created["ok"] is True
        return {
            "service_id": info["result"]["service_id"],
            "definition_ref": published["result"]["definition_ref"],
            "definition_id": published["result"]["definition_id"],
            "run_id": created["result"]["run_id"],
        }
    finally:
        app.close()


def test_codex_app_server_discovers_and_calls_the_installed_service(
        tmp_path, service):
    env, data = service
    codex_home = tmp_path / "codex"
    project = tmp_path / "project"
    project.mkdir()
    installed = _install("codex", codex_home, data, env)
    assert Path(installed["native_path"]) == codex_home / "config.toml"
    skill = codex_home / "skills" / "agentgenome" / "SKILL.md"
    assert skill.is_file()

    child = dict(env)
    child["CODEX_HOME"] = str(codex_home)
    app = CodexAppServer(child, project)
    try:
        app.initialize()
        skills = app.call("skills/list", {"cwds": [str(project)],
                                          "forceReload": True})
        found = [item for item in _walk(skills)
                 if item.get("name") == "agentgenome"]
        assert found, json.dumps(skills)[:2000]
        assert any(str(item.get("path", "")).endswith("SKILL.md")
                   for item in found)

        thread = app.call("thread/start", {"cwd": str(project),
                                           "ephemeral": True})
        thread_id = thread["result"]["thread"]["id"]
        status = app.call("mcpServerStatus/list", {"threadId": thread_id})
        status_text = json.dumps(status)
        assert "agentgenome" in status_text, status_text[:2000]
        assert "get_service_info" in status_text, status_text[:2000]

        info = app.tool(thread_id, "get_service_info", {})
        assert info["ok"] is True
        service_id = info["result"]["service_id"]
        assert service_id == _canonical_service_id(data)

        catalog = app.tool(thread_id, "list_definitions", {})
        assert catalog["ok"] is True
        assert isinstance(catalog["result"]["items"], list)

        published = app.tool(thread_id, "publish_definition", {
            "request_id": "u03-host-codex-publish",
            "document": graph_document(CODEX_DEFINITION)})
        assert published["ok"] is True
        definition_ref = published["result"]["definition_ref"]
        assert published["result"]["definition_id"] == CODEX_DEFINITION

        catalog = app.tool(thread_id, "list_definitions", {})
        assert any(item["definition_ref"] == definition_ref
                   for item in catalog["result"]["items"])

        created = app.tool(thread_id, "create_run", {
            "definition_ref": definition_ref, "inputs": RUN_INPUTS,
            "request_id": "u03-host-codex-run"})
        assert created["ok"] is True
        run_id = created["result"]["run_id"]

        deadline = time.monotonic() + 60
        while True:
            trace = app.tool(thread_id, "read_trace", {"run_id": run_id})
            assert trace["ok"] is True
            if trace["result"]["run"]["status"] in TERMINAL:
                break
            assert time.monotonic() < deadline, "run did not finish"
            time.sleep(0.1)
        result = trace["result"]
        assert result["service_id"] == service_id
        assert result["run"]["status"] == "completed"
        assert result["tree"] and any(node["state"] in ("succeeded", "completed")
                                      for node in result["tree"])
        assert result["candidates"] and result["reviews"]

        other_cwd = tmp_path / "other-project"
        other_cwd.mkdir()
        other = app.call("thread/start", {"cwd": str(other_cwd),
                                          "ephemeral": True})
        other_id = other["result"]["thread"]["id"]
        same = app.tool(other_id, "get_service_info", {})
        assert same["result"]["service_id"] == service_id

        failed = app.call("mcpServer/tool/call", {
            "server": "agentgenome", "threadId": thread_id,
            "tool": "read_trace", "arguments": {"run_id": "missing-run"}})
        assert _payload(failed)["ok"] is False
        _record("codex", version=_version("codex", child, project),
                trigger="$agentgenome (or /skills)",
                native_path=str(installed["native_path"]),
                skill=str(skill), service_id=service_id,
                definition_ref=definition_ref, definition_id=CODEX_DEFINITION,
                run_id=run_id, run_status=result["run"]["status"],
                same_service_in_other_cwd=True, error_visible=True,
                service_survives_client_close=True)
    finally:
        app.close()

    service_dir = data
    record = json.loads((service_dir / "host.json").read_text(encoding="utf-8"))
    assert os.kill(int(record["pid"]), 0) is None


def test_claude_cli_runs_real_tool_requests_against_the_installed_service(
        tmp_path, service):
    env, data = service
    claude_home = tmp_path / "claude-config"
    project = tmp_path / "project"
    project.mkdir()
    installed = _install("claude", claude_home, data, env)
    assert Path(installed["native_path"]) == claude_home / ".claude.json"
    skill = claude_home / "skills" / "agentgenome" / "SKILL.md"
    assert skill.is_file()

    fixture = AnthropicFixture().start()
    try:
        child = dict(env)
        child["CLAUDE_CONFIG_DIR"] = str(claude_home)
        child["ANTHROPIC_BASE_URL"] = fixture.base_url
        child["ANTHROPIC_API_KEY"] = "u03-fixture-only-not-a-secret"
        child["DISABLE_TELEMETRY"] = "1"
        child["DISABLE_ERROR_REPORTING"] = "1"
        child["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        done = _run(
            ["claude", "--setting-sources", "user", "-p",
             "/agentgenome Use the installed AgentGenome MCP connection. "
             "Discover the service, publish the controlled Graph, create a run, "
             "and read its trace until it finishes.",
             "--model", "claude-sonnet-4-6", "--no-session-persistence",
             "--output-format", "json", "--permission-mode",
             "bypassPermissions", "--allow-dangerously-skip-permissions"],
            env=child, cwd=project, timeout=180)
    finally:
        fixture.stop()

    assert not fixture.errors, fixture.errors
    assert done.returncode == 0, done.stdout + done.stderr
    assert fixture.requests and fixture.requests[0]["skill_loaded"], (
        fixture.requests[:2])
    names = fixture.tool_sequences
    for operation in ("get_service_info", "list_definitions",
                      "publish_definition", "create_run", "read_trace"):
        assert any(name == operation or name.endswith(f"__{operation}")
                   for name in names), names

    info = next(item["result"] for item in fixture.observations
                if item["tool"] == "get_service_info")
    published = next(item["result"] for item in fixture.observations
                     if item["tool"] == "publish_definition")
    created = next(item["result"] for item in fixture.observations
                   if item["tool"] == "create_run")
    traces = [item["result"] for item in fixture.observations
              if item["tool"] == "read_trace"]
    assert info["service_id"] == _canonical_service_id(data)
    assert published["definition_id"] == fixture.definition_id
    assert created["run_id"]
    assert traces and traces[-1]["run"]["status"] == "completed"
    assert traces[-1]["service_id"] == info["service_id"]
    assert any(node["state"] in ("succeeded", "completed")
               for node in traces[-1]["tree"])
    assert traces[-1]["candidates"] and traces[-1]["reviews"]
    _record("claude", version=_version("claude", child, project),
            trigger="installed agentgenome skill",
            native_path=str(installed["native_path"]), skill=str(skill),
            service_id=info["service_id"],
            definition_ref=published["definition_ref"],
            definition_id=fixture.definition_id, run_id=created["run_id"],
            run_status=traces[-1]["run"]["status"],
            tool_sequence=list(fixture.tool_sequences),
            model_source="controlled Anthropic tool_use requests; real MCP results")


def test_qoder_cli_discovers_the_installed_entry_and_skill_only(
        tmp_path, service):
    # Discovery-only: Qoder has no supported offline/provider override, so the
    # model loop that would call the MCP tools is not exercised here.
    env, data = service
    qoder_home = tmp_path / "qoder-config"
    project = tmp_path / "project"
    project.mkdir()
    installed = _install("qoder", qoder_home, data, env)
    assert Path(installed["native_path"]) == qoder_home / "settings.json"
    assert (qoder_home / "skills" / "agentgenome" / "SKILL.md").is_file()

    listed = _run(["qodercli", "--config-dir", str(qoder_home), "mcp", "list"],
                  env=env, cwd=project)
    assert listed.returncode == 0, listed.stdout + listed.stderr
    assert "agentgenome" in listed.stdout

    detail = _run(["qodercli", "--config-dir", str(qoder_home), "mcp", "get",
                   "agentgenome"], env=env, cwd=project)
    assert detail.returncode == 0, detail.stdout + detail.stderr
    assert "agentgenome" in detail.stdout
    assert "client_mcp" in detail.stdout

    skills = _run(["qodercli", "--config-dir", str(qoder_home), "skills",
                   "list"], env=env, cwd=project)
    assert skills.returncode == 0, skills.stdout + skills.stderr
    assert "agentgenome" in skills.stdout
    _record("qoder", version=_version("qodercli", env, project),
            trigger="discovered MCP entry + agentgenome skill",
            native_path=str(installed["native_path"]),
            skill=str(qoder_home / "skills" / "agentgenome" / "SKILL.md"),
            service_id=_canonical_service_id(data),
            discovery_only=True,
            limitation=("not logged in; no supported base-URL/custom-endpoint "
                        "override; BYOK is wizard-only; model-driven tool call "
                        "not verified; QODER_PERSONAL_ACCESS_TOKEN absent"))


def test_qoder_env_root_is_honoured_by_the_installed_cli(tmp_path, service):
    env, data = service
    qoder_root = tmp_path / "qoder-env"
    project = tmp_path / "project"
    project.mkdir()
    installed = _install("qoder", qoder_root, data, env)
    child = {**env, "QODER_CONFIG_DIR": str(qoder_root)}
    listed = _run(["qodercli", "mcp", "list"], env=child, cwd=project)
    assert listed.returncode == 0, listed.stdout + listed.stderr
    assert "agentgenome" in listed.stdout
    assert str(data) in listed.stdout
    detail = _run(["qodercli", "mcp", "get", "agentgenome"], env=child,
                  cwd=project)
    assert detail.returncode == 0, detail.stdout + detail.stderr
    assert "agentgenome" in detail.stdout
    skills = _run(["qodercli", "skills", "list"], env=child, cwd=project)
    assert skills.returncode == 0, skills.stdout + skills.stderr
    assert "agentgenome" in skills.stdout
    assert str(qoder_root / "skills" / "agentgenome" / "SKILL.md") in skills.stdout
    _record("qoder_env_root", version=_version("qodercli", child, project),
            native_path=str(installed["native_path"]),
            activation_root=str(qoder_root / "skills"),
            override="QODER_CONFIG_DIR", discovery_only=True)


RPC_PROBE = """\
import { writeFileSync } from "node:fs";
export default function (pi) {
  pi.registerCommand("agentgenome-probe", {
    description: "write the current tool registry",
    handler: async (args, ctx) => {
      writeFileSync(process.env.AGENTGENOME_PROBE_OUT, JSON.stringify({
        all: pi.getAllTools().map((tool) => tool.name),
        active: pi.getActiveTools(),
        mode: ctx.mode,
        cwd: ctx.cwd,
      }));
    },
  });
}
"""


class PiRpc:
    """One native Pi RPC process; probes and prompts enter through stdin."""

    def __init__(self, env: dict[str, str], cwd: Path, probe_out: Path,
                 *extra: str) -> None:
        self.probe_out = probe_out
        self.process = subprocess.Popen(
            ["pi", "--mode", "rpc", "--no-session", "--offline",
             "--no-context-files", "--no-skills", "--no-prompt-templates",
             "--no-themes", *extra],
            cwd=str(cwd), env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            bufsize=1)
        assert self.process.stdin and self.process.stdout and self.process.stderr
        self.records: "queue.Queue[dict]" = queue.Queue()
        self.pending: list[dict] = []
        self.stderr_lines: list[str] = []
        threading.Thread(target=lambda: [
            self.records.put(json.loads(line)) for line in self.process.stdout
            if line.strip()], daemon=True).start()
        threading.Thread(target=lambda: [
            self.stderr_lines.append(line) for line in self.process.stderr],
            daemon=True).start()

    def send(self, record: dict) -> None:
        assert self.process.stdin
        self.process.stdin.write(json.dumps(record) + "\n")
        self.process.stdin.flush()

    def response(self, request_id: str, timeout: float = 90) -> dict:
        deadline = time.monotonic() + timeout
        for index, record in enumerate(self.pending):
            if (record.get("type") == "response"
                    and record.get("id") == request_id):
                return self.pending.pop(index)
        while time.monotonic() < deadline:
            try:
                record = self.records.get(
                    timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                break
            if (record.get("type") == "response"
                    and record.get("id") == request_id):
                return record
            # Session events may precede a response; keep them for waiters.
            self.pending.append(record)
        raise AssertionError(
            f"no Pi response {request_id}: "
            f"{''.join(self.stderr_lines)[-2000:]}")

    def wait_settled(self, timeout: float = 180) -> dict:
        for index, record in enumerate(self.pending):
            if record.get("type") == "agent_settled":
                return self.pending.pop(index)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                record = self.records.get(
                    timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                break
            if record.get("type") == "agent_settled":
                return record
            self.pending.append(record)
        raise AssertionError(
            f"Pi never settled: {''.join(self.stderr_lines)[-3000:]}")

    def prompt(self, request_id: str, message: str,
               timeout: float = 90) -> dict:
        self.send({"id": request_id, "type": "prompt", "message": message})
        return self.response(request_id, timeout)

    def probe(self, request_id: str) -> dict:
        self.probe_out.unlink(missing_ok=True)
        assert self.prompt(request_id, "/agentgenome-probe")["success"] is True
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.probe_out.is_file():
                return json.loads(self.probe_out.read_text(encoding="utf-8"))
            time.sleep(0.05)
        raise AssertionError("Pi probe did not write the registry")

    def wait_active(self, timeout: float = 60) -> dict:
        deadline = time.monotonic() + timeout
        counter = 0
        while True:
            counter += 1
            snapshot = self.probe(f"probe-{counter}")
            if "validate_definition" in snapshot["active"]:
                return snapshot
            assert time.monotonic() < deadline, snapshot
            time.sleep(0.2)

    def close(self) -> None:
        assert self.process.stdin
        self.process.stdin.close()
        try:
            self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.process.kill()


def _pi_agent(agent: Path, data: Path, probe_out: Path) -> dict:
    installed = pi_integration.install(agent_dir=agent, data_dir=data)
    assert Path(installed["extension"]).is_file()
    extensions = agent / "extensions"
    extensions.mkdir(parents=True, exist_ok=True)
    (extensions / "zz-probe.js").write_text(RPC_PROBE, encoding="utf-8")
    return installed


def test_pi_native_activation_registers_the_installed_tools(tmp_path, service):
    env, data = service
    agent = tmp_path / "agent"
    project = tmp_path / "project"
    project.mkdir()
    probe_out = tmp_path / "probe.json"
    installed = _pi_agent(agent, data, probe_out)

    child = dict(env)
    child.update({"PI_CODING_AGENT_DIR": str(agent),
                  "AGENTGENOME_PROBE_OUT": str(probe_out)})
    pi = PiRpc(child, project, probe_out)
    try:
        pi.send({"id": "commands", "type": "get_commands"})
        commands = pi.response("commands")
        names = [item["name"] for item in commands["data"]["commands"]]
        assert "agentgenome" in names
        assert "agentgenome-probe" in names

        assert pi.prompt("activate", "/agentgenome")["success"] is True
        snapshot = pi.wait_active()
        assert "read_trace" in snapshot["all"]
        assert "read_trace" in snapshot["active"]
        assert snapshot["mode"] == "rpc"
        assert Path(snapshot["cwd"]).resolve() == project.resolve()
        active_tools = list(snapshot["active"])
        all_tools = list(snapshot["all"])

        assert pi.prompt("trace", "/agentgenome trace")["success"] is True

        assert pi.prompt("off", "/agentgenome off")["success"] is True
        snapshot = pi.probe("probe-off")
        assert "validate_definition" not in snapshot["active"]
        assert "read" in snapshot["active"]
        _record("pi_activation", version=_version("pi", child, project),
                command="/agentgenome", extension=str(installed["extension"]),
                tools=all_tools, active_after_activation=active_tools,
                trace_panel="TUI-only; RPC smoke via /agentgenome trace")
    finally:
        pi.close()

    record = json.loads((data / "host.json").read_text(encoding="utf-8"))
    assert os.kill(int(record["pid"]), 0) is None


def test_pi_native_tools_read_the_shared_catalog_and_a_codex_run(
        tmp_path, service):
    # Cross-host proof: Codex creates the definition and run through its native
    # MCP client; the native Pi model loop then calls its own registered tools
    # and must return the same service, definition and run values.
    env, data = service
    shared = _codex_publish_run(env, data, tmp_path / "codex-shared",
                                "u03-cross", CROSS_DEFINITION)
    agent = tmp_path / "agent"
    project = tmp_path / "project"
    project.mkdir()
    probe_out = tmp_path / "probe.json"
    installed = _pi_agent(agent, data, probe_out)

    fixture = AnthropicFixture(observe_run_id=shared["run_id"]).start()
    try:
        (agent / "models.json").write_text(json.dumps({
            "providers": {"anthropic": {
                "baseUrl": fixture.base_url,
                "apiKey": "u03-fixture-only-not-a-secret"}}}),
            encoding="utf-8")
        child = dict(env)
        child.update({"PI_CODING_AGENT_DIR": str(agent),
                      "AGENTGENOME_PROBE_OUT": str(probe_out)})
        pi = PiRpc(child, project, probe_out, "--provider", "anthropic",
                   "--model", "claude-sonnet-4-6")
        try:
            assert pi.prompt("activate", "/agentgenome")["success"] is True
            pi.wait_active()
            answer = pi.prompt(
                "observe",
                "Use the installed AgentGenome tools to report the service, "
                "list the catalog, and read the requested run.", timeout=180)
            assert answer["success"] is True, answer
            pi.wait_settled(timeout=180)
        finally:
            pi.close()
    finally:
        fixture.stop()

    assert not fixture.errors, fixture.errors
    assert "get_service_info" in fixture.tool_sequences
    assert "list_definitions" in fixture.tool_sequences
    assert "read_trace" in fixture.tool_sequences

    info = next(item["result"] for item in fixture.observations
                if item["tool"] == "get_service_info")
    catalog = next(item["result"] for item in fixture.observations
                   if item["tool"] == "list_definitions")
    traces = [item["result"] for item in fixture.observations
              if item["tool"] == "read_trace"]
    assert info["service_id"] == _canonical_service_id(data)
    assert info["service_id"] == shared["service_id"]
    assert any(item["definition_ref"] == shared["definition_ref"]
               for item in catalog["items"])
    assert traces and traces[-1]["service_id"] == shared["service_id"]
    assert traces[-1]["run"]["run_id"] == shared["run_id"]
    assert traces[-1]["run"]["status"] == "completed"
    assert any(node["state"] in ("succeeded", "completed")
               for node in traces[-1]["tree"])
    assert traces[-1]["candidates"] and traces[-1]["reviews"]
    _record("pi_native_tools", version=_version("pi", child, project),
            command="/agentgenome", extension=str(installed["extension"]),
            service_id=info["service_id"],
            definition_ref=shared["definition_ref"],
            definition_id=CROSS_DEFINITION, run_id=traces[-1]["run"]["run_id"],
            codex_service_id=shared["service_id"],
            codex_definition_ref=shared["definition_ref"],
            codex_run_id=shared["run_id"],
            run_status=traces[-1]["run"]["status"],
            tool_sequence=list(fixture.tool_sequences),
            model_source="controlled Anthropic tool_use requests; real MCP results",
            shared_service_with="codex")

    record = json.loads((data / "host.json").read_text(encoding="utf-8"))
    assert os.kill(int(record["pid"]), 0) is None
