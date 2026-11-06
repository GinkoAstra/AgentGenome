"""Service-issued identities. Never accept roles from request arguments.

Credentials resolve to existing Actors, not a second permissions model. Two
entry kinds exist: a publisher token for the public operation/MCP surface, and
a delivery token issued by the trusted Backend for exactly one Worker epoch.
A delivery token is accepted only by the Worker tool endpoint; the binding
(run/node/worker/epoch/delivery, review, decision, workspace) is written here
by the Backend from committed state and never supplied by the model.
"""
from __future__ import annotations

from contextvars import ContextVar
import hmac
import json
import os
from pathlib import Path
import secrets
import tempfile
import threading
import uuid
from typing import Any, Mapping

from .resources import ROLES, Actor, ControlPlaneError

AUTH_FORMAT = "agentgenome-service-auth/1"
AUTH_FILENAME = "service-auth.json"
PUBLISHER_KIND = "publisher"
DELIVERY_KIND = "delivery"
ENTRY_KINDS = frozenset({PUBLISHER_KIND, DELIVERY_KIND})
DELIVERY_FIELDS = ("run_id", "node_id", "worker_id", "delivery_key", "workspace")


class ServiceAuth:
    """Persistent private local credentials shared across service restarts.

    Bootstrap a publisher identity only from trusted service configuration.
    Validate a bearer token before dispatch; a client choosing actor_id/roles
    cannot widen access. Do not print credentials in health/log/error output.
    """

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self._issue_lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self.data_dir / AUTH_FILENAME

    def authenticate(self, bearer: str | None) -> Actor:
        entry = self.authenticate_entry(bearer)
        return Actor(entry["actor_id"], frozenset(entry["roles"]))

    def authenticate_entry(self, bearer: str | None) -> dict[str, Any]:
        """Resolve any credential entry or raise ControlPlaneError(unauthorized)."""
        token = _token_value(bearer)
        if not token:
            raise ControlPlaneError("unauthorized", "missing bearer token")
        data = self._load()
        if data is None:
            raise ControlPlaneError("unauthorized", "service has no publisher token")
        for entry in data["tokens"]:
            if hmac.compare_digest(entry["token"], token):
                return entry
        # Never echo the presented value; it may be a nearby secret.
        raise ControlPlaneError("unauthorized", "unknown bearer token")

    def authenticate_publisher(self, bearer: str | None) -> Actor:
        entry = self.authenticate_entry(bearer)
        if _kind(entry) != PUBLISHER_KIND:
            raise ControlPlaneError(
                "unauthorized", "this credential is not valid for the public API")
        return Actor(entry["actor_id"], frozenset(entry["roles"]))

    def authenticate_delivery(self, bearer: str | None) -> dict[str, Any]:
        entry = self.authenticate_entry(bearer)
        if _kind(entry) != DELIVERY_KIND:
            raise ControlPlaneError(
                "unauthorized", "this credential is not valid for worker tools")
        return entry

    def issue_delivery(self, binding: Mapping[str, Any]) -> dict[str, Any]:
        """Issue one idempotent delivery credential for a committed Worker epoch.

        Called only by the trusted Backend in the service process. The binding
        is revalidated against live state on every call, so a persisted token
        cannot outlive its worker epoch.
        """
        payload = _delivery_binding(binding)
        with self._issue_lock:
            data = self._load()
            if data is None:
                data = self._create()
            for entry in data["tokens"]:
                if (_kind(entry) == DELIVERY_KIND
                        and entry.get("binding") == payload):
                    return {"service_id": data["service_id"],
                            "token": entry["token"], "binding": payload}
            entry = {
                "kind": DELIVERY_KIND,
                "token": secrets.token_urlsafe(32),
                "actor_id": payload["worker_id"],
                "roles": ["worker"],
                "binding": payload,
            }
            data["tokens"].append(entry)
            self._write(data)
            return {"service_id": data["service_id"],
                    "token": entry["token"], "binding": payload}

    def publisher_credentials(self) -> dict[str, Any]:
        """Local deployment only: initialize/load publisher connection secret.

        Not a remotely callable operation. Store with owner-only permissions.
        The returned token is for the local launcher/client, never for a
        request body or a public health response.
        """
        data = self._load()
        if data is None:
            data = self._create()
        entry = next(item for item in data["tokens"]
                     if _kind(item) == PUBLISHER_KIND)
        return {"service_id": data["service_id"],
                "actor_id": entry["actor_id"],
                "roles": list(entry["roles"]),
                "token": entry["token"]}

    def service_id(self) -> str:
        data = self._load()
        if data is None:
            data = self._create()
        return data["service_id"]

    def existing_publisher_credentials(self) -> dict[str, Any] | None:
        """Read publisher connection data without creating any file/state."""
        data = self._load()
        if data is None:
            return None
        entry = next((item for item in data["tokens"]
                      if _kind(item) == PUBLISHER_KIND), None)
        if entry is None:
            return None
        return {"service_id": data["service_id"],
                "actor_id": entry["actor_id"],
                "roles": list(entry["roles"]),
                "token": entry["token"]}

    # -- storage ---------------------------------------------------------
    def _create(self) -> dict[str, Any]:
        token = secrets.token_urlsafe(32)
        data = {
            "format": AUTH_FORMAT,
            "service_id": uuid.uuid4().hex,
            "tokens": [{"kind": PUBLISHER_KIND, "token": token,
                        "actor_id": "publisher", "roles": ["publisher"]}],
        }
        self._write(data)
        return data

    def _load(self) -> dict[str, Any] | None:
        path = self.path
        try:
            mode = path.stat().st_mode
        except FileNotFoundError:
            return None
        if mode & 0o077:
            raise ControlPlaneError(
                "capability_unsupported",
                "service credential file is accessible by other users",
                path=str(path))
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ControlPlaneError(
                "capability_unsupported",
                "service credential file is unreadable",
                path=str(path)) from error
        if not isinstance(data, dict) or data.get("format") != AUTH_FORMAT:
            raise ControlPlaneError(
                "capability_unsupported",
                "service credential file has an unknown format",
                path=str(path))
        tokens = data.get("tokens")
        if not isinstance(tokens, list) or not tokens:
            raise ControlPlaneError(
                "capability_unsupported",
                "service credential file has no tokens", path=str(path))
        for entry in tokens:
            if (not isinstance(entry, dict)
                    or not isinstance(entry.get("token"), str)
                    or not entry["token"]
                    or not isinstance(entry.get("actor_id"), str)
                    or not entry["actor_id"]
                    or not isinstance(entry.get("roles"), list)
                    or not entry["roles"]
                    or not set(entry["roles"]) <= ROLES):
                raise ControlPlaneError(
                    "capability_unsupported",
                    "service credential file has an invalid token entry",
                    path=str(path))
            if "kind" in entry and entry["kind"] not in ENTRY_KINDS:
                raise ControlPlaneError(
                    "capability_unsupported",
                    "service credential file has an unknown entry kind",
                    path=str(path))
            if _kind(entry) == DELIVERY_KIND:
                try:
                    _delivery_binding(entry.get("binding"))
                except ControlPlaneError as error:
                    raise ControlPlaneError(
                        "capability_unsupported",
                        "service credential file has an invalid delivery binding",
                        path=str(path), reason=error.message) from error
        if not any(_kind(entry) == PUBLISHER_KIND for entry in tokens):
            raise ControlPlaneError(
                "capability_unsupported",
                "service credential file has no publisher token", path=str(path))
        if not isinstance(data.get("service_id"), str) or not data["service_id"]:
            raise ControlPlaneError(
                "capability_unsupported",
                "service credential file has no service identity",
                path=str(path))
        return data

    def _write(self, data: dict[str, Any]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(
            prefix=".service-auth.", suffix=".tmp", dir=self.data_dir)
        temp = Path(name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(data, handle, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
            os.chmod(self.path, 0o600)
        finally:
            temp.unlink(missing_ok=True)


def _kind(entry: Mapping[str, Any]) -> str:
    # P1 credential files predate the kind field and hold one publisher.
    return str(entry.get("kind") or PUBLISHER_KIND)


def _delivery_binding(binding: Any) -> dict[str, Any]:
    """Normalise and validate the trusted Worker epoch binding."""
    if not isinstance(binding, Mapping):
        raise ControlPlaneError("invalid_argument",
                                "delivery binding must be an object")
    payload: dict[str, Any] = {}
    for field in DELIVERY_FIELDS:
        value = binding.get(field)
        if not isinstance(value, str) or not value:
            raise ControlPlaneError(
                "invalid_argument", f"delivery binding needs {field}",
                field=field)
        payload[field] = value
    epoch = binding.get("epoch")
    if not isinstance(epoch, int) or isinstance(epoch, bool):
        raise ControlPlaneError("invalid_argument",
                                "delivery binding needs an integer epoch")
    payload["epoch"] = epoch
    contract_ref = binding.get("contract_ref")
    if contract_ref is not None:
        if not isinstance(contract_ref, str) or not contract_ref:
            raise ControlPlaneError("invalid_argument",
                                    "delivery contract_ref must be a string")
        payload["contract_ref"] = contract_ref
    for field in ("review", "decision"):
        value = binding.get(field)
        if value is None:
            continue
        if not isinstance(value, Mapping):
            raise ControlPlaneError(
                "invalid_argument", f"delivery {field} binding must be an object")
        try:
            payload[field] = json.loads(json.dumps(dict(value), sort_keys=True))
        except (TypeError, ValueError) as error:
            raise ControlPlaneError(
                "invalid_argument",
                f"delivery {field} binding is not JSON data") from error
    return payload


def _token_value(bearer: str | None) -> str:
    if not isinstance(bearer, str):
        return ""
    value = bearer.strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    return value


# The per-request identity bound by the network MCP adapter. A contextvar
# follows the request into the worker thread that executes a sync tool call.
_CURRENT_ACTOR: ContextVar[Actor | None] = ContextVar(
    "agentgenome_service_actor", default=None)


def bind_actor(actor: Actor) -> Any:
    return _CURRENT_ACTOR.set(actor)


def reset_actor(token: Any) -> None:
    _CURRENT_ACTOR.reset(token)


def current_actor() -> Actor:
    actor = _CURRENT_ACTOR.get()
    if actor is None:
        raise ControlPlaneError(
            "unauthorized",
            "no authenticated identity is bound to this request")
    return actor
