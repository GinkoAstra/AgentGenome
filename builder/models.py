"""编译产物的数据模型。"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


def freeze_mapping(values: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(values))


@dataclass(frozen=True)
class Diagnostic:
    code: str
    node: str | None
    message: str


class CompileError(Exception):
    def __init__(self, diagnostics: list[Diagnostic]):
        self.diagnostics = diagnostics
        super().__init__("\n".join(
            f"{item.code} {item.node or '-'}: {item.message}" for item in diagnostics
        ))


@dataclass(frozen=True)
class SourceRef:
    kind: str  # params | ancestor_input | sibling_output
    key: str
    node_path: str | None = None


@dataclass(frozen=True)
class InputSpec:
    type: str
    source: SourceRef


@dataclass(frozen=True)
class OutputRef:
    node_path: str
    key: str


@dataclass(frozen=True)
class OutputSpec:
    type: str
    compose: tuple[OutputRef, ...] = ()


@dataclass(frozen=True)
class VerifyRule:
    kind: str  # run | metric
    value: str
    expression: ast.Expression | None = None


@dataclass(frozen=True)
class Node:
    id: str
    path: str
    input: Mapping[str, InputSpec]
    output: Mapping[str, OutputSpec]
    verify: tuple[VerifyRule, ...]
    do: str | None
    do_kind: str | None
    children: tuple["Node", ...]
    mode: str | None
    retry: int | None
    on_fail: str
    needs: frozenset[str]
    danger: bool
    route: str | None
    route_hint: str


@dataclass(frozen=True)
class Graph:
    node: Node
    pin: str
    package_dir: Path
    params: frozenset[str]
