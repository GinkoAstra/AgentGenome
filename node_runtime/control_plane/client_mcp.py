"""Thin stdio MCP client adapter for native CLI hosts.

This process only discovers an existing service and forwards its remote MCP
tools/list and tools/call. It owns no database, model loop, Host or second
service. The remote session lives in one owner task so its async context is
entered and exited in the same task.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass
import json
import os
import sys
from typing import Any
import uuid

import anyio
import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError
import mcp.types as types

from .access_contracts import CLIENT_KINDS
from .client_access import ClientConnection, connect_client
from .resources import ControlPlaneError

SERVER_NAME = "agentgenome"
STATIC_INSTRUCTIONS = (
    "AgentGenome unified client. Use list_definitions to discover published "
    "Graphs, get_resource to read them, and create_run to submit work. This "
    "adapter forwards the running service's tools unchanged."
)


class RemoteTransportError(RuntimeError):
    """The remote MCP transport failed; the call was not resent."""

    def __init__(self, code: str, message: str, *,
                 unknown_outcome: bool) -> None:
        self.code = code
        self.message = message
        self.unknown_outcome = unknown_outcome
        super().__init__(f"{code}: {message}")


@dataclass
class _Request:
    operation: str
    future: asyncio.Future[Any]
    cursor: str | None = None
    name: str | None = None
    arguments: dict[str, Any] | None = None


def _transport_lost(request: _Request) -> RemoteTransportError:
    return RemoteTransportError(
        "transport_unavailable",
        f"remote MCP transport ended during {request.operation}; "
        "the call was not resent",
        unknown_outcome=request.operation == "call_tool")


def _queued_transport_lost(request: _Request) -> RemoteTransportError:
    return RemoteTransportError(
        "transport_unavailable",
        f"remote MCP transport ended before the queued {request.operation} "
        "was sent; the call was not sent",
        unknown_outcome=False)


def _find_mcp_error(error: BaseException) -> MCPError | None:
    if isinstance(error, MCPError):
        return error
    if isinstance(error, BaseExceptionGroup):
        for item in error.exceptions:
            found = _find_mcp_error(item)
            if found is not None:
                return found
    return None


def _root_error(error: BaseException) -> BaseException:
    """Unwrap an anyio/SDK single-child group, but keep a remote MCPError."""
    found = _find_mcp_error(error)
    if found is not None:
        return found
    while isinstance(error, BaseExceptionGroup) and len(error.exceptions) == 1:
        error = error.exceptions[0]
    return error


class _RemoteMCP:
    """Serial owner of one remote Streamable HTTP MCP session."""

    def __init__(self, *, data_dir: str | None, client_kind: str,
                 client_session_id: str, workspace: str) -> None:
        self._data_dir = data_dir
        self._client_kind = client_kind
        self._client_session_id = client_session_id
        self._workspace = workspace
        self._queue: asyncio.Queue[_Request | None] = asyncio.Queue()
        self._replace_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._conn: ClientConnection | None = None
        self._stack: AsyncExitStack | None = None
        self._http: httpx2.AsyncClient | None = None
        self._session: ClientSession | None = None
        self._instructions: str | None = None
        self._retired = False
        self._closed = False

    async def start(self) -> str | None:
        return await self._request("connect")

    async def list_tools(self, cursor: str | None = None) -> Any:
        return await self._request("list_tools", cursor=cursor)

    async def call_tool(self, name: str,
                        arguments: dict[str, Any]) -> Any:
        return await self._request("call_tool", name=name,
                                   arguments=arguments)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._task is not None:
            with suppress(BaseException):
                await self._queue.put(None)
                await self._task
            self._task = None
        connection = self._conn
        self._conn = None
        if connection is not None:
            with suppress(Exception):
                await asyncio.to_thread(connection.close)

    async def _request(self, operation: str, **fields: Any) -> Any:
        if self._closed:
            raise RuntimeError("the remote MCP client is closed")
        async with self._replace_lock:
            if self._closed:
                raise RuntimeError("the remote MCP client is closed")
            if self._task is None or self._task.done() or self._retired:
                previous = self._task
                if previous is not None and not previous.done():
                    try:
                        await previous
                    except asyncio.CancelledError:
                        if asyncio.current_task().cancelling():
                            raise
                    except BaseException:
                        pass
                if self._closed:
                    raise RuntimeError("the remote MCP client is closed")
                if self._task is None or self._task.done() or self._retired:
                    self._retired = False
                    self._task = asyncio.create_task(
                        self._run(), name="agentgenome-remote-mcp")
            future: asyncio.Future[Any] = (
                asyncio.get_running_loop().create_future())
            await self._queue.put(_Request(operation=operation, future=future,
                                           **fields))
        return await future

    def _settle_pending(self) -> None:
        """Fail accepted-but-unstarted requests; never hand them to a new owner."""
        while True:
            try:
                request = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if request is None or request.future.done():
                continue
            request.future.set_exception(_queued_transport_lost(request))

    async def _run(self) -> None:
        try:
            while True:
                try:
                    request = await self._queue.get()
                except asyncio.CancelledError:
                    if self._closed:
                        self._settle_pending()
                        raise
                    self._retired = True
                    self._settle_pending()
                    return
                if request is None:
                    return
                if request.future.cancelled():
                    continue
                try:
                    value = await self._perform(request)
                except asyncio.CancelledError:
                    if self._closed:
                        if not request.future.done():
                            request.future.cancel()
                        self._settle_pending()
                        raise
                    # A dead remote task group cancels its host task. Retire
                    # this owner and report a transport error instead of
                    # cancelling the caller's stdio session.
                    self._retired = True
                    if not request.future.done():
                        request.future.set_exception(_transport_lost(request))
                    self._settle_pending()
                    return
                except BaseException as error:
                    if not request.future.done():
                        request.future.set_exception(error)
                else:
                    if not request.future.done():
                        request.future.set_result(value)
        finally:
            self._retired = True
            with anyio.CancelScope(shield=True):
                await self._disconnect()

    async def _perform(self, request: _Request) -> Any:
        if request.operation == "connect":
            return await self._ensure_connected()
        if request.operation == "call_tool":
            await self._ensure_connected()
            return await self._call(request.name or "",
                                    request.arguments or {})
        if request.operation == "list_tools":
            await self._ensure_connected()
            return await self._list(request.cursor)
        raise RuntimeError(f"unknown adapter operation {request.operation}")

    async def _ensure_connected(self) -> str | None:
        if self._session is not None:
            return self._instructions
        if self._conn is None:
            self._conn = await asyncio.to_thread(
                connect_client, data_dir=self._data_dir,
                client_kind=self._client_kind,
                client_session_id=self._client_session_id,
                workspace=self._workspace)
        else:
            await asyncio.to_thread(self._conn.reconnect)

        attachment = self._conn.connection
        http = httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {attachment.token}"},
            timeout=30.0, trust_env=False)
        stack = AsyncExitStack()
        try:
            await stack.__aenter__()
            read, write = await stack.enter_async_context(
                streamable_http_client(
                    attachment.endpoint + "/mcp", http_client=http,
                    terminate_on_close=False))
            session = await stack.enter_async_context(
                ClientSession(read, write))
            initialized = await session.initialize()
        except BaseException:
            await _close_quietly(stack, http)
            raise
        self._stack = stack
        self._http = http
        self._session = session
        self._instructions = getattr(initialized, "instructions", None)
        return self._instructions

    async def _list(self, cursor: str | None) -> Any:
        session = self._session
        assert session is not None
        params = (types.PaginatedRequestParams(cursor=cursor)
                  if cursor is not None else None)
        try:
            return await session.list_tools(params=params)
        except BaseException as error:
            root = _root_error(error)
            if isinstance(root, MCPError):
                raise root
            if isinstance(root, asyncio.CancelledError):
                raise
            await self._disconnect()
            raise RemoteTransportError(
                "transport_unavailable",
                "remote MCP transport failed during tools/list",
                unknown_outcome=False) from root

    async def _call(self, name: str, arguments: dict[str, Any]) -> Any:
        session = self._session
        assert session is not None
        try:
            return await session.call_tool(name, arguments)
        except BaseException as error:
            root = _root_error(error)
            if isinstance(root, MCPError):
                raise root
            if isinstance(root, asyncio.CancelledError):
                raise
            await self._disconnect()
            raise RemoteTransportError(
                "transport_unavailable",
                "remote MCP transport failed during tools/call; the call was "
                "not resent",
                unknown_outcome=True) from root

    async def _disconnect(self) -> None:
        stack, http = self._stack, self._http
        self._stack = None
        self._http = None
        self._session = None
        if stack is not None:
            await _close_quietly(stack, http)
        elif http is not None:
            with suppress(Exception):
                await http.aclose()


async def _close_quietly(stack: AsyncExitStack,
                         http: httpx2.AsyncClient | None) -> None:
    with suppress(BaseException):
        await stack.aclose()
    if http is not None:
        with suppress(Exception):
            await http.aclose()


def _failure_result(operation: str,
                    error: BaseException) -> types.CallToolResult:
    if isinstance(error, RemoteTransportError):
        code = error.code
        message = error.message
        details: dict[str, Any] = {
            "unknown_outcome": error.unknown_outcome,
            "resent": False,
        }
    elif isinstance(error, ControlPlaneError):
        code = error.code
        message = error.message
        details = dict(error.details)
        details.setdefault("unknown_outcome", False)
        details.setdefault("resent", False)
    else:
        code = "internal"
        message = "the stdio adapter failed to forward this call"
        details = {"unknown_outcome": False, "resent": False}
    payload = {"ok": False, "operation": operation,
               "error": {"code": code, "message": message,
                         "details": details}}
    return types.CallToolResult(
        content=[types.TextContent(type="text",
                                   text=json.dumps(payload, sort_keys=True))],
        structured_content=payload,
        is_error=True)


def _protocol_error(error: BaseException) -> MCPError:
    if isinstance(error, RemoteTransportError):
        data: dict[str, Any] = {
            "code": error.code,
            "unknown_outcome": error.unknown_outcome,
            "resent": False,
        }
        return MCPError(types.INTERNAL_ERROR, error.message, data)
    if isinstance(error, ControlPlaneError):
        data = {"code": error.code, **error.details}
        return MCPError(types.INTERNAL_ERROR, error.message, data)
    return MCPError(types.INTERNAL_ERROR, "the stdio adapter failed",
                    {"code": "internal"})


async def _serve(*, data_dir: str | None, client_kind: str,
                 client_session_id: str, workspace: str) -> None:
    remote = _RemoteMCP(data_dir=data_dir, client_kind=client_kind,
                        client_session_id=client_session_id,
                        workspace=workspace)

    async def list_tools(_context: Any, params: Any) -> types.ListToolsResult:
        cursor = getattr(params, "cursor", None) if params is not None else None
        try:
            result = await remote.list_tools(cursor)
        except BaseException as error:
            root = _root_error(error)
            if isinstance(root, MCPError):
                raise root
            if isinstance(root, asyncio.CancelledError):
                raise
            raise _protocol_error(root) from root
        return types.ListToolsResult(tools=list(result.tools),
                                     next_cursor=result.next_cursor)

    async def call_tool(_context: Any, params: Any) -> types.CallToolResult:
        name = params.name
        arguments = dict(params.arguments or {})
        try:
            return await remote.call_tool(name, arguments)
        except BaseException as error:
            root = _root_error(error)
            if isinstance(root, MCPError):
                raise root
            if isinstance(root, asyncio.CancelledError):
                raise
            return _failure_result(name, root)

    try:
        instructions = await remote.start()
        server = Server(
            SERVER_NAME, version="0.1.0",
            instructions=instructions or STATIC_INSTRUCTIONS,
            on_list_tools=list_tools, on_call_tool=call_tool)
        async with stdio_server() as (read, write):
            await server.run(read, write,
                             server.create_initialization_options())
    finally:
        await remote.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m node_runtime.control_plane.client_mcp",
        description="Thin stdio MCP adapter for an existing AgentGenome "
                    "service; it forwards tools and owns no local state.")
    parser.add_argument("--client-kind", required=True, choices=CLIENT_KINDS,
                        help="host family reported to the service")
    parser.add_argument("--session-id", default=None,
                        help="stable id for this window (default: a new UUID)")
    parser.add_argument("--workspace", default=None,
                        help="display label only (default: current directory)")
    parser.add_argument("--data-dir", default=None,
                        help="service data directory (default: user default)")
    return parser


def main(argv: list[str] | None = None) -> int:
    """stdio entry: --client-kind, --session-id, --workspace, --data-dir.

    Defaults: session UUID per process, cwd workspace, shared user data-dir.
    stdout contains MCP frames only. Credentials must never enter tool output.
    """
    args = _parser().parse_args(argv)
    session_id = args.session_id or uuid.uuid4().hex
    workspace = args.workspace if args.workspace is not None else os.getcwd()
    try:
        asyncio.run(_serve(data_dir=args.data_dir,
                           client_kind=args.client_kind,
                           client_session_id=session_id,
                           workspace=workspace))
    except KeyboardInterrupt:
        return 130
    except ControlPlaneError as error:
        print(f"agentgenome client: {error.code}: {error.message}",
              file=sys.stderr)
        return 2
    except RemoteTransportError as error:
        print(f"agentgenome client: {error.code}: {error.message}",
              file=sys.stderr)
        return 2
    except Exception as error:
        print(f"agentgenome client: {type(error).__name__}: {error}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
