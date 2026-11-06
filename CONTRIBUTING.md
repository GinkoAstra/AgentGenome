# Contributing to AgentGenome

当前运行代码位于 `node_runtime/control_plane/`。开始前阅读 [README](README.md) 与 [架构说明](docs/architecture.md)，并使用独立分支处理改动。

## 开发环境

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e . pytest
.venv/bin/python -m pytest tests/control_plane -q
```

Node.js 探针与真实 Pi RPC 集成测试分别需要 Node.js 和 Pi。缺失工具导致的跳过应明确说明，不能视为集成已通过。测试不需要模型凭据；真实业务验收应另行配置模型、记录输入及接受的输出。

## 设计约束

- API 管权威状态、权限、事务与版本；外部客户端通过网络 API 调用，不能打开服务 SQLite。
- Scheduler 解释依赖和结构化执行事实，提交执行意图；Controller 协调执行，Backend 管理实际实例。
- Node / Worker 共用递归任务模型；Agent 内部的一轮交互不等于任务完成。
- 候选提交与验收接受分开。业务规则写在 Task / Checker，不在通用 Scheduler 增加行业分支。
- 修改冻结契约应产生新版本，不能覆盖历史输入、接受的产物或版本身份。
- 不把程序测试通过等同于真实模型业务效果；不把会话恢复等同于外部副作用可重放。

## 提交说明

说明具体问题、修改后的行为和相关验证结果。涉及接口时给出调用者、输入输出、状态变化及失败路径；同时更新文档与受影响示例。保持单次变更范围清晰。

请勿提交模型凭据、运行数据库、个人目录快照、会话日志、生成产物或本机虚拟环境。测试夹具应采用可以公开的合成数据。新增第三方内容应注明来源与适用许可证，并保留既有归属信息。
