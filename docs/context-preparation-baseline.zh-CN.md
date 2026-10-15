# 上下文准备基线（C0）：实测缺口、能力清单与先失败用例

日期：2026-10-15。计划：[上下文工程实施计划](context-engineering-implementation-plan.zh-CN.md)（本篇对应其 C0）。所属路线：[Harness 四方向开发路线图](harness-evolution-roadmap.zh-CN.md)。基线提交：`f30a1cb`（前置的[持久提交修复](harness-durable-commit-fix-plan.zh-CN.md)已交付）。

本文件只记录**实测**事实与**已登记**缺口，不把接口存在当作已实现。C1–C6 每完成一项，就翻转或删除这里对应的行，并在 [verification.md](verification.md) 记录新证据。

## 1. 前置：持久提交修复的最终状态（已核对）

| 项 | 实测 |
| --- | --- |
| 交付提交 | `3193551 fix: persist harness execution before dispatch`、`f30a1cb docs: record durable commit boundary verification`；工作区干净 |
| 权威 transcript | `RunRecord.messages` 跟随**已提交**的 `ExecutionState.messages`，中断时只追加 `_Drive.uncommitted` |
| 提交端口 | `Runner._drive` → `AgentLoopConfig.state_sink` → `DefaultHarness._state_sink`（`runtime/default_harness.py:939`）→ `_commit_execution_progress`（:969）→ `_commit`（:1232）/ `_commit_final`（:1257）；`start` 与 `resume` 共用同一 sink（:568、:817） |
| 暂停语义 | suspend hook 只暂存（`_make_suspend_hook`，:1034）；拿到 `SUSPENDED` 的那次事务一次写入 status + 快照 + 批次分区 + `pending_interaction` + 事件 |
| 终态语义 | `_commit_final` 是唯一终态提交者，不覆盖已终态记录；提交失败/lease 过期/CAS 冲突在派发前抛出 |
| 复核命令 | `.venv/bin/python -m pytest -q tests/test_runtime_commit_boundaries.py` → **20 passed** |

结论：本计划 §3.1 要复用的「完整 transcript」就是 `ExecutionState.messages` / `RunRecord.messages`；请求投影只能作为**派生物**存在，不能删减它。这一条同时是 [core-state-machine-migration.zh-CN.md](core-state-machine-migration.zh-CN.md) §16 的结论。

## 2. 本次执行的命令与数量

| 命令 | 结果 |
| --- | --- |
| `.venv/bin/python -m pytest -q tests/test_context.py tests/test_context_archive.py` | **20 passed**（与计划 §2.2 记录的基线一致） |
| `.venv/bin/python -m pytest -q tests/test_runtime_commit_boundaries.py` | **20 passed** |
| 四个边界探针（临时脚本，`TranscriptEntry + WindowedProjector`，见 §3） | 四个现象全部复现，输出见下表 |
| `.venv/bin/python -m pytest -q tests/test_context_boundaries.py` | **6 xfailed**（即 §6 的六个先失败用例） |
| 同一文件加 `--runxfail`（让失败显形） | **6 failed**，每条失败信息即 §6 的「当前事实」列 |
| `.venv/bin/python -m pytest -q tests/test_context_measurement.py`（C1） | **16 passed** |
| `.venv/bin/python -m pytest -q tests/test_context_measurement.py tests/test_context.py tests/test_context_archive.py tests/test_context_boundaries.py`（C1 阶段目标） | **34 passed, 6 xfailed** |
| `.venv/bin/python -m pytest -q tests/test_context_projection.py`（C2） | **19 passed** |
| `.venv/bin/python -m pytest -q tests/test_context.py tests/test_context_archive.py tests/test_context_measurement.py tests/test_context_projection.py tests/test_context_boundaries.py`（C2 阶段目标） | **59 passed, 2 xfailed** |

## 3. 四个边界探针的实测输出（计划 §2.2 复现）

用 `InMemoryTranscriptArchive` 写入、`WindowedProjector.snapshot()` 投影；每个探针独立归档。

| # | 输入 | 当前结果 | 目标 | 待修阶段 |
| --- | --- | --- | --- | --- |
| 1 | `system("you are terse")` + `user("hello")`，`char_budget=10`，`keep_system_entries=True` | 只剩 `user("hello")`，`dropped_entries=1`；`keep_system_entries` 是**无消费者的参数** | 有效系统/工具基线必须能重建；参数不得静默无效 | C2（E05） |
| 2 | 单条 100 字符消息，`char_budget=10` | 100 字符**全保留**，`dropped_entries=0`，`estimated_tokens=25` | 必需内容超限时明确失败，不能报告「已满足预算」 | C1/C2（E01） |
| 3 | 非空消息，`capacity=0` | 仍保留 1 条（`estimated_tokens=10`） | 不派发非空超限请求 | C2（E01；派发层已由 C1 的 `RequestBudget` 拒绝，见 §9） |
| 4 | `assistant(content, tool_calls=[call-1])` + `toolResult(tool_call_id=call-1)`，`char_budget=2` | 只剩孤立的 `toolResult`，调用消息被丢弃 | 调用与结果不可拆成孤立消息 | C2（E04） |

探针 1 的成因：`archive.py` 的 `WindowedProjector.snapshot` 从最新向前累加，`if kept and used + size > budget: break`——最新一条永远进窗口，且**从不检查** `keep_system_entries`。探针 3 的另一半是单位混用：`budget = min(char_budget, capacity)` 把 token 容量当字符预算花。

**C2 之后（已解除标记）**：四个现象都已在 `WindowedProjector` 里修好，并由 `tests/test_context_boundaries.py` 的前四个用例持续把关（断言改为目标行为：必需片段放不下时 `BudgetExceeded`、参数真的决定谁被丢、零容量不返回空视图、调用与结果同进同出）。受管路径的对应行为（重放基线、状态等价、宿主强制保留）另有 `tests/test_context_projection.py` 的 E04–E07 用例，见 §10。

## 4. 已有能力清单（逐条核对过，可复用）

| 落点 | 已有内容 | C0 核对结论 |
| --- | --- | --- |
| `context/policy.py` | `ContextWindow`（`limit_tokens`/`output_reserve`/`safety_margin`，`request_capacity` 未知时返回 `None`）、`ContextPolicy.assess/require`、`ContextAssessment`、`TranscriptArchive`/`ContextProjector` 协议、`SummaryResult`/`SummaryArtifact`/`validate_publication`、`Summarizer` 协议 | 复用；`validate_publication` 已做 scope + head + lease 三重校验，且拒绝空摘要 |
| `context/archive.py` | `InMemoryTranscriptArchive`（追加、fingerprint、head）、`WindowedProjector`、`ProjectionSnapshot`（`source_head`/`dropped_entries`/`is_stale`）、`_cache` | 只提供字符预算的粗投影；无片段依赖、无计量端口、缓存无上限 |
| `core/loop_config.py` | `transform_context`、`convert_to_llm`、`project_context`、`persist_transformed_context`、`prepare_next_turn`、`should_stop_after_turn`、`state_sink`、`max_turns`、`initial_turns_used` | 唯一注入面；**`DefaultHarness` 不透传它们**（见 §5） |
| `core/runner.py::_request_model` | 请求转换、投影闭包、`ContextProjected` 输入事件；`_RequestView.persist` | 请求视图与原始历史在这里分叉，也是缺口所在 |
| `core/messages.py` | `to_llm_messages`（未知 role 显式拒绝）、`normalize_context`、`resolve_transcript`（不支持中途 system 时折叠）、`get_current_system_prompt`、`get_current_tools`、`without_initial_system_message`、`resolve_transcript_tools`、`get_tool_state_changes` | C2 的「窗口起点重放有效 system/sections/tools 基线」直接复用这些函数，不另写简化状态 |
| `providers/core_bridge.py::to_provider_request` | transcript → `ProviderRequest`（system 折叠、工具声明、`max_output_tokens`、模型名） | 实际请求构建路径；计量必须走同一函数 |
| `providers/openai_compat.py` | `build_body`（公开）、`PayloadGate` 在 `json.dumps(build_body(...))` **之后**同步调用，拒绝即不发 | 最终字节门禁已存在且只看最终 body；C1 复用它，不在线程里跑异步压缩 |
| `runtime`/`storage` | 运行身份、lease/CAS、快照、事件、恢复（上一轮的提交端口） | 新的投影元数据必须挂到**同一提交边界**，不新增「先写摘要库再改状态」的流程 |
| `tests/wire_fixture.py` | 真实 loopback HTTP + 请求记录 | E09/E10 的现成夹具 |

## 5. 已登记的接线缺口与实测证据

| 缺口 | 证据（本节命令均在仓库根执行） |
| --- | --- |
| `context/` 包在 `src/` 里**没有任何生产消费者** | `grep -rn "ContextPolicy\|ContextWindow\|WindowedProjector\|InMemoryTranscriptArchive\|ProjectionSnapshot\|TranscriptArchive" --include="*.py" src/` → 只命中 `src/yanxu_harness/context/` 自身 |
| `project_context` 的闭包没有任何消费者 | `grep -rn "_project_context_for_budget" --include="*.py" src/` → 只有 `src/yanxu_harness/core/runner.py:387` 的**赋值**；`CoreStreamBridge` 不读它，`PayloadGate` 只收到 `(body, request)` |
| `DefaultHarness` 没有上下文准备面 | `grep -rn "project_context\|transform_context\|convert_to_llm\|persist_transformed_context" --include="*.py" src/yanxu_harness/runtime/` → 无命中 |
| 投影替换是**破坏性**的 | `core/transitions.py::_context_projected` 执行 `replace(state, messages=tuple(event.messages))`；`persist_transformed_context=True` 时原始历史被投影覆盖（先失败用例 5 实测：`['Q2','Q2b',A2]`，Q1/A1 不可再见） |
| 摘要只有接口与发布校验，没有生成/预算/存储/恢复 | `grep -rn "Summarizer\|SummaryResult\|SummaryArtifact" --include="*.py" src/` → 只有 `context/policy.py` 的定义与 `context/__init__.py` 的导出 |
| 运行记录没有投影字段 | `RunRecord`（`runtime/harness.py:194`）有 `request_fingerprint`、`execution`、`schema_version`，没有 `context_state`；`ExecutionState` 亦然 |

**C3 之后的现状**（上表是 C0 实测的基线，不是今天的描述）：

| 缺口 | C3 的处理 |
| --- | --- |
| `context/` 包无生产消费者 | `DefaultHarness(context_controller=...)` → `AgentLoopConfig.prepare_context` → `context/controller.py#ContextController`，每次请求 1 次；`tests/test_context_wiring.py` 用真实 loopback 验证 |
| `project_context` 闭包无人读取 | **字段与闭包一起删除**（plan §4.2 允许的弃用路径）；`validate_context_configuration()` 在与 `transform_context`/`persist_transformed_context` 同配时派发前拒绝 |
| `DefaultHarness` 无上下文准备面 | 已有；并校验控制器必须暴露 `prepare(payload, *, scope=...)` |
| 投影替换是破坏性的 | 受管模式**不再**发 `ContextProjected`：`request_view` 与 `context_state` 仅作派生视图/git 事实提交，`RunRecord.messages` 保持完整（E07） |
| 摘要只有接口与校验 | 未动（C5）：本轮不引入摘要生成，也不声称已有预算/single-flight/发布 CAS |
| 运行记录没有投影字段 | `RunRecord.context_state` + `ExecutionState.context_state` + `execution.request_view`，与状态同一事务提交；`context_schema_version` 未知时拒绝读取 |

### 5.1 hooks 的调用顺序与次数（当前事实）

在 `Runner._request_model` 中，一次模型请求按此顺序、此次数调用：

1. `transform_context(messages, signal)` —— 1 次；若 `persist_transformed_context=True`，其返回值经 `_RequestView.persist` 成为活动对话，并在请求结束后以 `ContextProjected` 输入事件替换 `state.messages`。
2. `convert_to_llm(request_messages)` —— 1 次（缺省用 `to_llm_messages`）。
3. `project_context(messages, new_messages, model, report, signal)` —— **0 次**；闭包挂到 `options["_project_context_for_budget"]` 后无人调用。若将来被消费，`convert_to_llm` 会在投影后**再调用 1 次**（同一个请求内共 2 次），这条次数与顺序必须在 C3 冻结。

`prepare_next_turn` 每个工具批次后 1 次（可替换上下文与可执行工具集）；`should_stop_after_turn` 每轮 1 次。上下文准备本身不得增加工具执行轮次。

### 5.2 未知预算的当前语义

| 情形 | 当前行为 | 风险 |
| --- | --- | --- |
| `ContextWindow(limit_tokens=None)` | `request_capacity=None`，`assess(...).fits is None`，`require()` **不抛**（只在 `fits is False` 时抛） | 未知窗口永远不会拒绝；是否继续完全由调用方决定（C1 要求把它变成显式的宿主策略） |
| `WindowedProjector.snapshot(capacity=None)` | 只用 `char_budget`，不报告 unknown | 未知容量与「无限」在调用方看来一样 |
| `estimated_tokens` | `used // 4`（字符数除以 4），字段名却叫 tokens | 不能被当作真实计费 token 或 provider 真实限制 |
| `capacity` 与 `char_budget` | `min(char_budget, capacity)` | token 容量被当字符预算花（探针 3） |
| `max_turns=None` | 无上限 | 与预算无关，但同样不得用「缺失 = 0」推断余量 |
| `max_output_tokens` 未声明 | `to_provider_request` 返回 `None` | 输出预留取不到真实默认值时必须报未知，不能按 0 计算余量（C1） |

## 6. 先失败用例：`tests/test_context_boundaries.py`

六个用例断言的是**目标行为**，用 `pytest.mark.xfail(strict=True)` 标记待修阶段：套件保持绿色，但现象一旦被修好，XPASS 会被 strict 变成失败，强制解除标记并更新本篇。每条都用 `--runxfail` 验证过「确实因目标行为未满足而失败」，而非因测试自身错误。

| 用例 | 现象/目标 | 标记 | 解除条件 |
| --- | --- | --- | --- |
| `test_boundary_1_a_declared_system_entry_is_not_silently_dropped` | 当前：`assert 'system' in ['user']` | C2 | 投影保留有效系统基线，或明确拒绝；若 C2 弃用 `WindowedProjector`，把用例改指向受管投影路径（E05） |
| `test_boundary_2_a_message_over_the_whole_budget_is_refused` | 当前：`DID NOT RAISE` | C1/C2 | 必需集合放不下时返回可辨认的拒绝（E01） |
| `test_boundary_3_a_zero_capacity_yields_nothing_to_send` | 当前：`capacity=0` 仍返回 1 条 | C2 | 零容量不产出可发送视图；单位命名后不得把 token 容量当字符预算（E01）。**派发层已由 C1 拒绝**（`tests/test_context_measurement.py::test_e01_a_zero_capacity_request_never_reaches_the_wire`），投影层留给 C2 |
| `test_boundary_4_a_tool_call_and_its_result_are_never_split` | 当前：`toolResult 'call-1' has no declaring assistant message` | C2 | 片段选择保持调用与结果同进同出（E04） |
| `test_boundary_5_a_persistent_projection_does_not_delete_the_conversation` | 当前：`['Q2','Q2b',A2]`，被裁掉的 Q1/A1 不再是历史 | C3（**已解除**） | 受管模式（`prepare_context` + 真实控制器）下，授权对话保持完整，只有请求视图变短；旧 flag 的破坏性保留但不再与受管模式共存（E07/§3.1） |
| `test_boundary_6_a_configured_projection_hook_is_consulted_before_dispatch` | 当前：真实 loopback 请求已发出，hook 调用次数 0 | C3（**已解除**） | 死的 `project_context` 被移除，`prepare_context` 被真实接线并在 `tests/test_context_wiring.py` 端到端断言（E09/E10） |

用例 4 的检查器 `assert_no_orphan_tool_results` 同时校验两个方向：孤立 result、以及「声明了调用却丢了结果」。它是 §3.2 的 Harness 验收契约（不是「provider 一定返回 400」的厂商事实），C2 的 E04 用例复用它。

## 7. 未验证/未执行项

- 尚未有受管上下文控制器、计量端口、片段投影实现；本篇不含任何生产代码改动。
- 未跑完整门禁（`scripts/ci_check.py --offline --wheelhouse build-wheelhouse`）：C0 只改测试与文档，等 C1–C3 落地后按计划 §13 运行并记录。
- 未接真实模型；E09/E10 用 loopback fixture，质量类结论留到 §12 的评测。
- 远程 CI、Python 3.10/3.11、Linux/Windows 均未执行；本轮只在本机 3.12.13（macOS arm64）上测量。

## 8. 下一步（C1 入口）

1. 引入可替换的 `TokenEstimator`/`TokenEstimate`，命名单位并给出 heuristic 标识；把 system + 全部工具 schema + 协议开销 + 输出配置纳入同一份请求计量。
2. 明确输出预留只扣一次；`C = max(0, L - R - safety_margin_tokens)`；未知窗口/未知计量如实报 unknown，由宿主策略决定继续或拒绝。
3. 复用 `build_body` + `PayloadGate` 做最终字节门禁；`capacity=0` 时不派发非空请求。
4. 交付后解除 `test_boundary_2`/`test_boundary_3` 的标记，并在 [verification.md](verification.md) 记录命令与数量。

## 9. C1 交付：完整请求计量与明确预算

新增 `src/yanxu_harness/context/measurement.py` 与 `tests/test_context_measurement.py`（16 条用例）。计量端口不依赖 provider 类：`MeasurementInput.from_request()` 从真实请求对象读 `system/messages/tools/max_output_tokens`，因此**计量的请求与实际发送的 body 出自同一份值**（用例同时断言 `report.payload_bytes` 等于真实 wire body 的字节数）。

| 决定 | 实现 |
| --- | --- |
| 计量覆盖 | `parts = {system, tools, messages, protocol}`；工具按 wire JSON（name/description/parameters/strict）计入，`tool_calls` 的 `function` JSON 计入，重发的 `reasoning_content`/`signature` 计入 |
| 无法计量的部分 | 图片或未知块 → `tokens=None` + `unmeasured=("messages[0]: image_url",)`；`TokenEstimate` 构造时拒绝「有 unmeasured 却给总数」 |
| 输出预留 | `effective_reserve = max(policy reserve, request max_output_tokens)`，只扣一次；来源记为 `request`/`policy`/`unknown` |
| 容量 | `capacity_for()` 走 `ContextWindow.request_capacity`，即既有的 `max(0, L - R - margin)`，不另写公式（有等价性用例） |
| 判定 | 硬失败（tokens > capacity、bytes > max_payload_bytes）为 `False`（未知预留只会让容量更小，已超限的请求不会变合法）；否则任一输入未知 → `fits is None`；全部已知才 `True` |
| 未知策略 | `UnknownPolicy.PROCEED`（报告 unknown 后继续）或 `REFUSE`（派发前拒绝）；未知窗口不是「无限」 |
| 最终门禁 | `payload_budget_gate(budget, estimator)` 即 `PayloadGate`（`(body, request) -> None`），在最终序列化字节上同步执行；拒绝 ⇒ 传输调用 0 次 |
| 诚实性 | 默认估算器 `exact=False`、`method="heuristic-chars-per-token"`，理由写明「不是 provider 计费数字」；heuristic + safety_margin 只能降低超限风险，不承诺 provider 永不报 overflow（那是另一种失败种类，保留原名） |

仍未解除的标记：`test_boundary_1/2/3/4`（投影层，C2）与 `test_boundary_5/6`（接线层，C3）。C1 只证明「计量与派发拒绝是真实的、可监督的」；把估计器接到**真实 Harness 路径**仍是 C3 的工作（见 §5 的接线缺口表）。

## 10. C2 交付：依赖完整的确定性投影

新增 `src/yanxu_harness/context/projection.py` 与 `tests/test_context_projection.py`（19 条用例），`WindowedProjector`（legacy 字符视图）同时被修正。

| 决定 | 实现 |
| --- | --- |
| 片段构造 | `message_fragments()`：连续 system 消息合为一个 `instructions` 片段；声明了调用的 assistant 消息吸收应答它的结果（`tool_call_id` 只在该 turn 范围内关联，不假设全局唯一）；没人声明的结果单独成 `orphan-result`，**不为它编造调用者** |
| 选择 | 只保留**后缀**（丢弃永远从最旧开始）：从中间丢会让重放的基线按历史从未有过的顺序应用状态更新 |
| 不可移除集合 | 最新片段（当前用户输入或当前工具往返）+ 宿主 `mandatory` 指定的消息；两者放不下就 `BudgetExceeded`，不静默丢 |
| 基线重放 | 被丢弃片段的状态更新用 `get_current_system_message()` 重放成一条 `SystemMessage` 放在窗口起点；`system_state()` 比较 `prompt`/`sections`/`tools` 是不是与完整历史等价，不等价就 `ContractError`（被撤回的工具不会复活） |
| 计量 | 用调用方注入的真实转换（测试里就是 `to_llm_messages` → `to_provider_request`）+ C1 的 `TokenEstimator`；先按片段估个起点（缓存），再对真实候选重测，每次多丢一个片段直到装得下或拒绝——次数受片段数上界约束 |
| 确定性 | 纯函数：无时钟、无随机、无字典序依赖；`request_fingerprint`/`effective_state_fingerprint`/`as_dict()` 可重放，相同输入给出同一投影 |
| 不改历史 | 用例断言输入消息的 `message_to_json` 前后一致，且投影里除了合成基线外都是**同一批对象**（`id()` 比较） |
| 不去重 | 文本相同的多条消息全部保留、顺序不变（同句合法出现两次） |
| 可解释 | `ProjectionResult.as_dict()`：scope、source_head、保留/丢弃片段与理由、估算方法与各分项、最终请求指纹、生效状态指纹、`policy_version`；`is_stale()` 对 head 漂移敏感 |
| 缓存上限 | `cache_limit`（默认 256）按插入顺序淘汰；字符视图 `WindowedProjector.cache_limit` 默认 64 |
| 不做的事 | 不截断工具参数或结果正文（C4 的有界视图与原文引用才做），不伪造结果以「凑齐协议」，最小必需集合仍超限时明确失败 |

`WindowedProjector` 的保留/修正方案：**保留但明确定位为 legacy 字符视图**（实现 `ContextProjector` 协议，`capacity` 是字符容量），并修好四个现象、缓存加上限；token 计量与真实请求结构归 `FragmentProjector`，不再把 token 容量当字符预算花。

## 11. C3 交付：受管上下文接入单一 Runner 与真实 Harness

第一批（C0–C3）在本节结束。新增 `src/yanxu_harness/context/controller.py` 与 `tests/test_context_wiring.py`（10 条用例），核心状态机新增一次显式准备步骤。

| 决定 | 实现 |
| --- | --- |
| 显式准备动作 | `RequestModel` 拆成 `PrepareRequest`（无网络、无工具）与 `SendModelRequest`（真正的请求）；准备结果以输入事件 `RequestPrepared(view, context_state, refused)` 回到同一个 reducer |
| 派发前提交 | `RequestPrepared` 的转移经既有 `state_sink` 提交后才执行 `SendModelRequest`；“另一连接能在字节发出前读到投影事实”由 `test_e09_the_projection_facts_are_durable_before_the_bytes_are_sent` 直接证明（闸门自己读 SQLite，并与将发送的 body 逐条比对） |
| 只生成请求视图 | `ExecutionState.request_view` 与 `messages` **分字段**；受管路径不再发 `ContextProjected`；`RunRecord.messages` 永不因投影变短（E07） |
| 投影事实 | `context_state`（`context_schema_version=1`）随状态同一事务提交：scope、source/request/生效状态指纹、估算器身份与方法、容量与输出预留来源、保留/丢弃片段与理由、`refused`；`RunRecord.context_state` + `ExecutionState.context_state` 读到未知 schema 时拒绝 |
| 拒绝是结果 | `ContextController.prepare()` 把“放不下”返回为 `refused`（而非异常）；reducer 转成 `USAGE_LIMITED` + `error_code=context_budget_exceeded` + `LoopStop.CONTEXT_BUDGET_EXCEEDED`，不耗轮次、不请求模型。最终字节闸抛出的 `BudgetExceeded` 在 `SendModelRequest` 内被归一为同一种预算拒绝（与 provider 自己的 context overflow 失败种类区分开） |
| 与旧 hooks 的规则 | `prepare_context` 与 `transform_context` / `persist_transformed_context=True` **同时配置时派发前拒绝**（`validate_context_configuration`）；`convert_to_llm` 允许共存，唯一顺序是“先准备、后转换”，每请求 1 次；`project_context` 字段与死闭包一起删除 |
| Harness 接线 | `DefaultHarness(context_controller=...)`（必须是暴露 `prepare(payload, *, scope=...)` 的对象），`start` 与 `resume` 用同一绑定；scope = `namespace/session_id/run_id` |
| 恢复 | 恢复时**重新准备**（投影是派生物，不授予执行权）：每次 attempt 各准备一次，源指纹随已增长的 transcript 变化；暂停占位结果在原位被替换，不重复追加（E15/E16 的过程级验证仍归 C6） |
| 不做的事 | 无第二套循环、无额外工具轮次；超大必需输出仍明确拒绝（C4 才做有界视图与原文引用）；不引入摘要生成（C5） |

C3 验收对应的实测：`tests/test_context_wiring.py` 10 passed；`tests/test_context_boundaries.py` 6 passed（4 个投影边界 + 2 个接线缺口，`xfail` 标记全部解除）；完整套件与门禁见 [verification.md](verification.md)。
