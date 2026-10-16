# 持久输入与任务编排：打包前独立复验

日期：2026-10-16。核对提交：`42a4ee8`，开始检查时工作树干净。对照[统一计划](durable-input-orchestration-implementation-plan.zh-CN.md)、[上轮独立审查](durable-input-orchestration-progress-review.zh-CN.md)和[实施方交付报告](durable-input-orchestration-delivery-report.zh-CN.md)。

后续实施任务：[发行阻断修复实施计划（DeepSeek 接手）](durable-input-orchestration-release-fix-plan.zh-CN.md)，覆盖本报告五项缺陷、恢复状态、兼容及 RF0–RF6 验收。

**结论：尚未通过打包前验收。** 上轮多项具体复现已修复，但输入消费水位和新 child_result 恢复分支仍存在三个 P1、两个 P2。保留当前 `0.1.0a2`；本次没有升级版本、构建正式发行包或上传发布。修复复验通过后，按既定安排升级为 `0.2.0a1` 并执行发行门禁。

## 1. 本次独立证据

- 使用 Python 3.12.13/macOS，在临时虚拟环境从本 checkout **非 editable 安装** `.[dev,yaml,pydantic]`；依赖来自本地 wheelhouse。
- 完整默认套件：**1253 passed / 1 skipped，68.57 秒**。
- 上轮额外探针重跑：namespace cap 正确阻止第二个同 namespace 任务；Run completed/Task queued 恢复正确结算 completed；单独 follow-up 形成两轮，首轮未提前吞入。
- 上轮长历史真实 Run 探针：201 条输入记录全部 applied，进行了 201 次模型调用，修复了原先固定第一页导致的漏读。
- 当前真实父子回归通过；父 Run 在 child 活动时为 suspended，随后可以完成正常恢复。
- 新增的[独立发行探针](../scripts/probe_orchestration_release_review.py)：**0 passed / 5 failed**。使用既有真实 DefaultHarness、SQLite、Gateway 和 Worker 测试装配，只有 provider 是脚本；不修改业务实现。
- 本次未重复执行完整离线 CI、wheel/sdist 发行安装或远程 CI，因为独立行为验收已经阻断。实施方历史发行记录不等于这些遗漏已通过。

独立探针单独运行，默认 pytest 尚不覆盖这五个场景。运行方法：

```bash
# 使用已安装当前 checkout 及 dev/yaml/pydantic extras 的解释器
python scripts/probe_orchestration_release_review.py
```

当前预期是退出码 1，逐项打印 FAIL。修复后应全部 PASS，并将相应行为断言纳入常规回归。脚本从 checkout 导入现有测试装配 helper，业务包来自所选解释器的安装环境。

## 2. DIO2-I1 / P1：MAX(applied) 跳过更早的未应用 follow-up

位置：[持久输入读取](../src/yanxu_harness/tasks/inputs.py#L73)、[SQLite 水位](../src/yanxu_harness/tasks/inbox.py#L1288)、[终结检查](../src/yanxu_harness/runtime/default_harness.py#L605)。

修复第一页漏读时，改为从 `MAX(sequence WHERE state=applied)` 向后读取。这需要“所有较早序号都已处理”的前提；steering/follow_up 在不同站点消费，实际允许较新的 steering 先应用，因此该前提不成立。

最小复现：

1. 接纳 initial，序号 0。
2. 接纳 follow_up，序号 1。
3. 接纳 steering，序号 2。
4. 启动真实 Harness；初始 steering 站点正确应用序号 2。
5. 水位变为 2；后续读取和终结守卫跳过仍 pending 的序号 1。

实际结果：

```text
run=completed, model_calls=1
initial=applied, follow-first=pending, steer-second=applied
```

这是 accepted 输入被遗留在已完成 Run，不能依靠修正报告或重开终态 Run 处理。

修复要求：

- 消费游标不能把“最大已应用序号”当作“连续已处理前缀”。
- 优先提供按 pending/claimed 与输入类型过滤的有界查询；或使用能保留空洞的游标/分页方案。
- 终结检查必须看见较早仍未应用的对话输入，不能仅判断其是否大于最大水位。
- 同步 SQLite 与 MemoryControlStore；覆盖两种类型的多种接纳顺序、多个 follow-up、长历史、控制项夹杂和恢复。
- 保留既有“follow-up 不提前进入首轮”与“超过 200 条历史仍能读取”的修复，不能以混合消费绕过问题。

## 3. DIO2-R1 / P1：child_result 恢复绕过配置漂移检查

位置：[resume 分支提前返回](../src/yanxu_harness/runtime/default_harness.py#L821)、[子结果恢复入口](../src/yanxu_harness/runtime/default_harness.py#L680)。

新无 batch 分支在原先 `_assert_arrangement` 之前返回。它直接以当前部署配置调用 `_drive`，未核对已提交快照的 arrangement。

复现：

1. 使用正确接线 finish_barrier 的真实父子装配，让父 Run 挂起。
2. 子任务真实完成，写 complete_child 并投递通知。
3. 新建父 Harness，将 system_prompt 改为另一组系统指令，确认 fingerprints 已改变。
4. 调用原 Run 的 resume。

实际结果：未拒绝漂移，父模型调用从 1 增到 2，Run completed。

此探针证明系统指令漂移未被拒绝；没有据此宣称其他所有配置都已实际复现。

修复要求：

- child_result 恢复同样从已提交执行快照读取原配置基线，在任何模型/工具派发前校验。
- 检查 instructions/config、工具、策略、exposure 等既有维度；缺基线或未知快照不得由当前配置回填后放行。
- 拒绝时维持可解释、可再次核对的原持久事实，不先覆盖成新部署 arrangement。
- 增加每个维度的真实父 Run 恢复测试，断言新部署 provider/工具调用次数为零。

## 4. DIO2-B1 / P1：child_result 恢复刷新轮次预算并覆盖累计计数

位置：[重新调用 _drive](../src/yanxu_harness/runtime/default_harness.py#L714)、[fresh loop 配置](../src/yanxu_harness/runtime/default_harness.py#L1258)。

恢复分支使用默认 `RunRequest(prompt="")`；默认 max_turns 为 None，且 fresh loop 的 initial_turns_used 为 0。原快照的 max_turns 和累计 turns_used 没有传入。

真实链路探针：

1. 父请求显式设置 `max_turns=1`，第一次模型返回后在 child 屏障挂起。
2. 已提交快照为 `max_turns=1, turns_used=1`。
3. 子任务完成，父 resume。
4. 父再次调用模型并返回 completed；最终 turns_used 仍是 1。

```text
before_status=suspended, declared_max_turns=1, before_turns=1
after_status=completed, actual_parent_model_calls=2, after_turns=1
```

实际执行超出已批准轮次，持久计数还低估了支出。重新执行“同一个逻辑 Run”不能重置其预算。

修复要求：

- 从原快照继承 max_turns、累计 turns_used 与其他相关预算事实，用同一 Runner 的明确恢复入口继续。
- 预算耗尽时不发下一次请求，返回明确 usage_limited，并如实处理所属子任务。
- 核对 usage/测量事实的累计和已提交上下文预算，不覆盖此前实际消耗。
- 同时核查任务剩余期限、配置 runtime 上限、共享 root 额度等在该新分支的接线；本探针仅对轮次重置给出实证，不把其他维度写成已复现。
- 覆盖多次 child 等待/恢复、父最后一个可用轮次和预算耗尽后的重复 resume。

## 5. DIO2-R2 / P2：子结果缺失时仍会调用父模型

位置：[子结果文本降级](../src/yanxu_harness/runtime/default_harness.py#L736)、[无条件继续](../src/yanxu_harness/runtime/default_harness.py#L698)。

子结果不存在时，`_child_result_text` 返回“No committed result”普通文本；恢复入口仍把这段文字加入用户消息并调用模型。

复现：父已 suspended；child 仍 queued，`coordinator.child_result("child")` 为 None。直接调用父 Harness 的公开 resume，模型调用从 1 增到 2；之后 finish_barrier 才又将父挂起。

这不是由提示词伪造结果：调用公开 resume 本身即可触发。worker 的正常唤醒路径没有覆盖错误重投或过早 resume 的防御。

修复要求：

- 先重新核对 parent/child 关系、namespace/session、child 已提交终态和对应结果通知。
- 没有结果时保持原等待，或返回结构化可重试拒绝；模型/工具调用和预算派发都应为零。
- 缺结果不能用普通字符串伪装成可继续的结果。
- 子结果的业务验收不通过应作为明确结果数据处理；与“结果事实尚不存在”分开。
- 增加过早 resume、重复唤醒、伪造 resolution、子结果缺失/不一致及迟到结果测试。

## 6. DIO2-C1 / P2：spawn 冲突只覆盖授权，仍遗漏任务载荷和身份

位置：[spawn 去重分支](../src/yanxu_harness/tasks/children.py#L453)、[授权摘要字段](../src/yanxu_harness/tasks/children.py#L222)。

本轮增加 authorization_digest 比较，修复了 tools/profile 等授权漂移。但该摘要不包含 initial_input、child_run_id、deadline_at，重复分支又在检查这些值之前返回。

独立探针分别只改变以下一个字段：

- initial_input：改为完全不同的任务。
- child_run_id：改为另一个 Run ID。
- deadline_at：从无限期限改为绝对截止时刻。

三种请求都返回 `created=False`，沿用旧子任务，未拒绝 config_conflict。请求中的新 Run/期限与已保存事实不一致。

修复要求：

- 分开定义有效授权摘要与完整 spawn 语义摘要；前者不能代替后者。
- 完整身份至少覆盖父/child/Run、初始输入语义、有效授权、工作区、预算、期限、结果协议，以及其余影响行为的字段。
- 规范化后在控制事务内校验；同身份同语义重投返回原事实，不同语义明确 config_conflict。
- 不将纯观测时间等非语义字段不加区分地纳入摘要，也不破坏 Gateway 回执缺失后的幂等收敛。
- 对正文、Run ID、期限分别测试，不只改 tools 后推论所有配置都受到保护。

## 7. 上轮修复状态与下一步

| 上轮问题 | 本次复验 |
| --- | --- |
| 父 Run 提前结束 | 正常接线路径已在 Run 终态前挂起；新恢复分支仍需补齐上述守卫 |
| 终态 Run / 未结算 Task 恢复失败 | 原复现已通过 |
| 超过 200 条历史漏读 | 原复现已通过；MAX 水位引入混合输入空洞问题 |
| namespace 并发上限 | 原真实在途复现已通过 |
| spawn 配置冲突 | 授权字段已有校验；完整语义仍遗漏 |
| follow-up 首轮提前消费 | 单独 follow-up 原复现通过；混合输入仍会遗失 |
| 真实 Runner 父子证据不足 | 已增加正常路径的真实 Harness 回归；新增分支的异常/预算/漂移证据待补齐 |

建议实施顺序：先将本脚本五个场景加入正式回归；修输入空洞、恢复基线与轮次连续性，再修结果就绪检查及完整 spawn 身份。完成后重跑独立探针、默认套件和发行门禁，保留本报告为失败历史。

本次仅增加本报告、可运行探针及生成清单，不修改业务代码、不把现有通过数写成新增场景已通过。后续仍由我在这些问题复验通过后处理版本、wheel/sdist、源码树外安装及打包报告。

