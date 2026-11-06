"""Teacher command-line client for the running control plane.

The Teacher edits a Graph YAML with ordinary read/edit tools, then calls this
client for validate/publish/run/status/report. Every subcommand resolves the
same service the installed Pi entry uses and forwards one public API operation
over HTTP; no graph or business rule is implemented here, and no SQLite file is
opened or created. The same client serves every SOP.
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

from . import discovery
from .client import ApiClient, ApiTransportError
from .resources import ControlPlaneError

FORMAT = "agentgenome-teacher-client/2"
WATCH_FORMAT = "agentgenome-trace-watch/1"


def _resolve(path: str | Path) -> Path:
    value = Path(path).expanduser()
    if not value.is_absolute():
        value = Path.cwd() / value
    return value.resolve()


def _client(data_dir: str | Path | None) -> ApiClient:
    connection = discovery.connection(data_dir)
    return ApiClient(connection.endpoint, connection.token)


def _call(client: ApiClient, tool: str,
          arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        return client.call(tool, arguments)
    except (ApiTransportError, ControlPlaneError) as error:
        raise SystemExit(f"{tool}: {error.code}: {error.message}") from error


def _get(client: ApiClient, path: str,
         params: dict[str, Any] | None = None) -> Any:
    try:
        return client.get(path, params)
    except (ApiTransportError, ControlPlaneError) as error:
        raise SystemExit(f"{path}: {error.code}: {error.message}") from error


def _jsonl_writer(path: str | None):
    if not path:
        return lambda _record: None
    target = _resolve(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    def write(record: dict[str, Any]) -> None:
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False,
                                    sort_keys=True) + "\n")
    return write


def _execution_pages(client: ApiClient, path: str,
                     params: dict[str, Any], *, all_pages: bool) -> dict[str, Any]:
    page = _get(client, path, params)
    entries = list(page["entries"])
    cursor = page["next_cursor"]["token"]
    while all_pages and page.get("has_more"):
        page = _get(client, path, {**params, "after": cursor})
        entries.extend(page["entries"])
        cursor = page["next_cursor"]["token"]
    return {**page, "entries": entries, "cursor": cursor}


def _document(path: str) -> dict[str, Any]:
    resolved = _resolve(path)
    text = resolved.read_text(encoding="utf-8")
    if resolved.suffix in (".yaml", ".yml"):
        document = yaml.safe_load(text)
    else:
        document = json.loads(text)
    if not isinstance(document, dict):
        raise SystemExit("definition file must contain an object")
    return document


def _inputs(items: list[str]) -> dict[str, Path]:
    values: dict[str, Path] = {}
    for item in items:
        name, separator, path = item.partition("=")
        if not separator or not name or not path:
            raise SystemExit("--input expects NAME=PATH")
        values[name] = _resolve(path)
    return values


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir",
                        default=os.environ.get("AGENTGENOME_DATA_DIR"),
                        help="service data directory "
                             "(default: per-user AgentGenome service)")
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="validate a Graph YAML/JSON file")
    validate.add_argument("file")

    publish = sub.add_parser("publish", help="publish a Graph YAML/JSON definition")
    publish.add_argument("file")
    publish.add_argument("--request-id", required=True)
    publish.add_argument("--base-revision", type=int)

    run = sub.add_parser("run", help="upload inputs and create a run")
    run.add_argument("--definition-ref", required=True)
    run.add_argument("--input", action="append", default=[], metavar="NAME=PATH")
    run.add_argument("--request-id", required=True)

    status = sub.add_parser("status", help="list runs or read one run snapshot")
    status.add_argument("run_id", nargs="?")

    events = sub.add_parser("events", help="read committed events")
    events.add_argument("run_id")
    events.add_argument("--after", type=int, default=0)
    events.add_argument("--limit", type=int, default=100)

    report = sub.add_parser("report", help="read the run report")
    report.add_argument("run_id")

    get = sub.add_parser("get", help="read one committed resource")
    get.add_argument("kind")
    get.add_argument("identifier")

    artifact = sub.add_parser(
        "artifact", help="read an artifact's bytes through the public API")
    artifact.add_argument("artifact_id")
    artifact.add_argument("--out", help="write the bytes to this file")

    intent = sub.add_parser("intent", help="pause/resume/cancel a run")
    intent.add_argument("run_id")
    intent.add_argument("desired", choices=("running", "paused", "cancelled"))
    intent.add_argument("--expected-revision", type=int, required=True)
    intent.add_argument("--request-id", required=True)

    trace = sub.add_parser(
        "trace", help="read one trace snapshot (task tree, waiting, timeline)")
    trace.add_argument("run_id")
    trace.add_argument("--node-id")
    trace.add_argument("--candidate-id")

    watch = sub.add_parser("watch", help="follow committed trace events as JSONL")
    watch.add_argument("run_id")
    watch.add_argument("--after", help="opaque cursor token from a snapshot")
    watch.add_argument("--limit", type=int, default=200)
    watch.add_argument("--once", action="store_true",
                       help="read one finite page instead of streaming")
    watch.add_argument("--max-events", type=int, default=0,
                       help="stop after N committed events (0 = unlimited)")
    watch.add_argument("--event", action="append", default=[],
                       choices=("control", "heartbeat", "cursor_expired"))
    watch.add_argument("--jsonl", help="append every selected frame here")

    execution = sub.add_parser(
        "execution", help="read one execution's bounded log page")
    execution.add_argument("run_id")
    execution.add_argument("worker_id")
    execution.add_argument("epoch", type=int)
    execution.add_argument("delivery_key")
    execution.add_argument("--after",
                           help="opaque execution cursor token; rejected with "
                                "cursor_expired when the mutable source changed "
                                "(reread from the start)")
    execution.add_argument("--limit", type=int, default=100)
    execution.add_argument("--all", action="store_true",
                           help="read every page currently available")
    execution.add_argument("--jsonl", help="append each entry here")

    requests = sub.add_parser(
        "requests", help="read bounded request diagnostics")
    requests.add_argument("--request-id")
    requests.add_argument("--run-id")
    requests.add_argument("--after", default="",
                          help="opaque cursor token from a previous page")
    requests.add_argument("--limit", type=int, default=100)
    requests.add_argument("--jsonl", help="append each record here")

    args = parser.parse_args(argv)
    client = _client(args.data_dir)

    if args.command == "validate":
        result = _call(client, "validate_definition",
                       {"document": _document(args.file)})
    elif args.command == "publish":
        document = _document(args.file)
        if args.base_revision is not None:
            document["base_revision"] = args.base_revision
        else:
            document.setdefault("base_revision", 0)
        result = _call(client, "publish_definition",
                       {"document": document, "request_id": args.request_id})
    elif args.command == "run":
        refs: dict[str, Any] = {}
        for name, path in _inputs(args.input).items():
            payload = path.read_bytes()
            media = mimetypes.guess_type(path)[0] or "text/plain"
            stored = _call(client, "put_artifact", {
                "base64": base64.b64encode(payload).decode("ascii"),
                "media_type": media,
                "request_id": f"{args.request_id}:input:{name}"})
            refs[name] = stored["artifact"]
        result = _call(client, "create_run", {
            "definition_ref": args.definition_ref,
            "inputs": refs, "request_id": args.request_id})
    elif args.command == "status":
        if args.run_id:
            result = _call(client, "list_resources", {"run_id": args.run_id})
        else:
            result = _call(client, "list_runs", {})
    elif args.command == "events":
        result = _call(client, "read_events", {
            "run_id": args.run_id, "after": args.after, "limit": args.limit})
    elif args.command == "report":
        result = _call(client, "get_report", {"run_id": args.run_id})
    elif args.command == "get":
        result = _call(client, "get_resource", {
            "kind": args.kind, "identifier": args.identifier})
    elif args.command == "artifact":
        meta = _call(client, "get_resource", {
            "kind": "artifact", "identifier": args.artifact_id})
        payload = _call(client, "read_artifact", {
            "id": meta["id"], "sha256": meta["sha256"],
            "size": meta["size"], "media_type": meta["media_type"]})
        if not args.out:
            result = payload
        else:
            content = (payload["text"].encode("utf-8")
                       if "text" in payload
                       else base64.b64decode(payload["base64"]))
            target = _resolve(args.out)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            result = {"artifact": payload.get("artifact"), "path": str(target),
                      "bytes": len(content)}
    elif args.command == "trace":
        params = {key: value for key, value in
                  {"node_id": args.node_id, "candidate_id": args.candidate_id}.items()
                  if value is not None}
        result = _get(client, f"/v1/runs/{quote(args.run_id, safe='')}/trace",
                      params)
    elif args.command == "watch":
        path = f"/v1/runs/{quote(args.run_id, safe='')}/watch"
        write = _jsonl_writer(args.jsonl)
        selected = set(args.event) or {"control"}
        result: dict[str, Any] | None = None
        if args.once:
            events = _get(client,
                          f"/v1/runs/{quote(args.run_id, safe='')}/events",
                          {"after": args.after, "limit": args.limit})
            records = []
            for event in events["events"]:
                if "control" not in selected:
                    continue
                cursor = {"service_id": events["service_id"],
                          "run_id": events["run_id"], "seq": event["seq"]}
                cursor["token"] = json.dumps(cursor, sort_keys=True,
                                             separators=(",", ":"))
                envelope = {"format": WATCH_FORMAT,
                            "service_id": events["service_id"],
                            "run_id": events["run_id"],
                            "cursor": cursor,
                            "event": event}
                write(envelope)
                records.append(event)
            result = {**events, "events": records}
        else:
            count = 0
            try:
                for frame in client.stream(path, {"after": args.after,
                                                  "limit": args.limit}):
                    if frame["event"] not in selected:
                        continue
                    try:
                        data = json.loads(frame["data"])
                    except ValueError:
                        data = {"raw": frame["data"]}
                    record = {"format": WATCH_FORMAT,
                              "service_id": (data.get("service_id")
                                             if isinstance(data, dict) else None),
                              "run_id": args.run_id,
                              "cursor": frame["id"],
                              "event": frame["event"],
                              "data": data}
                    write(record)
                    # Live watch is a stream, not a retained result list:
                    # emit each selected frame immediately so a supervisor can
                    # read it before the process is stopped.
                    print(json.dumps(record, ensure_ascii=False,
                                     sort_keys=True), flush=True)
                    count += 1
                    if 0 < args.max_events <= count:
                        break
            except (ApiTransportError, ControlPlaneError) as error:
                raise SystemExit(
                    f"watch: {error.code}: {error.message}") from error
    elif args.command == "execution":
        path = (f"/v1/runs/{quote(args.run_id, safe='')}/executions/"
                f"{quote(args.worker_id, safe='')}/{args.epoch}/"
                f"{quote(args.delivery_key, safe='')}")
        params: dict[str, Any] = {"limit": args.limit}
        if args.after:
            params["after"] = args.after
        result = _execution_pages(client, path, params, all_pages=args.all)
        write = _jsonl_writer(args.jsonl)
        for entry in result["entries"]:
            write(entry)
    elif args.command == "requests":
        params = {"after": args.after, "limit": args.limit}
        if args.request_id:
            params["request_id"] = args.request_id
        if args.run_id:
            params["run_id"] = args.run_id
        result = _get(client, "/v1/requests", params)
        write = _jsonl_writer(args.jsonl)
        for record in result["records"]:
            write(record)
    else:
        result = _call(client, "set_run_intent", {
            "run_id": args.run_id, "desired": args.desired,
            "expected_revision": args.expected_revision,
            "request_id": args.request_id})

    if result is not None:
        print(json.dumps({"format": FORMAT, "command": args.command,
                          "result": result},
                         ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
