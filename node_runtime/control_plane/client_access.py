"""Client-only attachment contract. No Host, Api, Store or implicit startup.

A window attaches to an already running service through discovery and the
public operation transport. Registration is metadata for this server lifetime;
it is not an execution identity and never changes Actor permissions.
"""
from __future__ import annotations
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .access_contracts import ClientKind, ClientRegistration
from .client import ApiClient
from .discovery import Connection, connection as discover_connection
from .resources import ControlPlaneError


class ServiceChangedError(ControlPlaneError):
    """Rediscovery found a different service identity at the same data dir."""

    def __init__(self, expected: str, observed: str) -> None:
        super().__init__(
            "invalid_argument",
            "the discovered service_id changed; attach deliberately to the new "
            "service instead of migrating calls",
            expected_service_id=expected, observed_service_id=observed)
        # The framework names this client-side outcome explicitly; keep the
        # stable code without widening the server's public error allowlist.
        self.code = "service_changed"
        self.args = (f"{self.code}: {self.message}",)


class ServiceUnavailableError(ControlPlaneError):
    """No reachable recorded service; discovery performed no write."""

    def __init__(self, cause: ControlPlaneError) -> None:
        super().__init__("invalid_argument", cause.message, **cause.details)
        self.code = "service_unavailable"
        self.args = (f"{self.code}: {self.message}",)


def _discover(data_dir: str | Path | None) -> Connection:
    """Discovery without creating state; normalize its unavailable outcome.

    ``discovery.connection`` uses the requested ``service_unavailable`` code,
    which is not part of the server-side ERROR_CODES allowlist and would be
    coerced to ``internal``. Only that coercion is corrected here; malformed
    credentials and invalid arguments keep their own codes.
    """
    try:
        return discover_connection(data_dir)
    except ControlPlaneError as error:
        if error.code == "internal":
            raise ServiceUnavailableError(error) from error
        raise


class ClientConnection:
    """One user's window attached to an existing service, not a Worker."""

    def __init__(self, *, connection: Connection,
                 registration: ClientRegistration,
                 api: ApiClient | None = None) -> None:
        self.connection = connection
        self.registration = registration
        self.api = api or ApiClient(connection.endpoint, connection.token)
        self._closed = False

    def call(self, operation: str,
             arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Forward once. Never replay an operation after unknown outcome.

        Transport failures keep their ``ApiTransportError`` and
        ``unknown_outcome`` contract; this wrapper adds no retry and no
        fallback to local state.
        """
        return self.api.call(operation, arguments)

    def reconnect(self) -> ClientRegistration:
        """Explicitly rediscover the SAME service_id and register the session.

        A new instance_id is expected after restart. A changed service_id
        requires a new deliberate attachment; do not migrate writes silently.
        """
        fresh = _discover(self.connection.data_dir)
        if fresh.service_id != self.connection.service_id:
            raise ServiceChangedError(self.connection.service_id,
                                      fresh.service_id)
        api = ApiClient(fresh.endpoint, fresh.token)
        registration = _register(api, self.registration)
        self.connection = fresh
        self.api = api
        self.registration = registration
        return registration

    def close(self) -> None:
        """Best-effort idempotent detach. Never stops service or run."""
        if self._closed:
            return
        self._closed = True
        try:
            self.api.call("unregister_client",
                          {"client_id": self.registration["client_id"]})
        except Exception:
            # Detach must not mask the caller's original failure or pretend
            # that the service/run stopped.
            return


def _register(api: ApiClient,
              previous: ClientRegistration) -> ClientRegistration:
    result = api.call("register_client", {
        "client_kind": previous["client_kind"],
        "client_session_id": previous["client_session_id"],
        "workspace": previous["workspace"],
    })
    if not isinstance(result, Mapping):
        raise ControlPlaneError(
            "internal", "service returned an invalid client registration")
    return dict(result)  # type: ignore[return-value]


def connect_client(*, data_dir: str | Path | None = None,
                   client_kind: ClientKind, client_session_id: str,
                   workspace: str | Path) -> ClientConnection:
    """Resolve default discovery and attach to a running service via API.

    Never constructs Host/Api/SQLite and never starts a service. A missing or
    unreachable service is reported as ``service_unavailable``; starting a
    service is a separate lifecycle action.
    """
    attachment = _discover(data_dir)
    api = ApiClient(attachment.endpoint, attachment.token)
    registration = api.call("register_client", {
        "client_kind": client_kind,
        "client_session_id": client_session_id,
        "workspace": str(workspace),
    })
    if not isinstance(registration, Mapping):
        raise ControlPlaneError(
            "internal", "service returned an invalid client registration")
    return ClientConnection(connection=attachment,
                            registration=dict(registration),  # type: ignore[arg-type]
                            api=api)
