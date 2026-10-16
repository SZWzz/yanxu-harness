# 诊断事实清单：哪些已经提交、哪些本轮补上、哪些不记录

日期：2026-10-16。适用提交：D1（`feat(diagnostics)` 系列，基线 `2c8a324`）。

本文件是实施计划 §6.1 要求的**缺口清单**。原则：能从已提交状态推导的内容不重复存储；确需新增的事实纳入既有 codec 与原子提交；不能证明的东西不写成事实。

## 1. 已经提交、可直接读取（D1 未新增）

| 事实 | 落点 | 报告里的用途 |
| --- | --- | --- |
| run 身份、状态、停止码、错误码、明细、轮次、最终文本、usage（最后一次助手消息） | `RunRecord`（runs 表） | 结局与原因、attempt 归属 |
| namespace / session / attempt / 投影事实 | `RunRecord` + `ExecutionState.attempt_id`、`context_state` | 读取隔离、来源 |
| 逐调用分区：call_id、工具、outcome、progress、model_action_id、operation_id、receipt 引用、args 摘要、观察摘要、source_attempt、终止标志 | `ExecutionState.batch`（`BatchCallState`） | 逐调用解释、证据引用 |
| 调用身份基线：config/tools/instructions/policy 指纹 + exposure manifest | `ExecutionState.arrangement` | 漂移拒绝的原因与比较来源 |
| 上下文投影：范围、source head、指纹、估算器身份/方法、估算 token、容量、保留/丢弃计数、拒绝原因 | `record.context_state` | 预算拒绝、投影解释 |
| 摘要计量：calls、input_chars、output_chars、宿主报告的 usage、`usage_unknown`、已发布范围 | `record.context_state` 的摘要事实 | 摘要成本（与主模型分开） |
| 事件与顺序 | 事件表（全局 cursor、`at` 墙钟、run_id、kind、payload） | 时间线、增量读取边界 |
| 操作账本 | `SqliteExecutionBackend`（operation_id、state、回执、证据） | 权威回执（只读引用，不复制） |

## 2. D1 新增的持久事实

| 新事实 | 形状 | 为什么要新增 | 原子性 |
| --- | --- | --- | --- |
| 真实请求派发窗口 | 事件 `model_request_dispatched`：`request_instance`、`model_action_id`、`attempt`、`started_at`（墙钟） | 逻辑 `model_action_id` 在恢复/重试时会重复出现；没有真实请求实例就无法按实例去重 usage，也无法把"发出去但没回来"写成事实 | 与 `Phase.REQUESTING_MODEL` 的提交同一事务（派发前已提交） |
| 真实请求完成计量 | 事件 `model_request_completed`：`request_instance`、`attempt`、`started_at`、`duration_ms`（单调）、`clock`、`payload_bytes` + 估算器名、`usage` | 耗时与成本的唯一落点；中断时该事件不存在，读取端应显示 incomplete 而不是 0 | 与接受该响应的状态（批次声明 / stop hook / 暂停）同一事务 |
| 逐调用 span | 事件 `tool_call_observed`：`call_id`、`tool_name`、`attempt`、`model_action_id`、`operation_id`、`outcome`、`status`、`started_at`、`duration_ms`、`clock`、`terminate`、`interrupted` | 批次分区是**当前状态**（下一轮会清空），而"这次调用花了多久"是历史，明天还要能读 | 与该次观察的提交同一事务 |
| 分区内的 span 副本 | `BatchCallState.started_at` / `duration_ms` | 让当前分区自解释（在线读快照即可），与事件互为同一事实的两个视图 | 同一次提交 |
| 待提交的测量 | `ExecutionState.last_request_measurement` | 驱动把测量交回提交端；提交端用它产生事件。单槽、提交后即失效 | 随下一次提交写入 |

`ExecutionState` schema 升到 **4**（1–3 仍可读）。新增字段：`request_instance`、`request_started_at`、`last_request_measurement`、分区内的两个计时字段。

## 3. 明确不记录（并说明理由）

| 不记录 | 理由 |
| --- | --- |
| reducer 内的时钟读取 | reducer 是纯函数：时间只能以数据形式随事件进入（`ModelCompleted` / `ToolCallObserved` 携带驱动在 I/O 边界测得的数字） |
| 跨进程的单调时钟相减 | 两个进程的 monotonic 值相减没有意义；跨进程等待只能用持久墙钟时间点，并说明墙钟误差 |
| 批次"总耗时"作为调用耗时之和 | 并行批次的墙钟跨度与各调用耗时是不同的量；报告分别呈现，不相加 |
| 原始 prompt / 工具参数 / 工具输出 / provider continuation / headers | 报告按字段白名单输出；原文只给受控引用，不设无鉴权导出开关 |
| 第二份操作账本 | 操作状态以引用形式指向账本所有者；账本无读取能力时报告 `unsupported`，不推断 |
| 估算 token 伪装成 Provider 实报 | 估算保留单位、估算器身份与方法；`payload_bytes` 命名估算器（`harness:json-utf8-v1`），Provider 未报告 usage 时记为 unknown（不写 0） |
| 费用（无价格表时） | 只有宿主显式提供币种与版本化价格表、且有匹配的 actual usage 时才计算；否则 unknown，不在线查询、不硬编码 |
| 导出队列 | 没有导出队列：事件先落库，fanout 是同步的、失败计数（`fanout_failures`）并可用 `replay_run_events` 重放。没有无界后台任务，也就没有需要丢的东西；这是设计选择，不是省略 |
| 进程内临时观测（lease/CAS 拒绝、fence 拒绝、fanout 失败） | 只在当前进程的内存里。读取端必须把它们标为 not_durable / OBSERVED，不能当成已提交事实；纯存储读取器看不到它们，报告要说明这一点 |

## 4. 读取端必须遵守的三条边界

1. **旧事件没有 attempt**：标 `unknown`/`legacy`，不准用当前 run 的 attempt 回填（`TraceEntry.attempt_source`）。
2. **未知事件或缺失字段**：给出覆盖限制（integrity/unsupported），不猜补。
3. **全局 cursor 分页**：页内没有目标 run 的事件也要按已扫描位置续读；`truncated`、`scan_limit_hit`、`exhausted` 与 `next_cursor` 是四个互相独立的显式结果。续读位置只能落在**实际返回**的最后一条匹配事件上——收集上限在扫描途中到达时就停在那里，把同一页剩下的匹配事件留给下一次读取；`high_water` 单独记录扫描过的最远位置。否则调用者照报告给的 cursor 续读，会跳过报告没带走的事件（`docs/runtime-diagnostics-evaluation-acceptance-review.zh-CN.md` §2 的 RDE-D1，修复见 F1）。"恰好满页"不等于"后面为空"：只有真的读到存储末尾（空页或短页）才 `exhausted=True`。
