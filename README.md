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

当前主要对话入口是普通 Pi 中的 `/agentgenome`。Teacher 是操作流程的普通 AI loop，通过 MCP 调用同一 API；执行端可以采用不同 Backend。统一安装器现在也能为 Codex、Qoder 和 Claude Code 写入原生 MCP 入口与技能，详细步骤与各宿主的验证范围见 [CLI 接入说明](docs/client-access.md)。

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

**对话入口与执行后端可以不同。** Pi 中的 Teacher 负责与用户讨论流程，通过 MCP 操作服务；Backend 负责运行承担具体任务的 Agent。Codex、Qoder 和 Claude Code 现在也有统一安装器生成的原生 MCP/技能入口，但它们没有 Pi 的终端面板插件；接入它们的执行后端与启用它们的对话入口是两件事。

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

安装和服务部署各做一次；日常使用从打开客户端并激活开始。全新环境按第 1 → 2 → 3 节执行；如果本机已经有 AgentGenome 服务在运行，可以跳过第 2 节。

### 1. 一次性安装

需要 **Node.js 20+**，以及已经安装并配置好自身模型的宿主 CLI（Pi、Codex、Qoder 或 Claude Code）。安装器会复用本机的 [uv](https://docs.astral.sh/uv/)；只有 uv 不存在时才需要 `curl` 下载固定版本的 uv。它不会修改 shell 配置文件。

npm 包目前**没有发布到 npm registry**。在仓库根目录用本地包运行：

```bash
npm exec --package=. -- agentgenome-setup --client pi
```

这条命令准备隔离的 uv tool 环境，以非可编辑方式安装 Python 运行时，再把所选客户端的原生入口交给同一个共享安装器。它**不启动服务**，也不配置 Worker／Checker 模型或凭据。入口绑定 uv tool 环境的持久解释器，不依赖 npm 缓存或源码目录；删除 npm 缓存后客户端仍可启动。

安装成功后，安装器会打印 `Native entry` 和 `Console` 的绝对路径。如果 `agentgenome` 不在 `PATH`，后续命令直接使用这个 `Console` 路径；不需要修改 shell 配置文件。

可以直接从 GitHub 分支 `feat/unified-client-access` 运行同一个安装器（仍然不是 registry 发布）：

```bash
npm exec --yes \
  --package=https://codeload.github.com/GinkoAstra/AgentGenome/tar.gz/refs/heads/feat/unified-client-access \
  -- agentgenome-setup --client pi
```

把 `--client pi` 换成 `codex`、`qoder` 或 `claude`。需要隔离安装时追加 `--config-root PATH`、`--data-dir PATH`；只查看计划而不下载或写入：

```bash
npm exec --package=. -- agentgenome-setup --client pi --dry-run
```

更新时重新运行同一条安装命令（内部使用 `--reinstall`），不需要 PyPI 发布。

### 2. 一次性部署服务（选择一种后端）

客户端只连接已经运行的本机服务，不会自动启动。另开终端，选择**一种**执行后端；如果本机已有 AgentGenome 服务在运行，跳过本节。

**Pi 后端**（主要对话入口；以下参数只用于服务端的 Worker 与语义 Checker）：

```bash
agentgenome start --backend pi \
  --provider YOUR_PROVIDER --model YOUR_MODEL
```

**Codex / Qoder / Claude 后端**（使用该 CLI 自己的配置与默认模型；`--provider` 会被拒绝）：

```bash
agentgenome start --backend codex
agentgenome start --backend qoder
agentgenome start --backend claude
```

需要覆盖模型或指定隔离目录时，可追加 `--model`、`--data-dir`、`--executable`；CLI 后端还接受 `--config-dir`。这些是服务端部署参数，不改变客户端会话使用的模型。为隔离工具权限，CLI 适配器不会完整继承 CLI 的 hooks、插件与 MCP 配置；Codex 的用户 `config.toml` 也被排除，因此其中的自定义 provider 配置目前不能直接复用。

### 3. 打开客户端并激活

日常使用不需要重复安装或部署：打开客户端、激活即可。安装完成后必须新开一个客户端会话，让原生配置和技能加载；不能在已打开的窗口中热装：

| 客户端 | 激活方式 | 说明 |
| --- | --- | --- |
| Pi | 新 Pi 窗口输入 `/agentgenome` | 使用 Pi 原生命令，不额外安装重复技能 |
| Codex | 输入 `$agentgenome` 或 `/skills` | Codex 中不使用 `/agentgenome` 斜杠命令 |
| Qoder | 新会话输入 `/agentgenome` | 使用原生技能与 MCP 入口 |
| Claude Code | 新会话输入 `/agentgenome` | 正常启动，不使用 `--bare` |

安装只写入各宿主的原生入口；真实调用验证与边界见 [CLI 接入说明](docs/client-access.md)。

### 4. 观察与继续

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
uv run --with pytest python -m pytest tests/control_plane -q
node --test tests/install/setup.test.mjs
```

测试覆盖协议、权限、版本、持久化、父子任务、返工、预算、服务通信与 trace。Node 测试覆盖 npm 一次性安装器的参数、uv 引导、退出码、持久入口（不向共享安装器传递解释器）与打包内容。Node.js 也用于 JavaScript 探针；实际 Pi RPC 测试需要本机 Pi，缺失时相应测试跳过。测试使用隔离目录和受控执行，不调用付费模型。业务模型的效果需要另行运行示例、检查接受的产物。

```text
bin/setup.mjs                 npm 一次性安装器（uv + 原生入口）
node_runtime/control_plane/   唯一运行实现
  backends/                  Pi / CLI / Script 适配器与 Pi 扩展
examples/agent-workflow/      可公开的 SOP、输入与程序 Checker
tests/control_plane/         当前实现的回归测试与探针
tests/install/               npm 安装器的聚焦测试
docs/                       使用与架构文档
pyproject.toml               Python 包、依赖与命令入口
package.json                 npm bin、Node 版本与打包文件白名单
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
