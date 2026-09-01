# AgentGenome

> 状态：早期开发中（WIP），接口与语义可能变化。

AgentGenome 是 LLM agent 任务的结构化编排/执行框架：把需求编译成带 `verify` / `on_fail`
声明的 YAML 执行图——如同在"诞生"时固定的基因组，由 CORE 引擎执行并做机器验收，
产出 append-only 台账（JSONL）与断点，运行可 resume、可复放、可审计。

## 特性

- **建图层（builder/）**：需求 → YAML 图，编译期钉死 verify，运行期不可降级
- **CORE（core/）**：执行 + 验收 + 运行保障；纯文件存储（YAML + JSONL + JSON），
  不依赖数据库或服务进程
- **业务层（cli.py + templates/）**：`init` / `check` / `run` / `resume` / `status`
  五个命令
- **接口层（adapters/）**：local shell、Kimi CLI、终端人工确认等端口实现

## 安装

要求 Python ≥ 3.11，运行时仅依赖 PyYAML：

```bash
pip install -e .
```

## 快速开始

```bash
# 生成一个骨架图
agentgenome init my-task

# 编译期检查
agentgenome check my-task/graph.yaml

# 执行
agentgenome run my-task/graph.yaml

# 中断后续跑 / 查看状态
agentgenome resume <run-id>
agentgenome status <run-id>
```

可运行示例见 `templates/data-cleaning/`（图 + 脚本）。

## 引用 / Citation

如果你在研究中使用了 AgentGenome，请引用。配套论文发表后，此处会更新为论文的
正式引用信息；目前请先引用本仓库：

```bibtex
@software{agentgenome2026,
  author = {GinkoAstra},
  title = {AgentGenome: Verification-Pinned Executable Task Graphs for LLM Agents},
  year = {2026},
  url = {https://github.com/GinkoAstra/AgentGenome}
}
```

## 开发

```bash
pip install -e . pytest
pytest tests/ -q
```

## License

[MIT](LICENSE)
