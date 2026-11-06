"""External HTTP client. No Api, Store, data-dir or implicit service startup.

P1 gives scripts an independent network client; P2 migrates the installed CLI.
"""
from __future__ import annotations

import errno
from http.client import HTTPException
import json
import socket
from typing import Any, Iterator, Mapping
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import quote, urlencode

from .resources import ControlPlaneError

TRANSPORT_UNAVAILABLE = "transport_unavailable"
TRANSPORT_TIMEOUT = "transport_timeout"
TRANSPORT_PROTOCOL = "transport_protocol_error"


class ApiTransportError(RuntimeError):
    """Transport failure; `unknown_outcome` says whether a write may commit.

    Only a failure that proves the request never left this process (for
    example connection refused or name resolution failure) may report a known
    outcome. A dropped, truncated or malformed response after a request was
    sent cannot prove the server did not commit it.
    """

    def __init__(self, code: str, message: str, *,
                 unknown_outcome: bool, details: dict[str, Any] | None = None) -> None:
        self.code = code
        self.message = message
        self.unknown_outcome = unknown_outcome
        self.details = dict(details or {})
        super().__init__(f"{code}: {message}")


class ApiClient:
    def __init__(self, endpoint: str, token: str, *, timeout: float = 30.0) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.token = token
        self.timeout = timeout
        # Never let an environment proxy intercept a loopback service.
        self._opener = urlrequest.build_opener(urlrequest.ProxyHandler({}))

    def call(self, operation: str,
             arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Return the operation result or a structured transport/domain error.

        Do not automatically repeat writes with a different request_id. Timeout
        may mean an already committed request. Never fall back to local state.
        """
        try:
            body = json.dumps(dict(arguments), allow_nan=False).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise ControlPlaneError(
                "invalid_argument",
                "arguments are not JSON serializable") from error
        request = urlrequest.Request(
            f"{self.endpoint}/v1/operations/{quote(operation, safe='')}",
            data=body,
            method="POST",
            headers={"Authorization": f"Bearer {self.token}",
                     "Content-Type": "application/json",
                     "Accept": "application/json"})
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                status = response.status
                raw = response.read()
        except urlerror.HTTPError as error:
            status = error.code
            try:
                raw = error.read()
            except Exception as read_error:  # the status arrived, the body did not
                raise ApiTransportError(
                    TRANSPORT_PROTOCOL,
                    "response body was lost; a write may already be committed",
                    unknown_outcome=True,
                    details={"operation": operation, "status": status}) from read_error
            finally:
                error.close()
        except urlerror.URLError as error:
            raise _transport_failure(operation, error) from error
        except HTTPException as error:
            # The request left this process, so a truncated or malformed
            # response cannot prove it was not committed.
            raise ApiTransportError(
                TRANSPORT_PROTOCOL,
                "response was truncated or malformed after the request was "
                "sent; a write may already be committed",
                unknown_outcome=True,
                details={"operation": operation}) from error
        except OSError as error:
            raise _transport_failure(operation, error) from error

        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ApiTransportError(
                TRANSPORT_PROTOCOL,
                "service returned a non-JSON response; a write may already be "
                "committed",
                unknown_outcome=True,
                details={"operation": operation, "status": status}) from error
        return _envelope_result(envelope, operation=operation, status=status)

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        """Read a service resource over GET; the same envelope rules apply."""
        request = self._get_request(path, params)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                status = response.status
                raw = response.read()
        except urlerror.HTTPError as error:
            status = error.code
            try:
                raw = error.read()
            except Exception as read_error:
                raise ApiTransportError(
                    TRANSPORT_PROTOCOL, "response body was lost",
                    unknown_outcome=False,
                    details={"path": path, "status": status}) from read_error
            finally:
                error.close()
        except urlerror.URLError as error:
            raise _transport_failure(path, error) from error
        except (HTTPException, OSError) as error:
            raise _transport_failure(path, error) from error
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ApiTransportError(
                TRANSPORT_PROTOCOL, "service returned a non-JSON response",
                unknown_outcome=False,
                details={"path": path, "status": status}) from error
        return _envelope_result(envelope, operation=path, status=status,
                                read_only=True)

    def stream(self, path: str,
               params: Mapping[str, Any] | None = None) -> Iterator[dict[str, Any]]:
        """Yield parsed SSE frames from a read-only stream (for `watch`).

        One network boundary for CLI/Pi: no second transport implementation.
        Closing the iterator closes the response; a dropped stream is reported
        as a transport error carrying the last cursor the caller already has.
        """
        request = self._get_request(path, params,
                                    accept="text/event-stream")
        try:
            response = self._opener.open(request, timeout=self.timeout)
        except urlerror.HTTPError as error:
            try:
                raw = error.read()
            except Exception:
                raw = b""
            finally:
                error.close()
            try:
                envelope = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ApiTransportError(
                    TRANSPORT_PROTOCOL, "watch was rejected",
                    unknown_outcome=False,
                    details={"path": path, "status": error.code}) from error
            _envelope_result(envelope, operation=path, status=error.code,
                             read_only=True)
            raise ApiTransportError(
                TRANSPORT_PROTOCOL, "watch was rejected",
                unknown_outcome=False,
                details={"path": path, "status": error.code})
        except urlerror.URLError as error:
            raise _transport_failure(path, error) from error
        try:
            if response.status != 200:
                raise ApiTransportError(
                    TRANSPORT_PROTOCOL, "watch was rejected",
                    unknown_outcome=False,
                    details={"path": path, "status": response.status})
            yield from _sse_frames(response)
        except (HTTPException, OSError) as error:
            raise _transport_failure(path, error) from error
        finally:
            response.close()

    def _get_request(self, path: str, params: Mapping[str, Any] | None,
                     *, accept: str = "application/json") -> Any:
        query = urlencode({key: value for key, value in (params or {}).items()
                           if value is not None})
        url = f"{self.endpoint}{path}" + (f"?{query}" if query else "")
        return urlrequest.Request(
            url, method="GET",
            headers={"Authorization": f"Bearer {self.token}",
                     "Accept": accept})


def _envelope_result(envelope: Any, *, operation: str, status: int,
                     read_only: bool = False) -> Any:
    unknown = not read_only
    if not isinstance(envelope, dict) or "ok" not in envelope:
        raise ApiTransportError(
            TRANSPORT_PROTOCOL,
            "service returned an unexpected envelope",
            unknown_outcome=unknown,
            details={"operation": operation, "status": status})
    if envelope.get("ok") is not True:
        failure = envelope.get("error")
        if not isinstance(failure, dict):
            raise ApiTransportError(
                TRANSPORT_PROTOCOL, "service returned an invalid error",
                unknown_outcome=unknown,
                details={"operation": operation, "status": status})
        details = failure.get("details")
        raise ControlPlaneError(
            failure.get("code", "internal"),
            failure.get("message", ""),
            **(details if isinstance(details, dict) else {}))
    return envelope.get("result")


def _sse_frames(response: Any) -> Iterator[dict[str, Any]]:
    """Minimal SSE parser: `id:`, `event:`, multi-line `data:` and comments."""
    event: dict[str, Any] = {}
    data: list[str] = []
    while True:
        line = response.readline()
        if not line:
            return
        text = line.decode("utf-8", errors="replace").rstrip("\r\n")
        if text == "":
            if data or event:
                yield {
                    "id": event.get("id"),
                    "event": event.get("event", "message"),
                    "data": ("\n".join(data)),
                }
            event, data = {}, []
            continue
        if text.startswith(":"):
            continue
        field, _, value = text.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "data":
            data.append(value)
        elif field in ("id", "event"):
            event[field] = value


def _transport_failure(operation: str,
                       error: BaseException) -> ApiTransportError:
    reason = getattr(error, "reason", error)
    if _is_timeout(error) or _is_timeout(reason):
        return ApiTransportError(
            TRANSPORT_TIMEOUT,
            "request timed out; a write may already be committed",
            unknown_outcome=True, details={"operation": operation})
    if _proven_before_send(reason):
        return ApiTransportError(
            TRANSPORT_UNAVAILABLE, "service is unreachable",
            unknown_outcome=False, details={"operation": operation})
    return ApiTransportError(
        TRANSPORT_UNAVAILABLE,
        "connection failed after the request may have been sent; outcome is "
        "unknown",
        unknown_outcome=True, details={"operation": operation})


def _proven_before_send(error: BaseException) -> bool:
    """Connection refused or name resolution failure means nothing was sent."""
    if isinstance(error, (ConnectionRefusedError, socket.gaierror)):
        return True
    return (isinstance(error, OSError)
            and getattr(error, "errno", None) == errno.ECONNREFUSED)


def _is_timeout(error: BaseException) -> bool:
    return isinstance(error, TimeoutError) or getattr(error, "errno", None) in (
        110,  # ETIMEDOUT
        60,   # ETIMEDOUT (macOS)
    )
