"""数据清洗示例：读 argv[1] 的 CSV，丢含空字段的行、去完全重复行，结果写 stdout。

用法（由 graph.yaml 的 do 模板填槽后调用）：
    python3 scripts/clean.py <输入.csv> > <产物路径>
"""
import csv
import sys


def main() -> int:
    with open(sys.argv[1], newline="") as f:
        rows = list(csv.reader(f))
    if not rows:
        print("clean: empty input", file=sys.stderr)
        return 1
    header, data = rows[0], rows[1:]
    seen = set()
    kept = []
    for row in data:
        if any(cell.strip() == "" for cell in row):
            continue
        key = tuple(row)
        if key in seen:
            continue
        seen.add(key)
        kept.append(row)
    out = csv.writer(sys.stdout)
    out.writerow(header)
    out.writerows(kept)
    return 0


if __name__ == "__main__":
    sys.exit(main())
