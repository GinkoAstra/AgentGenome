"""Strict, shared protocol primitives; no runtime or adapter dependencies."""
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile

class SopError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)

def canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise SopError('invalid_json', str(exc)) from exc

def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()

def file_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SopError('invalid_json', f'duplicate key: {key}')
        result[key] = value
    return result

def read_json(path):
    try:
        return json.loads(Path(path).read_text(), object_pairs_hook=_pairs,
                          parse_constant=lambda x: (_ for _ in ()).throw(SopError('invalid_json', x)))
    except (ValueError, OSError) as exc:
        raise SopError('invalid_json', str(exc)) from exc

def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (canonical(value) + '\n').encode()
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.pending-')
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)

class ExecutionError(SopError):
    """Trusted adapter evidence, never deserialized from a model response."""
    def __init__(self, code, message, *, not_dispatched=False):
        super().__init__(code, message)
        self.not_dispatched = not_dispatched
