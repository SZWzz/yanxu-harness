# Verification record

What is verified, by which test, and from which kind of evidence. This file exists
because "the tests pass" is not the same claim as "this window was exercised with a
real process and a real store" — and the difference is the whole point of the
durability contract.

Update it whenever a window gains or loses a test. Every entry names a test that can
be run individually:

```bash
python -m pytest -q tests/test_runtime_recovery.py::test_a_second_process_resumes_and_the_write_happens_exactly_once
```

## How each class of claim is measured

| Claim | How it is measured |
| --- | --- |
| an external effect happened at most once | the handler appends one line per invocation to a file that outlives the process, plus the operation ledger's receipt count — never a mock's call counter alone |
| state survived a restart | a **new process** (a real child) reads it, or a new store instance opened on the same file |
| nothing was written before a decision | the business row's version, the operation ledger's rows, and the handler's own append-only log are all checked |
| a pause is durable | the run record, its batch and its events are read back from SQLite after the process exited |
| an uncertain outcome stays uncertain | the receipt state in the ledger, and the run's `pending_interaction.kind == "reconciliation"` |
| events are fenced | the dispatch/attempt identity in the envelope, and the fence's recorded refusal reason |
| a distribution works outside its tree | `scripts/verify_release.py`: build, install into a throwaway venv, assert the imported file is the installed one, then run the copied tests and examples |

Failure windows are triggered with real child processes and explicit synchronisation
(`tests/fault_windows.py` kills a process with `os._exit` at a named point), not with
random sleeps or exception doubles. Every asynchronous or process-based test has a
timeout and reclaims its child.

## The matrix

| # | Window | Evidence |
| --- | --- | --- |
| T01 | normal dialogue, streaming tool round trip | `test_provider_core_bridge.py::test_a_tool_call_is_executed_once_by_the_gateway_and_its_result_reaches_the_model` (real loopback provider → adapter → the one loop → gateway), `::test_the_streamed_frames_and_the_final_message_agree`, `tests/core/test_core_loop_result.py::test_a_plain_answer_is_a_completed_run_with_its_text_and_usage` |
| T02 | turn budget reached | `tests/core/test_core_loop_result.py::test_hitting_the_turn_budget_is_usage_limited_and_not_completed`, `test_runtime_default_harness.py::test_a_turn_budget_stop_is_usage_limited_with_the_used_turn_count` |
| T03 | pause before approval: nothing written, no unapproved intent, readable from disk | `test_runtime_recovery.py::test_a_paused_run_survives_the_process_that_paused_it` (child process; asserts an **empty** operation ledger), `test_runtime_resume.py::test_a_write_pauses_before_the_next_model_request_and_is_persisted`, `::test_the_pause_is_committed_with_its_events` |
| T04 | approval drift (arguments, target version, tool version, policy, preview, expiry) | `tests/test_tools_gateway.py::test_an_approval_stops_applying_when_the_arguments_change`, `::test_an_approval_stops_applying_when_the_target_version_changes`, `::test_an_expired_approval_does_not_apply`, `::test_a_denied_call_stays_denied`, `::test_the_gateway_cannot_write_an_approval_itself`; `test_runtime_resume.py::test_an_expired_approval_goes_back_to_the_author_instead_of_running`, `::test_a_changed_tool_set_refuses_the_resume_with_the_drift_named`; the end-to-end drift scenario (with the earlier decision kept as history) is asserted inside `examples/05_durable_approved_edit.py` |
| T05 | the target moved between the check and the write | the example's stale-approval and compare-and-set scenarios (`examples/05_durable_approved_edit.py`, asserted by `tests/test_examples_durable_edit.py::test_the_default_example_runs_its_whole_chain_and_exits_clean`): the newer text stands and the model is told why; `tests/test_tools_core_bridge.py::test_target_version_comes_from_the_host_not_from_the_arguments` |
| T06 | killed after the pause, resumed by a new process | `test_runtime_recovery.py::test_a_second_process_resumes_and_the_write_happens_exactly_once` (two child processes; the handler's append-only log names the attempt), `::test_the_operation_ledger_shows_one_receipt_for_the_resumed_call`, `::test_resuming_a_finished_run_is_refused_and_does_not_write_again`, `::test_an_unapproved_resume_asks_again_and_writes_nothing` |
| T07 | crash after the intent, before the effect | `tests/test_storage_sqlite.py::test_a_process_that_dies_after_the_intent_leaves_an_unsettled_operation` (real killed child); `test_runtime_recovery.py::test_an_orphaned_run_enters_reconciliation_instead_of_staying_stuck` (a run whose driver died before recording its position becomes a reconciliation wait and replays nothing), `::test_a_reconciliation_resume_settles_only_with_business_evidence` |
| T08 | crash after the effect, before the receipt | `tests/test_storage_sqlite.py::test_a_process_that_dies_after_the_effect_reports_an_unprovable_outcome`; `test_runtime_resume.py::test_an_unproven_operation_stays_unknown_and_asks_for_evidence`, `::test_business_evidence_settles_an_unproven_operation`, `::test_a_reconciler_claim_without_evidence_is_refused`; `tests/test_tools_gateway.py::test_an_unprovable_outcome_is_reported_as_unknown_and_never_rerun` |
| T09 | crash after the receipt, before the checkpoint | `tests/test_storage_sqlite.py::test_a_settled_operation_is_replayed_after_a_restart`; `test_runtime_resume.py::test_a_settled_call_is_reused_and_only_the_waiting_one_is_dispatched` (the settled call's recorded result is reused; the handler is not called again) |
| T10 | one batch with a done call, a waiting call and an unknown one | `test_runtime_resume.py::test_a_settled_call_is_reused_and_only_the_waiting_one_is_dispatched` (partition + declared order), `tests/test_tools_core_bridge.py::test_every_status_keeps_its_own_name_and_only_success_is_not_an_error`, `::test_only_the_three_pending_statuses_suspend_the_run`, `tests/test_runtime_checkpoint.py::test_the_partition_must_cover_the_declared_batch_exactly` |
| T11 | two processes resume the same run | `test_runtime_isolation.py::test_a_second_worker_cannot_drive_the_same_run`, `::test_a_stale_worker_cannot_commit_after_losing_its_lease`, `::test_a_live_driver_cannot_be_recovered_from_under`, `tests/test_storage_sqlite.py::test_a_second_process_cannot_take_a_live_lease`, `::test_an_expired_lease_is_reclaimable_and_the_old_fence_is_dead`, `::test_a_lease_claimed_by_a_dead_process_is_still_held`, `test_runtime_isolation.py::test_a_lease_is_released_when_a_run_finishes` |
| T12 | the same ids in two namespaces | `test_runtime_isolation.py::test_the_operation_key_includes_the_namespace` (two distinct operations), `::test_a_second_namespace_cannot_read_or_resume_the_first_runs_record`, `test_runtime_default_harness.py::test_a_different_namespace_cannot_touch_the_same_run_id`, `tests/test_tools_gateway.py::test_a_namespace_mismatch_is_a_bug_not_a_denial` |
| T13 | duplicate approval, duplicate start, duplicate resume | `test_runtime_default_harness.py::test_a_duplicate_start_reuses_the_stored_state_instead_of_running_again`, `::test_starting_a_run_with_different_input_is_refused_and_changes_nothing`, `::test_a_second_invocation_while_running_is_busy`; `tests/test_tools_gateway.py::test_an_approved_call_executes_exactly_once`; `test_runtime_recovery.py::test_resuming_a_finished_run_is_refused_and_does_not_write_again` |
| T14 | denied or expired approval | `test_runtime_recovery.py::test_a_denied_decision_reaches_the_model_without_running_the_tool`, `tests/test_tools_gateway.py::test_a_denied_call_stays_denied`, `test_runtime_resume.py::test_an_expired_approval_goes_back_to_the_author_instead_of_running` |
| T15 | cancellation while waiting for the provider, and while a tool runs | `test_runtime_isolation.py::test_cancelling_from_another_thread_stops_a_blocked_provider` (a real other thread), `::test_a_cancelled_run_leaves_no_dangling_task_and_the_loop_stays_responsive`, `::test_cancellation_after_a_write_leaves_the_receipt_alone`, `test_provider_core_bridge.py::test_cancelling_a_slow_stream_is_bounded_and_reported_as_aborted`; final state: `test_runtime_default_harness.py::test_cancelling_an_active_run_records_a_cancelled_outcome`, `tests/core/test_core_loop_result.py::test_cancellation_between_turns_is_reported_as_cancelled` |
| T16 | a late event from an old attempt | `test_runtime_isolation.py::test_an_event_from_an_older_attempt_is_refused_with_a_reason`, `::test_a_stale_event_never_reaches_the_listener_or_the_transcript`, `::test_an_out_of_order_event_is_not_applied_twice`, `::test_an_event_from_another_namespace_or_run_is_refused` |
| T17 | the transaction committed, the consumer failed | `test_runtime_resume.py::test_a_listener_failure_does_not_roll_back_or_lose_the_events` (state intact, replay from the cursor delivers what was missed), `tests/test_storage_sqlite.py::test_state_and_events_are_visible_after_a_crash_at_fanout` |
| T18 | provider error, illegal frame, bad tool arguments | `test_provider_core_bridge.py::test_an_http_failure_is_an_error_terminal_with_a_machine_readable_kind`, `::test_a_stream_without_a_terminal_frame_is_refused_rather_than_reported_as_stop`, `::test_unparseable_tool_arguments_become_an_error_terminal_not_a_tool_call`, `::test_an_unrecognised_finish_reason_is_not_a_finished_answer`, `::test_missing_usage_fields_stay_missing`; `tests/test_provider_wire.py::test_an_unparseable_frame_names_the_json_error`; `test_runtime_default_harness.py::test_a_model_failure_is_failed_with_the_provider_kind` |
| T19 | future schema, corrupt checkpoint, missing batch | `tests/test_runtime_checkpoint.py::test_a_future_schema_version_is_refused`, `::test_a_corrupt_partition_in_a_record_is_refused_on_load`, `::test_a_missing_schema_version_is_refused_with_a_reason`; `tests/test_storage_sqlite.py::test_a_newer_schema_is_refused_not_guessed`; `test_runtime_isolation.py::test_a_future_envelope_version_is_refused`; `test_runtime_resume.py::test_a_suspended_record_without_a_batch_is_refused` |
| T20 | public events and run records leave no private material | `test_runtime_isolation.py::test_published_events_never_carry_host_secrets` (real provider with a key and a host header; the envelopes carry neither), `test_provider_core_bridge.py::test_a_thinking_signature_stays_on_the_block_and_out_of_public_payloads`, `::test_a_second_credential_path_is_refused`, `tests/core/test_core_events.py::test_message_update_rejects_raw_provider_event_in_public_payload`, `tests/test_events_and_versions.py`, `tests/test_provider_wire.py::test_the_api_key_travels_as_a_header_only`, `::test_a_credential_in_an_error_body_is_redacted` |

| T21 | one runner drives every entry point; explicit state and bounded tool concurrency | `tests/core/test_core_single_runner.py::test_the_compatibility_module_reexports_the_same_objects` (the old module and the runner are the same objects, and the harness drives the same kernel), `::test_the_old_loop_body_is_gone`, `::test_every_entry_point_calls_the_runner`, `::test_a_concurrency_limit_is_observed_and_does_not_reorder_results` (measured peak, unchanged result order), `tests/core/test_core_execution_state.py::test_a_recorded_event_stream_rebuilds_the_same_state_and_actions`, `::test_replaying_the_same_event_twice_changes_nothing`, `::test_a_repeated_event_is_ignored_with_a_reason_and_creates_no_second_action`, `::test_the_same_event_id_with_different_content_is_refused` |

| T22 | the commit port: the model's response is durable before its tool calls dispatch | `tests/core/test_core_execution_durable.py::test_the_state_is_committed_before_the_tool_batch_is_dispatched` (ordering asserted on the port itself), `tests/test_core_commit_port_crash.py::test_a_crash_inside_a_tool_leaves_the_declared_call_committed_and_the_effect_unproven` (**real child process** killed inside the handler with `os._exit`; the fsynced commit log shows `phase=running_batch`, the declared call and no result, while the effect log proves the handler ran), `::test_a_failing_commit_port_ends_the_run_instead_of_being_ignored`, `::test_a_commit_failure_at_the_batch_boundary_stops_the_dispatch`, `tests/test_runtime_execution_snapshot.py::test_a_pause_commits_the_explicit_state_alongside_the_batch`, `::test_the_outcome_reports_the_pending_work_projection`, `::test_a_snapshot_that_disagrees_with_the_batch_is_refused_on_resume`, `::test_a_record_without_a_snapshot_is_still_readable`, `::test_a_tampered_projection_cannot_make_the_snapshot_disagree`, `tests/core/test_core_execution_durable.py::test_a_future_or_missing_schema_version_is_refused`, `::test_an_unknown_phase_or_status_is_refused_not_guessed` |

| T24 | a `prepare_next_turn` hook replaces the context and its tool set | `tests/core/test_core_prepare_turn_tools.py::test_a_tool_revoked_by_prepare_next_turn_cannot_be_called` (handler never runs, result is "not found"), `::test_a_tool_granted_by_prepare_next_turn_can_be_called`, `::test_an_empty_replaced_tool_set_revokes_everything`, `::test_a_snapshot_with_messages_only_keeps_the_current_tool_set`, `::test_the_declarations_and_the_executable_set_cannot_drift_apart`. These exist because an independent audit reproduced the opposite behaviour in the first S3 implementation: a revoked tool still ran and a granted one could not be found |
| T23 | cancellation and isolation of a stale execution | `tests/core/test_core_cancellation_isolation.py::test_a_cancelled_run_executes_no_tool_handler` (handler counter stays empty), `::test_a_cancellation_does_not_discard_a_real_receipt`, `::test_the_first_cancellation_reason_wins`, `::test_a_settled_batch_from_an_old_turn_is_refused`, `::test_two_results_for_one_call_are_refused`, `::test_a_late_event_after_a_run_ended_cannot_start_a_new_turn`, `::test_a_settled_batch_in_a_newer_phase_is_refused_not_applied`, `::test_two_agents_on_one_loop_do_not_block_each_other`, `::test_the_event_stream_buffer_is_bounded_and_says_so_when_full`, `::test_a_cancelled_agent_run_leaves_no_dangling_task`; `tests/test_runtime_execution_snapshot.py::test_a_cancellation_while_suspended_is_recorded_and_recognized_before_resume` (nothing dispatched, refused afterwards), `::test_the_cancellation_record_keeps_the_first_reason`, `::test_cancelling_a_finished_run_does_nothing` |
| T25 | the wired commit boundaries of the real `DefaultHarness + SqlitePersistence` (F01–F10) | `tests/test_runtime_commit_boundaries.py`: **F01** `::test_f01_a_dying_handler_finds_its_call_declared_and_its_outcome_unclaimed` (real child, `os._exit` inside the handler; another process reads the `running_batch` snapshot, the assistant message, the declared `call-1` and the turn count while the ledger holds only an unsettled intent), **F02** `::test_f02_a_failed_boundary_commit_dispatches_nothing` (a controlled `commit_transition` failure at the dispatch port; an fsynced handshake records that the port was reached and asserts the effect file is still absent there, then handler calls = 0, no ledger row, no further model request, `storage_conflict` preserved), **F03** `::test_f03_the_request_phase_is_persisted_before_the_provider_sees_the_request` (the provider bridge reads the store from its own connection), **F04** `::test_f04_a_handler_that_dies_before_its_effect_leaves_the_call_declared_and_unproven` (child; the snapshot claims nothing, recovery asks the ledger for evidence, and the batch-less suspension is refused rather than guessed), **F05** `::test_f05_a_settled_call_stays_settled_and_is_not_dispatched_again` (child; one ledger row, receipt kept, handler not re-run), **F06** `::test_f06_a_pause_committed_before_the_driver_finishes_is_already_consistent` (child dies in the pause fanout; a second child resumes after the bounded lease wait and writes exactly once), **F07** `::test_f07_a_normal_multi_turn_run_writes_each_message_once`, `::test_f07_a_resume_replaces_the_placeholder_instead_of_appending_a_second_result`, `::test_f07_a_run_may_pause_more_than_once_and_finishes_once`, `::test_f07_a_pause_does_not_refresh_the_turn_budget`, `::test_f07_results_are_assembled_in_declaration_order_when_handlers_finish_out_of_order` (handshake-forced completion order `[c3, c1, c2]`; the stored transcript **and** the next real provider request both carry `[c1, c2, c3]`, each result checked against its own content), `::test_f07_a_partly_settled_batch_is_assembled_in_declaration_order_after_a_resume`, **F08** `::test_f08_an_expired_lease_before_the_boundary_commit_dispatches_nothing`, `::test_f08_a_concurrent_cancellation_survives_and_stops_the_dispatch` (real time and a real concurrent `cancel()`; the newer fact wins, the neighbouring run is untouched, the next entry commits `cancelled`), **F09** `::test_f09_a_failed_terminal_notification_leaves_one_committed_terminal_fact`, `::test_f09_an_interrupted_run_keeps_the_committed_history_exactly_once` (the context is not re-appended), `::test_f09_a_second_entry_does_not_revive_or_rewrite_a_finished_run`, **F10** `::test_f10_a_record_without_a_snapshot_is_read_on_its_batch_alone`, `::test_f10_an_unknown_execution_schema_is_refused_instead_of_guessed`, `::test_f10_a_run_record_without_a_schema_version_is_refused` |

## What is *not* verified here

Recorded honestly, because a missing row is not a passing row:

| Area | State |
| --- | --- |
| `unknown -> executed/failed` convergence | **not verified, and not implemented.** An independent P0: who decides, from which trusted evidence, through which reconciliation entry, under which audit rule is still undecided. Until then an unprovable operation stays `unknown` and is neither replayed nor reported as success. Passing F01–F10 does not close this item, and it does not close the safe-replay loop. |
| Python 3.10 | **executed in the alpha round** (CPython 3.10.14); **not executed after S0–S6**, because no 3.10 interpreter is installed on this machine any more. The static gates in `tests/test_python_floor.py` (`ast.parse(feature_version=(3, 10))` plus a scan for 3.11+/3.12+/3.13+ APIs) still hold and are what keeps the floor from drifting meanwhile — they complement a run, they do not replace it. |
| Linux / Windows | **not executed**. They are in the CI matrix, but the first CI run never started (account billing/limit, an external blocker). Path handling, encoding and SQLite are the places where a "works on macOS" claim usually breaks. |
| GitHub Actions | **never ran**. The workflow is a plan with a local equivalent: `scripts/ci_check.py --offline` runs the same steps on a workstation, and that is what is reported. |
| a real model provider | **not called** by any automated check. The provider tests use a loopback HTTP server; `examples/optional/real_model_edit.py` is the manual path and is outside the gate. |
| exactly-once for arbitrary external effects | **not claimed**. The package makes an unprovable outcome visible (`unknown`) instead of promising it cannot happen. |
| cross-database atomicity | **not claimed**. The run store, the operation ledger and the approval ledger may be three files (as in `examples/05_durable_approved_edit.py`); recovery works from references and receipts. |
| the core state-machine restructure (S2+) from `core-state-machine-implementation-plan.zh-CN.md` | **implemented: S0-S6**. The explicit state, the pure reducer and the single runner are in place (`core/execution.py`, `core/transitions.py`, `core/runner.py`) and every entry point goes through them. The migration table records each stage's status, including what each stage deliberately left undone. The dispatch-window recovery is deliberately still conservative: the committed snapshot describes the window and names the declared calls, but nothing is replayed from it without ledger evidence. |

## Release record for this alpha

Two record blocks, because the state-machine round (S0–S6 of
`docs/core-state-machine-implementation-plan.zh-CN.md`) changed the suite and was measured
separately from the round that produced the alpha itself. Both are the same version
(`0.1.0a2`, local candidate, nothing uploaded).

### The durable-commit round (2026-10-15): what was executed

| Item | Value |
| --- | --- |
| suite, Python 3.12.13 (macOS arm64, this checkout, `.venv` with all extras) | **793 passed, 1 skipped** in 32.4s (773 before this round, so +20 commit-boundary tests) |
| `scripts/ci_check.py --offline --wheelhouse build-wheelhouse` | **all six steps ok** (offline install, package tests, cold-import fence, build wheel+sdist and run the examples outside the source tree, markdown targets, provenance files) |
| the plan's target commands | `pytest -q tests/test_runtime_commit_boundaries.py` → **20 passed**; `pytest -q tests/test_runtime_execution_snapshot.py tests/test_runtime_resume.py tests/test_runtime_recovery.py tests/test_runtime_isolation.py` → **59 passed** |
| red first, green after | F01/F02 ran against the unfixed tree before any production change: F01 read `stored_execution = None` from the run store (the snapshot existed only in the child's memory), F02 raised nothing because the dispatch boundary was never committed at all. The same two tests pass after the wiring change; the other 18 were then added on the fixed tree. |
| what the fixed path persists before a tool dispatch (F01, real child) | `status=running`, `execution.phase=running_batch`, `execution.pending_work.declared=[call-1]`, `messages=[system, user, assistant(toolCall call-1)]`, `turns_used=1`, `batch=null`; the operations ledger holds one unsettled intent, and the effect file proves the handler ran |
| what a failed boundary commit does (F02) | `StorageConflict` propagates with its own `code`; handler calls 0, no effect file, no ledger row, no second model request; the record stays at `requesting_model` and declares no call |
| which calls may reuse a result (F05/F07) | a call with a gateway receipt is reused from its recorded result and never re-dispatched (one ledger row, handler count unchanged). A call the snapshot only *declared* while the batch outcome is unknown is never described as replayable: that answer needs the ledger. |
| whether a normal or resumed completion saves messages once | yes: one system message, one prompt, one result per `tool_call_id`, and results in declaration order — asserted on the store **and** on the next real provider request |
| the pause is self-consistent before the driver finishes (F06) | the status, snapshot, batch partition, interaction, transcript and events are written in one transaction, so a child killed in the pause fanout already leaves a state a second process resumes correctly (writes 0 before the decision, 1 after) |
| still not executed in this round | remote CI, Python 3.10/3.11 (no matching interpreter/wheelhouse here), Linux/Windows, a real model provider, and `scripts/verify_release.py` (no public type, persisted format or release claim changed: `RunRecord.schema_version` stays 1 and no new public API was added — only the missing wiring). |

### The state-machine round (S0–S6): what was executed

| Item | Value |
| --- | --- |
| suite, Python 3.12.13 (macOS arm64, this checkout, `.venv` with all extras) | **773 passed, 1 skipped** in 28.8s |
| suite, Python 3.11.15 (macOS arm64, throwaway venv outside the tree, base + pytest only) | **763 passed, 11 skipped** in 27.4s |
| the 11 skips on 3.11 | all `tests/test_skills_local.py` "needs the optional 'yaml' extra": the offline wheelhouse here carries a `cp312` PyYAML wheel, so the extra cannot be installed for a 3.11 interpreter without network. Recorded as a wheelhouse limitation, not as a passing row. |
| Python 3.10 | **not executed in this round**: no 3.10 interpreter is installed on this machine any more. The static floor gates still run (`tests/test_python_floor.py`: `ast.parse(feature_version=(3, 10))` plus a scan for 3.11+/3.12+/3.13+ APIs), and the per-interpreter 3.10 run recorded below belongs to the *previous* round of the same version — it did not include S0–S6. |
| `python -m pytest -q tests` (the local gate) | 773 passed, 1 skipped |
| `scripts/ci_check.py --offline --wheelhouse build-wheelhouse` | **all six steps ok** (offline install, package tests, cold-import fence, build wheel+sdist and run the examples outside the source tree, markdown targets, provenance files) |
| `scripts/verify_release.py --offline --wheelhouse build-wheelhouse --out build/release` | wheel **749 passed / 25 skipped**, sdist **749 passed / 25 skipped**; **all five examples exit 0** from the installed artifacts (including the end-to-end child-process chain); wheel 66 files, sdist 144 files |
| artifacts (this round) | built from `0793ed5` with a clean tree: `yanxu_harness-0.1.0a2-py3-none-any.whl` (66 files) and `yanxu_harness-0.1.0a2.tar.gz` (144 files). Their digests are **not** copied into this file on purpose (see below) — they travel in `build/release/SHA256SUMS`, which that run wrote. |
| report | `build/release/verification-report.md` + `SHA256SUMS`: the commit the artifacts were built from, whether the tree was clean at that moment, the per-file digests, and what was out of scope |
| phase commits | `e3599cf` (S0 characterization) · `2839ecf` (S1 outcomes) · `6a68b06` (S2 transitions) · `1e90fb4` (S3 single runner) · `ef7bf67` (S4 durable boundaries) · `1b98b18` (S5 cancellation/isolation) · `dee80ef` (S6 docs) · `ec3ecdf` (budget across a resume) · `08bb307` (acceptance matrix) · `78bf84e` (plan banner) · `d80620e` (the audit's tool-set regression and its two secondary notes) · `0793ed5` (before/after probe) |
| still not executed | remote CI (never ran: the first run was blocked externally), a real model provider (all provider tests use a loopback server), Linux and Windows (the CI matrix names them; no run has happened) |

### The alpha round (unchanged, for the same version)

| Item | Value |
| --- | --- |
| suite, Python 3.10.14 / 3.11.15 / 3.12.13 (macOS arm64, all extras) | 655 passed, 1 skipped on each; all five examples exit 0; `scripts/ci_check.py --offline` all six steps ok |
| release verification | wheel 633 passed / 23 skipped; sdist 633 passed / 23 skipped, on 3.10.14 and on 3.12.13 |
| artifacts | same two artifact names; digests were recorded in the `SHA256SUMS` of that run, and are superseded by the block above |
| phase commits | `9b16841` (standalone checks) · `6d38b47`, `8c4b142` (bridges) · `1be9e51` (harness lifecycle) · `23121ec` (durable resume) · `21db5d7` (fencing, cancellation, isolation) · `7e9388c` (end-to-end example) · `a2c352f` (adversarial windows) · `bfb2aba` (docs) · `6bd5772` (alpha prep) |

Digests stay out of this file by the same rule the alpha round used: this document is
itself part of the sdist, so copying an artifact's digest into it changes the digest of
the artifact it describes. The run's own `SHA256SUMS`, written next to the artifacts and
naming the commit, is the copy that can be checked.

The generated report speaks only for the run that produced it: it names the
interpreter and platform it ran on and states explicitly that any other Python
version or platform is outside its scope, pointing here for the per-interpreter
record. A report that claimed more than the run observed — for example "Python 3.10
was not executed because no interpreter is installed", which was hardcoded in the
first version of this generator and false on the machine that produced it — is a
defect, and `tests/test_build_scripts.py` now fails such a claim.

The extra skips under release verification are the checkout-only gates: packaging
metadata, the build scripts and the documents are not part of an installed artifact,
and those tests say so with a reason instead of passing silently.

Python 3.10 is in the alpha-round table on purpose: the declared minimum *was* executed
then, not inferred. This round could not repeat it (no 3.10 interpreter here), and says
so instead of quietly carrying the old number forward as if it still described the
current suite. The `yaml`-extra skips on the 3.11 run are the same kind of statement: a
wheelhouse built for one interpreter is not a 3.11 wheelhouse.

## Reproducing the evidence

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev,yaml,pydantic]'
python -m pytest -q tests                     # the suite, including the process tests
python examples/05_durable_approved_edit.py   # the end-to-end chain, in child processes
python scripts/ci_check.py --prepare-wheelhouse build-wheelhouse
python scripts/ci_check.py --offline --wheelhouse build-wheelhouse
python scripts/verify_release.py --offline --wheelhouse build-wheelhouse --keep
```
