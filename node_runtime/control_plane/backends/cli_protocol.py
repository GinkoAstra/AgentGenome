"""Native launch and event formats for externally installed Agent CLIs.

Only the differences between CLIs live here: command-line isolation, how the
temporary MCP server is advertised, and how each JSONL event stream is read.
The Backend owns one lifecycle for all of them.

Nothing in this module calls a model. A protocol builds an argv and parses
evidence the CLI actually wrote.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any, Iterable, Mapping

from .base import BackendError

MCP_SERVER_NAME = "agentgenome"
MCP_TOOL_PREFIX = f"mcp__{MCP_SERVER_NAME}__"
WORKER_MCP_MODULE = Path(__file__).with_name("worker_mcp.py")

# Codex features that expose native execution, delegation or background work.
CODEX_DISABLED_FEATURES = (
    "shell_tool", "multi_agent", "goals", "image_generation", "view_image",
    "skill_search", "skill_mcp_dependency_install", "hooks", "plugins", "apps",
    "computer_use", "browser_use", "browser_use_external",
    "browser_use_full_cdp_access", "auth_elicitation",
    "tool_call_mcp_elicitation", "tool_suggest", "sleep_tool",
    "unified_exec", "default_mode_request_user_input",
)
# code_mode_host must stay enabled on current Codex builds: it is the engine
# that executes MCP tool calls, and disabling it makes even file tools fail.
# Its tool surface is restricted by the per-run mcp_servers configuration.


def sanitized_tool_name(name: str) -> str:
    """The name Qoder/Claude derive for MCP tools (`node.echo` -> `node_echo`)."""
    return name.replace(".", "_")


def mcp_tool_names(names: Iterable[str]) -> list[str]:
    return [MCP_TOOL_PREFIX + sanitized_tool_name(name) for name in names]


def _executable_path(executable: str) -> str | None:
    if not isinstance(executable, str) or not executable:
        return None
    if os.sep in executable or (os.altsep and os.altsep in executable):
        path = Path(executable).expanduser()
        return str(path) if path.is_file() and os.access(path, os.X_OK) else None
    return shutil.which(executable)


class CliProtocol:
    """One installed CLI's command line and native event vocabulary."""

    name = ""
    executable_default = ""
    resume_supported = True

    def __init__(self, *, executable: str, model: str | None = None,
                 config_dir: str | Path | None = None) -> None:
        self.executable = executable
        self.model = model
        self.config_dir = Path(config_dir).resolve() if config_dir else None

    def check(self) -> None:
        if _executable_path(self.executable) is None:
            raise BackendError(
                "backend_unavailable",
                f"the {self.name} CLI executable was not found",
                executable=self.executable)
        if self.config_dir is not None and not self.config_dir.is_dir():
            raise BackendError(
                "backend_unavailable",
                f"the {self.name} configuration directory does not exist",
                config_dir=str(self.config_dir))

    def environment(self) -> dict[str, str]:
        return {}

    def requested_session(self) -> str | None:
        """A session id the first CLI invocation should adopt, if supported."""
        return None

    def mcp_config(self, server: Mapping[str, Any] | None) -> str | None:
        """A temporary MCP configuration document, or None when flags suffice."""
        return None

    def mcp_argv(self, config_path: Path | None, names: Iterable[str]) -> list[str]:
        return []

    def argv(self, *, workspace: Path, session_id: str | None, resume: bool,
             exposed: Iterable[str], mcp_config_path: Path | None,
             mcp_server: Mapping[str, Any] | None,
             settings_path: Path | None) -> list[str]:
        raise NotImplementedError

    # -- native event evidence -------------------------------------------
    def session_id(self, event: Mapping[str, Any]) -> str | None:
        value = event.get("session_id")
        return value if isinstance(value, str) and value else None

    def summarize(self, path: Path) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "session_id": None, "final_text": "", "error_message": None,
            "failed": False, "native_tool_starts": 0, "native_tool_ends": 0,
        }
        see_tool: set[str] = set()
        end_tool: set[str] = set()
        if not path.exists():
            return summary
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, Mapping):
                continue
            found = self.session_id(event)
            if found:
                summary["session_id"] = found
            self._summarize_event(event, summary, see_tool, end_tool)
        summary["native_tool_starts"] = len(see_tool)
        summary["native_tool_ends"] = len(end_tool)
        return summary

    def _summarize_event(self, event: Mapping[str, Any], summary: dict[str, Any],
                         see_tool: set[str], end_tool: set[str]) -> None:
        return None

    def entry(self, event: Mapping[str, Any], *, offset: int,
              line_bytes: int) -> dict[str, Any]:
        record: dict[str, Any] = {"kind": event.get("type"), "offset": offset,
                                  "line_bytes": line_bytes}
        return record


def _numeric_map(value: Any) -> tuple[dict[str, int | float], bool]:
    if not isinstance(value, Mapping):
        return {}, False
    numbers = {str(key): item for key, item in value.items()
               if isinstance(key, str)
               and isinstance(item, (int, float)) and not isinstance(item, bool)}
    return numbers, bool(numbers)


def _content_summary(message: Mapping[str, Any], *,
                     include_text: bool = False) -> dict[str, Any]:
    parts = message.get("content")
    text_parts = text_bytes = thinking_parts = 0
    tool_uses: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []
    if isinstance(parts, list):
        for part in parts:
            if not isinstance(part, Mapping):
                continue
            kind = part.get("type")
            if kind == "text" and isinstance(part.get("text"), str):
                text_parts += 1
                text_bytes += len(part["text"].encode("utf-8"))
            elif kind == "thinking":
                thinking_parts += 1
            elif kind == "tool_use":
                tool_uses.append({"call_id": part.get("id"),
                                  "tool": part.get("name")})
            elif kind == "tool_result":
                content = part.get("content")
                results = content if isinstance(content, list) else [content]
                body_bytes = 0
                for item in results:
                    if isinstance(item, Mapping) and isinstance(
                            item.get("text"), str):
                        body_bytes += len(item["text"].encode("utf-8"))
                tool_results.append({
                    "call_id": part.get("tool_use_id"),
                    "is_error": bool(part.get("is_error")),
                    "content_bytes": body_bytes,
                })
    return {"text_parts": text_parts, "text_bytes": text_bytes,
            "thinking_parts": thinking_parts,
            "tool_uses": tool_uses[:32], "tool_results": tool_results[:32]}


def _artifact_refs(value: Any, found: list[dict[str, Any]] | None = None,
                   depth: int = 0) -> list[dict[str, Any]]:
    found = found if found is not None else []
    if len(found) >= 20 or depth > 4:
        return found
    if isinstance(value, Mapping):
        identifier = value.get("id")
        if isinstance(identifier, str) and identifier.startswith("sha256:"):
            ref = {"id": identifier}
            for key in ("sha256", "size", "media_type"):
                item = value.get(key)
                if isinstance(item, (str, int)) and not isinstance(item, bool):
                    ref[key] = item
            if ref not in found:
                found.append(ref)
        for key in ("details", "result", "artifacts", "artifact", "output"):
            if key in value:
                _artifact_refs(value[key], found, depth + 1)
    elif isinstance(value, list):
        for item in value[:20]:
            _artifact_refs(item, found, depth + 1)
    elif isinstance(value, str) and len(value) <= 8192 \
            and value.lstrip().startswith(("{", "[")):
        try:
            _artifact_refs(json.loads(value), found, depth + 1)
        except ValueError:
            return found
    return found


class CodexProtocol(CliProtocol):
    name = "codex"
    executable_default = "codex"

    def environment(self) -> dict[str, str]:
        # CODEX_HOME is a read-only reference: --ignore-user-config below keeps
        # config.toml (hooks, MCP servers, profiles) out of this run while auth
        # still resolves from that directory.
        if self.config_dir is not None:
            return {"CODEX_HOME": str(self.config_dir)}
        return {}

    def requested_session(self) -> str | None:
        return None

    def argv(self, *, workspace: Path, session_id: str | None, resume: bool,
             exposed: Iterable[str], mcp_config_path: Path | None,
             mcp_server: Mapping[str, Any] | None,
             settings_path: Path | None) -> list[str]:
        names = list(exposed)
        command = [self.executable, "exec"]
        if resume:
            if not session_id:
                raise BackendError(
                    "capability_unsupported",
                    "Codex resume requires a native thread id")
            command += ["resume", session_id]
        command += [
            "--json", "--ignore-user-config", "--ignore-rules",
            "--skip-git-repo-check",
            "-c", 'approval_policy="never"',
            "-c", 'sandbox_mode="read-only"',
            "-c", 'web_search="disabled"',
            "-c", "tools.web_search=false",
            "-c", "project_doc_max_bytes=0",
        ]
        for feature in CODEX_DISABLED_FEATURES:
            command += ["--disable", feature]
        if self.model:
            command += ["-m", self.model]
        if mcp_server is not None:
            command += [
                "-c", "mcp_servers.agentgenome.command="
                      + json.dumps(mcp_server["command"]),
                "-c", "mcp_servers.agentgenome.args="
                      + json.dumps(list(mcp_server["args"])),
                "-c", 'mcp_servers.agentgenome.default_tools_approval_mode="approve"',
                "-c", "mcp_servers.agentgenome.enabled_tools="
                      + json.dumps(names),
            ]
            env = mcp_server.get("env")
            if isinstance(env, Mapping):
                for key, value in env.items():
                    command += [
                        "-c", f"mcp_servers.agentgenome.env.{key}="
                              + json.dumps(str(value))]
        command += ["-"]
        return command

    def session_id(self, event: Mapping[str, Any]) -> str | None:
        if event.get("type") == "thread.started":
            value = event.get("thread_id")
            return value if isinstance(value, str) and value else None
        return None

    def _summarize_event(self, event: Mapping[str, Any], summary: dict[str, Any],
                         see_tool: set[str], end_tool: set[str]) -> None:
        kind = event.get("type")
        if kind == "error":
            summary["failed"] = True
            summary["error_message"] = str(
                event.get("message") or event.get("error") or "error")[:500]
            return
        if kind == "turn.failed":
            summary["failed"] = True
            summary["error_message"] = str(
                event.get("error") or event.get("message") or "turn failed")[:500]
            return
        if kind != "item.completed":
            return
        item = event.get("item")
        if not isinstance(item, Mapping):
            return
        item_type = item.get("type")
        if item_type == "agent_message":
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                summary["final_text"] = text
        elif item_type == "error":
            summary["failed"] = True
            summary["error_message"] = str(
                item.get("message") or item.get("error") or "item error")[:500]
        elif item_type == "mcp_tool_call":
            call_id = str(item.get("id") or item.get("call_id") or "")
            if call_id:
                see_tool.add(call_id)
                if item.get("status") in ("completed", "failed") \
                        or item.get("result") is not None or item.get("error"):
                    end_tool.add(call_id)

    def entry(self, event: Mapping[str, Any], *, offset: int,
              line_bytes: int) -> dict[str, Any]:
        kind = event.get("type")
        record: dict[str, Any] = {"kind": kind, "offset": offset,
                                  "line_bytes": line_bytes}
        if kind in ("error", "turn.failed"):
            record["error"] = {"message": str(
                event.get("message") or event.get("error") or "error")[:500]}
            return record
        if kind == "thread.started":
            record["session_id"] = event.get("thread_id")
            return record
        if kind == "turn.completed":
            usage, available = _numeric_map(event.get("usage"))
            record["usage"] = usage if available else None
            record["usage_available"] = available
            return record
        if kind != "item.completed":
            return record
        item = event.get("item")
        if not isinstance(item, Mapping):
            return record
        item_type = item.get("type")
        record["item_type"] = item_type
        if item_type == "agent_message":
            text = item.get("text")
            if isinstance(text, str):
                record["text_parts"] = 1
                record["text_bytes"] = len(text.encode("utf-8"))
        elif item_type == "error":
            record["error"] = {"message": str(
                item.get("message") or item.get("error") or "item error")[:500]}
        elif item_type == "mcp_tool_call":
            record["call_id"] = item.get("id") or item.get("call_id")
            record["tool"] = item.get("tool") or item.get("name")
            record["server"] = item.get("server")
            record["status"] = item.get("status")
            record["is_error"] = bool(item.get("error"))
            if item.get("error"):
                record["error"] = {"message": str(item["error"])[:500]}
            else:
                record["artifact_refs"] = _artifact_refs(item.get("result"))
        return record


class StreamJsonProtocol(CliProtocol):
    """Qoder CLI and Claude Code share the stream-json event vocabulary."""

    def requested_session(self) -> str | None:
        import uuid
        return str(uuid.uuid4())

    def mcp_config(self, server: Mapping[str, Any] | None) -> str:
        servers = {} if server is None else {MCP_SERVER_NAME: dict(server)}
        return json.dumps({"mcpServers": servers})

    def session_id(self, event: Mapping[str, Any]) -> str | None:
        value = event.get("session_id")
        return value if isinstance(value, str) and value else None

    def _summarize_event(self, event: Mapping[str, Any], summary: dict[str, Any],
                         see_tool: set[str], end_tool: set[str]) -> None:
        kind = event.get("type")
        if kind == "assistant":
            message = event.get("message")
            if not isinstance(message, Mapping):
                return
            parts = message.get("content")
            if not isinstance(parts, list):
                return
            texts = []
            for part in parts:
                if not isinstance(part, Mapping):
                    continue
                if part.get("type") == "text" and isinstance(part.get("text"), str):
                    texts.append(part["text"])
                elif part.get("type") == "tool_use":
                    call_id = str(part.get("id") or "")
                    if call_id:
                        see_tool.add(call_id)
            if texts:
                summary["final_text"] = "".join(texts)
        elif kind == "user":
            message = event.get("message")
            if not isinstance(message, Mapping):
                return
            parts = message.get("content")
            if not isinstance(parts, list):
                return
            for part in parts:
                if isinstance(part, Mapping) and part.get("type") == "tool_result":
                    call_id = str(part.get("tool_use_id") or "")
                    if call_id:
                        end_tool.add(call_id)
        elif kind == "result":
            if event.get("is_error"):
                summary["failed"] = True
                summary["error_message"] = str(
                    event.get("result") or event.get("subtype") or "CLI error")[:500]
            elif isinstance(event.get("result"), str) and event["result"].strip():
                summary["final_text"] = event["result"]
        elif kind == "system" and event.get("subtype") not in (None, "init",
                                                               "hook_diagnostics",
                                                               "artifacts_update"):
            if event.get("subtype") in ("error", "api_error"):
                summary["failed"] = True
                summary["error_message"] = str(
                    event.get("error") or event.get("message")
                    or event.get("subtype"))[:500]

    def entry(self, event: Mapping[str, Any], *, offset: int,
              line_bytes: int) -> dict[str, Any]:
        kind = event.get("type")
        record: dict[str, Any] = {"kind": kind, "offset": offset,
                                  "line_bytes": line_bytes}
        if kind == "system":
            record["subtype"] = event.get("subtype")
            if isinstance(event.get("session_id"), str):
                record["session_id"] = event["session_id"]
            if event.get("subtype") == "init":
                tools = event.get("tools")
                servers = event.get("mcp_servers")
                plugins = event.get("plugins")
                record["model"] = event.get("model")
                record["permission_mode"] = event.get("permissionMode")
                record["tool_count"] = len(tools) if isinstance(tools, list) else None
                record["mcp_servers"] = [
                    item.get("name") if isinstance(item, Mapping) else str(item)
                    for item in servers[:16]] if isinstance(servers, list) else None
                record["plugin_count"] = (
                    len(plugins) if isinstance(plugins, list) else None)
            return record
        if kind in ("assistant", "user"):
            message = event.get("message")
            if not isinstance(message, Mapping):
                return record
            record["message_id"] = message.get("id")
            record["role"] = message.get("role")
            record["content"] = _content_summary(message)
            if "stop_reason" in message:
                record["stop_reason"] = message.get("stop_reason")
            usage, available = _numeric_map(message.get("usage"))
            record["usage"] = usage if available else None
            record["usage_available"] = available
            return record
        if kind == "result":
            record["subtype"] = event.get("subtype")
            record["is_error"] = bool(event.get("is_error"))
            record["num_turns"] = event.get("num_turns")
            duration = event.get("duration_ms")
            if isinstance(duration, (int, float)) and not isinstance(duration, bool):
                record["duration_ms"] = duration
            usage, available = _numeric_map(event.get("usage"))
            record["usage"] = usage if available else None
            record["usage_available"] = available
            if event.get("is_error"):
                record["error"] = {"message": str(
                    event.get("result") or event.get("subtype") or "CLI error")[:500]}
            return record
        return record


class QoderProtocol(StreamJsonProtocol):
    name = "qoder"
    executable_default = "qodercli"

    def mcp_argv(self, config_path: Path | None, names: Iterable[str]) -> list[str]:
        command = [
            "-p", "-o", "stream-json",
            "--tools", "",
            "--setting-sources", "",
            "--permission-mode", "dont_ask",
        ]
        if self.config_dir is not None:
            command += ["--config-dir", str(self.config_dir)]
        if config_path is not None:
            names = list(names)
            command += ["--strict-mcp-config",
                        "--mcp-config", str(config_path)]
            if names:
                command += ["--allowed-mcp-server-names", MCP_SERVER_NAME]
            for name in mcp_tool_names(names):
                command += ["--allowed-tools", name]
        return command

    def argv(self, *, workspace: Path, session_id: str | None, resume: bool,
             exposed: Iterable[str], mcp_config_path: Path | None,
             mcp_server: Mapping[str, Any] | None,
             settings_path: Path | None) -> list[str]:
        command = [self.executable] + self.mcp_argv(mcp_config_path, exposed)
        if resume:
            if not session_id:
                raise BackendError(
                    "capability_unsupported",
                    "Qoder resume requires a native session id")
            command += ["--resume", session_id]
        elif session_id:
            command += ["--session-id", session_id]
        if self.model:
            command += ["-m", self.model]
        return command


class ClaudeProtocol(StreamJsonProtocol):
    name = "claude"
    executable_default = "claude"

    def environment(self) -> dict[str, str]:
        if self.config_dir is not None:
            return {"CLAUDE_CONFIG_DIR": str(self.config_dir)}
        return {}

    def mcp_argv(self, config_path: Path | None, names: Iterable[str]) -> list[str]:
        command = [
            "-p", "--verbose", "--output-format", "stream-json",
            "--tools", "",
            "--setting-sources", "",
            "--permission-mode", "dontAsk",
            "--disable-slash-commands",
        ]
        if config_path is not None:
            command += ["--strict-mcp-config",
                        "--mcp-config", str(config_path)]
            allowed = mcp_tool_names(names)
            if allowed:
                command += ["--allowedTools", ",".join(allowed)]
        return command

    def argv(self, *, workspace: Path, session_id: str | None, resume: bool,
             exposed: Iterable[str], mcp_config_path: Path | None,
             mcp_server: Mapping[str, Any] | None,
             settings_path: Path | None) -> list[str]:
        command = [self.executable] + self.mcp_argv(mcp_config_path, exposed)
        if settings_path is not None:
            command += ["--settings", str(settings_path)]
        if resume:
            if not session_id:
                raise BackendError(
                    "capability_unsupported",
                    "Claude resume requires a native session id")
            command += ["--resume", session_id]
        elif session_id:
            command += ["--session-id", session_id]
        if self.model:
            command += ["--model", self.model]
        return command


class KimiProtocol(CliProtocol):
    name = "kimi"
    executable_default = "kimi"
    resume_supported = False

    def __init__(self, **_: Any) -> None:
        raise BackendError(
            "capability_unsupported",
            "the Kimi CLI adapter is not implemented: the installed protocol "
            "has not been verified (MoonshotAI/kimi-code differs from the old "
            "Python kimi-cli)")


PROTOCOLS = {
    "codex": CodexProtocol,
    "qoder": QoderProtocol,
    "claude": ClaudeProtocol,
    "kimi": KimiProtocol,
}


def make_protocol(backend: str, *, executable: str | None,
                  model: str | None, config_dir: str | Path | None) -> CliProtocol:
    factory = PROTOCOLS.get(backend)
    if factory is None:
        raise BackendError("capability_unsupported", "unknown CLI backend",
                           backend=backend)
    if factory is KimiProtocol:
        return KimiProtocol()
    resolved = executable or factory.executable_default
    return factory(executable=resolved, model=model, config_dir=config_dir)
