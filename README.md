<div align="center">

# AgentGenome

**把确认过的 SOP，交给可追踪、可验收的 Agent 执行。**

A persistent graph runtime for agent workflows.

`Python 3.11+` · `Graph /2` · `Pi + MCP` · `MIT`

[项目目标](#项目目标) · [要解决的问题](#要解决的问题) · [开始使用](#开始使用) · [系统结构](#系统结构) · [示例](#示例) · [开发与测试](#开发与测试)

</div>

---

## 项目目标

**AgentGenome 是一个持久化的 Agent 工作流运行时：把已经确认的工作方法，变成能够反复执行、检查结果和继续推进的 SOP。**

一个完整 Agent 可以连续调用模型与工具，但要长期执行同类业务，还需要明确：本次输入是什么、哪些步骤依赖哪些结果、谁决定任务完成，以及中断后从哪里继续。AgentGenome 将这些规则放入版本化的流程定义，并由独立服务保存执行状态。

项目希望让用户通过对话创建和修改流程，确认输入、输出与验收条件后，将执行交给系统。正常执行中的分工、结果提交、验收和返工按照声明的协议推进，减少人反复转述上下文、检查进度和手动串联 Agent 的工作。需要补充信息或超出既定约束时，系统应明确暴露状态，而不是无限续跑。

已有 Agent／Skills 中明确的工作方法可以作为 SOP 的来源；接入时仍需要声明任务契约、工具权限和验收方式，不能把任意提示词直接视为已验证流程。

## 要解决的问题

| 执行同类任务时遇到的问题 | AgentGenome 的处理方式 |
| --- | --- |
| 每次都要重新说明流程、分配任务和传递材料 | 用版本化 Graph 定义流程，按本次输入创建独立 Run |
| Agent 回答“完成”，但结果是否合格不明确 | 区分候选提交与接受；由父任务或声明的 Checker 检查结果 |
| 多个 Agent 的依赖、并行和返工靠对话人工协调 | Scheduler 根据持久任务关系和结构化结果推进，Controller 协调实际执行 |
| 关闭对话窗口后，不知道任务进行到哪里 | 常驻 API 服务保存状态；客户端重新连接，通过 trace 查看任务、候选与评审 |
| 修改流程后，难以区分旧结果依据哪套规则产生 | 固定本次使用的契约与输入版本，记录接受的输出及评审关系 |
| 更换执行 Agent 时，要重写流程控制逻辑 | Backend 适配执行协议；API、任务关系与验收语义保持统一 |

例如，一份数据清洗 SOP 可以声明“读取输入 → 规范化与去重 → 验收 → 生成统计”。换一批输入时再次运行同一版本；清洗结果未通过检查时携带反馈返工，后续统计只使用被接受的结果。**清洗规则属于任务与 Checker，通用运行时只负责按协议推进。**

## 什么时候使用

适合需要反复执行、有明确交付物、需要追踪验收和恢复过程的业务：数据整理、按固定规则生成报告、证据收集与核对，以及已有 SOP Agent 的组合运行。

一次性搜索、临时编码分工和并行评审，通常直接使用 Agent 原生 subagent 即可。当工作需要跨多次运行保留契约、状态和验收记录时，再引入这一层运行时。

流程可以预先定义，也可以在允许的规则内拆分子任务。固定的是任务契约、输入版本和已接受的输出，不要求预先固定全部子任务数量。框架能记录和执行验收协议；验收规则是否充分、业务结论是否正确，仍取决于具体任务设计与证据。

当前主要对话入口是普通 Pi 中的 `/agentgenome`。Teacher 是操作流程的普通 AI loop，通过 MCP 调用同一 API；执行端可以采用不同 Backend。**统一安装和自动配置仍在完善中**，下面给出当前实际可用的源码安装方式。

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

## 执行后端与当前支持范围

**对话入口与执行后端可以不同。** Pi 中的 Teacher 负责与用户讨论流程，通过 MCP 操作服务；Backend 负责运行承担具体任务的 Agent。接入 Codex 或 Qoder 执行后端，不代表已经为它们提供了与 Pi 相同的交互插件。

| 后端 | 当前验证范围 |
| --- | --- |
| Pi | 主要对话入口与 Agent 执行后端，已有真实业务示例 |
| Script | 显式加载的可信程序，可承担确定性任务或 Checker |
| Codex CLI | 真实模型完成候选提交、退回、原会话继续、接受流程 |
| Qoder CLI | 真实模型完成同一验收流程 |
| Claude Code | 真实 CLI＋受控模型协议验证通过；真实上游模型验收待完成 |
| Kimi | 尚未实现 |

CLI 适配复用同一 API、调度器和验收协议。当前只开放任务授权的系统工具及工作区读写，不开放任意原生 shell、subagent 或后台任务；支持当前原生会话续接，不提供历史会话快照或外部副作用重放。CLI 的模型请求次数无法可靠实施硬上限，因此相应预算明确不支持。

## 开始使用

### 1. 安装并接入 Pi

下面是当前的源码安装流程，只需安装运行依赖，使用前不需要安装 pytest 或运行开发测试。统一安装与自动配置仍是后续工作。

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

### 4. 可选：部署 CLI 执行后端

已完成 Python 包安装，并为所选 CLI 配置好认证后，可以启动单独的服务。以下示例使用 Codex；模型名称及配置目录需替换为实际值：

```bash
.venv/bin/agentgenome serve \
  --data-dir "$HOME/.local/state/agentgenome/codex" \
  --backend codex \
  --model YOUR_MODEL \
  --config-dir "$HOME/.codex"
```

`serve` 在前台运行。将 `--backend` 换成 `qoder` 或 `claude` 时，也应更换对应的模型与配置目录；可用 `--executable` 指定 CLI 路径。Graph 的 Agent 定义通过 `backend_ref` 引用服务注册的后端。这个独立服务不会自动替换 Pi 默认连接的服务。

CLI 后端不使用 Pi 的 `--provider`。为隔离工具权限，适配器不会完整继承 CLI 的 hooks、插件与 MCP 配置；Codex 的用户 `config.toml` 也被排除，因此其中的自定义 provider 配置目前不能直接复用。模型连接配置复用与安装简化仍需继续完善。

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
│                         Pi / CLI / Script Backend       │
└──────────┬──────────────────────────┬────────────────────┘
           ↕ 服务内部读写              ↓ 启动 / 观察 / 停止
    [SQLite + artifacts]       [Pi / CLI Agent 子进程]
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
  backends/                  Pi / CLI / Script 适配器与 Pi 扩展
examples/agent-workflow/      可公开的 SOP、输入与程序 Checker
tests/control_plane/         当前实现的回归测试与探针
docs/                       使用与架构文档
pyproject.toml               Python 包、依赖与命令入口
uv.lock                     已解析的依赖版本
```

贡献前阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。修改协议或执行行为时同步更新相关测试与示例；业务验收逻辑应留在 Task / Checker 中。

## 当前边界

当前版本为 `0.1.0`，使用 Graph /2 与 SQLite schema /3，面向本机常驻服务。现有后端包括 Pi、可信 Script，以及 Codex／Qoder／Claude CLI 适配器，各自验收范围见上文。尚不支持 Kimi，不承诺分布式集群、任意 CLI 适配或 Agent 原生后台 subagent 管理。

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
