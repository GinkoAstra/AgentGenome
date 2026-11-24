# Run the examples

[项目首页](../../README.md) · [中文说明](../../README.zh-CN.md) · [示例总览](../README.md) · [交互导览](../../docs/showcase/) · [Quickstart](../../docs/quickstart.md)

这些例子使用同一 Graph /2 协议，业务规则留在 Task / Checker 中，Graph YAML 与输入文件保持现有契约。清洗和 Goal 的程序 Checker 是可选外部程序，必须由部署者通过 `--program-config` 显式加载；服务不会按 Graph 字段导入任意 Python 代码，也不再支持 `--script-agent` 工厂。

| 文件 | 内容 |
| --- | --- |
| `graph-clean.yaml`、`clean-input.json` | 规范化、去重，再生成统计的两阶段 SOP |
| `graph-goal.yaml`、`research-notes.md` | 从固定离线材料选择原文证据的 Goal |
| `graph-builder.yaml`、[GRAPH-BUILDER.md](GRAPH-BUILDER.md) | 用已确认需求生成 Graph YAML |
| `graph-two-stage.yaml` | 使用受控外部程序夹具的协议示例，不代表模型业务效果 |
| `graph-human-gate.yaml` | 先学习主题、再在用户确认后进入练习的两阶段学习 SOP |
| `graph-command-probe.yaml` | Qoder Worker 显式授权只读宿主命令，由独立 Checker 复核；非沙箱 |
| [../programs/](../programs/) | 清洗与 Goal 使用的可选外部程序 |

先确定要运行的 Core。可以是安装器打印的绝对路径，也可以是本地源码构建产物：

```bash
CORE=/absolute/path/printed/by/agentgenome-setup
# 或：CORE="$PWD/rust/target/release/agentgenome"
PYTHON=$(command -v python3)
test -n "$CORE" -a -x "$CORE" -a -n "$PYTHON"
```

`PYTHON` 必须是绝对路径；程序配置不会做 `PATH` 查找。以下命令都在仓库根目录执行。

## 先运行一个清洗 SOP

为示例使用新的数据目录，避免改变正在运行的默认服务：

```bash
AG_DEMO_DIR=$(mktemp -d "${TMPDIR:-/tmp}/agentgenome-clean.XXXXXX")
AG_DEMO_WORK="$AG_DEMO_DIR/workspace"
mkdir -p "$AG_DEMO_WORK"
cp examples/agent-workflow/graph-clean.yaml "$AG_DEMO_WORK/sop.yaml"
cp examples/agent-workflow/clean-input.json "$AG_DEMO_WORK/input.json"
```

程序配置是部署者的可信文件。`code_path` 与 `interpreter` 都写成绝对路径；`code_sha256` 必须与当前程序文件一致。`backend_ref: "script"` 与 Graph 中的两个 `script` Agent 对应。

```bash
cat > "$AG_DEMO_DIR/clean-programs.json" <<EOF
{
  "format": "agentgenome-program-deployments/1",
  "programs": [{
    "backend_ref": "script",
    "code_path": "$PWD/examples/programs/data_clean_checker.py",
    "code_sha256": "905b8f385f6785ba0294e8d579fe8406adb193924b9d1deda551c9ff3f184d30",
    "interpreter": "$PYTHON",
    "prefix_args": [],
    "arguments": [],
    "capabilities": {
      "resumable_session": true,
      "live_observation": "full",
      "idempotent_delivery": true,
      "stop": "cooperative",
      "checkpoint": true,
      "backend_tools": [],
      "internal_activities": "none"
    },
    "provider_accounting": "no_model"
  }]
}
EOF
```

启动独立实例。Pi 是主要 Worker 后端，`script` 程序承担两个 Checker；`--provider` / `--model` 是服务端 Worker 与语义 Checker 的配置：

```bash
"$CORE" start --data-dir "$AG_DEMO_DIR" \
  --backend pi --provider YOUR_PROVIDER --model YOUR_MODEL \
  --program-config "$AG_DEMO_DIR/clean-programs.json"

"$CORE" teacher --data-dir "$AG_DEMO_DIR" --workspace "$AG_DEMO_WORK" \
  --provider YOUR_PROVIDER --model YOUR_MODEL
```

`start` 已经显式启动服务后才打开 `teacher`。`teacher` 打开的仍是普通 Pi 对话，通过 MCP 操作同一个 API；它使用自己的工作区，并在缺失时创建 `sop-guide.md` 与 `sop-template.yaml`，不覆盖已有文件。

在对话中输入：

> 请读取 sop.yaml 和 input.json，向我解释规则，然后按现有 SOP 校验、发布和运行。用真实报告确认两个阶段的接受结果，将接受的 clean.json 与 profile.json 导出到 deliveries，不要自行编写业务产物。

Teacher 应依次调用：`validate_definition` → `publish_definition` → `put_artifact` → `create_run`。本例的 Run 输入为 `dataset`，值是 `put_artifact` 返回的完整 `ArtifactRef`。随后查询报告和事件，并用 `read_artifact` 导出接受的内容。

预期结果：六行变为 Alice、Bob、Carol 三行；丢弃两行无效记录和一行重复记录；统计为 `{"rows": 3, "domains": {"example.com": 3}}`。`clean.json` 还包含审计计数 `input_rows: 6`、`output_rows: 3`、`invalid_rows: 2`、`duplicate_rows: 1`。核验导出的实际文件：

```bash
"$PYTHON" - "$AG_DEMO_WORK/deliveries/clean.json" \
  "$AG_DEMO_WORK/deliveries/profile.json" <<'PY'
import json
import sys

clean = json.load(open(sys.argv[1], encoding="utf-8"))
profile = json.load(open(sys.argv[2], encoding="utf-8"))
assert clean["rows"] == [
    {"name": "Alice", "email": "alice@example.com"},
    {"name": "Bob", "email": "bob@example.com"},
    {"name": "Carol", "email": "carol@example.com"},
]
assert clean["audit"] == {
    "input_rows": 6,
    "output_rows": 3,
    "invalid_rows": 2,
    "duplicate_rows": 1,
}
assert profile == {"rows": 3, "domains": {"example.com": 3}}
print("accepted clean/profile outputs match the example input")
PY
```

如果模型或 Checker 失败，应保留报告并检查反馈，不应手工补造产物后声称运行成功。

退出 Teacher 后，仍可查询该服务。完成演示后，只停止自己创建的实例：

```bash
"$CORE" status --data-dir "$AG_DEMO_DIR"
"$CORE" ctl status --data-dir "$AG_DEMO_DIR"
"$CORE" stop --data-dir "$AG_DEMO_DIR"
```

`mktemp` 目录适合试运行；长期业务请使用自选持久目录，并在重开会话时指向同一目录。停止服务不删除该目录。

## Goal：选择证据并生成离线页面

另建数据目录与工作区，将 `graph-goal.yaml` 复制为 `sop.yaml`、`research-notes.md` 复制为 `material.md`。程序仍是独立外部进程，但这次使用 `research_workflow.py` 和它自己的代码摘要：

```bash
AG_GOAL_DIR=$(mktemp -d "${TMPDIR:-/tmp}/agentgenome-goal.XXXXXX")
AG_GOAL_WORK="$AG_GOAL_DIR/workspace"
mkdir -p "$AG_GOAL_WORK"
cp examples/agent-workflow/graph-goal.yaml "$AG_GOAL_WORK/sop.yaml"
cp examples/agent-workflow/research-notes.md "$AG_GOAL_WORK/material.md"

cat > "$AG_GOAL_DIR/goal-programs.json" <<EOF
{
  "format": "agentgenome-program-deployments/1",
  "programs": [{
    "backend_ref": "script",
    "code_path": "$PWD/examples/programs/research_workflow.py",
    "code_sha256": "c213bd28d996ed1724dd91517c46b8ebd31991bbac63ab97d98be0f38403fe2a",
    "interpreter": "$PYTHON",
    "prefix_args": [],
    "arguments": [],
    "capabilities": {
      "resumable_session": true,
      "live_observation": "full",
      "idempotent_delivery": true,
      "stop": "cooperative",
      "checkpoint": true,
      "backend_tools": [],
      "internal_activities": "none"
    },
    "provider_accounting": "no_model"
  }]
}
EOF

"$CORE" start --data-dir "$AG_GOAL_DIR" \
  --backend pi --provider YOUR_PROVIDER --model YOUR_MODEL \
  --program-config "$AG_GOAL_DIR/goal-programs.json"

"$CORE" teacher --data-dir "$AG_GOAL_DIR" --workspace "$AG_GOAL_WORK" \
  --provider YOUR_PROVIDER --model YOUR_MODEL
```

其余步骤与清洗相同，Run 输入键为 `material`，媒体类型为 `text/markdown`。Pi 选择每个来源段的原文引用；程序检查标题、引用覆盖和原文对应关系，再确定性生成与核对 `lesson.html`。

这个 Goal 不联网，不检索最新资料，不证明引用论断本身正确。它验证的是已冻结材料的证据选择与可复算输出。`quotes-work` 配置了连续两次无改善的停滞边界；没有可接收回交的父节点时按失败边界结束，不无限重试。完成后停止独立实例：

```bash
"$CORE" stop --data-dir "$AG_GOAL_DIR"
```

## 人工进入确认：两阶段学习 SOP

`graph-human-gate.yaml` 是一个具体的学习流程：Run 输入是一份冻结的 `topic` 材料；`study` 阶段立即生成课程笔记、一个例题和至少三个自测问题；`practice` 阶段在用户明确表示“已复习完并准备好”之前保持 `pending`，确认后才生成练习题、参考答案和反馈；`check` 阶段按验收规则给出 receipt。确认只覆盖 `practice` 的首次进入，后续返工或契约修订不会再问一次。

### 在已连接的 `/agentgenome` 会话中使用（推荐）

如果服务已经部署并运行，直接在现有对话里启用 `/agentgenome`（Pi、Qoder、Claude Code 使用 `/agentgenome`，Codex 使用 `$agentgenome`），不需要为示例另起一个 Host，也不需要新安装入口。工作目录只放要交给这次 Run 的副本：

```bash
mkdir -p ./agentgenome-learning
cp examples/agent-workflow/graph-human-gate.yaml ./agentgenome-learning/sop.yaml
cd ./agentgenome-learning
```

在该目录的宿主会话中启用 `/agentgenome`，然后把主题内容作为 Run 输入交给 Teacher，例如：

> 读取 sop.yaml，向我说明两个阶段的验收规则。把下面的主题材料用 put_artifact 上传，按现有 SOP 校验、发布并创建 Run。study 接受后，把 practice 冻结的进入提示原样给我看并等待我的明确答复；不要自动确认。我确认后，用 confirm_node_start 记录确认，并在终态用真实报告确认两个阶段的结果，把接受的笔记和练习产物导出到 deliveries。

粘贴准备学习的主题文本作为 `topic` 输入；Teacher 依次使用 `validate_definition` → `publish_definition` → `put_artifact` → `create_run`。`study` 接受后，`practice` 在资源、Trace 和灯板中显示 `awaiting_confirmation`、冻结的提示、`contract_ref` 与 `input_refs_digest`；此时不应存在它的 Worker。只有用户看过提示并明确要求确认后，Teacher 才读取该节点当前 `revision` 并调用：

```json
{
  "run_id": "<run_id>",
  "node_id": "<practice node_id>",
  "expected_revision": 1,
  "request_id": "<unique request id>"
}
```

成功响应包含 `event_id`、新 `revision` 和 `start_gate.status == "approved"`。同一 `request_id` 与相同参数重试返回同一结果；之后 Scheduler 才创建唯一 Worker 绑定，投影变为 `entered`。若 Run 处于暂停、取消、失败或阻塞状态，旧的等待不会继续显示为待确认；恢复 `desired=running` 后才会重新显示等待。

边界：

- Teacher **不得替用户自动确认**，也不得把“开始运行”解释为对提示内容的同意；必须先展示冻结提示并取得用户明确答复。提示文本随定义冻结，Trace 只能展示，不能代替确认。
- 该确认只覆盖首次进入。后续轮次、返工（review 要求 revise）或契约修订不会再次要求进入确认，也不会把旧批准当成对修订后契约的接受；旧批准在投影中保留原有 `contract_ref` 与 `input_refs_digest`。
- Trace 终端与浏览器页面都是只读的，不提供确认按钮；`confirm_node_start` 只属于 publisher，不在 Worker 工具箱中。
- 本示例由结构校验、FakeBackend 集成测试和 MCP 传输测试覆盖（`rust/tests/human_gates.rs` 与 `rust/tests/r04c_client.rs`），包括等待、一次性确认、唯一绑定、返工保留进入状态、暂停/取消隐藏等待、声明期限过期和动态子节点的门禁；这些测试不产生真实模型业务效果，也没有在真实模型上验证学习质量。

### 可选：独立 Host 开发调试

只有在需要隔离数据目录、独立模型配置或调试 Host 本身时，才另起实例（Workers 仍使用真实 Pi 后端）：

```bash
AG_GATE_DIR=$(mktemp -d "${TMPDIR:-/tmp}/agentgenome-gate.XXXXXX")
AG_GATE_WORK="$AG_GATE_DIR/workspace"
mkdir -p "$AG_GATE_WORK"
cp examples/agent-workflow/graph-human-gate.yaml "$AG_GATE_WORK/sop.yaml"

"$CORE" start --data-dir "$AG_GATE_DIR" \
  --backend pi --provider YOUR_PROVIDER --model YOUR_MODEL
"$CORE" teacher --data-dir "$AG_GATE_DIR" --workspace "$AG_GATE_WORK" \
  --provider YOUR_PROVIDER --model YOUR_MODEL
```

然后按上一节的对话流程操作，`put_artifact` 的 Run 输入键是 `topic`。完成后只停止自己创建的实例：

```bash
"$CORE" stop --data-dir "$AG_GATE_DIR"
```

## 版本与验证边界

程序配置受启动时校验：格式必须是 `agentgenome-program-deployments/1`；`backend_ref` 唯一；`code_sha256` 必须匹配实际文件；解释器必须是可执行文件；程序必须声明空的 `backend_tools` 和 `internal_activities: none`。服务会记录程序文件、摘要、解释器与参数身份；后续配置不一致时会报告差异。每个示例 Host 只加载匹配的配置，清洗与 Goal 不交叉加载。

程序仅支持代码中明确列出的冻结 Task 版本。改输入、Graph id 或预算可以复用原 Task；更改业务规则需要同步修订并验证对应 Checker，不能只改 YAML 就期望原程序接受新的契约。

Python 只是这两个示例程序的依赖；Core 运行、安装和其他后端不需要 Python。不调用模型的本地验证：

```bash
cargo test --manifest-path rust/Cargo.toml --test r05d_business
cargo test --manifest-path rust/Cargo.toml --test r05d_program_protocol
node --test tests/install/*.test.mjs
```

这些测试证明协议接线、程序验收和受控路径；真实业务仍需配置模型运行并核对产物。即使输入和配置相同，也不承诺模型重复输出完全一致。Graph Builder 另见 [使用与边界](GRAPH-BUILDER.md)。
