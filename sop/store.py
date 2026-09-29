"""B4: content-addressed bytes and transactional run state/event/receipt storage."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time

from .common import SopError, canonical, digest, read_json


class Store:
    def __init__(self, root):
        self.root = Path(root).absolute()
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.resolve() != self.root:
            raise SopError('unsafe_path', 'data root cannot contain symlinks')
        self.objects = self.root / 'objects'
        self.objects.mkdir(exist_ok=True)
        if self.objects.resolve() != self.objects:
            raise SopError('unsafe_path', 'objects directory cannot be linked')
        (self.root / 'locks').mkdir(exist_ok=True)
        (self.root / 'work').mkdir(exist_ok=True)
        for directory in ('locks', 'work'):
            if (self.root / directory).resolve() != self.root / directory:
                raise SopError('unsafe_path', 'data directory cannot be linked')
        if (self.root / 'state.sqlite3').is_symlink():
            raise SopError('unsafe_path', 'database cannot be linked')
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS controls(run_id TEXT PRIMARY KEY, cancel_requested INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, revision INTEGER NOT NULL, state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                    revision INTEGER NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS receipts(run_id TEXT NOT NULL, message_id TEXT NOT NULL,
                    payload_hash TEXT NOT NULL, response TEXT NOT NULL, PRIMARY KEY(run_id,message_id));
            ''')

    def connect(self):
        db = sqlite3.connect(self.root / 'state.sqlite3', timeout=5)
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=FULL')
        return db

    @contextmanager
    def lock(self, run_id):
        if not run_id or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in run_id):
            raise SopError('invalid_id', 'invalid run identity')
        with (self.root / 'locks' / run_id).open('a') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise SopError('run_busy', 'another controller owns this run') from exc
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def put_bytes(self, data, kind='artifact'):
        sha = hashlib.sha256(data).hexdigest()
        target = self.objects / sha
        if target.exists():
            if target.is_symlink() or target.read_bytes() != data:
                raise SopError('integrity', 'content object changed')
        else:
            fd, name = tempfile.mkstemp(dir=self.objects, prefix='.pending-')
            try:
                with os.fdopen(fd, 'wb') as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(name, 0o400)
                os.replace(name, target)
                directory = os.open(self.objects, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
        return {'hash': sha, 'bytes': len(data), 'kind': kind}

    def put_json(self, value, kind='definition'):
        return self.put_bytes(canonical(value).encode(), kind)

    def import_file(self, path, kind='artifact'):
        path = Path(path).absolute()
        if path.resolve() != path or not path.is_file() or path.stat().st_nlink != 1:
            raise SopError('unsafe_path', 'only regular unlinked files are accepted')
        return self.put_bytes(path.read_bytes(), kind)

    def path(self, ref):
        if not isinstance(ref, dict) or set(ref) != {'hash', 'bytes', 'kind'}:
            raise SopError('invalid_artifact', 'expected host artifact reference')
        sha = ref['hash']
        if not isinstance(sha, str) or len(sha) != 64 or any(c not in '0123456789abcdef' for c in sha):
            raise SopError('invalid_artifact', 'invalid digest')
        path = self.objects / sha
        if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
            raise SopError('integrity', 'missing or linked content object')
        data = path.read_bytes()
        if type(ref['bytes']) is not int or len(data) != ref['bytes'] or hashlib.sha256(data).hexdigest() != sha:
            raise SopError('integrity', 'content object hash/size mismatch')
        return path

    def json(self, ref):
        return read_json(self.path(ref))

    def create(self, state):
        state['revision'] = 0
        with self.connect() as db:
            db.execute('INSERT INTO runs VALUES(?,?,?)', (state['id'], 0, canonical(state)))
            db.execute('INSERT INTO events(run_id,revision,kind,data) VALUES(?,?,?,?)',
                       (state['id'], 0, 'run_created', canonical({'root': state['root']})))
        return state

    def load(self, run_id):
        with self.connect() as db:
            row = db.execute('SELECT state FROM runs WHERE id=?', (run_id,)).fetchone()
        if not row:
            raise SopError('not_found', 'run does not exist')
        state = json.loads(row[0])
        if state.get('schema') != 'run/1':
            raise SopError('state_version', 'unsupported run format')
        return state

    def commit(self, state, kind, data=None, receipt=None):
        previous = state['revision']
        updated = dict(state, revision=previous + 1)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            cursor = db.execute('UPDATE runs SET revision=?,state=? WHERE id=? AND revision=?',
                                (previous + 1, canonical(updated), state['id'], previous))
            if cursor.rowcount != 1:
                raise SopError('stale_revision', 'run changed while committing')
            db.execute('INSERT INTO events(run_id,revision,kind,data) VALUES(?,?,?,?)',
                       (state['id'], previous + 1, kind, canonical(data or {})))
            if receipt:
                message_id, payload, response = receipt
                db.execute('INSERT INTO receipts VALUES(?,?,?,?)',
                           (state['id'], message_id, digest(payload), canonical(response)))
        state['revision'] = previous + 1
        return state

    def receipt(self, run_id, message_id, payload):
        with self.connect() as db:
            row = db.execute('SELECT payload_hash,response FROM receipts WHERE run_id=? AND message_id=?',
                             (run_id, message_id)).fetchone()
        if row:
            if row[0] != digest(payload):
                raise SopError('message_conflict', 'same message identity with different content')
            return json.loads(row[1])
        return None

    def events(self, run_id):
        with self.connect() as db:
            rows = db.execute('SELECT seq,revision,kind,data FROM events WHERE run_id=? ORDER BY seq', (run_id,)).fetchall()
        return [dict(seq=row[0], revision=row[1], kind=row[2], data=json.loads(row[3])) for row in rows]

    def request_cancel(self, run_id):
        self.load(run_id)
        with self.connect() as db:
            db.execute('INSERT INTO controls VALUES(?,1) ON CONFLICT(run_id) DO UPDATE SET cancel_requested=1', (run_id,))

    def cancellation_requested(self, run_id):
        with self.connect() as db:
            row = db.execute('SELECT cancel_requested FROM controls WHERE run_id=?', (run_id,)).fetchone()
        return bool(row and row[0])
