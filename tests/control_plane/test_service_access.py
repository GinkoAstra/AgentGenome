"""U01: server-lifetime client metadata, registration bounds and dispatch gates."""
from __future__ import annotations

import pytest

from node_runtime.control_plane.http_api import PUBLIC_OPERATIONS, dispatch
from node_runtime.control_plane.resources import Actor, ControlPlaneError
from node_runtime.control_plane.service_access import (MAX_REGISTRATIONS,
                                                      ServiceAccess)

FIELDS = {"client_id", "service_id", "instance_id", "client_kind",
          "client_session_id", "workspace", "registered_at"}


class _Backend:
    def __init__(self, **capabilities):
        self._capabilities = capabilities

    def capabilities(self):
        return dict(self._capabilities)


def _access() -> ServiceAccess:
    return ServiceAccess(
        service_id="service-1", instance_id="instance-1",
        backends={"script": _Backend(resumable_session=True,
                                     live_observation="full",
                                     backend_tools=[]),
                  "pi": _Backend(resumable_session=True,
                                 live_observation="limited",
                                 backend_tools=["read_file"])})


def _publisher(name: str = "publisher") -> Actor:
    return Actor(name, {"publisher"})


def test_service_info_reports_identity_backends_and_operations():
    info = _access().get_service_info(_publisher())

    assert info["format"] == "agentgenome-service-info/1"
    assert info["protocol"] == "agentgenome-control-plane/1"
    assert info["service_id"] == "service-1"
    assert info["instance_id"] == "instance-1"
    assert info["operations"] == list(PUBLIC_OPERATIONS)
    assert [backend["backend_ref"] for backend in info["backends"]] == \
        ["pi", "script"]
    assert info["backends"][1]["capabilities"] == {
        "resumable_session": True, "live_observation": "full",
        "backend_tools": []}
    assert set(info) == {"format", "service_id", "instance_id", "protocol",
                         "backends", "operations"}


def test_service_info_is_publisher_only():
    with pytest.raises(ControlPlaneError) as error:
        _access().get_service_info(Actor("reviewer", {"reviewer"}))
    assert error.value.code == "unauthorized"


def test_service_info_capabilities_are_not_shared_state():
    access = _access()
    first = access.get_service_info(_publisher())
    first["backends"][0]["capabilities"]["backend_tools"].append("write_file")
    second = access.get_service_info(_publisher())
    assert second["backends"][0]["capabilities"]["backend_tools"] == [
        "read_file"]


def test_registration_is_idempotent_and_updates_only_workspace():
    access, actor = _access(), _publisher()
    first = access.register_client(actor, client_kind="codex",
                                   client_session_id="session-a",
                                   workspace="/first")
    second = access.register_client(actor, client_kind="codex",
                                    client_session_id="session-a",
                                    workspace="/second")

    assert set(first) == FIELDS
    assert second["client_id"] == first["client_id"]
    assert second["registered_at"] == first["registered_at"]
    assert second["workspace"] == "/second"
    assert first["service_id"] == "service-1"
    assert first["instance_id"] == "instance-1"


def test_registration_reuse_requires_identical_actor_kind_and_session():
    access = _access()
    base = access.register_client(_publisher("one"), client_kind="pi",
                                  client_session_id="same",
                                  workspace="/w")
    other_actor = access.register_client(_publisher("two"), client_kind="pi",
                                         client_session_id="same",
                                         workspace="/w")
    other_kind = access.register_client(_publisher("one"), client_kind="qoder",
                                        client_session_id="same",
                                        workspace="/w")

    assert len({base["client_id"], other_actor["client_id"],
                other_kind["client_id"]}) == 3


def test_unregister_only_removes_the_callers_own_registration():
    access = _access()
    owner = _publisher("owner")
    intruder = _publisher("intruder")
    record = access.register_client(owner, client_kind="script",
                                    client_session_id="s", workspace="/w")

    assert access.unregister_client(intruder,
                                    client_id=record["client_id"]) == {
        "client_id": record["client_id"], "removed": False}
    again = access.register_client(owner, client_kind="script",
                                   client_session_id="s", workspace="/w")
    assert again["client_id"] == record["client_id"]

    assert access.unregister_client(owner,
                                    client_id=record["client_id"])["removed"]
    assert not access.unregister_client(owner,
                                        client_id=record["client_id"])["removed"]
    fresh = access.register_client(owner, client_kind="script",
                                   client_session_id="s", workspace="/w")
    assert fresh["client_id"] != record["client_id"]


def test_crashed_windows_at_capacity_evict_oldest_and_allow_new_session():
    access = _access()
    actor = _publisher()
    records = [access.register_client(actor, client_kind="pi",
                                      client_session_id=f"session-{index}",
                                      workspace="/w")
               for index in range(MAX_REGISTRATIONS)]
    assert len({record["client_id"] for record in records}) == MAX_REGISTRATIONS

    # Every crashed window left its registration behind; a new window still
    # registers and the earliest record is recycled instead of refused.
    overflow = access.register_client(actor, client_kind="pi",
                                      client_session_id="session-overflow",
                                      workspace="/w")
    assert overflow["client_id"] not in {record["client_id"]
                                         for record in records}
    assert access.unregister_client(
        actor, client_id=records[0]["client_id"])["removed"] is False

    # A surviving session keeps its identity without consuming a new slot.
    kept = access.register_client(actor, client_kind="pi",
                                  client_session_id="session-1",
                                  workspace="/w")
    assert kept["client_id"] == records[1]["client_id"]

    # Re-registering the recycled session is a fresh record, not the old one.
    revived = access.register_client(actor, client_kind="pi",
                                     client_session_id="session-0",
                                     workspace="/w")
    assert revived["client_id"] != records[0]["client_id"]


def test_fifo_eviction_follows_registration_order_and_cleans_owner_keys():
    access = _access()
    actor = _publisher()
    records = [access.register_client(actor, client_kind="pi",
                                      client_session_id=f"session-{index}",
                                      workspace="/w")
               for index in range(MAX_REGISTRATIONS)]

    access.register_client(actor, client_kind="pi",
                           client_session_id="overflow-1", workspace="/w")
    assert access.unregister_client(
        actor, client_id=records[0]["client_id"])["removed"] is False

    # The next-oldest registration is evicted by the following new session.
    access.register_client(actor, client_kind="pi",
                           client_session_id="overflow-2", workspace="/w")
    assert access.unregister_client(
        actor, client_id=records[1]["client_id"])["removed"] is False

    # Later sessions keep their identity and owner key under FIFO pressure.
    for index in (2, MAX_REGISTRATIONS - 1):
        kept = access.register_client(actor, client_kind="pi",
                                      client_session_id=f"session-{index}",
                                      workspace="/w")
        assert kept["client_id"] == records[index]["client_id"]

    # The stale key for an evicted session does not resurface an old client_id.
    stale_key = access.register_client(actor, client_kind="pi",
                                       client_session_id="session-0",
                                       workspace="/w")
    assert stale_key["client_id"] != records[0]["client_id"]


@pytest.mark.parametrize("kind,session,workspace", [
    ("shell", "s", "/w"),
    ("pi", "", "/w"),
    ("pi", "s" * 129, "/w"),
    ("pi", "s", ""),
    ("pi", "s", "w" * 4097),
])
def test_registration_validates_declared_fields(kind, session, workspace):
    with pytest.raises(ControlPlaneError) as error:
        _access().register_client(_publisher(), client_kind=kind,
                                  client_session_id=session,
                                  workspace=workspace)
    assert error.value.code == "invalid_argument"


def test_registration_does_not_grant_roles_or_operations():
    access = _access()
    record = access.register_client(
        _publisher(), client_kind="codex", client_session_id="s",
        workspace="/w")
    assert set(record) == FIELDS
    assert not {"roles", "actor", "actor_id"} & set(record)

    reviewer = Actor("reviewer", {"reviewer"})
    with pytest.raises(ControlPlaneError) as denied:
        access.register_client(reviewer, client_kind="codex",
                               client_session_id="s", workspace="/w")
    assert denied.value.code == "unauthorized"


def test_dispatch_rejects_identity_fields_and_unconfigured_access():
    actor = _publisher()
    extra = dispatch(None, actor, "register_client",
                     {"client_kind": "pi", "client_session_id": "s",
                      "workspace": "/w", "roles": ["publisher"]})
    assert extra["ok"] is False
    assert extra["error"]["code"] == "invalid_argument"

    unconfigured = dispatch(None, actor, "get_service_info", {})
    assert unconfigured["ok"] is False
    assert unconfigured["error"]["code"] == "internal"
