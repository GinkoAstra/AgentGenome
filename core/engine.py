"""M0 sequence 引擎：enter → exit(验收) → retry/report。"""

from __future__ import annotations

import ast
import json
import shlex
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from typing import Protocol

    class _Graph(Protocol):
        node: Any
        package_dir: Path

    Graph = _Graph
    Node = Any
    VerifyRule = Any

from .checkpoint import Checkpoint
from .ledger import Ledger, file_ref
from .ports import Answer, Candidate, FailDecl, JudgeResult, PortError, Ports, ShellResult


@dataclass(frozen=True)
class Failure:
    node: str
    stage: str
    cause_seq: int
    summary: str


class NodeFailed(Exception):
    def __init__(self, failure: Failure) -> None:
        self.failure = failure
        super().__init__(failure.summary)


class NodeAbandoned(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class Engine:
    def __init__(self, graph: Graph, ports: Ports, run_dir: Path, checkpoint: Checkpoint, ledger: Ledger) -> None:
        self.graph = graph
        self.ports = ports
        self.run_dir = run_dir
        self.checkpoint = checkpoint
        self.ledger = ledger
        self.refs: dict[str, dict[str, dict[str, Any]]] = {}
        self.tried: dict[str, FailDecl] = {}
        self.nodes = self._index(graph.node)

    def _index(self, node: Node) -> dict[str, Node]:
        result = {node.path: node}
        for child in node.children:
            result.update(self._index(child))
        return result

    def execute(self) -> dict[str, dict[str, Any]]:
        return self.node(self.graph.node)

    def node(self, node: Node) -> dict[str, dict[str, Any]]:
        entry = self.checkpoint.entry(node.path)
        if entry and entry.get("status") == "succeeded":
            refs = entry["outputs"]
            self.refs[node.path] = refs
            return refs
        if entry and entry.get("status") == "failed":
            raise NodeFailed(Failure(node.path, "do", 0, "该节点已在断点中失败"))
        if entry and entry.get("status") == "abandoned":
            self.checkpoint.reenter_abandoned(node.path)
            entry = self.checkpoint.entry(node.path)

        retries = entry.get("retries", 0) if entry else 0
        attempt = retries + 1
        while True:
            self.ledger.append("node_enter", node=node.path, attempt=attempt)
            try:
                refs = self._attempt(node, attempt)
            except NodeAbandoned as abandoned:
                self.checkpoint.mark_abandoned(node.path, retries)
                self.ledger.append(
                    "node_abandoned", node=node.path, attempt=attempt, reason=abandoned.reason
                )
                raise
            except NodeFailed as failed:
                failure = failed.failure
                if node.on_fail == "abort":
                    raise NodeAbandoned(f"节点声明放弃：{failure.summary}") from failed
                if node.on_fail == "ask":
                    if self._ask_on_fail(node, attempt, failure):
                        attempt += 1
                        continue
                    raise NodeAbandoned("人否决失败处置") from failed
                if node.retry is not None and attempt < node.retry:
                    self.checkpoint.mark_retry(node.path, attempt)
                    self.ledger.append(
                        "node_retry", node=node.path, attempt=attempt,
                        attempts=attempt, max=node.retry,
                    )
                    self.checkpoint.clear_failed_descendants(node.path)
                    retries = attempt
                    attempt = retries + 1
                    continue
                self.checkpoint.mark_failed(node.path, retries)
                self.ledger.append(
                    "node_failed", node=node.path, attempt=attempt,
                    stage=failure.stage, attempts=attempt, cause_seq=failure.cause_seq,
                    summary=failure.summary,
                )
                raise NodeFailed(Failure(node.path, failure.stage, failure.cause_seq, failure.summary)) from failed
            self.checkpoint.mark_succeeded(node.path, retries, refs)
            self.refs[node.path] = refs
            self.ledger.append("node_succeeded", node=node.path, attempt=attempt, outputs=refs)
            return refs

    def _ask_on_fail(self, node: Node, attempt: int, failure: Failure) -> bool:
        human = self.ports.human
        if human is None:
            self._verify_port_error("human", node, attempt, "未注入 human 端口")
        assert human is not None
        declaration = self._failure_declaration(node, failure, attempt)
        detail = {**declaration.__dict__, "cause_seq": failure.cause_seq}
        question = (
            f"节点 {node.path} 在 {failure.stage} 阶段第 {declaration.attempt} 次尝试失败："
            f"{failure.summary}。是否重试？"
        )
        evidence = {"failure": detail}
        started = time.monotonic()
        try:
            answer = human.ask(question, evidence)
            if not isinstance(answer, Answer) or (not answer.approve and not answer.feedback):
                raise PortError("HumanPort.ask 返回不符合协议", kind="bad_output")
        except PortError as exc:
            self._verify_port_error("human", node, attempt, str(exc), exc.kind)
        self.ledger.append(
            "call_human", node=node.path, attempt=attempt,
            question=self.ledger.log_ref("question", question), evidence=evidence,
            approve=answer.approve, feedback=answer.feedback,
            duration_ms=round((time.monotonic() - started) * 1000),
        )
        if not answer.approve:
            raise NodeAbandoned(answer.feedback or "人否决失败处置")
        return True

    def _failure_declaration(
        self, node: Node, failure: Failure, attempt: int | None = None,
    ) -> FailDecl:
        """以同一条 FailDecl 取证链服务 try 选路与 on_fail: ask。"""
        evidence: list[dict[str, Any]] = []
        artifact = self._artifact_path(node)
        if artifact.exists():
            evidence.append(file_ref(self.run_dir, artifact))
        event = self.ledger.event(failure.cause_seq)
        if event:
            evidence.extend(
                ref for key in ("stdout", "stderr") if isinstance((ref := event.get(key)), dict)
            )
        if attempt is None:
            entry = self.checkpoint.entry(node.path) or {}
            attempt = int(entry.get("retries", 0)) + 1
        return FailDecl(node.id, failure.stage, failure.summary, evidence, attempt)

    def _attempt(self, node: Node, attempt: int) -> dict[str, dict[str, Any]]:
        if node.danger:
            self._danger_gate(node, attempt)
        if node.do is not None:
            artifact = self._artifact_path(node)
            if node.do_kind == "llm":
                prompt = self._fill(node.do, node, artifact, quote=False)
                seq = self._ai_work(prompt, node, attempt)
            else:
                command = self._fill(node.do, node, artifact)
                result, seq = self._shell(command, node, attempt, "call_shell")
                if result.rc != 0:
                    raise NodeFailed(Failure(node.path, "do", seq, f"do 命令退出码 {result.rc}"))
            try:
                refs = {key: file_ref(self.run_dir, artifact) for key in node.output}
            except OSError:
                raise NodeFailed(Failure(node.path, "do", seq, "do 未物化声明的 artifact")) from None
        else:
            refs = self._try_children(node) if node.mode == "try" else self._sequence_children(node)

        for index, rule in enumerate(node.verify):
            failure = self._verify(rule, index, node, attempt, refs, artifact if node.do is not None else None)
            if failure:
                raise NodeFailed(failure)
        return refs

    def _sequence_children(self, node: Node) -> dict[str, dict[str, Any]]:
        for child in node.children:
            self.node(child)
        return self._compose(node)

    def _try_children(self, node: Node) -> dict[str, dict[str, Any]]:
        remaining = list(node.children)
        failures: list[FailDecl] = []
        self.tried = {}
        while remaining:
            if node.route == "llm":
                selected = self._route(node, remaining, failures)
                candidate = next(child for child in remaining if child.id == selected)
            else:
                candidate = remaining[0]
            remaining.remove(candidate)
            try:
                return self.node(candidate)
            except NodeFailed as failed:
                declaration = self._failure_declaration(candidate, failed.failure)
                failures.append(declaration)
                self.tried[candidate.id] = declaration
        raise NodeFailed(Failure(node.path, "do", 0, "try 候选池已耗尽"))

    def _route(self, node: Node, remaining: list[Node], failures: list[FailDecl]) -> str:
        ai = self.ports.ai
        if ai is None:
            self._verify_port_error("ai", node, 1, "未注入 ai 端口")
        assert ai is not None
        candidates = [Candidate(child.id, child.route_hint) for child in remaining]
        prompt = "请选择候选 id：" + json.dumps(
            {"failures": [item.__dict__ for item in failures],
             "candidates": [item.__dict__ for item in candidates]},
            ensure_ascii=False,
        )
        started = time.monotonic()
        try:
            chosen = ai.route(failures, candidates)
            if not isinstance(chosen, str) or chosen not in {item.id for item in candidates}:
                raise PortError("AIPort.route 返回池外候选 id", kind="bad_output")
        except PortError as exc:
            self._verify_port_error("ai", node, 1, str(exc), exc.kind)
        self.ledger.append(
            "route", node=node.path, attempt=1,
            failures=[item.__dict__ for item in failures],
            candidates=[{"id": item.id, "hint": item.hint} for item in candidates],
            chosen=chosen, prompt=self.ledger.log_ref("route", prompt),
            duration_ms=round((time.monotonic() - started) * 1000),
        )
        return chosen

    def _danger_gate(self, node: Node, attempt: int) -> None:
        human = self.ports.human
        if human is None:
            self._verify_port_error("human", node, attempt, "未注入 human 端口")
        assert human is not None
        artifact = self._artifact_path(node) if node.do is not None else None
        execution = self._fill(node.do, node, artifact, quote=node.do_kind != "llm") if node.do else None
        evidence = {
            "node": node.path,
            "execution": execution,
            "inputs": {key: self._input_slot(node, key) for key in node.input},
        }
        question = f"节点 {node.path} 声明 danger，即将执行以下动作，是否批准？"
        started = time.monotonic()
        try:
            answer = human.ask(question, evidence)
            if (not isinstance(answer, Answer) or not isinstance(answer.approve, bool)
                    or (not answer.approve and not answer.feedback)):
                raise PortError("HumanPort.ask 返回不符合协议", kind="bad_output")
        except PortError as exc:
            self._verify_port_error("human", node, attempt, str(exc), exc.kind)
        if not answer.batch_reused:
            self.ledger.append(
                "call_human", node=node.path, attempt=attempt,
                question=self.ledger.log_ref("question", question), evidence=evidence,
                approve=answer.approve, feedback=answer.feedback,
                **({"batch_approval": True} if answer.batch_approval else {}),
                duration_ms=round((time.monotonic() - started) * 1000),
            )
        if not answer.approve:
            raise NodeAbandoned(answer.feedback or "人拒绝 danger gate")

    def _ai_work(self, prompt: str, node: Node, attempt: int) -> int:
        ai = self.ports.ai
        if ai is None:
            error = PortError("未注入 ai 端口")
            self.ledger.append(
                "port_error", node=node.path, attempt=attempt,
                port="ai", kind=error.kind, message=str(error),
            )
            raise error
        started = time.monotonic()
        try:
            response = ai.work(prompt)
        except PortError as exc:
            self.ledger.append(
                "port_error", node=node.path, attempt=attempt,
                port="ai", kind=exc.kind, message=str(exc),
            )
            raise
        return self.ledger.append(
            "call_ai",
            node=node.path,
            attempt=attempt,
            prompt=self.ledger.log_ref("prompt", prompt),
            response=self.ledger.log_ref("response", response),
            duration_ms=round((time.monotonic() - started) * 1000),
        )

    def _compose(self, node: Node) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for key, spec in node.output.items():
            # compile 已保证 compose 完备；M0 每键的源引用直接复制。
            source = spec.compose[0]
            try:
                result[key] = self.refs[source.node_path][source.key]
            except KeyError as exc:  # 防御性协议错误，图在运行期不会重新解释 YAML。
                raise RuntimeError(f"compose 来源不存在：{source.node_path}.{source.key}") from exc
        return result

    def _verify(
        self, rule: VerifyRule, index: int, node: Node, attempt: int, refs: dict[str, dict[str, Any]],
        artifact: Path | None,
    ) -> Failure | None:
        if rule.kind == "run":
            command = self._fill(rule.value, node, artifact, refs)
            result, seq = self._shell(command, node, attempt, "check", rule_index=index, rule_type="run")
            if result.rc:
                return Failure(node.path, "verify", seq, f"verify run 规则 {index} 退出码 {result.rc}")
            return None
        if rule.kind == "llm":
            criterion = self._fill(rule.value, node, artifact, refs, quote=False)
            context = self._verify_context(node, refs, artifact)
            ai = self.ports.ai
            if ai is None:
                return self._verify_port_error("ai", node, attempt, "未注入 ai 端口")
            started = time.monotonic()
            try:
                judgment = ai.judge(criterion, context)
                if (not isinstance(judgment, JudgeResult) or not isinstance(judgment.passed, bool)
                        or not isinstance(judgment.reason, str) or not judgment.reason):
                    raise PortError("AIPort.judge 返回不符合协议", kind="bad_output")
            except PortError as exc:
                return self._verify_port_error("ai", node, attempt, str(exc), exc.kind)
            seq = self.ledger.append(
                "check", node=node.path, attempt=attempt, rule_index=index, rule_type="llm",
                verdict="pass" if judgment.passed else "fail",
                criterion=self.ledger.log_ref("criterion", criterion), context=context,
                reason=judgment.reason, duration_ms=round((time.monotonic() - started) * 1000),
            )
            return None if judgment.passed else Failure(node.path, "verify", seq, judgment.reason)
        if rule.kind == "human":
            question = self._fill(rule.value, node, artifact, refs, quote=False)
            evidence = self._verify_context(node, refs, artifact)
            human = self.ports.human
            if human is None:
                return self._verify_port_error("human", node, attempt, "未注入 human 端口")
            started = time.monotonic()
            try:
                answer = human.ask(question, evidence)
                if (not isinstance(answer, Answer) or not isinstance(answer.approve, bool)
                        or (not answer.approve and not answer.feedback)):
                    raise PortError("HumanPort.ask 返回不符合协议", kind="bad_output")
            except PortError as exc:
                return self._verify_port_error("human", node, attempt, str(exc), exc.kind)
            seq = self.ledger.append(
                "check", node=node.path, attempt=attempt, rule_index=index, rule_type="human",
                verdict="pass" if answer.approve else "fail",
                question=self.ledger.log_ref("question", question), evidence=evidence,
                feedback=answer.feedback, duration_ms=round((time.monotonic() - started) * 1000),
            )
            reason = answer.feedback or f"verify human 规则 {index} 被否决"
            return None if answer.approve else Failure(node.path, "verify", seq, reason)
        try:
            bindings = {key: self._typed_value(node.output[key].type, ref) for key, ref in refs.items()}
            result = bool(_evaluate(rule.expression.body, bindings))  # type: ignore[union-attr]
            payload: dict[str, Any] = {
                "rule_index": index, "rule_type": "metric", "verdict": "pass" if result else "fail",
                "expression": rule.value, "bindings": bindings, "result": result,
            }
            seq = self.ledger.append("check", node=node.path, attempt=attempt, **payload)
            if not result:
                return Failure(node.path, "verify", seq, f"verify metric 规则 {index} 为假")
            return None
        except Exception as exc:
            seq = self.ledger.append(
                "check", node=node.path, attempt=attempt, rule_index=index, rule_type="metric",
                verdict="fail", expression=rule.value, bindings={}, result=False, error=str(exc),
            )
            return Failure(node.path, "verify", seq, f"verify metric 规则 {index} 无法求值：{exc}")

    def _verify_context(
        self, node: Node, refs: dict[str, dict[str, Any]], artifact: Path | None
    ) -> dict[str, Any]:
        return {
            "artifact": file_ref(self.run_dir, artifact) if artifact is not None else None,
            "outputs": {
                key: self._typed_value(node.output[key].type, ref) for key, ref in refs.items()
            },
        }

    def _verify_port_error(
        self, port: str, node: Node, attempt: int, message: str, kind: str = "unavailable"
    ) -> Failure:
        seq = self.ledger.append(
            "port_error", node=node.path, attempt=attempt, port=port, kind=kind, message=message
        )
        raise PortError(message, kind=kind)

    def _shell(
        self, command: str, node: Node, attempt: int, event: str, **extra: Any
    ) -> tuple[ShellResult, int]:
        try:
            result = self.ports.shell.execute(command, self.graph.package_dir)
        except PortError as exc:
            self.ledger.append(
                "port_error", node=node.path, attempt=attempt,
                port="shell", kind=exc.kind, message=str(exc),
            )
            raise
        stdout = self.ledger.log_ref("stdout", result.stdout)
        stderr = self.ledger.log_ref("stderr", result.stderr)
        fields = {
            "command": result.command, "rc": result.rc, "stdout": stdout,
            "stderr": stderr, "duration_ms": result.duration_ms, **extra,
        }
        if event == "check":
            fields["verdict"] = "pass" if result.rc == 0 else "fail"
        seq = self.ledger.append(event, node=node.path, attempt=attempt, **fields)
        return result, seq

    def _artifact_path(self, node: Node) -> Path:
        directory = self.run_dir / "artifacts"
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{node.path.replace('.', '--')}.out"

    def _fill(
        self, template: str, node: Node, artifact: Path | None,
        refs: dict[str, dict[str, Any]] | None = None, *, quote: bool = True,
    ) -> str:
        refs = refs or self.refs.get(node.path, {})

        def replacement(match: Any) -> str:
            token = match.group(1)
            if token == "artifact":
                if artifact is None:
                    raise RuntimeError("{artifact} 仅可用于有产物的叶子")
                value: Any = str(artifact.resolve())
            else:
                scope, key = token.split(".", 1)
                if scope == "tried":
                    candidate, field = key.split(".", 1)
                    value = getattr(self.tried[candidate], field)
                elif scope == "input":
                    value = self._input_slot(node, key)
                elif scope == "output":
                    value = str((self.run_dir / refs[key]["path"]).resolve())
                else:
                    raise RuntimeError(f"无效槽位 {token}")
            return shlex.quote(str(value)) if quote else str(value)

        import re
        return re.sub(r"\{(artifact|tried\.[^{}]+|(?:input|output)\.[^{}]+)\}", replacement, template)

    def _input_slot(self, node: Node, key: str) -> Any:
        source = node.input[key].source
        if source.kind == "params":
            return self.checkpoint.params[source.key]
        if source.kind == "ancestor_input":
            return self._input_slot(self.nodes[source.node_path], source.key)
        return str((self.run_dir / self.refs[source.node_path][source.key]["path"]).resolve())

    def _typed_value(self, type_name: str, ref: dict[str, Any]) -> Any:
        path = self.run_dir / ref["path"]
        if type_name == "path":
            return str(path.resolve())
        text = path.read_text(encoding="utf-8")
        if type_name == "text":
            return text
        value = json.loads(text)
        if type_name == "number" and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise ValueError("产物不是 number")
        if type_name == "bool" and not isinstance(value, bool):
            raise ValueError("产物不是 bool")
        return value


def _evaluate(node: ast.AST, bindings: dict[str, Any]) -> Any:
    """执行已由 builder 白名单钉死的 AST，避免运行期 eval。"""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return bindings[node.id]
    if isinstance(node, ast.Attribute):
        base = _evaluate(node.value, bindings)
        if isinstance(base, dict):
            return base[node.attr]
        return getattr(base, node.attr)
    if isinstance(node, ast.Subscript):
        return _evaluate(node.value, bindings)[_evaluate(node.slice, bindings)]
    if isinstance(node, ast.Call):
        return len(_evaluate(node.args[0], bindings))
    if isinstance(node, ast.BoolOp):
        values = [_evaluate(value, bindings) for value in node.values]
        return all(values) if isinstance(node.op, ast.And) else any(values)
    if isinstance(node, ast.UnaryOp):
        value = _evaluate(node.operand, bindings)
        if isinstance(node.op, ast.Not):
            return not value
        return -value if isinstance(node.op, ast.USub) else +value
    if isinstance(node, ast.BinOp):
        left, right = _evaluate(node.left, bindings), _evaluate(node.right, bindings)
        operators = {
            ast.Add: lambda: left + right, ast.Sub: lambda: left - right, ast.Mult: lambda: left * right,
            ast.Div: lambda: left / right, ast.FloorDiv: lambda: left // right, ast.Mod: lambda: left % right,
        }
        return operators[type(node.op)]()
    if isinstance(node, ast.Compare):
        left = _evaluate(node.left, bindings)
        comparisons = {
            ast.Eq: lambda a, b: a == b, ast.NotEq: lambda a, b: a != b, ast.Lt: lambda a, b: a < b,
            ast.LtE: lambda a, b: a <= b, ast.Gt: lambda a, b: a > b, ast.GtE: lambda a, b: a >= b,
            ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b,
            ast.Is: lambda a, b: a is b, ast.IsNot: lambda a, b: a is not b,
        }
        for op, comparator in zip(node.ops, node.comparators):
            right = _evaluate(comparator, bindings)
            if not comparisons[type(op)](left, right):
                return False
            left = right
        return True
    raise ValueError(f"未支持的预编译表达式节点：{type(node).__name__}")
