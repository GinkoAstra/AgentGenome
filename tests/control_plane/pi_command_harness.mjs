/**
 * Node harness for the ordinary-Pi /agentgenome command extension.
 *
 * Imports the real pi_command.mjs with a fake ExtensionAPI that mirrors the
 * documented Pi surface used by this extension. The extension runs the real
 * `pi_integration prepare`, starts the real per-user network service, and the
 * Graph tools use the real Streamable HTTP MCP endpoint. No model is called and
 * no run is submitted.
 *
 * A second, in-process fake HTTP MCP service exercises delayed registration and
 * credential/connection lifecycle without depending on service timing.
 *
 * Run through pytest (test_pi_command_node.py), which supplies:
 *   AGENTGENOME_TEST_PYTHON      absolute Python with agentgenome deps
 *   AGENTGENOME_TEST_EXTENSION   absolute pi_command.mjs path
 *   AGENTGENOME_TEST_ROOT        repository/package root
 * and an isolated HOME/XDG_STATE_HOME so the default service is never shared
 * with the real user instance.
 */
import { spawn, spawnSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";

const python = process.env.AGENTGENOME_TEST_PYTHON;
const extensionPath = process.env.AGENTGENOME_TEST_EXTENSION;
const root = process.env.AGENTGENOME_TEST_ROOT;
const xdgState = process.env.XDG_STATE_HOME;
const PROVIDER = "llama-local";
const MODEL = "qwen3.5-2b";
const NATIVE_TOOLS = ["read", "bash", "edit", "write"];
// The advertised surface belongs to the service, not this harness. Reading it
// from the shared operation list keeps activation-parity checks correct when
// the public tool set grows, without loading the MCP SDK in this process.
const expectedTools = spawnSync(
  python,
  ["-c", "from node_runtime.control_plane.http_api import PUBLIC_OPERATIONS; print('\\n'.join(PUBLIC_OPERATIONS))"],
  { cwd: root, encoding: "utf8", env: { ...process.env, PYTHONPATH: root } },
).stdout.trim().split("\n").filter(Boolean);

if (!python || !extensionPath || !root || !xdgState) {
  console.error("missing harness environment (python/extension/root/xdg)");
  process.exit(2);
}

const extensionModule = await import(extensionPath);
const createExtension = extensionModule.default;
const defaultDataDir = join(xdgState, "agentgenome", "default");

const checks = [];
function check(name, condition, detail) {
  if (!condition) {
    throw new Error(`${name}${detail ? `: ${detail}` : ""}`);
  }
  checks.push(name);
}

function makePi(existingToolNames = [], { exec: execImpl } = {}) {
  const commands = new Map();
  const handlers = new Map();
  const tools = new Map();
  let active = [...NATIVE_TOOLS];
  const foreign = existingToolNames.map((name) => ({
    name, description: "third-party", parameters: { type: "object", properties: {} },
    sourceInfo: { path: "<foreign>", type: "extension" },
  }));

  const pi = {
    registerCommand(name, options) {
      commands.set(name, options);
    },
    registerTool(tool) {
      if (tools.has(tool.name)) throw new Error(`duplicate tool ${tool.name}`);
      tools.set(tool.name, tool);
      // Real Pi refreshes the tool registry on registerTool and adds every
      // newly registered tool to the active loadout.
      active = [...new Set([...active, tool.name])];
    },
    on(event, handler) {
      const list = handlers.get(event) ?? [];
      list.push(handler);
      handlers.set(event, list);
      return () => {
        const current = handlers.get(event) ?? [];
        handlers.set(event, current.filter((item) => item !== handler));
      };
    },
    getAllTools() {
      return [
        ...foreign,
        ...[...tools.values()].map((tool) => ({
          name: tool.name, description: tool.description,
          parameters: tool.parameters, sourceInfo: { path: "<agentgenome>" },
        })),
        ...NATIVE_TOOLS.map((name) => ({
          name, description: name, parameters: { type: "object", properties: {} },
          sourceInfo: { path: "<native>" },
        })),
      ];
    },
    getActiveTools() {
      return [...active];
    },
    setActiveTools(names) {
      const valid = new Set([...NATIVE_TOOLS, ...tools.keys()]);
      active = [...new Set(names.filter((name) => valid.has(name)))];
    },
    exec(command, args, options) {
      if (execImpl) return Promise.resolve(execImpl(command, args, options));
      return new Promise((resolvePromise) => {
        const child = spawn(command, args, {
          cwd: options?.cwd,
          stdio: ["ignore", "pipe", "pipe"],
        });
        let stdout = "";
        let stderr = "";
        let timer = null;
        child.stdout.on("data", (chunk) => { stdout += chunk; });
        child.stderr.on("data", (chunk) => { stderr += chunk; });
        child.on("error", (error) => {
          if (timer) clearTimeout(timer);
          resolvePromise({ stdout, stderr: stderr + String(error), code: 1, killed: false });
        });
        child.on("close", (code) => {
          if (timer) clearTimeout(timer);
          resolvePromise({ stdout, stderr, code: code ?? 0, killed: false });
        });
        if (options?.timeout) {
          timer = setTimeout(() => child.kill("SIGTERM"), options.timeout);
          timer.unref?.();
        }
      });
    },
  };
  return { pi, commands, handlers, tools, activeNames: () => [...active] };
}

function makeContext(cwd) {
  const notices = [];
  return {
    cwd,
    mode: "json",
    hasUI: true,
    notices,
    ui: {
      notify(message, type = "info") {
        notices.push({ message: String(message), type });
      },
    },
  };
}

function payloadOf(result) {
  const text = (result?.content ?? [])
    .filter((part) => part && part.type === "text")
    .map((part) => part.text)
    .join("\n");
  let payload = null;
  try {
    payload = JSON.parse(text);
  } catch {
    payload = null;
  }
  return { result, payload };
}

async function validate(env, ctx, callId) {
  const tool = env.tools.get("validate_definition");
  check(`validate tool registered (${callId})`, Boolean(tool));
  const { result, payload } = payloadOf(
    await tool.execute(callId, { definition_path: "sop.yaml" },
                       undefined, undefined, ctx));
  return { result, payload, details: result?.details ?? {} };
}

function sleep(ms) {
  return new Promise((resolvePromise) => setTimeout(resolvePromise, ms));
}

async function waitFor(predicate, what, timeout = 8000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await predicate()) return;
    await sleep(25);
  }
  throw new Error(`timed out waiting for ${what}`);
}

function fakeExecResult(payload) {
  return { code: 0, stdout: JSON.stringify(payload), stderr: "", killed: false };
}

function fakePrepared(fake, dataDir, workspace) {
  return {
    prompt: "FAKE TEACHER PROMPT",
    workspace,
    data_dir: dataDir,
    mcp_config: {
      format: "agentgenome-teacher-mcp/2",
      endpoint: fake.endpoint,
      token: fake.token,
      workspace,
      timeout_ms: 10000,
    },
    host_status: { running: true, pid: 4242 },
  };
}

/**
 * A real HTTP MCP endpoint in this process. It speaks JSON-RPC over POST and
 * records sessions/calls so the harness can observe connection lifecycle
 * (initialize -> list -> call -> DELETE / reconnect) without a service.
 */
async function startFakeMcp(name, { delayMs = 0 } = {}) {
  const token = `fake-token-${name}`;
  const state = { started: 0, deletes: 0, sessions: new Set(), calls: [] };

  function respond(res, status, payload, headers = {}) {
    const body = payload === undefined ? "" : JSON.stringify(payload);
    res.writeHead(status, {
      "Content-Type": "application/json",
      "Content-Length": Buffer.byteLength(body),
      ...headers,
    });
    res.end(body);
  }

  async function handle(req, res, body) {
    if (req.method === "DELETE") {
      state.deletes += 1;
      const id = req.headers["mcp-session-id"];
      if (id) state.sessions.delete(id);
      res.writeHead(204);
      res.end();
      return;
    }
    if (req.method !== "POST") {
      respond(res, 405, { error: "method" });
      return;
    }
    if (req.headers.authorization !== `Bearer ${token}`) {
      respond(res, 401, { error: "unauthorized" });
      return;
    }
    let message;
    try {
      message = JSON.parse(body || "{}");
    } catch {
      respond(res, 400, { error: "bad json" });
      return;
    }
    if (message.method === "initialize") {
      state.started += 1;
      const sessionId = `fake-session-${state.started}`;
      state.sessions.add(sessionId);
      await sleep(delayMs);
      respond(res, 200, {
        jsonrpc: "2.0", id: message.id, result: {
          protocolVersion: "2025-11-25", capabilities: {},
          serverInfo: { name: "fake", version: "1" },
        },
      }, { "Mcp-Session-Id": sessionId });
      return;
    }
    if (message.method === "notifications/initialized") {
      res.writeHead(202);
      res.end();
      return;
    }
    if (message.method === "tools/list") {
      respond(res, 200, {
        jsonrpc: "2.0", id: message.id, result: { tools: [{
          name: "validate_definition", description: "fake",
          inputSchema: { type: "object", properties: {
            document: { type: "object" }, definition_text: { type: "string" },
          } },
        }] },
      });
      return;
    }
    if (message.method === "tools/call") {
      state.calls.push(message.params);
      const payload = {
        ok: true, operation: "validate_definition",
        result: { fake: true, arguments: message.params?.arguments ?? {} },
      };
      respond(res, 200, {
        jsonrpc: "2.0", id: message.id, result: {
          content: [{ type: "text", text: JSON.stringify(payload) }],
          structuredContent: payload,
        },
      });
      return;
    }
    respond(res, 200, {
      jsonrpc: "2.0", id: message.id,
      error: { code: -32601, message: "method not found" },
    });
  }

  const server = createServer((req, res) => {
    let body = "";
    req.on("data", (chunk) => { body += chunk; });
    req.on("end", () => {
      handle(req, res, body).catch((error) => {
        try {
          respond(res, 500, { error: String(error) });
        } catch {
          // The response may already be closed.
        }
      });
    });
  });
  await new Promise((resolvePromise, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolvePromise);
  });
  const { port } = server.address();
  return {
    endpoint: `http://127.0.0.1:${port}`,
    token,
    state,
    close: () => new Promise((resolvePromise) => server.close(resolvePromise)),
  };
}

function publisherToken() {
  const auth = JSON.parse(
    readFileSync(join(defaultDataDir, "service-auth.json"), "utf8"));
  const entry = auth.tokens.find(
    (item) => (item.kind ?? "publisher") === "publisher");
  return entry?.token ?? null;
}

function stopService(dataDir) {
  spawnSync(python, ["-m", "node_runtime.control_plane.host", "stop",
                     "--data-dir", dataDir], { cwd: root, stdio: "ignore" });
}

const scratch = mkdtempSync(join(tmpdir(), "agentgenome-pi-command-"));
const project = join(scratch, "project");
const secondProject = join(scratch, "second-project");
const conflictProject = join(scratch, "conflict-project");
const fakeWorkspace = join(scratch, "fake-workspace");
mkdirSync(project, { recursive: true });
mkdirSync(secondProject, { recursive: true });
mkdirSync(conflictProject, { recursive: true });
mkdirSync(fakeWorkspace, { recursive: true });
const sop = readFileSync(join(root, "examples", "agent-workflow",
                              "graph-two-stage.yaml"));
writeFileSync(join(fakeWorkspace, "sop.yaml"), sop);
const fakeData = join(scratch, "fake-data");
const registrationData = join(scratch, "registration-data");

const fakes = [];

try {
  check("per-cwd data dir export removed",
        extensionModule.dataDirForCwd === undefined);
  check("the default service starts out absent", !existsSync(defaultDataDir));

  const env = makePi();
  createExtension(env.pi, {
    format: "agentgenome-pi-command/1", python, provider: PROVIDER, model: MODEL,
  });
  check("command registered", env.commands.has("agentgenome"));
  check("factory registers no tools before activation", env.tools.size === 0);
  check("factory starts no service before activation",
        !existsSync(defaultDataDir));

  const ctx = makeContext(project);
  await env.commands.get("agentgenome").handler("", ctx);
  check("activation registered every advertised tool",
        env.tools.size === expectedTools.length, `got ${env.tools.size}`);
  check("activation notified success",
        ctx.notices.some((item) => item.type === "info"
                         && item.message.includes("AgentGenome 已启用")));
  check("activation names the shared per-user data directory",
        ctx.notices.some((item) => item.message.includes(defaultDataDir)));
  check("activation names the session cwd as workspace",
        ctx.notices.some((item) => item.message.includes(project)));
  const active = env.activeNames();
  for (const name of NATIVE_TOOLS) {
    check(`native tool preserved: ${name}`, active.includes(name));
  }
  for (const name of env.tools.keys()) {
    check(`MCP tool active: ${name}`, active.includes(name));
  }

  writeFileSync(join(project, "sop.yaml"), sop);

  let { result, payload, details } = await validate(env, ctx, "validate-active-1");
  check("real network MCP validate succeeds",
        result.isError !== true && payload?.ok === true);
  check("validate returns Graph /2",
        payload?.result?.document?.format === "agentgenome-graph-definition/2");
  check("tool details no longer expose a stdio server pid",
        details.server_pid === undefined);
  check("tool details keep the tool and call identity",
        details.tool === "validate_definition" && details.call_id === "validate-active-1");

  // The publisher credential is a private file, never model-visible context.
  const token = publisherToken();
  check("service issued a publisher credential", typeof token === "string" && token.length > 0);
  const notices = ctx.notices.map((item) => item.message).join("\n");
  check("publisher token never appears in notifications", !notices.includes(token));
  const promptFile = join(defaultDataDir, "teacher", "prompt.md");
  check("publisher token never appears in the Teacher prompt",
        !readFileSync(promptFile, "utf8").includes(token));
  const logFile = join(defaultDataDir, "host.log");
  check("publisher token never appears in the service log",
        !existsSync(logFile) || !readFileSync(logFile, "utf8").includes(token));

  const toolCount = env.tools.size;
  await env.commands.get("agentgenome").handler("", ctx);
  check("repeat activation is idempotent", env.tools.size === toolCount);
  await env.commands.get("agentgenome").handler("status", ctx);
  check("status reports active",
        ctx.notices.some((item) => item.message.includes("AgentGenome 已启用")));
  const traceNotices = ctx.notices.length;
  await env.commands.get("agentgenome").handler("trace", ctx);
  check("trace stays non-TUI outside interactive mode",
        ctx.notices.slice(traceNotices).some((item) => item.type === "warning"
          && item.message.includes("交互式 TUI")));

  await env.commands.get("agentgenome").handler("off", ctx);
  const offActive = env.activeNames();
  check("off keeps native tools",
        NATIVE_TOOLS.every((name) => offActive.includes(name)));
  check("off removes MCP tools from the loadout",
        [...env.tools.keys()].every((name) => !offActive.includes(name)));
  ({ result, payload } = await validate(env, ctx, "validate-off"));
  check("inactive tool refuses execution",
        result.isError === true && payload?.ok === false
        && payload?.code === "not_active");

  await env.commands.get("agentgenome").handler("", ctx);
  ({ result, payload } = await validate(env, ctx, "validate-reenabled"));
  check("re-enable reconnects and validates",
        result.isError !== true && payload?.ok === true);
  await env.commands.get("agentgenome").handler("off", ctx);

  await env.commands.get("agentgenome").handler("", ctx);
  ({ result, payload } = await validate(env, ctx, "validate-before-prompt"));
  check("third activation validates", result.isError !== true && payload?.ok === true);

  const beforeStart = env.handlers.get("before_agent_start") ?? [];
  check("before_agent_start handler registered", beforeStart.length === 1);
  const options = { appendSystemPrompt: "user-append", selectedTools: ["read"] };
  const plain = beforeStart[0]({
    type: "before_agent_start", prompt: "hello", systemPrompt: "base-prompt",
    systemPromptOptions: options,
  }, ctx);
  check("normal path mutates appendSystemPrompt in place", plain === undefined);
  check("Teacher prompt is appended, not replaced",
        options.appendSystemPrompt.startsWith("user-append")
        && options.appendSystemPrompt.includes("AgentGenome 的 Teacher"));
  check("prompt injection keeps tool selection",
        options.selectedTools.length === 1 && options.selectedTools[0] === "read");
  check("prompt injection carries no credential",
        !options.appendSystemPrompt.includes(token));

  // An earlier extension can force the complete prompt, making
  // appendSystemPrompt irrelevant. The forced prefix must survive.
  const forcedOptions = {
    forceSystemPrompt: "forced-prefix", appendSystemPrompt: "user-append",
    selectedTools: ["read"],
  };
  const forced = beforeStart[0]({
    type: "before_agent_start", prompt: "hello",
    systemPrompt: "forced-prefix", systemPromptOptions: forcedOptions,
  }, ctx);
  check("forced prompt returns the complete prompt",
        typeof forced?.systemPrompt === "string"
        && forced.systemPrompt.startsWith("forced-prefix\n\n")
        && forced.systemPrompt.includes("你是 AgentGenome 的 Teacher"));
  check("forced path leaves appendSystemPrompt untouched",
        forcedOptions.appendSystemPrompt === "user-append");
  check("forced prompt carries no credential",
        !forced.systemPrompt.includes(token));
  const forcedAgain = beforeStart[0]({
    type: "before_agent_start", prompt: "again",
    systemPrompt: "forced-prefix", systemPromptOptions: {
      forceSystemPrompt: "forced-prefix", selectedTools: ["read"],
    },
  }, ctx);
  check("forced prompt does not accumulate",
        forcedAgain.systemPrompt.split("你是 AgentGenome 的 Teacher").length === 2);

  // A second cwd attaches to the same per-user service with its own workspace,
  // and closing that session leaves the first window's tools working.
  const second = makePi();
  createExtension(second.pi, {
    format: "agentgenome-pi-command/1", python, provider: PROVIDER, model: MODEL,
  });
  const secondCtx = makeContext(secondProject);
  writeFileSync(join(secondProject, "sop.yaml"), sop);
  await second.commands.get("agentgenome").handler("", secondCtx);
  check("a second cwd reuses the same default service",
        secondCtx.notices.some((item) => item.message.includes(defaultDataDir)));
  check("a second cwd registers the Graph tools",
        second.tools.size === expectedTools.length
        && second.activeNames().includes("validate_definition"));
  const secondResult = await validate(second, secondCtx, "validate-second-cwd");
  check("a second cwd validates against the same service",
        secondResult.payload?.ok === true);
  await second.commands.get("agentgenome").handler("off", secondCtx);
  ({ result, payload } = await validate(env, ctx, "validate-after-second-off"));
  check("closing the second window does not stop the first",
        result.isError !== true && payload?.ok === true);

  const sessionTree = env.handlers.get("session_tree") ?? [];
  check("session_tree handler registered", sessionTree.length === 1);
  sessionTree[0]({ type: "session_tree" }, ctx);
  ({ result, payload } = await validate(env, ctx, "validate-after-tree"));
  check("tree branch does not inherit activation",
        result.isError === true && payload?.code === "not_active");

  await env.commands.get("agentgenome").handler("", ctx);
  ({ result, payload } = await validate(env, ctx, "validate-before-shutdown"));
  check("fourth activation validates", result.isError !== true && payload?.ok === true);
  const shutdown = env.handlers.get("session_shutdown") ?? [];
  check("session_shutdown handler registered", shutdown.length === 1);
  shutdown[0]({ type: "session_shutdown" }, ctx);
  ({ result, payload } = await validate(env, ctx, "validate-after-shutdown"));
  check("shutdown closes the active mode",
        result.isError === true && payload?.code === "not_active");

  // -- delayed activation: off during prepare, concurrent command ----------
  const prepareWaiters = [];
  let prepareCalls = 0;
  const delayedFake = await startFakeMcp("delayed", { delayMs: 0 });
  fakes.push(delayedFake);
  const delayed = makePi([], {
    exec: () => {
      prepareCalls += 1;
      return new Promise((resolvePromise) => prepareWaiters.push(resolvePromise));
    },
  });
  createExtension(delayed.pi, {
    format: "agentgenome-pi-command/1", python, provider: PROVIDER, model: MODEL,
  });
  const delayedCtx = makeContext(secondProject);
  const activation = delayed.commands.get("agentgenome").handler("", delayedCtx);
  await waitFor(() => prepareCalls === 1, "delayed prepare to start");
  const duplicate = delayed.commands.get("agentgenome").handler("", delayedCtx);
  check("concurrent activation reports busy",
        delayedCtx.notices.some((item) => item.message.includes("正在启用，请稍候")));
  await delayed.commands.get("agentgenome").handler("off", delayedCtx);
  check("off during prepare cancels enabling",
        delayedCtx.notices.some((item) => item.message.includes("本次启用已取消")));
  for (const resolvePromise of prepareWaiters.splice(0)) {
    resolvePromise(fakeExecResult(
      fakePrepared(delayedFake, fakeData, secondProject)));
  }
  await activation;
  await duplicate;
  check("concurrent activation runs prepare once", prepareCalls === 1);
  check("cancelled activation registers no tools", delayed.tools.size === 0);
  check("cancelled activation never contacted the MCP endpoint",
        delayedFake.state.started === 0);
  check("cancelled activation leaves tools inactive",
        !delayed.activeNames().includes("validate_definition"));
  await delayed.commands.get("agentgenome").handler("status", delayedCtx);
  check("status reports cancelled activation as inactive",
        delayedCtx.notices.some((item) => item.message.includes("AgentGenome 未启用")));

  // -- delayed registration: session reset wins, handle stays reusable ----
  const registrationFake = await startFakeMcp("registration", { delayMs: 350 });
  fakes.push(registrationFake);
  const registration = makePi([], {
    exec: () => Promise.resolve(fakeExecResult(
      fakePrepared(registrationFake, registrationData, fakeWorkspace))),
  });
  createExtension(registration.pi, {
    format: "agentgenome-pi-command/1", python, provider: PROVIDER, model: MODEL,
  });
  const registrationCtx = makeContext(fakeWorkspace);
  const pendingActivation = registration.commands.get("agentgenome")
    .handler("", registrationCtx);
  await waitFor(() => registrationFake.state.started === 1,
                "delayed initialization to begin");
  registration.handlers.get("session_tree")[0](
    { type: "session_tree" }, registrationCtx);
  await pendingActivation;
  check("late registration still registers its tools",
        registration.tools.size === 1);
  check("late registration stays inactive",
        !registration.activeNames().includes("validate_definition"));
  await waitFor(() => registrationFake.state.deletes === 1,
                "the late registration session to close");
  check("late registration closes its discovery session",
        registrationFake.state.deletes === 1);
  ({ result, payload } = await validate(registration, registrationCtx,
                                        "validate-late-registration"));
  check("late registration tool refuses execution",
        result.isError === true && payload?.code === "not_active");
  check("refused tool does not reconnect",
        registrationFake.state.started === 1);

  await registration.commands.get("agentgenome").handler("", registrationCtx);
  check("late bridge can be activated later",
        registration.activeNames().includes("validate_definition"));
  let cycle = await validate(registration, registrationCtx, "validate-fake-1");
  check("late bridge reconnects to the network MCP endpoint",
        cycle.result.isError !== true && cycle.payload?.result?.fake === true);
  check("reconnect opens a new MCP session",
        registrationFake.state.started === 2);
  check("the uploaded definition arrives as text, not a client path",
        registrationFake.state.calls.length === 1
        && registrationFake.state.calls[0].arguments.definition_path === undefined
        && typeof registrationFake.state.calls[0].arguments.definition_text === "string"
        && registrationFake.state.calls[0].arguments.definition_text.includes(
          "agentgenome-graph-definition/2"));

  await registration.commands.get("agentgenome").handler("off", registrationCtx);
  await waitFor(() => registrationFake.state.deletes === 2,
                "the active session to close after off");
  check("off closes the active MCP session",
        registrationFake.state.deletes === 2);
  await registration.commands.get("agentgenome").handler("", registrationCtx);
  cycle = await validate(registration, registrationCtx, "validate-fake-2");
  check("closed bridge reconnects again",
        cycle.payload?.result?.fake === true
        && registrationFake.state.started === 3);
  await registration.commands.get("agentgenome").handler("off", registrationCtx);

  // A third-party tool with the same name must fail activation instead of
  // being silently shadowed.
  const conflictFake = await startFakeMcp("conflict", { delayMs: 0 });
  fakes.push(conflictFake);
  const conflicted = makePi(["validate_definition"], {
    exec: () => Promise.resolve(fakeExecResult(
      fakePrepared(conflictFake, join(scratch, "conflict-data"), conflictProject))),
  });
  createExtension(conflicted.pi, {
    format: "agentgenome-pi-command/1", python, provider: PROVIDER, model: MODEL,
  });
  const conflictCtx = makeContext(conflictProject);
  await conflicted.commands.get("agentgenome").handler("", conflictCtx);
  check("name conflict registers no tools", conflicted.tools.size === 0);
  check("name conflict reports explicitly",
        conflictCtx.notices.some((item) => item.type === "error"
                                 && item.message.includes("冲突")));
  check("name conflict leaves activation inactive",
        !conflicted.activeNames().includes("validate_definition"));

  console.log(JSON.stringify({ ok: true, checks }, null, 2));
} catch (error) {
  console.error(JSON.stringify({
    ok: false,
    error: String(error?.stack ?? error),
    checks,
  }, null, 2));
  process.exitCode = 1;
} finally {
  for (const fake of fakes) {
    try {
      await fake.close();
    } catch {
      // Already closed.
    }
  }
  stopService(defaultDataDir);
  rmSync(scratch, { recursive: true, force: true });
}
