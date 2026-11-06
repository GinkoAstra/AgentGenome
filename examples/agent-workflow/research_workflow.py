"""Evidence collection Goal and deterministic lesson tasks; explicit Host config.

AI selects excerpts. Program verifies frozen source correspondence and renders
accepted data. No open-ended semantic truth or web research is claimed.

This file is loaded only by an explicit trusted Host configuration:
``--script-agent examples/agent-workflow/research_workflow.py:build_agent``.
The pinned Task refs below are content addresses of the frozen Graph in
``graph-goal.yaml``; changing that Graph requires reviewing this program too.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
from typing import Any

# quotes-work and render-work Task refs in examples/agent-workflow/graph-goal.yaml.
SUPPORTED_QUOTES_TASK = (
    "sha256:c4c83ab2e99ff88cbdb4902ebdc0ff6ee5def63b8a35ed98736d10ac12db1260")
SUPPORTED_RENDER_TASK = (
    "sha256:b753fdafead470f58fe04dc3ff8c91d874a2d3aa30db7dac89f0a7f4f8308399")

_QUOTE_MIN_CHARS = 20
_H1 = re.compile(r"^#\s+(.*)$")
_H2 = re.compile(r"^##\s+(.*)$")
_SOURCE = re.compile(r"^(S\d+)\.\s*(.*)$")


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def parse_material(material: str) -> dict[str, Any]:
    """Return title and ordered sections [{source_id,title,body}] from H1/## S*.

    Only level-2 headings of the form ``S<n>. <title>`` are source sections.
    Other level-2 headings (for example ``Limits``) end the current section and
    are not citable. Duplicate S ids and a missing H1 are rejected.
    """
    if not isinstance(material, str):
        raise ValueError("the frozen material is not text")
    title: str | None = None
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    seen: set[str] = set()
    for line in material.splitlines():
        heading = _H2.match(line)
        if heading is not None:
            source = _SOURCE.match(heading.group(1).strip())
            if source is None:
                current = None
                continue
            source_id = source.group(1)
            if source_id in seen:
                raise ValueError(f"duplicate source id {source_id}")
            seen.add(source_id)
            current = {"source_id": source_id,
                       "title": source.group(2).strip(), "body": []}
            sections.append(current)
            continue
        top = _H1.match(line)
        if top is not None:
            text = top.group(1).strip()
            if title is None:
                title = text
            current = None
            continue
        if current is not None:
            current["body"].append(line)
    if title is None:
        raise ValueError("the frozen material has no H1 title")
    if not sections:
        raise ValueError("the frozen material has no S-numbered source section")
    return {
        "title": title,
        "sections": [{"source_id": item["source_id"],
                      "title": item["title"],
                      "body": "\n".join(item["body"]).strip()}
                     for item in sections],
    }


def _load_candidate(candidate: Any) -> tuple[Any, str | None]:
    if isinstance(candidate, dict):
        return candidate, None
    if isinstance(candidate, (bytes, bytearray)):
        try:
            text = bytes(candidate).decode("utf-8")
        except UnicodeDecodeError:
            return None, "the candidate is not valid UTF-8"
    elif isinstance(candidate, str):
        text = candidate
    else:
        return None, "the candidate is neither JSON text nor a JSON object"
    try:
        return json.loads(text), None
    except ValueError as error:
        return None, f"the candidate is not valid JSON: {error}"


def _empty_evaluation(issues: list[str], max_score: int,
                      source_ids: list[str]) -> dict[str, Any]:
    return {
        "complete": False,
        "comparable": False,
        "score": 0,
        "max_score": max_score,
        "issues": issues,
        "coverage": {source_id: False for source_id in source_ids},
        "title": None,
        "title_ok": False,
        "structure_ok": False,
        "missing_sources": list(source_ids),
        "duplicate_sources": [],
        "unknown_sources": [],
    }


def evaluate_quotes(material: str, candidate: Any) -> dict[str, Any]:
    """Return complete, comparable, score, issues, coverage and source IDs.

    Validate title, exact one quote/section, normalized contiguous source text
    (>=20 chars), no invented or duplicate sources. Partial valid JSON is a
    comparable draft; unreadable/unevaluable data is unknown, not false zero.
    """
    try:
        parsed = parse_material(material)
    except ValueError as error:
        return _empty_evaluation(
            [f"the frozen material cannot be parsed: {error}"], 0, [])
    sections = parsed["sections"]
    source_ids = [section["source_id"] for section in sections]
    by_source = {section["source_id"]: section for section in sections}
    max_score = len(sections) + 2

    body, error = _load_candidate(candidate)
    if error is not None:
        return _empty_evaluation([error], max_score, source_ids)

    issues: list[str] = []
    title_value = body.get("title") if isinstance(body, dict) else None
    quotes = body.get("quotes") if isinstance(body, dict) else None
    if not isinstance(body, dict):
        issues.append("the candidate JSON is not an object")
    if not isinstance(title_value, str):
        issues.append("the candidate title is not a string")
    if not isinstance(quotes, list):
        issues.append("the candidate quotes field is not an array")
    structure_ok = (isinstance(body, dict)
                    and isinstance(title_value, str)
                    and isinstance(quotes, list)
                    and all(isinstance(entry, dict)
                            and isinstance(entry.get("source_id"), str)
                            and isinstance(entry.get("quote"), str)
                            for entry in quotes))
    if not structure_ok and isinstance(quotes, list):
        issues.append("at least one quote entry is not an object with string "
                      "source_id and quote")

    title_ok = (isinstance(title_value, str)
                and title_value.strip() == parsed["title"].strip())
    if not title_ok:
        issues.append("the candidate title does not equal the material H1")

    counts = {source_id: 0 for source_id in source_ids}
    valid = {source_id: False for source_id in source_ids}
    unknown_sources: list[str] = []
    for index, entry in enumerate(quotes if isinstance(quotes, list) else []):
        if (not isinstance(entry, dict)
                or not isinstance(entry.get("source_id"), str)
                or not isinstance(entry.get("quote"), str)):
            continue
        source_id = entry["source_id"]
        if source_id not in counts:
            unknown_sources.append(source_id)
            continue
        counts[source_id] += 1
        normalized = _normalize(entry["quote"])
        section = by_source[source_id]
        valid[source_id] = (
            len(normalized) >= _QUOTE_MIN_CHARS
            and normalized in _normalize(section["body"]))

    missing = [source_id for source_id in source_ids if counts[source_id] == 0]
    duplicate = [source_id for source_id in source_ids if counts[source_id] > 1]
    unknown_unique = sorted(set(unknown_sources))
    for source_id in missing:
        issues.append(f"the source {source_id} has no quote")
    for source_id in duplicate:
        issues.append(f"the source {source_id} has more than one quote")
    for source_id in unknown_unique:
        issues.append(f"the quote cites unknown source id {source_id}")
    for source_id in source_ids:
        if counts[source_id] == 1 and not valid[source_id]:
            issues.append(
                f"the quote for {source_id} is not a contiguous source "
                f"excerpt of at least {_QUOTE_MIN_CHARS} characters")

    covered = sum(1 for source_id in source_ids
                  if counts[source_id] == 1 and valid[source_id])
    score = covered + (1 if title_ok else 0) + (1 if structure_ok else 0)
    complete = (
        structure_ok
        and title_ok
        and bool(source_ids)
        and not missing
        and not duplicate
        and not unknown_unique
        and covered == len(source_ids)
    )
    return {
        "complete": complete,
        "comparable": structure_ok,
        "score": score,
        "max_score": max_score,
        "issues": issues,
        "coverage": {
            source_id: counts[source_id] == 1 and valid[source_id]
            for source_id in source_ids
        },
        "title": title_value if isinstance(title_value, str) else None,
        "title_ok": title_ok,
        "structure_ok": structure_ok,
        "missing_sources": missing,
        "duplicate_sources": duplicate,
        "unknown_sources": unknown_unique,
    }


def _escape(value: str) -> str:
    return html.escape(value, quote=True)


def render_lesson(material: str, quotes: dict[str, Any]) -> str:
    """Validate accepted quotes, then render escaped self-contained HTML."""
    parsed = parse_material(material)
    result = evaluate_quotes(material, quotes)
    if not result["complete"]:
        raise ValueError(
            "the accepted quotes are not complete: " + "; ".join(result["issues"]))
    selected = {
        entry["source_id"]: _normalize(entry["quote"])
        for entry in quotes["quotes"]
    }
    parts = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        f"<title>{_escape(parsed['title'])}</title>",
        "<style>",
        "body { font-family: system-ui, sans-serif; line-height: 1.5; "
        "margin: 2rem auto; max-width: 46rem; padding: 0 1rem; color: #1f2933; }",
        "h1, h2 { line-height: 1.2; }",
        "blockquote { border-left: 4px solid #8aa4b8; margin: 1rem 0; "
        "padding: 0.25rem 1rem; background: #f3f6f8; }",
        "cite { display: block; margin-top: 0.5rem; color: #52606d; "
        "font-style: normal; }",
        ".note, .final { color: #52606d; }",
        ".final { margin-top: 2rem; padding-top: 1rem; "
        "border-top: 1px solid #d9e2ec; }",
        "</style>",
        "</head>",
        "<body>",
        f"<h1>{_escape(parsed['title'])}</h1>",
        '<p class="note">This lesson was generated from static pinned local '
        "notes. No web search or online citation is used.</p>",
    ]
    for section in parsed["sections"]:
        source_id = section["source_id"]
        parts.extend([
            f'<section id="{source_id.lower()}">',
            f"<h2>{_escape(source_id)}. {_escape(section['title'])}</h2>",
            "<blockquote>",
            f"<p>{_escape(selected[source_id])}</p>",
            f"<cite>{_escape(source_id)}</cite>",
            "</blockquote>",
            "</section>",
        ])
    parts.extend([
        "<h2>Sources</h2>",
        "<ul>",
    ])
    for section in parsed["sections"]:
        parts.append(f"<li>{_escape(section['source_id'])}</li>")
    parts.extend([
        "</ul>",
        '<p class="final">Generated from pinned local notes with no web '
        "retrieval.</p>",
        "</body>",
        "</html>",
        "",
    ])
    return "\n".join(parts)


def _first_mismatch(expected: str, actual: str) -> dict[str, int]:
    limit = min(len(expected), len(actual))
    offset = next((index for index in range(limit)
                   if expected[index] != actual[index]), limit)
    return {"offset": offset, "expected_length": len(expected),
            "actual_length": len(actual)}


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _short(tag: str, value: str) -> str:
    import hashlib
    return f"{tag}-{hashlib.sha256(value.encode()).hexdigest()[:32]}"


def _record_tool(state: Any, name: str, call_id: str,
                 arguments: dict[str, Any], output: Any) -> None:
    """Write the real tool receipt into the current ScriptBackend delivery."""
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


def _artifact_refs(refs: Any) -> list[dict[str, Any]]:
    return [dict(ref) for ref in refs or [] if isinstance(ref, dict)]


def _split_inputs(refs: list[dict[str, Any]]) -> tuple[
        list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (material refs, JSON refs) by declared media type."""
    material = [ref for ref in refs
                if ref.get("media_type") in ("text/markdown", "text/plain")]
    structured = [ref for ref in refs
                  if ref.get("media_type") == "application/json"]
    return material, structured


def _read_text(api: Any, actor: Any, ref: dict[str, Any]) -> str:
    return api.read_artifact(actor, ref).decode("utf-8")


def _reviewer_actor(spec: Any):
    from node_runtime.control_plane.resources import Actor
    return Actor(spec.worker_id, {"reviewer"}, run_ids={spec.run_id},
                 worker_id=spec.worker_id, epoch=spec.epoch)


def _worker_box(api: Any, spec: Any, *, reviewer: bool = False):
    from node_runtime.control_plane.tools import WorkerToolbox
    context = spec.context if isinstance(spec.context, dict) else {}
    if reviewer:
        return WorkerToolbox(
            api, run_id=spec.run_id, node_id=spec.node_id,
            worker_id=spec.worker_id, epoch=spec.epoch,
            tool_profile=spec.tool_profile,
            reviewer_ref=(context.get("reviewer_ref")),
            review_target=context.get("target_node_id"),
            review_candidate=context.get("candidate_id"),
            review_round=context.get("candidate_round"),
            contract_ref=context.get("contract_ref"))
    return WorkerToolbox(
        api, run_id=spec.run_id, node_id=spec.node_id,
        worker_id=spec.worker_id, epoch=spec.epoch,
        tool_profile=spec.tool_profile,
        contract_ref=context.get("contract_ref"))


def _quote_progress(message: dict[str, Any], api: Any, actor: Any,
                    material: str, current: dict[str, Any]) -> str:
    """Same-window score comparison from Scheduler-authorized material only."""
    if message.get("progress_policy") is None:
        return ""
    try:
        comparison = message.get("progress_comparison")
        if not isinstance(comparison, dict):
            return "unknown"
        previous = comparison.get("previous")
        if not isinstance(previous, dict):
            return "unknown"
        if not current.get("comparable"):
            return "unknown"
        authorized = {
            ref.get("id") for ref in message.get("material_refs") or []
            if isinstance(ref, dict)}
        quote_refs = [
            ref for ref in previous.get("output_refs") or []
            if isinstance(ref, dict)
            and ref.get("media_type") == "application/json"
            and ref.get("id") in authorized]
        if len(quote_refs) != 1:
            return "unknown"
        previous_candidate = json.loads(
            _read_text(api, actor, quote_refs[0]))
        previous_result = evaluate_quotes(material, previous_candidate)
        if not previous_result.get("comparable"):
            return "unknown"
        return ("improved" if current["score"] > previous_result["score"]
                else "not_improved")
    except Exception:
        return "unknown"


def _quote_review(api: Any, spec: Any, message: dict[str, Any], state: Any,
                  box: Any, actor: Any, target_task: str) -> None:
    context = spec.context if isinstance(spec.context, dict) else {}
    candidate_id = box.review_candidate
    candidate_round = box.review_round
    context_refs = _artifact_refs(context.get("input_refs"))
    output_refs = _artifact_refs(message.get("output_refs"))
    granted = {ref.get("id") for ref in context_refs}
    if any(ref.get("id") not in granted for ref in output_refs):
        _submit(api, spec, message, state, box, actor, target_task,
                "revise", "the candidate is not part of the frozen review binding",
                {"candidate_ids": [ref.get("id") for ref in output_refs]},
                unknown_progress=True)
        return
    candidate_ids = {ref.get("id") for ref in output_refs}
    summary_ref = message.get("summary_ref")
    if isinstance(summary_ref, dict):
        candidate_ids.add(summary_ref.get("id"))
    target_refs = [ref for ref in context_refs
                   if ref.get("id") not in candidate_ids]
    material_refs, structured_refs = _split_inputs(target_refs)
    if (len(output_refs) != 1 or len(material_refs) != 1
            or len(structured_refs) != 0):
        _submit(api, spec, message, state, box, actor, target_task,
                "revise",
                "the review binding must contain exactly one frozen material "
                "and one candidate quotes artifact",
                {"material_count": len(material_refs),
                 "candidate_count": len(output_refs)},
                unknown_progress=True)
        return
    if output_refs[0].get("media_type") != "application/json":
        _submit(api, spec, message, state, box, actor, target_task,
                "revise",
                "the candidate must be exactly one application/json output",
                {"candidate_media_types":
                 [ref.get("media_type") for ref in output_refs]},
                unknown_progress=True)
        return

    material = ""
    result: dict[str, Any] = {}
    try:
        material = _read_text(api, actor, material_refs[0])
        candidate = json.loads(_read_text(api, actor, output_refs[0]))
        result = evaluate_quotes(material, candidate)
    except Exception as error:
        _submit(api, spec, message, state, box, actor, target_task,
                "revise",
                f"authorized review material is unavailable or unreadable: {error}",
                {"material_ref": material_refs[0],
                 "candidate_ref": output_refs[0]},
                unknown_progress=True)
        return

    progress = _quote_progress(message, api, actor, material, result)
    evidence = {
        "template_ref": target_task,
        "material_ref": material_refs[0],
        "candidate_ref": output_refs[0],
        "score": result["score"],
        "max_score": result["max_score"],
        "complete": result["complete"],
        "comparable": result["comparable"],
        "coverage": result["coverage"],
        "issues": result["issues"],
        "missing_sources": result["missing_sources"],
        "duplicate_sources": result["duplicate_sources"],
        "unknown_sources": result["unknown_sources"],
    }
    if result["complete"]:
        _submit(api, spec, message, state, box, actor, target_task,
                "accept",
                f"all {len(result['coverage'])} source sections are quoted "
                f"verbatim with score {result['score']}/{result['max_score']}",
                evidence, progress=progress)
    else:
        _submit(api, spec, message, state, box, actor, target_task,
                "revise",
                "the quotes do not satisfy the declared evidence Goal: "
                + "; ".join(result["issues"]),
                evidence, progress=progress)


def _render_review(api: Any, spec: Any, message: dict[str, Any], state: Any,
                   box: Any, actor: Any, target_task: str) -> None:
    context = spec.context if isinstance(spec.context, dict) else {}
    context_refs = _artifact_refs(context.get("input_refs"))
    output_refs = _artifact_refs(message.get("output_refs"))
    granted = {ref.get("id") for ref in context_refs}
    if any(ref.get("id") not in granted for ref in output_refs):
        _submit(api, spec, message, state, box, actor, target_task,
                "revise", "the candidate is not part of the frozen review binding",
                {"candidate_ids": [ref.get("id") for ref in output_refs]})
        return
    candidate_ids = {ref.get("id") for ref in output_refs}
    summary_ref = message.get("summary_ref")
    if isinstance(summary_ref, dict):
        candidate_ids.add(summary_ref.get("id"))
    target_refs = [ref for ref in context_refs
                   if ref.get("id") not in candidate_ids]
    material_refs, structured_refs = _split_inputs(target_refs)
    if (len(output_refs) != 1 or len(material_refs) != 1
            or len(structured_refs) != 1):
        _submit(api, spec, message, state, box, actor, target_task,
                "revise",
                "the review binding must contain one frozen material, one "
                "accepted quotes artifact and one candidate HTML",
                {"material_count": len(material_refs),
                 "quotes_count": len(structured_refs),
                 "candidate_count": len(output_refs)})
        return
    if output_refs[0].get("media_type") != "text/html":
        _submit(api, spec, message, state, box, actor, target_task,
                "revise",
                "the candidate must be exactly one text/html output",
                {"candidate_media_types":
                 [ref.get("media_type") for ref in output_refs]})
        return

    material = ""
    quotes: Any = None
    candidate = ""
    try:
        material = _read_text(api, actor, material_refs[0])
        quotes = json.loads(_read_text(api, actor, structured_refs[0]))
        candidate = _read_text(api, actor, output_refs[0])
    except Exception as error:
        _submit(api, spec, message, state, box, actor, target_task,
                "revise",
                f"authorized review material is unavailable or unreadable: {error}",
                {"material_ref": material_refs[0],
                 "quotes_ref": structured_refs[0],
                 "candidate_ref": output_refs[0]})
        return
    result = evaluate_quotes(material, quotes)
    expected = ""
    if result["complete"]:
        expected = render_lesson(material, quotes)
    actual_sha = output_refs[0].get("sha256")
    mismatch = (_first_mismatch(expected, candidate)
                if expected and candidate != expected else None)
    evidence = {
        "template_ref": target_task,
        "material_ref": material_refs[0],
        "quotes_ref": structured_refs[0],
        "candidate_ref": output_refs[0],
        "quotes_score": result["score"],
        "quotes_max_score": result["max_score"],
        "quotes_complete": result["complete"],
        "quotes_issues": result["issues"],
        "expected_sha256": ("sha256:" + hashlib.sha256(
            expected.encode("utf-8")).hexdigest()) if expected else None,
        "actual_sha256": actual_sha,
        "mismatch": mismatch,
    }
    if result["complete"] and candidate == expected:
        _submit(api, spec, message, state, box, actor, target_task,
                "accept",
                "the candidate is byte-equal to the deterministic rendering "
                "of the accepted quotes",
                evidence)
    else:
        issue = ("the accepted quotes are not complete: "
                 + "; ".join(result["issues"])) if not result["complete"] else \
                "the candidate is not byte-equal to the deterministic rendering"
        _submit(api, spec, message, state, box, actor, target_task,
                "revise", issue, evidence)


def _submit(api: Any, spec: Any, message: dict[str, Any], state: Any,
            box: Any, actor: Any, target_task: str, verdict: str, summary: str,
            evidence: dict[str, Any], progress: str = "",
            unknown_progress: bool = False) -> None:
    candidate_id = box.review_candidate
    candidate_round = box.review_round
    if unknown_progress and message.get("progress_policy") is not None:
        progress = "unknown"
    feedback = dict(evidence)
    feedback.update({
        "candidate_id": candidate_id,
        "candidate_round": candidate_round,
        "target_node_id": box.review_target,
        "verdict": verdict,
        "summary": summary,
    })
    if progress:
        feedback["progress"] = {"result": progress}
    key = f"{spec.worker_id}:{candidate_id}:{candidate_round}"
    feedback_ref = box.put_feedback(
        json.dumps(feedback, ensure_ascii=False, indent=2, sort_keys=True),
        media_type="text/plain", call_id=_short("evidence-feedback", key))
    arguments = {
        "candidate_id": candidate_id,
        "candidate_round": candidate_round,
        "verdict": verdict,
        "feedback_ref": feedback_ref,
    }
    if progress:
        arguments["progress"] = {"result": progress}
    call_id = _short("evidence-review", key)
    output = box.invoke("node.submit_review", arguments, call_id=call_id)
    _record_tool(state, "node.submit_review", call_id, arguments,
                 output.get("output"))


def _run_render_worker(api: Any, spec: Any, message: dict[str, Any],
                       state: Any, box: Any) -> None:
    context = spec.context if isinstance(spec.context, dict) else {}
    refs = _artifact_refs(context.get("input_refs"))
    material_refs, structured_refs = _split_inputs(refs)
    if len(material_refs) != 1 or len(structured_refs) != 1:
        raise RuntimeError(
            "render-work needs exactly one frozen material and one accepted "
            "quotes artifact")
    material = _read_text(api, box.actor, material_refs[0])
    quotes = json.loads(_read_text(api, box.actor, structured_refs[0]))
    page = render_lesson(material, quotes)
    round_number = int(message.get("round") or 1)
    candidate_id = f"render-{spec.node_id}-e{spec.epoch}-r{round_number}"
    publish_call = _short("render-publish", f"{spec.worker_id}:{round_number}")
    published = box.publish_artifact(
        {"path": "lesson.html", "media_type": "text/html"},
        call_id=publish_call, content=page.encode("utf-8"))
    _record_tool(state, "node.publish_artifact", publish_call,
                 {"path": "lesson.html", "media_type": "text/html"},
                 published.get("output"))
    arguments = {
        "candidate_id": candidate_id,
        "round": round_number,
        "output_refs": [published["output"]],
    }
    submit_call = _short("render-submit", f"{spec.worker_id}:{round_number}")
    submitted = box.invoke("node.submit_candidate", arguments,
                           call_id=submit_call)
    _record_tool(state, "node.submit_candidate", submit_call, arguments,
                 submitted.get("output"))


def build_agent(api: Any):
    """Trusted ScriptBackend callback for work/review roles and pinned Tasks.

    Resolve actual Nodes via API, read authorized frozen input/candidate/old
    checkpoint artifacts, publish candidate or reviewer feedback through the
    existing WorkerToolbox, and record actual tools/provider0. No new runtime.
    """
    def run(spec: Any, message: dict[str, Any], state: Any) -> None:
        context = spec.context if isinstance(spec.context, dict) else {}
        role = context.get("role") or message.get("role")
        if role == "worker":
            box = _worker_box(api, spec)
            target = api.get_resource(box.actor, "node", spec.node_id)
            if not isinstance(target, dict):
                raise RuntimeError(
                    "render-work cannot read its actual Node binding")
            template = target.get("template_ref")
            contract = target.get("contract_ref")
            if (template != SUPPORTED_RENDER_TASK
                    or contract != template
                    or context.get("template_ref") != template
                    or context.get("contract_ref") != contract):
                raise RuntimeError(
                    "this program only executes the frozen render-work Task; "
                    f"actual={template} contract={contract} "
                    f"context={context.get('template_ref')}/"
                    f"{context.get('contract_ref')}")
            _run_render_worker(api, spec, message, state, box)
            return
        if role != "reviewer":
            raise RuntimeError(f"unsupported delivery role {role!r}")
        target_node = context.get("target_node_id") or message.get("target_node_id")
        candidate_id = context.get("candidate_id") or message.get("candidate_id")
        candidate_round = (context.get("candidate_round")
                           or message.get("candidate_round"))
        if (not isinstance(target_node, str) or not isinstance(candidate_id, str)
                or not isinstance(candidate_round, int)):
            raise RuntimeError("the review delivery has no bound candidate")
        actor = _reviewer_actor(spec)
        box = _worker_box(api, spec, reviewer=True)
        target = api.get_resource(actor, "node", target_node)
        task_ref = target["template_ref"]
        contract_ref = target["contract_ref"]
        if contract_ref != task_ref or task_ref not in (
                SUPPORTED_QUOTES_TASK, SUPPORTED_RENDER_TASK):
            _submit(api, spec, message, state, box, actor, task_ref, "revise",
                    "unsupported or locally revised frozen work Task; update "
                    "and review this program for the new SOP",
                    {"supported_tasks": [SUPPORTED_QUOTES_TASK,
                                         SUPPORTED_RENDER_TASK],
                     "actual_template_ref": task_ref,
                     "actual_contract_ref": contract_ref},
                    unknown_progress=True)
            return
        if task_ref == SUPPORTED_QUOTES_TASK:
            _quote_review(api, spec, message, state, box, actor, task_ref)
        else:
            _render_review(api, spec, message, state, box, actor, task_ref)

    return run
