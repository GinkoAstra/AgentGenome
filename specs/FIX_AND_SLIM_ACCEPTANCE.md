# 缺陷修复与实现瘦身：本轮验收范围

2026-11-06 用户授权：继续缺陷扫描、修复并推进完成瘦身。工作区仍为 `AgentGenome-slimming` / `refactor/runtime-slimming`，不发布或切换原工作区。

当前状态：F1–F4 及独立评审中的投递入口补充均已实现并验收。403 项独立整合回归、安装包真实 CLI 验证通过；结果与证据见 [完成记录](SLIMMING_COMPLETION.md)。以下保留验收要求。

## 已确认问题与具体改动

| 项 | 证据与改动 | 必须保留 | 验收 |
| --- | --- | --- | --- |
| F1 单个失败执行阻断其他 Run | 真正的程序 Checker 身份变化会令 BackendError 逃出整个 Host tick；按 Worker 隔离已知 BackendError | 身份不匹配必须拒绝；不伪造停止、成功或副作用可重放 | 持续拒绝旧执行时，新 Run 仍推进；普通执行和终态 drain 都覆盖 |
| F2 正式入口启动/停止身份不一致 | 前台 serve 自动生成 instance_id，stop 却从 argv 要求显式参数；改用现有持久服务/Host 身份及内核进程身份 | 目录、实例、PID start-time 校验；不停止不相关进程；HTTP 异常时仍能停止已确认的进程 | 模块、console-script、后台 start 入口；默认/相对目录；身份冲突拒绝；退出后未回收的子进程不能被当作仍执行任务 |
| F3 客户端为读取常量加载服务端 SDK | Teacher/Pi 安装仅读取 TOOL_NAMES，导致加载 MCP SDK；复用现有公共操作列表 | MCP 实际工具、参数适配、Pi 白名单与用户入口一致 | 新进程安装/Teacher 导入不加载 MCP SDK；真实 tools/list 一致；同解释器同脚本三次取中位数对比导入耗时与峰值 RSS |
| F4 停止被运行前校验阻断 | Controller 先取得 Backend handle；当前代码/材料校验失败会阻止进入 stop。停止路径只校验持久执行绑定，跳过仅用于继续运行的检查 | 当前代码不得接管旧执行；绑定/epoch、实际 PID、未知副作用及 stop/observe 语义保留 | 修改程序身份后 API 取消仍能停止；Pi 输入丢失/工具配置变化时仍能终止真实合成子进程；active 路径仍拒绝非法恢复 |

F3 不新建注册器或服务：`http_api.PUBLIC_OPERATIONS` 已存在，和 MCP 工具名同名同序。MCP 参数处理仍有协议适配职责，不能顺带合并。Teacher/Pi 读取共享操作名，MCP 对外工具名复用同一元组。

F4 来自修复期间的进一步源码核验，独立 reviewer 已用真实 Script API 取消路径复现；探针与修改前结果在 `.workspace/slimming-stop/`。该修复不需要新增 Controller 或 Backend 协议。F1 的 active 错误隔离仍应覆盖；terminal drain 验收升级为旧执行能安全停止且其他 Run 继续推进，不再要求该场景维持错误日志。

独立实现评审补充：stop-only start 取得句柄不代表可以运行新工作。Pi 的新投递必须拒绝明确 prepared=False，Script 的新投递必须校验当前程序身份；重复回执和停止行为保持原义。这两个遗漏已用直接 Backend 探针复现并修复，未声称外部 API 可达。

## 扫描范围和保留决定

独立 reviewer 已检查事实提交、事务/幂等、候选与评审、消息送达、Scheduler/API 共用 WorkerSpec、授权与预算收窄、Controller 观察提交，以及 HTTP/MCP/Worker 适配。本轮没有新证据支持合并 API/Scheduler 或移除权威提交时的校验。

- 保留现有包级 Python 导出。HTTP client 导入约 60 ms；为更小的潜在收益加一套懒加载或兼容框架不值得。
- 保留 jsonschema 依赖。MCP SDK 的已安装依赖元数据也要求它，删项目声明不能减少其安装量。
- 保留不同的持久化、权限与停止语义；不以行数目标驱动删代码。
- 语言迁移、桌面 Codex 安装接入和新增 Backend 不混入本轮。完成本轮不表示这些后续方向已完成。

## 完成条件

三个缺陷有真实回归，客户端导入有前后证据，当前被审查源码通过整合回归与独立 STS review，交付说明写明删除/合并/保留、指标口径及限制。测试失败或审查提出具体缺陷时继续修复；没有新证据时不重复扩张扫描。

Qoder 独占运行代码和相关测试；Codex 维护设计、独立源码核查、打包入口验证和最终裁决。旧回执不改写，新的验收绑定新的源清单。所有服务测试均使用合成临时目录，不操作现有用户服务。
