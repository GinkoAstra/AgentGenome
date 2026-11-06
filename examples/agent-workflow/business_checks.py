"""Example-level checks, loaded only by explicit trusted Host configuration.

Codex defines the public checking boundary. Qoder fills the reference rules.
The checker reads accepted artifacts; it does not manufacture Worker outputs.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any



def _valid_email(email: str) -> bool:
    if email.count('@') != 1:
        return False
    local, domain = email.split('@')
    if not local or '.' not in domain:
        return False
    return all(domain.split('.'))


def _normalize(row: dict[str, Any]) -> dict[str, str] | None:
    name = row.get('name')
    email = row.get('email')
    if not isinstance(name, str) or not isinstance(email, str):
        return None
    name = name.strip()
    email = email.strip().lower()
    if not name or not _valid_email(email):
        return None
    return {'name': name, 'email': email}


def expected_clean(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Return {rows, audit}: trim names/emails, lowercase emails; keep valid
    nonempty names and emails containing exactly one @ and a dotted domain;
    keep first normalized email; preserve order. Audit records input_rows,
    output_rows, invalid_rows and duplicate_rows (only valid duplicates).
    """
    kept: list[dict[str, str]] = []
    seen: set[str] = set()
    invalid = 0
    duplicate = 0
    for row in rows:
        normalized = _normalize(row)
        if normalized is None:
            invalid += 1
            continue
        if normalized['email'] in seen:
            duplicate += 1
            continue
        seen.add(normalized['email'])
        kept.append(normalized)
    return {
        'rows': kept,
        'audit': {
            'input_rows': len(rows),
            'output_rows': len(kept),
            'invalid_rows': invalid,
            'duplicate_rows': duplicate,
        },
    }


def expected_profile(clean: dict[str, Any]) -> dict[str, Any]:
    """Return {rows: count, domains: {normalized_domain: count}}."""
    counts: dict[str, int] = {}
    rows = clean['rows']
    for row in rows:
        domain = row['email'].split('@', 1)[1]
        counts[domain] = counts.get(domain, 0) + 1
    return {'rows': len(rows), 'domains': counts}


def verify_clean(source: list[dict[str, Any]], clean: dict[str, Any],
                 profile: dict[str, Any]) -> None:
    """Raise on any mismatch, independently of model and run success claims."""
    difference = _clean_difference(expected_clean(source), clean)
    assert difference is None, f"accepted cleaning output differs from SOP: {difference}"
    difference = _profile_difference(expected_profile(clean), profile)
    assert difference is None, f"accepted profile differs from cleaned rows: {difference}"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _short(tag: str, value: str) -> str:
    return f"{tag}-{hashlib.sha256(value.encode()).hexdigest()[:32]}"


def _is_review(message: dict[str, Any]) -> bool:
    return (message.get("role") == "reviewer"
            or message.get("kind") in ("review_start", "review_request"))


def _reviewer_actor(spec: Any):
    from node_runtime.control_plane.resources import Actor
    return Actor(spec.worker_id, {"reviewer"}, run_ids={spec.run_id},
                 worker_id=spec.worker_id, epoch=spec.epoch)


def _read_json_artifact(api: Any, actor: Any, ref: dict[str, Any]) -> Any:
    data = api.read_artifact(actor, ref)
    return json.loads(data.decode("utf-8"))


def _count_difference(label: str, got: Any, expected: int) -> str | None:
    # JSON true == 1 in Python; a boolean counter is not a count.
    if type(got) is not int or got != expected:
        return f"{label} differs (expected {expected}, got {got!r})"
    return None


def _clean_difference(expected: dict[str, Any],
                      candidate: Any) -> str | None:
    if not isinstance(candidate, dict):
        return "candidate is not a JSON object"
    rows = candidate.get("rows")
    if not isinstance(rows, list):
        return "rows is not a JSON array"
    if rows != expected["rows"]:
        for index, (want, got) in enumerate(zip(expected["rows"], rows)):
            if want != got:
                return f"row {index} differs (expected {want!r}, got {got!r})"
        return (f"rows length differs (expected {len(expected['rows'])}, "
                f"got {len(rows)})")
    audit = candidate.get("audit")
    if not isinstance(audit, dict):
        return "audit is not a JSON object"
    for name, value in expected["audit"].items():
        difference = _count_difference(f"audit.{name}", audit.get(name), value)
        if difference is not None:
            return difference
    return None


def _profile_difference(expected: dict[str, Any],
                        candidate: Any) -> str | None:
    if not isinstance(candidate, dict):
        return "candidate is not a JSON object"
    difference = _count_difference("rows", candidate.get("rows"),
                                   expected["rows"])
    if difference is not None:
        return difference
    domains = candidate.get("domains")
    if not isinstance(domains, dict):
        return "domains is not a JSON object"
    for name in sorted(set(expected["domains"]) | set(domains)):
        difference = _count_difference(f"domains[{name!r}]",
                                       domains.get(name),
                                       expected["domains"].get(name))
        if difference is not None:
            return difference
    return None


def _record_tool(state: Any, name: str, call_id: str,
                 arguments: dict[str, Any], output: Any) -> None:
    """Write the real tool receipt into the current ScriptBackend delivery.

    The Backend's own usage is not updated by Toolbox.invoke; recording the
    receipt here lets observe/Controller report the actual tool call while the
    call identity keeps retries idempotent.
    """
    if not isinstance(state, dict):
        return
    order = state.get("order") or []
    if not order:
        return
    delivery = state.get("deliveries", {}).get(order[-1])
    if not isinstance(delivery, dict):
        return
    tools = delivery.setdefault("tools", [])
    for entry in tools:
        if entry.get("name") == name and entry.get("call_id") == call_id:
            entry["result"] = output
            entry["finished_at"] = _now()
            return
    tools.append({
        "name": name, "effect": "idempotent", "call_id": call_id,
        "params": dict(arguments), "state": "end",
        "started_at": _now(), "finished_at": _now(), "result": output,
    })


def build_checker(api: Any):
    """Trusted ScriptBackend factory for the deterministic SOP Checkers.

    The callback receives the committed WorkerSpec and delivery message. It
    reads only authorized frozen inputs and the bound candidate through the
    public API, recomputes the SOP, and submits the same ``node.submit_review``
    receipt a Pi Checker would. It never publishes a replacement business
    artifact. Malformed/unavailable material is a revise, never an accept.
    """
    from node_runtime.control_plane.tools import WorkerToolbox

    # Reviewed immutable Task versions for this program's fixed SOP. These
    # references intentionally do not follow edits to a neighbouring YAML file.
    # Changing SOP semantics requires reviewing/updating the program too.
    supported_tasks = {
        "sha256:beeed8ce0476a8e9a0bed9788394b9db0e0b72623e15e61834b1156f028b846e": "clean",
        "sha256:bc9a776d223fb0dcb7b52a6de90b78bf13678046afe23af3fc64de9d7933dd95": "profile",
    }

    def run(spec: Any, message: dict[str, Any], state: Any) -> None:
        if not _is_review(message):
            raise RuntimeError(
                "the program Checker only handles review_receipt deliveries")
        context = spec.context if isinstance(spec.context, dict) else {}
        candidate_id = context.get("candidate_id") or message.get("candidate_id")
        candidate_round = (context.get("candidate_round")
                           or message.get("candidate_round"))
        target_node = (context.get("target_node_id")
                       or message.get("target_node_id"))
        reviewer_ref = (context.get("reviewer_ref")
                        or message.get("reviewer_ref"))
        if (not isinstance(candidate_id, str)
                or not isinstance(candidate_round, int)):
            raise RuntimeError("the review delivery has no bound candidate")
        box = WorkerToolbox(
            api, run_id=spec.run_id, node_id=spec.node_id,
            worker_id=spec.worker_id, epoch=spec.epoch,
            tool_profile=spec.tool_profile, reviewer_ref=reviewer_ref,
            review_target=target_node, review_candidate=candidate_id,
            review_round=candidate_round,
            contract_ref=context.get("contract_ref"))
        actor = _reviewer_actor(spec)
        call_key = f"{spec.worker_id}:{candidate_id}:{candidate_round}"

        def submit(verdict: str, feedback: str) -> None:
            feedback_ref = box.put_feedback(
                feedback, call_id=_short("checker-feedback", call_key))
            arguments = {
                "candidate_id": candidate_id,
                "candidate_round": candidate_round,
                "verdict": verdict,
                "feedback_ref": feedback_ref,
            }
            if message.get("progress_policy") is not None:
                # A program oracle cannot judge research quality; do not fake
                # improvement, and never let this path accept for that reason.
                arguments["progress"] = {"result": "unknown"}
            call_id = _short("checker-review", call_key)
            output = box.invoke("node.submit_review", arguments,
                                call_id=call_id)
            _record_tool(state, "node.submit_review", call_id,
                         arguments, output.get("output"))

        # The Api's assigned target is authoritative; delivery prose alone
        # cannot make an unsupported or locally revised contract acceptable.
        target_record = api.get_resource(actor, "node", target_node)
        task_ref = target_record["template_ref"]
        expected_stage = supported_tasks.get(task_ref)
        if (expected_stage is None
                or target_record["contract_ref"] != task_ref):
            submit("revise", "unsupported frozen Task or local acceptance version; "
                   "update and review the program Checker for the new SOP")
            return

        refs = [dict(ref) for ref in context.get("input_refs") or []]
        granted = {str(ref.get("id")) for ref in refs}
        output_refs = [dict(ref) for ref in message.get("output_refs") or []]
        if output_refs and any(str(ref.get("id")) not in granted
                               for ref in output_refs):
            submit("revise",
                   "the candidate is not part of the frozen review binding")
            return
        candidate_ids = {str(ref.get("id")) for ref in output_refs}
        summary_ref = message.get("summary_ref")
        if isinstance(summary_ref, dict):
            candidate_ids.add(str(summary_ref.get("id")))
        target_refs = [ref for ref in refs
                       if str(ref.get("id")) not in candidate_ids]
        if len(output_refs) != 1 or len(target_refs) != 1:
            submit("revise",
                   "the review binding must contain exactly one frozen input "
                   "and one candidate output")
            return
        try:
            target = _read_json_artifact(api, actor, target_refs[0])
            candidate = _read_json_artifact(api, actor, output_refs[0])
        except Exception as error:  # unreadable/unauthorized is not an accept
            submit("revise",
                   f"authorized review material is unavailable: {error}")
            return
        if expected_stage == "clean":
            if not isinstance(target, list):
                submit("revise",
                       "the frozen input shape does not match the cleaning SOP")
                return
            try:
                expected = expected_clean(target)
            except Exception as error:
                submit("revise",
                       f"the frozen input is not a valid cleaning source: {error}")
                return
            difference = _clean_difference(expected, candidate)
            stage = "cleaning"
        else:
            if not (isinstance(target, dict)
                    and isinstance(target.get("rows"), list)):
                submit("revise",
                       "the frozen input shape does not match the profile SOP")
                return
            try:
                expected = expected_profile(target)
            except Exception as error:
                submit("revise",
                       f"the frozen clean input is malformed: {error}")
                return
            difference = _profile_difference(expected, candidate)
            stage = "profile"
        if difference is None:
            submit("accept",
                   f"the recomputed {stage} result matches the frozen SOP")
        else:
            submit("revise", f"{stage} output differs: {difference}")

    return run


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('clean', type=Path)
    parser.add_argument('profile', type=Path)
    args = parser.parse_args()
    verify_clean(*(json.loads(p.read_text()) for p in
                   (args.source, args.clean, args.profile)))
    print('accepted business artifacts match the frozen SOP')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
