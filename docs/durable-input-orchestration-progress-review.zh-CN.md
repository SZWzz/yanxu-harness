# 持久输入与任务编排：独立实施情况审查

日期：2026-10-16。对照：[统一实施计划](durable-input-orchestration-implementation-plan.zh-CN.md)。

审查开始于 `96ffd5b`；期间实施方继续提交 Q6，最新核对点为 `ff56c78`。两者之间增加示例、测试和快照清单，本文涉及的运行时源码未改变。本报告记录检查时点，不替代实施方后续交付或修复后的验收。

## 1. 判断与阶段状态

**实现已经推进到 Q6，但不能将 Q0–Q5 全部视为验收通过。** 持久存储、同一 Runner 的输入接线、本地 worker、预算、父子关系与只读诊断都有实际代码；独立探针在 Q2、Q3、Q5 的真实组合路径复现了三个 P1 和三个 P2。

| 阶段 | 已观察到的交付 | 本次判断 |
| --- | --- | --- |
| F0–F4 | 四项诊断/评测修复及回归测试 | 相关测试通过；本次未发现新的 F 线阻断项 |
| Q0–Q1 | 版本化对象、协调端口、SQLite 控制库与内存参考实现 | 已落地；不据此扩大为所有竞争窗口已验收 |
| Q2 | 持久 input 引用、输入应用与 Run 提交接线 | 已落地，有输入历史截断与 follow-up 时机缺陷 |
| Q3 | LocalWorker、领取/释放、恢复、并发与公平排序 | 已落地，有终态恢复与 namespace 并发缺陷 |
| Q4 | 任务期限、预留与结算、root 共享预算接口 | 已落地；本次回归通过，真实父子链路仍受 Q5 缺陷阻断 |
| Q5 | ChildCoordinator、持久关系/outbox、等待与取消接口 | 已落地，真实 Run 的终结屏障与配置重投校验未达标 |
| Q6 | 任务只读诊断；三个新增示例及场景测试 | 已有两次提交，真实执行链路证据仍需补齐 |
| V | 文档/API/兼容矩阵、发行安装及离线总门禁 | 本次未执行完整发行门禁，不能写成已独立通过 |

统一计划开头仍写“尚未实施”，而任务契约记录 Q0–Q5 已交付。这是交付状态同步待办；下面的运行缺陷需要先修复，不能只更新文档为完成。

## 2. 验证方法与结果

在临时虚拟环境内，从当前 checkout 非 editable 安装 `.[dev,yaml,pydantic]`，依赖来自本地 wheelhouse。运行时探针使用实际 `DefaultHarness`、`SqliteControlStore`、`LocalWorker` 和现有测试中的 Gateway 装配；仅 provider 使用脚本模型。

- 首轮完整套件：**1239 passed / 1 skipped / 1 failed**。失败是测试运行期间新增示例尚未进入 SNAPSHOT；实施方随后在 `ff56c78` 刷新了清单。
- F 修复、tasks、任务诊断及新增示例/矩阵专项：**188 passed**。
- 在 `ff56c78` 清单刷新后完整套件复跑：**1247 passed / 1 skipped**（67.44 秒）。测试全部通过，额外行为探针仍可复现下面六项缺陷。
- 额外六个行为探针均复现下列问题；其中长输入探针实际运行了 200 次模型请求。
- 未执行远程 CI、真实模型、其他平台、StoryIDE 接入，以及完整 wheel/sdist 离线发行门禁。

复现装配可直接复用 `tests/test_tasks_children.py` 的 `_Fleet/_admit/_request/_policy`、`tests/test_tasks_scheduler.py` 的 `Fleet` 和 `tests/test_tasks_input_flow.py` 的 `_deploy/_context`。这些 helper 装配的是实际 Harness；以下探针没有修改运行时源码，也没有加入默认测试套件。

## 3. 已确认缺陷

### DIO-R1 / P1：父 Run 先完成，子结果唤醒后父任务失败

位置：[LocalWorker._settle](../src/yanxu_harness/tasks/scheduler.py#L545)、[ChildCoordinator.hold_for_children](../src/yanxu_harness/tasks/children.py#L603)。

`child_barrier` 在 `harness.start/resume` 返回后才执行。此时真实 Harness 已提交父 Run 的 `completed`；屏障只把 Task 改成 waiting，没有在 Run 终态之前建立可恢复等待。

复现：

1. 用真实 `_Fleet(max_concurrent=1)` 接纳 parent，登记 child。
2. 创建带 `coordinator.hold_for_children` 的 worker，执行 parent。
3. 检查父 Task 为 waiting，但 `load_run("run-parent").state["status"]` 已是 completed。
4. 执行 child；调用 `complete_child`、`deliver_notifications`。
5. 再执行父任务：返回 failed，错误为 `run 'run-parent' is completed; only a suspended run can be resumed`，父 Task 最终 failed。

这违反计划 §10.3 的“Run 终态前屏障”和并发 1 完整恢复要求。现有屏障测试只断言 Task waiting，未断言父 Run，也未把该路径执行至父恢复。

修复要求：

- 把检查活动 child、决定 join 的边界移到父 Run 正常终结提交之前，持久化同一 Runner 可恢复的等待事实。
- 完成“父实际运行 → 持久等待 → 子实际运行 → 完成通知 → 父原 Run 续跑”的完整链路。
- 不允许复活已完成 Run，也不能只将 Task waiting 当作 Run 已暂停的证据。
- 同时验证子失败、取消、恢复过程重启和并发 1。

### DIO-R2 / P1：Run 已完成而 Task 尚未结算时，恢复误报失败

位置：[LocalWorker._drive_claimed](../src/yanxu_harness/tasks/scheduler.py#L465)、[_run_record](../src/yanxu_harness/tasks/scheduler.py#L654)、[_is_terminal](../src/yanxu_harness/tasks/scheduler.py#L677)。

`load_run` 返回 `VersionedRun`，状态位于 `stored.state`。`_is_terminal` 却读取包装对象的 `.status`，把真实终态误判为可恢复，随后调用 resume。即使只修复类型读取，现有 `run_already_terminal` 分支也只是返回，未将未结算 Task 收敛到对应终态。

复现：

1. 接纳一个普通任务，使用真实 Harness 完成 Run。
2. 在 worker 的 `_settle` 入口注入异常，模拟 Run 已提交、Task 尚未结算时执行者消失；正常释放后 Task 回到 queued。
3. 新 worker 执行原 Task。
4. 观察 Run 为 completed，Task 从 queued 变成 failed，报“only a suspended run can be resumed”。

本探针使用边界故障注入，没有将其冒充真实强杀进程证据。数据库中的中间事实正是需要支持的恢复窗口。

修复要求：

- 统一解码持久 Run 记录，区分包装版本、业务记录和实际状态。
- 读取已提交 Run 终态并幂等投影/结算 Task；不发模型、不改已有 Run 终态。
- 覆盖 completed/failed/cancelled/usage_limited，以及结算前重启和重复接管。

### DIO-I1 / P1：输入历史达到 200 条后，accepted 输入被遗留在已完成 Run

位置：[PersistentInputReader.__call__](../src/yanxu_harness/tasks/inputs.py#L70)、[DefaultHarness._input_arrived](../src/yanxu_harness/runtime/default_harness.py#L594)。

两处都固定读取 `after_sequence=-1, limit=200`，再在 Python 中过滤 pending。历史 applied 记录占满第一页后，序号 200 及以后的输入永久不可见。终结检查也读相同的第一页，因而没有阻止结束。

真实链路探针：

- 初始输入序号为 0；provider 每收到一次模型请求，就通过真实控制库接纳一条 steering，最多 200 条。
- `RunRequest(max_turns=250)`，实际进行了 200 次模型请求。
- 结果：`run_status=completed, records=201, applied_count=200`。
- 最后一条 `during-200` 已 accepted，序号 200，状态仍为 pending；它没有被应用或取消。

修复要求：

- 对待消费项做有界查询，或正确分页跨过历史记录；不能把读取第一页当作整个 inbox。
- 消费查询与终结屏障采用一致的完整可见范围，保留实际处理上限。
- 增加超过 200 条累计历史、多个分页、混合控制项和终结竞争的真实 Run 回归。
- 明确已存在的“终态 + pending”坏状态如何诊断/迁移，不能直接复活 Run。

### DIO-S1 / P2：真实 Harness 在途时 namespace 并发上限失效

位置：[LocalWorker._choose](../src/yanxu_harness/tasks/scheduler.py#L346)、[在途记录写入](../src/yanxu_harness/tasks/scheduler.py#L461)、[slots](../src/yanxu_harness/tasks/scheduler.py#L208)。

`_inflight` 保存 Harness 对象，计数代码却把它当 TaskRecord 读取 `.namespace`。DefaultHarness 没有该属性，在途任务被归到空字符串。

复现：同一 worker 配置 `max_concurrent=2, max_per_namespace=1`，同 namespace 接纳 t1/t2。用 Event 将 t1 阻塞在真实 provider 调用内，然后调用第二次 run_once。

实际观察：

- t1 尚未结束，slots 为 `{"total": 1, "": 1}`。
- t2 仍获得执行权并 completed，同 namespace 超过约定上限。

现有测试向私有 `_inflight` 手工放入带 namespace 的假记录，因此没有覆盖实际值类型。

修复要求：持有明确的在途任务元数据；保留 Harness 句柄用于取消，但并发归属不从 Harness 猜测。用真实阻塞 provider 验证上限、不同 namespace 独立推进和槽位释放。

### DIO-C1 / P2：同 spawn 身份更改配置，返回新的授权却沿用旧关系

位置：[ChildCoordinator.spawn](../src/yanxu_harness/tasks/children.py#L452)。

已存在关系的分支只比较 child_task_id。相同 spawn 身份、相同 child ID，但改变 tools、初始任务或其他配置时，会返回 `created=False`，并带上本次新计算的 authorization，未校验与已持久化关系是否一致。

复现：

1. 第一次 spawn：稳定身份 `spawn:stable`，child ID 为 child，tools 为 search。
2. 第二次相同身份/child ID：tools 改为 read_file，同时改变初始任务文本。
3. 第二次没有 config_conflict，而是返回新授权；`outcome.authorization.digest` 与 `outcome.relation.authorization_digest` 不同。

此证据证明重投配置冲突和授权事实不一致；本次没有进一步宣称已实现越权工具执行。

修复要求：冻结完整 spawn 语义身份，至少覆盖父/child/Run、初始任务、有效权限、工作区、预算、期限和结果协议。相同身份不同配置拒绝；纯重投返回已保存的原授权。并发注册走同样的事务校验。

### DIO-I2 / P2：持久 follow_up 被提前当作首轮输入消费

位置：[PersistentInputReader.__call__](../src/yanxu_harness/tasks/inputs.py#L70)、[Runner._read_inputs](../src/yanxu_harness/core/runner.py#L456)。

Runner 明确传入 site/kind，持久读取器忽略二者，从 steering 和 follow_up 混合候选中选择。因此“等当前工作结束后的 follow-up”可以在初始 steering 轮询时就被应用。

复现：启动前接纳一个 follow_up，脚本 provider 准备两个最终回答。真实 Harness 只发起一次模型请求，会话角色顺序为 `system,user,user,assistant`，follow_up 状态为 applied。它未形成初始回答之后的新一轮。

修复要求：按既有 poll site/kind 消费对应输入，分别保留 steering 的 all 和 follow-up 的 one-at-a-time 语义；增加内存与持久通道行为对照、两类输入混合及恢复回归。

## 4. Q6 仍需补齐的验收证据

新增文件已存在，不能再把三个示例简单列为“尚未编写”。但目前的场景测试与父子示例有不同的验证强度：

- `tests/test_tasks_acceptance_matrix.py` 的 `ScriptedHarness` 替代整个 Harness，而不只是替换 provider；其 start/resume 手工提交 `{"phase":"done"}` 后返回 completed。
- `examples/10_parent_child_recovery.py` 的 `_ScriptedHarness` 同样手工确认输入，resume 直接返回 completed。
- 因而真实 SQLite、子进程和控制面可运行的证据是成立的；这些路径不能证明 DefaultHarness/Core/Runner 的等待与恢复已经正确。
- 已有真实 Harness 父子测试中，一条路径在 parent Run 尚未启动前就设等待，另一条只断言 waiting，不验证恢复至父最终结局。这正好绕过 DIO-R1 的窗口。

按计划 §11.2，仍应以真实 Harness/Runner/Gateway 驱动父子案例，保留脚本 provider，使用独立验收器检查已提交 Run/Task、输入、关系、回执、预算和 outbox。控制层替身测试可以保留，但标注其边界，不承担完整执行链路验收。

完整发行门禁及 contract/adapters/recovery/verification/架构/路线图同步需要在运行缺陷闭环后交付。本文没有把仍在实施的 Q6/V 项直接判成运行时回归。

## 5. 建议实施顺序

1. 先补六项独立探针为回归，尤其断言 Run 与 Task 两边事实，避免只看调度状态。
2. 优先修 DIO-R1、DIO-R2、DIO-I1；分别覆盖真实父子 Run、终态结算恢复、长期输入消费。
3. 修 namespace 槽位、spawn 完整配置幂等、持久输入时机。
4. 以真实 Runner 补齐 Q6 端到端场景，再执行 V 门禁和文档状态同步。
5. 修复后另写新核对提交与实际验证结果；保留本报告作为历史失败证据。

本次仅新增审查记录并按仓库流程刷新生成清单，不修改业务实现，不代替 DeepSeek 执行上述修复。

