"""Codex framework: instruction assets for native activation, not a new loop.

The shared installer owns planning, conflicts, permissions and uninstall.
This module only renders data assets. Native adapters determine the host's
verified discovery path and trigger syntax; no installation happens here.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .client_profiles import ENTRY_NAME, InteractiveClientKind
from . import teacher_entry

SKILL_RELATIVE = PurePosixPath(ENTRY_NAME) / "SKILL.md"
GRAPH_SOP_RELATIVE = PurePosixPath(ENTRY_NAME) / "references" / "graph-sop.md"


@dataclass(frozen=True)
class ActivationAsset:
    relative_path: PurePosixPath
    content: str


def _skill_text() -> str:
    return Path(__file__).with_name("client_activation.md").read_text(
        encoding="utf-8")


def _graph_sop_text() -> str:
    preface = (
        "# AgentGenome Graph SOP\n\n"
        "本文件同时包含通用协议参考与可 validate 的 Graph 模板。"
        "下文提到 sop-template.yaml 时，指本文件末尾的模板。\n\n"
    )
    return (preface + teacher_entry.GUIDE + "\n\n"
            + "# Graph SOP 模板\n\n" + teacher_entry.TEMPLATE)


def render_activation_assets(
        client_kind: InteractiveClientKind) -> tuple[ActivationAsset, ...]:
    """Return native skill/reference contents for supported non-Pi clients.

    Reuse client_activation.md and teacher_entry.GUIDE/TEMPLATE. Pi already
    has its native command and embedded guide, so return no duplicate skill.
    Paths are relative to the host adapter's explicit activation root.
    Never read credentials, create files, register a session, or start a model.
    """
    if client_kind == "pi":
        return ()
    if client_kind not in ("codex", "qoder", "claude"):
        raise ValueError(f"unsupported interactive client kind: {client_kind!r}")
    return (
        ActivationAsset(SKILL_RELATIVE, _skill_text()),
        ActivationAsset(GRAPH_SOP_RELATIVE, _graph_sop_text()),
    )
