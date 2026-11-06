/**
 * Teacher Graph tools: a real MCP client that lives inside the Pi extension.
 *
 * The Teacher is an ordinary Pi session. This extension talks to the running
 * AgentGenome service over the MCP Streamable HTTP transport, performs the
 * standard initialize handshake, and registers every advertised tool with its
 * exact name and input schema under the Pi tool protocol. No helper process,
 * shell command, direct database access or second state path is involved.
 *
 * Deliberate limits:
 * - One HTTP MCP session is reused for the whole activation; it is reopened
 *   only after a transport failure and a failed call is never replayed.
 * - A timeout abandons the session without assuming whether a write was
 *   committed. The API's request_id idempotency decides a later retry.
 * - Tool arguments are forwarded verbatim, except that `definition_path` is
 *   resolved by the Teacher against its own local workspace and uploaded as
 *   `definition_text`; the service never reads a caller-supplied path.
 */
import {
  readFileSync, realpathSync, statSync,
} from "node:fs";
import { isAbsolute, join, relative } from "node:path";

import { parseSseMessages, requestText } from "./service_http.mjs";

const CONFIG_ENV = "AGENTGENOME_TEACHER_MCP";
const CONFIG_FORMAT = "agentgenome-teacher-mcp/2";
const PROTOCOL_VERSION = "2025-11-25";
const CLIENT_NAME = "agentgenome-pi-teacher";
const DEFAULT_TIMEOUT_MS = 60000;
const MAX_TIMEOUT_MS = 300000;
const MAX_RESULT_CHARS = 200000;
const MAX_DEFINITION_BYTES = 8 * 1024 * 1024;
const MCP_PATH = "/mcp";
const DEFINITION_TOOLS = new Set(["validate_definition", "publish_definition"]);

function fail(code, message, details) {
  return Object.assign(new Error(message), { code, details });
}

export class ToolNameConflictError extends Error {
  constructor(names) {
    super(`tool name already registered by another extension: ${names.join(", ")}`);
    this.name = "ToolNameConflictError";
    this.code = "tool_name_conflict";
    this.names = names;
  }
}

function errorResult(code, message, details) {
  return {
    content: [{ type: "text", text: JSON.stringify({
      ok: false, code, message, details: details ?? null,
    }) }],
    isError: true,
  };
}

function normalizeConfig(config) {
  if (!config || config.format !== CONFIG_FORMAT) return null;
  if (typeof config.endpoint !== "string" || config.endpoint.length === 0) return null;
  if (typeof config.token !== "string" || config.token.length === 0) return null;
  if (config.workspace !== undefined && typeof config.workspace !== "string") {
    return null;
  }
  const timeout = Number(config.timeout_ms ?? DEFAULT_TIMEOUT_MS);
  return {
    ...config,
    timeout_ms: Number.isFinite(timeout) && timeout > 0
      ? Math.min(timeout, MAX_TIMEOUT_MS) : DEFAULT_TIMEOUT_MS,
  };
}

function loadConfig() {
  const raw = process.env[CONFIG_ENV];
  if (!raw) return null;
  // The bearer credential must not stay in the process environment once the
  // extension owns it in memory.
  delete process.env[CONFIG_ENV];
  let config;
  try {
    config = JSON.parse(raw);
  } catch {
    return null;
  }
  return normalizeConfig(config);
}

function textContent(result) {
  return (result?.content ?? [])
    .filter((part) => part && part.type === "text" && typeof part.text === "string")
    .map((part) => part.text)
    .join("\n");
}

function parseJson(text) {
  if (!text) return null;
  try {
    const value = JSON.parse(text);
    return value && typeof value === "object" ? value : null;
  } catch {
    return null;
  }
}

function modelText(payload) {
  const body = JSON.stringify(payload);
  if (body.length <= MAX_RESULT_CHARS) {
    return { text: body, truncated: false };
  }
  return {
    text: JSON.stringify({
      ok: payload.ok ?? true,
      operation: payload.operation,
      truncated: true,
      preview: body.slice(0, MAX_RESULT_CHARS),
      note: "result truncated; read the smallest resource or event page you need",
    }),
    truncated: true,
  };
}

function adaptResult(result) {
  if (result?.isError) {
    return errorResult("mcp_tool_error",
                       textContent(result) || "MCP tool reported an error");
  }
  const payload = result?.structuredContent ?? parseJson(textContent(result));
  if (payload && payload.ok === false) {
    const error = payload.error ?? {};
    return errorResult(error.code ?? "tool_failed",
                       error.message ?? "MCP tool failed", error.details);
  }
  if (!payload) {
    return {
      content: [{ type: "text", text: textContent(result).slice(0, MAX_RESULT_CHARS) }],
      details: undefined,
    };
  }
  const body = payload.ok === true
    ? { ok: true, operation: payload.operation, result: payload.result }
    : payload;
  const rendered = modelText(body);
  return {
    content: [{ type: "text", text: rendered.text }],
    details: {
      ...(body.result && typeof body.result === "object" ? body.result : {}),
      operation: body.operation,
      truncated: rendered.truncated,
    },
  };
}

class McpSession {
  constructor(config) {
    this.config = config;
    this.generation = 0;
    this.sessionId = null;
    this.protocolVersion = null;
    this.ready = false;
    this.connecting = null;
    this.nextId = 1;
  }

  get url() {
    return this.config.endpoint.replace(/\/+$/, "") + MCP_PATH;
  }

  async ensure() {
    if (this.ready) return;
    if (this.connecting === null) {
      const generation = this.generation;
      let connecting;
      connecting = this.connect(generation).finally(() => {
        if (this.connecting === connecting) this.connecting = null;
      });
      this.connecting = connecting;
    }
    await this.connecting;
  }

  async connect(generation) {
    this.ready = false;
    this.sessionId = null;
    this.protocolVersion = null;
    try {
      const result = await this.rpc("initialize", {
        protocolVersion: PROTOCOL_VERSION,
        capabilities: {},
        clientInfo: { name: CLIENT_NAME, version: "1" },
      }, generation);
      if (generation !== this.generation) {
        throw fail("not_active",
                   "AgentGenome is not active in this session; run /agentgenome first");
      }
      if (!result || typeof result.protocolVersion !== "string") {
        throw fail("mcp_protocol_error",
                   "MCP initialize returned no protocolVersion");
      }
      this.protocolVersion = result.protocolVersion;
      await this.notify("notifications/initialized", {}, generation);
      if (generation !== this.generation) {
        throw fail("not_active",
                   "AgentGenome is not active in this session; run /agentgenome first");
      }
      this.ready = true;
    } catch (error) {
      if (generation === this.generation) this.reset();
      throw error;
    }
  }

  headers() {
    const headers = {
      Authorization: `Bearer ${this.config.token}`,
      "Content-Type": "application/json",
      Accept: "application/json, text/event-stream",
    };
    if (this.sessionId) headers["Mcp-Session-Id"] = this.sessionId;
    if (this.protocolVersion) {
      headers["MCP-Protocol-Version"] = this.protocolVersion;
    }
    return headers;
  }

  async post(payload, expectedId, generation = this.generation) {
    let response;
    try {
      response = await requestText(this.url, {
        method: "POST",
        headers: this.headers(),
        body: JSON.stringify(payload),
        timeoutMs: this.config.timeout_ms,
      });
    } catch (error) {
      if (generation !== this.generation) {
        throw fail("not_active",
                   "AgentGenome is not active in this session; run /agentgenome first");
      }
      this.reset();
      if (error?.code === "timeout") {
        throw fail(
          "mcp_timeout",
          `MCP request did not answer within ${this.config.timeout_ms}ms; the `
          + "request may still have committed, so retry an operation with its "
          + "original request_id");
      }
      throw fail("mcp_unavailable",
                 `MCP service could not be reached: ${String(error?.message ?? error)}`);
    }
    if (generation !== this.generation) {
      throw fail("not_active",
                 "AgentGenome is not active in this session; run /agentgenome first");
    }
    const session = response.headers["mcp-session-id"];
    if (typeof session === "string" && session) this.sessionId = session;
    if (response.status === 401 || response.status === 403) {
      this.reset();
      throw fail("mcp_unauthorized",
                 "the AgentGenome service rejected this Teacher credential");
    }
    if (response.status === 404 && this.sessionId) {
      this.reset();
      throw fail("mcp_session_expired",
                 "the MCP session expired; the next call reconnects");
    }
    if (response.status < 200 || response.status >= 300) {
      this.reset();
      const body = response.body ?? "";
      throw fail("mcp_http_error",
                 `MCP service returned HTTP ${response.status}`
                 + (body ? `: ${body.slice(0, 500)}` : ""),
                 { status: response.status });
    }
    if (expectedId === null) return null;
    const contentType = String(response.headers["content-type"] ?? "");
    if (contentType.includes("text/event-stream")) {
      const message = parseSseMessages(response.body)
        .find((item) => item && item.id === expectedId) ?? null;
      if (message === null) {
        throw fail("mcp_protocol_error",
                   "MCP event stream ended without a response");
      }
      return message;
    }
    if (!String(response.body ?? "").trim()) {
      throw fail("mcp_protocol_error", "MCP service returned an empty response");
    }
    let message;
    try {
      message = JSON.parse(response.body);
    } catch {
      throw fail("mcp_protocol_error",
                 "MCP service returned non-JSON content");
    }
    if (Array.isArray(message)) {
      return message.find((item) => item && item.id === expectedId) ?? null;
    }
    return message;
  }

  async rpc(method, params, generation = this.generation) {
    const id = this.nextId++;
    let message;
    try {
      message = await this.post(
        { jsonrpc: "2.0", id, method, params: params ?? {} }, id, generation);
    } catch (error) {
      if (error?.code === "mcp_session_expired"
          && generation === this.generation) {
        this.ready = false;
      }
      throw error;
    }
    if (!message) {
      this.reset();
      throw fail("mcp_protocol_error", `${method} returned no response`);
    }
    if (message.error) {
      throw fail("mcp_rpc_error",
                 String(message.error.message ?? "MCP request failed"),
                 { rpc_code: message.error.code });
    }
    return message.result;
  }

  async notify(method, params, generation = this.generation) {
    await this.post(
      { jsonrpc: "2.0", method, params: params ?? {} }, null, generation);
  }

  async listTools(generation = this.generation) {
    const tools = [];
    let cursor;
    for (;;) {
      const result = await this.rpc(
        "tools/list", cursor ? { cursor } : {}, generation);
      if (Array.isArray(result?.tools)) tools.push(...result.tools);
      cursor = result?.nextCursor;
      if (!cursor) return tools;
    }
  }

  async callTool(name, arguments_, generation = this.generation) {
    return this.rpc(
      "tools/call", { name, arguments: arguments_ }, generation);
  }

  reset() {
    this.ready = false;
    this.sessionId = null;
    this.protocolVersion = null;
  }

  close() {
    this.generation += 1;
    const sessionId = this.sessionId;
    this.reset();
    this.connecting = null;
    if (!sessionId) return;
    requestText(this.url, {
      method: "DELETE",
      headers: {
        Authorization: `Bearer ${this.config.token}`,
        "Mcp-Session-Id": sessionId,
      },
      timeoutMs: 1000,
    }).catch(() => {});
  }
}

function workspaceText(workspace, requested) {
  if (typeof workspace !== "string" || workspace.length === 0) {
    throw fail("workspace_unconfigured",
               "definition_path needs the local Teacher workspace");
  }
  let root;
  try {
    root = realpathSync(workspace);
  } catch {
    throw fail("path_not_found", "the Teacher workspace does not exist");
  }
  let resolved;
  try {
    resolved = realpathSync(isAbsolute(requested) ? requested : join(root, requested));
  } catch {
    throw fail("path_not_found",
               "definition_path cannot be resolved in the Teacher workspace");
  }
  const inside = relative(root, resolved);
  if (inside.startsWith("..") || isAbsolute(inside)) {
    throw fail("path_outside_workspace",
               "definition_path must stay inside the Teacher workspace");
  }
  let info;
  try {
    info = statSync(resolved);
  } catch {
    throw fail("path_not_found", "definition_path cannot be read");
  }
  if (!info.isFile()) {
    throw fail("path_not_file", "definition_path must name a file");
  }
  if (info.size > MAX_DEFINITION_BYTES) {
    throw fail("material_too_large",
               "definition_path exceeds the definition size limit");
  }
  let bytes;
  try {
    bytes = readFileSync(resolved);
  } catch {
    throw fail("path_not_found", "definition_path cannot be read");
  }
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    throw fail("invalid_argument",
               "definition_path is not readable UTF-8 text");
  }
}

function advertisedSchema(tool) {
  // Pi advertises the local file parameter, but only this extension reads the
  // file; adaptArguments replaces it with definition_text before the network
  // MCP call. The public MCP schema therefore stays content-only.
  const schema = tool.inputSchema && typeof tool.inputSchema === "object"
    ? structuredClone(tool.inputSchema) : { type: "object", properties: {} };
  if (!DEFINITION_TOOLS.has(tool.name)) return schema;
  if (!schema.properties || typeof schema.properties !== "object") {
    schema.properties = {};
  }
  if (!schema.properties.definition_path) {
    schema.properties.definition_path = {
      type: "string",
      title: "Definition Path",
      description:
        "Local path relative to the Teacher workspace. The extension reads "
        + "the file and uploads its text; never combine with document or "
        + "definition_text.",
    };
  }
  return schema;
}

function adaptArguments(toolName, params, workspace) {
  const arguments_ = { ...(params ?? {}) };
  if (!DEFINITION_TOOLS.has(toolName)
      || arguments_.definition_path === undefined
      || arguments_.definition_path === null) {
    return arguments_;
  }
  const path = arguments_.definition_path;
  const text = arguments_.definition_text;
  const sources = [
    arguments_.document !== undefined && arguments_.document !== null,
    typeof text === "string" && text.length > 0,
    true,
  ];
  if (sources.filter(Boolean).length !== 1) {
    throw fail("invalid_argument",
               "pass exactly one of document, definition_text or definition_path");
  }
  if (typeof path !== "string" || path.length === 0) {
    throw fail("invalid_argument",
               "definition_path must be a nonempty string");
  }
  delete arguments_.definition_path;
  arguments_.definition_text = workspaceText(workspace, path);
  return arguments_;
}

function existingToolNames(pi) {
  if (typeof pi.getAllTools !== "function") {
    throw fail(
      "tool_registry_unavailable",
      "Pi does not expose getAllTools(), so tool-name conflicts cannot be ruled out");
  }
  const names = new Set();
  for (const tool of pi.getAllTools() ?? []) {
    if (tool && typeof tool.name === "string") names.add(tool.name);
  }
  return names;
}

function assertNoConflicts(pi, names) {
  const existing = existingToolNames(pi);
  const conflicts = names.filter((name) => existing.has(name));
  if (conflicts.length > 0) throw new ToolNameConflictError(conflicts);
}

/**
 * Register every MCP-advertised tool under the Pi tool protocol.
 *
 * `enabled` is evaluated on every execute; while it returns false the tool
 * refuses without touching the MCP session. `sessionShutdown` defaults to
 * true so the default environment-driven path keeps its original lifecycle.
 * `onResult` observes a successful committed result (for example the run this
 * Pi just created); it never changes or delays what the model receives.
 * Returns the registered names and an idempotent close function.
 */
export async function registerTeacherTools(pi, config, {
  enabled = () => true,
  sessionShutdown = true,
  checkConflicts = true,
  onResult = null,
} = {}) {
  const normalized = normalizeConfig(config);
  if (normalized === null) {
    throw fail("invalid_config", "AgentGenome teacher MCP config is invalid");
  }

  const session = new McpSession(normalized);
  let offered;
  try {
    await session.ensure();
    offered = await session.listTools();
  } catch (error) {
    session.close();
    throw error;
  }

  const tools = [];
  const seen = new Set();
  for (const tool of offered) {
    if (!tool || typeof tool.name !== "string" || seen.has(tool.name)) continue;
    seen.add(tool.name);
    tools.push(tool);
  }
  // Refuse before registering anything: a silent overwrite could shadow a
  // native or third-party tool of the same name. The load-time teacher path
  // is an isolated profile where the registry is not yet bound; it opts out.
  if (checkConflicts) {
    assertNoConflicts(pi, tools.map((tool) => tool.name));
  }

  const close = () => session.close();

  try {
    for (const tool of tools) {
      const parameters = advertisedSchema(tool);
      pi.registerTool({
        name: tool.name,
        label: tool.name,
        description: typeof tool.description === "string" && tool.description
          ? tool.description : tool.name,
        parameters,
        execute: async (callId, params) => {
          if (!enabled()) {
            return errorResult(
              "not_active",
              "AgentGenome is not active in this session; run /agentgenome first",
              { tool: tool.name, call_id: String(callId) });
          }
          const generation = session.generation;
          try {
            await session.ensure();
            if (generation !== session.generation || !enabled()) {
              return errorResult(
                "not_active",
                "AgentGenome is not active in this session; run /agentgenome first",
                { tool: tool.name, call_id: String(callId) });
            }
            const result = await session.callTool(
              tool.name, adaptArguments(tool.name, params, normalized.workspace),
              generation);
            if (generation !== session.generation) {
              return errorResult(
                "not_active",
                "AgentGenome is not active in this session; run /agentgenome first",
                { tool: tool.name, call_id: String(callId) });
            }
            const adapted = adaptResult(result);
            adapted.details = {
              ...(adapted.details && typeof adapted.details === "object"
                ? adapted.details : {}),
              tool: tool.name,
              call_id: String(callId),
            };
            if (adapted.isError !== true && typeof onResult === "function") {
              try {
                onResult({
                  tool: tool.name,
                  call_id: String(callId),
                  result: adapted.details,
                });
              } catch {
                // An observer failure must not alter a committed result.
              }
            }
            return adapted;
          } catch (error) {
            return errorResult(
              error?.code ?? "mcp_unavailable",
              String(error?.message ?? error),
              { tool: tool.name, call_id: String(callId) });
          }
        },
      });
    }
  } catch (error) {
    close();
    throw error;
  }

  if (sessionShutdown) {
    pi.on("session_shutdown", close);
  }
  return { toolNames: tools.map((tool) => tool.name), close };
}

export default async function teacherGraphTools(pi) {
  const config = loadConfig();
  if (config === null) return;
  try {
    // The installed Teacher profile uses --no-extensions and an explicit
    // extension path, so no foreign tool can collide; keep the original
    // load-time behavior instead of requiring a bound runtime.
    await registerTeacherTools(pi, config, { checkConflicts: false });
  } catch (error) {
    process.stderr.write(
      `agentgenome teacher MCP unavailable: ${String(error?.message ?? error)}\n`);
  }
}
