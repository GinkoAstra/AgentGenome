"""Focused tests for the ordinary-Pi /agentgenome integration layer.

These exercise only install/uninstall/prepare and the generated bootstrap.
Runtime semantics (Api/Scheduler/Controller) are covered elsewhere and are not
re-implemented here. prepare only discovers an already running service: every
test starts that service explicitly with the isolated `script` backend and no
model is called.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from node_runtime.control_plane import host as host_module
from node_runtime.control_plane import pi_integration
from node_runtime.control_plane.client_access import ServiceUnavailableError
from node_runtime.control_plane.http_api import PUBLIC_OPERATIONS
from node_runtime.control_plane.resources import ControlPlaneError

ROOT = Path(__file__).resolve().parents[2]
PROVIDER = "llama-local"
MODEL = "qwen3.5-2b"


def _quiet_stop(data_dir: Path) -> None:
    with contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        host_module.main(["stop", "--data-dir", str(data_dir)])


def _start_service(data_dir: Path | None = None) -> None:
    argv = ["start", "--backend", "script"]
    if data_dir is not None:
        argv += ["--data-dir", str(data_dir)]
    with contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        code = host_module.main(argv)
    assert code == 0


@pytest.fixture
def data_dir(tmp_path):
    root = tmp_path / "data"
    yield root
    _quiet_stop(root)


def test_install_is_idempotent_and_owns_only_its_bootstrap(tmp_path):
    agent = tmp_path / "agent"
    target = agent / "extensions" / "agentgenome.js"

    created = pi_integration.install(agent_dir=agent)
    assert created["state"] == "create"
    assert created["ignored_legacy"] == []
    assert target.is_file()
    text = target.read_text(encoding="utf-8")
    assert pi_integration.OWNER_MARKER in text
    assert str(pi_integration.PI_COMMAND.resolve()) in text
    assert json.dumps(os.path.abspath(sys.executable)) in text
    # A client install binds only the interpreter and, optionally, an isolated
    # data directory: no Worker model, endpoint, port or credential.
    assert PROVIDER not in text and MODEL not in text
    assert "Bearer" not in text and '"token"' not in text
    assert '"provider"' not in text and '"model"' not in text
    assert '"@earendil-works/pi-tui"' in text
    for helper in ("truncateToWidth", "visibleWidth", "wrapTextWithAnsi"):
        assert helper in text
    assert "helpers:" in text

    again = pi_integration.install(agent_dir=agent)
    assert again["state"] == "unchanged"
    assert target.read_text(encoding="utf-8") == text

    # Legacy provider/model inputs are accepted for compatibility but ignored;
    # they never change the generated client entry.
    legacy = pi_integration.install(
        provider=PROVIDER, model=MODEL, agent_dir=agent)
    assert legacy["state"] == "unchanged"
    assert legacy["ignored_legacy"] == ["provider", "model"]
    assert target.read_text(encoding="utf-8") == text

    # Settings and credentials are never part of the owned file.
    assert not (agent / "settings.json").exists()
    assert not (agent / "auth.json").exists()


def test_install_preview_is_pure_and_dry_run_writes_nothing(tmp_path):
    agent = tmp_path / "agent"

    plan = pi_integration.plan_install(agent_dir=agent)
    assert set(plan) == {"target", "content", "state", "previous_sha256"}
    assert plan["state"] == "create"
    assert plan["previous_sha256"] is None
    assert not agent.exists()

    preview = pi_integration.install(agent_dir=agent, dry_run=True)
    assert preview["state"] == "create"
    assert preview["dry_run"] is True
    assert not agent.exists()


def test_install_refuses_foreign_modified_and_legacy_files(tmp_path):
    foreign = tmp_path / "foreign"
    foreign_target = foreign / "extensions" / "agentgenome.js"
    foreign_target.parent.mkdir(parents=True)
    foreign_target.write_text("export default () => {};\n", encoding="utf-8")
    with pytest.raises(ControlPlaneError) as refused:
        pi_integration.install(agent_dir=foreign)
    assert refused.value.code == "invalid_argument"
    assert "refusing to overwrite" in refused.value.message
    assert foreign_target.read_text(encoding="utf-8") == "export default () => {};\n"

    owned = tmp_path / "owned"
    pi_integration.install(agent_dir=owned)
    owned_target = owned / "extensions" / "agentgenome.js"
    original = owned_target.read_text(encoding="utf-8")
    owned_target.write_text(original + "// user edit\n", encoding="utf-8")
    with pytest.raises(ControlPlaneError) as modified:
        pi_integration.install(agent_dir=owned)
    assert modified.value.code == "invalid_argument"
    assert owned_target.read_text(encoding="utf-8") == original + "// user edit\n"

    # A legacy file that only carries the marker is not overwrite authority.
    legacy = tmp_path / "legacy"
    legacy_target = legacy / "extensions" / "agentgenome.js"
    legacy_target.parent.mkdir(parents=True)
    legacy_target.write_text(
        f"// {pi_integration.OWNER_MARKER}\nlegacy body\n", encoding="utf-8")
    with pytest.raises(ControlPlaneError) as refused_legacy:
        pi_integration.install(agent_dir=legacy)
    assert refused_legacy.value.code == "invalid_argument"
    assert legacy_target.read_text(encoding="utf-8").endswith("legacy body\n")


def test_uninstall_removes_only_the_owned_bootstrap(tmp_path):
    agent = tmp_path / "agent"
    pi_integration.install(agent_dir=agent)
    target = agent / "extensions" / "agentgenome.js"

    removed = pi_integration.uninstall(agent_dir=agent)
    assert removed["removed"] is True
    assert not target.exists()

    again = pi_integration.uninstall(agent_dir=agent)
    assert again["removed"] is False
    assert again["reason"] == "not_installed"

    foreign = tmp_path / "foreign"
    foreign_target = foreign / "extensions" / "agentgenome.js"
    foreign_target.parent.mkdir(parents=True)
    foreign_target.write_text("module.exports = 1;\n", encoding="utf-8")
    with pytest.raises(ControlPlaneError) as refused:
        pi_integration.uninstall(agent_dir=foreign)
    assert refused.value.code == "invalid_argument"
    assert foreign_target.exists()

    modified = tmp_path / "modified"
    pi_integration.install(agent_dir=modified)
    modified_target = modified / "extensions" / "agentgenome.js"
    modified_target.write_text(
        modified_target.read_text(encoding="utf-8") + "// user edit\n",
        encoding="utf-8")
    with pytest.raises(ControlPlaneError) as refused_modified:
        pi_integration.uninstall(agent_dir=modified)
    assert refused_modified.value.code == "invalid_argument"
    assert modified_target.exists()


def test_install_binds_only_the_optional_data_dir(tmp_path):
    agent = tmp_path / "agent"
    isolated = tmp_path / "isolated-data"

    first = pi_integration.install(agent_dir=agent, data_dir=isolated)
    assert first["state"] == "create"
    text = (agent / "extensions" / "agentgenome.js").read_text(encoding="utf-8")
    assert json.dumps(str(isolated.resolve())) in text
    assert '"data_dir"' in text

    second = pi_integration.install(agent_dir=agent)
    assert second["state"] == "update"
    text = (agent / "extensions" / "agentgenome.js").read_text(encoding="utf-8")
    assert '"data_dir"' not in text


def test_install_honours_PI_CODING_AGENT_DIR(tmp_path, monkeypatch):
    agent = tmp_path / "env-agent"
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent))
    result = pi_integration.install()
    assert Path(result["agent_dir"]) == agent.resolve()
    assert (agent / "extensions" / "agentgenome.js").is_file()


def _python_without_mcp(script: str) -> subprocess.CompletedProcess:
    blocker = (
        "import sys\n"
        "class _NoMCP:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'mcp' or name.startswith('mcp.'):\n"
        "            raise ImportError('mcp is unavailable in this process')\n"
        "        return None\n"
        "sys.meta_path.insert(0, _NoMCP())\n"
    )
    environment = {**os.environ, "PYTHONPATH": str(ROOT),
                   "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run([sys.executable, "-c", blocker + script],
                          cwd=str(ROOT), env=environment,
                          capture_output=True, text=True, timeout=60)


def test_install_and_teacher_import_do_not_load_the_mcp_sdk(tmp_path):
    agent = tmp_path / "agent"
    script = (
        "import json\n"
        "from node_runtime.control_plane import pi_integration, teacher_entry\n"
        f"result = pi_integration.install(agent_dir={str(agent)!r})\n"
        "loaded = sorted(name for name in sys.modules\n"
        "                if name == 'mcp' or name.startswith('mcp.'))\n"
        "print(json.dumps({'state': result['state'],\n"
        "                  'tools': len(teacher_entry.teacher_tools()),\n"
        "                  'mcp_modules': loaded}))\n"
    )
    done = _python_without_mcp(script)
    assert done.returncode == 0, done.stdout + done.stderr
    assert json.loads(done.stdout) == {
        "state": "create",
        "tools": 4 + len(PUBLIC_OPERATIONS),
        "mcp_modules": [],
    }
    assert (agent / "extensions" / "agentgenome.js").is_file()


def test_prepare_without_a_service_is_typed_and_writes_nothing(tmp_path):
    data = tmp_path / "data"
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(ServiceUnavailableError) as missing:
        pi_integration.prepare_session(data_dir=data, workspace=workspace)
    assert missing.value.code == "service_unavailable"
    assert not data.exists()
    assert list(workspace.iterdir()) == []


def test_prepare_discovers_the_running_service_without_writing(
        data_dir, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _start_service(data_dir)

    prepared = pi_integration.prepare_session(
        data_dir=data_dir, workspace=workspace)
    assert set(prepared) == {"prompt", "workspace", "data_dir", "endpoint",
                             "service_id", "instance_id", "mcp_config",
                             "host_status", "tool_names"}
    assert prepared["workspace"] == str(workspace.resolve())
    assert prepared["data_dir"] == str(data_dir.resolve())
    assert prepared["endpoint"].startswith("http://127.0.0.1:")
    assert prepared["service_id"]
    assert prepared["host_status"]["running"] is True
    assert prepared["tool_names"] == list(PUBLIC_OPERATIONS)

    config = json.loads(prepared["mcp_config"])
    assert config["format"] == "agentgenome-teacher-mcp/2"
    assert config["endpoint"] == prepared["endpoint"]
    assert config["workspace"] == str(workspace.resolve())
    assert config["token"] and "command" not in config and "args" not in config

    # The credential stays inside the in-memory client config; it never enters
    # model-visible prompt text or status output.
    assert config["token"] not in prepared["prompt"]
    assert config["token"] not in json.dumps(prepared["host_status"])

    # prepare is discovery-only: neither the workspace nor the service data
    # directory gains a file, and no Teacher directory is created.
    assert list(workspace.iterdir()) == []
    assert not (data_dir / "teacher").exists()

    prompt = prepared["prompt"]
    assert str(data_dir.resolve()) in prompt
    assert str(workspace.resolve()) in prompt
    assert "斜杠入口环境" in prompt
    assert "Graph SOP 通用协议参考（内嵌）" in prompt
    assert "Graph SOP 模板（内嵌）" in prompt
    assert "agentgenome-graph-definition/2" in prompt
    # No automatic start or Host configuration matching commands.
    assert "node_runtime.control_plane.host" not in prompt
    assert "agentgenome start" not in prompt
    assert "host_config_mismatch" not in prompt
    assert "本入口只连接已经运行的 AgentGenome 服务" in prompt


def test_prepare_ignores_legacy_provider_and_model(data_dir, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _start_service(data_dir)

    plain = pi_integration.prepare_session(
        data_dir=data_dir, workspace=workspace)
    legacy = pi_integration.prepare_session(
        data_dir=data_dir, workspace=workspace,
        provider=PROVIDER, model=MODEL)
    assert legacy["prompt"] == plain["prompt"]
    assert legacy["mcp_config"] == plain["mcp_config"]
    assert legacy["service_id"] == plain["service_id"]


def test_default_service_is_shared_across_cwds_and_explicit_dir_is_separate(
        tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    one = tmp_path / "one"
    two = tmp_path / "two"
    one.mkdir()
    two.mkdir()
    default = state / "agentgenome" / "default"
    explicit_dir = tmp_path / "explicit"
    try:
        _start_service()
        first = pi_integration.prepare_session(workspace=one)
        second = pi_integration.prepare_session(workspace=two)
        assert Path(first["data_dir"]) == default.resolve()
        assert second["data_dir"] == first["data_dir"]
        assert second["endpoint"] == first["endpoint"]
        assert second["service_id"] == first["service_id"]
        assert second["instance_id"] == first["instance_id"]
        assert first["workspace"] == str(one.resolve())
        assert second["workspace"] == str(two.resolve())
        assert list(one.iterdir()) == []
        assert list(two.iterdir()) == []

        _start_service(explicit_dir)
        explicit = pi_integration.prepare_session(
            data_dir=explicit_dir, workspace=one)
        assert explicit["data_dir"] == str(explicit_dir.resolve())
        assert explicit["data_dir"] != first["data_dir"]
        assert explicit["service_id"] != first["service_id"]
    finally:
        _quiet_stop(default)
        _quiet_stop(explicit_dir)
