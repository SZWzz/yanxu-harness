from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from yanxu_harness.errors import ContractError

from yanxu_harness.plans import (
    AcceptanceCriterion,
    ApprovalDecision,
    ExecutionEnvelope,
    PlanApprovalRequest,
    PlanDefinition,
    PlanRefused,
    StepDefinition,
    VersionedRef,
)
from yanxu_harness.tasks.inbox import MemoryControlStore, SqliteControlStore
from yanxu_harness.tasks.scheduler import LocalWorker
from yanxu_harness.tasks.types import (
    BacklogLimits,
    InputContent,
    InputKind,
    TaskAdmission,
    TaskStatus,
)


def _definition(*, plan_id: str = "scheduler-plan") -> PlanDefinition:
    return PlanDefinition(
        namespace="ns",
        session_id="session",
        plan_id=plan_id,
        revision=1,
        title="Scheduler plan",
        goal="Run an approved step",
        created_by="host",
        steps=(
            StepDefinition(
                step_id="first",
                title="First step",
                instruction="Do the approved work",
                acceptance_criteria=(AcceptanceCriterion("done", "host.result", "1"),),
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


@pytest.fixture(params=("memory", "sqlite"))
def control(request: pytest.FixtureRequest, tmp_path):
    if request.param == "memory":
        store = MemoryControlStore(enable_orchestration=True)
    else:
        store = SqliteControlStore(tmp_path / "plans-scheduler.sqlite", enable_orchestration=True)
    yield store
    close = getattr(store, "close", None)
    if callable(close):
        close()


async def _approve(
    store,
    definition: PlanDefinition | None = None,
    *,
    expires_at_ms: int | None = None,
):
    definition = definition or _definition()
    record = await store.create_plan(
        definition, request_id=f"create-{definition.plan_id}", principal="host", now=100.0
    )
    record = await store.request_plan_review(
        definition.namespace, definition.plan_id, expected_control_version=record.control_version,
        request_id=f"review-{definition.plan_id}", principal="host", now=101.0,
    )
    return await store.decide_plan_approval(
        definition.namespace, definition.plan_id,
        PlanApprovalRequest(
            decision_id="approved-v1", decision=ApprovalDecision.APPROVED,
            approval_policy_version="policy-v1", expires_at_ms=expires_at_ms,
        ),
        expected_control_version=record.control_version,
        request_id=f"approve-{definition.plan_id}", principal="reviewer", now_ms=102_000,
    )


@pytest.mark.asyncio
async def test_ready_step_is_claimed_atomically_and_hidden_from_ordinary_worker(control) -> None:
    approved = await _approve(control)
    page = await control.read_ready_steps("ns", limit=8, now=103.0)

    assert len(page.candidates) == 1
    candidate = page.candidates[0]
    assert (candidate.namespace, candidate.plan_id, candidate.revision, candidate.step_id) == (
        "ns", "scheduler-plan", 1, "first"
    )
    assert candidate.ready_since == 102.0
    assert candidate.control_version == approved.control_version
    assert page.next_cursor is None

    result = await control.claim_plan_step(
        candidate,
        expected_control_version=approved.control_version,
        request_id="claim-first",
        principal="worker-host",
        holder="worker-1",
        ttl_seconds=30,
        now=103.0,
    )
    assert result.task.status is TaskStatus.RUNNING
    assert result.task.profile == "profile/default"
    assert result.task.workspace_ref == "workspace/main"
    assert result.claim.task_id == result.task.task_id
    inputs = await control.read_inputs(result.task.task_id)
    assert len(inputs) == 1
    assert inputs[0].kind is InputKind.INITIAL
    assert "Do the approved work" in inputs[0].content.text
    assert inputs[0].content.parameters["plan_step"] == {
        "plan_id": "scheduler-plan",
        "revision": 1,
        "step_id": "first",
    }
    assert inputs[0].content.parameters["execution_envelope"]["workspace_ref"]["ref"] == "workspace/main"

    assert await control.read_schedulable(limit=10, now=104.0) == ()
    with pytest.raises(PlanRefused) as bypass:
        await control.claim_task(
            result.task.task_id, holder="old-worker", ttl_seconds=30, now=104.0
        )
    assert bypass.value.reason_code == "execution_denied"


@pytest.mark.asyncio
async def test_claim_retry_reuses_binding_task_run_and_initial_input(control) -> None:
    approved = await _approve(control)
    candidate = (await control.read_ready_steps("ns", limit=8, now=103.0)).candidates[0]
    first = await control.claim_plan_step(
        candidate, expected_control_version=approved.control_version,
        request_id="claim-first", principal="worker-host", holder="worker-1",
        ttl_seconds=30, now=103.0,
    )
    repeated = await control.claim_plan_step(
        candidate, expected_control_version=approved.control_version,
        request_id="claim-first", principal="worker-host", holder="worker-1",
        ttl_seconds=30, now=104.0,
    )

    assert repeated.binding.binding_id == first.binding.binding_id
    assert repeated.task.task_id == first.task.task_id
    assert repeated.task.run_id == first.task.run_id
    assert repeated.claim.fencing_token == first.claim.fencing_token
    assert len(await control.read_inputs(first.task.task_id)) == 1


@pytest.mark.asyncio
async def test_only_enabled_worker_drives_ready_plan_steps_through_harness(control) -> None:
    await _approve(control)
    calls: list[tuple[str, str]] = []

    class Harness:
        async def start(self, request, context):
            del request, context
            return SimpleNamespace(status=SimpleNamespace(value="completed"), stop_code="")

    def harness_for(task, attempt_id):
        calls.append((task.task_id, attempt_id))
        return Harness()

    old_worker = LocalWorker(control, harness_for, holder="old", enable_orchestration=False)
    assert await old_worker.run_once(now=103.0) is None
    assert calls == []

    worker = LocalWorker(
        control,
        harness_for,
        holder="plan-worker",
        enable_orchestration=True,
        orchestration_namespace="ns",
    )
    result = await worker.run_once(now=104.0)

    assert result is not None
    assert result.outcome == "settled:completed"
    assert result.run_id.startswith("plan-run-")
    assert calls == [(result.task_id, result.task_id + "-a1")]
    assert await control.read_schedulable(limit=8, now=105.0) == ()


@pytest.mark.asyncio
async def test_ready_step_reads_are_namespace_scoped_and_worker_requires_explicit_scope(control) -> None:
    await _approve(control)

    assert (await control.read_ready_steps("other-namespace", limit=8, now=103.0)).candidates == ()
    with pytest.raises(ContractError, match="explicit namespace"):
        LocalWorker(control, lambda _task, _attempt: None, enable_orchestration=True)


@pytest.mark.asyncio
async def test_blocked_step_does_not_consume_ready_candidate_limit(control) -> None:
    definition = replace(
        _definition(),
        steps=(
            StepDefinition(
                step_id="a-blocked",
                title="Wait for predecessor",
                instruction="Do not run yet",
                depends_on=("z-ready",),
                acceptance_criteria=(AcceptanceCriterion("done-a", "host.result", "1"),),
            ),
            StepDefinition(
                step_id="z-ready",
                title="Ready first step",
                instruction="Run now",
                acceptance_criteria=(AcceptanceCriterion("done-z", "host.result", "1"),),
            ),
        ),
    )
    await _approve(control, definition)

    page = await control.read_ready_steps("ns", limit=1, now=103.0)

    assert [candidate.step_id for candidate in page.candidates] == ["z-ready"]
    assert page.scanned == 2
    assert page.next_cursor is None


@pytest.mark.asyncio
async def test_task_and_binding_roll_back_when_control_event_write_fails(control, monkeypatch) -> None:
    approved = await _approve(control)
    candidate = (await control.read_ready_steps("ns", limit=8, now=103.0)).candidates[0]

    def fail_event(event):
        raise RuntimeError("simulated event write failure")

    monkeypatch.setattr(control, "_plan_append_event_locked", fail_event)
    with pytest.raises(RuntimeError, match="simulated event write failure"):
        await control.claim_plan_step(
            candidate,
            expected_control_version=approved.control_version,
            request_id="claim-first",
            principal="worker-host",
            holder="worker-1",
            ttl_seconds=30,
            now=103.0,
        )

    snapshot = await control.read_plan("ns", "scheduler-plan")
    assert snapshot.record.status.value == "approved"
    assert snapshot.record.control_version == approved.control_version
    assert await control.read_schedulable(limit=8, now=104.0) == ()
    assert (await control.read_ready_steps("ns", limit=8, now=104.0)).candidates


@pytest.mark.asyncio
async def test_pause_closes_new_steps_but_allows_recovery_of_admitted_binding(control) -> None:
    approved = await _approve(control)
    candidate = (await control.read_ready_steps("ns", limit=8, now=103.0)).candidates[0]
    admitted = await control.claim_plan_step(
        candidate,
        expected_control_version=approved.control_version,
        request_id="claim-first",
        principal="worker-host",
        holder="worker-1",
        ttl_seconds=1,
        now=103.0,
    )
    paused = await control.pause_plan(
        "ns", "scheduler-plan", reason="host pause",
        expected_control_version=approved.control_version + 1,
        request_id="pause", principal="host", now=103.5,
    )
    await control.release_task_claim(
        admitted.task.task_id,
        holder="worker-1",
        attempt_id=admitted.claim.attempt_id,
        now=103.6,
    )

    page = await control.read_ready_steps("ns", limit=8, now=103.7)
    assert [(item.plan_id, item.step_id) for item in page.candidates] == [
        ("scheduler-plan", "first")
    ]
    recovered = await control.claim_plan_step(
        page.candidates[0],
        expected_control_version=paused.control_version,
        request_id="claim-first",
        principal="worker-host",
        holder="worker-2",
        ttl_seconds=30,
        now=103.7,
    )
    assert recovered.task.task_id == admitted.task.task_id
    assert recovered.task.run_id == admitted.task.run_id
    assert recovered.claim.fencing_token > admitted.claim.fencing_token


@pytest.mark.asyncio
async def test_expired_approval_and_plan_deadline_refuse_step_admission(control) -> None:
    await _approve(control, expires_at_ms=103_000)
    candidate = (await control.read_ready_steps("ns", limit=8, now=102.5)).candidates[0]
    with pytest.raises(PlanRefused) as expired:
        await control.claim_plan_step(
            candidate,
            expected_control_version=candidate.control_version,
            request_id="claim-first",
            principal="worker-host",
            holder="worker-1",
            ttl_seconds=30,
            now=104.0,
        )
    assert expired.value.reason_code == "approval_expired"
    assert await control.read_schedulable(limit=8, now=104.0) == ()

    await _approve(control, replace(_definition(plan_id="deadline-plan"), deadline_at_ms=103_000))
    deadline_candidate = (
        await control.read_ready_steps("ns", limit=8, now=102.5)
    ).candidates
    deadline_candidate = next(item for item in deadline_candidate if item.plan_id == "deadline-plan")
    with pytest.raises(PlanRefused) as deadline:
        await control.claim_plan_step(
            deadline_candidate,
            expected_control_version=deadline_candidate.control_version,
            request_id="claim-deadline",
            principal="worker-host",
            holder="worker-1",
            ttl_seconds=30,
            now=104.0,
        )
    assert deadline.value.reason_code == "deadline_exceeded"


@pytest.mark.asyncio
async def test_same_worker_cannot_drive_an_idempotent_claim_twice(control, monkeypatch) -> None:
    await _approve(control)
    starts = 0
    both_selected = asyncio.Event()
    run_started = asyncio.Event()
    finish_run = asyncio.Event()
    arrivals = 0

    class Harness:
        async def start(self, request, context):
            nonlocal starts
            del request, context
            starts += 1
            run_started.set()
            await finish_run.wait()
            return SimpleNamespace(status=SimpleNamespace(value="completed"), stop_code="")

    worker = LocalWorker(
        control,
        lambda _task, _attempt: Harness(),
        holder="one-worker",
        enable_orchestration=True,
        orchestration_namespace="ns",
    )
    original_choose = worker._choose

    async def competing_choose(at):
        nonlocal arrivals
        decision = await original_choose(at)
        arrivals += 1
        if arrivals == 2:
            both_selected.set()
        await both_selected.wait()
        return decision

    monkeypatch.setattr(worker, "_choose", competing_choose)
    first = asyncio.create_task(worker.run_once(now=103.0))
    second = asyncio.create_task(worker.run_once(now=103.0))
    await asyncio.wait_for(run_started.wait(), timeout=2)
    done, _pending = await asyncio.wait(
        {first, second}, timeout=2, return_when=asyncio.FIRST_COMPLETED
    )
    assert len(done) == 1
    assert next(iter(done)).result().outcome == "already_claimed_here"
    finish_run.set()
    results = await asyncio.gather(first, second)

    assert starts == 1
    assert sum(item is not None and item.outcome == "settled:completed" for item in results) == 1
    assert sum(item is not None and item.outcome == "already_claimed_here" for item in results) == 1


@pytest.mark.asyncio
async def test_plan_claim_obeys_existing_task_input_backlog(control) -> None:
    control.backlog = BacklogLimits(total=1)
    await control.admit_task(
        TaskAdmission(
            namespace="ns",
            session_id="session",
            task_id="ordinary-task",
            run_id="ordinary-run",
            initial_input=InputContent(kind=InputKind.INITIAL, text="already using capacity"),
            created_at=99.0,
        )
    )
    approved = await _approve(control)
    candidate = (await control.read_ready_steps("ns", limit=8, now=103.0)).candidates[0]

    with pytest.raises(PlanRefused) as full:
        await control.claim_plan_step(
            candidate,
            expected_control_version=approved.control_version,
            request_id="claim-first",
            principal="worker-host",
            holder="worker-1",
            ttl_seconds=30,
            now=103.0,
        )

    assert full.value.reason_code == "backlog_full"
    snapshot = await control.read_plan("ns", "scheduler-plan")
    assert snapshot.record.status.value == "approved"
    assert snapshot.record.control_version == approved.control_version
    assert [item.task_id for item in await control.read_schedulable(limit=8, now=104.0)] == [
        "ordinary-task"
    ]
    assert (await control.read_ready_steps("ns", limit=8, now=104.0)).candidates
