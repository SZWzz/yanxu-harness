# 持久输入与子任务恢复：发行阻断修复实施计划

日期：2026-10-16。核对基线：`42a4ee8`。状态：**已实施（RF0–RF6），待独立复验**。实施提交见 `2645822` 及其之前的 `75480b4`…`81f5109`；五探针实际输出、验收矩阵证据、版本/兼容变化与未执行边界见[发行阻断修复交付报告](durable-input-orchestration-release-fix-report.zh-CN.md)。

依据：[打包前独立复验](durable-input-orchestration-release-review.zh-CN.md)、[独立发行探针](../scripts/probe_orchestration_release_review.py)、[原统一实施计划](durable-input-orchestration-implementation-plan.zh-CN.md)。本计划只处理复验发现的五项问题及其恢复、兼容和验收闭环，不重新建设任务编排框架。

接手时重新核对 HEAD、工作树与适用规范。当前审查报告、探针及 SNAPSHOT 修改尚未全部提交，必须保留；本文中的建议 API/事件名称不是已有实现。

## 1. 目标、范围与交付责任

| 编号 | 级别 | 必须消除的行为 | 本计划阶段 |
| --- | --- | --- | --- |
| DIO2-I1 | P1 | 先接纳的 follow-up 被较新 steering 的 MAX(applied) 水位永久跳过 | RF1 |
| DIO2-R1 | P1 | child_result 恢复在系统指令/配置漂移后仍发模型请求 | RF2 |
| DIO2-B1 | P1 | 同一父 Run 恢复后刷新 max_turns、累计轮次和消耗 | RF3 |
| DIO2-R2 | P2 | 子任务仍 queued、结果尚不存在，父 resume 就调用模型 | RF2 |
| DIO2-C1 | P2 | 同 spawn 身份改变初始任务、child Run 或期限仍被视为重复 | RF4 |

完成定义：

1. 独立探针五项全部 PASS，正式 pytest 覆盖相同真实链路。
2. 较早待处理输入始终可见；父恢复先核对原配置与已提交结果，再按原预算继续。
3. spawn 完整语义在控制事务内校验，旧关系和旧快照有明确兼容结果。
4. 真实 SQLite、进程故障和源码树外安装的证据与模拟测试分别记录。
5. 更新实际接口、诊断、测试位置及生成清单；不能只让原五个最小输入碰巧通过。

DeepSeek 负责实施、回归、兼容说明与本地门禁。**本轮保留版本 `0.1.0a2`；最终独立复验通过后，由 Codex 统一升级到 `0.2.0a1`，构建正式 wheel/sdist 并交付安装包。** 实施期间的门禁构建属于验证产物，不上传或标为最终发行，也不提前接入 StoryIDE/MCP。

## 2. 不变约束

- 保持一个 Core Runner 和纯 reducer；worker/coordinator 只提供调度与控制事实，不增加第二套模型—工具循环。
- 输入应用、外部结果应用、Run 快照与相关事件有同一个控制库事务边界；派发必须等待提交成功。
- Task 和 Run 的已提交终态不复活。正常完成候选可以进入受控等待；failed/cancelled/usage_limited 不得被 child 屏障无条件改回等待。
- Gateway/宿主操作账本仍是工具效果证据所有者；child 通知与会话文本不授予权限，也不新增第二份工具账本。
- 单 Run 旧后端仍可使用原 API；启用新持久能力却缺必要端口时，在接纳/派发前明确拒绝。
- 保留 Python 3.10 语法和标准库基础安装；测试使用脚本 provider，执行路径使用真实 Harness/SQLite/Gateway。
- 不修改探针期望来掩盖失败，不删除测试、加 xfail 或放宽导出扫描换取绿色门禁。

## 3. 分阶段顺序

| 阶段 | 交付 | 退出条件 |
| --- | --- | --- |
| RF0 | 冻结原复现、恢复契约与红测 | 五个真实行为断言在修复前失败，现有正常路径仍可定位 |
| RF1 | 有界待处理输入查询与终结屏障 | 混合顺序无空洞，长历史不退化，提交竞争可解释 |
| RF2 | child 恢复共同守卫 | 漂移、结果缺失、归属不符时零派发，原等待保留 |
| RF3 | 明确的子结果等待/恢复状态与连续预算 | 原 Run 在预算内续跑，结果只应用一次，终态不复活 |
| RF4 | 完整 spawn 语义身份 | 同语义重投幂等，不同语义事务内冲突 |
| RF5 | 真实进程、兼容、诊断与示例闭环 | 故障窗口可恢复，旧记录不猜补，新分支有真实证据 |
| RF6 | 全量门禁与交付报告 | 所有缺陷闭环，并可交由 Codex 独立复验打包 |

RF2 的“守卫修复”先独立提交；RF3 再补齐明确的恢复状态，不把所有变化塞进同一个提交。每阶段保留已通过回归，未完成阶段如实标注。

## 4. RF0：先把失败写进默认回归

基线事实：默认套件 **1253 passed / 1 skipped**；独立探针 **0 passed / 5 failed**。这两个数字描述核对版本，不是本计划实施结果。

建议新增 `tests/test_tasks_release_regressions.py`，将五项探针改为默认收集的测试；公共装配可提取到现有测试 helper，避免复制整个 Harness。保留独立脚本作为交叉检查入口。若重构 helper，仅做等价复用，不用替身替换被测恢复路径。

红测至少包含：

- follow-up(seq 1) + steering(seq 2)：初始请求只读 steering，后续仍读到 follow-up，最终均可查询 applied。
- 父已等待、child 已完成：改变系统指令后恢复拒绝，父 provider/工具新增调用为零。
- child 仍 queued、无结果通知：调用公开 resume 不派发，不把“No committed result”加入会话。
- 父 max_turns=1 且已经用完：child 返回后不再调用父模型，轮次不回零。
- 同 spawn 身份分别只改 initial_input、child_run_id、deadline_at：每项都 config_conflict，且不多建 Task/Run/grant/outbox。

同时冻结这些结果：预算不足用明确 usage_limited；结果未就绪保持原 suspended 或返回结构化可重试拒绝；配置不一致沿用 ResumeRefused；spawn 语义不一致为 CONFIG_CONFLICT。记录实际异常/返回字段，不能只断言“抛了某个 Exception”。

## 5. RF1：按待处理事实读取，消除输入空洞

### 5.1 存储接口与读取

主要接点：`tasks/inputs.py`、`tasks/inbox.py`、`tasks/ports.py`、`tasks/types.py`、`DefaultHarness._input_arrived` 和终结事务。

采用**按状态与类型过滤的有界读取**。建议新增 `poll_pending_inputs(task_id, kind, limit) -> InputRead`：

- 仅选择目标 Task 中 pending/可重领 claimed 的指定对话类型，按 sequence 升序。
- 返回候选及同一读取快照中的 inbox_revision；读到候选不确认消费。
- steering 与 follow-up 分开查询，保留已有站点、all/one-at-a-time 和有界批量语义。
- AFTER_INNER 的 steering 兜底若保留，先确定没有可取 follow-up，再查询 steering；两次结果与 revision 必须来自同一逻辑读取边界。
- 不从 applied 历史第一页扫描，不从 MAX(applied) 开始，不因返回量受限宣称 inbox 已空。
- SQLite 与 MemoryControlStore 行为一致；共享契约套件覆盖两者。SQLite 使用实际事务和适合 task/state/kind/sequence 的索引。

`read_inputs` 继续用于完整历史/诊断分页。`read_applied_watermark` 可保留为观测高水位，但不得决定哪些输入仍有资格消费；文档明确其不是连续消费前缀。Core 已存 input_watermark 也不能据此排除更早 pending。

新增能力同步进入端口声明、能力检测与拒绝测试。不要仅用 getattr 缺失后回退到有缺陷的旧查询；普通单 Run 路径不要求实现新端口。

### 5.2 正常终结与应用事务

- 正常 completed 的最终控制事务检查：观察 revision 未过期，且不存在仍应处理的 pending/claimed 对话输入，**不按最大水位过滤**。
- 有新输入则回到已提交、可安全恢复的输入边界；不从新 Run 起步，不无限自旋。
- revision 检查与未处理项检查在最终写事务中完成。仅“事务外查到为空，然后提交”仍有竞争缺口。
- 输入接纳也必须核对关联 Run 的可接纳状态，覆盖“Run 已终态、Task 尚未结算”的窗口；重复已存在 input_id 的返回按原幂等契约处理。
- failed/cancelled/usage_limited 不为了清空输入继续派发；保留未应用/取消原因和原始事实，不能假标 applied。
- 应用标记、Run transcript/快照和事件仍一次提交；回滚后输入可再次读取，模型/工具调用为零。
- 旧数据库若已有终态 Run + pending 输入，只读诊断标出遗留项及原因；不得自动复活。任何后续重新执行由宿主显式新建 Task。

验收：两类输入全部排列、多个 follow-up、超过 200/多页历史、控制项占满历史页、批量边界、读后提交前进程退出、空读/终结竞争，以及 Run 已结束但 Task 未结算时的新输入接纳。

## 6. RF2：恢复前统一校验配置与结果就绪

### 6.1 共同守卫顺序

主要接点：`DefaultHarness.resume`、`_resume_child_result`、已有 arrangement 校验、`tasks.children.read_committed_result`。

让 batch / poll / child_result 的恢复共享可复用的检查函数；分支在完成通用检查后才驱动 Runner，不允许提前 return 绕过它们。

顺序固定为：

1. 核对 Run 的 namespace/session，取得 Run lease，重读当前状态。
2. 先处理已提交终态和取消请求；按当前 authoritative 状态分类。
3. 验证快照版本/合法相位，提取**已提交** arrangement 与预算事实。
4. 比较当前 instructions/config/tools/policy/exposure；漂移或缺少基线按已有结构化拒绝处理。
5. 若为 child 等待，重新读取可信关系、子终态与结果事实，确认确已就绪。
6. 按 RF3 检查剩余额度，提交已接受的结果/恢复状态，再派发同一 Runner 的动作。

异常路径释放本次租约，不覆盖旧部署指纹、原等待原因或已有结果。检查函数不读模型、不执行工具；Core reducer 不读数据库。

### 6.2 子结果必须可证明

结果读取返回结构化 ready/not_ready/inconsistent，不用普通文本代替事实缺失。可信读取至少核对：

- pending interaction 的 child 与 spawn 引用属于当前 parent。
- relation、parent/child Task、子 Run 的 namespace/session/固定 Run 映射一致。
- child Task 和真实 child Run 的已提交结局可相互核对；只有 Task 被手工标完成不够。
- 稳定完成通知属于该 relation/child，引用与结构化 ChildResult 一致。
- 缺通知但已有真实终态时，从既定 completion/outbox 收敛流程恢复通知；不能临时编一段字符串放行。
- accepted=false 的业务验收结果、failed/cancelled/usage_limited 都是可见结果数据；不要与“尚不存在结果”混淆，也不要默认无限重试。

not_ready 时保持原等待，或给明确可重试拒绝；不追加结果消息、不更新 turns/usage、不做预算预留、不发模型/工具。resolution 只作提示，不信任其 ready、approved、result、权限或任意 child_id。

给该控制库读取增加明确的一致性边界；完成事实提交后不可被外部 payload 覆盖。跨库引用无法核对时保守拒绝。

验收：所有漂移维度、缺基线、未知快照；提前/重复 resume；错误 parent/namespace/session；伪造 resolution；child 终态存在但结果未提交；业务验收失败和子失败的合法恢复。拒绝场景均断言零外部派发及原持久状态未被改写。

## 7. RF3：让子结果等待成为同一 Runner 的明确恢复状态

### 7.1 修复状态接线

当前 `_resume_child_result` 调用默认 `RunRequest(prompt="")`，`_drive` 新建 ExecutionState，导致 max_turns=None、initial_turns_used=0。**本轮不再用这条 fresh start 路径恢复 child 等待。**

建议实现一个小范围的 `Runner.resume_external` 入口和类型明确的外部等待/结果事件，复用现有驱动循环：

- 正常完成候选在最终终态之前通过已有 finish_barrier 适配器检查 child；需要等待时，Core/Run 都保存合法的 child_result 等待快照。
- 等待/完成判定通过明确动作与事件表达，不能把已终态 Core 快照私下改个 phase 当成暂停。已有公共 finish_barrier 回调签名可保持，由 Runtime 接线至 Runner。
- 复用 suspended 状态时扩充其合法 pending interaction 判别；确需新增等待相位/完成判定相位时，同步 reducer、状态校验和快照版本。
- `resume_external` 只接受匹配等待引用与基线的非终态快照，从可信 Runtime 接收已核对的结果事件。
- reducer 纯处理结果输入、去重和继续决策；模型请求准备、工具执行和提交仍由原 Runner 执行。
- 新 attempt 保持原 task/run，事件 ID 空间与 consumed 集合不冲突；不能删掉 consumed 来避免去重错误。
- 只有正常完成候选进入 join。取消、失败、超限按明确策略结算所属子树并保留未知效果，不被 finish_barrier 改成无限等待。

这是对既有状态机的受控补齐，不是增加一层 Agent Loop。旧 batch / poll 恢复入口保留，并复用上述守卫。

### 7.2 结果只应用一次，故障可恢复

稳定结果身份使用既有 completion/outbox/input 引用及可核对版本。记录“该结果已纳入父 Run”的事实，复用控制库的输入引用/原子提交能力；不要建立新的工具效果账本。

同一事务提交结果应用引用、父 transcript/快照、相关事件和继续相位：

- 提交前死亡：原等待/结果仍可重读，尚未派发。
- 提交后、请求前死亡：恢复快照证明结果已应用，不再次追加消息。
- 请求派发后死亡：沿用现有请求实例、预算与 Run/operation 恢复证据，不承诺模型请求恰好一次。
- 重复 completion、重复 wake/resume：不会重复应用同一结果；同一个 child 的不同正文不能覆盖已接受事实。
- 子结果始终为不可信任务数据；原始结果保留受控引用，公开诊断不泄漏全文。

不能只在进程内 set 里记“已看过”。若新增应用引用字段，说明版本、旧快照读取、写入和迁移规则。

### 7.3 预算和消耗连续

从原快照恢复 max_turns 与已提交 turns_used；同一逻辑 Run 累计轮次不减。不要默认新请求参数替代原预算。已有父 max_turns=1、已用 1 的探针，恢复后应 usage_limited，**父模型总调用仍为 1**。

- usage 与测量按真实 request_instance/稳定测量身份去重累计；不是按文本或逻辑 action_id 去重，也不把恢复后的消耗覆盖此前消耗。
- 任务 deadline_at 仍为持久绝对时刻；本次 invocation 的可用期限与宿主 max_runtime_seconds 等有效上限取最小值。
- invocation 时限保留原“每次调用”语义，不把上次 deadline_seconds 的相对时长猜成持久任务绝对期限。
- 原上下文预算、摘要消耗、输出限制、共享 root 预留继续有效；验证这些接线，不凭本次只复现轮次缺陷就宣称所有维度原先都失败。
- 所有新实际派发先预留并提交；结果重读、过早恢复和拒绝漂移不得产生新派发预留。未知费用不当零退款。
- 到期/超限之后不发模型或工具；最后的停止证据提交仍有清理边界。
- 父进入 usage_limited 时按既有子树策略记录取消/结算，不伪造仍在执行的 child 已停止。

验收：max_turns=1 耗尽、剩余 1 轮、多次 child 等待、恢复再次暂停、重复结果、迟到 usage、已到期和在派发中到期、摘要/上下文额度、预算账本与实际请求计数一致。

## 8. RF4：冻结完整 spawn 语义并在注册事务中比较

主要接点：`ChildSpawnRequest`、`ChildRelation`、`ChildCoordinator.spawn`、两种 ControlStore 的 register_child，以及相关序列化/迁移。

保留授权摘要的用途，另存版本化的 `SpawnSpec` 与完整语义摘要。建议字段：

| 纳入语义身份 | 规范化规则 |
| --- | --- |
| namespace/session、parent/child、child_run_id、spawn_identity | 精确核对，固定关系与 Run 映射 |
| initial_input | 采用含 kind、text、语义 parameters 的规范化内容，不只摘要提示文本 |
| 有效授权、profile、tools、workspace、result_schema | 对象键排序；无序工具集合排序；保留有语义的数组顺序 |
| budget unit/amount/有效约束 | 规范化单位、有限数值与零值语义，不将 None 和明确 0 混同 |
| deadline_at、priority | 绝对期限或明确 None；规范化后比较 |
| 行为协议/摘要算法版本 | 明确可支持版本，未知版本拒绝 |

created_at、观察时间、worker/attempt、通知重试次数等非工作语义字段不参与摘要。operation_id 继续是 Gateway 证据引用，定义它与稳定 spawn 身份的绑定规则；不能另起操作账本，也不能因回执补记就重复创建 child。

关键实现：

1. 首次注册在一个控制事务中检查父状态/取消/claim、计算后的 Spec、额度与唯一约束，写关系、子任务/initial、grant、outbox。
2. 重投在同一事务中比较已保存 Spec/digest，**不能只依靠 coordinator 的事务外预查**。
3. 相同身份同语义返回原 relation、原有效授权和原 grant；不使用本次新计算的授权替代原事实。
4. 不同正文/Run/期限等语义差异明确 CONFIG_CONFLICT，所有相关记录、额度和计数不变。
5. 并发两次首次登记一项成功，另一项按原事实幂等或冲突，不出现两个 created=True 或两个子任务。
6. 不从当前重投请求给旧 relation 回填“原 Spec”。仅当原 Task、initial、授权/预留等不可变记录足够证明时，才能有明确的一次性迁移；否则旧记录可诊断/取消，spawn 重投保守拒绝并说明 legacy evidence insufficient。

验收：正文、Run ID、期限、priority、工具、workspace、schema、预算分别单独改变；字典键/工具集合顺序变化应仍幂等；两连接/两进程同身份同/不同语义竞争；控制提交成功但 Gateway 回执未写时重投。拒绝前后核对 Task/Run/关系/outbox/预留数量与金额。

## 9. RF5：版本、真实故障和可解释交付

### 9.1 兼容必须明确

- 若新增 Core 等待相位/事件/持久应用引用，升级实际受影响的执行快照或事件格式版本，更新支持集合、序列化和静态校验。
- 现有 child_result Run 可能 status=suspended，而 execution 已是旧完成快照。仅在身份、基线、预算、transcript 和等待关系都能从已保存事实验证时迁移成新等待快照；证据不足返回结构化不可恢复原因，不能补成无限轮次。
- 单纯缺少新增可选字段可保守读取；缺少恢复所需证据则不派发。新版本读旧事实不等于可以安全恢复所有旧事实。
- ChildRelation/SpawnSpec、控制表/索引和能力端口各自记录变更；如果仍共用 TASK_SCHEMA_VERSION，解释对现有 Task/Input/预算记录的兼容，不能一并提高最低版本后弃读旧数据。
- 旧 max-applied 水位只作观测保留；不需要伪造一条连续消费历史来“迁移” pending 空洞。
- 不伪造外部效果；旧终态 Run 的遗留 pending 只诊断，不自动复活。

### 9.2 进程证据与公开诊断

至少新增两组真实新进程故障：输入纳入事务前/后；child 结果纳入父快照事务前/后。用 Event、管道或事务钩子定位窗口，不靠随机 sleep。重复通知与两个恢复者竞争另设确定性测试，保留旧 fence 拒绝。

诊断补齐恢复拒绝原因、等待对象、结果是否 ready/已应用、累计预算与原基线引用；查询不消费输入、不推进 Run、不续租。缺证据用明确字段说明，禁止把“No committed result”当模型消息。

父子示例/冻结案例至少有一条实际 DefaultHarness → 同一 Runner → SQLite/Gateway → child → 原 parent 恢复链路，断言两边 Run/Task 和持久结果身份。mini Harness 的控制层测试可保留且明确标注，不能承担完整恢复验收。

## 10. 验收矩阵

| 编号 | 场景 | 核心断言 |
| --- | --- | --- |
| FRI-01 | follow-up 早于 steering、反向及多条交错 | 各按原站点应用，无 pending 空洞；不提前吞 follow-up |
| FRI-02 | 200+ 历史、控制项满页、有限批量 | 最新和较早未应用项都可见，读取有界 |
| FRI-03 | 输入应用与终结竞争/提交失败 | 成功接纳可追踪，回滚零派发，终态不复活 |
| FRR-01 | 指令/config/tools/policy/exposure 各种漂移 | 结构化拒绝、零模型/工具/新预算预留，原基线保留 |
| FRR-02 | queued/running child、缺结果、错误归属 | 原等待保留，零派发，不追加占位结果 |
| FRR-03 | 合法失败/超限/验收失败结果 | 与缺结果区分，真实结局作为数据可见 |
| FRR-04 | child 完成通知重复、父恢复后再崩溃 | 结果只应用一次，恢复相位及调用身份正确 |
| FRB-01 | max_turns=1 已耗尽/还剩 1 次 | 不刷新预算，累计 turns 与实际调用一致 |
| FRB-02 | 多次等待、deadline、summary/root 额度 | 不刷新绝对期限，不重复预留/退款，usage 不覆盖 |
| FRC-01 | spawn 每个语义字段分别变化 | CONFIG_CONFLICT，原关系/额度/通知不变 |
| FRC-02 | 同身份同语义的规范化重投/并发竞争 | 原事实幂等返回，一个 child/Run/grant |
| FRV-01 | 旧 child 等待、旧关系、缺基线/未知版本 | 可证明才迁移，不能证明则派发前拒绝 |
| FRV-02 | 单 Run 老后端与新能力不足后端 | 原路径继续可用，缺新能力明确拒绝 |
| FRV-03 | 独立五探针和默认回归 | 五探针全 PASS，无 xfail/跳过替代 |
| FRV-04 | 源码树外发行安装 | wheel/sdist 安装后的真实恢复及 CLI/示例均可运行 |

对关键竞态同时验证 SQLite 与另一连接/新进程；Memory 测试不代替持久证明。运行耗尽场景先证明合法请求次数，再检查持久计数，避免“两个错误计数恰好相等”。

## 11. RF6：验证命令与实施交付

先使用安装了实际 checkout 与测试 extras 的独立环境，避免子进程 CLI 导入旧包。离线 wheelhouse 已存在时：

```bash
python -m venv build/dio2-fix-venv
build/dio2-fix-venv/bin/python -m pip install --no-index \
  --find-links build-wheelhouse '.[dev,yaml,pydantic]'
```

用该解释器跑以下命令；后续源码变更必须重新安装，或另建新环境，不能继续测试旧 wheel：

```bash
python scripts/probe_orchestration_release_review.py
python -m pytest -q tests/test_tasks_release_regressions.py \
  tests/test_tasks_runner_recovery.py tests/test_tasks_input_flow.py \
  tests/test_tasks_inbox.py tests/test_tasks_children.py \
  tests/test_tasks_budgets.py tests/test_tasks_scheduler.py \
  tests/test_tasks_contract.py tests/test_tasks_acceptance_matrix.py \
  tests/test_diagnostics_tasks.py
python -m pytest -q tests/test_runtime_recovery_gaps.py \
  tests/test_runtime_resume.py tests/test_runtime_execution_snapshot.py \
  tests/test_runtime_partial_recovery.py tests/test_runtime_commit_boundaries.py \
  tests/test_runtime_isolation.py tests/test_events_and_versions.py
```

`test_tasks_release_regressions.py` 为本计划建议新增文件，不能在文件尚不存在时记为已验证。若分拆正式测试文件，交付报告列出实际等价命令。新增 Core 事件/恢复入口时执行对应 reducer、执行边界、取消和持久化测试；不只测 Harness 返回值。

实现、文档和兼容矩阵同步后：

```bash
python scripts/export_public_snapshot.py --refresh-manifest
python -m pytest -q tests
python scripts/ci_check.py --offline --wheelhouse build-wheelhouse
```

上述门禁会构建验证用 wheel/sdist 并检查源码树外安装。若增加了新恢复场景，确认复制到树外的测试仍覆盖该分支；测试依赖必须由仓库携带，不能只在本机临时目录找到 helper。

最终交付至少包含：

1. RF0–RF6 各阶段提交、修复前/后断言，五探针实际输出及退出码。
2. 实际 API、等待/恢复状态图、事务/故障窗口表和所有格式/能力版本变化。
3. 每个验收矩阵编号对应的测试、实际解释器/平台与真实/模拟故障说明。
4. 更新 tasks-contract、adapters、recovery、verification、当前架构及相关样例；原审查失败事实不删除，只追加新核对点。
5. 已执行完整测试和离线门禁的命令、数字、日志位置；远程 CI/真实模型/其他平台没有执行就单列。
6. 最后刷新 SNAPSHOT，再核对清单测试和 git diff --check。后续文档再变也要重刷。
7. 可由 Codex 复验的提交与工作树说明；实施方“已完成”不代替最终独立发行验收。

## 12. 可直接交给 DeepSeek 的任务

> 请实施本文件的 RF0–RF6，先保留工作树里的独立审查报告与五项发行探针。把 DIO2-I1/R1/B1/R2/C1 转为默认回归红测：修复 MAX(applied) 丢弃较早 pending 输入；让 child_result 恢复先核对原配置基线与可信结果就绪，再从明确的持久等待快照继续同一 Runner，继承原轮次、期限和累计消耗，结果纳入与应用引用原子提交；为 spawn 保存并事务校验完整语义身份，不只比较授权摘要。保持单一 Runner、纯 reducer、Gateway 唯一工具效果账本、终态不复活、旧单 Run 后端可用及 Python 3.10/标准库基础安装。补齐 Memory/SQLite 契约、真实进程故障、旧快照/关系迁移、拒绝时零派发和真实父子示例。五探针全 PASS 后跑完整 pytest 与离线门禁，提供实际命令、提交、版本/兼容矩阵及未验证边界；更新文档和生成清单。保留 0.1.0a2，不上传发行、不接入 StoryIDE/MCP；最终独立复验通过后由 Codex 升级 0.2.0a1 并正式打包。

