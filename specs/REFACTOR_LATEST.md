# Runtime 实现瘦身计划

日期：2026-11-06。基线：v0.1，`64ab8d90d1b2e242ea31d70f56fe26a818737d59`。
实施分支：`refactor/runtime-slimming`；独立 worktree：`../AgentGenome-slimming`。

当前状态：按用户授权完成本轮缺陷扫描、修复与有证据的瘦身，**403 项独立整合回归通过**，wheel 隔离安装后的真实 CLI 启停通过，独立 STS 发现均已关闭。详见 [本轮完成记录](SLIMMING_COMPLETION.md) 和 [验收范围](FIX_AND_SLIM_ACCEPTANCE.md)。首批 372 项回归仅为 [历史交付](SLIMMING_RESULT.md)，不替代当前结果。语言迁移尚未开始，代码未提交或发布。

## 问题与顺序

用户要求把当前实现的对象、调用和重复职责理清，降低安装部署负担，并考虑将来迁移 Go / Rust / C++。用户另行澄清：桌面适配指桌面 Codex 可以安装、接入和使用 AgentGenome。

本轮先做实现瘦身。语言迁移放在职责收口与部署基线之后：更换语言本身不会消除重复状态、重复协议或含糊的职责；目前也没有资源测量支持某种语言必然带来所需收益。尚未选定迁移语言。

当前行为不是整体错误：HTTP 和 MCP 已共用操作分发与同一个 Api；WorkerSpec 已由 Scheduler / API 共用纯函数组合。需要删除的是经调用检查确认的无用实现，合并的是语义相同的职责，不能把现有服务重新拆成多个权威入口。源码事实见 [盘点](runtime-baseline.md)。

## 必须保留的不变量

**任意外部调用与 Worker 回传，都必须通过同一 API 的身份、权限、版本和事务规则；Scheduler 只能基于已提交事实推进，Controller 只能执行已提交意图。**

同时保留：

- Node / Worker 的递归任务语义，完整多轮 Agent 执行与 `worker_id + epoch` 绑定。
- 提交候选不等于完成；父或声明 Checker 接受后才推进完成状态。
- 冻结契约、输入与接受输出；动态子任务、返工、进展证据和停滞回交仍受原契约约束。
- CAS、幂等、消息送达事实、停止与恢复行为；不能将断线或未知副作用解释为可安全重放。
- 当前 Graph /2、schema /3、HTTP / MCP、Pi `/agentgenome` 和 trace 行为；不修改用户服务或数据。

## 实施切片与验收

以下是工作切片，只有用户另行授权才创建提交或推送。

| 切片 | 具体工作 | 完成判据 |
| --- | --- | --- |
| S0：源码基线 | 列出文件、代码对象、数据对象、调用入口；记录现有职责与真实重复 | 所有统计有明确口径；调用路径可定位到函数；不把 YAML、SQLite 或数据实例画成执行模块 |
| S1：证实的重复与死代码 | 删除 MCP 中未调用的 `definition_arguments`；三个进程 start-time 读取实现复用现有 spawn 模块中的纯读取函数 | 删除项没有动态注册或当前调用者；不增加模块/类/兼容包装；独立 spawn 启动与身份校验不退化 |
| S2：服务循环收口 | 删除没有当前调用者的 `Host.serve → Host._loop`，保留正式 `_command_serve → ApiServer.serve`，修正旧 P1/P2 注释 | 仅 ApiServer 管理服务循环；Host 保留组装、单次协调、claim/release；目录锁、启动声明、停止和恢复规则不变 |
| S3：核心与接入边界审查 | 逐入口确认 Api 定义/事实/计划/查询职责、Scheduler 计划提交、Controller 执行协调、协议适配及绑定组合 | 明确现存共用边界，记录保留理由；没有证实重复的核心不为缩短文件而拆成更多 Service / Repository |
| S4：首轮整体验收 | 核对删除/合并/保留清单，运行整合回归，确认发布文件范围 | 测试覆盖正式入口、状态推进、停止恢复和 Pi 接入；报告范围和未验证项，不将行数减少解释为性能提升 |

计划依据 S0 调用审查收敛为上述首轮范围：发现明确旧 Host 循环，因此删除；两个 Backend JSON 写入体虽相同，本轮保留，避免仅为八行复用添加持久化模块。Host/Server/spawn 的其他 JSON 写入语义不同，不能合并。Api 与 Scheduler 暂未发现可由本轮证据支持的大规模职责合并，不增加框架。

更深入的核心简化、依赖裁剪和资源测量属于后续工作；这轮交付不宣称代码已经达到最简，也不宣称已经降低运行内存。迁移前再对代表性工作负载建立部署/资源基线，不为一次清理执行完整语言选型研究。

### 验证方式

在本 worktree 安装项目开发环境后，使用：

```bash
python -m pytest tests/control_plane/test_api_server_p1.py tests/control_plane/test_backends.py tests/control_plane/test_w06_actual_paths.py -q
python -m pytest tests/control_plane/test_graph_v2_run.py tests/control_plane/test_progress_handoff.py tests/control_plane/test_acceptance_revisions.py -q
python -m pytest tests/control_plane/test_pi_integration.py tests/control_plane/test_pi_command_real_pi.py tests/control_plane/test_p3a_service.py tests/control_plane/test_p3b_trace_panel.py -q
python -m pytest tests/control_plane -q
```

前三条分别对应受影响切片，按实际修改选择；最终整合运行最后一条。先读已有测试覆盖再补必要反例，不为函数移动编写镜像测试。缺少 Node / Pi 导致的跳过须报告。v0.1 的旧通过记录仅证明基线；首批清理的 372 项结果保留于 `.workspace/slimming-final/`。当前最终源码已独立重跑全部测试，**403 项通过（159.21 秒）**，测试期间 runtime/tests/包配置摘要一致，结果位于 `.workspace/slimming-acceptance/`。

涉及进程身份/状态写入时，验证实际启动、退出后识别、恢复接管与停止，保留原有 fsync、权限和独立脚本执行要求。涉及 API/Scheduler 时，至少覆盖拒绝旧版本、重复请求、候选未接受、动态拆分、返工与停止恢复。跨版本数据测试只用副本或合成目录。

## 分工与完成定义

Codex 维护职责、保留/删除判断和基础接口；Qoder 按一个切片实现、自测并报告精确变更；Codex 独立读代码和验证，不将实现者的自述当成通过。同一文件只有一个作者。每次交付按既有规则进行独立 stop-that-shit 只读评审，并记录采纳/暂缓原因。

瘦身完成需要说明：减少了哪些重复职责或调用，保留了哪些必要边界，哪些外部行为被实测保留。文件数、类数或行数降低都不能单独充当完成判据。

## 后续独立方向

- 语言迁移：单独测量安装、依赖、空闲/执行资源与启动成本，再比较候选方案，确定目标语言与行为对照验收。必要时直接迁移已收口的接口，不要求把 Python 先重写一遍。
- 桌面 Codex 接入：让桌面 Codex 安装并使用同一 AgentGenome 服务。具体安装/连接机制需要核对其支持方式；本轮不声称已支持，也不是桌面自动操作。
- 更多 CLI Backend：Codex / QoderCLI / Claude Code / KimiCLI 的执行适配，与桌面客户端接入分别验收。
- Example 社区：确定可迁移 Graph 包所含结构、文件和依赖，再开展分发。

本轮不恢复旧实现、不更改模型与推理服务、不新建桌面 GUI、不选择迁移语言，也不自动发布。
