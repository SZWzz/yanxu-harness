# Core 执行状态机迁移表（S0 冻结）

核对日期：2026-10-15。本文是 [Core 执行骨架演进计划](core-state-machine-implementation-plan.zh-CN.md) 的 S0 交付物：
把现有双层循环的每个分支冻成「输入 / 原状态 / 动作 / 输出事件 / 返回结果」契约行，记录基线数量与本轮
兼容策略，并列出必须在后续阶段修掉的行为缺口。**S0 不修改任何生产执行逻辑。**

## 0. 核对结果（不是计划假设，是实测）

| 项目 | 实测值 |
| --- | --- |
| HEAD | `1c9ec7a`（计划书写的是 `9b16841`，已前进；`a2c352f` 及其后的 Alpha/Harness 工作都已合入） |
| 工作区 | 干净（`git status --porcelain` 为空），无未提交改动需要保护 |
| Python | `.venv` 为 CPython 3.12.13；仓库声明 `>=3.10`，`tests/test_python_floor.py` 静态守卫 3.10/3.11/3.12/3.13+ API |
| 基线（`python -m pytest -q tests`） | **657 passed, 1 skipped** in 28.49s |
| 基线（`python -m pytest -q tests/core`） | **193 passed** in 0.25s |
| 唯一跳过 | `tests/test_skills_local.py:267`，理由 `only meaningful when the extra is absent`（环境装了可选 extra） |
| 既有失败 | 无 |
| 证据文件 | `build/s0/baseline-core.txt`、`build/s0/baseline-all.txt`（工作区产物，不入库） |

计划中「Provider/Core、Gateway/Core 桥接与流式线程桥仍在开发中」的描述**已过期**：`providers/core_bridge.py`、
`tools/core_bridge.py` 都已实现并且有契约测试。计划要求的「先有可重复的模型—工具桥接和审批恢复基线再做主重构」
这一前置条件已经满足，S2 可以开始。

### 已经存在、本轮直接复用的能力（不要造第二套）

| 计划条目 | 现有落点 | 状态 |
| --- | --- | --- |
| S1 结构化结果与结束原因 | `core/outcome.py`：`RunStatus` / `LoopStop` / `LoopResult` / 状态迁移表 | 已实现 |
| S1 结构化入口 | `core/agent_loop.py`：`run_agent_loop_result` / `run_agent_loop_continue_result` | 已实现 |
| S1 暂停出口 | `AgentLoopConfig.should_suspend_after_batch` → `LoopResult.pending_interaction` | 已实现 |
| S1 工具结果状态分区 | `tools/core_bridge.py`：`PENDING_STATUSES` / `suspension_reason`；`runtime/batch.py` 的 call 分区 | 已实现 |
| S4 持久提交 / 恢复 | `runtime/default_harness.py`（start/resume/cancel/state/replay、lease/CAS、checkpoint、reconcile） | 已实现 |
| S5 取消与身份隔离 | `runtime/envelope.py` 的 fence、`_run_task` 的 task 级取消、`test_runtime_isolation.py` | 已实现 |
| S2/S3 显式状态与单一 Runner | **不存在**：`_run_loop` 仍用隐式局部控制流；`docs/verification.md` 已如实登记「未实现」 | **本轮工作** |

### S1 之后仍然存在的缺口（S0 复现，对应阶段修）

| 缺口 | 现状（实测行为） | 修在 |
| --- | --- | --- |
| 旧列表入口吞掉暂停 | **已修**：`run_agent_loop` / `run_agent_loop_continue` 在 `status == SUSPENDED` 时抛 `LoopSuspended`（携带可读 `LoopResult`），不再静默返回列表。复现：`tests/core/test_core_execution_outcomes.py::test_the_list_entry_raises_a_control_exception_carrying_the_pause` | S1 ✅ |
| `Agent` 没有结构化结果 | **已修**：新增 `Agent.prompt_result` / `Agent.continue_run_result` / `Agent.last_result`；`prompt()` 签名不变，暂停时抛 `LoopSuspended` | S1 ✅ |
| 无专用控制异常 | **已修**：`core/outcome.py#LoopSuspended`（非失败类型；`Agent` 先于通用 `except Exception` 捕获，不会被改写成模型失败） | S1 ✅ |
| `stop_reason=deferred` 被当成完成 | **已修**：模型自己宣告「这一轮没完」也报 `SUSPENDED`，`pending_interaction={"kind": "deferred", ...}`，不派发该轮工具调用 | S1 ✅ |
| 内部失败与模型失败混用 | **已修**：`LoopStop.INTERNAL_ERROR` 与 `MODEL_ERROR` 分开，`error_code="internal_error"` | S1 ✅ |
| 无显式状态 / 纯迁移函数 | **已修**：`core/execution.py` + `core/transitions.py` 提供显式 `ExecutionState`/`InputEvent`/`Action`/`Transition` 与纯 `advance`；旧循环本阶段未动 | S2 ✅ |
| 无单一 Runner | 动作（hook/模型/工具）仍在 `_run_loop` 内联执行；reducer 尚无驱动器，旧循环也未改接 | S3 |

## 1. 迁移表：旧循环分支 → 显式事件/状态/动作

约定：**输入事件**是 S2 之后 reducer 应收到的事件；**原状态**是 S0 现状里等价的局部位置；
**动作**是 Runner 之后应执行的可序列化意图；**输出事件**是今天已经产生、必须保持不变的事件序列；
**返回结果**是今天 `_run_loop` 的返回值（`_LoopEnd` → `LoopResult`）。

入口前置（`run_agent_loop_result` / `run_agent_loop_continue_result`）：

| # | 输入 | 原状态 | 动作 | 输出事件 | 返回 |
| --- | --- | --- | --- | --- | --- |
| E1 | `run_agent_loop(prompts, …)` | — | `validate_max_turns` → 解析 `stream_fn` → `_declare_tool_changes(context, prompts)` → 建 `AgentContext(messages=context.messages+initial)` | `AgentStart`、`TurnStart`、每条 initial 的 `MessageStart`+`MessageEnd` | 进入 B0 的主循环 |
| E2 | `run_agent_loop_continue(context, …)`，`context.messages` 非空且尾部不是 assistant | — | `validate_max_turns` → 解析 `stream_fn`；空/assistant 尾是硬守卫（`ValueError`，发生在任何事件之前） | `AgentStart`、`TurnStart` | 进入 B0，`turns` 从 1 起 |
| E3 | 非法 `max_turns`（0/负数/bool/浮点/字符串/列表） | — | `validate_max_turns` 抛 `ValueError` | **无**（任何事件之前） | 异常 |

内层主循环：

| # | 输入 | 原状态 | 动作 | 输出事件 | 返回结果 |
| --- | --- | --- | --- | --- | --- |
| B0 | 循环开始 | `turns=1`，`pending = poll(get_steering_messages)` | 轮询一次 steering | 无 | — |
| B1a | 进入第 2 轮及以后且 `max_turns` 已达标 | `last_completed != None` 且 `turns >= max_turns` | 无（不发模型请求，不 post `TurnStart`） | `AgentEnd(messages=new_messages)` | `USAGE_LIMITED` / `MAX_TURNS`，`error_code="max_turns"`（**实测不可达**：B8 在每次 `TurnEnd` 后已经拦下，见 §8） |
| B1b | 进入第 2 轮及以后 | `last_completed != None` | `prepare_next_turn(last_completed)`；可替换 `context` / `model` / `reasoning`，产出 `prepared` 消息 | 无 | — |
| B1c | B1b 之后 `pending` 为空 | `pending == []` | 再轮询一次 steering | 无 | — |
| B1d | 即将发请求 | — | `TurnStart`，`turns += 1` | `TurnStart` | — |
| B2 | 有待插入消息（`prepared`+`pending`） | — | `_declare_tool_changes` 计算「已提交历史 vs 可执行工具集」差，写成/替换 system 消息；追加进 `context.messages` 与 `new_messages` | 每条：`MessageStart`+`MessageEnd` | — |
| B3 | 请求模型 | — | `transform_context` → `convert_to_llm`/`to_llm_messages` → `get_api_key` → `project_context`（budget 回调）→ `stream_fn(model, llm_context, options)` | 帧循环：`start`→`MessageStart`；text/thinking/toolcall 增量→`MessageUpdate`；`done`/`error`→`result()` 后 `MessageEnd` | 最终 `AssistantMessage`（追加进 `new_messages`） |
| B4a | `message.stop_reason == deferred`（S1 新增） | 该轮有工具调用也不派发 | 无 | `TurnEnd(message, tool_results=())`、`AgentEnd` | `SUSPENDED`/`SUSPENDED`，`pending_interaction={"kind":"deferred", handle_id?, payload?}` |
| B4b | `message.stop_reason ∈ {error, aborted}` | — | **不执行工具** | `TurnEnd(message, tool_results=())`、`AgentEnd` | `ABORTED`→`CANCELLED`/`ABORTED`（`error_code="cancelled"`）；`ERROR`→`FAILED`/`MODEL_ERROR`（`error_code=raw_stop_reason or "model_error"`） |
| B5a | 有工具调用且 `stop_reason == length` | — | `_fail_truncated_batch`：整批**不执行**，每个调用一条错误结果 | 每调用：`ToolExecutionStart`、`ToolExecutionEnd(is_error=True)`、`MessageStart`+`MessageEnd` | 结果消息进入 `new_messages` |
| B5b | 有工具调用，非 length | — | `execute_tool_calls`：串行/并行（batch 内任一 `execution_mode=="sequential"` 即整批串行）；start/preflight 按源顺序，`ToolExecutionEnd` 按完成顺序，结果消息按源顺序 | `ToolExecutionStart`、`ToolExecutionUpdate`（可选，落定后丢弃）、`ToolExecutionEnd`、结果 `MessageStart`+`MessageEnd` | `ToolBatch(messages, terminate)` |
| B5c | B5b 之后 | 有 `calls` | `on_tool_batch_complete(message, tool_results)`（在 `prepare_next_turn` 之前） | 无 | — |
| B5d | B5c 之后 | 有 `calls` | `should_suspend_after_batch(message, tool_results)`；返回真值即暂停 | `TurnEnd(message, tool_results)`、`AgentEnd` | `SUSPENDED`/`SUSPENDED`，`pending_interaction=dict(...)` |
| B6 | 一轮结束 | — | 无 | `TurnEnd(message, tool_results)` | — |
| B7 | B6 之后 | — | `should_stop_after_turn(TurnEndContext)` 为真 | `AgentEnd` | `COMPLETED`/`STOP_HOOK`（**不是**批次完成） |
| B8 | B6 之后（B7 未命中） | `turns >= max_turns` | 不再请求下一轮 | `AgentEnd` | `USAGE_LIMITED`/`MAX_TURNS`，`error_code="max_turns"` |
| B9 | B6 之后（B8 未命中） | — | 轮询 steering；`pending` 或 `has_more_tool_calls` 为真则回到 B1 | 无 | — |
| B10 | 内层退出（无续跑、无 steering） | — | 轮询 `get_follow_ups`；有则回到 B1（外层重入） | 无 | — |
| B11 | 全空 | — | 无 | `AgentEnd` | 若 `signal.cancelled` → `CANCELLED`/`ABORTED`；否则 `COMPLETED`/`BATCH_TERMINATE`（上一批全部 `terminate`）或 `COMPLETED`/`ASSISTANT_FINAL` |

**不可改变的行为**（S2/S3 必须逐字保留，已有测试锁住）：
`length` 整批不执行；`terminate` 需**整批** finalized 结果都为真；暂停检查在下一轮请求**之前**；
`on_tool_batch_complete` 在 `prepare_next_turn` 之前；max_turns 允许**最后获准轮次的批次**结算；
工具并发不影响事件关联与结果消息顺序。

## 2. hooks 的调用位置、次数与失败语义

| hook | 位置 | 次数 | 失败语义（现状） |
| --- | --- | --- | --- |
| `transform_context` | B3，每次请求 | 每请求 1 次 | 异常冒泡出 `run_agent_loop_*`；`Agent` 层转成失败终态消息 |
| `convert_to_llm` | B3 | 每请求 1 次（`project_context` 触发预算投影时再 1 次） | 同上 |
| `project_context` | B3，经 `options["_project_context_for_budget"]` 交给宿主预算闸 | 每请求（宿主调用时） | 同上 |
| `prepare_next_turn` | B1b | 第 2 轮起每请求最多 1 次 | 同上 |
| `before_tool_call` | 工具 preflight，源顺序 | 每调用 1 次 | **不掀翻批次**：异常归一为 `is_error=True` 错误结果；返回 `{"block": True}` 也不执行 |
| `after_tool_call` | 工具结算 | 每调用 1 次 | 异常 → 该调用替换为错误结果；返回 override 可改 `content/details/usage/terminate/is_error` |
| `tool.prepare_arguments` | preflight，在 validator 之前 | 每调用 1 次 | 异常 → 错误结果 |
| `argument_validator` | preflight，在 `prepare_arguments` 之后 | 每调用 1 次 | 异常 → 错误结果；**没有 validator 不等于「放行」**（缺省只放行已带参数的调用） |
| `on_tool_batch_complete` | B5c，仅 `calls` 非空 | 每批次 1 次 | 异常冒泡（S3 保持） |
| `should_suspend_after_batch` | B5d，仅 `calls` 非空 | 每批次 1 次 | 异常冒泡 |
| `should_stop_after_turn` | B7，每 `TurnEnd` 之后 | 每轮 1 次 | 异常冒泡 |
| `get_steering_messages` | B0 / B1c / B9 | 每次轮次边界 1 次；`Agent.continue_run()` 走 steering 分支时首轮跳过 1 次 | 异常冒泡 |
| `get_follow_ups` | B10 | 每次内层退出 1 次 | 异常冒泡 |

## 3. 消息消费顺序（必须保持）

| 输入 | 接纳边界 | 消费边界 |
| --- | --- | --- |
| 初始 `prompt` | `Agent.prompt()` 忙时 `RuntimeError` | 第一轮请求内（E1 的 initial） |
| `steer()` | 随时入内存队列，不打断正在执行的工具 | **循环开头（B0，首个请求之前）**与 B1c / B9 的轮次边界；one-at-a-time 每次 1 条，all 一次取空 |
| `follow_up()` | 随时入内存队列 | 仅在内层「没有工具续跑且没有 steering」之后（B10） |
| `continue_run()` 的队列优先 | 尾巴是 assistant 时先 steering 再 follow-up，两者皆空则 `RuntimeError` | 走 steering 分支时跳过循环开头那次 steering 轮询（`skip_initial_steering_poll`），避免同一条被消费两次 |

`_declare_tool_changes` 只在**工具集相对已提交历史发生变化**时插入 system 消息；插到第一条非 system 消息
之前；已存在 system 消息时按其位置**替换**工具字段（保证重放恒等于当前工具集）。

## 4. 结束原因优先级（同一轮内，从高到低）

1. 模型 `error` / `aborted`（B4b）——工具一律不执行，`TurnEnd.tool_results` 为空。
2. 模型 `deferred`（B4a）——`SUSPENDED`，本轮工具调用不派发。
3. 批次后暂停（B5d）——`SUSPENDED`，不是终态，`pending_interaction` 必填。
4. `should_stop_after_turn`（B7）——`COMPLETED`，但 `stop=stop_hook`，**不等于**任务完成。
5. `max_turns` 达标（B1a 请求前 / B8 结算后）——`USAGE_LIMITED` + `error_code="max_turns"`；排队消息不再消费。
6. 轮次边界取消（B11）——`CANCELLED`/`ABORTED`；即使最后一轮刚好正常完成也不报 `completed`。
7. 自然收尾（B11）——`COMPLETED`/`BATCH_TERMINATE` 或 `ASSISTANT_FINAL`。

驱动层自己的失败（hook / bridge / driver 抛异常）不属于上述任何一条：`Agent` 把它归一为
`FAILED`/`INTERNAL_ERROR`（`error_code="internal_error"`），与提供方失败 `MODEL_ERROR` 分开。

「一次 drive 调用返回」≠「整个逻辑 run 结束」：`SUSPENDED` 会返回，但 run 只是暂停。

## 5. 兼容策略（S0 冻结，S1–S6 遵守）

| 面 | 策略 |
| --- | --- |
| 旧列表返回类型 | `run_agent_loop` / `run_agent_loop_continue` 继续返回 `list[AgentMessage]`（仅本次新增消息），不改变元素类型；结构化入口返回同一内核产出的 `LoopResult` |
| 旧入口遇到暂停 | **S1 已实施**：抛专用控制异常 `LoopSuspended` 并携带可读 `LoopResult`（`status=SUSPENDED`），不再静默返回列表；`Agent` 在通用 `except Exception` **之前**捕获它，因此暂停不会被改写成模型失败；`DefaultHarness` 用结构化入口，不受影响 |
| `Agent.prompt()` | 保留现有签名与「忙时拒绝」习惯；结构化结果通过**新增**接口/属性取得（`prompt_result` / `continue_run_result` / `last_result`），不改现有返回值类型 |
| 事件字段 | S2 不新增/不重命名 core 事件；S3 再把「流式进度」与「控制/终态」分路径，事件顺序契约不变（`tests/core/test_core_agent_loop.py` 的顺序断言是红线） |
| 等待语义 | `RunStatus.SUSPENDED` + `pending_interaction`；`awaiting_approval` / `deferred` / `unknown` 一律走结构化等待，不写伪成功工具消息、不请求下一轮；`denied` / `invalid_args` / `failed` / `cancelled` 作为**真实已结算**工具结果继续给模型 |
| 持久版本 | `CHECKPOINT_SCHEMA_VERSION=1`、`EVENT_ENVELOPE_SCHEMA_VERSION=1`、`SUPPORTED_*_VERSIONS=(1,)` 本轮不变；S6 为 `ExecutionState` 单独声明版本，未知/未来版本明确拒绝（`VersionRefused`），只扩列表不算兼容 |
| 源码引用 | 保留 `pi-agent-core 0.86.1 @ c7cdb46`（MIT）的来源注释、`THIRD_PARTY_NOTICES.md` 与 `LICENSES/`；新增模块（如 `core/execution.py`）标注「本项目新增设计」，不删除历史衍生关系 |

## 6. S2/S3 类型落点（与现有类型合并，不重复造）

| 计划建议名 | 落点 | 说明 |
| --- | --- | --- |
| `ExecutionState` | 新 `core/execution.py` | 阶段、已提交消息、当前模型动作 ID、批次分区、已消费输入、轮次/额度、指纹；不含 wall clock/协程/连接 |
| `InputEvent` | 同上 | `UserPrompt` / `Steering` / `FollowUp` / `ModelCompleted` / `ToolBatchSettled` / `SuspendDecided` / `Cancelled` / `ClockTick` 等，全部是纯数据 |
| `Action` | 同上 | `PrepareNextTurn` / `RequestModel` / `RunToolBatch` / `WaitForExternal` / `Finish`，可序列化，不含 callable |
| `advance(state, event) -> Transition` | 新 `core/transitions.py` | 纯函数：不读时钟、不建随机 ID、不调工具、不写存储 |
| Runner | 新 `core/runner.py` | 执行动作、消费模型流、调度有界工具批次、投递结果、清理；复用 `tool_execution.py` 与 provider 桥 |
| `LoopResult` | 复用 `core/outcome.py` | 不新增语义相同的第二套结果类型 |

现有 `LoopResult` / `RunStatus` / `LoopStop` 已经是唯一结果词汇，S2 只做「状态 → 迁移」，不引入第二套。

## 7. 基线复现命令

```bash
python -m pytest -q tests/core        # 193 passed
python -m pytest -q tests             # 657 passed, 1 skipped
python -m pytest -q tests -rs         # 打印跳过原因
```

## 8. S2 实施记录：显式状态与纯迁移函数

**新增模块（均为 core 内部，尚未进入公共导出，S6 再定）：**

| 模块 | 内容 |
| --- | --- |
| `src/yanxu_harness/core/execution.py` | `ExecutionState`（阶段 + 已提交消息 + 本轮新增消息 + pending/prepared + 工具声明 + 批次分区 + 已消费事件 + 轮次/额度 + 取消标记 + 终态）与 `Phase` / `PollSite` / `InputKind` / `BatchCallState`；`InputEvent`（`PromptSubmitted`/`ContinueRequested`/`InputsPolled`/`TurnPrepared`/`ModelCompleted`/`ToolBatchSettled`/`StopHookEvaluated`/`CancellationRequested`/`ContextProjected`）；`Action`（`ReadInputs`/`PrepareTurn`/`EvaluateStopHook`/`RequestModel`/`RunToolBatch`/`WaitForExternal`）；`Transition`（新状态 + 有序控制事件 + 动作 + `ignored_reason`） |
| `src/yanxu_harness/core/transitions.py` | `advance(state, event) -> Transition`（纯函数）、`result_of(state) -> LoopResult`、`validate_max_turns` |

**事件分类（计划 S2 第 3 条）**：

| 情形 | 处理 |
| --- | --- |
| 相同 `event_id` + 相同内容（重复投递） | 幂等忽略：状态不变，`Transition.ignored_reason` 给原因；不产生第二次模型请求或工具批次 |
| 相同 `event_id` + 不同内容 | 拒绝（`ContractError`）：一个 id 不得承载两个不同事实 |
| 未知事件类型（不是 `InputEvent`） | 拒绝，绝不静默改状态 |
| 阶段不匹配但执行已 `SUSPENDED`/`FINISHED`（迟到回调） | 忽略并给原因；执行保持终态，不被晚到事件复活 |
| 阶段不匹配且执行仍在进行 | 拒绝（驱动 bug，不猜） |
| 挂起期间的 `CancellationRequested` | 记录标记，阶段仍为 `SUSPENDED`（S5 的“挂起期间取消可持久记录”） |

**纯函数边界（计划 S2 第 4 条）**：`advance` 不读时钟（时间戳只来自 `event.at_ms`，合成 system 消息用它）、不生成随机 ID、不调用 hook/模型/工具、不写存储；事件里的 `messages`/`results`/`prepared` 必须是 tuple（传 list 直接拒绝），状态构造时把序列拷成 tuple，因此调用方之后改自己的列表影响不到已提交状态。消息对象按引用持有，约定“入状态后不得就地修改”。

**与旧循环的等价性**：`_run_loop` 一行未动，全部 711 条测试仍通过；reducer 有 34 条确定性测试（`tests/core/test_core_execution_state.py`），逐条对应本表 B0–B11 与 E1–E3，包括“记录过的事件流重放后状态与动作完全一致”“同一事件流重放两次不新增请求/批次”。真正的等价性证明在 S3：把现有全部测试跑在 Runner 上。

**迁移发现**：`_run_loop` 的 B1a（进入下一轮前再查一次 `max_turns`）**不可达** —— B8 在每次 `TurnEnd` 之后已经拦下达标的情况，而通往 B1 的两条路（B9 steering、B10 follow-up）都要先过 B8。reducer 因此没有为它建分支；S3 删除旧循环时一并删掉这段死判断。

**词汇统一（避免第二套）**：`CallOutcome` / `classify_status` 从 `runtime/batch.py` 移到 `core/outcome.py`（core 的 reducer 要判断批次是否阻塞，而 core 不能反向 import runtime）。`yanxu_harness.runtime.batch` 继续导出**同一对象**，所以 `docs/contract.md` 的依赖方向（runtime → core）成立，且不存在两份分类。状态→分类的映射改为按 gateway 写入的**状态字符串**取值（core 不 import tools 层），未知状态仍然拒绝；`tests/core/test_core_execution_state.py` 遍历 `InvocationStatus` 全部取值钉住该表。

**已搬走的纯计算**：`_declare_tool_changes` / `_with_tool_changes` 从 `agent_loop.py` 移到 `core/messages.py`（`declare_tool_changes` / `with_tool_changes`），新增 `at_ms` 参数让状态机传入时刻；`agent_loop._declare_tool_changes` 暂留为一行兼容垫片，**S3 删除**。

**删除点（写明的短期手段）**：

| 临时物 | 内容 | 删除阶段 |
| --- | --- | --- |
| `agent_loop._declare_tool_changes` 垫片 | 转调 `messages.declare_tool_changes` | S3 |
| `agent_loop._run_loop` / `_LoopEnd` / `_loop_result` | 隐式循环主体与它的结果投影（由 `transitions.result_of` 取代） | S3 |
| `agent_loop._deferred_interaction` | 已删除（S2 使用 `execution.deferred_interaction`） | 已完成 |
