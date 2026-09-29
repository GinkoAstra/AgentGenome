# AgentGenome

AgentGenome 实现参数化递归 SOP：从 Skill 保存原始要求、询问业务歧义、编制并检查候选，再绑定本次输入，执行固定能力或受约束 Pi 任务。运行可调用已有子图，也可在没有适用方法时组合声明能力生成子图，经检查后接续；父级仍独立验收。

当前注册领域为本地订单准备、清洗与客户报告：`orders.prepare`、`orders.clean`、`orders.report`、`orders.from_json`、`orders.prepare_configured`。任意措辞的 Skill 及明确本地引用可进入模型编制和独立语义审查；未支持的语义必须停止，不能当作背景丢掉。确定性订单夹具解析器保留作程序基线。

程序验证与真实模型验证分开：已有/生成子图、JSON 交接、任务内工具和故障恢复有可重复程序证据；旧后端 `bailian/glm-5.2` 的 HTTP 403 保留为历史证据。2026-09-29 已适配当前 Pi 0.87.1 和新配置模型 `idealab / bailian/deepseek-v4.1-flash`；首次真实探测被自动审批拦截，当前等待明确的数据发送授权，尚无新模型请求或有效任务样本。算法收益未测，整体目标未标为完成。程序行为由仓库内测试与验证脚本覆盖。

## 安装与程序验证

Python ≥ 3.11；新内核只用标准库，旧入口依赖 PyYAML。

```bash
pip install -e .
python3 -m unittest discover -s tests/recursive_sop -v
node --test tests/recursive_sop/pi_worker.test.mjs
python3 scripts/verify_recursive_sop.py --output /tmp/sop-recursive-evidence
python3 scripts/verify_sop_extensions.py --output /tmp/sop-extension-evidence
python3 scripts/verify_sop_parameters.py --output /tmp/sop-parameter-evidence
python3 scripts/verify_sop_semantics.py --output /tmp/sop-semantic-preparation
```

证据目录必须不存在或为空。第一个驱动验证 A 已有子图、B 运行中新图、固定对照各两批，B 第二批显式复用未发布候选；第二个验证 JSON 的已有/新图/固定三条路径，以及两批任务内工具执行；第三个验证参数修正后执行、无分支拒绝、修正次数耗尽三种结果。前三个驱动默认使用显式协议替身；语义驱动默认仅校验冻结原文与标签，零模型调用、状态not_run，不表示8例已通过。它们都不是实际模型验证。

安装后的 `agentgenome-sop` 与 `python3 -m sop` 等价。旧 `agentgenome`、`cli.py`、`builder`、`core` 保留历史入口，不承担本版恢复语义。

## 固定 CSV 或 JSON 流程

```bash
python3 -m sop definition --mode fixed --output /tmp/orders-fixed.json
python3 -m sop check /tmp/orders-fixed.json
python3 -m sop --data /tmp/orders-data run \
  --definition /tmp/orders-fixed.json \
  --input tests/fixtures/first-release/batch-01/input.csv \
  --rule highest_revision --backend none \
  --grant orders.profile orders.normalize orders.deduplicate orders.summarize
```

JSON 输入使用受检查的准备子图，不先去重或规范化。结果必须逐行逐字段与原 JSON 对应，检查通过后才给下游使用：

```bash
python3 -m sop definition --mode json-fixed --output /tmp/orders-json.json
python3 -m sop --data /tmp/orders-json-data run \
  --definition /tmp/orders-json.json \
  --input tests/fixtures/input-handoff/source.json \
  --rule highest_revision --backend none \
  --grant orders.prepare orders.profile orders.normalize orders.deduplicate orders.summarize
```

`definition --mode prepare` 可只执行准备，输出 prepared CSV 和 mapping，无需去重规则。`json-pi` 使用受约束 Pi 清洗。`--grant` 是本次能力授权；Skill 文本和定义检查报告不代替授权。固定路径不请求模型；模型不能修改注册脚本或验收器。

## 受约束参数填写

`prepare-configured` 固定执行同一转换程序，但要求填写完整 `column_map` 和 `chunk_size`。当前映射仅支持五个同名字段；分批大小实际影响序列化批次，不改变结果。宿主指定上限，候选不能修改程序、源数据、上限或检查规则。

```bash
python3 -m sop definition --mode prepare-configured --output /tmp/prepare-configured.json
python3 -m sop --data /tmp/form-data run --definition /tmp/prepare-configured.json \
  --input tests/fixtures/input-handoff/source.json --max-chunk-size 128 \
  --backend none --grant orders.prepare_configured
```

运行停在参数等待，返回 `run_id`、任务 ID、必填字段、约束及任务的 `parameter_revision`。将下面对象保存为 `/tmp/parameters.json`：

```json
{"column_map":{"order_id":"order_id","customer_id":"customer_id","revision":"revision","gross_cents":"gross_cents","refund_cents":"refund_cents"},"chunk_size":2}
```

```bash
python3 -m sop --data /tmp/form-data parameters RUN_ID TASK_ID \
  --values /tmp/parameters.json --message-id parameters-1 --revision PARAMETER_REVISION
python3 -m sop --data /tmp/form-data resume RUN_ID --backend none
```

参数提案的 revision 使用 **任务参数修订号**，不是运行事件修订号。部分合法字段可接受，但缺必填值时不执行；非法提案整包拒绝，不把合法部分偷偷提交。已被操作消费的参数不能修改。同 ID 重送返回原回执。

默认没有自动修正分支。需要执行前修正时，定义须预先声明 `parameter_validation/invalid_value → request_parameter_proposal`，仅能指定 `fields:["chunk_size"]` 和有限 `max_attempts`。修正与根预算、次数一起持久保存；夹带源引用或模板字段仍整包拒绝。完整例子可运行 `scripts/verify_sop_parameters.py`。

## 提交 Skill、回答和执行

先用已声明订单夹具验证确定性入口：

```bash
python3 -m sop --data /tmp/skill-data author \
  tests/fixtures/first-release/source-request.zh.md
python3 -m sop --data /tmp/skill-data draft-answer \
  DRAFT_ID dedup_policy highest_revision --message-id answer-1 --revision 1
python3 -m sop --data /tmp/skill-data run --draft DRAFT_ID \
  --input tests/fixtures/first-release/batch-01/input.csv \
  --backend scripted --library existing \
  --grant orders.profile orders.normalize orders.deduplicate orders.summarize
```

用第一条输出的真实 `draft_id`、`revision` 和问题 ID 替换占位符。启动直接引用同一候选、报告及有效答案，不必重填规则。`--library generated` 移除适用清洗方法，验证本次 planner 组合新图。`scripted` 明确用于程序验收。

真实文本编制须显式选择 Pi；此入口保存主文件及明确引用的本地资料，逐来源行提取要求，再用独立调用从原文核对语义，最后编译图与要求对应表：

```bash
python3 -m sop --data /tmp/semantic-data author /absolute/path/SKILL.md \
  --backend pi --max-model-calls 12
python3 -m sop --data /tmp/semantic-data draft DRAFT_ID
python3 -m sop --data /tmp/semantic-data draft-answer \
  DRAFT_ID dedup_policy highest_revision --message-id answer-1 --revision REVISION --backend pi
python3 -m sop --data /tmp/semantic-data draft-resume DRAFT_ID --backend pi
```

主文件允许 Markdown/文本，明确本地附件可为文本、JSON/YAML 或脚本文本；不执行导入脚本。来源缺失、越界路径、链接、未解释约束或独立审查未完成均不能交付。来源/附件、有效答案、定义、依赖或检查策略改变，使旧报告失效并保留历史。模型后端失败不回退夹具解析器。

语义草稿可在原预算内显式修订，并再次独立审查；原版本报告、拒绝原因和已绑定答案保留。材料已改变须重新提交，不能用修订绕过来源检查。取消可在模型调用期间登记，迟到结果不能恢复草稿：

```bash
python3 -m sop --data /tmp/semantic-data draft-revise DRAFT_ID \
  --request-model --message-id revise-1 --revision REVISION --backend pi
python3 -m sop --data /tmp/semantic-data draft-cancel DRAFT_ID \
  --message-id cancel-1 --revision REVISION
```

修订也可用 `--proposal /absolute/path/analysis.json` 替代 `--request-model`，但仍需独立审查后端。取消已交付草稿只返回已交付状态，不修改已接受运行。

确定性夹具入口只识别文档中的完整语句，支持 `去重规则：highest_revision。` / `去重规则：first。` 和 `执行模式：fixed。` / `执行模式：pi。`。它不用于衡量自然语言理解能力。

## 查询、问答、恢复与取消

```bash
python3 -m sop --data /tmp/orders-data status RUN_ID
python3 -m sop --data /tmp/orders-data events RUN_ID
python3 -m sop --data /tmp/orders-data answer RUN_ID QUESTION_ID highest_revision \
  --message-id answer-2 --revision REVISION
python3 -m sop --data /tmp/orders-data resume RUN_ID --backend scripted --library existing
python3 -m sop --data /tmp/orders-data cancel RUN_ID
python3 -m sop --data /tmp/orders-data export RUN_ID --output /tmp/orders-results
```

同消息同内容重送返回原回执；过期、冲突或跨实例回答拒绝。已接受方法、已提交候选和子结果在原位置续接。动作有意图却无可靠候选时进入 `effect_unknown`，不会因为缺少结果文件而重放。取消可在模型运行期间登记，在安全边界阻止新动作，不撤销既有效果。

D05 默认失败停止；仅定义预先声明且唯一匹配的安全重试或同候选重验可继续，次数和根预算不会因重启清零。候选只保存为未发布状态，换输入可显式复用。

## 真实 Pi 与任务内工具

使用已安装且版本一致的 Pi agent-core / pi-ai / coding-agent，显式支持 `0.80.6` 和 `0.87.1`；不会自动安装或回退其他模型。默认读取本机 Pi 配置，亦可显式设置 `SOP_PI_PROVIDER` / `SOP_PI_MODEL`。凭据不进入模型上下文和证据。0.87.1 使用新版 ModelRuntime 与 finishTurn 接口，初始化不刷新远程模型目录，交回或预算结束后不再发请求。

执行 agent 可用 `context`、`capability`、`handoff`。`capability` 请求由 Python 宿主逐次检查 run、定义和当前任务的权限交集、逻辑产物的归属/角色及有效规则；先持久意图再执行，提交回执后才返回逻辑句柄。模型不能获得 Store、任意文件路径、shell 或业务网络工具。相同已提交工具请求复用回执；工具候选不推进图步骤，最终结果仍须独立验收。

编制、语义审查、planner 只有 `context/handoff`。这是模型工具边界，可信 Python/Node 插件没有额外 OS 沙箱。实际模型调用和 Token 在证据中记录；`usage.model_calls` 是最大请求预留，不能当作实测用量。

确认模型端点的数据发送授权后，按已配置模型分别验证（以下选择仅作用于本终端进程，不改全局默认）：

```bash
export SOP_PI_PROVIDER=idealab
export SOP_PI_MODEL=bailian/deepseek-v4.1-flash
python3 scripts/verify_recursive_sop.py --backend pi --output /tmp/sop-real-recursive
python3 scripts/verify_sop_extensions.py --backend pi --output /tmp/sop-real-direct
python3 scripts/verify_sop_semantics.py --backend pi --output /tmp/sop-real-semantics
# 仅在该案例实际交付后执行同一草稿的完整交接：
python3 scripts/verify_sop_handoff.py --semantic-case /tmp/sop-real-semantics/case-02 \
  --backend pi --output /tmp/sop-real-handoff
```

递归驱动禁用任务内业务工具，保证实际经过 A/B 子图；直接工具驱动要求无子图。后端错误停止，不回退替身。语义驱动使用[8例冻结原文与独立角色标签](tests/fixtures/semantic-authoring-v1/README.md)，只有正确问题出现后才给模拟答案；任意报错不算正确拒绝，完整草稿、审查与失败均保留。交接驱动从持久草稿校验同一定义、报告及有效规则，以两批输入走已有/生成/候选复用，不另造根定义；当前只接受pi模式与明确highest_revision规则。失败保留逐例证据和未运行分母，源语义证据保持只读。该开发集不用于算法收益外推。Node 测试即使使用实际安装的 Pi loop，也因使用脚本模型传输而属于程序证据。

## 数据与边界

`.data/authoring` 保存原文快照、草稿、问题、答案和报告；`.data/objects` 保存不可变定义与产物；SQLite 保存运行、调用、动作、事件和回执；`.data/work` 为各动作独立目录。定义不含本次路径和结果。

CSV 使用 UTF-8、精确表头、LF/末尾换行、ASCII 整数。JSON 是包含相同五字段的对象数组，标识符为字符串、修订与金额为整数，禁止布尔/浮点或猜测转换。父级独立复算明细、汇总和质量；正确合计不能掩盖错误明细。见[执行边界](sop/B3.md)与[协议](sop/PROTOCOL.md)。

任意新脚本生成、未声明领域的参数含义推断、正式发布流程、外部副作用自动对账、多 worker 和旧断点迁移仍未实现。真实自然语言准确率、拆分质量、速度或 Token 收益均未验证。

[MIT](LICENSE)
