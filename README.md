<div align="center">

# AgentGenome

**把确认过的 SOP，交给可追踪、可验收的 Agent 执行。**

A persistent graph runtime for agent workflows.

`Python 3.11+` · `Graph /2` · `Pi + MCP` · `MIT`

[开始使用](#开始使用) · [系统结构](#系统结构) · [示例](#示例) · [开发与测试](#开发与测试)

</div>

---

AgentGenome 将任务定义、执行实例和验收结果分别记录：用 YAML 描述流程、输入输出与验收条件，由 Agent 多轮执行，再由父任务或声明的 Checker 接受结果。运行状态由独立 API 服务持久保存，对话窗口可以随时离开、重新连接和查看进展。

适合需要反复运行、明确交付物和恢复行为的 SOP。一次性搜索、编码分工或并行评审，可以直接使用 Agent 自身的 subagent。

当前源码采用重构后的 `node_runtime/control_plane/`：统一 API 服务、递归任务模型与 Pi 接入。历史提交中的旧引擎、命令和 Graph /1 格式不属于当前运行实现，安装与使用以本文为准。

## 一次业务如何完成

```text
你与 Pi 对话确认任务
        ↓
编辑 SOP YAML → 校验并发布版本 → 冻结输入、创建 Run
                                           ↓
                              Agent 执行 → 提交候选
                                           ↓
                                父任务 / Checker 验收
                                   ↙              ↘
                              反馈、返工       接受并固定输出
                                   ↑              ↓
                                   └─────── 后续任务 / 交付
```

- **流程可复用**：Graph、Task 和 Agent 定义按版本引用；输入和接受的输出保留内容身份。
- **任务可分解**：父子关系表达拆分，依赖边表达执行顺序；就绪任务在声明的并发与预算限制内执行。
- **验收可声明**：支持父任务、Agent Checker 或部署者加载的可信程序 Checker；提交候选与完成任务分开。
- **状态可恢复**：期望状态与执行事实分开保存，通过条件更新、幂等请求和执行身份协调恢复。
- **过程可查看**：Pi 终端面板、HTTP 快照、SSE 订阅和命令行使用同一服务的 trace 数据。

## CLI 执行后端

除 Pi／Script 外，现支持 Codex CLI、Qoder CLI 与 Claude Code 执行后端，继续通过同一 API、调度器和验收协议运行。详见 [配置与使用](specs/CLI_ADAPTER_USAGE.md) 和 [交付范围](specs/CLI_ADAPTER_STATUS.md)。

Codex／Qoder 已通过真实模型候选、退回、同会话续接、接受闭环；Claude 已通过真实 CLI＋受控模型协议验证，真实上游验收待完成。Kimi 尚未实现。统一安装与自动配置简化仍是后续工作，本次不宣称已提供。

## 开始使用

### 1. 安装

在取得源码后进入仓库根目录。需要 **Python 3.11+**，以及已安装、已配置模型的 **[Pi CLI](https://github.com/earendil-works/pi/tree/main/packages/coding-agent)**（`@earendil-works/pi-coding-agent`）。本次回归使用 Pi `1.5.0`、Node.js `22.22.2`，包含真实 RPC 接入检查；终端面板的历史界面验证使用 Pi `0.87.1`。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .

# 替换为你已经在 Pi 中配置好的 provider 与 model。
.venv/bin/python -m node_runtime.control_plane.pi_integration install \
  --provider YOUR_PROVIDER --model YOUR_MODEL
```

安装器仅在 Pi 的 Agent 目录写入自有扩展 `extensions/agentgenome.js`，不修改模型凭据。该入口绑定安装时的 Python 环境和源码位置，移动仓库或环境后需要重装。

这里的模型参数用于服务端 **Worker 和语义 Checker**；你当前 Pi 对话使用的模型保持原样。程序 Checker 不调用模型。

### 2. 在普通 Pi 中启用

在任意可写项目目录启动一个新的 Pi 窗口：

```bash
pi
```

输入：

```text
/agentgenome
```

然后直接描述业务，例如：

> 帮我创建一个处理客户名单的 SOP。先和我确认输入格式、去重规则、输出文件与验收条件，再生成 YAML、校验并发布。执行前先展示确认后的流程。

首次启用会连接或启动本机常驻 API 服务。草稿保存在当前工作目录；默认持久数据保存在 `$XDG_STATE_HOME/agentgenome/default`，未设置时为 `~/.local/state/agentgenome/default`。多个工作目录共享这一默认服务。

### 3. 观察与继续

| Pi 操作 | 效果 |
| --- | --- |
| `/agentgenome trace`，或点击右上角 `AgentGenome ▸` | 展开任务树、依赖、候选、评审与执行详情 |
| `Ctrl+E` | 保留面板，焦点返回对话输入 |
| `Esc` | 收起面板 |
| `/agentgenome status` | 查看当前接入状态 |
| `/agentgenome off` | 关闭本会话模式；后台服务与任务继续运行 |

退出 Pi 也不会停止服务。需要暂停或取消业务时，让 Teacher 对指定 Run 调用控制工具，并核对返回状态。完整操作见 [Pi 接入说明](docs/pi-extension.md)。

## 系统结构

以下框图区分**进程、代码组件和持久数据**。MCP 与 JSON HTTP 是同一 API 服务的协议入口。

```text
┌─ 客户端进程 ─────────────────────────────────────────────┐
│ 普通 Pi / Teacher                脚本 / 可选 CLI          │
│ 文件工具编辑 YAML；MCP 调用        JSON HTTP 调用          │
│ Pi Trace 面板：HTTP 快照 + SSE，只读                       │
└───────────────────────────┬─────────────────────────────┘
                            ↓ 网络请求
┌─ 常驻 API 服务进程 ──────────────────────────────────────┐
│ ApiServer：HTTP / MCP / SSE；认证与协议适配                │
│                          ↓                              │
│ Api：权限、版本、事务、幂等与权威状态                      │
│         ↑                           ↑                   │
│ Scheduler：依赖、拆分、执行绑定      Controller：执行协调  │
│         └── 经 API 提交执行意图 ────→ │                   │
│                                     ↓                   │
│                               Backend 适配器            │
│                         PiBackend / ScriptBackend       │
└──────────┬──────────────────────────┬────────────────────┘
           ↕ 服务内部读写              ↓ 启动 / 观察 / 停止
    [SQLite + artifacts]       [Pi Agent 子进程]
     状态 / 事件 / 内容          多轮模型请求与工具调用
                                └─ HTTP 回填候选与事实
```

ScriptBackend 在服务内运行显式加载的可信程序；它不是独立数据库或另一个调度器。请求循环与控制循环分开运行。

| 对象 | 含义 |
| --- | --- |
| Graph | 版本化的流程定义：Agent、Task、节点依赖与输入绑定 |
| Task / Node | Task 声明目标与契约；Node 是某次 Run 中的逻辑任务 |
| Worker | SOP 中的步骤角色，与 Node 共用递归任务模型；技术上的 `worker_id + epoch` 标识执行绑定 |
| Agent | 承担任务的完整执行实例，可包含多次模型请求、工具调用与恢复 |
| Candidate / Review | 候选输出与验收决策；候选被接受后才成为任务输出 |

最简单的 SOP 可以是 **One Node = One Worker = One Agent**。一轮模型回答结束，不代表任务完成。实现文件、接口与数据流见 [架构说明](docs/architecture.md)。

## 示例

| 示例 | 展示内容 | 后端 |
| --- | --- | --- |
| [数据清洗](examples/agent-workflow/graph-clean.yaml) | 六行输入，规范化与去重，再基于接受的结果生成统计 | Pi Worker + 程序 Checker |
| [证据 Goal](examples/agent-workflow/graph-goal.yaml) | 从固定材料选择原文证据，程序验证后渲染离线页面 | Pi + 可信程序 |
| [Graph Builder](examples/agent-workflow/GRAPH-BUILDER.md) | 将已确认的需求转换为 Graph /2 YAML | Pi Worker + Pi Checker |
| [两阶段协议](examples/agent-workflow/graph-two-stage.yaml) | 确定性的依赖与执行协议示例 | Script 测试夹具 |

从 [示例运行指南](examples/agent-workflow/README.md) 开始。清洗与 Goal 需要显式部署匹配的程序 Checker；默认 `/agentgenome` 服务不会自动加载示例 Python 文件。Graph Builder 已有结构校验，不承诺模型一定生成可发布的 SOP；生成后仍须通过真实 API 校验。

## 开发与测试

```bash
.venv/bin/python -m pip install pytest
.venv/bin/python -m pytest tests/control_plane -q
```

测试覆盖协议、权限、版本、持久化、父子任务、返工、预算、服务通信与 trace。Node.js 用于 JavaScript 探针；实际 Pi RPC 测试需要本机 Pi，缺失时相应测试跳过。测试使用隔离目录和受控执行，不调用付费模型。业务模型的效果需要另行运行示例、检查接受的产物。

```text
node_runtime/control_plane/   唯一运行实现
  backends/                  Pi / Script 适配器与 Pi 扩展
examples/agent-workflow/      可公开的 SOP、输入与程序 Checker
tests/control_plane/         当前实现的回归测试与探针
docs/                       使用与架构文档
pyproject.toml               Python 包、依赖与命令入口
uv.lock                     已解析的依赖版本
```

贡献前阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。修改协议或执行行为时同步更新相关测试与示例；业务验收逻辑应留在 Task / Checker 中。

## 当前边界

当前版本为 `0.1.0`，使用 Graph /2 与 SQLite schema /3，面向本机常驻服务。Pi 与可信 Script 是现有后端；没有承诺分布式集群、任意 CLI 适配或 Agent 原生后台 subagent 管理。

恢复的是持久任务状态与后端支持的会话，不能保证重放任意外部副作用，也不能保证模型重复输出完全相同。语义 Checker 的接受代表声明验收协议的执行结果，仍应按业务需要独立核验交付物。

## 引用 / Citation

如果在研究中使用 AgentGenome，可以引用本仓库：

```bibtex
@software{agentgenome2026,
  author = {GinkoAstra},
  title = {AgentGenome: Verification-Pinned Executable Task Graphs for LLM Agents},
  year = {2026},
  url = {https://github.com/GinkoAstra/AgentGenome}
}
```

## License

[MIT](LICENSE)。现有版权声明保留在许可证文件中。
