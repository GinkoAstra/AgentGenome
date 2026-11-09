"""Install the ordinary-Pi command entry and prepare its client attachment.

Install/uninstall delegate to the shared managed-file engine in
:mod:`client_install`; this module keeps only the Pi compatibility entry plus
service preparation. Runtime semantics remain in the running service, MCP and
the Teacher extension. No model, credential, database or service is created.

Commands:
    install    preview/apply <agent-dir>/extensions/agentgenome.js
    uninstall  remove only the owned, unmodified extension and record
    prepare    discover an existing service and return the Pi session payload
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Literal, TypedDict

from . import client_install, discovery, teacher_entry
from .client_access import ServiceUnavailableError
from .client_entries import (PI_COMMAND, PI_OWNER_MARKER as OWNER_MARKER,
                             plan_install as plan_native_install)
from .resources import ControlPlaneError

INSTALL_FORMAT = "agentgenome-pi-install/1"


class PiInstallPlan(TypedDict):
    """One extension file; a preview is read-only and contains no credential."""
    target: str
    content: str
    state: Literal["create", "unchanged", "update", "conflict"]
    previous_sha256: str | None


class PreparedPiSession(TypedDict):
    """Discovery payload, not registration: the live MCP bridge owns attachment.

    mcp_config contains credentials for IPC only; never persist/log this payload.
    service_id anchors a deliberate attachment; instance_id may change on restart.
    """
    prompt: str
    workspace: str
    data_dir: str
    endpoint: str
    service_id: str
    instance_id: str | None
    mcp_config: str
    host_status: dict[str, Any]
    tool_names: list[str]


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


def _engine_target(agent_dir: str | Path | None,
                   data_dir: str | Path | None) -> client_install.InstallTarget:
    root = _resolve_agent_dir(agent_dir)
    chosen_data = (discovery.resolve_data_dir(data_dir)
                   if data_dir is not None else None)
    return client_install.InstallTarget("pi", root, _python(), chosen_data)


def _pi_state(plan: client_install.InstallPlan, previous: str | None,
              changed: bool) -> Literal["create", "unchanged", "update", "conflict"]:
    if plan.state == "conflict":
        return "conflict"
    if not changed:
        return "unchanged"
    return "create" if previous is None else "update"


def _native_plan(target: client_install.InstallTarget):
    record, error = client_install.installed_record(target.config_root,
                                                    target.client_kind)
    if error is not None:
        raise ControlPlaneError("invalid_argument", error,
                                path=str(client_install.record_path(
                                    target.config_root, target.client_kind)))
    return plan_native_install(target, record)


def plan_install(*, agent_dir: str | Path | None = None,
                 data_dir: str | Path | None = None) -> PiInstallPlan:
    """Build a no-write plan without connecting to or starting any service.

    The shared engine owns the plan, record and conflict rules; this wrapper
    keeps the historical Pi payload shape for existing callers.
    """
    target = _engine_target(agent_dir, data_dir)
    native = _native_plan(target)
    plan = client_install.plan_install(target)
    return {
        "target": str(native.path),
        "content": native.content or "",
        "state": _pi_state(plan, native.before_sha256, native.changed),
        "previous_sha256": native.before_sha256,
    }


def install(*, provider: str | None = None, model: str | None = None,
            agent_dir: str | Path | None = None,
            data_dir: str | Path | None = None,
            dry_run: bool = False) -> dict[str, Any]:
    """Apply the shared installation plan; legacy provider/model are ignored."""
    ignored = [name for name, value in
               (("provider", provider), ("model", model)) if value is not None]
    target = _engine_target(agent_dir, data_dir)
    plan = client_install.plan_install(target)
    native = _native_plan(target)
    baseline = {
        "ok": True,
        "format": INSTALL_FORMAT,
        "state": _pi_state(plan, native.before_sha256, native.changed),
        "agent_dir": str(target.config_root),
        "extension": str(native.path),
        "record": str(client_install.record_path(target.config_root, "pi")),
        "python": _python(),
        "data_dir": str(target.data_dir) if target.data_dir is not None else None,
        "dry_run": dry_run,
        "ignored_legacy": ignored,
    }
    if plan.state == "conflict":
        if dry_run:
            baseline["previous_sha256"] = native.before_sha256
            return baseline
        raise ControlPlaneError(
            "invalid_argument",
            "the Pi extension is foreign, user-modified or a legacy install; "
            "refusing to overwrite it. Remove it deliberately, then reinstall.",
            path=str(native.path), conflicts=list(plan.conflicts))
    baseline["previous_sha256"] = native.before_sha256
    if not dry_run:
        client_install.apply_plan(plan)
    return baseline


def uninstall(*, agent_dir: str | Path | None = None) -> dict[str, Any]:
    target = _engine_target(agent_dir, None)
    native = _native_plan(target)
    plan = client_install.plan_uninstall(target)
    payload: dict[str, Any] = {
        "ok": True,
        "format": INSTALL_FORMAT,
        "agent_dir": str(target.config_root),
        "extension": str(native.path),
    }
    if plan.state == "conflict":
        raise ControlPlaneError(
            "invalid_argument",
            "the Pi extension is foreign, user-modified or a legacy install; "
            "refusing to remove it",
            path=str(native.path), conflicts=list(plan.conflicts))
    existed = native.before_sha256 is not None
    client_install.apply_plan(plan)
    payload.update({"removed": existed,
                    "reason": "uninstalled" if existed else "not_installed"})
    return payload


def _replace_once(text: str, old: str, new: str) -> str:
    if old not in text:
        raise ControlPlaneError(
            "internal",
            "the Teacher prompt template changed; the slash overlay cannot apply",
            fragment=old[:120])
    return text.replace(old, new, 1)


def _service_boundary(data_dir: Path) -> str:
    return (
        "- 本 Pi 会话的模型、provider 与工具权限保持用户原有设置；"
        "/agentgenome 不修改它们。\n"
        "- Worker 模型与 Backend 由本机部署者单独配置；本入口不读取、不写入，"
        "也不要求与会话模型一致。\n"
        "- AgentGenome 服务由部署者单独启动和维护；本入口只通过 MCP 连接"
        "已运行的服务，不会自动 start，也不会创建数据目录。\n"
        "- 工具返回服务不可达时，如实告知用户需要单独启动/维护服务；"
        "不要用 bash 自动 start 或另建数据目录。\n"
        f"- 所有 Graph 工具都通过服务端点绑定同一个数据目录（{data_dir}），"
        "不要尝试切换。\n"
    )


def _legacy_host_block(data_dir: Path) -> str:
    return (
        "- 提交 run 之前，先用 bash 执行\n"
        f'  agentgenome status --data-dir "{data_dir}"\n'
        '  并检查 JSON 的 "running" 为 true。若为 false，执行\n'
        f'  agentgenome start --data-dir "{data_dir}"\n'
        "  再 status 确认 running 后才提交 run；不要要求用户另开终端或手工执行底层命令。\n"
        "- 若 start 返回 host_config_mismatch，说明已有 host 按不同配置运行："
        "保留并复用现有 host，把差异报告给用户，不要停止它。\n"
        f"- 所有 Graph 工具都通过服务端点绑定同一个数据目录（{data_dir}），"
        "不要尝试切换。\n"
    )


def _slash_prompt(data_dir: Path, workspace: Path) -> str:
    """Render the Teacher rules for an ordinary Pi session without writing.

    The Teacher CLI still ships sop-guide.md/sop-template.yaml next to a
    workspace; the slash entry has no workspace-write step, so the same Graph
    constraints are embedded in this prompt instead of being removed.
    """
    prompt = teacher_entry.render_prompt(data_dir=data_dir, workspace=workspace)
    prompt = _replace_once(prompt, _legacy_host_block(data_dir),
                           _service_boundary(data_dir))
    prompt = _replace_once(
        prompt,
        f"- 通用协议参考: {workspace}/sop-guide.md（随安装分发，先读它；"
        "字段、委托、reviewer 与提交协议都以它和模板为准）。",
        "- 通用协议参考: 本提示末尾的《Graph SOP 通用协议参考》"
        "（随 /agentgenome 内嵌分发；字段、委托、reviewer 与提交协议都以它和模板为准）。")
    prompt = _replace_once(
        prompt,
        f"- 模板: {workspace}/sop-template.yaml（可 validate 的结构骨架，"
        "把 TODO 换成业务内容）。",
        "- 模板: 本提示末尾的《Graph SOP 模板》"
        "（可 validate 的结构骨架，把 TODO 换成业务内容）。")
    prompt = _replace_once(
        prompt,
        "2. 读 sop-guide.md 和 sop-template.yaml，用 edit/write",
        "2. 按本提示末尾的协议参考与模板，用 edit/write")
    guide = _replace_once(
        teacher_entry.GUIDE,
        "写 YAML 时以本文件 + sop-template.yaml 为准",
        "写 YAML 时以本协议参考和末尾模板为准")
    guide = _replace_once(
        guide,
        "1. 复制 sop-template.yaml 为 sop.yaml，用 edit/write",
        "1. 按本提示末尾的《Graph SOP 模板》写出 sop.yaml，用 edit/write")
    prompt += f"""
## 斜杠入口环境（本会话，优先于上面的环境说明）
- 本会话由用户在普通 Pi 工作目录中运行 /agentgenome 激活；Pi 原生 cwd、模型和工具保持不变。
- 本地工作区就是当前 cwd {workspace}；Graph YAML、输入文件与 deliveries/ 都放在这里，不同目录使用各自的文件。
- 所有 cwd 的 /agentgenome 会话共用一个按用户隔离的服务数据目录 {data_dir}；不要把服务状态写入工作区。
- MCP Graph 工具的 definition_path 按本地工作区解释：传 "sop.yaml" 这样的相对路径，不要把它当整段 YAML 或任意绝对路径；扩展会在本地读取后上传，服务端不读客户端路径。
- 本入口只连接已经运行的 AgentGenome 服务；不自动 start、不创建数据目录、不改变服务模型/后端配置。
- 服务由部署者单独启动/维护；工具返回服务不可达时，向用户说明需要单独启动服务，不要在 bash 中自动 start 或另建数据目录。
- 不需要通过全局环境变量传递 MCP 配置；本会话内的 MCP 客户端已经拿到连接参数。
- 已发布的定义、run 和 trace 保存在服务数据目录中；退出本窗口不会停止服务或正在执行的 run。

## Graph SOP 通用协议参考（内嵌）
{guide}

## Graph SOP 模板（内嵌）
{teacher_entry.TEMPLATE}
"""
    return prompt


def _mcp_tool_names(connection: discovery.Connection) -> list[str]:
    """Read the live tools/list; no Api/Store/Host import and no local state."""
    async def collect() -> list[str]:
        import httpx2
        import mcp.types as types
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        client = httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {connection.token}"},
            timeout=30.0, trust_env=False)
        try:
            streams = streamable_http_client(
                connection.endpoint + "/mcp", http_client=client,
                terminate_on_close=True)
            async with streams as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    names: list[str] = []
                    cursor: str | None = None
                    while True:
                        params = (types.PaginatedRequestParams(cursor=cursor)
                                  if cursor else None)
                        result = await session.list_tools(params=params)
                        names.extend(tool.name for tool in result.tools
                                     if isinstance(tool.name, str))
                        cursor = result.next_cursor
                        if not cursor:
                            return names
        finally:
            await client.aclose()

    try:
        return asyncio.run(collect())
    except Exception as error:
        raise ControlPlaneError(
            "internal",
            "the recorded service did not answer MCP tools/list",
            data_dir=str(connection.data_dir), error=repr(error)) from error


def _mcp_config(connection: discovery.Connection, workspace: Path) -> str:
    return json.dumps({
        "format": teacher_entry.MCP_CONFIG_FORMAT,
        "endpoint": connection.endpoint,
        "token": connection.token,
        "workspace": str(workspace),
        "timeout_ms": 120000,
    }, ensure_ascii=False, sort_keys=True)


def prepare_session(*, data_dir: str | Path | None = None,
                    workspace: str | Path | None = None,
                    provider: str | None = None,
                    model: str | None = None) -> PreparedPiSession:
    """Discover an existing service; never create/start one or write locally.

    Legacy provider/model inputs are accepted and ignored; the Pi window model
    is not a client-install concern.
    """
    del provider, model
    try:
        root = discovery.resolve_data_dir(data_dir)
        chosen = (Path(workspace).expanduser().resolve()
                  if workspace is not None else Path.cwd().resolve())
    except (OSError, RuntimeError) as error:
        raise ControlPlaneError(
            "invalid_argument", "service or workspace path cannot be resolved",
            data_dir=str(data_dir), workspace=str(workspace)) from error
    try:
        connection = discovery.connection(root)
    except ControlPlaneError as error:
        raise ServiceUnavailableError(error) from error
    try:
        tool_names = _mcp_tool_names(connection)
    except ControlPlaneError as error:
        raise ServiceUnavailableError(error) from error
    return {
        "prompt": _slash_prompt(root, chosen),
        "workspace": str(chosen),
        "data_dir": str(root),
        "endpoint": connection.endpoint,
        "service_id": connection.service_id,
        "instance_id": connection.instance_id,
        "mcp_config": _mcp_config(connection, chosen),
        "host_status": discovery.status(root),
        "tool_names": tool_names,
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
    install_parser.add_argument("--agent-dir", default=None,
                                help="Pi agent directory (default: "
                                     "$PI_CODING_AGENT_DIR or ~/.pi/agent)")
    install_parser.add_argument("--data-dir", default=None,
                                help="optional isolated service data directory")
    install_parser.add_argument("--dry-run", action="store_true",
                                help="preview only; write nothing")
    install_parser.add_argument("--provider", default=None,
                                help="deprecated and ignored")
    install_parser.add_argument("--model", default=None,
                                help="deprecated and ignored")

    uninstall_parser = sub.add_parser("uninstall")
    uninstall_parser.add_argument("--agent-dir", default=None)

    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("--data-dir", default=None,
                                help="service data directory "
                                     "(default: per-user AgentGenome state)")
    prepare_parser.add_argument("--workspace", default=None,
                                help="local Pi workspace (default: cwd)")
    prepare_parser.add_argument("--provider", default=None,
                                help="deprecated and ignored")
    prepare_parser.add_argument("--model", default=None,
                                help="deprecated and ignored")

    args = parser.parse_args(argv)
    try:
        if args.command == "install":
            payload = install(provider=args.provider, model=args.model,
                              agent_dir=args.agent_dir, data_dir=args.data_dir,
                              dry_run=args.dry_run)
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
