#!/usr/bin/env python3
"""根节点验收：摘要 JSON 与其引用的交付文件齐全性检查。

用法：verify_report.py <report.json>；全部通过退出 0，否则 stderr 原因 + 退出 1。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("用法：verify_report.py <report.json>")
    errors: list[str] = []
    try:
        with open(sys.argv[1], encoding="utf-8") as stream:
            report = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"摘要不可解析：{exc}", file=sys.stderr)
        raise SystemExit(1)
    for key in ("out_file", "report_file", "rows_in", "rows_out", "retention_rate_pct"):
        if key not in report:
            errors.append(f"摘要缺键：{key}")
    if errors:
        print("\n".join(errors), file=sys.stderr)
        raise SystemExit(1)
    if not isinstance(report["rows_out"], int) or report["rows_out"] <= 0:
        errors.append("rows_out 必须为正整数")
    if report["rows_out"] > report["rows_in"]:
        errors.append("rows_out 不应大于 rows_in")
    for key in ("out_file", "report_file"):
        path = Path(report[key])
        if not path.is_file() or path.stat().st_size == 0:
            errors.append(f"交付文件缺失或为空：{path}")
    if errors:
        print("\n".join(errors), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
