# Graph Builder

用一个 Graph 生成另一个 Graph /2 SOP 的 YAML。它只产出候选定义，不自动执行或发布生成的流程。

## 准备

按 [Pi 接入说明](../../docs/pi-extension.md) 安装，在可写工作目录启用 `/agentgenome`。Teacher 会准备 `sop-guide.md` 和 `sop-template.yaml`。将本目录的 `graph-builder.yaml` 复制到该工作区。

先和 Teacher 确认新业务的输入、输出、验收条件及允许使用的工具。当前 Builder 面向 Pi `read_file` / `write_file` 能力，不应生成未部署的 shell、网络、浏览器或原生 subagent 能力。

## 提交与导出

1. 将确认后的需求文本、`sop-guide.md` 和 `sop-template.yaml` 的实际内容分别经 `put_artifact` 冻结。
2. 校验并发布 `graph-builder.yaml`，保留返回的 `definition_ref`。
3. `create_run` 的输入键为 `requirements`、`guide`、`template`，每个值都是对应的完整 ArtifactRef。
4. 等待声明的 Pi Checker 验收，通过报告定位接受的 `application/yaml` 产物。
5. 用 `read_artifact` 读取真实内容并导出为 `deliveries/generated-graph.yaml`。
6. 对导出的文件调用真实 `validate_definition`。仅在结构与引用校验通过后，按用户要求发布；执行业务需要用户已经授权运行。

## 验收边界

Worker 与 Checker 都没有发布定义或运行 Graph 的工具。它们的接受结果是语义评审，不能代替 API 的结构校验，也不能据此宣称已完成业务。

本例已覆盖结构校验，尚不承诺真实模型能够生成合格 SOP。若生成物校验失败，保留原始已接受产物与错误，在派生副本中修改并重新校验；不要覆盖原件或冒充第一次产物已经通过。

这里产生的 Agent 是 Graph 内的 AgentDefinition / Task 数据，不是安装到全局 CLI 的 Agent 或 skill。
