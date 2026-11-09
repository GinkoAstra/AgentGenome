"""U03-common: one managed-file engine for Pi, Codex, Qoder and Claude.

Every invocation uses an explicit isolated --config-root. No installed CLI,
default user root, model or service is touched. The public JSON is asserted to
carry summaries and digests only, never raw native text, because merging an
existing client config can retain unrelated credentials.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from node_runtime.control_plane import (client_activation, client_entries,
                                        client_install, client_profiles)

ROOT = Path(__file__).resolve().parents[2]
SENTINEL = "SENTINEL-U03-SECRET-9f2c"
PYTHON = os.path.abspath(sys.executable)


@pytest.fixture
def isolated_env(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return {
        "HOME": str(home),
        "PI_CODING_AGENT_DIR": str(home / "pi-agent"),
        "CODEX_HOME": str(home / "codex-home"),
        "CLAUDE_CONFIG_DIR": str(home / "claude-home"),
        "PYTHONPATH": str(ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def _cli(env: dict, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "node_runtime.control_plane.client_install", *args],
        cwd=str(cwd or ROOT), env={**os.environ, **env},
        capture_output=True, text=True, timeout=120)


def _payload(done: subprocess.CompletedProcess) -> dict:
    assert done.stdout.strip(), done.stderr
    return json.loads(done.stdout)


def _entry(kind: str, python: str = PYTHON, data_dir: Path | None = None) -> dict:
    args = ["-m", "node_runtime.control_plane.client_mcp",
            "--client-kind", kind]
    if data_dir is not None:
        args += ["--data-dir", str(data_dir)]
    entry: dict = {"command": python, "args": args,
                   "env": {"PYTHONPATH": str(ROOT)}}
    if kind == "claude":
        entry["type"] = "stdio"
    return entry


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _activation_digests(kind: str) -> dict[str, str]:
    return {str(asset.relative_path): client_entries.sha256_text(asset.content)
            for asset in client_activation.render_activation_assets(kind)}


def _activation_root(kind: str, root: Path, env: dict | None = None) -> Path | None:
    if kind == "pi":
        return None
    chosen = client_profiles.default_activation_root(kind, root, explicit=True)
    assert chosen is not None
    return chosen.resolve()


def _activation_paths(kind: str, root: Path, env: dict | None = None) -> list[str]:
    activation_root = _activation_root(kind, root, env)
    if activation_root is None:
        return []
    return [str(activation_root / asset.relative_path)
            for asset in client_activation.render_activation_assets(kind)]


@pytest.mark.parametrize("kind", ["pi", "codex", "qoder", "claude"])
def test_plan_is_zero_write_and_carries_only_summaries(tmp_path, isolated_env, kind):
    root = tmp_path / kind
    args = ("plan", "--client-kind", kind, "--config-root", str(root))
    done = _cli(isolated_env, *args)
    assert done.returncode == 0, done.stderr
    payload = _payload(done)
    assert payload["ok"] is True and payload["command"] == "plan"
    assert payload["state"] == "ready" and payload["dry_run"] is True
    assert payload["client_kind"] == kind
    assert payload["config_root"] == str(root)
    assert payload["native_path"] == str(
        client_install.native_config_path(kind, root))
    assert payload["python"] == PYTHON
    assert payload["data_dir"] is None
    assert payload["activation_notes"]
    expected = ([payload["native_path"]]
                + _activation_paths(kind, root, isolated_env)
                + [str(client_install.record_path(root.resolve(), kind))])
    assert [change["path"] for change in payload["changes"]] == expected
    for change in payload["changes"]:
        assert set(change) == {"path", "action", "before_sha256", "after_sha256"}
        assert change["action"] == "create" and change["before_sha256"] is None
        assert change["after_sha256"]
    expected_root = _activation_root(kind, root, isolated_env)
    assert payload["activation_root"] == (
        str(expected_root) if expected_root is not None else None)
    assert "after_text" not in done.stdout
    assert not root.exists()


@pytest.mark.parametrize("kind", ["qoder", "claude"])
def test_json_hosts_merge_and_remove_only_the_owned_entry(tmp_path, isolated_env, kind):
    root = tmp_path / kind
    root.mkdir()
    native = root / ("settings.json" if kind == "qoder" else ".claude.json")
    original = {
        "theme": "dark",
        "mcpServers": {
            "other": {"command": "/usr/bin/other", "args": ["--serve"]},
        },
    }
    native.write_text(json.dumps(original, indent=2) + "\n", encoding="utf-8")

    installed = _cli(isolated_env, "install", "--client-kind", kind,
                     "--config-root", str(root))
    assert installed.returncode == 0, installed.stderr
    payload = _payload(installed)
    assert payload["state"] == "ready"
    actions = {change["path"]: change["action"] for change in payload["changes"]}
    expected_actions = {
        str(native): "update",
        str(client_install.record_path(root.resolve(), kind)): "create",
    }
    for path in _activation_paths(kind, root, isolated_env):
        expected_actions[path] = "create"
    assert actions == expected_actions

    document = _read_json(native)
    assert document["theme"] == "dark"
    assert document["mcpServers"]["other"] == original["mcpServers"]["other"]
    entry = document["mcpServers"]["agentgenome"]
    assert entry["command"] == PYTHON
    assert entry["args"][:2] == ["-m", "node_runtime.control_plane.client_mcp"]
    assert entry["args"][2:4] == ["--client-kind", kind]
    assert entry["env"] == {"PYTHONPATH": str(ROOT)}
    assert ("type" in entry) is (kind == "claude")
    if kind == "claude":
        assert entry["type"] == "stdio"

    record = _read_json(client_install.record_path(root, kind))
    assert record == {
        "format": "agentgenome-install-record/1",
        "client_kind": kind,
        "entry_digest": client_entries.json_digest(entry),
        "block_digest": None,
        "python": PYTHON,
        "data_dir": None,
        "activation_root": str(_activation_root(kind, root, isolated_env)),
        "activation": _activation_digests(kind),
    }
    assert SENTINEL not in native.read_text(encoding="utf-8")

    again = _payload(_cli(isolated_env, "plan", "--client-kind", kind,
                          "--config-root", str(root)))
    assert again["state"] == "unchanged" and again["changes"] == []

    removed = _cli(isolated_env, "uninstall", "--client-kind", kind,
                   "--config-root", str(root))
    assert removed.returncode == 0, removed.stderr
    document = _read_json(native)
    assert "agentgenome" not in document["mcpServers"]
    assert document["mcpServers"]["other"] == original["mcpServers"]["other"]
    assert document["theme"] == "dark"
    assert not client_install.record_path(root, kind).exists()
    assert not any(Path(path).exists()
                   for path in _activation_paths(kind, root, isolated_env))

    empty = _payload(_cli(isolated_env, "uninstall", "--client-kind", kind,
                          "--config-root", str(root)))
    assert empty["state"] == "unchanged" and empty["changes"] == []
    assert _read_json(native)["theme"] == "dark"


def test_codex_toml_entry_is_parseable_and_preserves_unrelated_sections(
        tmp_path, isolated_env):
    root = tmp_path / "codex"
    root.mkdir()
    native = root / "config.toml"
    native.write_text(
        'model = "gpt-5"\n\n'
        "[mcp_servers.other]\n"
        'command = "/usr/bin/other"\n'
        'args = ["--serve"]\n', encoding="utf-8")

    installed = _cli(isolated_env, "install", "--client-kind", "codex",
                     "--config-root", str(root))
    assert installed.returncode == 0, installed.stderr
    text = native.read_text(encoding="utf-8")
    document = tomllib.loads(text)
    assert document["model"] == "gpt-5"
    assert document["mcp_servers"]["other"] == {
        "command": "/usr/bin/other", "args": ["--serve"]}
    owned = document["mcp_servers"]["agentgenome"]
    assert owned == _entry("codex")

    record = _read_json(client_install.record_path(root, "codex"))
    assert record["entry_digest"] == client_entries.json_digest(owned)
    block = text[text.index(client_entries.TOML_BEGIN):
                 text.index(client_entries.TOML_END) + len(
                     client_entries.TOML_END)]
    assert record["block_digest"] == client_entries.sha256_text(block)

    again = _payload(_cli(isolated_env, "plan", "--client-kind", "codex",
                          "--config-root", str(root)))
    assert again["state"] == "unchanged"
    assert native.read_text(encoding="utf-8") == text

    removed = _cli(isolated_env, "uninstall", "--client-kind", "codex",
                   "--config-root", str(root))
    assert removed.returncode == 0, removed.stderr
    document = tomllib.loads(native.read_text(encoding="utf-8"))
    assert document == {
        "model": "gpt-5",
        "mcp_servers": {"other": {"command": "/usr/bin/other",
                                  "args": ["--serve"]}},
    }
    assert not client_install.record_path(root, "codex").exists()


@pytest.mark.parametrize("existing", [
    '[mcp_servers."agentgenome"]\ncommand = "/bin/false"\n',
    'mcp_servers.agentgenome.command = "/bin/false"\n',
    '[mcp_servers]\nagentgenome = { command = "/bin/false", args = [] }\n',
])
def test_codex_semantic_entry_without_record_is_a_no_write_conflict(
        tmp_path, isolated_env, existing):
    root = tmp_path / "codex"
    root.mkdir()
    native = root / "config.toml"
    native.write_text(existing, encoding="utf-8")

    planned = _cli(isolated_env, "plan", "--client-kind", "codex",
                   "--config-root", str(root))
    assert planned.returncode == 3, planned.stdout + planned.stderr
    payload = _payload(planned)
    assert payload["ok"] is False and payload["state"] == "conflict"
    assert payload["conflicts"]

    installed = _cli(isolated_env, "install", "--client-kind", "codex",
                     "--config-root", str(root))
    assert installed.returncode == 3, installed.stdout + installed.stderr
    assert native.read_text(encoding="utf-8") == existing
    assert not client_install.record_path(root, "codex").exists()


def test_codex_user_extended_owned_table_is_never_rewritten_or_removed(
        tmp_path, isolated_env):
    root = tmp_path / "codex"
    installed = _cli(isolated_env, "install", "--client-kind", "codex",
                     "--config-root", str(root))
    assert installed.returncode == 0, installed.stderr
    native = root / "config.toml"
    extended = native.read_text(encoding="utf-8") + 'timeout = 5\n'
    native.write_text(extended, encoding="utf-8")

    for command in ("install", "uninstall"):
        done = _cli(isolated_env, command, "--client-kind", "codex",
                    "--config-root", str(root))
        assert done.returncode == 3, (command, done.stdout, done.stderr)
        assert _payload(done)["state"] == "conflict"
        assert native.read_text(encoding="utf-8") == extended
    assert tomllib.loads(extended)["mcp_servers"]["agentgenome"]["timeout"] == 5
    assert client_install.record_path(root, "codex").is_file()


def test_codex_user_added_subtable_after_the_marker_is_a_conflict(
        tmp_path, isolated_env):
    root = tmp_path / "codex"
    installed = _cli(isolated_env, "install", "--client-kind", "codex",
                     "--config-root", str(root))
    assert installed.returncode == 0, installed.stderr
    native = root / "config.toml"
    extended = (native.read_text(encoding="utf-8")
                + '[mcp_servers.agentgenome.headers]\nX-Trace = "1"\n')
    native.write_text(extended, encoding="utf-8")

    removed = _cli(isolated_env, "uninstall", "--client-kind", "codex",
                   "--config-root", str(root))
    assert removed.returncode == 3, removed.stdout + removed.stderr
    assert native.read_text(encoding="utf-8") == extended


@pytest.mark.parametrize("kind", ["qoder", "claude"])
def test_json_foreign_and_modified_entries_are_never_overwritten(
        tmp_path, isolated_env, kind):
    root = tmp_path / kind
    root.mkdir()
    native = root / ("settings.json" if kind == "qoder" else ".claude.json")
    foreign = {"mcpServers": {"agentgenome": {"command": "/bin/false"}}}
    native.write_text(json.dumps(foreign) + "\n", encoding="utf-8")

    installed = _cli(isolated_env, "install", "--client-kind", kind,
                     "--config-root", str(root))
    assert installed.returncode == 3, installed.stdout + installed.stderr
    assert _read_json(native) == foreign

    clean = tmp_path / f"{kind}-clean"
    assert _cli(isolated_env, "install", "--client-kind", kind,
                "--config-root", str(clean)).returncode == 0
    clean_native = clean / native.name
    edited = _read_json(clean_native)
    edited["mcpServers"]["agentgenome"]["command"] = "/bin/false"
    clean_native.write_text(json.dumps(edited) + "\n", encoding="utf-8")
    edited_text = clean_native.read_text(encoding="utf-8")

    for command in ("install", "uninstall"):
        done = _cli(isolated_env, command, "--client-kind", kind,
                    "--config-root", str(clean))
        assert done.returncode == 3, (command, done.stdout, done.stderr)
        assert clean_native.read_text(encoding="utf-8") == edited_text


@pytest.mark.parametrize("kind,filename", [("qoder", "settings.json"),
                                          ("claude", ".claude.json"),
                                          ("codex", "config.toml")])
def test_sentinel_secret_stays_on_disk_and_out_of_every_output(
        tmp_path, isolated_env, kind, filename):
    root = tmp_path / kind
    root.mkdir()
    native = root / filename
    if kind == "codex":
        native.write_text(
            f'api_key = "{SENTINEL}"\n\n'
            "[mcp_servers.other]\n"
            f'command = "/usr/bin/{SENTINEL}"\n', encoding="utf-8")
    else:
        native.write_text(json.dumps({
            "apiKey": SENTINEL,
            "mcpServers": {"other": {"command": "/usr/bin/other",
                                     "env": {"TOKEN": SENTINEL}}},
        }) + "\n", encoding="utf-8")

    outputs = []
    for command in ("plan", "install", "plan", "uninstall"):
        done = _cli(isolated_env, command, "--client-kind", kind,
                    "--config-root", str(root))
        assert done.returncode == 0, (command, done.stdout, done.stderr)
        outputs.append(done.stdout + done.stderr)
    assert SENTINEL not in "".join(outputs)
    assert "after_text" not in "".join(outputs)

    kept = native.read_text(encoding="utf-8")
    assert SENTINEL in kept
    if kind == "codex":
        assert tomllib.loads(kept)["api_key"] == SENTINEL
        assert "agentgenome" not in tomllib.loads(kept)["mcp_servers"]
    else:
        document = json.loads(kept)
        assert document["apiKey"] == SENTINEL
        assert document["mcpServers"]["other"]["env"]["TOKEN"] == SENTINEL
        assert "agentgenome" not in document["mcpServers"]
    assert not client_install.record_path(root, kind).exists()


def test_python_and_data_dir_are_bound_and_a_data_dir_switch_updates(
        tmp_path, isolated_env):
    root = tmp_path / "qoder"
    data = tmp_path / "service-data"
    interpreter = tmp_path / "venv" / "python"
    native = root / "settings.json"

    installed = _cli(isolated_env, "install", "--client-kind", "qoder",
                     "--config-root", str(root), "--python", str(interpreter),
                     "--data-dir", str(data))
    assert installed.returncode == 0, installed.stderr
    entry = _read_json(native)["mcpServers"]["agentgenome"]
    assert entry == _entry("qoder", python=str(interpreter),
                           data_dir=data.resolve())
    record = _read_json(client_install.record_path(root, "qoder"))
    assert record["python"] == str(interpreter)
    assert record["data_dir"] == str(data.resolve())

    switched = _cli(isolated_env, "install", "--client-kind", "qoder",
                    "--config-root", str(root))
    assert switched.returncode == 0, switched.stderr
    payload = _payload(switched)
    assert payload["state"] == "ready"
    assert payload["changes"][0]["action"] == "update"
    entry = _read_json(native)["mcpServers"]["agentgenome"]
    assert entry == _entry("qoder")
    assert _read_json(
        client_install.record_path(root, "qoder"))["data_dir"] is None


def test_dry_runs_write_nothing_and_conflicts_use_a_stable_nonzero_exit(
        tmp_path, isolated_env):
    root = tmp_path / "pi"
    preview = _cli(isolated_env, "install", "--client-kind", "pi",
                   "--config-root", str(root), "--dry-run")
    assert preview.returncode == 0, preview.stderr
    assert _payload(preview)["dry_run"] is True
    assert not root.exists()

    assert _cli(isolated_env, "install", "--client-kind", "pi",
                "--config-root", str(root)).returncode == 0
    extension = root / "extensions" / "agentgenome.js"
    original = extension.read_text(encoding="utf-8")
    removal = _cli(isolated_env, "uninstall", "--client-kind", "pi",
                   "--config-root", str(root), "--dry-run")
    assert removal.returncode == 0
    assert extension.read_text(encoding="utf-8") == original
    assert client_install.record_path(root, "pi").is_file()

    extension.write_text(original + "// user edit\n", encoding="utf-8")
    blocked = _cli(isolated_env, "uninstall", "--client-kind", "pi",
                   "--config-root", str(root))
    assert blocked.returncode == 3
    assert _payload(blocked)["state"] == "conflict"

    unknown = _cli(isolated_env, "plan", "--client-kind", "script",
                   "--config-root", str(root))
    assert unknown.returncode == 2


def test_two_clients_share_one_root_with_independent_records(
        tmp_path, isolated_env):
    root = tmp_path / "shared"
    codex_activation = tmp_path / "codex-skills"
    for kind in ("qoder", "codex"):
        args = ["install", "--client-kind", kind, "--config-root", str(root)]
        if kind == "codex":
            # The shared config root's skills area is owned by qoder; claiming
            # a distinct native skill root keeps ownership unambiguous.
            args += ["--activation-root", str(codex_activation)]
        installed = _cli(isolated_env, *args)
        assert installed.returncode == 0, installed.stderr
    qoder_record = client_install.record_path(root, "qoder")
    codex_record = client_install.record_path(root, "codex")
    assert qoder_record.is_file() and codex_record.is_file()
    assert _read_json(qoder_record)["client_kind"] == "qoder"
    assert _read_json(codex_record)["client_kind"] == "codex"
    assert _read_json(codex_record)["activation_root"] == str(codex_activation)
    codex_skill = codex_activation / "agentgenome" / "SKILL.md"
    codex_before = codex_skill.read_text(encoding="utf-8")

    removed = _cli(isolated_env, "uninstall", "--client-kind", "qoder",
                   "--config-root", str(root))
    assert removed.returncode == 0, removed.stderr
    assert not qoder_record.exists()
    assert codex_record.is_file()
    assert codex_skill.read_text(encoding="utf-8") == codex_before
    assert "agentgenome" in tomllib.loads(
        (root / "config.toml").read_text(encoding="utf-8"))["mcp_servers"]
    assert "agentgenome" not in _read_json(root / "settings.json")["mcpServers"]

    replayed = _cli(isolated_env, "install", "--client-kind", "qoder",
                    "--config-root", str(root))
    assert replayed.returncode == 0, replayed.stderr
    assert qoder_record.is_file()
    assert client_install.record_path(root, "codex").is_file()
    assert codex_skill.read_text(encoding="utf-8") == codex_before


def test_shared_skill_root_is_a_no_write_conflict_not_shared_ownership(
        tmp_path, isolated_env):
    root = tmp_path / "shared"
    first = _cli(isolated_env, "install", "--client-kind", "qoder",
                 "--config-root", str(root))
    assert first.returncode == 0, first.stderr
    skill = root / "skills" / "agentgenome" / "SKILL.md"
    owned = skill.read_text(encoding="utf-8")
    qoder_native = (root / "settings.json").read_text(encoding="utf-8")

    second = _cli(isolated_env, "install", "--client-kind", "codex",
                  "--config-root", str(root))
    assert second.returncode == 3, second.stdout + second.stderr
    assert _payload(second)["state"] == "conflict"
    assert skill.read_text(encoding="utf-8") == owned
    assert not (root / "config.toml").exists()
    assert not client_install.record_path(root, "codex").exists()
    assert (root / "settings.json").read_text(encoding="utf-8") == qoder_native

    removed = _cli(isolated_env, "uninstall", "--client-kind", "qoder",
                   "--config-root", str(root))
    assert removed.returncode == 0, removed.stderr
    assert not skill.exists()


def test_foreign_identical_activation_file_is_never_claimed_or_deleted(
        tmp_path, isolated_env):
    root = tmp_path / "codex"
    skill = root / "skills" / "agentgenome" / "SKILL.md"
    asset = next(item for item in client_activation.render_activation_assets("codex")
                 if str(item.relative_path) == "agentgenome/SKILL.md")
    skill.parent.mkdir(parents=True)
    skill.write_text(asset.content, encoding="utf-8")
    original = skill.read_text(encoding="utf-8")

    installed = _cli(isolated_env, "install", "--client-kind", "codex",
                     "--config-root", str(root))
    assert installed.returncode == 3, installed.stdout + installed.stderr
    assert _payload(installed)["state"] == "conflict"
    assert skill.read_text(encoding="utf-8") == original
    assert not (root / "config.toml").exists()
    assert not client_install.record_path(root, "codex").exists()

    removed = _cli(isolated_env, "uninstall", "--client-kind", "codex",
                   "--config-root", str(root))
    assert removed.returncode == 0, removed.stderr
    assert skill.read_text(encoding="utf-8") == original


def test_activation_root_is_persisted_and_a_different_root_writes_nothing(
        tmp_path, isolated_env):
    root = tmp_path / "codex"
    recorded = tmp_path / "recorded-skills"
    other = tmp_path / "other-skills"
    installed = _cli(isolated_env, "install", "--client-kind", "codex",
                     "--config-root", str(root),
                     "--activation-root", str(recorded))
    assert installed.returncode == 0, installed.stderr
    assert _read_json(client_install.record_path(
        root, "codex"))["activation_root"] == str(recorded)
    native = root / "config.toml"
    native_before = native.read_text(encoding="utf-8")

    for command in ("plan", "install", "uninstall"):
        done = _cli(isolated_env, command, "--client-kind", "codex",
                    "--config-root", str(root),
                    "--activation-root", str(other))
        assert done.returncode == 3, (command, done.stdout, done.stderr)
        assert _payload(done)["state"] == "conflict"
        assert not other.exists()
        assert native.read_text(encoding="utf-8") == native_before
        assert (recorded / "agentgenome" / "SKILL.md").is_file()
    assert client_install.record_path(root, "codex").is_file()

    removed = _cli(isolated_env, "uninstall", "--client-kind", "codex",
                   "--config-root", str(root))
    assert removed.returncode == 0, removed.stderr
    assert not (recorded / "agentgenome" / "SKILL.md").exists()


def test_qoder_default_root_honours_the_measured_env_override(
        tmp_path, isolated_env):
    # qodercli 1.1.85 reads QODER_CONFIG_DIR exactly like --config-dir, so an
    # implicit install must target that root instead of ~/.qoder.
    qoder_root = tmp_path / "qoder-env-root"
    env = {**isolated_env, "QODER_CONFIG_DIR": str(qoder_root)}
    installed = _cli(env, "install", "--client-kind", "qoder")
    assert installed.returncode == 0, installed.stdout + installed.stderr
    assert Path(_payload(installed)["native_path"]) == qoder_root / "settings.json"
    assert (qoder_root / "settings.json").is_file()
    assert (qoder_root / "skills" / "agentgenome" / "SKILL.md").is_file()
    assert client_install.record_path(qoder_root, "qoder").is_file()


def test_common_record_without_activation_assets_upgrades_additively(
        tmp_path, isolated_env):
    # An accepted U03-common record owns the native entry and no skill assets.
    # Installing the shared activation assets is additive: it must not demand an
    # uninstall/reinstall, and it must not rewrite the owned native entry.
    root = tmp_path / "codex"
    installed = _cli(isolated_env, "install", "--client-kind", "codex",
                     "--config-root", str(root))
    assert installed.returncode == 0, installed.stderr
    record_path = client_install.record_path(root, "codex")
    record = _read_json(record_path)
    native = root / "config.toml"
    native_before = native.read_text(encoding="utf-8")
    skills = root / "skills"
    assert (skills / "agentgenome" / "SKILL.md").is_file()

    # Exact accepted common shape: no activation root and no owned assets.
    record.pop("activation_root")
    record.pop("activation")
    record_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n",
                           encoding="utf-8")
    for path in sorted(skills.rglob("*"), reverse=True):
        if path.is_file():
            path.unlink()
        else:
            path.rmdir()
    skills.rmdir()

    upgraded = _cli(isolated_env, "install", "--client-kind", "codex",
                    "--config-root", str(root))
    assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr
    updated = _read_json(record_path)
    assert updated["activation_root"] == str(skills.resolve())
    assert updated["activation"] == _activation_digests("codex")
    assert native.read_text(encoding="utf-8") == native_before
    assert (skills / "agentgenome" / "SKILL.md").is_file()
    assert (skills / "agentgenome" / "references" / "graph-sop.md").is_file()

    removed = _cli(isolated_env, "uninstall", "--client-kind", "codex",
                   "--config-root", str(root))
    assert removed.returncode == 0, removed.stderr
    assert not (skills / "agentgenome" / "SKILL.md").exists()
    assert not (skills / "agentgenome" / "references" / "graph-sop.md").exists()


def test_codex_activation_note_names_its_measured_native_trigger(
        tmp_path, isolated_env):
    planned = _payload(_cli(isolated_env, "plan", "--client-kind", "codex",
                            "--config-root", str(tmp_path / "codex")))
    assert "Native trigger after reopen: $agentgenome (or /skills)." in (
        planned["activation_notes"])
    assert not any("Native trigger after reopen: /agentgenome"
                   in note for note in planned["activation_notes"])


def test_claude_explicit_config_root_uses_native_override_semantics(
        tmp_path, isolated_env):
    # The installer process deliberately lacks CLAUDE_CONFIG_DIR; launching
    # Claude with CLAUDE_CONFIG_DIR=<root> must discover <root>/skills.
    env = dict(isolated_env)
    env.pop("CLAUDE_CONFIG_DIR", None)
    root = tmp_path / "claude-config"
    planned = _payload(_cli(env, "plan", "--client-kind", "claude",
                            "--config-root", str(root)))
    assert planned["activation_root"] == str(root / "skills")
    assert str(root / "skills" / "agentgenome" / "SKILL.md") in [
        change["path"] for change in planned["changes"]]
    assert planned["native_path"] == str(root / ".claude.json")
    assert not root.exists()


def test_existing_0600_config_stays_private_across_install_update_uninstall(
        tmp_path, isolated_env):
    root = tmp_path / "qoder"
    root.mkdir()
    native = root / "settings.json"
    native.write_text('{"theme": "dark"}\n', encoding="utf-8")
    native.chmod(0o600)
    record = client_install.record_path(root, "qoder")
    data = tmp_path / "data"

    installed = _cli(isolated_env, "install", "--client-kind", "qoder",
                     "--config-root", str(root), "--data-dir", str(data))
    assert installed.returncode == 0, installed.stderr
    assert stat.S_IMODE(native.stat().st_mode) == 0o600
    assert stat.S_IMODE(record.stat().st_mode) == 0o600

    updated = _cli(isolated_env, "install", "--client-kind", "qoder",
                   "--config-root", str(root))
    assert updated.returncode == 0, updated.stderr
    assert stat.S_IMODE(native.stat().st_mode) == 0o600
    assert stat.S_IMODE(record.stat().st_mode) == 0o600

    removed = _cli(isolated_env, "uninstall", "--client-kind", "qoder",
                   "--config-root", str(root))
    assert removed.returncode == 0, removed.stderr
    assert stat.S_IMODE(native.stat().st_mode) == 0o600


def test_new_credential_capable_files_are_created_private(tmp_path, isolated_env):
    root = tmp_path / "pi"
    installed = _cli(isolated_env, "install", "--client-kind", "pi",
                     "--config-root", str(root))
    assert installed.returncode == 0, installed.stderr
    assert stat.S_IMODE(
        (root / "extensions" / "agentgenome.js").stat().st_mode) == 0o600
    assert stat.S_IMODE(
        client_install.record_path(root, "pi").stat().st_mode) == 0o600


@pytest.mark.parametrize("existing", [
    {"mcpServers": {"agentgenome": None}},
    {"mcpServers": None},
    {"mcpServers": []},
])
def test_json_present_null_or_invalid_container_is_a_no_write_conflict(
        tmp_path, isolated_env, existing):
    root = tmp_path / "qoder"
    root.mkdir()
    native = root / "settings.json"
    original = json.dumps(existing, indent=2) + "\n"
    native.write_text(original, encoding="utf-8")

    for command in ("plan", "install", "uninstall"):
        done = _cli(isolated_env, command, "--client-kind", "qoder",
                    "--config-root", str(root))
        assert done.returncode == 3, (command, done.stdout, done.stderr)
        assert _payload(done)["state"] == "conflict"
        assert native.read_text(encoding="utf-8") == original
        assert not client_install.record_path(root, "qoder").exists()


def test_json_entry_nulled_after_install_is_a_no_write_conflict(
        tmp_path, isolated_env):
    root = tmp_path / "qoder"
    installed = _cli(isolated_env, "install", "--client-kind", "qoder",
                     "--config-root", str(root))
    assert installed.returncode == 0, installed.stderr
    native = root / "settings.json"
    document = _read_json(native)
    document["mcpServers"]["agentgenome"] = None
    nulled = json.dumps(document, indent=2) + "\n"
    native.write_text(nulled, encoding="utf-8")

    for command in ("plan", "install", "uninstall"):
        done = _cli(isolated_env, command, "--client-kind", "qoder",
                    "--config-root", str(root))
        assert done.returncode == 3, (command, done.stdout, done.stderr)
        assert native.read_text(encoding="utf-8") == nulled
    assert client_install.record_path(root, "qoder").is_file()


def test_toml_user_table_inside_the_managed_block_is_never_deleted(
        tmp_path, isolated_env):
    root = tmp_path / "codex"
    installed = _cli(isolated_env, "install", "--client-kind", "codex",
                     "--config-root", str(root))
    assert installed.returncode == 0, installed.stderr
    native = root / "config.toml"
    text = native.read_text(encoding="utf-8")
    marker = text.index(client_entries.TOML_END)
    edited = (text[:marker] + "[user_settings]\nkeep_me = true\n\n"
              + text[marker:])
    native.write_text(edited, encoding="utf-8")

    for command in ("plan", "install", "uninstall"):
        done = _cli(isolated_env, command, "--client-kind", "codex",
                    "--config-root", str(root))
        assert done.returncode == 3, (command, done.stdout, done.stderr)
        assert _payload(done)["state"] == "conflict"
        assert native.read_text(encoding="utf-8") == edited
    assert tomllib.loads(edited)["user_settings"]["keep_me"] is True


def test_toml_unrelated_semantics_survive_update_and_uninstall(
        tmp_path, isolated_env):
    root = tmp_path / "codex"
    root.mkdir()
    native = root / "config.toml"
    original = (
        'model = "gpt-5"\n\n'
        "[user_settings]\n"
        "keep_me = true\n\n"
        "[mcp_servers.other]\n"
        'command = "/usr/bin/other"\n')
    native.write_text(original, encoding="utf-8")
    data = tmp_path / "data"

    installed = _cli(isolated_env, "install", "--client-kind", "codex",
                     "--config-root", str(root), "--data-dir", str(data))
    assert installed.returncode == 0, installed.stderr
    document = tomllib.loads(native.read_text(encoding="utf-8"))
    assert document["model"] == "gpt-5"
    assert document["user_settings"] == {"keep_me": True}
    assert document["mcp_servers"]["agentgenome"] == _entry(
        "codex", data_dir=data.resolve())

    updated = _cli(isolated_env, "install", "--client-kind", "codex",
                   "--config-root", str(root))
    assert updated.returncode == 0, updated.stderr
    document = tomllib.loads(native.read_text(encoding="utf-8"))
    assert document["model"] == "gpt-5"
    assert document["user_settings"] == {"keep_me": True}

    removed = _cli(isolated_env, "uninstall", "--client-kind", "codex",
                   "--config-root", str(root))
    assert removed.returncode == 0, removed.stderr
    leftover = native.read_text(encoding="utf-8")
    assert client_entries.TOML_BEGIN not in leftover
    assert tomllib.loads(leftover) == tomllib.loads(original)
