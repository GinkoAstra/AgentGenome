"""Server-lifetime client metadata; never a Worker registry or state database.

ApiServer owns one instance and passes it to the common HTTP/MCP dispatcher.
Catalog storage/read permissions remain in Api.list_definitions. Registration
is metadata only: it neither grants a role nor changes an Actor's visibility.
"""
from __future__ import annotations

import copy
import threading
from collections.abc import Mapping
from typing import Any

from .access_contracts import CLIENT_KINDS, ClientRegistration, ServiceInfo
from .http_api import PUBLIC_OPERATIONS
from .resources import Actor, fail, require, require_text
from .storage import new_id, now

MAX_REGISTRATIONS = 1024
SERVICE_INFO_FORMAT = "agentgenome-service-info/1"
SERVICE_PROTOCOL = "agentgenome-control-plane/1"


def _capabilities(backend_ref: str, backend: Any) -> dict[str, Any]:
    getter = getattr(backend, "capabilities", None)
    value = getter() if callable(getter) else backend
    as_dict = getattr(value, "as_dict", None)
    if callable(as_dict):
        value = as_dict()
    if not isinstance(value, Mapping):
        fail("internal", "backend does not describe its capabilities",
             backend_ref=backend_ref)
    return copy.deepcopy(dict(value))


class ServiceAccess:
    def __init__(self, *, service_id: str, instance_id: str,
                 backends: Mapping[str, Any]) -> None:
        """Receive real deployed Backend instances; cache no credential."""
        self.service_id = require_text(service_id, "service_id", max_length=128)
        self.instance_id = require_text(instance_id, "instance_id",
                                        max_length=128)
        self._backends = {ref: _capabilities(ref, backend)
                          for ref, backend in backends.items()}
        self._operations = tuple(PUBLIC_OPERATIONS)
        self._lock = threading.RLock()
        self._registrations: dict[str, ClientRegistration] = {}
        self._owners: dict[str, str] = {}
        self._keys: dict[tuple[str, str, str], str] = {}

    def get_service_info(self, actor: Actor) -> ServiceInfo:
        """Publisher-only view of actual service/capabilities/operations."""
        actor.require_role("publisher")
        with self._lock:
            backends = [{"backend_ref": ref,
                         "capabilities": copy.deepcopy(self._backends[ref])}
                        for ref in sorted(self._backends)]
            operations = list(self._operations)
        return {
            "format": SERVICE_INFO_FORMAT,
            "service_id": self.service_id,
            "instance_id": self.instance_id,
            "protocol": SERVICE_PROTOCOL,
            "backends": backends,
            "operations": operations,
        }

    def register_client(self, actor: Actor, *, client_kind: str,
                        client_session_id: str,
                        workspace: str) -> ClientRegistration:
        """Idempotent registration, bounded to 1024 by FIFO recycling.

        A crashed window that never unregisters cannot permanently block new
        connections: at capacity the earliest registered_at record is dropped
        (insertion order on ties). Eviction is metadata-only and never changes
        an Actor's permissions or a run.
        """
        actor.require_role("publisher")
        require(isinstance(client_kind, str) and client_kind in CLIENT_KINDS,
                "invalid_argument", "unsupported client_kind",
                client_kind=str(client_kind)[:32],
                supported=sorted(CLIENT_KINDS))
        require_text(client_session_id, "client_session_id", max_length=128)
        require_text(workspace, "workspace", max_length=4096)
        key = (actor.actor_id, client_kind, client_session_id)
        with self._lock:
            existing = self._keys.get(key)
            if existing is not None and existing in self._registrations:
                record = self._registrations[existing]
                record["workspace"] = workspace
                return dict(record)
            if len(self._registrations) >= MAX_REGISTRATIONS:
                oldest = min(
                    self._registrations,
                    key=lambda client_id: self._registrations[client_id][
                        "registered_at"])
                self._drop(oldest)
            client_id = f"client-{new_id()}"
            record: ClientRegistration = {
                "client_id": client_id,
                "service_id": self.service_id,
                "instance_id": self.instance_id,
                "client_kind": client_kind,
                "client_session_id": client_session_id,
                "workspace": workspace,
                "registered_at": now(),
            }
            self._registrations[client_id] = record
            self._owners[client_id] = actor.actor_id
            self._keys[key] = client_id
            return dict(record)

    def unregister_client(self, actor: Actor,
                          *, client_id: str) -> dict[str, Any]:
        """Best-effort detach; must not cancel a run or stop an execution."""
        actor.require_role("publisher")
        require_text(client_id, "client_id", max_length=128)
        with self._lock:
            record = self._registrations.get(client_id)
            if record is None or self._owners.get(client_id) != actor.actor_id:
                return {"client_id": client_id, "removed": False}
            self._drop(client_id)
            return {"client_id": client_id, "removed": True}

    def _drop(self, client_id: str) -> None:
        record = self._registrations.pop(client_id)
        owner = self._owners.pop(client_id, None)
        if owner is not None:
            self._keys.pop((owner, record["client_kind"],
                            record["client_session_id"]), None)
