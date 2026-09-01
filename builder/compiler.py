"""Graph YAML 的机械编译、静态校验与整包 pin 计算。"""

from __future__ import annotations

import ast
import hashlib
import os
import re
from pathlib import Path
from typing import Any

import yaml

from .models import (
    CompileError,
    Diagnostic,
    Graph,
    InputSpec,
    Node,
    OutputRef,
    OutputSpec,
    SourceRef,
    VerifyRule,
    freeze_mapping,
)

_ID = re.compile(r"^[a-z][a-z0-9_-]*$")
_SLOT = re.compile(r"\{(artifact|(?:input|output)\.[^{}]+)\}")
_TRIED_SLOT = re.compile(r"\{tried\.([a-z][a-z0-9_-]*)\.(stage|reason|attempt)\}")
_TYPES = {"text", "json", "path", "number", "bool"}
_NEEDS = {"shell", "llm", "human"}


class _Compiler:
    def __init__(self, package_dir: Path) -> None:
        self.package_dir = package_dir.resolve()
        self.diagnostics: list[Diagnostic] = []
        self.params: set[str] = set()
        self.all_paths: set[str] = set()
        self.all_ids: set[str] = set()

    def error(self, code: str, node: str | None, message: str) -> None:
        self.diagnostics.append(Diagnostic(code, node, message))

    def load(self) -> Any | None:
        path = self.package_dir / "graph.yaml"
        try:
            with path.open(encoding="utf-8") as stream:
                return yaml.safe_load(stream)
        except (OSError, yaml.YAMLError) as exc:
            self.error("E_YAML_PARSE", None, f"无法解析 graph.yaml：{exc}")
            return None

    def compile(self) -> Graph | None:
        data = self.load()
        if data is None:
            return None
        if not isinstance(data, dict) or set(data) != {"version", "node"}:
            self.error("E_TOPLEVEL", None, "顶层必须且只能包含 version 与 node")
            return None
        if data["version"] != "gt/1.0":
            self.error("E_VERSION", None, 'version 必须为 "gt/1.0"')
        if not isinstance(data["node"], dict):
            self.error("E_TOPLEVEL", None, "node 必须是映射")
            return None
        self._collect_ids(data["node"])
        root = self.node(data["node"], "", [], [], {})
        if root is None or self.diagnostics:
            return None
        try:
            pin = package_pin(self.package_dir)
        except ValueError as exc:
            self.error("E_PACKAGE_SYMLINK", None, str(exc))
            return None
        return Graph(root, pin, self.package_dir, frozenset(self.params))

    def _collect_ids(self, raw: dict[str, Any]) -> None:
        """预收集 id，令跨分支/前序违规稳定归到 E_EDGE_SCOPE。"""
        raw_id = raw.get("id")
        if isinstance(raw_id, str):
            self.all_ids.add(raw_id)
        children = raw.get("children", [])
        if isinstance(children, list):
            for child in children:
                if isinstance(child, dict):
                    self._collect_ids(child)

    def node(
        self,
        raw: dict[str, Any],
        parent_path: str,
        ancestors: list[tuple[str, dict[str, Any]]],
        prior_siblings: list[tuple[str, str, dict[str, Any]]],
        sibling_ids: dict[str, int],
    ) -> Node | None:
        raw_id = raw.get("id")
        path = f"{parent_path}.{raw_id}" if parent_path and isinstance(raw_id, str) else str(raw_id or parent_path or "<root>")
        if not isinstance(raw_id, str) or not _ID.fullmatch(raw_id):
            self.error("E_NODE_ID", path, "id 必须匹配 ^[a-z][a-z0-9_-]*$")
        else:
            path = f"{parent_path}.{raw_id}" if parent_path else raw_id
            if raw_id in sibling_ids:
                self.error("E_NODE_ID", path, "同一父节点下 id 必须唯一")
            sibling_ids[raw_id] = sibling_ids.get(raw_id, 0) + 1
            self.all_paths.add(path)
            self.all_ids.add(raw_id)

        for disabled in ("budget",):
            if disabled in raw:
                self.error("E_M0_DISABLED", path, f"{disabled} 在 schema 中合法但 M0 未启用")
        danger = raw.get("danger", False)
        if not isinstance(danger, bool):
            self.error("E_NODE_KIND", path, "danger 必须为 bool")
            danger = False
        if raw.get("mode") == "loop":
            self.error("E_M0_DISABLED", path, f"mode {raw.get('mode')} 合法但 M0 未启用")

        has_do, has_children, has_mode = "do" in raw, "children" in raw, "mode" in raw
        is_leaf = has_do and not has_children and not has_mode
        is_group = not has_do and has_children and has_mode
        if not (is_leaf or is_group):
            self.error("E_NODE_KIND", path, "节点必须恰为叶子 do 或非叶子 children + mode")

        outputs_raw = raw.get("output", {})
        if not isinstance(outputs_raw, dict):
            self.error("E_COMPOSE", path, "output 必须是映射")
            outputs_raw = {}
        inputs_raw = raw.get("input", {})
        if not isinstance(inputs_raw, dict):
            self.error("E_INPUT_FROM", path, "input 必须是映射")
            inputs_raw = {}

        children_raw = raw.get("children", [])
        if has_children and not isinstance(children_raw, list):
            self.error("E_NODE_KIND", path, "children 必须是列表")
            children_raw = []
        if is_group and raw.get("mode") not in {"sequence", "try", "loop"}:
            self.error("E_NODE_KIND", path, "非叶子 mode 必须为 sequence 或 try")
        route = raw.get("route", "static") if raw.get("mode") == "try" else None
        if "route" in raw and raw.get("mode") != "try":
            self.error("E_ROUTE", path, "route 仅可挂在 mode: try 节点")
        elif raw.get("mode") == "try" and route not in {"static", "llm"}:
            self.error("E_ROUTE", path, "try 的 route 必须为 static 或 llm")
        route_hint = raw.get("route_hint", "")
        if not isinstance(route_hint, str):
            self.error("E_ROUTE", path, "route_hint 必须为字符串")
            route_hint = ""

        child_outputs = {
            child.get("id"): child.get("output", {})
            for child in children_raw
            if isinstance(child, dict) and isinstance(child.get("id"), str)
        }
        output_specs = self.outputs(outputs_raw, path, is_group, child_outputs)
        if raw.get("mode") == "try":
            signatures = {
                tuple(sorted((key, spec.get("type")) for key, spec in output.items()
                             if isinstance(key, str) and isinstance(spec, dict)))
                for output in child_outputs.values() if isinstance(output, dict)
            }
            if len(signatures) > 1:
                self.error("E_TRY_CANDIDATE", path, "try 的候选 output 声明必须同构")
        input_specs = self.inputs(inputs_raw, path, ancestors, prior_siblings)
        verifies = self.verifies(raw.get("verify"), path, output_specs)
        command, do_kind = self.command(raw.get("do"), path) if has_do else (None, None)
        retry, on_fail = self.on_fail(raw.get("on_fail"), path)
        needs = self.needs(raw, path)

        children: list[Node] = []
        if isinstance(children_raw, list):
            child_siblings: list[tuple[str, str, dict[str, Any]]] = []
            child_ids: dict[str, int] = {}
            for child_raw in children_raw:
                if not isinstance(child_raw, dict):
                    self.error("E_NODE_KIND", path, "children 的每项必须是节点映射")
                    continue
                child = self.node(
                    child_raw, path, ancestors + [(path, inputs_raw)], child_siblings, child_ids
                )
                if child is not None:
                    children.append(child)
                    child_siblings.append((child.id, child.path, child_raw.get("output", {})))
        if raw.get("mode") == "try":
            self.tried_slots(children_raw, path, route)

        # Slots are checked after declarations have been inspected.
        if command is not None:
            self.slots(command, path, set(input_specs), set(output_specs), allow_artifact=True)
        for rule in verifies:
            if rule.kind in {"run", "llm", "human"}:
                self.slots(rule.value, path, set(input_specs), set(output_specs), allow_artifact=True)
        return Node(
            raw_id if isinstance(raw_id, str) else "",
            path,
            freeze_mapping(input_specs),
            freeze_mapping(output_specs),
            tuple(verifies),
            command,
            do_kind,
            tuple(children),
            raw.get("mode") if is_group else None,
            retry,
            on_fail,
            needs,
            danger,
            route,
            route_hint,
        )

    def tried_slots(self, children: list[Any], path: str, route: Any) -> None:
        for index, child in enumerate(children):
            if not isinstance(child, dict):
                continue
            text = str(child)
            for candidate, _field in _TRIED_SLOT.findall(text):
                prior = {item.get("id") for item in children[:index] if isinstance(item, dict)}
                if route != "static" or candidate not in prior:
                    self.error("E_SLOT", path, "{tried.*} 仅可在 static try 的后序候选引用")

    def needs(self, raw: dict[str, Any], path: str) -> frozenset[str]:
        """在编译期归一化端口需求；CORE 仅消费这个已钉死的结果。"""
        declared_raw = raw.get("needs")
        declared: set[str] = set()
        if declared_raw is not None:
            if not isinstance(declared_raw, list) or any(
                not isinstance(item, str) or item not in _NEEDS for item in declared_raw
            ):
                self.error("E_NEEDS", path, "needs 必须是 shell/llm/human 的子集列表")
            else:
                declared = set(declared_raw)

        inferred: set[str] = set()
        do = raw.get("do")
        if isinstance(do, dict) and len(do) == 1:
            if "run" in do:
                inferred.add("shell")
            elif "llm" in do:
                inferred.add("llm")
        verify = raw.get("verify")
        if isinstance(verify, list):
            for rule in verify:
                if isinstance(rule, dict) and len(rule) == 1:
                    kind = next(iter(rule))
                    if kind == "run":
                        inferred.add("shell")
                    elif kind in {"llm", "human"}:
                        inferred.add(kind)
        if raw.get("danger") is True or raw.get("on_fail") == "ask":
            inferred.add("human")
        if raw.get("mode") == "try" and raw.get("route", "static") == "llm":
            inferred.add("llm")

        missing = inferred - declared
        if declared_raw is not None and missing:
            self.error("E_NEEDS", path, f"needs 声明缺少结构推导需求：{', '.join(sorted(missing))}")
        return frozenset(declared | inferred)

    def outputs(
        self, raw: dict[str, Any], path: str, is_group: bool, child_outputs: dict[str, Any]
    ) -> dict[str, OutputSpec]:
        result: dict[str, OutputSpec] = {}
        for key, spec in raw.items():
            if not isinstance(key, str) or not isinstance(spec, dict) or spec.get("type") not in _TYPES:
                self.error("E_COMPOSE", path, f"输出 {key!r} 必须声明有效 type")
                continue
            compose_raw = spec.get("compose")
            refs: list[OutputRef] = []
            if is_group:
                if not isinstance(compose_raw, list) or not compose_raw:
                    self.error("E_COMPOSE", path, f"非叶子输出 {key} 必须有非空 compose")
                else:
                    for item in compose_raw:
                        parsed = self.compose_ref(item, path, child_outputs)
                        if parsed:
                            refs.append(parsed)
            elif "compose" in spec:
                self.error("E_COMPOSE", path, f"叶子输出 {key} 不可声明 compose")
            result[key] = OutputSpec(spec["type"], tuple(refs))
        return result

    def compose_ref(self, value: Any, path: str, child_outputs: dict[str, Any]) -> OutputRef | None:
        if not isinstance(value, str):
            self.error("E_COMPOSE", path, "compose 项必须是 child.output.key")
            return None
        pieces = value.split(".")
        if (len(pieces) != 3 or pieces[1] != "output" or pieces[0] not in child_outputs
                or not isinstance(child_outputs[pieces[0]], dict)
                or pieces[2] not in child_outputs[pieces[0]]):
            self.error("E_COMPOSE", path, f"无效 compose 来源 {value!r}")
            return None
        return OutputRef(f"{path}.{pieces[0]}", pieces[2])

    def inputs(
        self,
        raw: dict[str, Any],
        path: str,
        ancestors: list[tuple[str, dict[str, Any]]],
        siblings: list[tuple[str, str, dict[str, Any]]],
    ) -> dict[str, InputSpec]:
        result: dict[str, InputSpec] = {}
        for key, spec in raw.items():
            if not isinstance(key, str) or not isinstance(spec, dict) or spec.get("type") not in _TYPES:
                self.error("E_INPUT_FROM", path, f"输入 {key!r} 必须声明有效 type")
                continue
            source = self.resolve_source(spec.get("from"), key, path, ancestors, siblings)
            if source:
                result[key] = InputSpec(spec["type"], source)
        return result

    def resolve_source(
        self, value: Any, key: str, path: str, ancestors: list[tuple[str, dict[str, Any]]],
        siblings: list[tuple[str, str, dict[str, Any]]],
    ) -> SourceRef | None:
        if value is None:
            sibling_matches = [
                SourceRef("sibling_output", key, sibling_path)
                for _, sibling_path, outputs in siblings
                if isinstance(outputs, dict) and key in outputs
            ]
            if len(sibling_matches) == 1:
                return sibling_matches[0]
            if len(sibling_matches) > 1:
                self.error("E_INPUT_FROM", path, f"输入 {key} 的自动兄弟来源不唯一")
                return None
            ancestor_matches = [
                SourceRef("ancestor_input", key, ancestor_path)
                for ancestor_path, inputs in reversed(ancestors)
                if isinstance(inputs, dict) and key in inputs
            ]
            if len(ancestor_matches) == 1:
                return ancestor_matches[0]
            if len(ancestor_matches) > 1:
                self.error("E_INPUT_FROM", path, f"输入 {key} 的自动祖先来源不唯一")
                return None
            self.params.add(key)
            return SourceRef("params", key)
        if not isinstance(value, str):
            self.error("E_INPUT_FROM", path, f"输入 {key} 的 from 必须是字符串")
            return None
        parts = value.split(".")
        if len(parts) == 2 and parts[0] == "params":
            self.params.add(parts[1])
            return SourceRef("params", parts[1])
        if len(parts) == 3 and parts[1] == "output":
            for sibling_id, sibling_path, outputs in siblings:
                if sibling_id == parts[0] and isinstance(outputs, dict) and parts[2] in outputs:
                    return SourceRef("sibling_output", parts[2], sibling_path)
            code = "E_EDGE_SCOPE" if parts[0] in self.all_ids else "E_INPUT_FROM"
            self.error(code, path, f"来源 {value!r} 不是前序兄弟输出")
            return None
        if len(parts) == 3 and parts[1] == "input":
            for ancestor_path, inputs in reversed(ancestors):
                if ancestor_path.rsplit(".", 1)[-1] == parts[0] and isinstance(inputs, dict) and parts[2] in inputs:
                    return SourceRef("ancestor_input", parts[2], ancestor_path)
            code = "E_EDGE_SCOPE" if parts[0] in self.all_ids else "E_INPUT_FROM"
            self.error(code, path, f"来源 {value!r} 不是祖先输入")
            return None
        self.error("E_INPUT_FROM", path, f"无效输入来源 {value!r}")
        return None

    def command(self, raw: Any, path: str) -> tuple[str | None, str | None]:
        if not isinstance(raw, dict) or len(raw) != 1:
            self.error("E_NODE_KIND", path, "do 必须是 {run: <shell 字符串>} 或 {llm: <prompt 模板>}")
            return None, None
        kind, value = next(iter(raw.items()))
        if kind not in {"run", "llm"} or not isinstance(value, str):
            self.error("E_NODE_KIND", path, "do 必须是 {run: <shell 字符串>} 或 {llm: <prompt 模板>}")
            return None, None
        return value, kind

    def on_fail(self, raw: Any, path: str) -> tuple[int | None, str]:
        if raw is None or raw == "report":
            return None, "report"
        if isinstance(raw, str) and raw in {"abort", "ask"}:
            return None, raw
        if isinstance(raw, dict) and set(raw) == {"retry"}:
            number = raw["retry"]
            if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
                self.error("E_RETRY_N", path, "retry 必须为正整数")
                return None, "report"
            return number, "retry"
        if isinstance(raw, (str, dict)):
            self.error("E_M0_DISABLED", path, "on_fail 仅支持 retry/report/abort/ask")
        else:
            self.error("E_RETRY_N", path, "on_fail 必须为 report 或 {retry: N}")
        return None, "report"

    def verifies(self, raw: Any, path: str, outputs: dict[str, OutputSpec]) -> list[VerifyRule]:
        if not isinstance(raw, list) or not raw:
            self.error("E_VERIFY_REQUIRED", path, "verify 必须是非空规则列表")
            return []
        result: list[VerifyRule] = []
        for item in raw:
            if not isinstance(item, dict) or len(item) != 1:
                self.error("E_VERIFY_REQUIRED", path, "每条 verify 必须是单键映射")
                continue
            kind, value = next(iter(item.items()))
            if kind == "run" and isinstance(value, str):
                result.append(VerifyRule("run", value))
            elif kind == "metric" and isinstance(value, str):
                expression = self.metric(value, path, set(outputs))
                if expression:
                    result.append(VerifyRule("metric", value, expression))
            elif kind in {"llm", "human"} and isinstance(value, str):
                result.append(VerifyRule(kind, value))
            else:
                self.error("E_VERIFY_REQUIRED", path, "verify 必须是 run/metric/llm/human 字符串")
        return result

    def metric(self, text: str, path: str, outputs: set[str]) -> ast.Expression | None:
        try:
            parsed = ast.parse(text, mode="eval")
        except SyntaxError as exc:
            self.error("E_METRIC_EXPR", path, f"metric 语法错误：{exc.msg}")
            return None
        valid = True
        for node in ast.walk(parsed):
            if isinstance(node, ast.Name):
                if node.id not in outputs and node.id != "len":
                    self.error("E_METRIC_EXPR", path, f"metric 引用了未声明输出 {node.id}")
                    valid = False
            elif isinstance(node, ast.Call):
                if not (isinstance(node.func, ast.Name) and node.func.id == "len"
                        and len(node.args) == 1 and not node.keywords):
                    self.error("E_METRIC_EXPR", path, "metric 仅允许调用 len(value)")
                    valid = False
            elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
                self.error("E_METRIC_EXPR", path, "metric 禁止 ** 运算")
                valid = False
            elif isinstance(node, (ast.Expression, ast.Load, ast.Name, ast.Attribute, ast.Subscript,
                                  ast.Constant, ast.Compare, ast.BoolOp, ast.BinOp, ast.UnaryOp,
                                  ast.Call, ast.And, ast.Or, ast.Not, ast.USub, ast.UAdd,
                                  ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod,
                                  ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
                                  ast.In, ast.NotIn, ast.Is, ast.IsNot)):
                continue
            else:
                self.error("E_METRIC_EXPR", path, f"metric 不允许 {type(node).__name__}")
                valid = False
        return parsed if valid else None

    def slots(self, text: str, path: str, inputs: set[str], outputs: set[str], allow_artifact: bool) -> None:
        for token in _SLOT.findall(text):
            if token == "artifact" and allow_artifact:
                continue
            parts = token.split(".")
            valid = ((len(parts) == 2 and parts[0] == "input" and parts[1] in inputs)
                     or (len(parts) == 2 and parts[0] == "output" and parts[1] in outputs))
            if not valid:
                self.error("E_SLOT", path, f"槽位 {{{token}}} 未引用已声明键")


def package_pin(package_dir: str | Path) -> str:
    """依照设计文档 §10.2 为图包所有常规文件计算确定性 pin。"""
    root = Path(package_dir)
    if not root.is_dir():
        raise ValueError(f"图包目录不存在：{root}")
    files: list[Path] = []
    for current, dirs, names in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in list(dirs):
            candidate = current_path / name
            if candidate.is_symlink():
                raise ValueError(f"图包内不允许符号链接：{candidate.relative_to(root)}")
            if name == "__pycache__":
                dirs.remove(name)
        for name in names:
            candidate = current_path / name
            if candidate.is_symlink():
                raise ValueError(f"图包内不允许符号链接：{candidate.relative_to(root)}")
            if name.endswith(".pyc") or name == ".DS_Store" or not candidate.is_file():
                continue
            files.append(candidate)
    lines = []
    for file in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
        content_hash = hashlib.sha256(file.read_bytes()).hexdigest()
        lines.append(f"{file.relative_to(root).as_posix()}  {content_hash}\n")
    return "sha256:" + hashlib.sha256("".join(lines).encode()).hexdigest()


def check(package_dir: str | Path) -> list[Diagnostic]:
    compiler = _Compiler(Path(package_dir))
    compiler.compile()
    return compiler.diagnostics


def compile(package_dir: str | Path) -> Graph:
    compiler = _Compiler(Path(package_dir))
    graph = compiler.compile()
    if compiler.diagnostics:
        raise CompileError(compiler.diagnostics)
    assert graph is not None
    return graph
