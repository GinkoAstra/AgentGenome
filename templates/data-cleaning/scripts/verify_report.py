"""数据清洗示例：根节点 verify 脚本——报告 JSON 必须含非负整数的 rows 字段。

用法（由 graph.yaml 根节点 verify 的 run 规则填槽后调用）：
    python3 scripts/verify_report.py <report.json>
退出码 0 = 过，非 0 = 不过（system-design §1.2）。
"""
import json
import sys


def main() -> int:
    with open(sys.argv[1]) as f:
        report = json.load(f)
    rows = report.get("rows")
    if not isinstance(rows, int) or isinstance(rows, bool) or rows < 0:
        print(f"verify_report: bad rows field: {rows!r}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
