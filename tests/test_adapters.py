from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import pytest

from adapters import KimiCLI, TerminalHuman
from core import PortError


def test_kimi_cli_subprocess_boundaries(monkeypatch) -> None:
    calls: list[list[str]] = []
    def ok(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 9, "answer", "ignored")
    monkeypatch.setattr(subprocess, "run", ok)
    assert KimiCLI("fake", "m", 2).work("prompt") == "answer"
    assert calls == [["fake", "-p", "prompt", "-m", "m"]]

    def timeout(*args, **kwargs): raise subprocess.TimeoutExpired("kimi", 2)
    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(PortError) as error: KimiCLI().work("x")
    assert error.value.kind == "timeout"

    def missing(*args, **kwargs): raise FileNotFoundError("missing")
    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(PortError) as error: KimiCLI().work("x")
    assert error.value.kind == "unavailable"

    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "not-json", ""))
    with pytest.raises(PortError) as error: KimiCLI().judge("criterion", {})
    assert error.value.kind == "bad_output"


def test_kimi_cli_fake_executable_uses_documented_print_surface(
    tmp_path: Path, monkeypatch
) -> None:
    fake = tmp_path / "fake-kimi"
    arguments = tmp_path / "arguments.json"
    fake.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path
Path(os.environ["KIMI_FAKE_ARGS"]).write_text(json.dumps(sys.argv[1:]))
print(os.environ["KIMI_FAKE_STDOUT"])
raise SystemExit(9)
""",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setenv("KIMI_FAKE_ARGS", str(arguments))
    monkeypatch.setenv("KIMI_FAKE_STDOUT", "work complete")
    client = KimiCLI(str(fake), "configured-model", 2)
    assert client.work("write artifact") == "work complete\n"
    assert json.loads(arguments.read_text()) == ["-p", "write artifact", "-m", "configured-model"]

    monkeypatch.setenv("KIMI_FAKE_STDOUT", '{"passed": true, "reason": "looks good"}')
    assert client.judge("judge it", {"artifact": "ref"}).passed is True
    assert "judge it" in json.loads(arguments.read_text())[1]

    monkeypatch.setenv("KIMI_FAKE_STDOUT", '{"chosen": "second"}')
    from core import Candidate, FailDecl
    assert client.route(
        [FailDecl("first", "do", "failed", [], 1)],
        [Candidate("second", "fallback")],
    ) == "second"
    prompt = json.loads(arguments.read_text())[1]
    assert "fallback" in prompt and "failed" in prompt


class _TtyInput(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_terminal_human_batch_approval_uses_exact_session_key() -> None:
    output = io.StringIO()
    human = TerminalHuman(_TtyInput("a\nn\nchanged action\n"), output)
    first = human.ask("approve?", {"node": "root.danger", "execution": "rm -rf scratch"})
    same = human.ask("approve?", {"node": "root.danger", "execution": "rm -rf scratch"})
    changed = human.ask("approve?", {"node": "root.danger", "execution": "rm -rf other"})
    assert first.batch_approval is True and first.batch_reused is False
    assert same.approve is True and same.batch_reused is True
    assert changed.approve is False and changed.feedback == "changed action"
    assert output.getvalue().count("批准？") == 2


def test_terminal_human_preserves_rejection_feedback_verbatim() -> None:
    output = io.StringIO()
    human = TerminalHuman(_TtyInput(" N \n   \n  Mixed Case Feedback  \n"), output)
    answer = human.ask("approve?", {})
    assert answer.approve is False
    assert answer.feedback == "  Mixed Case Feedback  "
    assert output.getvalue().count("否决必须填写反馈：") == 1


def test_terminal_human_rejects_noninteractive_input() -> None:
    human = TerminalHuman(io.StringIO(), io.StringIO())
    with pytest.raises(PortError) as error:
        human.ask("approve?", {"node": "root", "execution": "command"})
    assert error.value.kind == "refused"
