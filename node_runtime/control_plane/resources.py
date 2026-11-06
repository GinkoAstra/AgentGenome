"""Resource, fact and identity schemas for the unified control plane.

Pure validation only: no I/O, no state. The store and API rely on these helpers
so that a malformed request is rejected before a transaction starts.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Iterable, Mapping

RESOURCE_FORMAT = "agentgenome-control-plane-resource/1"
DEFINITION_FORMAT = "agentgenome-graph-definition/2"
EVENT_FORMAT = "agentgenome-control-plane-event/1"

ROLES = frozenset({"publisher", "worker", "reviewer", "controller", "scheduler"})
DEFINITION_POLICIES = frozenset({"fixed", "adaptive"})
JOIN_RULES = frozenset({"all"})
VERDICTS = frozenset({"accept", "revise", "expand"})

NODE_STATES = (
    "pending",
    "ready",
    "running",
    "waiting_decision",
    "waiting_children",
    "awaiting_review",
    "succeeded",
    "failed",
    "cancelled",
    "blocked",
)
TERMINAL_NODE_STATES = frozenset({"succeeded", "failed", "cancelled"})
WORKER_OBSERVED = frozenset({"starting", "active", "idle", "stopped", "failed", "unknown"})
WORKER_DESIRED = frozenset({"active", "stopped"})
RUN_DESIRED = frozenset({"running", "paused", "cancelled"})

RUN_STATUSES = (
    "pending", "running", "waiting", "paused", "completed", "failed", "cancelled", "blocked",
)
RUN_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"running", "paused", "failed", "cancelled", "blocked"}),
    "running": frozenset({"waiting", "paused", "completed", "failed", "cancelled", "blocked"}),
    "waiting": frozenset({"running", "paused", "failed", "cancelled", "blocked"}),
    "paused": frozenset({"running", "waiting", "completed", "failed", "cancelled", "blocked"}),
    "blocked": frozenset({"running", "waiting", "paused", "failed", "cancelled"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}

NODE_TRANSITIONS: dict[str, frozenset[str]] = {
    # A Node whose declared bindings cannot be resolved fails before it runs.
    "pending": frozenset({"ready", "failed", "cancelled", "blocked"}),
    "ready": frozenset({"running", "succeeded", "failed", "cancelled", "blocked"}),
    "running": frozenset({"waiting_decision", "awaiting_review", "waiting_children",
                          "succeeded", "failed", "cancelled", "blocked"}),
    "waiting_decision": frozenset({"running", "failed", "cancelled", "blocked"}),
    "waiting_children": frozenset({"running", "awaiting_review", "failed", "cancelled", "blocked"}),
    "awaiting_review": frozenset({"running", "waiting_children", "succeeded", "failed", "cancelled", "blocked"}),
    "blocked": frozenset({"pending", "ready", "running", "cancelled"}),
    "succeeded": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}

SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
ARTIFACT_ID = re.compile(r"^sha256:[0-9a-f]{64}$")


class ControlPlaneError(Exception):
    """Stable public error code with a JSON-safe detail payload."""

    def __init__(self, code: str, message: str = "", **details: Any) -> None:
        if code not in ERROR_CODES:
            code = "internal"
        self.code = code
        self.message = message or code
        self.details = details
        super().__init__(f"{self.code}: {self.message}")


ERROR_CODES = frozenset({
    "invalid_argument",
    "unauthorized",
    "not_found",
    "revision_conflict",
    "idempotency_conflict",
    "stale_epoch",
    "stale_observation",
    "epoch_in_flight",
    "epoch_unreconciled",
    "candidate_mismatch",
    "feedback_pending",
    "decision_conflict",
    "required_children_open",
    "dependency_cycle",
    "reviewer_undeclared",
    "budget_exhausted",
    "capability_unsupported",
    "artifact_mismatch",
    "cursor_expired",
    "internal",
})


def fail(code: str, message: str = "", **details: Any) -> None:
    raise ControlPlaneError(code, message, **details)


# ---------------------------------------------------------------------------
# Canonical JSON and digests


def _check_json(value: Any, path: str = "$") -> None:
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            fail("invalid_argument", "Non-finite numbers are not valid JSON", path=path)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _check_json(item, f"{path}[{index}]")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                fail("invalid_argument", "JSON object keys must be nonempty strings", path=path)
            _check_json(item, f"{path}.{key}")
        return
    fail("invalid_argument", f"Unsupported JSON value {type(value).__name__}", path=path)


def is_json(value: Any) -> bool:
    try:
        _check_json(value)
    except ControlPlaneError:
        return False
    return True


def canonical(value: Any) -> str:
    _check_json(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def require(condition: bool, code: str = "invalid_argument", message: str = "", **details: Any) -> None:
    if not condition:
        fail(code, message, **details)


def require_object(value: Any, label: str, *, allowed: Iterable[str] | None = None,
                   required: Iterable[str] = ()) -> Mapping[str, Any]:
    require(isinstance(value, dict), "invalid_argument", f"{label} must be an object")
    keys = set(value)
    if required:
        missing = sorted(set(required) - keys)
        require(not missing, "invalid_argument", f"{label} is missing required fields", missing=missing)
    if allowed is not None:
        extra = sorted(keys - set(allowed))
        require(not extra, "invalid_argument", f"{label} has unknown fields", extra=extra)
    _check_json(value)
    return value


def require_text(value: Any, label: str, *, max_length: int = 4096) -> str:
    require(isinstance(value, str), "invalid_argument",
            f"{label} must be a string (got {type(value).__name__})")
    require(len(value) > 0, "invalid_argument", f"{label} must be a nonempty string")
    require(len(value) <= max_length, "invalid_argument",
            f"{label} must be at most {max_length} characters (got {len(value)})")
    return value


def require_slug(value: Any, label: str) -> str:
    require(isinstance(value, str) and SLUG.match(value) is not None, "invalid_argument",
            f"{label} must be a slug")
    return value


def require_int(value: Any, label: str, *, minimum: int = 0) -> int:
    require(isinstance(value, int) and not isinstance(value, bool) and value >= minimum,
            "invalid_argument", f"{label} must be an integer >= {minimum}")
    return value


def artifact_ref(value: Any, label: str = "artifact_ref") -> dict[str, Any]:
    ref = require_object(value, label, allowed={"id", "sha256", "size", "media_type"},
                         required={"id", "sha256", "size", "media_type"})
    require(isinstance(ref["id"], str) and ARTIFACT_ID.match(ref["id"]) is not None,
            "invalid_argument", f"{label}.id must be sha256:<hex>")
    require(isinstance(ref["sha256"], str) and SHA256_HEX.match(ref["sha256"]) is not None,
            "invalid_argument", f"{label}.sha256 must be 64 lowercase hex characters")
    require(ref["id"] == f"sha256:{ref['sha256']}", "invalid_argument",
            f"{label}.id must equal sha256:{ref['sha256']}")
    require_int(ref["size"], f"{label}.size")
    require_text(ref["media_type"], f"{label}.media_type", max_length=256)
    return {"id": ref["id"], "sha256": ref["sha256"], "size": ref["size"],
            "media_type": ref["media_type"]}


def optional_artifact_ref(value: Any, label: str) -> dict[str, Any] | None:
    return None if value is None else artifact_ref(value, label)


# ---------------------------------------------------------------------------
# Identity


@dataclass(frozen=True)
class Actor:
    """Authenticated caller identity. Transport maps bearer tokens onto this."""

    actor_id: str
    roles: frozenset[str]
    run_ids: frozenset[str] | None = None
    worker_id: str | None = None
    epoch: int | None = None
    controller_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "roles", frozenset(self.roles))
        object.__setattr__(self, "run_ids",
                           None if self.run_ids is None else frozenset(self.run_ids))
        require(isinstance(self.actor_id, str) and 0 < len(self.actor_id) <= 128,
                "invalid_argument", "actor_id must be a nonempty string")
        require(bool(self.roles) and self.roles <= ROLES, "invalid_argument",
                "actor roles must be a nonempty subset of known roles")
        if self.epoch is not None:
            require_int(self.epoch, "actor epoch")
        if self.worker_id is not None:
            require_text(self.worker_id, "actor worker_id", max_length=128)
        if self.controller_id is not None:
            require_text(self.controller_id, "actor controller_id", max_length=128)

    def require_role(self, role: str) -> None:
        require(role in self.roles, "unauthorized", f"actor lacks role {role}", role=role)

    def can_read(self, run_id: str) -> bool:
        return self.run_ids is None or run_id in self.run_ids

    def require_read(self, run_id: str) -> None:
        if not self.can_read(run_id):
            fail("not_found", "run is not visible to this actor", run_id=run_id)


# ---------------------------------------------------------------------------
# Graph definition


# The trusted role ceiling a Node's Worker may narrow. Identity comes from the
# committed Node/Worker binding; a definition cannot grant more than this.
WORKER_TOOLS = (
    {"name": "node.submit_candidate", "version": "1", "scope": "node:self"},
    {"name": "node.request_expansion", "version": "1", "scope": "node:self"},
    {"name": "node.request_decision", "version": "1", "scope": "node:self"},
    {"name": "node.submit_review", "version": "1", "scope": "review:assigned"},
    {"name": "node.resolve_decision", "version": "1", "scope": "decision:parent"},
    {"name": "node.publish_artifact", "version": "1", "scope": "node:self"},
    {"name": "node.revise_acceptance", "version": "1", "scope": "acceptance:children"},
)
REVIEWER_TOOLS = (
    {"name": "node.submit_review", "version": "1", "scope": "review:assigned"},
    {"name": "node.submit_candidate", "version": "1", "scope": "node:self"},
)


def review_bindings(target: Mapping[str, Any],
                    candidate: Mapping[str, Any]) -> dict[str, list[Any]]:
    """Literal frozen inputs of one review_receipt Node.

    The checker sees exactly the candidate version and the target's frozen
    inputs; it never receives the parent Agent's private conversation. Both
    the API (authorization) and the Scheduler (node creation) derive this same
    value, so the two cannot drift.
    """
    candidate_refs = list(candidate.get("output_refs") or [])
    if candidate.get("summary_ref") is not None:
        candidate_refs.append(candidate["summary_ref"])
    return {"candidate": candidate_refs,
            "target_inputs": list(target.get("input_refs") or [])}


def review_node_fields(*, run_id: str, target: Mapping[str, Any],
                       candidate: Mapping[str, Any],
                       reviewer_ref: str) -> dict[str, Any]:
    """The one deterministic review_receipt Node the bound candidate permits.

    The Scheduler creates from this value and the API compares a submitted
    create against the same value; neither side owns a second copy of the id,
    closure or literal input shape.
    """
    node_id = f"{target['node_id']}-review-r{candidate['round']}"
    worker_id = f"{node_id}-w1"
    return {
        "node_id": node_id,
        "run_id": run_id,
        "parent_id": target["node_id"],
        "expansion_id": None,
        "local_child_id": None,
        "template_ref": reviewer_ref,
        "contract_ref": reviewer_ref,
        "input_bindings": review_bindings(target, candidate),
        "delegation": {},
        "dependencies": [],
        "reviewer_ref": reviewer_ref,
        "closure": {
            "kind": "review_receipt",
            "target_node_id": target["node_id"],
            "candidate_id": candidate["candidate_id"],
            "reviewer_ref": reviewer_ref,
            "worker_id": worker_id,
        },
        "state": "pending",
    }




def validate_closure(value: Any, label: str) -> dict[str, Any]:
    """Finite checker closure, bound to one checker Worker and candidate receipt."""
    item = require_object(
        value, label,
        required={"kind", "target_node_id", "candidate_id", "reviewer_ref",
                  "worker_id"},
        allowed={"kind", "target_node_id", "candidate_id", "reviewer_ref",
                 "worker_id"})
    require(item["kind"] == "review_receipt", "capability_unsupported",
            f"{label}.kind is unsupported", kind=str(item["kind"])[:64])
    for name in ("target_node_id", "candidate_id", "reviewer_ref", "worker_id"):
        require_text(item[name], f"{label}.{name}", max_length=128)
    return dict(item)


def require_acyclic(dependencies: Mapping[str, list[str]]) -> None:
    remaining = {name: set(deps) for name, deps in dependencies.items()}
    while remaining:
        ready = [name for name, deps in remaining.items() if not deps]
        if not ready:
            fail("dependency_cycle", "dependency graph contains a cycle",
                 nodes=sorted(remaining))
        for name in ready:
            del remaining[name]
            for deps in remaining.values():
                deps.discard(name)


SCHEMA_TYPES = frozenset({"object", "array", "string", "integer", "number",
                          "boolean", "null"})
SCHEMA_KEYWORDS = frozenset({"type", "properties", "required", "additionalProperties",
                             "items", "enum", "minimum", "maximum", "minLength",
                             "maxLength"})


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _json_equal(left: Any, right: Any) -> bool:
    """JSON scalar identity: booleans never equal numbers; objects ignore key order."""
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if left is None or right is None:
        return left is None and right is None
    if _is_number(left) and _is_number(right):
        return left == right
    if isinstance(left, str) and isinstance(right, str):
        return left == right
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _json_equal(first, second) for first, second in zip(left, right))
    if isinstance(left, dict) and isinstance(right, dict):
        return set(left) == set(right) and all(
            _json_equal(left[key], right[key]) for key in left)
    return False


def _type_matches(name: str, value: Any) -> bool:
    if name == "object":
        return isinstance(value, dict)
    if name == "array":
        return isinstance(value, list)
    if name == "string":
        return isinstance(value, str)
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return _is_number(value)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "null":
        return value is None
    return False


def _type_name(value: Any) -> str:
    for name in ("null", "boolean", "integer", "number", "string", "array", "object"):
        if _type_matches(name, value):
            return name
    return "unknown"


def validate_input_schema(schema: Any, path: str = "input_schema") -> None:
    """Validate the supported JSON-schema subset; unsupported keywords fail closed."""
    require(isinstance(schema, dict), "invalid_argument", f"{path} must be an object")
    unknown = sorted(set(schema) - SCHEMA_KEYWORDS)
    if unknown:
        fail("capability_unsupported", f"{path} uses unsupported keywords",
             path=path, keywords=unknown)
    if "type" in schema:
        raw = schema["type"]
        types = [raw] if isinstance(raw, str) else raw
        require(isinstance(types, list) and types and
                all(isinstance(item, str) and item in SCHEMA_TYPES for item in types),
                "invalid_argument", f"{path}.type is invalid")
    if "properties" in schema:
        properties = schema["properties"]
        require(isinstance(properties, dict), "invalid_argument",
                f"{path}.properties must be an object")
        for name, child in properties.items():
            require_slug(name, f"{path}.properties key")
            validate_input_schema(child, f"{path}.properties.{name}")
    if "required" in schema:
        required = schema["required"]
        require(isinstance(required, list) and
                all(isinstance(name, str) and name for name in required),
                "invalid_argument", f"{path}.required must be a list of names")
        require(len(set(required)) == len(required), "invalid_argument",
                f"{path}.required contains duplicates")
    if "additionalProperties" in schema:
        extra = schema["additionalProperties"]
        if isinstance(extra, dict):
            validate_input_schema(extra, f"{path}.additionalProperties")
        else:
            require(isinstance(extra, bool), "invalid_argument",
                    f"{path}.additionalProperties must be a boolean or schema")
    if "items" in schema:
        validate_input_schema(schema["items"], f"{path}.items")
    if "enum" in schema:
        values = schema["enum"]
        require(isinstance(values, list) and values, "invalid_argument",
                f"{path}.enum must be a nonempty list")
    for name in ("minimum", "maximum"):
        if name in schema:
            require(_is_number(schema[name]), "invalid_argument",
                    f"{path}.{name} must be a number")
    for name in ("minLength", "maxLength"):
        if name in schema:
            require_int(schema[name], f"{path}.{name}", minimum=0)


def validate_schema_value(schema: Mapping[str, Any], value: Any, path: str) -> None:
    """Check a value against the same subset accepted by validate_input_schema."""
    if "enum" in schema and not any(_json_equal(value, allowed)
                                    for allowed in schema["enum"]):
        fail("invalid_argument", f"{path} is not an allowed value",
             path=path, allowed=schema["enum"])
    declared = schema.get("type")
    if declared is not None:
        types = [declared] if isinstance(declared, str) else declared
        if not any(_type_matches(name, value) for name in types):
            fail("invalid_argument", f"{path} has the wrong type",
                 path=path, expected=types, actual=_type_name(value))
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                fail("invalid_argument", f"{path}.{name} is required",
                     path=f"{path}.{name}")
        extra = set(value) - set(properties)
        if extra:
            additional = schema.get("additionalProperties", True)
            if additional is False:
                fail("invalid_argument", f"{path} has undeclared properties",
                     path=path, extra=sorted(extra))
            if isinstance(additional, dict):
                for name in sorted(extra):
                    validate_schema_value(additional, value[name], f"{path}.{name}")
        for name, child in properties.items():
            if name in value:
                validate_schema_value(child, value[name], f"{path}.{name}")
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            validate_schema_value(schema["items"], item, f"{path}[{index}]")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            fail("invalid_argument", f"{path} is shorter than minLength",
                 path=path, actual=len(value), minLength=schema["minLength"])
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            fail("invalid_argument", f"{path} is longer than maxLength",
                 path=path, actual=len(value), maxLength=schema["maxLength"])
    if _is_number(value):
        if "minimum" in schema and value < schema["minimum"]:
            fail("invalid_argument", f"{path} is below minimum",
                 path=path, actual=value, minimum=schema["minimum"])
        if "maximum" in schema and value > schema["maximum"]:
            fail("invalid_argument", f"{path} is above maximum",
                 path=path, actual=value, maximum=schema["maximum"])




# ---------------------------------------------------------------------------
# Facts


VALID_DECISION_STATES = frozenset({"open", "resolved", "cancelled"})


def validate_fact(fact: Any) -> dict[str, Any]:
    item = require_object(fact, "fact", required={"kind", "run_id"})
    kind = item.get("kind")
    if kind == "candidate_submitted":
        return _validate_candidate(item)
    if kind == "review_submitted":
        return _validate_review(item)
    if kind == "expansion_requested":
        return _validate_expansion(item)
    if kind == "decision_requested":
        return _validate_decision_request(item)
    if kind == "decision_resolved":
        return _validate_decision_resolution(item)
    if kind == "worker_observed":
        return _validate_observed(item)
    if kind == "acceptance_revision_requested":
        return _validate_acceptance_revision_request(item)
    fail("invalid_argument", "unknown fact kind", kind=str(kind)[:64])


def _validate_acceptance_revision_request(fact: Mapping[str, Any]
                                          ) -> dict[str, Any]:
    """W05b model-supplied proposal fields only.

    The parent execution identity, origin, authority and frozen upper version
    are derived by the Api from committed state. Accepting them here would let
    a model forge the provenance of its own local criteria.
    """
    item = require_object(
        fact, "acceptance revision request fact",
        required={"kind", "run_id", "target_node_id", "previous_ref",
                  "criteria_ref"},
        allowed={"kind", "run_id", "target_node_id", "previous_ref",
                 "criteria_ref"},
    )
    require_text(item["run_id"], "run_id", max_length=128)
    require_text(item["target_node_id"], "target_node_id", max_length=128)
    item = dict(item)
    item["previous_ref"] = require_text(item["previous_ref"], "previous_ref",
                                        max_length=71)
    require(ARTIFACT_ID.fullmatch(item["previous_ref"]) is not None,
            "invalid_argument", "previous_ref must be a content reference")
    item["criteria_ref"] = artifact_ref(item["criteria_ref"], "criteria_ref")
    return item


def _validate_decision_request(fact: Mapping[str, Any]) -> dict[str, Any]:
    item = require_object(
        fact, "decision request fact",
        required={"kind", "run_id", "node_id", "worker_id", "epoch", "round",
                  "contract_ref", "decision_id", "reason", "question"},
        allowed={"kind", "run_id", "node_id", "worker_id", "epoch", "round",
                 "contract_ref", "decision_id", "reason", "question", "options",
                 "material_refs"},
    )
    require_text(item["run_id"], "run_id", max_length=128)
    require_text(item["node_id"], "node_id", max_length=128)
    require_text(item["worker_id"], "worker_id", max_length=128)
    require_int(item["epoch"], "epoch")
    require_int(item["round"], "round", minimum=1)
    require_text(item["contract_ref"], "contract_ref")
    require_text(item["decision_id"], "decision_id", max_length=128)
    require_text(item["reason"], "reason")
    require_text(item["question"], "question")
    item = dict(item)
    options = item.get("options", [])
    require(isinstance(options, list), "invalid_argument", "options must be a list")
    require(len(options) <= 32, "invalid_argument", "options must have at most 32 entries")
    for index, option in enumerate(options):
        require_text(option, f"options[{index}]", max_length=512)
    require(len(set(options)) == len(options), "invalid_argument",
            "options must be unique")
    item["options"] = list(options)
    refs = item.get("material_refs", [])
    require(isinstance(refs, list), "invalid_argument", "material_refs must be a list")
    item["material_refs"] = [artifact_ref(ref, "material_refs item") for ref in refs]
    return item


def _validate_decision_resolution(fact: Mapping[str, Any]) -> dict[str, Any]:
    item = require_object(
        fact, "decision resolution fact",
        required={"kind", "run_id", "node_id", "worker_id", "epoch",
                  "decision_id", "answer"},
        allowed={"kind", "run_id", "node_id", "worker_id", "epoch",
                 "decision_id", "answer", "option"},
    )
    require_text(item["run_id"], "run_id", max_length=128)
    require_text(item["node_id"], "node_id", max_length=128)
    require_text(item["worker_id"], "worker_id", max_length=128)
    require_int(item["epoch"], "epoch")
    require_text(item["decision_id"], "decision_id", max_length=128)
    require_text(item["answer"], "answer", max_length=8192)
    item = dict(item)
    if "option" in item:
        require_text(item["option"], "option", max_length=512)
    return item


def _validate_candidate(fact: Mapping[str, Any]) -> dict[str, Any]:
    item = require_object(fact, "candidate fact",
                          required={"kind", "run_id", "node_id", "worker_id", "epoch",
                                    "round", "candidate_id", "output_refs", "contract_ref"},
                          allowed={"kind", "run_id", "node_id", "worker_id", "epoch",
                                   "round", "candidate_id", "output_refs", "contract_ref",
                                   "summary_ref"})
    require_text(item["run_id"], "run_id", max_length=128)
    require_text(item["node_id"], "node_id", max_length=128)
    require_text(item["worker_id"], "worker_id", max_length=128)
    require_int(item["epoch"], "epoch")
    require_int(item["round"], "round", minimum=1)
    require_text(item["candidate_id"], "candidate_id", max_length=128)
    require_text(item["contract_ref"], "contract_ref")
    refs = item["output_refs"]
    require(isinstance(refs, list) and refs, "invalid_argument", "output_refs must be nonempty")
    item = dict(item)
    item["output_refs"] = [artifact_ref(ref, "output_refs item") for ref in refs]
    if "summary_ref" in item:
        item["summary_ref"] = optional_artifact_ref(item["summary_ref"], "summary_ref")
    return item


def _validate_review(fact: Mapping[str, Any]) -> dict[str, Any]:
    item = require_object(
        fact, "review fact",
        required={"kind", "run_id", "node_id", "candidate_id", "candidate_round",
                  "verdict", "feedback_ref"},
        allowed={"kind", "run_id", "node_id", "target_node_id", "candidate_id",
                 "candidate_round", "verdict", "feedback_ref", "proposal",
                 "progress"},
    )
    if "target_node_id" in item and item["target_node_id"] != item["node_id"]:
        fail("invalid_argument", "target_node_id must equal node_id")
    require_text(item["node_id"], "node_id", max_length=128)
    require_text(item["candidate_id"], "candidate_id", max_length=128)
    require_int(item["candidate_round"], "candidate_round", minimum=1)
    require(item["verdict"] in VERDICTS, "invalid_argument", "unknown review verdict")
    item = dict(item)
    item["feedback_ref"] = artifact_ref(item["feedback_ref"], "feedback_ref")
    if item["verdict"] == "expand" and "proposal" not in item:
        fail("invalid_argument", "expand verdict requires a proposal")
    if item["verdict"] != "expand" and "proposal" in item:
        fail("invalid_argument", "proposal is only valid with expand")
    if "proposal" in item:
        item["proposal"] = _validate_expansion_payload(item["proposal"])
    if "progress" in item:
        # The reviewer supplies the one business evaluation result. Every other
        # ProgressEvaluation field is derived by the API from committed facts;
        # a model may not name identities, windows or refs.
        progress = require_object(item["progress"], "review progress",
                                  allowed={"result"}, required={"result"})
        result = progress["result"]
        require(isinstance(result, str)
                and result in {"improved", "not_improved", "unknown"},
                "invalid_argument", "review progress.result is unsupported",
                result=str(result)[:64])
        item["progress"] = {"result": result}
    return item


def _validate_expansion(fact: Mapping[str, Any]) -> dict[str, Any]:
    item = require_object(
        fact, "expansion fact",
        required={"kind", "run_id", "node_id", "expansion_id", "children", "join_rule"},
        allowed={"kind", "run_id", "node_id", "expansion_id", "children", "join_rule",
                 "contract_ref"},
    )
    require_text(item["node_id"], "node_id", max_length=128)
    result = {"kind": item["kind"], "run_id": item["run_id"], "node_id": item["node_id"],
              **_validate_expansion_payload({key: item[key] for key in
                                             ("expansion_id", "children", "join_rule")})}
    if "contract_ref" in item:
        result["contract_ref"] = require_text(item["contract_ref"], "contract_ref",
                                              max_length=71)
    return result


def _validate_expansion_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    item = require_object(payload, "expansion payload",
                          required={"expansion_id", "children", "join_rule"},
                          allowed={"expansion_id", "children", "join_rule",
                                   "contract_ref"})
    require_text(item["expansion_id"], "expansion_id", max_length=128)
    require(item["join_rule"] in JOIN_RULES, "invalid_argument",
            "unsupported join_rule", join_rule=str(item["join_rule"])[:64])
    children = item["children"]
    require(isinstance(children, list) and children, "invalid_argument",
            "children must be a nonempty list")
    seen: set[str] = set()
    normalised: list[dict[str, Any]] = []
    for raw in children:
        child = require_object(
            raw, "expansion child",
            required={"local_child_id", "input_bindings"},
            allowed={"local_child_id", "contract_ref", "input_bindings", "dependencies",
                     "template_ref", "reviewer_ref", "delegation"},
        )
        local_id = require_slug(child["local_child_id"], "local_child_id")
        if local_id in seen:
            fail("invalid_argument", "local_child_id must be unique within an expansion")
        seen.add(local_id)
        if "contract_ref" in child:
            require_text(child["contract_ref"], f"child {local_id}.contract_ref")
        require(isinstance(child["input_bindings"], dict), "invalid_argument",
                f"child {local_id}.input_bindings must be an object")
        dependencies = child.get("dependencies", [])
        require(isinstance(dependencies, list) and all(isinstance(dep, str) for dep in dependencies),
                "invalid_argument", f"child {local_id}.dependencies must be a list of names")
        for dep in dependencies:
            if dep == local_id:
                fail("dependency_cycle", "a child cannot depend on itself", child=local_id)
        if "template_ref" in child:
            require_text(child["template_ref"], f"child {local_id}.template_ref")
        if "reviewer_ref" in child:
            require_text(child["reviewer_ref"], f"child {local_id}.reviewer_ref")
        delegation = child.get("delegation", {})
        require(isinstance(delegation, dict), "invalid_argument",
                f"child {local_id}.delegation must be an object")
        require(set(delegation) <= {"context", "tool_profile", "budget", "runtime_options"},
                "invalid_argument", f"child {local_id}.delegation has unknown fields",
                unknown=sorted(set(delegation) -
                               {"context", "tool_profile", "budget", "runtime_options"}))
        for name, value in delegation.items():
            require(isinstance(value, dict), "invalid_argument",
                    f"child {local_id}.delegation.{name} must be an object")
        normalised.append({
            "local_child_id": local_id,
            "contract_ref": child.get("contract_ref"),
            "input_bindings": child["input_bindings"],
            "dependencies": list(dependencies),
            "template_ref": child.get("template_ref"),
            "reviewer_ref": child.get("reviewer_ref"),
            "delegation": {name: dict(value) for name, value in delegation.items()},
        })
    known = {child["local_child_id"] for child in normalised}
    for child in normalised:
        unknown = sorted(set(child["dependencies"]) - known)
        if unknown:
            fail("invalid_argument", "child dependencies must reference siblings",
                 child=child["local_child_id"], unknown=unknown)
    require_acyclic({child["local_child_id"]: list(child["dependencies"]) for child in normalised})
    return {"expansion_id": item["expansion_id"], "join_rule": item["join_rule"],
            "children": normalised}


def _validate_observed(fact: Mapping[str, Any]) -> dict[str, Any]:
    item = require_object(
        fact, "observation fact",
        required={"kind", "run_id", "node_id", "worker_id", "epoch", "intent_seq",
                  "observed"},
        allowed={"kind", "run_id", "node_id", "worker_id", "epoch", "intent_seq",
                 "observed", "session_ref", "checkpoint_ref", "error", "usage",
                 "observation_id", "delivery_key", "activities",
                 "activities_complete"},
    )
    require_text(item["run_id"], "run_id", max_length=128)
    require_text(item["node_id"], "node_id", max_length=128)
    require_text(item["worker_id"], "worker_id", max_length=128)
    require_int(item["epoch"], "epoch")
    require_int(item["intent_seq"], "intent_seq", minimum=1)
    require(item["observed"] in WORKER_OBSERVED, "invalid_argument", "unknown observed state")
    item = dict(item)
    if "observation_id" in item:
        require_int(item["observation_id"], "observation_id", minimum=0)
    if "session_ref" in item:
        require_text(item["session_ref"], "session_ref", max_length=1024)
    if "checkpoint_ref" in item:
        require_text(item["checkpoint_ref"], "checkpoint_ref", max_length=1024)
    if "delivery_key" in item:
        require_text(item["delivery_key"], "delivery_key", max_length=128)
    if "error" in item:
        require(isinstance(item["error"], dict), "invalid_argument", "error must be an object")
    if "usage" in item:
        usage = require_object(item["usage"], "observation usage")
        require(set(usage) <= {"provider_calls", "tool_calls"}, "invalid_argument",
                "observation usage has unknown counters",
                unknown=sorted(set(usage) - {"provider_calls", "tool_calls"}))
        for name, value in usage.items():
            require_int(value, f"usage {name}", minimum=0)
        item["usage"] = dict(usage)
    has_activities = "activities" in item
    has_complete = "activities_complete" in item
    require(has_activities == has_complete, "invalid_argument",
            "activities and activities_complete must be provided together")
    if has_activities:
        from .execution_facts import validate_activity_snapshot
        require("observation_id" in item, "invalid_argument",
                "activity observations require observation_id")
        require(isinstance(item["activities_complete"], bool), "invalid_argument",
                "activities_complete must be a boolean")
        item["activities"] = [fact.as_dict()
                              for fact in validate_activity_snapshot(item["activities"])]
    return item


# ---------------------------------------------------------------------------
# Plan mutations (scheduler only)


def validate_read_set(read_set: Any) -> list[dict[str, Any]]:
    require(isinstance(read_set, list), "invalid_argument", "read_set must be a list")
    seen: set[tuple[str, str]] = set()
    items: list[dict[str, Any]] = []
    for raw in read_set:
        item = require_object(raw, "read_set item", required={"kind", "id", "revision"},
                              allowed={"kind", "id", "revision"})
        require(item["kind"] in {"run", "node", "worker"}, "invalid_argument",
                "unknown read_set resource kind")
        require_text(item["id"], "read_set id", max_length=128)
        require_int(item["revision"], "read_set revision", minimum=1)
        key = (item["kind"], item["id"])
        if key in seen:
            fail("invalid_argument", "read_set contains duplicate resources")
        seen.add(key)
        items.append({"kind": item["kind"], "id": item["id"], "revision": item["revision"]})
    return items


MUTABLE_NODE_FIELDS = frozenset({"state", "round", "accepted_candidate_ref", "input_refs"})
MUTABLE_WORKER_FIELDS = frozenset({"intent_seq", "desired", "observed", "session_ref",
                                   "checkpoint_ref", "last_event_id", "epoch",
                                   # Protected rebind only: the Api accepts
                                   # these solely on an epoch replacement
                                   # whose pure recomposition it verifies.
                                   "context", "tool_profile", "budget",
                                   "required_capabilities"})
MUTABLE_RUN_FIELDS = frozenset({"status", "summary"})


def validate_mutations(mutations: Any) -> list[dict[str, Any]]:
    require(isinstance(mutations, list) and mutations, "invalid_argument",
            "mutations must be a nonempty list")
    items: list[dict[str, Any]] = []
    for raw in mutations:
        item = require_object(raw, "mutation", required={"kind", "op"},
                              allowed={"kind", "op", "id", "fields"})
        kind, op = item["kind"], item["op"]
        if kind == "node" and op == "create":
            fields = require_object(
                item.get("fields"), "node create fields",
                required={"node_id", "run_id", "template_ref", "contract_ref",
                          "dependencies", "reviewer_ref"},
                allowed={"node_id", "run_id", "parent_id", "template_ref", "contract_ref",
                         "input_bindings", "input_refs", "dependencies", "reviewer_ref",
                         "state", "round", "expansion_id", "local_child_id", "closure",
                         "delegation"},
            )
            require_text(fields["node_id"], "node_id", max_length=128)
            require_text(fields["run_id"], "run_id", max_length=128)
            require_text(fields["template_ref"], "template_ref")
            require_text(fields["contract_ref"], "contract_ref")
            require_text(fields["reviewer_ref"], "reviewer_ref", max_length=128)
            dependencies = fields["dependencies"]
            require(isinstance(dependencies, list) and
                    all(isinstance(dep, str) and dep for dep in dependencies),
                    "invalid_argument", "dependencies must be a list of node ids")
            state = fields.get("state", "pending")
            require(state in NODE_STATES, "invalid_argument", "unknown node state")
            if "closure" in fields:
                validate_closure(fields["closure"], "node create closure")
            if "delegation" in fields:
                delegation = require_object(
                    fields["delegation"], "node create delegation",
                    allowed={"context", "tool_profile", "budget", "runtime_options"})
                for name in ("context", "tool_profile", "budget", "runtime_options"):
                    if name in delegation:
                        require_object(delegation[name],
                                       f"node create delegation.{name}")
                fields = dict(fields)
                fields["delegation"] = dict(delegation)
            items.append({"kind": "node", "op": "create", "fields": dict(fields)})
        elif kind == "node" and op == "update":
            fields = require_object(item.get("fields"), "node update fields")
            require(set(fields) <= MUTABLE_NODE_FIELDS, "unauthorized",
                    "node update may only change mutable fields",
                    forbidden=sorted(set(fields) - MUTABLE_NODE_FIELDS))
            require_text(item.get("id"), "node id", max_length=128)
            if "state" in fields:
                require(fields["state"] in NODE_STATES, "invalid_argument", "unknown node state")
            if "round" in fields:
                require_int(fields["round"], "round")
            items.append({"kind": "node", "op": "update", "id": item["id"],
                          "fields": dict(fields)})
        elif kind == "worker" and op == "create":
            fields = require_object(
                item.get("fields"), "worker create fields",
                required={"worker_id", "node_id", "backend_ref", "agent_ref", "controller_id"},
                allowed={"worker_id", "node_id", "backend_ref", "agent_ref", "controller_id",
                         "epoch", "desired", "context", "tool_profile", "budget",
                         "checkpoint", "required_capabilities", "runtime_options"},
            )
            require_text(fields["worker_id"], "worker_id", max_length=128)
            require_text(fields["node_id"], "node_id", max_length=128)
            require_text(fields["backend_ref"], "backend_ref")
            require_text(fields["agent_ref"], "agent_ref")
            require_text(fields["controller_id"], "controller_id", max_length=128)
            if "epoch" in fields:
                require_int(fields["epoch"], "epoch")
            if "desired" in fields:
                require(fields["desired"] in WORKER_DESIRED, "invalid_argument",
                        "unknown worker desired state")
            for name in ("context", "tool_profile", "budget",
                         "required_capabilities", "runtime_options"):
                if name in fields:
                    require_object(fields[name], f"worker {name}")
            if "checkpoint" in fields and fields["checkpoint"] is not None:
                require_object(fields["checkpoint"], "worker checkpoint")
            items.append({"kind": "worker", "op": "create", "fields": dict(fields)})
        elif kind == "worker" and op == "update":
            fields = require_object(item.get("fields"), "worker update fields")
            require(set(fields) <= MUTABLE_WORKER_FIELDS, "unauthorized",
                    "worker update may only change mutable fields",
                    forbidden=sorted(set(fields) - MUTABLE_WORKER_FIELDS))
            require_text(item.get("id"), "worker id", max_length=128)
            if "intent_seq" in fields:
                require_int(fields["intent_seq"], "intent_seq", minimum=1)
            if "desired" in fields:
                require(fields["desired"] in WORKER_DESIRED, "invalid_argument",
                        "unknown worker desired state")
            if "observed" in fields:
                require(fields["observed"] in WORKER_OBSERVED, "invalid_argument",
                        "unknown observed state")
            if "epoch" in fields:
                require_int(fields["epoch"], "epoch", minimum=1)
            for name in ("context", "tool_profile", "budget",
                         "required_capabilities"):
                if name in fields:
                    require_object(fields[name], f"worker {name}")
            items.append({"kind": "worker", "op": "update", "id": item["id"],
                          "fields": dict(fields)})
        elif kind == "acceptance_revision" and op == "apply":
            require_text(item.get("id"), "acceptance revision event id",
                         max_length=128)
            fields = require_object(item.get("fields", {}),
                                    "acceptance revision apply fields",
                                    allowed=set())
            items.append({"kind": "acceptance_revision", "op": "apply",
                          "id": item["id"], "fields": dict(fields)})
        elif kind == "expansion" and op == "apply":
            require_text(item.get("id"), "expansion id", max_length=128)
            fields = require_object(item.get("fields", {}), "expansion apply fields",
                                    allowed=set())
            items.append({"kind": "expansion", "op": "apply", "id": item["id"],
                          "fields": dict(fields)})
        elif kind == "decision_request" and op == "create":
            require_text(item.get("id"), "decision_request id", max_length=128)
            fields = require_object(
                item.get("fields"), "decision_request create fields",
                required={"source", "node_id", "comparison_ref",
                          "trigger_checkpoint_id"},
                allowed={"source", "node_id", "comparison_ref",
                         "trigger_checkpoint_id"},
            )
            require(fields["source"] == "progress_stalled", "invalid_argument",
                    "decision_request source is unsupported",
                    source=str(fields["source"])[:64])
            require_text(fields["node_id"], "decision_request node_id",
                         max_length=128)
            require(isinstance(fields["comparison_ref"], str)
                    and ARTIFACT_ID.fullmatch(fields["comparison_ref"]) is not None,
                    "invalid_argument",
                    "decision_request comparison_ref must be sha256:<hex>")
            require_text(fields["trigger_checkpoint_id"],
                         "decision_request trigger_checkpoint_id", max_length=128)
            items.append({"kind": "decision_request", "op": "create",
                          "id": item["id"], "fields": dict(fields)})
        elif kind == "run" and op == "update":
            require_text(item.get("id"), "run id", max_length=128)
            fields = require_object(item.get("fields"), "run update fields")
            require(set(fields) <= MUTABLE_RUN_FIELDS, "unauthorized",
                    "run update may only change mutable fields",
                    forbidden=sorted(set(fields) - MUTABLE_RUN_FIELDS))
            if "status" in fields:
                require(fields["status"] in RUN_STATUSES, "invalid_argument", "unknown run status")
            items.append({"kind": "run", "op": "update", "id": item["id"],
                          "fields": dict(fields)})
        else:
            fail("invalid_argument", "unknown mutation", kind=str(kind)[:64], op=str(op)[:64])
    return items


def check_run_transition(current: str, target: str) -> None:
    require(current in RUN_STATUSES and target in RUN_STATUSES, "invalid_argument",
            "unknown run status")
    if current == target:
        return
    if target not in RUN_STATUS_TRANSITIONS[current]:
        fail("invalid_argument", f"illegal run status transition {current} -> {target}",
             current=current, target=target)


def check_node_transition(current: str, target: str) -> None:
    require(current in NODE_STATES and target in NODE_STATES, "invalid_argument",
            "unknown node state")
    if current == target:
        return
    if target not in NODE_TRANSITIONS[current]:
        fail("invalid_argument", f"illegal node transition {current} -> {target}",
             current=current, target=target)
