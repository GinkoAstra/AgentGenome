# Core concepts

AgentGenome coordinates work that must be repeatable, inspectable, and accepted against explicit criteria. Its central rule is simple: **publishing a candidate and accepting a result are different facts.**

## From a confirmed SOP to an accepted Run

```text
1. Author       Teacher + human agree on inputs, outputs, tools, and acceptance rules
2. Define       Graph YAML declares Agents, Tasks, Nodes, dependencies, and budgets
3. Publish      API freezes the definition and returns a content-addressed reference
4. Freeze       Run inputs are uploaded as immutable artifacts
5. Execute      Scheduler, Controller, and Backend run Nodes as dependencies become ready
6. Review       A parent or declared Checker accepts, revises, or expands a candidate
7. Commit       Scheduler persists the accepted candidate as the Node output
8. Deliver      Only accepted artifacts become downstream inputs or exported deliveries
9. Observe      Trace reads the same persistent facts without controlling the Run
```

A Teacher helps author and operate this workflow. It is not a second runtime, and closing the Teacher session does not stop the service or cancel a Run.

## Graph, revision, and Run

A **Graph** is a versioned workflow definition. It contains named Agents, Tasks, Nodes, dependencies, input bindings, and budgets. The current format is `agentgenome-graph-definition/2`.

Publishing produces a `definition_ref` for exact definition bytes and a revision number. A **Run** pins one definition reference and complete input artifact references. Editing a local YAML or input file later cannot reinterpret an existing Run.

To evolve a workflow, publish a new revision against the expected `base_revision`, then create a new Run. The old definition, Run, reviews, and outputs remain historical facts.

## Agent, Task, and Node

An **Agent** defines how work is executed: Backend, instructions, control-plane tools, and permitted backend bridges. It is an execution definition, not a persisted Run.

A **Task** defines a unit of work and its contract:

- A `candidate` Task produces proposed output artifacts.
- A `review_receipt` Task evaluates one bound candidate and returns exactly one verdict.

A **Node** binds a candidate Task, reviewer, dependencies, and inputs into the Graph. All declared Nodes belong to the Run; recursive expansion can add child work without changing the frozen parent contract.

```yaml
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
      clean: {from: clean}
```

The `profile` stage cannot consume an arbitrary draft from `clean`. Its input binding resolves to the accepted `clean` output.

## Worker, execution, and epoch

A **Worker** is the recursive task role responsible for a Node. An **Agent execution** is the complete multi-turn instance that performs that work; it may include model requests, native tools, and session continuation.

Technical identities such as `worker_id`, `epoch`, and `delivery_key` bind an actual execution to persistent task state. This lets the control plane distinguish a retried delivery, a resumed execution, and a new candidate round, and lets Trace select the correct execution details.

The simplest SOP can be **one Node = one Worker = one Agent**. A single model response is only one part of that execution, not proof of completion.

## Candidate, review, and accepted state

A Worker publishes artifacts and submits a candidate with a unique `candidate_id`, `round`, and complete artifact references. This records a proposal; it does not complete the Node. The Node remains active and then awaits review rather than becoming an accepted output.

The declared parent or Checker inspects the exact candidate and its authorized inputs, then submits a receipt:

- `accept` — the candidate satisfies the declared contract and can be committed as output.
- `revise` — feedback must be delivered before the Worker continues with a later round.
- `expand` — work can be decomposed under the Graph's expansion policy and budgets.

A review receipt is bound to the exact candidate and round. A model saying “done” is not a review receipt.

Even an `accept` receipt has one more control-plane step: the Scheduler must persist the Node as `succeeded` with `accepted_candidate_ref`. Only that committed accepted output unlocks dependent Nodes. The [interactive tour](showcase/) compresses this transition into a visual event, but real reports expose the underlying facts.

For deterministic business rules, a trusted program Checker can recompute expected output instead of asking another model for an opinion. Program Checkers are explicit deployment configuration, not arbitrary executable code loaded from a Graph.

## Artifacts

An **Artifact** is immutable content with this identity:

```text
id = sha256:<digest>
sha256 + size + media_type + stored bytes
```

The artifact store verifies content identity when bytes are written and read. Candidates contain complete artifact references, not just local paths. Definitions, Run inputs, feedback, and accepted outputs therefore form a stable chain:

```text
Graph revision + input hashes + candidate/review receipts + accepted output hashes
```

Downstream Nodes and human exports consume accepted bytes. A file left in a Worker workspace is not automatically a delivery.

## Scheduling and execution responsibilities

| Component | Responsibility |
| --- | --- |
| API | Authentication, permissions, protocol/version checks, transactions, idempotency, and persistence |
| Scheduler | Dependency advancement, expansion, accepted-state transitions, capacity, and execution binding |
| Controller | Coordination of execution intents already committed by the Scheduler |
| Backend | Real Agent or trusted program execution and observation |
| Trace | Read-only snapshots, events, resources, and execution details over the same service |

The Scheduler does not know what makes an email valid, an audit counter correct, or a lesson useful. Those rules belong to Tasks and Checkers. Backends do not declare their own candidates accepted, and Trace does not become a second scheduler.

## Persistence, pause, and recovery

SQLite stores authoritative state, events, and revisions; the artifact store holds input and output bytes. Runs persist independently of the Teacher conversation.

Pausing a Run, cancelling a Run, and stopping a service are different operations. Restart behavior follows persisted facts, backend capabilities, and declared budgets. It is not a promise to replay arbitrary external side effects safely or reproduce model output byte for byte.

## What acceptance does—and does not—mean

Keep these claims separate:

1. **Candidate submitted** — a Worker proposed exact artifacts.
2. **Accepted state committed** — the declared review protocol accepted a candidate and the Scheduler persisted it as the Node output.
3. **Business result independently verified** — post-run checks or human review confirm that the delivered bytes satisfy the real business need.

AgentGenome preserves the evidence connecting these claims. It does not turn a semantic Checker into a universal proof of correctness, and a terminal Run state alone is not a substitute for inspecting the actual receipts and artifacts.
