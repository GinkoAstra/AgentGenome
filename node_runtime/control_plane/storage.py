"""SQLite transaction store for the unified control plane.

One database file holds resources, events, facts, idempotency receipts and
artifact metadata. Every write uses BEGIN IMMEDIATE so conditional revisions
and request_id deduplication are decided inside the same transaction as the
effects. A caller-supplied fault hook exists only for deterministic crash
injection in tests; it is never wired by the service host.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Callable, Iterator
import uuid

from .resources import fail

SCHEMA_VERSION = "3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS definitions (
    definition_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    definition_ref TEXT NOT NULL UNIQUE,
    document_digest TEXT NOT NULL,
    document_json TEXT NOT NULL,
    library_refs_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    request_id TEXT NOT NULL,
    PRIMARY KEY (definition_id, revision)
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    definition_ref TEXT NOT NULL,
    definition_digest TEXT NOT NULL,
    input_refs_json TEXT NOT NULL,
    policy_ref TEXT NOT NULL,
    budget_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    desired TEXT NOT NULL,
    status TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    revision INTEGER NOT NULL,
    event_seq INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS nodes (
    node_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    parent_id TEXT REFERENCES nodes(node_id),
    expansion_id TEXT,
    local_child_id TEXT,
    template_ref TEXT NOT NULL,
    contract_ref TEXT NOT NULL,
    input_bindings_json TEXT NOT NULL,
    input_refs_json TEXT,
    delegation_json TEXT NOT NULL DEFAULT '{}',
    dependencies_json TEXT NOT NULL,
    reviewer_ref TEXT NOT NULL,
    closure_json TEXT,
    state TEXT NOT NULL,
    round INTEGER NOT NULL,
    accepted_candidate_ref TEXT,
    revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS nodes_by_run ON nodes(run_id);
CREATE TABLE IF NOT EXISTS workers (
    worker_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    node_id TEXT NOT NULL REFERENCES nodes(node_id),
    backend_ref TEXT NOT NULL,
    agent_ref TEXT NOT NULL,
    required_capabilities_json TEXT NOT NULL DEFAULT '{}',
    controller_id TEXT NOT NULL,
    epoch INTEGER NOT NULL,
    intent_seq INTEGER NOT NULL,
    desired TEXT NOT NULL,
    observed TEXT NOT NULL,
    session_ref TEXT,
    checkpoint_ref TEXT,
    context_json TEXT NOT NULL DEFAULT '{}',
    tool_profile_json TEXT NOT NULL DEFAULT '{}',
    budget_json TEXT NOT NULL DEFAULT '{}',
    checkpoint_json TEXT,
    usage_json TEXT NOT NULL DEFAULT '{"provider_calls": 0, "tool_calls": 0}',
    activities_json TEXT NOT NULL DEFAULT '[]',
    activities_complete INTEGER NOT NULL DEFAULT 0,
    last_event_id TEXT,
    revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS workers_by_run ON workers(run_id);
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    node_id TEXT,
    worker_id TEXT,
    epoch INTEGER,
    payload_json TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (run_id, seq)
);
CREATE INDEX IF NOT EXISTS events_by_run ON events(run_id, seq);
CREATE TABLE IF NOT EXISTS candidates (
    candidate_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    node_id TEXT NOT NULL REFERENCES nodes(node_id),
    worker_id TEXT NOT NULL,
    epoch INTEGER NOT NULL,
    round INTEGER NOT NULL,
    output_refs_json TEXT NOT NULL,
    contract_ref TEXT NOT NULL,
    summary_ref_json TEXT,
    event_id TEXT NOT NULL UNIQUE,
    decision TEXT,
    decision_event_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS candidates_by_node ON candidates(run_id, node_id);
CREATE UNIQUE INDEX IF NOT EXISTS candidates_open_by_node
    ON candidates(node_id) WHERE decision IS NULL;
CREATE TABLE IF NOT EXISTS reviews (
    event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    node_id TEXT NOT NULL REFERENCES nodes(node_id),
    candidate_id TEXT NOT NULL,
    verdict TEXT NOT NULL,
    feedback_ref_json TEXT,
    reviewer TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS expansions (
    expansion_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    parent_node_id TEXT NOT NULL REFERENCES nodes(node_id),
    join_rule TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    event_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS expansion_children (
    expansion_id TEXT NOT NULL REFERENCES expansions(expansion_id),
    local_child_id TEXT NOT NULL,
    child_node_id TEXT NOT NULL REFERENCES nodes(node_id),
    PRIMARY KEY (expansion_id, local_child_id)
);
CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    worker_id TEXT NOT NULL REFERENCES workers(worker_id),
    node_id TEXT NOT NULL,
    epoch INTEGER NOT NULL,
    delivery_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    created_by TEXT NOT NULL,
    delivery_state TEXT NOT NULL,
    revision INTEGER NOT NULL,
    enqueue_event_id TEXT REFERENCES events(event_id),
    delivered_by TEXT,
    delivery_receipt_json TEXT,
    delivery_event_id TEXT REFERENCES events(event_id),
    created_at TEXT NOT NULL,
    delivered_at TEXT,
    UNIQUE (worker_id, epoch, delivery_key)
);
CREATE INDEX IF NOT EXISTS messages_by_worker ON messages(worker_id, created_at);
CREATE TABLE IF NOT EXISTS expansion_requests (
    expansion_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    node_id TEXT NOT NULL REFERENCES nodes(node_id),
    source TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL,
    reason_json TEXT,
    source_event_id TEXT NOT NULL REFERENCES events(event_id),
    applied_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS expansion_requests_by_run ON expansion_requests(run_id, node_id);
CREATE TABLE IF NOT EXISTS decision_requests (
    decision_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    node_id TEXT NOT NULL REFERENCES nodes(node_id),
    parent_id TEXT NOT NULL REFERENCES nodes(node_id),
    worker_id TEXT NOT NULL,
    epoch INTEGER NOT NULL,
    round INTEGER NOT NULL,
    contract_ref TEXT NOT NULL,
    reason TEXT NOT NULL,
    question TEXT NOT NULL,
    options_json TEXT NOT NULL,
    material_refs_json TEXT NOT NULL,
    state TEXT NOT NULL,
    reply_json TEXT,
    resolved_by TEXT,
    resolved_epoch INTEGER,
    request_event_id TEXT NOT NULL REFERENCES events(event_id),
    resolved_event_id TEXT REFERENCES events(event_id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS decision_requests_open_by_node
    ON decision_requests(node_id) WHERE state = 'open';
CREATE INDEX IF NOT EXISTS decision_requests_by_run
    ON decision_requests(run_id, node_id);
CREATE TABLE IF NOT EXISTS idempotency (
    actor_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    request_id TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (actor_id, operation, request_id)
);
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    media_type TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_id() -> str:
    return str(uuid.uuid4())


def dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def load(value: str | None) -> Any:
    return None if value is None else json.loads(value)


class Tx:
    """One open transaction. SQL lives in the API but sequencing lives here."""

    def __init__(self, conn: sqlite3.Connection, store: "Store") -> None:
        self.conn = conn
        self.store = store

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def all(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def append_event(self, *, run_id: str, kind: str, payload: dict[str, Any],
                     actor_id: str, request_id: str, node_id: str | None = None,
                     worker_id: str | None = None, epoch: int | None = None) -> tuple[str, int]:
        row = self.one("SELECT event_seq FROM runs WHERE run_id = ?", (run_id,))
        if row is None:
            raise KeyError(run_id)
        seq = int(row["event_seq"]) + 1
        self.execute("UPDATE runs SET event_seq = ? WHERE run_id = ?", (seq, run_id))
        event_id = new_id()
        self.execute(
            "INSERT INTO events (event_id, run_id, seq, kind, node_id, worker_id, epoch,"
            " payload_json, actor_id, request_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (event_id, run_id, seq, kind, node_id, worker_id, epoch,
             dump(payload), actor_id, request_id, now()),
        )
        return event_id, seq

    def idempotency_lookup(self, actor_id: str, operation: str, request_id: str,
                           request_digest: str) -> dict[str, Any] | None:
        row = self.one(
            "SELECT request_digest, response_json FROM idempotency"
            " WHERE actor_id = ? AND operation = ? AND request_id = ?",
            (actor_id, operation, request_id),
        )
        if row is None:
            return None
        if row["request_digest"] != request_digest:
            fail("idempotency_conflict", "request_id was reused with different content",
                 operation=operation, request_id=request_id)
        return load(row["response_json"])

    def idempotency_store(self, actor_id: str, operation: str, request_id: str,
                          request_digest: str, response: dict[str, Any]) -> None:
        self.execute(
            "INSERT INTO idempotency (actor_id, operation, request_id, request_digest,"
            " response_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (actor_id, operation, request_id, request_digest, dump(response), now()),
        )

    def fault(self, name: str, detail: Any = None) -> None:
        self.store.fault(name, detail)


class Store:
    def __init__(self, path: str | Path,
                 fault_hook: Callable[[str, Any], None] | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fault_hook = fault_hook
        self._initialize()

    def fault(self, name: str, detail: Any = None) -> None:
        if self.fault_hook is not None:
            self.fault_hook(name, detail)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        return conn

    def _initialize(self) -> None:
        """Create schema /3 and additively upgrade a complete schema /2.

        The version is inspected through a read-only connection first, so a
        schema /1 (or unknown) database is refused without changing its bytes,
        journal mode, or connection history. Only an empty/new database is
        created. A complete schema /2 is upgraded in the same transaction that
        records the new version; resources, events, revisions and receipts are
        preserved. Before that upgrade a consistent SQLite snapshot is written
        beside the database, so a copy of the main file with a live WAL is not
        mistaken for a backup. An incomplete database is never silently
        repaired.
        """
        version, tables, columns = _inspect_schema(self.path)
        if version not in (None, "2", SCHEMA_VERSION):
            fail("capability_unsupported",
                 "the data directory uses an unsupported schema version; "
                 "this host only opens schema /3 and upgrades a complete /2",
                 schema_version=version, supported=SCHEMA_VERSION,
                 path=str(self.path))
        if version is not None:
            # Refuse an incomplete database before opening a writable
            # connection, so its bytes and journal mode are untouched.
            missing = sorted(SCHEMA_TABLES - tables)
            if missing:
                fail("capability_unsupported",
                     "the schema database is incomplete; refusing to "
                     "repair or rewrite existing history", missing=missing)
            if version == "2" and WORKER_ACTIVITY_COLUMNS & columns:
                fail("capability_unsupported",
                     "the schema /2 database already has activity columns; "
                     "refusing to guess its history",
                     columns=sorted(WORKER_ACTIVITY_COLUMNS & columns))
            if version == SCHEMA_VERSION and not WORKER_ACTIVITY_COLUMNS <= columns:
                fail("capability_unsupported",
                     "the schema /3 database is missing activity columns",
                     missing=sorted(WORKER_ACTIVITY_COLUMNS - columns))
        if version == "2":
            _backup_before_upgrade(self.path)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            tables = _table_names(conn)
            if "meta" in tables:
                row = conn.execute(
                    "SELECT value FROM meta WHERE key = 'schema_version'"
                ).fetchone()
                recorded = None if row is None else str(row[0])
                if recorded not in ("2", SCHEMA_VERSION):
                    fail("capability_unsupported",
                         "the data directory schema changed during initialization",
                         schema_version=recorded, supported=SCHEMA_VERSION)
                missing = sorted(SCHEMA_TABLES - tables)
                if missing:
                    fail("capability_unsupported",
                         "the schema database is incomplete; refusing to "
                         "repair or rewrite existing history", missing=missing)
                columns = _column_names(conn, "workers")
                if recorded == "2":
                    unexpected = sorted(WORKER_ACTIVITY_COLUMNS & columns)
                    if unexpected:
                        fail("capability_unsupported",
                             "the schema /2 database already has activity "
                             "columns; refusing to guess its history",
                             columns=unexpected)
                    for statement in MIGRATION_2_TO_3:
                        conn.execute(statement)
                elif not WORKER_ACTIVITY_COLUMNS <= columns:
                    fail("capability_unsupported",
                         "the schema /3 database is missing activity columns",
                         missing=sorted(WORKER_ACTIVITY_COLUMNS - columns))
                conn.execute(
                    "UPDATE meta SET value = ? WHERE key = 'schema_version'",
                    (SCHEMA_VERSION,),
                )
            elif tables:
                fail("capability_unsupported",
                     "the database has an unknown legacy schema and was not "
                     "written", tables=sorted(tables), supported=SCHEMA_VERSION)
            else:
                for statement in _statements(SCHEMA):
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                    (SCHEMA_VERSION,),
                )
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @contextmanager
    def write(self) -> Iterator[Tx]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield Tx(conn, self)
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @contextmanager
    def read(self) -> Iterator[Tx]:
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            yield Tx(conn, self)
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()



WORKER_ACTIVITY_COLUMNS = frozenset({"activities_json", "activities_complete"})

# Additive only: schema /2 rows keep their resources, events and receipts.
MIGRATION_2_TO_3 = (
    "ALTER TABLE workers ADD COLUMN activities_json TEXT NOT NULL DEFAULT '[]'",
    "ALTER TABLE workers ADD COLUMN activities_complete INTEGER NOT NULL DEFAULT 0",
)


def _backup_before_upgrade(path: Path) -> Path:
    """Write one consistent snapshot of a schema /2 database, WAL included.

    ``Connection.backup`` copies a live database through SQLite, so unflushed
    WAL pages are captured; copying only the main file would not be a valid
    backup while the database is in WAL mode.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = path.with_name(f"{path.name}.schema-2-backup-{stamp}-{uuid.uuid4().hex[:8]}")
    source_uri = f"file:{path.as_posix()}?mode=ro"
    source = sqlite3.connect(source_uri, uri=True, timeout=30)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    return target


def _statements(script: str) -> list[str]:
    return [part.strip() for part in script.split(";") if part.strip()]


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
        " AND name NOT LIKE 'sqlite_%'").fetchall()
    return {row[0] if isinstance(row, tuple) else row["name"] for row in rows}


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1] if isinstance(row, tuple) else row["name"] for row in rows}


# Every table the schema initializer must have created. A database whose meta
# says /2 or /3 but is missing one of these is not silently repaired.
SCHEMA_TABLES = frozenset({
    "meta", "definitions", "runs", "nodes", "workers", "events", "candidates",
    "reviews", "expansions", "expansion_children", "messages",
    "expansion_requests", "decision_requests", "idempotency", "artifacts",
})


def _inspect_schema(path: Path) -> tuple[str | None, set[str], set[str]]:
    """Read-only schema inspection; never writes the database.

    Returns ``(version, tables, worker_columns)``. ``version`` is ``None`` for
    a new/empty database, ``"2"`` for the upgradable schema, ``"3"`` for the
    current schema, and the recorded legacy/unknown version otherwise. An
    existing database without a meta table is treated as a legacy schema that
    must be refused, even when its tables happen to overlap current names.
    """
    if not path.exists() or path.stat().st_size == 0:
        return None, set(), set()
    uri = f"file:{path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    try:
        tables = _table_names(conn)
        if not tables:
            return None, set(), set()
        columns = (_column_names(conn, "workers") if "workers" in tables
                   else set())
        if "meta" not in tables:
            return "legacy", tables, columns
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        if row is None:
            return "legacy", tables, columns
        return str(row[0]), tables, columns
    finally:
        conn.close()


def ensure_directory(path: str | Path) -> Path:
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    return directory
