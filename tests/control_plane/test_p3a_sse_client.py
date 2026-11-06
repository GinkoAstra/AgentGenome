"""Bounded SSE client queue: real Node transport against local scripted streams.

The probe imports the shipped `service_http.mjs` (no reimplementation): it
asserts a slow consumer produces server-side backpressure without losing or
reordering frames, and an oversized incomplete frame fails explicitly.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROBE = Path(__file__).with_name("fixtures") / "sse_queue_probe.mjs"
TRANSPORT = ROOT / "node_runtime" / "control_plane" / "backends" / "service_http.mjs"


def test_sse_client_applies_backpressure_and_bounds_frames():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the SSE transport probe")
    assert TRANSPORT.is_file(), TRANSPORT
    env = dict(os.environ)
    env["AGENTGENOME_SERVICE_HTTP"] = str(TRANSPORT)
    completed = subprocess.run(
        [node, str(PROBE)], cwd=str(ROOT), env=env,
        capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, (
        f"probe exited {completed.returncode}\n"
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}")
    verdict = json.loads(completed.stdout)
    assert verdict["ok"] is True, verdict
    assert verdict["backpressure"]["backpressure"] is True, verdict
    assert verdict["backpressure"]["received"] == verdict["backpressure"]["frames"]
    assert verdict["oversized"]["code"] == "sse_frame_too_large"
