# 首轮实现瘦身记录

本页保留首批清理的历史范围；后续缺陷修复与最终验收见 [本轮完成记录](SLIMMING_COMPLETION.md)。

日期：2026-11-06。分支：`refactor/runtime-slimming`，基线：v0.1 / `64ab8d9`。
状态：首批清理已独立验收通过；完整架构瘦身、安装优化和语言迁移不在本回执的完成声明内。

## 实际改变

| 位置 | 改动 | 对系统结构的作用 |
| --- | --- | --- |
| `host.py:Host` | 删除没有当前调用者的 `serve → _loop` 及仅为其使用的导入 | 删除旧无网络运行入口；正式 CLI 仍由 `ApiServer.serve` 承载 HTTP / MCP 与控制循环 |
| `backends/spawn.py:process_start` | 公开并复用现有的 PID start-time 读取函数 | Host、PiBackend 和独立 spawn 脚本使用同一实现；没有新模块、类或兼容包装 |
| `mcp_server.py:build_server` | 删除无引用的嵌套 `definition_arguments` | 只保留工具实际使用的定义参数处理路径 |
| `server.py` 与 Host 注释 | 删除已失效的 P1/P2 迁移叙述，明确当前服务职责 | 注释与实际正式入口一致 |

没有修改 Graph、schema、工具协议、Scheduler、API 事务或测试。Host 的 claim/release、启动声明、目录锁、服务记录及实际停止路径保留。正式 `agentgenome serve` 在基线已经启动 ApiServer，本次删除的是未使用的旧程序入口，不是补建缺失 API 服务。

## 保留与暂缓

- 两个 `_pid_alive` 保留各自语义：Host 的 `os.kill(pid, 0)` 判断与 Pi 的参数/身份判断不能因为都调用 PID 读取函数就合并。
- Pi / Script 两个 Backend JSON 写入体相同；本轮未为这八行创建额外持久化模块。其他 Host / Server / spawn JSON 写入涉及不同权限、格式和目录刷新要求，也未强行合并。
- HTTP / MCP 已共用 `dispatch` 和同一个 Api；WorkerSpec 已共用组合函数；Controller / Backend 有明确的执行协议。保留这些边界。
- Api 的事务校验与 Scheduler 的推进决策不是同一责任。此次代表性调用审查没有提供拆分这两个大文件的充分理由，尚未完成所有动态路径与性能审计。
- 更深入的依赖裁剪、安装简化与语言迁移仍属后续工作，本次不声称“全部实现已最简”。

## 变更量与证据

运行文件仍为 42 个，Python 类定义仍为 53 个；函数定义由 725 减为 719，总代码行数由 24,549 减为 24,480。改动 5 个运行文件，新增 21 行、删除 90 行。统计含空行和注释；不据此推断性能、内存或部署成本改善。

Qoder 编码，Codex 独立核对 diff、实际调用和边界；stop-that-shit 独立 reviewer 未发现阻断问题。Qoder 原始记录中的“Host.serve 的 caller 是 Host._loop”写反；源码关系是 **Host.serve 调用 Host._loop，Host.serve 自身无当前调用者**，裁决以源码为准。

Codex 在本 worktree 独立运行 `python -m pytest tests/control_plane -q`：**372 passed in 164.99s，无跳过，退出码 0**。测试前后 runtime/tests/包配置摘要一致，`git diff --check` 通过。覆盖范围以现有回归为准；本次没有另跑真实模型业务验收或桌面 Codex 接入验证。

本地证据在 `.workspace/slimming-final/`：测试前后源码清单、日志、结果和修改后统计；不是需要发布的运行文件。没有新建提交或推送，也没有修改已有用户服务、前端回答或旧工作区源码。

后续路线见 [实施计划](REFACTOR_LATEST.md)；基线文件、代码对象、数据对象及调用链见 [源码盘点](runtime-baseline.md)。
