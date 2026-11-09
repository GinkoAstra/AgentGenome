# CLI Adapter 实施计划（用户已批准）

2026-11-07。分支 feat/cli-adapters；工作区 ../AgentGenome-cli-adapters。
基线：v0.1 + AgentGenome-slimming 已验收未提交改动的精确复制；78项摘要一致，来源记录 .workspace/cli-adapters/inherited-baseline.json。原瘦身工作区不修改，不自动提交发布。

## 范围与顺序

优先级：CLI执行适配优先于语言迁移。Codex CLI、Qoder CLI、Claude Code、Kimi CLI 全部在范围内；Pi/Script参与回归。桌面客户端接入另行验收。
1. 四种CLI能力/版本/协议核实；不以命令帮助代替执行证据。
2. Codex定义基础框架与契约，Qoder具体实现。复用AgentBackend生命周期，不向Scheduler/API添加CLI业务分支。
3. Worker system tools经同一网络API提交事实；原生/桥接工具不得扩大WorkerSpec授权。
4. 每个Adapter验证执行、候选、退回继续、接受、停止、重启/不确定状态、重复投递及trace；Pi/Script回归。
5. 独立STS评审、源码审查及测试后接受；模拟/真实CLI/真实模型业务三层证据分开。

## 不变量

一轮结束不等于Node完成；候选必须被父/Checker接受。AgentBackend.start仅准备，不调用模型。绑定由run/node/worker/epoch/delivery固定。接收消息不等于进入会话。恢复已知会话不等于重放外部副作用。不支持的硬预算、工具权限、会话或后台控制必须明确拒绝，不能仅靠提示词保证。

## 实施记录

当前：三种 CLI 适配代码已完成，455 项独立回归通过；按证据范围部分接受，见 [当前交付状态](CLI_ADAPTER_STATUS.md)。本机codex0.161.0/qodercli1.1.82/claude2.1.143；kimi不在PATH，尚未实现。运行环境不升级、不更改全局配置与用户服务，不复活旧adapters目录。

### 已接受的基础框架

- `backends/preparation.py::WorkerPreparation`：复用冻结材料、工具授权及提示装配；从 Pi 机械提取的 15 个方法保持行为一致。
- `Observation` 的调用次数允许未知；公开 `usage` 只包含有观察依据的计数，`usage_lower_bound` 保留持久累计下界，供 Controller 和 Scheduler 继续执行预算约束。未知不等于零，不重置预算。
- 基础框架的独立回执 `.workspace/cli-adapters/base-framework-accepted.json`，30／11／26 项对应定点验证已通过。此回执不接受未完成的 CLI 适配代码。

### 当前实现与验收分工

Qoder 填充 `cli.py`、`cli_protocol.py`、`worker_mcp.py`、Host 组合注册及其测试。Codex 独占基础框架、`test_cli_review_acceptance.py`、真实流程验收脚本及独立回执。每个文件只有一位作者。

原生会话续接仅承诺当前本机会话的恢复；本轮不提供 CLI 历史会话快照／任意外部副作用重放，`checkpoint` 不可宣称支持。CLI 无法可靠在发起请求前拦截 provider 次数时，拒绝相应硬预算，真实次数保持未知。

已发现的实际缺陷和修正要求记录在 `.workspace/cli-adapters/review-inbox.md`；已修正并通过独立 STS 闭合复核，不沿用基础框架通过的测试数宣称新适配已完成。独立检查包含真实 stdio MCP 协议验证、完整执行身份、零工具隔离、主进程先退出的进程组清理，以及完整候选→退回→同会话续接→接受路径。

### 环境与证据范围

- Codex／Qoder 已通过真实模型完整候选→退回→原会话续接→接受流程，独立程序 Checker 核验实际产物。此结果证明最小验收闭环，不推定所有业务 SOP 均已验证。
- Claude Code 已安装，现有上游连续 HTTP 502；真实模型业务验收待可用环境；真实 CLI＋本地受控模型的完整协议闭环已通过，不重复无效调用或修改全局配置。
- Kimi 未在 PATH 发现；安装位置／隔离安装选择等待用户回复。官方当前 `MoonshotAI/kimi-code` 与旧 Python `kimi-cli` 协议不同，已保存官方源码版本 `eb2035f3cad57cc12d4d5082ec626f7f0ea5f651` 的参考资料于 `.workspace/cli-adapters/probes/kimi-reference/`。此参考不是已运行或已适配的证据，Kimi 仍属于本轮未完成范围。
