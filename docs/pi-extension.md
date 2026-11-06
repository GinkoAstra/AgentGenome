# 在普通 Pi 中使用 AgentGenome

[返回首页](../README.md)

## 安装与模型

在仓库根目录执行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m node_runtime.control_plane.pi_integration install \
  --provider YOUR_PROVIDER --model YOUR_MODEL
```

用已经在本机 Pi 配置的模型替换占位符。安装参数配置后台 Pi Worker 和语义 Checker，不改变普通 Pi 的对话模型。[Pi 安装与配置](https://github.com/earendil-works/pi/tree/main/packages/coding-agent)以其项目文档为准。本次源码回归使用 Pi `1.5.0` / Node.js `22.22.2` 并通过真实 RPC 接入检查；终端面板的历史界面验证使用 Pi `0.87.1`。

安装器写入 `~/.pi/agent/extensions/agentgenome.js`；设置 `PI_CODING_AGENT_DIR` 或传入 `--agent-dir` 可选择独立配置目录。它不修改凭据和 `settings.json`，不覆盖没有自有标记的同名扩展。生成的入口引用安装时的 Python 和包路径；移动后重装。

安装后打开新的 Pi 窗口，在可写工作目录输入 `/agentgenome`。已有对话上下文、模型和原生工具保留，Graph 工具通过真实 MCP 客户端连接服务。`agentgenome-teacherctl` 只是可选脚本工具，不是 Teacher 的必经层。

## 草稿和服务状态分别放在哪里

当前目录用于 SOP 草稿。启用时生成通用的 `sop-guide.md` 和 `sop-template.yaml`，Teacher 从这两份文件了解协议。

服务独立常驻，默认数据目录为 `$XDG_STATE_HOME/agentgenome/default`，未设置时为 `~/.local/state/agentgenome/default`。不同 cwd 的 Pi 连接同一个默认实例；首次启用时不存在则自动启动。已有实例的模型 / 后端配置不一致时会报告 `host_config_mismatch`，不会偷偷重启覆盖它。

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

以下命令在仓库根目录执行；也可使用安装环境中的完整可执行路径：

```bash
.venv/bin/agentgenome status
.venv/bin/agentgenome-teacherctl status
.venv/bin/agentgenome-teacherctl trace RUN_ID
.venv/bin/agentgenome-teacherctl watch RUN_ID
```

`status` 不会自动创建服务。需要手动启动自己的服务时，模型参数必须与部署配置一致：

```bash
.venv/bin/agentgenome start --backend pi \
  --provider YOUR_PROVIDER --model YOUR_MODEL
```

`stop` 是服务生命周期操作，与业务暂停或取消不同。只有确实要停止所管理的服务时才执行：

```bash
.venv/bin/agentgenome stop
```

清洗 / Goal 示例依赖可信程序 Checker，请按 [示例指南](../examples/agent-workflow/README.md) 启动独立实例。普通 `/agentgenome` 安装入口没有程序工厂参数，不能假定它会自动安装这些 Checker。

卸载 Pi 扩展：

```bash
.venv/bin/python -m node_runtime.control_plane.pi_integration uninstall
```

卸载仅删除自有入口，不删除服务数据，也不停止 Run。
