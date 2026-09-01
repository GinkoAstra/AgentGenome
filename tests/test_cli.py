from __future__ import annotations

from pathlib import Path

import cli
import pytest
from adapters import LocalShell
from core import Answer, Ports


def test_init_check_run_and_status_smoke(tmp_path: Path, monkeypatch, capsys) -> None:
    package = tmp_path / "package"
    assert cli.main(["init", str(package)]) == 0
    assert cli.main(["check", str(package)]) == 0
    monkeypatch.chdir(tmp_path)
    assert cli.main(["run", str(package)]) == 0
    run_id = next((tmp_path / "runs").iterdir()).name
    assert cli.main(["status", run_id]) == 0
    assert "succeeded" in capsys.readouterr().out


def test_param_key_set_must_match_exactly(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    params = tmp_path / "params.yaml"
    params.write_text("unexpected: 1\n", encoding="utf-8")
    package = Path(__file__).parents[1] / "templates" / "data-cleaning"
    assert cli.main(["run", str(package), "--params", str(params)]) == 3


def test_init_never_overwrites_graph(tmp_path: Path) -> None:
    package = tmp_path / "package"
    assert cli.main(["init", str(package)]) == 0
    assert cli.main(["init", str(package)]) == 3


def test_run_handshake_failure_creates_no_run_directory(tmp_path: Path, monkeypatch, capsys) -> None:
    package = tmp_path / "package"
    package.mkdir()
    (package / "graph.yaml").write_text(
        """version: "gt/1.0"
node:
  id: root
  needs: [shell, llm]
  do: {run: "echo hi > {artifact}"}
  output: {message: {type: path}}
  verify: [{run: "test -s {artifact}"}]
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    assert cli.main(["run", str(package)]) == 3
    assert "AI 配置文件不存在" in capsys.readouterr().err
    assert not (tmp_path / "runs").exists()


def test_cli_handshake_rejection_creates_no_run_directory(tmp_path: Path, monkeypatch, capsys) -> None:
    package = tmp_path / "package"
    package.mkdir()
    (package / "graph.yaml").write_text(
        """version: "gt/1.0"
node:
  id: root
  do: {llm: "write {artifact}"}
  output: {message: {type: path}}
  verify: [{run: "test -s {artifact}"}]
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "_ports_for", lambda graph: Ports(LocalShell()))
    monkeypatch.chdir(tmp_path)
    assert cli.main(["run", str(package)]) == 3
    assert "E_NEEDS_PORT_ABSENT" in capsys.readouterr().err
    assert not (tmp_path / "runs").exists()


def test_ai_config_errors_and_lazy_port_injection(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"; config = home / ".graphtree" / "config.yaml"
    monkeypatch.setattr(cli.Path, "home", lambda: home)
    package = tmp_path / "pkg"; package.mkdir()
    (package / "graph.yaml").write_text("""version: "gt/1.0"
node:
  id: root
  needs: [shell, llm]
  do: {llm: "write {artifact}"}
  output: {result: {type: path}}
  verify: [{run: "test -s {artifact}"}]
""")
    graph = cli.compile(package)
    with pytest.raises(ValueError, match="AI 配置文件不存在"): cli._ports_for(graph)
    config.parent.mkdir(parents=True)
    for body in ["{}", "ai: {backend: pi}", "ai: {backend: kimi, kimi: {timeout_s: 0}}"]:
        config.write_text(body)
        with pytest.raises(ValueError): cli._ports_for(graph)
    config.write_text("ai: {backend: kimi, kimi: {executable: fake, model: m, timeout_s: 2}}")
    ports = cli._ports_for(graph)
    assert ports.ai.executable == "fake" and ports.ai.model == "m"


@pytest.mark.parametrize(
    "body",
    [
        "[]",
        "ai: {}",
        "ai: {backend: pi}",
        "ai: {backend: kimi, unexpected: true}",
        "ai: {backend: kimi, kimi: {unexpected: true}}",
        "ai: {backend: kimi, kimi: {executable: ''}}",
        "ai: {backend: kimi, kimi: {model: 3}}",
        "ai: {backend: kimi, kimi: {timeout_s: false}}",
        "ai: [",
    ],
)
def test_cli_rejects_each_ai_config_error_before_run_dir(
    tmp_path: Path, monkeypatch, body: str
) -> None:
    home = tmp_path / "home"
    config = home / ".graphtree" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(body, encoding="utf-8")
    monkeypatch.setattr(cli.Path, "home", lambda: home)
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "graph.yaml").write_text(
        """version: "gt/1.0"
node:
  id: root
  do: {llm: "write {artifact}"}
  output: {result: {type: path}}
  verify: [{run: "test -s {artifact}"}]
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    assert cli.main(["run", str(package)]) == 3
    assert not (tmp_path / "runs").exists()


def test_cli_injects_kimi_port_only_when_llm_is_needed(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    config = home / ".graphtree" / "config.yaml"
    config.parent.mkdir(parents=True)
    fake = tmp_path / "fake-kimi"
    fake.write_text(
        """#!/usr/bin/env python3
import sys
from pathlib import Path
Path(sys.argv[2].removeprefix("write ")).write_text("artifact", encoding="utf-8")
print("done")
""",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    config.write_text(f"ai: {{backend: kimi, kimi: {{executable: {fake}}}}}", encoding="utf-8")
    monkeypatch.setattr(cli.Path, "home", lambda: home)
    llm_package = tmp_path / "llm"
    llm_package.mkdir()
    (llm_package / "graph.yaml").write_text(
        """version: "gt/1.0"
node:
  id: root
  do: {llm: "write {artifact}"}
  output: {result: {type: path}}
  verify: [{run: "test -s {artifact}"}]
""",
        encoding="utf-8",
    )
    shell_package = tmp_path / "shell"
    shell_package.mkdir()
    (shell_package / "graph.yaml").write_text(
        """version: "gt/1.0"
node:
  id: root
  do: {run: "echo shell > {artifact}"}
  output: {result: {type: path}}
  verify: [{run: "test -s {artifact}"}]
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    assert cli.main(["run", str(llm_package)]) == 0
    config.unlink()
    assert cli.main(["run", str(shell_package)]) == 0


def test_cli_reports_abandoned_status_and_exit_code(tmp_path: Path, monkeypatch, capsys) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "graph.yaml").write_text(
        """version: "gt/1.0"
node:
  id: root
  danger: true
  do: {run: "echo never > {artifact}"}
  output: {result: {type: path}}
  verify: [{run: "test -s {artifact}"}]
""",
        encoding="utf-8",
    )

    class DenyingHuman:
        def ask(self, question: str, evidence: dict[str, object]) -> Answer:
            return Answer(False, "not authorized")

        def capabilities(self) -> dict[str, object]:
            return {"interactive": True}

    monkeypatch.setattr(cli, "_ports_for", lambda graph: Ports(LocalShell(), human=DenyingHuman()))
    monkeypatch.chdir(tmp_path)
    assert cli.main(["run", str(package)]) == 2
    run_id = next((tmp_path / "runs").iterdir()).name
    assert cli.main(["status", run_id]) == 0
    assert "abandonment:" in capsys.readouterr().out
