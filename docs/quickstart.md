# Quickstart

This guide builds the current Rust Core, installs the ordinary Pi entry, runs the two-stage cleaning SOP in an isolated data directory, inspects its Trace, and publishes a deliberate Graph revision.

The [interactive browser tour](showcase/) is an optional scripted explanation. It does not replace these real steps or produce execution evidence.

## Prerequisites

- Linux x86_64 with glibc, the currently validated target
- A Rust toolchain
- Pi CLI and a provider/model available in your environment
- Python 3 for this example's trusted program Checker; it is not a Core dependency

Replace `YOUR_PROVIDER` and `YOUR_MODEL` below with values supported by your Pi configuration.

## 1. Build and install the native entry

From the repository root:

```bash
cargo build --release --locked --manifest-path rust/Cargo.toml
CORE="$PWD/rust/target/release/agentgenome"
"$CORE" client install --client pi
```

The native entry references this binary's absolute path. Keep the build artifact in place, or copy it to a stable location and run `client install` using that absolute path. Open a new Pi session after installation so the native configuration and skill are loaded.

To install a different host, replace `pi` with `codex`, `qoder`, or `claude` and follow its activation instructions in [CLI and MCP access](client-access.md).

## 2. Prepare an isolated cleaning example

Use a fresh data directory so the example does not alter an existing default service:

```bash
AG_DEMO_DIR=$(mktemp -d "${TMPDIR:-/tmp}/agentgenome-clean.XXXXXX")
AG_DEMO_WORK="$AG_DEMO_DIR/workspace"
mkdir -p "$AG_DEMO_WORK/deliveries"
cp examples/agent-workflow/graph-clean.yaml "$AG_DEMO_WORK/sop.yaml"
cp examples/agent-workflow/clean-input.json "$AG_DEMO_WORK/input.json"
PYTHON=$(command -v python3)
```

The cleaning Graph uses Pi Workers and a trusted external program for both Checkers. Create the deployment configuration with absolute paths and the current program SHA256:

```bash
cat > "$AG_DEMO_DIR/clean-programs.json" <<EOF
{
  "format": "agentgenome-program-deployments/1",
  "programs": [{
    "backend_ref": "script",
    "code_path": "$PWD/examples/programs/data_clean_checker.py",
    "code_sha256": "905b8f385f6785ba0294e8d579fe8406adb193924b9d1deda551c9ff3f184d30",
    "interpreter": "$PYTHON",
    "prefix_args": [],
    "arguments": [],
    "capabilities": {
      "resumable_session": true,
      "live_observation": "full",
      "idempotent_delivery": true,
      "stop": "cooperative",
      "checkpoint": true,
      "backend_tools": [],
      "internal_activities": "none"
    },
    "provider_accounting": "no_model"
  }]
}
EOF
```

This is explicit trusted deployment configuration. A Graph cannot load arbitrary executable code by itself.

## 3. Start the service

```bash
"$CORE" start --data-dir "$AG_DEMO_DIR" \
  --backend pi --provider YOUR_PROVIDER --model YOUR_MODEL \
  --program-config "$AG_DEMO_DIR/clean-programs.json"
```

`client install` and `/agentgenome` do not start the service for you. Use the same Core path and `--data-dir` for every command below.

## 4. Run the SOP through the Teacher

Start a Teacher session connected to that service:

```bash
"$CORE" teacher --data-dir "$AG_DEMO_DIR" --workspace "$AG_DEMO_WORK" \
  --provider YOUR_PROVIDER --model YOUR_MODEL
```

Then ask:

> Read `sop.yaml` and `input.json`, explain the rules, and validate, publish, and run the existing SOP without changing its Tasks. Use the real report to confirm both stages were accepted. Export the accepted `clean.json` and `profile.json` into `deliveries`; do not write business outputs yourself.

Behind the conversation, the Teacher uses the same public operations as the CLI:

```text
validate_definition → publish_definition → put_artifact → create_run
                   → list_runs / read_events / get_report → read_artifact
```

Every write needs a unique `request_id`. Retry a lost request with the same ID and identical content rather than silently creating a second Run.

### Equivalent CLI operations

If you prefer direct commands, the same flow is available without the Teacher:

```bash
"$CORE" ctl validate "$AG_DEMO_WORK/sop.yaml" --data-dir "$AG_DEMO_DIR"
"$CORE" ctl publish "$AG_DEMO_WORK/sop.yaml" \
  --request-id pub-clean-001 --data-dir "$AG_DEMO_DIR"
```

Copy the returned `definition_ref`, then create the Run:

```bash
"$CORE" ctl run --definition-ref DEFINITION_REF --request-id run-clean-001 \
  --input dataset="$AG_DEMO_WORK/input.json" --data-dir "$AG_DEMO_DIR"
```

`ctl run` uploads and freezes the input before creating the Run. Replace `RUN_ID` and `ARTIFACT_ID` with real values from the run/report output:

```bash
"$CORE" ctl report RUN_ID --data-dir "$AG_DEMO_DIR"
"$CORE" ctl artifact ARTIFACT_ID \
  --out "$AG_DEMO_WORK/deliveries/clean.json" --data-dir "$AG_DEMO_DIR"
```

Export both accepted artifacts using their actual report references. Do not infer an artifact ID from a filename or manufacture a missing result.

## 5. Verify the accepted output

The expected `clean.json` contains Alice, Bob, and Carol in source order, with this audit:

```json
{
  "input_rows": 6,
  "output_rows": 3,
  "invalid_rows": 2,
  "duplicate_rows": 1
}
```

The expected `profile.json` is:

```json
{"rows": 3, "domains": {"example.com": 3}}
```

Check the files exported from accepted artifact references, not files a Worker left in its workspace:

```bash
"$PYTHON" - "$AG_DEMO_WORK/deliveries/clean.json" \
  "$AG_DEMO_WORK/deliveries/profile.json" <<'PY'
import json
import sys

clean = json.load(open(sys.argv[1], encoding="utf-8"))
profile = json.load(open(sys.argv[2], encoding="utf-8"))
assert clean["rows"] == [
    {"name": "Alice", "email": "alice@example.com"},
    {"name": "Bob", "email": "bob@example.com"},
    {"name": "Carol", "email": "carol@example.com"},
]
assert clean["audit"] == {
    "input_rows": 6,
    "output_rows": 3,
    "invalid_rows": 2,
    "duplicate_rows": 1,
}
assert profile == {"rows": 3, "domains": {"example.com": 3}}
print("accepted clean/profile outputs match the frozen example input")
PY
```

A terminal Run state, a Checker receipt, and an independent output check answer different questions. Keep the report and artifact hashes with the result.

## 6. Inspect the Run with Trace

Print one terminal snapshot:

```bash
"$CORE" trace --data-dir "$AG_DEMO_DIR" --run RUN_ID --once
```

For continuous terminal refresh, omit `--once` in a TTY. To print the read-only browser URL and credential-file path:

```bash
"$CORE" trace --web --data-dir "$AG_DEMO_DIR" --run RUN_ID
```

Trace reads the same authenticated service. It does not start another service, advance scheduling, submit a review, or confirm a human gate. See [Trace terminal and browser views](trace.md).

## 7. Evolve the SOP deliberately

The first publication of a definition uses `base_revision: 0` and produces revision 1. After making an intentional change, validate the complete YAML and publish it against the current base:

```bash
"$CORE" ctl validate "$AG_DEMO_WORK/sop.yaml" --data-dir "$AG_DEMO_DIR"
"$CORE" ctl publish "$AG_DEMO_WORK/sop.yaml" --base-revision 1 \
  --request-id pub-clean-002 --data-dir "$AG_DEMO_DIR"
```

Use a fresh `request_id` for changed content. The new revision has a new `definition_ref`; create a new Run against it. Existing Runs remain pinned to their original definition and input artifacts. Reusing a stale base revision, or reusing a request ID with different content, produces a conflict rather than rewriting history.

Changing business rules may also require a new Checker implementation and deployment. Do not expect an old trusted program to accept a contract it does not support.

## 8. Stop only the example service

After the Teacher session ends, the service and Run records remain available. When you no longer need this isolated example:

```bash
"$CORE" status --data-dir "$AG_DEMO_DIR"
"$CORE" stop --data-dir "$AG_DEMO_DIR"
```

Stopping a service is different from pausing or cancelling a Run, and stopping the service does not delete the data directory.

## Next steps

- Understand [why candidate submission is not acceptance](concepts.md#candidate-review-and-accepted-state).
- Compare other [business examples and protocol fixtures](../examples/README.md).
- Learn the complete [Graph /2 protocol](../rust/assets/teacher/sop-guide.md).
