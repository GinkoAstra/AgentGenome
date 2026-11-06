"""Run the real /agentgenome command harness under Node.

The harness imports the shipped pi_command.mjs with a fake ExtensionAPI, runs
the real prepare command, starts the real per-user network service and calls
Graph tools through the real Streamable HTTP MCP endpoint. This wrapper only
supplies isolated paths and asserts the harness verdict; it does not
re-implement command behavior.

HOME/XDG_STATE_HOME point into the test's temporary directory, so the default
service used here is never the real user instance.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HARNESS = Path(__file__).with_name("pi_command_harness.mjs")
EXTENSION = ROOT / "node_runtime" / "control_plane" / "backends" / "pi_command.mjs"


def test_agentgenome_command_harness(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the Pi command harness")
    assert EXTENSION.is_file(), f"missing shipped extension: {EXTENSION}"

    home = tmp_path / "home"
    state = tmp_path / "state"
    home.mkdir()
    state.mkdir()
    env = dict(os.environ)
    env.update({
        "HOME": str(home),
        "XDG_STATE_HOME": str(state),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "PI_CODING_AGENT_DIR": str(tmp_path / "pi-agent"),
        "AGENTGENOME_TEST_PYTHON": os.path.abspath(sys.executable),
        "AGENTGENOME_TEST_EXTENSION": str(EXTENSION),
        "AGENTGENOME_TEST_ROOT": str(ROOT),
    })
    completed = subprocess.run(
        [node, str(HARNESS)],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=240,
    )
    assert completed.returncode == 0, (
        f"harness exited {completed.returncode}\n"
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}")
    verdict = json.loads(completed.stdout)
    assert verdict["ok"] is True, verdict
    assert verdict["checks"], verdict
