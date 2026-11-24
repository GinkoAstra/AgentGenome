import {NODE_COPY, NODE_ORDER, RunModel, SCENARIOS} from "./simulation.mjs";

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const model = new RunModel();
let timer = null;
let selectedNode = "define";
let selectedCode = "graph";

const codeExamples = {
  graph: {
    file: "graph-clean.yaml",
    label: "GRAPH /2",
    title: "Dependencies carry accepted state.",
    text: "needs: [clean] makes profile wait. {from: clean} binds the accepted clean artifact, not an unreviewed candidate.",
    code: `format: agentgenome-graph-definition/2
definition_id: pi-clean-and-profile
policy: fixed

nodes:
  clean:
    task_ref: clean-work
    reviewer_ref: clean-check
    needs: []
    inputs:
      dataset: {run_input: dataset}

  profile:
    task_ref: profile-work
    reviewer_ref: profile-check
    needs: [clean]
    inputs:
      clean: {from: clean}`
  },
  contract: {
    file: "tasks.clean-work",
    label: "TASK CONTRACT",
    title: "Acceptance rules live with the work.",
    text: "The Task freezes the expected artifact and acceptance rule. The bound Checker independently recomputes the result instead of trusting the Worker’s summary.",
    code: `tasks:
  clean-work:
    format: agentgenome-task-definition/1
    agent_ref: cleaner
    completion_kind: candidate
    contract:
      input_schema: {type: array}
      output_schema: {type: array}
      acceptance: >-
        The candidate must contain one JSON artifact
        whose rows match the normalized source rows,
        in source order, and whose audit counters
        account for every source row.

  clean-check:
    completion_kind: review_receipt
    agent_ref: clean-checker`
  },
  output: {
    file: "deliveries/clean.json",
    label: "ACCEPTED ARTIFACT",
    title: "Delivery means accepted bytes.",
    text: "The output is a content-addressed artifact backed by a review receipt. Downstream Nodes and exports consume these exact accepted bytes.",
    code: `{
  "rows": [
    {"name": "Alice", "email": "alice@example.com"},
    {"name": "Bob", "email": "bob@example.com"},
    {"name": "Carol", "email": "carol@example.com"}
  ],
  "audit": {
    "input_rows": 6,
    "output_rows": 3,
    "invalid_rows": 2,
    "duplicate_rows": 1
  }
}`
  }
};

const architectureLayers = {
  api: {
    label: "API / AUTHORITATIVE STATE",
    title: "One service owns the facts.",
    text: "HTTP, MCP, and event subscriptions share the same API. Authentication, protocol checks, versions, transactions, and persistence stay at this boundary.",
    boundary: "Whether a business artifact is correct."
  },
  scheduler: {
    label: "SCHEDULER / GRAPH ADVANCEMENT",
    title: "Dependencies become executable work.",
    text: "The Scheduler advances Nodes, handles declared expansion and acceptance, enforces capacity, and binds complete execution identities to committed work.",
    boundary: "Email validity, audit rules, or lesson quality."
  },
  controller: {
    label: "CONTROLLER / COORDINATION",
    title: "Committed intents become execution.",
    text: "The Controller coordinates execution that the Scheduler has already committed. It does not create a second workflow or bypass persistent facts.",
    boundary: "New scheduling policy or acceptance criteria."
  },
  backend: {
    label: "BACKEND / REAL EXECUTION",
    title: "Agents and programs do the work.",
    text: "Backends start, resume, deliver to, observe, and stop actual Agent or trusted program instances. Control-plane tools are declared by the Agent; native CLI tools keep the host CLI's own permissions.",
    boundary: "Declaring its own candidate accepted."
  },
  trace: {
    label: "TRACE / READ-ONLY PROJECTION",
    title: "Observe without becoming a scheduler.",
    text: "Trace reads snapshots, control events, and execution details from the same service. The terminal panel and CLI do not open a separate state database.",
    boundary: "Advancing Nodes or changing business state."
  }
};

function stopPlayback() {
  if (timer !== null) {
    clearInterval(timer);
    timer = null;
  }
}

function startPlayback() {
  if (model.complete) model.reset();
  stopPlayback();
  timer = setInterval(() => {
    model.step();
    if (model.complete) stopPlayback();
    render();
  }, Number($("#speed-select").value));
  model.step();
  if (model.complete) stopPlayback();
  render();
}

function artifactName(event) {
  if (!event?.artifact) return "no artifact payload";
  if (event.kind === "input") return "clean-input.json";
  if (event.kind === "revise") return "review receipt";
  if (event.kind === "delivery") return "accepted deliveries";
  if (event.node === "profile" || event.node === "profile-review") return "profile.json";
  return "clean.json";
}

function renderTimeline(snapshot) {
  const timeline = $("#timeline");
  timeline.replaceChildren();
  if (snapshot.visibleEvents.length === 0) {
    const empty = document.createElement("li");
    empty.className = "timeline-empty";
    empty.textContent = "Events will appear here as the run advances.";
    timeline.append(empty);
    return;
  }
  snapshot.visibleEvents.forEach((event, index) => {
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.className = `timeline-event ${index === snapshot.cursor - 1 ? "current" : ""}`;
    button.dataset.cursor = String(index + 1);
    button.setAttribute("aria-label", `Revisit event ${index + 1}: ${event.title}`);
    const marker = document.createElement("span");
    marker.className = `timeline-marker ${event.kind}`;
    marker.textContent = event.kind === "accepted" || event.kind === "delivery" ? "✓" : event.kind === "revise" ? "!" : String(index + 1);
    const copy = document.createElement("span");
    copy.className = "timeline-copy";
    const title = document.createElement("strong");
    title.textContent = event.title;
    const meta = document.createElement("small");
    meta.textContent = `${event.at} · ${event.kind}`;
    copy.append(title, meta);
    button.append(marker, copy);
    item.append(button);
    timeline.append(item);
  });
}

function renderNodes(snapshot) {
  for (const node of NODE_ORDER) {
    const element = $(`.graph-node[data-node="${node}"]`);
    const state = snapshot.nodes[node];
    element.dataset.state = state;
    element.classList.toggle("selected", selectedNode === node);
    element.classList.toggle("event-origin", snapshot.currentEvent?.node === node);
    element.querySelector(".node-state").textContent = state;
    element.setAttribute("aria-label", `${NODE_COPY[node].label}: ${state}. Select to inspect this stage.`);
  }
  const copy = NODE_COPY[selectedNode];
  const explainer = $("#node-explainer");
  explainer.replaceChildren();
  const title = document.createElement("strong");
  title.textContent = copy.title;
  const text = document.createElement("span");
  text.textContent = copy.text;
  explainer.append(title, text);
}

function render() {
  const snapshot = model.snapshot();
  const statusLabels = {ready: "Ready to run", running: SCENARIOS[model.scenario].label, accepted: "All outputs accepted"};
  $("#run-status").textContent = statusLabels[snapshot.status];
  $("#run-dot").dataset.status = snapshot.status;
  $("#step-count").textContent = `${snapshot.cursor} / ${snapshot.total} events`;
  $("#progress-fill").style.width = `${(snapshot.cursor / snapshot.total) * 100}%`;
  $("#play-button").innerHTML = timer === null
    ? `<span aria-hidden="true">▶</span> ${snapshot.complete ? "Replay tour" : snapshot.cursor ? "Resume" : "Play tour"}`
    : `<span aria-hidden="true">Ⅱ</span> Pause`;
  $("#step-button").disabled = snapshot.complete;

  renderNodes(snapshot);
  renderTimeline(snapshot);

  const event = snapshot.currentEvent;
  $("#empty-event").hidden = Boolean(event);
  $("#event-content").hidden = !event;
  if (event) {
    $("#event-kind").textContent = event.kind.toUpperCase();
    $("#event-kind").dataset.kind = event.kind;
    $("#event-time").textContent = event.at;
    $("#event-title").textContent = event.title;
    $("#event-description").textContent = event.description;
    $("#artifact-content").textContent = event.artifact ?? "This control event has no artifact payload. Its state transition and execution identity are recorded in the run history.";
    $("#tour-announcement").textContent = event.title;
  } else {
    $("#artifact-content").textContent = "No payload yet. Step through the run to inspect frozen input, candidates, and review receipts.";
    $("#tour-announcement").textContent = "Tour reset";
  }
  $("#artifact-name").textContent = artifactName(event);
}

function selectScenario(scenario) {
  stopPlayback();
  model.reset(scenario);
  selectedNode = "define";
  for (const button of $$(".scenario-button")) {
    const active = button.dataset.scenario === scenario;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  }
  render();
}

function selectCode(key) {
  selectedCode = key;
  const example = codeExamples[key];
  $("#code-file").textContent = example.file;
  $("#code-content").textContent = example.code;
  $("#code-panel").setAttribute("aria-labelledby", `tab-${key}`);
  const notes = $("#code-notes");
  notes.replaceChildren();
  const label = document.createElement("span");
  label.className = "note-index";
  label.textContent = example.label;
  const title = document.createElement("h3");
  title.textContent = example.title;
  const text = document.createElement("p");
  text.textContent = example.text;
  const link = document.createElement("a");
  link.href = "https://github.com/GinkoAstra/AgentGenome/blob/main/rust/assets/teacher/sop-guide.md";
  link.textContent = "Read the full protocol ↗";
  notes.append(label, title, text, link);
  for (const tab of $$("[data-code]")) {
    const active = tab.dataset.code === key;
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
  }
}

const featureViewport = $("#feature-viewport");
const featureCards = $$("[data-feature]");
const featureDots = $$("[data-feature-target]");
const featureMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
let activeFeature = 0;
let featureScrollFrame = null;
let featureResizeFrame = null;

function renderFeature(index, announce = false) {
  activeFeature = index;
  featureCards.forEach((card, position) => {
    const active = position === index;
    card.classList.toggle("featured", active);
    if (active) card.setAttribute("aria-current", "true");
    else card.removeAttribute("aria-current");
  });
  featureDots.forEach((dot, position) => {
    const active = position === index;
    dot.classList.toggle("active", active);
    dot.setAttribute("aria-pressed", String(active));
  });
  $("#feature-prev").disabled = index === 0;
  $("#feature-next").disabled = index === featureCards.length - 1;
  $("#feature-page").textContent = `${String(index + 1).padStart(2, "0")} / ${String(featureCards.length).padStart(2, "0")}`;
  if (announce) {
    const title = featureCards[index].querySelector("h3").textContent;
    $("#feature-status").textContent = `Showing feature ${index + 1} of ${featureCards.length}: ${title}`;
  }
}

function centerFeature(index, smooth = true) {
  const cardRect = featureCards[index].getBoundingClientRect();
  const viewportRect = featureViewport.getBoundingClientRect();
  const delta = cardRect.left + cardRect.width / 2 - viewportRect.left - viewportRect.width / 2;
  featureViewport.scrollTo({left: featureViewport.scrollLeft + delta, behavior: smooth && !featureMotion.matches ? "smooth" : "auto"});
}

function selectFeature(index, announce = true) {
  const next = Math.max(0, Math.min(index, featureCards.length - 1));
  renderFeature(next, announce);
  centerFeature(next);
}

featureViewport.addEventListener("scroll", () => {
  if (featureScrollFrame !== null) return;
  featureScrollFrame = requestAnimationFrame(() => {
    featureScrollFrame = null;
    const viewportRect = featureViewport.getBoundingClientRect();
    const center = viewportRect.left + viewportRect.width / 2;
    let nearest = 0;
    let distance = Infinity;
    featureCards.forEach((card, index) => {
      const rect = card.getBoundingClientRect();
      const candidate = Math.abs(rect.left + rect.width / 2 - center);
      if (candidate < distance) {
        nearest = index;
        distance = candidate;
      }
    });
    renderFeature(nearest);
  });
}, {passive: true});
featureViewport.addEventListener("keydown", (event) => {
  if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
  event.preventDefault();
  selectFeature(activeFeature + (event.key === "ArrowRight" ? 1 : -1));
});
featureCards.forEach((card, index) => card.addEventListener("click", () => selectFeature(index)));
featureDots.forEach((dot) => dot.addEventListener("click", () => selectFeature(Number(dot.dataset.featureTarget))));
$("#feature-prev").addEventListener("click", () => selectFeature(activeFeature - 1));
$("#feature-next").addEventListener("click", () => selectFeature(activeFeature + 1));
window.addEventListener("resize", () => {
  if (featureResizeFrame !== null) cancelAnimationFrame(featureResizeFrame);
  featureResizeFrame = requestAnimationFrame(() => {
    featureResizeFrame = null;
    centerFeature(activeFeature, false);
  });
});
renderFeature(0);
centerFeature(0, false);

async function copyText(text, button) {
  try {
    await navigator.clipboard.writeText(text);
    const previous = button.textContent;
    button.textContent = "Copied";
    setTimeout(() => { button.textContent = previous; }, 1600);
  } catch {
    button.textContent = "Copy unavailable";
  }
}

$("#play-button").addEventListener("click", () => {
  if (timer === null) startPlayback();
  else {
    stopPlayback();
    render();
  }
});
$("#step-button").addEventListener("click", () => {
  stopPlayback();
  model.step();
  render();
});
$("#reset-button").addEventListener("click", () => selectScenario(model.scenario));
$("#speed-select").addEventListener("change", () => {
  if (timer !== null) {
    stopPlayback();
    timer = setInterval(() => {
      model.step();
      if (model.complete) stopPlayback();
      render();
    }, Number($("#speed-select").value));
    render();
  }
});
for (const button of $$(".scenario-button")) {
  button.addEventListener("click", () => selectScenario(button.dataset.scenario));
}
for (const button of $$(".graph-node")) {
  button.addEventListener("click", () => {
    selectedNode = button.dataset.node;
    render();
  });
}
$("#timeline").addEventListener("click", (event) => {
  const button = event.target.closest("[data-cursor]");
  if (!button) return;
  stopPlayback();
  model.seek(Number(button.dataset.cursor));
  render();
});
for (const tab of $$("[data-code]")) {
  tab.addEventListener("click", () => selectCode(tab.dataset.code));
  tab.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
    event.preventDefault();
    const keys = Object.keys(codeExamples);
    const direction = event.key === "ArrowRight" ? 1 : -1;
    const next = keys[(keys.indexOf(selectedCode) + direction + keys.length) % keys.length];
    selectCode(next);
    $(`#tab-${next}`).focus();
  });
}
for (const button of $$("[data-layer]")) {
  button.addEventListener("click", () => {
    const layer = architectureLayers[button.dataset.layer];
    $("#layer-label").textContent = layer.label;
    $("#layer-title").textContent = layer.title;
    $("#layer-text").textContent = layer.text;
    $("#layer-boundary").textContent = layer.boundary;
    for (const candidate of $$("[data-layer]")) {
      const active = candidate === button;
      candidate.classList.toggle("active", active);
      candidate.setAttribute("aria-pressed", String(active));
    }
  });
}
$("#copy-code").addEventListener("click", (event) => copyText(codeExamples[selectedCode].code, event.currentTarget));
$("#copy-install").addEventListener("click", (event) => copyText($("#install-commands").textContent, event.currentTarget));

selectCode("graph");
render();
