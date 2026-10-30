# Plan 与任务 DAG 验收映射（PA01–PA40）

审查方式：实施者对当前工作树进行自验，不是独立复核。状态仅表示表内测试或明确的边界证据；未执行的解释器/平台不会由已有测试数推断为通过。

| ID | 结果 | 测试 / 证据 |
| --- | --- | --- |
| PA01 | 通过 | `tests/test_plans_contract.py::test_invalid_dags_are_rejected_before_review`、`test_dependency_edge_limit_is_enforced`；无效定义不能进入控制存储。 |
| PA02 | 通过 | `tests/test_plans_contract.py::test_digest_ignores_step_and_dependency_set_order_but_not_content`、`test_audit_time_is_not_part_of_the_content_digest`。 |
| PA03 | 通过 | `tests/test_plans_scheduler.py::test_expired_approval_and_plan_deadline_refuse_step_admission`；`tests/test_plans_store.py::test_expired_approval_does_not_reopen_a_paused_plan`；未批准定义没有 ready 候选。 |
| PA04 | 通过 | `tests/test_plans_store.py::test_approval_is_bound_to_current_definition_and_trusted_principal`、`test_replanning_publishes_a_new_draft_without_inheriting_approval`。 |
| PA05 | 通过 | 同上 trusted-principal 测试；控制 API 从宿主 principal 取审批/验收主体，候选 JSON 中的额外 `approval` 字段由 `tests/test_examples_plans.py::test_candidate_with_unrecognized_authority_fields_is_rejected_before_plan_creation` 拒绝。 |
| PA06 | 通过 | `tests/test_plans_acceptance.py::test_task_completion_does_not_unlock_a_dependent_step`、`test_acceptance_requires_the_committed_run_and_matching_evidence`。 |
| PA07 | 通过 | `tests/test_plans_acceptance.py::test_diamond_dag_waits_for_both_accepted_predecessors`：A accepted 后 B/C ready；D 等待 B、C 都 accepted。Worker 并发上限另由 PA37 验证。 |
| PA08 | 通过 | `test_task_completion_does_not_unlock_a_dependent_step`、`test_rejected_verification_does_not_unlock_dependents`、`test_unknown_operations_and_unreadable_evidence_remain_blocked`。 |
| PA09 | 通过 | `tests/test_plans_acceptance.py::test_nonzero_test_result_is_rejected_by_host_verifier`。退出码属于宿主证据；Harness 不内置测试进程或 shell verifier。 |
| PA10 | 通过 | `tests/test_plans_acceptance.py::test_evidence_with_mismatched_identity_is_refused` 覆盖 namespace、Plan、revision、binding、Task、Run 和 Run version。 |
| PA11 | 通过 | `test_evidence_change_during_verification_is_inconclusive`、`test_evidence_reader_timeout_is_inconclusive_and_does_not_unlock`、`test_verifier_timeout_is_inconclusive`。 |
| PA12 | 通过 | `tests/test_plans_store.py::test_create_is_idempotent_and_binds_an_immutable_revision`、`test_controls_use_cas_and_same_request_replays_the_original_record`、`tests/test_plans_scheduler.py::test_claim_retry_reuses_binding_task_run_and_initial_input`；PA21 覆盖接受通知重启消费。 |
| PA13 | 通过 | `test_create_is_idempotent_and_binds_an_immutable_revision` 覆盖 request key 重用冲突和同 Plan 身份的不同内容冲突。 |
| PA14 | 通过 | `tests/test_plans_scheduler.py::test_two_sqlite_workers_admit_only_one_identity_for_the_same_step` 使用两个 SQLite 连接并发 claim。 |
| PA15 | 通过 | `test_ready_step_is_claimed_atomically_and_hidden_from_ordinary_worker`、`test_only_enabled_worker_drives_ready_plan_steps_through_harness`。 |
| PA16 | 通过 | `tests/test_plans_scheduler.py::test_blocked_step_does_not_consume_ready_candidate_limit`。 |
| PA17 | 通过 | `test_step_claim_races_with_pause_or_replan_at_one_sqlite_commit_boundary`，两条 SQLite 连接同时尝试准入与关门；成功的一方必须有完整 binding/Task/事件，另一方不能派发。 |
| PA18 | 通过 | `tests/test_plans_acceptance.py::test_pause_keeps_completed_work_and_requires_resume_before_unblocking`：暂停期间已完成事实保留，验收后仍不解锁后继，显式 resume 后才 ready。 |
| PA19 | 通过 | `tests/test_plans_replanning.py::test_real_process_exit_after_plan_admission_recovers_same_cancelled_run`，子进程在 Run 创建前退出，重启沿用原绑定。 |
| PA20 | 通过 | `test_pa20_real_process_terminal_run_is_settled_and_accepted_after_restart`。 |
| PA21 | 通过 | `test_pa21_real_process_acceptance_outbox_is_consumed_once_after_restart`。 |
| PA22 | 通过 | `test_pa22_real_gateway_effect_remains_unknown_and_is_not_run_again`；真实 Gateway/SQLite 操作账本证明 unknown 不重复派发。 |
| PA23 | 通过 | PA17 的 claim/replan 竞争；另有 `test_replan_closes_admission_and_atomically_records_active_cancel_targets` 与 `test_reconcile_settles_terminal_run_before_replan_can_publish`。 |
| PA24 | 通过 | `tests/test_plans_replanning.py::test_unknown_operation_keeps_old_revision_active_and_unpublishable`。 |
| PA25 | 通过 | `tests/test_plans_store.py::test_replanning_publishes_a_new_draft_without_inheriting_approval` 扩展到 revision 2 的重新 review、重新批准、绑定和拒绝旧 revision 候选。 |
| PA26 | 通过 | PA25 的旧 revision candidate 拒绝；`test_cancellation_recovery_claim_waits_for_old_fence_and_keeps_binding` 验证新 fence；`tests/test_plans_store.py::test_outbox_delivery_is_durable_and_idempotent` 验证迟到重复通知无第二次交付。 |
| PA27 | 通过 | `tests/test_plans_acceptance.py::test_rejected_completed_step_can_be_explicitly_retried_with_new_binding` 与 `test_inconclusive_binding_cannot_be_retried`。 |
| PA28 | 通过 | `tests/test_plans_scheduler.py::test_zero_plan_execution_limit_refuses_all_step_admission`、`test_expired_approval_and_plan_deadline_refuse_step_admission`；`tests/test_plans_replanning.py::test_step_execution_limit_is_shared_across_published_revisions`。 |
| PA29 | 通过 | `tests/test_plans_replanning.py::test_cancel_plan_stays_cancelling_until_task_and_run_are_settled` 保留完成事实并等到 binding 静止后取消。 |
| PA30 | 通过 | `tests/test_plans_scheduler.py::test_task_and_binding_roll_back_when_control_event_write_fails`、`tests/test_plans_acceptance.py::test_acceptance_and_downstream_notification_roll_back_together`。失败注入在事务事件边界，检查原子回滚和无后继准入。 |
| PA31 | 通过 | `tests/test_plans_store.py::test_outbox_delivery_is_durable_and_idempotent`、PA21、`tests/test_plans_scheduler.py::test_blocked_step_does_not_consume_ready_candidate_limit`。 |
| PA32 | 通过 | `tests/test_plans_acceptance.py::test_sqlite_plan_schema_v1_migrates_when_orchestration_is_enabled`、`test_read_only_store_refuses_plan_migration_without_writing`、`tests/test_plans_store.py::test_sqlite_refuses_an_unknown_plan_schema_version`、`test_reference_store_requires_explicit_orchestration_enablement`。 |
| PA33 | 通过 | `tests/test_diagnostics_plans.py::test_plan_cli_does_not_create_a_missing_database`、`test_plan_inspector_refuses_writable_store_and_wrong_identity`。 |
| PA34 | 通过 | `tests/test_plans_acceptance.py::test_finalize_requires_all_steps_and_plan_level_criteria`、`tests/test_plans_store.py::test_replanning_publishes_a_new_draft_without_inheriting_approval`。 |
| PA35 | 部分通过 | 完整套件为 1,592 passed、22 skipped、2 failed；失败是未修改的 Core 取消测试。离线 CI 与树外验证也只重现这两条。快照门禁已重生成并单独通过。 |
| PA36 | 部分验证 | Python 3.12 完成主测；Python 3.11.15 通过源码导入烟测；当前环境没有 Python 3.10。wheel/sdist 已构建并安装到源码树外，复制套件为 1,665 passed、30 skipped、2 failed（同两条 Core 取消用例）。 |
| PA37 | 通过 | `tests/test_plans_scheduler.py::test_worker_merges_ordinary_task_and_ready_plan_under_one_slot` 验证普通 Task 与 ready Plan 进入同一单并发槽。 |
| PA38 | 通过 | `tests/test_examples_plans.py::test_candidate_with_unrecognized_authority_fields_is_rejected_before_plan_creation`；现有 Gateway write policy 行为另由 `tests/test_tools_core_bridge.py` 与 `tests/test_tools_policy.py` 覆盖。规划 Provider 没有写工具可见性。 |
| PA39 | 通过 | `tests/test_plans_replanning.py::test_reconcile_settles_terminal_run_before_replan_can_publish` 与 `test_unknown_operation_keeps_old_revision_active_and_unpublishable`；maintenance reconcile 不调用 Harness，也不清除 unknown。 |
| PA40 | 通过 | `tests/test_plans_acceptance.py::test_late_rejection_cannot_overwrite_final_acceptance`；同 binding 的终局决定不可被不同 request 的晚到拒绝覆盖。 |

## 最终验证

此报告由实施者自验，不代表独立安全或业务验收。完整套件的两个失败详见下表；相关 Core 文件没有本次改动。未执行的版本和平台保留为未验证。

| 检查 | 命令 / 结果 |
| --- | --- |
| Plan / diagnostics / examples / public API 聚焦回归 | `.venv/bin/python -m pytest -q tests/test_plans_contract.py tests/test_plans_store.py tests/test_plans_scheduler.py tests/test_plans_acceptance.py tests/test_plans_replanning.py tests/test_diagnostics_plans.py tests/test_examples_plans.py tests/test_public_api.py tests/test_public_snapshot_whitelist.py tests/test_import_fence.py` → **157 passed in 2.96s**。 |
| 完整源码套件 | `.venv/bin/python -m pytest -q tests` → **1,592 passed, 22 skipped, 2 failed in 100.20s**。失败：`tests/core/test_core_loop_result.py::test_cancellation_between_turns_is_reported_as_cancelled` 与 `tests/core/test_core_tool_execution.py::test_cancellation_before_any_hook_yields_an_aborted_result`。 |
| 快照一致性 | `.venv/bin/python -m pytest -q tests/test_build_scripts.py::test_the_committed_manifest_matches_the_current_tree` → **1 passed**；最终 `SNAPSHOT.json` 列出 **381 files**。 |
| Python 3.12 离线 wheelhouse | `.venv/bin/python scripts/ci_check.py --prepare-wheelhouse build-wheelhouse` → **46 wheels prepared**。 |
| 最终离线 CI 与树外制品 | `.venv/bin/python scripts/ci_check.py --offline --wheelhouse build-wheelhouse --keep-artifacts --artifacts-dir build/p0.6-ci-final` 退出 **1**：package suite **1,592 passed, 22 skipped, 2 failed**；wheel/sdist 构建、离线安装和树外 wheel 导入成功，复制 suite **1,665 passed, 30 skipped, 2 failed**，两处均只失败于同两条 Core 取消用例。冷导入、相对 Markdown 链接、来源文件检查通过。wheel `yanxu_harness-0.4.0a1-py3-none-any.whl` 为 666,939 bytes，SHA-256 `0de39e45ae882b998979db9b23546aea7c74f03864618efcd7db4cfcb918c004`；sdist `yanxu_harness-0.4.0a1.tar.gz` 为 1,607,186 bytes，SHA-256 `b016f5913bb8ec26238303293250da89d999c0a39b865f0bfe4c790eb8dcc149`。 |
| Python 3.11 | `PYTHONPATH=src python3.11 -c 'import sys, yanxu_harness; print(sys.version.split()[0], yanxu_harness.__file__)'` → **3.11.15**，源码导入成功。 |
| Python 3.10 | 当前环境无 `python3.10` 可执行文件，未验证。 |
