"""Synthetic path-boundary tests for the Pi tool profile (no model call)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
MODULE = Path(__file__).resolve().parents[2] / "node_runtime/control_plane/backends/pi_tools.mjs"
PROBE = Path(__file__).with_name("fixtures") / "tool_probe.mjs"

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def run_tool(workspace: Path, name: str, params: dict, receipts: Path) -> dict:
    environment = dict(os.environ)
    environment.update({
        "AGENTGENOME_WORKSPACE": str(workspace),
        "AGENTGENOME_TOOL_RECEIPT": str(receipts),
        "AGENTGENOME_WORKER_ID": "probe-worker",
        "AGENTGENOME_NODE_ID": "probe-node",
        "AGENTGENOME_RUN_ID": "probe-run",
        "AGENTGENOME_EPOCH": "1",
    })
    completed = subprocess.run(
        [NODE, str(PROBE), str(MODULE), name, json.dumps(params)],
        capture_output=True, text=True, env=environment, timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


@pytest.fixture
def space(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    private = outside / "private.txt"
    private.write_text("synthetic private bytes", encoding="utf-8")
    return workspace, outside, private, tmp_path / "receipts.jsonl"


def test_read_file_rejects_a_final_symlink(space):
    workspace, outside, private, receipts = space
    (workspace / "shortcut").symlink_to(private)
    result = run_tool(workspace, "read_file", {"path": "shortcut"}, receipts)
    assert result.get("isError") is True
    assert payload(result)["code"] == "path_symlink"
    assert private.read_text(encoding="utf-8") == "synthetic private bytes"


def test_read_file_rejects_a_symlinked_parent(space):
    workspace, outside, private, receipts = space
    (workspace / "nested").symlink_to(outside)
    result = run_tool(workspace, "read_file", {"path": "nested/private.txt"}, receipts)
    assert result.get("isError") is True
    assert payload(result)["code"] == "path_symlink"


def test_write_file_rejects_a_symlinked_parent_without_touching_outside(space):
    workspace, outside, private, receipts = space
    (workspace / "nested").symlink_to(outside)
    result = run_tool(workspace, "write_file",
                      {"path": "nested/escape.txt", "text": "escaped"}, receipts)
    assert result.get("isError") is True
    assert payload(result)["code"] == "path_symlink"
    assert not (outside / "escape.txt").exists()


def test_write_file_rejects_a_final_symlink(space):
    workspace, outside, private, receipts = space
    (workspace / "shortcut").symlink_to(private)
    result = run_tool(workspace, "write_file",
                      {"path": "shortcut", "text": "replaced"}, receipts)
    assert result.get("isError") is True
    assert payload(result)["code"] == "path_symlink"
    assert private.read_text(encoding="utf-8") == "synthetic private bytes"
    assert (workspace / "shortcut").is_symlink()


@pytest.mark.parametrize("path", ["/etc/passwd", "../outside/private.txt",
                                  "nested/../../outside/private.txt"])
def test_lexical_escape_is_rejected(space, path):
    workspace, outside, private, receipts = space
    result = run_tool(workspace, "read_file", {"path": path}, receipts)
    assert result.get("isError") is True
    assert payload(result)["code"] == "path_outside_workspace"


def test_regular_write_and_read_roundtrip(space):
    workspace, outside, private, receipts = space
    written = run_tool(workspace, "write_file",
                       {"path": "sub/result.txt", "text": "hello"}, receipts)
    assert written.get("isError") is not True
    assert payload(written)["sha256"] and payload(written)["bytes"] == 5
    read = run_tool(workspace, "read_file", {"path": "sub/result.txt"}, receipts)
    assert payload(read)["text"] == "hello"
    assert (workspace / "sub/result.txt").read_text(encoding="utf-8") == "hello"
    entries = [json.loads(line) for line in receipts.read_text().splitlines()]
    assert {entry["phase"] for entry in entries} == {"start", "end"}
    assert all(entry["worker_id"] == "probe-worker" and entry["epoch"] == 1
               for entry in entries)
