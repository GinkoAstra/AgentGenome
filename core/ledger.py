"""append-only JSONL 台账与外置日志引用。"""

from __future__ import annotations

import hashlib
import json
import os
import warnings
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def file_ref(run_dir: Path, path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    return {
        "path": path.relative_to(run_dir).as_posix(),
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


class Ledger:
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.path = run_dir / "ledger.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._seq = self._read_next_seq()

    def _read_next_seq(self) -> int:
        if not self.path.exists():
            return 1
        raw = self.path.read_bytes()
        if raw and not raw.endswith(b"\n"):
            # 仅撕裂的尾行可丢；否则下一次 append 会把两条 JSON 粘成一行。
            complete_end = raw.rfind(b"\n") + 1
            with self.path.open("r+b") as stream:
                stream.truncate(complete_end)
                stream.flush()
                os.fsync(stream.fileno())
            warnings.warn("ledger.jsonl 含撕裂尾行，已丢弃该行", RuntimeWarning, stacklevel=2)
            raw = raw[:complete_end]
        complete = raw.splitlines()
        if not complete:
            return 1
        try:
            return int(json.loads(complete[-1])["seq"]) + 1
        except (ValueError, KeyError, json.JSONDecodeError):
            raise ValueError("ledger.jsonl 含非尾行损坏")

    @property
    def next_seq(self) -> int:
        return self._seq

    def log_ref(self, stream: str, text: str) -> dict[str, Any]:
        logs = self.run_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        path = logs / f"{self._seq:04d}-{stream}.log"
        path.write_text(text, encoding="utf-8")
        return file_ref(self.run_dir, path)

    def event(self, seq: int) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        for line in self.path.read_text(encoding="utf-8").splitlines():
            payload = json.loads(line)
            if payload.get("seq") == seq:
                return payload
        return None

    def append(
        self, event: str, *, node: str | None = None, attempt: int | None = None, **fields: Any
    ) -> int:
        payload: dict[str, Any] = {
            "seq": self._seq,
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "event": event,
        }
        if node is not None:
            payload["node"] = node
        if attempt is not None:
            payload["attempt"] = attempt
        payload.update(fields)
        encoded = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        with self.path.open("ab") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        self._seq += 1
        return payload["seq"]
