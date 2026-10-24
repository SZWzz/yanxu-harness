# Plan 与任务 DAG 契约（P0.0）

状态：P0.0 契约冻结；运行时能力按 `docs/plans-progress.zh-CN.md` 逐阶段实现与验证。已完成阶段的 API 可在当前开发检出中使用；本文不代表它们已包含于已发布的 `0.4.0a1` 包。

## 1. 所有权和边界

- Plan 控制存储拥有 Plan 定义、revision、control version、审批历史、控制事件、步骤绑定和验收记录。
- 现有 Task 控制存储拥有 Task 状态、输入、claim/fencing、取消请求、预算 reservation 和通知；Harness/Run 存储拥有 Run 状态与事件；ToolGateway 及其账本拥有工具操作与 `unknown` 事实。
- Plan 只引用 Task、Run、工具证据，不复制这些对象的可写状态。步骤 `accepted` 是独立的验收事实，Task `completed` 不会自动解锁依赖。
- Plan approval 只准入已批准 revision 的新 Step 执行，不替代工具审批，也不改变运行 profile 的权限。
- 只有宿主认证身份后才能调用控制 API。库记录宿主给出的可信 principal；字符串、模型输出和计划内容本身均不建立信任。
- 首版不生成模型候选、不启动后台线程、不提供通用 DSL、自动重试、跨 Plan 边或跨 revision 的自动结果复用。宿主可以显式调用下述 `retry_step`。

## 2. 定义与规范化

`PlanDefinition` 是不可变、带 schema version 的值，至少包含：

| 字段 | 合同 |
| --- | --- |
| `schema_version` | 当前首版为 `1`；未知版本拒绝。 |
| `namespace`, `session_id`, `plan_id`, `revision` | 非空稳定身份；revision 从 1 开始单调增加。修改内容发布新 revision。 |
| `title`, `goal` | 去除首尾空白后非空；分别不超过 200 与 4,000 UTF-8 字节。 |
| `constraints` | 最多 64 个有 ID 的声明；`declarative` 仅说明意图，`host_enforced` 必须引用宿主强制策略。文本本身不构成策略。 |
| `acceptance_criteria` | 最多 64 个命名 criterion，每项绑定注册 verifier 的 ID、版本和 JSON 配置；不支持未注册 verifier。 |
| `steps` | 1–128 个有唯一 ID 的 Step。总依赖边不超过 1,024。 |
| `execution_envelope` | profile、workspace、policy、deployment 与预算的稳定引用和版本；只标识宿主选择，不授予权限。 |
| `risks`, `expected_changes` | 可选、有界的审阅说明；路径/范围描述默认不是执行限制。 |
| `created_by`, `created_at`, `source` | 审计来源；操作者权限由宿主另行确认。时间由可信宿主注入。 |
| `proposal_run_ref` | 可选、带 namespace/session/run 身份的候选来源引用，不是 Step 执行绑定。 |
| `supersedes_revision`, `replan_reason` | 新 revision 可引用被替代版本和原因；不自动继承其批准或验收。 |
| `max_step_executions` | 跨所有 revision 与 retry 的累计新准入上限，取值 0–256，默认 256；0 合法且禁止准入。 |
| `deadline_at_ms` | 可选绝对 Unix 时间，整数毫秒；恢复不可刷新。 |
| `max_concurrency` | 宿主允许的 Plan 并发上限，取值 1–128，默认 1；不得超过 Worker 的全局与 namespace 上限。 |

`StepDefinition` 至少包含 `step_id/title/instruction/depends_on/acceptance_criteria/input_refs/required_context_refs/execution_profile_ref/workspace_ref/output_contract`。step ID 使用 `[a-z0-9][a-z0-9_.-]{0,119}`；标题非空且不超过 200 UTF-8 字节，指令非空且不超过 32 KiB。`depends_on` 是同一 revision 的 `all_accepted` 集合，不允许重复、自引用、缺节点或环。外部输入/上下文只能是宿主显式解析的固定引用，不能隐式读取任意路径。

所有配置只接受 JSON null、boolean、整数、字符串、数组和字符串键对象；拒绝浮点数、非有限值、重复 JSON 键、非字符串键、孤立 surrogate、非 JSON Python 对象及超限结构。单个规范化定义最多 256 KiB UTF-8、深度 32、每个 Step 最多 64 个验收 criterion。`created_at` 是不参与摘要的可信审计时间；计划 deadline 使用整数毫秒。顶层定义、nested mappings 和 arrays 在构造后不可变。

规范化摘要算法名为 `yanxu-plan-json-v1`：以 UTF-8 编码 `json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)`；对象键按 Unicode 码点排序；数组顺序保留，但 steps 按 `step_id` 排序，`depends_on` 按 ID 去重检查后排序，具集合语义的 criterion 按 criterion ID 排序。摘要为 `sha256("yanxu-plan-definition:v1\\0" + canonical_bytes)` 的小写十六进制值。服务端 `created_at` 不纳入摘要；其余定义字段纳入摘要。解析 JSON 时必须在丢失重复键信息前检测重复键。禁止 Python `hash()`、`repr()` 或无版本的序列化器作为持久摘要。

## 3. 控制版本、审批与身份

- `revision` 只随计划内容变化；`control_version` 随审批、控制状态、准入、验收、重规划和终态事件单调递增。变更操作携带 `expected_control_version` 和稳定 `request_id`。
- 同一 `request_id` 与同一内容重投返回原事实；同键异内容返回 `request_id_conflict`。CAS 失败返回 `version_conflict`，不得覆盖并发事实。
- 审批绑定 `(namespace, plan_id, revision, definition_digest, execution_envelope_digest, approval_policy_version)`，并记录唯一 `decision_id`、`approved|rejected`、可信 `decided_by`、时间、可选到期时间及原因。审批历史追加保存。
- 撤销以新的唯一撤销事实引用原 `decision_id`；不删除或改写批准。过期或撤销关闭新准入。已准入工作按在途边界处理。
- 批准只覆盖其绑定的完整 revision，不覆盖未来 revision 或其他 Plan。重规划后的批准从空开始。

稳定主键按字段编码/结构化元组生成，不使用无分隔字符串拼接。不同 namespace/session 的身份不得互相读取、批准、验收、领取或引用证据。

## 4. 状态与拒绝

Plan 状态：`draft / reviewing / approved / executing / paused / replanning / cancelling / completed / failed / cancelled`。终态为 `completed / failed / cancelled`，不能复活。

允许迁移：

| 当前状态 | 允许的目标 | 前置条件 |
| --- | --- | --- |
| `draft` | `reviewing` | 定义、DAG、envelope 和 verifier 合法。 |
| `reviewing` | `approved` 或 `draft` | 可信批准绑定当前摘要；拒绝/撤回应保留审阅历史。 |
| `approved` | `executing` | 第一个 Step 原子准入成功时迁移。 |
| `approved / executing` | `paused` | 暂停、批准到期/撤销或确定性阻塞关闭新准入。 |
| `paused` | `approved / executing` | 明确 resume；批准、期限、部署指纹和既有绑定重新核对。 |
| `approved / executing / paused` | `replanning` | 持久记录原因及旧 revision 的活动绑定，关闭新准入。 |
| `replanning` | `draft` | 所有旧绑定达到静止边界后原子发布下一 revision。 |
| `approved / executing` | `completed` | 全部 Step accepted、Plan verifier 通过、无活动绑定或 unknown。 |
| 任一非终态（除 `cancelling`） | `cancelling` | 持久取消意图和活动绑定通知同事务提交。 |
| `cancelling` | `cancelled` | 活动执行及 unknown 均已结算，无未处理在途事实。 |
| `approved / executing / paused / replanning` | `failed` | 宿主明确结束且活动执行及 unknown 已结算。 |

失败/拒绝/不确定的 Step 默认暂停 Plan 并阻塞后继；不自动终态化、不无限重试。`rejected` 或 `inconclusive` 验收在与记录相同的事务内将 executing Plan 暂停；验收可在 paused 状态重新读取，恢复执行则需要有效批准和显式 `resume_plan`。控制 API 以稳定拒绝码报告至少这些类别：`invalid_definition`, `unsupported_schema_version`, `content_conflict`, `request_id_conflict`, `version_conflict`, `invalid_transition`, `approval_required`, `approval_expired`, `approval_revoked`, `plan_control_closed`, `dependency_pending`, `dependency_rejected`, `dependency_failed`, `deadline_exceeded`, `execution_limit`, `active_binding`, `retry_not_allowed`, `task_id_conflict`, `binding_conflict`, `namespace_mismatch`, `evidence_unavailable`, `evidence_mismatch`, `deployment_drift`, `cancel_requested`, `operation_unknown`, `store_unsupported`, `scan_limit`。实现可增加更精确代码，不可把这些失败降格为成功或无区分的空结果。

## 5. DAG、预算和执行准入

- 图只引用本 Plan 当前 revision，依赖规则只有 `all_accepted`。ready 表示所有前置 Step 有有效 `accepted` 记录；Task completed、模型声明和工具 receipt 单独均不足以满足依赖。
- Step 视图为派生值：`blocked / ready / running / waiting / awaiting_validation / accepted / rejected / failed / cancelled / superseded`。优先级固定为 superseded revision 标 `superseded`；有效 accepted 优先；再看活动 binding 对应 Task/Run；Task completed 且无验收为 `awaiting_validation`；其余由控制门和依赖推导。
- readiness 查询是有界提示，不授予执行权。宿主按授权 namespace 调用 `read_ready_steps(namespace, limit, cursor)`；cursor 不能跨 namespace 复用。`ready_since` 取审批及最后一项前置验收满足时的持久事实时间。查询必须可续扫，blocked 节点不能因占据首个分页永久遮住 ready 节点。
- `claim_plan_step` 在单个控制事务里重检 Plan/revision/control version、有效批准、envelope/deadline、取消意图、前置验收、运行次数、现有 binding、Task/Run 唯一性和预算；原子写 binding、Task、INITIAL 输入、claim/fence、准入事实和通知，然后才可由 Worker 驱动 Harness。
- 同一请求重投收敛到原 binding/Task/Run，不双建输入；崩溃恢复沿用这些身份并递增现有 attempt/fence。业务 retry 必须显式调用 `retry_step`，创建新 Task/Run/binding，并在 `retries_binding_id` 中引用旧 binding；不得复活旧 Task。
- `retry_step(namespace, plan_id, step_id, retries_binding_id, expected_control_version, request_id, principal, holder, ttl_seconds)` 只接受当前 revision 中该 Step 最新、`rejected` 的 binding，且其 Task 必须已正常 completed、所有验收 criterion 已有最终结果并至少一项 rejected。重试仍重验当前 Plan/批准/deadline/依赖/并发/累计准入上限；`inconclusive`、operation unknown、缺验收和过期/撤销批准均不允许启动重试。新 binding、Task、INITIAL 输入、claim/fence、事件/outbox 与 Plan control version 在同一事务提交。重复请求复用新 binding；Worker 后续恢复也沿用同一 Task/Run/binding。
- 已有执行权的重新领取只能恢复同一不可变规范和原 binding；不得借恢复顺便准入另一 Step。`read_schedulable` 与普通 `claim_task` 不得返回/领取计划绑定 Task；不具备编排能力的 Worker 明确拒绝驱动它们。
- Reference `LocalWorker` 只有在 `enable_orchestration=True` 且宿主给出单一 `orchestration_namespace` 时才读取 Plan 候选；P0 的 Plan 候选优先级固定为 0，沿用 Worker 的 aging、公平轮转和并发上限。多 namespace 宿主可按授权边界创建多个 Worker。
- Plan 额度：最多 128 Steps、1,024 edges、256 KiB 定义、32 层结构、每 Plan 累计最多 256 次 Step 新准入（所有 revision 与 retry 共用，不重置）；`max_step_executions=0` 合法并禁止准入；deadline 为不可刷新的绝对时间；默认并发上限 1。各 Task 仍遵守现有预算。首版不声称有共享 tokens/cost/dispatch 预算。

## 6. 验收与完成

`EvidenceSnapshot` 是宿主在控制事务外读取的有界、不可变证据快照，绑定 namespace/Plan/revision/step/binding/Task/Run 与 Run 版本，包含证据引用、来源版本、payload、未结算操作和读取错误。证据读取超时、不可用、缺引用或仍有 unknown 时结果为 `inconclusive`，不得把不确定状态转为业务拒绝或成功。输出契约只支持 `required_keys`、`equals`、`evidence_refs` 三类确定性检查；未知规则明确拒绝。

验收记录绑定 Plan/revision/step/binding、Task/Run、verifier ID/version、criteria 摘要、证据引用、来源快照摘要、决定主体/时间及原因。结果为 `accepted / rejected / inconclusive`。`inconclusive` 不解锁下游；读取不可用与业务拒绝分开表达。证据必须核对 namespace、Task、Run、binding 和版本/摘要。

verifier 由宿主通过精确的 `(verifier_id, verifier_version)` 显式注册；每项配置时间上限和最大输入字节数，执行只读、异步且可取消。未注册、超时、超限和普通 verifier 错误均返回 `inconclusive`；取消传播给调用方。禁止在 verifier 内运行 shell、派发模型请求或调用工具。外部证据先在事务外读取为不可变快照，再在短事务内 CAS 对照 binding 与本地版本。验收期间会重新读取 Task、Run 与 evidence snapshot；来源版本/摘要变化记录为 `source_changed` 并保留 inconclusive，保证 verifier 结论不跨快照提交。该读取不跨控制存储写事务；accepted 绑定某个具体、不可变的来源版本，不承诺共享工作区此后持续不变。accepted/rejected 是同一 binding 的终局互斥决定；inconclusive 可在可读证据后重判。并发矛盾决定由 CAS/唯一性约束拒绝其一。SQLite 计划 schema 当前为 v2；启用编排时 v1 可写库原子迁移到 v2，read-only 打开 v1 必须只读拒绝，不改表或 schema 标记。

Step 只有在 Task/Run 正常完成、相关 operation unknown 已解决、证据可读且版本匹配、verifier 接受并成功记录时才 accepted。Task completed 不自动转 accepted。Plan 只有所有当前 revision Step accepted、Plan criteria 通过且无活动执行/unknown 才完成。

## 7. 暂停、取消和重规划

`claim_plan_step` 成功提交即属于“已准入、可能开始外部执行”；它不证明模型或工具已开始。pause/revoke/expiry 仅阻止之后的新准入。claim 与 pause/revoke 按同一控制事务序列先后决定是否在途。已准入工作可能沿既有边界继续，不能承诺追溯取消远端请求。

控制门关闭时，维护型 `reconcile` 只读账本、发现已提交事实、接纳宿主证据并结算，不得调用可能继续派发的普通 `Harness.resume`。会重新派发的恢复必须先有同 revision 有效批准和显式 resume；取消恢复先应用持久取消意图。更快停止通过现有 Task cancel/期限语义请求，不承诺强杀不协作 handler。

重规划流程固定为：原子关闭准入并记录旧 revision 活动绑定 → 对活动 Task 持久请求取消 → 宿主驱动取消/恢复/核对 → 保留真实完成、失败、取消及 unknown → 所有旧 binding 不再可能执行且 unknown 已结算后才 CAS 发布新 revision → 新 revision 从 draft 重新审阅和批准。P0 不支持两个 revision 同时执行，也不自动复用旧 revision 的 accepted Step。

## 8. 存储、兼容与诊断

- 编排能力由可选 `OrchestrationStore` port 表达，不强迫既有第三方 Task backend 实现新方法。`SqliteControlStore` 与 `MemoryControlStore` 通过 `enable_orchestration=True` 显式启用；默认打开旧控制库不建 Plan 表。缺能力或无法保证同控制库事务时显式 `store_unsupported`。
- `SqliteControlStore` 在现有控制数据库/连接实现 Plan、revision、审批、绑定、验收、事件和 outbox 的原子写；`MemoryControlStore` 在同一锁内实现相同合同并声明 volatile。禁止 Plan DB 与 Task DB 的假原子双写。
- Plan、控制存储和诊断 schema 独立版本化。只读诊断不建表、不迁移、不创建不存在的数据库；旧库/未知 schema 的行为是明确拒绝或报告 unsupported。
- 事件与控制状态同事务提交。通知/回调仅加速；有界 outbox 和 `reconcile(limit, cursor)` 可从持久 binding 发现遗漏并幂等收敛。写事务失败时不得继续外部派发。
- `PlanInspector` 只读返回 Plan 控制版本、审批范围、Step 依赖/阻塞、Task/Run/binding/验收引用、活动 claim、限额、近期事件、unknown/unsupported/integrity 覆盖限制；提供 JSON 与 Markdown，不读取凭据或无界原始输出。

旧单 Run、普通 Task、MCP、Extensions、Context API 和基础包依赖保持不变。新增 public API 必须更新 `docs/contract.md`、公开 API snapshot/whitelist、文档索引和验收映射。P0 的阶段验收和仍未验证项记录在 `docs/plans-progress.zh-CN.md` 与 `docs/plans-acceptance-review.zh-CN.md`。
