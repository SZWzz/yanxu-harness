# 任务、输入、执行权与取消契约（Q0）

日期：2026-10-16。状态：**已冻结**（Q0）。实现见 `src/yanxu_harness/tasks/types.py`（词汇与纯决策）与
`src/yanxu_harness/tasks/ports.py`（控制存储能力端口）。测试：`tests/test_tasks_contract.py`。

本文件是后续 Q1–Q6 的合同：谁拥有什么、哪些状态变化合法、输入按什么顺序被取用、事务边界在哪里、
哪些竞争结果必须先定序。它只描述**契约**，不描述尚未实现的存储或调度器；持久化在 Q1 落地前，本文
中出现的接口都是要求，不是已交付能力。

## 1. 对象与所有者

| 对象 | 所有者 | 定义 | 明确不是 |
| --- | --- | --- | --- |
| namespace | 宿主 | 租户/项目隔离边界 | 会话的别名；run_id 不构成跨租户权限 |
| session | 宿主 | 会话分组 | 共享任务状态、预算或权限的容器 |
| task | 宿主创建 | 可排队/等待/取消/验收的工作单元，绑定一个固定逻辑 Run | Core 执行事实本身 |
| run | Core | 权威执行事实；worker 变化不换 ID | 挂在 task 上的一份状态副本 |
| attempt | worker 领取 | 一次取得执行权的尝试；重领换 ID，逻辑任务与预算不重置 | 新的 task 或新的 Run |
| input | 宿主/内部事实 | 已接纳的有序输入：稳定 input_id、内容摘要、目标、消费事实 | 模型可见的原文本身 |
| child relation | 父任务的控制事务 | 父、稳定 spawn 身份、子、授权配置与预算预留 | 工具副作用的证明 |

**一个 Task 一个 Run。** 完成后新的用户请求是显式创建的新 Task/Run（可关联原 session）；不隐式重开
已终结的 Run。审批与崩溃恢复仍属于原 Run。

**task 不缓存 run 状态。** 见 `TaskRecord` 的字段——没有 `run_status`。需要知道运行状态时读运行。
"Scheduler 以为运行结束了" 这类不一致，只有把两份状态放在两个地方才会出现。

## 2. 任务状态表

状态：`queued`、`running`、`waiting`、`cancelling`、`completed`、`failed`、`cancelled`、`usage_limited`。
终态：`completed`、`failed`、`cancelled`、`usage_limited`（**不可复活**）。

`queued` / `waiting` 的存在是为了让"调度器还没取到"和"在等一个运行之外的事实"不必伪装成 run 状态：
排队不伪造运行，调度等待不伪造模型成功。

| 从 | 到 | 允许的原因码 |
| --- | --- | --- |
| queued | running | `execution_right_claimed` |
| queued | cancelling | `cancel_requested` |
| queued | failed | `work_failed` |
| queued | usage_limited | `budget_exhausted` |
| running | queued | `execution_right_lost` |
| running | waiting | `awaiting_external_fact` |
| running | cancelling | `cancel_requested` |
| running | completed | `work_completed` |
| running | failed | `work_failed` |
| running | cancelled | `cancellation_settled` |
| running | usage_limited | `budget_exhausted` |
| waiting | queued | `external_fact_persisted` |
| waiting | cancelling | `cancel_requested` |
| waiting | failed | `work_failed` |
| cancelling | cancelled | `cancellation_settled` |
| cancelling | completed | `work_completed` |
| cancelling | failed | `work_failed` |
| cancelling | usage_limited | `budget_exhausted` |
| cancelling | waiting | `awaiting_external_fact` |

表由 `transition_table()` 导出、由测试逐边钉住（合法边允许、非法边与错误原因都拒绝）。要点：

- **取消是请求，不是判决。** 任何非终态收到取消请求 → `cancelling`，并把 `cancel_requested_at`
  写入记录；随后按事务事实落定：运行先结束就是 `completed`/`failed`/`usage_limited`，取消先落定就是
  `cancelled`。取消不删除已经发生的副作用，也不把未知项写成零。
- **失去执行权回到 `queued`**，不是 `failed`：同一个逻辑任务与预算由新的 attempt 继续。租约过期不代表
  远端工具已停止。
- **终态没有出边**：`assert_transition` 对终态直接拒绝；晚期输入、旧 callback、迟到子结果都不复活任务。

## 3. 输入类型与接纳

| 类型 | 来源 | 轮询位置 | 取用顺序 |
| --- | --- | --- | --- |
| `initial` | 宿主 | 运行开始前 | 一次一条 |
| `steering` | 宿主 / 模型投影 | 下一次模型请求之前 | 全部（按 sequence） |
| `follow_up` | 宿主 / 模型投影 | 轮询边界 | 一次一条 |
| `cancel_request` | 宿主 | 派发前检查 | 控制项，不作为对话取用 |
| `approval_decision` | 宿主 | 工具核对/审批处 | 控制项 |
| `child_completed` | 内部事实 | 父任务等待处 | 控制项 |

顺序表由 `INPUT_ORDER` 冻结，取用由 `select_inputs`（纯函数）决定：按 sequence 升序，`all` 类型整批取、
`one-at-a-time` 类型每次一条、控制类型永不作为对话取用。`steering` 不强行打断已经派发的工具或模型请求。

接纳结果只有三种：`accepted`（可靠保存，**不等于**模型已读取或任务成功）、`duplicate`（同键同内容，
返回原接纳结果，不再扣积压额度）、`refused`（带拒绝码）：

| 拒绝码 | 何时 |
| --- | --- |
| `task_terminal` | Task 已终态：不再接纳新输入，宿主应创建新 Task |
| `cancelled_first` | 取消请求先落定：非控制输入不再进入对话（已 accepted 的保留为事实） |
| `content_conflict` | 同键不同内容：调用者必须解决，不能静默覆盖 |
| `backlog_full` | 任务/命名空间/总量积压达到上限（**只拒绝新的，不挤掉已 accepted 的**） |
| `payload_too_large` | 单条载荷超过上限 |
| `model_cannot_control` | 模型来源的输入试图成为控制项（取消、审批、子完成） |
| `identity_mismatch` / `unknown_target` / `not_permitted` | 身份、目标或授权不符 |

幂等键至少覆盖 `namespace/task/input_id`；内容摘要覆盖类型、目标、内容与语义参数（不含编码、时间戳、
序号）。相同键相同内容 = 重投；相同键不同内容 = 冲突。

输入状态：`pending` → `claimed` → `applied`；另有 `cancelled`、`refused`。`claimed` 只是临时消费权
（领取者 + 到期时间），进程在领取后、提交前死亡则输入可被再领取；`applied` **必须**指出产生接纳效果的
Run revision（可选事件序号）——"从队列取出"不是应用证明。`applied` 只表示输入已进入持久执行状态，
不承诺模型看到或采用了该文本。

## 4. 事务边界与竞争结果

必须由**同一个事务**完成的操作（能力端口要求，`CoordinationStore`）：

1. `admit_task`：创建 Task 与其 `initial` 输入并固定 Run ID；重复提交返回同一记录；
2. `admit_input`：接纳输入、分配序号、更新 inbox revision（事务内重读去重记录、任务状态、积压计数）；
3. `claim_task`：领取/续租/释放调度执行权，带持久 fencing token；
4. `commit_run_transition_with_inputs`：Run 快照 + 输入 `applied` + 事件 + 控制 outbox 一起提交；
5. `reserve_budget` / `settle_budget` / `register_child`：按租约与额度准入并幂等结算；登记子关系、预算
   预留与调度通知同事务。

应用边界（Q2 实现，Q0 冻结）：

```text
读取候选输入（不确认）
  → reducer 接受带 input_id 的事件
  → 控制存储原子提交：Run 快照 / transcript 增量 + input applied + 事件
  → 才允许准备并派发后续外部工作
```

**空读与终态提交的竞争**必须处理：终态事务检查已观察的 inbox revision 与可消费输入——新输入先接纳则
重走既有轮询边界；终态先提交则新输入被 `task_terminal` 拒绝。不能 accepted 后静默丢掉，也不能让终态
被旧 callback 复活。

竞争结果的既定顺序：**取消请求 vs 接纳**（先落定者胜；另一个以事实保留）、**取消请求 vs 完成**
（按事务事实落定，副作用与未知项保留）、**spawn 登记 vs 父取消**（父取消先提交则拒绝 spawn；spawn 先
提交则子任务纳入取消传播）、**领取 vs 领取**（唯一约束决定唯一持有者，旧 fence 无法提交）。

## 5. 预算单位与预留

单位（`BudgetUnit`）：`turns`、`model_requests`、`dispatches`、`concurrency`、`depth`、`seconds`、
`tokens`、`cost_micros`。首版保证可硬约束的是轮次/派发次数/并发/任务数/深度/时间；token 与费用在宿主
给出真实 usage 与价格表前记为 `unknown`，不写成 0，也不凭未知计费退还额度。

预留状态：`reserved` → `settled` / `released` / `unknown`。规则：

- 预留与额度检查必须在外部 await **之前**；提交失败不派发；
- 去重键是稳定派发/预留身份（`dispatch_identity`），**不是**逻辑 model action id：恢复后重新发出的请求
  可能再次计费，两次请求是两次派发；
- `released` 只给"可证明从未开始"的工作；已 `settled` 的预留不能再退（否则可用额度会大于 root 授予的）；
- 派发窗口内失去最终 usage 的预留保持 `unknown`（附原因），取消请求不立刻释放未确认停止的开销。

## 6. 能力端口与拒绝

`coordination_capability(store)` 返回 `full` / `partial` / `none`；`require_coordination(store)` 在
**任何接纳之前**拒绝 `partial`/`none`，并列出缺失的方法。理由：显式启用持久任务却缺少原子能力时，
唯一诚实的做法是拒绝，而不是退化成两次独立写入（那正是"输入丢了但 Run 已提交"的来源）。

单 Run 的旧后端无需实现这些扩展：不启用持久任务的部署完全不受影响，`tests/test_tasks_contract.py`
用"没有这些方法的对象"钉住这一点。

## 7. Q1 已交付：持久 inbox 与控制存储

实现：`src/yanxu_harness/tasks/inbox.py`；测试：`tests/test_tasks_inbox.py`（23 passed）。

- **同一个库、同一个事务。** `SqliteControlStore` 继承 `SqlitePersistence`，控制表与 Run 表在
  同一文件、同一连接：`commit_run_transition_with_inputs` 一次事务写入 Run 快照、事件、输入
  `applied` 标记与控制通知。跨库的"两阶段假装"没有发生，也不需要。
- **表**：`tasks`、`inputs`、`inbox_counters`（`next_sequence`/`revision`/`pending_count`/
  `pending_bytes`）、`task_claims`、`claim_counters`、`children`、`outbox`、`budgets`、
  `control_meta`。顺序号、inbox revision、积压条数与字节数都在事务内更新。
- **I-01/I-02 实测**：两个连接并发接纳同一 input_id → 恰好一个 accepted、一个 duplicate；两个连接
  并发 `claim_task` → 恰好一个 claim（fence 1），另一个为空；两个连接抢最后一个容量位 → 一个
  accepted、一个 `backlog_full`，已 accepted 的不被挤掉；真实子进程写入同一库，父进程可见。
- **领取与失去**：活动 claim 期间他人不能领取；过期后可重领，fence 递增、attempt 换新而逻辑任务与
  预算不变；旧 fence 提交被 `ClaimLost` 拒绝，且**没有留下任何 Run 状态**。释放执行权把任务退回
  `queued`（`execution_right_lost`），不写成 failed。
- **只读与版本**：`read_only=True` 能读任务/输入，写路径全部拒绝，不创建文件、不建表、不迁移；没有
  控制表的库给出 "no control schema"；`control_schema_version` 未知更高版本被 `VersionRefused` 拒绝。
- **内存参考实现**：`MemoryControlStore` 与 SQLite 实现走同一批纯决策函数，`durability` 返回
  `VOLATILE`，两个实例不共享任何东西——这是声明的语义，不是缺陷。

尚未交付（属 Q2–Q5）：Runner 的持久输入接线、本地 worker/调度、父子执行与预算/期限整合。因此
`docs/contract.md` 里"没有任务调度与 subagent 执行"的声明**继续有效**。

## 8. Q2 已交付：同一 Core 的原子消费接线

实现：`src/yanxu_harness/tasks/inputs.py`（`PersistentInputReader`）、`core/runner.py` 的持久读取分支与
`resume_poll` 入口、`runtime/default_harness.py` 的确认提交与终结竞争守卫；测试：
`tests/test_tasks_input_flow.py`（12 passed）。

- **读取不确认，提交才确认。** 轮询返回 `InputPoll`（消息 + 每个输入的 `input_id`/`sequence` + 观察到的
  inbox revision）；执行快照记录**待确认集合**、**已应用水位**与**观察 revision**（执行状态 schema 5，
  1–4 仍可读）。确认发生在写入该状态的同一个事务里：`commit_run_transition_with_inputs` 一次提交 Run
  快照、事件与 `applied` 标记，并被写入的快照里 `input_refs` 已清空。
- **初始输入同一边界**：创建 Run 的第一个事务确认 `initial` 输入——"运行存在"与"开场指令已应用"不会
  被一次崩溃拆开。
- **四个窗口实测**：提交前崩溃（真实子进程 `os._exit(9)`）→ 输入仍 `pending`、无 Run 记录，重试恰好
  应用一次；提交后重试不重复追加（store 拒绝第二次应用）；同一 `input_id` 重投 → `duplicate`，会话
  只出现一次、工具只跑一次（副作用文件只有一行）。
- **终结竞争**：终态事务校验"运行观察到的 revision"（`InboxMoved`）。在最后窗口落地的输入会让提交被拒，
  运行回到**已提交的 `COLLECTING` 快照**继续（`resume_poll`）——这是唯一能证明"没有未结算外部动作"的
  边界；复用的新 driver 使用独立事件 id 空间（`resumedN-`），因此不会再撞上已消费的 event id。为此
  sink 会写入"观察到持久 inbox"的 `COLLECTING` 状态（无 inbox 的队列读取仍不写）。
- **控制项不进入对话**：`cancel_request`/`approval_decision`/`child_completed` 永不被当作对话读取；读取
  取消请求本身不取消任何东西（取消由拥有该边界的组件处理）。
- **双来源拒绝**：同时配置持久读取器与内存回调 → `ConfigurationError`（一条通道不能有两个来源）。

## 9. Q3 已交付：有界本地多任务调度

实现：`src/yanxu_harness/tasks/scheduler.py`（`LocalWorker`/`SchedulerLimits`/`ScheduleDecision`/
`WorkResult`）+ 控制存储的 `save_task`（按状态 CAS）与 `read_schedulable`；测试：
`tests/test_tasks_scheduler.py`（12 passed）。宿主显式调用 `submit`/`run_once`/`run_until_idle`/
`stop`/`query`/`cancel`/`wake`，没有守护进程、后台线程或隐式循环。

- **只调度，不发请求。** 模型请求、工具批次、提交边界与恢复仍属于同一个 Runner 与 harness；worker
  只挑选、领取、分类与结算。
- **一个逻辑执行者。** 调度 claim 带 fencing 与到期；两个 worker 竞争同一任务 → 恰好一个驱动，另一个
  得到 `held_elsewhere`（不是错误、不是重试）。最终提交仍以 Run 自身租约/fencing 为准，调度 claim 不能
  绕过它。
- **领取后重读分类**：终态 → 释放；等待中 → 释放（槽位让出）；已有 Run → `harness.resume` 恢复或按
  终态结算，**绝不新起一个 Run 冒充重试**；无 Run → `harness.start`（prompt 来自 `initial` 输入）。
- **公平且可解释**：全局与 namespace 并发上限；排序 = 优先级 → 准入顺序，另有 **aging**（超过
  `priority_aging_seconds` 的旧任务先试，避免持续高优先级输入饿死普通任务）；namespace 轮转；
  `ScheduleDecision` 记录原因与被跳过的候选。
- **失效 claim 不是判决**：claim 过期后任务仍是可调度的（`read_schedulable` 把"无/过期 claim 的
  RUNNING"与 QUEUED 一并返回），重领换 attempt、逻辑任务与预算不变；claim 过期不写成 failed。
- **等待让出槽位**：暂停 → `WAITING` + 释放 claim；宿主持久化外部事实后调用 `wake`（WAITING→QUEUED，
  `external_fact_persisted`）重新可调度。取消请求优先于 wake。
- **停止不是取消**：`stop()` 停止领取、给已开始的驱动有界宽限，不把任务标记为 cancelled，也不释放可能仍
  在派发的驱动所持有的 claim；被强杀后由另一 worker 以原身份与证据接管。
- **取消是请求**：`queued|waiting → cancelling → cancelled` 两步；queued 取消不启动 Run（测试断言
  `load_run` 为空）；RUNNING 取消交给持有该 Run 的驱动，本进程够不着的 Run 只记录请求，不冒充已取消。

## 10. Q4 已交付：任务期限与父子共享预算

实现：`src/yanxu_harness/tasks/budgets.py`（`TaskBudget`/`BudgetLedger`/`DispatchBudget`/
`BudgetSnapshot`）、`TaskRecord.deadline_at` + `remaining_seconds()`、`TaskTreeLimits`、runner 的
`reserve_dispatch`/`settle_dispatch` 钩子与 harness 的 `budget=` 接线；测试：
`tests/test_tasks_budgets.py`（15 passed）。

- **预留先于外部 await**：每个模型请求在调用 provider **之前**预留；测试断言 provider 被调用时账本里
  已经有这一行。预留失败会抛 `BudgetRefused`，请求根本不发出（`stream.calls == 0`）。
- **身份是派发而非逻辑动作**：`DispatchBudget.identity()` 用真实 `request_instance`，所以恢复后重发同一
  逻辑动作会**再预留一次**（可能再次计费），不会被去重掉。
- **unknown 不是零**：provider 失败/取消/结果不明 → 预留转 `unknown` 并保留占用；只有"可证明未开始"
  才 `release`，`settled`/`unknown` 一律拒绝释放（取消请求本身也不释放）。结算幂等，重复结算不改变数字。
- **晚到 usage 幂等**：`record_late_usage` 以 `late:{reservation}:{sequence}` 为身份补记，重投只记一次；
  估算不冒充实报，未约束的单位不编数字。
- **父子共享额度**：`grant_to_child` 受 root 剩余额度约束（超出即 `BudgetRefused`），分配**不增加** root
  总额度；重复 spawn（同稳定身份）返回同一预留，重复结算幂等。
- **期限**：`deadline_at` 作为**绝对时刻**持久化，重启后在本进程算剩余时长并交给 harness 作为 invocation
  预算（`min(任务剩余, 配置上限)`，`RunRequest.deadline_seconds` → `_Drive.max_runtime`）；已过期限的任务
  **不构建 harness、不发任何请求**，直接 `usage_limited`（`deadline_exceeded`）。
- **树有界**：`TaskTreeLimits`（首版 `max_depth=1`、`max_tasks_per_root=8`）在 `admit_task`/`register_child`
  的事务里拒绝越界（超深、超量），不是事后统计。

## 11. 本阶段不包含

不做 Web 平台、分布式消息中间件、通用 DAG 语言、任意 Agent 网络、远程 subagent 协议、插件市场、生产
沙箱；不增加第二套 agent loop；不把会话对象/Task/队列/锁写入 `ExecutionState`。Q1 起才落地存储与调度，
Q0 只冻结上面这些名字、状态、表与边界。
