# CLI Adapter 当前交付状态

2026-11-07，工作区 `../AgentGenome-cli-adapters`，分支 `feat/cli-adapters`。
四 CLI 计划尚未整体完成；下表区分源码、实际 CLI 协议与真实模型证据。

| 后端 | 适配代码 | 实际 CLI 协议 | 真实模型完整流程 |
| --- | --- | --- | --- |
| Codex 0.161.0 | 已实现 | 通过 MCP、同线程续接、trace | 通过：候选 1 → revise → 原会话候选 2 → accept |
| Qoder 1.1.82 | 已实现 | 通过 MCP、同会话续接、trace | 通过：同上 |
| Claude Code 2.1.143 | 已实现 | 真实 CLI＋本地受控模型完成同一流程与 trace | 待可用上游；现有环境持续 HTTP 502 |
| Kimi | 未实现，显式拒绝 | 待安装路径或隔离安装选择 | 未验证 |

真实流程使用独立程序 Checker 检查实际产物 JSON，第一轮 `{"value":1}` 被退回，
第二轮 `{"value":2}` 被接受。Codex／Qoder 使用实际配置的模型；Claude 使用本地
受控模型协议 fixture，不能据此推定真实模型表现。原生会话标识、候选版本、评审
记录与产物摘要均独立核对；`max_inflight_nodes=1` 下完整推进。

## 当前代码组成

| 文件／代码对象 | 职责 |
| --- | --- |
| `backends/preparation.py::WorkerPreparation` | Pi 与 CLI 共用冻结材料、授权和提示装配 |
| `backends/cli.py::CliBackend` | 公共投递、观察、同会话续接、停止、恢复核验及执行日志 |
| `backends/cli_protocol.py` 中三个 Protocol | 各 CLI 的参数、MCP 配置与事件格式差异 |
| `backends/worker_mcp.py::Bridge` | granted 工具映射、受限文件操作、请求同一网络 Worker API |
| `host.py::Host` | 显式部署注册和参数；可继续挂载可信 Script Checker |
| `base.py`、`api.py`、`controller.py`、`scheduler.py` 的通用用量处理 | 未知计数与持久累计下界分开；保留已有预算，不加入 CLI 调度分支 |

API 继续负责权限、版本和持久状态；Scheduler 继续决定任务关系推进；Controller
协调已提交执行。CLI 回答结束不代表 Node 完成。Graph /2、数据库 schema /3 保持。

## 独立验收

最终独立整合回归 **455 项全部通过**（187.31 秒），测试期间运行代码及测试源码摘要一致。
本地独立回执 `.workspace/cli-adapters/independent-acceptance.json`（验证产物不随源码发布） 接受下述已验证范围，
四 CLI 计划仍未整体完成；实现者 `progress.md` 不替代验收。19 项 STS 定点测试已包含
在 455 项中，不重复累加。wheel 内 46 个 Python／MJS 文件与最终源码逐字节一致。

已获得的独立证据：

- STS 生命周期评审关闭；19 项针对性测试通过，含 PID 复用、主进程先退出、
  并发观察、MCP 锚点恢复停止、缺退出记录的 running／failed 投递。
- 三后端在测试服务重启后均通过 HTTP trace 与分页执行日志验证；执行身份与
  游标一致，未知 provider 计数未伪造成零。
- wheel 已在新的隔离虚拟环境安装；实际 Codex 后端 Host 启动、复用同一进程、
  状态查询及停止通过，入口确实从该 wheel 导入。此项不调用模型。
- 本轮修正了一个既有 Pi 测试的文件发布竞态：捕获文件写完后再原子发布给
  断言读取，断言语义及产品行为保持。

## 使用边界与接续条件

[使用说明](CLI_ADAPTER_USAGE.md) 包含部署命令、模型与配置目录、工具授权、预算、
会话和停止语义。CLI 仅开放获准的系统工具及工作区 read/write；不开放任意原生
shell／subagent。不能可靠限制 provider 请求次数，因此该硬预算明确不支持。
原生 session 续接不等于历史 checkpoint 或外部副作用重放。

本次源码交付不切换用户已有服务。下一步需要 Kimi 的安装位置／
隔离安装选择，以及 Claude 可用模型环境；不改动全局配置或推理服务来掩盖缺口。

独立证据目录：`.workspace/cli-adapters/`。主要记录：
`base-framework-accepted.json`、`codex-final-regression.json`、
`wheel-smoke-result.json`、`trace-audit.log`，以及各 `probes/real-cycle-*` 下的结果。
