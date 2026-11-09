# 在普通 CLI 中接入 AgentGenome

本文说明如何把 AgentGenome 作为**对话客户端**接入 Pi、Codex、Qoder 和 Claude Code。统一入口是一次性 npm 安装器：它准备持久运行环境，然后委托同一个共享安装器写入一个原生 MCP 入口，以及（除 Pi 外的）技能说明。它不创建服务、不修改模型或凭据、不代替宿主自己的模型会话。

## 一次性安装

前提：

1. **Node.js 20+**，以及已经安装并配置好自身模型的宿主 CLI。
2. 本机有 [uv](https://docs.astral.sh/uv/) 时直接复用；uv 缺失时需要 `curl`，安装器会下载固定版本的官方 uv 到 `$XDG_DATA_HOME/agentgenome/bootstrap/uv`（未设置时为 `~/.local/share/...`），不修改 shell 配置文件。
3. 服务必须先单独部署并运行（见下文）；宿主只通过 MCP 连接已经运行的服务，不会自动 start。

npm 包目前**没有发布到 npm registry**。在仓库根目录执行：

```bash
npm exec --package=. -- agentgenome-setup --client pi
```

可以直接从 GitHub 分支 `feat/unified-client-access` 运行同一个安装器：

```bash
npm exec --yes \
  --package=https://codeload.github.com/GinkoAstra/AgentGenome/tar.gz/refs/heads/feat/unified-client-access \
  -- agentgenome-setup --client pi
```

把 `--client pi` 换成 `codex`、`qoder` 或 `claude`，并按需追加：

```text
--config-root PATH   显式隔离的原生配置根
--data-dir PATH      写入入口的服务数据目录
--dry-run            只显示计划；不下载、不写入、不安装、不启动服务
--help               用法与退出码
```

- 安装器以非可编辑方式把 Python 包安装进 uv tool 环境，并定位该环境的持久命令；客户端入口绑定该环境的解释器，不依赖 npm 缓存或源码目录。安装成功会打印 `Console: /绝对路径/agentgenome`；如果 `agentgenome` 不在 `PATH`，后续命令直接使用这个绝对路径。
- 安装记录保存在 `<config-root>/.agentgenome/<client-kind>-install-record.json`，只包含自有入口/技能的摘要、解释器、数据目录和已解析的 activation root，不记录用户的其他配置或凭据。
- 更新时重新运行安装命令（内部使用 `--reinstall`）；不需要 PyPI 发布。
- 安装后必须**重新打开宿主会话**，使原生配置/技能加载；不支持在已打开的窗口中热装。

运行环境安装完成后，仍可用共享安装器做更细的预览或隔离管理：

```bash
agentgenome client plan|install|uninstall \
  --client pi|codex|qoder|claude \
  [--config-root PATH] [--activation-root PATH] \
  [--data-dir PATH] [--python PATH] [--dry-run]
```

- `--client-kind` 仍作为 `--client` 的兼容别名，参数、JSON 输出与退出码相同。
- `plan` 永远零写入；`install`/`uninstall` 也接受 `--dry-run`。
- 冲突（文件已存在、内容不同、被用户改过或属于其他安装记录）返回退出码 `3`，且不写入任何文件；非法参数返回 `2`。

## 先部署服务（选择一种后端）

安装器不会启动服务。首次使用前，用安装输出中的 `agentgenome`（或 `Console` 绝对路径）选择一种后端；如果本机已有服务在运行，跳过本节：

```bash
# Pi：provider/model 只用于服务端 Worker 与语义 Checker
agentgenome start --backend pi \
  --provider YOUR_PROVIDER --model YOUR_MODEL

# Codex / Qoder / Claude：使用该 CLI 自己的配置与默认模型
agentgenome start --backend codex
agentgenome start --backend qoder
agentgenome start --backend claude
```

需要覆盖模型或指定隔离目录时，可追加 `--model`、`--data-dir`、`--executable`；CLI 后端还接受 `--config-dir`。`--provider` 只对 Pi 后端有效。

## Pi

Pi 已有原生 `/agentgenome` 命令和内嵌协议参考，因此不安装重复技能。

```bash
npm exec --package=. -- agentgenome-setup --client pi --data-dir PATH
```

- 原生入口：`<agent-dir>/extensions/agentgenome.js`；agent-dir 默认 `~/.pi/agent`，可用 `PI_CODING_AGENT_DIR` 覆盖。隔离安装时追加非空的 `--config-root PATH`。
- 启用：在新 Pi 窗口中输入 `/agentgenome`；之后 `/agentgenome trace`、`/agentgenome status`、`/agentgenome off`。
- 重新加载：必须新开 Pi 会话/进程。
- 已验证：真实 Pi 1.5.0 RPC 进程加载扩展，`/agentgenome` 触发真实 MCP `tools/list` 和 `register_client`，注册 `get_service_info`、`list_definitions`、`read_trace` 等原生工具；受控模型只提供工具请求，实际结果来自安装后的 MCP 桥和真实 run。`/agentgenome off` 后工具停用，服务与 run 继续运行。
- 限制：trace 面板只在交互式 TUI 中挂载，RPC 验收改为命令/工具观察；模型会话的模型与 provider 保持用户原样。

## Codex CLI

```bash
# 默认安装；隔离时才传入非空的显式 --config-root
npm exec --package=. -- agentgenome-setup --client codex --data-dir PATH
```

- 原生入口：`<CODEX_HOME>/config.toml` 的 `mcp_servers.agentgenome`；技能写入 `<CODEX_HOME>/skills/agentgenome/SKILL.md` 与 `references/graph-sop.md`。`CODEX_HOME` 默认 `~/.codex`。
- 重新加载：新开 Codex 会话；工具/技能不会注入已打开的会话。
- 原生触发：`$agentgenome` 或 `/skills`。安装说明中写的是实测到的触发形式，不把 `/agentgenome` 当作 Codex 命令。
- 已验证：官方 `codex app-server --stdio` 的 `skills/list` 发现技能，`mcpServerStatus/list` 发现 `agentgenome` 工具，`mcpServer/tool/call` 真实执行 `get_service_info`、`list_definitions`、`publish_definition`、`create_run`、`read_trace`，并完成真实 run；两个 cwd 返回同一 `service_id`，错误输入可见，关闭 app-server 后服务仍存活。

## Qoder CLI

```bash
# 默认安装
npm exec --package=. -- agentgenome-setup --client qoder --data-dir PATH

# 隔离安装：显式非空根（QODER_CONFIG_DIR 或 --config-root，二者在 1.1.85 上等价）
npm exec --package=. -- agentgenome-setup --client qoder \
  --config-root PATH --data-dir PATH
```

- 原生入口：`<config-root>/settings.json` 的 `mcpServers.agentgenome`；技能写入 `<config-root>/skills/agentgenome/`。
- 原生根：`QODER_CONFIG_DIR` 或每次运行的 `--config-dir`，默认 `~/.qoder`。二者在 qodercli 1.1.85 上均已用隔离根实测。
- 激活：新开 qodercli 会话，输入 `/agentgenome`。
- 已验证（默认隔离矩阵）：隔离 `--config-dir`/`QODER_CONFIG_DIR` 下，`qodercli mcp list`、`mcp get agentgenome` 和 `skills list` 发现安装的入口与技能。
- 已验证：使用现有登录的真实 Qoder CLI 调用了 `get_service_info`、`list_definitions` 和 `read_trace`，读取了 Codex 创建的同一服务、定义版本和已完成运行。
- 验证范围：默认隔离测试覆盖安装和技能发现；真实调用另行通过集成验证。额外使用项目隔离技能、`--setting-sources project,local` 与严格 MCP 配置验证了 `/agentgenome`：CLI 报告技能已激活，随后三项真实只读工具调用返回一致的 service/run 身份。正常使用无需这些测试隔离参数；若自行限制 setting sources，应包含技能所在的来源。验证没有安装或修改用户全局配置，也没有读取或复制凭据。

## Claude Code

```bash
# 默认安装
npm exec --package=. -- agentgenome-setup --client claude --data-dir PATH

# 隔离安装：显式非空根
npm exec --package=. -- agentgenome-setup --client claude \
  --config-root PATH --data-dir PATH
```

- 原生入口：`<config-root>/.claude.json` 的 `mcpServers.agentgenome`；技能写入 `<config-root>/skills/agentgenome/`。
- 路径语义：显式 `--config-root` 等价于启动时 `CLAUDE_CONFIG_DIR=<root>`，因此技能根是 `<root>/skills`；未显式覆盖时默认 MCP 文件为 `~/.claude.json`，技能根为 `~/.claude/skills`。安装器进程自身的 `CLAUDE_CONFIG_DIR` 不决定显式 root 的语义。
- 重新加载：新开 Claude 会话，正常启动即可加载用户技能与 MCP 配置；不要用 `--bare`，否则用户技能来源不会被加载。隔离测试使用 `--setting-sources user` 是为了排除项目设置，这不是安装后的必需参数。
- 已验证：真实 Claude CLI 进程加载已安装技能与 MCP 桥；受控 Anthropic 协议 fixture **只提供模型的 `tool_use` 请求**，真实 `tool_result` 全部来自 Claude 自己的 MCP 客户端和隔离服务；`get_service_info`、`list_definitions`、`publish_definition`、`create_run`、`read_trace` 返回真实值，run 完成。
- 限制：受控 fixture 验证的是客户端接入和真实工具链路，不代表上游模型质量；未使用真实 Anthropic 凭据。

## 归属、升级与卸载

- 记录同时保存自有 activation 文件摘要和**已解析**的 activation root。卸载/更新默认使用记录中的 root，不重新猜测。
- 调用方显式给出不同的 `--activation-root` 时，返回零写入冲突，不转移归属。
- 共享技能根或用户已有的同内容文件不会被“认领”；发生冲突时保留用户文件。
- 旧版本安装记录没有 activation root 且没有自有技能文件时，安装新技能属于加法升级，不需要先卸载；已有自有技能但缺少 root 的记录会被拒绝。
- 卸载只删除记录中仍与安装摘要一致的自有文件；被用户修改的文件保留并报告冲突。安装完成后可运行：

```bash
agentgenome client uninstall --client KIND [--config-root PATH] [--dry-run]
```

## 验收范围速览

| 宿主 | 安装/发现 | 原生 MCP 工具调用 | 实际 run/trace | 证据等级 |
| --- | --- | --- | --- | --- |
| Pi 1.5.0 | 是 | 是（原生 RPC 模型循环） | 是（读取 Codex 创建的同一 run） | 原生进程 + 受控模型请求 |
| Codex 0.161.0 | 是（app-server） | 是（官方 app-server 控制协议） | 是 | 原生进程，无模型依赖 |
| Qoder 1.1.85 | 是 | 是（opt-in 原生认证探针） | 是（同一 live service/run，completed） | 原生 CLI + 真实登录；默认矩阵仅发现 |
| Claude 2.1.143 | 是 | 是（真实 CLI） | 是 | 原生 CLI + 受控 Anthropic 请求 |

自动化宿主测试使用隔离配置与服务。Qoder 的真实调用验证使用宿主已有登录，并单独限制 MCP 配置、工作目录和可调用工具；验证结束后停止测试服务。
