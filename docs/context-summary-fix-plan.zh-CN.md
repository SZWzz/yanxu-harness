# 摘要取消与运行预算隔离修复计划（DeepSeek 实施）

日期：2026-10-16。核对基线：`6270dff`。

状态：**已实施（R1/R2 修复、回归测试与文档同步已完成；独立复核待做）**。本文是修复任务书；实施记录见文末第 10 节，完整证据见[验收复核](context-engineering-acceptance-review.zh-CN.md)。

## 1. 本轮目标与范围

修复上下文工程交付中的两项缺陷：

| 编号 | 优先级 | 已确认的问题 | 修复后的行为 |
| --- | --- | --- | --- |
| R1 | P1 | 摘要捕获外部 `CancelledError` 后正常返回，运行期限到点仍调用主模型 | 外部取消继续传播；运行期限与用户取消沿既有 Harness 契约结束；主模型不再派发 |
| R2 | P2 | 共享 `SummaryCompactor.usage` 让独立运行互相消耗额度、串写开销 | 摘要预算、占用、结算和输出事实均归属逻辑运行；不同运行互不影响 |

基线独立门禁结果为 **897 passed / 1 skipped，本地门禁 6/6 通过**，但额外探针复现了上述两项问题。因此必须新增能在基线上失败的回归测试，不能仅凭原有门禁全绿交付。

保持单一 Runner、纯 reducer、Gateway 账本、原始历史与请求视图分离，以及派发前真实持久提交的现有设计。本轮不引入新的 agent loop，不重写上下文引擎，不实施多任务编排、subagent 或通用分布式摘要调度。

Python 最低版本仍为项目声明的 3.10；不得直接依赖 3.11 才有的 `TaskGroup`、`asyncio.timeout` 等 API。

## 2. 必须保持的约束

1. 外部取消是控制信号，不能转成摘要成功或普通回退。
2. 摘要自身的限时或普通失败，在父运行仍有效时，可以按现有策略回退到确定性投影。
3. 已开始的辅助请求占用摘要调用额度。取消、异常、恢复和缓存淘汰都不能把它重新变成未调用；无法确认的 token 开销继续标记为未知。
4. 预算归属于 `namespace + session_id + run_id`，更换 `attempt_id` 不产生新预算。身份编码沿用现有约定；如调整编码，须兼容已持久化的 scope，不能直接切换造成旧记录失配。
5. 同一运行并发准备时，预算检查与预留必须先于外部调用且不可穿插超额；不同运行不能因为部署级锁而串行等待整个摘要请求。
6. `context_state.summary` 必须描述本次运行自身的事实，不能通过读取共享对象上的“最后一次 usage”生成。
7. 原文、工具调用与结果的依赖关系、摘要来源范围、Provider 最终请求预算检查保持现有契约。

## 3. R1：修复取消传播

### 3.1 改动入口

- [compaction.py](../src/yanxu_harness/context/compaction.py)：`SummaryCompactor.compact`、`_summarize`、`_summarize_once` 和进行中任务管理。
- [controller.py](../src/yanxu_harness/context/controller.py)：`prepare_async` 的异常传播与开销事实出口。
- [default_harness.py](../src/yanxu_harness/runtime/default_harness.py)：核对 `_run_task`、`cancel` 和准备回调的接线；仅在取消记账或清理确有需要时做最小修改。

当前根因是 `compact` 的 `except asyncio.CancelledError` 返回 `_baseline(...)`。运行期限通过 `asyncio.wait_for` 取消循环任务后，这个分支让循环继续执行。不能靠调整 timeout 数值或只补查一次 cancellation token 掩盖根因。

### 3.2 取消和超时的处理表

| 触发来源 | 准备层处理 | Harness / 调用者看到的结果 | 主模型 |
| --- | --- | --- | --- |
| 调用者取消 `prepare_async` 任务 | 必要清理、结算后重新抛出取消 | `CancelledError` | 不派发 |
| `Harness.cancel` | 不吞取消，遵循既有取消令牌与驱动任务协议 | `cancelled` | 不派发 |
| `max_runtime_seconds` 到期 | 不吞驱动任务收到的取消 | `usage_limited / max_runtime_exceeded` | 不派发 |
| 仅摘要自己的 timeout 到期，父运行仍有效 | 结束该辅助请求并返回有界回退 | 保留现有回退契约 | 根据有效投影与主请求预算决定 |
| 摘要普通异常，父运行仍有效 | 记录失败与未知开销，按现有策略回退 | 保留现有回退契约 | 同上 |

局部 timeout 与外部取消重叠时，不能无条件把父运行的取消降格成回退。除取消传播外，也核对 Runner 在准备完成至派发之间既有的停止检查。

### 3.3 任务所有权与清理

本轮建议使摘要进行中任务归属运行级状态。不同运行不共享可取消的进行中任务；相同运行、相同摘要来源的并发请求可以复用一次辅助调用。

对相同运行的共享调用明确区分“取消一个等待者”和“取消整个运行”：

- 一个等待者退出，不应误杀仍有合法等待者使用的调用。
- 运行取消，或已没有等待者需要该调用时，取消并回收辅助任务；不再发布其结果或写入成功缓存。
- 不用无界 `shield` 把摘要留在后台运行。若为解除等待者与共享任务的取消耦合使用保护，必须同时实现所有者取消、最后等待者退出和任务回收。
- `_in_flight` 在成功、异常、取消和超时路径都能释放；只有完成且通过来源校验的结果才能发布。
- 清理与开销结算保持幂等，避免内层记录一次、外层又记录一次。

遵循当前适配器的协作式取消契约。有界清理不代表能强行终止拒绝响应取消的第三方协程；实现与交付记录不得作出这种保证，也不能为等待它而无限阻塞运行结束。

## 4. R2：将摘要状态绑定到逻辑运行

### 4.1 推荐结构

保留部署级 `ContextController` 的复用方式，在摘要组件内部增加明确的运行级状态对象。名称由实现者按项目风格确定，无需为本次修复大幅增加公共类型。

```text
共享 ContextController / SummaryCompactor
  ├─ 配置：projector、summarizer、SummaryBudget
  ├─ 有界结果缓存：沿用 scope、来源 head、策略等校验
  └─ 运行级状态（按逻辑运行身份隔离）
       ├─ 已结算 usage 与未知开销
       ├─ 当前调用预留 / 进行中请求
       ├─ 同运行预算准入保护
       └─ 恢复来源与生命周期

prepare(scope, prior)
  → 取得该运行状态并恢复可信记录
  → 查缓存 / 原子检查与预留预算
  → 发起摘要并结算，或安全回退 / 传播取消
  → 返回该次准备对应的 usage 快照
  → context_state.summary
```

不能只把 `self.usage` 改成字典而继续让 `_compaction_facts` 读取全局最后值。建议让 `CompactionResult` 或内部等价结果携带不可变的 usage 快照，`_compaction_facts` 直接使用这个快照，确保结果、scope 和计量来自同一次准备。

### 4.2 预算准入与结算

1. 缓存命中沿用现有范围与来源校验，不新增一次实际调用费用；也不能借缓存把其他运行的计量复制进来。
2. 新调用在第一个可能让出执行权的外部 await 之前完成预算检查与预留。
3. 同一运行、不同来源同时请求摘要时，共享同一调用上限；`max_calls=1` 必须最多启动一次外部调用。
4. 同一运行、相同来源的复用请求，只预留和结算一次。每个等待者拿到与其准备结果一致的计量快照。
5. 发起后失败、取消或超时仍消耗一次调用；已知 usage 如实记入，未确认 usage 不编造为零。
6. 未发起的预算拒绝不收费。取消发生在发起前与发起后，测试需分别明确。

现有字符限制究竟是单次限制还是累计限制，以 `SummaryBudget` 的公开契约为准；本轮不要顺带改变其语义。检查所有限制时都不能引用其他运行的计量。

### 4.3 prior 恢复与兼容

- 同一运行的下一轮与新 attempt 恢复，继续使用该运行已有额度。
- 新运行无 prior 时建立自己的初始状态，不读取上一运行的 usage。
- 恢复只合并同一运行的事实。当前 `restore` 依据全局调用次数拒绝较小值的方式必须移除或改为严格限定在同一运行内。
- 活跃运行收到较旧的同运行 prior，不能回滚已经预留或已结算的开销；不同运行计数的大小无可比性。
- 新格式若增加身份或版本字段，明确校验；未知版本、显式身份不匹配不能默默接受。旧格式缺少身份时，只能由已经完成身份校验的宿主记录绑定到当前运行，不能猜测归属。
- 兼容现有直接使用 `SummaryCompactor`、`SummaryUsage`、`restore` 的调用方式。对无法安全支持的多运行旧用法，给出明确错误与迁移说明；不能让旧接口悄悄读写不确定的运行。

### 4.4 生命周期

避免无界增长的运行状态表。运行结束后可以释放任务与锁，但缓存淘汰、状态释放不意味着同一逻辑运行重新获得预算。

Harness 路径从该 run 的持久记录恢复；独立控制器使用者需要明确的运行状态持有/恢复/关闭约定。若仅有 scope 而没有可恢复事实，不能把“之前的状态已被淘汰”当作新的运行。实现者必须说明这一边界并测试选定的行为。

## 5. 取消、计量与持久化的交界

正常准备结果目前通过 `RequestPrepared → state_sink → commit_transition` 持久化；取消可能发生在准备结果产生之前。因此只修复 `CancelledError` 的 `raise`，仍需确认已发生的摘要费用如何进入最终运行记录。

实施时先画清楚“预留、发起、结算、取消、最终提交”的顺序，再按现有持久提交机制做最小补充：

1. 正常完成时，将本次运行的计量快照随准备事实提交，继续遵守派发前提交要求。
2. 协作式运行取消或期限结束时，把已经发生的摘要调用和未知开销带入运行最终记录，不能只留在 compactor 内存。取消不产生可用摘要视图，也不能触发主模型请求。
3. 不绕过存储版本校验、租约与提交 fencing 直接写数据库；不另建一套与 `RunRecord` 竞争的摘要账本。
4. 最终取消记录必须可在新建控制器后读取到正确开销；正常暂停/恢复必须继续受已用额度约束。
5. 对“外部摘要已发出，但进程在开销提交前崩溃”的窗口单独说明。只有存在持久预留或可对账事实时才能声称恢复后不会重新获得额度；未实现这种能力时明确保留限制，不能宣称外部费用恰好一次。

第 5 项属于需要诚实说明的崩溃边界，不要求本轮建设通用外部调用账本。不得因此削弱第 2 项正常协作式取消时的开销保存。

## 6. 分阶段实施与提交

| 阶段 | 工作 | 完成条件 |
| --- | --- | --- |
| S0：红测与契约 | 将验收探针变成带断言的测试；确定取消所有权、运行状态生命周期与旧格式恢复策略 | R1、R2 用例在基线失败，失败原因与复现一致 |
| S1：取消修复 | 外部取消传播、局部 timeout 回退、辅助任务清理与幂等计量 | 真实 Harness 期限和显式取消均不再派发主模型；局部 timeout 仍可正常回退 |
| S2：运行隔离 | 引入运行级状态、预算预留、结果计量快照与受控恢复 | 顺序、并发、交错恢复均不串账；同运行不能超额 |
| S3：持久与兼容 | 接通取消最终开销记录；覆盖 SQLite 重建、旧记录、直接调用接口和状态释放 | 恢复不补发额度；已持久提交与历史保留行为不回退 |
| S4：交付验收 | 运行定向测试与完整门禁，更新复核状态和相关文档 | 给出实际提交、命令、结果与剩余边界 |

建议按上述依赖形成小提交；红测可以与对应修复一同提交，但交付记录要保留其基线失败证据。不要将无关重构混入修复。

## 7. 必须覆盖的测试矩阵

并发测试使用 `asyncio.Event` 等握手，确保取消确实发生在摘要等待期间；不要主要依赖几十毫秒的 sleep 猜测执行顺序。真实期限用例可使用短而有余量的超时，并增加独立的测试兜底超时，避免失败后挂住整套测试。

| 编号 | 场景 | 核心断言 |
| --- | --- | --- |
| T1 | 直接取消正在摘要的 `prepare_async` | 上层收到取消；无成功结果、无迟到发布；进行中任务回收 |
| T2 | 真实 `DefaultHarness` 运行期限在摘要中到期 | 主模型 0 次；`usage_limited / max_runtime_exceeded`；记录已发生开销 |
| T3 | 真实 `Harness.cancel` 在摘要中触发 | 主模型 0 次；`cancelled`；取消清理与开销保存 |
| T4 | 仅摘要自己的 timeout 或普通异常 | 父运行有效时安全回退；原文完整；调用计数不退回，未知费用保持未知 |
| T5 | 无摘要的快速路径 | 原有取消、预算与请求准备行为不回退 |
| T6 | 同一控制器顺序运行 A、B | 各实际调用 1 次，各只记录自己的开销 |
| T7 | 同一控制器并发运行 A、B | 各自有额度；内容、来源、scope、usage 不串 |
| T8 | 同一运行下一轮及新 attempt 恢复 | 已用 `max_calls=1` 时不再调用；内存与 SQLite 重建结果一致 |
| T9 | 高计数 A 与低计数 B 交错恢复 | 按身份恢复；旧 prior 不回滚同运行开销；不跨运行取最大值 |
| T10 | 同运行、不同来源并发准备，`max_calls=1` | 实际辅助调用最多 1 次，拒绝者无虚假新增费用 |
| T11 | 同运行、相同来源复用，取消一个等待者 | 合法等待者仍按所有权策略完成；一次调用只结算一次；整体取消能清理 |
| T12 | 缓存命中、状态关闭与淘汰后重新进入 | 无额外调用计费、无跨 scope 泄漏；释放对象不补发额度 |
| T13 | 旧格式 prior、错误身份与未知版本 | 按明确兼容规则恢复或拒绝，无静默串账 |
| T14 | 取消前未发起与发起后取消 | 未发起不收费；已发起费用保存，最终记录与内存一致 |

T10–T13 是本次修复必须守住的相邻边界，不表示这些场景已经全部在基线上独立复现为缺陷。

优先扩展现有 [摘要测试](../tests/test_context_summary.py)、[接线测试](../tests/test_context_wiring.py)、[上下文持久性测试](../tests/test_context_durability.py) 和 [运行隔离测试](../tests/test_runtime_isolation.py)。若测试明显变得难读，再拆出聚焦文件。

## 8. 验证与交付

仓库根目录先运行相关测试：

```bash
.venv/bin/python -m pytest tests/test_context_summary.py tests/test_context_wiring.py tests/test_context_durability.py tests/test_runtime_isolation.py tests/test_runtime_execution_snapshot.py tests/test_runtime_resume.py
```

定向测试通过后运行一次本地完整门禁：

```bash
.venv/bin/python scripts/ci_check.py --offline --wheelhouse build-wheelhouse
```

若本地离线依赖不可用，按仓库既有流程准备依赖并记录实际执行方式，不删减检查伪造通过。发生生产改动或新失败后，再运行受影响检查；不需要无理由反复跑全套。

重新执行[验收复核中的探针](context-engineering-acceptance-review.zh-CN.md#可直接运行的基线复现)，预期至少变为：

```text
independent_run_budgets:
  actual_a = 1
  actual_b = 1
  reported_a = 1
  reported_b = 1

deadline_during_summary:
  summary_calls = 1
  main_model_calls = 0
  status = usage_limited
  error_code = max_runtime_exceeded
```

最终交付记录必须包含：

- 实际修复提交、改动文件、关键设计决策，以及新增测试对应的场景。
- R1 / R2 的修复前后证据和实际测试数量；不要把基线 `897` 当作修复后应固定的总数。
- 取消来源的区分方式、共享任务所有权、计量恢复规则与生命周期说明。
- 旧持久格式和直接使用 compactor 的兼容说明；崩溃记账窗口等尚未覆盖的边界。
- 更新[验收复核](context-engineering-acceptance-review.zh-CN.md)状态；按实际变化同步[上下文工程实施计划](context-engineering-implementation-plan.zh-CN.md)与[当前架构说明](current-harness-architecture.zh-CN.md)，不提前写成已完成。

本轮验收不要求真实模型质量评测。也不能因脚本化测试通过就声称真实模型、跨平台或外部调用计费一致性全部得到验证。

## 9. 可直接交给 DeepSeek 的任务

> 请实施 `docs/context-summary-fix-plan.zh-CN.md`。先读取仓库适用规范和 `docs/context-engineering-acceptance-review.zh-CN.md`，核对当前提交与基线差异；若问题已被部分修复，补齐缺少的行为与回归测试。先证明 R1 取消吞噬和 R2 运行预算串账的红测，再按 S1–S4 完成修复。重点检查真实 Harness 的期限/取消结果、运行级预算准入、计量快照以及取消最终记录。保持 Python 3.10、单一 Runner 和派发前持久提交契约。完成定向测试和完整门禁，更新复核文档，报告实际结果与剩余限制；不要扩展到新的 agent loop 或通用调度系统。

## 10. 实施记录

改动文件：

- `src/yanxu_harness/context/compaction.py`：运行级状态 `_SummaryRun`（按 scope 隔离的 usage、预留计数、进行中调用）与共享调用 `_SharedCall`；预算检查与预留同步完成于首个外部 `await` 之前；共享调用按“最后等待者退出才回收”处理，外部取消在结算后继续传播；`restore(prior, scope=...)` 校验记录身份与版本；`release_run`/`run_limit` 提供有界生命周期。
- `src/yanxu_harness/context/controller.py`：`prepare_async` 按 scope 恢复；`_compaction_facts` 使用 `CompactionResult.summary_usage` 快照；新增 `summary_facts(scope)` 供取消收尾读取。
- `src/yanxu_harness/runtime/default_harness.py`：准备回调捕获取消后记录已发生开销并重新抛出；终态记录合并该摘要事实；`_apply_result` 增加 `context_state` 入参。
- `src/yanxu_harness/context/__init__.py`：导出 `SUMMARY_FACTS_VERSION`。
- `tests/test_context_summary_isolation.py`：T1–T14 回归。

基线与修复后证据（同一份测试，同一命令）：

```text
# 基线（仅 stash src/，保留新测试）
.venv/bin/python -m pytest tests/test_context_summary_isolation.py -q
13 failed, 5 passed
# 其中 T1 为 "DID NOT RAISE CancelledError"，即 R1 根因；T6/T7 为 R2 串账。

# 修复后
.venv/bin/python -m pytest tests/test_context_summary_isolation.py -q
18 passed
```

关键设计决策：

1. **取消是控制信号**：`compact` 捕获 `CancelledError` 后只做结算与回收，再原样抛出；摘要自身的 `TimeoutError`/普通异常仍在父运行有效时回退确定性投影。
2. **所有权与回收**：多个等待者 `shield` 同一个辅助任务；等待者各自退出只递减计数，计数归零才取消并回收；不把摘要留在后台无界运行。
3. **计量恢复**：`restore` 只在同一运行内比较计数（旧 prior 不回滚已花费），跨运行不取最大值；带 `scope`/`summary_schema_version` 的新格式显式校验，缺身份的旧格式由已完成身份校验的宿主记录绑定。
4. **生命周期**：`release_run(scope)` 只释放内存、不补发额度；`run_limit` 只淘汰无进行中调用的运行。仅有 scope、没有可恢复事实时无法区分新运行与已淘汰运行，这一边界由宿主通过传入已提交 prior 保证；独立控制器使用者必须遵守同样的持有/恢复/释放约定。
5. **崩溃边界（未覆盖）**：“外部摘要已发出、进程在开销提交前崩溃”的窗口仍未建立持久预留或对账能力，因此不宣称恢复后绝不会重新获得额度，也不宣称外部费用恰好一次。第 5 节第 2 项（协作式取消/期限终止时保存已发生开销）已实现并测试。

兼容性：旧的 `SummaryCompactor.compact`/`usage`/`restore(prior)` 单运行用法继续可用（首次 `compact` 接管无 scope 的 prior）；当同一 compactor 已服务多个命名运行后再调用无 scope 的 `restore`/`usage`，会明确报错而不是猜测归属。
