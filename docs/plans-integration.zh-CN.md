# Plan 与任务 DAG：最小接入和迁移

此能力是可选编排层。Plan 控制记录、Task 调度、Run 执行、工具副作用和业务验收分别沿用各自的权威所有者。它不创建第二个模型循环，也不替代 ToolGateway 的权限判断。

## 最小接入路径

下面突出宿主必须作出的控制选择；Provider、Gateway 和 SQLite 的完整装配代码见 [`examples/15_plan_dag.py`](../examples/15_plan_dag.py) 及其可读的共享 wiring [`examples/plan_dag_support.py`](../examples/plan_dag_support.py)。不要把下方省略的 host-owned evidence reader 当成库默认值。

```python
from yanxu_harness.plans import (
    AcceptanceCriterion, ApprovalDecision, ExecutionEnvelope, PlanApprovalRequest,
    PlanDefinition, StepDefinition, VersionedRef,
)
from yanxu_harness.tasks import LocalWorker, SqliteControlStore

control = SqliteControlStore("state/control.db", enable_orchestration=True)
criterion = AcceptanceCriterion("source-read", "host.receipt", "1")
definition = PlanDefinition(
    namespace="project-a", session_id="s1", plan_id="review-1", revision=1,
    title="Review source", goal="Read and verify the selected source.",
    steps=(StepDefinition("read", "Read", "Read the approved source.", (criterion,)),),
    execution_envelope=ExecutionEnvelope(
        VersionedRef("profile/read", "1"), VersionedRef("workspace/a", "1"),
        VersionedRef("policy/read", "1"), VersionedRef("deployment/a", "1"),
        VersionedRef("budget/a", "1"),
    ),
)
record = await control.create_plan(definition, request_id="create-1", principal="host")
record = await control.request_plan_review(
    "project-a", "review-1", expected_control_version=record.control_version,
    request_id="review-1", principal="host",
)
await control.decide_plan_approval(
    "project-a", "review-1",
    PlanApprovalRequest("approval-1", ApprovalDecision.APPROVED, "policy-v1"),
    expected_control_version=record.control_version, request_id="approve-1", principal="reviewer",
)
worker = LocalWorker(
    control, harness_for, enable_orchestration=True, orchestration_namespace="project-a",
)
# evidence_reader and read-only verifiers are supplied by the host.
```

`harness_for(task, attempt_id)` should construct the same `DefaultHarness` path as a normal persistent Task: the same Core Runner, explicitly deployed tools, Gateway policy, run persistence, `control=control`, and `task_id=task.task_id`. After `worker.run_until_idle()`, the host reads committed Task/Run/operation facts into bounded `EvidenceSnapshot`s, calls `AcceptanceCoordinator.accept_step()` or `reconcile()`, and delivers pending Plan outbox entries. Only accepted criteria unlock dependents. `finalize_plan()` rechecks all current sources before marking the Plan complete.

## 完整离线示例

```sh
python examples/15_plan_dag.py
python examples/16_plan_recovery.py
```

Both use fixed offline Providers and a real SQLite control store, ToolGateway, and operation ledger. The first parses a Provider proposal before approval and runs a two-step dependency chain. The second exits a subprocess after the terminal Run commit but before Task settlement, then reopens the same store and finishes with the original binding, Task, Run, and Gateway receipt. `tests/test_examples_plans.py` executes both examples as subprocesses.

## 只读 Plan 诊断

```sh
python -m yanxu_harness.diagnostics plan \
  --control state/control.db --namespace project-a --session s1 --plan-id review-1 \
  --format markdown
```

The command opens an existing SQLite control store in read-only mode and refuses to create or migrate it. JSON is the default output. `PlanInspector` exposes committed references, dependency blockers, claims, approval coverage, budget references, recent event digests, and unknown/unsupported/inconsistent boundaries. It does not return prompts, criterion evidence payloads, operation payloads, or host quota values that are not stored in the Plan envelope.

## 分层迁移

| Existing integration | Add next | Host work required |
| --- | --- | --- |
| Ordinary provider/tool loop | `DefaultHarness` | Map provider output to neutral events; define stable run identity and persistence. |
| Single Run | Persistent Task | Use `SqliteControlStore` or a contract-compatible backend; supply explicit Task identity and `LocalWorker`; preserve Gateway policy. |
| Persistent Task | Optional Plan/DAG | Opt in with `enable_orchestration=True`; construct and validate Plan data; authenticate review/approval actors; explicitly enable one namespace per Worker. |
| Plan/DAG | Acceptance and recovery | Supply read-only evidence collection and verifiers; call `reconcile` and deliver outbox notices; choose pause/retry/replan actions. |

Each layer remains independently usable. Ordinary Run and Task paths do not need Plan tables or a Plan-aware Worker. The reference `MemoryControlStore` is volatile; it is useful for tests and in-process hosts, not a cross-process recovery claim. No interface in this release starts a background daemon or chooses product tools, workspaces, shell commands, or business acceptance rules.
