/**
 * Pre-call budget guard for the Pi backend.
 *
 * Loaded as its own extension next to the tool profile, so every profile gets
 * the same gate. A provider request is reserved before it can leave the
 * process; a tool call is reserved before its execute() can run. The status
 * file is written atomically after every reservation, so a killed process
 * still leaves honest (conservative) counts.
 *
 * Counts are per delivery; the Backend supplies the cumulative usage of
 * earlier deliveries in "prior" and combines both.
 */
import {
  closeSync, constants, fsyncSync, openSync, readFileSync, renameSync, writeSync,
} from "node:fs";

const FILE = process.env.AGENTGENOME_BUDGET_FILE;
const FORMAT = "agentgenome-pi-budget/1";
const ZERO = { provider_calls: 0, tool_calls: 0 };

function load() {
  if (!FILE) return null;
  let state = null;
  try {
    state = JSON.parse(readFileSync(FILE, "utf8"));
  } catch {
    state = null;
  }
  if (!state || state.format !== FORMAT) return null;
  state.prior = { ...ZERO, ...(state.prior ?? {}) };
  state.caps = state.caps ?? {};
  state.counts = { ...ZERO, ...(state.counts ?? {}) };
  state.budget = Array.isArray(state.budget) ? state.budget : [];
  state.budget_exceeded = state.budget_exceeded === true;
  return state;
}

function save(state) {
  state.extension = true;
  const temporary = `${FILE}.${process.pid}.tmp`;
  const fd = openSync(temporary,
                      constants.O_WRONLY | constants.O_CREAT | constants.O_TRUNC, 0o600);
  try {
    writeSync(fd, JSON.stringify(state));
    fsyncSync(fd);
  } finally {
    closeSync(fd);
  }
  renameSync(temporary, FILE);
}

function total(state, name) {
  return (state.prior[name] ?? 0) + (state.counts[name] ?? 0);
}

function reserve(state, name) {
  const cap = state.caps[name];
  if (typeof cap === "number" && total(state, name) >= cap) {
    if (!state.budget.includes(name)) state.budget.push(name);
    state.budget_exceeded = true;
    save(state);
    return false;
  }
  state.counts[name] = (state.counts[name] ?? 0) + 1;
  save(state);
  return true;
}

export default function budgetGuard(pi) {
  const state = load();
  if (!state) return;
  pi.on("before_provider_request", async (event, ctx) => {
    if (reserve(state, "provider_calls")) return;
    ctx.abort();
    throw new Error("agentgenome-budget-exhausted");
  });
  pi.on("tool_call", async () => {
    if (reserve(state, "tool_calls")) return undefined;
    return { block: true, reason: "budget_exhausted", terminate: true };
  });
}
