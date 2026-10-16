# 交付报告：持久输入、任务编排与诊断评测缺陷修复

日期：2026-10-16。核对基线：`745fb3b`。实施计划：[durable-input-orchestration-implementation-plan.zh-CN.md](durable-input-orchestration-implementation-plan.zh-CN.md)。状态：**已实施**（F0–F4、Q0–Q6/V；未执行项单列在 §7）。

本报告只写这次真正执行过的命令与结果。远程 CI、真实模型、3.10/3.11 与 Linux/Windows 没跑过，都在 §7 明说，不藏进成功总数。

## 1. 提交与阶段

| 阶段 | 提交 | 内容 |
| --- | --- | --- |
| F0 | `5b1a147` | 独立验收四项 P2（RDE-D1/E1/E2/E3）先变成失败目标测试，保留修复前事实 |
| F1 | `6baec42` | 分页从最后一条**实际携带**的事件续读，不再跳过已扫描未返回的匹配事件 |
| F2 | `2ff6f6a` | 评测身份覆盖真值、夹具 argv、执行策略与嵌套配置；摘要算法显式版本化，旧摘要标 legacy/incomparable |
| F3 | `e9b394b` | 逐次比较按冻结样本键一对一匹配；缺失进入 unmatched/incomparable，不复制基线 |
| F4 | `53d344b` | 命令采集阶段即有界；超限策略、期限取小、停止后回收、策略与摘要接通 |
| 样例 | `6d46eae`、`587bc23`、`36ee3ce`、`210dda7` | 用同一脚本重生成评测样本并刷新清单 |
| Q0 | `26f2507` | 冻结任务、输入、执行权、取消与预算契约（`docs/tasks-contract.zh-CN.md`） |
| Q1 | `b72d1d1` | 持久 inbox 与控制存储（同库同事务） |
| Q2 | `4473d84` | 同一 Core 的原子消费接线（读取不确认、提交才确认） |
| Q3 | `0e6fc95` | 有界、公平、可取消的本地调度器 |
| Q4 | `0194768` | 任务期限与父子共享预算，预留先于派发 |
| Q5 | `d548a03` | 可恢复父子任务：spawn/wait/结果/取消与终结屏障 |
| Q6a | `96ffd5b` | 只读任务诊断（`TaskInspector` + `diagnostics task` CLI） |
| Q6b | `ff56c78` | 三个离线示例与 §11.2 冻结场景 |
| V | 本次提交 | 文档、能力矩阵、清单与源码树外发行验证 |

## 2. F 线：四项缺陷的修复前后

四项由独立验收（[runtime-diagnostics-evaluation-acceptance-review.zh-CN.md](runtime-diagnostics-evaluation-acceptance-review.zh-CN.md) §2–§5）用可运行探针复现。F0（`5b1a147`）先把探针变成断言，修复前失败；F1–F4 逐项转绿。固定测试位置在 `tests/test_acceptance_review_gaps.py`：

| 编号 | 修复前 | 修复后 | 测试 |
| --- | --- | --- | --- |
| RDE-D1 | 7 条匹配事件只返回 `[0,1]`，`next_cursor=6`，续读为空 | 连续续读覆盖 0–6，无遗漏重复；`truncated`/`scan_limit_hit`/`exhausted` 分别说明 | `test_rde_d1_a_truncated_report_does_not_skip_the_events_it_left_out` |
| RDE-E1 | 只改验收真值或测试命令，摘要仍相同 | 真值改变 → 案例身份改变；测试 argv 改变 → 夹具身份改变 | `test_rde_e1_changing_the_acceptance_truth_changes_the_case_identity`、`..._the_test_command_changes_the_fixture_identity` |
| RDE-E2 | 基线 1 样本、10 tokens 被报成 3 样本、30 tokens | 原始样本只计一次；缺失匹配进入 incomparable | `test_rde_e2_a_baseline_sample_is_counted_once` |
| RDE-E3 | 先读入 262145 字符再裁剪为 2000 | 采集阶段即有字节上限，保留量不随总量增长 | `test_rde_e3_the_command_output_is_bounded_while_it_is_collected`、`..._the_kept_amount_does_not_grow_with_the_total` |

F 门禁：四项红测转绿，原 PTR-R1/R2/R3 回归通过（`docs/runtime-diagnostics-evaluation-acceptance-review.zh-CN.md` §6），比较与样例兼容说明更新。

## 3. 任务 API、版本与旧后端能力矩阵

| 层 | 入口 | 说明 |
| --- | --- | --- |
| 契约 | `tasks/types.py` | 状态表、输入类型/顺序、接纳结果、claim、child relation/result、预算预留、控制 outbox。`TASK_SCHEMA_VERSION=1` |
| 能力端口 | `tasks/ports.py` | `CoordinationStore` 的 `COORDINATION_METHODS`；`require_coordination` 在**任何接纳之前**拒绝 `partial`/`none` |
| 控制存储 | `tasks/inbox.py` | `SqliteControlStore`（继承 `SqlitePersistence`，同库同事务）+ `MemoryControlStore`（`VOLATILE`）。控制库 schema 独立版本（`control_meta`），未知更高版本 `VersionRefused` |
| 持久输入 | `tasks/inputs.py` | `PersistentInputReader`；执行快照 schema 5 记录待确认集合/已应用水位/观察 revision |
| 调度 | `tasks/scheduler.py` | `LocalWorker`、`SchedulerLimits`、`ScheduleDecision`、`WorkResult` |
| 预算 | `tasks/budgets.py` | `BudgetLedger`、`TaskBudget`、`DispatchBudget`、`BudgetSnapshot` |
| 父子 | `tasks/children.py` | `ChildCoordinator`、`ChildPolicy`、`ChildDelegation`、`ChildAuthorization`、`ChildSpawnRequest`、`ResultVerifier` |
| 诊断 | `diagnostics/tasks.py` | `TaskInspector`、`TaskDiagnosticReport`；只读连接、身份校验、白名单输出 |

能力矩阵：

| 后端 | `coordination_capability` | 行为 |
| --- | --- | --- |
| `SqliteControlStore` | `full` | 全部原子操作；启用持久任务的推荐路径 |
| `MemoryControlStore` | `full` | 同一批纯决策，但 `durability` 为 `VOLATILE`，不跨进程 |
| 单 Run 旧后端 | `none` | 原 `start`/`resume`/`cancel` 路径完全不变 |
| 缺部分方法的存储 | `partial` | 启用持久任务时在执行前 `require_coordination` 拒绝，不退化成两次独立写 |

`ReadBoundary`/诊断报告 schema、`EvalCase` 摘要算法版本均显式；旧记录可读，能力不足时列 legacy/incomparable，不用新算法回填后宣称可比。

## 4. 三个离线示例

模型都是脚本；SQLite、worker、coordinator、子进程真实执行。

| 示例 | 真实部分 | 脚本部分 |
| --- | --- | --- |
| `examples/08_durable_input_redelivery.py` | 真实第二进程 `os._exit(9)`、真实控制库、真实 commit 边界 | 报告 completed 的迷你 harness |
| `examples/09_fair_task_scheduling.py` | 真实 worker、fenced claim、queued/waiting 状态机 | 报告 completed/suspended 的迷你 harness |
| `examples/10_parent_child_recovery.py` | 真实第二进程、真实 coordinator、真实终结屏障 | 报告 completed 的迷你 harness |

## 5. 诊断与评测证据

- 任务诊断：`python -m yanxu_harness.diagnostics task --control <db> --namespace N --session S --task-id T`。输出 JSON/Markdown，含 task/run/attempt、输入序列与 applied revision、claim/fence/live、子关系与验收、预算预留、控制 outbox；原文、子结果全文与绝对工作区路径以摘要/尾部替代（`tests/test_diagnostics_tasks.py` D-01/D-02）。
- 运行诊断与冻结评测：源码树外安装后从 wheel/sdist 运行（§1 V 行），离线评测案例与只读诊断报告均 `OK`。
- §11.2 冻结场景：`tests/test_tasks_acceptance_matrix.py`，七个场景各由独立验收器重读已提交控制记录判定，另含 F2 摘要规则与 F 线测试位置。

## 6. 验收矩阵证据位置

| 编号 | 测试位置 |
| --- | --- |
| F-01…F-05 | `tests/test_acceptance_review_gaps.py`、`tests/test_evals_offline.py`、`tests/test_diagnostics_reader.py` |
| I-01…I-06 | `tests/test_tasks_inbox.py`、`tests/test_tasks_input_flow.py` |
| S-01…S-03 | `tests/test_tasks_scheduler.py`、`tests/test_tasks_acceptance_matrix.py` |
| B-01、B-02 | `tests/test_tasks_budgets.py`、`tests/test_tasks_acceptance_matrix.py::shared-budget` |
| C-01…C-04 | `tests/test_tasks_children.py`、`tests/test_tasks_acceptance_matrix.py::parent-child-recovery`、`parent-cancel-late-result` |
| D-01、D-02 | `tests/test_diagnostics_tasks.py` |
| V-01 | `tests/test_tasks_contract.py`（`partial`/`none` 拒绝）、`tests/test_storage_sqlite.py`、`tests/test_runtime_recovery_compat.py` |
| V-02 | `scripts/verify_release.py`（本次运行的 wheel/sdist 记录） |

实际命令与结果见 [verification.md](verification.md) 的“durable-input-orchestration round”表：专项回归 **248 passed**；`tests/core/test_core_agent.py` **43 passed**；Q0–Q5 任务套件 **121 passed**；完整套件 **1247 passed / 1 skipped**；`ci_check.py --offline` **六步全 ok**；`verify_release.py` wheel/sdist **各 1223 passed / 25 skipped**，**十个示例源码树外全部 exit 0**。

失败分类：任务失败（`failed`/`usage_limited`）、基础设施错误（`infra_error`）与无法比较（`invalid_result`/legacy）在评测层分开；控制层以 `ChildRefused`/`BudgetRefused`/`ClaimLost`/`VersionRefused`/`ConfigurationError` 命名拒绝，不伪装成成功。

## 7. 独立复核后的缺陷修复（2026-10-16）

复核发现 7 项问题，均已修复并加回归测试：

| # | 问题 | 根因 | 修复 | 测试 |
| --- | --- | --- | --- | --- |
| 1 | 父 Run 提前完成，子任务返回后父任务恢复失败 | `_run_record` 返回 `VersionedRun`，`_is_terminal` 看不到 `status`，已完成的 Run 被当成可 resume，`resume` 拒绝后报 failed | `_run_record` 解析为 `RunRecord`（非完整 state 用 `_RunView` 取 status）；已完成 Run 在恢复时由 `_settle` 结算，不再误报失败 | `test_a_run_that_finished_before_its_task_was_settled_is_settled_on_recovery`、`test_a_parent_settles_after_its_child_completes_using_the_real_runner` |
| 2 | Run 已完成、Task 尚未结算时恢复误报失败 | 同上 | 同上 | 同上 |
| 3 | 累计输入超过 200 条后，新输入仍 pending 而 Run 完成 | reader 与终结守卫都从 sequence -1 读一页，历史超过一页后新输入不可见 | `read_applied_watermark` + 从水位向后读；`_input_arrived` 同改 | `test_a_new_input_is_seen_after_more_than_a_page_of_history` |
| 4 | 真实任务运行时 namespace 并发上限失效 | `_inflight` 存 harness 对象（无 namespace），按名称空间计数恒为 “” | 新增 `_inflight_tasks` 存任务记录，`slots`/`_choose` 从任务读取 | `test_the_namespace_cap_counts_tasks_not_harness_objects` |
| 5 | 相同 spawn 身份改变配置未拒绝 | 只比较 parent/child id，不比较授权摘要 | `spawn` 与 `register_child` 都比较 `authorization_digest`，不同即 `config_conflict` | `test_a_retry_that_changes_the_authorization_is_refused` |
| 6 | follow-up 被提前并入首轮 | reader 忽略轮询站点的期望类型 | steering 站点只取 steering；AFTER_INNER 优先 follow-up、无 follow-up 时才取 steering | `test_a_follow_up_is_not_read_at_the_opening_steering_site` |
| 7 | 示例/场景用 stub 替换了整个 Harness，未证明真实 Runner 的父子恢复 | — | 新增真实 `DefaultHarness`（父子各一个）的回归 | `test_a_parent_settles_after_its_child_completes_using_the_real_runner` |

**边界说明：** 终结屏障在 **Task** 终态前执行（`LocalWorker._settle` 的 `child_barrier`），并在“Run 已完成、Task 未结算”的恢复路径上重查；因此父 Task 不会在有活动 child 时完成，崩溃也不会绕过。父 Run 自身可能已先提交终态，此时父的“续跑”是结算而不是新的模型轮次；把该屏障提前到 Run 终态之前（以 deferred spawn 工具批汶挂起 + 解析）是已记录的下一个增量，本轮不以“给 pending_interaction 加字段”假装已支持。

## 8. 实测限制与未执行项

- **SQLite 部署边界**：控制库、Run 库与操作账本默认同文件同事务；跨库只靠稳定身份与核对，不宣称分布式原子事务。
- **协作取消与进程清理**：POSIX/Windows 子进程及后代清理能力分别声明；未能证明已停止的外部工作记为未知。
- **工作区并发**：默认对子任务隔离工作区；共享写工作区的串行/版本控制由宿主负责。
- **未知计费**：派发窗口内无最终 usage 的预留保持 `unknown`，不退款、不写零。
- **数据保留**：首版不自动清理输入正文/去重记录/证据，容量有界并记录后续清理约束。
- **未执行**：远程 CI（从未运行）；Python 3.10/3.11（本机无对应解释器/wheelhouse）；Linux/Windows；真实模型 provider（所有评测与示例离线）；StoryIDE 宿主接入（按计划在验收后再执行）。
