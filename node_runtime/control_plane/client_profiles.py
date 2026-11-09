"""Client entry profiles; never import execution Backends to discover a CLI.

A profile records only what was inspected from the installed native CLI: the
executable name, the user-level config file it reads, and the environment
override it documents. It does not claim hot-loading, a slash command, or a
tool-approval behaviour that was not measured on the host.

Native roots (verified 2026-11-09 against installed help/isolated probes):
  pi      PI_CODING_AGENT_DIR, else ~/.pi/agent  (extensions/agentgenome.js)
  codex   CODEX_HOME, else ~/.codex              (config.toml, mcp_servers)
  qoder   QODER_CONFIG_DIR, else ~/.qoder        (settings.json, mcpServers;
                                                   --config-dir per run)
  claude  CLAUDE_CONFIG_DIR, else ~               (.claude.json, mcpServers)

Script is an API client, not an installable interactive CLI. Kimi is not
implemented and stays capability_unsupported.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

InteractiveClientKind = Literal["pi", "codex", "qoder", "claude"]

ENTRY_NAME = "agentgenome"


@dataclass(frozen=True)
class ClientProfile:
    kind: InteractiveClientKind
    executable: str
    native_format: Literal["pi-extension", "toml-mcp", "json-mcp"]
    # Directory holding the managed native file. The file name is native to the
    # client and is never shared between hosts.
    root_source: str


CLIENT_PROFILES: tuple[ClientProfile, ...] = (
    ClientProfile("pi", "pi", "pi-extension", "PI_CODING_AGENT_DIR/~/.pi/agent"),
    ClientProfile("codex", "codex", "toml-mcp", "CODEX_HOME/~/.codex"),
    ClientProfile("qoder", "qodercli", "json-mcp",
                  "QODER_CONFIG_DIR/~/.qoder"),
    ClientProfile("claude", "claude", "json-mcp", "CLAUDE_CONFIG_DIR/~"),
)

_BY_KIND = {profile.kind: profile for profile in CLIENT_PROFILES}


def profile_for(kind: InteractiveClientKind) -> ClientProfile:
    try:
        return _BY_KIND[kind]
    except KeyError:
        raise ValueError(f"unsupported interactive client kind: {kind!r}") from None


def default_config_root(kind: InteractiveClientKind) -> Path:
    """The inspected user-level root, honouring only documented overrides."""
    profile_for(kind)
    if kind == "pi":
        override = os.environ.get("PI_CODING_AGENT_DIR")
        return Path(override).expanduser() if override else Path.home() / ".pi" / "agent"
    if kind == "codex":
        override = os.environ.get("CODEX_HOME")
        return Path(override).expanduser() if override else Path.home() / ".codex"
    if kind == "qoder":
        # Measured on qodercli 1.1.85 with an isolated sentinel root: both
        # QODER_CONFIG_DIR and --config-dir redirect settings and skills.
        override = os.environ.get("QODER_CONFIG_DIR")
        return (Path(override).expanduser() if override
                else Path.home() / ".qoder")
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(override).expanduser() if override else Path.home()


def native_config_path(kind: InteractiveClientKind, config_root: str | Path) -> Path:
    """The exact native file this installer may own one entry inside."""
    root = Path(config_root).expanduser()
    profile_for(kind)
    if kind == "pi":
        return root / "extensions" / f"{ENTRY_NAME}.js"
    if kind == "codex":
        return root / "config.toml"
    if kind == "qoder":
        return root / "settings.json"
    return root / ".claude.json"


def default_activation_root(kind: InteractiveClientKind, config_root: str | Path,
                            *, explicit: bool = False) -> Path | None:
    """Native skill discovery root, or None when the host needs no skill.

    Verified 2026-11-09 with isolated native sessions:
      codex   $CODEX_HOME/skills
      qoder   $QODER_CONFIG_DIR/skills or <--config-dir>/skills
      claude  $CLAUDE_CONFIG_DIR/skills; otherwise ~/.claude/skills

    ``explicit`` means the caller supplied the native config root itself (for
    Claude: launch with ``CLAUDE_CONFIG_DIR=<root>``), so the skill root is
    derived from that native root instead of the installer process environment.
    Pi already has /agentgenome and receives no duplicate skill.
    """
    profile_for(kind)
    root = Path(config_root).expanduser()
    if kind == "pi":
        return None
    if kind == "claude" and not explicit and not os.environ.get("CLAUDE_CONFIG_DIR"):
        return root / ".claude" / "skills"
    return root / "skills"
