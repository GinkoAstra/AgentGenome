from __future__ import annotations

import json
from pathlib import Path

import pytest

from node_runtime.control_plane.backends.base import BackendError
from node_runtime.control_plane.backends.cli_protocol import (
    CODEX_DISABLED_FEATURES,
    MCP_SERVER_NAME,
    ClaudeProtocol,
    CodexProtocol,
    QoderProtocol,
    make_protocol,
    mcp_tool_names,
    sanitized_tool_name,
)


def test_tool_names_are_sanitized_and_prefixed():
    assert sanitized_tool_name("node.submit_candidate") == "node_submit_candidate"
    assert mcp_tool_names(["node.echo"]) == ["mcp__agentgenome__node_echo"]


def test_codex_argv_binds_mcp_environment_and_resume(tmp_path):
    protocol = CodexProtocol(executable="codex", model="gpt-test")
    server = {
        "command": "python",
        "args": ["/tmp/bridge.py"],
        "env": {"AGENTGENOME_TOOL_CONTEXT": "/tmp/context.json"},
    }
    argv = protocol.argv(
        workspace=tmp_path, session_id="thread-1", resume=True,
        exposed=("node.publish_artifact", "read_file"),
        mcp_config_path=None, mcp_server=server, settings_path=None)

    assert argv[:3] == ["codex", "exec", "resume"]
    assert argv[3] == "thread-1"
    assert argv[-1] == "-"
    assert "--ignore-user-config" in argv
    assert "--ignore-rules" in argv
    for feature in CODEX_DISABLED_FEATURES:
        index = argv.index(feature)
        assert argv[index - 1] == "--disable"
    assert "-m" in argv and "gpt-test" in argv
    config = [argv[index + 1] for index, item in enumerate(argv[:-1])
              if item == "-c"]
    assert any('mcp_servers.agentgenome.command="python"' in item
               for item in config)
    assert any("mcp_servers.agentgenome.enabled_tools=" in item
               and "node.publish_artifact" in item for item in config)
    assert any("mcp_servers.agentgenome.env.AGENTGENOME_TOOL_CONTEXT="
               in item for item in config)


def test_codex_keeps_code_mode_host_enabled_for_mcp_execution(tmp_path):
    # Current Codex builds execute MCP calls through the code-mode host;
    # disabling it makes even file tools unavailable.
    assert "code_mode_host" not in CODEX_DISABLED_FEATURES
    argv = CodexProtocol(executable="codex").argv(
        workspace=tmp_path, session_id=None, resume=False, exposed=(),
        mcp_config_path=None, mcp_server=None, settings_path=None)
    disabled = [argv[index + 1] for index, item in enumerate(argv[:-1])
                if item == "--disable"]
    assert "code_mode_host" not in disabled


def test_codex_requires_native_thread_for_resume(tmp_path):
    protocol = CodexProtocol(executable="codex", model=None)
    with pytest.raises(BackendError) as caught:
        protocol.argv(workspace=tmp_path, session_id=None, resume=True,
                      exposed=(), mcp_config_path=None, mcp_server=None,
                      settings_path=None)
    assert caught.value.code == "capability_unsupported"


def test_qoder_empty_grants_still_use_strict_empty_mcp_config(tmp_path):
    protocol = QoderProtocol(executable="qodercli", model=None)
    document = json.loads(protocol.mcp_config(None))
    assert document == {"mcpServers": {}}
    argv = protocol.argv(
        workspace=tmp_path, session_id="session-1", resume=False,
        exposed=(), mcp_config_path=tmp_path / "empty.json",
        mcp_server=None, settings_path=None)
    assert "--strict-mcp-config" in argv
    assert str(tmp_path / "empty.json") in argv
    assert "--allowed-mcp-server-names" not in argv
    assert "--allowed-tools" not in argv
    assert "--tools" in argv and argv[argv.index("--tools") + 1] == ""
    assert "--setting-sources" in argv


def test_qoder_grants_use_sanitized_allowed_tools(tmp_path):
    protocol = QoderProtocol(executable="qodercli", model=None)
    argv = protocol.argv(
        workspace=tmp_path, session_id=None, resume=False,
        exposed=("node.echo",), mcp_config_path=tmp_path / "mcp.json",
        mcp_server={"command": "py", "args": ["bridge"]}, settings_path=None)
    index = [item for item in argv if item.startswith("mcp__agentgenome__")]
    assert index == ["mcp__agentgenome__node_echo"]
    assert "--allowed-mcp-server-names" in argv
    assert MCP_SERVER_NAME in argv


def test_claude_uses_sanitized_private_settings(tmp_path):
    protocol = ClaudeProtocol(executable="claude", model="claude-test")
    settings = tmp_path / "settings.json"
    argv = protocol.argv(
        workspace=tmp_path, session_id="session-2", resume=True,
        exposed=(), mcp_config_path=tmp_path / "empty.json",
        mcp_server=None, settings_path=settings)
    assert "--strict-mcp-config" in argv
    assert "--settings" in argv and str(settings) in argv
    assert "--resume" in argv and "session-2" in argv
    assert argv[-2:] == ["--model", "claude-test"]
    # Claude 2.1.143 rejects the Qoder/Codex short option with
    # "unknown option '-m'".
    assert "-m" not in argv
    assert "--tools" in argv and argv[argv.index("--tools") + 1] == ""


def test_codex_event_summary_extracts_thread_and_final_text(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text("\n".join(json.dumps(item) for item in [
        {"type": "thread.started", "thread_id": "thread-9"},
        {"type": "turn.started"},
        {"type": "item.completed",
         "item": {"type": "mcp_tool_call", "id": "call-1",
                  "server": "agentgenome", "tool": "node.echo",
                  "status": "completed",
                  "result": {"artifact": {"id": "sha256:abc"}}}},
        {"type": "item.completed",
         "item": {"type": "agent_message", "text": "all done"}},
        {"type": "turn.completed", "usage": {"input_tokens": 7}},
    ]) + "\n", encoding="utf-8")
    summary = CodexProtocol(executable="codex", model=None).summarize(path)
    assert summary["session_id"] == "thread-9"
    assert summary["final_text"] == "all done"
    assert summary["native_tool_starts"] == 1
    assert summary["native_tool_ends"] == 1
    assert summary["failed"] is False


def test_stream_json_summary_uses_native_session_and_tool_ids(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text("\n".join(json.dumps(item) for item in [
        {"type": "system", "subtype": "init", "session_id": "native-1",
         "tools": [], "mcp_servers": []},
        {"type": "assistant",
         "message": {"id": "m1", "role": "assistant",
                     "content": [{"type": "text", "text": "done"}]}},
        {"type": "assistant",
         "message": {"id": "m2", "role": "assistant",
                     "content": [{"type": "tool_use", "id": "tool-1",
                                  "name": "mcp__agentgenome__node_echo"}]}},
        {"type": "user",
         "message": {"role": "user",
                     "content": [{"type": "tool_result",
                                  "tool_use_id": "tool-1"}]}},
        {"type": "result", "is_error": False, "result": "done",
         "num_turns": 1},
    ]) + "\n", encoding="utf-8")
    summary = QoderProtocol(executable="qodercli", model=None).summarize(path)
    assert summary["session_id"] == "native-1"
    assert summary["final_text"] == "done"
    assert summary["native_tool_starts"] == 1
    assert summary["native_tool_ends"] == 1


def test_failed_turn_is_not_reported_as_success(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text(json.dumps({
        "type": "turn.failed", "error": "quota exhausted"}) + "\n",
        encoding="utf-8")
    summary = CodexProtocol(executable="codex", model=None).summarize(path)
    assert summary["failed"] is True
    assert "quota" in summary["error_message"]


def test_kimi_is_explicitly_not_implemented():
    with pytest.raises(BackendError) as caught:
        make_protocol("kimi", executable=None, model=None, config_dir=None)
    assert caught.value.code == "capability_unsupported"
    assert "not implemented" in str(caught.value)


def test_unknown_backend_is_rejected():
    with pytest.raises(BackendError) as caught:
        make_protocol("unknown", executable=None, model=None, config_dir=None)
    assert caught.value.code == "capability_unsupported"
