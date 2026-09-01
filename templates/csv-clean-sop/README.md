# csv-clean-sop —— 固定 SOP 型数据清洗 skill 的 GraphTree 转化

把"探查画像 → 规则决策 → 确定性清洗 → 多重验收 → 审计报告"这条固定 SOP
从"提示词里请 AI 自觉遵守"升级为"verify 钉死、不过不能往下走"的图。

## SOP 来源

流程与规则概念取自开源 skill
[lytssaa/data-cleaning-skill](https://github.com/lytssaa/data-cleaning-skill)
（五段单向管线 + "AI 决策、Python 执行" + 留存率/异常值审计口径）。
该仓库**未附 license**，故本图包只转化其 SOP 流程，`scripts/` 全部为
本仓库自写实现（纯标准库，与 GraphTree 运行时零新增依赖保持一致）。
范围收缩：仅 CSV/TSV，源 skill 的 15 格式 ingest 与语义层未转化。

社区选型说明：数据清洗类 skill 无公认"最常用"者——星数最高的相邻物
K-Dense-AI/scientific-agent-skills（41k★，163 个技能）无清洗技能；
coffeefuelbump/csv-data-summarizer（457★）是分析向而非清洗向。
lytssaa 这个是专用清洗 skill 中 SOP 最成型者（自带强制 checklist 与
审计契约），故选它。

## 节点树

```
clean_sop (sequence)                     verify: verify_report.py + metric
├── profile (run)      数据画像           verify: run + metric(len(columns)>0)
├── rules (try, route: static)           verify: run test -s
│   ├── given (run)    rules_file 非空 → 校验人工规则   route_hint 有
│   └── decide (llm)   空 → AI 读画像按决策表写规则
│                      verify: run(validate_rules) + llm(决策质量)；on_fail retry 2
├── execute (run)      确定性清洗管线     danger: true（写图外交付物）
│                      verify: run + metric(rows_out>0) + metric(留存率≥90)
│                            + llm(SOP 红线评审) + human(抽查批准)
└── report (run)       审计 → md 报告 + json 摘要   verify: run + metric
```

## params（三键必须全给；一律用绝对路径）

| 键 | 含义 |
|---|---|
| `data_file` | 待清洗 CSV/TSV（只读，绝不就地覆盖） |
| `rules_file` | 人工规则 JSON 路径；**空串 = 走 llm 叶子由 AI 按画像决策** |
| `out_file` | 清洗结果交付路径（已存在则 execute 拒绝并失败） |

规则 JSON schema 见 `scripts/validate_rules.py`  docstring。

## 人何时被问

一次 run 共两处，都在 `execute`：

1. **danger gate**（执行前）：批准即将运行的清洗命令（evidence 含填槽后
   命令全文与规则产物路径，可先打开规则核对）；
2. **verify human**（执行后）：抽查交付文件 + 审计，批准才算交付。

## 失败 / 放弃 / resume

- `given` 失败（rules_file 为空或非法）→ try 自动换路 `decide`，台账留
  `node_failed` + 后续候选事件；两候选都失败 → try 池耗尽 → run failed（exit 1）。
- `decide` 产物不合 schema / AI 评审不过 → `on_fail: {retry: 2}` 原地重试
  （同一 prompt 重发，不喂反馈——M1 语义）。
- `execute` 留存率 < 90% 或评审/人否决 → 节点 failed → run failed（exit 1）。
  **规则要改 = 改 rules_file 开新 run**；resume 不会重算已 succeeded 的
  rules（断点 memo），在 failed 的 run 里改规则没有意义。
- danger 拒绝 → execute abandoned 直传根 → run abandoned（exit 2），
  `graphtree resume` 重进重过 gate。
- 注意 M0 已知限制：run 期间 cwd = 图包目录，脚本在包内留文件会破 pin；
  本包脚本只写 stdout 与 params 指定的图外路径，不受影响。

## M1 机制用法对照

| 机制 | 位置 |
|---|---|
| `do: llm` | `rules.decide`（AI 决策规则，产物契约 = 写到 {artifact}） |
| `try` + `route_hint` | `rules`（人工规则 / AI 决策 两条预声明走法） |
| verify `llm` | `decide`（决策质量软判定）、`execute`（SOP 红线评审） |
| verify `human` | `execute`（抽查批准） |
| verify `metric` | 画像列数、留存率 ≥ 90、rows_out > 0（编译期钉死） |
| `danger` | `execute`（写图外交付物） |
| `on_fail: retry` | `decide`（retry 2） |
| `needs` | 根节点显式声明 `[shell, llm, human]`（与推导一致，多报合法） |

未使用：`on_fail: abort/ask`——本 SOP 的失败语义是"改规则开新 run"
（运行期结构冻结），ask 重进无法更换上游已 memo 的 rules 输入，故不挂；
`{tried.*}`——`decide` 不需要 given 的失败原因（空 rules_file 是常态换路，
不是错误）。

## 已知限制

- **needs 是图级并集**：只要图里存在 `decide`（llm 叶子），即使走
  `given` 分支（纯人工规则、实际不调用 AI），run 也要求
  `~/.graphtree/config.yaml` 在位且握手含 llm 端口。纯 shell 降级跑
  人工规则路径在当前 M1 语义下不可行。
- retry 不喂失败反馈（M1 既定语义）：`decide` 重试是同一 prompt 重发，
  AI 评审意见不会进入下一次尝试；要利用评审意见只能人改 rules_file 开新 run。
- 仅 CSV/TSV；类型系统为 int/float/date/str；日期格式为内置四种。

## 验证记录（2026-09-01）

- `check`：通过（`check: ok`）。
- 脚本 sanity：profile → validate → clean → report → verify 全链直跑通过
  （examples/dirty.csv：16 → 15 行，留存 93.75%，哨兵/幽灵/类型失败/填充
  各计数入审计）。
- **降级端到端 run**（`~/.graphtree/config.yaml` 不在位，按 docs/phases
  提示词的分档口径用桩代替真实模型，**不代表真实 kimi 验收**）：
  - fake kimi 可执行体（work 写预制规则 / judge 恒过）+ pty 驱动人答；
  - happy path（rules_file 空 → decide 候选）：exit 0，台账 36 事件
    （含 try 换路 node_failed(given) → call_ai(decide)、danger 的
    call_human、五重 verify 全 pass、run_start ports 快照）；
  - 人工规则路径（rules_file 非空 → given 候选）：exit 0；
  - danger 拒绝 → exit 2 abandoned（abandonment 五要素齐全）→
    resume 重过 gate → exit 0 succeeded。
- **真实 AI 端到端 run（pi 后端，2026-09-01）**：`ai.kimi.executable` 指向
  `pi`（调用面同构 `pi -p`），模型 qwen3.8-max（bailian provider），pty
  驱动人答。结果：
  - **exit 0 succeeded**（run `20260901-125904-e6b9`）：decide 真实 call_ai
    70s 产出合法规则（列名用画像 normalized 字段、salary_元 判长尾选 none、
    哨兵 -5/300/-999 全中），两处 verify llm 真实评审通过并给出逐条理由，
    danger + verify human 真实过闸。
  - **此前两次真实 run 的失败同样有价值**，证明 verify 钉死不是摆设：
    a) exit 1——decide 把 `salary_元` 猜成 `salary_yuan` 导致哨兵规则静默
    未命中，execute 的 verify llm 读交付文件后如实否决（顺带暴露了
    "AI 无法预测列名规范化"的设计缺陷 → 已修：profile 增 `normalized`
    字段；及 int 列中位数填充取整）；b) exit 3——pi 的 judge 响应偶发
    非纯 JSON，撞 KimiCLI 整段 `json.loads` 解析 → `PortError(bad_output)`。
    适配器解析脆性是真实发现，M1 实现侧建议增强（提取首个 JSON 对象）；
    实验侧可用垫片 executable 绕过（本仓库不收垫片）。
- **真实 kimi 端到端 run（2026-09-01）**：exit 0 succeeded（run
  `20260901-134048-eed6`）：decide 真实 call_ai（45s）产出合法规则，两处
  verify llm 真实评审通过（理由逐条入台账），danger + verify human 过闸。
  首轮真实 kimi run 暴露适配器 bug（judge/route 解析不吃 kimi 的 "• "
  transcript 前缀 → bad_output，exit 3），已修 `adapters/kimi_cli.py`
  （提取首个括号配平 JSON 对象）并加回归测试。
- **仍未验证**：`route: llm` 选路（本图用 static）。
