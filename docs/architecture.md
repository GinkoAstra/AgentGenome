# Architecture

AgentGenome 当前是一个本机常驻服务，管理递归任务、执行绑定、版本化输入输出和验收。协议入口、控制循环、执行后端与持久数据各有明确职责。下述路径均相对于 `node_runtime/control_plane/`。

## 进程与代码组件

| 进程 | 文件 / 对象 | 职责 |
| --- | --- | --- |
| 普通 Pi | `backends/pi_command.mjs`、`pi_teacher_tools.mjs` | 激活 Teacher；将工具调用发送到 `/mcp`；原生文件工具编辑本地 YAML |
| 同一 Pi | `backends/pi_trace.mjs`、`service_http.mjs` | 展示只读运行快照、事件订阅与执行详情 |
| 可选脚本客户端 | `teacher_cli.py`、`client.py::ApiClient` | 直接调用 HTTP；不位于 Pi 与 MCP 之间 |
| 常驻 API 服务 | `server.py::ApiServer`、`http_api.py`、`mcp_server.py`、`service_auth.py` | 网络生命周期、协议适配、认证；统一进入同一个 Api |
| 服务内部 | `api.py::Api`、`storage.py`、`artifacts.py` | 权限、条件更新、幂等、版本、事务、状态和产物 |
| 服务内部 | `host.py::Host`、`scheduler.py::Scheduler`、`policies.py` | 读取持久事实，计算依赖 / 拆分 / 就绪 / 预算，提交执行绑定与意图 |
| 服务内部 | `controller.py::Controller` | 协调已提交执行的启动、投递、观察和停止；不做业务验收 |
| 服务内部 | `backends/pi.py::PiBackend`、`backends/script.py::ScriptBackend` | 管理实际 Pi Agent 或可信程序执行，向控制面报告观察结果 |
| Pi Worker / Checker 子进程 | `backends/pi_node_tools.mjs`、`pi_tools.mjs` | 通过 HTTP 提交结构化事实与候选，管理授权范围内的文件工具 |
| 服务内部 | `trace.py::TraceService`、`RequestDiagnostics` | 状态投影、执行详情与请求诊断；不推进任务 |

`ApiServer` 管理请求循环和独立的控制线程；控制线程调用 `Host.reconcile_once()`，Host 负责组件组装与单次协调。SQLite 是嵌入式持久存储，不是单独启动的数据库服务；ScriptBackend 在服务内执行显式授权的程序。Pi Worker 是服务启动的子进程。

HTTP 和 MCP 共用 `http_api.PUBLIC_OPERATIONS` 中的公开操作名。Teacher/Pi 安装入口读取这份定义，服务进程再加载实际 MCP SDK 和注册工具；参数适配仍由各协议入口处理。

## 数据对象

| 数据 | 表达内容 | 对应定义 / 处理文件 |
| --- | --- | --- |
| GraphDefinition | 流程版本、阶段依赖、输入绑定、Agent / Task 引用与预算 | `api.py` |
| AgentDefinition | 后端、指令、工具权限与必要能力 | `agent_definitions.py` |
| TaskDefinition | 目标、输入输出契约、验收、拆分与进展规则 | `task_definitions.py` |
| Run / Node | 一次流程执行、逻辑任务、父子关系与依赖状态 | `resources.py`、`api.py` |
| WorkerSpec / 执行绑定 | 冻结定义与本次委派组合出的执行参数；绑定身份、epoch、delivery | `worker_specs.py`、`bindings.py`、`backends/base.py` |
| Candidate / Review | 候选产物、接受 / 返工等结构化评审结果 | `resources.py`、`api.py` |
| ArtifactRef | 内容标识及哈希、大小、媒体类型；对应不可变输入或输出字节 | `artifacts.py` |

这些是数据结构；解析、校验或修改它们的函数才是代码操作。YAML 是定义的序列化形式，不是一个执行组件。Node 与 Worker 不拥有两套任务调度规则；技术执行绑定也不等同于一次模型请求。

## 一次运行的数据流

| 步骤 | 调用者与操作 | 输入 → 结果 |
| --- | --- | --- |
| 1. 编辑 | Teacher 的文件工具 | 用户确认的需求 → 工作区 YAML |
| 2. 发布 | Teacher → MCP → Api | YAML 文本 → 已校验的不可变定义引用 |
| 3. 提交 | Teacher / 脚本 → Api | 输入字节 → ArtifactRef；定义引用 + 输入 → Run |
| 4. 调度 | Scheduler → Api | 已提交状态、依赖、预算 → 执行意图与完整绑定 |
| 5. 执行 | Controller → Backend | 已提交 WorkerSpec → 实际多轮 Agent 执行与观察事实 |
| 6. 候选 | Worker → HTTP → Api | 产物内容与执行身份 → Candidate，等待验收 |
| 7. 验收 | 父 Agent / 声明 Checker → Api | 冻结输入、候选、契约 → 结构化 Review |
| 8. 推进 | Scheduler → Api | 已提交评审 → 接受、返工、拆分或后续任务 |
| 9. 交付 | Teacher / 客户端读取 | 已接受 ArtifactRef → 导出文件与报告 |

拆分请求进入控制面并经策略、权限和预算校验，再创建受管理的子任务。父子树与依赖边分别表达。Scheduler 解释已提交的结果，不自行判定“清洗得好不好”。

## 服务接口

| 接口 | 用途 |
| --- | --- |
| `POST /mcp` 等 MCP 传输请求 | Streamable HTTP MCP，Teacher 工具发现与调用 |
| `POST /v1/operations/{operation}` | JSON 客户端操作：定义、输入、Run、报告与控制 |
| `POST /v1/worker/tool` | 带执行身份的 Worker / Checker 工具调用 |
| `GET /v1/runs/{run_id}/trace` | 一致快照与控制游标 |
| `GET /v1/runs/{run_id}/watch` | SSE 事件订阅 |
| `GET /v1/runs/{run_id}/executions/{worker_id}/{epoch}/{delivery_key}` | 指定执行的分页详情 |
| `GET /v1/requests` | 请求诊断 |
| `GET /healthz`、`GET /readyz` | 服务健康与就绪 |

客户端通过服务发现文件获取端点与凭据；发现文件不是客户端直接打开 SQLite 的理由。MCP 和 JSON 路由复用 Api 的权限与事务规则。网络 MCP 接收 `definition_text` / `document`；Pi 的 `definition_path` 是本地扩展读取文件后转换上传的便捷参数，不允许远程服务读取客户端路径。

## 完成、恢复与观测

先持久提交意图，再执行；观察真实结果，再提交状态。`spec` 表示期望，`status` 表示事实，两者可能暂时不同。旧执行身份不能覆盖新绑定，重试写请求需保留原 request-id 与内容。

单个 Worker 的 BackendError 在该 Worker 的协调边界记录，其他 Worker/Run 继续推进。停止路径仍核验持久执行绑定及实际进程身份，但不依赖当前程序代码、工具配置或输入材料能否继续运行。新投递则必须满足运行准备与程序身份要求；停止请求本身不能代替已停止的观察事实。

服务的 `start/serve/stop` 使用数据目录中的 Host 所有权记录和服务记录关联实例，并核对内核 PID start-time。前台、后台和 console-script 使用同一所有权规则；不依赖命令行里是否显式写出 instance-id。

任务提交候选后等待父或 Checker 接受。返工可以继续执行会话；需要系统独立调度和验收的工作才成为子任务。冻结的是契约、输入与接受的输出，并非所有流程都预先固定子任务数量。

Goal 的进展来自声明的评估证据；Scheduler 累计结构化结果，按规则触发停滞回交。没有可接收回交的根节点按声明的边界处理，不能无限续跑。

Trace 读取同一服务的权威状态，并提供独立的执行日志游标。它不创建第二套运行状态，不请求模型，也不代替业务验收。恢复持久任务状态不能保证任意外部副作用的严格重放。
