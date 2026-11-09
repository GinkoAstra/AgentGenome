"""U01: published definition catalog ordering, filtering and cursor semantics."""
from __future__ import annotations

import base64
import json

import pytest

from conftest import make_graph
from helpers import Cluster, publisher, rid
from node_runtime.control_plane.resources import Actor, ControlPlaneError

TEACHER = publisher("teacher")


def _cluster(tmp_path) -> Cluster:
    return Cluster(tmp_path / "catalog", None)


def _publish(cluster: Cluster, definition_id: str, *, base_revision: int = 0,
             description: str | None = None) -> dict:
    document = make_graph(definition_id=definition_id)
    document["base_revision"] = base_revision
    if description is not None:
        document["description"] = description
    return cluster.publish(document)


def _ids(page: dict) -> list[str]:
    return [item["definition_id"] for item in page["items"]]


def test_catalog_lists_only_the_newest_revision_with_description(tmp_path):
    cluster = _cluster(tmp_path)
    _publish(cluster, "alpha")
    second = _publish(cluster, "alpha", base_revision=1,
                      description="second revision")

    page = cluster.api.list_definitions(TEACHER)

    assert page["next_cursor"] is None
    assert len(page["items"]) == 1
    item = page["items"][0]
    assert set(item) == {"definition_id", "revision", "definition_ref",
                         "description", "created_at"}
    assert item["definition_id"] == "alpha"
    assert item["revision"] == 2
    assert item["definition_ref"] == second["definition_ref"]
    assert item["description"] == "second revision"
    assert item["created_at"]


def test_catalog_orders_by_definition_id_and_pages_without_duplicates(tmp_path):
    cluster = _cluster(tmp_path)
    for definition_id in ("delta", "alpha", "charlie"):
        _publish(cluster, definition_id)

    first = cluster.api.list_definitions(TEACHER, limit=2)
    second = cluster.api.list_definitions(TEACHER, limit=2,
                                          after=first["next_cursor"])
    full = cluster.api.list_definitions(TEACHER, limit=100)

    assert _ids(first) == ["alpha", "charlie"]
    assert first["next_cursor"]
    assert _ids(second) == ["delta"]
    assert second["next_cursor"] is None
    assert _ids(full) == ["alpha", "charlie", "delta"]
    assert len(set(_ids(full))) == len(full["items"])


def test_query_is_a_literal_substring_of_id_or_description(tmp_path):
    cluster = _cluster(tmp_path)
    _publish(cluster, "alpha", description="Review the report pipeline")
    _publish(cluster, "beta")

    assert _ids(cluster.api.list_definitions(TEACHER, query="alp")) == ["alpha"]
    assert _ids(cluster.api.list_definitions(TEACHER, query="report")) == [
        "alpha"]
    assert _ids(cluster.api.list_definitions(TEACHER, query="REPORT")) == []
    assert _ids(cluster.api.list_definitions(TEACHER, query="beta")) == ["beta"]
    assert _ids(cluster.api.list_definitions(TEACHER, query="")) == [
        "alpha", "beta"]
    assert _ids(cluster.api.list_definitions(TEACHER)) == ["alpha", "beta"]


@pytest.mark.parametrize("limit", [0, 101, True, "5", 1.5, None])
def test_limit_rejects_values_outside_one_to_one_hundred(tmp_path, limit):
    cluster = _cluster(tmp_path)
    with pytest.raises(ControlPlaneError) as error:
        cluster.api.list_definitions(TEACHER, limit=limit)
    assert error.value.code == "invalid_argument"


def test_limit_bounds_are_inclusive(tmp_path):
    cluster = _cluster(tmp_path)
    _publish(cluster, "alpha")
    assert _ids(cluster.api.list_definitions(TEACHER, limit=1)) == ["alpha"]
    assert _ids(cluster.api.list_definitions(TEACHER, limit=100)) == ["alpha"]


def test_query_length_boundary_and_type(tmp_path):
    cluster = _cluster(tmp_path)
    _publish(cluster, "alpha")
    assert cluster.api.list_definitions(
        TEACHER, query="x" * 256)["items"] == []
    with pytest.raises(ControlPlaneError) as too_long:
        cluster.api.list_definitions(TEACHER, query="x" * 257)
    assert too_long.value.code == "invalid_argument"
    with pytest.raises(ControlPlaneError) as wrong_type:
        cluster.api.list_definitions(TEACHER, query=5)
    assert wrong_type.value.code == "invalid_argument"


@pytest.mark.parametrize("after", ["", 123, "not-a-cursor", "abc.123"])
def test_after_rejects_malformed_cursors(tmp_path, after):
    cluster = _cluster(tmp_path)
    _publish(cluster, "alpha")
    with pytest.raises(ControlPlaneError) as error:
        cluster.api.list_definitions(TEACHER, after=after)
    assert error.value.code == "invalid_argument"


def test_cursor_corruption_and_query_mismatch_are_rejected(tmp_path):
    cluster = _cluster(tmp_path)
    for definition_id in ("alpha", "alpine", "beta"):
        _publish(cluster, definition_id)

    page = cluster.api.list_definitions(TEACHER, query="al", limit=1)
    cursor = page["next_cursor"]
    assert cursor

    encoded, check = cursor.rsplit(".", 1)
    corrupt_digest = (encoded + "."
                      + ("0" if check[0] != "0" else "1") + check[1:])
    corrupt_payload = f"{encoded}A.{check}"
    with pytest.raises(ControlPlaneError) as digest_error:
        cluster.api.list_definitions(TEACHER, query="al",
                                     after=corrupt_digest)
    assert digest_error.value.code == "invalid_argument"
    with pytest.raises(ControlPlaneError) as payload_error:
        cluster.api.list_definitions(TEACHER, query="al",
                                     after=corrupt_payload)
    assert payload_error.value.code == "invalid_argument"

    with pytest.raises(ControlPlaneError) as mismatch:
        cluster.api.list_definitions(TEACHER, after=cursor)
    assert mismatch.value.code == "invalid_argument"

    follow = cluster.api.list_definitions(TEACHER, query="al", after=cursor)
    assert _ids(follow) == ["alpine"]


def test_cursor_is_opaque_and_bound_to_the_normalized_query(tmp_path):
    cluster = _cluster(tmp_path)
    for definition_id in ("alpha", "beta", "charlie"):
        _publish(cluster, definition_id)

    page = cluster.api.list_definitions(TEACHER, limit=1)
    cursor = page["next_cursor"]
    encoded, check = cursor.rsplit(".", 1)
    payload = json.loads(base64.urlsafe_b64decode(
        encoded + "=" * (-len(encoded) % 4)))
    assert payload["format"] == "agentgenome-definition-cursor/1"
    assert payload["after"] == "alpha"
    assert payload["query"] == ""

    # An absent query and an explicit empty query normalize to the same page.
    follow = cluster.api.list_definitions(TEACHER, query="", after=cursor)
    assert _ids(follow) == ["beta", "charlie"]


def test_next_page_observes_a_new_revision_published_between_requests(tmp_path):
    cluster = _cluster(tmp_path)
    for definition_id in ("alpha", "beta", "charlie"):
        _publish(cluster, definition_id)
    first = cluster.api.list_definitions(TEACHER, limit=2)
    assert _ids(first) == ["alpha", "beta"]

    published = _publish(cluster, "charlie", base_revision=1,
                         description="newer charlie")
    second = cluster.api.list_definitions(TEACHER, limit=2,
                                          after=first["next_cursor"])
    assert _ids(second) == ["charlie"]
    assert second["items"][0]["revision"] == 2
    assert second["items"][0]["definition_ref"] == published["definition_ref"]


def test_catalog_requires_publisher_and_grants_no_definition_access(tmp_path):
    cluster = _cluster(tmp_path)
    _publish(cluster, "alpha")
    _publish(cluster, "beta")
    with pytest.raises(ControlPlaneError) as error:
        cluster.api.list_definitions(Actor("reviewer", {"reviewer"}))
    assert error.value.code == "unauthorized"

    # A valid cursor only moves the page; it is not an access token.
    page = cluster.api.list_definitions(TEACHER, limit=1)
    assert page["next_cursor"]
    with pytest.raises(ControlPlaneError) as cursor_denied:
        cluster.api.list_definitions(Actor("reviewer", {"reviewer"}),
                                     after=page["next_cursor"])
    assert cursor_denied.value.code == "unauthorized"
