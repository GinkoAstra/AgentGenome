<div align="center">

# AgentGenome

**Turn a confirmed SOP into a persistent Agent workflow with explicit acceptance.**

A Rust control plane for versioned, review-gated Agent workflows.

`Core 0.2.1-dev` · `Graph /2` · `Pi + MCP` · `MIT`

[Why AgentGenome](#why-agentgenome) · [Interactive tour](#interactive-tour) · [Quickstart](#quickstart) · [Examples](#examples) · [Architecture](#architecture) · [中文说明](README.zh-CN.md)

</div>

---

> **A candidate submission is not an accepted result.** Workers propose artifacts; a parent or declared Checker reviews them; the Scheduler persists acceptance before downstream work can consume the output.

## Why AgentGenome

A capable Agent can call models and tools, but repeated business execution also needs explicit inputs, dependencies, acceptance rules, and recoverable state. AgentGenome turns those rules into a versioned Graph and runs it in a persistent service.

| Repeated-work problem | AgentGenome's model |
| --- | --- |
| The process and context are re-explained for every run | Publish a versioned Graph; create an independent Run with frozen inputs |
| “Done” in natural language is treated as delivery | Separate candidates, review receipts, and accepted outputs |
| Dependencies and rework are coordinated by hand | The Scheduler advances persistent Node relationships; the Controller coordinates execution |
| Closing the chat loses operational context | Reconnect to the same service and inspect Runs through Trace |
| A revised SOP makes old results ambiguous | Each Run remains pinned to its definition, input artifacts, candidates, and reviews |
| Changing the execution Agent rewrites workflow logic | Backends adapt execution while the API and acceptance semantics stay consistent |

AgentGenome is a good fit for repeatable work with clear deliverables and review criteria: data cleaning, fixed-rule report generation, evidence extraction, and combinations of existing SOP Agents. For one-off searches or temporary coding splits, an Agent's native subagents are usually simpler.

The runtime records and executes the declared acceptance protocol. The quality of that protocol—and the business truth of an accepted result—still depends on the Task, Checker, and evidence you provide.

## Interactive tour

Explore the two-stage cleaning workflow in a dependency-free browser tour:

- **Happy path:** a Worker submits a candidate and independent program Checkers accept both stages.
- **Revision loop:** a Checker rejects an incorrect audit count, returns feedback, and accepts a corrected second round.
- Select Graph nodes, inspect Task contracts and accepted artifacts, and explore API / Scheduler / Controller / Backend / Trace responsibilities.
- Play, pause, step, change speed, or revisit any event in the timeline.

**[Open the interactive tour](https://ginkoastra.github.io/AgentGenome/)** · [Tour source and local preview](docs/showcase/)

The tour uses scripted local data. It explains the protocol without connecting to a runtime; real acceptance requires a real Run, receipts, and artifacts.

## How it works

```text
Confirm the SOP and its acceptance rules
                ↓
Validate and publish Graph YAML → freeze inputs → create a Run
                                                   ↓
                                      Worker submits a candidate
                                                   ↓
                                      Parent / Checker reviews it
                                           ↙               ↘
                                    revise + feedback       accept
                                           ↓               ↓
                                    next candidate round   Scheduler persists acceptance
                                                           ↓
                                            accepted artifacts unlock downstream Nodes
```

- **Versioned definitions:** Graph, Task, and Agent references preserve the exact contract used by each Run.
- **Recursive work:** parent/child relationships represent expansion; dependency edges represent execution order.
- **Explicit acceptance:** parent review, semantic Agent Checkers, and trusted program Checkers are supported.
- **Persistent state:** API state, execution facts, request IDs, and execution identities are stored independently of a chat session.
- **Read-only observation:** Pi, terminal, browser, HTTP snapshots, and SSE read the same service facts without becoming another scheduler.

## Quickstart

The source route builds the native Core and installs a host entry. Installation, service startup, and client activation are separate steps: none of them silently performs the others.

### Prerequisites

- Linux x86_64 with glibc is the currently validated target.
- A Rust toolchain for the source build.
- Pi CLI with a configured provider and model. Node.js 20+ is only required for the optional precompiled-binary installer and installer tests.
- Python 3 only for the optional external programs used by the cleaning and evidence examples; the Core itself does not require Python.

### 1. Build and install the Pi entry

```bash
cargo build --release --locked --manifest-path rust/Cargo.toml
CORE="$PWD/rust/target/release/agentgenome"
"$CORE" client install --client pi
```

This entry points to the build artifact's absolute path. Keep it in place, or copy the binary to a stable location before running `client install` from that path.

### 2. Start the service explicitly

```bash
"$CORE" start --backend pi \
  --provider YOUR_PROVIDER --model YOUR_MODEL
```

Use `--data-dir PATH` consistently across commands if you want an isolated service. Codex, Qoder, and Claude Code backends use their own CLI configuration; see [client access](docs/client-access.md).

### 3. Activate a new client session

Open a new Pi session and enter:

```text
/agentgenome
```

Ask the Teacher to validate, publish, and run a Graph. Publishing a definition does not create a Run. Closing Pi does not stop the service or cancel work.

For a complete, isolated cleaning Run—including trusted Checker configuration, exact CLI commands, accepted-output checks, Trace, and Graph revision—follow the **[Quickstart guide](docs/quickstart.md)**.

## Examples

| Example | What it demonstrates | Execution |
| --- | --- | --- |
| [Clean + profile](examples/agent-workflow/graph-clean.yaml) | Six rows are normalized and deduplicated; statistics consume only the accepted clean artifact | Pi Workers + trusted program Checkers |
| [Evidence Goal](examples/agent-workflow/graph-goal.yaml) | Verbatim evidence from frozen notes and deterministic offline rendering | Pi + trusted programs |
| [Graph Builder](examples/agent-workflow/GRAPH-BUILDER.md) | Conversational conversion of confirmed requirements into candidate Graph YAML | Pi Worker + Pi Checker; API validation still required |
| [Two-stage protocol](examples/agent-workflow/graph-two-stage.yaml) | Deterministic dependency and execution wiring | Controlled program fixture, not a business result |
| [Human start gate](examples/agent-workflow/graph-human-gate.yaml) | A learning SOP waits for explicit user confirmation before first entering practice | Pi + publisher confirmation |
| [Host command probe](examples/agent-workflow/graph-command-probe.yaml) | Explicitly authorized read-only `ss`/`lsof` commands with independent review | Qoder Worker + Qoder Checker |

Start with the **[example catalog](examples/README.md)** and **[detailed example guide](examples/agent-workflow/README.md)**. Trusted programs are loaded only through explicit `--program-config` deployment; a Graph cannot import arbitrary Python code. The command probe is not a sandbox.

## Core concepts

| Object | Meaning |
| --- | --- |
| Graph | A versioned workflow definition containing Agents, Tasks, Nodes, dependencies, and input bindings |
| Run | One execution frozen to a definition revision and content-addressed input artifacts |
| Task / Node | A Task declares work and its contract; a Node is a logical task instance in a Run |
| Worker | The recursive task role responsible for a Node; `worker_id + epoch` identifies an execution binding |
| Agent | A complete execution instance that may span model calls, tool calls, and resumed sessions |
| Candidate / Review | Proposed outputs and the verdict bound to an exact candidate and round |
| Artifact | Immutable bytes with a SHA256 content identity, size, and media type |
| Trace | A read-only projection of persistent state, events, reviews, and execution details |

A candidate submission leaves the Node awaiting review. Even an `accept` receipt is not the final fact by itself: the Scheduler must persist `succeeded` with `accepted_candidate_ref`. Only then does the output become available to dependent Nodes.

Read **[Core concepts](docs/concepts.md)** for the full model and **[Graph /2 protocol](rust/assets/teacher/sop-guide.md)** for field-level rules.

## Architecture

```text
┌─ Clients ────────────────────────────────────────────────┐
│ Ordinary Pi / Teacher      scripts / optional CLI        │
│ MCP or JSON HTTP           read-only terminal / web Trace│
└───────────────────────────┬──────────────────────────────┘
                            ↓
┌─ Persistent API service ─────────────────────────────────┐
│ API: authentication, versions, transactions, facts       │
│ Scheduler: dependencies, review outcomes, execution bind │
│ Controller: committed execution coordination             │
│ Backend adapters: Pi / CLI / trusted external programs   │
└───────────────┬───────────────────────────┬──────────────┘
                ↓                           ↓
       SQLite + artifact store       real Agent / program processes
```

Business rules stay in Tasks and Checkers. The Scheduler does not decide whether an email is valid or a report is correct, and Trace cannot advance a Run. See the [architecture guide](docs/architecture.md) for implementation details.

## Backends and current verification

The conversational entry and execution backend are independent choices. Pi's Teacher operates the same API through MCP; other hosts can use native MCP/skills entries without gaining Pi's terminal panel.

| Backend | Current status |
| --- | --- |
| Pi | Primary conversational entry and Agent execution backend; real business examples |
| External program (`script`) | Explicitly deployed deterministic Worker or Checker processes |
| Codex CLI | Real-model candidate, revision, same-session continuation, and acceptance flow verified |
| Qoder CLI | Real-model candidate, revision, continuation, and acceptance flow verified |
| Claude Code | Real CLI + controlled model protocol verified; real upstream-model acceptance remains pending |
| Kimi | Not implemented |

Native CLI tools, Skills, configuration, and permissions remain under the host CLI's control. AgentGenome does not bypass their approval settings by default. See [Worker tools and execution boundaries](docs/worker-commands.md).

## Documentation

- [Documentation index](docs/index.md)
- [Quickstart](docs/quickstart.md)
- [Core concepts](docs/concepts.md)
- [Pi integration](docs/pi-extension.md)
- [CLI and MCP access](docs/client-access.md)
- [Trace terminal and browser views](docs/trace.md)
- [Example catalog](examples/README.md)
- [中文说明](README.zh-CN.md)

## Development and tests

```bash
cargo test --locked --manifest-path rust/Cargo.toml
node --test tests/install/*.test.mjs
node --test tests/control_plane/showcase_simulation.test.mjs
```

The Rust suite covers definitions, permissions, versions, persistence, task expansion, revision, service communication, lifecycle, backend protocols, and Trace. Some integration tests require additional local tools described in the [development guide](CONTRIBUTING.md). Tests use controlled fixtures and do not prove real-model business quality.

## Current boundaries

Core `0.2.1-dev` uses Graph `/2` and SQLite schema `/3`. It currently targets a local persistent service on Linux x86_64 glibc. The npm package and precompiled Core have no public registry/release distribution yet.

AgentGenome does not claim distributed-cluster scheduling, arbitrary CLI compatibility, universal management of an Agent's own background subagents, deterministic model output, or safe replay of arbitrary external side effects. A semantic Checker records the declared review protocol; independently verify important business deliveries.

## Citation

```bibtex
@software{agentgenome2026,
  author = {GinkoAstra},
  title = {AgentGenome: Verification-Pinned Executable Task Graphs for LLM Agents},
  year = {2026},
  url = {https://github.com/GinkoAstra/AgentGenome}
}
```

## License

[MIT](LICENSE). Existing copyright notices are preserved in the license file.
