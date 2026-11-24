# AgentGenome documentation

AgentGenome is a persistent control plane for versioned, review-gated Agent workflows. Start with the path that matches what you want to do.

## Learn

- [Project README](../README.md) — value, features, installation, backends, architecture, and boundaries.
- [中文说明](../README.zh-CN.md) — 项目目标、完整安装说明、使用流程与能力边界。
- [Core concepts](concepts.md) — Graphs, Runs, Tasks, Workers, candidates, reviews, artifacts, and Trace.
- [Interactive tour](showcase/) — a scripted browser walkthrough of candidate acceptance and revision.

## Install and use

- [Quickstart](quickstart.md) — build the Rust Core, run the cleaning SOP, inspect Trace, and publish a new revision.
- [Local installation](local-install.md) — native Core and managed-service installation details.
- [Pi integration](pi-extension.md) — slash commands, lifecycle, terminal panel, and troubleshooting.
- [CLI and MCP access](client-access.md) — supported hosts, activation, and verification scope.
- [Trace terminal and browser views](trace.md) — read-only observation and credential boundaries.
- [Trace UI](trace-ui.md) — stages, status, themes, and keyboard controls.

## Author a workflow

- [Graph /2 protocol](../rust/assets/teacher/sop-guide.md) — authoritative fields and runtime protocol distributed with the Core.
- [SOP template](../rust/assets/teacher/sop-template.yaml) — a starting point for a complete Graph.
- [Worker tools and execution boundaries](worker-commands.md) — native CLI permissions, explicit `run_command` grants, and limitations.
- [Example catalog](../examples/README.md) — choose a business example, protocol fixture, or human-confirmation flow.
- [Detailed example guide](../examples/agent-workflow/README.md) — exact inputs, trusted program setup, expected outputs, and operations.

## Understand and contribute

- [Architecture](architecture.md) — processes, components, persistence, and execution responsibilities.
- [Contributing](../CONTRIBUTING.md) — development setup, verification, and contribution expectations.
- [Showcase source](showcase/README.md) — local preview, static boundaries, tests, and GitHub Pages publication.

The interactive showcase is deliberately separate from the runtime. Only the HTTP/MCP API and real examples execute AgentGenome workflows; the static page neither persists a Run nor proves business acceptance.
