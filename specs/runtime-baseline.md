# v0.1 Runtime 源码盘点

日期：2026-11-06。基线提交：`64ab8d90d1b2e242ea31d70f56fe26a818737d59`。
本页描述该提交的源码事实；优化建议不代表已经修改代码。

## 统计口径

运行文件 `42` 个：Python `35` 个，JavaScript module `7` 个，共 `24,549` 行（含注释、空行）。Python AST 中有 `53` 个 class 定义，其中 `20` 个带 dataclass 标记；有 `725` 个函数定义，包含顶层函数、类方法和嵌套函数；顶层函数 `247` 个，直接类方法 `438` 个。

这些是语法定义数量，不是系统职责数量，也不是实际运行实例数量。Python 有 `7,509` 个语法调用点，不等于运行调用次数。JS 只统计文件和行数，未统计其函数/对象，不能把 Python 的 class 数称为整个系统的对象总数。扫描包括 `node_runtime/__init__.py` 包入口，不包括测试、YAML、运行数据库、历史目录。

## 代码模块、数据和进程分别是什么

| 逻辑职责 | 代码文件与主要代码对象 / 函数 | 接收或产生的数据 |
| --- | --- | --- |
| 服务组装与生命周期 | `host.py:Host`、`Host.reconcile_once`；`server.py:ApiServer`；`discovery.py:Connection`；`service_auth.py:ServiceAuth` | 服务记录、连接信息、认证后身份 |
| 外部协议入口 | `http_api.py:dispatch`；`mcp_server.py:build_server`；`client.py:ApiClient` | operation / arguments、API 结果、错误 envelope |
| 权威状态操作 | `api.py:Api` 的 `create_run`、`append_fact`、`commit_plan`、`get_resource` | Run、Node、执行绑定、候选、父决策、事件和版本 |
| 持久化 | `storage.py:Store/Tx`；`artifacts.py:ArtifactStore` | SQLite 行、事务、事件游标、artifact 字节；这些数据不是服务进程 |
| 定义和绑定组合 | `task_definitions.py:compile_definition_bundle`；`agent_definitions.py:build_worker_spec`；`worker_specs.py:compose_worker_spec` | TaskDefinition、AgentDefinition、TaskDelegation、有效 WorkerSpec |
| 图推进 | `scheduler.py:Scheduler.reconcile/_plan`；`policies.py:ExpansionPolicy/FixedPolicy/AdaptivePolicy` | 读集合、期望变更、依赖和拆分计划 |
| 执行协调 | `controller.py:Controller.reconcile`；`backends/base.py:AgentBackend`；`backends/pi.py:PiBackend`；`backends/script.py:ScriptBackend` | WorkerSpec、Handle、DeliveryReceipt、Observation；代码对象并不等于某个业务 Agent |
| Worker 工具回传 | `worker_api.py:execute`；`tools.py:WorkerToolbox.invoke`；`pi_tools.mjs/pi_node_tools.mjs` | 带执行身份的工具请求、产物、候选、review、拆分请求 |
| Trace 投影 | `trace.py:TraceService/RequestDiagnostics`；`pi_trace.mjs` | 状态快照、控制事件和执行日志；不是另一个调度器 |
| 用户接入 | `pi_integration.py`；`pi_command.mjs`；`pi_teacher_tools.mjs`；`teacher_entry.py`；可选 `teacher_cli.py` | 安装配置、工作区草稿、工具参数；YAML 是输入数据，不是代码模块 |
| 共享协议规则 | `resources.py`、`execution_facts.py`、`lifecycle.py`、`bindings.py`、`decisions.py` | schema、身份、事实、合法状态与投递约束；名称相近不证明重复 |

进程边界：Host 和 ApiServer 在同一服务进程组装 API、Scheduler、Controller；ApiServer 的控制线程调用 `Host.reconcile_once()`。Pi 用户会话是客户端；PiBackend 的实际 Pi 执行是子进程。SQLite 是嵌入式持久化，不是需要另启的数据库服务。这里不是声称每个代码对象都是一个进程。

## 可定位的代表性调用链

1. **用户请求**：MCP tool → `mcp_server.build_server` 中 `call` → `http_api.dispatch`；HTTP → `ApiServer._dispatch` → 同一个 `dispatch` → `Api` 操作 → `Store/Tx`。API 承担权限和持久化语义，协议入口不是第二套业务服务。
2. **状态推进**：`ApiServer._control_loop` → `Host.reconcile_once` → `Scheduler.reconcile` → `_plan` → `Api.commit_plan`。提交后 Host 调用 `Controller.reconcile` → `AgentBackend.start/deliver/observe/stop`。这是两条经持久状态衔接的路径，不是 HTTP 请求等待整个 Graph 执行结束。
3. **Worker 回传**：服务的 Worker 路由 → `ApiServer._worker_dispatch` → `worker_api.execute` → `WorkerToolbox.invoke` → `Api.append_fact`。Worker 身份来自认证绑定，不能由模型参数覆盖。
4. **候选与验收**：候选和 review 作为不同事实进入 API；Scheduler 根据已提交事实计划后续变更。模型一轮结束、工具返回或进程退出都不能直接替代 Node 的接受条件。
5. **绑定构造**：Scheduler 与 API 使用 `compose_worker_spec`，进一步调用 `build_worker_spec`；前者组装任务上下文，后者合成定义/委派约束。两次校验位于提议和权威提交边界，不能仅因都涉及 WorkerSpec 就删除其中一处。

以上来自静态函数体和调用者核对，不是本轮采集的动态 trace。冻结基线的源码行号可由下表及符号定位；实际执行验收沿用切片计划分别安排。

## 已发现项及处理建议

| 项 | 源码证据（v0.1） | 判断与下一动作 |
| --- | --- | --- |
| 未使用的转换函数 | `mcp_server.py:136` 嵌套 `definition_arguments`；当前工具调用的是 `_definition_document` | 当前 runtime 中没有调用点；列入删除候选，实施前核对注册与测试，不删除实际使用路径 |
| 同一进程身份算法三份 | `backends/pi.py:140`、`backends/spawn.py:20`、`host.py:73` 的 `_process_start` | 函数体相同；考虑复用一份纯读取逻辑。spawn 当前按文件路径独立执行，不能引入破坏启动的包导入依赖 |
| 同一 Backend 状态写入两份 | `backends/pi.py:112`、`backends/script.py:57` 的 `_write_json` | 函数体相同；可考虑单一有明确语义的写入函数；不为两处复用引入通用持久化框架 |
| 其他同名 JSON 写入 | `host.py:58`、`server.py:1014`、`backends/spawn.py:31` | 行为不同：服务记录设置 0600，spawn 刷新目录，Host 的格式/持久化不同。不能仅按名称合并并丢掉区别 |
| Backend 锁包装相似 | Pi / Script `_guard` | 两个 Backend 各有自身状态路径和生命周期；先证明共享包装能简化调用，再决定，暂不增加共同基类 |
| Api / Scheduler 文件较大 | `api.py` 4,120 行；`scheduler.py` 2,602 行 | 说明审查范围集中，不证明它们应该拆成更多服务。需要按定义/事实/计划/查询及推进决策逐段找重复和越界 |
| MCP / HTTP 已共用语义 | `mcp_server.py:call` → `http_api.dispatch` | 保留。不要再次创建所谓“统一 API”或把 teacherctl 设为必经层 |

## 完整文件清单

类名是 Python 代码定义名；其中 dataclass 多为数据承载对象，不能将本表直接画成系统组件图。JS 的“未解析”表示此次未计数。

| 文件（相对仓库） | 行数 | Python class 定义 |
| --- | ---: | --- |
| `node_runtime/__init__.py` | 1 | — |
| `node_runtime/control_plane/__init__.py` | 10 | — |
| `node_runtime/control_plane/agent_definitions.py` | 582 | `AgentDefinition`, `TaskDelegation` |
| `node_runtime/control_plane/api.py` | 4120 | `Api` |
| `node_runtime/control_plane/artifacts.py` | 88 | `ArtifactStore` |
| `node_runtime/control_plane/backends/__init__.py` | 28 | — |
| `node_runtime/control_plane/backends/base.py` | 279 | `BackendError`, `Capabilities`, `WorkerSpec`, `Handle`, `DeliveryReceipt`, `Observation`, `CheckpointRef`, `ExecutionLog`, `StopReceipt`, `AgentBackend` |
| `node_runtime/control_plane/backends/locks.py` | 49 | `ExecutionLock` |
| `node_runtime/control_plane/backends/pi.py` | 2236 | `_RecordedProcess`, `PiBackend` |
| `node_runtime/control_plane/backends/pi_budget.mjs` | 81 | JS 未解析 |
| `node_runtime/control_plane/backends/pi_command.mjs` | 491 | JS 未解析 |
| `node_runtime/control_plane/backends/pi_node_tools.mjs` | 280 | JS 未解析 |
| `node_runtime/control_plane/backends/pi_teacher_tools.mjs` | 623 | JS 未解析 |
| `node_runtime/control_plane/backends/pi_tools.mjs` | 245 | JS 未解析 |
| `node_runtime/control_plane/backends/pi_trace.mjs` | 1610 | JS 未解析 |
| `node_runtime/control_plane/backends/script.py` | 675 | `ScriptBackend` |
| `node_runtime/control_plane/backends/service_http.mjs` | 276 | JS 未解析 |
| `node_runtime/control_plane/backends/spawn.py` | 80 | — |
| `node_runtime/control_plane/bindings.py` | 131 | — |
| `node_runtime/control_plane/client.py` | 283 | `ApiTransportError`, `ApiClient` |
| `node_runtime/control_plane/controller.py` | 353 | `Controller` |
| `node_runtime/control_plane/decisions.py` | 60 | — |
| `node_runtime/control_plane/discovery.py` | 179 | `Connection` |
| `node_runtime/control_plane/execution_facts.py` | 617 | `ExecutionIdentity`, `ActivityFact`, `ProgressEvaluation`, `AcceptanceRevision` |
| `node_runtime/control_plane/host.py` | 867 | `Host` |
| `node_runtime/control_plane/http_api.py` | 280 | — |
| `node_runtime/control_plane/lifecycle.py` | 294 | — |
| `node_runtime/control_plane/mcp_server.py` | 309 | — |
| `node_runtime/control_plane/pi_integration.py` | 388 | — |
| `node_runtime/control_plane/policies.py` | 135 | `ExpansionPolicy`, `FixedPolicy`, `AdaptivePolicy` |
| `node_runtime/control_plane/resources.py` | 1018 | `ControlPlaneError`, `Actor` |
| `node_runtime/control_plane/scheduler.py` | 2602 | `Scheduler` |
| `node_runtime/control_plane/server.py` | 1022 | `ServiceStartError`, `StopIncomplete`, `_GracefulServer`, `ApiServer`, `_BearerASGI`, `_RejectClientIdentity`, `_McpActorBinding`, `_McpDiagnostics` |
| `node_runtime/control_plane/service_auth.py` | 316 | `ServiceAuth` |
| `node_runtime/control_plane/storage.py` | 555 | `Tx`, `Store` |
| `node_runtime/control_plane/task_definitions.py` | 811 | `TaskDefinition`, `DefinitionBundle`, `_Library` |
| `node_runtime/control_plane/teacher_cli.py` | 349 | — |
| `node_runtime/control_plane/teacher_entry.py` | 417 | — |
| `node_runtime/control_plane/tools.py` | 577 | `ToolError`, `ToolSpec`, `WorkerToolbox` |
| `node_runtime/control_plane/trace.py` | 939 | `ControlCursor`, `RequestDiagnostics`, `TraceService` |
| `node_runtime/control_plane/worker_api.py` | 149 | — |
| `node_runtime/control_plane/worker_specs.py` | 144 | — |

## 证据与限制

基线精确哈希及每个函数/类的行号保存在本地 `.workspace/slimming-baseline/inventory.json`（生成证据，不进入发布包）。AST 使用 Python 标准库分析；逐文件内容 SHA-256 可用于判断后续改动。公开文档中的指标对应上述基线提交。

基线盘点阶段核对了上述代表性调用链和明确重复候选，没有声称完成所有动态路径审计、资源测量或语言选型。原发布回归 372 项通过仅证明基线；随后首批清理与独立新回归见 [交付记录](SLIMMING_RESULT.md)，不回写本页的 v0.1 统计。
