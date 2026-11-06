/**
 * Agent Worker tool profile for the Pi backend.
 *
 * Identity (worker/node/epoch/run) is injected by the Backend through the
 * environment, never by model arguments. Every effect is recorded with fsync
 * before and after execution so a killed process leaves an honest trace.
 *
 * Paths are confined to the worker workspace by real path, not by string
 * prefix: no absolute path, no `..`, no symlinked parent or final component.
 */
import { createHash } from "node:crypto";
import {
  closeSync, constants, fsyncSync, lstatSync, mkdirSync, openSync, readFileSync,
  realpathSync, renameSync, statSync, writeSync,
} from "node:fs";
import { dirname, isAbsolute, join, relative, resolve, sep } from "node:path";

const RECEIPT = process.env.AGENTGENOME_TOOL_RECEIPT;
const WORKSPACE = process.env.AGENTGENOME_WORKSPACE;
const MAX_BYTES = 512 * 1024;

function fail(code, message) {
  throw Object.assign(new Error(message), { code });
}

function identity() {
  return {
    worker_id: process.env.AGENTGENOME_WORKER_ID ?? null,
    node_id: process.env.AGENTGENOME_NODE_ID ?? null,
    epoch: Number(process.env.AGENTGENOME_EPOCH ?? 0),
    run_id: process.env.AGENTGENOME_RUN_ID ?? null,
  };
}

function record(entry) {
  if (!RECEIPT) throw fail("tool_receipt_unconfigured", "tool receipt path is not configured");
  const line = JSON.stringify({ ...identity(), ...entry, at: new Date().toISOString() }) + "\n";
  const fd = openSync(RECEIPT, "a");
  try {
    writeSync(fd, line);
    fsyncSync(fd);
  } finally {
    closeSync(fd);
  }
}

function requireString(value, label) {
  if (typeof value !== "string" || value.length === 0) throw fail("invalid_arguments", `${label} must be a non-empty string`);
  return value;
}

function workspaceRoot() {
  if (!WORKSPACE) throw fail("workspace_unconfigured", "worker workspace is not configured");
  let root;
  try {
    root = realpathSync(WORKSPACE);
  } catch {
    throw fail("workspace_unconfigured", "worker workspace does not exist");
  }
  return root;
}

function inside(root, candidate) {
  return candidate === root || candidate.startsWith(root + sep);
}

function safePath(value) {
  const text = requireString(value, "path");
  if (isAbsolute(text) || text.split(/[\\/]/).some((part) => part === "..")) {
    throw fail("path_outside_workspace", "path must stay inside the worker workspace");
  }
  const root = workspaceRoot();
  const full = resolve(root, text);
  if (!inside(root, full)) {
    throw fail("path_outside_workspace", "path must stay inside the worker workspace");
  }
  return { root, full };
}

function requireNoSymlink(root, target) {
  const parts = relative(root, target).split(sep).filter(Boolean);
  let current = root;
  for (const part of parts) {
    current = join(current, part);
    let info;
    try {
      info = lstatSync(current);
    } catch (error) {
      if (error?.code === "ENOENT") return;
      throw error;
    }
    if (info.isSymbolicLink()) {
      throw fail("path_symlink", "symlinks are not allowed inside the worker workspace");
    }
  }
}

function readPath(value) {
  const { root, full } = safePath(value);
  requireNoSymlink(root, full);
  let real;
  try {
    real = realpathSync(full);
  } catch (error) {
    if (error?.code === "ENOENT") throw fail("path_not_found", "file does not exist");
    throw error;
  }
  if (!inside(root, real)) {
    throw fail("path_outside_workspace", "path must stay inside the worker workspace");
  }
  const info = statSync(real);
  if (!info.isFile()) throw fail("path_not_file", "path must name a regular file");
  return { root, full: real };
}

function writePath(value) {
  const { root, full } = safePath(value);
  const parent = dirname(full);
  const parts = relative(root, parent).split(sep).filter(Boolean);
  let current = root;
  for (const part of parts) {
    current = join(current, part);
    let info;
    try {
      info = lstatSync(current);
    } catch (error) {
      if (error?.code !== "ENOENT") throw error;
      mkdirSync(current);
      continue;
    }
    if (info.isSymbolicLink()) {
      throw fail("path_symlink", "symlinks are not allowed inside the worker workspace");
    }
    if (!info.isDirectory()) {
      throw fail("path_not_directory", "a parent path component is not a directory");
    }
  }
  const realParent = realpathSync(parent);
  if (!inside(root, realParent)) {
    throw fail("path_outside_workspace", "path must stay inside the worker workspace");
  }
  let existing;
  try {
    existing = lstatSync(full);
  } catch (error) {
    if (error?.code !== "ENOENT") throw error;
    existing = null;
  }
  if (existing?.isSymbolicLink()) {
    throw fail("path_symlink", "symlinks are not allowed inside the worker workspace");
  }
  if (existing && !existing.isFile()) {
    throw fail("path_not_file", "path must name a regular file");
  }
  return { root, full };
}

function writeAtomic(path, text) {
  const temporary = `${path}.tmp-${process.pid}-${Date.now()}`;
  let fd;
  try {
    fd = openSync(temporary,
                  constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW,
                  0o600);
  } catch (error) {
    if (error?.code === "ELOOP") {
      throw fail("path_symlink", "symlinks are not allowed inside the worker workspace");
    }
    throw error;
  }
  try {
    writeSync(fd, text);
    fsyncSync(fd);
  } finally {
    closeSync(fd);
  }
  renameSync(temporary, path);
}

function toolFailure(callId, name, effect, params, error) {
  const code = error?.code ?? "tool_error";
  record({ phase: "end", call_id: callId, name, effect, params, error: { code, message: String(error?.message ?? error) } });
  return { content: [{ type: "text", text: JSON.stringify({ ok: false, code }) }], isError: true };
}

export default function controlPlaneTools(pi) {
  const tools = [
    {
      name: "write_file",
      effect: "idempotent",
      description: "Write UTF-8 text to a file. path is relative to the workspace root, for example dataset.json (not workspace/dataset.json). Same content produces the same bytes.",
      parameters: {
        type: "object",
        properties: { path: { type: "string" }, text: { type: "string" } },
        required: ["path", "text"],
        additionalProperties: false,
      },
      resolvePath: writePath,
      run(params, full) {
        writeAtomic(full, params.text);
        const bytes = Buffer.from(params.text, "utf8");
        return { path: params.path, sha256: createHash("sha256").update(bytes).digest("hex"), bytes: bytes.length };
      },
    },
    {
      name: "read_file",
      effect: "read_only",
      description: "Read a UTF-8 text file. path is relative to the workspace root, for example inputs/page.json.",
      parameters: {
        type: "object",
        properties: { path: { type: "string" } },
        required: ["path"],
        additionalProperties: false,
      },
      resolvePath: readPath,
      run(params, full) {
        const bytes = readFileSync(full);
        if (bytes.length > MAX_BYTES) throw fail("material_too_large", `file exceeds ${MAX_BYTES} bytes`);
        const text = bytes.toString("utf8");
        return { path: params.path, text, sha256: createHash("sha256").update(bytes).digest("hex"), bytes: bytes.length };
      },
    },
  ];

  for (const tool of tools) {
    pi.registerTool({
      name: tool.name,
      label: tool.name,
      description: tool.description,
      parameters: tool.parameters,
      execute: async (callId, params) => {
        let full;
        try {
          full = tool.resolvePath(params.path).full;
          record({ phase: "start", call_id: callId, name: tool.name, effect: tool.effect, params, target: params.path });
          const result = tool.run(params, full, callId, tool.name, tool.effect);
          record({ phase: "end", call_id: callId, name: tool.name, effect: tool.effect, params, target: params.path, result });
          return { content: [{ type: "text", text: JSON.stringify({ ok: true, ...result }) }], details: result };
        } catch (error) {
          return toolFailure(callId, tool.name, tool.effect, params, error);
        }
      },
    });
  }
}
