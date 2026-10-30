from __future__ import annotations

import hashlib
import json
import time
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from child_process import child_json, run_child

from yanxu_harness.plans import (
    AcceptanceCriterion,
    AcceptanceResult,
    ApprovalDecision,
    ExecutionEnvelope,
    PlanApprovalRequest,
    PlanDefinition,
    PlanRefused,
    PlanStepCandidate,
    PlanStatus,
    StepDefinition,
    VersionedRef,
)
from yanxu_harness.plans.acceptance import (
    AcceptanceCoordinator,
    EvidenceSnapshot,
    VerificationDecision,
    VerifierRegistry,
)
from yanxu_harness.tasks.inbox import MemoryControlStore, SqliteControlStore
from yanxu_harness.tasks.scheduler import LocalWorker
from yanxu_harness.tasks.types import TaskStatus
from yanxu_harness.runtime.default_harness import DefaultHarness


def _definition(revision: int = 1, *, reason: str = "") -> PlanDefinition:
    return PlanDefinition(
        namespace="ns",
        session_id="session",
        plan_id="replan-plan",
        revision=revision,
        title="Replan safely",
        goal="Wait for all old execution to settle before publishing a revision",
        created_by="host",
        supersedes_revision=revision - 1 if revision > 1 else None,
        replan_reason=reason,
        steps=(
            StepDefinition(
                "first",
                "First",
                "Do the first step",
                (AcceptanceCriterion("done", "host.result", "1"),),
            ),
        ),
        execution_envelope=ExecutionEnvelope(
            profile_ref=VersionedRef("profile/default", "1"),
            workspace_ref=VersionedRef("workspace/main", "v1"),
            policy_ref=VersionedRef("policy/default", "1"),
            deployment_ref=VersionedRef("deployment/local", "1"),
            budget_ref=VersionedRef("budget/default", "1"),
        ),
    )


def _dag_definition(plan_id: str, *, namespace: str = "ns", session_id: str = "session") -> PlanDefinition:
    criterion = (AcceptanceCriterion("done", "host.result", "1"),)
    return PlanDefinition(
        namespace=namespace,
        session_id=session_id,
        plan_id=plan_id,
        revision=1,
        title="Crash window DAG",
        goal="Recover committed facts across a real process exit",
        created_by="host",
        steps=(
            StepDefinition("first", "First", "Perform the first step", criterion),
            StepDefinition(
                "second", "Second", "Continue after verified acceptance", criterion,
                depends_on=("first",),
            ),
        ),
        execution_envelope=ExecutionEnvelope(
            profile_ref=VersionedRef("profile/default", "1"),
            workspace_ref=VersionedRef("workspace/main", "v1"),
            policy_ref=VersionedRef("policy/default", "1"),
            deployment_ref=VersionedRef("deployment/local", "1"),
            budget_ref=VersionedRef("budget/default", "1"),
        ),
    )


async def _approve_dag(control, plan_id: str, *, namespace: str = "ns", session_id: str = "session"):
    definition = _dag_definition(plan_id, namespace=namespace, session_id=session_id)
    record = await control.create_plan(
        definition, request_id=f"create-{plan_id}", principal="host", now=100.0
    )
    record = await control.request_plan_review(
        namespace, plan_id, expected_control_version=record.control_version,
        request_id=f"review-{plan_id}", principal="host", now=101.0,
    )
    return await control.decide_plan_approval(
        namespace,
        plan_id,
        PlanApprovalRequest(
            decision_id=f"approval-{plan_id}",
            decision=ApprovalDecision.APPROVED,
            approval_policy_version="policy-v1",
        ),
        expected_control_version=record.control_version,
        request_id=f"approve-{plan_id}",
        principal="reviewer",
        now_ms=102_000,
    )


async def _claim_dag_first(control, plan_id: str, *, namespace: str = "ns"):
    current = await control.read_plan(namespace, plan_id)
    candidate = (await control.read_ready_steps(namespace, limit=8, now=103.0)).candidates[0]
    assert candidate.step_id == "first"
    identity = {
        "version": 1,
        "namespace": candidate.namespace,
        "plan_id": candidate.plan_id,
        "revision": candidate.revision,
        "step_id": candidate.step_id,
    }
    request_id = "worker-step-" + hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return await control.claim_plan_step(
        candidate,
        expected_control_version=current.record.control_version,
        request_id=request_id,
        principal="host",
        holder="worker-a",
        ttl_seconds=30.0,
        now=103.0,
    )


@pytest.fixture(params=("memory", "sqlite"))
def control(request: pytest.FixtureRequest, tmp_path):
    if request.param == "memory":
        store = MemoryControlStore(enable_orchestration=True)
    else:
        store = SqliteControlStore(tmp_path / "plans-replanning.sqlite", enable_orchestration=True)
    yield store
    close = getattr(store, "close", None)
    if callable(close):
        close()


async def _claimed(control):
    record = await control.create_plan(
        _definition(), request_id="create-replan", principal="host", now=100.0
    )
    record = await control.request_plan_review(
        "ns", "replan-plan", expected_control_version=record.control_version,
        request_id="review-replan", principal="host", now=101.0,
    )
    record = await control.decide_plan_approval(
        "ns",
        "replan-plan",
        PlanApprovalRequest(
            decision_id="approval-replan",
            decision=ApprovalDecision.APPROVED,
            approval_policy_version="policy-v1",
        ),
        expected_control_version=record.control_version,
        request_id="approve-replan",
        principal="reviewer",
        now_ms=102_000,
    )
    candidate = (await control.read_ready_steps("ns", limit=8, now=103.0)).candidates[0]
    return await control.claim_plan_step(
        candidate,
        expected_control_version=record.control_version,
        request_id="claim-replan",
        principal="host",
        holder="worker-a",
        ttl_seconds=30,
        now=103.0,
    )


async def _complete(control, run_store, claim, *, status: str = "completed") -> None:
    task = await control.read_task(claim.task.task_id)
    moved = task.transition(TaskStatus.COMPLETED, reason="work_completed", at=104.0)
    await control.save_task(moved, expected_status=task.status)
    await control.commit_run_transition_with_inputs(
        claim.binding.run_id,
        {
            "run_id": claim.binding.run_id,
            "namespace": "ns",
            "session_id": "session",
            "status": status,
            "final_text": "settled",
        },
        expected_version=None,
        task_id=claim.task.task_id,
    )


def _coordinator(control, run_store, *, unresolved: tuple[str, ...] = (), cancel_task=None):
    async def evidence(binding, task, run):
        return EvidenceSnapshot(
            namespace=binding.namespace,
            plan_id=binding.plan_id,
            revision=binding.revision,
            step_id=binding.step_id,
            binding_id=binding.binding_id,
            task_id=task.task_id,
            run_id=run.run_id,
            run_version=run.version,
            evidence_refs=(f"artifact:{binding.step_id}@v1",),
            payload={"result": "done"},
            unresolved_operations=unresolved,
        )

    verifiers = VerifierRegistry()

    async def verify(_criterion, _snapshot):
        return VerificationDecision(AcceptanceResult.ACCEPTED)

    verifiers.register("host.result", "1", verify, timeout_seconds=1, max_input_bytes=4096)
    return AcceptanceCoordinator(
        control,
        evidence,
        verifiers,
        run_store=control,
        cancel_task=cancel_task,
    )


@pytest.mark.asyncio
async def test_replan_closes_admission_and_atomically_records_active_cancel_targets(control) -> None:
    claim = await _claimed(control)
    before = await control.read_plan("ns", "replan-plan")

    replanning = await control.request_replan(
        "ns",
        "replan-plan",
        reason="scope changed",
        expected_control_version=before.record.control_version,
        request_id="replan-request",
        principal="host",
        now=105.0,
    )
    replay = await control.request_replan(
        "ns",
        "replan-plan",
        reason="scope changed",
        expected_control_version=before.record.control_version,
        request_id="replan-request",
        principal="host",
        now=999.0,
    )
    task = await control.read_task(claim.task.task_id)
    event = (await control.read_plan_events("ns", "replan-plan"))[-1]

    assert replanning.status is PlanStatus.REPLANNING
    assert replay == replanning
    assert task.cancel_requested_at == 105.0
    assert event.event_type == "replan_requested"
    assert event.payload["binding_ids"] == (claim.binding.binding_id,)
    assert event.payload["task_ids"] == (claim.task.task_id,)
    assert (await control.read_ready_steps("ns", limit=8, now=106.0)).candidates == ()
    with pytest.raises(PlanRefused) as active:
        await control.publish_plan_revision(
            _definition(2, reason="scope changed"),
            expected_control_version=replanning.control_version,
            request_id="publish-too-early",
            principal="host",
            now=107.0,
        )
    assert active.value.reason_code == "active_binding"


@pytest.mark.asyncio
async def test_cancellation_recovery_claim_waits_for_old_fence_and_keeps_binding(control) -> None:
    original = await _claimed(control)
    plan = await control.read_plan("ns", "replan-plan")
    await control.request_replan(
        "ns", "replan-plan", reason="scope changed",
        expected_control_version=plan.record.control_version,
        request_id="replan-recovery-claim", principal="host", now=105.0,
    )

    held = await control.claim_plan_cancellation(
        original.task.task_id, holder="worker-b", ttl_seconds=30, now=132.9
    )
    recovered = await control.claim_plan_cancellation(
        original.task.task_id, holder="worker-b", ttl_seconds=30, now=133.0
    )

    assert held is None
    assert recovered is not None
    assert recovered.binding.binding_id == original.binding.binding_id
    assert recovered.binding.task_id == original.task.task_id
    assert recovered.binding.run_id == original.binding.run_id
    assert recovered.task.cancel_requested_at == 105.0
    assert recovered.claim.fencing_token > original.claim.fencing_token
    assert (await control.read_plan("ns", "replan-plan")).record.status is PlanStatus.REPLANNING


@pytest.mark.asyncio
async def test_reconcile_retries_and_delivers_durable_cancellation_outbox(control) -> None:
    claim = await _claimed(control)
    plan = await control.read_plan("ns", "replan-plan")
    await control.request_plan_cancel(
        "ns", "replan-plan", reason="operator stopped the plan",
        expected_control_version=plan.record.control_version,
        request_id="cancel-outbox", principal="host", now=105.0,
    )

    async def fail_dispatch(_task_id: str, *, reason: str) -> None:
        raise RuntimeError("worker unavailable")

    failed = await _coordinator(control, control, cancel_task=fail_dispatch).reconcile(
        "ns", "replan-plan", limit=20, principal="host", now=106.0
    )
    entries = await control.read_plan_outbox(
        pending_only=False, limit=100, namespace="ns", plan_id="replan-plan"
    )
    cancellation = next(item for item in entries if item.kind == "cancel_requested")

    assert failed.pending == (claim.binding.binding_id,)
    assert cancellation.delivered_at is None
    assert cancellation.attempts == 1
    assert cancellation.last_error == "cancel_dispatch_failed"

    delivered_to: list[tuple[str, str]] = []

    async def dispatch(task_id: str, *, reason: str) -> None:
        delivered_to.append((task_id, reason))

    succeeded = await _coordinator(control, control, cancel_task=dispatch).reconcile(
        "ns", "replan-plan", limit=20, principal="host", now=107.0
    )
    entries = await control.read_plan_outbox(
        pending_only=False, limit=100, namespace="ns", plan_id="replan-plan"
    )
    cancellation = next(item for item in entries if item.kind == "cancel_requested")

    assert succeeded.pending == (claim.binding.binding_id,)
    assert delivered_to == [(claim.task.task_id, "operator stopped the plan")]
    assert cancellation.delivered_at == 107.0
    assert cancellation.attempts == 2


@pytest.mark.asyncio
async def test_worker_recovers_plan_cancellation_before_run_start_without_dispatch(control) -> None:
    claim = await _claimed(control)
    plan = await control.read_plan("ns", "replan-plan")
    await control.request_replan(
        "ns", "replan-plan", reason="scope changed",
        expected_control_version=plan.record.control_version,
        request_id="replan-before-run", principal="host", now=105.0,
    )
    dispatched: list[Any] = []

    async def stream_fn(*_args: Any, **_kwargs: Any) -> None:
        dispatched.append("provider")

    run_store = control

    def harness_for(task, attempt_id: str):
        return DefaultHarness(
            stream_fn=stream_fn,
            persistence=control,
            control=control,
            task_id=task.task_id,
            holder=attempt_id,
            clock=time.time,
        )

    worker = LocalWorker(
        control,
        harness_for,
        holder="worker-b",
        principal="host",
        clock=time.time,
        enable_orchestration=True,
        orchestration_namespace="ns",
    )
    settled = await worker.cancel(claim.task.task_id, reason="scope changed")
    run = await control.load_run(claim.binding.run_id)

    assert settled is not None and settled.status is TaskStatus.CANCELLED
    assert run is not None and run.state["status"] == "cancelled"
    assert dispatched == []

    reconciler = _coordinator(control, run_store)
    report = await reconciler.reconcile(
        "ns", "replan-plan", limit=20, principal="host", now=135.0
    )
    current = await control.read_plan("ns", "replan-plan")
    assert report.quiesced == 1
    revised = await control.publish_plan_revision(
        _definition(2, reason="scope changed"),
        expected_control_version=current.record.control_version,
        request_id="publish-after-cancel-recovery",
        principal="host",
        now=136.0,
    )
    assert revised.status is PlanStatus.DRAFT
    assert revised.current_revision == 2


@pytest.mark.asyncio
async def test_real_process_exit_after_plan_admission_recovers_same_cancelled_run(tmp_path) -> None:
    database = tmp_path / "plan-crash-window.sqlite"
    child = run_child(
        [
            "plan-replan-before-run-die",
            str(database),
            str(tmp_path / "unused-marker"),
            str(tmp_path / "unused-counter"),
        ]
    )

    assert child.returncode == 9, child.stderr[-4000:]
    identity = child_json(child)
    dispatched: list[str] = []
    control = SqliteControlStore(database, enable_orchestration=True)
    try:
        async def stream_fn(*_args: Any, **_kwargs: Any) -> None:
            dispatched.append("provider")
            raise AssertionError("pre-run cancellation recovery must not dispatch")

        def harness_for(task, attempt_id: str):
            return DefaultHarness(
                stream_fn=stream_fn,
                persistence=control,
                control=control,
                task_id=task.task_id,
                holder=attempt_id,
                clock=time.time,
            )

        worker = LocalWorker(
            control,
            harness_for,
            holder="recovery-worker",
            principal="host",
            clock=time.time,
            enable_orchestration=True,
            orchestration_namespace="ns",
        )
        task = await worker.cancel(identity["task_id"], reason="scope changed")
        run = await control.load_run(identity["run_id"])
        bindings = await control.read_plan_bindings("ns", "replan-plan")

        assert task is not None and task.status is TaskStatus.CANCELLED
        assert run is not None and run.state["status"] == "cancelled"
        assert dispatched == []
        assert [(item.binding_id, item.task_id, item.run_id) for item in bindings] == [
            (identity["binding_id"], identity["task_id"], identity["run_id"])
        ]
        report = await _coordinator(control, control).reconcile(
            "ns", "replan-plan", limit=20, principal="host", now=time.time()
        )
        assert report.quiesced == 1
        current = await control.read_plan("ns", "replan-plan")
        revised = await control.publish_plan_revision(
            _definition(2, reason="scope changed"),
            expected_control_version=current.record.control_version,
            request_id="publish-after-real-crash-recovery",
            principal="host",
            now=time.time(),
        )
        assert revised.current_revision == 2
    finally:
        control.close()


@pytest.mark.asyncio
async def test_pa20_real_process_terminal_run_is_settled_and_accepted_after_restart(tmp_path) -> None:
    database = tmp_path / "pa20-terminal-run.sqlite"
    control = SqliteControlStore(database, enable_orchestration=True)
    try:
        await _approve_dag(control, "pa20-plan")
        original = await _claim_dag_first(control, "pa20-plan")
    finally:
        control.close()

    child = run_child(
        ["plan-terminal-run-before-task-die", str(database), "unused", "unused"]
    )
    assert child.returncode == 9
    assert child_json(child)["run_id"] == original.binding.run_id

    control = SqliteControlStore(database, enable_orchestration=True)
    try:
        task_before = await control.read_task(original.task.task_id)
        run_before = await control.load_run(original.binding.run_id)
        assert task_before is not None and task_before.status is TaskStatus.RUNNING
        assert run_before is not None and run_before.state["status"] == "completed"
        assert (await control.read_plan_bindings("ns", "pa20-plan"))[0].binding_status == "admitted"

        def harness_for(_task, _attempt_id: str):
            # The worker only needs the real persistence read to classify a committed
            # terminal Run; a provider or model loop must not start on this path.
            return SimpleNamespace(_persistence=control)

        worker = LocalWorker(
            control,
            harness_for,
            holder="pa20-recovery-worker",
            principal="host",
            clock=lambda: 134.0,
            enable_orchestration=True,
            orchestration_namespace="ns",
        )
        settled = await worker.run_once(now=134.0)
        task_after = await control.read_task(original.task.task_id)
        assert settled is not None and settled.outcome == "settled:completed"
        assert task_after is not None and task_after.status is TaskStatus.COMPLETED

        acceptance = await _coordinator(control, control).reconcile(
            "ns", "pa20-plan", limit=20, principal="host", now=135.0
        )
        assert acceptance.accepted == 1
        assert len(await control.read_plan_acceptances("ns", "pa20-plan", revision=1)) == 1
        ready = await control.read_ready_steps("ns", limit=8, now=136.0)
        assert [item.step_id for item in ready.candidates] == ["second"]
        bindings = await control.read_plan_bindings("ns", "pa20-plan", 1)
        assert [(item.binding_id, item.task_id, item.run_id) for item in bindings[:1]] == [
            (original.binding.binding_id, original.task.task_id, original.binding.run_id)
        ]
    finally:
        control.close()


@pytest.mark.asyncio
async def test_pa21_real_process_acceptance_outbox_is_consumed_once_after_restart(tmp_path) -> None:
    database = tmp_path / "pa21-acceptance-outbox.sqlite"
    control = SqliteControlStore(database, enable_orchestration=True)
    try:
        await _approve_dag(control, "pa21-plan")
        original = await _claim_dag_first(control, "pa21-plan")
        await _complete(control, control, original)
    finally:
        control.close()

    child = run_child(
        ["plan-acceptance-before-notice-consume-die", str(database), "unused", "unused"]
    )
    assert child.returncode == 9
    accepted = child_json(child)
    assert accepted["results"] == ["accepted"]

    control = SqliteControlStore(database, enable_orchestration=True)
    try:
        records = await control.read_plan_acceptances(
            "ns", "pa21-plan", revision=1, binding_id=original.binding.binding_id
        )
        assert len(records) == 1
        assert records[0].acceptance_id == accepted["acceptance_ids"][0]
        notices = await control.read_plan_outbox(
            pending_only=True, limit=100, namespace="ns", plan_id="pa21-plan"
        )
        acceptance_notice = next(item for item in notices if item.kind == "step_accepted")
        assert await control.deliver_plan_outbox(acceptance_notice.outbox_id, at=106.0) is True
        assert await control.deliver_plan_outbox(acceptance_notice.outbox_id, at=107.0) is False

        ready = await control.read_ready_steps("ns", limit=8, now=108.0)
        assert [item.step_id for item in ready.candidates] == ["second"]
        plan = await control.read_plan("ns", "pa21-plan")
        second = await control.claim_plan_step(
            ready.candidates[0],
            expected_control_version=plan.record.control_version,
            request_id="pa21-downstream-admission",
            principal="host",
            holder="recovery-worker",
            ttl_seconds=30.0,
            now=108.0,
        )
        replay = await control.claim_plan_step(
            ready.candidates[0],
            expected_control_version=plan.record.control_version,
            request_id="pa21-downstream-admission",
            principal="host",
            holder="recovery-worker",
            ttl_seconds=30.0,
            now=109.0,
        )
        assert second.binding.binding_id == replay.binding.binding_id
        assert second.task.task_id == replay.task.task_id
        bindings = await control.read_plan_bindings("ns", "pa21-plan", 1)
        assert len([item for item in bindings if item.step_id == "second"]) == 1
        events = await control.read_plan_events("ns", "pa21-plan")
        assert sum(item.event_type == "step_accepted" for item in events) == 1
    finally:
        control.close()


@pytest.mark.asyncio
async def test_pa22_real_gateway_effect_remains_unknown_and_is_not_run_again(tmp_path) -> None:
    from yanxu_harness.runtime import Principal, RunContext, RunIdentity
    from yanxu_harness.storage import SqliteExecutionBackend
    from yanxu_harness.tools import (
        ConservativePolicy,
        EffectState,
        InMemoryApprovalStore,
        InvocationStatus,
        ToolDefinition,
        ToolEffect,
        ToolGateway,
        ToolInvocation,
        ToolRegistry,
    )

    database = tmp_path / "pa22-control.sqlite"
    operations_path = tmp_path / "pa22-operations.sqlite"
    effects_path = tmp_path / "pa22-effects.txt"
    control = SqliteControlStore(database, enable_orchestration=True)
    try:
        await _approve_dag(control, "pa22-plan", namespace="project-a", session_id="s1")
    finally:
        control.close()

    child = run_child(
        [
            "plan-tool-effect-before-receipt-die",
            str(database),
            str(operations_path),
            str(effects_path),
        ]
    )
    assert child.returncode == 9, child.stderr[-4000:]
    assert effects_path.read_text(encoding="utf-8").splitlines() == ["pa22-output.txt"]

    control = SqliteControlStore(database, enable_orchestration=True)
    backend = SqliteExecutionBackend(operations_path)
    try:
        bindings = await control.read_plan_bindings("project-a", "pa22-plan", 1)
        assert len(bindings) == 1
        binding = bindings[0]
        task = await control.read_task(binding.task_id)
        run = await control.load_run(binding.run_id)
        assert task is not None and task.status is TaskStatus.RUNNING
        assert run is not None and run.state["status"] == "running"
        unsettled = backend.unsettled(run_id=binding.run_id)
        assert len(unsettled) == 1
        assert backend.receipt(unsettled[0].operation_id) is None

        def handler(_context, arguments):
            with effects_path.open("a", encoding="utf-8") as handle:
                handle.write(f"{arguments['path']}\n")
            return {"written": arguments["path"]}

        definition = ToolDefinition(
            "notes.write",
            "write one approved note",
            effects=frozenset({ToolEffect.WRITE}),
            args_validator=lambda arguments: {"path": arguments["path"]},
            handler=handler,
            preview=lambda _context, arguments: {"path": arguments["path"]},
        )
        gateway = ToolGateway(
            registry=ToolRegistry([definition]),
            policy=ConservativePolicy(auto_write=True),
            approvals=InMemoryApprovalStore(),
            backend=backend,
        )
        context = RunContext(
            identity=RunIdentity(
                namespace="project-a",
                session_id="s1",
                run_id=binding.run_id,
                attempt_id="recovery",
            ),
            principal=Principal(subject="host"),
        )
        result = await gateway.invoke(
            ToolInvocation(
                call_id="pa22-call",
                namespace="project-a",
                tool_name="notes.write",
                arguments={"path": "pa22-output.txt"},
                session_id="s1",
                run_id=binding.run_id,
                operation_id=unsettled[0].operation_id,
            ),
            context,
        )
        assert result.status is InvocationStatus.UNKNOWN
        assert result.receipt is not None and result.receipt.state is EffectState.UNKNOWN
        assert "not run again" in result.receipt.detail
        assert effects_path.read_text(encoding="utf-8").splitlines() == ["pa22-output.txt"]
        assert (await control.read_plan("project-a", "pa22-plan")).record.status is PlanStatus.EXECUTING
        assert (await control.read_plan_acceptances("project-a", "pa22-plan", revision=1)) == ()
        ready = await control.read_ready_steps("project-a", limit=8, now=200.0)
        assert all(item.step_id != "second" for item in ready.candidates)
    finally:
        backend.close()
        control.close()


@pytest.mark.asyncio
async def test_reconcile_settles_terminal_run_before_replan_can_publish(control) -> None:
    run_store = control
    claim = await _claimed(control)
    plan = await control.read_plan("ns", "replan-plan")
    await control.request_replan(
        "ns", "replan-plan", reason="scope changed",
        expected_control_version=plan.record.control_version,
        request_id="replan-before-complete", principal="host", now=105.0,
    )
    await _complete(control, run_store, claim)
    reconciler = _coordinator(control, run_store)

    report = await reconciler.reconcile(
        "ns", "replan-plan", limit=20, principal="host", now=106.0
    )
    current = await control.read_plan("ns", "replan-plan")
    bindings = await control.read_plan_bindings("ns", "replan-plan")

    assert report.quiesced == 1
    assert report.pending == ()
    assert bindings[0].binding_status == "settled_completed"
    assert current.record.status is PlanStatus.REPLANNING
    revised = await control.publish_plan_revision(
        _definition(2, reason="scope changed"),
        expected_control_version=current.record.control_version,
        request_id="publish-after-quiescence",
        principal="host",
        now=107.0,
    )
    assert revised.status is PlanStatus.DRAFT
    assert revised.current_revision == 2


@pytest.mark.asyncio
async def test_unknown_operation_keeps_old_revision_active_and_unpublishable(control) -> None:
    run_store = control
    claim = await _claimed(control)
    plan = await control.read_plan("ns", "replan-plan")
    replanning = await control.request_replan(
        "ns", "replan-plan", reason="scope changed",
        expected_control_version=plan.record.control_version,
        request_id="replan-unknown", principal="host", now=105.0,
    )
    await _complete(control, run_store, claim)
    reconciler = _coordinator(control, run_store, unresolved=("operation-unknown",))

    report = await reconciler.reconcile(
        "ns", "replan-plan", limit=20, principal="host", now=106.0
    )

    assert report.quiesced == 0
    assert report.pending == (claim.binding.binding_id,)
    with pytest.raises(PlanRefused) as active:
        await control.publish_plan_revision(
            _definition(2, reason="scope changed"),
            expected_control_version=replanning.control_version,
            request_id="publish-with-unknown",
            principal="host",
            now=107.0,
        )
    assert active.value.reason_code == "active_binding"


@pytest.mark.asyncio
async def test_step_execution_limit_is_shared_across_published_revisions(control) -> None:
    source = _dag_definition("global-execution-limit")
    original = replace(source, steps=(source.steps[0],), max_step_executions=1)
    record = await control.create_plan(
        original, request_id="create-global-limit", principal="host", now=100.0
    )
    record = await control.request_plan_review(
        "ns", original.plan_id, expected_control_version=record.control_version,
        request_id="review-global-limit", principal="host", now=101.0,
    )
    record = await control.decide_plan_approval(
        "ns", original.plan_id,
        PlanApprovalRequest("approval-global-limit-1", ApprovalDecision.APPROVED, "policy-v1"),
        expected_control_version=record.control_version,
        request_id="approve-global-limit-1", principal="reviewer", now_ms=102_000,
    )
    first_candidate = (await control.read_ready_steps("ns", limit=8, now=103.0)).candidates[0]
    first = await control.claim_plan_step(
        first_candidate,
        expected_control_version=record.control_version,
        request_id="claim-global-limit-1", principal="host", holder="worker-1",
        ttl_seconds=30, now=103.0,
    )
    plan = await control.read_plan("ns", original.plan_id)
    await control.request_replan(
        "ns", original.plan_id, reason="publish a new revision",
        expected_control_version=plan.record.control_version,
        request_id="replan-global-limit", principal="host", now=104.0,
    )
    await _complete(control, control, first)
    settled = await _coordinator(control, control).reconcile(
        "ns", original.plan_id, principal="host", now=106.0
    )
    assert settled.quiesced == 1
    current = await control.read_plan("ns", original.plan_id)
    revision_two = replace(
        original, revision=2, supersedes_revision=1,
        replan_reason="publish a new revision", title="Execution limit still applies",
    )
    current = await control.publish_plan_revision(
        revision_two, expected_control_version=current.record.control_version,
        request_id="publish-global-limit-2", principal="host", now=107.0,
    )
    current = await control.request_plan_review(
        "ns", original.plan_id, expected_control_version=current.control_version,
        request_id="review-global-limit-2", principal="host", now=108.0,
    )
    current = await control.decide_plan_approval(
        "ns", original.plan_id,
        PlanApprovalRequest("approval-global-limit-2", ApprovalDecision.APPROVED, "policy-v2"),
        expected_control_version=current.control_version,
        request_id="approve-global-limit-2", principal="reviewer", now_ms=109_000,
    )
    candidate = PlanStepCandidate(
        namespace="ns", plan_id=original.plan_id, revision=2, step_id="first",
        priority=0, ready_since=109.0, control_version=current.control_version,
        definition_digest=current.definition_digest,
    )

    with pytest.raises(PlanRefused) as exhausted:
        await control.claim_plan_step(
            candidate, expected_control_version=current.control_version,
            request_id="claim-global-limit-2", principal="host", holder="worker-2",
            ttl_seconds=30, now=110.0,
        )

    assert exhausted.value.reason_code == "execution_limit"
    assert len(await control.read_plan_bindings("ns", original.plan_id)) == 1
    assert await control.read_plan_bindings("ns", original.plan_id, revision=2) == ()


@pytest.mark.asyncio
async def test_cancel_plan_stays_cancelling_until_task_and_run_are_settled(control) -> None:
    run_store = control
    claim = await _claimed(control)
    plan = await control.read_plan("ns", "replan-plan")
    cancelling = await control.request_plan_cancel(
        "ns", "replan-plan", reason="operator stopped the plan",
        expected_control_version=plan.record.control_version,
        request_id="cancel-plan",
        principal="host",
        now=105.0,
    )
    called: list[tuple[str, str]] = []

    async def dispatch(task_id: str, *, reason: str) -> None:
        called.append((task_id, reason))

    reconciler = _coordinator(control, run_store, cancel_task=dispatch)
    first = await reconciler.reconcile(
        "ns", "replan-plan", limit=20, principal="host", now=106.0
    )

    assert cancelling.status is PlanStatus.CANCELLING
    assert first.pending == (claim.binding.binding_id,)
    assert called == [(claim.task.task_id, "operator stopped the plan")]
    assert (await control.read_plan("ns", "replan-plan")).record.status is PlanStatus.CANCELLING

    await _complete(control, run_store, claim)
    second = await reconciler.reconcile(
        "ns", "replan-plan", limit=20, principal="host", now=107.0
    )
    current = await control.read_plan("ns", "replan-plan")

    assert second.quiesced == 1
    assert second.pending == ()
    assert current.record.status is PlanStatus.CANCELLED
    assert called == [(claim.task.task_id, "operator stopped the plan")]

