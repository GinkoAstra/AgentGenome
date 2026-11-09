"""Host composition of CLI adapters: explicit deployment selection only."""
from __future__ import annotations

import argparse
import sys

import pytest

from node_runtime.control_plane.host import Host, _config_mismatch
from node_runtime.control_plane.resources import ControlPlaneError


def _host(tmp_path, *, backend: str, config_dir=None, model=None):
    return Host(tmp_path / "data", backend=backend, model=model,
                executable=sys.executable, config_dir=config_dir)


def test_host_binds_codex_model_and_read_only_config_dir(tmp_path):
    config = tmp_path / "codex-home"
    config.mkdir()
    host = _host(tmp_path, backend="codex", config_dir=config,
                 model="gpt-test")
    backend = host.backends["codex"]
    assert backend.model == "gpt-test"
    assert backend.config_dir == config.resolve()
    argv = backend.protocol.argv(
        workspace=tmp_path, session_id=None, resume=False, exposed=(),
        mcp_config_path=None, mcp_server=None, settings_path=None)
    assert argv[argv.index("-m") + 1] == "gpt-test"
    assert backend.protocol.environment() == {
        "CODEX_HOME": str(config.resolve())}


def test_host_binds_claude_model_with_long_option_only(tmp_path):
    config = tmp_path / "claude-home"
    config.mkdir()
    host = _host(tmp_path, backend="claude", config_dir=config,
                 model="claude-test")
    backend = host.backends["claude"]
    argv = backend.protocol.argv(
        workspace=tmp_path, session_id=None, resume=False, exposed=(),
        mcp_config_path=None, mcp_server=None, settings_path=None)
    assert argv[argv.index("--model") + 1] == "claude-test"
    assert "-m" not in argv
    assert backend.protocol.environment() == {
        "CLAUDE_CONFIG_DIR": str(config.resolve())}


def test_host_binds_qoder_config_dir_flag_without_model(tmp_path):
    config = tmp_path / "qoder-home"
    config.mkdir()
    host = _host(tmp_path, backend="qoder", config_dir=config)
    backend = host.backends["qoder"]
    argv = backend.protocol.argv(
        workspace=tmp_path, session_id=None, resume=False, exposed=(),
        mcp_config_path=tmp_path / "empty.json", mcp_server=None,
        settings_path=None)
    assert argv[argv.index("--config-dir") + 1] == str(config.resolve())
    assert "-m" not in argv and "--model" not in argv


def test_host_without_config_dir_records_null_and_matches_itself(tmp_path):
    host = _host(tmp_path, backend="codex")
    assert host.config_dir is None
    assert host.claim() is None
    record = host.record()
    assert record["config_dir"] is None
    args = argparse.Namespace(
        backend="codex", provider=None, model=None, executable=sys.executable,
        config_dir=None, controller_id="ctl-1", scheduler_id="scheduler")
    assert _config_mismatch(record, args) is None


def test_host_config_dir_participates_in_restart_matching(tmp_path):
    config = tmp_path / "codex-home"
    config.mkdir()
    host = _host(tmp_path, backend="codex", config_dir=config)
    assert host.claim() is None
    record = host.record()
    assert record["config_dir"] == str(config.resolve())
    args = argparse.Namespace(
        backend="codex", provider=None, model=None, executable=sys.executable,
        config_dir=str(config), controller_id="ctl-1",
        scheduler_id="scheduler")
    assert _config_mismatch(record, args) is None
    args.config_dir = None
    mismatch = _config_mismatch(record, args)
    assert mismatch["config_dir"] == {
        "running": str(config.resolve()), "requested": None}


def test_host_rejects_provider_for_cli_backend(tmp_path):
    with pytest.raises(ControlPlaneError) as caught:
        Host(tmp_path / "data", backend="codex", provider="openai",
             executable=sys.executable)
    assert caught.value.code == "invalid_argument"


def test_host_rejects_config_dir_for_non_cli_backend(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    with pytest.raises(ControlPlaneError) as caught:
        Host(tmp_path / "data", backend="script", config_dir=config)
    assert caught.value.code == "invalid_argument"


def test_host_rejects_unimplemented_kimi(tmp_path):
    with pytest.raises(ControlPlaneError) as caught:
        Host(tmp_path / "data", backend="kimi", executable=sys.executable)
    assert caught.value.code == "capability_unsupported"
    assert "not implemented" in str(caught.value)
