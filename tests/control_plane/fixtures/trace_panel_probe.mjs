/**
 * Panel probe: the shipped pi_trace.mjs against a real loopback HTTP/SSE
 * service and a fake Pi overlay host.
 *
 * The probe imports the shipped transport (service_http.mjs) and the official
 * pi-tui width helpers. It asserts the dual compact/expanded overlay lifecycle,
 * per-render visibility, explicit editor return, coalesced refresh with
 * preserved selection/focus/scroll, detail cursor paging and dedupe,
 * cursor_expired replacement, run switching, session-run ownership, bounded
 * initial-snapshot retry, cancellation on collapse/close, the stale response
 * guard and CJK/narrow widths. It never calls a model.
 */
import { createServer } from "node:http";
import { pathToFileURL } from "node:url";

const modulePath = process.env.AGENTGENOME_TRACE_MODULE;
const tuiEntry = process.env.AGENTGENOME_PI_TUI_ENTRY;
if (!modulePath || !tuiEntry) {
  console.error("missing AGENTGENOME_TRACE_MODULE or AGENTGENOME_PI_TUI_ENTRY");
  process.exit(2);
}

const { createTraceClient, createTracePanel } = await import(
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

const NOW = "2026-11-04T10:00:00Z";
const TOKEN = "probe-token";

function node(id, label, parent, state, round, deps = []) {
  return {
    node_id: id, label, parent_id: parent, expansion_id: null,
    local_child_id: label, template_ref: null, reviewer_ref: null,
    state, round, dependencies: deps, accepted_candidate_ref: null,
    latest_candidate_id: null, current_execution: null, revision: 1,
    updated_at: NOW,
  };
}

function snapshot(runId) {
  const root = node("root", "根任务", null, "running", 1);
  const first = node("child-a", "清洗步骤A", "root", "succeeded", 2);
  const second = node("child-b", "校验步骤B", "root", "failed", 1, ["child-a"]);
  const base = {
    format: "agentgenome-trace/1",
    service_id: "svc-probe",
    run: {
      run_id: runId, status: runId === "run-A" ? "running" : "paused",
      desired: "running", revision: 3, definition_ref: { definition_id: "def" },
      created_at: NOW, updated_at: NOW, summary: "probe",
    },
    cursor: {
      format: "agentgenome-cursor/1", token: `control-${runId}-1`,
      service_id: "svc-probe", run_id: runId, seq: 1,
    },
    filters: { node_id: null, candidate_id: null },
    tree: [root, first, second],
    decomposition: [
      { parent_id: "root", child_id: "child-a", expansion_id: "x1",
        local_child_id: "清洗步骤A" },
      { parent_id: "root", child_id: "child-b", expansion_id: "x1",
        local_child_id: "校验步骤B" },
    ],
    dependencies: [{ node_id: "child-b", depends_on: "child-a" }],
    workers: [],
    executions: [{
      worker_id: "worker-a", node_id: "child-a", epoch: 2, current: true,
      backend_ref: "pi", agent_ref: "agent", observed: null, desired: null,
      last_event_id: "e2", delivery_keys: ["delivery-a2"],
      deliveries: [{
        message_id: "m2", delivery_key: "delivery-a2", epoch: 2,
        delivery_state: "delivered", created_at: NOW, delivered_at: NOW,
      }],
      usage: { available: false }, activities_complete: null,
    }, {
      worker_id: "worker-a", node_id: "child-a", epoch: 1, current: false,
      backend_ref: "pi", agent_ref: "agent", observed: null, desired: null,
      last_event_id: null, delivery_keys: ["delivery-a1"],
      deliveries: [{
        message_id: "m1", delivery_key: "delivery-a1", epoch: 1,
        delivery_state: "delivered", created_at: NOW, delivered_at: NOW,
      }],
      usage: null, activities_complete: null,
    }],
    candidates: [{
      candidate_id: "cand-a-1", node_id: "child-a", worker_id: "worker-a",
      epoch: 2, round: 1, decision: "accepted", event_id: "e1",
      decision_event_id: "e2", output_refs: [{ artifact_id: "art-1" }],
      created_at: NOW,
    }],
    reviews: [{
      event_id: "rev-1", node_id: "child-a", candidate_id: "cand-a-1",
      verdict: "accepted",
      feedback_ref: {
        id: `sha256:${"a".repeat(64)}`, sha256: "a".repeat(64), size: 12,
        media_type: "text/plain",
      },
      reviewer: "checker", worker_id: "worker-a", epoch: 2, created_at: NOW,
    }],
    decisions: [],
    waiting: [{
      node_id: "root", state: "running", reason: "awaiting_child",
      evidence: { child_id: "child-b" },
    }],
    timeline: {
      events: [{
        seq: 1, kind: "node_succeeded", run_id: runId, node_id: "child-a",
        worker_id: "worker-a", epoch: 2, created_at: NOW,
      }],
      cursor: { token: `control-${runId}-1` }, next_cursor: 1,
      oldest_seq: 1, has_earlier: false, limit: 200, truncated: false,
    },
    availability: {
      control_events: "committed", execution_logs: "backend-owned",
      request_diagnostics: "unconfigured",
    },
  };
  if (runId !== "run-A") {
    base.tree = [root];
    base.decomposition = [];
    base.dependencies = [];
    base.executions = [{
      worker_id: "worker-b", node_id: "root", epoch: 1, current: true,
      backend_ref: "pi", agent_ref: "agent", observed: null, desired: null,
      last_event_id: "b1", delivery_keys: ["delivery-b1"],
      deliveries: [{
        message_id: "mb", delivery_key: "delivery-b1", epoch: 1,
        delivery_state: "delivered", created_at: NOW, delivered_at: NOW,
      }],
      usage: { available: false }, activities_complete: null,
    }];
    base.candidates = [];
    base.reviews = [];
    base.waiting = [];
    base.timeline.events = [];
  }
  return base;
}

function executionEntry(offset, kind, extra = {}) {
  return {
    offset, source: "pi", observed_at: NOW, kind,
    worker_id: "worker-a", epoch: 2, delivery_key: "delivery-a2", ...extra,
  };
}

const state = {
  listRuns: 0,
  snapshots: [],
  snapshotByRun: new Map(),
  snapshotFailures: new Map(),
  snapshotTerminal: new Set(),
  watchOpened: 0,
  watchClosed: 0,
  watchByRun: new Map(),
  watchStreams: new Map(),
  executionRequests: [],
  execExpired: new Set(),
  gate: null,
};

function envelope(result, operation) {
  return { ok: true, operation, result };
}
function failureEnvelope(code, message) {
  return { ok: false, operation: "execution", error: { code, message } };
}

function sendJson(res, status, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(status, {
    "Content-Type": "application/json",
    "Content-Length": Buffer.byteLength(body),
  });
  res.end(body);
}

async function handle(req, res) {
  const url = new URL(req.url, "http://127.0.0.1");
  if (req.headers.authorization !== `Bearer ${TOKEN}`) {
    sendJson(res, 401, { ok: false, error: { code: "unauthorized",
                                             message: "bad token" } });
    return;
  }
  if (req.method === "POST" && url.pathname === "/v1/operations/list_runs") {
    state.listRuns += 1;
    sendJson(res, 200, envelope({
      runs: [
        { run_id: "run-A", status: "running", created_at: NOW,
          updated_at: NOW, summary: "a" },
        { run_id: "run-B", status: "paused", created_at: "2026-11-03T10:00:00Z",
          updated_at: NOW, summary: "b" },
      ],
    }, "list_runs"));
    return;
  }
  const trace = url.pathname.match(/^\/v1\/runs\/([^/]+)\/trace$/);
  if (req.method === "GET" && trace) {
    const runId = decodeURIComponent(trace[1]);
    state.snapshots.push(runId);
    state.snapshotByRun.set(runId,
      (state.snapshotByRun.get(runId) ?? 0) + 1);
    if (state.snapshotTerminal.has(runId)) {
      sendJson(res, 404, { ok: false, error: { code: "not_found",
                                               message: "missing run" } });
      return;
    }
    const remaining = state.snapshotFailures.get(runId) ?? 0;
    if (remaining > 0) {
      state.snapshotFailures.set(runId, remaining - 1);
      sendJson(res, 503, { ok: false, error: { code: "service_unavailable",
                                               message: "temporary" } });
      return;
    }
    if (state.gate) await state.gate;
    sendJson(res, 200, envelope(snapshot(runId), "trace"));
    return;
  }
  const watch = url.pathname.match(/^\/v1\/runs\/([^/]+)\/watch$/);
  if (req.method === "GET" && watch) {
    const runId = decodeURIComponent(watch[1]);
    state.watchOpened += 1;
    state.watchByRun.set(runId, (state.watchByRun.get(runId) ?? 0) + 1);
    res.writeHead(200, {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
      Connection: "keep-alive",
    });
    res.write(": connected\n\n");
    state.watchStreams.set(runId, res);
    req.on("close", () => {
      state.watchClosed += 1;
      if (state.watchStreams.get(runId) === res) state.watchStreams.delete(runId);
    });
    return;
  }
  const exec = url.pathname.match(
    /^\/v1\/runs\/([^/]+)\/executions\/([^/]+)\/([^/]+)\/([^/]+)$/);
  if (req.method === "GET" && exec) {
    const runId = decodeURIComponent(exec[1]);
    const workerId = decodeURIComponent(exec[2]);
    const epoch = decodeURIComponent(exec[3]);
    const delivery = decodeURIComponent(exec[4]);
    const after = url.searchParams.get("after");
    state.executionRequests.push({ runId, workerId, epoch, delivery, after });
    if (runId === "run-A" && !state.execExpired.has("run-A")
        && state.executionRequests.filter((item) => item.runId === "run-A")
          .length === 1) {
      // The first detail read expires: the panel must re-read from the start.
      state.execExpired.add("run-A");
      sendJson(res, 409, failureEnvelope("cursor_expired", "detail expired"));
      return;
    }
    if (runId === "run-B" && !state.execExpired.has("run-B")) {
      state.execExpired.add("run-B");
      sendJson(res, 409, failureEnvelope("cursor_expired", "detail expired"));
      return;
    }
    let entries = [];
    let next = "exec-end";
    let hasMore = false;
    if (runId === "run-A" && after === null) {
      entries = [
        executionEntry(10, "tool_execution_start",
                       { tool: "bash", target_path: "data.csv" }),
        executionEntry(20, "tool_execution_end",
                       { tool: "bash", is_error: false, duration_ms: 120,
                         artifact_refs: [{ artifact_id: "art-1" }] }),
      ];
      next = "exec-page-2";
      hasMore = true;
    } else if (runId === "run-A" && after === "exec-page-2") {
      entries = [
        executionEntry(20, "tool_execution_end",
                       { tool: "bash", is_error: false, duration_ms: 120,
                         artifact_refs: [{ artifact_id: "art-1" }] }),
        executionEntry(30, "message_end",
                       { role: "assistant", usage_available: false,
                         stop_reason: "end" }),
        executionEntry(40, "tool_execution_start",
                       { tool: "python", target_path: "clean.py" }),
        executionEntry(50, "tool_execution_end",
                       { tool: "python", is_error: false, duration_ms: 7,
                         artifact_refs: [{ artifact_id: "art-2" }] }),
        executionEntry(60, "message_end",
                       { role: "assistant", usage_available: false,
                         stop_reason: "end" }),
        executionEntry(70, "tool_execution_start",
                       { tool: "grep", target_path: "report.csv" }),
      ];
    } else if (runId === "run-B") {
      entries = [{
        offset: 5, source: "pi", observed_at: NOW, kind: "tool_execution_end",
        tool: "bash", is_error: true,
        error: { message: "permission denied: /secret\n(exit 1)" },
        worker_id: "worker-b", epoch: 1, delivery_key: "delivery-b1",
      }];
    }
    sendJson(res, 200, envelope({
      format: "agentgenome-execution-trace/1",
      availability: "available",
      entries,
      next_cursor: { format: "agentgenome-execution-cursor/1",
                     token: next },
      has_more: hasMore,
      partial: false,
      detail: {},
      limit: 100,
    }, "execution"));
    return;
  }
  sendJson(res, 404, { ok: false, error: { code: "not_found",
                                           message: url.pathname } });
}

function pushControl(runId, token) {
  const stream = state.watchStreams.get(runId);
  if (!stream) return false;
  stream.write(`id: ${token}\nevent: control\ndata: {}\n\n`);
  return true;
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
    focusCalls: 0,
    unfocusCalls: 0,
    hidden: false,
    focus() { this.focused = true; this.focusCalls += 1; },
    unfocus() { this.focused = false; this.unfocusCalls += 1; },
    isFocused() { return this.focused; },
    setHidden(value) { this.hidden = value === true; },
    isHidden() { return this.hidden; },
    hide() { this.hidden = true; },
  };
}

function host() {
  const notices = [];
  const hostState = { renders: 0, customCalls: 0 };
  const overlays = [];
  const tui = { requestRender() { hostState.renders += 1; } };
  const theme = {
    fg(_token, text) { return text; },
    bg(_token, text) { return text; },
    style(_text, _options) { return ""; },
  };
  const keybindings = {
    matches(data, name) { return KEY[name] === data; },
  };
  const ui = {
    notify(message, type = "info") {
      notices.push({ message: String(message), type });
    },
    custom(factory, options) {
      hostState.customCalls += 1;
      const entry = {
        options,
        handle: makeHandle(),
        component: null,
        resolve: null,
        doneCalled: false,
        doneResult: undefined,
      };
      const promise = new Promise((resolve) => { entry.resolve = resolve; });
      entry.component = factory(tui, theme, keybindings, (result) => {
        entry.doneCalled = true;
        entry.doneResult = result;
        entry.resolve(result);
      });
      options.onHandle?.(entry.handle);
      overlays.push(entry);
      return promise;
    },
  };
  const context = { mode: "tui", ui, cwd: "/workspace/probe" };
  return { context, notices, hostState, overlays };
}

const server = createServer((req, res) => {
  handle(req, res).catch((error) => {
    try {
      sendJson(res, 500, { ok: false, error: { code: "probe",
                                               message: String(error) } });
    } catch {
      // Response may already be finished.
    }
  });
});
await new Promise((resolve, reject) => {
  server.once("error", reject);
  server.listen(0, "127.0.0.1", resolve);
});
const { port } = server.address();
const endpoint = `http://127.0.0.1:${port}`;

let panel = null;
const client = createTraceClient({ endpoint, token: TOKEN });

try {
  const { context, hostState, overlays } = host();
  panel = createTracePanel({
    client, helpers, mode: "tui", ui: context.ui,
    notify: (message, type) => context.ui.notify(message, type),
  });

  // ------------------------------------------------------------ dual overlay
  check("mount starts both width-class overlays",
    panel.mount() === true && hostState.customCalls === 2,
    String(hostState.customCalls));
  const compactOverlay = overlays[0];
  const expandedOverlay = overlays[1];
  const compactOptions = compactOverlay.options.overlayOptions();
  const expandedOptions = expandedOverlay.options.overlayOptions();
  check("compact entry is a bounded numeric overlay at the right edge",
    typeof compactOptions.width === "number"
    && compactOptions.width >= 18 && compactOptions.width <= 38
    && compactOptions.anchor === "top-right"
    && compactOptions.nonCapturing === true);
  check("expanded panel uses Pi's percentage width class",
    expandedOptions.width === "60%" && expandedOptions.minWidth === 20
    && expandedOptions.maxHeight === "80%"
    && expandedOptions.anchor === "top-right"
    && expandedOptions.nonCapturing === true);
  check("per-render visibility selects exactly one overlay",
    compactOptions.visible() === true && expandedOptions.visible() === false);

  await waitFor(() => panel.runsLoaded, "run list");
  check("collapsed entry falls back to the newest listed run",
    panel.selectedRunId === "run-A", String(panel.selectedRunId));
  check("collapsed entry starts no watch", state.watchOpened === 0,
    String(state.watchOpened));
  check("initial collapsed mount releases focus",
    compactOverlay.handle.isFocused() === false
    && compactOverlay.handle.focusCalls === 0);
  const compact = panel.render(30);
  check("compact entry renders one bounded line", compact.length === 1
    && visibleWidth(compact[0]) <= 30, JSON.stringify(compact));
  check("compact entry names the run and status",
    compact[0].includes("AgentGenome") && compact[0].includes("running"),
    compact[0]);

  // --------------------------------------------------------------- expand
  const press = compactOverlay.component.handleMouse(
    { type: "press", button: "left", x: 1, y: 0 });
  check("press expands and focuses the expanded overlay",
    press && press.handled === true && panel.expanded === true
    && expandedOverlay.handle.isFocused() === true);
  check("expand flips per-render visibility",
    compactOptions.visible() === false && expandedOptions.visible() === true);
  check("release does not double-toggle",
    compactOverlay.component.handleMouse(
      { type: "release", button: "left", x: 1, y: 0 }) === undefined
    && panel.expanded === true);
  await waitFor(() => panel.snapshot !== null && state.watchOpened === 1,
                "snapshot and watch");
  check("snapshot without execution rows reports unavailable detail",
    panel.selectedNodeId === "root"
    && panel.detail === null, String(panel.selectedNodeId));

  const expanded = panel.render(60);
  check("expanded view renders a bounded panel",
    expanded.length >= 4 && expanded.length <= 80
    && expanded.every((line) => visibleWidth(line) <= 60));
  check("expanded view separates tree and dependencies",
    expanded.join("\n").includes("树") && expanded.join("\n").includes("依赖"));

  // --------------------------------------------------------------- details
  panel.handleInput(KEY["tui.select.down"]);
  await waitFor(() => panel.detail?.entries.length === 7, "child detail");
  await waitFor(() => panel.detail?.hasMore === false, "detail fully paged");
  const offsets = panel.detail.entries.map((entry) => entry.offset);
  check("detail dedupes by source and offset", offsets.length === 7
    && String(offsets) === "10,20,30,40,50,60,70", String(offsets));
  check("detail keeps full worker/epoch/delivery identity",
    panel.detail.identity.workerId === "worker-a"
    && panel.detail.identity.epoch === 2
    && panel.detail.identity.deliveryKey === "delivery-a2");
  const detailText = panel.render(60).join("\n");
  check("detail shows execution identity and the newest entries",
    detailText.includes("worker-a") && detailText.includes("delivery-a2")
    && detailText.includes("python") && detailText.includes("7ms"),
    detailText);
  check("detail window hides older entries until scrolled",
    !detailText.includes("120ms"), detailText);
  check("review feedback shows the artifact id, not a placeholder",
    detailText.includes("accepted/…aaaaaaaa")
    && !detailText.includes("accepted/—"), detailText);

  // ------------------------------------------------------ editor return
  panel.handleInput("\u0005");
  check("Ctrl+E releases focus without collapsing",
    panel.expanded === true && expandedOverlay.handle.isFocused() === false
    && expandedOverlay.handle.unfocusCalls === 1);
  panel.expand();
  check("expand can re-focus the same mounted overlay",
    expandedOverlay.handle.isFocused() === true
    && expandedOverlay.handle.focusCalls === 2);
  panel.render(60);
  const editorRow = panel.rowMap.findIndex((row) => row?.kind === "editor");
  check("expanded view exposes a clickable editor row", editorRow >= 0);
  const editorPress = expandedOverlay.component.handleMouse(
    { type: "press", button: "left", x: 1, y: editorRow });
  check("clicking the editor row returns focus",
    editorPress?.handled === true && editorPress?.focus === undefined
    && expandedOverlay.handle.isFocused() === false);

  // Scroll one entry back so the older bash entry becomes visible.
  panel.scrollDetail(1);
  const scrollBefore = panel.detailScroll;
  const focusBefore = expandedOverlay.handle.focusCalls;
  state.snapshots.length = 0;
  check("detail window shows earlier entries after scrolling",
    panel.render(60).join("\n").includes("120ms"));

  // ------------------------------------------- coalesced control refresh
  check("watch control frame is delivered", pushControl("run-A", "control-A-2"));
  await waitFor(() => state.snapshots.filter((id) => id === "run-A").length >= 1,
                "coalesced snapshot refresh");
  await sleep(100);
  check("refresh preserves node selection",
    panel.selectedNodeId === "child-a", String(panel.selectedNodeId));
  check("refresh preserves scroll", panel.detailScroll === scrollBefore
    && scrollBefore === 1);
  check("refresh never steals focus",
    expandedOverlay.handle.focusCalls === focusBefore);

  // ------------------------------------------------- tree/dependency view
  panel.handleInput("t");
  const depsView = panel.render(60).join("\n");
  check("dependency view shows edges", depsView.includes("← 清洗步骤A")
    || depsView.includes("← child-a"), depsView);
  panel.handleInput("t");

  // --------------------------------------------------------- run switch
  const openedBefore = state.watchOpened;
  panel.handleInput("r");
  check("run picker renders existing runs",
    panel.render(60).join("\n").includes("选择运行"));
  panel.handleInput(KEY["tui.select.down"]);
  panel.handleInput(KEY["tui.select.confirm"]);
  await waitFor(() => panel.selectedRunId === "run-B"
    && panel.snapshot?.run?.run_id === "run-B"
    && state.watchOpened === openedBefore + 1, "run switch");
  await waitFor(() => panel.detail?.entries.length === 1, "run B detail");
  check("a run switch resets selection to the new tree",
    panel.selectedNodeId === "root");
  check("detail cursor_expired re-reads and replaces the snapshot",
    state.execExpired.has("run-B") && panel.detail.entries.length === 1
    && panel.detail.entries[0].offset === 5);
  const failedText = panel.render(60).join("\n");
  check("a failed tool shows the bounded service error on one line",
    failedText.includes("失败")
    && failedText.includes("permission denied: /secret (exit 1)")
    && !failedText.includes("permission denied: /secret\n"),
    failedText);
  check("the picker switch pins the manual run",
    panel.manualRunId === "run-B");

  // ------------------------------------------- session ownership / pin
  const beforePin = state.watchOpened;
  panel.selectRun("run-A", { manual: true });
  await waitFor(() => panel.selectedRunId === "run-A"
    && panel.snapshot?.run?.run_id === "run-A"
    && state.watchOpened === beforePin + 1, "manual run selection");
  panel.noteSessionRun("run-B");
  await sleep(100);
  check("a manual pin survives a later session run",
    panel.selectedRunId === "run-A" && panel.manualRunId === "run-A"
    && panel.sessionRunId === "run-B");

  // ------------------------------------------- collapse aborts watch
  const watchClosedBefore = state.watchClosed;
  panel.render(60);
  expandedOverlay.component.handleMouse(
    { type: "press", button: "left", x: 1, y: 0 });
  check("collapse closes the expanded view and aborts its watch",
    panel.expanded === false && panel.watchAbort === null
    && expandedOverlay.handle.isFocused() === false);
  await waitFor(() => state.watchClosed > watchClosedBefore, "watch abort");
  check("collapse renders the compact entry again",
    panel.render(30).length === 1
    && compactOptions.visible() === true
    && expandedOptions.visible() === false);

  // ------------------------------------ stale response after collapse
  let releaseGate = null;
  state.gate = new Promise((resolve) => { releaseGate = resolve; });
  const watchBeforeRace = state.watchOpened;
  const snapshotsBeforeRace = state.snapshotByRun.get("run-A") ?? 0;
  compactOverlay.component.handleMouse(
    { type: "press", button: "left", x: 1, y: 0 });
  await waitFor(() => (state.snapshotByRun.get("run-A") ?? 0)
    > snapshotsBeforeRace, "gated snapshot request");
  panel.render(30);
  compactOverlay.component.handleMouse(
    { type: "press", button: "left", x: 1, y: 0 });
  const staleSnapshot = panel.snapshot;
  releaseGate();
  state.gate = null;
  await sleep(150);
  check("stale snapshot never resurrects a closed view",
    panel.expanded === false && panel.snapshot === staleSnapshot
    && state.watchOpened === watchBeforeRace);

  // --------------------------------------- narrow and CJK widths
  compactOverlay.component.handleMouse(
    { type: "press", button: "left", x: 1, y: 0 });
  await waitFor(() => panel.snapshot !== null && panel.expanded === true
    && panel.connection.state === "connected", "re-expand");
  const narrow = panel.render(26);
  check("narrow width stays within the terminal",
    narrow.every((line) => visibleWidth(line) <= 26),
    JSON.stringify(narrow));
  check("narrow width keeps a readable detail view",
    narrow.join("\n").includes("窄屏"), narrow.join("\n"));
  const wider = panel.render(44);
  check("CJK labels truncate by display columns",
    wider.every((line) => visibleWidth(line) <= 44),
    JSON.stringify(wider));

  panel.close();
  check("close resolves both overlay promises",
    compactOverlay.doneCalled === true && expandedOverlay.doneCalled === true);
  check("close stops timers and clears session ownership",
    panel.clock === null && panel.watchAbort === null
    && panel.refreshTimer === null && panel.sessionRunId === null
    && panel.manualRunId === null);
  check("close hides both width classes",
    compactOptions.visible() === false && expandedOptions.visible() === false);

  // ------------------------------------ initial snapshot retry (run B)
  state.snapshotFailures.set("run-B", 1);
  const retryHost = host();
  const retryPanel = createTracePanel({
    client, helpers, mode: "tui", ui: retryHost.context.ui,
    notify: (message, type) => retryHost.context.ui.notify(message, type),
  });
  check("retry panel mounts both overlays", retryPanel.mount() === true
    && retryHost.hostState.customCalls === 2);
  await waitFor(() => retryPanel.runsLoaded, "retry run list");
  const watchBBase = state.watchByRun.get("run-B") ?? 0;
  retryPanel.noteSessionRun("run-B");
  await waitFor(() => retryPanel.selectedRunId === "run-B", "session run pick");
  check("session run selects a run that is not the newest fallback",
    retryPanel.selectedRunId === "run-B"
    && (state.watchByRun.get("run-B") ?? 0) === watchBBase);
  const retrySnapshots = state.snapshotByRun.get("run-B") ?? 0;
  retryPanel.render(30);
  retryHost.overlays[0].component.handleMouse(
    { type: "press", button: "left", x: 1, y: 0 });
  await waitFor(() => retryPanel.connection.state === "reconnecting",
                "snapshot retry status");
  check("temporary snapshot failure reports a bounded retry",
    retryPanel.connection.message.includes("秒后重试")
    && retryPanel.snapshot === null,
    retryPanel.connection.message);
  await waitFor(() => retryPanel.snapshot?.run?.run_id === "run-B"
    && retryPanel.connection.state === "connected"
    && (state.snapshotByRun.get("run-B") ?? 0) > retrySnapshots,
    "snapshot retry recovery");
  check("bounded retry recovers the initial snapshot",
    state.snapshotFailures.get("run-B") === 0
    && (state.watchByRun.get("run-B") ?? 0) >= 1);

  // ------------------------------------- terminal snapshot error
  state.snapshotTerminal.add("run-A");
  retryPanel.render(60);
  retryHost.overlays[1].component.handleMouse(
    { type: "press", button: "left", x: 1, y: 0 });
  retryPanel.selectRun("run-A", { manual: true });
  const runABefore = state.snapshotByRun.get("run-A") ?? 0;
  retryPanel.render(30);
  retryHost.overlays[0].component.handleMouse(
    { type: "press", button: "left", x: 1, y: 0 });
  await waitFor(() => retryPanel.connection.state === "disconnected",
                "terminal snapshot error");
  check("auth/not-found ends the retry loop explicitly",
    retryPanel.snapshot === null && retryPanel.opening === false
    && (state.snapshotByRun.get("run-A") ?? 0) === runABefore + 1,
    String(state.snapshotByRun.get("run-A")));
  await sleep(700);
  check("terminal snapshot error does not retry again",
    (state.snapshotByRun.get("run-A") ?? 0) === runABefore + 1);

  // ------------------------------ collapse/close cancel pending retry
  retryPanel.render(60);
  retryHost.overlays[1].component.handleMouse(
    { type: "press", button: "left", x: 1, y: 0 });
  retryPanel.selectRun("run-B", { manual: true });
  state.snapshotFailures.set("run-B", 100);
  const runBBefore = state.snapshotByRun.get("run-B") ?? 0;
  retryPanel.render(30);
  retryHost.overlays[0].component.handleMouse(
    { type: "press", button: "left", x: 1, y: 0 });
  await waitFor(() => (state.snapshotByRun.get("run-B") ?? 0) > runBBefore,
                "pending retry request");
  retryPanel.render(60);
  retryHost.overlays[1].component.handleMouse(
    { type: "press", button: "left", x: 1, y: 0 });
  const afterCollapse = state.snapshotByRun.get("run-B") ?? 0;
  await sleep(700);
  check("collapse cancels the pending retry",
    retryPanel.expanded === false
    && (state.snapshotByRun.get("run-B") ?? 0) === afterCollapse);

  retryPanel.render(30);
  retryHost.overlays[0].component.handleMouse(
    { type: "press", button: "left", x: 1, y: 0 });
  await waitFor(() => (state.snapshotByRun.get("run-B") ?? 0) > afterCollapse,
                "second retry request");
  const beforeClose = state.snapshotByRun.get("run-B") ?? 0;
  retryPanel.close();
  await sleep(900);
  check("close cancels the pending retry without resurrection",
    retryPanel.expanded === false
    && (state.snapshotByRun.get("run-B") ?? 0) === beforeClose
    && retryPanel.sessionRunId === null && retryPanel.manualRunId === null);
} finally {
  if (panel && !panel.closed) panel.close();
  for (const stream of state.watchStreams.values()) stream.end();
  await new Promise((resolve) => server.close(resolve));
}

process.stdout.write(JSON.stringify({ ok: true, checks }) + "\n");
