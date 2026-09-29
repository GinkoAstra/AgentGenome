# recursive-sop/1 implementation contract

B1: `sop.authoring`, `sop.skill_authoring`, `sop.sources`; B2: `sop.runtime`; B3: `sop.capabilities`, `sop.agents`, `sop/pi_worker.mjs`; B4: `sop.store`. Python standard library only for the new core. All public values are strict JSON (duplicate keys, NaN, infinity rejected). `sop.common` exports `SopError(code, message)`, `canonical(value) -> str`, `digest(value) -> sha256 hex`, `file_digest(Path)`, `read_json(Path)`, `write_json(Path, value)`.

Definition (immutable content-addressed JSON):
```
{"schema":"sop/1","id":"orders-report","task":"orders.report",
 "params":{"source_file":{"type":"artifact","required":true},"rule":{"type":"string","enum":["highest_revision","first"],"required":false}},
 "capabilities":["orders.profile","orders.normalize","orders.deduplicate","orders.summarize"],
 "steps":[{"id":"profile","kind":"fixed","capability":"orders.profile","inputs":{"source_file":"$input.source_file"}},
          {"id":"clean","kind":"pi","task":"orders.clean","inputs":{"source_file":"$input.source_file","rule":"$input.rule"}},
          {"id":"summary","kind":"fixed","capability":"orders.summarize","inputs":{"cleaned":"$steps.clean.cleaned","source_file":"$input.source_file"}}],
 "outputs":{"cleaned":"$steps.clean.cleaned","summary":"$steps.summary.summary","quality":"$steps.summary.quality"},
 "checks":["orders.report"],"recovery":[]}
```
Bindings are exactly `$input.NAME` or `$steps.ID.OUTPUT`; literal constants are JSON, literal path strings cannot become artifacts. Unknown optional binding resolves to null. Each step is fixed, pi, or call. `call` has `task`, `inputs`, and optional `definition` (hash). `pi` may request subtask at any depth subject to root budget. Definition `supported_rules` optionally restricts applicability. Clean SOP params source_file(required artifact), rule(required enum); normalize(source_file)->normalized; deduplicate(normalized,rule)->cleaned; checks=[orders.clean]. Capabilities include transitive child capabilities. Checks use immutable registry IDs; unknown checks fail closed. No inline code or arbitrary shell, definition permissions cannot exceed original run grant. Recovery rules: {id,phase,code,action,max_attempts}; phase execution/verification, action retry/recheck; registered forms additionally admit parameter_validation/invalid_value with action request_parameter_proposal, fields=[chunk_size], and a bounded count. Retry needs trusted not_dispatched evidence. No model-defined recovery changes.

B3 Registry API: `Registry()`; `.catalog()` dict capability ID -> {inputs: schema dict, outputs: list[str], version: digest, effects: 'local_artifact'}; `.task_contracts()` trusted input/output/role/capability contracts; `.checkers()` dict checker ID -> version; `.execute(capability, inputs, work_dir:Path) -> dict[str,Path]`; `.verify(task, inputs, outputs) -> {passed:bool, diagnostics:list, metrics:dict}`; inputs/outputs to B3 are trusted resolved local Paths (rule string); registry never receives definition code. Raise SopError with stable code for errors. Native artifacts only; verifier independently recomputes from source_file, not expected fixtures. Report outputs are cleaned.csv, summary.csv, quality.json. `orders.profile` output `profile`; normalize `normalized`; deduplicate `cleaned`; summarize `summary,quality`. Registry controls all filenames. Pin implementation and checker hashes. No external side effect capabilities shipped.

B1 API: `validate_definition(definition, registry) -> report` (passed,diagnostics,definition_hash,dependency_hashes,checker_versions); `root_definition(mode='pi')`; `clean_definition(rule=None)`; `Library(definitions)`; `prepare_method(task, inputs, library, registry, planner=None) -> {definition,origin,selection}`. Origin existing/generated/candidate_reuse; reject first-only when rule highest_revision; generate via injected planner `.plan(context)->definition` only when no existing match. Context contains task/rule/catalog, no expected fixtures. Planner absent => capability_missing, not silent generation. `Authoring(data_dir:Path, registry, model=None)`; `.submit(source_path, proposal=None)->draft`; `.answer(draft_id, request_id, value, message_id, revision)->draft`; `.get(draft_id)->draft`. Source ingested/snapshotted, line provenance and bidirectional requirement map; scope unsupported => stopped, never silently drop. Explicit known fixture/scoped text parsing is deterministic and clearly labelled; arbitrary interpretation via injected `.author(context)` model, never claim general NL accuracy. Draft states authoring/waiting_info/checking/delivered/stopped. Draft outputs definition, requirements, report, source_hash, answers, publication='unpublished', trials='not_run'. Same-id same-payload answer idempotent before stale revision check; conflict rejected. Different source/answers/dependencies invalidate current report. Compilation executes zero business capabilities.

B3 Agent API: `.respond(context)->dict` four returns `need_info` {field:'rule',reason}, `need_subtask` {task,inputs,reason,definition?}, `candidate_result` {outputs}, `cannot_continue` {reason}. Runtime wraps identity/token; context fields task,inputs (artifact handles only),facts,child_result (logical output handles),catalog,continuation,limits. Agent must never access filesystem or expected fixtures. `ScriptedAgent` deterministic protocol fixture explicitly marked; `ScriptedPlanner` creates clean composition from capability catalog (protocol test only). `PiAgent` and `PiPlanner` use genuine installed Pi agent loop; isolated context; read-only author/review/planner expose context/handoff. Execution Pi may additionally invoke a host-controlled capability tool; no arbitrary shell/file/network tools. Provider network is model transport only. Responses include optional `_usage`/`_trace`; expose actual model identity and errors, no fake fallback. Tool proposals routed to Python B2, not directly executed in Node. Pi worker never owns graph state. Runtime receives logical handles and validates parent-owned source/rule; cannot widen source via response.

B4: content-addressed JSON definitions and blobs under `.data/objects`; SQLite transaction stores run snapshot plus ordered events and message receipts. One process owns each run with flock. Intent persists before execute, result bytes before candidate commit. Restart from running intent => effect_unknown (even local scripts; conservative default); verifying resumes same candidate; child accepted/finished not regenerated/re-executed. Inputs snapshotted and hashed, artifacts reject symlinks; dependency drift blocks. State revisions plus message payload digest enforce deduplication; answers bound to exact owner/request/run only. Definition report is not execution authorization or result validation.

## Final v1 refinements

D04 discriminator is `kind`. Runtime envelope metadata is never accepted from model fields. Step `pi` candidate is saved as `verifying_agent` before checker dispatch. Fixed candidate uses `committing_step` after bytes are saved, so a restart commits the saved result without repeating the tool. Whole-task `verifying` also consumes the same immutable candidate. Unknown dispatch remains `effect_unknown`; v1 has no generic external reconciliation API.

The registered tasks are `orders.clean`, `orders.report`, `orders.prepare`, `orders.from_json`, and `orders.prepare_configured`, declared by the trusted Registry rather than separate compiler/runtime tables. `checks` must equal `[task]`. Input and output artifact roles are checked in addition to their JSON type. Rule bindings cannot replace the parent's accepted rule. A source may also bind a preceding verified preparation output with the source_file role; raw JSON has the distinct source_json role. Fixed paths have no model calls. Capability implementation/checker identity covers `capabilities.py`, `common.py`, `contracts.py`, and `input_preparation.py`; definition checks are rerun on restart and dependencies compared with admission evidence.

`Authoring` retains the explicitly labelled `scoped_orders_fixture_parser` baseline. `SemanticAuthoring(data_dir, registry, model, reviewer, max_model_calls=12)` implements text plus referenced local sources, separate model interpretation and independent source-based review. `collect_sources()` snapshots file content, hashes and line positions; native source paths never enter model context. `skill-analysis/1` must classify every nonempty line exactly once and cite precise registered semantic clauses. Unsupported claims stop delivery. The reviewer returns `skill-review/1` bound to source bundle, analysis and effective answers. Passing semantic review still requires definition validation and later independent business acceptance; model quality is unverified.

Draft question uses `status`; runtime question uses `state`. Draft answers use `answers.rule.value`; explicit per-run rule selection may leave answers empty with both rule choices declared. `.submit`, `.answer`, `.get`, `.resume` persist analysis, questions, receipts, reservations and reports. Generation and review have independent calls and share one persisted draft budget; lost replies consume their reservation. No automatic business trial occurs. A delivered report binds source/attachment, analysis, answer, definition, requirement map, review and implementation identities. Changed material invalidates it. The application hands the same definition/report/answers to runtime with separate execution authorization.

A `call` stores explicit `fact_bindings` only for a rule inherited from `$input.rule`. Child question choices intersect all mapped ancestor rule domains. Accepted answers recheck current definition constraints; returned facts are checked again before parent binding. A child without a method and missing rule waits before selection. Known instance-local facts may satisfy a request without asking the user; repeated identical requests stop as stagnation. Unrelated sibling facts are excluded. Context retains current contract, required outputs, checks, forbidden effects, capability catalog, and `information_query=context_only`.

Root budgets are `max_actions`, `max_model_calls`, `max_depth` (default 100/20/8). `usage.model_calls` reserves the adapter's maximum provider calls before dispatch, including planning; actual requests/Token are separate adapter evidence, and unused reservations are conservatively not refunded. Lost responses keep their reservations. Fixed execution, checks, method preparation, child creation and recovery consume root action budget. `recovery.max_attempts` is the number of EXTRA recovery actions, not total attempts. Recovery reserves its original candidate/continuation and budget in the same commit. Unknown effects never match retry. No arbitrary result repair is shipped. Configured preparation supports the explicitly declared pre-execution chunk_size correction branch described below; invalid answer/runtime fields never change code, source, authority or checks.

SQLite `controls` accepts a cancellation request without acquiring the busy run's execution lock; only B2 commits cancellation at a safe boundary. In-flight result evidence may still be stored, no new dependent action is authorized. Run state, events and receipts are atomic within a database transaction. Object bytes are saved/fsynced before references commit. B4 refuses symlinked internal directories and verifies object size/hash on reuse.

CSV acceptance and lexical limits are specified in `B3.md`. No shipped operation has external effects; the crash protocol is deliberately conservative even for local files. Single-process trusted adapters are not an OS security boundary against malicious Python plugins. The Pi-facing interface does not expose arbitrary code, filesystem, shell or business network tools.


## Input preparation and task-internal execution

`prepare_definition()` creates an orders.prepare fixed task; `json_report_definition(mode)` calls that child before the usual report. The prepare capability returns `prepared` and `mapping`, with registered `result_check=orders.prepare`. B2 checks the candidate against original JSON before committing any output to downstream consumers. The mapping has source/prepared hashes, field identity and one source index to each data-record index. No normalization, deduplication, sorting or monetary changes are allowed at this boundary. Whole orders.from_json acceptance repeats preparation and business checks.

`PiAgent(enable_tools=True).respond_with_tools(context, execute)` adds a trusted callback with signature `execute(capability, inputs) -> {operation, outputs, reused}`. `enable_tools=False` is a host choice for explicit recursive-path evaluation. The model cannot change it. The worker's `capability` tool takes `{capability, inputs_json}`; Python parses strict JSON before invoking the callback. A bounded JSONL bridge exchanges uniquely identified requests/results; timeouts, bad frames or host exceptions close the session and kill the worker. No host failure becomes a model-retry hint.

Effective capability grant is run ∩ accepted definition ∩ current task contract. Input artifact ownership and semantic role are checked against bound inputs, current child results and this step's committed tool receipts. Scalar rule must equal the bound fact. A global object hash alone grants no access. Every operation persists intent and action budget before dispatch, candidate bytes before references, and any capability-specific check before a receipt. Receipts are in `context.tool_receipts`; identical committed capability+inputs in the same step reuse the original operation. Tool commits do not increment graph pc. Only independently accepted D04 candidate_result increments pc. Restore first commits/checks a saved tool candidate, then resumes with receipts; unresolved intent remains effect_unknown. Revoked or failed sessions cannot dispatch again or override a controller recovery with a late handoff.


Semantic draft lifecycle: `.cancel(draft_id, message_id=..., revision=...)` durably
records control intent without the global authoring/model lock. Short per-draft
control locking serializes cancellation with delivery. Accepted cancellation
stops further model requests, preserves late evidence, invalidates candidate
reports and cannot revive on resume. A delivered draft is already complete and
cancel returns `requested=false`. `.revise(draft_id, message_id=..., revision=...,
proposal=None, request_model=False)` accepts exactly one full analysis proposal
or a new bounded author call. It preserves sources, effective answers, original
budget/usage and prior analysis/report/diagnostics. Revisions always require a new
independent review; changed source requires resubmission. It never edits an
already accepted run definition. Message conflicts/stale revisions fail before
mutation; interrupted review resumes against the saved answer/revision.

For a declared retry of an agent tool, B2 saves `retrying_agent_tool` and the
original operation identity, then retries that capability with its original
inputs before any new model dispatch. Recovery cannot become a replacement
subgraph chosen by Pi. Candidate recovery/recheck likewise retains the original
operation and artifact handles; counters and reservations survive restart.


## Parameter proposal protocol

`Registry.parameter_forms()` returns trusted, versioned editable fields for
orders.prepare_configured. `configured_prepare_definition()` is a fixed single
registered conversion with source_file/source_json, column_map/object and
chunk_size/integer. The first-release mapping is the complete five-field identity
map; other interpretations are unsupported. chunk_size determines actual CSV
record batches, preserving exact output values/order and mapping provenance.

`Runtime.start(..., parameter_policy={max_chunk_size:1024})` pins a trusted policy;
missing form fields may create a run but cannot start effects. `advance()` enters
task `waiting_parameters` and run `waiting_info`, with a structured form question.
`propose_parameters(run_id, task_id, patch, message_id=..., revision=...)` uses the
same run lock as execution startup. Revision means task.parameter_revision,
independent of run event revision. It accepts only editable data fields, validates
the whole patch, and atomically saves bindings/history, readiness and receipt.
Accepted partial fields still leave missing fields waiting. Illegal patches keep
all prior bindings and their parameter revision. After operation start, parameter
updates are locked; each operation pins the consumed parameter revision.

Only a predeclared parameter_validation/invalid_value branch may request a new
chunk_size proposal. Its trusted evidence comes from the form validator, before
execution. The branch's allowed fields, count and root budget are saved with the
rejection receipt; restarts/new parameter revisions cannot reset them. Unknown or
noneditable fields reject the entire envelope without expanding the correction.
An empty proposal cannot end an unfulfilled correction. With no matching branch
or exhausted count, invalid values stop the run. Policy/source/code/check fields
are never editable. Cancellation and dependency/source integrity are checked
before acceptance. Duplicate messages return the original receipt first.

Parameter validation also intersects the accepted definition parameter domains, including any stricter enum, both before accepting a proposal and immediately before consumption. The question exposes this enum along with the host maximum; meeting the form alone never weakens a definition constraint.


## Pi 0.87.1 environment compatibility

Only matching installed core/ai/coding 0.80.6 or 0.87.1 are accepted. Package discovery walks from the resolved CLI to the package identity, including the new bundled entry. The newer release uses public ModelRuntime/ModelRegistry with allowModelNetwork=false and refreshOnCreate=false, system messages and finishTurn; the older release retains its explicit API branch. Immediate handoff, host failure, cancellation/deadline and request-budget termination retain the same meaning, with a pre-stream budget/authority guard in both versions. No business capabilities, fixed implementations or acceptance checks change. Explicit process-level provider/model selection can choose an already configured model; unavailable defaults fail without fallback. Safe diagnostics may include a fixed provider-error category and numeric HTTP status, never provider response text or credentials.
