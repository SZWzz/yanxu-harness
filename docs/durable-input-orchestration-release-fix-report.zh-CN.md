# 持久输入与子任务恢复：发行阻断修复交付报告（RF0–RF6）

日期：2026-10-16。核对基线：`42a4ee8`（默认套件 **1253 passed / 1 skipped**，独立探针 **0 passed / 5 failed**）。
任务书：[发行阻断修复实施计划](durable-input-orchestration-release-fix-plan.zh-CN.md)。失败历史保留在
[打包前独立复验](durable-input-orchestration-release-review.zh-CN.md)，本文件只追加新核对点，不删旧事实。

状态：**实施方已交付，待 Codex 独立复验随后升级 `0.2.0a1` 并正式打包**。本轮保留版本 `0.1.0a2`，门禁构建仅为验证产物。

## 1. 五项缺陷：修复前红 / 修复后绿

独立探针 `scripts/probe_orchestration_release_review.py` 在 `42a4ee8` 上 **0 passed / 5 failed**（exit 1），
现在 **5 passed / 0 failed**（exit 0）。同一真实链路已作为默认收集测试固化在
`tests/test_tasks_release_regressions.py`，探针保留为交叉检查入口。

| 缺陷 | 级 | 修复前实际输出 | 修复后 | 提交 |
| --- | --- | --- | --- | --- |
| DIO2-I1 | P1 | `run=completed, model_calls=1, inputs={initial: applied, follow-first: pending, steer-second: applied}` | follow-first/steer-second 均 applied，follow-up 在自己站点消费 | `fe36bc3`（RF1） |
| DIO2-R1 | P1 | `drift accepted: outcome=completed, model_calls=1->2` | `ResumeRefused(arrangement_drift)`，父模型调用不变 | `6dccc13`（RF2） |
| DIO2-R2 | P2 | `child still queued, no committed result; model_calls=1->2` | 结构化拒绝，零派发，不写入占位文本 | `6dccc13`（RF2） |
| DIO2-B1 | P1 | `declared max_turns=1, actual model_calls=2, recorded turns_used=1, outcome=completed` | `usage_limited`，父模型总调用 1，turns_used 仍 1 | `e48d80e`（RF3） |
| DIO2-C1 | P2 | `changed spawn payload accepted as duplicate: [initial_input, child_run_id, deadline_at]` | 逐字段 `CONFIG_CONFLICT`，记录/额度/计数不变 | `81f5109`（RF4） |

阶段提交（各自可独立审阅）：

```text
75480b4 test(tasks): RF0 冻结复现与红测（含保留的审查报告、探针、计划状态与清单）
fe36bc3 fix(tasks):  RF1 按状态读取待处理输入 + 终结屏障
6dccc13 fix(runtime): RF2 恢复共同守卫 + 可证明子结果
e48d80e feat(runtime): RF3 子结果等待为同一 Runner 的合法恢复状态 + 连续预算
81f5109 fix(tasks):  RF4 完整 spawn 语义在登记事务内比较
2645822 feat(diagnostics): RF5 真实进程窗口、旧快照迁移与诊断补齐
```

RF0 红测证据：`PYTHONPATH=src python -m pytest -q tests/test_tasks_release_regressions.py` 在 `75480b4` 上
**5 failed**，逐项失败原因即上表“修复前实际输出”。

## 2. 实际接口、版本与兼容

### 2.1 新增/扩张的公共接口

| 接口 | 位置 | 说明 |
| --- | --- | --- |
| `CoordinationStore.poll_pending_inputs(task_id, *, kind=None, limit=8) -> InputRead` | `tasks/ports.py`、`tasks/inbox.py`（SQLite + 内存参考实现） | 只选 `pending`/可重领 `claimed` 的对话类输入，按 sequence 升序，返回候选与同一读取的 `inbox_revision`，不确认消费 |
| `InputRead` | `tasks/types.py` | `candidates` + `revision` |
| `COORDINATION_READ_METHODS` / `require_input_polling` / `missing_read_methods` | `tasks/ports.py` | 读端口进入能力检测与拒绝；缺读端的存储由 reader 在首次读取前拒绝 |
| `PendingInputsRemain` | `tasks/types.py` | 终结事务拒绝“还有未处理对话输入” |
| `RefusalCode.RUN_TERMINAL` | `tasks/types.py` | Run 已终态、Task 未结算时的接纳拒绝码 |
| `read_child_result(control, *, parent_task_id, child_task_id, spawn_identity, run_store) -> ChildResultReadiness` | `tasks/children.py` | 结构化 `ready`/`not_ready`/`inconsistent` 可信读取 |
| `SpawnSpec` / `SPAWN_SPEC_VERSION` / `SPAWN_SPEC_ALGORITHM` | `tasks/children.py` | 版本化完整 spawn 语义与摘要 |
| `ChildRelation.spawn_digest` / `spawn_spec` | `tasks/types.py`、控制库 `children` 表 | 空摘要 = legacy，不回溯填充 |
| `ExternalWaitRequested` / `ExternalResultResolved` | `core/execution.py`、`core/transitions.py` | FINISHED→SUSPENDED 的合法等待迁移；SUSPENDED 结果应用（事件 id 进 `consumed`） |
| `Runner.resume_external` / `run_agent_loop_resume_external_result` | `core/runner.py` | 从已提交等待快照继续同一 Runner |
| `RecoveryFact` | `diagnostics/tasks.py` | 报告：run 状态、等待对象、结果 ready/说明、累计轮次、基线摘要、快照 schema |

### 2.2 等待/恢复状态

```text
RUNNING ──(正常完成候选 + finish_barrier 命名 child)──► SUSPENDED(child_result)
                                                            │  运行侧 task: WAITING(waiting_for=child:<id>)
                                                            ▼
                       可信读取 read_child_result：
                         ready        ──► ExternalResultResolved ──► 同一 Runner 继续
                         not_ready    ──► 保留原等待 / 结构化可重试拒绝（零派发）
                         inconsistent ──► 结构化拒绝（零派发，原持久事实不改写）
```

- 等待快照经 reducer（`ExternalWaitRequested`）产生，保留 `turns_used`/`max_turns`/`arrangement`；
  旧形态（status=suspended + execution 为 finished 快照）在 arrangement/预算/transcript 可验证时同样经 reducer 迁移，
  否则 `child_result_unmigratable`。
- `failed`/`cancelled` 不进 join；`usage_limited` 可挂起等待子任务，但恢复时预算已尽，直接保持 `usage_limited`（不会无限等待）。

### 2.3 事务与故障窗口

| 窗口 | 处理 | 证据 |
| --- | --- | --- |
| 输入读后、提交前崩溃 | 输入仍 claimable；Run 快照不存在 | `test_tasks_input_flow.py::test_a_crash_before_the_commit_leaves_the_input_claimable` |
| 输入提交后崩溃 | Run + applied 标记都在；重复应用被拒 | `test_tasks_release_recovery.py::test_a_process_dying_after_the_input_commit_does_not_reapply_the_input` |
| 空读后、终结前到达输入 | 终结事务拒绝（`PendingInputsRemain`），回到轮询边界 | `test_tasks_input_flow.py::test_an_input_that_arrives_while_the_run_decides_to_stop_continues_it`、`test_tasks_inbox.py::test_the_terminal_commit_refuses_to_finish_over_a_pending_input` |
| 子终态已提交、父应用前崩溃 | 第二进程从等待快照恢复，结果只应用一次 | `test_tasks_release_recovery.py::test_a_process_dying_before_the_parent_applies_the_child_result` |
| 重复完成通知 / 重复 wake | 事件 id 进 `consumed`，重复为 no-op | `test_tasks_release_regressions.py::test_the_child_result_is_applied_once_across_repeated_resumes`、`test_tasks_children.py::test_duplicate_delivery_admits_the_parent_input_once` |
| 同身份不同语义并发登记 | 事务内比较 spec，冲突不改任何记录 | `test_tasks_inbox.py::test_register_child_compares_the_spawn_spec_in_the_transaction` |

### 2.4 版本与兼容

| 版本/布局 | 变化 | 兼容 |
| --- | --- | --- |
| `ExecutionState` 快照 | **未新增字段、未改相位集合**；新增的两个事件是 reducer 输入，不进入持久格式 | 旧快照原样可读；新增等待用既有 `SUSPENDED` 相位 |
| `ChildRelation.spawn_digest` / `spawn_spec` | 控制库 `children` 表新增两列 | `ALTER TABLE ... ADD COLUMN`（空默认）；旧行摘要为空并标为 legacy，读取只读消费，不提高最低版本 |
| `TASK_SCHEMA_VERSION` | 仍为 1 | 新增字段均有默认值；旧 Task/Input/预算记录照常读取 |
| `CONTROL_SCHEMA_VERSION` | 仍为 1 | 迁移在写入入口事务内补齐缺列；只读入口只校验版本、不迁移 |
| `SpawnSpec` | 新版本 1 + 摘要算法 `canonical-json-sha256-v1` | 未知版本/算法在比较前拒绝 |
| `read_applied_watermark` | 语义澄清为**观测高水位** | 保留 API；不再决定消费资格，文档与 docstring 明示 |

## 3. 验收矩阵（计划 §10）证据位置

| 编号 | 测试位置 | 说明 |
| --- | --- | --- |
| FRI-01 | `test_tasks_release_regressions.py::test_a_follow_up_admitted_before_a_steering_input_is_not_lost` / `::test_a_steering_input_admitted_before_a_follow_up_is_still_applied` / `::test_multiple_follow_ups_each_become_their_own_turn` | 两种顺序、多条 follow-up 各一回合；真实 Harness |
| FRI-02 | `test_tasks_runner_recovery.py::test_a_new_input_is_seen_after_more_than_a_page_of_history`、`test_tasks_inbox.py::test_poll_pending_inputs_selects_by_state_and_kind_without_a_watermark` | 250 条历史 + 有界读取（limit=1） |
| FRI-03 | `test_tasks_input_flow.py::test_applying_an_input_twice_is_refused_rather_than_appended`、`test_tasks_release_recovery.py::test_a_process_dying_after_the_input_commit_does_not_reapply_the_input`、`test_tasks_inbox.py::test_the_terminal_commit_refuses_to_finish_over_a_pending_input` | 真实提交/真实进程 |
| FRR-01 | `test_tasks_release_regressions.py::test_a_child_result_resume_refuses_each_drifted_dimension`（instructions/tools）、`test_runtime_resume.py::test_a_changed_tool_set_refuses_the_resume_with_the_drift_named` | 共享 `_compare_arrangement`；真实 Harness |
| FRR-02 | `::test_a_resume_without_a_committed_child_result_dispatches_nothing`、`::test_a_terminal_child_without_a_committed_notice_is_refused_not_fabricated`、`::test_read_child_result_classifies_queued_failed_and_mismatched` | queued/缺通知/错误归属；零派发 |
| FRR-03 | `::test_read_child_result_classifies_queued_failed_and_mismatched` | `failed`/`accepted=False` 为 ready 可见结果 |
| FRR-04 | `::test_the_child_result_is_applied_once_across_repeated_resumes`、`test_tasks_release_recovery.py::test_a_process_dying_before_the_parent_applies_the_child_result` | 只应用一次；真实进程 |
| FRB-01 | `test_tasks_release_regressions.py::test_resuming_a_parent_that_used_its_last_turn_does_not_refresh_the_budget` | max_turns=1 已用尽 → usage_limited，调用数不变 |
| FRB-02 | `test_tasks_budgets.py`（父子共享额度/未知结算）、`test_tasks_release_regressions.py::test_the_child_result_is_applied_once_across_repeated_resumes` | deadline 取小值接线在 `_invocation_runtime_limit`；本次未单独复现全部 deadline/summary 维度（见 §5） |
| FRC-01 | `::test_the_same_spawn_identity_with_a_changed_payload_is_a_conflict` | 初始输入/子 Run/期限/priority/tools/workspace/schema/预算逐项改变 |
| FRC-02 | `::test_spawn_spec_normalization_ignores_key_and_set_order`、`test_tasks_inbox.py::test_register_child_compares_the_spawn_spec_in_the_transaction` | 键序/工具集合序幂等；事务内比较（两存储） |
| FRV-01 | `::test_a_legacy_finished_wait_snapshot_is_migrated_through_the_reducer`、`::test_a_legacy_relation_without_a_spawn_spec_refuses_a_resubmit` | 可证明才迁移；否则拒绝 |
| FRV-02 | `test_tasks_contract.py::test_a_single_run_backend_is_not_a_coordination_store`、`::test_a_store_without_the_pending_input_read_is_partial_and_the_reader_refuses` | 旧单 Run 可用；缺能力明确拒绝 |
| FRV-03 | `scripts/probe_orchestration_release_review.py`（5 passed / 0 failed，exit 0）+ `tests/test_tasks_release_regressions.py`（17） | 无 xfail/skip 替代 |
| FRV-04 | `scripts/ci_check.py --offline --wheelhouse build-wheelhouse`（6/6）、`scripts/verify_release.py` | 源码树外 wheel/sdist 安装运行 |

真实/模拟故障分别标记：`test_tasks_release_recovery.py` 两组为**真实新进程终止**（子进程 `os._exit(9)`）；
其余恢复测试为受控钩子/直接构造记录窗口（在各自 docstring 标注）。

## 4. 文档与样例更新

- [tasks-contract.zh-CN.md](tasks-contract.zh-CN.md)：新增 §13“发行阻断修复已交付（RF1–RF5）”，§14 顺延。
- [recovery.md](recovery.md)：新增“按状态读取待处理输入”“子结果等待”“spawn 语义冻结与事务内比较”。
- [contract.md](contract.md)：§10 增补当前协作端口/等待/语义身份边界。
- [current-harness-architecture.zh-CN.md](current-harness-architecture.zh-CN.md)：新增“持久输入与子结果等待（RF1–RF3）”接线图。
- [verification.md](verification.md)：新增本轮门禁与发行验证行。
- [durable-input-orchestration-release-fix-plan.zh-CN.md](durable-input-orchestration-release-fix-plan.zh-CN.md)：状态行更新（正文原貌保留）。
- [samples/task-diagnostics.md](samples/task-diagnostics.md) / `.json`：由 `scripts/regenerate_task_diagnostics_sample.py` 从真实 SQLite 重新生成，
  新增 Recovery 段落（run 状态、等待对象、结果 ready、轮次、基线摘要、快照版本）。
- 原审查失败事实（`durable-input-orchestration-release-review.zh-CN.md`、探针）保留未改，只在本报告追加核对结果。

## 5. 已执行验证与未执行边界

已执行（Python 3.12.13 / macOS arm64，本 checkout，`PYTHONPATH=src`）：

```bash
python scripts/probe_orchestration_release_review.py          # 5 passed, 0 failed (exit 0)
python -m pytest -q tests                                     # 1284 passed, 1 skipped
python scripts/export_public_snapshot.py --refresh-manifest   # 252 files
python scripts/ci_check.py --offline --wheelhouse build-wheelhouse   # 6/6 ok
python scripts/verify_release.py --offline --wheelhouse build-wheelhouse
#   wheel 1259 passed / 26 skipped；sdist 1259 passed / 26 skipped；十个示例全部 exit 0；
#   离线评测与只读诊断从安装产物运行；wheel 99 files，sdist 247 files
```

记录一个与本轮修复无关的观察：`tests/test_runtime_recovery_gaps.py::test_r2_the_runtime_limit_covers_the_recovered_handler`
使用 30ms 运行上限断言处理器已被中断；它在负载下的发行步骤中曾一次出现“处理器在派发前就超时”的时序抖动（`called == []`），
单独重跑 20/20 通过、随后 `ci_check` 重跑 6/6 通过。该测试未被修改或放宽。

未执行（如实单列）：远程 CI；Python 3.10/3.11（本机无对应解释器/wheelhouse）；Linux/Windows；
真实模型 provider（全部评测与示例离线设计）。SQLite 部署边界、协作取消与进程清理能力、
工作区并发、未知计费与数据保留沿用此前记录的实测限制；本地两进程测试不宣称多主机生产验证。
FRB-02 只对轮次（FRB-01）给出实证，deadline/摘要/root 额度维度沿用既有预算测试，未在本轮重新复现。

## 6. 清单与门禁

`python scripts/export_public_snapshot.py --refresh-manifest` 后清单与 `tests/test_build_scripts.py`
清单测试一致；`git diff --check` 无空白问题；后续任何文档改动都会再次刷新清单。

## 7. 交接

本轮所有修复均在 `main` 上以 `75480b4`…`2645822` 的独立提交完成，工作树干净。实施方“已完成”不替代最终独立发行验收：
请 Codex 复验五探针、默认套件与源码树外发行，通过后再升级 `0.2.0a1`、构建正式 wheel/sdist 并交付安装包。
