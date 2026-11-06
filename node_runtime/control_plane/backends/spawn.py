"""Record a spawned worker's identity before replacing the launcher image.

The Controller may die between ``Popen`` and persisting the pid. The child then
keeps running (it is in its own session) while the Backend has no durable
identity for it. This launcher writes pid + kernel start time first, fsyncs the
record, and only then ``exec``s the real backend so the recorded pid stays
valid; a restarted Controller can adopt, observe and stop the actual process.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

FORMAT = "agentgenome-spawn/1"


def _process_start(pid: int) -> int | None:
    try:
        line = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        return int(line[line.rindex(")") + 2:].split()[19])
    except (ValueError, IndexError):
        return None


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    try:
        descriptor = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("usage: spawn.py RECORD DELIVERY_KEY ARGV0 [ARG...]",
              file=sys.stderr)
        return 2
    record_path = Path(argv[0])
    delivery_key = argv[1]
    command = argv[2:]
    pid = os.getpid()
    _write_json(record_path, {
        "format": FORMAT,
        "pid": pid,
        "process_start": _process_start(pid),
        "delivery_key": delivery_key,
        "argv0": command[0],
    })
    try:
        os.execvp(command[0], command)
    except OSError as error:
        # Leave the recorded identity in place: it names a pid that is about
        # to exit, which is honest evidence, not a reason to spawn again.
        print(f"could not exec {command[0]}: {error}", file=sys.stderr)
        return 127
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
