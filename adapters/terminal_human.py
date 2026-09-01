"""终端 HumanPort 的交互实现。"""

from __future__ import annotations

import sys
from typing import Any, TextIO

from core import Answer, PortError


class TerminalHuman:
    def __init__(
        self,
        input_stream: TextIO | None = None,
        output_stream: TextIO | None = None,
    ) -> None:
        self._input = input_stream or sys.stdin
        self._output = output_stream or sys.stdout
        self._approved: set[tuple[str, str]] = set()

    def ask(self, question: str, evidence: dict[str, Any]) -> Answer:
        if not self._input.isatty():
            raise PortError("human 端口需要交互式终端", kind="refused")
        node = evidence.get("node")
        execution = evidence.get("execution")
        batchable = isinstance(node, str) and isinstance(execution, str)
        key = (node, execution) if batchable else None
        if key is not None and key in self._approved:
            return Answer(True, batch_reused=True)
        print(question, file=self._output)
        print(evidence, file=self._output)
        reply = self._read("批准？[y/N" + ("/a" if batchable else "") + "] ").strip().lower()
        if reply in {"y", "yes"}:
            return Answer(True)
        if batchable and reply in {"a", "all"}:
            assert key is not None
            self._approved.add(key)
            return Answer(True, batch_approval=True)
        feedback = self._read("请填写否决反馈：")
        while not feedback.strip():
            feedback = self._read("否决必须填写反馈：")
        return Answer(False, feedback)

    def capabilities(self) -> dict[str, Any]:
        return {"interactive": self._input.isatty()}

    def _read(self, prompt: str) -> str:
        print(prompt, end="", file=self._output, flush=True)
        value = self._input.readline()
        if value == "":
            raise PortError("human 端口的交互输入已结束", kind="refused")
        return value[:-1] if value.endswith("\n") else value
