"""Unified-client boundary data, authored by Codex before implementation.

These describe data crossing the API boundary, not execution state or auth.
Runtime validation remains mandatory; TypedDict is not a request validator.
"""
from __future__ import annotations
from typing import Any, Literal, TypedDict, get_args

ClientKind = Literal['pi', 'qoder', 'codex', 'claude', 'script']
CLIENT_KINDS: tuple[ClientKind, ...] = get_args(ClientKind)


class ClientRegistration(TypedDict):
    client_id: str
    service_id: str
    instance_id: str
    client_kind: ClientKind
    client_session_id: str
    workspace: str
    registered_at: str


class BackendSummary(TypedDict):
    backend_ref: str
    capabilities: dict[str, Any]


class ServiceInfo(TypedDict):
    format: str
    service_id: str
    instance_id: str
    protocol: str
    backends: list[BackendSummary]
    operations: list[str]


class DefinitionSummary(TypedDict):
    definition_id: str
    revision: int
    definition_ref: str
    description: str
    created_at: str


class DefinitionPage(TypedDict):
    items: list[DefinitionSummary]
    next_cursor: str | None
