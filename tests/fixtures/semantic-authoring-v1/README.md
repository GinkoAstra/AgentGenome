# Skill 原文语义验收集 v1

8 个开发期构造案例、9 个原文文件。原文由一个 Codex 角色编写，另一个角色仅看原文与原始业务契约，在生成结果出现前独立标注。不是人类盲评，不是算法收益 holdout，也不能据此外推任意领域准确率。

原文只在 `cases/case-*/SKILL.md` 和明确引用的 `details.md`；标签位于独立 `labels.json`，不进入模型来源包。`manifest.json` 固定每个原文、引用图和标签的摘要。更改任何材料后不得把新结果拼接到本版本；须显式形成新评估版本。

| 案例 | 独立预期 |
|---|---|
| 01 | 明确最大整数 revision，固定步骤，编制交付 |
| 02 | 同义措辞及引用附件，允许 Pi，编制交付 |
| 03 | 完全省略保留规则，先问；不能伪造原文说明缺失 |
| 04 | “最新”含义不明，先问；不能借候选默认值 |
| 05 | 明确首次记录，固定步骤，不能擅自换最大 revision |
| 06 | 两种完整口径显式留到每次运行选择，编制不追问也不默认 |
| 07 | 原文必做上传超出本地声明能力，定位第19行拒绝 |
| 08 | 背景/示例中的排除指定客户仍是必做约束，定位第23行拒绝 |

默认只校验冻结材料，不构造 Pi 端口、不调用模型、不执行业务：

```bash
python3 scripts/verify_sop_semantics.py --output /tmp/sop-semantic-preparation
```

输出 `preparation_valid=true,status=not_run,complete=false,cases=[]` 表示准备可用，不能说8例已通过。旧后端 HTTP 403 保留为历史。2026-09-29已适配新Pi与已配置模型，新端点探测被自动审批在派发前拦截，等待明确的数据发送授权；本集仍未发真实请求。

取得新模型端点的数据发送授权后，在新的空目录显式运行：

```bash
export SOP_PI_PROVIDER=idealab
export SOP_PI_MODEL=bailian/deepseek-v4.1-flash
python3 scripts/verify_sop_semantics.py --backend pi \
  --max-model-calls 12 --output /tmp/sop-real-semantics
```

每例隔离存储；生成与审查独立调用。每个草稿最多预留12次模型请求，每端口最多3次、每请求4096输出tokens；8例总上限96次预留，实际请求/usage另存。只对03/04的正确结构化问题交付已冻结的模拟答案，并重新审查；不自动修订失败解释、不自动换模型、不回退替身。第一次后端/基础设施失败停止整批，保存失败例和未运行例。模型协议或语义错误计为该例失败，不能作为正确拒绝。

程序判定核对原文身份/覆盖、初始状态、执行模式、保留规则与参数域、父契约检查和对应表。预期拒绝必须有未支持要求或独立审查拒绝，并精确定位预先标注的单行、附非空说明；整篇拒绝或任意停止不能算识别正确。结构检查仍不能形式证明全部自然语言含义，审阅时应结合冻结 `source_constraints`、原文和保存的分析逐项复核。

每例保存 `initial.json`、可选 `answer-message.json`、`final.json`、`assessment.json` 及 `.data/authoring` 持久记录。汇总保存材料版本、实现/驱动摘要、模型尝试、预算与错误分类。定义交付与订单运行/验收/发布是不同状态；本驱动只测编制语义。真实递归 A/B 和任务内工具分别用 `verify_recursive_sop.py --backend pi`、`verify_sop_extensions.py --backend pi`；算法比较另按预登记，不共享本集作收益测试集。
