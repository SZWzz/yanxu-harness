from __future__ import annotations

import asyncio
import multiprocessing
import sqlite3
from dataclasses import replace
from typing import Any

import pytest

from yanxu_harness.errors import ContractError
from yanxu_harness.plans import (
    AcceptanceCriterion,
    ApprovalDecision,
    AcceptanceResult,
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
from yanxu_harness.tasks import (
    BudgetLimit,
    BudgetLimitMode,
    BudgetOwnerKind,
    BudgetOwnerRef,
    BudgetPolicyChangeRecord,
    BudgetPolicySnapshot,
    BudgetRefused,
    BudgetReservation,
    BudgetUnit,
    MemoryControlStore,
    ReservationState,
    SqliteControlStore,
    TaskAdmission,
    TypedDispatchBudget,
    TypedBudgetDispatch,
    TypedBudgetStore,
)
from yanxu_harness.tasks.types import ChildRelation
from yanxu_harness.tasks.types import TaskStatus


def _policy(owner: BudgetOwnerRef, limit: int) -> BudgetPolicySnapshot:
    return BudgetPolicySnapshot(
        owner=owner,
        session_id="session",
        policy_version="budget-v1",
        limits=(BudgetLimit(BudgetUnit.MODEL_REQUESTS, limit),),
    )


def _cost_policy(owner: BudgetOwnerRef, currency: str, price_version: str) -> BudgetPolicySnapshot:
    return BudgetPolicySnapshot(
        owner=owner,
        session_id="session",
        policy_version="cost-budget-v1",
        limits=(
            BudgetLimit(
                BudgetUnit.COST_MICROS,
                100,
                mode=BudgetLimitMode.OBSERVE,
                currency=currency,
                price_version=price_version,
            ),
        ),
    )


def _versioned_policy(owner: BudgetOwnerRef, version: str, limit: int) -> BudgetPolicySnapshot:
    return BudgetPolicySnapshot(
        owner=owner,
        session_id="session",
        policy_version=version,
        limits=(BudgetLimit(BudgetUnit.MODEL_REQUESTS, limit),),
    )


def _downgrade_typed_budget_metadata_to_v1(path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP TABLE typed_budget_policy_changes")
        connection.execute("DROP TABLE typed_budget_policy_versions")
        connection.execute(
            "UPDATE control_meta SET value = '1' WHERE key = 'typed_budget_schema_version'"
        )
        connection.commit()
    finally:
        connection.close()


def _request(
    task_id: str,
    run_id: str,
    identity: str = "request-1",
    *,
    amount: int = 1,
    unit: BudgetUnit = BudgetUnit.MODEL_REQUESTS,
) -> TypedBudgetDispatch:
    return TypedBudgetDispatch(
        task_id=task_id,
        run_id=run_id,
        dispatch_identity=identity,
        request_instance=f"instance-{identity}",
        unit=unit,
        amount=amount,
    )


def _definition() -> PlanDefinition:
    return PlanDefinition(
        namespace="ns",
        session_id="session",
        plan_id="budget-plan",
        revision=1,
        title="Budget plan",
        goal="Run one budgeted step",
        created_by="host",
        steps=(
            StepDefinition(
                step_id="first",
                title="First",
                instruction="Do the approved work",
                acceptance_criteria=(AcceptanceCriterion("done", "host.done", "yes"),),
            ),
        ),
        execution_envelope=ExecutionEnvelope(
            profile_ref=VersionedRef("profile/default", "1"),
            workspace_ref=VersionedRef("workspace/main", "1"),
            policy_ref=VersionedRef("policy/default", "1"),
            deployment_ref=VersionedRef("deployment/local", "1"),
            budget_ref=VersionedRef("budget/default", "budget-v1"),
        ),
    )


async def _claim_plan_task(store):
    definition = _definition()
    record = await store.create_plan(
        definition, request_id="create-budget-plan", principal="host", now=100.0
    )
    record = await store.request_plan_review(
        "ns", "budget-plan", expected_control_version=record.control_version,
        request_id="review-budget-plan", principal="host", now=101.0,
    )
    record = await store.decide_plan_approval(
        "ns",
        "budget-plan",
        PlanApprovalRequest(
            decision_id="approve-budget-plan",
            decision=ApprovalDecision.APPROVED,
            approval_policy_version="approval-v1",
        ),
        expected_control_version=record.control_version,
        request_id="approve-budget-plan",
        principal="reviewer",
        now_ms=102_000,
    )
    candidate = (await store.read_ready_steps("ns", limit=8, now=103.0)).candidates[0]
    return await store.claim_plan_step(
        candidate,
        expected_control_version=record.control_version,
        request_id="claim-budget-plan-step",
        principal="worker-host",
        holder="worker-1",
        ttl_seconds=30,
        now=103.0,
    )


@pytest.fixture(params=("memory", "sqlite"))
def control(request, tmp_path):
    if request.param == "memory":
        store = MemoryControlStore(enable_orchestration=True, enable_typed_budgets=True)
    else:
        store = SqliteControlStore(
            tmp_path / "typed-budget.sqlite",
            enable_orchestration=True,
            enable_typed_budgets=True,
        )
    yield store
    close = getattr(store, "close", None)
    if callable(close):
        close()


@pytest.mark.asyncio
async def test_plan_task_child_dispatch_uses_all_committed_scopes_and_one_actual(control) -> None:
    assert isinstance(control, TypedBudgetStore)
    claimed = await _claim_plan_task(control)
    parent = claimed.task
    child_admission = TaskAdmission(
        namespace="ns",
        session_id="session",
        task_id="child-task",
        run_id="child-run",
        root_task_id=parent.task_id,
        parent_task_id=parent.task_id,
        depth=1,
    )
    relation = ChildRelation(
        parent_task_id=parent.task_id,
        child_task_id=child_admission.task_id,
        spawn_identity="spawn-child-1",
        namespace="ns",
        session_id="session",
    )
    await control.register_child(relation, child_task=child_admission, now=104.0)

    owners = tuple(sorted((
        BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, "ns", parent.task_id),
        BudgetOwnerRef(BudgetOwnerKind.PLAN, "ns", "budget-plan"),
        BudgetOwnerRef(BudgetOwnerKind.TASK_GRANT, "ns", "spawn-child-1"),
    ), key=lambda owner: owner.key))
    for owner in owners:
        await control.put_budget_policy(_policy(owner, 3))

    dispatch = _request("child-task", "child-run")
    reserved = await control.reserve_typed_budget_dispatch(dispatch, now=105.0)
    replay = await control.reserve_typed_budget_dispatch(dispatch, now=106.0)
    assert reserved == replay
    assert tuple(item.owner for item in reserved.coverage) == owners
    assert all(item.policy_digest for item in reserved.coverage)
    assert reserved.state is ReservationState.RESERVED

    for owner in owners:
        usage = await control.read_typed_budget_usage(owner, BudgetUnit.MODEL_REQUESTS)
        assert (usage.actual, usage.reserved, usage.unknown, usage.used) == (0, 1, 0, 1)

    settled = await control.settle_typed_budget_reservation(
        reserved.reservation_id, actual=1, note="provider usage fact", now=107.0
    )
    assert settled.settled_actual == 1
    assert await control.read_typed_budget_reservation(reserved.reservation_id) == settled
    for owner in owners:
        usage = await control.read_typed_budget_usage(owner, BudgetUnit.MODEL_REQUESTS)
        assert (usage.actual, usage.reserved, usage.unknown) == (1, 0, 0)

    with pytest.raises(ContractError, match="different Task/Run"):
        await control.reserve_typed_budget_dispatch(
            _request("child-task", "child-run", amount=2), now=108.0
        )
    with pytest.raises(ContractError, match="different Task/Run"):
        await control.reserve_typed_budget_dispatch(
            _request(
                "child-task", "child-run", unit=BudgetUnit.TURNS
            ),
            now=109.0,
        )
    await control.admit_task(
        TaskAdmission(namespace="ns", session_id="session", task_id="other-task", run_id="other-run")
    )
    with pytest.raises(ContractError, match="different Task/Run"):
        await control.reserve_typed_budget_dispatch(
            _request("other-task", "other-run"), now=110.0
        )


@pytest.mark.asyncio
async def test_model_and_auxiliary_budget_adapter_cover_plan_root_and_child_grant(control) -> None:
    claimed = await _claim_plan_task(control)
    parent = claimed.task
    child = TaskAdmission(
        namespace="ns",
        session_id="session",
        task_id="budgeted-child",
        run_id="budgeted-child-run",
        root_task_id=parent.task_id,
        parent_task_id=parent.task_id,
        depth=1,
    )
    spawn_identity = "budgeted-child-spawn"
    await control.register_child(
        ChildRelation(
            parent_task_id=parent.task_id,
            child_task_id=child.task_id,
            spawn_identity=spawn_identity,
            namespace="ns",
            session_id="session",
        ),
        child_task=child,
        now=104.0,
    )
    owners = (
        BudgetOwnerRef(BudgetOwnerKind.PLAN, "ns", "budget-plan"),
        BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, "ns", parent.task_id),
        BudgetOwnerRef(BudgetOwnerKind.TASK_GRANT, "ns", spawn_identity),
    )
    for owner in owners:
        await control.put_budget_policy(_policy(owner, 3))

    budget = TypedDispatchBudget(control, task_id=child.task_id)
    primary = await budget.reserve(
        {"task_id": child.task_id, "run_id": child.run_id, "request_instance": "child-primary"}
    )
    auxiliary = await budget.reserve_auxiliary(
        {"task_id": child.task_id, "run_id": child.run_id}, kind="summary"
    )
    for reservation_set in (primary, auxiliary):
        rows = budget._reservation_items(reservation_set)  # noqa: SLF001 - inspect scope coverage
        assert len(rows) == 1
        assert {item.owner for item in rows[0].coverage} == set(owners)

    await budget.settle(primary, {"status": "completed", "usage": {}})
    await budget.settle_auxiliary(auxiliary, usage={})
    for owner in owners:
        usage = await control.read_typed_budget_usage(owner, BudgetUnit.MODEL_REQUESTS)
        assert (usage.actual, usage.reserved, usage.unknown) == (2, 0, 0)


@pytest.mark.asyncio
async def test_a_short_scope_refuses_atomically_before_any_coverage_is_written(control) -> None:
    claimed = await _claim_plan_task(control)
    task = claimed.task
    plan_owner = BudgetOwnerRef(BudgetOwnerKind.PLAN, "ns", "budget-plan")
    root_owner = BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, "ns", task.task_id)
    grant_owner = BudgetOwnerRef(BudgetOwnerKind.TASK_GRANT, "ns", "spawn-short")
    await control.put_budget_policy(_policy(plan_owner, 5))
    await control.put_budget_policy(_policy(root_owner, 5))
    await control.put_budget_policy(_policy(grant_owner, 0))

    child = TaskAdmission(
        namespace="ns", session_id="session", task_id="child-short", run_id="run-short",
        root_task_id=task.task_id, parent_task_id=task.task_id, depth=1,
    )
    await control.register_child(
        ChildRelation(
            parent_task_id=task.task_id,
            child_task_id=child.task_id,
            spawn_identity="spawn-short",
            namespace="ns",
            session_id="session",
        ),
        child_task=child,
        now=104.0,
    )
    # The policy was installed before the relation to prove that scope matching is based on the
    # committed stable spawn identity, not a caller-provided scope list.
    with pytest.raises(BudgetRefused) as refused:
        await control.reserve_typed_budget_dispatch(_request("child-short", "run-short"), now=105.0)
    assert refused.value.code == "budget_refused"
    for owner in (plan_owner, root_owner, grant_owner):
        usage = await control.read_typed_budget_usage(owner, BudgetUnit.MODEL_REQUESTS)
        assert usage.used == 0


@pytest.mark.asyncio
async def test_unknown_keeps_its_reserved_coverage(control) -> None:
    task = await control.admit_task(
        TaskAdmission(namespace="ns", session_id="session", task_id="root", run_id="root-run")
    )
    owner = BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, "ns", task.task_id)
    await control.put_budget_policy(_policy(owner, 1))
    reserved = await control.reserve_typed_budget_dispatch(_request("root", "root-run"), now=1.0)
    unknown = await control.settle_typed_budget_reservation(
        reserved.reservation_id, unknown="transport outcome missing", now=2.0
    )
    assert unknown.state is ReservationState.UNKNOWN
    usage = await control.read_typed_budget_usage(owner, BudgetUnit.MODEL_REQUESTS)
    assert (usage.reserved, usage.unknown, usage.used, usage.available) == (0, 1, 1, 0)


@pytest.mark.asyncio
async def test_budget_policy_updates_use_cas_and_keep_spend_across_limit_changes(control) -> None:
    await control.admit_task(
        TaskAdmission(namespace="ns", session_id="session", task_id="policy-task", run_id="policy-run")
    )
    owner = BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, "ns", "policy-task")
    first = _versioned_policy(owner, "budget-v1", 1)
    await control.put_budget_policy(first)
    one = await control.reserve_typed_budget_dispatch(
        _request("policy-task", "policy-run", "policy-spend-1"), now=10.0
    )
    await control.settle_typed_budget_reservation(one.reservation_id, actual=1, now=11.0)

    increased = _versioned_policy(owner, "budget-v2", 2)
    increase = await control.update_budget_policy(
        increased,
        expected_policy_digest=first.digest,
        actor="reviewer",
        idempotency_key="budget-change-increase-1",
        authorization_ref="approved-plan-revision:2",
        reason="approve the newly reviewed revision budget",
        now=12.0,
    )
    replayed_increase = await control.update_budget_policy(
        increased,
        expected_policy_digest=first.digest,
        actor="reviewer",
        idempotency_key="budget-change-increase-1",
        authorization_ref="approved-plan-revision:2",
        reason="approve the newly reviewed revision budget",
        now=99.0,
    )
    assert replayed_increase == increase
    assert increase.previous_digest == first.digest
    assert increase.policy_digest == increased.digest
    assert increase.actor == "reviewer"
    assert increase.authorization_ref == "approved-plan-revision:2"
    assert BudgetPolicyChangeRecord.from_record(increase.to_record()) == increase

    two = await control.reserve_typed_budget_dispatch(
        _request("policy-task", "policy-run", "policy-spend-2"), now=13.0
    )
    await control.settle_typed_budget_reservation(two.reservation_id, actual=1, now=14.0)

    lowered = _versioned_policy(owner, "budget-v3", 1)
    await control.update_budget_policy(
        lowered,
        expected_policy_digest=increased.digest,
        actor="reviewer",
        idempotency_key="budget-change-lower-1",
        authorization_ref="approved-plan-revision:3",
        reason="lower the cap without rewriting settled spend",
        now=15.0,
    )
    assert await control.read_budget_policy(owner) == lowered
    assert await control.read_budget_policy_version(owner, first.digest) == first
    changes = await control.read_budget_policy_changes(owner)
    assert [item.idempotency_key for item in changes] == [
        "budget-change-increase-1",
        "budget-change-lower-1",
    ]
    usage = await control.read_typed_budget_usage(owner, BudgetUnit.MODEL_REQUESTS)
    assert (usage.actual, usage.reserved, usage.unknown, usage.used) == (2, 0, 0, 2)

    with pytest.raises(BudgetRefused):
        await control.reserve_typed_budget_dispatch(
            _request("policy-task", "policy-run", "policy-spend-3"), now=16.0
        )
    with pytest.raises(ContractError, match="expected policy digest"):
        await control.update_budget_policy(
            _versioned_policy(owner, "budget-v4", 3),
            expected_policy_digest=first.digest,
            actor="reviewer",
            idempotency_key="budget-change-stale-cas",
            authorization_ref="approved-plan-revision:4",
            reason="stale compare and swap must not raise the cap",
            now=17.0,
        )
    with pytest.raises(ContractError, match="idempotency"):
        await control.update_budget_policy(
            _versioned_policy(owner, "budget-v2-conflict", 4),
            expected_policy_digest=first.digest,
            actor="reviewer",
            idempotency_key="budget-change-increase-1",
            authorization_ref="approved-plan-revision:2",
            reason="a key cannot describe another policy",
            now=18.0,
        )


@pytest.mark.asyncio
async def test_concurrent_budget_policy_compare_and_swap_has_one_winner(control) -> None:
    await control.admit_task(
        TaskAdmission(namespace="ns", session_id="session", task_id="cas-task", run_id="cas-run")
    )
    owner = BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, "ns", "cas-task")
    current = _versioned_policy(owner, "budget-v1", 1)
    await control.put_budget_policy(current)
    one = _versioned_policy(owner, "budget-v2", 2)
    two = _versioned_policy(owner, "budget-v3", 3)

    async def update(policy: BudgetPolicySnapshot, key: str):
        return await control.update_budget_policy(
            policy,
            expected_policy_digest=current.digest,
            actor="budget-controller",
            idempotency_key=key,
            authorization_ref=f"approved:{policy.policy_version}",
            reason="concurrent compare-and-swap probe",
            now=20.0,
        )

    results = await asyncio.gather(update(one, "cas-change-one"), update(two, "cas-change-two"), return_exceptions=True)
    winners = [item for item in results if not isinstance(item, BaseException)]
    losers = [item for item in results if isinstance(item, BaseException)]
    assert len(winners) == len(losers) == 1
    assert isinstance(losers[0], ContractError)
    assert "expected policy digest" in str(losers[0])
    latest = await control.read_budget_policy(owner)
    assert latest in {one, two}
    assert len(await control.read_budget_policy_changes(owner)) == 1


@pytest.mark.asyncio
async def test_plan_budget_is_shared_across_replanned_revision_and_new_task(control) -> None:
    first_claim = await _claim_plan_task(control)
    owner = BudgetOwnerRef(BudgetOwnerKind.PLAN, "ns", "budget-plan")
    first_policy = _versioned_policy(owner, "budget-v1", 1)
    await control.put_budget_policy(first_policy)
    first_budget = TypedDispatchBudget(control, task_id=first_claim.task.task_id)
    reservation = await first_budget.reserve(
        {
            "task_id": first_claim.task.task_id,
            "run_id": first_claim.binding.run_id,
            "request_instance": "revision-one-request",
        }
    )
    await first_budget.settle(reservation, {"status": "completed", "usage": {}})

    plan = await control.read_plan("ns", "budget-plan")
    replanning = await control.request_replan(
        "ns",
        "budget-plan",
        reason="revise the approved execution scope",
        expected_control_version=plan.record.control_version,
        request_id="budget-plan-replan",
        principal="host",
        now=104.0,
    )
    task = await control.read_task(first_claim.task.task_id)
    await control.save_task(
        task.transition(TaskStatus.COMPLETED, reason="work_completed", at=104.0),
        expected_status=task.status,
    )
    await control.commit_run_transition_with_inputs(
        first_claim.binding.run_id,
        {
            "run_id": first_claim.binding.run_id,
            "namespace": "ns",
            "session_id": "session",
            "status": "completed",
            "final_text": "settled",
        },
        expected_version=None,
        task_id=first_claim.task.task_id,
    )

    async def evidence(binding, bound_task, run_record):
        return EvidenceSnapshot(
            namespace=binding.namespace,
            plan_id=binding.plan_id,
            revision=binding.revision,
            step_id=binding.step_id,
            binding_id=binding.binding_id,
            task_id=bound_task.task_id,
            run_id=run_record.run_id,
            run_version=run_record.version,
            evidence_refs=(f"artifact:{binding.step_id}@v1",),
            payload={"result": "done"},
        )

    verifiers = VerifierRegistry()

    async def verify(_criterion, _snapshot):
        return VerificationDecision(AcceptanceResult.ACCEPTED)

    verifiers.register("host.done", "yes", verify, timeout_seconds=1, max_input_bytes=4096)
    report = await AcceptanceCoordinator(
        control, evidence, verifiers, run_store=control
    ).reconcile("ns", "budget-plan", principal="host", now=106.0)
    assert report.quiesced == 1

    current = await control.read_plan("ns", "budget-plan")
    revision_two = replace(
        current.definition,
        revision=2,
        supersedes_revision=1,
        replan_reason="revise the approved execution scope",
        execution_envelope=replace(
            current.definition.execution_envelope,
            budget_ref=VersionedRef("budget/default", "budget-v2"),
        ),
    )
    current_record = await control.publish_plan_revision(
        revision_two,
        expected_control_version=current.record.control_version,
        request_id="budget-plan-publish-v2",
        principal="host",
        now=107.0,
    )
    reviewed = await control.request_plan_review(
        "ns",
        "budget-plan",
        expected_control_version=current_record.control_version,
        request_id="budget-plan-review-v2",
        principal="host",
        now=108.0,
    )
    approved = await control.decide_plan_approval(
        "ns",
        "budget-plan",
        PlanApprovalRequest("budget-plan-approval-v2", ApprovalDecision.APPROVED, "approval-v2"),
        expected_control_version=reviewed.control_version,
        request_id="budget-plan-approve-v2",
        principal="reviewer",
        now_ms=109_000,
    )
    second_policy = _versioned_policy(owner, "budget-v2", 1)
    await control.update_budget_policy(
        second_policy,
        expected_policy_digest=first_policy.digest,
        actor="reviewer",
        idempotency_key="budget-plan-policy-v2",
        authorization_ref="budget-plan-approval-v2",
        reason="carry the approved cap into the revised plan",
        now=110.0,
    )
    second_candidate = (await control.read_ready_steps("ns", limit=8, now=110.0)).candidates[0]
    second_claim = await control.claim_plan_step(
        second_candidate,
        expected_control_version=approved.control_version,
        request_id="budget-plan-claim-v2",
        principal="worker-host",
        holder="worker-v2",
        ttl_seconds=30,
        now=110.0,
    )
    assert second_claim.binding.revision == 2
    assert second_claim.task.task_id != first_claim.task.task_id

    second_budget = TypedDispatchBudget(control, task_id=second_claim.task.task_id)
    with pytest.raises(BudgetRefused):
        await second_budget.reserve(
            {
                "task_id": second_claim.task.task_id,
                "run_id": second_claim.binding.run_id,
                "request_instance": "revision-two-request",
            }
        )
    usage = await control.read_typed_budget_usage(owner, BudgetUnit.MODEL_REQUESTS)
    assert (usage.actual, usage.reserved, usage.unknown, usage.used) == (1, 0, 0, 1)


def test_sqlite_typed_budget_v1_migrates_policy_history_atomically(tmp_path) -> None:
    path = tmp_path / "typed-budget-v1.sqlite"
    store = SqliteControlStore(path, enable_typed_budgets=True)
    run = asyncio.run

    async def setup() -> tuple[BudgetOwnerRef, BudgetPolicySnapshot]:
        await store.admit_task(
            TaskAdmission(namespace="ns", session_id="session", task_id="migration-task", run_id="migration-run")
        )
        owner = BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, "ns", "migration-task")
        policy = _versioned_policy(owner, "budget-v1", 3)
        await store.put_budget_policy(policy)
        reservation = await store.reserve_typed_budget_dispatch(
            _request("migration-task", "migration-run", "before-migration"), now=10.0
        )
        await store.settle_typed_budget_reservation(reservation.reservation_id, actual=2, now=11.0)
        return owner, policy

    owner, policy = run(setup())
    store.close()
    _downgrade_typed_budget_metadata_to_v1(path)

    migrated = SqliteControlStore(path, enable_typed_budgets=True)
    try:
        assert run(migrated.read_budget_policy(owner)) == policy
        assert run(migrated.read_budget_policy_version(owner, policy.digest)) == policy
        assert run(migrated.read_budget_policy_changes(owner)) == ()
        usage = run(migrated.read_typed_budget_usage(owner, BudgetUnit.MODEL_REQUESTS))
        assert (usage.actual, usage.reserved, usage.unknown) == (2, 0, 0)
    finally:
        migrated.close()


def test_sqlite_invalid_v1_policy_aborts_migration_without_partial_schema(tmp_path) -> None:
    path = tmp_path / "typed-budget-v1-invalid.sqlite"
    store = SqliteControlStore(path, enable_typed_budgets=True)
    run = asyncio.run

    async def setup() -> BudgetOwnerRef:
        await store.admit_task(
            TaskAdmission(namespace="ns", session_id="session", task_id="invalid-migration-task", run_id="run")
        )
        owner = BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, "ns", "invalid-migration-task")
        await store.put_budget_policy(_versioned_policy(owner, "budget-v1", 3))
        return owner

    run(setup())
    store.close()
    _downgrade_typed_budget_metadata_to_v1(path)
    connection = sqlite3.connect(path)
    try:
        connection.execute("UPDATE typed_budget_policies SET record_json = '{}' ")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(ContractError):
        SqliteControlStore(path, enable_typed_budgets=True)

    connection = sqlite3.connect(path)
    try:
        version = connection.execute(
            "SELECT value FROM control_meta WHERE key = 'typed_budget_schema_version'"
        ).fetchone()[0]
        table_names = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert version == "1"
        assert "typed_budget_policy_versions" not in table_names
        assert "typed_budget_policy_changes" not in table_names
    finally:
        connection.close()


@pytest.mark.parametrize("control_kind", ["memory", "sqlite"])
def test_budget_policy_update_cannot_change_cost_currency(control_kind, tmp_path) -> None:
    if control_kind == "memory":
        control = MemoryControlStore(enable_typed_budgets=True)
    else:
        control = SqliteControlStore(tmp_path / "currency-update.sqlite", enable_typed_budgets=True)
    owner = BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, "ns", "currency-owner")
    current = BudgetPolicySnapshot(
        owner=owner,
        session_id="session",
        policy_version="currency-v1",
        limits=(
            BudgetLimit(
                BudgetUnit.COST_MICROS,
                100,
                hard_bound_ref="host:cost-bound",
                currency="USD",
                price_version="price-v1",
            ),
        ),
    )
    changed = BudgetPolicySnapshot(
        owner=owner,
        session_id="session",
        policy_version="currency-v2",
        limits=(
            BudgetLimit(
                BudgetUnit.COST_MICROS,
                200,
                hard_bound_ref="host:cost-bound",
                currency="EUR",
                price_version="price-v2",
            ),
        ),
    )
    try:
        asyncio.run(control.put_budget_policy(current))
        with pytest.raises(ContractError, match="cannot change cost currency"):
            asyncio.run(
                control.update_budget_policy(
                    changed,
                    expected_policy_digest=current.digest,
                    actor="host",
                    idempotency_key="currency-change",
                    authorization_ref="approval:currency",
                    reason="currency changed",
                    now=3.0,
                )
            )
        assert asyncio.run(control.read_budget_policy(owner)) == current
        assert asyncio.run(control.read_budget_policy_changes(owner)) == ()
    finally:
        if control_kind == "sqlite":
            control.close()


def test_sqlite_policy_change_audit_failure_rolls_back_snapshot_and_history(tmp_path) -> None:
    store = SqliteControlStore(tmp_path / "budget-policy-audit.sqlite", enable_typed_budgets=True)
    run = asyncio.run
    run(
        store.admit_task(
            TaskAdmission(namespace="ns", session_id="session", task_id="task", run_id="run")
        )
    )
    owner = BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, "ns", "task")
    first = _versioned_policy(owner, "budget-v1", 1)
    second = _versioned_policy(owner, "budget-v2", 2)
    run(store.put_budget_policy(first))
    store._connection.execute(  # noqa: SLF001 - inject a failure inside the policy transaction
        "CREATE TRIGGER fail_budget_policy_audit BEFORE INSERT ON typed_budget_policy_changes "
        "BEGIN SELECT RAISE(ABORT, 'policy audit failure'); END"
    )
    try:
        with pytest.raises(sqlite3.IntegrityError, match="policy audit failure"):
            run(
                store.update_budget_policy(
                    second,
                    expected_policy_digest=first.digest,
                    actor="reviewer",
                    idempotency_key="audit-failure",
                    authorization_ref="approved:revision-2",
                    reason="injected audit insert failure",
                    now=20.0,
                )
            )
        assert run(store.read_budget_policy(owner)) == first
        assert run(store.read_budget_policy_version(owner, second.digest)) is None
        assert run(store.read_budget_policy_changes(owner)) == ()
    finally:
        store.close()


def test_read_only_typed_budget_v1_refuses_without_migration(tmp_path) -> None:
    path = tmp_path / "typed-budget-v1-read-only.sqlite"
    store = SqliteControlStore(path, enable_typed_budgets=True)
    store.close()
    _downgrade_typed_budget_metadata_to_v1(path)

    with pytest.raises(ContractError, match="schema is incomplete or unsupported"):
        SqliteControlStore(path, read_only=True, enable_typed_budgets=True)

    connection = sqlite3.connect(path)
    try:
        version = connection.execute(
            "SELECT value FROM control_meta WHERE key = 'typed_budget_schema_version'"
        ).fetchone()[0]
        table_names = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert version == "1"
        assert "typed_budget_policy_versions" not in table_names
        assert "typed_budget_policy_changes" not in table_names
    finally:
        connection.close()


def test_v1_typed_budget_schema_does_not_block_control_store_when_capability_disabled(tmp_path) -> None:
    path = tmp_path / "typed-budget-v1-disabled.sqlite"
    enabled = SqliteControlStore(path, enable_typed_budgets=True)
    asyncio.run(
        enabled.admit_task(
            TaskAdmission(namespace="ns", session_id="session", task_id="legacy-task", run_id="legacy-run")
        )
    )
    enabled.close()
    _downgrade_typed_budget_metadata_to_v1(path)

    control = SqliteControlStore(path)
    try:
        task = asyncio.run(control.read_task("legacy-task"))
        assert task is not None and task.run_id == "legacy-run"
        assert not control._typed_budget_enabled
    finally:
        control.close()

    connection = sqlite3.connect(path)
    try:
        version = connection.execute(
            "SELECT value FROM control_meta WHERE key = 'typed_budget_schema_version'"
        ).fetchone()[0]
        assert version == "1"
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_legacy_budget_migration_preserves_facts_under_task_root_only(control) -> None:
    claimed = await _claim_plan_task(control)
    task = claimed.task
    root_owner = BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, "ns", task.task_id)

    settled = BudgetReservation(
        root_task_id=task.task_id,
        task_id=task.task_id,
        unit=BudgetUnit.MODEL_REQUESTS,
        amount=2.0,
        reservation_id="legacy-settled",
        dispatch_identity="legacy-dispatch-settled",
        state=ReservationState.SETTLED,
        settled_amount=3.0,
        created_at=104.0,
        settled_at=105.0,
    )
    unknown = BudgetReservation(
        root_task_id=task.task_id,
        task_id=task.task_id,
        unit=BudgetUnit.DISPATCHES,
        amount=1.0,
        reservation_id="legacy-unknown",
        dispatch_identity="legacy-dispatch-unknown",
        state=ReservationState.UNKNOWN,
        note="result missing",
        created_at=106.0,
    )
    ambiguous = BudgetReservation(
        root_task_id=task.task_id,
        task_id=task.task_id,
        unit=BudgetUnit.TOKENS,
        amount=8.0,
        reservation_id="legacy-tokens",
        dispatch_identity="legacy-dispatch-tokens",
        created_at=107.0,
    )
    fractional = BudgetReservation(
        root_task_id=task.task_id,
        task_id=task.task_id,
        unit=BudgetUnit.MODEL_REQUESTS,
        amount=1.5,
        reservation_id="legacy-fractional",
        dispatch_identity="legacy-dispatch-fractional",
        created_at=108.0,
    )
    estimated = BudgetReservation(
        root_task_id=task.task_id,
        task_id=task.task_id,
        unit=BudgetUnit.TURNS,
        amount=1.0,
        reservation_id="legacy-estimated",
        dispatch_identity="legacy-dispatch-estimated",
        state=ReservationState.SETTLED,
        settled_amount=1.0,
        estimated=True,
        created_at=109.0,
        settled_at=110.0,
    )
    for reservation in (settled, unknown, ambiguous, fractional, estimated):
        await control.reserve_budget(
            root_task_id=task.task_id,
            task_id=task.task_id,
            reservation=reservation,
            now=reservation.created_at,
        )

    report = await control.migrate_legacy_budget_reservations(task.task_id)
    assert report.root_task_id == task.task_id
    statuses = {item.source_reservation_id_digest: item.status for item in report.entries}
    # The report hashes source ids so diagnostics do not have to echo arbitrary legacy identifiers.
    import hashlib

    digest = lambda value: hashlib.sha256(value.encode("utf-8")).hexdigest()
    assert statuses[digest("legacy-settled")] == "migrated"
    assert statuses[digest("legacy-unknown")] == "migrated"
    assert statuses[digest("legacy-tokens")] == "skipped"
    assert statuses[digest("legacy-fractional")] == "skipped"
    assert statuses[digest("legacy-estimated")] == "skipped"

    typed = await control.read_typed_budget_reservations_for_owner(root_owner, limit=20)
    migrated = {item.note.split(":", 1)[1]: item for item in typed if item.note.startswith("legacy_source:")}
    assert set(migrated) == {digest("legacy-settled"), digest("legacy-unknown")}
    for reservation in migrated.values():
        assert tuple(item.owner for item in reservation.coverage) == (root_owner,)
    assert migrated[digest("legacy-settled")].settled_actual == 3
    assert migrated[digest("legacy-unknown")].state is ReservationState.UNKNOWN
    usage = await control.read_typed_budget_usage(root_owner, BudgetUnit.MODEL_REQUESTS)
    assert (usage.actual, usage.reserved, usage.unknown) == (3, 0, 0)
    assert len(await control.read_reservations(task.task_id)) == 5

    replay = await control.migrate_legacy_budget_reservations(task.task_id)
    assert {item.status for item in replay.entries} == {"already_migrated", "skipped"}
    assert len(await control.read_typed_budget_reservations_for_owner(root_owner, limit=20)) == 2
    await control.settle_typed_budget_reservation(
        migrated[digest("legacy-unknown")].reservation_id,
        actual=2,
        now=111.0,
    )
    replay_after_settlement = await control.migrate_legacy_budget_reservations(task.task_id)
    assert {item.status for item in replay_after_settlement.entries} == {
        "already_migrated",
        "skipped",
    }


@pytest.mark.asyncio
async def test_legacy_budget_migration_rolls_back_all_imports_on_sqlite_failure(tmp_path) -> None:
    path = tmp_path / "legacy-budget-rollback.sqlite"
    control = SqliteControlStore(
        path, enable_orchestration=True, enable_typed_budgets=True
    )
    try:
        claimed = await _claim_plan_task(control)
        task = claimed.task
        for index in range(2):
            reservation = BudgetReservation(
                root_task_id=task.task_id,
                task_id=task.task_id,
                unit=BudgetUnit.MODEL_REQUESTS,
                amount=1.0,
                reservation_id=f"legacy-rollback-{index}",
                dispatch_identity=f"legacy-rollback-dispatch-{index}",
                created_at=200.0 + index,
            )
            await control.reserve_budget(
                root_task_id=task.task_id,
                task_id=task.task_id,
                reservation=reservation,
                now=reservation.created_at,
            )
        control._connection.execute(
            "CREATE TRIGGER fail_typed_budget_import BEFORE INSERT ON typed_budget_coverage "
            "BEGIN SELECT RAISE(ABORT, 'injected migration failure'); END"
        )
        control._connection.commit()

        with pytest.raises(sqlite3.IntegrityError, match="injected migration failure"):
            await control.migrate_legacy_budget_reservations(task.task_id)

        root_owner = BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, "ns", task.task_id)
        assert await control.read_typed_budget_reservations_for_owner(root_owner) == ()
        assert len(await control.read_reservations(task.task_id)) == 2
    finally:
        control.close()


@pytest.mark.asyncio
async def test_legacy_budget_migration_refuses_read_only_store(tmp_path) -> None:
    path = tmp_path / "legacy-budget-readonly.sqlite"
    writable = SqliteControlStore(path, enable_typed_budgets=True)
    await writable.admit_task(
        TaskAdmission(namespace="ns", session_id="session", task_id="root", run_id="run")
    )
    writable.close()

    readonly = SqliteControlStore(path, read_only=True, enable_typed_budgets=True)
    try:
        with pytest.raises(ContractError, match="writable control store"):
            await readonly.migrate_legacy_budget_reservations("root")
    finally:
        readonly.close()


@pytest.mark.asyncio
async def test_settlement_committed_before_reply_is_idempotent_when_retried(control, monkeypatch) -> None:
    await control.admit_task(
        TaskAdmission(
            namespace="ns", session_id="session", task_id="settle-task", run_id="settle-run"
        )
    )
    reservation = await control.reserve_typed_budget_dispatch(
        _request("settle-task", "settle-run", "settle-reply-loss"), now=10.0
    )
    original = control.settle_typed_budget_reservation
    dropped = False

    async def commit_then_drop_reply(reservation_id: str, **kwargs: Any) -> Any:
        nonlocal dropped
        result = await original(reservation_id, **kwargs)
        if not dropped:
            dropped = True
            raise RuntimeError("settlement reply was lost after commit")
        return result

    monkeypatch.setattr(control, "settle_typed_budget_reservation", commit_then_drop_reply)
    with pytest.raises(RuntimeError, match="reply was lost"):
        await control.settle_typed_budget_reservation(
            reservation.reservation_id, actual=1, note="provider completed", now=11.0
        )
    committed = await control.read_typed_budget_reservation(reservation.reservation_id)
    replay = await control.settle_typed_budget_reservation(
        reservation.reservation_id, actual=1, note="provider completed", now=12.0
    )
    assert committed is not None and committed.state is ReservationState.SETTLED
    assert replay == committed


@pytest.mark.asyncio
async def test_cost_scopes_must_share_currency_and_price_version(control) -> None:
    claimed = await _claim_plan_task(control)
    task = claimed.task
    plan_owner = BudgetOwnerRef(BudgetOwnerKind.PLAN, "ns", "budget-plan")
    root_owner = BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, "ns", task.task_id)
    await control.put_budget_policy(_cost_policy(plan_owner, "USD", "price-v1"))
    await control.put_budget_policy(_cost_policy(root_owner, "EUR", "price-v1"))

    conflicting = TypedBudgetDispatch(
        task_id=task.task_id,
        run_id=task.run_id,
        dispatch_identity="cost-conflict",
        request_instance="cost-conflict-instance",
        unit=BudgetUnit.COST_MICROS,
        amount=10,
        currency="USD",
        price_version="price-v1",
    )
    with pytest.raises(ContractError, match="same currency and price version"):
        await control.reserve_typed_budget_dispatch(conflicting, now=104.0)
    assert (await control.read_typed_budget_usage(plan_owner, BudgetUnit.COST_MICROS)).used == 0
    assert (await control.read_typed_budget_usage(root_owner, BudgetUnit.COST_MICROS)).used == 0

    # A standalone Task scope also rejects a dispatch whose pricing version differs, while a
    # matching observe scope records usage without reporting a hard available limit.
    await control.admit_task(
        TaskAdmission(namespace="ns", session_id="session", task_id="cost-only", run_id="cost-only-run")
    )
    cost_owner = BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, "ns", "cost-only")
    await control.put_budget_policy(_cost_policy(cost_owner, "USD", "price-v1"))
    with pytest.raises(ContractError, match="same currency and price version"):
        await control.reserve_typed_budget_dispatch(
            TypedBudgetDispatch(
                task_id="cost-only",
                run_id="cost-only-run",
                dispatch_identity="cost-price-mismatch",
                request_instance="cost-price-mismatch-instance",
                unit=BudgetUnit.COST_MICROS,
                amount=10,
                currency="USD",
                price_version="price-v2",
            ),
            now=105.0,
        )
    settled_cost = await control.reserve_typed_budget_dispatch(
        TypedBudgetDispatch(
            task_id="cost-only",
            run_id="cost-only-run",
            dispatch_identity="cost-matching-version",
            request_instance="cost-matching-version-instance",
            unit=BudgetUnit.COST_MICROS,
            amount=10,
            currency="USD",
            price_version="price-v1",
        ),
        now=106.0,
    )
    assert settled_cost.coverage[0].mode is BudgetLimitMode.OBSERVE
    cost_usage = await control.read_typed_budget_usage(cost_owner, BudgetUnit.COST_MICROS)
    assert cost_usage.used == 10
    assert cost_usage.available is None


@pytest.mark.asyncio
async def test_actual_overage_is_kept_and_closes_new_admission(control) -> None:
    task = await control.admit_task(
        TaskAdmission(namespace="ns", session_id="session", task_id="overage", run_id="overage-run")
    )
    owner = BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, "ns", task.task_id)
    await control.put_budget_policy(_policy(owner, 1))
    reserved = await control.reserve_typed_budget_dispatch(_request("overage", "overage-run"), now=1.0)
    settled = await control.settle_typed_budget_reservation(
        reserved.reservation_id, actual=2, note="provider reported overage", now=2.0
    )
    assert settled.settled_actual == 2
    usage = await control.read_typed_budget_usage(owner, BudgetUnit.MODEL_REQUESTS)
    assert (usage.actual, usage.used, usage.available, usage.over_limit) == (2, 2, 0, 1)
    with pytest.raises(BudgetRefused):
        await control.reserve_typed_budget_dispatch(
            _request("overage", "overage-run", "request-after-overage"), now=3.0
        )


@pytest.mark.asyncio
async def test_failure_after_canonical_insert_rolls_back_reservation_and_coverage(control, monkeypatch) -> None:
    task = await control.admit_task(
        TaskAdmission(namespace="ns", session_id="session", task_id="fault", run_id="fault-run")
    )
    owner = BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, "ns", task.task_id)
    await control.put_budget_policy(_policy(owner, 2))
    insert = control._typed_budget_insert_reservation_locked

    def fail_after_insert(reservation):
        insert(reservation)
        raise RuntimeError("injected failure after canonical reservation insert")

    monkeypatch.setattr(control, "_typed_budget_insert_reservation_locked", fail_after_insert)
    with pytest.raises(RuntimeError, match="after canonical reservation insert"):
        await control.reserve_typed_budget_dispatch(_request("fault", "fault-run"), now=1.0)
    usage = await control.read_typed_budget_usage(owner, BudgetUnit.MODEL_REQUESTS)
    assert usage.used == 0


def _contend_for_last_budget(path: str, identity: str, barrier: Any, queue: Any) -> None:
    store = SqliteControlStore(path, enable_typed_budgets=True)
    try:
        barrier.wait(timeout=10)
        try:
            asyncio.run(
                store.reserve_typed_budget_dispatch(
                    _request("root", "root-run", identity), now=3.0
                )
            )
        except Exception as exc:  # the child process returns only a compact result to the parent
            queue.put((identity, getattr(exc, "code", type(exc).__name__)))
        else:
            queue.put((identity, "reserved"))
    finally:
        store.close()


def test_independent_processes_contend_for_the_last_typed_budget_unit(tmp_path) -> None:
    path = tmp_path / "typed-budget-race.sqlite"
    store = SqliteControlStore(path, enable_typed_budgets=True)
    asyncio.run(
        store.admit_task(
            TaskAdmission(namespace="ns", session_id="session", task_id="root", run_id="root-run")
        )
    )
    owner = BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, "ns", "root")
    asyncio.run(store.put_budget_policy(_policy(owner, 1)))
    store.close()

    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    queue = context.Queue()
    processes = [
        context.Process(target=_contend_for_last_budget, args=(str(path), f"request-{n}", barrier, queue))
        for n in (1, 2)
    ]
    for process in processes:
        process.start()
    results = [queue.get(timeout=20) for _ in processes]
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0
    assert sorted(result for _, result in results) == ["budget_refused", "reserved"]


@pytest.mark.asyncio
async def test_plan_records_cannot_be_reopened_without_orchestration_and_losing_budget_scope(tmp_path) -> None:
    path = tmp_path / "typed-budget-plan-scope.sqlite"
    store = SqliteControlStore(
        path, enable_orchestration=True, enable_typed_budgets=True
    )
    try:
        claimed = await _claim_plan_task(store)
        task = claimed.task
        plan_owner = BudgetOwnerRef(BudgetOwnerKind.PLAN, "ns", "budget-plan")
        root_owner = BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, "ns", task.task_id)
        await store.put_budget_policy(_policy(plan_owner, 3))
        await store.put_budget_policy(_policy(root_owner, 3))
    finally:
        store.close()

    with pytest.raises(PlanRefused, match="reopen it with orchestration enabled"):
        SqliteControlStore(path, enable_typed_budgets=True)
    reopened = SqliteControlStore(
        path, enable_orchestration=True, enable_typed_budgets=True
    )
    try:
        reservation = await reopened.reserve_typed_budget_dispatch(
            _request(task.task_id, task.run_id, "plan-bound-after-reopen"), now=104.0
        )
        assert {item.owner for item in reservation.coverage} == {plan_owner, root_owner}
    finally:
        reopened.close()


def test_read_only_typed_budget_store_requires_an_existing_complete_schema(tmp_path) -> None:
    typed_path = tmp_path / "typed-budget-read-only.sqlite"
    writable = SqliteControlStore(typed_path, enable_typed_budgets=True)
    writable.close()
    readonly = SqliteControlStore(
        typed_path, read_only=True, enable_typed_budgets=True
    )
    readonly.close()

    legacy_path = tmp_path / "legacy-read-only.sqlite"
    legacy = SqliteControlStore(legacy_path)
    legacy.close()
    with pytest.raises(ContractError, match="no typed budget schema"):
        SqliteControlStore(
            legacy_path, read_only=True, enable_typed_budgets=True
        )


@pytest.mark.asyncio
async def test_reserved_and_unknown_usage_survive_sqlite_reopen(tmp_path) -> None:
    path = tmp_path / "typed-budget-recovery.sqlite"
    store = SqliteControlStore(path, enable_typed_budgets=True)
    task = await store.admit_task(
        TaskAdmission(namespace="ns", session_id="session", task_id="recover", run_id="recover-run")
    )
    owner = BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, "ns", task.task_id)
    await store.put_budget_policy(_policy(owner, 2))
    reserved = await store.reserve_typed_budget_dispatch(_request("recover", "recover-run"), now=1.0)
    store.close()

    reopened = SqliteControlStore(path, enable_typed_budgets=True)
    unknown = await reopened.settle_typed_budget_reservation(
        reserved.reservation_id, unknown="provider response was lost", now=2.0
    )
    reopened.close()

    recovered = SqliteControlStore(path, enable_typed_budgets=True)
    try:
        assert await recovered.read_typed_budget_reservation(reserved.reservation_id) == unknown
        usage = await recovered.read_typed_budget_usage(owner, BudgetUnit.MODEL_REQUESTS)
        assert (usage.actual, usage.reserved, usage.unknown, usage.used) == (0, 0, 1, 1)
    finally:
        recovered.close()


@pytest.mark.asyncio
async def test_late_usage_report_resolves_unknown_once_and_preserves_actual_overage(control) -> None:
    task_id, run_id = "late-task", "late-run"
    await control.admit_task(
        TaskAdmission(namespace="ns", session_id="session", task_id=task_id, run_id=run_id)
    )
    owner = BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, "ns", task_id)
    policy = BudgetPolicySnapshot(
        owner=owner,
        session_id="session",
        policy_version="late-usage-v1",
        limits=(
            BudgetLimit(BudgetUnit.MODEL_REQUESTS, 2),
            BudgetLimit(
                BudgetUnit.INPUT_TOKENS,
                1,
                hard_bound_ref="host:token-upper-bound-v1",
            ),
        ),
    )
    await control.put_budget_policy(policy)
    reserved = await control.reserve_typed_budget_dispatch(
        _request(task_id, run_id, "late-request"), now=200.0
    )
    unknown = await control.settle_typed_budget_reservation(
        reserved.reservation_id, unknown="the provider timed out before usage arrived", now=201.0
    )
    resolved = await control.record_typed_budget_usage(
        unknown.reservation_id,
        report_id="provider-final:late-request",
        unit=BudgetUnit.MODEL_REQUESTS,
        actual=1,
        now=202.0,
    )
    replay = await control.record_typed_budget_usage(
        unknown.reservation_id,
        report_id="provider-final:late-request",
        unit=BudgetUnit.MODEL_REQUESTS,
        actual=1,
        now=203.0,
    )
    assert resolved.state is ReservationState.SETTLED and resolved.settled_actual == 1
    assert replay == resolved
    with pytest.raises(ContractError, match="different report identity"):
        await control.record_typed_budget_usage(
            unknown.reservation_id,
            report_id="provider-final:other-report",
            unit=BudgetUnit.MODEL_REQUESTS,
            actual=1,
            now=203.5,
        )

    token_report = await control.record_typed_budget_usage(
        unknown.reservation_id,
        report_id="provider-final:late-request",
        unit=BudgetUnit.INPUT_TOKENS,
        actual=3,
        now=204.0,
    )
    token_replay = await control.record_typed_budget_usage(
        unknown.reservation_id,
        report_id="provider-final:late-request",
        unit=BudgetUnit.INPUT_TOKENS,
        actual=3,
        now=205.0,
    )
    assert token_replay == token_report
    with pytest.raises(ContractError, match="different unit or amount"):
        await control.record_typed_budget_usage(
            unknown.reservation_id,
            report_id="provider-final:late-request",
            unit=BudgetUnit.INPUT_TOKENS,
            actual=4,
            now=206.0,
        )

    requests = await control.read_typed_budget_usage(owner, BudgetUnit.MODEL_REQUESTS)
    tokens = await control.read_typed_budget_usage(owner, BudgetUnit.INPUT_TOKENS)
    assert (requests.actual, requests.unknown, requests.reserved) == (1, 0, 0)
    assert (tokens.actual, tokens.over_limit, tokens.available) == (3, 2, 0)
