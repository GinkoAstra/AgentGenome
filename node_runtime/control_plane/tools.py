"""System tool protocol for Worker and Reviewer Agents.

A Backend registers these tools in its Agent runtime and passes model output
through `invoke`. Caller identity (run/node/worker/epoch/reviewer) comes from
the bound adapter, never from model arguments. Tool name, version and scope are
checked against the committed tool profile before any API call.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from . import bindings as B
from . import resources as R


class ToolError(RuntimeError):
    def __init__(self, code: str, message: str = "", **details: Any) -> None:
        super().__init__(message or code)
        self.code = code
        self.details = details


@dataclass(frozen=True)
class ToolSpec:
    name: str
    version: str
    scope: str
    effect: str
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "version": self.version, "scope": self.scope,
                "effect": self.effect, "input_schema": dict(self.input_schema),
                "output_schema": dict(self.output_schema)}


# The exact object node.publish_artifact returns. Exposed verbatim to the
# model as the item schema so a candidate cannot cite a bare artifact id.
ARTIFACT_REF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "sha256": {"type": "string"},
        "size": {"type": "integer", "minimum": 0},
        "media_type": {"type": "string"},
    },
    "required": ["id", "sha256", "size", "media_type"],
    "additionalProperties": False,
}


TOOL_SPECS: dict[str, ToolSpec] = {
    "node.submit_candidate": ToolSpec(
        name="node.submit_candidate", version="1", scope="node:self",
        effect="idempotent",
        input_schema={
            "type": "object",
            "properties": {
                "candidate_id": {"type": "string", "minLength": 1},
                "round": {"type": "integer", "minimum": 1},
                "output_refs": {"type": "array", "items": ARTIFACT_REF_SCHEMA},
                "summary_ref": {"type": ["object", "null"]},
            },
            "required": ["candidate_id", "round", "output_refs"],
            "additionalProperties": False,
        },
        output_schema={"type": "object", "required": ["candidate_id"]},
    ),
    "node.request_expansion": ToolSpec(
        name="node.request_expansion", version="1", scope="node:self",
        effect="idempotent",
        input_schema={
            "type": "object",
            "properties": {
                "expansion_id": {"type": "string", "minLength": 1},
                "join_rule": {"type": "string", "enum": ["all"]},
                "children": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["expansion_id", "join_rule", "children"],
            "additionalProperties": False,
        },
        output_schema={"type": "object", "required": ["expansion_id"]},
    ),
    "node.submit_review": ToolSpec(
        name="node.submit_review", version="1", scope="review:assigned",
        effect="idempotent",
        input_schema={
            "type": "object",
            "properties": {
                "candidate_id": {"type": "string", "minLength": 1},
                "candidate_round": {"type": "integer", "minimum": 1},
                "verdict": {"type": "string", "enum": ["accept", "revise", "expand"]},
                "feedback_ref": {"type": "object"},
                "proposal": {"type": "object"},
                "progress": {
                    "type": "object",
                    "properties": {
                        "result": {"type": "string",
                                   "enum": ["improved", "not_improved", "unknown"]},
                    },
                    "required": ["result"],
                    "additionalProperties": False,
                },
            },
            "required": ["candidate_id", "candidate_round", "verdict", "feedback_ref"],
            "additionalProperties": False,
        },
        output_schema={"type": "object", "required": ["verdict"]},
    ),
    "node.publish_artifact": ToolSpec(
        name="node.publish_artifact", version="1", scope="node:self",
        effect="idempotent",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "minLength": 1},
                "media_type": {"type": "string", "minLength": 1},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        output_schema={"type": "object",
                       "required": ["id", "sha256", "size", "media_type"]},
    ),
    "node.request_decision": ToolSpec(
        name="node.request_decision", version="1", scope="node:self",
        effect="idempotent",
        input_schema={
            "type": "object",
            "properties": {
                "decision_id": {"type": "string", "minLength": 1},
                "reason": {"type": "string", "minLength": 1},
                "question": {"type": "string", "minLength": 1},
                "options": {"type": "array", "items": {"type": "string"}},
                "material_refs": {"type": "array", "items": ARTIFACT_REF_SCHEMA},
            },
            "required": ["decision_id", "reason", "question"],
            "additionalProperties": False,
        },
        output_schema={"type": "object",
                       "required": ["decision_id", "node_id", "state"]},
    ),
    "node.resolve_decision": ToolSpec(
        name="node.resolve_decision", version="1", scope="decision:parent",
        effect="idempotent",
        input_schema={
            "type": "object",
            "properties": {
                "decision_id": {"type": "string", "minLength": 1},
                "answer": {"type": "string", "minLength": 1},
                "option": {"type": "string", "minLength": 1},
            },
            "required": ["decision_id", "answer"],
            "additionalProperties": False,
        },
        output_schema={"type": "object",
                       "required": ["decision_id", "node_id", "state"]},
    ),
    "node.revise_acceptance": ToolSpec(
        name="node.revise_acceptance", version="1", scope="acceptance:children",
        effect="idempotent",
        input_schema={
            "type": "object",
            "properties": {
                "target_node_id": {"type": "string", "minLength": 1},
                "previous_ref": {"type": "string", "minLength": 1},
                "criteria_ref": ARTIFACT_REF_SCHEMA,
            },
            "required": ["target_node_id", "previous_ref", "criteria_ref"],
            "additionalProperties": False,
        },
        output_schema={"type": "object",
                       "required": ["target_node_id", "contract_ref"]},
    ),
}

for _spec in TOOL_SPECS.values():
    R.validate_input_schema(dict(_spec.input_schema))
    R.validate_input_schema(dict(_spec.output_schema))


def _grants(profile: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    raw = profile.get("tools") if isinstance(profile, Mapping) else None
    if raw is None:
        return {}
    if isinstance(raw, Mapping):
        raw = list(raw.values())
    if not isinstance(raw, (list, tuple)):
        raise ToolError("capability_unsupported",
                        "tool_profile.tools must be a list or object")
    granted: dict[str, dict[str, Any]] = {}
    for item in raw:
        if isinstance(item, str):
            spec = TOOL_SPECS.get(item)
            if spec is None:
                continue
            granted[item] = {"name": item, "version": spec.version,
                             "scope": spec.scope}
        elif isinstance(item, Mapping) and isinstance(item.get("name"), str):
            granted[item["name"]] = dict(item)
    return granted


class WorkerToolbox:
    """Trusted adapter for one Worker epoch. Model input cannot change identity."""

    def __init__(self, api: Any, *, run_id: str, node_id: str, worker_id: str,
                 epoch: int, tool_profile: Mapping[str, Any],
                 reviewer_ref: str | None = None,
                 review_target: str | None = None,
                 review_candidate: str | None = None,
                 review_round: int | None = None,
                 decision_ids: Iterable[str] | None = None,
                 request_prefix: str | None = None,
                 contract_ref: str | None = None) -> None:
        self.api = api
        self.run_id = run_id
        self.node_id = node_id
        self.worker_id = worker_id
        self.epoch = epoch
        self.profile = dict(tool_profile or {})
        self.granted = _grants(self.profile)
        self.reviewer_ref = reviewer_ref
        self.review_target = review_target
        self.review_candidate = review_candidate
        self.review_round = review_round
        self.decision_ids = frozenset(decision_ids or ())
        self.actor = R.Actor(worker_id, {"worker"}, run_ids={run_id},
                             worker_id=worker_id, epoch=epoch)
        self.request_prefix = request_prefix or f"tool:{worker_id}:{epoch}"
        # The effective contract is fixed when this execution context is
        # created. Reading the Node's current value on every call would let an
        # old execution relabel a candidate after a local revision applied.
        self.contract_ref = contract_ref

    def schemas(self) -> list[dict[str, Any]]:
        return [TOOL_SPECS[name].as_dict() for name in sorted(self.granted)
                if name in TOOL_SPECS]

    def _authorize(self, name: str, arguments: Any, call_id: str) -> ToolSpec:
        if not isinstance(name, str) or name not in TOOL_SPECS:
            raise ToolError("capability_unsupported", "unknown tool", tool=name)
        grant = self.granted.get(name)
        if grant is None:
            raise ToolError("unauthorized", "tool is not granted to this Worker",
                            tool=name)
        spec = TOOL_SPECS[name]
        if str(grant.get("version", spec.version)) != spec.version:
            raise ToolError("capability_unsupported", "tool version is not supported",
                            tool=name, version=grant.get("version"))
        scope = str(grant.get("scope", spec.scope))
        if name == "node.submit_review":
            if scope != "review:assigned" or self.reviewer_ref is None:
                raise ToolError("unauthorized", "review scope was not granted")
        elif name == "node.resolve_decision":
            if scope != "decision:parent" or not self.decision_ids:
                raise ToolError("unauthorized", "decision scope was not granted")
        elif name == "node.revise_acceptance":
            if scope != "acceptance:children":
                raise ToolError("unauthorized",
                                "acceptance revision scope was not granted")
        elif scope != "node:self":
            raise ToolError("unauthorized", "tool scope is not granted",
                            tool=name, scope=scope)
        R.validate_schema_value(dict(spec.input_schema), arguments, "arguments")
        R.require_text(call_id, "call_id", max_length=128)
        if name == "node.resolve_decision":
            self._check_decision_binding(arguments)
        return spec

    def preflight(self, name: str, arguments: Any, *, call_id: str) -> None:
        """Validate everything checkable before the tool has side effects.

        The bridge calls this before materialising feedback text, so a review
        for the wrong candidate is rejected without writing an artifact. The
        tool itself validates again under ``invoke``.
        """
        self._authorize(name, arguments, call_id)
        if name == "node.submit_review":
            self._check_review_binding(arguments)

    def invoke(self, name: str, arguments: Any, *, call_id: str) -> dict[str, Any]:
        spec = self._authorize(name, arguments, call_id)
        if name == "node.submit_candidate":
            output = self._submit_candidate(arguments, call_id)
        elif name == "node.request_expansion":
            output = self._request_expansion(arguments, call_id)
        elif name == "node.publish_artifact":
            raise ToolError("invalid_arguments",
                            "publish_artifact requires trusted file content")
        elif name == "node.request_decision":
            output = self._request_decision(arguments, call_id)
        elif name == "node.resolve_decision":
            output = self._resolve_decision(arguments, call_id)
        elif name == "node.revise_acceptance":
            output = self._revise_acceptance(arguments, call_id)
        else:
            output = self._submit_review(arguments, call_id)
        R.validate_schema_value(dict(spec.output_schema), output, "output")
        return {"ok": True, "tool": name, "version": spec.version,
                "output": output}

    def publish_artifact(self, arguments: Any, *, call_id: str,
                         content: bytes) -> dict[str, Any]:
        """Register trusted workspace bytes under this Worker's identity."""
        spec = self._authorize("node.publish_artifact", arguments, call_id)
        media_type = arguments.get("media_type") or "application/octet-stream"
        ref = self.api.put_artifact(
            self.actor, f"{self.request_prefix}:artifact:{call_id}",
            content, media_type)["artifact"]
        R.validate_schema_value(dict(spec.output_schema), ref, "output")
        return {"ok": True, "tool": spec.name, "version": spec.version,
                "output": ref}

    # -- tools -----------------------------------------------------------
    def _submit_candidate(self, args: Mapping[str, Any], call_id: str) -> dict[str, Any]:
        node = self.api.get_resource(self.actor, "node", self.node_id)
        fact = {
            "kind": "candidate_submitted",
            "run_id": self.run_id,
            "node_id": self.node_id,
            "worker_id": self.worker_id,
            "epoch": self.epoch,
            "round": args["round"],
            "candidate_id": args["candidate_id"],
            "output_refs": args["output_refs"],
            "contract_ref": (self.contract_ref
                             if self.contract_ref is not None
                             else node["contract_ref"]),
        }
        if args.get("summary_ref") is not None:
            fact["summary_ref"] = args["summary_ref"]
        return self.api.append_fact(self.actor, fact,
                                    f"{self.request_prefix}:candidate:{call_id}",
                                    node["revision"])

    def _request_expansion(self, args: Mapping[str, Any],
                           call_id: str) -> dict[str, Any]:
        node = self.api.get_resource(self.actor, "node", self.node_id)
        children = []
        for raw in args["children"]:
            child = dict(raw)
            child["input_bindings"] = self._child_bindings(
                child.get("input_bindings") or {})
            # Only an omitted field defaults to the parent Worker. An explicit
            # name or explicit null passes through so the API rejects invalid
            # input instead of the tool silently rewriting it.
            if "reviewer_ref" not in child:
                child["reviewer_ref"] = "parent"
            children.append(child)
        fact = {
            "kind": "expansion_requested",
            "run_id": self.run_id,
            "node_id": self.node_id,
            "expansion_id": args["expansion_id"],
            "join_rule": args["join_rule"],
            "children": children,
            # Fixed when this execution context was bound; a replaced epoch may
            # not keep expanding under the Node's newer contract.
            "contract_ref": (self.contract_ref
                             if self.contract_ref is not None
                             else node["contract_ref"]),
        }
        return self.api.append_fact(self.actor, fact,
                                    f"{self.request_prefix}:expansion:{call_id}",
                                    node["revision"])

    def _binding_snapshot(self) -> dict[str, Any]:
        resources = self.api.list_resources(self.actor, self.run_id)
        run = self.api.get_resource(self.actor, "run", self.run_id)
        nodes = resources["nodes"]
        return {"run": run, "nodes": nodes,
                "by_id": {node["node_id"]: node for node in nodes},
                "candidates": resources["candidates"]}

    def _child_bindings(self, bindings: Mapping[str, Any]) -> dict[str, Any]:
        """Resolve ``{"from_parent": NAME}`` against the parent's frozen inputs.

        A Worker sees material paths, not artifact ids; this symbolic reference
        lets a parent bind a child to one of its own committed inputs without
        guessing digests. The resolved value must be an artifact the parent
        itself was granted, and a symbolic parent binding is resolved inside
        the parent's own dependency scope before any child can see it.
        """
        parent = self.api.get_resource(self.actor, "node", self.node_id)
        frozen_refs = parent.get("input_refs")
        if frozen_refs is None:
            raise ToolError("candidate_mismatch",
                            "the parent inputs are not frozen yet")
        frozen = {ref["id"]: ref for ref in frozen_refs}
        available = parent.get("input_bindings") or {}
        resolved: dict[str, Any] = {}
        for name, value in bindings.items():
            if isinstance(value, Mapping) and set(value) == {"from_parent"}:
                resolved[name] = self._parent_binding(
                    parent, available, name, value["from_parent"], frozen)
            else:
                # Sibling-relative bindings belong to the child's own scope and
                # are frozen by the Scheduler once its dependencies are accepted.
                resolved[name] = value
        return resolved

    def _parent_binding(self, parent: Mapping[str, Any],
                        available: Mapping[str, Any], name: str, source: Any,
                        frozen: Mapping[str, dict[str, Any]]) -> Any:
        if source not in available:
            raise ToolError("invalid_arguments",
                            "from_parent names an unknown parent input",
                            name=name, source=source,
                            available=sorted(available))
        value = available[source]
        if isinstance(value, list):
            refs = []
            for item in value:
                if not B.is_artifact_ref(item):
                    raise ToolError("invalid_arguments",
                                    "parent input binding is not a frozen artifact",
                                    name=name)
                refs.append(dict(item))
            self._require_frozen(refs, frozen, name)
            return refs
        if B.is_artifact_ref(value):
            self._require_frozen([value], frozen, name)
            return dict(value)
        if isinstance(value, Mapping) and any(key in value for key in B.SYMBOLIC_KEYS):
            refs = B.resolve_binding_refs(self._binding_snapshot(), parent,
                                          {name: value})
            if not refs:
                raise ToolError("invalid_arguments",
                                "parent input binding resolved to no artifacts",
                                name=name)
            self._require_frozen(refs, frozen, name)
            return refs[0] if len(refs) == 1 else refs
        raise ToolError("invalid_arguments",
                        "parent input binding is not a frozen artifact", name=name)

    @staticmethod
    def _require_frozen(refs: list[dict[str, Any]],
                        frozen: Mapping[str, dict[str, Any]], name: str) -> None:
        foreign = [ref["id"] for ref in refs if ref["id"] not in frozen]
        if foreign:
            raise ToolError("candidate_mismatch",
                            "parent input binding names an artifact the parent was not granted",
                            name=name, artifacts=foreign)

    def _check_review_binding(self, args: Mapping[str, Any]) -> None:
        if self.review_target is None:
            raise ToolError("unauthorized", "no review target is bound")
        if args["candidate_id"] != self.review_candidate:
            raise ToolError("candidate_mismatch",
                            "review must target the assigned candidate",
                            expected=self.review_candidate,
                            actual=args["candidate_id"])
        if self.review_round is not None and args["candidate_round"] != self.review_round:
            raise ToolError("candidate_mismatch", "review round does not match",
                            expected=self.review_round,
                            actual=args["candidate_round"])

    def _submit_review(self, args: Mapping[str, Any], call_id: str) -> dict[str, Any]:
        self._check_review_binding(args)
        node = self.api.get_resource(self.actor, "node", self.review_target)
        reviewer_actor = R.Actor(self.worker_id, {"reviewer"},
                                 run_ids={self.run_id}, worker_id=self.worker_id,
                                 epoch=self.epoch)
        fact = {
            "kind": "review_submitted",
            "run_id": self.run_id,
            "node_id": self.review_target,
            "candidate_id": args["candidate_id"],
            "candidate_round": args["candidate_round"],
            "verdict": args["verdict"],
            "feedback_ref": args["feedback_ref"],
        }
        if "proposal" in args:
            fact["proposal"] = args["proposal"]
        if "progress" in args:
            fact["progress"] = dict(args["progress"])
        return self.api.append_fact(reviewer_actor, fact,
                                    f"{self.request_prefix}:review:{call_id}",
                                    node["revision"])

    def _check_decision_binding(self, args: Mapping[str, Any]) -> None:
        decision_id = args["decision_id"]
        if decision_id not in self.decision_ids:
            raise ToolError("unauthorized", "decision is not bound to this delivery",
                            decision_id=decision_id)

    def _request_decision(self, args: Mapping[str, Any],
                          call_id: str) -> dict[str, Any]:
        node = self.api.get_resource(self.actor, "node", self.node_id)
        fact = {
            "kind": "decision_requested",
            "run_id": self.run_id,
            "node_id": self.node_id,
            "worker_id": self.worker_id,
            "epoch": self.epoch,
            "round": node["round"] + 1,
            "contract_ref": (self.contract_ref
                             if self.contract_ref is not None
                             else node["contract_ref"]),
            "decision_id": args["decision_id"],
            "reason": args["reason"],
            "question": args["question"],
        }
        if "options" in args:
            fact["options"] = args["options"]
        if "material_refs" in args:
            fact["material_refs"] = args["material_refs"]
        return self.api.append_fact(self.actor, fact,
                                    f"{self.request_prefix}:decision:{call_id}",
                                    node["revision"])

    def _resolve_decision(self, args: Mapping[str, Any],
                          call_id: str) -> dict[str, Any]:
        self._check_decision_binding(args)
        decision = self.api.get_resource(self.actor, "decision_request",
                                         args["decision_id"])
        if decision["run_id"] != self.run_id:
            raise ToolError("unauthorized", "decision belongs to another run")
        node = self.api.get_resource(self.actor, "node", decision["node_id"])
        fact = {
            "kind": "decision_resolved",
            "run_id": self.run_id,
            "node_id": decision["node_id"],
            "worker_id": self.worker_id,
            "epoch": self.epoch,
            "decision_id": args["decision_id"],
            "answer": args["answer"],
        }
        if "option" in args:
            fact["option"] = args["option"]
        return self.api.append_fact(self.actor, fact,
                                    f"{self.request_prefix}:decision-reply:{call_id}",
                                    node["revision"])

    def _revise_acceptance(self, args: Mapping[str, Any],
                           call_id: str) -> dict[str, Any]:
        """Propose a versioned local contract for one direct child.

        Only the target, the client precondition and criteria bytes come from
        the model. The Api derives origin, frozen upper version and actual
        parent authority from committed state in the same transaction.
        """
        target = self.api.get_resource(self.actor, "node",
                                       args["target_node_id"])
        fact = {
            "kind": "acceptance_revision_requested",
            "run_id": self.run_id,
            "target_node_id": args["target_node_id"],
            "previous_ref": args["previous_ref"],
            "criteria_ref": args["criteria_ref"],
        }
        response = self.api.append_fact(
            self.actor, fact,
            f"{self.request_prefix}:acceptance:{call_id}", target["revision"])
        return {"target_node_id": target["node_id"],
                "contract_ref": response["contract_ref"],
                "previous_ref": response["previous_ref"],
                "revision_id": response["revision_id"],
                "state": response["state"]}

    def put_feedback(self, content: bytes | str,
                     media_type: str = "text/plain", *, call_id: str) -> dict[str, Any]:
        """Reviewer-created feedback artifact under its declared identity.

        The request id includes the model's call id: retrying the same call is
        idempotent, while a corrected call in the same delivery is a new write.
        """
        R.require_text(call_id, "call_id", max_length=128)
        reviewer_actor = R.Actor(self.worker_id, {"reviewer"},
                                 run_ids={self.run_id}, worker_id=self.worker_id,
                                 epoch=self.epoch)
        return self.api.put_artifact(reviewer_actor,
                                     f"{self.request_prefix}:feedback:{call_id}",
                                     content, media_type)["artifact"]
