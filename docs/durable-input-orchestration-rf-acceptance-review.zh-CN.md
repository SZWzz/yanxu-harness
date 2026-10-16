# RF0–RF6 修复后的独立验收

日期：2026-10-16。核对提交：`da2bb91`。检查开始时工作树干净。依据：[修复实施计划](durable-input-orchestration-release-fix-plan.zh-CN.md)、[实施方交付报告](durable-input-orchestration-release-fix-report.zh-CN.md)。

**结论：原五项缺陷的最小复现已通过；RF3/RF5 的完整恢复验收仍有一项 P1、两项 P2，暂不进入正式打包。** 本报告没有把已经通过的五个复现重新判为失败。新增场景对应原计划的“结果提交后进程退出”“多次子任务等待/恢复”“超限不改成等待”，不是新增产品功能。

## 1. 实际验证

在临时 Python 3.12.13/macOS arm64 虚拟环境内，从当前 checkout 非 editable 安装 `.[dev,yaml,pydantic]`，依赖仅来自本地 wheelhouse。

| 检查 | 本次独立结果 |
| --- | --- |
| `scripts/probe_orchestration_release_review.py` | **5 passed / 0 failed，exit 0** |
| 完整默认套件 | **1284 passed / 1 skipped，69.53 秒** |
| [RF 生命周期补充探针](../scripts/probe_orchestration_rf_acceptance.py) | **0 passed / 3 failed，exit 1** |
| RF-A1 故障方式 | 真实子进程在持久提交后 `os._exit(19)`，新执行者接管 |
| RF-A2/RF-A3 | 真实 Harness/Runner/SQLite/Gateway，脚本 provider，确定性顺序 |

原五项关闭的证据包括：混合输入不丢失、指令漂移拒绝、缺结果零派发、最后一轮恢复不刷新预算，以及完整 spawn 载荷冲突拒绝。

本次没有升级版本或构建正式安装包；仍是 `0.1.0a2`。未重复运行完整发行门禁、远程 CI、其他 Python/操作系统或真实模型。实施方此前门禁记录继续保留，但不能代替下面三个窗口的独立验证。

## 2. RF-A1 / P1：结果事件提交后退出，原 Run 无法继续

位置：

- [结果事件进入准备相位](../src/yanxu_harness/core/transitions.py#L846)
- [Runner 提交后执行动作](../src/yanxu_harness/core/runner.py#L415)
- [孤儿恢复分类](../src/yanxu_harness/runtime/default_harness.py#L1092)

对应计划 RF3 §7.2：“提交后、请求前死亡，恢复快照证明结果已应用，不再次追加消息”；RF5 要求应用事务前/后的真实进程证据。

真实进程复现：

1. 父 Run 合法 suspended，child 已真实完成且通知已提交。
2. 父 resume 接受 `ExternalResultResolved`；状态 sink 提交 `AWAITING_PREPARE`。
3. 确认结果事件已写入 consumed、结果正文已保存于快照 pending，尚未执行下一准备动作/模型请求；进程立即退出 19。
4. 仅等待死亡执行者明确的一秒租约到期，新执行者重开同一个 SQLite 库并恢复原 Run。

实际结果：

```text
committed_phase=awaiting_prepare
recovered_status=suspended
interaction=reconciliation
recovery_model_calls=0
```

再次 resume 报“suspended but carries no recorded tool batch”。`_recover_orphan` 只恢复完成判决或工具批次，将新分支提交的准备状态落入泛化 reconciliation；该状态又没有 batch，无法通过现有恢复入口续跑。

这里的问题不是多执行了工具，而是**已经有持久结果与明确待办相位，却进入无法继续的核对死路**。现有真实进程测试覆盖的是父应用结果之前退出，未覆盖应用后的这个窗口。

修复边界：

- 为“外部结果已提交、下一准备/请求尚未完成”的状态定义可恢复入口和待办动作契约。
- 保留原 transcript/pending、consumed、预算、结果身份和部署基线；先按原守卫检查，再继续同一 Runner。
- 不能再次发送同一结果事件并用当前时间戳重算不同内容，也不能清空 consumed 或改成新 Run 来绕过去重。
- 不要无条件重放任意有外部副作用的 prepare hook；需要证明准备动作可重做，或为其保存足够的执行/额度证据。
- 增加该真实进程窗口，以及准备完成/请求派发前的相邻窗口；断言结果只纳入一次，原 Run 可达到最终结局。

## 3. RF-A2 / P2：两个子结果都已提交，父只消费第一个便完成

位置：

- [终结屏障仅选择活动 child](../src/yanxu_harness/tasks/children.py#L762)
- [唤醒等待所有活动 child 结束](../src/yanxu_harness/tasks/children.py#L1062)
- [恢复只构造指定 child 的一个结果事件](../src/yanxu_harness/runtime/default_harness.py#L740)

对应计划 RF3 的多次 child 等待/恢复、稳定结果应用身份，以及真实父子链路验收。

复现：

1. 同一个父任务登记两个所属 child。
2. 父进入第一个 child 的持久等待，释放执行槽。
3. 两个 child 均真实执行完成，分别提交 `FIRST_RESULT` 和 `SECOND_RESULT`，再投递通知。
4. coordinator 可查询两个结果；父原 Run 恢复。

实际结果：

```text
run_status=completed
committed_child_results=2
first_in_parent=True
second_in_parent=False
```

父只追加等待引用指向的第一个 child。之后 `finish_interaction` 发现没有 active child，便允许结束；没有区分“已经终态”与“结果已纳入父 Run”。第二条通知存在于控制面，却没有成为父模型可见的数据。

本复现针对自动 join/恢复路径；没有声称宿主完全无法通过 coordinator 单独读取第二个结果。

修复边界：

- 持久区分 child 已终态、结果已就绪、父已应用该结果。
- 自动 join 不能仅以 `active_children == 0` 推断结果处理完毕。
- 可将所属待应用结果作为一个有界集合提交，或按稳定顺序逐个恢复；两种实现都必须保留每条结果的去重身份与原预算。
- 已完成但尚未应用的 child 同样进入就绪集合；业务验收失败/子失败结果不可静默省略。
- 显式等待一个 child 与自动等待全部所属 child 的语义要分清。
- 验证两个 child 反序完成、完成发生在父建立等待前、通知重投和结果纳入过程中重启，保证每条所需结果只应用一次。

## 4. RF-A3 / P2：usage_limited 被 finish_barrier 改成等待

位置：[正常终态屏障的状态范围](../src/yanxu_harness/runtime/default_harness.py#L664)。

原计划 §2、RF3 §7.1 明确：正常完成候选可以 join；failed/cancelled/usage_limited 不得被 child 屏障无条件改回等待。实施报告 §2.2 改为允许 usage_limited 等待 child，但没有交付相应有界等待与停止结算策略，不能据此视为原验收条件已满足。

复现：

1. 父请求 `max_turns=1`，这一次真实模型请求返回工具调用，Gateway 正常结算。
2. 该轮结束后需要继续请求，但已耗尽轮次，Core 产生 usage_limited。
3. 所属 child 仍 queued；执行 finish_barrier。

实际结果：

```text
run_status=suspended
stop_code=suspended
error_code=max_turns
child_status=queued
interaction=child_result
```

超限结局被覆盖，父继续依赖外部 child 完成通知。原预算停止无法独立收敛；child 若还在审批/未知效果等等待中，父也可能长期保持等待。

修复边界：

- 恢复计划规定的超限停止语义，不将 usage_limited 加入普通正常完成 join。
- 父按 usage_limited 结算；所属子任务按明确策略记录取消意图与预算结算，未知副作用/费用继续保留。
- 不通过强制执行 child 消耗额外预算来掩盖父超限，也不伪造外部工作已经停止。
- 分别验证 max_turns、绝对 deadline 与其他 usage stop；保留正常 completed 等待子任务的行为。

## 5. 可运行复现与交付要求

用已安装当前源码和测试 extras 的解释器运行：

```bash
python scripts/probe_orchestration_release_review.py
python scripts/probe_orchestration_rf_acceptance.py
```

在本核对点，第一条 exit 0，第二条 exit 1。补充脚本复用既有真实部署 helper；RF-A1 的退出由提交钩子精确定位，短暂等待只用于死亡执行者租约过期，不用于碰撞故障窗口。

修复时将三场景纳入默认 pytest，不用修改断言、跳过测试或重新创建任务规避。完成后：

1. 复跑原五探针及三项补充探针。
2. 跑真实进程、Core 状态/取消、预算/父子及原恢复回归。
3. 更新实际恢复相位、结果应用事实、停止策略与兼容说明，保留本报告历史。
4. 刷新 SNAPSHOT，跑完整套件及离线发行门禁，再交由 Codex 独立复验。
5. 通过后由 Codex 升级 `0.2.0a1` 并正式打包，当前不上传发行。

本次只新增独立验收报告、补充探针和生成清单，并向实施方报告追加复验链接；未修改业务实现。

