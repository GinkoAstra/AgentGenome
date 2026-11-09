"""Thin native entry adapters used by the shared install engine.

Each adapter renders exactly one owned entry in one native file and reports a
digest of that entry. It never discovers a service, imports an execution
Backend, calls a model, or writes anything: planning is pure text in/out.

Native shapes verified by isolated probes (temporary HOME, explicit config
root, temporary cwd, explicit scope):
  pi      extensions/agentgenome.js extension wrapper
  codex   [mcp_servers.agentgenome] table in config.toml
  qoder   mcpServers.agentgenome in settings.json
  claude  mcpServers.agentgenome in .claude.json (stdio entry)
"""
from __future__ import annotations

import hashlib
import json
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .client_profiles import (ENTRY_NAME, InteractiveClientKind,
                              native_config_path, profile_for)

PI_OWNER_MARKER = "agentgenome-pi-integration/1"
PI_COMMAND = Path(__file__).with_name("backends") / "pi_command.mjs"
PACKAGE_ROOT = Path(__file__).resolve().parents[2]
TOML_BEGIN = "# >>> agentgenome managed entry >>>"
TOML_END = "# <<< agentgenome managed entry <<<"
_ABSENT = object()


@dataclass(frozen=True)
class NativePlan:
    path: Path
    before_sha256: str | None
    content: str | None  # desired text; None deletes an owned file
    changed: bool
    entry_digest: str | None
    notes: tuple[str, ...]
    conflicts: tuple[str, ...] = ()
    # TOML only: the exact managed-block digest recorded on install.
    block_digest: str | None = None


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def json_digest(value: object) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    return sha256_text(canonical)


def read_managed_text(path: Path) -> tuple[str | None, str | None, str | None]:
    """Return (sha256, text, error) without following a symlink.

    ``(None, None, None)`` means the path is absent. Refusing symlinks keeps an
    atomic replace from silently replacing the link instead of its target.
    """
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None, None, None
    if stat.S_ISLNK(info.st_mode):
        return None, None, f"refusing to manage a symbolic link: {path}"
    if not stat.S_ISREG(info.st_mode):
        return None, None, f"managed path is not a regular file: {path}"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        return None, None, f"managed file is not readable UTF-8 text: {path} ({error})"
    return sha256_text(text), text, None


def _notes(kind: InteractiveClientKind, path: Path) -> tuple[str, ...]:
    if kind == "pi":
        return (
            f"Owned Pi extension: {path}. Open a new Pi session so the "
            "extension is discovered, then run /agentgenome in that window.",
            "/agentgenome off detaches only the current window; it does not "
            "stop the service or a run.",
        )
    host = {"codex": "Codex", "qoder": "qodercli", "claude": "Claude Code"}[kind]
    return (
        f"Owned native MCP server entry: {path}.",
        f"{host} reads this entry when a session starts; a new window may be "
        "required for it to appear.",
        "The shared installer adds an agentgenome skill; the Graph operations "
        "are also exposed as native MCP tools.",
        "One bridge process registers one activation UUID; a host that shares "
        "one MCP process across windows shows one connection, not one per "
        "window.",
    )


def _conflict_plan(kind: InteractiveClientKind, path: Path, sha: str | None,
                   conflicts: tuple[str, ...], digest: str | None = None,
                   content: str | None = None) -> NativePlan:
    return NativePlan(path=path, before_sha256=sha, content=content,
                      changed=False, entry_digest=digest,
                      notes=_notes(kind, path), conflicts=conflicts)


# ---------------------------------------------------------------------------
# Pi extension wrapper


def _pi_bootstrap(target) -> str:
    if not PI_COMMAND.is_file():
        raise RuntimeError(f"shipped pi_command.mjs is missing: {PI_COMMAND}")
    options: dict[str, object] = {
        "format": "agentgenome-pi-command/1",
        "python": target.python,
    }
    if target.data_dir is not None:
        options["data_dir"] = str(target.data_dir)
    encoded = json.dumps(options, ensure_ascii=False, indent=2, sort_keys=True)
    return (
        f"// {PI_OWNER_MARKER} managed by node_runtime.control_plane; "
        "do not edit.\n"
        "import { truncateToWidth, visibleWidth, wrapTextWithAnsi } from "
        "\"@earendil-works/pi-tui\";\n"
        f"import createExtension from {json.dumps(str(PI_COMMAND.resolve()))};\n\n"
        "export default (pi) => createExtension(pi, {\n"
        f"  ...{encoded},\n"
        "  helpers: { truncateToWidth, visibleWidth, wrapTextWithAnsi },\n"
        "});\n"
    )


def _plan_pi(target, record: dict | None, *, remove: bool) -> NativePlan:
    record_digest = None if record is None else record["entry_digest"]
    path = native_config_path("pi", target.config_root)
    sha, text, error = read_managed_text(path)
    if error is not None:
        return _conflict_plan("pi", path, sha, (error,))
    if remove:
        if text is None:
            return NativePlan(path, sha, None, False, None, _notes("pi", path))
        if (record_digest is None or record_digest != sha
                or PI_OWNER_MARKER not in text):
            return _conflict_plan(
                "pi", path, sha,
                ("the Pi extension is foreign, user-modified or a legacy "
                 "install; refusing to remove it. Remove it deliberately, "
                 "then reinstall.",))
        return NativePlan(path, sha, None, True, None, _notes("pi", path))
    content = _pi_bootstrap(target)
    digest = sha256_text(content)
    if text is not None:
        if (record_digest is None or record_digest != sha
                or PI_OWNER_MARKER not in text):
            return _conflict_plan(
                "pi", path, sha,
                ("the Pi extension is foreign, user-modified or a legacy "
                 "install; refusing to overwrite it. Remove it deliberately, "
                 "then reinstall.",), digest=digest, content=content)
    return NativePlan(path, sha, content, text != content, digest,
                      _notes("pi", path))


# ---------------------------------------------------------------------------
# MCP server entries


def _mcp_entry(target, *, claude: bool) -> dict:
    args = ["-m", "node_runtime.control_plane.client_mcp",
            "--client-kind", target.client_kind]
    if target.data_dir is not None:
        args += ["--data-dir", str(target.data_dir)]
    entry: dict[str, object] = {
        "command": target.python,
        "args": args,
        "env": {"PYTHONPATH": str(PACKAGE_ROOT)},
    }
    if claude:
        entry["type"] = "stdio"
    return entry


def _json_container(document: dict, path: Path) -> tuple[dict | None, tuple[str, ...]]:
    container = document.get("mcpServers", _ABSENT)
    if container is _ABSENT:
        container = {}
        document["mcpServers"] = container
    if not isinstance(container, dict):
        return None, (f"mcpServers in {path} is not an object",)
    return container, ()


def _plan_json(target, record: dict | None, *, remove: bool) -> NativePlan:
    kind = target.client_kind
    record_digest = None if record is None else record["entry_digest"]
    path = native_config_path(kind, target.config_root)
    sha, text, error = read_managed_text(path)
    if error is not None:
        return _conflict_plan(kind, path, sha, (error,))
    if text is None:
        document: dict = {}
    else:
        try:
            document = json.loads(text)
        except ValueError:
            return _conflict_plan(
                kind, path, sha,
                (f"{path} is not valid JSON; refusing to edit it",))
        if not isinstance(document, dict):
            return _conflict_plan(
                kind, path, sha,
                (f"{path} does not contain a JSON object",))
    container, conflict = _json_container(document, path)
    if conflict:
        return _conflict_plan(kind, path, sha, conflict)
    assert container is not None
    current = container.get(ENTRY_NAME, _ABSENT)
    current_digest = None if current is _ABSENT else json_digest(current)

    if remove:
        if current is _ABSENT:
            return NativePlan(path, sha, text, False, None, _notes(kind, path))
        if record_digest is None:
            return _conflict_plan(
                kind, path, sha,
                (f"mcpServers.{ENTRY_NAME} exists in {path} but is not owned "
                 "by this installer; refusing to remove it.",))
        if current_digest != record_digest:
            return _conflict_plan(
                kind, path, sha,
                (f"mcpServers.{ENTRY_NAME} in {path} was modified after "
                 "installation; refusing to remove it.",))
        del container[ENTRY_NAME]
        new_text = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
        return NativePlan(path, sha, new_text, new_text != text, None,
                          _notes(kind, path))

    entry = _mcp_entry(target, claude=kind == "claude")
    digest = json_digest(entry)
    if current is not _ABSENT:
        if record_digest is None:
            return _conflict_plan(
                kind, path, sha,
                (f"mcpServers.{ENTRY_NAME} already exists in {path} but is not "
                 "owned by this installer; refusing to overwrite it.",),
                digest=digest)
        if current_digest != record_digest:
            return _conflict_plan(
                kind, path, sha,
                (f"mcpServers.{ENTRY_NAME} in {path} was modified after "
                 "installation; refusing to overwrite it.",), digest=digest)
        if current_digest == digest:
            return NativePlan(path, sha, text, False, digest,
                              _notes(kind, path))
    container[ENTRY_NAME] = entry
    new_text = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    return NativePlan(path, sha, new_text, new_text != text, digest,
                      _notes(kind, path))


# ---------------------------------------------------------------------------
# Codex TOML


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_block(entry: dict) -> str:
    args = ", ".join(_toml_string(item) for item in entry["args"])
    environment = "{" + ", ".join(
        f"{_toml_string(key)} = {_toml_string(value)}"
        for key, value in entry["env"].items()) + "}"
    return "\n".join((
        TOML_BEGIN,
        f"[mcp_servers.{ENTRY_NAME}]",
        f"command = {_toml_string(entry['command'])}",
        f"args = [{args}]",
        f"env = {environment}",
        TOML_END,
    ))


def _block_span(text: str) -> tuple[int, int] | None:
    start = text.find(TOML_BEGIN)
    if start < 0:
        return None
    end = text.find(TOML_END, start)
    if end < 0:
        return None
    return start, end + len(TOML_END)


def _toml_document(text: str) -> dict | None:
    if not text.strip():
        return {}
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None
    return document


def _toml_table(document: dict) -> dict | None | object:
    """The semantic mcp_servers.agentgenome table, or _ABSENT when undefined.

    tomllib resolves every legal spelling (quoted headers, dotted keys, inline
    tables), so ownership is decided by meaning instead of a text pattern.
    """
    servers = document.get("mcp_servers", _ABSENT)
    if servers is _ABSENT:
        return _ABSENT
    if not isinstance(servers, dict):
        raise ValueError("mcp_servers is not a TOML table")
    table = servers.get(ENTRY_NAME, _ABSENT)
    if table is _ABSENT:
        return _ABSENT
    if not isinstance(table, dict):
        raise ValueError(f"mcp_servers.{ENTRY_NAME} is not a TOML table")
    return table


def _without_entry(document: dict) -> dict:
    """Parsed semantics with only the owned table removed."""
    clone = dict(document)
    servers = clone.get("mcp_servers", _ABSENT)
    if isinstance(servers, dict):
        remaining = {key: value for key, value in servers.items()
                     if key != ENTRY_NAME}
        if remaining:
            clone["mcp_servers"] = remaining
        else:
            clone.pop("mcp_servers")
    return clone


def _validate_toml(text: str, expected: dict | object,
                   unrelated: dict | None = None) -> bool:
    """True when ``text`` parses, owns exactly ``expected``, and leaves every
    other parsed section identical to ``unrelated``."""
    document = _toml_document(text)
    if document is None:
        return False
    try:
        actual = _toml_table(document)
    except ValueError:
        return False
    if expected is _ABSENT:
        if actual is not _ABSENT:
            return False
    elif actual != expected:
        return False
    if unrelated is not None and _without_entry(document) != _without_entry(
            unrelated):
        return False
    return True


def _plan_toml(target, record: dict | None, *, remove: bool) -> NativePlan:
    kind = "codex"
    entry_digest = None if record is None else record["entry_digest"]
    block_digest = None if record is None else record.get("block_digest")
    path = native_config_path(kind, target.config_root)
    sha, text, error = read_managed_text(path)
    if error is not None:
        return _conflict_plan(kind, path, sha, (error,))
    current_text = text or ""
    document = _toml_document(current_text)
    if document is None:
        return _conflict_plan(
            kind, path, sha,
            (f"{path} is not valid TOML; refusing to edit it",))
    try:
        table = _toml_table(document)
    except ValueError as problem:
        return _conflict_plan(kind, path, sha, (f"{path}: {problem}",))
    span = _block_span(current_text)
    block = current_text[span[0]:span[1]] if span is not None else None
    block_sha = sha256_text(block) if block is not None else None
    table_digest = None if table is _ABSENT else json_digest(table)

    if remove:
        if table is _ABSENT:
            if span is not None:
                return _conflict_plan(
                    kind, path, sha,
                    ("an agentgenome ownership marker exists in "
                     f"{path} but its table is gone; refusing to edit it",))
            return NativePlan(path, sha, text, False, None, _notes(kind, path))
        if entry_digest is None:
            return _conflict_plan(
                kind, path, sha,
                (f"mcp_servers.{ENTRY_NAME} exists in {path} but is not owned "
                 "by this installer; refusing to remove it.",))
        if table_digest != entry_digest:
            return _conflict_plan(
                kind, path, sha,
                (f"mcp_servers.{ENTRY_NAME} in {path} was modified after "
                 "installation; refusing to remove it.",))
        if span is None:
            return _conflict_plan(
                kind, path, sha,
                (f"the agentgenome entry in {path} lost its ownership marker; "
                 "refusing to remove it.",))
        if block_sha != block_digest:
            return _conflict_plan(
                kind, path, sha,
                (f"the agentgenome block in {path} was edited after "
                 "installation; refusing to remove it.",))
        new_text = (current_text[:span[0]] + current_text[span[1]:]).lstrip("\n")
        if not _validate_toml(new_text, _ABSENT, unrelated=document):
            return _conflict_plan(
                kind, path, sha,
                (f"removing the agentgenome entry from {path} would change "
                 "other TOML semantics; refusing to remove it.",))
        return NativePlan(path, sha, new_text, new_text != text, None,
                          _notes(kind, path))

    entry = _mcp_entry(target, claude=False)
    digest = json_digest(entry)
    new_block = _toml_block(entry)
    new_block_sha = sha256_text(new_block)
    if table is not _ABSENT:
        if span is None:
            return _conflict_plan(
                kind, path, sha,
                (f"mcp_servers.{ENTRY_NAME} already exists in {path} without "
                 "an agentgenome ownership marker; refusing to overwrite it.",),
                digest=digest)
        if entry_digest is None:
            return _conflict_plan(
                kind, path, sha,
                (f"an agentgenome marker exists in {path} but has no install "
                 "record; refusing to overwrite it.",), digest=digest)
        if table_digest != entry_digest:
            return _conflict_plan(
                kind, path, sha,
                (f"mcp_servers.{ENTRY_NAME} in {path} was modified after "
                 "installation; refusing to overwrite it.",), digest=digest)
        if block_sha != block_digest:
            return _conflict_plan(
                kind, path, sha,
                (f"the agentgenome block in {path} was edited after "
                 "installation; refusing to overwrite it.",), digest=digest)
        if table_digest == digest:
            # Semantically current; leave spacing and comments untouched.
            return NativePlan(path, sha, text, False, digest,
                              _notes(kind, path), block_digest=block_digest)
        new_text = (current_text[:span[0]] + new_block
                    + current_text[span[1]:])
    else:
        if span is not None:
            return _conflict_plan(
                kind, path, sha,
                (f"an agentgenome ownership marker exists in {path} but its "
                 "table is gone; refusing to overwrite it.",), digest=digest)
        prefix = current_text
        if prefix and not prefix.endswith("\n"):
            prefix += "\n"
        if prefix:
            prefix += "\n"
        new_text = prefix + new_block + "\n"
    if not _validate_toml(new_text, entry, unrelated=document):
        return _conflict_plan(
            kind, path, sha,
            (f"the generated Codex entry for {path} is not accepted by the "
             "TOML parser; refusing to write it.",), digest=digest)
    return NativePlan(path, sha, new_text, new_text != text, digest,
                      _notes(kind, path), block_digest=new_block_sha)


def plan_install(target, record: dict | None) -> NativePlan:
    kind = target.client_kind
    profile_for(kind)
    if kind == "pi":
        return _plan_pi(target, record, remove=False)
    if kind == "codex":
        return _plan_toml(target, record, remove=False)
    return _plan_json(target, record, remove=False)


def plan_uninstall(target, record: dict | None) -> NativePlan:
    kind = target.client_kind
    profile_for(kind)
    if kind == "pi":
        return _plan_pi(target, record, remove=True)
    if kind == "codex":
        return _plan_toml(target, record, remove=True)
    return _plan_json(target, record, remove=True)
