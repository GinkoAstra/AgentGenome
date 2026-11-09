"""U03-host: shared activation assets are pure text, never an installer.

Pi keeps its native /agentgenome command and must not receive a duplicate
skill. Codex, qodercli and Claude Code receive one skill plus the generated
Graph SOP reference, all rendered from the shared instruction body and the
Teacher guide/template. Rendering creates no file and reads no credential.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from node_runtime.control_plane import client_activation, teacher_entry
from node_runtime.control_plane.client_activation import (
    GRAPH_SOP_RELATIVE, SKILL_RELATIVE, ActivationAsset, render_activation_assets)


def test_pi_has_no_duplicate_skill():
    assert render_activation_assets("pi") == ()


@pytest.mark.parametrize("kind", ["codex", "qoder", "claude"])
def test_each_non_pi_host_gets_one_skill_and_one_reference(kind):
    assets = render_activation_assets(kind)
    assert tuple(asset.relative_path for asset in assets) == (
        SKILL_RELATIVE, GRAPH_SOP_RELATIVE)
    assert all(isinstance(asset, ActivationAsset) for asset in assets)
    skill, reference = (asset.content for asset in assets)

    assert skill.startswith("---\nname: agentgenome\n")
    assert "description:" in skill.split("---", 2)[1]
    assert "existing `agentgenome` MCP connection" in skill
    assert "not a Worker or a new Agent loop" in skill
    assert "never silently create a new request" in skill

    assert teacher_entry.GUIDE in reference
    assert teacher_entry.TEMPLATE in reference
    assert "sop-template.yaml" in reference


def test_rendering_is_deterministic_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    before = sorted(Path(tmp_path).rglob("*"))
    first = render_activation_assets("codex")
    second = render_activation_assets("codex")
    assert first == second
    assert sorted(Path(tmp_path).rglob("*")) == before


def test_unknown_kind_is_rejected():
    with pytest.raises(ValueError):
        render_activation_assets("script")
    with pytest.raises(ValueError):
        render_activation_assets("kimi")


def test_assets_never_read_the_environment(monkeypatch, tmp_path):
    for name in ("HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME",
                 "PI_CODING_AGENT_DIR", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(name, str(tmp_path / "sentinel"))
    assets = render_activation_assets("claude")
    joined = "\n".join(asset.content for asset in assets)
    assert str(tmp_path) not in joined
    assert os.environ["ANTHROPIC_API_KEY"] not in joined
