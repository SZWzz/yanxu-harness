# B0 / B3：预算 owner、策略快照与额度变更合同

日期：2026-10-29。

本合同冻结 Plan 共享预算及原子 reservation 接线使用的身份、计量单位、策略快照和额度变更规则。B0 提供不可变值类型与旧 `TaskBudget` 的显式转换；B3 增加带版本、CAS 和审计的策略更新。

## 1. 所有者身份

`BudgetOwnerRef` 由 `kind`、`namespace` 和 `owner_id` 组成。B0 冻结 Task root 与 Plan 账户；B1 为预算授权边界增加 `task_grant`，其 `owner_id` 是已提交子关系的稳定 `spawn_identity`：

| kind | owner_id | 账户生命周期 |
| --- | --- | --- |
| `task_root` | 已持久化 root `task_id` | 一个 root Task 及其全部子 Task、attempt 和真实派发共享 |
| `plan` | 稳定 `plan_id` | 同一 namespace 下跨 Plan revision、retry、attempt 和进程重启共享 |
| `task_grant` | 已提交 `spawn_identity` | 一个父子边界获批的子额度；父链中的嵌套 grant 分别覆盖其后代 |

`session_id` 放在 `BudgetPolicySnapshot`，用于检查访问与所属关系；它不是账户 key。Plan revision 也不进入账户 key。改变 owner kind、namespace 或 owner id 就是另一个账户，调用方不得借此复制或补充原额度。dispatch 的适用 owner 由控制存储从 Task/Plan/父子关系推导，调用方不能删减 scope。

## 2. 不可变策略快照

`BudgetPolicySnapshot` 固定 owner、session、策略版本、limits、root task 数量/深度限制及 schema version。limits 按 unit 稳定排序且同一 snapshot 不得重复 unit。其 digest 为 `sha256:` 前缀的 SHA-256，计算输入为 UTF-8、无空白、key 排序、非 ASCII 原样编码的规范 JSON，域分离前缀为 `yanxu-budget-policy:v1\0`。

快照中的策略版本由可信 host 提供。上层 Plan 批准应引用不可变版本；策略变更不得原位改写已批准快照。digest 用来比较/复核持久事实，不是授权凭据，也不证明 Provider 或后端实际执行了上界。

模型请求先读取适用策略，再准备 reservation 批次；提交批次时必须在同一事务/锁内重读所有适用 owner 的当前 digest，并与快照的完整 owner→digest 集合相等。期间出现策略新增、删除或更新时，本次 dispatch 在 Provider 调用前拒绝，不能只验证当前正在预留的 `model_requests` 单位而漏掉未生成 reservation 的 hard token/cost 限制。

## 3. 精确单位与执行能力

新的 `BudgetLimit.limit` 必须是非负 Python `int`，拒绝 bool、float、NaN、Infinity、负数及有小数的值。首轮明确单位：

| unit | 量纲 | 说明 |
| --- | --- | --- |
| `turns`、`model_requests`、`dispatches`、`concurrency`、`depth` | 整数个数 | 精确计数，策略为 hard limit |
| `input_tokens`、`output_tokens` | tokenizer 输出的整数 token | 无 host 提供的可执行上界时只能 observe；hard limit 必须带 `hard_bound_ref` |
| `duration_milliseconds` | 整数毫秒 | 旧秒数只可显式、精确转换为毫秒；deadline 仍使用既有绝对时刻合同 |
| `cost_micros` | 所选币种的百万分之一单位 | 必须记录三位大写币种代码与 price version；缺可信 host 上界时只能 observe，hard limit 必须带 `hard_bound_ref` |

每个 unit 在一个 owner 的策略内只有一个额度。这样同一个 owner 不会同时混用不同币种的 cost limit；跨 owner scope 的币种和价格版本一致性由 B1 原子覆盖合同检查。

`observe` 仅描述可收集/比较的事实，不关闭 dispatch、不预留硬额度。模型请求数、工具 dispatch 数、turn、concurrency、depth 与 duration 的新策略必须使用 `hard`。Token/cost `hard` 的 `hard_bound_ref` 是指向 host 可执行约束证据的稳定引用，不是自我声明；不能验证该能力的 host 必须拒绝硬限额请求或将其降为 observe。

旧 `tokens` 缺少 input/output 方向，旧 `seconds` 也不携带整数尺度。二者仍可由现有 `TaskBudget`/`BudgetLedger` 使用，但不能直接进入新快照：`tokens` 转换会拒绝；`seconds` 只有能无损映射为整数毫秒时才转换。cost 转换需要显式提供币种与 price version。任何拒绝都不能静默舍弃额度。

## 4. Legacy TaskBudget 转换

`snapshot_from_task_budget(...)` 是显式迁移边界，不替换旧 Task API。它把完整的 TaskBudget root allowance 绑定到显式 `task_root` owner，并且：

- 先验证数值有限且非负；整数计量必须能无损转为整数。
- 将旧 `seconds` 乘以 1000，只有结果是整数时映射为 `duration_milliseconds`。
- 拒绝方向不明的旧 `tokens`，不把它猜成 input 或 output token。
- 对 token/cost，只有传入 `hard_bound_refs` 中相应单位的 host 上界引用才标 hard，否则标 observe。cost 同时需要 `cost_currency` 和 `price_version`。
- 保留 `max_tasks_per_root` 与 `max_depth`，不把 root Task 语义伪装成 Plan owner。

兼容性保证：已有 `TaskBudget`、`BudgetLedger`、`BudgetReservation` 和 Task SQLite ledger 暂保持现有调用形状及 schema；B1 使用独立 typed reservation schema，不自动转换旧账本。实际 dispatch 接线属于 B2；旧 reservation 诊断与迁移属于 B4。

## 5. 显式策略版本更新（B3）

`put_budget_policy(...)` 只用于安装 owner 的初始快照；同一摘要重放幂等，其他摘要必须走 `update_budget_policy(...)`。更新要求调用方提供当前 `expected_policy_digest`、新快照、唯一 `idempotency_key`、`actor`、`authorization_ref` 与变更原因。新 digest 和 policy version 在该 owner 下唯一，owner 与 session 不得改变。

Memory 在同一锁内完成 CAS；SQLite 在一个 `BEGIN IMMEDIATE` 事务中追加历史版本和审计记录，再移动当前策略指针。并发更新只有一个能基于同一旧 digest 成功。相同幂等键及相同变更内容重放返回原审计记录；键被不同内容重用或旧 digest 已过期则拒绝。历史快照可按 digest 读取，审计变更可按 owner 读取。

`actor` 和 `authorization_ref` 是宿主提供的审计数据，不会由 reference store 自行认证或验证授权。宿主必须先验证可信主体和额度变更权限，再调用更新接口；策略 digest 与审计记录本身不构成授权。

对已批准 Plan，改变其适用预算范围仍须建立新 Plan revision 并重新审批，不得改写批准时引用的快照。经授权的限额降低会关闭超限后的新准入，但既有 reservation、unknown 覆盖与 settled actual 保留，仍允许结算或取消。已发生 cost usage 后，策略不得改用另一币种。

SQLite typed budget schema v2 增加策略历史与变更审计表。显式开启 typed budget capability 的可写打开会在单一事务内将完整 v1 schema 升到 v2，并从现行策略回填历史；回填不修改 reservation 或 actual。只读打开不执行迁移，v1 库启用该 capability 时明确拒绝。Memory store 提供相同的进程内更新合同，但不提供持久历史。

## 6. 明确不作的保证

此合同不提供 tokenizer、价格表、沙箱、跨 Worker 并发锁或 token/cost 硬上界。deadline 与 Plan 自带的持久 `max_concurrency` 继续由其既有控制路径拥有；Worker 本地 slot 不能冒充跨进程并发账户。没有可验证计量能力时，系统记录已知 usage 或拒绝 hard policy，不用估算值伪装实际消费。
