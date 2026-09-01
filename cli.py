"""GraphTree M0 的五命令人机入口。"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from adapters import KimiCLI, LocalShell, TerminalHuman
from builder import CompileError, check, compile
from core import CheckpointError, PortError, Ports, RunStatus, handshake, resume, run

_SKELETON = """version: "gt/1.0"
node:
  id: hello
  mode: sequence
  output:
    greeting: {type: path, compose: [greet.output.greeting]}
  verify:
    - run: "test -s {output.greeting}"
  children:
    - id: greet
      do: {run: "echo hello > {artifact}"}
      output:
        greeting: {type: path}
      verify:
        - run: "test -s {artifact}"
"""


def _error(text: str) -> int:
    print(text, file=sys.stderr)
    return 3


def _run_dir() -> Path:
    base = Path.cwd() / "runs"
    base.mkdir(exist_ok=True)
    while True:
        identifier = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
        directory = base / identifier
        try:
            directory.mkdir()
            return directory
        except FileExistsError:
            continue


def _load_params(path: str | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        with Path(path).open(encoding="utf-8") as stream:
            params = yaml.safe_load(stream)
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"无法读取参数文件：{exc}") from exc
    if not isinstance(params, dict):
        raise ValueError("参数文件必须解析为 YAML 映射")
    try:
        json.dumps(params)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"参数必须可 JSON 序列化：{exc}") from exc
    return params


def _handshake_or_raise(graph: Any, ports: Ports) -> None:
    diagnostics = handshake(graph, ports)
    if diagnostics:
        text = "\n".join(
            f"{item.code} {item.node or '-'}: {item.message}" for item in diagnostics
        )
        raise ValueError(text)


def _ports_for(graph: Any) -> Ports:
    needs = {need for node in _walk(graph.node) for need in node.needs}
    ai = None
    if "llm" in needs:
        executable, model, timeout = _load_kimi_config()
        ai = KimiCLI(executable, model, timeout)
    return Ports(LocalShell(), ai=ai, human=TerminalHuman() if "human" in needs else None)


def _load_kimi_config() -> tuple[str, str | None, int | float]:
    path = Path.home() / ".graphtree" / "config.yaml"
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"AI 配置文件不存在：{path}") from exc
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"无法读取 AI 配置：{exc}") from exc
    if not isinstance(raw, dict) or set(raw) != {"ai"}:
        raise ValueError("配置顶层必须仅包含 ai")
    ai_config = raw["ai"]
    if not isinstance(ai_config, dict):
        raise ValueError("ai 必须为映射")
    unknown_ai = set(ai_config) - {"backend", "kimi"}
    if unknown_ai:
        raise ValueError(f"ai 含未知键：{sorted(unknown_ai)}；合法键为 backend、kimi")
    if "backend" not in ai_config:
        raise ValueError("ai.backend 为必填项")
    if ai_config["backend"] != "kimi":
        raise ValueError("ai.backend 仅支持 kimi")
    kimi = ai_config.get("kimi", {})
    if not isinstance(kimi, dict):
        raise ValueError("ai.kimi 必须为映射")
    unknown_kimi = set(kimi) - {"executable", "model", "timeout_s"}
    if unknown_kimi:
        raise ValueError(
            f"ai.kimi 含未知键：{sorted(unknown_kimi)}；合法键为 executable、model、timeout_s"
        )
    executable = kimi.get("executable", "kimi")
    model = kimi.get("model")
    timeout = kimi.get("timeout_s", 3600)
    if not isinstance(executable, str) or not executable:
        raise ValueError("ai.kimi.executable 必须为非空字符串")
    if model is not None and (not isinstance(model, str) or not model):
        raise ValueError("ai.kimi.model 必须为非空字符串或省略")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValueError("ai.kimi.timeout_s 必须为正数")
    return executable, model, timeout


def _walk(node: Any) -> list[Any]:
    return [node, *[item for child in node.children for item in _walk(child)]]


def _print_result(result: Any) -> int:
    print(f"status: {result.status}")
    print(f"run_id: {result.run_id}")
    print(f"run_dir: {result.run_dir}")
    if result.outputs is not None:
        print(f"outputs: {json.dumps(result.outputs, ensure_ascii=False)}")
    if result.failure is not None:
        print(f"failure: {result.failure['node']} {result.failure['stage']}: {result.failure['summary']}")
    if result.abandonment is not None:
        print(f"abandonment: {json.dumps(result.abandonment, ensure_ascii=False)}")
    return {RunStatus.SUCCEEDED: 0, RunStatus.FAILED: 1, RunStatus.ABANDONED: 2}[result.status]


def command_init(args: argparse.Namespace) -> int:
    target = Path(args.dir)
    if target.exists() and not target.is_dir():
        return _error("init 目标必须是目录")
    target.mkdir(parents=True, exist_ok=True)
    graph = target / "graph.yaml"
    if graph.exists():
        return _error(f"拒绝覆盖已有 graph.yaml：{graph}")
    graph.write_text(_SKELETON, encoding="utf-8")
    print(f"已生成 {graph}")
    return 0


def command_check(args: argparse.Namespace) -> int:
    diagnostics = check(args.pkg_dir)
    if diagnostics:
        for item in diagnostics:
            print(f"{item.code} {item.node or '-'}: {item.message}")
        return 3
    print("check: ok")
    return 0


def command_run(args: argparse.Namespace) -> int:
    try:
        graph = compile(args.pkg_dir)
        params = _load_params(args.params)
        if set(params) != set(graph.params):
            raise ValueError(f"参数键必须恰为 {sorted(graph.params)}，实际为 {sorted(params)}")
        ports = _ports_for(graph)
        _handshake_or_raise(graph, ports)
        result = run(graph, params, ports, _run_dir())
    except (CompileError, CheckpointError, PortError, ValueError, OSError) as exc:
        return _error(str(exc))
    return _print_result(result)


def command_resume(args: argparse.Namespace) -> int:
    directory = Path.cwd() / "runs" / args.run_id
    if not directory.is_dir():
        return _error(f"run 目录不存在：{directory}")
    try:
        graph = compile(args.pkg_dir)
        ports = _ports_for(graph)
        _handshake_or_raise(graph, ports)
        result = resume(graph, ports, directory)
    except (CompileError, CheckpointError, PortError, ValueError, OSError) as exc:
        return _error(str(exc))
    return _print_result(result)


def _read_status_checkpoint(directory: Path) -> dict[str, Any]:
    path = directory / "checkpoint.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"断点损坏：{exc}") from exc
    if not isinstance(raw, dict) or raw.get("ckpt_v") != 1 or not isinstance(raw.get("pin"), str):
        raise ValueError("断点损坏：格式无效")
    if not isinstance(raw.get("params"), dict) or not isinstance(raw.get("nodes"), dict):
        raise ValueError("断点损坏：缺少 params 或 nodes")
    return raw


def _last_run_event(directory: Path, event_name: str) -> dict[str, Any] | None:
    path = directory / "ledger.jsonl"
    if not path.exists():
        return None
    with path.open("rb") as stream:
        stream.seek(0, 2)
        end = stream.tell()
        block = b""
        while end:
            size = min(4096, end)
            end -= size
            stream.seek(end)
            block = stream.read(size) + block
            lines = block.splitlines()
            for line in reversed(lines):
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("event") == event_name:
                    return event
            if len(lines) > 1:
                return None
    return None


def command_status(args: argparse.Namespace) -> int:
    directory = Path.cwd() / "runs" / args.run_id
    if not directory.is_dir():
        return _error(f"run 目录不存在：{directory}")
    checkpoint_path = directory / "checkpoint.json"
    if not checkpoint_path.exists():
        print(f"run_id: {args.run_id}\nstatus: not_started")
        return 0
    try:
        data = _read_status_checkpoint(directory)
    except ValueError as exc:
        return _error(str(exc))
    root_entries = [(path, value) for path, value in data["nodes"].items() if "." not in path]
    state = root_entries[0][1].get("status") if root_entries else None
    state = state if state in {"succeeded", "failed", "abandoned"} else "in_progress"
    print(f"run_id: {args.run_id}")
    print(f"pin: {data['pin']}")
    print(f"status: {state}")
    print(f"params: {json.dumps(data['params'], ensure_ascii=False)}")
    for path, value in sorted(data["nodes"].items()):
        print(f"node: {path} status={value.get('status', 'running')} retries={value.get('retries')}")
    if state == "failed":
        failed = _last_run_event(directory, "run_failed")
        if failed:
            print(f"failure: {failed.get('node')} {failed.get('stage')}: {failed.get('summary')}")
    if state == "abandoned":
        abandoned = _last_run_event(directory, "run_abandoned")
        if abandoned:
            print(f"abandonment: {json.dumps(abandoned.get('abandonment'), ensure_ascii=False)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="graphtree")
    subparsers = parser.add_subparsers(dest="command", required=True)
    init = subparsers.add_parser("init")
    init.add_argument("dir")
    init.set_defaults(handler=command_init)
    check_cmd = subparsers.add_parser("check")
    check_cmd.add_argument("pkg_dir")
    check_cmd.set_defaults(handler=command_check)
    run_cmd = subparsers.add_parser("run")
    run_cmd.add_argument("pkg_dir")
    run_cmd.add_argument("--params")
    run_cmd.set_defaults(handler=command_run)
    resume_cmd = subparsers.add_parser("resume")
    resume_cmd.add_argument("pkg_dir")
    resume_cmd.add_argument("run_id")
    resume_cmd.set_defaults(handler=command_resume)
    status = subparsers.add_parser("status")
    status.add_argument("run_id")
    status.set_defaults(handler=command_status)
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
