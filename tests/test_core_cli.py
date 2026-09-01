from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from adapters import LocalShell, TerminalHuman
from builder import compile
from core import (
    CheckpointError,
    Answer,
    JudgeResult,
    PortError,
    Ports,
    ShellResult,
    handshake,
    resume,
    run,
)


def test_data_cleaning_run_records_all_port_calls(tmp_path: Path) -> None:
    source = tmp_path / "input.csv"
    source.write_text("name,age\nAda,36\nAda,36\nBob,\nCy,12\n", encoding="utf-8")
    graph = compile("templates/data-cleaning")
    result = run(graph, {"data_file": str(source)}, Ports(LocalShell()), tmp_path / "run")
    assert result.status == "succeeded"
    events = [json.loads(line) for line in (tmp_path / "run" / "ledger.jsonl").read_text().splitlines()]
    assert len([item for item in events if item["event"] == "call_shell"]) == 2
    assert len([item for item in events if item["event"] == "check" and item["rule_type"] == "run"]) == 3
    assert len([item for item in events if item["event"] == "port_error"]) == 0
    checkpoint = json.loads((tmp_path / "run" / "checkpoint.json").read_text())
    assert checkpoint["nodes"]["clean-sop"]["status"] == "succeeded"
    for ref in result.outputs.values():
        assert (tmp_path / "run" / ref["path"]).is_file()


def test_failure_retries_then_propagates_to_root(tmp_path: Path) -> None:
    graph = compile("templates/data-cleaning")
    result = run(graph, {"data_file": str(tmp_path / "missing.csv")}, Ports(LocalShell()), tmp_path / "run")
    assert result.status == "failed"
    events = [json.loads(line) for line in (tmp_path / "run" / "ledger.jsonl").read_text().splitlines()]
    assert [item["event"] for item in events].count("node_retry") == 1
    assert [item["event"] for item in events].count("node_failed") == 2
    assert events[-1]["event"] == "run_failed"


class SimulatedKill(BaseException):
    pass


class StopThenRun:
    def __init__(self) -> None:
        self.stop = True
        self.local = LocalShell()

    def execute(self, command: str, cwd: Path) -> ShellResult:
        if "MARK_STOP" in command and self.stop:
            self.stop = False
            raise SimulatedKill
        return self.local.execute(command.replace("MARK_STOP", "echo '{\"rows\": 1}'"), cwd)


def test_resume_memoizes_successful_nodes(tmp_path: Path) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "graph.yaml").write_text(
        """version: "gt/1.0"
node:
  id: root
  mode: sequence
  output: {report: {type: json, compose: [second.output.report]}}
  verify: [{metric: "report.rows > 0"}]
  children:
    - id: first
      do: {run: "echo done > {artifact}"}
      output: {done: {type: path}}
      verify: [{run: "test -s {artifact}"}]
    - id: second
      do: {run: "MARK_STOP > {artifact}"}
      output: {report: {type: json}}
      verify: [{metric: "report.rows > 0"}]
""",
        encoding="utf-8",
    )
    graph = compile(package)
    run_dir = tmp_path / "run"
    with pytest.raises(SimulatedKill):
        run(graph, {}, Ports(StopThenRun()), run_dir)
    resumed_shell = StopThenRun()
    resumed_shell.stop = False
    result = resume(graph, Ports(resumed_shell), run_dir)
    assert result.status == "succeeded"
    events = [json.loads(line) for line in (run_dir / "ledger.jsonl").read_text().splitlines()]
    assert len([event for event in events if event["event"] == "call_shell" and event["node"] == "root.first"]) == 1


def test_resume_rejects_pin_mismatch_and_terminal_run(tmp_path: Path) -> None:
    graph = compile("templates/data-cleaning")
    run_dir = tmp_path / "run"
    result = run(graph, {"data_file": str(tmp_path / "missing")}, Ports(LocalShell()), run_dir)
    assert result.status == "failed"
    with pytest.raises(CheckpointError):
        resume(graph, Ports(LocalShell()), run_dir)
    raw = json.loads((run_dir / "checkpoint.json").read_text())
    raw["pin"] = "sha256:different"
    (run_dir / "checkpoint.json").write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(CheckpointError, match="pin"):
        resume(graph, Ports(LocalShell()), run_dir)


class BrokenShell:
    def execute(self, command: str, cwd: Path) -> ShellResult:
        raise PortError("shell 不可用")


def test_port_error_remains_nonterminal_and_is_ledgored(tmp_path: Path) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "graph.yaml").write_text(
        """version: "gt/1.0"
node:
  id: root
  do: {run: "echo hi > {artifact}"}
  output: {message: {type: path}}
  verify: [{run: "test -s {artifact}"}]
""",
        encoding="utf-8",
    )
    run_dir = tmp_path / "run"
    with pytest.raises(PortError):
        run(compile(package), {}, Ports(BrokenShell()), run_dir)
    events = [json.loads(line) for line in (run_dir / "ledger.jsonl").read_text().splitlines()]
    assert events[-1]["event"] == "port_error"
    assert "root" not in json.loads((run_dir / "checkpoint.json").read_text())["nodes"]


def test_core_has_no_builder_or_adapter_runtime_imports() -> None:
    for path in Path("core").glob("*.py"):
        content = path.read_text(encoding="utf-8")
        assert "from builder" not in content
        assert "from adapters" not in content


class ScriptedAI:
    def capabilities(self) -> dict[str, object]:
        return {"max_context": None, "structured_output": False}


class ScriptedHuman:
    def __init__(self, interactive: bool) -> None:
        self.interactive = interactive

    def capabilities(self) -> dict[str, object]:
        return {"interactive": self.interactive}


def _graph_with_needs(tmp_path: Path, needs: str) -> object:
    package = tmp_path / "pkg"
    package.mkdir(parents=True)
    (package / "graph.yaml").write_text(
        f"""version: "gt/1.0"
node:
  id: root
  needs: [{needs}]
  do: {{run: "echo hi > {{artifact}}"}}
  output: {{message: {{type: path}}}}
  verify: [{{run: "test -s {{artifact}}"}}]
""",
        encoding="utf-8",
    )
    return compile(package)


def test_handshake_rejects_absent_or_noninteractive_ports(tmp_path: Path) -> None:
    ai_graph = _graph_with_needs(tmp_path / "ai", "shell, llm")
    absent_ai = handshake(ai_graph, Ports(LocalShell()))
    assert [(item.code, item.node) for item in absent_ai] == [("E_NEEDS_PORT_ABSENT", "root")]
    assert handshake(ai_graph, Ports(LocalShell(), ai=ScriptedAI())) == []

    human_graph = _graph_with_needs(tmp_path / "human", "shell, human")
    absent_human = handshake(human_graph, Ports(LocalShell()))
    assert [(item.code, item.node) for item in absent_human] == [("E_NEEDS_PORT_ABSENT", "root")]

    noninteractive = handshake(human_graph, Ports(LocalShell(), human=ScriptedHuman(False)))
    assert [(item.code, item.node) for item in noninteractive] == [("E_NEEDS_NONINTERACTIVE", "root")]


def test_handshake_accepts_declared_ports_and_m0_data_cleaning() -> None:
    graph = compile("templates/data-cleaning")
    assert handshake(graph, Ports(LocalShell())) == []


def _llm_graph(tmp_path: Path, on_fail: str = "report") -> object:
    package = tmp_path / "pkg"
    package.mkdir(parents=True)
    (package / "graph.yaml").write_text(
        f"""version: "gt/1.0"
node:
  id: root
  needs: [shell, llm]
  do: {{llm: "write file to {{artifact}}"}}
  output: {{message: {{type: path}}}}
  verify: [{{run: "test -s {{artifact}}"}}]
  on_fail: {on_fail}
""",
        encoding="utf-8",
    )
    return compile(package)


class ArtifactWritingAI:
    def __init__(self, fail_first: bool = False) -> None:
        self.fail_first = fail_first
        self.prompts: list[str] = []

    def work(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.fail_first or len(self.prompts) > 1:
            Path(prompt.removeprefix("write file to ")).write_text("AI output\n", encoding="utf-8")
        return "work complete"

    def capabilities(self) -> dict[str, object]:
        return {"max_context": 1234, "structured_output": False}


def test_llm_leaf_records_artifact_logs_and_port_capabilities(tmp_path: Path) -> None:
    ai = ArtifactWritingAI()
    result = run(_llm_graph(tmp_path), {}, Ports(LocalShell(), ai=ai), tmp_path / "run")
    assert result.status == "succeeded"
    events = [json.loads(line) for line in (tmp_path / "run" / "ledger.jsonl").read_text().splitlines()]
    started = events[0]
    call = next(event for event in events if event["event"] == "call_ai")
    assert started["ports"] == {"ai": ai.capabilities(), "human": None}
    assert (tmp_path / "run" / result.outputs["message"]["path"]).read_text() == "AI output\n"
    for key, content in (("prompt", ai.prompts[0]), ("response", "work complete")):
        ref = call[key]
        assert (tmp_path / "run" / ref["path"]).read_text() == content


def test_llm_leaf_missing_artifact_fails_and_retry_replays_same_prompt(tmp_path: Path) -> None:
    graph = _llm_graph(tmp_path, "{retry: 2}")
    ai = ArtifactWritingAI(fail_first=True)
    result = run(graph, {}, Ports(LocalShell(), ai=ai), tmp_path / "run")
    assert result.status == "succeeded"
    assert ai.prompts == [ai.prompts[0], ai.prompts[0]]

    class MissingArtifactAI(ArtifactWritingAI):
        def work(self, prompt: str) -> str:
            self.prompts.append(prompt)
            return "work complete but no artifact"

    missing = MissingArtifactAI()
    failure = run(_llm_graph(tmp_path / "missing"), {}, Ports(LocalShell(), ai=missing), tmp_path / "missing-run")
    assert failure.status == "failed"
    assert failure.failure["stage"] == "do"


def _review_graph(tmp_path: Path, rules: str, needs: str) -> object:
    package = tmp_path / "pkg"
    package.mkdir(parents=True)
    (package / "graph.yaml").write_text(
        f"""version: "gt/1.0"
node:
  id: root
  needs: [{needs}]
  do: {{run: "echo reviewable > {{artifact}}"}}
  output: {{message: {{type: text}}}}
  verify:
{rules}
""",
        encoding="utf-8",
    )
    return compile(package)


class ReviewAI:
    def __init__(self, passed: bool = True, malformed: bool = False) -> None:
        self.passed, self.malformed = passed, malformed
        self.contexts: list[dict[str, object]] = []

    def judge(self, criterion: str, context: dict[str, object]) -> JudgeResult | object:
        self.contexts.append(context)
        return object() if self.malformed else JudgeResult(self.passed, "review reason")

    def capabilities(self) -> dict[str, object]:
        return {"max_context": None, "structured_output": False}


class ReviewHuman:
    def __init__(self, approve: bool) -> None:
        self.approve = approve
        self.calls = 0

    def ask(self, question: str, evidence: dict[str, object]) -> Answer:
        self.calls += 1
        return Answer(self.approve, None if self.approve else "needs changes")

    def capabilities(self) -> dict[str, object]:
        return {"interactive": True}


def test_llm_and_human_verify_record_evidence_and_short_circuit(tmp_path: Path) -> None:
    graph = _review_graph(
        tmp_path,
        '    - llm: "review {artifact}"\n    - human: "approve {artifact}"',
        "shell, llm, human",
    )
    ai, human = ReviewAI(), ReviewHuman(True)
    result = run(graph, {}, Ports(LocalShell(), ai=ai, human=human), tmp_path / "run")
    assert result.status == "succeeded"
    assert set(ai.contexts[0]) == {"artifact", "outputs"}
    events = [json.loads(line) for line in (tmp_path / "run" / "ledger.jsonl").read_text().splitlines()]
    checks = [event for event in events if event["event"] == "check"]
    assert [event["rule_type"] for event in checks] == ["llm", "human"]
    assert (tmp_path / "run" / checks[0]["criterion"]["path"]).read_text().startswith("review ")
    assert checks[1]["feedback"] is None

    failed_ai, uncalled_human = ReviewAI(False), ReviewHuman(True)
    failed = run(graph, {}, Ports(LocalShell(), ai=failed_ai, human=uncalled_human), tmp_path / "failed")
    assert failed.status == "failed"
    assert uncalled_human.calls == 0


def test_verify_protocol_errors_are_hard_errors(tmp_path: Path) -> None:
    graph = _review_graph(tmp_path, '    - llm: "review"', "shell, llm")
    with pytest.raises(PortError) as raised:
        run(graph, {}, Ports(LocalShell(), ai=ReviewAI(malformed=True)), tmp_path / "run")
    assert raised.value.kind == "bad_output"


def test_llm_verify_failure_retries_with_a_fresh_judgment(tmp_path: Path) -> None:
    package = tmp_path / "retry"
    package.mkdir()
    (package / "graph.yaml").write_text(
        """version: "gt/1.0"
node:
  id: root
  do: {run: "echo reviewable > {artifact}"}
  output: {message: {type: text}}
  verify: [{llm: "review"}]
  on_fail: {retry: 2}
""",
        encoding="utf-8",
    )

    class FlippingAI(ReviewAI):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def judge(self, criterion: str, context: dict[str, object]) -> JudgeResult:
            self.calls += 1
            return JudgeResult(self.calls == 2, "retry" if self.calls == 1 else "approved")

    ai = FlippingAI()
    result = run(compile(package), {}, Ports(LocalShell(), ai=ai), tmp_path / "run")
    assert result.status == "succeeded"
    assert ai.calls == 2
    events = [
        json.loads(line)
        for line in (tmp_path / "run" / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [event["verdict"] for event in events if event["event"] == "check"] == ["fail", "pass"]
    assert [event["event"] for event in events].count("node_retry") == 1


def test_danger_rejection_abandons_and_resume_reenters_gate(tmp_path: Path) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "graph.yaml").write_text(
        """version: "gt/1.0"
node:
  id: root
  needs: [shell, human]
  mode: sequence
  output: {result: {type: path, compose: [guarded.output.result]}}
  verify: [{run: "test -s {output.result}"}]
  children:
    - id: guarded
      danger: true
      do: {run: "echo done > {artifact}"}
      output: {result: {type: path}}
      verify: [{run: "test -s {artifact}"}]
    - id: pending
      do: {run: "echo never > {artifact}"}
      output: {result: {type: path}}
      verify: [{run: "test -s {artifact}"}]
""",
        encoding="utf-8",
    )
    class GateHuman:
        def __init__(self) -> None:
            self.answers = [False, True]
            self.calls = 0

        def ask(self, question: str, evidence: dict[str, object]) -> Answer:
            answer = self.answers.pop(0)
            self.calls += 1
            return Answer(answer, None if answer else "not now")

        def capabilities(self) -> dict[str, object]:
            return {"interactive": True}

    graph, human, run_dir = compile(package), GateHuman(), tmp_path / "run"
    abandoned = run(graph, {}, Ports(LocalShell(), human=human), run_dir)
    assert abandoned.status == "abandoned"
    assert abandoned.abandonment["reason"] == "not now"
    assert {entry["node"] for entry in abandoned.abandonment["completed"]} == set()
    checkpoint = json.loads((run_dir / "checkpoint.json").read_text())
    assert checkpoint["nodes"]["root"]["status"] == "abandoned"
    assert checkpoint["nodes"]["root.guarded"]["status"] == "abandoned"
    assert "root.pending" not in checkpoint["nodes"]
    resumed = resume(graph, Ports(LocalShell(), human=human), run_dir)
    assert resumed.status == "succeeded"
    assert human.calls == 2


def test_try_static_and_llm_route_select_fallback(tmp_path: Path) -> None:
    def graph(route: str) -> object:
        package = tmp_path / route
        package.mkdir()
        second = (
            "echo {tried.first.stage} > {artifact}"
            if route == "static" else "echo yes > {artifact}"
        )
        (package / "graph.yaml").write_text(
            f"""version: "gt/1.0"
node:
  id: root
  mode: try
  route: {route}
  needs: [shell{", llm" if route == "llm" else ""}]
  output: {{result: {{type: path, compose: [first.output.result]}}}}
  verify: [{{run: "test -s {{output.result}}"}}]
  children:
    - id: first
      route_hint: fails
      do: {{run: "echo partial > {{artifact}}; echo failure >&2; false"}}
      output: {{result: {{type: path}}}}
      verify: [{{run: "test -s {{artifact}}"}}]
    - id: second
      route_hint: succeeds
      do: {{run: "{second}"}}
      output: {{result: {{type: path}}}}
      verify: [{{run: "test -s {{artifact}}"}}]
""", encoding="utf-8")
        return compile(package)

    static = run(graph("static"), {}, Ports(LocalShell()), tmp_path / "static-run")
    assert static.status == "succeeded"
    assert (tmp_path / "static-run" / static.outputs["result"]["path"]).read_text() == "do\n"

    class RouteAI(ArtifactWritingAI):
        def __init__(self) -> None:
            super().__init__()
            self.routes: list[list[str]] = []

        def route(self, failures: list[object], candidates: list[object]) -> str:
            self.routes.append([candidate.id for candidate in candidates])
            return "first" if len(self.routes) == 1 else "second"

    ai = RouteAI()
    routed = run(graph("llm"), {}, Ports(LocalShell(), ai=ai), tmp_path / "llm-run")
    assert routed.status == "succeeded"
    assert ai.routes == [["first", "second"], ["second"]]
    events = [json.loads(line) for line in (tmp_path / "llm-run" / "ledger.jsonl").read_text().splitlines()]
    routes = [event for event in events if event["event"] == "route"]
    assert len(routes) == 2
    evidence = routes[1]["failures"][0]["evidence"]
    paths = {ref["path"] for ref in evidence}
    assert "artifacts/root--first.out" in paths
    assert (tmp_path / "llm-run" / "artifacts/root--first.out").read_text() == "partial\n"
    logs = [tmp_path / "llm-run" / path for path in paths if path.startswith("logs/")]
    assert {path.read_text() for path in logs} == {"", "failure\n"}


@pytest.mark.parametrize(("policy", "approve", "status"), [
    ("abort", None, "abandoned"), ("ask", False, "abandoned"),
])
def test_on_fail_abort_and_ask_denial_abandon(
    tmp_path: Path, policy: str, approve: bool | None, status: str
) -> None:
    package = tmp_path / policy
    package.mkdir()
    (package / "graph.yaml").write_text(f"""version: "gt/1.0"
node:
  id: root
  needs: [shell{", human" if policy == "ask" else ""}]
  do: {{run: "false"}}
  output: {{result: {{type: path}}}}
  verify: [{{run: "test -s {{artifact}}"}}]
  on_fail: {policy}
""", encoding="utf-8")
    class Human:
        def ask(self, question: str, evidence: dict[str, object]) -> Answer:
            return Answer(approve, "stop")  # type: ignore[arg-type]
        def capabilities(self) -> dict[str, object]:
            return {"interactive": True}
    result = run(compile(package), {}, Ports(LocalShell(), human=Human() if approve is not None else None), tmp_path / "run")
    assert result.status == status
    assert result.abandonment["reason"] == ("stop" if policy == "ask" else "节点声明放弃：do 命令退出码 1")


def test_on_fail_ask_approval_reenters_node(tmp_path: Path) -> None:
    package = tmp_path / "pkg"; package.mkdir()
    (package / "graph.yaml").write_text("""version: "gt/1.0"
node:
  id: root
  needs: [shell, human]
  do: {run: "MARK > {artifact}"}
  output: {result: {type: path}}
  verify: [{run: "test -s {artifact}"}]
  on_fail: ask
""", encoding="utf-8")
    class Shell:
        calls = 0
        def execute(self, command: str, cwd: Path) -> ShellResult:
            self.calls += 1
            if self.calls == 1: return ShellResult(command, 1, "", "", 0)
            if "> " not in command:
                return LocalShell().execute(command, cwd)
            Path(command.split("> ")[1]).write_text("ok")
            return ShellResult(command, 0, "", "", 0)
    class Human:
        def ask(self, question: str, evidence: dict[str, object]) -> Answer: return Answer(True)
        def capabilities(self) -> dict[str, object]: return {"interactive": True}
    assert run(compile(package), {}, Ports(Shell(), human=Human()), tmp_path / "run").status == "succeeded"


def test_on_fail_ask_evidence_contains_faildecl_artifact_and_cause_logs(tmp_path: Path) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "graph.yaml").write_text(
        """version: "gt/1.0"
node:
  id: root
  do: {run: "echo partial > {artifact}; printf stdout-detail; printf stderr-detail >&2; false"}
  output: {result: {type: path}}
  verify: [{run: "test -s {artifact}"}]
  on_fail: ask
""",
        encoding="utf-8",
    )

    class Human:
        def __init__(self) -> None:
            self.question = ""
            self.evidence: dict[str, object] = {}

        def ask(self, question: str, evidence: dict[str, object]) -> Answer:
            self.question, self.evidence = question, evidence
            return Answer(False, "  Keep Mixed Case  ")

        def capabilities(self) -> dict[str, object]:
            return {"interactive": True}

    human = Human()
    run_dir = tmp_path / "run"
    result = run(compile(package), {}, Ports(LocalShell(), human=human), run_dir)
    assert result.status == "abandoned"
    assert result.abandonment["reason"] == "  Keep Mixed Case  "

    detail = human.evidence["failure"]
    assert isinstance(detail, dict)
    assert detail["candidate"] == "root"
    assert detail["stage"] == "do"
    assert detail["reason"] == "do 命令退出码 1"
    assert detail["attempt"] == 1
    assert isinstance(detail["cause_seq"], int)
    assert "第 1 次尝试失败" in human.question
    refs = detail["evidence"]
    assert isinstance(refs, list)
    paths = {ref["path"] for ref in refs if isinstance(ref, dict)}
    assert paths == {
        "artifacts/root.out",
        f"logs/{detail['cause_seq']:04d}-stdout.log",
        f"logs/{detail['cause_seq']:04d}-stderr.log",
    }
    assert (run_dir / f"logs/{detail['cause_seq']:04d}-stdout.log").read_text() == "stdout-detail"
    assert (run_dir / f"logs/{detail['cause_seq']:04d}-stderr.log").read_text() == "stderr-detail"
    events = [json.loads(line) for line in (run_dir / "ledger.jsonl").read_text().splitlines()]
    call = next(event for event in events if event["event"] == "call_human")
    assert call["evidence"] == human.evidence
    assert call["feedback"] == "  Keep Mixed Case  "


def test_on_fail_ask_refused_is_hard_error(tmp_path: Path) -> None:
    package = tmp_path / "pkg"; package.mkdir()
    (package / "graph.yaml").write_text("""version: "gt/1.0"
node:
  id: root
  needs: [shell, human]
  do: {run: "false"}
  output: {result: {type: path}}
  verify: [{run: "test -s {artifact}"}]
  on_fail: ask
""", encoding="utf-8")
    class RefusingHuman:
        def ask(self, question: str, evidence: dict[str, object]) -> Answer:
            raise PortError("no tty", kind="refused")
        def capabilities(self) -> dict[str, object]: return {"interactive": True}
    with pytest.raises(PortError, match="no tty") as raised:
        run(compile(package), {}, Ports(LocalShell(), human=RefusingHuman()), tmp_path / "run")
    assert raised.value.kind == "refused"


@pytest.mark.parametrize("policy", ["ask", "abort"])
def test_try_node_on_fail_transitions_to_abandoned(tmp_path: Path, policy: str) -> None:
    package = tmp_path / policy; package.mkdir()
    (package / "graph.yaml").write_text(f"""version: "gt/1.0"
node:
  id: root
  mode: try
  needs: [shell{", human" if policy == "ask" else ""}]
  on_fail: {policy}
  output: {{result: {{type: path, compose: [candidate.output.result]}}}}
  verify: [{{run: "test -s {{output.result}}"}}]
  children:
    - id: candidate
      do: {{run: "false"}}
      output: {{result: {{type: path}}}}
      verify: [{{run: "test -s {{artifact}}"}}]
""", encoding="utf-8")
    class Human:
        def ask(self, question: str, evidence: dict[str, object]) -> Answer: return Answer(False, "stop")
        def capabilities(self) -> dict[str, object]: return {"interactive": True}
    result = run(compile(package), {}, Ports(LocalShell(), human=Human() if policy == "ask" else None), tmp_path / "run")
    assert result.status == "abandoned"


def test_danger_batch_approval_audits_selection_once(tmp_path: Path) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "graph.yaml").write_text(
        """version: "gt/1.0"
node:
  id: root
  danger: true
  do: {run: "false"}
  output: {result: {type: path}}
  verify: [{run: "test -s {artifact}"}]
  on_fail: {retry: 2}
""",
        encoding="utf-8",
    )

    class TtyInput(io.StringIO):
        def isatty(self) -> bool:
            return True

    result = run(
        compile(package),
        {},
        Ports(LocalShell(), human=TerminalHuman(TtyInput("a\n"), io.StringIO())),
        tmp_path / "run",
    )
    assert result.status == "failed"
    events = [
        json.loads(line)
        for line in (tmp_path / "run" / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    calls = [event for event in events if event["event"] == "call_human"]
    assert len(calls) == 1
    assert calls[0]["batch_approval"] is True
