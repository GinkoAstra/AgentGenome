"""本地 /bin/sh 命令端口。"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from core.ports import ShellResult, ShellUnavailable


class LocalShell:
    """薄、无状态的 ShellPort 实现。"""

    def execute(self, command: str, cwd: Path) -> ShellResult:
        started = time.monotonic()
        try:
            completed = subprocess.run(
                ["/bin/sh", "-c", command],
                cwd=cwd,
                text=True,
                capture_output=True,
                check=False,
            )
        except OSError as exc:
            raise ShellUnavailable(f"无法启动 /bin/sh：{exc}") from exc
        return ShellResult(
            command=command,
            rc=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_ms=round((time.monotonic() - started) * 1000),
        )
