#!/usr/bin/env python3
"""SOP 第五步：审计 JSON → 人读 Markdown 报告 + 机读摘要 JSON。

用法：quality_report.py <audit.json> <out_file>
- Markdown 报告写到 <out_file>.report.md（对外交付物，随清洗结果一起走）；
- 摘要 JSON 写 stdout（run 的 artifact，供根节点 compose / verify 消费）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("用法：quality_report.py <audit.json> <out_file>")
    with open(sys.argv[1], encoding="utf-8") as stream:
        audit = json.load(stream)
    out_file = sys.argv[2]
    report_file = out_file + ".report.md"

    lines = [
        "# 数据清洗审计报告",
        "",
        f"- 输入行数：{audit['rows_in']}",
        f"- 输出行数：{audit['rows_out']}",
        f"- 留存率：{audit['retention_rate_pct']}%",
        f"- 去重删除：{audit['duplicates_removed']} 行",
        f"- 幽灵字符串清洗：{audit['ghost_cleaned']} 格",
        "",
        "## 明细",
        "",
        f"- 列重命名：`{json.dumps(audit['columns_renamed'], ensure_ascii=False)}`",
        f"- 哨兵值转缺失：`{json.dumps(audit['sentinel_to_na'], ensure_ascii=False)}`",
        f"- 类型转换失败置空：`{json.dumps(audit['type_coerce_failures'], ensure_ascii=False)}`",
        f"- 缺失填充：`{json.dumps(audit['missing_filled'], ensure_ascii=False)}`",
        f"- 文本规整格数：{audit['text_normalized']}",
        f"- 异常值裁剪（不删行）：`{json.dumps(audit['outliers_clipped'], ensure_ascii=False)}`",
        "",
        "## 警告",
        "",
    ]
    lines += [f"- {w}" for w in audit.get("warnings", [])] or ["- （无）"]
    Path(report_file).write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(json.dumps({
        "out_file": out_file,
        "report_file": report_file,
        "rows_in": audit["rows_in"],
        "rows_out": audit["rows_out"],
        "retention_rate_pct": audit["retention_rate_pct"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
