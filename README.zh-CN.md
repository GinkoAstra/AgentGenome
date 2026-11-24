<div align="center">

# AgentGenome

**把确认过的 SOP，交给可追踪、可验收的 Agent 执行。**

A persistent graph runtime for agent workflows.

`Rust 0.2.1-dev（本地构建）` · `Graph /2` · `Pi + MCP` · `MIT`

[English](README.md) · [文档导航](docs/index.md) · [交互导览](https://ginkoastra.github.io/AgentGenome/) · [快速开始](docs/quickstart.md) · [示例总览](examples/README.md)

</div>

---

> **候选提交 ≠ 验收完成。** Worker 只提交候选产物；父任务或声明的 Checker 评审后，Scheduler 持久化接受状态，下游任务才能读取该输出。

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
| 外部程序（`script`） | 由部署者通过 `--program-config` 显式加载，作为独立进程承担确定性任务或 Checker |
| Codex CLI | 真实模型完成候选提交、退回、原会话继续、接受流程 |
| Qoder CLI | 真实模型完成同一验收流程 |
| Claude Code | 真实 CLI＋受控模型协议验证通过；真实上游模型验收待完成 |
| Kimi | 尚未实现 |

Adapter 直接启动原生 Agent，保留 CLI 自己的工具、Skills、配置和权限设置，并注入独立的 `agentgenome_worker` MCP；Pi 通过扩展注入同一控制面工具。Graph 的身份、任务推进和候选验收仍由控制面管理。`backend_tools` 只控制额外注入的桥接工具，不限制原生 Bash、文件工具或 subagent；原生权限由 CLI 配置决定，Adapter 不默认跳过审批。无交互 Worker 所需权限应预先在 CLI 配置中明确授权，详见 [Worker 工具与执行边界](docs/worker-commands.md)。

支持原生会话续接；本地停止只确认所托管的进程组，无法保证远端或脱离进程组的作业停止。原生工具事件可进入 Trace，但不声称完整枚举 Agent 内部活动，也不提供外部副作用重放。Codex／Qoder／Claude 的模型请求与全部工具调用次数无法可靠实施硬上限，明确填写这些硬预算时会拒绝启动；可使用流程轮次、并发量和执行期限约束。

## 交互导览

无需安装或模型调用即可在浏览器中理解完整验收流程：

- **正常接受**：Worker 提交候选，独立程序 Checker 验收清洗与统计两个阶段。
- **退回修正**：Checker 发现审计计数错误并返回反馈，Worker 第二轮修正后再被接受。
- 支持播放、暂停、逐步、倍速和历史回看；可查看 Graph 节点、Task 契约、接受产物与架构分层。

**[打开交互导览](https://ginkoastra.github.io/AgentGenome/)**，或在仓库根目录本地预览：

```bash
python3 -m http.server 8000 --directory docs/showcase
```

然后访问 `http://localhost:8000`。展示站使用脚本化本地数据，不连接真实运行时，也不替代真实 Run 的验收回执和产物。

## 开始使用

安装和服务部署各做一次；日常使用从打开客户端并激活开始。全新环境按第 1 → 2 → 3 节执行；如果本机已经有 AgentGenome 服务在运行，可以跳过第 2 节。

### 1. 一次性安装原生入口

这一步只安装宿主入口和受校验的 Core，**不启动服务**。运行安装器需要 **Node.js 20+**，以及已经安装并配置好自身模型的宿主 CLI（Pi、Codex、Qoder 或 Claude Code）。预编译 Core 本身不需要 Python、uv、Rust 或编译器，安装器也不修改 shell 配置文件。

当前 `0.2.1-dev` Core 与 `release-manifest.json` 来自本仓库的本地构建，只验证了 **Linux x86_64（glibc）**。npm 包没有发布到 npm registry，仓库也**没有公开的 GitHub release**，因此不要依赖默认下载地址。在仓库根目录显式传入已构建的 Core：

```bash
node bin/setup.mjs --client pi \
  --artifact "$PWD/rust/target/release/agentgenome"
```

`--artifact` 必须指向预编译 Core 的绝对路径，而且其目标、字节大小、SHA256 与 `--version` 必须与随包 manifest 完全一致；只有匹配的预编译 Core 才能直接通过这条路径安装。需要隔离服务数据目录时追加 `--data-dir PATH`，需要隔离宿主配置时追加 `--config-root PATH`；把 `--client pi` 换成 `codex`、`qoder` 或 `claude`。安装器读取随包的 manifest，核对目标、字节大小、SHA256 与 Core 的 `--version`，再把 Core 原子安装到 `$XDG_DATA_HOME/agentgenome/runtime/<version>/<target>/agentgenome`（未设置时回退到 `~/.local/share`），最后写入该宿主的原生入口。运行时不依赖 npm 缓存或源码目录，删除缓存后入口仍可使用。

如果没有现成二进制，可以先从源码构建。源码构建要求 Rust 工具链；它与上述 Node 一次性安装是两条独立路线。最简单的源码路线是由构建产物直接写入原生入口，并保留这个可执行文件：

```bash
cargo build --release --locked --manifest-path rust/Cargo.toml
"$PWD/rust/target/release/agentgenome" client install --client pi
```

需要隔离服务数据目录或宿主配置时，分别追加 `--data-dir PATH` 或 `--config-root PATH`。该入口绑定构建产物的绝对路径，移动或删除产物会使入口失效；需要一个持久位置时，先把产物复制到自己的稳定目录，再用该绝对路径执行命令。

**不要**把任意新构建直接作为 `--artifact` 传给 `bin/setup.mjs`：安装器使用随包 manifest 的固定目标、字节大小、SHA256 和版本，只有与该 manifest 完全匹配的预编译 Core 才会通过。若要准备自己的发布包或镜像，可在构建后生成匹配的元数据：

```bash
node tools/prepare-release.mjs \
  --binary "$PWD/rust/target/release/agentgenome" \
  --output-dir "$PWD/dist/release-candidate" \
  --manifest "$PWD/dist/release-candidate/release-manifest.json"
```

该命令写出新的 artifact 和 manifest，不下载、不发布、不安装、不启动服务，属于开发者/打包准备，不是普通安装的绕过。安装器只读取随包 manifest，因此新 manifest 需要进入对应的安装包或镜像才会生效；不要为了适配任意构建而修改仓库已签入的 `release-manifest.json`。

只查看安装计划而不下载、写入或调用 Core：

```bash
node bin/setup.mjs --client pi \
  --artifact "$PWD/rust/target/release/agentgenome" --dry-run
```

更新时重新运行同一条安装命令：内容匹配的已验证 Core 会被复用，随后只更新原生入口。安装成功后，安装器会打印 `Runtime` 的绝对路径和 `Start the service with: '<Runtime>' start` 命令；如果安装时传入了 `--data-dir`，该命令会带上同一路径。安装器不会把 Core 加到 `PATH`，后续命令直接使用打印出的绝对路径。`--release-base-url` 仅用于本地镜像或传输验证，loopback HTTP 同样只用于本地测试。

### 2. 一次性部署服务（选择一种后端）

安装器、`/agentgenome` 和 `status` 都不会自动启动服务。另开终端，选择**一种**执行后端并显式启动；如果本机已有 AgentGenome 服务在运行，跳过本节。先把下面第一行的 `RUNTIME` 换成安装器打印的绝对路径（未设置 `XDG_DATA_HOME` 时通常为 `$HOME/.local/share/agentgenome/runtime/0.2.1-dev/x86_64-unknown-linux-gnu/agentgenome`），路径含空格时保留引号：

**Pi 后端**（主要对话入口；以下参数只用于服务端的 Worker 与语义 Checker）：

```bash
RUNTIME=/absolute/path/printed/by/agentgenome-setup
"$RUNTIME" start --backend pi \
  --provider YOUR_PROVIDER --model YOUR_MODEL
```

**Codex / Qoder / Claude 后端**（使用该 CLI 自己的配置与默认模型；`--provider` 会被拒绝）：

```bash
"$RUNTIME" start --backend codex
"$RUNTIME" start --backend qoder
"$RUNTIME" start --backend claude
```

需要覆盖模型或指定隔离目录时，可追加 `--model`、`--data-dir`、`--executable`；CLI 后端还接受 `--config-dir`。如果安装时使用了 `--data-dir`，启动时也必须带上同一个 `--data-dir`（安装器打印的命令已经包含），否则客户端入口与服务会指向不同目录。这些是服务端部署参数，不改变客户端会话使用的模型。CLI 后端直接启动原生 CLI，保留其自身的工具、Skills、配置与权限设置（包括 Codex 的用户 `config.toml`）；Adapter 只注入独立的 `agentgenome_worker` MCP 桥接，不默认跳过审批，无交互 Worker 所需权限应在 CLI 配置中预先授权。外部程序 Checker 通过 `--program-config` 显式部署，格式见 [示例说明](examples/agent-workflow/README.md)。

常用的运行期命令都使用同一个绝对 Core 路径和同一个 `--data-dir`：

```bash
"$RUNTIME" serve --backend pi --provider YOUR_PROVIDER --model YOUR_MODEL  # 前台服务
"$RUNTIME" status                          # 服务生命周期与进程身份
"$RUNTIME" stop                            # 核验身份后停止服务
"$RUNTIME" ctl status                      # 经服务 API 查询 Run 报告
"$RUNTIME" ctl status RUN_ID               # 查询指定 Run
"$RUNTIME" ctl trace RUN_ID                # 查看任务、候选、评审与产物
```

`serve` 在当前进程拥有数据库和网络生命周期；`start` 启动受管理的后台服务并等待就绪；`stop` 只停止所管理的服务，不等同于暂停或取消业务。诊断命令始终使用远程 API，不依赖本地数据库回退。

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

不开 Pi 时，也可以用只读客户端观察同一个服务：`"$RUNTIME" trace --run RUN_ID --once` 输出纯文本任务树，`"$RUNTIME" trace --web` 打印浏览器地址与凭据文件路径。二者都不启动第二个服务、不改任务状态。用法与安全边界见 [Trace 终端与浏览器视图](docs/trace.md)。

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
│                     Pi / CLI / external program Backend │
└──────────┬──────────────────────────┬────────────────────┘
           ↕ 服务内部读写              ↓ 启动 / 观察 / 停止
    [SQLite + artifacts]       [Pi / CLI Agent 子进程]
     状态 / 事件 / 内容          多轮模型请求与工具调用
                                └─ HTTP 回填候选与事实
                                [外部程序进程：JSONL / 只读授权的 Artifact]
```

`PiBackend` 启动 Pi Agent 子进程；`CliBackend` 适配 Codex、Qoder 或 Claude 的原生会话；`ProgramBackend` 启动由 `--program-config` 明确部署的外部程序，而不是在服务进程内导入 Python。三者都通过 `AgentBackend` 接口由 Controller 调用，不拥有自己的调度器或数据库。请求循环与控制循环分开运行。

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
| [两阶段协议](examples/agent-workflow/graph-two-stage.yaml) | 确定性的依赖与执行协议示例 | 外部程序测试夹具（非生产） |
| [人工进入确认](examples/agent-workflow/graph-human-gate.yaml) | 学习 SOP 在首次进入练习前等待用户明确确认 | Pi + publisher 确认 |
| [宿主命令探针](examples/agent-workflow/graph-command-probe.yaml) | Qoder Worker 显式授权 `run_command`，只读 `ss`/`lsof` 后由独立 Checker 复核 | Qoder Worker + Qoder Checker |

从 [示例总览](examples/README.md) 和 [示例运行指南](examples/agent-workflow/README.md) 开始。清洗与 Goal 需要显式部署匹配的程序 Checker；默认 `/agentgenome` 服务不会自动加载示例 Python 文件。Graph Builder 已有结构校验，不承诺模型一定生成可发布的 SOP；生成后仍须通过真实 API 校验。

## 开发与测试

```bash
cargo test --manifest-path rust/Cargo.toml
node --test tests/install/*.test.mjs
```

Rust 测试覆盖定义校验、权限、版本、SQLite 持久化、父子任务、返工、预算、服务通信、生命周期、Pi／CLI／外部程序协议与 trace。Node 测试覆盖一次性安装器的参数、manifest 校验、下载／大小／SHA256／版本校验、loopback HTTP、退出码传播、dry-run 零副作用、旧二进制保留与打包内容。测试使用隔离目录和受控执行，不调用付费模型；真实业务效果仍要运行示例并核对接受的产物。

完整 Rust 测试还需要额外的开发工具，缺失时会明确失败而不是静默跳过：

- 系统 `python3`：`r05d_program_protocol` 与 `r05d_business` 使用 `/usr/bin/python3` 运行 Python 标准库测试夹具。
- Node.js：Pi JavaScript 资产测试调用 `node`；安装器测试也需要 Node。
- 真实 Python MCP SDK：`r04c` 互操作测试需要单独准备的 `.venv` 和 `rust/target/debug/examples/r04c_bridge_probe` 探针。

这些只是测试工具，预编译 Core 的运行不需要 Python、Node、uv、Rust 或编译器。准备 MCP 测试环境并运行完整 Rust 测试：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install mcp
cargo build --manifest-path rust/Cargo.toml --example r04c_bridge_probe
cargo test --manifest-path rust/Cargo.toml
```

```text
rust/src/main.rs              CLI 入口与内部 worker 桥接
rust/src/public_cli/          serve/start/status/stop/client/teacher/ctl 参数与生命周期
rust/src/runtime.rs           后端选择、--program-config 加载与 Host 组装
rust/src/server.rs            ApiServer：HTTP / MCP / SSE 与独立控制循环
rust/src/http_api.rs          公开操作名与统一 dispatch
rust/src/api/                 唯一权威状态、权限、事务与版本
rust/src/host.rs              Api / Scheduler / Controller 的组合根
rust/src/scheduler.rs         依赖、拆分、就绪、预算与执行意图
rust/src/controller.rs        已提交执行绑定的启动、投递、观察与停止
rust/src/execution.rs         WorkerSpec、委派与预算组合
rust/src/execution/           共享的后端能力与工具授予校验
rust/src/input_refs.rs        只读输入视图与输入绑定解析
rust/src/backend.rs           AgentBackend 协议
rust/src/backends/            Pi / CLI / 外部程序适配器
rust/src/backends/spawn.rs    子进程启动、进程组终止与 Worker MCP 桥
rust/src/backends/receipts.rs 工具回执解析
rust/src/trace.rs             状态投影、执行详情与请求诊断
rust/src/teacher.rs           Teacher 准备、MCP 配置与普通 Pi 会话
rust/assets/pi/               Pi 扩展与 Worker / Trace JavaScript
rust/assets/teacher/          SOP 协议、模板与激活说明
examples/agent-workflow/      公开的 Graph、输入与示例说明
examples/programs/            可选的 Python 外部程序（非 Core 依赖）
tests/install/                一次性安装器的聚焦测试
Git 历史 07fdb94             旧 Python 实现，仅供查阅，不是运行时回退
package.json                  npm bin、Node 版本与打包文件白名单
release-manifest.json         本地 0.2.1-dev 构建的目标、大小与 SHA256
```

贡献前阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。修改协议或执行行为时同步更新相关测试与示例；业务验收逻辑应留在 Task / Checker 中。

## 当前边界

当前 Core 为 `0.2.1-dev`，使用 Graph /2 与 SQLite schema /3，面向本机常驻服务；二进制与 manifest 只在本地构建，未发布到 npm 或公开 release，当前验证目标是 Linux x86_64 glibc。现有后端包括 Pi、部署者显式加载的外部程序，以及 Codex／Qoder／Claude CLI 适配器，各自验收范围见上文。尚不支持 Kimi，不承诺分布式集群、任意 CLI 适配或 Agent 原生后台 subagent 管理。

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
