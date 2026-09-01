"""CORE 消费的端口协议；实现只存在于 adapters。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Protocol


class PortError(RuntimeError):
    """端口不可用等硬错误；必须上抛，不能转换为图失败。"""

    valid_kinds: ClassVar[frozenset[str]] = frozenset(
        {"unavailable", "timeout", "refused", "bad_output"}
    )
    kind: ClassVar[str] = "unavailable"

    def __init__(self, message: str, *, kind: str | None = None) -> None:
        selected_kind = kind or self.kind
        if selected_kind not in self.valid_kinds:
            raise ValueError(f"未知 PortError kind：{selected_kind}")
        self.kind = selected_kind
        super().__init__(message)


class ShellUnavailable(PortError):
    kind = "unavailable"


@dataclass(frozen=True)
class ShellResult:
    command: str
    rc: int
    stdout: str
    stderr: str
    duration_ms: int


class ShellPort(Protocol):
    def execute(self, command: str, cwd: Path) -> ShellResult: ...


@dataclass(frozen=True)
class JudgeResult:
    passed: bool
    reason: str


@dataclass(frozen=True)
class Answer:
    approve: bool
    feedback: str | None = None
    batch_approval: bool = False
    batch_reused: bool = False

    def __post_init__(self) -> None:
        if not self.approve and not self.feedback:
            raise ValueError("否决必须提供非空 feedback")
        if self.batch_reused and not self.approve:
            raise ValueError("批量批准复用只能返回批准")


@dataclass(frozen=True)
class FailDecl:
    candidate: str
    stage: str
    reason: str
    evidence: list[dict[str, Any]]
    attempt: int


@dataclass(frozen=True)
class Candidate:
    id: str
    hint: str


class AIPort(Protocol):
    def work(self, prompt: str) -> str: ...

    def judge(self, criterion: str, context: dict[str, Any]) -> JudgeResult: ...

    def route(self, failures: list[FailDecl], candidates: list[Candidate]) -> str: ...

    def capabilities(self) -> dict[str, Any]: ...


class HumanPort(Protocol):
    def ask(self, question: str, evidence: dict[str, Any]) -> Answer: ...

    def capabilities(self) -> dict[str, Any]: ...


@dataclass(frozen=True)
class Ports:
    shell: ShellPort
    ai: AIPort | None = None
    human: HumanPort | None = None
