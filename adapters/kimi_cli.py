"""Kimi Code CLI 的无状态 AIPort 适配器。"""

from __future__ import annotations

import json
import re
import subprocess
from typing import Any

from core import Candidate, FailDecl, JudgeResult, PortError


def _parse_json_object(raw: str) -> Any:
    """从 print 模式 stdout 中解析单个 JSON 对象。

    先整段解析；失败则剥离 kimi text 模式的 transcript 前缀（行首 "• "）
    并提取首个括号配平的 JSON 对象（真实验收发现：kimi 0.39.1 的 stdout
    带 • 前缀，pi 偶发围栏/前言，整段 json.loads 对两者都太脆）。
    """
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    cleaned = re.sub(r"(?m)^\s*•\s*", "", raw)
    start = cleaned.find("{")
    if start >= 0:
        depth = 0
        in_string = False
        escape = False
        for index in range(start, len(cleaned)):
            char = cleaned[index]
            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(cleaned[start : index + 1])
    raise ValueError("stdout 中不存在可解析的 JSON 对象")


class KimiCLI:
    def __init__(self, executable: str = "kimi", model: str | None = None, timeout_s: float = 3600) -> None:
        if not executable:
            raise ValueError("KimiCLI executable 不可为空")
        if model is not None and not model:
            raise ValueError("KimiCLI model 不可为空字符串")
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or timeout_s <= 0:
            raise ValueError("KimiCLI timeout_s 必须为正数")
        self.executable, self.model, self.timeout_s = executable, model, timeout_s

    def _call(self, prompt: str) -> str:
        command = [self.executable, "-p", prompt] + (["-m", self.model] if self.model else [])
        try:
            result = subprocess.run(command, text=True, capture_output=True, timeout=self.timeout_s, check=False)
        except FileNotFoundError as exc:
            raise PortError(str(exc), kind="unavailable") from exc
        except OSError as exc:
            raise PortError(str(exc), kind="unavailable") from exc
        except subprocess.TimeoutExpired as exc:
            raise PortError(str(exc), kind="timeout") from exc
        return result.stdout

    def work(self, prompt: str) -> str:
        return self._call(prompt)

    def judge(self, criterion: str, context: dict[str, Any]) -> JudgeResult:
        raw = self._call(
            f"{criterion}\n仅返回 JSON: {{\"passed\":bool,\"reason\":str}}\n"
            f"{json.dumps(context, ensure_ascii=False)}"
        )
        try:
            value = _parse_json_object(raw)
            passed, reason = value["passed"], value["reason"]
        except (ValueError, KeyError, TypeError) as exc:
            raise PortError("Kimi judge 输出无效", kind="bad_output") from exc
        if not isinstance(passed, bool) or not isinstance(reason, str) or not reason:
            raise PortError("Kimi judge 输出不符合协议", kind="bad_output")
        return JudgeResult(passed, reason)

    def route(self, failures: list[FailDecl], candidates: list[Candidate]) -> str:
        raw = self._call(
            "根据失败记录选择下一候选。仅返回 JSON: {\"chosen\": \"候选 id\"}\n"
            + json.dumps(
                {
                    "failures": [item.__dict__ for item in failures],
                    "candidates": [item.__dict__ for item in candidates],
                },
                ensure_ascii=False,
            )
        )
        try:
            chosen = _parse_json_object(raw)["chosen"]
        except (ValueError, KeyError, TypeError) as exc:
            raise PortError("Kimi route 输出无效", kind="bad_output") from exc
        if not isinstance(chosen, str) or chosen not in {item.id for item in candidates}:
            raise PortError("Kimi route 输出不符合候选池协议", kind="bad_output")
        return chosen

    def capabilities(self) -> dict[str, Any]:
        result: dict[str, Any] = {"max_context": None, "structured_output": False}
        if self.model: result["model"] = self.model
        return result
