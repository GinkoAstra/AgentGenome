/**
 * System tools for the Pi backend.
 *
 * The tool list, caller identity and review binding live in a context file
 * written by the Backend outside the model workspace. Every call is posted to
 * the running AgentGenome service over HTTP with the delivery credential from
 * that file; the service re-reads the committed Worker binding and goes
 * through the Unified API. Model arguments never carry identity, and no Python
 * process is started per tool call.
 */
import {
  closeSync, fsyncSync, lstatSync, openSync, readFileSync, realpathSync,
  renameSync, statSync, writeSync,
} from "node:fs";
import { isAbsolute, join, relative, sep } from "node:path";

import { requestText } from "./service_http.mjs";

const CONTEXT = process.env.AGENTGENOME_TOOL_CONTEXT;
const RECEIPT = process.env.AGENTGENOME_TOOL_RECEIPT;
const YIELD = process.env.AGENTGENOME_YIELD_FILE;
const TIMEOUT_MS = 60000;
const YIELD_FORMAT = "agentgenome-pi-yield/1";
const CONTEXT_FORMAT = "agentgenome-worker-tool-context/2";
const ENDPOINT_PATH = "/v1/worker/tool";
const REQUEST_TOOL = "node.request_decision";
const MAX_ARTIFACT_BYTES = 8 * 1024 * 1024;

function writeYield(context, output) {
  if (!YIELD) return;
  const payload = {
    format: YIELD_FORMAT,
    delivery_key: context.delivery_key,
    run_id: context.run_id,
    worker_id: context.worker_id,
    node_id: context.node_id,
    epoch: context.epoch,
    decision_id: output?.decision_id ?? null,
    at: new Date().toISOString(),
  };
  const temporary = `${YIELD}.${process.pid}.tmp`;
  const fd = openSync(temporary, "w", 0o600);
  try {
    writeSync(fd, JSON.stringify(payload));
    fsyncSync(fd);
  } finally {
    closeSync(fd);
  }
  renameSync(temporary, YIELD);
}

function fail(code, message) {
  throw Object.assign(new Error(message), { code });
}

function record(entry) {
  if (!RECEIPT) return;
  const line = JSON.stringify({ ...entry, at: new Date().toISOString() }) + "\n";
  const fd = openSync(RECEIPT, "a");
  try {
    writeSync(fd, line);
    fsyncSync(fd);
  } finally {
    closeSync(fd);
  }
}

function loadContext() {
  if (!CONTEXT) return null;
  let context;
  try {
    context = JSON.parse(readFileSync(CONTEXT, "utf8"));
  } catch {
    return null;
  }
  // The credential must not survive in the model process environment; the
  // in-memory copy is the only one this extension uses.
  delete process.env.AGENTGENOME_TOOL_CONTEXT;
  if (!context || context.format !== CONTEXT_FORMAT) return null;
  if (typeof context.endpoint !== "string" || !context.endpoint) return null;
  if (typeof context.token !== "string" || !context.token) return null;
  if (!Array.isArray(context.tools)) return null;
  return context;
}

function readWorkspaceFile(context, requested) {
  // The model only says which workspace-relative path to upload. The bytes
  // and every boundary check live here, on the same machine that holds the
  // file; the service never reads a caller path.
  const workspace = context.workspace;
  if (typeof workspace !== "string" || !workspace) {
    fail("unauthorized", "delivery has no worker workspace");
  }
  if (typeof requested !== "string" || !requested) {
    fail("invalid_arguments", "path is required");
  }
  const parts = requested.replace(/\\/g, "/").split("/")
    .filter((part) => part !== "" && part !== ".");
  if (!parts.length || isAbsolute(requested) || parts.includes("..")) {
    fail("path_outside_workspace",
         "path must stay inside the worker workspace");
  }
  let root;
  try {
    root = realpathSync(workspace);
  } catch {
    fail("workspace_unconfigured", "worker workspace does not exist");
  }
  let current = root;
  for (const part of parts) {
    current = join(current, part);
    let info;
    try {
      info = lstatSync(current);
    } catch {
      fail("path_not_found",
           `file ${requested} does not exist; paths are relative to the workspace root`);
    }
    if (info.isSymbolicLink()) {
      fail("path_symlink",
           "symlinks are not allowed inside the worker workspace");
    }
  }
  const info = statSync(current);
  if (!info.isFile()) {
    fail("path_not_file", "path must name a regular file");
  }
  const real = realpathSync(current);
  if (real !== root && !real.startsWith(root + sep)) {
    fail("path_outside_workspace",
         "path must stay inside the worker workspace");
  }
  if (info.size > MAX_ARTIFACT_BYTES) {
    fail("material_too_large",
         `artifact exceeds ${MAX_ARTIFACT_BYTES} bytes`);
  }
  return readFileSync(current);
}

async function callWorker(context, request) {
  let response;
  try {
    response = await requestText(context.endpoint + ENDPOINT_PATH, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${context.token}`,
        "Content-Type": "application/json",
        Accept: "application/json",
      },
      body: JSON.stringify(request),
      timeoutMs: TIMEOUT_MS,
    });
  } catch (error) {
    if (error?.code === "timeout") {
      throw fail("worker_timeout",
                 `worker tool did not answer within ${TIMEOUT_MS}ms`);
    }
    throw fail("worker_unavailable",
               `worker service could not be reached: ${String(error?.message ?? error)}`);
  }
  let payload;
  try {
    payload = JSON.parse(response.body);
  } catch {
    throw fail("worker_unavailable",
               `worker service returned no JSON (HTTP ${response.status})`);
  }
  if (!payload || payload.ok !== true) {
    const error = payload?.error ?? {};
    throw fail(error.code ?? "tool_failed",
               error.message ?? `worker service returned HTTP ${response.status}`);
  }
  const result = payload.result;
  if (!result || typeof result !== "object") {
    throw fail("invalid_response", "worker service returned no tool result");
  }
  return result;
}

export default function nodeSystemTools(pi) {
  const context = loadContext();
  if (context === null) return;
  const granted = new Set(context.tools.map((tool) => tool.name));
  // Pi prepares every call in a response before executing any of them, so a
  // successful request_decision cannot be observed in time to block siblings.
  // The hook instead records the request and blocks everything after it.
  let yieldRequested = false;
  let yielded = false;
  pi.on("tool_call", async (event) => {
    if (!granted.has(REQUEST_TOOL)) return undefined;
    if (yielded) {
      return { block: true, reason: "decision_pending", terminate: true };
    }
    if (event.toolName === REQUEST_TOOL) {
      yieldRequested = true;
      return undefined;
    }
    if (yieldRequested) {
      return { block: true, reason: "decision_pending", terminate: true };
    }
    return undefined;
  });
  pi.on("before_provider_request", async (event, ctx) => {
    // Backstop when the batch could not terminate as a whole (for example a
    // sibling tool that had already run). The worker has yielded; another
    // provider request would run it against a decision that is not answered.
    if (!yielded) return undefined;
    ctx.abort();
    throw new Error("agentgenome-decision-pending");
  });
  for (const tool of context.tools) {
    pi.registerTool({
      name: tool.name,
      label: tool.name,
      description: tool.description,
      parameters: tool.parameters,
      execute: async (callId, params) => {
        record({ phase: "start", call_id: callId, name: tool.name,
                 effect: "idempotent", params: params ?? {},
                 worker_id: context.worker_id, node_id: context.node_id,
                 epoch: context.epoch, run_id: context.run_id });
        let output;
        try {
          const request = {
            call_id: String(callId), tool: tool.name, arguments: params ?? {},
          };
          if (tool.name === "node.publish_artifact") {
            request.content_base64 =
              readWorkspaceFile(context, params?.path).toString("base64");
          }
          const result = await callWorker(context, request);
          output = result.output;
        } catch (error) {
          const code = error?.code ?? "tool_failed";
          if (tool.name === REQUEST_TOOL) yieldRequested = false;
          record({ phase: "end", call_id: callId, name: tool.name,
                   effect: "idempotent", params: params ?? {},
                   error: { code, message: String(error?.message ?? error) } });
          return {
            content: [{ type: "text", text: JSON.stringify({
              ok: false, code, message: String(error?.message ?? error),
            }) }],
            isError: true,
          };
        }
        if (tool.name === REQUEST_TOOL &&
            (typeof output?.decision_id !== "string" || !output.decision_id)) {
          yieldRequested = false;
          record({ phase: "end", call_id: callId, name: tool.name,
                   effect: "idempotent", params: params ?? {},
                   error: { code: "invalid_response",
                            message: "decision request did not return a decision id" } });
          return {
            content: [{ type: "text", text: JSON.stringify({
              ok: false, code: "invalid_response",
            }) }],
            isError: true,
          };
        }
        record({ phase: "end", call_id: callId, name: tool.name,
                 effect: "idempotent", params: params ?? {}, result: output });
        if (tool.name === REQUEST_TOOL) {
          // The decision is committed; write the durable proof before the
          // process can exit so the Backend reports a yield, not a failure.
          yielded = true;
          writeYield(context, output);
          return {
            content: [{ type: "text", text: JSON.stringify({ ok: true, ...output }) }],
            details: output,
            terminate: true,
          };
        }
        return {
          content: [{ type: "text", text: JSON.stringify({ ok: true, ...output }) }],
          details: output,
        };
      },
    });
  }
}
