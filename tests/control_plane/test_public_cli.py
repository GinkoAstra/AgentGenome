"""Public router: client commands stay independent of Host imports.

These tests exercise the packaged console entry (``cli:main``) through the
module route, with an explicit isolated config root. They assert routing,
help exposure, the ``--client`` alias, exit codes and zero-write previews;
they never touch a user config root, service, model or CLI native process.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYTHON = os.path.abspath(sys.executable)


def _env(home: Path) -> dict[str, str]:
    return {
        **os.environ,
        "HOME": str(home),
        "PI_CODING_AGENT_DIR": str(home / "pi-agent"),
        "CODEX_HOME": str(home / "codex-home"),
        "CLAUDE_CONFIG_DIR": str(home / "claude-home"),
        "PYTHONPATH": str(ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def _run(args: list[str], home: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PYTHON, "-m", "node_runtime.control_plane.cli", *args],
        cwd=str(ROOT), env=_env(home), capture_output=True, text=True, timeout=120)


def test_client_route_never_imports_host_or_runtime(tmp_path):
    root = tmp_path / "pi-root"
    script = (
        "import sys\n"
        "from node_runtime.control_plane import cli\n"
        "code = cli.main(['client', 'plan', '--client', 'pi',\n"
        "                '--config-root', sys.argv[1]])\n"
        "bad = sorted(name for name in sys.modules if name in {\n"
        "    'node_runtime.control_plane.host',\n"
        "    'node_runtime.control_plane.server',\n"
        "    'node_runtime.control_plane.api',\n"
        "    'node_runtime.control_plane.scheduler',\n"
        "})\n"
        "print('IMPORTS=' + ','.join(bad))\n"
        "raise SystemExit(code)\n"
    )
    done = subprocess.run(
        [PYTHON, "-c", script, str(root)],
        cwd=str(ROOT), env=_env(tmp_path), capture_output=True, text=True,
        timeout=120)
    assert done.returncode == 0, done.stderr
    assert "IMPORTS=" in done.stdout
    assert "IMPORTS=\n" in done.stdout
    payload = json.loads(done.stdout[:done.stdout.rindex("IMPORTS=")].strip())
    assert payload["command"] == "plan"
    assert payload["client_kind"] == "pi"
    assert payload["dry_run"] is True
    assert not root.exists(), "plan must not create the config root"


def test_root_help_lists_the_client_group_and_host_commands(tmp_path):
    done = _run(["--help"], tmp_path)
    assert done.returncode == 0, done.stderr
    assert "client" in done.stdout
    assert "plan, install or remove one interactive CLI entry" in done.stdout
    for command in ("serve", "start", "status", "stop", "teacher"):
        assert command in done.stdout


def test_client_help_exposes_actions_and_the_client_alias(tmp_path):
    group = _run(["client", "--help"], tmp_path)
    assert group.returncode == 0, group.stderr
    for action in ("plan", "install", "uninstall"):
        assert action in group.stdout
    action_help = _run(["client", "plan", "--help"], tmp_path)
    assert action_help.returncode == 0, action_help.stderr
    assert "--client" in action_help.stdout
    assert "--client-kind" in action_help.stdout
    assert "--config-root" in action_help.stdout
    assert "--data-dir" in action_help.stdout


def test_client_alias_matches_client_kind_without_writing(tmp_path):
    root = tmp_path / "shared-root"
    aliased = _run(
        ["client", "plan", "--client", "codex", "--config-root", str(root)],
        tmp_path)
    legacy = _run(
        ["client", "plan", "--client-kind", "codex", "--config-root", str(root)],
        tmp_path)
    assert aliased.returncode == legacy.returncode == 0
    assert aliased.stdout == legacy.stdout
    assert json.loads(aliased.stdout)["dry_run"] is True
    assert not root.exists()


def test_client_conflict_keeps_exit_code_three(tmp_path):
    root = tmp_path / "conflict-root"
    native = root / "extensions" / "agentgenome.js"
    native.parent.mkdir(parents=True)
    native.write_text("// foreign extension\n", encoding="utf-8")
    done = _run(
        ["client", "install", "--client", "pi", "--config-root", str(root)],
        tmp_path)
    assert done.returncode == 3, done.stderr
    payload = json.loads(done.stdout)
    assert payload["ok"] is False
    assert payload["conflicts"]
    assert native.read_text(encoding="utf-8") == "// foreign extension\n"


def test_invalid_client_kind_keeps_exit_code_two(tmp_path):
    done = _run(
        ["client", "plan", "--client", "vim", "--config-root", str(tmp_path)],
        tmp_path)
    assert done.returncode == 2
    assert "invalid choice" in done.stderr


def test_console_entry_points_at_the_public_router():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["scripts"]["agentgenome"] == (
        "node_runtime.control_plane.cli:main")
