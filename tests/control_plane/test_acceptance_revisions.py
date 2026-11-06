"""W05a component tests for the acceptance revision descriptor."""

import pytest

from node_runtime.control_plane.execution_facts import (
    AcceptanceRevision,
    validate_acceptance_revision,
)
from node_runtime.control_plane.resources import ControlPlaneError, digest

CONTRACT = "sha256:" + "a" * 64
PREVIOUS = "sha256:" + "b" * 64
UPPER = "sha256:" + "c" * 64
AUTHORITY = "sha256:" + "d" * 64
CRITERIA = {"id": "sha256:" + "e" * 64, "sha256": "e" * 64, "size": 128,
            "media_type": "text/markdown"}


def revision(**overrides):
    document = {
        "contract_ref": CONTRACT,
        "previous_ref": PREVIOUS,
        "upper_contract_ref": UPPER,
        "origin": "runtime_derived",
        "criteria_ref": dict(CRITERIA),
        "authority_ref": AUTHORITY,
    }
    document.update(overrides)
    return document


def rejection(document):
    with pytest.raises(ControlPlaneError) as raised:
        validate_acceptance_revision(document)
    assert raised.value.code == "invalid_argument"
    return raised.value


# ---------------------------------------------------------------------------
# Legal histories


def test_valid_revision_round_trips_as_detached_json():
    document = revision()

    parsed = validate_acceptance_revision(document)

    assert isinstance(parsed, AcceptanceRevision)
    assert parsed.as_dict() == document
    assert parsed.origin == "runtime_derived"

    serialized = parsed.as_dict()
    serialized["criteria_ref"]["size"] = 999
    serialized["contract_ref"] = PREVIOUS
    assert parsed.criteria_ref == CRITERIA
    assert parsed.contract_ref == CONTRACT
    digest(parsed.as_dict())


def test_parser_detaches_caller_criteria_ref():
    criteria = dict(CRITERIA)
    document = revision(criteria_ref=criteria)

    parsed = validate_acceptance_revision(document)

    criteria["size"] = 0
    document["criteria_ref"]["media_type"] = "mutated"
    assert parsed.criteria_ref == CRITERIA


def test_human_confirmed_history_may_have_no_runtime_bounds():
    parsed = validate_acceptance_revision(revision(
        previous_ref=None, upper_contract_ref=None, origin="human_confirmed"))

    assert parsed.previous_ref is None
    assert parsed.upper_contract_ref is None
    assert parsed.as_dict()["previous_ref"] is None
    assert parsed.as_dict()["upper_contract_ref"] is None


def test_first_local_version_may_reuse_upper_as_previous():
    parsed = validate_acceptance_revision(revision(previous_ref=UPPER))

    assert parsed.previous_ref == UPPER
    assert parsed.upper_contract_ref == UPPER
    assert parsed.contract_ref != UPPER


def test_successor_local_version_keeps_previous_and_upper_distinct():
    parsed = validate_acceptance_revision(revision(previous_ref=PREVIOUS))

    assert parsed.previous_ref == PREVIOUS
    assert parsed.upper_contract_ref == UPPER
    assert parsed.contract_ref not in (PREVIOUS, UPPER)


# ---------------------------------------------------------------------------
# Origin and version-boundary rules


@pytest.mark.parametrize("origin", ["agent", "", "HUMAN_CONFIRMED", 1, None])
def test_unknown_origin_rejects(origin):
    rejection(revision(origin=origin))


def test_runtime_derived_requires_frozen_upper():
    error = rejection(revision(upper_contract_ref=None))
    assert "upper" in error.message


def test_revision_cannot_point_to_itself():
    rejection(revision(previous_ref=CONTRACT))
    rejection(revision(upper_contract_ref=CONTRACT))


# ---------------------------------------------------------------------------
# Content reference strictness


@pytest.mark.parametrize("field,value", [
    ("contract_ref", "sha256:" + "A" * 64),
    ("contract_ref", "sha256:" + "a" * 63),
    ("contract_ref", "sha256:" + "a" * 65),
    ("contract_ref", "a" * 64),
    ("contract_ref", ""),
    ("contract_ref", CONTRACT + "\n"),
    ("authority_ref", "worker-1"),
    ("authority_ref", AUTHORITY + "\n"),
    ("authority_ref", "sha256:" + "D" * 64),
    ("previous_ref", "sha256:" + "b" * 63),
    ("previous_ref", PREVIOUS + "\n"),
    ("upper_contract_ref", "sha256:" + "C" * 64),
    ("upper_contract_ref", UPPER + "\n"),
])
def test_content_refs_and_authority_reject_bad_values(field, value):
    rejection(revision(**{field: value}))


@pytest.mark.parametrize("criteria", [
    {"id": "nope"},
    {**CRITERIA, "id": "sha256:" + "e" * 63},
    {**CRITERIA, "size": -1},
    {**CRITERIA, "media_type": ""},
    None,
])
def test_criteria_ref_reuses_artifact_ref(criteria):
    rejection(revision(criteria_ref=criteria))


# ---------------------------------------------------------------------------
# Shape, unknowns and immutability


@pytest.mark.parametrize("field", [
    "contract_ref", "previous_ref", "upper_contract_ref", "origin",
    "criteria_ref", "authority_ref"])
def test_every_field_is_required_even_when_nullable(field):
    document = revision()
    document.pop(field)
    rejection(document)


@pytest.mark.parametrize("extra", ["actor", "run_id", "worker_id", "epoch"])
def test_unknown_fields_reject(extra):
    error = rejection(revision(**{extra: "x"}))
    assert error.details["extra"] == [extra]


@pytest.mark.parametrize("document", [None, [], "revision", ()])
def test_revision_must_be_a_json_object(document):
    rejection(document)


def test_revision_is_frozen():
    parsed = validate_acceptance_revision(revision())

    with pytest.raises(Exception):
        parsed.origin = "human_confirmed"
