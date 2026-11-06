/**
 * Command-level session-run probe.
 *
 * Imports the real `/agentgenome` extension and the real Teacher MCP bridge,
 * activates it with a fake `pi_integration prepare` result, and points both the
 * MCP bridge and the trace client at one in-process fake service. It asserts
 * that a successful create_run made by this Pi becomes the default collapsed
 * selection even when another run is newer, that failed or unrelated tools
 * never claim a run, and that off / session reset clear the ownership.
 *
 * No Python service is started, no model is called and no real run is created.
 */
import { createServer } from "node:http";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const modulePath = process.env.AGENTGENOME_PI_COMMAND_MODULE;
const tuiEntry = process.env.AGENTGENOME_PI_TUI_ENTRY;
if (!modulePath || !tuiEntry) {
  console.error("missing AGENTGENOME_PI_COMMAND_MODULE or AGENTGENOME_PI_TUI_ENTRY");
  process.exit(2);
}

const { default: createExtension } = await import(
  pathToFileURL(modulePath).href);
const { truncateToWidth, visibleWidth, wrapTextWithAnsi } = await import(
  pathToFileURL(tuiEntry).href);
const helpers = { truncateToWidth, visibleWidth, wrapTextWithAnsi };

const checks = [];
function check(name, condition, detail) {
  if (!condition) throw new Error(`${name}${detail ? `: ${detail}` : ""}`);
  checks.push(name);
}
function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
async function waitFor(predicate, what, timeout = 8000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await predicate()) return;
    await sleep(10);
  }
  throw new Error(`timed out waiting for ${what}`);
}

const TOKEN = "session-probe-token";
const FOREIGN = {
  run_id: "run-foreign", status: "running",
  created_at: "2026-11-04T12:00:00Z", updated_at: "2026-11-04T12:00:00Z",
};
const OWNED_CREATED = "2026-11-04T10:00:00Z";

const state = {
  runs: [{ ...FOREIGN }],
  mcpSessions: 0,
  calls: [],
  listRuns: 0,
};

function sendJson(res, status, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(status, {
    "Content-Type": "application/json",
    "Content-Length": Buffer.byteLength(body),
  });
  res.end(body);
}

function envelope(result, operation) {
  return { ok: true, operation, result };
}

function mcpToolCall(id, name, args) {
  state.calls.push({ name, args });
  if (name === "create_run") {
    if (args?.fail === true) {
      return {
        jsonrpc: "2.0", id, result: {
          content: [{ type: "text", text: JSON.stringify({
            ok: false, operation: "create_run",
            error: { code: "run_rejected", message: "rejected by policy" },
          }) }],
          isError: false,
        },
      };
    }
    const runId = String(args?.run_id ?? "run-owned");
    if (!state.runs.some((run) => run.run_id === runId)) {
      // Deliberately older than the foreign run: session ownership must not be
      // inferred from created_at.
      state.runs.push({
        run_id: runId, status: "running",
        created_at: OWNED_CREATED, updated_at: OWNED_CREATED,
      });
    }
    const payload = {
      ok: true, operation: "create_run",
      result: { run_id: runId, status: "running", revision: 1 },
    };
    return {
      jsonrpc: "2.0", id, result: {
        content: [{ type: "text", text: JSON.stringify(payload) }],
        structuredContent: payload,
      },
    };
  }
  const payload = {
    ok: true, operation: name,
    result: { valid: true, definition_format: "agentgenome-graph-definition/2" },
  };
  return {
    jsonrpc: "2.0", id, result: {
      content: [{ type: "text", text: JSON.stringify(payload) }],
      structuredContent: payload,
    },
  };
}

function handleMcp(req, res, body) {
  if (req.method === "DELETE") {
    res.writeHead(204);
    res.end();
    return;
  }
  if (req.method !== "POST") {
    sendJson(res, 405, { error: "method" });
    return;
  }
  if (req.headers.authorization !== `Bearer ${TOKEN}`) {
    sendJson(res, 401, { error: "unauthorized" });
    return;
  }
  let message;
  try {
    message = JSON.parse(body || "{}");
  } catch {
    sendJson(res, 400, { error: "bad json" });
    return;
  }
  if (message.method === "initialize") {
    state.mcpSessions += 1;
    const body = JSON.stringify({
      jsonrpc: "2.0", id: message.id, result: {
        protocolVersion: "2025-11-25", capabilities: {},
        serverInfo: { name: "session-probe", version: "1" },
      },
    });
    res.writeHead(200, {
      "Content-Type": "application/json",
      "Content-Length": Buffer.byteLength(body),
      "Mcp-Session-Id": `session-${state.mcpSessions}`,
    });
    res.end(body);
    return;
  }
  if (message.method === "notifications/initialized") {
    res.writeHead(202);
    res.end();
    return;
  }
  if (message.method === "tools/list") {
    sendJson(res, 200, {
      jsonrpc: "2.0", id: message.id, result: { tools: [{
        name: "create_run", description: "create a run",
        inputSchema: { type: "object", properties: {
          run_id: { type: "string" }, fail: { type: "boolean" },
        } },
      }, {
        name: "validate_definition", description: "validate",
        inputSchema: { type: "object", properties: {
          definition_path: { type: "string" },
        } },
      }] },
    });
    return;
  }
  if (message.method === "tools/call") {
    sendJson(res, 200, mcpToolCall(message.id, message.params?.name,
                                   message.params?.arguments ?? {}));
    return;
  }
  sendJson(res, 200, {
    jsonrpc: "2.0", id: message.id,
    error: { code: -32601, message: "method not found" },
  });
}

async function handle(req, res) {
  let body = "";
  req.on("data", (chunk) => { body += chunk; });
  await new Promise((resolve) => req.on("end", resolve));
  const url = new URL(req.url, "http://127.0.0.1");
  if (url.pathname === "/mcp") {
    handleMcp(req, res, body);
    return;
  }
  if (req.headers.authorization !== `Bearer ${TOKEN}`) {
    sendJson(res, 401, { ok: false, error: { code: "unauthorized",
                                             message: "bad token" } });
    return;
  }
  if (req.method === "POST" && url.pathname === "/v1/operations/list_runs") {
    state.listRuns += 1;
    sendJson(res, 200, envelope({ runs: state.runs.map((run) => ({ ...run })) },
                                "list_runs"));
    return;
  }
  sendJson(res, 404, { ok: false, error: { code: "not_found",
                                           message: url.pathname } });
}

const KEY = {
  "tui.select.up": "\u001b[A",
  "tui.select.down": "\u001b[B",
  "tui.select.pageUp": "\u001b[5~",
  "tui.select.pageDown": "\u001b[6~",
  "tui.select.confirm": "\r",
  "tui.select.cancel": "\u001b",
};

function makeHandle() {
  return {
    focused: false,
    focus() { this.focused = true; },
    unfocus() { this.focused = false; },
    isFocused() { return this.focused; },
    setHidden() {},
    isHidden() { return false; },
    hide() {},
  };
}

function host() {
  const notices = [];
  const overlays = [];
  const tui = { requestRender() {} };
  const theme = {
    fg(_token, text) { return text; },
    bg(_token, text) { return text; },
    style(_text, _options) { return ""; },
  };
  const keybindings = { matches(data, name) { return KEY[name] === data; } };
  const ui = {
    notify(message, type = "info") {
      notices.push({ message: String(message), type });
    },
    custom(factory, options) {
      const entry = { options, handle: makeHandle(), component: null,
                      doneCalled: false };
      entry.component = factory(tui, theme, keybindings, () => {
        entry.doneCalled = true;
      });
      options.onHandle?.(entry.handle);
      overlays.push(entry);
      return new Promise(() => {});
    },
  };
  return { ui, notices, overlays };
}

function makePi(prepared) {
  const commands = new Map();
  const handlers = new Map();
  const tools = new Map();
  let active = ["read", "bash"];
  const pi = {
    registerCommand(name, options) { commands.set(name, options); },
    registerTool(tool) {
      tools.set(tool.name, tool);
      active = [...new Set([...active, tool.name])];
    },
    on(event, handler) {
      const list = handlers.get(event) ?? [];
      list.push(handler);
      handlers.set(event, list);
    },
    getAllTools() {
      return [...tools.values()].map((tool) => ({
        name: tool.name, description: tool.description,
        parameters: tool.parameters, sourceInfo: { path: "<agentgenome>" },
      }));
    },
    getActiveTools() { return [...active]; },
    setActiveTools(names) { active = [...new Set(names)]; },
    async exec() {
      return { code: 0, stdout: JSON.stringify(prepared), stderr: "",
               killed: false };
    },
  };
  return { pi, commands, handlers, tools, activeNames: () => [...active] };
}

function compactRender(overlays) {
  const compact = overlays.length >= 2 ? overlays[overlays.length - 2] : null;
  if (!compact) return "";
  return compact.component.render(40).join("\n");
}

const server = createServer((req, res) => {
  handle(req, res).catch((error) => {
    try {
      sendJson(res, 500, { error: String(error) });
    } catch {
      // Response may already be closed.
    }
  });
});
await new Promise((resolve, reject) => {
  server.once("error", reject);
  server.listen(0, "127.0.0.1", resolve);
});
const { port } = server.address();
const endpoint = `http://127.0.0.1:${port}`;

const scratch = mkdtempSync(join(tmpdir(), "agentgenome-session-probe-"));

try {
  const prepared = {
    prompt: "SESSION PROBE PROMPT",
    workspace: scratch,
    data_dir: join(scratch, "data"),
    mcp_config: {
      format: "agentgenome-teacher-mcp/2", endpoint, token: TOKEN,
      workspace: scratch, timeout_ms: 10000,
    },
    host_status: { running: true, pid: 4242 },
  };
  const env = makePi(prepared);
  createExtension(env.pi, {
    format: "agentgenome-pi-command/1", python: "/usr/bin/python3",
    provider: "probe-provider", model: "probe-model", helpers,
  });
  check("command registered", env.commands.has("agentgenome"));
  check("factory registers no tools before activation", env.tools.size === 0);

  const { ui, notices, overlays } = host();
  const ctx = { cwd: scratch, mode: "tui", ui };
  await env.commands.get("agentgenome").handler("", ctx);
  check("activation registers the advertised Graph tools",
    env.tools.has("create_run") && env.tools.has("validate_definition")
    && env.tools.size === 2);
  check("activation mounts both trace overlays", overlays.length === 2);
  check("activation advertises the trace entry",
    notices.some((item) => item.message.includes("Trace:")));

  await waitFor(() => compactRender(overlays).includes("run-fore"),
                "foreign fallback run");
  check("collapsed panel starts on the newest listed run",
    compactRender(overlays).includes("run-fore"));

  const createRun = env.tools.get("create_run");
  const created = await createRun.execute("call-create-1", {
    run_id: "run-owned", fail: false,
  }, undefined, undefined, ctx);
  check("successful create_run stays a normal tool result",
    created.isError !== true
    && created.details?.run_id === "run-owned"
    && created.details?.call_id === "call-create-1");
  await waitFor(() => compactRender(overlays).includes("run-owne"),
                "owned run selection");
  check("successful create_run becomes the default selection",
    compactRender(overlays).includes("run-owne")
    && !compactRender(overlays).includes("run-fore"));
  check("the owned run is older than the foreign fallback",
    state.runs.find((run) => run.run_id === "run-owned").created_at
      < state.runs.find((run) => run.run_id === "run-foreign").created_at);

  const failed = await createRun.execute("call-create-2", {
    run_id: "run-rejected", fail: true,
  }, undefined, undefined, ctx);
  await sleep(150);
  check("failed create_run reports an error",
    failed.isError === true
    && JSON.parse(failed.content[0].text).code === "run_rejected");
  check("failed create_run never claims a session run",
    compactRender(overlays).includes("run-owne")
    && !compactRender(overlays).includes("run-rejected"));

  const validate = env.tools.get("validate_definition");
  const validated = await validate.execute("call-validate", {}, undefined,
                                           undefined, ctx);
  await sleep(150);
  check("unrelated successful tools never claim a session run",
    validated.isError !== true && compactRender(overlays).includes("run-owne"));

  await env.commands.get("agentgenome").handler("off", ctx);
  check("off closes the trace overlay",
    overlays[0].doneCalled === true && overlays[1].doneCalled === true);
  const refused = await createRun.execute("call-after-off", {
    run_id: "run-after-off",
  }, undefined, undefined, ctx);
  check("off leaves tools inactive",
    refused.isError === true
    && JSON.parse(refused.content[0].text).code === "not_active");

  await env.commands.get("agentgenome").handler("", ctx);
  await waitFor(() => overlays.length === 4
    && compactRender(overlays).includes("run-fore"),
    "re-activation fallback");
  check("re-activation does not reuse the previous owned run",
    compactRender(overlays).includes("run-fore")
    && !compactRender(overlays).includes("run-owne"));

  const sessionStart = env.handlers.get("session_start") ?? [];
  check("session reset handler registered", sessionStart.length === 1);
  sessionStart[0]({ type: "session_start" }, ctx);
  check("session reset closes the trace overlay",
    overlays[2].doneCalled === true && overlays[3].doneCalled === true);
  await env.commands.get("agentgenome").handler("", ctx);
  await waitFor(() => overlays.length === 6
    && compactRender(overlays).includes("run-fore"),
    "post-reset activation fallback");
  check("post-reset activation starts without session ownership",
    !compactRender(overlays).includes("run-owne"));
} finally {
  await new Promise((resolve) => server.close(resolve));
  rmSync(scratch, { recursive: true, force: true });
}

process.stdout.write(JSON.stringify({ ok: true, checks }) + "\n");
