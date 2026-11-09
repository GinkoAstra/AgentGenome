"""U03-common: the four interactive client profiles and their native roots.

Static registry only: no installed CLI, no user config and no execution Backend
is touched. Default roots are checked with an isolated HOME and explicit
documented overrides.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from node_runtime.control_plane import client_profiles
from node_runtime.control_plane.client_profiles import (
    CLIENT_PROFILES, default_activation_root, default_config_root,
    native_config_path, profile_for)

ROOT = Path(__file__).resolve().parents[2]


def test_registry_covers_exactly_the_interactive_cli_matrix():
    kinds = [profile.kind for profile in CLIENT_PROFILES]
    assert kinds == ["pi", "codex", "qoder", "claude"]
    executables = {profile.kind: profile.executable for profile in CLIENT_PROFILES}
    assert executables == {"pi": "pi", "codex": "codex",
                           "qoder": "qodercli", "claude": "claude"}
    formats = {profile.kind: profile.native_format for profile in CLIENT_PROFILES}
    assert formats == {"pi": "pi-extension", "codex": "toml-mcp",
                       "qoder": "json-mcp", "claude": "json-mcp"}
    for kind in kinds:
        assert profile_for(kind).kind == kind


@pytest.mark.parametrize("kind", ["script", "kimi", "", "PI"])
def test_script_and_unknown_kinds_are_not_installable(kind):
    with pytest.raises(ValueError):
        profile_for(kind)
    with pytest.raises(ValueError):
        native_config_path(kind, "/root")


def test_native_files_are_documented_names_under_the_config_root(tmp_path):
    assert native_config_path("pi", tmp_path) == tmp_path / "extensions" / "agentgenome.js"
    assert native_config_path("codex", tmp_path) == tmp_path / "config.toml"
    assert native_config_path("qoder", tmp_path) == tmp_path / "settings.json"
    assert native_config_path("claude", tmp_path) == tmp_path / ".claude.json"


def test_default_roots_honour_only_documented_overrides(tmp_path, monkeypatch):
    for name in ("PI_CODING_AGENT_DIR", "CODEX_HOME", "CLAUDE_CONFIG_DIR",
                 "QODER_CONFIG_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    assert default_config_root("pi") == tmp_path / ".pi" / "agent"
    assert default_config_root("codex") == tmp_path / ".codex"
    assert default_config_root("qoder") == tmp_path / ".qoder"
    assert default_config_root("claude") == tmp_path

    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi-override"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-override"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-override"))
    monkeypatch.setenv("QODER_CONFIG_DIR", str(tmp_path / "qoder-override"))
    assert default_config_root("pi") == tmp_path / "pi-override"
    assert default_config_root("codex") == tmp_path / "codex-override"
    assert default_config_root("claude") == tmp_path / "claude-override"
    # Measured on qodercli 1.1.85: an isolated QODER_CONFIG_DIR root supplies
    # both settings.json and skills, same as --config-dir.
    assert default_config_root("qoder") == tmp_path / "qoder-override"
    assert not (tmp_path / ".pi").exists()


def test_activation_roots_match_measured_native_discovery(tmp_path, monkeypatch):
    for name in ("CLAUDE_CONFIG_DIR",):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert default_activation_root("pi", tmp_path) is None
    assert default_activation_root("codex", tmp_path) == tmp_path / "skills"
    assert default_activation_root("qoder", tmp_path) == tmp_path / "skills"
    assert (default_activation_root("claude", tmp_path)
            == tmp_path / ".claude" / "skills")

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    assert (default_activation_root("claude", tmp_path / "claude-config")
            == tmp_path / "claude-config" / "skills")

    # An explicit native config root is launched via an override, so its skill
    # root follows that root even when the installer process has no env var.
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    assert (default_activation_root("claude", tmp_path / "explicit",
                                    explicit=True)
            == tmp_path / "explicit" / "skills")


def test_client_kinds_have_one_declaration_reused_by_the_boundary():
    from node_runtime.control_plane import (
        access_contracts, client_mcp, service_access)

    assert access_contracts.CLIENT_KINDS == (
        "pi", "qoder", "codex", "claude", "script")
    assert service_access.CLIENT_KINDS is access_contracts.CLIENT_KINDS
    assert client_mcp.CLIENT_KINDS is access_contracts.CLIENT_KINDS
    assert ({profile.kind for profile in CLIENT_PROFILES} | {"script"}
            == set(access_contracts.CLIENT_KINDS))


def test_profiles_never_import_execution_backends():
    script = (
        "import json, sys\n"
        "import node_runtime.control_plane.client_profiles as profiles\n"
        "import node_runtime.control_plane.client_entries as entries\n"
        "bad = sorted(name for name in sys.modules\n"
        "             if name.startswith('node_runtime.control_plane.backends'))\n"
        "print(json.dumps({'kinds': [p.kind for p in profiles.CLIENT_PROFILES],\n"
        "                  'backends': bad}))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", script], cwd=str(ROOT),
        env={**os.environ, "PYTHONPATH": str(ROOT),
             "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stdout + done.stderr
    assert json.loads(done.stdout) == {
        "kinds": ["pi", "codex", "qoder", "claude"], "backends": []}
