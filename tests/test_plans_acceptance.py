from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace

import pytest

from yanxu_harness.plans import (
    AcceptanceCriterion,
    AcceptanceResult,
    ApprovalDecision,
    ExecutionEnvelope,
    PlanApprovalRequest,
    PlanDefinition,
    PlanRefused,
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
from yanxu_harness.tasks.types import TaskStatus
from yanxu_harness.storage.memory import InMemoryPersistence


def _definition(*, plan_criteria: bool = False, output_contract=None) -> PlanDefinition:
    criteria = (AcceptanceCriterion("done", "host.result", "1"),)
    return PlanDefinition(
        namespace="ns",
        session_id="session",
        plan_id="accept-plan",
        revision=1,
        title="Acceptance",
        goal="Verify submitted evidence before unlocking the next step",
        created_by="host",
        steps=(
            StepDefinition("first", "First", "Do the first step", criteria, output_contract=output_contract),
            StepDefinition("second", "Second", "Do the second step", criteria, depends_on=("first",)),
        ),
        acceptance_criteria=(
            (AcceptanceCriterion("whole-plan", "host.result", "1"),) if plan_criteria else ()
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
        store = SqliteControlStore(tmp_path / "plans-acceptance.sqlite", enable_orchestration=True)
    yield store
    close = getattr(store, "close", None)
    if callable(close):
        close()


async def _approve(store, *, plan_criteria: bool = False):
    definition = _definition(plan_criteria=plan_criteria)
    record = await store.create_plan(definition, request_id="create", principal="host", now=100.0)
    record = await store.request_plan_review(
        "ns", "accept-plan", expected_control_version=record.control_version,
        request_id="review", principal="host", now=101.0,
    )
    record = await store.decide_plan_approval(
        "ns", "accept-plan",
        PlanApprovalRequest(
            decision_id="approved-v1", decision=ApprovalDecision.APPROVED,
            approval_policy_version="policy-v1",
        ),
        expected_control_version=record.control_version,
        request_id="approve", principal="host", now_ms=102_000,
    )
    candidate = (await store.read_ready_steps("ns", limit=8, now=103.0)).candidates[0]
    claimed = await store.claim_plan_step(
        candidate, expected_control_version=record.control_version,
        request_id="claim-first", principal="host", holder="worker",
        ttl_seconds=30, now=103.0,
    )
    return record, claimed


async def _complete_bound_run(store, run_store, claimed, *, at: float = 104.0):
    task = await store.read_task(claimed.task.task_id)
    completed = task.transition(TaskStatus.COMPLETED, reason="work_completed", at=at)
    await store.save_task(completed, expected_status=task.status)
    await run_store.save_run(
        claimed.binding.run_id,
        {
            "run_id": claimed.binding.run_id,
            "namespace": claimed.binding.namespace,
            "session_id": "session",
            "status": "completed",
            "final_text": "done",
        },
        expected_version=None,
    )


def _registry(result: AcceptanceResult = AcceptanceResult.ACCEPTED, *, fail: bool = False) -> VerifierRegistry:
    registry = VerifierRegistry()

    async def verify(_criterion, _snapshot):
        if fail:
            raise RuntimeError("verifier failed")
        return VerificationDecision(result)

    registry.register("host.result", "1", verify, timeout_seconds=1.0, max_input_bytes=4096)
    return registry


def _evidence_reader(*, unresolved: tuple[str, ...] = (), fail: bool = False):
    async def read(binding, task, run):
        if fail:
            raise OSError("evidence source unavailable")
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

    return read


@pytest.mark.asyncio
async def test_task_completion_does_not_unlock_a_dependent_step(control) -> None:
    run_store = InMemoryPersistence()
    _, claimed = await _approve(control)
    await _complete_bound_run(control, run_store, claimed)

    assert (await control.read_ready_steps("ns", limit=8, now=105.0)).candidates == ()


@pytest.mark.asyncio
async def test_acceptance_requires_the_committed_run_and_matching_evidence(control) -> None:
    run_store = InMemoryPersistence()
    _, claimed = await _approve(control)
    await _complete_bound_run(control, run_store, claimed)
    coordinator = AcceptanceCoordinator(control, _evidence_reader(), _registry(), run_store=run_store)

    accepted = await coordinator.accept_step(
        "ns", "accept-plan", "first", request_id="accept-first", principal="host", now_ms=105_000
    )

    assert len(accepted) == 1
    assert accepted[0].result is AcceptanceResult.ACCEPTED
    page = await control.read_ready_steps("ns", limit=8, now=106.0)
    assert [candidate.step_id for candidate in page.candidates] == ["second"]
    events = await control.read_plan_events("ns", "accept-plan")
    assert any(event.event_type == "step_accepted" for event in events)
    assert any(entry.kind == "step_accepted" for entry in await control.read_plan_outbox())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reader", "expected"),
    [
        (_evidence_reader(unresolved=("op-1",)), AcceptanceResult.INCONCLUSIVE),
        (_evidence_reader(fail=True), AcceptanceResult.INCONCLUSIVE),
    ],
)
async def test_unknown_operations_and_unreadable_evidence_remain_blocked(control, reader, expected) -> None:
    run_store = InMemoryPersistence()
    _, claimed = await _approve(control)
    await _complete_bound_run(control, run_store, claimed)
    coordinator = AcceptanceCoordinator(control, reader, _registry(), run_store=run_store)

    records = await coordinator.accept_step(
        "ns", "accept-plan", "first", request_id="accept-first", principal="host", now_ms=105_000
    )

    assert records[0].result is expected
    assert (await control.read_ready_steps("ns", limit=8, now=106.0)).candidates == ()


@pytest.mark.asyncio
async def test_rejected_verification_does_not_unlock_dependents(control) -> None:
    run_store = InMemoryPersistence()
    _, claimed = await _approve(control)
    await _complete_bound_run(control, run_store, claimed)
    coordinator = AcceptanceCoordinator(
        control, _evidence_reader(), _registry(AcceptanceResult.REJECTED), run_store=run_store
    )

    records = await coordinator.accept_step(
        "ns", "accept-plan", "first", request_id="accept-first", principal="host", now_ms=105_000
    )

    assert records[0].result is AcceptanceResult.REJECTED
    assert (await control.read_plan("ns", "accept-plan")).record.status.value == "paused"
    assert (await control.read_ready_steps("ns", limit=8, now=106.0)).candidates == ()


@pytest.mark.asyncio
async def test_rejected_completed_step_can_be_explicitly_retried_with_new_binding(control) -> None:
    run_store = InMemoryPersistence()
    _, original = await _approve(control)
    await _complete_bound_run(control, run_store, original)
    coordinator = AcceptanceCoordinator(
        control, _evidence_reader(), _registry(AcceptanceResult.REJECTED), run_store=run_store
    )
    await coordinator.accept_step(
        "ns", "accept-plan", "first", request_id="reject-first", principal="host", now_ms=105_000
    )
    paused = await control.read_plan("ns", "accept-plan")
    before_retry = await control.resume_plan(
        "ns",
        "accept-plan",
        expected_control_version=paused.record.control_version,
        request_id="resume-before-retry",
        principal="host",
        now=105.5,
    )

    retried = await control.retry_step(
        "ns", "accept-plan", "first", retries_binding_id=original.binding.binding_id,
        expected_control_version=before_retry.control_version,
        request_id="retry-first", principal="host", holder="worker-2", ttl_seconds=30, now=106.0,
    )
    repeated = await control.retry_step(
        "ns", "accept-plan", "first", retries_binding_id=original.binding.binding_id,
        expected_control_version=before_retry.control_version,
        request_id="retry-first", principal="host", holder="worker-2", ttl_seconds=30, now=107.0,
    )

    bindings = await control.read_plan_bindings("ns", "accept-plan")
    assert len(bindings) == 2
    assert bindings[0].binding_id == original.binding.binding_id
    assert bindings[0].binding_status == "rejected"
    assert retried.binding.binding_id != original.binding.binding_id
    assert retried.binding.retries_binding_id == original.binding.binding_id
    assert retried.task.task_id != original.task.task_id
    assert retried.task.run_id != original.task.run_id
    assert repeated.binding.binding_id == retried.binding.binding_id
    assert repeated.task.task_id == retried.task.task_id
    assert repeated.task.run_id == retried.task.run_id
    assert len(await control.read_inputs(original.task.task_id)) == 1
    assert len(await control.read_inputs(retried.task.task_id)) == 1
    retry_events = [
        event for event in await control.read_plan_events("ns", "accept-plan")
        if event.event_type == "step_retried"
    ]
    assert len(retry_events) == 1
    assert retry_events[0].payload["retries_binding_id"] == original.binding.binding_id
    assert sum(entry.kind == "step_retried" for entry in await control.read_plan_outbox()) == 1
    after_retry = await control.read_plan("ns", "accept-plan")
    recovery_candidate = (await control.read_ready_steps("ns", limit=8, now=137.0)).candidates[0]
    recovered = await control.claim_plan_step(
        recovery_candidate,
        expected_control_version=after_retry.record.control_version,
        request_id="worker-retry-recover",
        principal="worker",
        holder="worker-3",
        ttl_seconds=30,
        now=137.0,
    )
    assert recovered.binding.binding_id == retried.binding.binding_id
    assert recovered.task.task_id == retried.task.task_id
    assert recovered.task.run_id == retried.task.run_id
    assert recovered.claim.fencing_token > retried.claim.fencing_token
    assert (await control.read_plan("ns", "accept-plan")).record.control_version == (
        before_retry.control_version + 1
    )


@pytest.mark.asyncio
async def test_inconclusive_binding_cannot_be_retried(control) -> None:
    run_store = InMemoryPersistence()
    _, claimed = await _approve(control)
    await _complete_bound_run(control, run_store, claimed)
    coordinator = AcceptanceCoordinator(
        control,
        _evidence_reader(unresolved=("operation-unknown",)),
        _registry(),
        run_store=run_store,
    )
    outcome = await coordinator.accept_step(
        "ns", "accept-plan", "first", request_id="inconclusive-first", principal="host", now_ms=105_000
    )
    assert outcome[0].result is AcceptanceResult.INCONCLUSIVE
    assert (await control.read_plan("ns", "accept-plan")).record.status.value == "paused"
    snapshot = await control.read_plan("ns", "accept-plan")

    with pytest.raises(PlanRefused) as refused:
        await control.retry_step(
            "ns",
            "accept-plan",
            "first",
            retries_binding_id=claimed.binding.binding_id,
            expected_control_version=snapshot.record.control_version,
            request_id="retry-inconclusive",
            principal="host",
            holder="worker-2",
            ttl_seconds=30,
            now=106.0,
        )
    assert refused.value.reason_code == "retry_not_allowed"
    assert len(await control.read_plan_bindings("ns", "accept-plan")) == 1


@pytest.mark.asyncio
async def test_verifier_failure_is_inconclusive_and_does_not_unlock(control) -> None:
    run_store = InMemoryPersistence()
    _, claimed = await _approve(control)
    await _complete_bound_run(control, run_store, claimed)
    coordinator = AcceptanceCoordinator(
        control, _evidence_reader(), _registry(fail=True), run_store=run_store
    )

    records = await coordinator.accept_step(
        "ns", "accept-plan", "first", request_id="accept-first", principal="host", now_ms=105_000
    )

    assert records[0].result is AcceptanceResult.INCONCLUSIVE
    assert records[0].reason_code == "verifier_error"
    assert (await control.read_ready_steps("ns", limit=8, now=106.0)).candidates == ()


@pytest.mark.asyncio
async def test_verifier_timeout_is_inconclusive() -> None:
    registry = VerifierRegistry()

    async def wait_forever(_criterion, _snapshot):
        await asyncio.Event().wait()

    registry.register("host.result", "1", wait_forever, timeout_seconds=0.001, max_input_bytes=4096)
    decision = await registry.verify(
        _definition().steps[0].acceptance_criteria[0],
        EvidenceSnapshot(
            namespace="ns",
            plan_id="accept-plan",
            revision=1,
            step_id="first",
            binding_id="binding-first",
            task_id="task-first",
            run_id="run-first",
            run_version=1,
            evidence_refs=("artifact:first@v1",),
            payload={},
        ),
    )

    assert decision.result is AcceptanceResult.INCONCLUSIVE
    assert decision.reason_code == "verifier_timeout"


@pytest.mark.asyncio
async def test_evidence_reader_timeout_is_inconclusive_and_does_not_unlock(control) -> None:
    run_store = InMemoryPersistence()
    _, claimed = await _approve(control)
    await _complete_bound_run(control, run_store, claimed)

    async def wait_forever(_binding, _task, _run):
        await asyncio.Event().wait()

    coordinator = AcceptanceCoordinator(
        control,
        wait_forever,
        _registry(),
        run_store=run_store,
        evidence_timeout_seconds=0.001,
    )
    records = await coordinator.accept_step(
        "ns", "accept-plan", "first", request_id="accept-timeout", principal="host", now_ms=105_000
    )

    assert records[0].result is AcceptanceResult.INCONCLUSIVE
    assert records[0].reason_code == "evidence_unavailable"
    assert (await control.read_ready_steps("ns", limit=8, now=106.0)).candidates == ()


@pytest.mark.asyncio
async def test_evidence_change_during_verification_is_inconclusive(control) -> None:
    run_store = InMemoryPersistence()
    _, claimed = await _approve(control)
    await _complete_bound_run(control, run_store, claimed)
    base_reader = _evidence_reader()
    reads = 0

    async def changing_reader(binding, task, run):
        nonlocal reads
        reads += 1
        evidence = await base_reader(binding, task, run)
        return evidence if reads == 1 else replace(evidence, payload={"result": "changed"})

    coordinator = AcceptanceCoordinator(control, changing_reader, _registry(), run_store=run_store)
    records = await coordinator.accept_step(
        "ns", "accept-plan", "first", request_id="accept-changing", principal="host", now_ms=105_000
    )

    assert records[0].result is AcceptanceResult.INCONCLUSIVE
    assert records[0].reason_code == "source_changed"
    assert (await control.read_ready_steps("ns", limit=8, now=106.0)).candidates == ()


@pytest.mark.asyncio
async def test_output_contract_failure_blocks_acceptance(control) -> None:
    run_store = InMemoryPersistence()
    definition = _definition(output_contract={"required_keys": ["result"], "equals": {"result": "passed"}})
    # Use a Plan id unique to this test while keeping the normal approval path.
    definition = PlanDefinition(
        namespace=definition.namespace,
        session_id=definition.session_id,
        plan_id="output-contract-plan",
        revision=definition.revision,
        title=definition.title,
        goal=definition.goal,
        created_by=definition.created_by,
        steps=definition.steps,
        execution_envelope=definition.execution_envelope,
    )
    record = await control.create_plan(definition, request_id="create-output", principal="host", now=100.0)
    record = await control.request_plan_review(
        "ns", definition.plan_id, expected_control_version=record.control_version,
        request_id="review-output", principal="host", now=101.0,
    )
    record = await control.decide_plan_approval(
        "ns", definition.plan_id,
        PlanApprovalRequest(
            decision_id="approved-output", decision=ApprovalDecision.APPROVED,
            approval_policy_version="policy-v1",
        ),
        expected_control_version=record.control_version,
        request_id="approve-output", principal="host", now_ms=102_000,
    )
    candidate = (await control.read_ready_steps("ns", limit=8, now=103.0)).candidates[0]
    claimed = await control.claim_plan_step(
        candidate, expected_control_version=record.control_version,
        request_id="claim-output", principal="host", holder="worker", ttl_seconds=30, now=103.0,
    )
    await _complete_bound_run(control, run_store, claimed)

    coordinator = AcceptanceCoordinator(control, _evidence_reader(), _registry(), run_store=run_store)
    records = await coordinator.accept_step(
        "ns", definition.plan_id, "first", request_id="accept-output", principal="host", now_ms=105_000
    )

    assert records[0].result is AcceptanceResult.REJECTED
    assert records[0].reason_code == "output_contract_failed"
    assert (await control.read_ready_steps("ns", limit=8, now=106.0)).candidates == ()


@pytest.mark.asyncio
async def test_acceptance_and_downstream_notification_roll_back_together(control, monkeypatch) -> None:
    run_store = InMemoryPersistence()
    _, claimed = await _approve(control)
    await _complete_bound_run(control, run_store, claimed)
    coordinator = AcceptanceCoordinator(control, _evidence_reader(), _registry(), run_store=run_store)
    original = control._plan_append_event_locked

    def fail_event(_event):
        raise RuntimeError("simulated event write failure")

    monkeypatch.setattr(control, "_plan_append_event_locked", fail_event)
    with pytest.raises(RuntimeError, match="event write failure"):
        await coordinator.accept_step(
            "ns", "accept-plan", "first", request_id="accept-first", principal="host", now_ms=105_000
        )
    monkeypatch.setattr(control, "_plan_append_event_locked", original)

    assert await control.read_plan_acceptances("ns", "accept-plan") == ()
    bindings = await control.read_plan_bindings("ns", "accept-plan")
    assert bindings[0].binding_status == "admitted"
    assert (await control.read_ready_steps("ns", limit=8, now=106.0)).candidates == ()


@pytest.mark.asyncio
async def test_finalize_requires_all_steps_and_plan_level_criteria(control) -> None:
    run_store = InMemoryPersistence()
    approved, claimed = await _approve(control, plan_criteria=True)
    await _complete_bound_run(control, run_store, claimed)
    coordinator = AcceptanceCoordinator(control, _evidence_reader(), _registry(), run_store=run_store)

    await coordinator.accept_step(
        "ns", "accept-plan", "first", request_id="accept-first", principal="host", now_ms=105_000
    )
    with pytest.raises(PlanRefused) as incomplete:
        await coordinator.finalize_plan(
            "ns", "accept-plan", expected_control_version=(await control.read_plan("ns", "accept-plan")).record.control_version,
            request_id="finalize-early", principal="host", now=106.0,
        )
    assert incomplete.value.reason_code in {"steps_pending", "plan_acceptance_pending"}

    latest = await control.read_plan("ns", "accept-plan")
    assert latest.record.status.value != "completed"
    assert latest.record.control_version >= approved.control_version

    candidate = (await control.read_ready_steps("ns", limit=8, now=107.0)).candidates[0]
    second = await control.claim_plan_step(
        candidate,
        expected_control_version=candidate.control_version,
        request_id="claim-second",
        principal="host",
        holder="worker",
        ttl_seconds=30,
        now=107.0,
    )
    await _complete_bound_run(control, run_store, second, at=108.0)
    await coordinator.accept_step(
        "ns", "accept-plan", "second", request_id="accept-second", principal="host", now_ms=109_000
    )

    latest = await control.read_plan("ns", "accept-plan")
    completed = await coordinator.finalize_plan(
        "ns", "accept-plan", expected_control_version=latest.record.control_version,
        request_id="finalize", principal="host", now=110.0,
    )
    assert completed.status.value == "completed"
    assert (await control.read_plan("ns", "accept-plan")).record == completed
    assert any(event.event_type == "completed" for event in await control.read_plan_events("ns", "accept-plan"))
    repeated = await coordinator.finalize_plan(
        "ns", "accept-plan", expected_control_version=latest.record.control_version,
        request_id="finalize", principal="host", now=111.0,
    )
    assert repeated == completed


def _downgrade_plan_schema_to_v1(path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP TABLE plan_acceptances")
        connection.execute(
            "UPDATE control_meta SET value = '1' WHERE key = 'plan_schema_version'"
        )
        connection.commit()
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_sqlite_plan_schema_v1_migrates_when_orchestration_is_enabled(tmp_path) -> None:
    path = tmp_path / "plan-v1.sqlite"
    store = SqliteControlStore(path, enable_orchestration=True)
    await store.create_plan(_definition(), request_id="create", principal="host", now=100.0)
    store.close()
    _downgrade_plan_schema_to_v1(path)

    migrated = SqliteControlStore(path, enable_orchestration=True)
    try:
        assert (await migrated.read_plan("ns", "accept-plan")) is not None
        assert migrated._connection.execute(
            "SELECT value FROM control_meta WHERE key = 'plan_schema_version'"
        ).fetchone()[0] == "2"
        assert migrated._connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'plan_acceptances'"
        ).fetchone() == ("plan_acceptances",)
    finally:
        migrated.close()


def test_read_only_store_refuses_plan_migration_without_writing(tmp_path) -> None:
    path = tmp_path / "plan-v1-read-only.sqlite"
    store = SqliteControlStore(path, enable_orchestration=True)
    store.close()
    _downgrade_plan_schema_to_v1(path)

    with pytest.raises(PlanRefused) as refused:
        SqliteControlStore(path, read_only=True, enable_orchestration=True)
    assert refused.value.reason_code == "store_unsupported"
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT value FROM control_meta WHERE key = 'plan_schema_version'"
        ).fetchone()[0] == "1"
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'plan_acceptances'"
        ).fetchone() is None
    finally:
        connection.close()

