"""U03-common: Pi keeps its compatibility entry but uses the shared engine.

These checks prove pi_integration no longer owns a second installer: the same
record, plan states and conflict rules come from client_install, and the
module CLI plans the exact same file for an isolated agent directory.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from node_runtime.control_plane import client_entries, client_install, pi_integration
from node_runtime.control_plane.resources import ControlPlaneError

ROOT = Path(__file__).resolve().parents[2]


def _cli(root: Path, command: str = "plan") -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(ROOT),
           "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run(
        [sys.executable, "-m", "node_runtime.control_plane.client_install",
         command, "--client-kind", "pi", "--config-root", str(root)],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120)


def test_pi_install_writes_the_one_shared_ownership_record(tmp_path):
    agent = tmp_path / "agent"
    result = pi_integration.install(agent_dir=agent)
    assert result["state"] == "create"

    extension = agent.resolve() / "extensions" / "agentgenome.js"
    record_path = client_install.record_path(agent.resolve(), "pi")
    assert Path(result["record"]) == record_path
    assert Path(result["extension"]) == extension
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["format"] == client_install.RECORD_FORMAT
    assert record["client_kind"] == "pi"
    assert record["python"] == os.path.abspath(sys.executable)
    assert record["entry_digest"] == client_entries.sha256_text(
        extension.read_text(encoding="utf-8"))
    assert record["activation"] == {}
    assert record["activation_root"] is None
    assert set(record) == {"format", "client_kind", "entry_digest",
                           "block_digest", "python", "data_dir",
                           "activation_root", "activation"}


def test_pi_plan_and_cli_plan_describe_the_same_shared_plan(tmp_path):
    agent = tmp_path / "agent"
    planned = pi_integration.plan_install(agent_dir=agent)
    done = _cli(agent, "plan")
    assert done.returncode == 0, done.stderr
    payload = json.loads(done.stdout)
    assert payload["native_path"] == planned["target"]
    assert payload["state"] == "ready"
    assert planned["state"] == "create"
    assert planned["content"] and planned["previous_sha256"] is None

    pi_integration.install(agent_dir=agent)
    assert pi_integration.plan_install(agent_dir=agent)["state"] == "unchanged"
    assert json.loads(_cli(agent, "plan").stdout)["state"] == "unchanged"


def test_pi_foreign_preview_refuses_without_leaking_foreign_text(tmp_path):
    agent = tmp_path / "agent"
    target = agent / "extensions" / "agentgenome.js"
    target.parent.mkdir(parents=True)
    target.write_text("// FOREIGN-USER-JUNK\nexport default () => {};\n",
                      encoding="utf-8")

    plan = pi_integration.plan_install(agent_dir=agent)
    assert plan["state"] == "conflict"
    assert plan["previous_sha256"]
    assert "FOREIGN-USER-JUNK" not in plan["content"]
    assert pi_integration.OWNER_MARKER in plan["content"]

    with pytest.raises(ControlPlaneError) as refused:
        pi_integration.install(agent_dir=agent)
    assert refused.value.code == "invalid_argument"
    assert target.read_text(encoding="utf-8").startswith("// FOREIGN-USER-JUNK")
    assert not client_install.record_path(agent, "pi").exists()

    done = _cli(agent, "install")
    assert done.returncode == 3
    assert "FOREIGN-USER-JUNK" not in done.stdout + done.stderr


def test_pi_uninstall_removes_only_the_owned_file_and_record(tmp_path):
    agent = tmp_path / "agent"
    pi_integration.install(agent_dir=agent)
    extension = agent / "extensions" / "agentgenome.js"
    record = client_install.record_path(agent, "pi")

    removed = pi_integration.uninstall(agent_dir=agent)
    assert removed["removed"] is True and removed["reason"] == "uninstalled"
    assert not extension.exists() and not record.exists()

    again = pi_integration.uninstall(agent_dir=agent)
    assert again["removed"] is False and again["reason"] == "not_installed"


def test_pi_cli_install_and_uninstall_round_trip_through_the_engine(tmp_path):
    agent = tmp_path / "agent"
    installed = _cli(agent, "install")
    assert installed.returncode == 0, installed.stderr
    payload = json.loads(installed.stdout)
    assert payload["client_kind"] == "pi"
    assert Path(payload["native_path"]).is_file()
    assert client_install.record_path(agent, "pi").is_file()

    removed = _cli(agent, "uninstall")
    assert removed.returncode == 0, removed.stderr
    assert not Path(payload["native_path"]).exists()
    assert not client_install.record_path(agent, "pi").exists()
