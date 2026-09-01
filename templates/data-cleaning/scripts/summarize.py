"""数据清洗示例：读清洗后的 CSV，产出 JSON 报告 {"rows": N, "columns": [...]} 到 stdout。

用法（由 graph.yaml 的 do 模板填槽后调用）：
    python3 scripts/summarize.py <清洗后.csv> > <产物路径>
"""
import csv
import json
import sys


def main() -> int:
    with open(sys.argv[1], newline="") as f:
        rows = list(csv.reader(f))
    header = rows[0] if rows else []
    report = {"rows": max(len(rows) - 1, 0), "columns": header}
    json.dump(report, sys.stdout, ensure_ascii=False)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
