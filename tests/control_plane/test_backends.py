from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import pytest

from node_runtime.control_plane.backends import (
    BackendError,
    PiBackend,
    ScriptBackend,
    WorkerSpec,
)


def _process_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    try:
        line = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return False
    return line[line.rindex(")") + 2] not in "ZXx"


def spec(worker: str = "w1", node: str = "n1", epoch: int = 1,
         desired: str = "active", run_id: str = "r1",
         budget: dict | None = None) -> WorkerSpec:
    return WorkerSpec(worker_id=worker, node_id=node, epoch=epoch, intent_seq=1,
                      backend_ref="script", agent_ref="test-agent",
                      desired=desired, run_id=run_id, budget=budget or {})


@pytest.fixture
def script(tmp_path):
    return ScriptBackend(tmp_path / "data")


def test_script_delivery_runs_tools_and_deduplicates(script):
    backend = script
    handle = backend.start(spec(), ("w1", 1))
    message = {
        "brief": "write the result",
        "script": {
            "tool_calls": [{"name": "write_file", "effect": "idempotent",
                            "target": "result.json",
                            "params": {"path": "result.json"},
                            "content": json.dumps({"summary": "ok", "count": 2})}],
            "result_path": "result.json",
            "required_fields": ["summary", "count"],
        },
    }
    receipt = backend.deliver(handle, message, "d1")
    assert receipt.accepted and receipt.outcome == "delivered"
    observation = backend.observe(handle)
    assert observation.observed == "idle"
    assert observation.result["result"] == {"summary": "ok", "count": 2}
    assert observation.tool_calls == 1
    duplicate = backend.deliver(handle, message, "d1")
    assert duplicate.duplicate and duplicate.delivery_id == receipt.delivery_id
    assert backend.observe(handle).tool_calls == 1


def test_script_unknown_effect_blocks_resume(script):
    backend = script
    handle = backend.start(spec(), ("w1", 1))
    message = {"brief": "crash",
               "script": {"tool_calls": [{"name": "external", "effect": "unknown",
                                          "target": "out.txt", "params": {}}],
                          "crash_after_tool_start": True}}
    backend.deliver(handle, message, "d1")
    observation = backend.observe(handle)
    assert observation.observed == "unknown"
    assert observation.error["code"] == "tool_effect_unknown"
    with pytest.raises(BackendError) as caught:
        backend.deliver(handle, {"brief": "next"}, "d2")
    assert caught.value.code == "unknown_effect"
    result = backend.reconcile_tools(handle)
    assert result["unresolved"]
    assert backend.observe(handle).observed == "unknown"


def test_script_idempotent_effect_reconciles_and_resumes(script):
    backend = script
    handle = backend.start(spec(), ("w1", 1))
    message = {"brief": "crash",
               "script": {"tool_calls": [{"name": "write_file", "effect": "idempotent",
                                          "target": "out.txt",
                                          "params": {"path": "out.txt", "text": "abc"},
                                          "content": "abc"}],
                          "crash_after_tool_start": True}}
    backend.deliver(handle, message, "d1")
    assert backend.observe(handle).observed == "unknown"
    result = backend.reconcile_tools(handle)
    assert result["unresolved"] == []
    assert result["resolved"][0]["applied"] is False
    assert backend.observe(handle).error["code"] == "tool_effects_reconciled"
    receipt = backend.deliver(handle, {"brief": "resume",
                                       "script": {"result": {"summary": "resumed"}}}, "d2")
    assert receipt.accepted
    assert backend.observe(handle).result["result"] == {"summary": "resumed"}


def test_script_stop_is_not_stopped_until_observed(script):
    backend = script
    handle = backend.start(spec(), ("w1", 1))
    backend.deliver(handle, {"brief": "wait", "script": {"mode": "hang"}}, "d1")
    assert backend.observe(handle).observed == "active"
    receipt = backend.stop(handle, "stop-1")
    assert receipt.accepted and receipt.already_stopped is False
    pending = backend.observe(handle)
    assert pending.observed == "active"
    assert pending.stop_requested
    assert pending.error["code"] == "stop_pending"
    backend.complete_stop(handle)
    stopped = backend.observe(handle)
    assert stopped.observed == "stopped"
    backend.simulate_late_result(handle, "d1", {"summary": "late"})
    assert backend.observe(handle).result is None
    with pytest.raises(BackendError) as caught:
        backend.deliver(handle, {"brief": "late"}, "d2")
    assert caught.value.code == "stopped"


def test_script_stop_before_delivery_is_recorded_without_launch(tmp_path):
    backend = ScriptBackend(tmp_path / "data")
    handle = backend.start(spec(desired="stopped"), ("w1", 1))
    assert backend.observe(handle).observed == "idle"
    assert backend.stop(handle, "stop-1").already_stopped
    assert backend.observe(handle).observed == "stopped"
    with pytest.raises(BackendError) as caught:
        backend.deliver(handle, {"brief": "late"}, "d1")
    assert caught.value.code == "stopped"


def test_script_stop_ignores_changed_program_identity_only(tmp_path):
    first = ScriptBackend(tmp_path / "data", identity={"program": "old"})
    first.start(spec(), ("w1", 1))
    second = ScriptBackend(tmp_path / "data", identity={"program": "new"})

    # Active work under the old execution binding is still refused.
    with pytest.raises(BackendError) as caught:
        second.start(spec(), ("w1", 1))
    assert caught.value.code == "capability_unsupported"

    handle = second.start(spec(desired="stopped"), ("w1", 1))
    assert second.stop(handle, "stop-1").already_stopped
    assert second.observe(handle).observed == "stopped"
    state = json.loads(
        (tmp_path / "data" / "backends" / "script" / "w1" / "1"
         / "state.json").read_text(encoding="utf-8"))
    assert state["agent_identity"] == {"program": "old"}


def test_script_changed_program_delivery_through_stop_handle_is_refused(tmp_path):
    calls: list[str] = []
    first = ScriptBackend(tmp_path / "data", identity={"program": "old"},
                          agent=lambda *args: calls.append("old"))
    handle = first.start(spec(), ("w1", 1))
    assert first.deliver(handle, {"brief": "one"}, "d1").accepted
    assert calls == ["old"]

    state_path = (tmp_path / "data" / "backends" / "script" / "w1" / "1"
                  / "state.json")
    before = state_path.read_bytes()
    second = ScriptBackend(tmp_path / "data", identity={"program": "new"},
                           agent=lambda *args: calls.append("new"))
    stopped_handle = second.start(spec(desired="stopped"), ("w1", 1))

    with pytest.raises(BackendError) as caught:
        second.deliver(stopped_handle, {"brief": "two"}, "d2")
    assert caught.value.code == "capability_unsupported"
    assert calls == ["old"]
    assert state_path.read_bytes() == before

    # The pre-existing receipt still deduplicates, and stop remains available.
    assert second.deliver(stopped_handle, {"brief": "one"}, "d1").duplicate
    assert second.stop(stopped_handle, "stop-1").accepted
    assert second.observe(stopped_handle).observed == "stopped"
    assert calls == ["old"]


def test_script_stop_still_rejects_a_changed_committed_spec(tmp_path):
    backend = ScriptBackend(tmp_path / "data", identity={"program": "same"})
    backend.start(spec(), ("w1", 1))
    with pytest.raises(BackendError) as caught:
        backend.start(spec(node="other", desired="stopped"), ("w1", 1))
    assert caught.value.code == "capability_unsupported"
    assert caught.value.details["fields"] == ["node_id"]


def test_script_checkpoint_restore_preserves_delivery(script, tmp_path):
    backend = script
    handle = backend.start(spec(), ("w1", 1))
    backend.deliver(handle, {"brief": "one", "script": {"result": {"summary": "one"}}}, "d1")
    before = backend.observe(handle)
    checkpoint = backend.checkpoint(handle)
    assert checkpoint.kind == "script-state"

    reopened = ScriptBackend(tmp_path / "data")
    restored = reopened.restore(spec(), checkpoint.as_dict(), ("w1", 1))
    after = reopened.observe(restored)
    assert after.result["result"] == {"summary": "one"}
    duplicate = reopened.deliver(restored, {"brief": "one"}, "d1")
    assert duplicate.duplicate
    assert after.session_ref == before.session_ref


def test_pi_backend_enforces_the_wall_clock_budget(tmp_path):
    fake = tmp_path / "fake-pi"
    fake.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    fake.chmod(0o755)
    backend = PiBackend(tmp_path / "data", executable=str(fake),
                        max_wall_seconds=0.5, grace_seconds=0.5)
    handle = backend.start(spec(), ("w1", 1))
    backend.deliver(handle, {"brief": "hang forever"}, "d1")
    deadline = time.monotonic() + 10
    observation = backend.observe(handle)
    while observation.observed in ("active", "starting") and time.monotonic() < deadline:
        time.sleep(0.05)
        observation = backend.observe(handle)
    assert observation.observed == "failed"
    assert observation.error["code"] == "wall_timeout"


def test_pi_backend_enforces_the_call_budget(tmp_path):
    fake = tmp_path / "chatty-pi"
    fake.write_text(
        "#!/bin/sh\n"
        "i=0\n"
        "while [ $i -lt 50 ]; do\n"
        "  echo '{\"type\":\"tool_execution_start\"}' >> \"$AGENTGENOME_EVENTS\"\n"
        "  i=$((i+1))\n"
        "  sleep 0.02\n"
        "done\n"
        "sleep 30\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    backend = PiBackend(tmp_path / "data", executable=str(fake), max_wall_seconds=30)
    handle = backend.start(spec(budget={"max_tool_calls": 3}), ("w1", 1))
    backend.deliver(handle, {"brief": "loop with tools"}, "d1")
    deadline = time.monotonic() + 15
    observation = backend.observe(handle)
    while observation.observed in ("active", "starting") and time.monotonic() < deadline:
        time.sleep(0.05)
        observation = backend.observe(handle)
    assert observation.observed == "failed"
    assert observation.error["code"] == "budget_exceeded"
    assert observation.error["tool_calls"] > 3


def test_pi_backend_flags_a_final_batch_that_exceeds_the_budget(tmp_path):
    fake = tmp_path / "batch-pi"
    fake.write_text(
        "#!/bin/sh\n"
        "i=0\n"
        "while [ $i -lt 5 ]; do\n"
        "  echo '{\"type\":\"tool_execution_start\"}' >> \"$AGENTGENOME_EVENTS\"\n"
        "  i=$((i+1))\n"
        "done\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    backend = PiBackend(tmp_path / "data", executable=str(fake), max_wall_seconds=30)
    handle = backend.start(spec(budget={"max_tool_calls": 3}), ("w1", 1))
    backend.deliver(handle, {"brief": "burst then exit"}, "d1")
    observation = None
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        observation = backend.observe(handle)
        if observation.observed not in ("active", "starting"):
            break
        time.sleep(0.05)
    assert observation.observed == "failed"
    assert observation.error["code"] == "budget_exceeded"
    exit_record = json.loads(
        (backend._dir(("w1", 1)) / "exit" / "d1.json").read_text(encoding="utf-8"))
    assert exit_record["counts"] == {"provider_calls": 0, "tool_calls": 5}
    assert exit_record["code"] == 0


def test_pi_budget_is_cumulative_across_deliveries_and_gates_dispatch(tmp_path):
    fake = tmp_path / "two-call-pi"
    fake.write_text(
        "#!/bin/sh\n"
        "echo '{\"type\":\"tool_execution_start\"}' >> \"$AGENTGENOME_EVENTS\"\n"
        "echo '{\"type\":\"tool_execution_start\"}' >> \"$AGENTGENOME_EVENTS\"\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    backend = PiBackend(tmp_path / "data", executable=str(fake), max_wall_seconds=30)
    handle = backend.start(spec(budget={"max_tool_calls": 2}), ("w1", 1))
    backend.deliver(handle, {"brief": "one"}, "d1")
    settled = None
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        settled = backend.observe(handle)
        if settled.observed not in ("active", "starting"):
            break
        time.sleep(0.05)
    assert settled.tool_calls == 2
    with pytest.raises(BackendError) as caught:
        backend.deliver(handle, {"brief": "two"}, "d2")
    assert caught.value.code == "budget_exhausted"
    assert caught.value.details["usage"]["tool_calls"] == 2
    assert caught.value.details["caps"] == {"tool_calls": 2}
    assert not (backend._dir(("w1", 1)) / "events" / "d2.jsonl").exists()

    reopened = PiBackend(tmp_path / "data", executable=str(fake), max_wall_seconds=30)
    restored = reopened.start(spec(budget={"max_tool_calls": 2}), ("w1", 1))
    with pytest.raises(BackendError) as caught:
        reopened.deliver(restored, {"brief": "after restart"}, "d2")
    assert caught.value.code == "budget_exhausted"


def test_pi_budget_counts_every_delivery_towards_the_cap(tmp_path):
    fake = tmp_path / "two-call-pi"
    fake.write_text(
        "#!/bin/sh\n"
        "echo '{\"type\":\"tool_execution_start\"}' >> \"$AGENTGENOME_EVENTS\"\n"
        "echo '{\"type\":\"tool_execution_start\"}' >> \"$AGENTGENOME_EVENTS\"\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    backend = PiBackend(tmp_path / "data", executable=str(fake), max_wall_seconds=30)
    handle = backend.start(spec(budget={"max_tool_calls": 3}), ("w1", 1))
    backend.deliver(handle, {"brief": "one"}, "d1")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if (backend._dir(("w1", 1)) / "exit" / "d1.json").exists():
            break
        time.sleep(0.05)
    backend.deliver(handle, {"brief": "two"}, "d2")
    observation = None
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        observation = backend.observe(handle)
        if observation.observed not in ("active", "starting"):
            break
        time.sleep(0.05)
    assert observation.observed == "failed"
    assert observation.error["code"] == "budget_exceeded"
    assert observation.error["tool_calls"] == 4
    exit_record = json.loads(
        (backend._dir(("w1", 1)) / "exit" / "d2.json").read_text(encoding="utf-8"))
    assert exit_record["counts"] == {"provider_calls": 0, "tool_calls": 4}
    assert exit_record["delivery_counts"] == {"provider_calls": 0, "tool_calls": 2}
    assert exit_record["usage_prior"] == {"provider_calls": 0, "tool_calls": 2}


def test_pi_observe_does_not_call_the_exit_gap_unknown(tmp_path):
    fake = tmp_path / "quick-pi"
    fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)
    backend = PiBackend(tmp_path / "data", executable=str(fake))
    handle = backend.start(spec(), ("w1", 1))
    backend.deliver(handle, {"brief": "finish immediately"}, "d1")
    for _ in range(20):
        assert backend.observe(handle).observed != "unknown"
        time.sleep(0.005)


def test_pi_delivery_blocks_on_an_unresolved_receipt(tmp_path):
    backend = PiBackend(tmp_path / "data")
    handle = backend.start(spec(), ("w1", 1))
    receipt = backend._receipt_path(("w1", 1))
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps({
        "phase": "start", "call_id": "call-1", "name": "external", "effect": "unknown",
        "worker_id": "w1", "node_id": "n1", "epoch": 1, "run_id": "r1",
    }) + "\n", encoding="utf-8")
    with pytest.raises(BackendError) as caught:
        backend.deliver(handle, {"brief": "next"}, "d1")
    assert caught.value.code == "unknown_effect"
    assert backend.reconcile_tools(handle)["unresolved"]


def test_pi_result_validation_is_explicit(tmp_path):
    backend = PiBackend(tmp_path / "data")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = {"workspace": str(workspace)}
    delivery = {"message": {"result_path": "result.json",
                            "required_fields": ["summary", "count"]}}
    events = {"final_text": ""}

    result, error = backend._result(state, delivery, events)
    assert result is None and error["code"] == "no_valid_result"

    (workspace / "result.json").write_text("not json", encoding="utf-8")
    result, error = backend._result(state, delivery, events)
    assert result is None and error["code"] == "no_valid_result"

    (workspace / "result.json").write_text('{"summary": "x"}', encoding="utf-8")
    result, error = backend._result(state, delivery, events)
    assert result is None and error["code"] == "no_valid_result"

    (workspace / "result.json").write_text('{"summary": "x", "count": 2}',
                                           encoding="utf-8")
    result, error = backend._result(state, delivery, events)
    assert error is None
    assert result["result"] == {"summary": "x", "count": 2}


def test_pi_prompt_names_only_the_bound_tools(tmp_path):
    backend = PiBackend(tmp_path / "data", tools=("write_result",))
    state = {"worker_id": "w1", "node_id": "n1", "epoch": 1}
    prompt = backend._prompt(state, {
        "brief": "do it", "result_path": "result.json",
        "required_fields": ["summary", "count"],
    }, backend.tools)
    assert "TOOLS: write_result." in prompt
    assert "write_file" not in prompt
    assert "'result.json'" in prompt

    default = PiBackend(tmp_path / "other")._prompt(
        state, {"brief": "do it"}, ("write_file", "read_file"))
    assert "TOOLS: write_file, read_file." in default
    assert "RESULT: when finished, reply with your final answer" in default


def test_pi_prompt_carries_definition_sop_and_instructions(tmp_path):
    backend = PiBackend(tmp_path / "data")
    state = {"worker_id": "w1", "node_id": "n1", "epoch": 1, "agent_ref": "cleaner",
             "context": {"role": "worker", "brief": "Clean one page.",
                         "contract_ref": "clean", "template_ref": "clean-page",
                         "sop_brief": "Iris cleaning overview.",
                         "instructions": "Reject ids below 1; never impute."}}
    prompt = backend._prompt(state, {"brief": "clean"}, ("write_file",))
    assert "CONTEXT: Clean one page." in prompt
    assert "SOP: Iris cleaning overview." in prompt
    assert "INSTRUCTIONS: Reject ids below 1; never impute." in prompt

    joined = backend._prompt(state, {
        "brief": "merge", "children": [
            {"node_id": "n-child-a", "accepted_candidate_ref": "candidate-a"},
            {"node_id": "n-child-b", "accepted_candidate_ref": "candidate-b"}]},
        ("write_file",))
    assert "CHILDREN (all accepted):" in joined
    assert "n-child-a: candidate candidate-a" in joined


def test_pi_prompt_carries_business_context_to_worker_and_checker(tmp_path):
    context = {"role": "worker", "brief": "Clean one page.",
               "business_parameters": {"output_language": "CONTEXT-UNIQUE-614"}}
    state = {"worker_id": "w1", "node_id": "n1", "epoch": 1,
             "agent_ref": "cleaner", "context": context}
    prompt = PiBackend(tmp_path / "data")._prompt(
        state, {"brief": "clean"}, ("write_file",))
    assert "BUSINESS CONTEXT:" in prompt
    assert "CONTEXT-UNIQUE-614" in prompt

    review = {"target_node_id": "n1", "candidate_id": "c1",
              "candidate_round": 1}
    review_prompt = PiBackend(tmp_path / "other")._prompt(
        state, {"brief": "review", "target": {
            "node_id": "n1", "template_ref": "clean", "contract_ref": "clean",
            "context": {"business_parameters": {
                "output_language": "CONTEXT-UNIQUE-614"}}}},
        ("write_file",), review=review)
    assert "business context:" in review_prompt
    assert "CONTEXT-UNIQUE-614" in review_prompt


def test_pi_delivery_captures_the_exact_launch_prompt(tmp_path):
    fake = tmp_path / "quiet-pi"
    fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)
    backend = PiBackend(tmp_path / "data", executable=str(fake), max_wall_seconds=30)
    bound = WorkerSpec(
        worker_id="w1", node_id="n1", epoch=1, intent_seq=1,
        backend_ref="pi", agent_ref="sop-cleaner@v1", desired="active", run_id="r1",
        context={"role": "worker", "brief": "Clean one page.",
                 "contract_ref": "clean-page", "template_ref": "clean-page",
                 "sop_brief": "Iris cleaning overview.",
                 "instructions": "Reject ids below 1; never impute."},
        tool_profile={"tools": [], "backend_tools": ["write_file"]},
    )
    handle = backend.start(bound, ("w1", 1))
    backend.deliver(handle, {"brief": "clean the page"}, "d1")
    deadline = time.monotonic() + 10
    exit_path = backend._dir(("w1", 1)) / "exit" / "d1.json"
    while not exit_path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    state = json.loads(backend._state_path(("w1", 1)).read_text(encoding="utf-8"))
    prompt_path = Path(state["deliveries"]["d1"]["prompt_ref"])
    assert prompt_path == backend._prompt_path(("w1", 1), "d1")
    prompt = prompt_path.read_text(encoding="utf-8")
    assert "AGENT: sop-cleaner@v1" in prompt
    assert "CONTEXT: Clean one page." in prompt
    assert "SOP: Iris cleaning overview." in prompt
    assert "INSTRUCTIONS: Reject ids below 1; never impute." in prompt
    assert "TASK:\nclean the page" in prompt


def _bound_spec(ref: dict, *, context: dict | None = None,
                tool_profile: dict | None = None,
                desired: str = "active") -> WorkerSpec:
    return WorkerSpec(
        worker_id="w1", node_id="n1", epoch=1, intent_seq=1,
        backend_ref="pi", agent_ref="cleaner@v1", desired=desired, run_id="r1",
        context=context if context is not None else {
            "role": "worker", "brief": "clean the table",
            "contract_ref": "clean", "template_ref": "clean",
            "input_refs": [ref],
        },
        tool_profile=tool_profile if tool_profile is not None else {
            "tools": [], "backend_tools": ["write_file"]},
    )


def test_pi_materializes_frozen_inputs_and_keeps_the_binding(tmp_path):
    content = b"frozen dataset"
    ref = {"id": "sha256:" + __import__("hashlib").sha256(content).hexdigest(),
           "sha256": __import__("hashlib").sha256(content).hexdigest(),
           "size": len(content), "media_type": "text/plain"}
    backend = PiBackend(tmp_path / "data", artifact_reader=lambda item: content)
    handle = backend.start(_bound_spec(ref), ("w1", 1))
    workspace = Path(handle.workspace)
    materialized = list((workspace / "inputs").iterdir())
    assert [path.read_bytes() for path in materialized] == [content]
    prompt = backend._prompt(backend._state(("w1", 1)),
                             {"brief": "do it"}, ("write_file",))
    assert "AGENT: cleaner@v1" in prompt
    assert "ROLE: worker" in prompt
    assert "CONTRACT: clean" in prompt
    assert "FROZEN INPUTS" in prompt and "inputs/" in prompt

    with pytest.raises(BackendError) as caught:
        backend.start(_bound_spec(ref, context={"role": "reviewer"}), ("w1", 1))
    assert caught.value.code == "capability_unsupported"

    materialized[0].write_bytes(b"tampered")
    with pytest.raises(BackendError) as caught:
        backend.start(_bound_spec(ref), ("w1", 1))
    assert caught.value.code == "material_missing"


def test_pi_missing_material_or_unknown_tool_fails_explicitly(tmp_path):
    digest = "0" * 64
    ref = {"id": f"sha256:{digest}", "sha256": digest, "size": 4,
           "media_type": "text/plain"}
    backend = PiBackend(tmp_path / "data")
    with pytest.raises(BackendError) as caught:
        backend.start(_bound_spec(ref), ("w1", 1))
    assert caught.value.code == "capability_unsupported"

    other = PiBackend(tmp_path / "wrong", tools=("write_file",),
                      artifact_reader=lambda item: b"xxxx")
    with pytest.raises(BackendError) as caught:
        other.start(_bound_spec(ref), ("w1", 1))
    assert caught.value.code == "material_missing"

    unknown = PiBackend(tmp_path / "unknown", tools=("write_file",),
                        artifact_reader=lambda item: b"xxxx")
    with pytest.raises(BackendError) as caught:
        unknown.start(_bound_spec(ref, tool_profile={"backend_tools": ["shell"]}),
                      ("w1", 1))
    assert caught.value.code == "capability_unsupported"


def test_pi_stop_before_delivery_skips_fetch_until_active(tmp_path):
    content = b"frozen dataset"
    digest = hashlib.sha256(content).hexdigest()
    ref = {"id": f"sha256:{digest}", "sha256": digest, "size": len(content),
           "media_type": "text/plain"}
    reads: list[dict] = []
    backend = PiBackend(
        tmp_path / "data",
        artifact_reader=lambda item: reads.append(item) or content)

    handle = backend.start(_bound_spec(ref, desired="stopped"), ("w1", 1))
    assert reads == [], "a stop-only start fetched frozen materials"
    assert handle.pid is None
    assert backend.stop(handle, "stop-1").already_stopped
    assert backend.observe(handle).observed == "stopped"

    active = backend.start(_bound_spec(ref), ("w1", 1))
    assert reads, "the active path did not materialize the frozen input"
    assert backend._state(("w1", 1))["prepared"] is True
    [material] = list((Path(active.workspace) / "inputs").iterdir())
    material.unlink()
    with pytest.raises(BackendError) as caught:
        backend.start(_bound_spec(ref), ("w1", 1))
    assert caught.value.code == "material_missing"


def test_pi_direct_delivery_to_an_unprepared_stop_state_is_refused(tmp_path):
    content = b"frozen dataset"
    digest = hashlib.sha256(content).hexdigest()
    ref = {"id": f"sha256:{digest}", "sha256": digest, "size": len(content),
           "media_type": "text/plain"}
    reads: list[dict] = []
    backend = PiBackend(
        tmp_path / "data",
        artifact_reader=lambda item: reads.append(item) or content)
    handle = backend.start(_bound_spec(ref, desired="stopped"), ("w1", 1))
    state_path = backend._state_path(("w1", 1))
    before = state_path.read_bytes()

    with pytest.raises(BackendError) as refused:
        backend.deliver(handle, {"brief": "clean the table"}, "d1")

    assert refused.value.code == "capability_unsupported"
    assert refused.value.details.get("prepared") is False
    assert reads == [], "an unprepared execution fetched frozen materials"
    assert handle.pid is None
    assert state_path.read_bytes() == before, "a refused delivery mutated state"
    state = backend._state(("w1", 1))
    assert state["order"] == [] and state["deliveries"] == {}
    assert state["stop"] is None
    assert not backend._events_path(("w1", 1), "d1").exists()
    assert not backend._prompt_path(("w1", 1), "d1").exists()
    assert not backend._spawn_record_path(("w1", 1), "d1").exists()

    # The committed stop still completes; a later active start prepares.
    assert backend.stop(handle, "stop-1").already_stopped
    assert backend.observe(handle).observed == "stopped"
    backend.start(_bound_spec(ref), ("w1", 1))
    assert backend._state(("w1", 1))["prepared"] is True


def test_pi_stop_still_rejects_a_changed_execution_binding(tmp_path):
    content = b"frozen dataset"
    digest = hashlib.sha256(content).hexdigest()
    ref = {"id": f"sha256:{digest}", "sha256": digest, "size": len(content),
           "media_type": "text/plain"}
    backend = PiBackend(tmp_path / "data", artifact_reader=lambda item: content)
    backend.start(_bound_spec(ref), ("w1", 1))
    with pytest.raises(BackendError) as caught:
        backend.start(_bound_spec(ref, context={"role": "reviewer"},
                                  desired="stopped"), ("w1", 1))
    assert caught.value.code == "capability_unsupported"


def test_pi_stop_terminates_a_running_child_after_input_tamper(tmp_path):
    content = b"frozen dataset"
    digest = hashlib.sha256(content).hexdigest()
    ref = {"id": f"sha256:{digest}", "sha256": digest, "size": len(content),
           "media_type": "text/plain"}
    fake = tmp_path / "fake-pi"
    fake.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    fake.chmod(0o755)
    backend = PiBackend(tmp_path / "data", executable=str(fake),
                        artifact_reader=lambda item: content,
                        max_wall_seconds=30)
    handle = backend.start(_bound_spec(ref), ("w1", 1))
    backend.deliver(handle, {"brief": "hang"}, "d1")

    pid = None
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        delivery = backend._state(("w1", 1))["deliveries"]["d1"]
        pid = delivery.get("pid")
        if isinstance(pid, int) and _process_running(pid):
            break
        time.sleep(0.02)
    assert isinstance(pid, int) and _process_running(pid)

    [material] = list((Path(handle.workspace) / "inputs").iterdir())
    material.unlink()
    with pytest.raises(BackendError) as caught:
        backend.start(_bound_spec(ref), ("w1", 1))
    assert caught.value.code == "material_missing"

    stopped_handle = backend.start(
        _bound_spec(ref, desired="stopped"), ("w1", 1))
    receipt = backend.stop(stopped_handle, "stop-1")
    assert receipt.accepted and receipt.pid == pid
    observation = backend.observe(stopped_handle)
    deadline = time.monotonic() + 10
    while observation.observed == "active" and time.monotonic() < deadline:
        time.sleep(0.05)
        observation = backend.observe(stopped_handle)
    assert observation.observed == "stopped"
    assert not _process_running(pid)


def test_pi_system_tools_follow_the_committed_profile(tmp_path):
    empty = {"role": "worker", "brief": "b", "contract_ref": "c",
             "template_ref": "t", "input_refs": []}

    def bound(tools, backend):
        return backend.start(_bound_spec(
            {"id": "sha256:" + "0" * 64, "sha256": "0" * 64, "size": 0,
             "media_type": "text/plain"},
            context=empty, tool_profile={"tools": tools}), ("w1", 1))

    ok_tools = [{"name": "node.submit_candidate", "version": "1",
                 "scope": "node:self"}]
    unknown = PiBackend(tmp_path / "unknown", data_dir=tmp_path / "unknown")
    with pytest.raises(BackendError) as caught:
        bound([{"name": "node.shell", "version": "1", "scope": "node:self"}],
              unknown)
    assert caught.value.code == "capability_unsupported"

    stale = PiBackend(tmp_path / "version", data_dir=tmp_path / "version")
    with pytest.raises(BackendError) as caught:
        bound([{"name": "node.submit_candidate", "version": "2",
                "scope": "node:self"}], stale)
    assert caught.value.code == "capability_unsupported"

    wide = PiBackend(tmp_path / "scope", data_dir=tmp_path / "scope")
    with pytest.raises(BackendError) as caught:
        bound([{"name": "node.submit_candidate", "version": "1",
                "scope": "run:all"}], wide)
    assert caught.value.code == "unauthorized"

    unwired = PiBackend(tmp_path / "unwired")
    with pytest.raises(BackendError) as caught:
        bound(ok_tools, unwired)
    assert caught.value.code == "capability_unsupported"

    fake = tmp_path / "fake-pi"
    fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)
    wired = PiBackend(tmp_path / "wired", executable=str(fake),
                      data_dir=tmp_path / "wired")
    wired.set_credential_issuer(
        lambda binding: {"endpoint": "http://127.0.0.1:1",
                         "token": "delivery-token"})
    handle = bound(ok_tools, wired)
    assert handle.worker_id == "w1"
    wired.deliver(handle, {"kind": "start", "brief": "b", "goal": "g",
                           "acceptance": "a",
                           "output_schema": {"type": "object"}}, "d1")
    context_path = wired._tool_context_path(("w1", 1), "d1")
    assert context_path.is_file()
    assert (context_path.stat().st_mode & 0o077) == 0
    context = json.loads(context_path.read_text(encoding="utf-8"))
    assert context["format"] == "agentgenome-worker-tool-context/2"
    assert context["endpoint"] == "http://127.0.0.1:1"
    assert "data_dir" not in context and "python" not in context


def test_pi_review_tool_is_live_only_for_a_bound_delivery(tmp_path):
    empty = {"role": "worker", "brief": "b", "contract_ref": "c",
             "template_ref": "t", "input_refs": []}
    fake = tmp_path / "fake-pi"
    fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)
    backend = PiBackend(tmp_path / "data", executable=str(fake),
                        data_dir=tmp_path / "data")
    backend.set_credential_issuer(
        lambda binding: {"endpoint": "http://127.0.0.1:1",
                         "token": "delivery-token"})
    backend.start(_bound_spec(
        {"id": "sha256:" + "0" * 64, "sha256": "0" * 64, "size": 0,
         "media_type": "text/plain"},
        context=empty,
        tool_profile={"tools": [{"name": "node.submit_review", "version": "1",
                                 "scope": "review:assigned"}]}), ("w1", 1))
    state = backend._state(("w1", 1))

    names, specs, binding, decision = backend._system_tools(state, {"kind": "start"})
    assert names == [] and specs == [] and binding is None and decision is None

    with pytest.raises(BackendError) as caught:
        backend._system_tools(state, {"kind": "review_request",
                                      "node_id": "n1", "reviewer_ref": "r"})
    assert caught.value.code == "capability_unsupported"

    names, specs, binding, decision = backend._system_tools(state, {
        "kind": "review_request", "node_id": "n1", "candidate_id": "c1",
        "candidate_round": 1, "reviewer_ref": "r"})
    assert names == ["node.submit_review"]
    assert specs[0]["parameters"]["required"] == [
        "candidate_id", "candidate_round", "verdict", "feedback_ref"]
    assert binding == {"target_node_id": "n1", "candidate_id": "c1",
                       "candidate_round": 1, "reviewer_ref": "r"}
    assert decision is None


def test_capabilities_are_explicit(tmp_path):
    capabilities = PiBackend(tmp_path / "capabilities").capabilities().as_dict()
    assert capabilities == {
        "resumable_session": True,
        "live_observation": "limited",
        "idempotent_delivery": False,
        "stop": "process",
        "checkpoint": True,
        "backend_tools": ["write_file", "read_file"],
        "internal_activities": "none",
    }
