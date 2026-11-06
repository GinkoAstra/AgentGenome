"""Client-side service discovery. No Host, Api, Store or SQLite access.

Launchers and CLI commands locate one running service through its durable
service record and a loopback HTTP probe. Reading the record or probing health
must never create a database, credential file or data directory, so a
disconnected client cannot silently become a second state owner.

The default per-user instance lives under ``$XDG_STATE_HOME`` (or
``~/.local/state``) in ``agentgenome/default``. An explicit ``--data-dir``
always names a different instance; credentials and databases never cross.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

from .resources import ControlPlaneError
from .service_auth import ServiceAuth

SERVICE_FORMAT = "agentgenome-api-service/1"
SERVICE_FILENAME = "service.json"
PROBE_TIMEOUT = 1.5


@dataclass(frozen=True)
class Connection:
    data_dir: Path
    endpoint: str
    token: str
    service_id: str
    instance_id: str | None
    pid: int | None
    ready: bool
    health: dict[str, Any]


def default_data_dir() -> Path:
    """The single per-user default instance, independent of any cwd."""
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".local" / "state"
    return (root / "agentgenome" / "default").resolve()


def resolve_data_dir(data_dir: str | Path | None = None) -> Path:
    chosen = default_data_dir() if data_dir is None else Path(data_dir)
    try:
        return chosen.expanduser().resolve()
    except (OSError, RuntimeError) as error:
        raise ControlPlaneError(
            "invalid_argument", "data directory cannot be resolved",
            data_dir=str(data_dir)) from error


def read_record(data_dir: str | Path) -> dict[str, Any] | None:
    path = Path(data_dir) / SERVICE_FILENAME
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("format") != SERVICE_FORMAT:
        return None
    return record


def probe(endpoint: str | None) -> dict[str, Any] | None:
    """GET /healthz on loopback; never follow a proxy or another host."""
    if not isinstance(endpoint, str) or not endpoint.startswith("http://"):
        return None
    opener = urlrequest.build_opener(urlrequest.ProxyHandler({}))
    request = urlrequest.Request(endpoint.rstrip("/") + "/healthz",
                                 method="GET",
                                 headers={"Accept": "application/json"})
    try:
        with opener.open(request, timeout=PROBE_TIMEOUT) as response:
            if response.status != 200:
                return None
            body = json.loads(response.read().decode("utf-8"))
    except (urlerror.URLError, OSError, ValueError):
        return None
    if not isinstance(body, dict) or body.get("ok") is not True:
        return None
    return body


def readiness(endpoint: str) -> dict[str, Any] | None:
    opener = urlrequest.build_opener(urlrequest.ProxyHandler({}))
    request = urlrequest.Request(endpoint.rstrip("/") + "/readyz",
                                 method="GET",
                                 headers={"Accept": "application/json"})
    try:
        with opener.open(request, timeout=PROBE_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urlerror.URLError, OSError, ValueError):
        return None


def identity_matches(record: dict[str, Any],
                     health: dict[str, Any] | None) -> bool:
    """A reused port from another instance is not this data directory."""
    if health is None:
        return False
    if health.get("service_id") != record.get("service_id"):
        return False
    return health.get("instance_id") == record.get("instance_id")


def status(data_dir: str | Path | None = None) -> dict[str, Any]:
    root = resolve_data_dir(data_dir)
    record = read_record(root)
    base: dict[str, Any] = {
        "format": "agentgenome-service-status/1",
        "data_dir": str(root),
        "default": data_dir is None,
        "running": False,
        "ready": False,
        "endpoint": None,
        "service_id": (record or {}).get("service_id"),
        "instance_id": (record or {}).get("instance_id"),
        "pid": None,
        "data_exists": root.exists(),
    }
    if record is None:
        return base
    health = probe(record.get("endpoint"))
    if not identity_matches(record, health):
        return base
    ready = readiness(record["endpoint"]) or {}
    base.update({
        "running": True,
        "ready": ready.get("ready") is True,
        "endpoint": record["endpoint"],
        "pid": health.get("pid"),
        "checks": ready.get("checks"),
        "last_tick_at": ready.get("last_tick_at"),
        "protocols": record.get("protocols"),
    })
    if ready.get("control_loop_error") is not None:
        base["control_loop_error"] = ready["control_loop_error"]
    return base


def connection(data_dir: str | Path | None = None) -> Connection:
    """Return the verified endpoint and publisher token, or raise.

    A stopped service must surface as a network/domain failure; it must never
    create a local database or fall back to in-process execution.
    """
    root = resolve_data_dir(data_dir)
    record = read_record(root)
    if record is None:
        raise ControlPlaneError(
            "service_unavailable", "no running AgentGenome service was recorded",
            data_dir=str(root))
    health = probe(record.get("endpoint"))
    if not identity_matches(record, health):
        raise ControlPlaneError(
            "service_unavailable", "the recorded service is not reachable",
            data_dir=str(root), endpoint=record.get("endpoint"))
    credentials = ServiceAuth(root).existing_publisher_credentials()
    if credentials is None:
        raise ControlPlaneError(
            "service_unavailable", "the service has no publisher credential",
            data_dir=str(root))
    ready_body = readiness(record["endpoint"]) or {}
    return Connection(
        data_dir=root,
        endpoint=record["endpoint"],
        token=credentials["token"],
        service_id=credentials["service_id"],
        instance_id=health.get("instance_id"),
        pid=health.get("pid"),
        ready=ready_body.get("ready") is True,
        health=health,
    )
