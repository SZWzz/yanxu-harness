# B1 / B3：typed dispatch reservation、多范围覆盖与策略历史合同

日期：2026-10-29。

本合同定义一次真实 dispatch 如何在 Task root、Plan 与子任务 grant 预算中共同准入。它保留旧 `TaskBudget` / `BudgetLedger` / SQLite `budgets` 表；B1 不把旧 float 账本改造成新硬预算，也不接入 Provider 或 Gateway 的实际派发时点（该接线属于 B2）。

## 1. 单一支出事实与适用 scope

`TypedBudgetDispatch` 记录一个 dispatch 的 Task、Run、dispatch identity、真实 request instance、unit 和整数预留量。cost dispatch 另外要求币种和 price version。对象不接受调用方传入 scope 列表。

控制存储在同一 reservation 事务中从已提交事实推导全部覆盖范围：

| owner kind | 稳定 owner id | 来源 |
| --- | --- | --- |
| `task_root` | root `task_id` | Task 的 `root_task_id` 与完整父链必须一致 |
| `plan` | `plan_id` | Task 或其祖先的已提交 Plan Step binding；同一父链不能跨多个 Plan |
| `task_grant` | `spawn_identity` | 每一条已提交父子关系；嵌套子任务覆盖沿父链适用的 grant |

namespace 属于所有 owner key；session 作为策略访问元数据，在 dispatch 时复核。若子 Task 缺少父子关系、关系与 Task 父字段不一致、父链循环或跨 namespace/session，typed reservation 拒绝。缺失的 scope policy 仍保留一条无硬限额的 coverage 事实，不会因此从适用 scope 集合中删除。

`task_grant` 是 B1 为 Plan 明确的子授权范围增加的 owner kind。其 identity 使用已持久化 `spawn_identity`，不会创建第二个 dispatch identity 或 operation ledger。

## 2. 幂等身份与事务不变量

同一 namespace 的 `dispatch_identity` 唯一对应一个 reservation。完整内容包括 Task、Run、request instance、unit、amount，以及 cost 币种/价格版本。相同内容重放返回原记录；任何内容不一致，或从当前已提交关系推导出的 scope 集合改变，都拒绝重放。恢复后产生了新的真实 Provider 请求时，必须使用新的 request instance 与 dispatch identity。

一个 `typed_budget_dispatches` 行拥有唯一的 usage/state 事实；多条 `typed_budget_coverage` 行只指出此事实适用哪些预算 owner，不能相加成多份费用。对每个 owner/unit 单独计算：

```text
used = settled_actual + active_reserved + unresolved_reserved
available = max(0, hard_limit - used)
```

一次 reservation 在一个 Memory 临界区或一个 SQLite `BEGIN IMMEDIATE` 事务中完成所有 scope 读取、hard limit 检查、canonical reservation 和 coverage 插入。任何 scope 不足或任何写入失败，整笔回滚。调用方无法传入不完整 scope 集合。

对于 `cost_micros`，所有已配置的适用 scope 和 dispatch 必须使用相同币种与 price version。observe policy 会保留额度/元数据，但不阻止 reservation。token/cost 的硬上界仍须满足 B0 的 host capability 引用合同。

## 3. 状态与实际用量

状态复用 `reserved`、`settled`、`released`、`unknown`：

- `reserved` 和 `unknown` 都占用完整预留量；没有结果不等于零成本。
- `settled` 在唯一 canonical reservation 上保存一次整数 `settled_actual`。实际值可超过预留；超额完整保留，并使后续 hard admission 无可用额度。
- `released` 只允许上层在证明 dispatch 未开始后调用；typed store 将释放作为最终状态，未知或已结算记录不能退款。
- 相同结算可幂等重放；不同结算拒绝。迟到 usage 的专用身份/合并流程属于 B2。

`read_typed_budget_usage(owner, unit)` 返回该 scope 的 actual/reserved/unknown 分项。各 scope 视图会引用相同 dispatch actual；跨 scope 汇总必须按 canonical reservation 去重。

## 4. 存储与兼容

Memory 与 SQLite reference store 实现可选 `TypedBudgetStore` capability。SQLite 的 `typed_budget_schema_version` 独立于 Task control 与 Plan schema；B1 创建 v1 policy、dispatch 和 coverage 表，B3 的 v2 增加策略历史和变更审计表。显式 `enable_typed_budgets=True` 的可写打开会将完整 v1 schema 原子迁移到 v2，并从现行策略回填历史版本；reservation 和 usage 不变。只读打开不迁移：启用该 capability 必须已有当前完整 schema，否则拒绝。

typed budget policy 的已存快照不可变，同摘要重放幂等；新策略必须通过 `update_budget_policy` 携带旧 digest CAS、唯一 idempotency key、可信主体和授权引用，策略历史与审计记录和当前指针在同一临界区/事务更新。降低限额不会删除实际消费或既有覆盖。旧 Task `budgets` 表、历史 float reservation 和 Task API 保持原状；B4 的迁移是单独、显式的 typed-budget 导入操作，不会自动改写旧记录。

## 5. B4：预算诊断与旧账本导入

`migrate_legacy_budget_reservations(root_task_id)` 仅在 typed-budget capability 已启用且 control store 可写时执行。调用必须指定真实 Root Task。每笔来源 reservation ID 以 SHA-256 进入稳定的 synthetic legacy dispatch identity；合成 Run identity 表示来源账本没有保留真实 Run，不能据此推断它受过 Plan 共享预算。迁移 coverage 只有 `task_root` owner，旧 reservation 行保持不变，重复迁移幂等且不会复位已迁移 typed fact 后续的 unknown/settled 状态。

只迁移可精确表示为非负整数且单位明确的事实。方向不明的 `tokens`、`seconds`、缺 currency/price version 的 legacy cost、estimated、小数、非有限、负数和整数范围溢出记录返回跳过原因，来源行仍可从旧 API 读取。SQLite 导入处于一个事务；失败整批回滚。Memory reference store 同样在锁内导入，并在失败时恢复原 typed reservation 状态。只读 store 拒绝迁移；只读旧库或未启用 typed schema 的诊断标记 typed 视图不可用，不自动升级 schema。

Task 与 Plan 诊断报告使用同一 `TypedBudgetView` 结构：owner/unit scope 提供 limit、actual、reserved、unknown、used、over-limit、available、upper-bound overrun 和 enforcement basis；dispatch 事实按 canonical reservation ID 去重，scope 行只引用 ID。跨 scope 的 totals 不得相加。Task 原有 legacy budget 行保留在独立兼容字段中，不计入 typed scope totals；已导入 typed reservation 提供 `legacy_source_reservation_id_digest` 供审计关联。Task-grant owner ID 在诊断输出中摘要化。

token/cost 的 hard scope 只有提供 host `hard_bound_ref` 时才报告上界 enforcement 能力；无该引用的观察限额使用 `observe_only`，不会被渲染成硬约束。actual 高于某 dispatch 上界时完整记录，并报告 `upper_bound_overrun`。

## 6. 不作的保证

此阶段提供可事务验证的 typed reservation 能力，不表示当前 Provider、摘要模型、工具或 Gateway 已在每次真实外部请求前调用它。该派发接线、unknown/迟到 usage/跨进程恢复属于 B2。`observe` token/cost 不是 hard limit；Memory store 不提供跨进程容量协调。没有硬上界证据时不宣称相应用量受到约束。
