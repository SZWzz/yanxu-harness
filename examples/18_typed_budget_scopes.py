"""Offline B1 demonstration of one typed reservation covering committed scopes.

This is a storage example only: it does not call a Provider, tool handler or external service.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from yanxu_harness.plans import (
    AcceptanceCriterion,
    ApprovalDecision,
    ExecutionEnvelope,
    PlanApprovalRequest,
    PlanDefinition,
    StepDefinition,
    VersionedRef,
)
from yanxu_harness.tasks import (
    BudgetLimit,
    BudgetOwnerKind,
    BudgetOwnerRef,
    BudgetPolicySnapshot,
    BudgetUnit,
    SqliteControlStore,
    TaskAdmission,
    TypedBudgetDispatch,
)
from yanxu_harness.tasks.types import ChildRelation


def _emit(value: dict[str, object]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _policy(owner: BudgetOwnerRef) -> BudgetPolicySnapshot:
    return BudgetPolicySnapshot(
        owner=owner,
        session_id="session",
        policy_version="budget-v1",
        limits=(BudgetLimit(BudgetUnit.MODEL_REQUESTS, 2),),
    )


async def _run(control_path: Path) -> None:
    store = SqliteControlStore(
        control_path,
        enable_orchestration=True,
        enable_typed_budgets=True,
    )
    try:
        definition = PlanDefinition(
            namespace="ns",
            session_id="session",
            plan_id="budget-demo",
            revision=1,
            title="Typed budget scopes",
            goal="Show one reservation across committed scopes",
            created_by="host",
            steps=(
                StepDefinition(
                    step_id="inspect",
                    title="Inspect",
                    instruction="Inspect the approved item",
                    acceptance_criteria=(AcceptanceCriterion("found", "host.found", "yes"),),
                ),
            ),
            execution_envelope=ExecutionEnvelope(
                profile_ref=VersionedRef("profile/default", "1"),
                workspace_ref=VersionedRef("workspace/demo", "1"),
                policy_ref=VersionedRef("policy/default", "1"),
                deployment_ref=VersionedRef("deployment/local", "1"),
                budget_ref=VersionedRef("budget/demo", "budget-v1"),
            ),
        )
        plan = await store.create_plan(
            definition, request_id="create-budget-demo", principal="host"
        )
        plan = await store.request_plan_review(
            "ns",
            "budget-demo",
            expected_control_version=plan.control_version,
            request_id="review-budget-demo",
            principal="host",
        )
        plan = await store.decide_plan_approval(
            "ns",
            "budget-demo",
            PlanApprovalRequest(
                decision_id="approve-budget-demo",
                decision=ApprovalDecision.APPROVED,
                approval_policy_version="approval-v1",
            ),
            expected_control_version=plan.control_version,
            request_id="approve-budget-demo",
            principal="reviewer",
        )
        candidate = (await store.read_ready_steps("ns", limit=4)).candidates[0]
        plan_claim = await store.claim_plan_step(
            candidate,
            expected_control_version=plan.control_version,
            request_id="claim-budget-demo-step",
            principal="worker-host",
            holder="plan-worker",
            ttl_seconds=60,
        )

        child_admission = TaskAdmission(
            namespace="ns",
            session_id="session",
            task_id="budget-child",
            run_id="budget-child-run",
            root_task_id=plan_claim.task.task_id,
            parent_task_id=plan_claim.task.task_id,
            depth=1,
        )
        spawn_identity = "budget-demo-spawn"
        await store.register_child(
            ChildRelation(
                parent_task_id=plan_claim.task.task_id,
                child_task_id=child_admission.task_id,
                spawn_identity=spawn_identity,
                namespace="ns",
                session_id="session",
            ),
            child_task=child_admission,
        )

        owners = (
            BudgetOwnerRef(BudgetOwnerKind.PLAN, "ns", "budget-demo"),
            BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, "ns", plan_claim.task.task_id),
            BudgetOwnerRef(BudgetOwnerKind.TASK_GRANT, "ns", spawn_identity),
        )
        for owner in owners:
            await store.put_budget_policy(_policy(owner))

        child_claim = await store.claim_task(
            child_admission.task_id,
            holder="child-worker",
            ttl_seconds=60,
        )
        assert child_claim is not None
        dispatch = TypedBudgetDispatch(
            task_id=child_admission.task_id,
            run_id=child_admission.run_id,
            dispatch_identity="budget-demo-request-1",
            request_instance="request-instance-1",
            unit=BudgetUnit.MODEL_REQUESTS,
            amount=1,
        )
        reservation = await store.reserve_typed_budget_dispatch(dispatch, claim=child_claim)
        replay = await store.reserve_typed_budget_dispatch(dispatch, claim=child_claim)
        usage = [
            await store.read_typed_budget_usage(owner, BudgetUnit.MODEL_REQUESTS)
            for owner in owners
        ]
        _emit(
            {
                "stage": "reserved",
                "reservation_id": reservation.reservation_id,
                "same_idempotent_reservation": replay.reservation_id == reservation.reservation_id,
                "covered_owners": [list(item.owner.key) for item in reservation.coverage],
                "reserved_by_scope": [item.reserved for item in usage],
                "external_dispatch": "not_performed",
            }
        )
        released = await store.settle_typed_budget_reservation(
            reservation.reservation_id,
            release=True,
            note="the offline example did not open an external dispatch window",
        )
        _emit(
            {
                "stage": "released_before_dispatch",
                "state": released.state.value,
                "used_by_scope": [
                    (await store.read_typed_budget_usage(owner, BudgetUnit.MODEL_REQUESTS)).used
                    for owner in owners
                ],
                "provider_calls": 0,
            }
        )
    finally:
        store.close()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="yanxu-typed-budget-") as directory:
        asyncio.run(_run(Path(directory) / "control.sqlite"))


if __name__ == "__main__":
    main()
