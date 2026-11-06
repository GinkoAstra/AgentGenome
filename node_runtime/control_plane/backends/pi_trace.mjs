/**
 * AgentGenome trace panel for the ordinary Pi terminal.
 *
 * The panel is a read-only client of the same running service as the Teacher
 * tools. It reads the P3a HTTP snapshot/execution routes and follows the same
 * event stream over SSE; it never reads service files, keeps no local state
 * database and never asks a model to refresh the view.
 *
 * Pi owns the overlay lifecycle. The collapsed entry is a non-capturing
 * overlay so activation never steals the editor; the expanded panel requests
 * focus only after the user clicks it or runs `/agentgenome trace`. All width
 * handling uses the helpers injected by the installed Pi extension wrapper.
 */
import { requestText, streamSse } from "./service_http.mjs";

const REFRESH_DELAY_MS = 150;
const WATCH_IDLE_MS = 45000;
const STALE_AFTER_MS = 35000;
const RECONNECT_MAX_MS = 5000;
const RUN_LIMIT = 50;
const MAX_NODE_ROWS = 10;
const MAX_RUN_ROWS = 10;
const MAX_LOG_LINES = 5;
const MAX_TIMELINE_LINES = 3;
const MAX_DETAIL_ENTRIES = 400;
const MAX_DETAIL_PAGES = 3;

function failure(code, message, extra = {}) {
  const error = new Error(message);
  error.code = code;
  Object.assign(error, extra);
  return error;
}

function envelopeResult(response, operation) {
  let payload = null;
  try {
    payload = JSON.parse(response.body ?? "");
  } catch {
    throw failure("transport_protocol_error",
                  `${operation} returned a non-JSON response`,
                  { status: response.status });
  }
  if (!payload || typeof payload !== "object" || typeof payload.ok !== "boolean") {
    throw failure("transport_protocol_error",
                  `${operation} returned an unexpected envelope`,
                  { status: response.status });
  }
  if (payload.ok !== true) {
    const error = payload.error && typeof payload.error === "object"
      ? payload.error : {};
    throw failure(error.code ?? "service_error",
                  String(error.message ?? `${operation} failed`),
                  { status: response.status, details: error.details ?? null });
  }
  return payload.result;
}

function transportFailure(operation, error) {
  return failure(
    error?.code === "timeout" ? "transport_timeout" : "transport_unavailable",
    `${operation}: ${String(error?.message ?? error)}`);
}

/**
 * HTTP/SSE client for the trace routes. `request`/`stream` default to the
 * shipped loopback transport; tests inject the same module, never a second
 * network implementation.
 */
export function createTraceClient({
  endpoint, token, timeoutMs = 30000, idleTimeoutMs = WATCH_IDLE_MS,
  request = requestText, stream = streamSse,
} = {}) {
  if (typeof endpoint !== "string" || endpoint.length === 0) {
    throw failure("invalid_config", "trace client needs a service endpoint");
  }
  if (typeof token !== "string" || token.length === 0) {
    throw failure("invalid_config", "trace client needs a service credential");
  }
  const base = endpoint.replace(/\/+$/, "");
  const auth = { Authorization: `Bearer ${token}` };

  function target(path, params) {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params ?? {})) {
      if (value !== null && value !== undefined) query.set(key, String(value));
    }
    const suffix = query.toString();
    return `${base}${path}${suffix ? `?${suffix}` : ""}`;
  }

  async function get(path, params) {
    let response;
    try {
      response = await request(target(path, params), {
        headers: { ...auth, Accept: "application/json" },
        timeoutMs,
      });
    } catch (error) {
      throw transportFailure(path, error);
    }
    return envelopeResult(response, path);
  }

  async function operation(name, arguments_) {
    let response;
    try {
      response = await request(
        target(`/v1/operations/${encodeURIComponent(name)}`),
        {
          method: "POST",
          headers: { ...auth, "Content-Type": "application/json",
                     Accept: "application/json" },
          body: JSON.stringify(arguments_ ?? {}),
          timeoutMs,
        });
    } catch (error) {
      throw transportFailure(name, error);
    }
    return envelopeResult(response, name);
  }

  function executionPath(runId, identity) {
    return `/v1/runs/${encodeURIComponent(runId)}/executions/`
      + `${encodeURIComponent(identity.workerId)}/${encodeURIComponent(identity.epoch)}/`
      + `${encodeURIComponent(identity.deliveryKey)}`;
  }

  return {
    async listRuns({ limit = RUN_LIMIT } = {}) {
      const result = await operation("list_runs", { limit });
      return Array.isArray(result?.runs) ? result.runs : [];
    },
    async snapshot(runId) {
      return get(`/v1/runs/${encodeURIComponent(runId)}/trace`);
    },
    async execution(runId, identity, { after = null, limit = 100 } = {}) {
      return get(executionPath(runId, identity), { after, limit });
    },
    watchFrames(runId, after, signal) {
      return stream(
        target(`/v1/runs/${encodeURIComponent(runId)}/watch`, { after }),
        {
          headers: { ...auth, Accept: "text/event-stream" },
          timeoutMs: idleTimeoutMs,
          signal,
        });
    },
  };
}

function shortId(value, length = 8) {
  if (typeof value !== "string" || value.length === 0) return "—";
  return value.length > length ? value.slice(0, length) : value;
}

// Committed artifact refs are objects; show the real id, tail-first for
// content-addressed ids so different artifacts do not collapse to "sha256:".
function artifactLabel(value) {
  if (typeof value === "string" && value.length > 0) return shortId(value, 14);
  if (!value || typeof value !== "object") return null;
  const id = typeof value.id === "string" && value.id.length > 0
    ? value.id
    : (typeof value.artifact_id === "string" && value.artifact_id.length > 0
      ? value.artifact_id : null);
  if (id === null) return null;
  if (id.startsWith("sha256:") && id.length > 15) return `…${id.slice(-8)}`;
  return shortId(id, 14);
}

// Service-provided error summaries are already sanitized and bounded; keep the
// rendered line single-line and short.
function clipText(value, limit = 120) {
  const text = String(value ?? "").replace(/\s+/g, " ").trim();
  if (text.length === 0) return "";
  return text.length > limit ? `${text.slice(0, limit - 1)}…` : text;
}

function jsonShort(value, limit = 36) {
  if (value === null || value === undefined) return "不可用";
  let text;
  try {
    text = JSON.stringify(value);
  } catch {
    text = String(value);
  }
  if (text === undefined) return "不可用";
  return text.length > limit ? `${text.slice(0, limit - 1)}…` : text;
}

function clockOf(iso) {
  if (typeof iso !== "string" || iso.length < 19) return "--:--:--";
  return iso.slice(11, 19);
}

function stateColor(state) {
  if (state === "succeeded" || state === "accepted") return "success";
  if (state === "failed" || state === "cancelled" || state === "rejected") {
    return "error";
  }
  if (state === "paused" || state === "waiting_decision") return "warning";
  return "dim";
}

function entryLine(entry) {
  const at = clockOf(entry.observed_at);
  const kind = String(entry.kind ?? "unknown");
  if (kind === "tool_execution_end") {
    const failed = entry.is_error === true;
    const state = failed ? "失败" : "完成";
    const duration = entry.duration_ms !== null && entry.duration_ms !== undefined
      ? ` ${entry.duration_ms}ms` : "";
    const refs = !failed && Array.isArray(entry.artifact_refs)
      ? ` refs=${entry.artifact_refs.length}` : "";
    const error = failed && typeof entry.error?.message === "string"
      ? ` · ${clipText(entry.error.message)}` : "";
    return `${at} ${entry.tool ?? "tool"} ${state}${duration}${refs}${error}`;
  }
  if (kind === "tool_execution_start") {
    const path = typeof entry.target_path === "string"
      ? ` ${entry.target_path}` : "";
    return `${at} → ${entry.tool ?? "tool"}${path}`;
  }
  if (kind === "message_end") {
    const usage = entry.usage_available === true
      ? ` usage ${jsonShort(entry.usage, 30)}` : " usage 不可用";
    const stop = entry.stop_reason ? ` ${entry.stop_reason}` : "";
    return `${at} ${entry.role ?? "message"}${stop}${usage}`;
  }
  if (kind === "gap" || kind === "unparsed") {
    return `${at} ${kind} ${entry.reason ?? entry.line_bytes ?? ""}`.trim();
  }
  const detail = entry.error?.message ? ` 错误: ${entry.error.message}` : "";
  return `${at} ${kind}${detail}`;
}

function timelineLine(event) {
  const node = event.node_id ? ` node=${shortId(event.node_id, 10)}` : "";
  const worker = event.worker_id ? ` w=${shortId(event.worker_id, 8)}` : "";
  const epoch = event.epoch !== null && event.epoch !== undefined
    ? ` e=${event.epoch}` : "";
  return `#${event.seq} ${clockOf(event.created_at)} ${event.kind}`
    + `${node}${worker}${epoch}`;
}

class TracePanel {
  constructor({ client, helpers, mode, ui, notify, now }) {
    this.client = client;
    this.helpers = helpers;
    this.mode = mode;
    this.ui = ui;
    this.notify = typeof notify === "function" ? notify : () => {};
    this.now = typeof now === "function" ? now : Date.now;

    this.closed = false;
    this.mounted = false;
    this.expanded = false;
    this.disposed = false;
    this.overlays = {
      compact: { handle: null, done: null },
      expanded: { handle: null, done: null },
    };
    this.tui = null;
    this.theme = null;
    this.keybindings = null;

    this.generation = 0;
    this.openRequest = 0;
    this.watchGeneration = 0;
    this.runsRequest = 0;
    this.runs = [];
    this.runsLoaded = false;
    this.runsError = null;
    this.selectedRunId = null;
    this.sessionRunId = null;
    this.manualRunId = null;
    this.runStatus = null;
    this.runUpdatedAt = null;

    this.snapshot = null;
    this.view = "tree";
    this.nodeRows = [];
    this.nodeLabels = new Map();
    this.selectedNodeId = null;
    this.selectedIndex = 0;
    this.detail = null;
    this.detailScroll = 0;
    this.runPicker = false;
    this.pickerIndex = 0;

    this.connection = { state: "idle", message: null };
    this.watchToken = null;
    this.watchAbort = null;
    this.lastFrameAt = 0;
    this.lastSyncMs = null;
    this.refreshTimer = null;
    this.refreshQueued = false;
    this.refreshing = false;
    this.reconnect = null;
    this.staleTimer = null;
    this.opening = false;

    this.cachedWidth = null;
    this.cachedLines = null;
    this.rowMap = [];

    this.clock = setInterval(() => this.checkStale(), 5000);
    this.clock.unref?.();
  }

  // ---------------------------------------------------------------- lifecycle

  mount() {
    if (this.closed || this.mounted || this.mode !== "tui") return false;
    if (!this.helpers || typeof this.helpers.truncateToWidth !== "function"
        || typeof this.helpers.visibleWidth !== "function"
        || typeof this.helpers.wrapTextWithAnsi !== "function") {
      return false;
    }
    if (typeof this.ui?.custom !== "function") return false;
    this.mounted = true;
    if (!this.mountOverlay("compact") || !this.mountOverlay("expanded")) {
      this.mounted = false;
      return false;
    }
    this.loadRuns();
    return true;
  }

  // One overlay per width class. Pi resolves overlayOptions once before
  // showOverlay, so changing the mounted width at runtime is not possible;
  // both supported widths stay mounted and the documented per-render visible()
  // hook selects exactly one. Mode changes therefore never remount or dispose
  // a component, and there is still only one panel instance and one watch.
  mountOverlay(kind) {
    let pending;
    try {
      pending = this.ui.custom((tui, theme, keybindings, done) => {
        this.tui = tui;
        this.theme = theme;
        this.keybindings = keybindings;
        this.overlays[kind].done = done;
        return {
          render: (width) => this.render(width),
          handleInput: (data) => this.handleInput(data),
          handleMouse: (event) => this.handleMouse(event),
          invalidate: () => this.invalidate(),
          dispose: () => this.noteOverlayDisposed(kind),
        };
      }, {
        overlay: true,
        overlayOptions: () => this.overlayOptions(kind),
        onHandle: (handle) => this.noteOverlayHandle(kind, handle),
      });
    } catch (error) {
      this.connection = { state: "disconnected",
                          message: String(error?.message ?? error) };
      return false;
    }
    Promise.resolve(pending).catch((error) => {
      if (this.closed) return;
      this.connection = { state: "disconnected",
                          message: String(error?.message ?? error) };
      this.requestRender();
    });
    return true;
  }

  noteOverlayHandle(kind, handle) {
    this.overlays[kind].handle = handle;
    if (this.closed) {
      this.hideOverlay(kind);
      return;
    }
    if (kind === "expanded") {
      if (this.expanded) this.focusExpanded();
      return;
    }
    queueMicrotask(() => {
      if (this.closed || this.expanded) return;
      this.releaseFocus();
    });
  }

  noteOverlayDisposed(kind) {
    const slot = this.overlays[kind];
    slot.done = null;
    slot.handle = null;
  }

  hideOverlay(kind) {
    const slot = this.overlays[kind];
    const done = slot.done;
    slot.done = null;
    slot.handle = null;
    if (typeof done !== "function") return;
    try {
      done(undefined);
    } catch {
      // The overlay may already be gone.
    }
  }

  close() {
    if (this.closed) return;
    this.closed = true;
    this.expanded = false;
    this.opening = false;
    this.sessionRunId = null;
    this.manualRunId = null;
    this.generation += 1;
    this.openRequest += 1;
    this.runsRequest += 1;
    this.stopWatch();
    this.clearTimers();
    if (this.clock) {
      clearInterval(this.clock);
      this.clock = null;
    }
    this.mounted = false;
    this.hideOverlay("expanded");
    this.hideOverlay("compact");
    this.invalidate();
  }

  dispose() {
    if (this.disposed) return;
    this.disposed = true;
    this.closed = true;
    this.opening = false;
    this.sessionRunId = null;
    this.manualRunId = null;
    this.generation += 1;
    this.openRequest += 1;
    this.runsRequest += 1;
    this.stopWatch();
    this.clearTimers();
    if (this.clock) {
      clearInterval(this.clock);
      this.clock = null;
    }
    this.mounted = false;
    this.hideOverlay("expanded");
    this.hideOverlay("compact");
  }

  overlayOptions(kind) {
    if (kind === "expanded") {
      return { anchor: "top-right", width: "60%", minWidth: 20,
               maxHeight: "80%", margin: 1, nonCapturing: true,
               visible: () => this.expanded && !this.closed };
    }
    const text = this.compactText();
    const width = Math.min(38,
      Math.max(18, this.helpers.visibleWidth(text) + 2));
    return { anchor: "top-right", width, margin: 1, nonCapturing: true,
             visible: () => !this.expanded && !this.closed };
  }

  expand() {
    if (this.closed || !this.mounted) return;
    if (!this.expanded) {
      this.expanded = true;
      this.generation += 1;
      this.requestRender();
      if (this.selectedRunId) this.openRun();
      else this.loadRuns();
    }
    this.focusExpanded();
  }

  collapse() {
    if (!this.expanded) return;
    this.expanded = false;
    this.runPicker = false;
    this.opening = false;
    this.generation += 1;
    this.openRequest += 1;
    this.stopWatch();
    this.cancelDelay();
    this.clearRefreshTimer();
    this.releaseFocus();
    this.requestRender();
  }

  toggle() {
    if (this.expanded) this.collapse();
    else this.expand();
  }

  focusExpanded() {
    const handle = this.overlays.expanded.handle;
    if (!handle) return;
    try {
      handle.focus();
    } catch {
      // A vanished overlay must not break the command.
    }
  }

  releaseFocus() {
    const handle = this.overlays.expanded.handle;
    if (!handle) return;
    try {
      if (handle.isFocused?.()) handle.unfocus();
    } catch {
      // Returning focus is best effort; the editor keeps working.
    }
  }

  // ---------------------------------------------------------------- service

  async loadRuns() {
    const token = ++this.runsRequest;
    try {
      const runs = await this.client.listRuns({ limit: RUN_LIMIT });
      if (this.closed || token !== this.runsRequest) return;
      this.runs = runs;
      this.runsLoaded = true;
      this.runsError = null;
      const selected = runs.find((run) => run.run_id === this.selectedRunId);
      if (selected) {
        this.runStatus = selected.status ?? this.runStatus;
        this.runUpdatedAt = selected.updated_at ?? this.runUpdatedAt;
      }
      const owned = this.sessionRunId !== null
        && runs.some((run) => run.run_id === this.sessionRunId);
      const pinned = this.manualRunId !== null
        && runs.some((run) => run.run_id === this.manualRunId);
      if (this.selectedRunId === null) {
        // A run this Pi created wins over an arbitrary newest run; without one
        // the newest listed run is only an initial fallback, not session-owned.
        const pick = owned ? this.sessionRunId
          : pinned ? this.manualRunId
            : runs[0]?.run_id ?? null;
        if (pick) this.applyRunSelection(pick);
      } else if (this.manualRunId === null && owned
                 && this.sessionRunId !== this.selectedRunId) {
        this.applyRunSelection(this.sessionRunId);
      }
      this.runPicker = this.runPicker && this.runs.length > 0;
      this.pickerIndex = clampIndex(this.pickerIndex, this.runs.length);
      this.requestRender();
      if (this.expanded && this.selectedRunId && this.snapshot === null
          && !this.opening) {
        this.openRun();
      }
    } catch (error) {
      if (this.closed || token !== this.runsRequest) return;
      this.runsLoaded = true;
      this.runsError = { code: error?.code ?? "unavailable",
                         message: String(error?.message ?? error) };
      this.setConnection(connectionOf(error), this.runsError.message);
      this.requestRender();
    }
  }

  current(gen, runId, requestId) {
    return !this.closed && this.expanded && gen === this.generation
      && runId === this.selectedRunId && requestId === this.openRequest;
  }

  async openRun() {
    if (!this.expanded || !this.selectedRunId || this.closed) return;
    // A fresh snapshot owns the connection: end any previous stream and any
    // pending retry so cursor_expired recovery cannot leave two loops alive.
    this.stopWatch();
    this.cancelDelay();
    const gen = this.generation;
    const runId = this.selectedRunId;
    const requestId = ++this.openRequest;
    this.opening = true;
    let delayMs = 500;
    try {
      for (;;) {
        this.setConnection("connecting");
        try {
          const snapshot = await this.client.snapshot(runId);
          if (!this.current(gen, runId, requestId)) return;
          this.applySnapshot(snapshot);
          this.watchToken = snapshot.cursor?.token ?? null;
          this.lastFrameAt = this.now();
          this.setConnection("connected");
          this.startWatch();
          return;
        } catch (error) {
          if (!this.current(gen, runId, requestId)) return;
          const message = String(error?.message ?? error);
          if (terminalSnapshotError(error)) {
            this.setConnection(connectionOf(error), message);
            return;
          }
          const seconds = Math.round(delayMs / 100) / 10;
          this.setConnection("reconnecting",
            `快照读取失败，${seconds} 秒后重试: ${message}`);
          await this.delay(delayMs);
          if (!this.current(gen, runId, requestId)) return;
          delayMs = Math.min(delayMs * 2, RECONNECT_MAX_MS);
        }
      }
    } finally {
      if (requestId === this.openRequest) this.opening = false;
    }
  }

  scheduleRefresh() {
    if (this.refreshTimer !== null) return;
    this.refreshTimer = setTimeout(() => {
      this.refreshTimer = null;
      this.refreshSnapshot();
    }, REFRESH_DELAY_MS);
    this.refreshTimer.unref?.();
  }

  async refreshSnapshot() {
    if (!this.expanded || !this.selectedRunId || this.closed || !this.snapshot) {
      return;
    }
    if (this.refreshing) {
      this.refreshQueued = true;
      return;
    }
    this.refreshing = true;
    const gen = this.generation;
    const runId = this.selectedRunId;
    const requestId = this.openRequest;
    try {
      const snapshot = await this.client.snapshot(runId);
      if (!this.current(gen, runId, requestId)) return;
      this.applySnapshot(snapshot);
      if (this.connection.state !== "connected") {
        this.setConnection("connected");
      }
    } catch (error) {
      if (!this.current(gen, runId, requestId)) return;
      this.setConnection(connectionOf(error), String(error?.message ?? error));
    } finally {
      this.refreshing = false;
      if (this.refreshQueued) {
        this.refreshQueued = false;
        this.scheduleRefresh();
      }
    }
  }

  applySnapshot(snapshot) {
    if (!snapshot || snapshot.format !== "agentgenome-trace/1"
        || typeof snapshot.cursor?.token !== "string") {
      throw failure("transport_protocol_error",
                    "trace snapshot is missing its committed cursor");
    }
    const previous = this.selectedNodeId;
    this.snapshot = snapshot;
    this.runStatus = snapshot.run?.status ?? this.runStatus;
    this.runUpdatedAt = snapshot.run?.updated_at ?? this.runUpdatedAt;
    this.lastSyncMs = this.now();
    this.rebuildRows();
    if (previous && this.nodeRows.some((row) => row.nodeId === previous)) {
      this.selectedNodeId = previous;
      this.selectedIndex = this.nodeRows.findIndex((row) => row.nodeId === previous);
    } else if (this.nodeRows.length > 0) {
      this.selectedNodeId = this.nodeRows[0].nodeId;
      this.selectedIndex = 0;
      this.detail = null;
    } else {
      this.selectedNodeId = null;
      this.selectedIndex = 0;
      this.detail = null;
    }
    this.requestRender();
    this.ensureDetail();
  }

  rebuildRows() {
    this.nodeRows = [];
    this.nodeLabels = new Map();
    const tree = Array.isArray(this.snapshot?.tree) ? this.snapshot.tree : [];
    const byId = new Map(tree.map((node) => [node.node_id, node]));
    this.nodeLabels = byId;
    const children = new Map();
    for (const edge of this.snapshot?.decomposition ?? []) {
      if (!byId.has(edge.parent_id) || !byId.has(edge.child_id)) continue;
      const list = children.get(edge.parent_id) ?? [];
      list.push(edge.child_id);
      children.set(edge.parent_id, list);
    }
    const seen = new Set();
    const visit = (nodeId, depth) => {
      if (seen.has(nodeId)) return;
      seen.add(nodeId);
      this.nodeRows.push({ nodeId, depth });
      for (const child of children.get(nodeId) ?? []) visit(child, depth + 1);
    };
    for (const node of tree) {
      if (!node.parent_id || !byId.has(node.parent_id)) visit(node.node_id, 0);
    }
    for (const node of tree) visit(node.node_id, 0);
  }

  // ---------------------------------------------------------------- watching

  startWatch() {
    this.stopWatch();
    if (!this.expanded || this.closed || !this.selectedRunId) return;
    const gen = this.generation;
    const runId = this.selectedRunId;
    const controller = new AbortController();
    const watchGen = this.watchGeneration;
    this.watchAbort = controller;
    this.lastFrameAt = this.now();
    this.watchLoop(runId, gen, watchGen, controller).catch((error) => {
      if (controller.signal.aborted) return;
      this.setConnection(connectionOf(error), String(error?.message ?? error));
    });
  }

  stopWatch() {
    this.watchGeneration += 1;
    if (this.watchAbort !== null) {
      try {
        this.watchAbort.abort();
      } catch {
        // Already closed.
      }
      this.watchAbort = null;
    }
  }

  watchCurrent(gen, runId, watchGen, controller) {
    return !this.closed && this.expanded && gen === this.generation
      && runId === this.selectedRunId && watchGen === this.watchGeneration
      && !controller.signal.aborted;
  }

  async watchLoop(runId, gen, watchGen, controller) {
    let delay = 500;
    while (this.watchCurrent(gen, runId, watchGen, controller)) {
      let failureSeen = null;
      try {
        for await (const frame of this.client.watchFrames(
          runId, this.watchToken, controller.signal)) {
          if (!this.watchCurrent(gen, runId, watchGen, controller)) return;
          this.lastFrameAt = this.now();
          if (frame.event === "heartbeat") {
            if (this.connection.state !== "connected") {
              this.setConnection("connected");
            }
            continue;
          }
          if (frame.event === "control") {
            const token = cursorTokenOf(frame);
            if (token) this.watchToken = token;
            if (this.connection.state !== "connected") {
              this.setConnection("connected");
            }
            this.scheduleRefresh();
            delay = 500;
            continue;
          }
          if (frame.event === "cursor_expired") {
            failureSeen = failure("cursor_expired",
                                  "trace cursor expired; re-reading snapshot");
            break;
          }
        }
      } catch (error) {
        failureSeen = error;
      }
      if (!this.watchCurrent(gen, runId, watchGen, controller)) return;
      if (controller.signal.aborted) return;
      if (failureSeen) {
        const code = failureSeen.code;
        const status = failureSeen.status;
        if (code === "cursor_expired" || status === 409) {
          this.watchToken = null;
          await this.openRun();
          return;
        }
        if (code === "unauthorized" || status === 401 || status === 403) {
          this.setConnection("disconnected", "认证失败");
          return;
        }
        if (status === 404) {
          this.setConnection("disconnected", "任务不存在");
          return;
        }
        this.setConnection("reconnecting",
                           String(failureSeen.message ?? failureSeen));
      } else {
        this.setConnection("reconnecting", "事件流已结束");
      }
      await this.delay(delay);
      if (!this.watchCurrent(gen, runId, watchGen, controller)) return;
      delay = Math.min(delay * 2, RECONNECT_MAX_MS);
    }
  }

  delay(ms) {
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        if (this.reconnect?.timer === timer) this.reconnect = null;
        resolve();
      }, ms);
      timer.unref?.();
      this.reconnect = { timer, resolve };
    });
  }

  cancelDelay() {
    if (this.reconnect === null) return;
    const { timer, resolve } = this.reconnect;
    clearTimeout(timer);
    this.reconnect = null;
    resolve();
  }

  checkStale() {
    if (!this.expanded || this.closed) return;
    if (this.connection.state !== "connected"
        && this.connection.state !== "stale") return;
    if (this.now() - this.lastFrameAt <= STALE_AFTER_MS) return;
    this.setConnection("stale", "超过 35 秒未收到服务事件");
  }

  clearRefreshTimer() {
    if (this.refreshTimer !== null) {
      clearTimeout(this.refreshTimer);
      this.refreshTimer = null;
    }
  }

  clearTimers() {
    this.clearRefreshTimer();
    this.cancelDelay();
  }

  setConnection(state, message = null) {
    if (this.connection.state === state && this.connection.message === message) {
      return;
    }
    this.connection = { state, message };
    this.requestRender();
  }

  // ---------------------------------------------------------------- selection

  selectNode(nodeId, { keepIndex = false } = {}) {
    if (!nodeId) return;
    const changed = nodeId !== this.selectedNodeId;
    this.selectedNodeId = nodeId;
    if (!keepIndex) {
      const index = this.nodeRows.findIndex((row) => row.nodeId === nodeId);
      this.selectedIndex = index >= 0 ? index : 0;
    }
    if (changed) {
      this.detail = null;
      this.detailScroll = 0;
      this.requestRender();
    }
    this.ensureDetail();
  }

  moveSelection(delta) {
    if (this.nodeRows.length === 0) return;
    const index = clampIndex(this.selectedIndex + delta, this.nodeRows.length);
    const row = this.nodeRows[index];
    this.selectedIndex = index;
    if (row) this.selectNode(row.nodeId, { keepIndex: true });
    else this.requestRender();
  }

  toggleView() {
    this.view = this.view === "tree" ? "dependencies" : "tree";
    this.requestRender();
  }

  selectRun(runId, { manual = false } = {}) {
    this.runPicker = false;
    if (manual && typeof runId === "string" && runId.length > 0) {
      this.manualRunId = runId;
    }
    if (!runId || runId === this.selectedRunId) {
      this.requestRender();
      return;
    }
    this.applyRunSelection(runId);
  }

  // A run created by this Pi in this activation. Automatic polling may never
  // replace a manual choice, and a run created elsewhere is never claimed.
  noteSessionRun(runId) {
    if (this.closed || typeof runId !== "string" || runId.length === 0) return;
    this.sessionRunId = runId;
    if (this.manualRunId === null && this.selectedRunId !== runId) {
      this.applyRunSelection(runId);
    } else {
      this.requestRender();
    }
    this.loadRuns();
  }

  applyRunSelection(runId) {
    if (!runId || this.closed) return;
    this.selectedRunId = runId;
    const run = this.runs.find((item) => item.run_id === runId);
    this.runStatus = run?.status ?? null;
    this.runUpdatedAt = run?.updated_at ?? null;
    this.snapshot = null;
    this.detail = null;
    this.nodeRows = [];
    this.selectedNodeId = null;
    this.selectedIndex = 0;
    this.detailScroll = 0;
    this.watchToken = null;
    this.generation += 1;
    this.openRequest += 1;
    this.opening = false;
    this.stopWatch();
    this.cancelDelay();
    this.clearRefreshTimer();
    this.requestRender();
    if (this.expanded) this.openRun();
  }

  openRunPicker() {
    this.runPicker = true;
    this.pickerIndex = Math.max(0,
      this.runs.findIndex((run) => run.run_id === this.selectedRunId));
    this.requestRender();
    this.loadRuns();
  }

  // ---------------------------------------------------------------- detail

  executionIdentity() {
    if (!this.snapshot || !this.selectedNodeId) return null;
    const executions = (this.snapshot.executions ?? [])
      .filter((item) => item.node_id === this.selectedNodeId);
    if (executions.length === 0) return null;
    let latest = executions[0];
    for (const item of executions) {
      if (Number(item.epoch) > Number(latest.epoch)) latest = item;
    }
    const deliveries = Array.isArray(latest.deliveries) ? latest.deliveries : [];
    const delivery = deliveries.length > 0
      ? deliveries[deliveries.length - 1] : null;
    return {
      nodeId: this.selectedNodeId,
      workerId: latest.worker_id,
      epoch: latest.epoch,
      deliveryKey: delivery?.delivery_key ?? null,
      deliveryState: delivery?.delivery_state ?? null,
      backendRef: latest.backend_ref ?? null,
      current: latest.current === true,
      usage: latest.usage ?? null,
      key: `${latest.worker_id}/${latest.epoch}/${delivery?.delivery_key ?? "-"}`,
    };
  }

  resetDetail(identity) {
    this.detail = {
      identity,
      identityKey: identity.key,
      entries: [],
      seen: new Set(),
      nextCursor: null,
      hasMore: false,
      availability: null,
      partial: false,
      note: null,
      error: null,
      loading: false,
    };
  }

  ensureDetail() {
    if (!this.expanded || this.closed || !this.snapshot) return;
    const identity = this.executionIdentity();
    if (identity === null) {
      if (this.detail !== null) {
        this.detail = null;
        this.requestRender();
      }
      return;
    }
    if (!identity.deliveryKey) {
      if (this.detail?.identityKey !== identity.key) {
        this.resetDetail(identity);
        this.detail.availability = "no_delivery";
      }
      this.requestRender();
      return;
    }
    const replace = this.detail === null
      || this.detail.identityKey !== identity.key;
    this.loadDetail(identity, { replace });
  }

  detailCurrent(gen, runId, nodeId, requestId, identity) {
    return !this.closed && this.expanded && gen === this.generation
      && runId === this.selectedRunId && nodeId === this.selectedNodeId
      && requestId === this.openRequest
      && this.detail?.identityKey === identity.key;
  }

  async loadDetail(identity, { replace = false, retried = false } = {}) {
    if (!identity.deliveryKey) return;
    const gen = this.generation;
    const runId = this.selectedRunId;
    const nodeId = this.selectedNodeId;
    const requestId = this.openRequest;
    if (replace || this.detail?.identityKey !== identity.key) {
      this.resetDetail(identity);
    }
    const detail = this.detail;
    if (!detail || detail.loading) return;
    detail.loading = true;
    let after = replace ? null : detail.nextCursor;
    let pages = 0;
    this.requestRender();
    try {
      for (;;) {
        const page = await this.client.execution(runId, identity,
                                                 { after, limit: 100 });
        if (!this.detailCurrent(gen, runId, nodeId, requestId, identity)) {
          // A collapse or a view switch may abandon this page; release the
          // loading flag so the next snapshot can fetch again.
          if (this.detail === detail) detail.loading = false;
          return;
        }
        if (replace) {
          detail.entries = [];
          detail.seen = new Set();
          replace = false;
        }
        this.appendEntries(detail, page?.entries ?? [], identity);
        detail.availability = page?.availability ?? null;
        detail.partial = page?.partial === true;
        detail.note = page?.detail ?? null;
        detail.nextCursor = page?.next_cursor?.token ?? after;
        detail.hasMore = page?.has_more === true;
        detail.error = null;
        detail.loading = false;
        this.requestRender();
        after = detail.nextCursor;
        pages += 1;
        if (!detail.hasMore || pages >= MAX_DETAIL_PAGES) break;
      }
    } catch (error) {
      if (!this.detailCurrent(gen, runId, nodeId, requestId, identity)) {
        if (this.detail === detail) detail.loading = false;
        return;
      }
      if (this.detail) this.detail.loading = false;
      if ((error?.code === "cursor_expired" || error?.status === 409)
          && !retried) {
        this.detail = null;
        await this.loadDetail(identity, { replace: true, retried: true });
        return;
      }
      if (this.detail) {
        this.detail.availability = "error";
        this.detail.error = { code: error?.code ?? "detail_error",
                              message: String(error?.message ?? error) };
      }
      this.requestRender();
    }
  }

  appendEntries(detail, entries, identity) {
    for (const entry of entries) {
      if (!entry || typeof entry !== "object") continue;
      if (entry.worker_id !== undefined && entry.worker_id !== identity.workerId) {
        continue;
      }
      if (entry.epoch !== undefined && Number(entry.epoch) !== Number(identity.epoch)) {
        continue;
      }
      if (entry.delivery_key !== undefined
          && entry.delivery_key !== identity.deliveryKey) {
        continue;
      }
      if (entry.offset !== undefined && entry.offset !== null) {
        const key = `${entry.source ?? ""}:${entry.offset}`;
        if (detail.seen.has(key)) continue;
        detail.seen.add(key);
      }
      detail.entries.push(entry);
    }
    while (detail.entries.length > MAX_DETAIL_ENTRIES) {
      const removed = detail.entries.shift();
      if (removed && removed.offset !== undefined && removed.offset !== null) {
        detail.seen.delete(`${removed.source ?? ""}:${removed.offset}`);
      }
    }
  }

  // ---------------------------------------------------------------- input

  handleInput(data) {
    if (this.closed) return;
    // Explicit editor return while the panel stays expanded. handle.unfocus()
    // clears Pi's overlay focus restore, so later renders and refreshes cannot
    // take the editor back.
    if (data === "\u0005") {
      this.releaseFocus();
      return;
    }
    const match = (name) => {
      try {
        return this.keybindings?.matches?.(data, name) === true;
      } catch {
        return false;
      }
    };
    if (this.runPicker) {
      if (match("tui.select.cancel")) {
        this.runPicker = false;
        this.requestRender();
        return;
      }
      if (match("tui.select.up")) {
        this.pickerIndex = clampIndex(this.pickerIndex - 1, this.runs.length);
        this.requestRender();
        return;
      }
      if (match("tui.select.down")) {
        this.pickerIndex = clampIndex(this.pickerIndex + 1, this.runs.length);
        this.requestRender();
        return;
      }
      if (match("tui.select.confirm")) {
        const run = this.runs[this.pickerIndex];
        if (run) this.selectRun(run.run_id, { manual: true });
        else this.requestRender();
        return;
      }
      return;
    }
    if (!this.expanded) return;
    if (match("tui.select.cancel")) {
      this.collapse();
      return;
    }
    if (match("tui.select.up")) {
      this.moveSelection(-1);
      return;
    }
    if (match("tui.select.down")) {
      this.moveSelection(1);
      return;
    }
    if (match("tui.select.pageUp")) {
      this.scrollDetail(MAX_LOG_LINES);
      return;
    }
    if (match("tui.select.pageDown")) {
      this.scrollDetail(-MAX_LOG_LINES);
      return;
    }
    if (match("tui.select.confirm")) {
      if (!this.selectedRunId) this.openRunPicker();
      return;
    }
    if (data === "t") this.toggleView();
    else if (data === "r") this.openRunPicker();
  }

  handleMouse(event) {
    if (this.closed || !event) return undefined;
    if (event.type === "wheel") {
      if (!this.expanded) return undefined;
      const direction = (event.wheelDelta ?? 0) < 0 ? 1 : -1;
      this.scrollDetail(direction);
      return { handled: true, render: true };
    }
    if (event.type !== "press" || event.button !== "left") return undefined;
    const row = this.rowMap[event.y];
    if (!row || !row.kind) return undefined;
    if (row.kind === "toggle") {
      // expand()/collapse() own the focus transition through the matching
      // overlay handle; letting the mouse dispatcher focus this component
      // would hand focus to the now-hidden compact overlay.
      this.toggle();
      return { handled: true, render: true };
    }
    if (row.kind === "editor") {
      this.releaseFocus();
      return { handled: true, render: true };
    }
    if (!this.expanded) return undefined;
    if (row.kind === "runtime") {
      this.openRunPicker();
      return { handled: true, render: true, focus: true };
    }
    if (row.kind === "view") {
      this.toggleView();
      return { handled: true, render: true, focus: true };
    }
    if (row.kind === "run") {
      this.selectRun(row.runId, { manual: true });
      return { handled: true, render: true, focus: true };
    }
    if (row.kind === "node") {
      this.selectNode(row.nodeId);
      return { handled: true, render: true, focus: true };
    }
    return undefined;
  }

  scrollDetail(delta) {
    const length = this.detail?.entries.length ?? 0;
    const max = Math.max(0, length - MAX_LOG_LINES);
    this.detailScroll = Math.max(0,
      Math.min(max, this.detailScroll + delta));
    this.requestRender();
  }

  // ---------------------------------------------------------------- rendering

  invalidate() {
    this.cachedWidth = null;
    this.cachedLines = null;
  }

  requestRender() {
    this.invalidate();
    if (this.tui) {
      try {
        this.tui.requestRender();
      } catch {
        // The TUI may already be shutting down.
      }
    }
  }

  style(token, text) {
    try {
      return this.theme?.fg?.(token, text) ?? text;
    } catch {
      return text;
    }
  }

  line(text, width) {
    return this.helpers.truncateToWidth(text, width, "…");
  }

  compactText() {
    if (!this.selectedRunId) {
      return this.runsLoaded ? "AgentGenome ▸ 选择任务" : "AgentGenome ▸ 加载任务";
    }
    const status = this.runStatus ? ` ${this.runStatus}` : "";
    const extra = ["disconnected", "unauthorized", "stale", "reconnecting"]
      .includes(this.connection.state)
      ? ` · ${connectionLabel(this.connection.state)}` : "";
    return `AgentGenome ▸ ${shortId(this.selectedRunId)}${status}${extra}`;
  }

  render(width) {
    const resolved = Math.max(1, Math.floor(Number(width)) || 1);
    if (this.cachedLines !== null && this.cachedWidth === resolved) {
      return this.cachedLines;
    }
    this.rowMap = [];
    const lines = this.expanded
      ? this.renderExpanded(resolved)
      : [this.renderCompact(resolved)];
    this.cachedWidth = resolved;
    this.cachedLines = lines;
    return lines;
  }

  renderCompact(width) {
    this.rowMap = [{ kind: "toggle" }];
    let text = this.style("accent", "AgentGenome ▸");
    if (this.selectedRunId) {
      text += ` ${this.style("dim", shortId(this.selectedRunId))}`;
      if (this.runStatus) {
        text += ` ${this.style(stateColor(this.runStatus), this.runStatus)}`;
      }
    } else {
      text += ` ${this.style("dim",
        this.runsLoaded ? "选择任务" : "加载任务")}`;
    }
    if (["disconnected", "unauthorized", "stale", "reconnecting"]
      .includes(this.connection.state)) {
      text += ` ${this.style("warning",
        `· ${connectionLabel(this.connection.state)}`)}`;
    }
    return this.line(text, width);
  }

  renderExpanded(width) {
    const lines = [];
    const push = (text, row = null) => {
      lines.push(this.line(text, width));
      this.rowMap.push(row);
    };
    push(this.headerText(), { kind: "toggle" });
    push(this.runText(), { kind: "runtime" });
    push(this.viewText(), { kind: "view" });
    push(this.style("dim", "─".repeat(Math.max(1, width - 1))));

    if (this.runPicker) {
      this.renderPicker(push, width);
      return lines;
    }
    if (!this.selectedRunId) {
      push(this.style("dim", this.runsLoaded
        ? "没有选中的 run；按 r 选择已有运行。" : "正在读取运行列表…"));
      push(this.helpText(true), { kind: "editor" });
      return lines;
    }
    if (this.snapshot === null) {
      const message = this.connection.message
        ?? "正在读取 trace 快照…";
      const color = ["disconnected", "unauthorized"].includes(this.connection.state)
        ? "error" : this.connection.state === "reconnecting" ? "warning" : "dim";
      push(this.style(color, message));
      push(this.helpText(true), { kind: "editor" });
      return lines;
    }

    const narrow = width < 40;
    if (narrow) {
      push(this.style("dim", "窄屏详情视图 · ↑/↓ 节点 · Esc 收起"));
    } else {
      this.renderNodes(push);
    }
    for (const text of this.detailLines()) {
      push(text);
    }
    push(this.helpText(false), { kind: "editor" });
    return lines;
  }

  headerText() {
    const connection = connectionLabel(this.connection.state);
    const connectionColor = ["connected"].includes(this.connection.state)
      ? "success"
      : ["connecting", "reconnecting"].includes(this.connection.state)
        ? "warning"
        : ["stale", "disconnected", "unauthorized"].includes(this.connection.state)
          ? "error" : "dim";
    let text = `${this.style("accent", "▾ AgentGenome")} `;
    text += `${this.style(connectionColor, connection)}`;
    if (this.connection.message) {
      text += ` ${this.style("dim", `· ${this.connection.message}`)}`;
    }
    if (this.lastSyncMs !== null) {
      text += ` ${this.style("dim",
        `同步 ${new Date(this.lastSyncMs).toISOString().slice(11, 19)}Z`)}`;
    }
    return text;
  }

  runText() {
    if (!this.selectedRunId) {
      return `${this.style("dim", "运行 未选择")} ${this.style("dim", "(r 选择)")}`;
    }
    const updated = this.runUpdatedAt
      ? ` 更新 ${String(this.runUpdatedAt).slice(0, 19)}` : " 更新 不可用";
    return `${this.style("accent", "运行")} ${shortId(this.selectedRunId, 12)}`
      + ` ${this.runStatus ?? "unknown"}${this.style("dim", updated)}`;
  }

  viewText() {
    const tree = this.view === "tree" ? "树" : "依赖";
    const other = this.view === "tree" ? "依赖" : "树";
    const label = this.selectedNodeId
      ? this.nodeLabels.get(this.selectedNodeId)?.label ?? this.selectedNodeId
      : "未选择";
    return `视图 ${this.style("accent", `[${tree}]`)} ${this.style("dim", other)}`
      + ` ${this.style("dim", `· 节点 ${this.nodeRows.length} · 选中 ${label} (t 切换)`)}`;
  }

  renderNodes(push) {
    const total = this.nodeRows.length;
    if (total === 0) {
      push(this.style("dim", "任务树为空"));
      return;
    }
    let start = 0;
    if (total > MAX_NODE_ROWS) {
      start = Math.max(0, Math.min(total - MAX_NODE_ROWS,
        this.selectedIndex - Math.floor(MAX_NODE_ROWS / 2)));
    }
    const end = Math.min(total, start + MAX_NODE_ROWS);
    for (let index = start; index < end; index += 1) {
      const row = this.nodeRows[index];
      push(this.nodeText(row, index), { kind: "node", nodeId: row.nodeId });
    }
    if (start > 0 || end < total) {
      push(this.style("dim", `… ${start + 1}-${end}/${total} 节点`));
    }
  }

  nodeText(row, index) {
    const node = this.nodeLabels.get(row.nodeId);
    if (!node) return row.nodeId;
    const selected = row.nodeId === this.selectedNodeId;
    const marker = selected ? "› " : "  ";
    const indent = this.view === "tree" ? "  ".repeat(row.depth) : "";
    const label = node.label || node.node_id;
    const state = String(node.state ?? "unknown");
    const round = node.round !== null && node.round !== undefined
      ? `r${node.round}` : "r?";
    const execution = node.current_execution
      ? ` ${shortId(node.current_execution.worker_id, 6)}/${node.current_execution.epoch}`
      : "";
    let text = `${indent}${marker}${label} `
      + this.style(stateColor(state), `[${state}]`) + ` ${round}${execution}`;
    if (this.view === "dependencies") {
      const deps = Array.isArray(node.dependencies) ? node.dependencies : [];
      const names = deps
        .map((id) => this.nodeLabels.get(id)?.label ?? id)
        .map((name) => shortId(name, 12));
      text += deps.length > 0
        ? ` ← ${names.join(", ")}` : " ← 无";
    }
    return selected ? this.style("accent", text) : text;
  }

  detailLines() {
    const lines = [];
    const node = this.selectedNodeId
      ? this.nodeLabels.get(this.selectedNodeId) : null;
    if (!node) return [this.style("dim", "未选择节点")];
    const state = String(node.state ?? "unknown");
    lines.push(this.style("accent", "节点")
      + ` ${node.label || node.node_id} `
      + this.style(stateColor(state), `[${state}]`)
      + ` r${node.round ?? "?"}`);
    const identity = this.executionIdentity();
    if (identity) {
      const delivery = identity.deliveryKey
        ? shortId(identity.deliveryKey, 12) : "无投递";
      const phase = identity.current ? "当前" : "历史";
      lines.push(`执行 w=${shortId(identity.workerId, 10)} e=${identity.epoch}`
        + ` d=${delivery} ${phase}`
        + (identity.deliveryState ? ` ${identity.deliveryState}` : ""));
      lines.push(this.style("dim", `usage ${identity.usage
        ? jsonShort(identity.usage, 40) : "不可用"}`));
    } else {
      lines.push(this.style("dim", "执行 不可用：没有已提交的 worker/epoch"));
    }
    const candidates = (this.snapshot.candidates ?? [])
      .filter((item) => item.node_id === node.node_id);
    if (candidates.length > 0) {
      const latest = candidates[candidates.length - 1];
      lines.push(`候选 ${shortId(latest.candidate_id, 10)}`
        + ` decision=${latest.decision ?? "未决"}`
        + ` refs=${jsonShort(latest.output_refs, 30)}`);
    } else {
      lines.push(this.style("dim", "候选 无"));
    }
    const reviews = (this.snapshot.reviews ?? [])
      .filter((item) => item.node_id === node.node_id);
    if (reviews.length > 0) {
      lines.push(`评审 ${reviews.length}: `
        + reviews.slice(-3)
          .map((item) => {
            const feedback = artifactLabel(item.feedback_ref);
            return feedback
              ? `${item.verdict ?? "?"}/${feedback}` : item.verdict ?? "?";
          })
          .join(", "));
    } else {
      lines.push(this.style("dim", "评审 无"));
    }
    const waiting = (this.snapshot.waiting ?? [])
      .find((item) => item.node_id === node.node_id);
    if (waiting) {
      lines.push(`等待 ${waiting.reason}`
        + (waiting.evidence ? ` 证据 ${jsonShort(waiting.evidence, 34)}` : ""));
    }
    const detail = this.detail;
    if (!detail || (identity && detail.identityKey !== identity.key)) {
      lines.push(this.style("dim", "执行日志 切换中…"));
      return lines;
    }
    if (!identity?.deliveryKey) {
      lines.push(this.style("dim", "执行日志 不可用：没有已提交投递"));
      return lines;
    }
    const availability = detail.availability ?? "unknown";
    const flags = `${detail.hasMore ? " 更多" : ""}${detail.partial ? " 部分" : ""}`;
    lines.push(this.style("dim",
      `执行日志 ${availability} ${detail.entries.length} 条${flags}`));
    if (detail.error) {
      lines.push(this.style("error",
        `读取失败 ${detail.error.code}: ${detail.error.message}`));
    } else if (detail.entries.length === 0) {
      const reason = detail.note?.reason ?? detail.note?.message;
      lines.push(this.style("dim",
        detail.loading ? "读取中…" : `无条目${reason ? `：${reason}` : ""}`));
    } else {
      const end = Math.max(0, detail.entries.length - this.detailScroll);
      const start = Math.max(0, end - MAX_LOG_LINES);
      if (start > 0) {
        lines.push(this.style("dim", `↑ 更早 ${start} 条（PgUp/PgDn 或滚轮）`));
      }
      for (const entry of detail.entries.slice(start, end)) {
        lines.push(this.style("dim", entryLine(entry)));
      }
    }
    const timeline = this.snapshot.timeline ?? {};
    const events = Array.isArray(timeline.events) ? timeline.events : [];
    lines.push(this.style("dim",
      `时间线 ${events.length}${timeline.truncated ? " (更早被截断)" : ""}`));
    for (const event of events.slice(-MAX_TIMELINE_LINES)) {
      lines.push(this.style("dim", timelineLine(event)));
    }
    return lines;
  }

  renderPicker(push) {
    push(this.style("accent", "选择运行")
      + this.style("dim", ` (${this.runs.length})`));
    if (this.runsError) {
      push(this.style("error",
        `运行列表不可用 ${this.runsError.code}: ${this.runsError.message}`));
    } else if (!this.runsLoaded) {
      push(this.style("dim", "正在读取运行列表…"));
    } else if (this.runs.length === 0) {
      push(this.style("dim", "服务没有可见的 run"));
    } else {
      const total = this.runs.length;
      let start = 0;
      if (total > MAX_RUN_ROWS) {
        start = Math.max(0, Math.min(total - MAX_RUN_ROWS,
          this.pickerIndex - Math.floor(MAX_RUN_ROWS / 2)));
      }
      const end = Math.min(total, start + MAX_RUN_ROWS);
      for (let index = start; index < end; index += 1) {
        const run = this.runs[index];
        const marker = index === this.pickerIndex ? "› " : "  ";
        push(`${marker}${shortId(run.run_id, 12)} `
          + this.style(stateColor(run.status), run.status ?? "unknown")
          + ` ${clockOf(run.created_at)}`,
          { kind: "run", runId: run.run_id });
      }
      if (start > 0 || end < total) {
        push(this.style("dim", `… ${start + 1}-${end}/${total}`));
      }
    }
    push(this.helpText(true));
  }

  helpText(picker) {
    return this.style("dim", picker && this.runPicker
      ? "↑/↓ 选择 · Enter 确认 · Esc 返回 · Ctrl+E 回编辑器"
      : "Ctrl+E 回编辑器 · ↑/↓ 节点 · t 树/依赖 · r 运行"
        + " · PgUp/PgDn 日志 · Esc 收起");
  }
}

function clampIndex(index, length) {
  if (length <= 0) return 0;
  return Math.max(0, Math.min(length - 1, index));
}

function connectionLabel(state) {
  switch (state) {
    case "connected": return "已连接";
    case "connecting": return "连接中";
    case "reconnecting": return "重连中";
    case "stale": return "数据过期";
    case "disconnected": return "已断开";
    case "unauthorized": return "认证失败";
    default: return "未展开";
  }
}

function connectionOf(error) {
  const code = error?.code;
  const status = error?.status;
  if (code === "unauthorized" || status === 401 || status === 403) {
    return "unauthorized";
  }
  if (code === "cursor_expired" || status === 409) return "reconnecting";
  if (status === 404 || code === "not_found") return "disconnected";
  return "disconnected";
}

// A missing run, a rejected credential and a malformed snapshot will not heal
// by retrying; transient transport/service failures will.
function terminalSnapshotError(error) {
  const code = error?.code;
  const status = error?.status;
  return code === "unauthorized" || status === 401 || status === 403
    || code === "not_found" || status === 404
    || code === "transport_protocol_error";
}

function cursorTokenOf(frame) {
  if (typeof frame?.id === "string" && frame.id.length > 0) return frame.id;
  try {
    const data = JSON.parse(frame?.data ?? "");
    const token = data?.cursor?.token;
    return typeof token === "string" && token.length > 0 ? token : null;
  } catch {
    return null;
  }
}

export function createTracePanel(options) {
  return new TracePanel(options);
}
