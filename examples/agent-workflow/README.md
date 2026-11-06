# Run the examples

[返回首页](../../README.md)

这些例子使用同一 Graph /2 协议，业务规则留在 Task / Checker 中。清洗和 Goal 的程序 Checker 需要部署者显式加载，不能由 Graph 指定任意 Python 代码。

| 文件 | 内容 |
| --- | --- |
| `graph-clean.yaml`、`clean-input.json` | 规范化、去重，再生成统计的两阶段 SOP |
| `business_checks.py` | 清洗 / 统计的可信 Checker 与独立输出校验器 |
| `graph-goal.yaml`、`research-notes.md` | 从固定离线材料选择原文证据的 Goal |
| `research_workflow.py` | 证据验证、确定性渲染与验收程序 |
| `graph-builder.yaml`、[GRAPH-BUILDER.md](GRAPH-BUILDER.md) | 用已确认需求生成 Graph YAML |
| `graph-two-stage.yaml` | 使用受控 Script 夹具的协议示例，不代表模型业务效果 |

## 先运行一个清洗 SOP

在仓库根目录执行。模型名称替换为本机 Pi 已配置的值。为示例使用新的数据目录，避免改变正在运行的默认服务：

```bash
AG_DEMO_DIR=$(mktemp -d "${TMPDIR:-/tmp}/agentgenome-clean.XXXXXX")
AG_DEMO_WORK="$AG_DEMO_DIR/workspace"
mkdir -p "$AG_DEMO_WORK"
cp examples/agent-workflow/graph-clean.yaml "$AG_DEMO_WORK/sop.yaml"
cp examples/agent-workflow/clean-input.json "$AG_DEMO_WORK/input.json"

.venv/bin/agentgenome start --data-dir "$AG_DEMO_DIR" \
  --backend pi --provider YOUR_PROVIDER --model YOUR_MODEL \
  --script-agent examples/agent-workflow/business_checks.py:build_checker

.venv/bin/agentgenome teacher --data-dir "$AG_DEMO_DIR" \
  --workspace "$AG_DEMO_WORK" \
  --provider YOUR_PROVIDER --model YOUR_MODEL
```

这里 `teacher` 打开的仍是普通 Pi 对话，通过 MCP 操作同一个 API。使用专用命令是为了连接这个配置了程序 Checker 的独立示例实例；日常默认服务使用 `/agentgenome`。

在对话中输入：

> 请读取 sop.yaml 和 input.json，向我解释规则，然后按现有 SOP 校验、发布和运行。用真实报告确认两个阶段的接受结果，将接受的 clean.json 与 profile.json 导出到 deliveries，不要自行编写业务产物。

Teacher 应依次调用：`validate_definition` → `publish_definition` → `put_artifact` → `create_run`。本例的 Run 输入为 `dataset`，值是 `put_artifact` 返回的完整 ArtifactRef。随后查询报告和事件，并用 `read_artifact` 导出接受的内容。

预期结果：六行变为 Alice、Bob、Carol 三行；丢弃两行无效记录和一行重复记录；统计为 `{"rows": 3, "domains": {"example.com": 3}}`。核验导出的实际文件：

```bash
.venv/bin/python examples/agent-workflow/business_checks.py \
  examples/agent-workflow/clean-input.json \
  "$AG_DEMO_WORK/deliveries/clean.json" \
  "$AG_DEMO_WORK/deliveries/profile.json"
```

如果模型或 Checker 失败，应保留报告并检查反馈，不应手工补造产物后声称运行成功。

退出 Teacher 后，仍可查询该服务。完成演示后，只停止自己创建的实例：

```bash
.venv/bin/agentgenome-teacherctl --data-dir "$AG_DEMO_DIR" status
.venv/bin/agentgenome stop --data-dir "$AG_DEMO_DIR"
```

`mktemp` 目录适合试运行；长期业务请使用自选持久目录，并在重开会话时指向同一目录。停止服务不删除该目录。

## Goal：选择证据并生成离线页面

另建数据目录与工作区，将 `graph-goal.yaml` 复制为 `sop.yaml`、`research-notes.md` 复制为 `material.md`。启动命令中的工厂改为：

```text
--script-agent examples/agent-workflow/research_workflow.py:build_agent
```

其余步骤与清洗相同，Run 输入键为 `material`，媒体类型为 `text/markdown`。Pi 选择每个来源段的原文引用；程序检查标题、引用覆盖和原文对应关系，再确定性生成与核对 `lesson.html`。

这个 Goal 不联网，不检索最新资料，不证明引用论断本身正确。它验证的是已冻结材料的证据选择与可复算输出。`quotes-work` 配置了连续两次无改善的停滞边界；没有可接收回交的父节点时按失败边界结束，不无限重试。

## 版本与验证边界

程序 Checker 仅支持代码中明确列出的冻结 Task 版本。改输入、Graph id 或预算可以复用原 Task；更改业务规则需要同步修订并验证对应 Checker，不能只改 YAML 就期望原程序接受新的契约。

每个示例 Host 注册一个匹配的可信工厂。清洗与 Goal 使用不同实例，不把它们的工厂交叉加载。Host 记录程序文件、工厂名及内容身份；后续配置不一致时会报告差异。

不调用模型的本地验证：

```bash
.venv/bin/python -m pytest \
  tests/control_plane/test_examples_business.py \
  tests/control_plane/test_w06f_goal_evidence.py -q
```

这些测试证明协议接线、程序验收和受控路径；真实业务仍需配置模型运行并核对产物。Graph Builder 另见 [使用与边界](GRAPH-BUILDER.md)。
