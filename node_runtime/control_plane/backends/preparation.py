"""Shared frozen input, tool grant and prompt preparation for Agent Backends.

No process, scheduler or transport behavior lives here. Pi and CLI Backends
consume the same committed WorkerSpec and delivery semantics.
"""
from __future__ import annotations
import hashlib
import json
import os
import stat
import threading
from pathlib import Path
from typing import Any, Mapping
from ..resources import canonical, digest
from ..tools import TOOL_SPECS
from .base import BackendError, WorkerSpec

_PROMPT_CONTEXT_KEYS = frozenset({
    "role", "run_id", "node_id", "worker_id", "backend_ref", "agent_ref",
    "template_ref", "contract_ref", "input_refs", "reviewer_ref", "grants",
    "brief", "sop_brief", "instructions", "target_node_id", "candidate_id",
    "candidate_round", "review_inputs", "review_candidate_ids",
    "review_children", "progress", "progress_comparison",
})


SYSTEM_TOOLS = {
    "node.submit_candidate": (
        "Submit one candidate for this Node. output_refs is a list of complete "
        "artifact references exactly as returned by node.publish_artifact: "
        "{\"id\": \"sha256:<hex>\", \"sha256\": \"<64 hex>\", \"size\": <int>, "
        "\"media_type\": \"<type>\"}. Pass each returned object whole; a bare id "
        "or path is rejected."),
    "node.request_expansion": (
        "Ask the Scheduler to create child Nodes from an expansion proposal."),
    "node.request_decision": (
        "Ask this Node's parent for a decision and yield the turn until it "
        "answers. Use this only when the task truly depends on a choice you "
        "cannot make. The parent answer arrives as the next delivery."),
    "node.resolve_decision": (
        "Answer the bound child decision exactly once. decision_id comes from "
        "the delivered decision request. Include option only when the request "
        "offered options."),
    "node.submit_review": (
        "Submit exactly one review for the assigned candidate. feedback_ref "
        "accepts an artifact reference or {\"text\": \"...\"} to register "
        "feedback text under this reviewer. When the delivery states a frozen "
        "progress policy, progress={\"result\": improved|not_improved|unknown} "
        "is mandatory and unknown is the honest answer when the candidate is "
        "unusable or not comparable."),
    "node.revise_acceptance": (
        "Propose a versioned local acceptance contract for one direct child "
        "Node. previous_ref is the child's current contract_ref and "
        "criteria_ref is an artifact reference whose JSON object has "
        "input_schema, output_schema and acceptance. The criteria are ordinary "
        "JSON: formatting and key order are yours; only structure and content "
        "are checked. The frozen parent/upper contract is derived by the "
        "control plane; you cannot change it."),
    "node.publish_artifact": (
        "Publish a file from the worker workspace as an immutable artifact. "
        "path is relative to the workspace root and is exactly the path you "
        "passed to write_file (for example result.json, never "
        "workspace/result.json). Returns the artifact reference to pass in "
        "candidate output_refs."),
}


REVIEW_KINDS = ("review_request", "review_start")


def _sha256_bytes(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


class WorkerPreparation:
    def _verify_binding(self, state: Mapping[str, Any], spec: WorkerSpec) -> None:
        if (state.get("agent_ref") != spec.agent_ref or
                dict(state.get("context") or {}) != dict(spec.context) or
                dict(state.get("tool_profile") or {}) != dict(spec.tool_profile)):
            raise BackendError(
                "capability_unsupported",
                "the committed agent/context/tool profile changed for this execution")

    def _tools(self, state: Mapping[str, Any]) -> tuple[str, ...]:
        profile = state.get("tool_profile") or {}
        if "backend_tools" not in profile:
            return tuple(self.tools)
        raw = profile["backend_tools"]
        if (not isinstance(raw, (list, tuple)) or
                any(not isinstance(name, str) for name in raw)):
            raise BackendError("capability_unsupported",
                               "tool_profile.backend_tools must be a list of tool names")
        unknown = [name for name in raw if name not in self.tools]
        if unknown:
            raise BackendError("capability_unsupported",
                               "the backend does not provide a granted tool",
                               tools=unknown, provided=list(self.tools))
        return tuple(raw)

    def _system_grants(self, state: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        """Validate system-tool grants from the committed Worker profile."""
        profile = state.get("tool_profile") or {}
        raw = profile.get("tools")
        if raw is None:
            return {}
        if isinstance(raw, Mapping):
            raw = list(raw.values())
        if not isinstance(raw, (list, tuple)):
            raise BackendError("capability_unsupported",
                               "tool_profile.tools must be a list or object")
        granted: dict[str, dict[str, Any]] = {}
        for item in raw:
            if isinstance(item, str):
                name, entry = item, {"name": item}
            elif isinstance(item, Mapping) and isinstance(item.get("name"), str):
                name, entry = item["name"], dict(item)
            else:
                raise BackendError("capability_unsupported",
                                   "tool_profile.tools entries need a name")
            if name not in SYSTEM_TOOLS:
                raise BackendError("capability_unsupported",
                                   "the bridge does not provide a granted system tool",
                                   tool=name)
            spec = TOOL_SPECS[name]
            if str(entry.get("version", spec.version)) != spec.version:
                raise BackendError("capability_unsupported",
                                   "the system tool version is not supported",
                                   tool=name, version=entry.get("version"))
            scope = str(entry.get("scope", spec.scope))
            if scope != spec.scope:
                raise BackendError("unauthorized",
                                   "the system tool scope is not granted",
                                   tool=name, scope=scope)
            granted[name] = entry
        if granted and self.credential_issuer is None:
            raise BackendError("capability_unsupported",
                               "system tools require the running network service "
                               "credential issuer")
        return granted

    @staticmethod
    def _review_binding(state: Mapping[str, Any],
                        message: Mapping[str, Any]) -> dict[str, Any] | None:
        if message.get("kind") not in REVIEW_KINDS:
            return None
        target = message.get("target_node_id") or message.get("node_id")
        candidate = message.get("candidate_id")
        round_number = message.get("candidate_round")
        reviewer = message.get("reviewer_ref")
        if not (isinstance(target, str) and target
                and isinstance(candidate, str) and candidate
                and isinstance(round_number, int)
                and isinstance(reviewer, str) and reviewer):
            raise BackendError("capability_unsupported",
                               "the review delivery does not bind a candidate",
                               kind=message.get("kind"))
        return {"target_node_id": target, "candidate_id": candidate,
                "candidate_round": round_number, "reviewer_ref": reviewer}

    @staticmethod
    def _decision_binding(state: Mapping[str, Any],
                          message: Mapping[str, Any]) -> dict[str, Any] | None:
        if message.get("kind") != "decision_request":
            return None
        decision_id = message.get("decision_id")
        child = message.get("child_node_id")
        if not (isinstance(decision_id, str) and decision_id
                and isinstance(child, str) and child):
            raise BackendError("capability_unsupported",
                               "the decision delivery does not bind an id",
                               kind=message.get("kind"))
        return {"decision_id": decision_id, "child_node_id": child}

    def _system_tools(self, state: Mapping[str, Any],
                      message: Mapping[str, Any]
                      ) -> tuple[list[str], list[dict[str, Any]],
                                 dict[str, Any] | None,
                                 dict[str, Any] | None]:
        granted = self._system_grants(state)
        binding = self._review_binding(state, message)
        decision = self._decision_binding(state, message)
        names: list[str] = []
        resources: list[dict[str, Any]] = []
        for name in sorted(granted):
            if name == "node.submit_review" and binding is None:
                # The grant is only live for the delivery that binds a candidate.
                continue
            if name == "node.resolve_decision" and decision is None:
                # The grant is only live for the delivery that binds a decision.
                continue
            names.append(name)
            resources.append({"name": name,
                              "description": SYSTEM_TOOLS[name],
                              "parameters": dict(TOOL_SPECS[name].input_schema)})
        return names, resources, binding, decision

    @staticmethod
    def _frozen_ref(ref: Any) -> tuple[str, str, int]:
        if not isinstance(ref, Mapping) or "id" not in ref or "sha256" not in ref:
            raise BackendError("material_missing",
                               "message/input reference is not a frozen artifact",
                               ref=dict(ref) if isinstance(ref, Mapping) else ref)
        artifact_id = str(ref["id"])
        expected = str(ref["sha256"])
        if artifact_id != f"sha256:{expected}":
            raise BackendError("material_missing",
                               "reference digest is inconsistent",
                               artifact_id=artifact_id)
        size = ref.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise BackendError("material_missing",
                               "reference size is not a byte count",
                               artifact_id=artifact_id)
        return artifact_id, expected, size

    def _read_material(self, ref: Mapping[str, Any]) -> tuple[bytes, str, str, int]:
        artifact_id, expected, size = self._frozen_ref(ref)
        if self.artifact_reader is None:
            raise BackendError("capability_unsupported",
                               "no artifact reader is configured for frozen materials",
                               artifact_id=artifact_id)
        try:
            content = self.artifact_reader(ref)
        except BackendError:
            raise
        except Exception as error:
            raise BackendError("material_missing", "material could not be read",
                               artifact_id=artifact_id) from error
        if not isinstance(content, (bytes, bytearray)):
            raise BackendError("material_missing", "artifact reader must return bytes",
                               artifact_id=artifact_id)
        content = bytes(content)
        if _sha256_bytes(content) != artifact_id:
            raise BackendError("material_missing",
                               "artifact content does not match its frozen digest",
                               artifact_id=artifact_id)
        if len(content) != size:
            raise BackendError("material_missing",
                               "artifact size does not match its frozen reference",
                               artifact_id=artifact_id, expected=size,
                               actual=len(content))
        return content, artifact_id, expected, size

    @staticmethod
    def _material_dir(root: Path, *parts: str) -> Path:
        current = root
        for part in parts:
            current = current / part
            try:
                info = current.lstat()
            except FileNotFoundError:
                current.mkdir()
                continue
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise BackendError("path_symlink",
                                   "a material path component is not a real directory",
                                   path=str(current.relative_to(root)))
        return current

    @staticmethod
    def _write_material(path: Path, content: bytes) -> None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            info = None
        if info is not None and stat.S_ISLNK(info.st_mode):
            raise BackendError("path_symlink",
                               "a material file is a symlink", path=path.name)
        temporary = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        with open(temporary, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    def _materialize(self, state: dict[str, Any], spec: WorkerSpec) -> None:
        refs = list((spec.context or {}).get("input_refs") or [])
        total = sum(int(ref.get("size", 0)) for ref in refs
                    if isinstance(ref, Mapping))
        if self.max_context_bytes is not None and total > self.max_context_bytes:
            raise BackendError("budget_exhausted",
                               "the frozen context exceeds the backend limit",
                               size=total, limit=self.max_context_bytes)
        workspace = Path(state["workspace"])
        target_dir = self._material_dir(workspace, "inputs")
        materials: list[dict[str, Any]] = []
        for ref in refs:
            content, artifact_id, expected, size = self._read_material(ref)
            path = target_dir / expected
            if not path.exists() or _sha256_bytes(path.read_bytes()) != artifact_id:
                self._write_material(path, content)
            materials.append({
                "id": artifact_id, "sha256": expected, "size": len(content),
                "media_type": ref.get("media_type"),
                "path": path.relative_to(workspace).as_posix(),
            })
        state["materials"] = materials

    @staticmethod
    def _message_refs(message: Mapping[str, Any]) -> list[tuple[str, Any]]:
        refs: list[tuple[str, Any]] = []
        for field, role in (("input_refs", "input"),
                            ("output_refs", "candidate output"),
                            ("summary_ref", "candidate summary"),
                            ("feedback_ref", "review feedback"),
                            ("material_refs", "progress history"
                             if message.get("progress_comparison") is not None
                             else "decision material")):
            value = message.get(field)
            if value is None:
                continue
            if field in ("input_refs", "output_refs", "material_refs"):
                if not isinstance(value, list):
                    raise BackendError("material_missing",
                                       f"message {field} must be a list", field=field)
                refs.extend((role, item) for item in value)
            else:
                refs.append((role, value))
        prior = message.get("prior_feedback")
        if isinstance(prior, list):
            # A revised local contract starts a fresh epoch: the past verdicts
            # travel as real materials, not as a summary the model must trust.
            for entry in prior:
                if isinstance(entry, Mapping) and entry.get("feedback_ref") is not None:
                    refs.append(("review feedback", entry["feedback_ref"]))
        return refs

    def _materialize_message(self, state: Mapping[str, Any], delivery_key: str,
                             message: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Copy only the artifacts this delivery was granted into the workspace.

        The initial context stays immutable; message materials are per-delivery
        and fail before any process starts when missing, tampered or over the
        configured material budget.
        """
        initial = {str(item["id"]) for item in state.get("materials") or []}
        unique: list[tuple[str, dict[str, Any], str, str, int]] = []
        seen = set(initial)
        for role, ref in self._message_refs(message):
            artifact_id, expected, size = self._frozen_ref(ref)
            if artifact_id in seen:
                continue
            seen.add(artifact_id)
            unique.append((role, dict(ref), artifact_id, expected, size))
        total = sum(item[4] for item in unique)
        if self.max_context_bytes is not None and total > self.max_context_bytes:
            raise BackendError("budget_exhausted",
                               "the delivered materials exceed the backend limit",
                               kind="delivery_materials", size=total,
                               limit=self.max_context_bytes)
        if not unique:
            return []
        workspace = Path(state["workspace"])
        folder = self._material_dir(
            workspace, "messages",
            hashlib.sha256(delivery_key.encode("utf-8")).hexdigest()[:16])
        materials: list[dict[str, Any]] = []
        for role, ref, artifact_id, expected, size in unique:
            content, checked_id, checked, checked_size = self._read_material(ref)
            path = folder / checked
            if not path.exists() or _sha256_bytes(path.read_bytes()) != checked_id:
                self._write_material(path, content)
            materials.append({
                "id": checked_id, "sha256": checked, "size": checked_size,
                "media_type": ref.get("media_type"), "role": role,
                "path": path.relative_to(workspace).as_posix(),
            })
        return materials

    @staticmethod
    def _verify_materials(state: Mapping[str, Any]) -> None:
        workspace = Path(state["workspace"])
        tracked = list(state.get("materials") or [])
        for delivery in (state.get("deliveries") or {}).values():
            tracked.extend(delivery.get("materials") or [])
        seen: set[str] = set()
        for item in tracked:
            key = f"{item['path']}:{item['sha256']}"
            if key in seen:
                continue
            seen.add(key)
            path = workspace / str(item["path"])
            if not path.is_file() or path.is_symlink() or \
                    _sha256_bytes(path.read_bytes()) != f"sha256:{item['sha256']}":
                raise BackendError("material_missing",
                                   "a granted material was removed or modified",
                                   artifact_id=item["id"], path=item["path"])

    @staticmethod
    def _prompt(state: Mapping[str, Any], message: Mapping[str, Any],
                tools: tuple[str, ...], *, system_tools: tuple[str, ...] = (),
                review: Mapping[str, Any] | None = None,
                decision: Mapping[str, Any] | None = None,
                delivered: list[Mapping[str, Any]] | None = None) -> str:
        context = state.get("context") or {}
        role = str(message.get("role") or context.get("role") or "worker")
        lines = [
            f"WORKER: {state['worker_id']}",
            f"NODE: {state['node_id']}",
            f"EPOCH: {state['epoch']}",
            f"AGENT: {state.get('agent_ref')}",
            f"ROLE: {role}",
        ]
        for label, key in (("CONTRACT", "contract_ref"), ("TEMPLATE", "template_ref"),
                           ("CONTEXT", "brief"), ("SOP", "sop_brief"),
                           ("INSTRUCTIONS", "instructions")):
            value = context.get(key)
            if isinstance(value, str) and value.strip():
                lines.append(f"{label}: {value.strip()}")
        progress_policy = context.get("progress")
        if isinstance(progress_policy, Mapping) and progress_policy:
            lines.append(
                f"PROGRESS POLICY: {canonical(dict(progress_policy))}. Each "
                "candidate of this Node is checkpointed and judged for "
                "improvement by the bound evaluator.")
        business = {name: value for name, value in sorted(context.items())
                    if name not in _PROMPT_CONTEXT_KEYS}
        if business:
            lines.append("BUSINESS CONTEXT:")
            for name, value in business.items():
                lines.append(f"  {name}: {canonical(value)}")
        materials = list(state.get("materials") or [])
        if materials:
            lines.append("FROZEN INPUTS (read-only, already in the workspace):")
            for item in materials:
                media = item.get("media_type") or "application/octet-stream"
                lines.append(f"  - {item['path']} ({media}, {item['size']} bytes)")
            if review is not None:
                lines.append("Read these frozen candidate materials before deciding.")
        review_inputs = context.get("review_inputs")
        candidate_ids = context.get("review_candidate_ids")
        if review is not None and isinstance(review_inputs, Mapping) and review_inputs:
            lines.append("REVIEW INPUTS (frozen facts declared for this review):")
            for name, artifact_id in sorted(review_inputs.items()):
                sha = str(artifact_id).removeprefix("sha256:")
                lines.append(f"  - {name}: inputs/{sha}")
        if review is not None and isinstance(candidate_ids, list) and candidate_ids:
            lines.append("CANDIDATE OUTPUTS (the artifacts under review):")
            for artifact_id in candidate_ids:
                sha = str(artifact_id).removeprefix("sha256:")
                lines.append(f"  - inputs/{sha}")
        delivered = list(delivered or [])
        accepted_children = (context.get("review_children")
                             if review is not None else None) \
            or (message.get("review_children") if review is not None else None)
        if isinstance(accepted_children, list) and accepted_children:
            paths = {str(item["id"]): item["path"]
                     for item in list(state.get("materials") or []) + delivered}
            lines.append("ACCEPTED CHILDREN (committed results of the reviewed Node):")
            for child in accepted_children:
                if not isinstance(child, Mapping):
                    continue
                lines.append(f"  - node {child.get('node_id')} "
                             f"candidate {child.get('candidate_id')}")
                for artifact_id in child.get("output_ids") or []:
                    path = paths.get(str(artifact_id)) or \
                        f"inputs/{str(artifact_id).removeprefix('sha256:')}"
                    lines.append(f"    - {path}")
        if delivered:
            lines.append("DELIVERED MATERIALS (read-only, this delivery):")
            for item in delivered:
                media = item.get("media_type") or "application/octet-stream"
                lines.append(f"  - {item['path']} ({item.get('role', 'material')}, "
                             f"{media}, {item['size']} bytes)")
            if review is not None and any(
                    item.get("role") == "candidate output" for item in delivered):
                lines.append("Read every candidate output and summary before deciding.")
            elif any(item.get("role") == "review feedback" for item in delivered):
                lines.append("Read the review feedback before continuing.")
        children = message.get("children")
        if isinstance(children, list) and children:
            lines.append("CHILDREN (all accepted):")
            for child in children:
                if isinstance(child, Mapping):
                    lines.append(
                        f"  - {child.get('node_id')}: "
                        f"candidate {child.get('accepted_candidate_ref')}")
        accepted = message.get("accepted_children")
        if isinstance(accepted, list) and accepted:
            lines.append("ACCEPTED CHILDREN (committed; do not redo):")
            for child in accepted:
                if isinstance(child, Mapping):
                    lines.append(
                        f"  - {child.get('node_id')}: "
                        f"candidate {child.get('candidate_id')}")
        open_subtasks = message.get("open_subtasks")
        if isinstance(open_subtasks, list) and open_subtasks:
            lines.append("OPEN SUBTASKS (still owned by this Node):")
            for child in open_subtasks:
                if isinstance(child, Mapping):
                    lines.append(
                        f"  - {child.get('node_id')}: {child.get('state')}")
        reply = message.get("kind") == "decision_resolved"
        if decision is not None:
            lines.append("DECISION REQUEST (from your child):")
            lines.append(f"  decision: {decision['decision_id']}")
            lines.append(f"  child node: {decision['child_node_id']}")
            lines.append(f"  question: {message.get('question')}")
            lines.append(f"  reason: {message.get('reason')}")
            options = message.get("options")
            if isinstance(options, list) and options:
                lines.append(f"  options: {', '.join(map(str, options))}")
        if reply:
            lines.append("DECISION REPLY:")
            lines.append(f"  decision: {message.get('decision_id')}")
            lines.append(f"  answer: {message.get('answer')}")
            if message.get("option") is not None:
                lines.append(f"  option: {message['option']}")
            lines.append("Continue the original task with this answer.")
        if review is not None:
            lines.append(
                f"REVIEW: target node {review['target_node_id']}, "
                f"candidate {review['candidate_id']}, "
                f"round {review['candidate_round']}")
            review_policy = message.get("progress_policy")
            if isinstance(review_policy, Mapping) and review_policy:
                lines.append(
                    "PROGRESS POLICY: "
                    f"{canonical(dict(review_policy))}. Compare this candidate "
                    "with the previous evaluated checkpoint of the same window "
                    "and report one explicit progress result.")
            comparison = message.get("progress_comparison")
            if isinstance(comparison, Mapping):
                previous = comparison.get("previous")
                if isinstance(previous, Mapping):
                    paths = {
                        str(item["id"]): item["path"]
                        for item in (list(state.get("materials") or [])
                                     + list(delivered or []))}
                    lines.append(
                        "PROGRESS COMPARISON: current candidate "
                        f"{review['candidate_id']} (round "
                        f"{review['candidate_round']}) versus previous "
                        f"checkpoint {previous.get('candidate_id')} "
                        f"(round {previous.get('round')}, prior evaluation: "
                        f"{previous.get('result')}). The earlier checkpoint is a "
                        "committed evaluated candidate; it need not be accepted.")
                    for ref in previous.get("output_refs") or []:
                        path = paths.get(str(ref.get("id")))
                        if path is not None:
                            lines.append(f"  previous output: {path}")
                    summary_ref = previous.get("summary_ref")
                    if isinstance(summary_ref, Mapping):
                        path = paths.get(str(summary_ref.get("id")))
                        if path is not None:
                            lines.append(f"  previous summary: {path}")
                    evidence_ref = previous.get("evidence_ref")
                    if isinstance(evidence_ref, Mapping):
                        path = paths.get(str(evidence_ref.get("id")))
                        if path is not None:
                            lines.append(f"  previous review feedback: {path}")
                    lines.append(
                        "Only report improved or not_improved when the current "
                        "and previous materials are genuinely comparable; "
                        "otherwise report unknown.")
                else:
                    lines.append(
                        "PROGRESS COMPARISON: no previous evaluated checkpoint "
                        "exists in this window. Report unknown unless the frozen "
                        "policy declares a valid initial baseline.")
        elif role == "reviewer":
            lines.append(
                f"REVIEW: target node {context.get('target_node_id')}, "
                f"candidate {context.get('candidate_id')}, "
                f"round {context.get('candidate_round')}")
        lines += [
            "",
            "TASK:",
            str(message["brief"]).strip(),
        ]
        goal = message.get("goal")
        if isinstance(goal, str) and goal.strip():
            lines += ["", "GOAL:", goal.strip()]
        acceptance = message.get("acceptance")
        if isinstance(acceptance, str) and acceptance.strip():
            lines += ["", "ACCEPTANCE:", acceptance.strip()]
        if message.get("output_schema") is not None:
            lines += ["", "OUTPUT SCHEMA:", canonical(message["output_schema"])]
        upper_acceptance = message.get("upper_acceptance")
        if isinstance(upper_acceptance, str) and upper_acceptance.strip():
            lines += ["", "UPPER ACCEPTANCE (frozen; the local contract cannot"
                      " relax it):", upper_acceptance.strip()]
        if message.get("upper_output_schema") is not None:
            lines += ["", "UPPER OUTPUT SCHEMA (frozen):",
                      canonical(message["upper_output_schema"])]
        if isinstance(message.get("previous_contract_ref"), str):
            lines += ["", f"PREVIOUS CONTRACT: {message['previous_contract_ref']}"]
        previous_round = message.get("round")
        if isinstance(previous_round, int):
            lines += ["", f"ROUND: {previous_round}"]
        target = message.get("target")
        if review is not None and isinstance(target, Mapping):
            lines += ["", "TARGET TASK (the node under review):",
                      f"  node: {target.get('node_id')}",
                      f"  template: {target.get('template_ref')}",
                      f"  contract: {target.get('contract_ref')}"]
            target_goal = target.get("goal")
            if isinstance(target_goal, str) and target_goal.strip():
                lines.append(f"  goal: {target_goal.strip()}")
            target_acceptance = target.get("acceptance")
            if isinstance(target_acceptance, str) and target_acceptance.strip():
                lines.append(f"  acceptance: {target_acceptance.strip()}")
            if target.get("output_schema") is not None:
                lines.append(f"  output schema: {canonical(target['output_schema'])}")
            target_context = target.get("context")
            if isinstance(target_context, Mapping) and target_context:
                lines.append(f"  business context: {canonical(dict(target_context))}")
        lines += [
            "",
            (f"TOOLS: {', '.join(tuple(tools) + tuple(system_tools))}. "
             "Paths are relative to the workspace root, which is the current "
             "directory: write result.json, not workspace/result.json."
             if tools or system_tools else
             "TOOLS: none. Answer from the frozen inputs and reply."),
        ]
        if system_tools:
            lines.append(
                "SYSTEM TOOLS are recorded by the control plane; your identity, "
                "the review binding and the decision binding are fixed by the "
                "adapter, never by arguments.")
        if review is not None:
            action = (
                f"ACTION: call node.submit_review exactly once with "
                f"candidate_id={review['candidate_id']!r}, "
                f"candidate_round={review['candidate_round']}, your verdict, and "
                'feedback_ref={"text": "..."}')
            if isinstance(message.get("progress_policy"), Mapping):
                action += (
                    ', and progress={"result": "improved" | "not_improved" | '
                    '"unknown"} (required by the frozen progress policy; compare '
                    "against the delivered previous checkpoint and use unknown "
                    "when it is absent, unusable or not comparable)")
            lines.append(action + ".")
        elif decision is not None:
            lines.append(
                f"ACTION: call node.resolve_decision exactly once with "
                f"decision_id={decision['decision_id']!r} and answer=\"...\"; "
                "include option only when the request offered options. Then "
                "continue your own task.")
        elif "node.submit_candidate" in system_tools:
            candidate_round = message.get("round")
            round_hint = (str(candidate_round)
                          if isinstance(candidate_round, int)
                          and not isinstance(candidate_round, bool)
                          else "the candidate round declared in this delivery")
            submission_id = "candidate-" + digest({
                "node_id": state.get("node_id"),
                "worker_id": state.get("worker_id"),
                "epoch": state.get("epoch"),
                "contract_ref": context.get("contract_ref"),
                "round": candidate_round,
            }).removeprefix("sha256:")[:32]
            lines.append(
                "ACTION: write the Task's output file, then call "
                "node.publish_artifact with its relative path and media_type. "
                "Copy the WHOLE returned ArtifactRef, including its sha256: "
                "id, sha256, size and media_type. A write_file digest or file "
                "path is NOT a published artifact. Never guess references. "
                "Then call node.submit_candidate once using exactly "
                f"candidate_id={submission_id!r}, round={round_hint}, "
                "and output_refs=[the returned ArtifactRef]. "
                "This is the technical ID for this delivery's attempt; do not "
                "reuse an earlier candidate_id from session history. "
                "If publication/submission fails, fix that error; do not repeat "
                "the same invalid reference. A text answer is not a candidate.")
            if "node.request_decision" in system_tools:
                lines.append(
                    "If a necessary choice belongs to your parent, use the "
                    "granted node.request_decision tool and yield for its reply.")
        elif "node.request_decision" in system_tools:
            lines.append(
                "ACTION: finish the task when you can. If a choice truly "
                "belongs to your parent, call node.request_decision once and "
                "stop; the answer arrives as the next delivery.")
        result_path = message.get("result_path")
        if isinstance(result_path, str) and result_path:
            fields = message.get("required_fields") or []
            lines.append(
                f"RESULT: when finished, save a JSON object to {result_path!r} in the "
                f"workspace containing these fields: {', '.join(map(str, fields))}. "
                "Use one of the available tools above to write it; do not use a tool "
                "that is not listed. After the result exists, stop calling tools and "
                "reply with one short sentence. Do not write any other file."
            )
        elif review is None and decision is None and "node.submit_candidate" in system_tools:
            lines.append(
                "RESULT: after a successful candidate submission, acknowledge "
                "briefly and end this turn. The Node still awaits its separate "
                "parent/Checker acceptance; do not claim task completion.")
        else:
            lines.append("RESULT: when finished, reply with your final answer and stop.")
        if "node.request_decision" in system_tools or reply:
            lines.append("Do not ask questions in plain text.")
        else:
            lines.append("Do not ask questions. Do not wait for further input.")
        return "\n".join(lines)

