"""Real Pi RPC: the installed extension is discovered and activates tools.

This does not use a fake ExtensionAPI. It starts the installed `pi` binary in
RPC mode with an isolated agent directory and an empty project cwd, asks for
commands, runs /agentgenome, and reads the real tool registry from a tiny probe
extension. No model is called and no run is submitted.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROVIDER = "llama-local"
MODEL = "qwen3.5-2b"

PROBE = """\
import { writeFileSync } from "node:fs";
export default function (pi) {
  pi.registerCommand("agentgenome-probe", {
    description: "write the current tool registry",
    handler: async (args, ctx) => {
      writeFileSync(process.env.AGENTGENOME_PROBE_OUT, JSON.stringify({
        all: pi.getAllTools().map((tool) => tool.name),
        active: pi.getActiveTools(),
        mode: ctx.mode,
        cwd: ctx.cwd,
      }));
    },
  });
}
"""


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def test_real_pi_rpc_sees_the_installed_command(tmp_path):
    pi = shutil.which("pi")
    if pi is None:
        pytest.skip("pi is required for the real RPC check")

    agent = tmp_path / "agent"
    project = tmp_path / "project"
    extensions = agent / "extensions"
    extensions.mkdir(parents=True)
    project.mkdir()
    home = tmp_path / "home"
    state = tmp_path / "state"
    home.mkdir()
    state.mkdir()
    probe_out = tmp_path / "probe.json"

    install = subprocess.run(
        [sys.executable, "-m", "node_runtime.control_plane.pi_integration",
         "install", "--provider", PROVIDER, "--model", MODEL,
         "--agent-dir", str(agent)],
        cwd=str(ROOT), capture_output=True, text=True, timeout=60)
    assert install.returncode == 0, install.stderr
    (extensions / "zz-probe.js").write_text(PROBE, encoding="utf-8")

    env = dict(os.environ)
    env.update({"PI_CODING_AGENT_DIR": str(agent),
                "HOME": str(home),
                "XDG_STATE_HOME": str(state),
                "XDG_CONFIG_HOME": str(tmp_path / "config"),
                "XDG_CACHE_HOME": str(tmp_path / "cache"),
                "AGENTGENOME_PROBE_OUT": str(probe_out)})
    process = subprocess.Popen(
        [pi, "--mode", "rpc", "--no-session", "--offline",
         "--no-context-files", "--no-skills", "--no-prompt-templates",
         "--no-themes"],
        cwd=str(project), env=env, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    assert process.stdin and process.stdout and process.stderr

    events: list[dict] = []
    stderr_lines: list[str] = []
    records: "queue.Queue[dict]" = queue.Queue()

    def read_stdout():
        for line in process.stdout:
            try:
                records.put(json.loads(line))
            except ValueError:
                pass

    def read_stderr():
        for line in process.stderr:
            stderr_lines.append(line)

    threading.Thread(target=read_stdout, daemon=True).start()
    threading.Thread(target=read_stderr, daemon=True).start()

    def send(record):
        process.stdin.write(json.dumps(record) + "\n")
        process.stdin.flush()

    def response(req_id, timeout=90):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                record = records.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                break
            events.append(record)
            if record.get("type") == "response" and record.get("id") == req_id:
                return record
        raise AssertionError(f"no response for {req_id}")

    def probe(req_id):
        probe_out.unlink(missing_ok=True)
        send({"id": req_id, "type": "prompt", "message": "/agentgenome-probe"})
        assert response(req_id)["success"] is True
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if probe_out.is_file():
                return json.loads(probe_out.read_text(encoding="utf-8"))
            time.sleep(0.05)
        raise AssertionError("probe did not write the tool registry")

    def diagnostics() -> str:
        service = state / "agentgenome" / "default"
        log = service / "host.log"
        tail = log.read_text(encoding="utf-8")[-3000:] if log.exists() else ""
        others = [event for event in events if event.get("type") != "response"]
        return (f"events={others[-20:]!r}\n"
                f"stderr={''.join(stderr_lines)[-3000:]!r}\n"
                f"host.log={tail!r}\n"
                f"service={sorted(p.name for p in service.iterdir()) if service.exists() else []}")

    def probe_until_ready():
        deadline = time.monotonic() + 60
        counter = 0
        while time.monotonic() < deadline:
            counter += 1
            snapshot = probe(f"probe-active-{counter}")
            if "validate_definition" in snapshot.get("active", []):
                return snapshot
            time.sleep(0.2)
        raise AssertionError("activation did not register tools\n" + diagnostics())

    try:
        send({"id": "commands", "type": "get_commands"})
        commands = response("commands")
        names = [command["name"] for command in commands["data"]["commands"]]
        assert "agentgenome" in names, names
        assert "agentgenome-probe" in names, names

        send({"id": "activate", "type": "prompt", "message": "/agentgenome"})
        assert response("activate")["success"] is True

        snapshot = probe_until_ready()
        assert snapshot["mode"] == "rpc"
        assert Path(snapshot["cwd"]).resolve() == project.resolve()
        assert "validate_definition" in snapshot["active"]
        assert "validate_definition" in snapshot["all"]

        # The trace panel is TUI-only; in RPC mode the command must answer
        # without rendering a terminal UI or breaking the session.
        send({"id": "trace", "type": "prompt", "message": "/agentgenome trace"})
        assert response("trace")["success"] is True
        snapshot = probe("probe-after-trace")
        assert snapshot["mode"] == "rpc"
        assert "validate_definition" in snapshot["active"]

        send({"id": "off", "type": "prompt", "message": "/agentgenome off"})
        assert response("off")["success"] is True
        snapshot = probe("probe-off")
        assert "validate_definition" not in snapshot["active"]
        assert "read" in snapshot["active"]  # native tools stay active

        assert not (project / ".agentgenome").exists()
        service_dir = state / "agentgenome" / "default"
        host_record = json.loads(
            (service_dir / "host.json").read_text(encoding="utf-8"))
        assert _pid_alive(int(host_record["pid"]))
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
        subprocess.run(
            [sys.executable, "-m", "node_runtime.control_plane.host", "stop",
             "--data-dir", str(state / "agentgenome" / "default")],
            cwd=str(ROOT), capture_output=True, text=True, timeout=30)
