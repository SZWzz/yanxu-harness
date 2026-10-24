# Plan 与任务 DAG 实施进度

状态：进行中。范围为方案 `docs/plan-dag-harness-implementation-plan.zh-CN.md` 的 P0.0–P0.6；不包含 P1/P2。此文记录实际观测结果，不把计划目标当作通过证据。

## 开工基线

| 项目 | 记录 |
| --- | --- |
| 基线提交 | `8b92d50b2664431ee62d1f2b94a24254484d33da` |
| 工作分支 | `main`（开工时） |
| Python / OS | Python 3.12.13；Darwin kernel 27.0.0 arm64 |
| 包版本 / 最低版本 | `0.4.0a1`；`requires-python >=3.10` |
| 可选依赖 | `.venv` 中 pytest、pytest-asyncio、pydantic、yaml 已安装；MCP SDK 与 jsonschema 未安装 |
| 开工前工作区 | 用户已有 `M docs/README.md` 与未跟踪的方案文档 `docs/plan-dag-harness-implementation-plan.zh-CN.md`；保留，不纳入本轮改动或提交 |
| 相关测试基线 | `.venv/bin/python -m pytest -q tests/test_tasks_contract.py tests/test_tasks_scheduler.py tests/test_tasks_inbox.py tests/test_tasks_acceptance_matrix.py` → **86 passed in 1.92s** |

仓库根目录原本没有 `AGENTS.md`/`RTK.md`。本轮按用户要求新增：`AGENTS.md` 提供 CodeGraph 规则，`RTK.md` 记录项目边界与兼容要求。

## 阶段状态

| 阶段 | 状态 | 结果 / 验证 |
| --- | --- | --- |
| P0.0 契约冻结 | 已完成 | 冻结数据模型、规范摘要、审批绑定、状态迁移、拒绝码、DAG/额度、原子准入、验收及静止重规划边界，见 `docs/plans-contract.zh-CN.md`。实现阶段发现的偏差必须回写本契约后再继续。 |
| P0.1 纯模型与 DAG | 已完成 | 新增不可变版本化定义、严格 JSON codec、内容/envelope SHA-256 摘要、图上限/环校验、拓扑排序、依赖阻塞原因与纯状态迁移。命令：`.venv/bin/python -m pytest -q tests/test_plans_contract.py` → **14 passed**。 |
| P0.2 控制存储 | 已完成 | 新增可选 `OrchestrationStore`，Memory/SQLite 在各自临界区/同一 SQLite 控制事务内实现幂等 create、CAS 控制、审批/撤销、暂停/恢复、静止重规划发布、历史事件和 outbox。通过 `enable_orchestration=True` 显式迁移；默认旧库不建 Plan 表；只读/未知 schema 明确拒绝。Plan 合同与存储测试 **41 passed**；计划存储与全部 `tests/test_tasks*.py`、Task 诊断/示例合并回归 **222 passed in 6.04s**。提交：`a98696a`。 |
| P0.3 调度接线 | 已完成 | 新增 namespace-scoped、有续扫游标的 `read_ready_steps`；候选扫描有界且 blocked step 不占结果额度。`claim_plan_step` 在 Memory 临界区/SQLite 同一控制事务内重验 CAS、批准、deadline、依赖、执行次数、并发、Task 输入 backlog 和 binding，再写 binding、Task、INITIAL 输入（含 Plan/Step 与固定资源引用）、claim/fence、Plan 事件/outbox；Task ID/Run ID 按结构化身份的版本化 SHA-256 派生。普通 `read_schedulable` 隐藏绑定 Task，普通 `claim_task` 明确拒绝；`LocalWorker` 仅显式启用且指定 namespace 时把 Plan 候选并入现有优先级 aging、并发限制和 namespace 轮转，经同一 Harness 驱动。已覆盖双 Store、同请求幂等、事件写失败整笔回滚、pause 后既有 binding 恢复、审批到期/deadline 与 backlog 拒绝、namespace 隔离、同 Worker 防重复 drive。测试：`.venv/bin/python -m pytest -q tests/test_plans_scheduler.py` → **20 passed**；计划/Task/诊断/示例/import fence/public API 合并回归 **254 passed in 7.10s**。提交：`961c209`。 |
| P0.4 验收与完成 | 已完成 | 新增有界 `EvidenceSnapshot` 与精确 ID/version 注册的只读 verifier；evidence、Task、Run 在 verifier 执行后再次读取并核对版本/摘要，漂移为 `source_changed` inconclusive。未结算操作、缺失/超时证据、缺 verifier、超时/超限/普通 verifier 错误均不解锁；拒绝或 inconclusive 与 AcceptanceRecord、Plan 版本、事件/outbox 原子提交，并将 executing Plan 暂停。`record_acceptance` 校验 revision、binding、Task、criterion、verifier/摘要，accepted/rejected 不可覆盖，inconclusive 可重判。Plan finalize 重核当前 binding/证据/unknown、全部步骤与 Plan criteria，完成通知同事务写入并支持重投。显式 `retry_step` 仅接受最新已拒绝、Task completed 且每项 criterion 已终判的 binding；需有效批准/恢复与剩余额度，生成新 Task/Run/binding 并保存 `retries_binding_id`，同时覆盖恢复和请求重投。Plan schema 升至 v2；SQLite 可写 v1 原子迁移，read-only v1 不改库并明确拒绝。Memory 与 SQLite 聚焦合同及调度回归见下方记录。 |
| P0.5 暂停/重规划/恢复 | 未开始 | 尚无新控制操作或跨进程故障验证。 |
| P0.6 诊断/示例/交付 | 未开始 | 尚无 PlanInspector、示例、完整 PA 映射或树外安装验证。 |

## 变更与偏离

- 首轮保守冻结 Plan 的动态 token/cost/dispatch 共享预算为不支持；只保证文档列出的结构、次数、deadline、并发与现有 Task 预算。没有虚构不存在的 root Task。
- P0.0 契约单独提交为 `95a8e93`；P0.1 纯模型与 DAG 单独提交为 `fcf5756`；P0.2 控制存储提交为 `a98696a`；P0.3 调度接线提交为 `961c209`。根目录指引提交为 `968ae44`。用户原有方案文档未纳入提交；原有 `docs/README.md` 计划索引行保留并更新为“实施中”。

## 阶段验证记录

P0.2 合并回归命令：`.venv/bin/python -m pytest -q tests/test_plans_contract.py tests/test_plans_store.py tests/test_tasks*.py tests/test_diagnostics_tasks.py tests/test_examples_tasks.py` → **222 passed in 6.04s**。此前 API/import 边界检查也通过：包含 `tests/test_import_fence.py`、`tests/test_public_api.py` 与 `tests/test_public_snapshot_whitelist.py` 的命令 **188 passed in 2.18s**。阶段验证命令、结果、失败和未测平台追加在对应阶段完成时。首轮 P0 完成前不得将总体状态改为完成，也不得删除失败/未测记录。

P0.3 focused command: `.venv/bin/python -m pytest -q tests/test_plans_scheduler.py` → **20 passed in 0.28s**. Final Plan/Task/diagnostics/examples/import/public API regression: `.venv/bin/python -m pytest -q tests/test_plans_contract.py tests/test_plans_store.py tests/test_plans_scheduler.py tests/test_tasks*.py tests/test_diagnostics_tasks.py tests/test_examples_tasks.py tests/test_import_fence.py tests/test_public_api.py tests/test_public_snapshot_whitelist.py` → **254 passed in 7.10s**. A new test file first ran red with 4 `AttributeError` failures because the API was not implemented yet; after adding namespace scoping, one refactor run exposed 10 `UnboundLocalError` failures from a shadowed closure parameter. A later memory rollback run exposed 3 `TypeError` failures because `deepcopy` cannot copy `FrozenJSONDict`; the snapshot now copies only mutable containers while reusing immutable records. All were fixed before final green runs. No platform other than the current Darwin arm64/Python 3.12 environment was tested.

P0.4 focused regression: `.venv/bin/python -m pytest -q tests/test_plans_acceptance.py tests/test_plans_scheduler.py tests/test_plans_store.py tests/test_plans_contract.py` → **90 passed in 0.72s**. Public surface and sdist whitelist: `.venv/bin/python -m pytest -q tests/test_public_api.py tests/test_public_snapshot_whitelist.py` → **6 passed in 0.15s**. The retry and acceptance tests are parameterized across Memory and SQLite; v1→v2 migration and read-only refusal are exercised against SQLite. During test-first work, missing retry API, retry recovery identity, paused-plan behavior, source drift detection and a missing timeout fixture field each produced a failing focused run and were repaired before the passing runs. These are development-checkout results, not a wheel/sdist or complete-suite claim. Python 3.10/3.11 and non-Darwin platforms remain untested in this phase.
