export const NODE_ORDER = ["define", "clean", "clean-review", "profile", "profile-review", "delivery"];

export const NODE_COPY = {
  define: {
    label: "Freeze",
    title: "Definition + inputs",
    text: "Publishing freezes the Graph /2 definition. Uploading the source data creates an immutable, content-addressed input artifact."
  },
  clean: {
    label: "Clean",
    title: "Candidate-producing Worker",
    text: "The Pi Worker normalizes names and emails, removes invalid and duplicate rows, then publishes clean.json as a candidate."
  },
  "clean-review": {
    label: "Check",
    title: "Independent cleaning Checker",
    text: "A trusted program recomputes the expected rows and audit counters from the frozen input. It accepts only an exact match."
  },
  profile: {
    label: "Profile",
    title: "Dependent Worker",
    text: "This stage starts after cleaning is accepted. It counts rows and domains from the accepted clean.json, not from an arbitrary draft."
  },
  "profile-review": {
    label: "Check",
    title: "Independent profile Checker",
    text: "The second program Checker recomputes the profile and binds its verdict to the exact candidate and round."
  },
  delivery: {
    label: "Deliver",
    title: "Accepted artifacts",
    text: "Only reviewed outputs become deliveries. The accepted bytes remain tied to the Graph revision, inputs, candidates, and receipts."
  }
};

const FINAL_CLEAN = `{
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
}`;

const FINAL_PROFILE = `{
  "rows": 3,
  "domains": {"example.com": 3}
}`;

const sharedStart = [
  {
    at: "00:00",
    kind: "definition",
    title: "Graph definition published",
    description: "The clean → profile SOP is validated and frozen as a content-addressed Graph /2 definition.",
    node: "define",
    changes: {define: "active"}
  },
  {
    at: "00:01",
    kind: "input",
    title: "Six source rows frozen",
    description: "The input artifact now has a stable hash. Later edits to a local file cannot change this run.",
    node: "define",
    changes: {define: "accepted"},
    artifact: `[
  {"name":" Alice ","email":" ALICE@EXAMPLE.COM "},
  {"name":"Alice duplicate","email":"alice@example.com"},
  {"name":" Bob ","email":"bob@example.com"},
  {"name":"Blank","email":""},
  {"name":" Carol ","email":"CAROL@example.com"},
  {"name":"Invalid","email":"bad-address"}
]`
  },
  {
    at: "00:02",
    kind: "execution",
    title: "Clean Worker started",
    description: "The Scheduler binds a Worker to the clean Node. Frozen inputs are provided as read-only artifacts; native CLI tools keep the host's configured permissions.",
    node: "clean",
    changes: {clean: "active"}
  }
];

const cleanCandidate = {
  at: "00:08",
  kind: "candidate",
  title: "Candidate submitted · round 1",
  description: "The Worker publishes clean.json and submits a candidate. The Node is still waiting for review—not complete.",
  node: "clean",
  changes: {clean: "review", "clean-review": "active"},
  artifact: FINAL_CLEAN
};

const profileAndDelivery = [
  {
    at: "00:16",
    kind: "execution",
    title: "Profile Worker started",
    description: "The accepted clean artifact satisfies the dependency and becomes the profile stage's frozen input.",
    node: "profile",
    changes: {profile: "active"}
  },
  {
    at: "00:21",
    kind: "candidate",
    title: "Profile candidate submitted",
    description: "The Worker counts three accepted rows and one normalized domain, then submits profile.json for review.",
    node: "profile",
    changes: {profile: "review", "profile-review": "active"},
    artifact: FINAL_PROFILE
  },
  {
    at: "00:23",
    kind: "accepted",
    title: "Profile candidate accepted",
    description: "The Checker independently recomputes both counters and accepts this exact candidate. The Scheduler persists the accepted candidate reference before delivery.",
    node: "profile-review",
    changes: {profile: "accepted", "profile-review": "accepted"},
    artifact: FINAL_PROFILE
  },
  {
    at: "00:24",
    kind: "delivery",
    title: "Run complete · outputs accepted",
    description: "Both business stages have accepted artifacts. The run now has a traceable delivery, not merely a model claiming success.",
    node: "delivery",
    changes: {delivery: "accepted"},
    artifact: `${FINAL_CLEAN}\n\n--- profile.json ---\n${FINAL_PROFILE}`
  }
];

export const SCENARIOS = {
  happy: {
    label: "Accepted on first pass",
    shortLabel: "Happy path",
    events: [
      ...sharedStart,
      cleanCandidate,
      {
        at: "00:11",
        kind: "accepted",
        title: "Clean candidate accepted",
        description: "The Checker's accept receipt matches its independent recomputation. The Scheduler then persists the accepted candidate as the clean Node output.",
        node: "clean-review",
        changes: {clean: "accepted", "clean-review": "accepted"},
        artifact: FINAL_CLEAN
      },
      ...profileAndDelivery
    ]
  },
  revision: {
    label: "Checker requests a revision",
    shortLabel: "Revision loop",
    events: [
      ...sharedStart,
      {
        ...cleanCandidate,
        artifact: FINAL_CLEAN.replace('"duplicate_rows": 1', '"duplicate_rows": 0')
      },
      {
        at: "00:11",
        kind: "revise",
        title: "Checker rejects the audit count",
        description: "The rows are correct, but duplicate_rows must be 1. The Checker returns revise with concrete feedback tied to round 1.",
        node: "clean-review",
        changes: {clean: "revise", "clean-review": "revise"},
        artifact: `{
  "verdict": "revise",
  "candidate_round": 1,
  "feedback": "audit.duplicate_rows: expected 1, got 0"
}`
      },
      {
        at: "00:12",
        kind: "execution",
        title: "Worker resumes for round 2",
        description: "The same frozen contract and input remain in force. The Worker receives the review feedback and corrects the counter.",
        node: "clean",
        changes: {clean: "active", "clean-review": "idle"}
      },
      {
        ...cleanCandidate,
        at: "00:14",
        title: "Corrected candidate submitted · round 2",
        description: "A new candidate round references the corrected artifact. The original candidate and rejection remain in history.",
        changes: {clean: "review", "clean-review": "active"}
      },
      {
        at: "00:15",
        kind: "accepted",
        title: "Round 2 accepted",
        description: "The Checker recomputes from the frozen source and accepts round 2. The Scheduler commits this candidate as the accepted clean output.",
        node: "clean-review",
        changes: {clean: "accepted", "clean-review": "accepted"},
        artifact: FINAL_CLEAN
      },
      ...profileAndDelivery
    ]
  }
};

function initialNodes() {
  return Object.fromEntries(NODE_ORDER.map((node) => [node, "idle"]));
}

export class RunModel {
  constructor(scenario = "happy") {
    this.reset(scenario);
  }

  reset(scenario = this.scenario) {
    if (!SCENARIOS[scenario]) {
      throw new Error(`Unknown scenario: ${scenario}`);
    }
    this.scenario = scenario;
    this.cursor = 0;
    this.nodes = initialNodes();
    return this.snapshot();
  }

  seek(cursor) {
    if (!Number.isInteger(cursor) || cursor < 0 || cursor > this.events.length) {
      throw new RangeError("Cursor must identify a position in the selected scenario");
    }
    this.cursor = 0;
    this.nodes = initialNodes();
    for (let index = 0; index < cursor; index += 1) {
      this.apply(this.events[index]);
    }
    this.cursor = cursor;
    return this.snapshot();
  }

  step() {
    if (this.complete) return this.snapshot();
    this.apply(this.events[this.cursor]);
    this.cursor += 1;
    return this.snapshot();
  }

  apply(event) {
    Object.assign(this.nodes, event.changes);
  }

  get events() {
    return SCENARIOS[this.scenario].events;
  }

  get complete() {
    return this.cursor === this.events.length;
  }

  snapshot() {
    return {
      scenario: this.scenario,
      cursor: this.cursor,
      total: this.events.length,
      complete: this.complete,
      status: this.complete ? "accepted" : this.cursor === 0 ? "ready" : "running",
      currentEvent: this.cursor === 0 ? null : this.events[this.cursor - 1],
      visibleEvents: this.events.slice(0, this.cursor),
      nodes: {...this.nodes}
    };
  }
}
