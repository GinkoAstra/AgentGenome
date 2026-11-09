# 在普通 Pi 中使用 AgentGenome

[返回首页](../README.md)

## 安装与激活

前提是 **Node.js 20+** 和已经安装、配置好模型的 Pi CLI。npm 包目前**没有发布到 npm registry**；在仓库根目录执行：

```bash
npm exec --package=. -- agentgenome-setup --client pi
```

分支 `feat/unified-client-access` 推送后，也可从 GitHub 运行同一个安装器，命令见 [CLI 接入说明](client-access.md)。安装器复用本机 uv；uv 缺失时用 `curl` 下载固定版本的官方 uv，不修改 shell 配置文件。

安装器以非可编辑方式把运行时放进 uv tool 环境，只写入 Pi 扩展 `~/.pi/agent/extensions/agentgenome.js`；设置 `PI_CODING_AGENT_DIR` 或传入 `--config-root` 可选择独立配置目录。它不启动服务、不配置 Worker 或语义 Checker 的模型，也不修改凭据；不会覆盖没有自有标记的同名扩展。入口绑定 uv tool 环境的持久解释器，删除 npm 缓存后仍可使用；更新时重新运行同一条安装命令。安装成功会打印 `Console: /绝对路径/agentgenome`；如果 `agentgenome` 不在 `PATH`，后续命令直接使用这个绝对路径。旧的 `--provider`/`--model` 参数会被接受但忽略。[Pi 安装与配置](https://github.com/earendil-works/pi/tree/main/packages/coding-agent)以其项目文档为准。本次源码回归使用 Pi `1.5.0` / Node.js `22.22.2` 并通过真实 RPC 接入检查；终端面板的历史界面验证使用 Pi `0.87.1`。

启动服务后，打开新的 Pi 窗口，在可写工作目录输入 `/agentgenome`。已有对话上下文、模型和原生工具保留，Graph 工具通过真实 MCP 客户端连接已经运行的服务。`agentgenome-teacherctl` 只是可选脚本工具，不是 Teacher 的必经层。

## 草稿和服务状态分别放在哪里

当前目录用于 SOP 草稿。启用时生成通用的 `sop-guide.md` 和 `sop-template.yaml`，Teacher 从这两份文件了解协议。

服务独立常驻，默认数据目录为 `$XDG_STATE_HOME/agentgenome/default`，未设置时为 `~/.local/state/agentgenome/default`。不同 cwd 的 Pi 连接同一个默认实例；`/agentgenome` 只发现并连接已经运行的服务，不会自动启动。服务未运行时会明确报告不可用，需要先单独部署。客户端不配置或比较服务端 Worker 的模型；部署配置由服务管理。

退出 Pi、切换会话或 `/agentgenome off` 会清理本窗口的连接与面板，后台服务和 Run 继续存在。再次启用后可查询原有 Run。模型凭据沿用 Pi 自己的配置，不写入 Graph YAML。

## 从对话到产物

1. 与 Teacher 确认目标、输入、输出、验收与预算。
2. Teacher 用文件工具编辑 YAML，经 `validate_definition` 校验、`publish_definition` 发布版本。
3. 将输入内容经 `put_artifact` 冻结，再用返回的完整引用 `create_run`。
4. 运行中的 Worker 产生候选；父任务或声明 Checker 给出评审，控制面推进。
5. 用报告核对已接受的输出，再通过 `read_artifact` 导出。记录 Run 与产物身份，不用模型的自述替代状态。

发布定义不会自动执行业务。写请求回执丢失后，应保持同一个 request-id 和请求内容查询或重试，避免生成重复 Run。

## Trace 与终端操作

| 操作 | 行为 |
| --- | --- |
| 点击 `AgentGenome ▸` 或 `/agentgenome trace` | 展开 / 收起只读面板 |
| `r` | 选择 Run |
| `t` | 切换分解树 / 依赖视图 |
| `↑` / `↓` | 选择节点，查看执行身份、候选、反馈与产物 |
| `Ctrl+E` | 焦点回到编辑器，面板继续展示 |
| `Esc` | 收起面板 |
| `/agentgenome status` | 当前启用状态、工作区与连接配置；prepare 时 Host 状态不是实时业务报告 |
| `/agentgenome off` | 停用本会话模式，不取消 Run 或停止服务 |

面板展开后读取快照并订阅 SSE，折叠后取消订阅。连接中断、过期、认证失败均有显式状态。RPC / JSON / print 模式不显示终端浮层；可使用 CLI trace 查询。

## 可选的脚本与排障命令

以下命令直接使用安装器放进 uv tool 环境的持久命令；如果 `agentgenome` 不在 `PATH`，用安装成功时打印的 `Console` 绝对路径代替：

```bash
agentgenome status
agentgenome-teacherctl status
agentgenome-teacherctl trace RUN_ID
agentgenome-teacherctl watch RUN_ID
```

`status` 不会自动创建服务。以下是独立的服务部署步骤，不是客户端安装；需要手动启动自己的服务时，模型参数必须与部署配置一致：

```bash
agentgenome start --backend pi \
  --provider YOUR_PROVIDER --model YOUR_MODEL
```

`stop` 是服务生命周期操作，与业务暂停或取消不同。只有确实要停止所管理的服务时才执行：

```bash
agentgenome stop
```

清洗 / Goal 示例依赖可信程序 Checker，请按 [示例指南](../examples/agent-workflow/README.md) 启动独立实例。普通 `/agentgenome` 安装入口没有程序工厂参数，不能假定它会自动安装这些 Checker。

卸载 Pi 扩展：

```bash
agentgenome client uninstall --client pi
```

卸载仅删除自有入口，不删除服务数据，也不停止 Run。
