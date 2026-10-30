# Harness 下一阶段 R2 独立复核

日期：2026-10-29（Asia/Shanghai）
复核对象：`codex/plan-dag-harness` 当前工作树中的 P0.6 Plan/DAG 候选
状态：**R2 通过**。本报告是独立实现审查，不构成安全认证。

复核者在用户授权后以只读方式检查源码并运行测试；没有修改仓库文件。审查覆盖 PlanDefinition 摘要、principal 与审批撤销、SQLite claim/pause/replan 竞争、验收证据漂移、跨进程 Gateway unknown、跨 revision 执行限额、普通 Task 隔离及只读 PlanInspector/CLI，并实际穿过 SQLite、LocalWorker、DefaultHarness 和 ToolGateway。

## 结论

当前 P0.6 契约范围内没有未披露的阻断问题。独立复核验证了实施者在 R2 本地探针中修复的两项缺陷：PlanInspector 曾忽略跨 revision 执行次数和全局活动绑定数；同 holder 重放活跃 claim 曾绕过当前批准/撤销检查。当前修复分别有真实 SQLite 诊断回归和 Memory/SQLite claim 回归覆盖。

R3 可以通过，前提是保留以下已声明边界：审批撤销或暂停关闭后续准入，但不追溯终止已提交 claim 的工作；此阶段不承诺每次模型/工具派发都重新读取授权。后续 E1/E2 需要在真实 dispatch 边界实现并验证执行绑定/撤权守卫，若产品契约要求撤销后不再派发，则该守卫是发布门禁。

## R2 本地发现及修复记录

| ID | 严重度 / 归属 | 复现与修复 | 复验 |
| --- | --- | --- | --- |
| R2-L1 | P2；`diagnostics/plans.py` | 真实 SQLite 下有一个活动 binding、`max_concurrency=1` 且另一步骤无依赖时，`read_ready_steps` 返回空，但 Inspector 原报 `ready`。修复为读取所有 revision 的 Plan bindings，当前步骤细节仍按当前 revision 投影，并按 Plan 全局活动数与累计执行数报告阻塞。 | 新用例修复前 `1 failed`、修复后 `1 passed`；独立跨 revision 探针报告 `blocked/step_execution_limit`。 |
| R2-L2 | P1；`plans/storage.py` | 已 claim 的同 holder/request 在批准撤销后重放，旧分支在检查当前批准前返回 live claim。新增 Memory/SQLite 参数化回归；修复为幂等重放前也通过当前 Plan claim gates。 | 修复前 Memory 与 SQLite **2 failed**；修复后撤销/claim 重放和原有幂等回归合计 **6 passed**，Plan 聚焦套件 **169 passed**。 |

两项修复保存在当前工作树；提交归属将在 R3 候选收口时遵守 R0 对开工前用户文件的逐文件归属记录，不把既有未跟踪 P0.6 文件整体加入提交。

## 检查结果

| 检查 | 结论 | 实际观察与边界 |
| --- | --- | --- |
| 定义摘要与规范化 | 通过 | `plans/codec.py` 规范排序步骤、依赖和验收条件，排除 `created_at`；JSON 冻结拒绝不支持类型与浮点。定义顺序/时间变化测试通过。 |
| Trusted principal、审批与撤销 | 通过，依赖宿主身份信任 | 存储把调用方提供的 principal 记录为批准人，并把批准绑定到定义和 envelope digest；撤销暂停 Plan，恢复要求有效新批准。库本身不认证 principal；宿主必须从已认证上下文提供，不能接受模型或普通用户载荷自报。已 claim 工作撤销后的行为按 P0.6 在途边界处理。 |
| SQLite claim/pause/replan 竞争 | 通过 | claim 的 revision/digest/control-version 检查与准入位于事务内。双 SQLite 连接竞争探针验证只有事务先胜出的一方改变状态。 |
| Acceptance evidence drift | 通过，有外部 TOCTOU 窗口 | 验证前后重读 Task、Run 与 evidence；来源快照变化产生 `INCONCLUSIVE/source_changed`。最后一次复读与记录 acceptance 之间仍可能有外部事实变化；消除该窗口需要宿主提供事务性读取或版本 fencing。 |
| 跨进程 Gateway unknown | 通过 | PA22 子进程测试让 handler 写入并 fsync 副作用后、receipt 持久化前退出。重启后状态保持 unknown，handler 未重跑，验收/下游步骤保持阻塞。 |
| 跨 revision 执行次数 | 通过 | SQLite 探针在 revision 1 留下 binding、revision 2 设限后，调度候选为空；PlanInspector 为 `blocked/step_execution_limit`。全局 concurrency 用尽也报告 blocked。 |
| 普通 Task 绕过 Plan | 通过 | Plan binding Task 不进入普通 schedulable 队列；普通 `claim_task` 被拒绝。Worker 的 Plan 路径要求显式启用 orchestration 和 namespace。 |
| PlanInspector 与 CLI 只读 | 通过 | Inspector 要求只读 store，报告标明多表读取非原子；CLI 不创建缺失数据库。对比调用前后的持久记录确认无写入。诊断状态覆盖全 revision 执行计数和 Plan concurrency。 |

## 复核者运行的测试

聚焦审查集：

```sh
.venv/bin/python -m pytest -q \
  tests/test_plans_contract.py tests/test_plans_store.py \
  tests/test_plans_scheduler.py tests/test_plans_acceptance.py \
  tests/test_plans_replanning.py tests/test_diagnostics_plans.py
```

结果：**143 passed**。

竞争、绕过、跨 revision 额度与 PA22 Gateway 故障恢复：

```sh
.venv/bin/python -m pytest -q \
  tests/test_plans_scheduler.py::test_step_claim_races_with_pause_or_replan_at_one_sqlite_commit_boundary \
  tests/test_plans_scheduler.py::test_ready_step_is_claimed_atomically_and_hidden_from_ordinary_worker \
  tests/test_plans_scheduler.py::test_only_enabled_worker_drives_ready_plan_steps_through_harness \
  tests/test_plans_replanning.py::test_step_execution_limit_is_shared_across_published_revisions \
  tests/test_plans_replanning.py::test_pa22_real_gateway_effect_remains_unknown_and_is_not_run_again
```

结果：**9 passed**。另用临时 SQLite 数据库验证 revision 2 诊断，输出：

```text
{'current_revision': 2, 'old_revision_bindings': 1,
 'current_ready_candidates': 0, 'diagnostic_status': 'blocked',
 'blocking_reason': 'step_execution_limit'}
```

实施者随后将新回归纳入更广的本地套件：Plan/存储/Worker/接受/重规划/诊断/Gateway **169 passed**；Plan 示例、DefaultHarness 与恢复路径 **44 passed**。它们是当前工作树额外验证，不替代上述独立运行记录。

## 预算与 Gateway 补充复审（2026-10-29）

在用户要求暂缓“开发完成、验收通过”后，独立复核进一步检查了 B2 预算派发、E2 执行绑定和 `WorkResult` 公开投影。用户报告的四项原始缺陷均已复现并修复：handler 已进入但 neutral receipt 被退额度；策略快照与事务预留之间允许新增 hard 限额绕过；异步预算预留期间撤权后 handler 仍可执行；`WorkResult.to_record()` 错归到 guard 类。

后续只读探针又覆盖了取消与恢复窗口：

- intent 准入前已取消不得尝试 settlement；准入后、handler 前取消要留下 `handler_entered=False` receipt 并清理 intent。
- 等待 reservation 或 post-reservation binding check 期间取消，等待持久结算完成后传播取消。
- `reserve_tool` 若先提交 reservation、再因取消或异常在返回 handle 前退出，Gateway 按稳定 dispatch identity reconcile；找不到匹配行时不创建预留。
- handler receipt 已持久化后，budget settle/reconcile 不因调用方取消而中断；handler 前拒绝的额度释放同样受保护。
- 未产生 reservation 的 pre-reservation binding refusal 可以在 dispatch quota 为零时 replay，不会尝试新预留。

截至最后一次复查，独立审查者确认上述问题与原始四项均已关闭，没有剩余具体发现。`handler_entered` 持久阶段证据、完整策略 digest 集事务校验、预算后执行绑定复查和 `WorkResult.to_record()` 归属均经代码审查与回归覆盖。

聚焦验证由实施者在当前源码树运行：

```sh
pytest -q \
  tests/test_typed_budget_dispatch.py tests/test_typed_budget_store.py \
  tests/test_tools_gateway.py tests/test_tools_ledger_recovery.py \
  tests/test_runtime_recovery_compat.py tests/test_plans_scheduler.py \
  tests/test_public_api.py
```

结果：**182 passed**。独立复核者分别运行预算/Gateway/Plan Scheduler 子集（117 passed），并对最后的 no-handle reconcile 和 shield 补丁只读复查；未修改仓库。最新候选完整离线 CI 结果见 `docs/verification.md`。本机仅 macOS CPython 3.12.13；Linux/Windows 以及修复后源码的 Python 3.10/3.11 矩阵仍未验证。
