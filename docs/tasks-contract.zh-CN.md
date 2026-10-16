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

## 8. 本阶段不包含

不做 Web 平台、分布式消息中间件、通用 DAG 语言、任意 Agent 网络、远程 subagent 协议、插件市场、生产
沙箱；不增加第二套 agent loop；不把会话对象/Task/队列/锁写入 `ExecutionState`。Q1 起才落地存储与调度，
Q0 只冻结上面这些名字、状态、表与边界。
