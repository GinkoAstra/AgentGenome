"""Focused tests for the ordinary-Pi /agentgenome integration layer.

These exercise only install/uninstall/prepare and the generated bootstrap.
Runtime semantics (Api/Scheduler/Controller) are covered elsewhere and are not
re-implemented here. No model is called: the Host starts and stops, but no run
is submitted.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import sys
from pathlib import Path

import pytest

from node_runtime.control_plane import host as host_module
from node_runtime.control_plane import pi_integration
from node_runtime.control_plane.mcp_server import TOOL_NAMES
from node_runtime.control_plane.resources import ControlPlaneError

PROVIDER = "llama-local"
MODEL = "qwen3.5-2b"


def _quiet_stop(data_dir: Path) -> None:
    with contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        host_module.main(["stop", "--data-dir", str(data_dir)])


@pytest.fixture
def data_dir(tmp_path):
    root = tmp_path / "data"
    yield root
    _quiet_stop(root)


def test_install_is_idempotent_and_owns_only_its_bootstrap(tmp_path):
    agent = tmp_path / "agent"
    target = agent / "extensions" / "agentgenome.js"

    created = pi_integration.install(
        provider=PROVIDER, model=MODEL, agent_dir=agent)
    assert created["state"] == "created"
    assert target.is_file()
    text = target.read_text(encoding="utf-8")
    assert pi_integration.OWNER_MARKER in text
    assert str(pi_integration.PI_COMMAND.resolve()) in text
    assert json.dumps(os.path.abspath(sys.executable)) in text
    assert PROVIDER in text and MODEL in text
    # The wrapper supplies Pi's official width helpers to the trace panel via
    # the extension loader alias; no machine-global path is embedded.
    assert '"@earendil-works/pi-tui"' in text
    for helper in ("truncateToWidth", "visibleWidth", "wrapTextWithAnsi"):
        assert helper in text
    assert "helpers:" in text

    again = pi_integration.install(
        provider=PROVIDER, model=MODEL, agent_dir=agent)
    assert again["state"] == "unchanged"
    assert target.read_text(encoding="utf-8") == text

    updated = pi_integration.install(
        provider=PROVIDER, model="other-model", agent_dir=agent)
    assert updated["state"] == "updated"
    assert "other-model" in target.read_text(encoding="utf-8")

    # Settings and credentials are never part of the owned file.
    assert not (agent / "settings.json").exists()
    assert not (agent / "auth.json").exists()


def test_install_refuses_a_foreign_extension_file(tmp_path):
    agent = tmp_path / "agent"
    target = agent / "extensions" / "agentgenome.js"
    target.parent.mkdir(parents=True)
    target.write_text("export default () => {};\n", encoding="utf-8")

    with pytest.raises(ControlPlaneError) as refused:
        pi_integration.install(provider=PROVIDER, model=MODEL, agent_dir=agent)
    assert refused.value.code == "invalid_argument"
    assert "not owned" in refused.value.message
    assert target.read_text(encoding="utf-8") == "export default () => {};\n"


def test_uninstall_removes_only_the_owned_bootstrap(tmp_path):
    agent = tmp_path / "agent"
    pi_integration.install(provider=PROVIDER, model=MODEL, agent_dir=agent)
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


def test_install_honours_PI_CODING_AGENT_DIR(tmp_path, monkeypatch):
    agent = tmp_path / "env-agent"
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent))
    result = pi_integration.install(provider=PROVIDER, model=MODEL)
    assert Path(result["agent_dir"]) == agent.resolve()
    assert (agent / "extensions" / "agentgenome.js").is_file()


def test_prepare_starts_and_reuses_the_real_service(data_dir, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = pi_integration.prepare_session(
        data_dir=data_dir, workspace=workspace,
        provider=PROVIDER, model=MODEL)
    assert set(first) == {"prompt", "workspace", "data_dir", "endpoint",
                          "mcp_config", "host_status", "tool_names"}
    assert first["workspace"] == str(workspace.resolve())
    assert first["data_dir"] == str(data_dir.resolve())
    assert first["endpoint"].startswith("http://127.0.0.1:")
    assert first["host_status"]["running"] is True
    pid = first["host_status"]["pid"]
    assert isinstance(pid, int)
    assert first["tool_names"] == list(TOOL_NAMES)

    config = json.loads(first["mcp_config"])
    assert config["format"] == "agentgenome-teacher-mcp/2"
    assert config["endpoint"] == first["endpoint"]
    assert config["workspace"] == str(workspace.resolve())
    assert config["token"] and "command" not in config and "args" not in config

    # The credential is available only to the in-memory MCP client; it never
    # becomes model-visible prompt text or status output.
    assert config["token"] not in first["prompt"]
    assert config["token"] not in json.dumps(first["host_status"])

    # The slash entry keeps the native cwd, so the prompt names the absolute
    # Python and workspace rather than relying on an activated PATH.
    assert str(data_dir.resolve()) in first["prompt"]
    assert str(workspace.resolve()) in first["prompt"]
    assert os.path.abspath(sys.executable) in first["prompt"]
    assert "斜杠入口环境" in first["prompt"]
    assert "服务数据目录" in first["prompt"]
    assert (data_dir / "teacher" / "prompt.md").read_text(
        encoding="utf-8") == first["prompt"]

    # Reference files belong to the caller's local workspace, never the
    # shared service state.
    assert (workspace / "sop-guide.md").is_file()
    assert (workspace / "sop-template.yaml").is_file()

    second = pi_integration.prepare_session(
        data_dir=data_dir, workspace=workspace,
        provider=PROVIDER, model=MODEL)
    assert second["host_status"]["running"] is True
    assert second["host_status"]["pid"] == pid
    assert second["endpoint"] == first["endpoint"]


def test_prepare_reports_a_model_conflict_without_stopping_the_host(
        data_dir, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = pi_integration.prepare_session(
        data_dir=data_dir, workspace=workspace,
        provider=PROVIDER, model=MODEL)
    pid = first["host_status"]["pid"]

    conflicted = pi_integration.prepare_session(
        data_dir=data_dir, workspace=workspace,
        provider=PROVIDER, model="other-model")
    status = conflicted["host_status"]
    assert status["error"] == "host_config_mismatch"
    assert status["mismatch"]["model"] == {
        "running": MODEL, "requested": "other-model"}
    assert status["pid"] == pid

    record = json.loads((data_dir / "host.json").read_text(encoding="utf-8"))
    assert record["pid"] == pid
    with contextlib.redirect_stdout(io.StringIO()):
        with contextlib.redirect_stderr(io.StringIO()):
            assert host_module.main(
                ["status", "--data-dir", str(data_dir),
                 "--provider", PROVIDER, "--model", MODEL]) == 0


def test_default_service_is_shared_across_cwds_and_explicit_dir_is_separate(
        tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    one = tmp_path / "one"
    two = tmp_path / "two"
    one.mkdir()
    two.mkdir()

    first = pi_integration.prepare_session(
        workspace=one, provider=PROVIDER, model=MODEL)
    second = pi_integration.prepare_session(
        workspace=two, provider=PROVIDER, model=MODEL)
    default = state / "agentgenome" / "default"
    explicit_dir = tmp_path / "explicit"
    try:
        assert Path(first["data_dir"]) == default.resolve()
        assert second["data_dir"] == first["data_dir"]
        assert second["endpoint"] == first["endpoint"]
        assert second["host_status"]["pid"] == first["host_status"]["pid"]
        assert first["workspace"] == str(one.resolve())
        assert second["workspace"] == str(two.resolve())
        assert first["workspace"] != second["workspace"]
        assert not (one / ".agentgenome").exists()
        assert not (two / ".agentgenome").exists()

        explicit = pi_integration.prepare_session(
            data_dir=explicit_dir, workspace=one,
            provider=PROVIDER, model=MODEL)
        assert explicit["data_dir"] == str(explicit_dir.resolve())
        assert explicit["data_dir"] != first["data_dir"]
        assert explicit["host_status"]["pid"] != first["host_status"]["pid"]
    finally:
        _quiet_stop(default)
        _quiet_stop(explicit_dir)


def test_slash_prompt_shell_quotes_every_host_argument(tmp_path):
    data_dir = tmp_path / "data dir; touch PWNED"
    workspace = data_dir / "teacher" / "workspace"
    provider = 'local "gateway"; $HOME'
    model = "m 2; $(touch PWNED)"

    prompt = pi_integration._slash_prompt(
        {"root": data_dir, "workspace": workspace},
        provider=provider, model=model)

    base = [os.path.abspath(sys.executable), "-m",
            "node_runtime.control_plane.host"]
    status_argv = base + ["status", "--data-dir", str(data_dir)]
    start_argv = base + ["start", "--data-dir", str(data_dir),
                         "--backend", "pi", "--provider", provider,
                         "--model", model]
    assert shlex.join(status_argv) in prompt
    assert shlex.join(start_argv) in prompt
    assert "--backend pi" in prompt

    # The same command line must parse back to exactly the intended argv:
    # spaces and metacharacters stay inside their own arguments.
    parsed = [shlex.split(line.strip()) for line in prompt.splitlines()
              if "node_runtime.control_plane.host" in line]
    assert status_argv in parsed
    assert start_argv in parsed

    # No unquoted interpolation of user-controlled values.
    assert f'--data-dir "{data_dir}"' not in prompt
    assert f'--provider "{provider}"' not in prompt
    assert f'--model "{model}"' not in prompt
