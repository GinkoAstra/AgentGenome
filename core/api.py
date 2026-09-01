"""CORE 的公开 run / resume API。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from .checkpoint import Checkpoint, CheckpointError
from .engine import Engine, NodeAbandoned, NodeFailed
from .ledger import Ledger
from .ports import Ports


@dataclass(frozen=True)
class Diagnostic:
    """启动前协议核对的 CORE 侧诊断；与 builder 同形但不跨层导入。"""

    code: str
    node: str | None
    message: str


class RunStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ABANDONED = "abandoned"


@dataclass(frozen=True)
class RunResult:
    status: RunStatus
    run_id: str
    run_dir: Path
    outputs: dict[str, dict[str, Any]] | None = None
    failure: dict[str, Any] | None = None
    abandonment: dict[str, Any] | None = None


def _paths(node: Any) -> set[str]:
    return {node.path, *[path for child in node.children for path in _paths(child)]}


def _nodes(node: Any) -> list[Any]:
    return [node, *[descendant for child in node.children for descendant in _nodes(child)]]


def handshake(graph: Any, ports: Ports) -> list[Diagnostic]:
    """核对已由 builder 归一化的 needs，不在运行期重新推导图结构。"""
    nodes = _nodes(graph.node)

    def first_node(need: str) -> str | None:
        return next(
            (node.path for node in nodes if need in getattr(node, "needs", frozenset())),
            None,
        )

    diagnostics: list[Diagnostic] = []
    for need, port in (("llm", ports.ai), ("human", ports.human)):
        node = first_node(need)
        if node is not None and port is None:
            diagnostics.append(
                Diagnostic("E_NEEDS_PORT_ABSENT", node, f"节点需要 {need} 端口，但未注入")
            )
    human_node = first_node("human")
    if human_node is not None and ports.human is not None:
        capabilities = ports.human.capabilities()
        if not capabilities.get("interactive", False):
            diagnostics.append(
                Diagnostic(
                    "E_NEEDS_NONINTERACTIVE",
                    human_node,
                    "节点需要交互式 human 端口，但端口未声明 interactive: true",
                )
            )
    return diagnostics


def _result_success(graph: Any, run_dir: Path, outputs: dict[str, dict[str, Any]]) -> RunResult:
    return RunResult(RunStatus.SUCCEEDED, run_dir.name, run_dir, outputs=outputs)


def _result_failure(run_dir: Path, failed: NodeFailed) -> RunResult:
    failure = failed.failure
    return RunResult(
        RunStatus.FAILED, run_dir.name, run_dir,
        failure={"node": failure.node, "stage": failure.stage, "cause_seq": failure.cause_seq, "summary": failure.summary},
    )


def _abandonment(graph: Any, run_dir: Path, reason: str, checkpoint: Checkpoint) -> dict[str, Any]:
    completed = [
        {"node": path, "outputs": entry["outputs"]}
        for path, entry in checkpoint.nodes.items() if entry.get("status") == "succeeded"
    ]
    remaining = [
        node.path for node in _nodes(graph.node)
        if checkpoint.entry(node.path) is None or checkpoint.entry(node.path).get("status") != "succeeded"
    ]
    return {
        "completed": completed, "remaining": remaining, "affected": [],
        "reason": reason, "suggestion": [],
    }


def _result_abandoned(graph: Any, run_dir: Path, abandoned: NodeAbandoned, checkpoint: Checkpoint) -> RunResult:
    return RunResult(
        RunStatus.ABANDONED, run_dir.name, run_dir,
        abandonment=_abandonment(graph, run_dir, abandoned.reason, checkpoint),
    )


def run(graph: Any, params: dict[str, Any], ports: Ports, run_dir: str | Path) -> RunResult:
    directory = Path(run_dir)
    checkpoint = Checkpoint.create(directory, graph.pin, params)
    ledger = Ledger(directory)
    ledger.append(
        "run_start",
        pin=graph.pin,
        params=params,
        ledger_v=1,
        ports={
            "ai": ports.ai.capabilities() if ports.ai is not None else None,
            "human": ports.human.capabilities() if ports.human is not None else None,
        },
    )
    engine = Engine(graph, ports, directory, checkpoint, ledger)
    try:
        outputs = engine.execute()
    except NodeAbandoned as abandoned:
        result = _result_abandoned(graph, directory, abandoned, checkpoint)
        ledger.append("run_abandoned", abandonment=result.abandonment)
        return result
    except NodeFailed as failed:
        result = _result_failure(directory, failed)
        ledger.append("run_failed", **result.failure)
        return result
    ledger.append("run_succeeded", outputs=outputs)
    return _result_success(graph, directory, outputs)


def resume(graph: Any, ports: Ports, run_dir: str | Path) -> RunResult:
    directory = Path(run_dir)
    checkpoint = Checkpoint.load(directory, graph.pin, _paths(graph.node))
    root = checkpoint.entry(graph.node.path)
    if root and root.get("status") in {"succeeded", "failed"}:
        raise CheckpointError("run 已是终态，不可 resume")
    ledger = Ledger(directory)
    ledger.append("run_resumed", pin=graph.pin)
    engine = Engine(graph, ports, directory, checkpoint, ledger)
    try:
        outputs = engine.execute()
    except NodeAbandoned as abandoned:
        result = _result_abandoned(graph, directory, abandoned, checkpoint)
        ledger.append("run_abandoned", abandonment=result.abandonment)
        return result
    except NodeFailed as failed:
        result = _result_failure(directory, failed)
        ledger.append("run_failed", **result.failure)
        return result
    ledger.append("run_succeeded", outputs=outputs)
    return _result_success(graph, directory, outputs)
