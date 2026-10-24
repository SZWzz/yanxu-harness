from __future__ import annotations

import asyncio
import sqlite3

import pytest

from yanxu_harness.plans import (
    AcceptanceCriterion,
    ApprovalDecision,
    ExecutionEnvelope,
    OrchestrationStore,
    PlanApprovalRequest,
    PlanDefinition,
    PlanRefused,
    PlanStatus,
    StepDefinition,
    VersionedRef,
    require_orchestration,
)
from yanxu_harness.tasks.inbox import MemoryControlStore, SqliteControlStore


def _definition(revision: int = 1, *, goal: str = "Complete a bounded task", reason: str = "") -> PlanDefinition:
    return PlanDefinition(
        namespace="ns",
        session_id="session",
        plan_id="plan-1",
        revision=revision,
        title="Example Plan",
        goal=goal,
        created_by="host",
        created_at=10.0,
        supersedes_revision=revision - 1 if revision > 1 else None,
        replan_reason=reason,
        steps=(
            StepDefinition(
                step_id="a",
                title="Prepare",
                instruction="Prepare the input",
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
        store = SqliteControlStore(tmp_path / "control.sqlite", enable_orchestration=True)
    yield store
    close = getattr(store, "close", None)
    if callable(close):
        close()


async def _create(store) -> object:
    return await store.create_plan(
        _definition(), request_id="create-1", principal="alice", now=100.0
    )


async def _approve(store, *, now_ms: int = 100_000, expires_at_ms: int | None = None):
    await _create(store)
    await store.request_plan_review(
        "ns", "plan-1", expected_control_version=1,
        request_id="review-1", principal="alice", now=100.0,
    )
    return await store.decide_plan_approval(
        "ns",
        "plan-1",
        PlanApprovalRequest(
            decision_id="approval-1",
            decision=ApprovalDecision.APPROVED,
            approval_policy_version="policy-v1",
            expires_at_ms=expires_at_ms,
            reason="review passed",
        ),
        expected_control_version=2,
        request_id="approve-1",
        principal="reviewer",
        now_ms=now_ms,
    )


@pytest.mark.asyncio
async def test_reference_stores_implement_the_optional_orchestration_contract(control) -> None:
    assert isinstance(control, OrchestrationStore)
    assert require_orchestration(control) is control


@pytest.mark.asyncio
async def test_create_is_idempotent_and_binds_an_immutable_revision(control) -> None:
    first = await _create(control)
    repeated = await control.create_plan(
        _definition(), request_id="create-1", principal="alice", now=999.0
    )
    snapshot = await control.read_plan("ns", "plan-1")

    assert repeated == first
    assert first.status is PlanStatus.DRAFT
    assert snapshot.definition.created_at == 100.0
    assert snapshot.record.definition_digest == snapshot.definition.definition_digest
    assert len(await control.read_plan_events("ns", "plan-1")) == 1
    assert len(await control.read_plan_outbox()) == 1
    with pytest.raises(PlanRefused) as reused:
        await control.create_plan(
            _definition(goal="different"), request_id="create-1", principal="alice"
        )
    assert reused.value.reason_code == "request_id_conflict"
    with pytest.raises(PlanRefused) as changed:
        await control.create_plan(
            _definition(goal="different"), request_id="create-2", principal="alice"
        )
    assert changed.value.reason_code == "content_conflict"


@pytest.mark.asyncio
async def test_controls_use_cas_and_same_request_replays_the_original_record(control) -> None:
    created = await _create(control)
    reviewed = await control.request_plan_review(
        "ns", "plan-1", expected_control_version=created.control_version,
        request_id="review-1", principal="alice", now=101.0,
    )
    replay = await control.request_plan_review(
        "ns", "plan-1", expected_control_version=created.control_version,
        request_id="review-1", principal="alice", now=500.0,
    )
    assert reviewed == replay
    assert reviewed.status is PlanStatus.REVIEWING
    with pytest.raises(PlanRefused) as stale:
        await control.request_plan_review(
            "ns", "plan-1", expected_control_version=created.control_version,
            request_id="review-stale", principal="alice",
        )
    assert stale.value.reason_code == "version_conflict"
    with pytest.raises(PlanRefused) as conflict:
        await control.request_plan_review(
            "ns", "plan-1", expected_control_version=created.control_version,
            request_id="review-1", principal="other",
        )
    assert conflict.value.reason_code == "request_id_conflict"


@pytest.mark.asyncio
async def test_approval_is_bound_to_current_definition_and_trusted_principal(control) -> None:
    approved = await _approve(control)
    snapshot = await control.read_plan("ns", "plan-1")
    approvals, revocations = await control.read_plan_approvals("ns", "plan-1")

    assert approved.status is PlanStatus.APPROVED
    assert snapshot.record.active_approval_id == "approval-1"
    assert approvals[0].decided_by == "reviewer"
    assert approvals[0].definition_digest == snapshot.record.definition_digest
    assert approvals[0].execution_envelope_digest == snapshot.definition.envelope_digest
    assert revocations == ()


@pytest.mark.asyncio
async def test_rejection_stays_revisable_and_does_not_create_approval(control) -> None:
    await _create(control)
    await control.request_plan_review(
        "ns", "plan-1", expected_control_version=1,
        request_id="review-1", principal="alice", now=101.0,
    )
    rejected = await control.decide_plan_approval(
        "ns", "plan-1",
        PlanApprovalRequest("decision-no", "rejected", "policy-v1", reason="needs edits"),
        expected_control_version=2,
        request_id="reject-1", principal="reviewer", now_ms=101_000,
    )
    snapshot = await control.read_plan("ns", "plan-1")

    assert rejected.status is PlanStatus.DRAFT
    assert snapshot.record.active_approval_id == ""
    assert snapshot.approvals[0].decision is ApprovalDecision.REJECTED


@pytest.mark.asyncio
async def test_revocation_is_append_only_and_resume_requires_a_fresh_approval(control) -> None:
    approved = await _approve(control, now_ms=100_000, expires_at_ms=200_000)
    paused = await control.pause_plan(
        "ns", "plan-1", reason="operator pause", expected_control_version=approved.control_version,
        request_id="pause-1", principal="alice", now=101.0,
    )
    revoked = await control.revoke_plan_approval(
        "ns", "plan-1", "approval-1", reason="policy changed",
        expected_control_version=paused.control_version, request_id="revoke-1",
        principal="security", now_ms=102_000,
    )
    with pytest.raises(PlanRefused) as denied:
        await control.resume_plan(
            "ns", "plan-1", expected_control_version=revoked.control_version,
            request_id="resume-denied", principal="alice", now=103.0,
        )
    assert denied.value.reason_code == "approval_revoked"

    renewed = await control.decide_plan_approval(
        "ns", "plan-1",
        PlanApprovalRequest("approval-2", "approved", "policy-v2", expires_at_ms=300_000),
        expected_control_version=revoked.control_version,
        request_id="approve-2", principal="reviewer", now_ms=103_000,
    )
    resumed = await control.resume_plan(
        "ns", "plan-1", expected_control_version=renewed.control_version,
        request_id="resume-1", principal="alice", now=104.0,
    )
    snapshot = await control.read_plan("ns", "plan-1")

    assert resumed.status is PlanStatus.APPROVED
    assert snapshot.record.active_approval_id == "approval-2"
    assert [item.decision_id for item in snapshot.approvals] == ["approval-1", "approval-2"]
    assert [item.decision_id for item in snapshot.revocations] == ["approval-1"]


@pytest.mark.asyncio
async def test_replanning_publishes_a_new_draft_without_inheriting_approval(control) -> None:
    approved = await _approve(control)
    replanning = await control.request_replan(
        "ns", "plan-1", reason="scope changed", expected_control_version=approved.control_version,
        request_id="replan-1", principal="alice", now=102.0,
    )
    revised = await control.publish_plan_revision(
        _definition(2, goal="Revised goal", reason="scope changed"),
        expected_control_version=replanning.control_version,
        request_id="publish-2", principal="alice", now=103.0,
    )
    snapshot = await control.read_plan("ns", "plan-1")

    assert revised.current_revision == 2
    assert revised.status is PlanStatus.DRAFT
    assert revised.active_approval_id == ""
    assert snapshot.definition.goal == "Revised goal"
    assert len(snapshot.approvals) == 1
    assert (await control.read_plan_revision("ns", "plan-1", 1)).goal == "Complete a bounded task"


@pytest.mark.asyncio
async def test_outbox_delivery_is_durable_and_idempotent(control) -> None:
    await _create(control)
    entries = await control.read_plan_outbox()
    assert entries[0].pending
    assert await control.deliver_plan_outbox(entries[0].outbox_id, at=101.0)
    assert not await control.deliver_plan_outbox(entries[0].outbox_id, at=102.0)
    assert (await control.read_plan_outbox(pending_only=False))[0].attempts == 1


@pytest.mark.asyncio
async def test_scope_reads_do_not_cross_namespaces(control) -> None:
    await _create(control)
    assert await control.read_plan("other", "plan-1") is None
    assert await control.read_plan_revision("other", "plan-1", 1) is None
    assert await control.read_plan_events("other", "plan-1") == ()


@pytest.mark.asyncio
async def test_failed_event_write_rolls_back_record_revision_request_and_outbox(control) -> None:
    original = control._plan_append_event_locked

    def fail(_event):
        raise RuntimeError("injected event write failure")

    control._plan_append_event_locked = fail
    with pytest.raises(RuntimeError, match="injected"):
        await _create(control)
    control._plan_append_event_locked = original

    assert await control.read_plan("ns", "plan-1") is None
    assert await control.read_plan_events("ns", "plan-1") == ()
    assert await control.read_plan_outbox() == ()
    created = await _create(control)
    assert created.control_version == 1


@pytest.mark.asyncio
async def test_expired_approval_does_not_reopen_a_paused_plan(control) -> None:
    approved = await _approve(control, now_ms=100_000, expires_at_ms=101_000)
    paused = await control.pause_plan(
        "ns", "plan-1", reason="operator pause", expected_control_version=approved.control_version,
        request_id="pause-1", principal="alice", now=100.5,
    )
    with pytest.raises(PlanRefused) as expired:
        await control.resume_plan(
            "ns", "plan-1", expected_control_version=paused.control_version,
            request_id="resume-expired", principal="alice", now=101.0,
        )
    assert expired.value.reason_code == "approval_expired"


def test_non_orchestration_backend_is_refused() -> None:
    with pytest.raises(PlanRefused) as unsupported:
        require_orchestration(object())
    assert unsupported.value.reason_code == "store_unsupported"


def test_reference_store_requires_explicit_orchestration_enablement() -> None:
    control = MemoryControlStore()
    with pytest.raises(PlanRefused) as unsupported:
        require_orchestration(control)
    assert unsupported.value.reason_code == "store_unsupported"


@pytest.mark.asyncio
async def test_two_sqlite_connections_cannot_both_win_the_same_control_version(tmp_path) -> None:
    path = tmp_path / "shared.sqlite"
    first = SqliteControlStore(path, enable_orchestration=True)
    second = SqliteControlStore(path, enable_orchestration=True)
    try:
        await _create(first)
        outcomes = await asyncio.gather(
            first.request_plan_review(
                "ns", "plan-1", expected_control_version=1,
                request_id="review-left", principal="left", now=101.0,
            ),
            second.request_plan_review(
                "ns", "plan-1", expected_control_version=1,
                request_id="review-right", principal="right", now=101.0,
            ),
            return_exceptions=True,
        )
        assert sum(not isinstance(item, Exception) for item in outcomes) == 1
        failures = [item for item in outcomes if isinstance(item, PlanRefused)]
        assert len(failures) == 1
        assert failures[0].reason_code == "version_conflict"
        snapshot = await first.read_plan("ns", "plan-1")
        assert snapshot.record.control_version == 2
        assert len(await first.read_plan_events("ns", "plan-1")) == 2
    finally:
        first.close()
        second.close()


def test_sqlite_read_only_refuses_missing_plan_schema_without_creating_it(tmp_path) -> None:
    path = tmp_path / "old-control.sqlite"
    writable = SqliteControlStore(path)
    writable.close()
    reader = SqliteControlStore(path, read_only=True)
    try:
        with pytest.raises(PlanRefused) as unsupported:
            asyncio.run(reader.read_plan("ns", "plan-1"))
        assert unsupported.value.reason_code == "store_unsupported"
    finally:
        reader.close()
    with sqlite3.connect(path) as connection:
        names = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert not any(name.startswith("plan_") for name in names)


def test_sqlite_refuses_an_unknown_plan_schema_version(tmp_path) -> None:
    path = tmp_path / "future.sqlite"
    writable = SqliteControlStore(path, enable_orchestration=True)
    writable._connection.execute(
        "UPDATE control_meta SET value = '99' WHERE key = 'plan_schema_version'"
    )
    writable.close()
    with pytest.raises(PlanRefused) as unsupported:
        SqliteControlStore(path)
    assert unsupported.value.reason_code == "store_unsupported"
