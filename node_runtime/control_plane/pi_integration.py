"""Install the ordinary-Pi command entry and prepare its existing control plane.

Only this integration owns bootstrap files. Runtime semantics remain in Host,
Teacher and MCP. No model credentials belong in generated bootstrap files.

Commands:
    install    write <agent-dir>/extensions/agentgenome.js for this package
    uninstall  remove only that owned file
    prepare    prepare Teacher files, start/reuse the Host and return Pi config
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import discovery, teacher_entry
from .http_api import PUBLIC_OPERATIONS as TOOL_NAMES
from .resources import ControlPlaneError

INSTALL_FORMAT = "agentgenome-pi-install/1"
COMMAND_CONFIG_FORMAT = "agentgenome-pi-command/1"
OWNER_MARKER = "agentgenome-pi-integration/1"
EXTENSION_NAME = "agentgenome.js"
PI_COMMAND = Path(__file__).with_name("backends") / "pi_command.mjs"


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ControlPlaneError("invalid_argument", f"{field} must be a nonempty string")
    return value.strip()


def default_agent_dir() -> Path:
    """Pi's user agent directory, honouring its documented override."""
    override = os.environ.get("PI_CODING_AGENT_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".pi" / "agent"


def _resolve_agent_dir(agent_dir: str | Path | None) -> Path:
    chosen = Path(agent_dir).expanduser() if agent_dir is not None else default_agent_dir()
    return chosen.resolve()


def _python() -> str:
    # Keep the interpreter path as invoked: resolving symlinks would drop a
    # virtual environment and its installed packages.
    return os.path.abspath(sys.executable)


def _bootstrap_text(*, provider: str, model: str) -> str:
    if not PI_COMMAND.is_file():
        raise ControlPlaneError(
            "not_found", "shipped pi_command.mjs is missing", path=str(PI_COMMAND))
    options = json.dumps({
        "format": COMMAND_CONFIG_FORMAT,
        "python": _python(),
        "provider": provider,
        "model": model,
    }, ensure_ascii=False, indent=2, sort_keys=True)
    # A managed marker makes ownership explicit: install never overwrites a
    # file written by another tool, and uninstall never deletes one. The bare
    # pi-tui import resolves through Pi's extension loader alias, so the trace
    # panel uses the official width helpers without a machine-specific path.
    return (
        f"// {OWNER_MARKER} managed by node_runtime.control_plane.pi_integration; "
        "do not edit.\n"
        "import { truncateToWidth, visibleWidth, wrapTextWithAnsi } from "
        "\"@earendil-works/pi-tui\";\n"
        f"import createExtension from {json.dumps(str(PI_COMMAND.resolve()))};\n\n"
        "export default (pi) => createExtension(pi, {\n"
        f"  ...{options},\n"
        "  helpers: { truncateToWidth, visibleWidth, wrapTextWithAnsi },\n"
        "});\n"
    )


def _owned_regular_file(path: Path) -> str | None:
    """Return the current text when the target is a plain file we own."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode):
        raise ControlPlaneError(
            "invalid_argument", "refusing to follow a symlinked extension path",
            path=str(path))
    if not stat.S_ISREG(info.st_mode):
        raise ControlPlaneError(
            "invalid_argument", "extension path is not a regular file",
            path=str(path))
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ControlPlaneError(
            "invalid_argument", "extension file is not readable UTF-8 text",
            path=str(path)) from error
    if OWNER_MARKER not in text:
        raise ControlPlaneError(
            "invalid_argument",
            "extension file is not owned by this installer; refusing to overwrite",
            path=str(path))
    return text


def install(*, provider: str, model: str,
            agent_dir: str | Path | None = None) -> dict[str, Any]:
    provider = _text(provider, "provider")
    model = _text(model, "model")
    root = _resolve_agent_dir(agent_dir)
    extensions = root / "extensions"
    target = extensions / EXTENSION_NAME
    text = _bootstrap_text(provider=provider, model=model)

    current = _owned_regular_file(target)
    if current == text:
        state = "unchanged"
    else:
        state = "updated" if current is not None else "created"
        try:
            extensions.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
            temporary.write_text(text, encoding="utf-8")
            os.replace(temporary, target)
        except OSError as error:
            raise ControlPlaneError(
                "internal", "cannot write the Pi extension bootstrap",
                path=str(target), error=repr(error)) from error

    return {
        "ok": True,
        "format": INSTALL_FORMAT,
        "state": state,
        "agent_dir": str(root),
        "extension": str(target),
        "python": _python(),
        "provider": provider,
        "model": model,
    }


def uninstall(*, agent_dir: str | Path | None = None) -> dict[str, Any]:
    root = _resolve_agent_dir(agent_dir)
    target = root / "extensions" / EXTENSION_NAME
    current = _owned_regular_file(target)
    if current is None:
        return {
            "ok": True,
            "format": INSTALL_FORMAT,
            "removed": False,
            "reason": "not_installed",
            "agent_dir": str(root),
            "extension": str(target),
        }
    try:
        target.unlink()
    except OSError as error:
        raise ControlPlaneError(
            "internal", "cannot remove the Pi extension bootstrap",
            path=str(target), error=repr(error)) from error
    return {
        "ok": True,
        "format": INSTALL_FORMAT,
        "removed": True,
        "agent_dir": str(root),
        "extension": str(target),
    }


def _host_block(data_dir: Path) -> str:
    return (
        "- 提交 run 之前，先用 bash 执行\n"
        f'  agentgenome status --data-dir "{data_dir}"\n'
        '  并检查 JSON 的 "running" 为 true。若为 false，执行\n'
        f'  agentgenome start --data-dir "{data_dir}"\n'
        "  再 status 确认 running 后才提交 run；不要要求用户另开终端或手工执行底层命令。\n"
    )


def _host_command(data_dir: Path, *, provider: str, model: str,
                  start: bool) -> str:
    argv = [
        _python(), "-m", "node_runtime.control_plane.host",
        "start" if start else "status", "--data-dir", str(data_dir),
    ]
    if start:
        argv.extend(["--backend", "pi", "--provider", provider,
                     "--model", model])
    # Quote every argument, including the Python path and user-provided data
    # directory/provider/model, so spaces and shell metacharacters stay literal.
    return shlex.join(argv)


def _slash_prompt(paths: dict[str, Path], *, provider: str, model: str) -> str:
    """Reuse the Teacher template, fixing only the slash-entry environment.

    The ordinary Pi session keeps its native cwd, so workspace files need
    absolute paths while ``definition_path`` stays workspace-relative. Host
    management names the installed Python explicitly instead of relying on an
    activated virtual environment's PATH.
    """
    data_dir = paths["root"]
    workspace = paths["workspace"]
    prompt = teacher_entry.PROMPT.format(data_dir=data_dir, workspace=workspace)
    status_command = _host_command(data_dir, provider=provider, model=model,
                                   start=False)
    start_command = _host_command(data_dir, provider=provider, model=model,
                                  start=True)
    replacement = (
        "- 提交 run 之前，先用 bash 执行\n"
        f"  {status_command}\n"
        '  并检查 JSON 的 "running" 为 true。若为 false，执行\n'
        f"  {start_command}\n"
        "  再 status 确认 running 后才提交 run；不要要求用户另开终端或手工执行底层命令。\n"
    )
    if _host_block(data_dir) in prompt:
        prompt = prompt.replace(_host_block(data_dir), replacement)
    prompt += f"""
## 斜杠入口环境（本会话，优先于上面的环境说明）
- 本会话由用户在普通 Pi 工作目录中运行 /agentgenome 激活；Pi 原生 cwd、模型和工具保持不变。
- 本地工作区就是当前 cwd {workspace}；Graph YAML、输入文件与 deliveries/ 都放在这里，不同目录使用各自的文件。
- 所有 cwd 的 /agentgenome 会话共用一个按用户隔离的服务数据目录 {data_dir}；不要把服务状态写入工作区。
- MCP Graph 工具的 definition_path 按本地工作区解释：传 "sop.yaml" 这样的相对路径，不要把它当整段 YAML 或任意绝对路径；扩展会在本地读取后上传，服务端不读客户端路径。
- 管理 host 使用本次安装的 Python 绝对路径，不依赖未激活 venv 的 PATH：
  {status_command}
  {start_command}
- prepare 已尝试启动或复用相同参数配置的 host；本次 host_status 是真实结果。若 running 不为 true，先按上面命令处理；遇到 host_config_mismatch 保留现有 host 并把差异报告给用户，不要停止它。
- 不需要通过全局环境变量传递 MCP 配置；本会话内的 MCP 客户端已经拿到连接参数。
"""
    return prompt


def _first_json(*texts: str) -> dict[str, Any] | None:
    for text in texts:
        stripped = text.strip()
        if not stripped:
            continue
        try:
            value = json.loads(stripped)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _start_or_reuse_host(data_dir: Path, *, provider: str, model: str
                         ) -> dict[str, Any]:
    """Run the launcher in a child process; never construct Host here.

    The launcher only reads records and probes HTTP; the detached service is
    the only process that opens the database. A configuration mismatch is
    returned as data, not raised: the existing Host keeps running.
    """
    package_root = str(Path(__file__).resolve().parents[2])
    environment = os.environ.copy()
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        package_root if not existing
        else package_root + os.pathsep + existing)
    command = [
        _python(), "-m", "node_runtime.control_plane.host", "start",
        "--data-dir", str(data_dir), "--backend", "pi",
        "--provider", provider, "--model", model,
    ]
    try:
        done = subprocess.run(command, cwd=package_root, env=environment,
                              capture_output=True, text=True, timeout=60,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"ok": False, "error": "host_start_failed",
                "message": repr(error)}
    payload = _first_json(done.stdout, done.stderr)
    if payload is None:
        return {
            "ok": False,
            "error": "host_start_failed",
            "exit_code": done.returncode,
            "stdout": done.stdout.strip()[-2000:],
            "stderr": done.stderr.strip()[-2000:],
        }
    return payload


def prepare_session(*, data_dir: str | Path | None = None,
                    workspace: str | Path | None = None,
                    provider: str, model: str) -> dict[str, Any]:
    provider = _text(provider, "provider")
    model = _text(model, "model")
    try:
        root = discovery.resolve_data_dir(data_dir)
        chosen = (Path(workspace).expanduser().resolve()
                  if workspace is not None else Path.cwd().resolve())
    except (OSError, RuntimeError) as error:
        raise ControlPlaneError(
            "invalid_argument", "service or workspace path cannot be resolved",
            data_dir=str(data_dir), workspace=str(workspace)) from error
    paths = teacher_entry.prepare(root, chosen)
    prompt = _slash_prompt(paths, provider=provider, model=model)
    try:
        paths["prompt"].write_text(prompt, encoding="utf-8")
    except OSError as error:
        raise ControlPlaneError(
            "internal", "cannot write the prepared Teacher prompt",
            path=str(paths["prompt"]), error=repr(error)) from error
    host_status = _start_or_reuse_host(root, provider=provider, model=model)
    try:
        connection = discovery.connection(root)
    except ControlPlaneError as error:
        raise ControlPlaneError(
            "service_unavailable",
            "control-plane service is not reachable; no Teacher activation "
            "was created",
            data_dir=str(root), host_status=host_status,
            error=error.code) from error
    return {
        "prompt": prompt,
        "workspace": str(paths["workspace"]),
        "data_dir": str(root),
        "endpoint": connection.endpoint,
        "mcp_config": teacher_entry.mcp_config(
            paths, endpoint=connection.endpoint, token=connection.token),
        "host_status": host_status,
        "tool_names": list(TOOL_NAMES),
    }


def _error_payload(error: ControlPlaneError) -> dict[str, Any]:
    return {"ok": False, "error": {
        "code": error.code, "message": error.message, "details": error.details}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m node_runtime.control_plane.pi_integration",
        description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    install_parser = sub.add_parser("install")
    install_parser.add_argument("--provider", required=True,
                                help="Host Worker provider fixed at install time")
    install_parser.add_argument("--model", required=True,
                                help="Host Worker model fixed at install time")
    install_parser.add_argument("--agent-dir", default=None,
                                help="Pi agent directory (default: "
                                     "$PI_CODING_AGENT_DIR or ~/.pi/agent)")

    uninstall_parser = sub.add_parser("uninstall")
    uninstall_parser.add_argument("--agent-dir", default=None)

    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("--data-dir", default=None,
                                help="service data directory "
                                     "(default: per-user AgentGenome state)")
    prepare_parser.add_argument("--workspace", default=None,
                                help="local Teacher workspace (default: cwd)")
    prepare_parser.add_argument("--provider", required=True)
    prepare_parser.add_argument("--model", required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "install":
            payload = install(provider=args.provider, model=args.model,
                              agent_dir=args.agent_dir)
        elif args.command == "uninstall":
            payload = uninstall(agent_dir=args.agent_dir)
        else:
            payload = prepare_session(
                data_dir=args.data_dir, workspace=args.workspace,
                provider=args.provider, model=args.model)
    except ControlPlaneError as error:
        print(json.dumps(_error_payload(error), ensure_ascii=False,
                         sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
