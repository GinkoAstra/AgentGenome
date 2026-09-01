#!/usr/bin/env python3
"""SOP 第三步：按规则执行确定性清洗管线。

用法：clean.py <data_file> <rules.json> <out_file>，审计 JSON 写 stdout。

执行顺序（每步计数入审计，任何删除/填充可追溯）：
1. 读取（编码/分隔符探测，同 profile.py）
2. 列标准化：NFKC + strip + 小写下划线 + 重命名去重
3. 去重：全行去重（rules.dedup），记删除条数
4. 哨兵值 → 缺失（rules.sentinels）
5. 幽灵字符串统一为缺失（"", "NA", "null", "-" 等）
6. 类型规整：rules.types 显式转换，失败置缺失并计数（不静默丢行）
7. 缺失填充：rules.missing 按列策略（median/mean/mode/字面量）
8. 文本规整：rules.text_normalize 列做 NFKC + strip
9. 异常值裁剪：IQR/percentile/zscore 截断到围栏，**只裁剪不删行**

红线：不就地覆盖源文件；out_file 已存在即拒绝（退出 1）。
"""

from __future__ import annotations

import csv
import json
import statistics
import sys
import unicodedata
from datetime import datetime

from profile import _GHOSTS, _DATE_FORMATS, _read_rows, _snake


def _is_ghost(value: str) -> bool:
    return value.strip().lower() in _GHOSTS


def _standardize_columns(header: list[str]) -> tuple[list[str], dict[str, str]]:
    seen: dict[str, int] = {}
    renamed: dict[str, str] = {}
    new_header = []
    for name in header:
        candidate = _snake(name)
        if candidate in seen:
            seen[candidate] += 1
            candidate = f"{candidate}_{seen[candidate]}"
        else:
            seen[candidate] = 1
        if candidate != name:
            renamed[name] = candidate
        new_header.append(candidate)
    return new_header, renamed


def _to_number(value: str) -> float | None:
    try:
        return float(value.strip())
    except (ValueError, AttributeError):
        return None


def _to_date(value: str) -> str | None:
    text = value.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _coerce_columns(
    header: list[str], rows: list[list[str]], types: dict[str, str]
) -> tuple[list[list[str]], dict[str, int]]:
    """类型规整：失败置空（缺失），计数入审计；行数不变。"""
    failures: dict[str, int] = {}
    for column, target in types.items():
        if column not in header:
            continue
        index = header.index(column)
        if target == "str":
            for row in rows:
                if index < len(row) and row[index] is not None:
                    row[index] = row[index].strip()
            continue
        count = 0
        for row in rows:
            if index >= len(row) or _is_ghost(row[index]):
                row[index] = ""
                continue
            if target in {"int", "float"}:
                number = _to_number(row[index])
                if number is None:
                    row[index] = ""
                    count += 1
                else:
                    row[index] = str(int(number)) if target == "int" and number == int(number) else str(number)
            elif target == "date":
                normalized = _to_date(row[index])
                if normalized is None:
                    row[index] = ""
                    count += 1
                else:
                    row[index] = normalized
        if count:
            failures[column] = count
    return rows, failures


def _fill_missing(
    header: list[str], rows: list[list[str]], missing: dict, types: dict[str, str]
) -> dict[str, int]:
    filled: dict[str, int] = {}
    for column, strategy in missing.items():
        if column not in header:
            continue
        index = header.index(column)
        blanks = [row for row in rows if index >= len(row) or _is_ghost(row[index])]
        if not blanks:
            continue
        value: str
        if strategy == "mode":
            texts = [row[index].strip() for row in rows if index < len(row) and not _is_ghost(row[index])]
            if not texts:
                continue
            value = statistics.mode(texts)
        elif strategy in {"median", "mean"}:
            numbers = [
                n for row in rows
                if index < len(row) and not _is_ghost(row[index])
                for n in [_to_number(row[index])] if n is not None
            ]
            if not numbers:
                continue
            is_int_column = types.get(column) == "int"
            if strategy == "median":
                value = statistics.median(numbers)
            else:
                value = statistics.fmean(numbers)
            value = str(round(value) if is_int_column else round(value, 2))
        else:
            value = str(strategy)
        for row in blanks:
            row[index] = value
        filled[column] = len(blanks)
    return filled


def _clip_outliers(
    header: list[str], rows: list[list[str]], outlier: dict, types: dict[str, str]
) -> dict[str, dict]:
    """异常值只裁剪到围栏，不删行。返回 {列: {method, lower, upper, clipped}}。"""
    method_default = outlier.get("method", "none")
    k = outlier.get("k", 1.5)
    per_column = outlier.get("per_column", {})
    targets = [c for c, t in types.items() if t in {"int", "float"}] + list(per_column)
    report: dict[str, dict] = {}
    for column in dict.fromkeys(targets):
        if column not in header:
            continue
        method = per_column.get(column, {}).get("method", method_default)
        if method == "none":
            continue
        index = header.index(column)
        values = [
            (row, n) for row in rows
            if index < len(row) and not _is_ghost(row[index])
            for n in [_to_number(row[index])] if n is not None
        ]
        if len(values) < 4:
            continue
        numbers = sorted(n for _, n in values)
        if method == "iqr":
            q1, _, q3 = statistics.quantiles(numbers, n=4)
            lower, upper = q1 - k * (q3 - q1), q3 + k * (q3 - q1)
        elif method == "percentile":
            bounds = statistics.quantiles(numbers, n=200)
            lower, upper = bounds[0], bounds[-1]
        else:  # zscore
            mean = statistics.fmean(numbers)
            stdev = statistics.stdev(numbers) or 1.0
            lower, upper = mean - 3 * stdev, mean + 3 * stdev
        clipped = 0
        for row, number in values:
            if number < lower:
                row[index] = str(round(lower, 2))
                clipped += 1
            elif number > upper:
                row[index] = str(round(upper, 2))
                clipped += 1
        if clipped:
            report[column] = {
                "method": method, "lower": round(lower, 2), "upper": round(upper, 2),
                "clipped": clipped,
            }
    return report


def main() -> None:
    if len(sys.argv) != 4:
        raise SystemExit("用法：clean.py <data_file> <rules.json> <out_file>")
    data_file, rules_path, out_file = sys.argv[1:]
    with open(rules_path, encoding="utf-8") as stream:
        rules = json.load(stream)
    try:
        out_stream = open(out_file, "x", encoding="utf-8-sig", newline="")
    except FileExistsError:
        print(f"拒绝覆盖已存在的交付文件：{out_file}", file=sys.stderr)
        raise SystemExit(1)

    header, rows = _read_rows(data_file)
    rows_in = len(rows)
    width = len(header)
    rows = [row + [""] * (width - len(row)) if len(row) < width else row[:width] for row in rows]

    audit: dict = {"rows_in": rows_in, "warnings": []}

    # 2. 列标准化
    header, renamed = _standardize_columns(header)
    audit["columns_renamed"] = renamed

    # 3. 去重（全行）
    if rules.get("dedup", True):
        seen: set[tuple] = set()
        kept = []
        for row in rows:
            key = tuple(row)
            if key not in seen:
                seen.add(key)
                kept.append(row)
        audit["duplicates_removed"] = len(rows) - len(kept)
        rows = kept
    else:
        audit["duplicates_removed"] = 0

    # 4. 哨兵值 → 缺失
    sentinel_hits: dict[str, int] = {}
    for column, sentinels in rules.get("sentinels", {}).items():
        if column not in header:
            audit["warnings"].append(f"sentinels 引用了不存在的列：{column}")
            continue
        index = header.index(column)
        tokens = {str(s) for s in sentinels}
        hits = 0
        for row in rows:
            if row[index].strip() in tokens:
                row[index] = ""
                hits += 1
        if hits:
            sentinel_hits[column] = hits
    audit["sentinel_to_na"] = sentinel_hits

    # 5. 幽灵字符串统一
    ghost = 0
    for row in rows:
        for index, value in enumerate(row):
            if value != "" and _is_ghost(value):
                row[index] = ""
                ghost += 1
    audit["ghost_cleaned"] = ghost

    # 6. 类型规整
    types = rules.get("types", {})
    rows, audit["type_coerce_failures"] = _coerce_columns(header, rows, types)

    # 7. 缺失填充
    audit["missing_filled"] = _fill_missing(header, rows, rules.get("missing", {}), types)

    # 8. 文本规整
    normalized = 0
    for column in rules.get("text_normalize", []):
        if column not in header:
            audit["warnings"].append(f"text_normalize 引用了不存在的列：{column}")
            continue
        index = header.index(column)
        for row in rows:
            value = unicodedata.normalize("NFKC", row[index]).strip()
            if value != row[index]:
                row[index] = value
                normalized += 1
    audit["text_normalized"] = normalized

    # 9. 异常值裁剪（只裁剪不删行）
    audit["outliers_clipped"] = _clip_outliers(header, rows, rules.get("outlier", {}), types)

    rows_out = len(rows)
    audit["rows_out"] = rows_out
    audit["retention_rate_pct"] = round(rows_out / rows_in * 100, 2) if rows_in else 0.0

    writer = csv.writer(out_stream)
    writer.writerow(header)
    writer.writerows(rows)
    out_stream.close()
    print(json.dumps(audit, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
