# Harness 派发前持久提交缺陷：DeepSeek 修复任务书

日期：2026-10-15。核对基线：`81a2f05`。状态：**修复方案，尚未实施**。

目标：修复已复现的 Harness 持久化接线缺陷，让“工具派发前已保存完整模型响应与执行位置”在真实 `DefaultHarness + SqlitePersistence` 路径成立。

这是一轮恢复正确性修复。沿用现有 Core reducer、单一 Runner、Persistence 和 Gateway；不新增 Agent Loop、操作账本、subagent 或持久任务队列。

## 1. 已确认的缺陷与基线证据

### 1.1 缺陷落点

- `runtime/default_harness.py::_state_sink` 返回同步回调，只把 `ExecutionState` 序列化后放入内存 `holder["record"]`。
- `core/runner.py::Runner._drive` 在执行动作前等待 `state_sink`，但当前等待到的是“内存替换完成”，不是“数据库事务提交成功”。
- `DefaultHarness._emit_core_event` 只处理 fence、内存消息和观察者通知，不会替快照提交数据库。
- 真正的数据库写入由 `_commit` 调用 `Persistence.commit_transition` 完成；当前进度回调没有走到这里。

因此，Core 提交端口本身可用，并不等于具体 Harness 已经正确连接持久层。

### 1.2 已执行的真实进程复现

使用脚本化模型与真实 SQLite，不请求外部模型：

1. 子进程通过 `DefaultHarness.start` 发起运行，模型返回 `probe.write` 调用 `c1`。
2. 工具处理器将一条效果记录写入文件，`flush + os.fsync` 后执行 `os._exit(9)`。
3. 另一进程新建 `SqlitePersistence`，读取同一个 `run_id`。

基线实际结果：

```text
child_exit = 9
effect_exists = true
stored_status = running
stored_execution = null
stored_messages_count = 0
stored_batch = null
```

该探针使用直接注入的 `ToolSpec`，用于单独核对 Harness 的快照提交，不证明 Gateway 账本丢失，也不覆盖网关的副作用保证。修复测试还要增加经 `core_tool -> ToolGateway` 的集成场景。

### 1.3 现有门禁为什么没发现

- 基线本地门禁实测为 **773 passed / 1 skipped，6/6 检查通过**。
- `tests/test_core_commit_port_crash.py` 证明的是低层 Core 加自定义落盘 sink 的行为。
- `tests/test_runtime_execution_snapshot.py` 覆盖暂停/结束后的快照，以及手工构造的运行中快照，缺少在真实 Harness 的工具执行窗口内观察 SQLite 的断言。

修复必须先补真实 Harness 路径的红测，不可用更多手工构造 `ExecutionState` 的测试替代。

## 2. 开工规则与范围

1. 重新检查 HEAD、工作区、适用仓库指令及真实源码。若有 `.codegraph/`，先使用 CodeGraph 理解相关路径；不要自行重新索引。
2. 保留接手时已有改动，尤其是本次生成的架构说明。不要 reset、清理或覆盖其他人的文件。
3. 下文函数名与行号依据上述基线，允许小范围调整实现落点，以实际契约为准。
4. 生产改动优先限制在 `runtime/default_harness.py`；确需调整 Core 回调边界或存储端口时，记录原因并补对应契约测试。
5. 本轮不承诺任意外部副作用 exactly-once、不实现半截模型流恢复、不把内存 steering/follow-up 改造成持久 inbox。
6. **本轮完成不等于 `unknown` 已解决，也不等于 safe-replay 闭环完成。** `unknown` 由谁、凭什么证据收敛为 `executed/failed`，仍是项目最高优先级（P0）的独立后续事项；责任人与证据机制待单独立项确定。本轮只要求保留证据并守住拒绝盲重放的边界，不得以缺少 receipt、超时或进程退出推断“没有执行”。

## 3. 修复后必须成立的契约

| 边界 | 必须持久可读的事实 | 后续约束 |
| --- | --- | --- |
| 请求模型之前 | 请求阶段、已有上下文、稳定模型动作引用与逻辑预算 | 提交失败不得发出该模型请求 |
| 完整模型响应之后、工具派发之前 | 最终 assistant 消息、完整工具调用声明、执行阶段、轮次及关联信息 | 提交成功后才允许调用工具；半截参数不进入执行依据 |
| 已结算批次之后、下一次模型请求之前 | 真实工具结果与消息顺序、批次结果或可核对的引用 | 不把已完成结果丢回未知，也不重复执行已确认完成的操作 |
| 对外宣告持久暂停之前 | 一致的 execution、batch、pending_interaction、消息、预算与暂停事件 | 新进程立即读到一致状态，不依赖驱动返回后再补一笔事务 |
| 对外宣告最终完成之前 | 最终 RunStatus、结束原因、消息、预算与终态事件 | 终态事实由单一负责者提交；晚到回调不得覆盖 |

数据库中的 `RUNNING_BATCH` 只证明这些调用已经声明，不能独自证明处理器已经执行或尚未执行。恢复仍依赖 Gateway 的 intent/receipt 与宿主证据。

## 4. 建议实现方式

### 4.1 将状态回调接到真正的提交端口

增加或复用一个异步进度提交方法，由 `_state_sink` 返回的异步回调等待它完成。命名可以是 `_commit_execution_progress`，不作为新增公共 API。

推荐流程：

```text
Runner 接受迁移
  -> 构建候选 RunRecord
  -> 序列化并校验快照及消息
  -> commit_transition(expected_version, lease, state, 必要事件)
  -> 获得提交后的版本
  -> 更新本次驱动的内存视图
  -> 执行该迁移的模型或工具动作
```

- 复用 `_commit` 和现有 `Persistence.commit_transition`，携带 `holder.version` 与 `holder.lease`；不要旁路调用 SQLite 写表。
- 可以小范围调整 `_commit` 的候选记录传参方式，使提交失败时不会把未提交候选误认为权威状态。
- 以执行边界为提交单位，不按每个 token/delta 写整份 transcript。
- 原始 provider 材料可存在私有快照；不要把整份快照当公开事件 payload 发布。
- 不让 Core 导入数据库或审批层。Core 只等待宿主提交端口。

### 4.2 同步维护消息与预算，修掉“保存后再追加一遍”的陷阱

当前 `_apply_result` / `_merge_transcript` 的规则是“已有消息 + 本次输出”。一旦进度提交已经保存本次输出，这个规则会重复追加。

实施前明确一个权威规则，建议：

- 正常进度提交时，`ExecutionState.messages` 是该时点的完整已提交 transcript；`RunRecord.messages` 与其保持一致，不再把完整历史当增量附加。
- 正常收尾优先使用最终完整状态映射；旧入口仍按已有返回类型工作。
- 若选择增量合并，必须记录明确的位置/边界，区分已有前缀与本次新增消息。
- 不按内容相等去重。同一句用户输入、同样的模型回答，都可能合法地出现多次。
- 取消/超时路径不一定有正常终态快照：核对 `_run_task`、`_interrupted_result` 与 `scope.messages`，只合入实际未保存的部分，保留既有中断消息语义。
- `turns_used`、原 `max_turns` 和 `attempt_id` 与当前快照一致；恢复不能重置逻辑运行预算。

必须增加“执行多轮后正常完成”和“暂停后恢复完成”的消息条数、role 顺序、tool_call_id 一一对应断言。

**F07 还必须验证相对序列一致，而不只验证 ID 配对正确。** 同一模型响应声明调用 `[c1, c2, c3]`，即使处理器按 `[c3, c1, c2]` 完成，持久 transcript 与下一次实际交给 provider 的工具结果也必须按 `[c1, c2, c3]` 组装，并逐项核对结果内容。用明确握手构造乱序完成；测试同时覆盖正常执行和暂停恢复后的结果组装，不能只比较 ID 集合或内部 batch。尚未取得结果的调用不能为了补齐序列而伪造成功/失败。

这里将保序作为 Harness 的确定性契约。Provider 格式另须满足各自协议：Anthropic 要求按 `tool_use_id` 配对，在紧随其后的 user 消息中集中回传结果，且结果块位于文本之前；OpenAI 使用 `tool_call_id`（Chat Completions）或 `call_id`（Responses）关联结果。所核对的官方文档未建立“两家 API 均因结果相对乱序必然返回 400”的通用结论，不以该说法作为验收依据。参见 [Anthropic 结果格式](https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls) 与 [OpenAI 函数调用](https://developers.openai.com/api/docs/guides/function-calling)。

### 4.3 将暂停与终态提交收敛到明确负责者

当前 `_make_suspend_hook` 会在 reducer 接受 `ToolBatchSettled` 之前提交暂停；此时 holder 中的 snapshot 可能仍处于派发阶段。只增加进度写库，会暴露这个相邻窗口的一致性问题。

建议让暂停 hook 收集批次事实和宿主决定，由拿到完整暂停状态的一方完成权威暂停事务。具体重排由实现者选择，但必须满足：

- `RunRecord.status`、`execution.phase`、批次分区和 `pending_interaction` 在暂停事务中相互一致。
- 不先写入 `SUSPENDED + 旧 RUNNING_BATCH 快照`，再依赖下一次写入修正。
- `resume` 校验 `_assert_execution_snapshot_agrees` 继续有效；不要通过删除校验掩盖分歧。
- `approval_requested`、`run_suspended`、`run_finished` 等持久事件与对应状态一起提交，提交后再通知持久事件观察者。
- 核对现有 `_commit_final` 和 `suspended_committed` 的职责，避免新回调与原收尾各创建一次相同的持久事实。
- 普通 Core 流式观察事件保持既有顺序契约；它们不自动等同于持久提交确认。
- 暂停可发生多次，终态只出现一次；不要把一次暂停当成整个逻辑运行结束。

### 4.4 正确处理恢复期间的运行状态

当前 `resume` 在继续驱动时，holder 的记录可能仍带上一次 `SUSPENDED` 状态。增加运行中提交后，需要显式处理这一点：

- 在新 attempt 真正推进前，按既有状态迁移规则转换为 `RUNNING`，同步当前 attempt、上下文、预算与已解除的交互。
- 同一逻辑调用保留 namespace/session/run 和稳定 operation ID；新 attempt 不得绕过 Gateway 去重。
- 原审批、配置与工具集指纹校验继续执行；快照存在不代表授权成立。
- 已完成结果复用；等待审批仍等待；无法证明的外部操作保持 unknown/reconciliation。

**关联恢复契约核对**：当前 `_recover_orphan` 可把无批次的运行改成 reconciliation 暂停，而普通 `resume` 又要求 `record.batch` 存在。新快照持久化后，必须明确这些窗口如何处理，不能只声称“有快照，所以可自动恢复”。

本轮要求：实际支持的恢复窗口有真实测试；尚不支持的窗口保留证据、返回可辨认的限制并写入交接记录。若提供从 orphan reconciliation 继续的入口，必须从可信账本/宿主证据建立合法批次，不得伪造结果或接受客户端布尔值作为成功证据。

### 4.5 提交失败、租约失效与并发取消

- 进度提交失败立即停止后续模型/工具派发，保留存储错误的性质，不改写成模型业务错误或成功。
- CAS/fence 冲突不能通过去掉 `expected_version`、清空 lease 或盲目覆盖新版本解决。
- 并发写入的取消请求、已经存在的终态必须被保留；进度快照不能把它们覆盖掉。
- 如果事务可能已提交但 await 被取消，不假称事务一定回滚。后续从数据库权威状态重新核对；当前驱动不得继续派发。
- 租约/CAS 能拒绝过期提交，不代表可以撤回已开始的远程副作用。测试租约在提交前失效时不得派发即可，不夸大保证。

## 5. 实施顺序与逐步验收

### D0：先补能证明接线错误的红测

建议新增 `tests/test_runtime_commit_boundaries.py`，真实子进程场景复用/扩展 `tests/fault_windows.py` 与 `tests/child_process.py`。

最少先完成 F01、F02（见下表），记录在基线上的失败输出。不要先改生产代码，再用能过的新断言证明自己。

### D1：接通异步进度提交与 transcript 映射

实现 4.1/4.2/4.5。验证写库发生在外部动作之前，错误会阻止派发，正常结束没有重复历史。保留同一 Runner。

### D2：统一暂停、恢复与终态边界

实现 4.3/4.4，核对审批暂停、重复暂停、预算、取消、监听器失败和孤儿运行的支持范围。不得只修 start 而遗漏 resume 使用的同一个 sink。

### D3：完整门禁、证据与文档

修复后运行第 7 节命令。更新真实结果与仍未支持的窗口；同步架构说明中的红色缺口，不提前改成“已修复”。

## 6. 必需回归矩阵

下表是目标测试，不表示已经实现或通过。名字可调整，验证含义不可弱化。

| 编号 | 场景与实际路径 | 核心断言 |
| --- | --- | --- |
| F01 | 真实 Harness + SQLite；工具写效果后 `os._exit` | 另一进程能读到 `RUNNING_BATCH` 快照、最终 assistant、声明 `c1`、已用轮次；外部效果存在，未结算调用不得被描述为成功或可盲重放 |
| F02 | 在 handler 调用前的 `commit_transition` 端口注入一次受控失败 | 已接受初始运行；握手确认到达工具边界提交端口且 handler 尚未进入，再触发失败；驱动失败退出后 handler 调用仍为 0、效果文件不存在、不发下一模型请求；存储失败可辨认 |
| F03 | provider 开始接收请求时，从独立连接读取 SQLite | 请求阶段已持久可见，快照可反序列化；使用持久存储而非只观察 holder |
| F04 | 工具边界提交成功后、处理器开始前退出 | 声明与阶段可读、效果不存在；仅凭快照不擅自断定 Gateway 操作是否开始；恢复遵守证据规则 |
| F05 | 经 `core_tool -> ToolGateway` 执行并结算后，在后续模型动作处退出 | 真实 receipt 保留，相关结果可核对；修复没有新增第二份操作账本；恢复不重新调用已确认完成的 handler |
| F06 | 审批暂停刚提交、驱动尚未完成收尾时退出 | snapshot、batch、interaction、消息和事件一致；新进程能按已有审批恢复契约推进，未批准时无写入 |
| F07 | 正常多轮、暂停恢复、多次暂停以及预算耗尽；握手控制并发乱序完成 | transcript 不重复、不按内容误去重；ID 与结果逐项配对正确，持久 transcript 及实际回传 provider 的结果 ID 相对序列均与该轮调用声明一致；逻辑预算不重置 |
| F08 | lease 失效或 CAS 冲突发生在进度提交前 | 不再派发模型/工具，不覆盖新取消或终态；其他 namespace/run 的状态不受影响 |
| F09 | 提交后通知失败、取消/超时、重复入口 | 已提交事实仍可补读；中断历史不重复；终态事件不重复、不被旧回调复活 |
| F10 | 缺失快照的旧记录与未知 schema | 旧记录按已声明兼容路径处理；未知/损坏格式明确拒绝，不通过扩版本列表假装兼容 |

子进程测试要求：

- **故障注入边界**：F01/F04 验证崩溃恢复，必须在真实子进程用 `os._exit` 触发退出，不能以异常 mock 替代；F02 验证提交失败后的控制流，允许仅在 `commit_transition` 端口注入一次受控失败，仍走真实 Harness 与 SQLite 路径。F02 的注入点必须严格位于 handler 调用之前，以显式检查点/握手确认 handler 尚未进入，并在驱动失败退出后核对调用计数与外部效果；不能靠 sleep 推测窗口，也不能用替换 handler 为抛异常来证明“不执行”。
- 使用真实 `DefaultHarness`、真实 `SqlitePersistence`，以及实际工具调用；不能直接给低层 Runner 换一个自定义 fsync sink 充当集成验证。
- 用明确检查点/握手触发退出，不靠随机 sleep 猜窗口。副作用用独立文件或业务表计数，跨进程仍能观察。
- 子进程有超时、资源与连接能清理；父进程不能读取子进程内存变量作为证据。
- lease 过期使用既有测试时钟/同步机制或有界等待，不改生产代码关闭 fence。

## 7. 验证命令

先运行新增且已确认能复现缺陷的目标测试，再运行相关集成测试和完整门禁。以下为修复后的目标命令：

```bash
.venv/bin/python -m pytest -q tests/test_runtime_commit_boundaries.py
.venv/bin/python -m pytest -q tests/test_runtime_execution_snapshot.py tests/test_runtime_resume.py tests/test_runtime_recovery.py tests/test_runtime_isolation.py
.venv/bin/python scripts/ci_check.py --offline --wheelhouse build-wheelhouse
```

完整本地门禁已经包含全套包测试、冷导入、安装产物与示例、文档链接及来源文件检查；通过后不为凑次数重复跑同一套。

若修复调整公共类型、持久格式或发布文档，另执行并保存详细发布证据：

```bash
.venv/bin/python scripts/verify_release.py --offline --wheelhouse build-wheelhouse --out build/durable-commit-fix-release
```

wheelhouse 与解释器必须匹配。远程 CI、Python 3.10、Linux/Windows、真实模型分别记录是否真实执行，不能拿本地 3.12 结果代替。

## 8. 完成交付物

1. 小范围生产修复与真实 Harness 回归测试；先红后绿证据。
2. 说明进度、暂停、恢复、终态分别由谁提交，以及失败时为何不会继续派发。
3. F01–F10 的实际落点/结果；未支持项写明边界与后续处理方式。
4. 更新 [验证记录](verification.md)、[状态机迁移表](core-state-machine-migration.zh-CN.md) 和 [当前架构图](current-harness-architecture.zh-CN.md)。
5. 保留 pi 来源说明与 MIT 文件；不改变单一 Runner 的结构。
6. 交接记录单列 P0 后续事项：`unknown -> executed/failed` 的判定责任人、可信证据来源、核对入口与审计规则仍待确定；证据不足时继续保持 unknown。即使 F01–F10 全部通过，也只能宣告本轮持久提交边界修复完成，不能据此关闭该事项或宣告 safe-replay 闭环完成。

建议分为两个可审查提交：

- `fix: persist harness execution before dispatch`：生产修复与必要回归测试。
- `docs: record durable commit boundary verification`：真实验收记录、架构图与剩余限制。

交付报告不能只写“测试全绿”。至少回答：工具开始前数据库里有什么；进程退出后另一个进程读到了什么；哪些调用可以重用结果，哪些仍须核对；正常/恢复完成后消息是否只保存一次。

## 9. 给 DeepSeek 的执行提示

> 请按本任务书修复 `DefaultHarness._state_sink` 只更新内存、未在外部动作派发前真正提交 SQLite 的缺陷。先核对当前 HEAD 与工作区，再补真实 `DefaultHarness + SqlitePersistence + 子进程退出` 红测；随后接通异步进度提交，处理 transcript 去重边界、暂停/终态提交一致性、恢复预算、lease/CAS 与取消。保持单一 Runner 与既有 Gateway 操作账本，不扩展多任务或 subagent。按 F01–F10 逐项交付实际证据，更新验证记录和架构图；不支持的恢复窗口明确写出，不以测试总数代替该窗口的验证。无需发布包、推送或部署。
