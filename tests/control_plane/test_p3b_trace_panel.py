"""P3b trace panel: shipped panel against a real loopback HTTP/SSE service.

The probe imports `pi_trace.mjs`, the shipped `service_http.mjs` transport and
the official pi-tui width helpers from the installed Pi package. No model is
called and no service is started; the fake service is a local HTTP server.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROBE = Path(__file__).with_name("fixtures") / "trace_panel_probe.mjs"
MODULE = ROOT / "node_runtime" / "control_plane" / "backends" / "pi_trace.mjs"
SESSION_PROBE = Path(__file__).with_name("fixtures") / "session_run_probe.mjs"
COMMAND = ROOT / "node_runtime" / "control_plane" / "backends" / "pi_command.mjs"


def _pi_tui_entry() -> Path | None:
    pi = shutil.which("pi")
    if pi is None:
        return None
    package = Path(pi).resolve().parents[2]
    entry = (package / "node_modules" / "@earendil-works" / "pi-tui"
             / "dist" / "index.js")
    return entry if entry.is_file() else None


def test_trace_panel_lifecycle_and_cursor_semantics():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the trace panel probe")
    entry = _pi_tui_entry()
    if entry is None:
        pytest.skip("the installed Pi pi-tui helpers are required")
    assert MODULE.is_file(), MODULE
    env = dict(os.environ)
    env["AGENTGENOME_TRACE_MODULE"] = str(MODULE)
    env["AGENTGENOME_PI_TUI_ENTRY"] = str(entry)
    completed = subprocess.run(
        [node, str(PROBE)], cwd=str(ROOT), env=env,
        capture_output=True, text=True, timeout=120)
    assert completed.returncode == 0, (
        f"probe exited {completed.returncode}\n"
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}")
    verdict = json.loads(completed.stdout)
    assert verdict["ok"] is True, verdict
    assert len(verdict["checks"]) >= 20, verdict


def test_session_run_ownership_through_the_command():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the session run probe")
    entry = _pi_tui_entry()
    if entry is None:
        pytest.skip("the installed Pi pi-tui helpers are required")
    assert COMMAND.is_file(), COMMAND
    env = dict(os.environ)
    env["AGENTGENOME_PI_COMMAND_MODULE"] = str(COMMAND)
    env["AGENTGENOME_PI_TUI_ENTRY"] = str(entry)
    completed = subprocess.run(
        [node, str(SESSION_PROBE)], cwd=str(ROOT), env=env,
        capture_output=True, text=True, timeout=120)
    assert completed.returncode == 0, (
        f"probe exited {completed.returncode}\n"
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}")
    verdict = json.loads(completed.stdout)
    assert verdict["ok"] is True, verdict
    assert len(verdict["checks"]) >= 12, verdict
