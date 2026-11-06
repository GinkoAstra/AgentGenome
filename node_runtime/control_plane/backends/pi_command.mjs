/**
 * Ordinary-Pi `/agentgenome` entry.
 *
 * The extension factory only registers one command and its lifecycle events.
 * No process starts, no Graph tool is registered and no Teacher prompt is
 * injected until the user runs `/agentgenome` in a session. Activation prepares
 * the existing control plane through the installed Python, reuses the shipped
 * MCP client bridge for the Graph tools, and appends the Teacher prompt to the
 * current Pi system prompt. The native Pi context, model and tools stay as the
 * user configured them; `/agentgenome off` only removes this mode.
 *
 * The generated user extension (`agentgenome.js`) imports this module and
 * passes the absolute Python executable plus the Worker provider/model that
 * were fixed at install time. The session's own model is never used to infer
 * the Host Worker configuration.
 */
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { registerTeacherTools, ToolNameConflictError } from "./pi_teacher_tools.mjs";
import { createTraceClient, createTracePanel } from "./pi_trace.mjs";

export const COMMAND_NAME = "agentgenome";
export const CONFIG_FORMAT = "agentgenome-pi-command/1";
const PREPARE_TIMEOUT_MS = 60000;

// node_runtime/control_plane/backends/pi_command.mjs -> package root.
const PACKAGE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../../..");

function fail(message, code) {
  return Object.assign(new Error(message), { code });
}

function normalizeOptions(options) {
  if (!options || options.format !== CONFIG_FORMAT) {
    throw fail(
      `AgentGenome Pi command config must use format ${CONFIG_FORMAT}`,
      "invalid_config");
  }
  for (const key of ["python", "provider", "model"]) {
    if (typeof options[key] !== "string" || options[key].length === 0) {
      throw fail(`AgentGenome Pi command config is missing ${key}`,
                 "invalid_config");
    }
  }
  return {
    ...options,
    packageRoot: options.packageRoot || PACKAGE_ROOT,
  };
}

function parseJson(text) {
  try {
    const value = JSON.parse(text);
    return value && typeof value === "object" ? value : null;
  } catch {
    return null;
  }
}

function describeFailure(result) {
  const payload = parseJson(result?.stderr) || parseJson(result?.stdout);
  const error = payload?.error;
  if (error && typeof error.message === "string") return error.message;
  if (typeof payload?.message === "string") return payload.message;
  const stderr = String(result?.stderr ?? "").trim();
  if (stderr) return stderr.split("\n").slice(-4).join(" | ");
  return `prepare exited with code ${result?.code ?? "unknown"}`;
}

function parsePrepared(result) {
  if (!result || result.code !== 0) {
    throw fail(describeFailure(result), "prepare_failed");
  }
  const payload = parseJson(result.stdout);
  if (!payload) {
    throw fail("prepare returned no JSON object", "prepare_failed");
  }
  if (typeof payload.prompt !== "string" || payload.prompt.length === 0) {
    throw fail("prepare returned no prompt", "prepare_failed");
  }
  if (typeof payload.workspace !== "string" || payload.workspace.length === 0) {
    throw fail("prepare returned no workspace", "prepare_failed");
  }
  if (typeof payload.data_dir !== "string" || payload.data_dir.length === 0) {
    throw fail("prepare returned no service data directory", "prepare_failed");
  }
  const mcpConfig = typeof payload.mcp_config === "string"
    ? parseJson(payload.mcp_config) : payload.mcp_config;
  if (!mcpConfig || typeof mcpConfig !== "object") {
    throw fail("prepare returned no MCP config", "prepare_failed");
  }
  const hostStatus = payload.host_status;
  if (!hostStatus || typeof hostStatus !== "object") {
    throw fail("prepare returned no host status", "prepare_failed");
  }
  return { ...payload, mcpConfig, hostStatus };
}

function hostSummary(status) {
  if (!status) return "unknown";
  if (status.error) {
    const mismatch = status.mismatch
      ? ` mismatch=${JSON.stringify(status.mismatch)}` : "";
    return `${status.error}${mismatch}`;
  }
  return `running=${status.running === true}, pid=${status.pid ?? "null"}`;
}

export default function agentgenomeCommandExtension(pi, rawOptions) {
  const config = normalizeOptions(rawOptions);

  // Registered once per Pi process; Pi has no unregister operation. Activation
  // state decides whether the tools may run, so a session reset only needs to
  // deactivate and close the MCP connection.
  let bridge = null;
  let registration = null;
  let activationInFlight = null;
  let activationGeneration = 0;
  let trace = null;
  const state = {
    active: false,
    prompt: null,
    workspace: null,
    dataDir: null,
    hostStatus: null,
    mcpConfig: null,
  };

  function currentToolNames() {
    return bridge ? bridge.toolNames : [];
  }

  function removeTools() {
    const names = currentToolNames();
    if (names.length === 0 || typeof pi.setActiveTools !== "function") return;
    try {
      const remaining = pi.getActiveTools()
        .filter((name) => !names.includes(name));
      pi.setActiveTools(remaining);
    } catch {
      // A stale runtime cannot be repaired from here; the next activation
      // re-adds the tools when the runtime is bound again.
    }
  }

  function closeTrace() {
    if (trace === null) return;
    const current = trace;
    trace = null;
    try {
      current.close();
    } catch {
      // The overlay may already be gone; activation still ends cleanly.
    }
  }

  function deactivate() {
    // Any in-flight activation compares this generation after each await; an
    // off/session reset must win even if prepare or tool registration finishes
    // later. Pi cannot unregister tools, so a late registration keeps its
    // reusable handle but is closed and left inactive.
    activationGeneration += 1;
    const wasActive = state.active;
    state.active = false;
    state.prompt = null;
    state.workspace = null;
    state.dataDir = null;
    state.hostStatus = null;
    state.mcpConfig = null;
    closeTrace();
    if (bridge) {
      try {
        bridge.close();
      } catch {
        // The session may already be closed.
      }
    }
    removeTools();
    return wasActive;
  }

  async function prepare(ctx) {
    const cwd = typeof ctx?.cwd === "string" ? ctx.cwd : "";
    if (!cwd) throw fail("this Pi context has no working directory",
                         "no_cwd");
    // The service data directory is per-user and shared by every cwd; the
    // local workspace is this session's cwd. prepare starts or reuses the
    // default service and returns a delivery-free publisher connection.
    const result = await pi.exec(config.python, [
      "-m", "node_runtime.control_plane.pi_integration", "prepare",
      "--workspace", cwd,
      "--provider", config.provider,
      "--model", config.model,
    ], {
      cwd: config.packageRoot,
      timeout: PREPARE_TIMEOUT_MS,
      signal: ctx?.signal,
    });
    return { prepared: parsePrepared(result) };
  }

  function currentBridge(prepared) {
    if (bridge !== null) return Promise.resolve(bridge);
    if (registration === null) {
      // One registration attempt per process. A second activation while this
      // is pending must not register the same names again.
      registration = registerTeacherTools(pi, prepared.mcpConfig, {
        enabled: () => state.active,
        sessionShutdown: false,
        // A run created by this Pi becomes the panel's default selection. The
        // callback is silent for failures and is always the committed result.
        onResult: (event) => {
          if (!state.active || trace === null) return;
          if (event?.tool !== "create_run") return;
          const runId = event.result?.run_id;
          if (typeof runId === "string" && runId.length > 0) {
            trace.noteSessionRun(runId);
          }
        },
      }).then((handle) => {
        if (bridge === null) bridge = handle;
        return bridge;
      }).finally(() => {
        registration = null;
      });
    }
    return registration;
  }

  // A read-only view over the same prepared service credential. The panel is a
  // nonmodal overlay owned by Pi; it never starts a service, calls a model or
  // writes local state. Only interactive TUI sessions get it, and only when the
  // installed wrapper supplied Pi's official width helpers.
  function ensureTrace(ctx) {
    if (ctx?.mode !== "tui" || state.mcpConfig === null) return false;
    if (trace !== null) return true;
    if (!config.helpers) return false;
    let client;
    try {
      client = createTraceClient({
        endpoint: state.mcpConfig.endpoint,
        token: state.mcpConfig.token,
      });
    } catch {
      return false;
    }
    let panel;
    try {
      panel = createTracePanel({
        client,
        helpers: config.helpers,
        mode: ctx.mode,
        ui: ctx.ui,
        notify: (message, type) => ctx.ui.notify(message, type),
      });
    } catch {
      return false;
    }
    if (!panel.mount()) {
      panel.dispose();
      return false;
    }
    trace = panel;
    return true;
  }

  async function runActivation(ctx, generation) {
    let prepared;
    try {
      ({ prepared } = await prepare(ctx));
    } catch (error) {
      if (generation === activationGeneration) {
        ctx.ui.notify(`AgentGenome 启用失败: ${String(error?.message ?? error)}`,
                      "error");
      }
      return;
    }
    if (generation !== activationGeneration) return;

    let handle;
    try {
      handle = await currentBridge(prepared);
    } catch (error) {
      if (generation !== activationGeneration) return;
      const code = error?.code ?? "mcp_unavailable";
      if (error instanceof ToolNameConflictError || code === "tool_name_conflict") {
        ctx.ui.notify(
          `AgentGenome 启用失败: 工具名与现有工具冲突（${error.names?.join(", ")}）。`
          + "未注册任何 AgentGenome 工具。",
          "error");
      } else {
        ctx.ui.notify(`AgentGenome 启用失败: ${String(error?.message ?? error)}`,
                      "error");
      }
      return;
    }
    if (generation !== activationGeneration) {
      // Registration finished after off/session reset. Keep the handle so a
      // later activation can reuse the registered tool names, but close its
      // MCP connection and leave every guard inactive. Pi adds every newly
      // registered tool to the active loadout, so remove our names again.
      try {
        handle.close();
      } catch {
        // The connection may already be closed.
      }
      removeTools();
      return;
    }

    state.active = true;
    state.prompt = prepared.prompt;
    state.workspace = prepared.workspace;
    state.dataDir = prepared.data_dir;
    state.hostStatus = prepared.hostStatus;
    state.mcpConfig = prepared.mcpConfig;

    try {
      const current = pi.getActiveTools();
      pi.setActiveTools([...new Set([...current, ...currentToolNames()])]);
    } catch (error) {
      deactivate();
      ctx.ui.notify(
        `AgentGenome 已准备，但工具激活失败: ${String(error?.message ?? error)}`,
        "error");
      return;
    }

    const host = hostSummary(prepared.hostStatus);
    const warning = prepared.hostStatus.running === true
      ? "" : "\n警告: Host 未按本次参数运行，先处理上面的 host 状态再提交 run。";
    const traceHint = ensureTrace(ctx)
      ? "\nTrace: 右上角 AgentGenome ▸（点击或 /agentgenome trace 展开）"
      : "";
    ctx.ui.notify(
      `AgentGenome 已启用。\n数据目录: ${prepared.data_dir}\n工作区: ${prepared.workspace}`
      + `\nHost: ${host}\nGraph 工具: ${currentToolNames().length} 个`
      + `${traceHint}${warning}`,
      prepared.hostStatus.running === true ? "info" : "warning");
  }

  function activate(ctx) {
    if (state.active) {
      ensureTrace(ctx);
      ctx.ui.notify(
        `AgentGenome 已启用（数据目录: ${state.dataDir}）。重复命令不会重复启动。`,
        "info");
      return Promise.resolve();
    }
    if (activationInFlight !== null) {
      ctx.ui.notify(
        "AgentGenome 正在启用，请稍候；如需取消请执行 /agentgenome off。",
        "info");
      return activationInFlight;
    }
    ctx.ui.notify(
      `AgentGenome 正在准备普通 Pi 入口（Worker ${config.provider}/${config.model}）...`,
      "info");

    const generation = activationGeneration;
    const run = runActivation(ctx, generation)
      .catch((error) => {
        if (generation === activationGeneration) {
          ctx.ui.notify(
            `AgentGenome 启用失败: ${String(error?.message ?? error)}`,
            "error");
        }
      })
      .finally(() => {
        if (activationInFlight === run) activationInFlight = null;
      });
    activationInFlight = run;
    return run;
  }

  function reportStatus(ctx) {
    if (activationInFlight !== null && !state.active) {
      ctx.ui.notify(
        "AgentGenome 正在启用：prepare 或工具注册尚未完成。"
        + "可执行 /agentgenome off 取消本次启用。",
        "info");
      return;
    }
    if (!state.active) {
      ctx.ui.notify(
        "AgentGenome 未启用。原生 Pi 上下文、模型和工具保持不变；"
        + "运行 /agentgenome 启用，/agentgenome off 停用。",
        "info");
      return;
    }
    ctx.ui.notify(
      `AgentGenome 已启用。\n数据目录: ${state.dataDir}\n工作区: ${state.workspace}`
      + `\nHost（prepare 时）: ${hostSummary(state.hostStatus)}`
      + `\nGraph 工具: ${currentToolNames().length} 个`
      + `\nTrace 面板: ${trace !== null
        ? "已挂载（/agentgenome trace 切换）" : "当前会话不显示"}`,
      "info");
  }

  function traceCommand(ctx) {
    if (ctx?.mode !== "tui") {
      ctx.ui.notify(
        "AgentGenome trace 面板只在交互式 TUI 中渲染；当前模式不使用终端 UI。",
        "warning");
      return;
    }
    if (!state.active) {
      ctx.ui.notify(
        "AgentGenome 未启用；先运行 /agentgenome 启用，再打开 trace 面板。",
        "warning");
      return;
    }
    if (!ensureTrace(ctx)) {
      ctx.ui.notify(
        "AgentGenome trace 面板未能挂载：当前安装入口没有提供 Pi 的宽度 "
        + "helpers。请用最新安装器重新安装普通 Pi 入口后重启会话。",
        "error");
      return;
    }
    trace.toggle();
  }

  function commandHandler(args, ctx) {
    const action = String(args ?? "").trim();
    if (action === "") return activate(ctx);
    if (action === "trace") {
      traceCommand(ctx);
      return;
    }
    if (action === "off") {
      const pending = activationInFlight !== null;
      const wasActive = deactivate();
      ctx.ui.notify(
        pending
          ? "AgentGenome 正在启用；本次启用已取消，原生/其他扩展工具保留，"
            + "Host 与 run 未停止。"
          : wasActive
            ? "AgentGenome 已停用；原生/其他扩展工具保留，Host 与 run 未停止。"
            : "AgentGenome 原本未启用。",
        "info");
      return;
    }
    if (action === "status") {
      reportStatus(ctx);
      return;
    }
    ctx.ui.notify(
      "用法: /agentgenome（启用）、/agentgenome trace（trace 面板）、"
      + "/agentgenome off（停用）、/agentgenome status",
      "warning");
  }

  pi.registerCommand(COMMAND_NAME, {
    description: "启用或停用当前会话的 AgentGenome Teacher 模式并打开 trace 面板",
    handler: commandHandler,
  });

  // Pi re-normalizes the options each run. An earlier extension may have
  // forced the complete prompt, which makes appendSystemPrompt irrelevant;
  // in that case return the rendered prompt plus this mode's prompt so the
  // forced prefix and the Teacher instructions both survive.
  pi.on("before_agent_start", (event) => {
    if (!state.active || !state.prompt) return;
    const options = event?.systemPromptOptions;
    if (!options) return;
    const forced = options.forceSystemPrompt;
    if (typeof forced === "string" && forced.length > 0) {
      const base = typeof event.systemPrompt === "string" && event.systemPrompt.length > 0
        ? event.systemPrompt : forced;
      return { systemPrompt: `${base}\n\n${state.prompt}` };
    }
    const existing = options.appendSystemPrompt || "";
    options.appendSystemPrompt = existing
      ? `${existing}\n\n${state.prompt}`
      : state.prompt;
  });

  // A new session or a different tree branch must not inherit an activation
  // from a context that is no longer current. Re-run /agentgenome to reconnect
  // the same cwd data directory.
  pi.on("session_start", () => {
    deactivate();
  });
  pi.on("session_tree", () => {
    deactivate();
  });
  pi.on("session_shutdown", () => {
    deactivate();
  });
}
