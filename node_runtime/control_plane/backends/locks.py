"""Advisory cross-process lock for one execution entity.

Two Controllers in different OS processes can address the same
(worker_id, epoch). A process-local ``RLock`` cannot serialise them, so every
state read that decides whether an effect already happened must happen under a
lock shared by the whole machine. ``flock`` is released by the kernel when the
owning process dies, which is the property the execution boundary needs.
"""
from __future__ import annotations

import fcntl
import os
import time
from pathlib import Path


class ExecutionLock:
    """Exclusive per-execution lock backed by the execution directory."""

    def __init__(self, path: str | Path, timeout: float = 60.0) -> None:
        self.path = Path(path)
        self.timeout = timeout
        self._fd: int | None = None

    def __enter__(self) -> "ExecutionLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise TimeoutError(
                        f"execution lock is held by another process: {self.path}")
                time.sleep(0.01)
        self._fd = fd
        return self

    def __exit__(self, *exc_info: object) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
