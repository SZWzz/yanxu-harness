# Core 执行状态机迁移表（S0 冻结 · S1–S6 已实施）

> **交接摘要（2026-10-15）**：S0–S6 全部实施并分阶段提交（`e3599cf`、`2839ecf`、`6a68b06`、`1e90fb4`、`ef7bf67`、`1b98b18`、`dee80ef`、`ec3ecdf`、以及本记录所在的提交）。
> 单一执行内核：`core/runner.py#Runner` 驱动纯 reducer `core/transitions.py#advance`，状态与动作在 `core/execution.py`；旧入口全部是同一内核的薄包装。
> 本地门禁实测：`python -m pytest -q tests` → **768 passed, 1 skipped**；`scripts/ci_check.py --offline` → 6/6 步 ok；`scripts/verify_release.py` → wheel/sdist 各 **744 passed / 25 skipped**，五个示例全部退出 0。
> **未完成/未执行**：远程 CI（外部阻断）、真实模型、Linux、Windows、以及 Python 3.10 的**本轮**复跑（本机已无 3.10 解释器，静态门禁仍在跑）。
> 仍未收窄的两个已知边界：派发窗口恢复必须依赖 Gateway 账本证据；线程内阻塞 I/O 不能强制停止（靠连接关闭/超时/晚到结果隔离）。

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
| 恢复不读显式状态 | **已修**：`RunRecord.execution` 提交显式快照（`core/execution.state_to_record`），`RunOutcome/RunState.pending_work` 直接给出待办；两半不一致时拒绝恢复 | S4 ✅ |
| 无显式状态 / 纯迁移函数 | **已修**：`core/execution.py` + `core/transitions.py` 提供显式 `ExecutionState`/`InputEvent`/`Action`/`Transition` 与纯 `advance`；旧循环本阶段未动 | S2 ✅ |
| 并发/慢消费者无上限、旧结果可写入新状态 | **已修**：reducer 校验结果只属于本轮（越界/重复拒绝）；事件流缓冲有界并计数溢出；两个 Agent 并行时慢观察者不阻塞对方 | S5 ✅ |
| 无单一 Runner | **已修**：`core/runner.py#Runner` 驱动 reducer，全部入口（旧列表入口、结构化入口、`Agent`、`DefaultHarness`）共用它；`_run_loop`/`_LoopEnd`/`_loop_result` 已删除 | S3 ✅ |

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
| `agent_loop._declare_tool_changes` 垫片 | 转调 `messages.declare_tool_changes` | S3 ✅ 已删 |
| `agent_loop._run_loop` / `_LoopEnd` / `_loop_result` | 隐式循环主体与它的结果投影（由 `transitions.result_of` 取代） | S3 ✅ 已删 |
| `agent_loop._deferred_interaction` | 已删除（S2 使用 `execution.deferred_interaction`） | 已完成 |

## 9. S3 实施记录：单一 Runner

**落点**：`core/loop_config.py`（从 `agent_loop.py` 拆出的 `AgentLoopConfig`、两个 hook 入参、两个校验器）与 `core/runner.py`（`Runner` + 四个公开入口）。`core/agent_loop.py` 现在只是重新导出**同一对象**的兼容模块（`tests/core/test_core_single_runner.py` 用对象同一性钡住）。

**驱动循环**：`advance → 按顺序发控制事件 → 若已 SUSPENDED/FINISHED 就返回 result_of(state) → 执行动作 → 把产出的输入事件入队`。动作与事件的对应：

| 动作 | Runner 做什么 | 回送事件 |
| --- | --- | --- |
| `ReadInputs(site,kind)` | 调 `get_steering_messages`/`get_follow_ups` | `InputsPolled` |
| `PrepareTurn` | 调 `prepare_next_turn`（无 hook 则短路） | `TurnPrepared` |
| `EvaluateStopHook` | 调 `should_stop_after_turn`（无 hook 则 `False`） | `StopHookEvaluated` |
| `RequestModel` | `transform_context`→`convert_to_llm`→`get_api_key`→`project_context`→`stream_fn`；流式帧直接发 | `ContextProjected`（仅当投影要持久化）+ `ModelCompleted` |
| `RunToolBatch` | `length` 截断走错误结果（不执行）；否则 `execute_tool_calls`；然后 `on_tool_batch_complete` 与 `should_suspend_after_batch` | `ToolBatchSettled(results, terminate, suspend)` |
| `WaitForExternal` | 不执行：reducer 已返回暂停 | — |

**进度与控制的归属**（计划 S3 第 5 条）：模型流的 `MessageStart`/`MessageUpdate`/`MessageEnd` 与工具批的 `ToolExecutionStart`/`Update`/`End` + 结果消息框由 Runner 发（它们与执行本身交错：end 按完成顺序、结果消息按源顺序）；`AgentStart`/`AgentEnd`、`TurnStart`/`TurnEnd` 与插入消息的框由 reducer 发。顺序与旧循环逐字一致（既有断言全过）。

**监听器两类用途**（计划 S3 第 6 条）：`DefaultHarness(listener=...)` 是观察者（事件提交后才投递，失败不回滚，见 T17）；`run_listener` 接受已提交的持久事件。Runner 只有一条有序四路，不自己缓冲；慢消费者由宿主侧有界队列处理（S5）。

**取消**：Runner 每轮开始前检查 token，把一次尚未记录的取消作为 `CancellationRequested` 事件放在待处理事件**之前**，因此「已取消时不新派发」由 reducer 的 `_finish_natural` 统一决定，而不散在两处；停止 hook 与模型错误仍然优先（与旧循环的 B7 早于 B11 一致）。

**工具并发**：`AgentLoopConfig.max_concurrent_tools`（`Agent(...)`、`DefaultHarness(...)`、`runtime.validate_max_concurrent_tools`）显式限定同时**执行**的调用数；preflight（start 事件、参数准备、`before_tool_call`）仍串行且按源顺序，`end` 按完成顺序、结果消息按源顺序，因此限额不影响事件关联（`tests/core/test_core_single_runner.py` 实测峰值）。默认 `None` = 保持旧行为（全部并发）。

**一个兼容性修正**：`prepare_next_turn` 换掉 context 时，替换集里的**工具集**也会生效（`TurnPrepared.context_tools`）——旧循环用 `snapshot["context"].tools` 算声明差，少了这一步就会把已移除的工具当成仍可执行。

**仍是旧行为的点（留给后续阶段）**：并发调用之间的取消只在调用边界收口（S5）；单个阻塞 I/O 的线程不能被协程强制停止（S5）；恢复路径仍由 `DefaultHarness` 自己拼事件与结果（S4）。

## 10. S4 实施记录：持久暂停与恢复边界

**新增提交端口**：`AgentLoopConfig.state_sink`（`Callable[[ExecutionState], Any]`，可同步可异步）。Runner 在**每个被接受的状态之后、执行该迁移的动作之前**调用它，因此“模型的最终响应先进入已提交状态，再派发它的工具调用”是被顺序钡住的，而不是靠注释；端口抛异常会中止本次运行（一次没提交成功的调用不得被静默忽略）。

**`DefaultHarness` 的接线（复用，不改恢复路径）**：

- `RunRecord.execution`：**可选**字段，内容是 `core.execution.state_to_record(ExecutionState)`，自带 `schema_version`（当前 1）。无此字段的老记录照旧只靠 `messages`/`batch` 恢复（`RunRecord.from_record` 已按「忽略未知可选键」设计，因此旧 reader 读新记录也不会炸）。
- sink 只在会在崩溃后留下问题的相位写库（`REQUESTING_MODEL` / `RUNNING_BATCH` / `AWAITING_PREPARE` / `AWAITING_STOP_HOOK` / `SUSPENDED`），队列读取（`COLLECTING`）不重复序列化 transcript。**该接线是 S4 之后补齐的**：S4 时 sink 只更新内存，真正的提交见 §16。
- 恢复前校验：`_assert_execution_snapshot_agrees` 在两个已提交事实（snapshot 的批次分区 vs `RunRecord.batch`）不一致时**拒绝恢复**并说明分歧；两半一致时不增加任何判断。缺失 snapshot 不算分歧。
- 孤儿恢复的证据变强：`_recover_orphan` 现在会把 snapshot 里“已声明但无结果”的调用逐个列进 reconciliation 的 `detail`（读的是 `pending_work` 的 `declared` + `batch_outcome_unknown`），而不是只说“不知道发生过什么”。仍然不自动重试。
- `RunOutcome.pending_work` / `RunState.pending_work`：把 snapshot 的投影直接交给宿主（`blocking` / `replayable` / `declared` / `batch_outcome_unknown` / `model_request_outstanding` / `interaction` / `suspended`），不再需要宿主自己解析批次。

**`pending_work_of(state)` 的四个问题**（计划 S4.3/S4.4/S4.9）：

| 字段 | 回答 | 依据 |
| --- | --- | --- |
| `blocking` | 哪些调用绝对不能再派发 | `awaiting`（等人决定）/ `uncertain`（等业务证据） |
| `replayable` | 哪些调用可证明没跑过 | 仅 `unstarted`（批次已记录但未到达） |
| `declared` + `batch_outcome_unknown` | 崩在派发窗口里：模型要了这些调用，结果未记录 | `phase == RUNNING_BATCH`；必须查 Gateway intent/receipt，不自动重试 |
| `model_request_outstanding` | 是不是死在模型请求里（≠ 工具副作用） | `phase == REQUESTING_MODEL`；未完成的请求不可能产生外部副作用，可以再问一次 |

**真实子进程证据**：`tests/fault_windows.py` 新增 `batch-dispatch-die`：工具在写出副作用（append-only 文件 + fsync）后 `os._exit(9)`；`tests/core/test_core_execution_durable.py` 断言 commit log 的最后一条已经是 `phase=running_batch`、`declared=[c1]`、`batch=[]`（没有任何结果被声明），并且一个全新进程能从这些字节重建状态并回答“能不能派发”。这就是 C08/C09 的窗口。

**仍是旧行为的点**：派发窗口的恢复仍然保守（不因 snapshot 存在就自动重放）；`DefaultHarness` 仍自己拼恢复用的事件与结果，snapshot 不替代 Gateway 的操作账本（计划 S4.7 的边界保持不变）。

## 11. S5 实施记录：取消、运行隔离与资源生命周期

**所有权与清理期限**（计划 S5.1；代码位置即所有者）：

| 资源 | 所有者 | 何时创建 / 释放 | 取消时发生什么 |
| --- | --- | --- | --- |
| 运行取消令牌 `CancellationToken` | `Agent._active_run` / `_Drive.token` | 每次调用建一个；`_finish_run` / 作用域退出后不再被引用 | 首次 `cancel(reason)` 生效，原因只记第一次；令牌本身不做任何清理 |
| provider 流（含 `FramePump` 线程） | Runner 的 `_stream_turn`；线程由 `providers/frame_pump.py` 拥有 | 请求开始时建；`result()`/终帧后关闭；协作式取消交给流自己观察 `signal` | 连接关闭/超时由 provider 实现负责；Runner 不假装能强制停住一个阻塞的 `recv` |
| 工具任务（并发批次） | `execute_tool_calls` 的 `asyncio.gather` | 批内并发；`gather` 结算后才返回 | 取消只在调用之间收口：已开始的调用按**真实结果**结算，未开始的不再派发 |
| `ToolExecutionUpdate` 任务 | `tool_execution._execute_prepared` 的 `pending_updates` | 工具 `on_update` 时创建；执行落定后 `_drain` 统一 await，之后不再接收 | 落定后创建的 update 被丢弃；已创建的一定在 `ToolExecutionEnd` 之前结算 |
| 事件监听器 / 观察者 | `Agent._listeners`、`DefaultHarness(listener=...)` | 订阅时注册；退订幂等 | 取消不回滚已提交事实；`DefaultHarness(replay_run_events)` 是补读路径 |
| 驱动任务 | `DefaultHarness._run_task` | 每次 start/resume 用 `asyncio.ensure_future` 建一个，存在 `_Drive.task` | 宿主取消 → 任务被取消，并归一为 `CANCELLED` outcome（消息不丢）；`max_runtime_seconds` 到点也走同一清理 |
| 租约 | `DefaultHarness._claim` | start/resume 时领取，终态提交**之后**才释放 | 租约丢失 → 后续提交被 fence 拒绝；不声称能撤回已发生的远程副作用 |
| 内存队列 | `Agent._steering` / `_follow_up` | 进程内；`reset()`/`clear_*` 清空 | 不是持久任务队列；挂起期间到达的消息不会自行唤起新调用 |

**已取消时不新派发**（S5.2）：Runner 每轮把尚未记录的取消作为事件送入 reducer；工具批仍走 `execute_tool_calls`，它在每次调用前检查 `signal`，因此未开始的处理器不会被执行，而已完成调用的**真实 receipt 不会被丢弃**（`tests/core/test_core_cancellation_isolation.py::test_a_cancellation_does_not_discard_a_real_receipt` 用真实 end 事件钉住）。这是刻意保持的旧语义：消息文本仍为 `Operation aborted`，事件顺序不变。

**旧结果不得写入新状态**（S5.3）：reducer 在应用 `ToolBatchSettled` 前校验每条结果都属于**本轮**声明的调用，且一个调用只有一条结果；越界结果、重复结果、相位不匹配的批次全部拒绝（`_assert_results_belong_to_this_batch`）。终态/挂起后的事件被幂等忽略，不能复活运行。

**并发与有界缓冲**（S5.5）：`tests/core/test_core_cancellation_isolation.py` 实测同一事件循环上两个 `Agent` 并行——慢观察者阻塞期间，另一个运行仍然到达终态；`core/stream.py` 的缓冲上限会显式计入 `overflow_count`，且终态帧永不被上限拒（否则消费者会永久等待）。

**挂起期间取消可持久记录并被识别**（S5.6）：`DefaultHarness.cancel` 对非本地运行的运行只**记录**请求（不假装它已停止）；`resume` 发现 `cancel_requested` 时把这次暂停提交为 `CANCELLED`（`stop_code=aborted`），不派发任何调用、不伪造工具结果，并保留批次作为“当时正要执行什么”的证据。事件词表新增 `EventKind.RUN_CANCELLED`。

**一个刻意的收窄与随之撤回**：本阶段一度让 reducer 在 `cancel_requested` 时跳过整个工具批（连 preflight hook 也不跑）。那会改变 `before_tool_call` 的调用次数与既有断言 `Operation aborted`，因此回退了；`RunToolBatch` 不含取消分支，取消语义仍由执行器逐调用决定。

## 12. S6 实施记录：兼容收尾、文档与验证

**兼容映射（无迁移开关可用，因为不需要）**：全程没有引入「新/旧循环」配置开关 —— 旧入口与结构化入口从 S3 起就是同一 `Runner` 的薄包装，`core/agent_loop.py` 只重新导出同一对象。因此 S6 不需要删除开关，只需要确认没有第二套业务判断：

| 入口 | 现状 |
| --- | --- |
| `run_agent_loop` / `run_agent_loop_continue` | 列表返回；暂停抛 `LoopSuspended`（携带同一 `LoopResult`） |
| `run_agent_loop_result` / `run_agent_loop_continue_result` | 结构化结果 |
| `Agent.prompt` / `continue_run` | 签名不变（返回 `None`）；暂停抛 `LoopSuspended`，失败仍合成终态 assistant 消息 |
| `Agent.prompt_result` / `continue_run_result` / `last_result` | 结构化结果 |
| `DefaultHarness.start/resume/cancel/state` | 全程走结构化入口；`RunStatus`/`LoopStop` 与 Core 一致，快照提供 `pending_work` |

**公共导出**（`yanxu_harness.core.__init__`，同时登记在 `docs/contract.md` 与 `tests/test_public_api.py`）：新增 `Action`、`ExecutionState`、`InputEvent`、`Phase`、`Transition`、`advance`、`result_of`、`pending_work_of`、`state_to_record`、`state_from_record`、`CallOutcome`、`classify_status`（后两者先前只能从 `runtime.batch` 取，现在两条路径是同一对象）。

**格式与版本**：`EXECUTION_STATE_SCHEMA_VERSION = 1`（`core/execution.py`）；未知/缺失版本、未知相位或状态一律拒绝（`ContractError`/`VersionRefused`）。`RunRecord.execution` 是**可选**字段，老记录（无 snapshot）继续按 `messages`/`batch` 读取 —— 这是真实迁移（“没有快照”是合法状态），不是把版本列表扩一格。事件信封与 checkpoint 版本本轮未变。

**私有材料与公开事件**（S6.3）：`core/events.public_event_payload` 递归剔除续传字段（本轮修了一个真缺陷：映射**键名**以前不过滤，藏在 `arguments`/`details` 里的 `thinking_signature` 会漏进公开 payload）；`yanxu_harness.events.assert_public_payload` 把三个续传字段名加入拒绝集；快照（私有恢复状态）仍然保留签名。证据：`tests/core/test_core_execution_durable.py::test_a_nested_continuation_field_leaves_no_trace_in_the_public_projection` 与 T20 的既有用例。

**来源说明**：`THIRD_PARTY_NOTICES.md` 更新为「派生范围 + 本项目新增设计」两段：派生部分（消息/事件/循环/取消语义）保留 pi-agent-core 0.86.1 @ `c7cdb46` 的归属与许可证；新增部分（显式状态、纯迁移、单一 Runner、结果词表、版本化快照、并发上限、提交端口）明确记为本项目设计，不因重写而抹掉衍生关系。

**本轮真实验证结果**（全部在本机跑，逐条对应命令）：

| 命令 | 结果 |
| --- | --- |
| `python -m pytest -q tests`（`.venv`，3.12.13，含全部 extras） | **773 passed, 1 skipped**（29s，审计修复后复跑） |
| 同一套测试在 3.11.15 的独立 venv（树外，无 yaml extra） | **763 passed, 11 skipped**（跳过全是 yaml extra 缺失；本机 wheelhouse 只有 cp312 的 PyYAML） |
| Python 3.10 | **本轮未执行**（本机已无 3.10 解释器）；静态门禁仍跑，alpha 轮曾真实执行过 3.10.14 |
| `scripts/ci_check.py --offline --wheelhouse build-wheelhouse` | **6/6 步全部 ok** |
| `scripts/verify_release.py --offline --wheelhouse build-wheelhouse --out build/release` | wheel **749 passed / 25 skipped**；sdist **749 passed / 25 skipped**；**5 个示例全部退出 0**（含跨进程端到端链路） |
| 远程 CI / 真实模型 / Linux / Windows | **未执行**（CI 首次运行因外部计费阻断；provider 测试用 loopback；无跳平台机器） |

**未完成的诚实清单**：派发窗口的恢复仍保守（快照指出窗口与候选调用，但没有账本证据不放行）；UI 慢消费者在 Harness 层仍靠宿主有界队列，Core 只保证事件流缓冲有界并计数溢出；线程内阻塞 I/O 仍不能强制停止（靠连接关闭/超时/晚到结果隔离）。

## 13. 验收矩阵（计划 §6 的 C01–C18）逐项对应

| 编号 | 用例 | 证据（可单独运行） |
| --- | --- | --- |
| C01 | 普通对话、工具多轮、无工具回答 | `tests/core/test_core_agent_loop.py`（事件骨架、工具轮、结果顺序）、`tests/core/test_core_loop_result.py::test_a_plain_answer_is_a_completed_run_with_its_text_and_usage`、`tests/test_provider_core_bridge.py::test_a_tool_call_is_executed_once_by_the_gateway_and_its_result_reaches_the_model`（T01） |
| C02 | steering 与 follow-up 同时存在，只消费一次 | `tests/core/test_core_execution_boundaries.py::test_steering_is_consumed_at_the_turn_boundary_before_any_follow_up`、`::test_steering_is_not_replayed_into_the_first_request_twice`、`tests/core/test_core_agent.py::test_one_at_a_time_mode_spreads_queued_messages_over_turns`、`::test_all_mode_puts_every_queued_message_into_the_first_request` |
| C03 | 预算耗尽时仍有排队消息；恢复不重置预算 | `tests/core/test_core_agent_loop.py::test_max_turns_does_not_drain_messages_after_the_last_request`、`::test_queued_steering_and_follow_up_use_only_the_remaining_turn_budget`、`tests/core/test_core_execution_state.py::test_a_request_over_budget_is_not_made`、`::test_a_resumed_invocation_continues_the_spent_turn_count`、`tests/test_runtime_execution_snapshot.py::test_a_resume_does_not_refresh_the_turn_budget` |
| C04 | length / stop hook / batch terminate / 取消交叉 | `tests/core/test_core_execution_boundaries.py::test_a_length_truncated_batch_never_runs_even_when_a_stop_hook_would_fire`、`::test_a_terminating_batch_does_not_hide_a_cancellation_that_arrived_between_turns`、`tests/core/test_core_loop_result.py` 的四个停止路径用例 |
| C05 | awaiting approval / deferred / unknown | `tests/core/test_core_execution_outcomes.py::test_agent_prompt_raises_the_pause_and_leaves_no_fabricated_failure`、`tests/core/test_core_execution_state.py::test_a_waiting_call_suspends_before_the_next_request`、`::test_an_unproven_call_suspends_for_reconciliation_not_as_success`、`::test_a_deferred_model_turn_is_a_pause`、`tests/test_tools_core_bridge.py::test_only_the_three_pending_statuses_suspend_the_run` |
| C06 | 一项已完成、一项待批、一项未知 | `tests/core/test_core_single_runner.py::test_a_mixed_batch_pauses_without_a_next_request_and_keeps_each_status`、`::test_the_reducer_partitions_a_partial_batch_into_all_four_outcomes`、`tests/test_runtime_resume.py::test_a_settled_call_is_reused_and_only_the_waiting_one_is_dispatched` |
| C07 | 重复或冲突输入/结果 | `tests/core/test_core_execution_state.py::test_a_repeated_event_is_ignored_with_a_reason_and_creates_no_second_action`、`::test_the_same_event_id_with_different_content_is_refused`、`tests/core/test_core_cancellation_isolation.py::test_a_settled_batch_from_an_old_turn_is_refused`、`::test_two_results_for_one_call_are_refused` |
| C08 | 模型请求前 / 半截流 / 完整响应落盘后崩溃 | `tests/test_core_commit_port_crash.py`（真实子进程：半截调用不执行、声明已落盘）、`tests/core/test_core_execution_durable.py::test_the_state_is_committed_before_the_tool_batch_is_dispatched`、`::test_a_commit_failure_at_the_batch_boundary_stops_the_dispatch` |
| C09 | Gateway intent/effect/receipt 三个窗口 | `tests/test_storage_sqlite.py::test_a_process_that_dies_after_the_intent_leaves_an_unsettled_operation`、`::test_a_process_that_dies_after_the_effect_reports_an_unprovable_outcome`、`::test_a_settled_operation_is_replayed_after_a_restart`（T07–T09） |
| C10 | 输入出队后、状态提交前崩溃 | `tests/test_storage_sqlite.py::test_state_and_events_are_visible_after_a_crash_at_fanout`、`tests/test_runtime_recovery.py::test_a_paused_run_survives_the_process_that_paused_it`；提交端口在“出队后”的等价窗口见 `tests/core/test_core_execution_durable.py::test_the_state_is_committed_before_the_tool_batch_is_dispatched` |
| C11 | 提交成功、发布失败 | `tests/test_runtime_resume.py::test_a_listener_failure_does_not_roll_back_or_lose_the_events`（cursor 补读） |
| C12 | 迟到的旧 attempt / 跳 namespace 同名 ID | `tests/test_runtime_isolation.py::test_an_event_from_an_older_attempt_is_refused_with_a_reason`、`::test_a_stale_event_never_reaches_the_listener_or_the_transcript`、`::test_the_operation_key_includes_the_namespace`、`tests/core/test_core_cancellation_isolation.py::test_a_late_event_after_a_run_ended_cannot_start_a_new_turn` |
| C13 | 两进程恢复、租约失效 | `tests/test_runtime_isolation.py::test_a_second_worker_cannot_drive_the_same_run`、`::test_a_stale_worker_cannot_commit_after_losing_its_lease`、`tests/test_storage_sqlite.py::test_a_second_process_cannot_take_a_live_lease`、`::test_an_expired_lease_is_reclaimable_and_the_old_fence_is_dead`（T11） |
| C14 | 取消发生在模型/工具/等待/清理阶段 | `tests/core/test_core_cancellation_isolation.py::test_a_cancelled_run_executes_no_tool_handler`、`::test_a_cancellation_does_not_discard_a_real_receipt`、`::test_a_cancelled_agent_run_leaves_no_dangling_task`、`tests/test_runtime_isolation.py::test_cancelling_from_another_thread_stops_a_blocked_provider`、`::test_a_cancelled_run_leaves_no_dangling_task_and_the_loop_stays_responsive`、`tests/test_runtime_execution_snapshot.py::test_a_cancellation_while_suspended_is_recorded_and_recognized_before_resume` |
| C15 | 并发额度与慢消费者 | `tests/core/test_core_single_runner.py::test_a_concurrency_limit_is_observed_and_does_not_reorder_results`、`::test_a_limit_of_one_serialises_execution_but_keeps_correlation`、`::test_an_invalid_concurrency_limit_is_refused_before_any_event`、`tests/core/test_core_cancellation_isolation.py::test_two_agents_on_one_loop_do_not_block_each_other`、`::test_the_event_stream_buffer_is_bounded_and_says_so_when_full` |
| C16 | reducer 纯净与可重放 | `tests/core/test_core_execution_state.py::test_the_same_state_and_event_give_the_same_transition`、`::test_the_reducer_never_reads_a_clock`、`::test_the_state_copies_sequences_it_is_given`、`::test_a_recorded_event_stream_rebuilds_the_same_state_and_actions`、`::test_replaying_the_same_event_twice_changes_nothing` |
| C17 | 旧函数 / Agent / Harness 三种入口 | `tests/core/test_core_single_runner.py::test_the_compatibility_module_reexports_the_same_objects`、`::test_the_old_loop_body_is_gone`、`::test_every_entry_point_calls_the_runner`、`tests/core/test_core_loop_result.py::test_the_legacy_list_entry_points_return_the_same_messages` |
| C18 | future/corrupt schema 与敏感信息 | `tests/core/test_core_execution_durable.py::test_a_future_or_missing_schema_version_is_refused`、`::test_an_unknown_phase_or_status_is_refused_not_guessed`、`::test_a_nested_continuation_field_leaves_no_trace_in_the_public_projection`、`tests/test_runtime_checkpoint.py::test_a_future_schema_version_is_refused`、`tests/test_runtime_isolation.py::test_published_events_never_carry_host_secrets`、`tests/test_provider_core_bridge.py::test_a_thinking_signature_stays_on_the_block_and_out_of_public_payloads` |

**未覆盖的跳平台/外部项**（不当作通过行）：远程 CI、真实模型、Linux、Windows（见 `docs/verification.md`）。

## 14. 与计划的差异登记

| 计划条目 | 落地情况 | 原因 |
| --- | --- | --- |
| §4.3「当前模型动作 ID」 | `ExecutionState.model_action_id = "request:<turn>"`，随快照持久化 | 用同一个 ID 把响应/批次关联回产生它的请求；一次调用内同一时刻只有一个待完成动作（由 `Transition.actions` 只有一项钉住） |
| §4.2「``drive_until_boundary``」 | 名为 `Runner._drive`（驱动循环）+ 结构化入口 | 同一职责，不复用计划里的临时名以免与已存在类型重名 |
| §4.2「配置开关仅可短期迁移」 | **未引入任何开关** | S1–S3 的兼容由「同一 Runner + 薄包装 + `LoopSuspended`」完成，不需要在运行时二选一 |
| §5 S0「旧循环 B1a」 | reducer 里保留了这一检查，但语义变为「恢复后额度已用尽」的闸门 | 无 offset 时它不可达；S6 修好恢复预算后它变成 C03 的关键分支 |
| §5 S5「已取消时跳过整批」 | **回退**，改为沿用执行器逐调用短路 | 跳过整批会改变 `before_tool_call` 调用次数与既有 `Operation aborted` 断言；“不新派发”由执行器保证并有测试 |
| §6 C09/C10 的 Harness 侧组合 | 复用既有 T07–T10 窗口；本轮新增的是 Core 提交端口的同窗口（T22） | 不重复造第二套故障注入 |

## 15. 独立审计发现的回归与修复（2026-10-15 复审）

**审计发现（成立，已复现）**：`Runner` 只在构造/初始状态时取一次可执行工具集，`prepare_next_turn` 换掉
context 后，**声明**换了、但**执行**仍用启动时那份工具列表。后果与旧循环相反：

| 场景（第二轮调用工具） | 旧循环 | 修复前的 Runner |
| --- | --- | --- |
| hook **移除** `t1`（`context=[t2]`，调用 `t1`） | `Tool t1 not found`，不执行 | **`t1` 被执行** |
| hook **新增** `t3`（`context=[t1,t3]`，调用 `t3`） | 执行 | **`Tool t3 not found`** |

即：被收回的工具仍可被调用并落副作用，被授予的工具反而调不到。这属于「改变现有可观察行为」，
而 768 条测试当时没有覆盖该路径，因此没有被任何门禁拦住 —— 审计的判定是对的。

**修复**：`core/runner.py` 把「可执行集合」与「声明」分开持有并同步替换。
`Runner._executable` 是驱动侧的一半（`ToolSpec` 带 callable，绝不进状态/事件），
`_initial_state` 从当前 context 取，`_prepare_turn` 在 hook 换 context 时**同时**替换它（`context.tools or []`
—— 空列表就是「没有可执行工具」，与旧循环一致）；reducer 仍只记录 `ToolDeclaration`。

**新增回归测试**（`tests/core/test_core_prepare_turn_tools.py`，5 条，修复前 3 条红、修复后全绿）：

- 被收回的工具：handler 从未被调用，结果 `is_error=True` 且文本含 `not found`；
- 被授予的工具：handler 恰好被调用一次；
- 空工具集：换 context 后所有调用都 `not found`（不是"沿用旧集合"）；
- 只换 messages（对照组）：工具集合保持不变，调用照常执行；
- 声明与可执行集合不漂移：`TurnPrepared.context_tools` 与 `Runner._context(state).tools` 名字必须一致，
  且事件里只允许出现 `ToolDeclaration`。

**同时修掉的文档问题**：§12 的验证表数字滞后（762/752/738 → 773/763/749）；`core/__init__.py`、
`core/types.py`、`core/stream.py`、`core/messages.py` 与 `tests/core/test_core_messages.py` 里指向
**本仓库不存在**的旧宿主路径（`backend/agent_core`、`packages/agent-harness/...`、
`docs/reference/pi-0.86.1/upstream/...`、`tests/test_agent_core_boundaries.py`）已改为「包名 + commit」
或真实存在的守卫（`tests/test_import_fence.py`）；`docs/contract.md` 的宿主 shim 示例改为明确的假设路径。

### 审计场景的前后对照（可直接复现）

审计用 `git archive 1c9ec7a` 对比旧版本；本节记录同一探针在本仓库的实测输出（探针逐字复现审计表格中的两个场景：
第一轮调用一个保留工具以触发下一轮，`prepare_next_turn` 换 context，第二轮调用目标工具）。

```bash
# 旧版本（重构前）
git archive 1c9ec7a | tar -x -C /tmp/oldrev && PYTHONPATH=/tmp/oldrev/src python /tmp/probe_tools.py
# 当前 HEAD
PYTHONPATH=src python /tmp/probe_tools.py
```

| 场景 | 旧版本 `1c9ec7a` | 修复前（`dee80ef` 一带） | 当前 HEAD |
| --- | --- | --- | --- |
| hook 移除 `t1`，第二轮调 `t1` | `NOT FOUND`，handler 只跑 `c0` | `EXECUTED`，handler 跑 `c0,c1` | `NOT FOUND`，handler 只跑 `c0` |
| hook 新增 `t3`，第二轮调 `t3` | `EXECUTED`，handler 跑 `c0,c1` | `NOT FOUND` | `EXECUTED`，handler 跑 `c0,c1` |

即：修复后两个方向都与重构前逐字一致；`tests/core/test_core_prepare_turn_tools.py` 把这四条断言固化在门禁里，
`docs/verification.md` 的 T24 行指向它。

## 16. 派发前持久提交接线修复（2026-10-15）

S4 登记的「提交端口」在 Core 已经存在，但 `DefaultHarness` 的 sink 只更新内存（`holder["record"]`），
没有调用 `Persistence.commit_transition`：Runner 等到的是内存替换，不是事务提交。本节记录接线修复后的新契约，
任务书见 [修复方案](harness-durable-commit-fix-plan.zh-CN.md)，实测矩阵见 [验证记录](verification.md)。

**谁提交什么**（按执行边界，不按 token/delta）：

| 相位 | 提交者 | 同一事务里写下的内容 |
| --- | --- | --- |
| `REQUESTING_MODEL` / `RUNNING_BATCH` / `AWAITING_PREPARE` / `AWAITING_STOP_HOOK` | `_commit_execution_progress`（sink） | `status=running`、`execution=state_to_record(state)`、`messages`、`turns_used`、`pending_interaction=None` |
| `SUSPENDED` | 同上 | `status=suspended`、暂停快照、批次分区、`pending_interaction`、`approval_requested`/`run_suspended` 事件 |
| `FINISHED` | `_commit_final`（唯一负责者） | 终态状态、结束原因、消息、预算、唯一一条 `run_finished` |

- 候选记录以 `proposed=` 传入 `_commit`：只有提交成功才写入 `holder`，失败不会把未提交的候选当成权威状态。
- `expected_version` 与 lease 随每次提交一起校验；lease 过期、CAS 冲突、并发取消都会中止本次派发（`StorageConflict` 原样向上传，不自降级成模型错误）。
- hook 不再自行提交暂停：`_make_suspend_hook` 只把批次事实与宿主决定暂存在 `holder["pending_suspend"]`，由拿到 reducer 暂停状态的一方在同一事务里提交，避免出现 `suspended + 旧 RUNNING_BATCH 快照` 的中间态。
- transcript 只有一条规则：`RunRecord.messages` 与已提交的 `ExecutionState.messages` 一致。中断（取消/超时）时只追回「已宣告但尚未提交」的消息（`_Drive.uncommitted`），不按内容去重。
- `resume` 与 `start` 共用同一个 sink；恢复中的第一次执行中提交会把记录从 `SUSPENDED` 转回 `RUNNING`（attempt、上下文、预算与已解除的交互同步）。

**恢复窗口的实际支持范围**：

| 窗口 | 处理 |
| --- | --- |
| 快照已到 `FINISHED`，进程死在写终态之前 | `_recover_orphan` 把快照的结论作为终态事实提交（没有派发） |
| 崩在工具派发窗口（`batch_outcome_unknown`） | reconciliation 暂停，列出声明的调用；不自动重放 |
| 崩在模型请求中（`model_request_outstanding`） | reconciliation 暂停并说明“响应未提交、不可能有工具调用被派发”；因无批次记录，不自动重开请求 |

**未完成事项（独立 P0）**：`unknown -> executed/failed` 的判定责任人、可信证据来源、核对入口与审计规则仍未确定；证据不足时继续保持 unknown。本轮通过 F01–F10 不等于关闭该事项，也不等于宣告 safe-replay 闭环完成。
