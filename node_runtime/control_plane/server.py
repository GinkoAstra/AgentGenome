"""The installed network service for the control plane.

One ApiServer owns the Host (Api, Scheduler, Controller and configured
Backends), exposes an explicit operation surface over HTTP and MCP through a
single dispatcher and Api, and runs the reconciliation loop on a separate
thread. Host remains the owner of the application objects; this module adds
the network boundary, and ``agentgenome serve`` starts this server. There is
no alternate in-process serving loop.
"""
from __future__ import annotations

import contextlib
import functools
import ipaddress
import json
import os
from pathlib import Path
import signal
import socket
import sys
import threading
import time
from typing import Any, Mapping

import anyio
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS
import uvicorn
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from .host import Host
from .http_api import (IDENTITY_FIELDS, dispatch, error_envelope, ok_envelope,
                       status_for_error)
from .mcp_server import (TOOL_ARGUMENTS, bind_request_arguments,
                         build_server, reset_request_arguments)
from .resources import ControlPlaneError
from .service_access import ServiceAccess
from .service_auth import (DELIVERY_KIND, ServiceAuth, bind_actor,
                           current_actor, reset_actor)
from .trace import TraceService
from . import worker_api
from .tools import ToolError

SERVICE_FORMAT = "agentgenome-api-service/1"
SERVICE_FILENAME = "service.json"
STOP_TIMEOUT = 5.0


class ServiceStartError(RuntimeError):
    """The service did not claim the data directory or bind its socket."""

    def __init__(self, reason: str, **detail: Any) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(reason)


class StopIncomplete(RuntimeError):
    """Stop was requested while service work still runs; ownership is kept."""

    def __init__(self, pending: list[str]) -> None:
        self.pending = list(pending)
        super().__init__("stop incomplete: " + ", ".join(self.pending))


class _GracefulServer(uvicorn.Server):
    """Uvicorn server that lets this service own process signal handling.

    Uvicorn's default handler re-raises the captured signal after a graceful
    shutdown, which terminates the process before ``serve()`` can release the
    Host claim or clear a live watch. The service installs its own SIGTERM/
    SIGINT handler that only requests ``should_exit`` so the normal shutdown
    and cleanup path always runs.
    """

    @contextlib.contextmanager
    def capture_signals(self):
        yield


class ApiServer:
    """One data-directory owner, request server and separate control loop.

    Bind loopback only. Claim the existing Host lock before publishing ready;
    a second service or old Host must not run against the same directory.
    Public readiness requires initialized storage and a live control loop.
    Requests never execute reconciliation inline. Shutdown does not fabricate
    completion or cancel every Run; retain the existing recovery semantics.
    """

    def __init__(self, host: Host, *, bind: str = "127.0.0.1",
                 port: int = 0,
                 heartbeat_timeout: float | None = None,
                 sse_poll_interval: float = 0.5,
                 sse_heartbeat_interval: float = 15.0) -> None:
        if not _is_loopback(bind):
            raise ValueError("ApiServer only binds a loopback address")
        self.host = host
        self.bind = bind
        self.port = port
        self.endpoint: str | None = None
        self.auth = ServiceAuth(host.data_dir)
        self.service_access = ServiceAccess(
            service_id=self.auth.service_id(),
            instance_id=host.instance_id, backends=host.backends)
        self.trace = TraceService(
            host.api, service_id=self.auth.service_id(),
            backends=host.backends,
            request_log=Path(host.data_dir) / "requests.jsonl")
        self.sse_poll_interval = max(0.01, float(sse_poll_interval))
        self.sse_heartbeat_interval = max(0.05, float(sse_heartbeat_interval))
        self.heartbeat_timeout = (
            heartbeat_timeout if heartbeat_timeout is not None
            else max(10.0, 3 * host.scheduler_interval))
        self._app: ASGIApp | None = None
        self._socket: socket.socket | None = None
        self._uvicorn: uvicorn.Server | None = None
        self._http_thread: threading.Thread | None = None
        self._control_thread: threading.Thread | None = None
        self._control_stop = threading.Event()
        self._health_lock = threading.Lock()
        self._last_tick: float | None = None
        self._last_tick_at: str | None = None
        self._last_error: str | None = None
        self._closed = False
        self._log_path = Path(host.data_dir) / "service.log"

    # -- application -----------------------------------------------------
    def build_app(self) -> ASGIApp:
        """Build HTTP routes and network MCP around the same Api and auth.

        No per-request Api/Store construction; authenticate before dispatch.
        MCP and JSON preserve identical Actor/CAS/idempotency behavior.
        """
        if self._app is not None:
            return self._app
        mcp = build_server(
            api=self.host.api, actor_resolver=current_actor,
            middleware=[_McpDiagnostics(self.trace),
                        _RejectClientIdentity(), _McpActorBinding(self.auth)],
            trace=self.trace, service_access=self.service_access)
        mcp_starlette = mcp.streamable_http_app(
            streamable_http_path="/mcp", host=self.bind)
        session_manager = mcp.session_manager
        secured_mcp = _BearerASGI(mcp_starlette, self.auth)

        async def healthz(_: Request) -> Response:
            return JSONResponse({
                "ok": True,
                "service": "agentgenome",
                "service_id": self.auth.service_id(),
                "instance_id": self.host.instance_id,
                "pid": os.getpid(),
            })

        async def readyz(_: Request) -> Response:
            storage = await anyio.to_thread.run_sync(self._storage_ready)
            loop = self._loop_status()
            ready = storage and loop["ok"]
            body: dict[str, Any] = {
                "ok": ready,
                "ready": ready,
                "service_id": self.auth.service_id(),
                "instance_id": self.host.instance_id,
                "checks": {"storage": storage, "control_loop": loop["ok"]},
                "last_tick_at": loop["last_tick_at"],
            }
            if loop["last_error"] is not None:
                body["control_loop_error"] = loop["last_error"]
            return JSONResponse(body, status_code=200 if ready else 503)

        async def operation(request: Request) -> Response:
            name = request.path_params["operation"]
            started = time.monotonic()
            try:
                actor = self.auth.authenticate_publisher(
                    request.headers.get("authorization"))
            except ControlPlaneError as error:
                self._record(name, "unauthorized", status=401, error=error,
                             duration=time.monotonic() - started)
                return JSONResponse(error_envelope(name, error),
                                    status_code=status_for_error(error.code))
            try:
                arguments = await request.json()
            except Exception:
                error = ControlPlaneError(
                    "invalid_argument", "request body must be a JSON object")
                self._record(name, "invalid_argument", status=400, error=error,
                             actor=actor, duration=time.monotonic() - started)
                return JSONResponse(error_envelope(name, error),
                                    status_code=400)
            if not isinstance(arguments, dict):
                error = ControlPlaneError(
                    "invalid_argument", "request body must be a JSON object")
                self._record(name, "invalid_argument", status=400, error=error,
                             actor=actor, duration=time.monotonic() - started)
                return JSONResponse(error_envelope(name, error),
                                    status_code=400)
            envelope = await anyio.to_thread.run_sync(
                self._dispatch, actor, name, arguments)
            if envelope["ok"]:
                self._record(name, "ok", status=200, actor=actor,
                             duration=time.monotonic() - started,
                             **_association(arguments))
                return JSONResponse(envelope, status_code=200)
            failure = ControlPlaneError(
                envelope["error"]["code"], envelope["error"]["message"])
            self._record(name, "error", status=status_for_error(failure.code),
                         error=failure, actor=actor,
                         duration=time.monotonic() - started,
                         **_association(arguments))
            return JSONResponse(envelope,
                                status_code=status_for_error(failure.code))

        async def worker_tool(request: Request) -> Response:
            """One Worker system-tool call under a delivery credential.

            The credential's server-side binding is the only identity source;
            the body carries a granted tool name and schema-validated args.
            """
            try:
                entry = self.auth.authenticate_delivery(
                    request.headers.get("authorization"))
            except ControlPlaneError as error:
                self._record("worker.tool", "unauthorized", status=401,
                             error=error)
                return JSONResponse(error_envelope("worker.tool", error),
                                    status_code=status_for_error(error.code))
            try:
                payload = await request.json()
            except Exception:
                payload = None
            if not isinstance(payload, dict):
                error = ControlPlaneError(
                    "invalid_argument", "request body must be a JSON object")
                self._record("worker.tool", "invalid_argument", status=400,
                             error=error)
                return JSONResponse(error_envelope("worker.tool", error),
                                    status_code=400)
            started = time.monotonic()
            envelope = await anyio.to_thread.run_sync(
                self._worker_dispatch, entry, payload)
            status = (200 if envelope["ok"]
                      else status_for_error(envelope["error"]["code"]))
            self._record(
                "worker.tool", "ok" if envelope["ok"] else "error",
                status=status,
                error=(None if envelope["ok"] else ControlPlaneError(
                    envelope["error"]["code"], envelope["error"]["message"])),
                duration=time.monotonic() - started,
                worker_id=_scalar(payload.get("worker_id")),
                delivery_key=_scalar((payload.get("receipt") or {}).get("delivery_key")
                                     if isinstance(payload.get("receipt"), Mapping)
                                     else None))
            return JSONResponse(envelope, status_code=status)

        async def trace_snapshot(request: Request) -> Response:
            run_id = request.path_params["run_id"]
            return await self._invoke(
                request, "read_trace",
                functools.partial(
                    self.trace.snapshot, run_id=run_id,
                    node_id=request.query_params.get("node_id"),
                    candidate_id=request.query_params.get("candidate_id")),
                run_id=run_id)

        async def trace_events(request: Request) -> Response:
            run_id = request.path_params["run_id"]

            def read(actor: Any) -> dict[str, Any]:
                return self.trace.events(
                    actor, run_id, after=request.query_params.get("after"),
                    limit=_query_int(request, "limit", 100))

            return await self._invoke(request, "read_trace_events", read,
                                      run_id=run_id)

        async def trace_execution(request: Request) -> Response:
            run_id = request.path_params["run_id"]
            delivery_key = request.path_params["delivery_key"]

            def read(actor: Any) -> dict[str, Any]:
                try:
                    epoch = int(request.path_params["epoch"])
                except ValueError as error:
                    raise ControlPlaneError(
                        "invalid_argument", "epoch must be an integer") from error
                return self.trace.execution(
                    actor, run_id, request.path_params["worker_id"], epoch,
                    delivery_key,
                    after=request.query_params.get("after"),
                    limit=_query_int(request, "limit", 100))

            return await self._invoke(
                request, "read_execution", read, run_id=run_id,
                worker_id=request.path_params["worker_id"],
                delivery_key=delivery_key)

        async def trace_requests(request: Request) -> Response:
            def read(actor: Any) -> dict[str, Any]:
                return self.trace.requests(
                    actor,
                    request_id=request.query_params.get("request_id"),
                    run_id=request.query_params.get("run_id"),
                    after=request.query_params.get("after"),
                    limit=_query_int(request, "limit", 100))

            return await self._invoke(
                request, "read_requests", read,
                run_id=request.query_params.get("run_id"))

        async def trace_watch(request: Request) -> Response:
            operation = "read_trace_watch"
            run_id = request.path_params["run_id"]
            after = (request.query_params.get("after")
                     or request.headers.get("last-event-id"))
            started = time.monotonic()
            try:
                actor = self.auth.authenticate_publisher(
                    request.headers.get("authorization"))
            except ControlPlaneError as error:
                self._record(operation, "unauthorized", status=401,
                             error=error, duration=time.monotonic() - started,
                             run_id=run_id)
                return JSONResponse(error_envelope(operation, error),
                                    status_code=status_for_error(error.code))
            # Validate the scoped cursor before any stream bytes are sent, so a
            # wrong service/run or an ahead cursor is a JSON resnapshot error.
            try:
                await anyio.to_thread.run_sync(functools.partial(
                    self.trace.events, actor, run_id, after=after, limit=1))
            except ControlPlaneError as error:
                self._record(operation, "error", status=status_for_error(error.code),
                             error=error, actor=actor,
                             duration=time.monotonic() - started, run_id=run_id)
                return JSONResponse(error_envelope(operation, error),
                                    status_code=status_for_error(error.code))
            self._record(operation, "stream_open", status=200, actor=actor,
                         duration=time.monotonic() - started, run_id=run_id)
            return StreamingResponse(
                self._watch_frames(request, actor, run_id, after),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache",
                         "X-Accel-Buffering": "no",
                         "Connection": "keep-alive"})

        @contextlib.asynccontextmanager
        async def lifespan(_: Starlette):
            async with session_manager.run():
                yield

        self._app = Starlette(
            routes=[
                Route("/healthz", healthz, methods=["GET"]),
                Route("/readyz", readyz, methods=["GET"]),
                Route("/v1/operations/{operation}", operation, methods=["POST"]),
                Route("/v1/worker/tool", worker_tool, methods=["POST"]),
                Route("/v1/runs/{run_id}/trace", trace_snapshot, methods=["GET"]),
                Route("/v1/runs/{run_id}/events", trace_events, methods=["GET"]),
                Route("/v1/runs/{run_id}/watch", trace_watch, methods=["GET"]),
                Route("/v1/runs/{run_id}/executions/{worker_id}/{epoch}/{delivery_key}",
                      trace_execution, methods=["GET"]),
                Route("/v1/requests", trace_requests, methods=["GET"]),
                Mount("/", app=secured_mcp),
            ],
            lifespan=lifespan,
        )
        return self._app

    async def _invoke(self, request: Request, operation: str, call: Any,
                      **association: Any) -> Response:
        """Authenticate, run one finite trace read off the event loop, record it."""
        started = time.monotonic()
        try:
            actor = self.auth.authenticate_publisher(
                request.headers.get("authorization"))
        except ControlPlaneError as error:
            self._record(operation, "unauthorized", status=401, error=error,
                         duration=time.monotonic() - started, **association)
            return JSONResponse(error_envelope(operation, error),
                                status_code=status_for_error(error.code))
        try:
            result = await anyio.to_thread.run_sync(call, actor)
        except ControlPlaneError as error:
            self._record(operation, "error",
                         status=status_for_error(error.code), error=error,
                         actor=actor, duration=time.monotonic() - started,
                         **association)
            return JSONResponse(error_envelope(operation, error),
                                status_code=status_for_error(error.code))
        except Exception as error:  # transport boundary, never silent
            self._log("trace_error", operation=operation,
                      error=type(error).__name__)
            internal = ControlPlaneError("internal", "internal service error")
            self._record(operation, "error", status=500, error=internal,
                         actor=actor, duration=time.monotonic() - started,
                         **association)
            return JSONResponse(error_envelope(operation, internal),
                                status_code=500)
        self._record(operation, "ok", status=200, actor=actor,
                     duration=time.monotonic() - started, **association)
        return JSONResponse(ok_envelope(operation, result))

    async def _watch_frames(self, request: Request, actor: Any, run_id: str,
                            after: Any):
        """Follow committed events; each durable advance is one SSE frame.

        The generator reads the same event log the JSON transport reads and
        only advances after a frame is yielded. A slow client delays only its
        own coroutine; no shared unbounded queue and no control-loop work.
        """
        cursor = after
        last_sent = time.monotonic()
        while True:
            # An idle subscriber holds an HTTP connection; service shutdown
            # must end it rather than waiting for the client to disconnect.
            if (self._closed
                    or (self._uvicorn is not None and self._uvicorn.should_exit)
                    or await request.is_disconnected()):
                return
            try:
                page = await anyio.to_thread.run_sync(functools.partial(
                    self.trace.events, actor, run_id, after=cursor, limit=200))
            except ControlPlaneError as error:
                payload = {"service_id": self.trace.service_id, "run_id": run_id,
                           "code": error.code, "instruction": "resnapshot",
                           "detailed": error.details}
                yield _sse_frame("cursor_expired", payload)
                return
            if page["events"]:
                for event in page["events"]:
                    token = json.dumps(
                        {"service_id": self.trace.service_id,
                         "run_id": run_id, "seq": event["seq"]},
                        sort_keys=True, separators=(",", ":"))
                    yield _sse_frame("control", {
                        "format": "agentgenome-trace-event/1",
                        "service_id": self.trace.service_id,
                        "run_id": run_id,
                        "cursor": {"service_id": self.trace.service_id,
                                   "run_id": run_id, "seq": event["seq"],
                                   "token": token},
                        "event": event,
                    }, event_id=token)
                cursor = page["next_cursor"]["token"]
                last_sent = time.monotonic()
                continue
            if time.monotonic() - last_sent >= self.sse_heartbeat_interval:
                yield _sse_frame("heartbeat", {
                    "service_id": self.trace.service_id,
                    "run_id": run_id,
                    "latest_seq": page["latest_seq"],
                    "at": _now(),
                })
                last_sent = time.monotonic()
            await anyio.sleep(self.sse_poll_interval)

    def _record(self, operation: str, outcome: str, *, status: int | None = None,
                error: ControlPlaneError | None = None,
                actor: Any = None, duration: float | None = None,
                **fields: Any) -> None:
        """Append one bounded diagnostic record; never fails the request."""
        diagnostics = self.trace.diagnostics
        if diagnostics is None:
            return
        safe: dict[str, Any] = {}
        for key, value in fields.items():
            if isinstance(value, bool) or value is None:
                continue
            if isinstance(value, (str, int, float)):
                safe[key] = value[:128] if isinstance(value, str) else value
        diagnostics.record(
            operation=operation, outcome=outcome, status=status,
            actor_id=(actor.actor_id if actor is not None else None),
            duration_ms=(int(duration * 1000) if duration is not None else None),
            error=({"code": error.code, "message": error.message[:500]}
                   if error is not None else None),
            **safe)

    def _dispatch(self, actor: Any, operation: str,
                  arguments: Mapping[str, Any]) -> dict[str, Any]:
        try:
            return dispatch(self.host.api, actor, operation, arguments,
                            trace=self.trace,
                            service_access=self.service_access)
        except Exception as error:  # transport boundary, never silent
            self._log("request_error", operation=operation,
                      error=type(error).__name__)
            return error_envelope(
                operation,
                ControlPlaneError("internal", "internal service error"))

    def _worker_dispatch(self, entry: Mapping[str, Any],
                         payload: Mapping[str, Any]) -> dict[str, Any]:
        if entry.get("kind") != DELIVERY_KIND:
            return error_envelope(
                "worker.tool",
                ControlPlaneError("unauthorized",
                                  "credential is not a delivery credential"))
        try:
            result = worker_api.execute(self.host.api, entry["binding"], payload)
            return ok_envelope("worker.tool", result)
        except ToolError as error:
            # Worker tool codes are their own stable surface; do not funnel
            # them through ControlPlaneError's public-operation allowlist.
            return {"ok": False, "operation": "worker.tool",
                    "error": {"code": error.code,
                              "message": str(error) or error.code,
                              "details": error.details}}
        except ControlPlaneError as error:
            return error_envelope("worker.tool", error)
        except Exception as error:
            self._log("worker_error", error=type(error).__name__)
            return error_envelope(
                "worker.tool",
                ControlPlaneError("internal", "internal service error"))

    def _issue_delivery(self, binding: Mapping[str, Any]) -> dict[str, Any]:
        """Trusted issuer handed to the Backend after the endpoint is bound."""
        entry = self.auth.issue_delivery(binding)
        return {"endpoint": self.endpoint, "token": entry["token"],
                "service_id": entry["service_id"]}

    # -- lifecycle -------------------------------------------------------
    def serve(self) -> int:
        """Own the lifecycle, publish endpoint identity and serve until stopped."""
        try:
            self._start_parts()
        except ServiceStartError as error:
            print(json.dumps({"error": error.reason, **error.detail}),
                  file=sys.stderr)
            return 2
        self._write_service_record()
        finished = True
        previous_handlers: dict[int, Any] = {}

        def request_exit(signum: int, _frame: Any) -> None:
            self._log("signal", signal=signum)
            if self._uvicorn is not None:
                self._uvicorn.should_exit = True

        if threading.current_thread() is threading.main_thread():
            for handled in (signal.SIGTERM, signal.SIGINT):
                previous_handlers[handled] = signal.signal(
                    handled, request_exit)
        try:
            self._uvicorn.run(sockets=[self._socket])
        finally:
            for handled, previous in previous_handlers.items():
                signal.signal(handled, previous)
            self._control_stop.set()
            if self._control_thread is not None:
                self._control_thread.join(STOP_TIMEOUT)
            finished = self._finish()
        if not finished:
            print(json.dumps({"error": "stop_incomplete",
                              "pending": self._pending_threads()}),
                  file=sys.stderr)
            return 3
        return 0

    def start_background(self, *, startup_timeout: float = 15.0) -> str:
        """Start this service on a background thread; used by isolated tests."""
        self._start_parts()
        self._write_service_record()
        self._http_thread = threading.Thread(
            target=self._uvicorn.run, kwargs={"sockets": [self._socket]},
            name="agentgenome-http", daemon=True)
        self._http_thread.start()
        deadline = time.monotonic() + startup_timeout
        while not self._uvicorn.started:
            if not self._http_thread.is_alive():
                self._stop_after_failure()
                raise ServiceStartError("service_failed")
            if time.monotonic() > deadline:
                self._stop_after_failure()
                raise ServiceStartError("service_start_timeout")
            time.sleep(0.01)
        assert self.endpoint is not None
        return self.endpoint

    def stop(self, *, timeout: float = STOP_TIMEOUT) -> None:
        """Stop only this service instance; keep ownership until work ends.

        A timeout raises StopIncomplete while the service record still names
        this process and the Host claim is retained. Calling stop() again
        retries the same joins and completes once the work has exited.
        """
        if self._closed:
            return
        if self._uvicorn is not None:
            self._uvicorn.should_exit = True
        self._control_stop.set()
        pending = self._join_threads(timeout)
        if pending:
            self._log("stop_incomplete", pending=pending)
            raise StopIncomplete(pending)
        if not self._finish():
            pending = self._pending_threads() or ["unknown"]
            self._log("stop_incomplete", pending=pending)
            raise StopIncomplete(pending)

    def _stop_after_failure(self) -> None:
        try:
            self.stop(timeout=1.0)
        except StopIncomplete:
            pass

    def _join_threads(self, timeout: float) -> list[str]:
        if self._control_thread is not None:
            self._control_thread.join(timeout)
        if self._http_thread is not None:
            self._http_thread.join(timeout)
        return self._pending_threads()

    def _pending_threads(self) -> list[str]:
        pending: list[str] = []
        if (self._control_thread is not None
                and self._control_thread.is_alive()):
            pending.append("control_loop")
        if self._http_thread is not None and self._http_thread.is_alive():
            pending.append("http")
        return pending

    def _start_parts(self) -> None:
        refusal = self.host.claim()
        if refusal is not None:
            raise ServiceStartError(refusal.get("error", "host_already_running"),
                                    **refusal)
        try:
            self.auth.publisher_credentials()
            app = self.build_app()
            family = socket.AF_INET6 if ":" in self.bind else socket.AF_INET
            requested = self.port or self._published_port()
            try:
                self._socket = socket.create_server(
                    (self.bind, requested), family=family, backlog=2048)
            except OSError as error:
                if requested:
                    # A previously issued delivery context names this endpoint;
                    # silently choosing another port would strand it.
                    raise ServiceStartError(
                        "service_port_unavailable", port=requested,
                        bind=self.bind, detail=str(error)) from error
                raise
            self.port = self._socket.getsockname()[1]
            self.endpoint = _endpoint(self.bind, self.port)
            self.host.attach_credential_issuer(self._issue_delivery)
            config = uvicorn.Config(app, log_level="warning", access_log=False,
                                    lifespan="on")
            self._uvicorn = _GracefulServer(config)
            self._start_control_loop()
        except BaseException:
            self._control_stop.set()
            if self._control_thread is not None:
                self._control_thread.join(STOP_TIMEOUT)
            self._finish()
            raise

    def _published_port(self) -> int:
        """Reuse the endpoint already issued to this data directory's clients."""
        try:
            record = json.loads(
                (Path(self.host.data_dir) / SERVICE_FILENAME)
                .read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return 0
        if not isinstance(record, dict):
            return 0
        if record.get("service_id") != self.auth.service_id():
            return 0
        port = record.get("port")
        if not isinstance(port, int) or isinstance(port, bool):
            return 0
        return port if 0 < port < 65536 else 0

    def _finish(self) -> bool:
        """Publish stopped and release ownership only when no work remains."""
        if self._closed:
            return True
        self._control_stop.set()
        if self._pending_threads():
            return False
        self._closed = True
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
        self._update_service_record()
        self.host.release()
        return True

    # -- control loop ----------------------------------------------------
    def _start_control_loop(self) -> None:
        self._control_thread = threading.Thread(
            target=self._control_loop, name="agentgenome-control", daemon=True)
        self._control_thread.start()

    def _control_loop(self) -> None:
        while not self._control_stop.is_set():
            try:
                self.host.reconcile_once()
                with self._health_lock:
                    self._last_tick = time.monotonic()
                    self._last_tick_at = _now()
                    self._last_error = None
            except Exception as error:
                with self._health_lock:
                    self._last_error = type(error).__name__
                self._log("control_error", error=repr(error))
            self._control_stop.wait(self.host.scheduler_interval)

    def _loop_status(self) -> dict[str, Any]:
        with self._health_lock:
            last_tick = self._last_tick
            last_error = self._last_error
            last_tick_at = self._last_tick_at
        alive = (self._control_thread is not None
                 and self._control_thread.is_alive())
        fresh = (last_tick is not None
                 and (time.monotonic() - last_tick) <= self.heartbeat_timeout)
        return {"ok": alive and fresh and last_error is None,
                "last_tick_at": last_tick_at,
                "last_error": last_error}

    # -- readiness and identity -----------------------------------------
    def _storage_ready(self) -> bool:
        try:
            with self.host.api.store.read() as tx:
                tx.one("SELECT 1")
            return True
        except Exception:
            return False

    def _service_record(self) -> dict[str, Any]:
        return {
            "format": SERVICE_FORMAT,
            "service_id": self.auth.service_id(),
            "instance_id": self.host.instance_id,
            "pid": os.getpid(),
            "endpoint": self.endpoint,
            "bind": self.bind,
            "port": self.port,
            "data_dir": str(self.host.data_dir),
            "protocols": {"http": "/v1/operations/{operation}",
                          "worker_tools": "/v1/worker/tool",
                          "mcp": "/mcp"},
            "started_at": _now(),
            "stopped_at": None,
        }

    def _write_service_record(self) -> None:
        _write_json(Path(self.host.data_dir) / SERVICE_FILENAME,
                    self._service_record())

    def _update_service_record(self) -> None:
        path = Path(self.host.data_dir) / SERVICE_FILENAME
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if record.get("pid") != os.getpid():
            return
        record["stopped_at"] = _now()
        _write_json(path, record)

    def _log(self, event: str, **detail: Any) -> None:
        entry = {"at": _now(), "event": event, **detail}
        try:
            with self._log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, sort_keys=True) + "\n")
        except OSError:
            pass


class _BearerASGI:
    """Reject an unauthenticated request before MCP dispatches any message."""

    def __init__(self, app: ASGIApp, auth: ServiceAuth) -> None:
        self.app = app
        self.auth = auth

    async def __call__(self, scope: Scope, receive: Receive,
                       send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        try:
            actor = self.auth.authenticate_publisher(
                Headers(scope=scope).get("authorization"))
        except ControlPlaneError as error:
            response = JSONResponse(error_envelope("mcp", error),
                                    status_code=status_for_error(error.code))
            await response(scope, receive, send)
            return
        scope.setdefault("state", {})["actor"] = actor
        await self.app(scope, receive, send)


class _RejectClientIdentity:
    """An MCP request cannot choose its own actor or roles.

    The SDK argument model ignores unknown fields, so this runs on the raw
    params before validation to match the JSON transport's structured refusal.
    """

    async def __call__(self, ctx: Any, call_next: Any) -> Any:
        if ctx.method == "tools/call" and isinstance(ctx.params, Mapping):
            name = ctx.params.get("name")
            arguments = ctx.params.get("arguments")
            allowed = TOOL_ARGUMENTS.get(name) if isinstance(name, str) else None
            if allowed is not None and isinstance(arguments, Mapping):
                extra = sorted(set(arguments) - allowed)
                if extra:
                    detail: dict[str, Any] = {"fields": extra}
                    if set(extra) & IDENTITY_FIELDS:
                        detail["reason"] = "identity comes from the bearer token"
                        raise MCPError(
                            INVALID_PARAMS,
                            "identity comes from the bearer token, not request "
                            "arguments", detail)
                    raise MCPError(INVALID_PARAMS,
                                   "unknown argument for tool", detail)
        return await call_next(ctx)


class _McpActorBinding:
    """Bind the authenticated Actor for one MCP request.

    The ASGI wrapper already rejected bad credentials; this resolves the
    identity for tool execution so no tool accepts actor_id/roles arguments.
    """

    def __init__(self, auth: ServiceAuth) -> None:
        self.auth = auth

    async def __call__(self, ctx: Any, call_next: Any) -> Any:
        request = ctx.request
        actor = getattr(request.state, "actor", None) if request is not None else None
        if actor is None:
            header = (request.headers.get("authorization")
                      if request is not None else None)
            actor = self.auth.authenticate_publisher(header)
        supplied: Mapping[str, Any] | None = None
        if ctx.method == "tools/call" and isinstance(ctx.params, Mapping):
            candidate = ctx.params.get("arguments")
            if isinstance(candidate, Mapping):
                supplied = candidate
        token = bind_actor(actor)
        arguments_token = bind_request_arguments(supplied)
        try:
            return await call_next(ctx)
        finally:
            reset_request_arguments(arguments_token)
            reset_actor(token)


class _McpDiagnostics:
    """Record finite MCP tool calls in the same bounded service diagnostics.

    Runs after actor binding, so the request arguments are the already
    validated tool arguments. Bodies and credentials are never copied.
    """

    def __init__(self, trace: TraceService) -> None:
        self.trace = trace

    async def __call__(self, ctx: Any, call_next: Any) -> Any:
        if ctx.method != "tools/call" or not isinstance(ctx.params, Mapping):
            return await call_next(ctx)
        name = ctx.params.get("name")
        arguments = ctx.params.get("arguments")
        arguments = arguments if isinstance(arguments, Mapping) else {}
        started = time.monotonic()
        try:
            result = await call_next(ctx)
        except Exception as error:
            # Argument/identity refusal happens before the tool runs. It must
            # still leave a diagnostic naming the operation and request, and
            # then propagate unchanged.
            self._record(name, arguments, started, "error",
                         code=str(getattr(error, "code", "mcp_error"))[:64],
                         message=str(getattr(error, "message", error))[:500])
            raise
        diagnostics = self.trace.diagnostics
        if diagnostics is None:
            return result
        if isinstance(result, Mapping):
            nested = result.get("structuredContent",
                                result.get("structured_content"))
            payload: Mapping[str, Any] = (
                nested if isinstance(nested, Mapping) else result)
            failed = bool(result.get("isError") or result.get("is_error"))
        else:
            structured = getattr(result, "structured_content", None)
            payload = structured if isinstance(structured, Mapping) else {}
            failed = bool(getattr(result, "is_error", False))
        if failed:
            message = ""
            content = (result.get("content") if isinstance(result, Mapping)
                       else getattr(result, "content", None)) or []
            for item in content:
                text = (item.get("text") if isinstance(item, Mapping)
                        else getattr(item, "text", ""))
                if text:
                    message = str(text)[:500]
                    break
            self._record(name, arguments, started, "error",
                         code="tool_error",
                         message=message or "MCP tool returned an error")
            return result
        if "ok" not in payload:
            outcome = "observed"
            ok = True
            error = None
        else:
            ok = payload.get("ok") is not False
            outcome = "ok" if ok else "error"
            error = (payload.get("error")
                     if isinstance(payload.get("error"), Mapping) else None)
        self._record(
            name, arguments, started, outcome,
            code=None if error is None else str(error.get("code"))[:64],
            message=None if error is None else str(error.get("message", ""))[:500],
            status=(200 if ok else status_for_error(
                str((error or {}).get("code")))))
        return result

    def _record(self, name: Any, arguments: Mapping[str, Any], started: float,
                outcome: str, *, code: str | None = None,
                message: str | None = None,
                status: int | None = None) -> None:
        diagnostics = self.trace.diagnostics
        if diagnostics is None:
            return
        diagnostics.record(
            operation=f"mcp:{str(name)[:120]}",
            outcome=outcome,
            status=status,
            duration_ms=int((time.monotonic() - started) * 1000),
            **_association(arguments),
            error=(None if code is None else {
                "code": code, "message": message or ""}))


def _association(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Only safe, already-public association fields; never a request body."""
    fields: dict[str, Any] = {}
    for key in ("request_id", "run_id", "node_id", "worker_id",
                "epoch", "delivery_key"):
        value = arguments.get(key)
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, (str, int)):
            fields[key] = value[:128] if isinstance(value, str) else value
    return fields


def _scalar(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (str, int, float)):
        return value[:128] if isinstance(value, str) else value
    return None


def _query_int(request: Request, name: str, default: int) -> int:
    raw = request.query_params.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as error:
        raise ControlPlaneError(
            "invalid_argument", f"{name} must be an integer") from error
    if value < 0:
        raise ControlPlaneError("invalid_argument",
                                f"{name} must be >= 0")
    return value


def _sse_frame(event: str, payload: Any, *, event_id: str | None = None) -> bytes:
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"),
                      sort_keys=True)
    for line in data.splitlines() or [""]:
        lines.append(f"data: {line}")
    return ("\n".join(lines) + "\n\n").encode("utf-8")


def _is_loopback(bind: str) -> bool:
    if bind == "localhost":
        return True
    try:
        return ipaddress.ip_address(bind).is_loopback
    except ValueError:
        return False


def _endpoint(bind: str, port: int) -> str:
    host = f"[{bind}]" if ":" in bind else bind
    return f"http://{host}:{port}"


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temp, 0o600)
    os.replace(temp, path)
