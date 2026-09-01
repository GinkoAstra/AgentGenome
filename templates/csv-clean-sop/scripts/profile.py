#!/usr/bin/env python3
"""SOP 第一步：数据画像（只读）。

读取 CSV/TSV，输出画像 JSON 到 stdout：
{file, rows, cols, columns: [{name, null_rate, dtype_guess, samples, min?, max?, mean?}]}

编码探测：utf-8-sig → utf-8 → gbk；分隔符：csv.Sniffer，失败回退逗号。
仅标准库，与 GraphTree 运行时零新增依赖保持一致。
"""

from __future__ import annotations

import csv
import json
import statistics
import sys
from datetime import datetime

_GHOSTS = {"", "na", "n/a", "null", "none", "nan", "-"}
_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y")


def _snake(name: str) -> str:
    """列标准化规则的唯一实现；clean.py 复用，保证画像与清洗口径一致。"""
    import unicodedata

    text = unicodedata.normalize("NFKC", name).strip().lower()
    out = []
    for ch in text:
        out.append(ch if (ch.isalnum() or ch == "_") else "_")
    result = "".join(out).strip("_")
    while "__" in result:
        result = result.replace("__", "_")
    return result or "col"


def _read_rows(path: str) -> tuple[list[str], list[list[str]]]:
    raw = None
    for encoding in ("utf-8-sig", "utf-8", "gbk"):
        try:
            with open(path, encoding=encoding, newline="") as stream:
                raw = stream.read()
            break
        except (UnicodeDecodeError, UnicodeError):
            continue
    if raw is None:
        raise SystemExit(f"无法解码文件：{path}")
    try:
        dialect = csv.Sniffer().sniff(raw[:8192], delimiters=",\t;|")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(raw.splitlines(), dialect))
    if not rows:
        raise SystemExit("文件为空或无法解析为表格")
    return rows[0], rows[1:]


def _is_ghost(value: str) -> bool:
    return value.strip().lower() in _GHOSTS


def _guess_type(values: list[str]) -> str:
    """对非空样例做类型猜测：int → float → date → str。"""
    if not values:
        return "str"
    try:
        for v in values:
            int(v)
        return "int"
    except ValueError:
        pass
    try:
        for v in values:
            float(v)
        return "float"
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            for v in values:
                datetime.strptime(v.strip(), fmt)
            return "date"
        except ValueError:
            continue
    return "str"


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("用法：profile.py <data_file>")
    header, rows = _read_rows(sys.argv[1])
    seen: dict[str, int] = {}
    normalized_header = []
    for name in header:
        candidate = _snake(name)
        if candidate in seen:
            seen[candidate] += 1
            candidate = f"{candidate}_{seen[candidate]}"
        else:
            seen[candidate] = 1
        normalized_header.append(candidate)
    columns = []
    for index, name in enumerate(header):
        raw_values = [row[index] for row in rows if index < len(row)]
        non_null = [v.strip() for v in raw_values if not _is_ghost(v)]
        null_rate = round(1 - len(non_null) / len(raw_values), 4) if raw_values else 0.0
        guess = _guess_type(non_null)
        column: dict = {
            "name": name,
            "normalized": normalized_header[index],
            "null_rate": null_rate,
            "dtype_guess": guess,
            "samples": non_null[:5],
        }
        if guess in {"int", "float"} and non_null:
            numbers = [float(v) for v in non_null]
            column.update(
                min=min(numbers), max=max(numbers), mean=round(statistics.fmean(numbers), 4)
            )
        columns.append(column)
    print(json.dumps({
        "file": sys.argv[1],
        "rows": len(rows),
        "cols": len(header),
        "columns": columns,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
