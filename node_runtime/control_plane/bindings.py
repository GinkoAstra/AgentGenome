"""Resolve symbolic input bindings inside a Node's own declared scope.

The Scheduler freezes every Node's ``input_refs`` from its bindings once its
dependencies are accepted. A trusted adapter may later reuse one of those
bindings for a child expansion, but it must resolve the binding in the same
scope the parent used; copying the raw symbolic value would let another
expansion reinterpret a local name.
"""
from __future__ import annotations

from typing import Any, Mapping

from .resources import ControlPlaneError


ARTIFACT_KEYS = frozenset({"id", "sha256", "size", "media_type"})
SYMBOLIC_KEYS = ("from", "$from", "run_input")


def is_artifact_ref(value: Any) -> bool:
    return isinstance(value, Mapping) and set(value) == ARTIFACT_KEYS


def resolve_binding_refs(snap: Mapping[str, Any], node: Mapping[str, Any],
                         bindings: Any) -> list[dict[str, Any]]:
    """Return the frozen artifact refs a binding set names for this Node."""
    found: list[dict[str, Any]] = []

    def add_ref(value: Any) -> bool:
        if is_artifact_ref(value):
            found.append({key: value[key] for key in
                          ("id", "sha256", "size", "media_type")})
            return True
        return False

    def resolve(value: Any) -> None:
        if add_ref(value):
            return
        if isinstance(value, list):
            for item in value:
                resolve(item)
            return
        if not isinstance(value, dict):
            return
        if "from" in value or "$from" in value:
            key = value.get("from", value.get("$from"))
            dependency = binding_source(snap, node, key)
            if dependency["state"] != "succeeded":
                raise ControlPlaneError(
                    "candidate_mismatch",
                    "input binding source has no accepted result",
                    from_node=dependency["node_id"], state=dependency["state"])
            span = str(value.get("field", "accepted"))
            accepted = dependency["accepted_candidate_ref"]
            if accepted is None:
                raise ControlPlaneError(
                    "candidate_mismatch", "input binding source has no accepted result",
                    from_node=dependency["node_id"])
            candidate = next((item for item in snap["candidates"]
                              if item["candidate_id"] == accepted), None)
            if candidate is None:
                raise ControlPlaneError("not_found", "accepted candidate is missing",
                                        candidate_id=accepted)
            if span in ("accepted", "outputs", "output_refs"):
                for ref in candidate["output_refs"]:
                    add_ref(ref)
            elif span in ("summary", "summary_ref") and candidate.get("summary_ref"):
                add_ref(candidate["summary_ref"])
            else:
                raise ControlPlaneError("invalid_argument",
                                        "unsupported input binding field", field=span)
            return
        if "run_input" in value:
            run_inputs = snap["run"].get("input_refs") or {}
            if value["run_input"] not in run_inputs:
                raise ControlPlaneError("not_found", "run input is missing",
                                        name=value["run_input"])
            resolve(run_inputs[value["run_input"]])
            return
        for item in value.values():
            resolve(item)

    resolve(bindings)
    unique: dict[str, dict[str, Any]] = {}
    for ref in found:
        unique[ref["id"]] = ref
    return list(unique.values())


def binding_source(snap: Mapping[str, Any], node: Mapping[str, Any],
                   key: str) -> Mapping[str, Any]:
    """Resolve a binding source inside the node's declared dependency scope.

    Sibling or nested expansions may each declare a child with the same
    local name, so a run-wide ``local_child_id`` match could hand one
    expansion's accepted bytes to another. A binding may only name an
    explicit node id or a local child of this node's own expansion, and
    that node must be one of this node's declared dependencies.
    """
    node_id = node["node_id"]
    dependencies = set(node.get("dependencies") or [])
    explicit = snap["by_id"].get(key)
    if explicit is not None:
        if explicit["node_id"] not in dependencies:
            raise ControlPlaneError(
                "unauthorized",
                "input binding source is not a declared dependency",
                from_node=key, node_id=node_id)
        return explicit
    expansion_id = node.get("expansion_id")
    matches = [item for item in snap["nodes"]
               if expansion_id is not None
               and item.get("expansion_id") == expansion_id
               and item.get("local_child_id") == key]
    if not matches:
        raise ControlPlaneError(
            "not_found",
            "input binding references an unknown node or local child",
            from_node=key, node_id=node_id)
    if len(matches) > 1:
        raise ControlPlaneError(
            "invalid_argument",
            "local child id is ambiguous within this expansion",
            from_node=key, expansion_id=expansion_id)
    dependency = matches[0]
    if dependency["node_id"] not in dependencies:
        raise ControlPlaneError(
            "unauthorized",
            "local child alias is not a declared dependency",
            from_node=key, node_id=node_id, expansion_id=expansion_id)
    return dependency
