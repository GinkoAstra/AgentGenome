# Examples

These examples use the same Graph `/2` control plane for different purposes. Business rules live in Task definitions and explicitly deployed Checkers; the Scheduler remains business-agnostic.

## Choose an example

| Example | Best for | Workers and reviewers | Input | Expected delivery |
| --- | --- | --- | --- | --- |
| [Clean + profile](agent-workflow/graph-clean.yaml) | A first business workflow with deterministic acceptance | Pi Workers; trusted program Checkers | `clean-input.json` | `clean.json` + `profile.json` |
| [Evidence Goal](agent-workflow/graph-goal.yaml) | Source-grounded work with deterministic rendering | Pi evidence Worker; trusted programs | `research-notes.md` | `quotes.json` + offline `lesson.html` |
| [Graph Builder](agent-workflow/GRAPH-BUILDER.md) | Exploring conversational Graph creation | Pi Worker and Pi Checker | Confirmed requirements | Candidate YAML; real API validation still required |
| [Two-stage protocol](agent-workflow/graph-two-stage.yaml) | Control-plane dependency and review wiring | Controlled program fixtures | Protocol fixture | Not a business result |
| [Human start gate](agent-workflow/graph-human-gate.yaml) | Explicit confirmation before entering a learning practice stage | Pi Workers and reviewer; publisher confirmation | Frozen `topic` material | Accepted notes and practice artifacts |
| [Host command probe](agent-workflow/graph-command-probe.yaml) | Explicitly authorized host command execution and independent review | Qoder Worker and Qoder Checker | Read-only `ss`/`lsof` probe | Protocol evidence, not a security sandbox |
| [External programs](programs/) | Deterministic cleaning and evidence Checkers | Independently deployed Python processes | Absolute code path + matching SHA256 | Trusted review or rendering behavior |

The [detailed example guide](agent-workflow/README.md) provides the exact trusted-program configuration, startup commands, expected outputs, human-confirmation flow, and cleanup steps.

## Recommended path

1. Open the [interactive tour](../docs/showcase/) to understand candidate review and revision with scripted local data.
2. Follow the [Quickstart](../docs/quickstart.md) to build the Rust Core and install a native client entry.
3. Run the [cleaning example](agent-workflow/README.md) in an isolated data directory.
4. Inspect real review receipts and export only accepted artifacts.
5. Verify the exported bytes independently before treating them as a business delivery.

A static tour, a terminal Run state, a Checker receipt, and an independent output check answer different questions. Keep all relevant evidence with your result.

## What the examples prove

### Clean + profile

The Worker normalizes six rows, drops two invalid rows and one duplicate, and reports audit counters. A trusted program Checker recomputes the expected result from the frozen original input. The profile stage starts only after the clean output is accepted.

This is the smallest complete example of **candidate → independent review → committed accepted output → dependent work**.

### Evidence Goal

The Agent selects verbatim, contiguous excerpts from pinned local notes. Trusted programs check source coverage and correspondence, render a fixed offline page, and byte-compare the result.

This example verifies source correspondence and deterministic rendering—not open-ended research quality, current web facts, or scientific truth. It has no web tool.

### Graph Builder

Graph Builder converts confirmed requirements into candidate Graph YAML. Its output still needs real API validation and deliberate publication. Structural validation does not promise that every generated SOP is useful or that a model will always produce publishable YAML.

### Two-stage protocol

This deterministic fixture exercises Graph dependencies and review wiring. It must not be presented as a successful data-cleaning or business workflow.

### Human start gate

The `practice` Node waits for an explicit publisher confirmation before its first Worker is bound. Trace displays the waiting state but cannot approve it. Starting a Run is not implicit consent to the gate prompt, and the Teacher must not confirm on the user's behalf.

Structural and protocol tests cover the gate; real-model teaching quality is not thereby accepted.

### Host command probe

The Qoder Worker receives an explicit `run_command` grant for a narrow read-only host probe, and an independent Checker reviews its result. Native command execution follows the host CLI's own permissions and is **not sandboxed**. Do not generalize this example into arbitrary command authorization.

## Operational safety

Trusted external programs are registered at service startup through `--program-config`. Their code path, interpreter, capabilities, and SHA256 are validated. Use separate data directories for examples with different trusted deployments, and stop only services you started.

Run-specific artifacts, Teacher workspaces, credentials, and event logs belong in a local work directory, not in the source examples. Python is used by optional example programs only; the native Core does not depend on it.
