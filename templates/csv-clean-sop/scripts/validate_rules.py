#!/usr/bin/env python3
"""规则 JSON 的机械校验与规范化（canonical 化后写 stdout）。

规则 schema（本图包自定义，供 clean.py 消费）：
{
  "types":          {列: "int"|"float"|"str"|"date"},
  "dedup":          true|false,
  "missing":        {列: "median"|"mean"|"mode"|任意字面量},
  "sentinels":      {列: [哨兵值...]},
  "outlier":        {"method": "iqr"|"percentile"|"zscore"|"none",
                     "k": 1.5,
                     "per_column": {列: {"method": ...}}},
  "text_normalize": [列...]
}

合法 → canonical JSON 写 stdout，退出 0；非法 → stderr 逐条原因，退出 1。
"""

from __future__ import annotations

import json
import sys

_TYPE_TARGETS = {"int", "float", "str", "date"}
_OUTLIER_METHODS = {"iqr", "percentile", "zscore", "none"}
_TOP_KEYS = {"types", "dedup", "missing", "sentinels", "outlier", "text_normalize"}


def _fail(errors: list[str]) -> None:
    for error in errors:
        print(f"规则非法：{error}", file=sys.stderr)
    raise SystemExit(1)


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("用法：validate_rules.py <rules.json>")
    try:
        with open(sys.argv[1], encoding="utf-8") as stream:
            rules = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"规则文件不可解析：{exc}", file=sys.stderr)
        raise SystemExit(1)
    if not isinstance(rules, dict):
        _fail(["顶层必须是 JSON 对象"])

    errors: list[str] = []
    unknown = set(rules) - _TOP_KEYS
    if unknown:
        errors.append(f"未知键：{sorted(unknown)}；合法键为 {sorted(_TOP_KEYS)}")

    types = rules.get("types", {})
    if not isinstance(types, dict):
        errors.append("types 必须是 {列: 目标类型} 映射")
    else:
        for column, target in types.items():
            if target not in _TYPE_TARGETS:
                errors.append(f"types.{column} 目标类型 {target!r} 越词表 {sorted(_TYPE_TARGETS)}")

    dedup = rules.get("dedup", True)
    if not isinstance(dedup, bool):
        errors.append("dedup 必须是 bool")

    missing = rules.get("missing", {})
    if not isinstance(missing, dict) or any(not isinstance(v, (str, int, float)) for v in missing.values()):
        errors.append('missing 必须是 {列: "median"|"mean"|"mode"|字面量} 映射')

    sentinels = rules.get("sentinels", {})
    if not isinstance(sentinels, dict) or any(not isinstance(v, list) for v in sentinels.values()):
        errors.append("sentinels 必须是 {列: [哨兵值...]} 映射")

    outlier = rules.get("outlier", {})
    if not isinstance(outlier, dict):
        errors.append("outlier 必须是对象")
    else:
        method = outlier.get("method", "none")
        if method not in _OUTLIER_METHODS:
            errors.append(f"outlier.method {method!r} 越词表 {sorted(_OUTLIER_METHODS)}")
        k = outlier.get("k", 1.5)
        if isinstance(k, bool) or not isinstance(k, (int, float)) or k <= 0:
            errors.append("outlier.k 必须是正数")
        per_column = outlier.get("per_column", {})
        if not isinstance(per_column, dict):
            errors.append("outlier.per_column 必须是映射")
        else:
            for column, spec in per_column.items():
                if not isinstance(spec, dict) or spec.get("method") not in _OUTLIER_METHODS:
                    errors.append(f"outlier.per_column.{column} 必须声明合法 method")

    text_normalize = rules.get("text_normalize", [])
    if not isinstance(text_normalize, list) or any(not isinstance(v, str) for v in text_normalize):
        errors.append("text_normalize 必须是列名列表")

    if errors:
        _fail(errors)
    print(json.dumps(rules, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
