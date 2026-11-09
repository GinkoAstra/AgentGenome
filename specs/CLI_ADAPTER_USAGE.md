# CLI Adapter 使用说明

2026-11-07。适用于 `feat/cli-adapters` 中的 Codex / Qoder / Claude 三个 CLI
后端；Kimi 尚未实现（见末节）。本文只描述当前源码行为，不替代验收回执。

## 1. 选择后端

先按项目 README 安装 Python 环境及 AgentGenome，并独立安装所选 CLI。Host
注册部署允许的后端，Graph 的 Agent 定义通过 `backend_ref` 引用该后端：

```bash
.venv/bin/python -m node_runtime.control_plane.host serve \
  --data-dir ./.agentgenome-cli-service \
  --backend codex \
  --model <model-id> \
  --config-dir /path/to/existing/codex-home
```

| 参数 | 含义 |
| --- | --- |
| `--backend codex|qoder|claude` | 选择对应 CLI 适配器；`pi` / `script` 保持原有行为 |
| `--executable PATH` | 覆盖默认可执行文件（`codex` / `qodercli` / `claude`），启动时校验存在且可执行 |
| `--model NAME` | 显式传给 CLI；建议指定。省略时使用隔离启动后的 CLI 默认值，被排除配置中的模型设置不会继承 |
| `--config-dir DIR` | 指定已有 CLI 配置／认证目录，启动时校验存在；不会改写原配置文件，CLI 可在自己的目录保存运行会话；仅对 CLI 后端有效 |
| `--provider` | 仅 Pi 使用；CLI 后端传 `--provider` 直接 `invalid_argument` |

同一个数据目录只允许一个 Host 占有。重复 `start` 会与 `host.json` 中的
`backend/model/executable/config_dir/controller_id/scheduler_id` 比较；不一致
返回 `host_config_mismatch`。`config_dir` 未指定时持久化为 JSON `null`，不会
写成字符串 `"None"`。

## 2. CLI 启动参数（模型与配置）

- Codex：`codex exec --json ...`，显式模型用 `-m <model>`；配置目录通过
  `CODEX_HOME` 引用，同时使用 `--ignore-user-config` / `--ignore-rules`
  避免继承用户 hooks、profiles 与 MCP 服务器。这也排除了该 config.toml 中
  的模型和自定义 provider 设置；当前路径使用原生认证，尚不承诺该文件中的
  任意自定义 provider 可直接迁移。
- Qoder：`qodercli -p -o stream-json ...`，显式模型用 `-m <model>`；配置目录
  通过 `--config-dir <dir>`。
- Claude：`claude -p --verbose --output-format stream-json ...`，显式模型只用
  `--model <model>`。Claude 2.1.143 不接受 `-m`（报 unknown option），适配器
  不会生成该短选项；配置目录通过 `CLAUDE_CONFIG_DIR`。

模型调用由外部 CLI 完成。为隔离 hooks 和插件，Claude 必要的 `env` 与
`apiKeyHelper` 设置会复制到执行目录内权限为 `0600` 的私有设置文件；其中
可能包含部署提供的认证信息。它们不进入 Graph、模型提示或公开 trace。

## 3. 工具隔离与授权

每个 delivery 只启动一个受控 stdio MCP 桥（`worker_mcp.py`），并把 CLI 的
原生工具面关闭：

- Codex 关闭 shell、subagent、goals、browser、plugins、hooks、unified exec、
  `default_mode_request_user_input` 等原生能力；`code_mode_host` 保留，因为
  当前 Codex 版本依赖它执行 MCP 调用，其工具面仍由本次运行的
  `mcp_servers.agentgenome.enabled_tools` 限定。
- Qoder / Claude 使用 `--tools ""`、`--setting-sources ""` 和
  `--strict-mcp-config`。即使没有任何授权，也写入并加载
  `{"mcpServers": {}}`，防止继承外部 MCP。
- 本地文件工具只有 `read_file` / `write_file`，且仅当 Worker 的
  `tool_profile.backend_tools` 明确列出时才暴露；路径限定在工作区，冻结的
  `inputs/` 与 `messages/` 只读。
- 系统工具只来自 `tool_profile.tools` 的显式授权，校验名称、版本与 scope；
  `node.submit_review` 和 `node.resolve_decision` 只在绑定对应候选/决策的
  delivery 中生效。模型不能通过调用参数扩大 run/node/worker/epoch、工作区、
  review 或 decision 绑定。

## 4. 同一网络 Worker API 与候选语义

系统工具由桥接进程直接调用正在运行的 AgentGenome 服务：

- 端点：`POST /v1/worker/tool`，携带 delivery 私有的 Bearer 凭据；
- 凭据与身份来自权限 `0600` 的 tool-context 文件，身份字段由桥接代码固定，
  不向模型暴露；
- `node.submit_candidate` 只创建候选，**不等于接受**，也不会把 Node 标记为
  完成；必须由父节点或独立 Checker 通过 `node.submit_review` 接受；
- 成功响应返回 ToolSpec 的结构化输出；失败若在请求可能已提交后发生
  （超时、连接中断、非 JSON、无法确认的状态码），保留 unmatched start，
  报告 `tool_outcome_unknown`，本轮后续工具调用被拒绝，不做新 call id 重试。

## 5. 预算与计数

- `max_tool_calls` 由桥接在每次执行前预留并在预算文件中 fsync；用尽后拒绝
  下一次调用并触发 `budget_exhausted`。
- `max_provider_calls` 被明确拒绝（`capability_unsupported`）：CLI 适配器无法
  在 provider 请求发出前可靠拦截，因此不提供会失守的硬预算。
- `provider_calls` 在 Backend 观察中为未知；公开 API 的 `usage` 省略该字段，
  不将未知伪造成 0。`usage_lower_bound` 保留已持久累计下界，供预算续接使用。
  `tool_calls` 统计桥接在执行前持久预留的调用次数，不代表原生模型请求数。

## 6. 会话续接与边界

- 支持同一执行绑定内的原生 session 续接：从 CLI 事件中读取真实 session /
  thread id，下一次 delivery 用 `--resume` / `codex exec resume` 继续。
- 不支持历史 checkpoint：`capabilities.checkpoint=False`，`checkpoint()` /
  `restore()` 返回 `capability_unsupported`。
- 不支持任意历史会话快照或外部副作用重放；`reconcile_tools` 只对账本地
  `read_file` / `write_file` 与已知未决调用，系统工具的真实结果只能由服务
  状态证明。
- `internal_activities="none"` 仅建立在原生 shell/subagent/background 已关闭、
  所有工具经同一可停止进程组中的 MCP 桥执行的前提上；观察完整为空。

## 7. 停止与精确进程身份

- 进程身份是 `pid + kernel start`；仅有 PID 存在不算存活，缓存 Popen 被采用
  前必须核验当前内核 start，PID 复用不会导致误杀。
- watcher 独占 leader 回收；终止后扫描整个进程组，确认无活跃成员才写
  `group_cleaned=true`。leader 先退出而 MCP 子进程仍存活时，观察保持
  `active`/`unknown`，不提前释放调度容量。
- Host 重启后 leader 已消失时，用 MCP 桥自记录的 `pid/start/pgid`（与
  delivery 全身份匹配）锚定原进程组；没有精确锚点则拒绝 stop，不凭 pgid
  猜测。清理成功后会把持久退出证据更新为 `group_cleaned=true`，后续
  `observe` 才能返回 `stopped`。

## 8. 使用与验证命令

```bash
# 单元/生命周期测试（真实派生子进程，不调用模型）
.venv/bin/python -m pytest tests/control_plane/test_cli_backend.py \
  tests/control_plane/test_cli_protocol.py \
  tests/control_plane/test_cli_host_selection.py \
  tests/control_plane/test_worker_mcp.py -q

# 全量回归
.venv/bin/python -m pytest tests/control_plane -q
```

## 9. 证据范围与未完成项

- Qoder 真实模型完整 SOP：已独立核验（两候选、Checker revise→accept、同
  native session 续接）。
- Codex 真实模型完整 SOP：已独立核验（同上）。
- Claude：真实 CLI + 本地受控模型完成完整 SOP；真实上游模型验收因持续
  HTTP 502 待可用环境，不能视为已通过。
- Kimi：明确未实现。安装位置/隔离安装选择等待用户回复；`Host` 与
  `make_protocol` 均以 `capability_unsupported` 拒绝，不伪造协议。
- 上述真实模型证据不替代恢复路径（restart/unknown 对账）的验收。
