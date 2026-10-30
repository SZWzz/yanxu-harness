from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from yanxu_harness.diagnostics import (
    PlanInspector,
    TaskInspector,
    plan_report_to_json,
    plan_report_to_markdown,
)
from yanxu_harness.diagnostics.__main__ import main as diagnostics_main
from yanxu_harness.errors import ConfigurationError, ContractError
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
    BudgetLimitMode,
    BudgetOwnerKind,
    BudgetOwnerRef,
    BudgetPolicySnapshot,
    BudgetUnit,
    SqliteControlStore,
    TypedBudgetDispatch,
)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def _definition(*, max_concurrency: int = 2, parallel_steps: bool = False) -> PlanDefinition:
    return PlanDefinition(
        namespace="ns",
        session_id="s1",
        plan_id="inspect-plan",
        revision=1,
        title="Inspect facts",
        goal="SECRET-PLAN-GOAL",
        created_by="host",
        steps=(
            StepDefinition(
                "first", "Read source", "SECRET-STEP-INSTRUCTION",
                (AcceptanceCriterion("done", "host.result", "1"),),
            ),
            StepDefinition(
                "second", "Verify result", "SECRET-DOWNSTREAM-INSTRUCTION",
                (AcceptanceCriterion("verified", "host.result", "1"),),
                depends_on=() if parallel_steps else ("first",),
            ),
        ),
        execution_envelope=ExecutionEnvelope(
            profile_ref=VersionedRef("profile/default", "1"),
            workspace_ref=VersionedRef("workspace/demo", "v2"),
            policy_ref=VersionedRef("policy/default", "3"),
            deployment_ref=VersionedRef("deployment/local", "1"),
            budget_ref=VersionedRef("budget/referenced-only", "4"),
        ),
        max_step_executions=4,
        max_concurrency=max_concurrency,
        deadline_at_ms=500_000,
    )


def _seed(path: Path, plan_definition: PlanDefinition | None = None) -> None:
    async def setup() -> None:
        control = SqliteControlStore(path, enable_orchestration=True)
        try:
            definition = plan_definition or _definition()
            record = await control.create_plan(
                definition, request_id="create-inspect", principal="host", now=100.0
            )
            record = await control.request_plan_review(
                "ns", "inspect-plan", expected_control_version=record.control_version,
                request_id="review-inspect", principal="host", now=101.0,
            )
            record = await control.decide_plan_approval(
                "ns", "inspect-plan",
                PlanApprovalRequest(
                    decision_id="approved-inspect",
                    decision=ApprovalDecision.APPROVED,
                    approval_policy_version="review-v1",
                ),
                expected_control_version=record.control_version,
                request_id="approve-inspect", principal="reviewer", now_ms=102_000,
            )
            candidate = (await control.read_ready_steps("ns", limit=8, now=103.0)).candidates[0]
            claimed = await control.claim_plan_step(
                candidate,
                expected_control_version=record.control_version,
                request_id="claim-inspect",
                principal="host",
                holder="worker-a",
                ttl_seconds=30,
                now=103.0,
            )
            await control.commit_run_transition_with_inputs(
                claimed.binding.run_id,
                {
                    "run_id": claimed.binding.run_id,
                    "namespace": "ns",
                    "session_id": "s1",
                    "status": "running",
                    "final_text": "SECRET-RUN-OUTPUT",
                },
                expected_version=None,
                task_id=claimed.task.task_id,
            )
        finally:
            control.close()

    run(setup())


async def _durable_facts(control: SqliteControlStore) -> tuple[Any, ...]:
    snapshot = await control.read_plan("ns", "inspect-plan")
    bindings = await control.read_plan_bindings("ns", "inspect-plan")
    events = await control.read_plan_events("ns", "inspect-plan")
    outbox = await control.read_plan_outbox(
        pending_only=False, limit=100, namespace="ns", plan_id="inspect-plan"
    )
    task = await control.read_task(bindings[0].task_id)
    claim = await control.read_claim(bindings[0].task_id)
    run_record = await control.load_run(bindings[0].run_id)
    acceptances = await control.read_plan_acceptances("ns", "inspect-plan", revision=1)
    return (
        snapshot.record,
        bindings,
        events,
        outbox,
        task,
        claim,
        run_record,
        acceptances,
    )


def test_plan_inspector_reports_bound_facts_and_dependency_blockers_without_payloads(
    tmp_path: Path, capsys: Any,
) -> None:
    path = tmp_path / "control.sqlite"
    _seed(path)
    before_store = SqliteControlStore(path, enable_orchestration=True)
    before = run(_durable_facts(before_store))
    before_store.close()

    control = SqliteControlStore(path, read_only=True, enable_orchestration=True)
    try:
        report = run(
            PlanInspector(control=control, clock=lambda: 104.0).inspect_plan(
                "inspect-plan", namespace="ns", session_id="s1"
            )
        )
    finally:
        control.close()

    record = report.to_record()
    steps = {item.step_id: item for item in report.steps}
    assert report.status == "executing"
    assert report.revision == 1
    assert report.control_version == before[0].control_version
    assert report.approval_coverage[0].active is True
    assert report.approval_coverage[0].covers_current_definition is True
    assert steps["first"].status == "running"
    assert steps["first"].task_id == before[1][0].task_id
    assert steps["first"].run_id == before[1][0].run_id
    assert steps["first"].task_status == "running"
    assert steps["first"].run_status == "running"
    assert steps["first"].attempt_count == 1
    assert steps["first"].claim is not None and steps["first"].claim.live
    assert steps["second"].status == "blocked"
    assert steps["second"].blocking_reason == "dependency_pending"
    assert steps["second"].blocked_by == ("first",)
    assert report.budget["max_step_executions"] == 4
    assert report.budget["max_concurrency"] == 2
    assert report.budget["quantitative_host_budget"]["available"] is False
    assert any("typed budget diagnostics are unavailable" in item for item in report.unsupported)
    assert any("execution ledger" in item for item in report.unsupported)
    assert any(item.event_type == "step_admitted" for item in report.events)
    assert report.pending_notifications is not None

    rendered = plan_report_to_json(report) + plan_report_to_markdown(report)
    for secret in (
        "SECRET-PLAN-GOAL",
        "SECRET-STEP-INSTRUCTION",
        "SECRET-DOWNSTREAM-INSTRUCTION",
        "SECRET-RUN-OUTPUT",
    ):
        assert secret not in rendered
    assert json.loads(plan_report_to_json(report))["steps"][1]["blocked_by"] == ["first"]

    after_store = SqliteControlStore(path, enable_orchestration=True)
    after = run(_durable_facts(after_store))
    after_store.close()
    assert after == before

    assert diagnostics_main(
        [
            "plan",
            "--control",
            str(path),
            "--namespace",
            "ns",
            "--session",
            "s1",
            "--plan-id",
            "inspect-plan",
            "--format",
            "json",
        ]
    ) == 0
    old_schema_record = json.loads(capsys.readouterr().out)
    old_budget_view = old_schema_record["budget"]["quantitative_host_budget"]
    assert old_budget_view["available"] is False
    assert "no migration was attempted" in old_budget_view["reason"]


def test_task_and_plan_budget_diagnostics_share_canonical_dispatch_facts(
    tmp_path: Path, capsys: Any
) -> None:
    path = tmp_path / "typed-budget-diagnostics.sqlite"
    _seed(path)
    control = SqliteControlStore(
        path, enable_orchestration=True, enable_typed_budgets=True
    )
    try:
        binding = (run(control.read_plan_bindings("ns", "inspect-plan")))[0]
        plan_owner = BudgetOwnerRef(BudgetOwnerKind.PLAN, "ns", "inspect-plan")
        root_task = run(control.read_task(binding.task_id))
        root_owner = BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, "ns", root_task.task_id)
        for owner in (plan_owner, root_owner):
            run(
                control.put_budget_policy(
                    BudgetPolicySnapshot(
                        owner=owner,
                        session_id="s1",
                        policy_version="diag-budget-v1",
                        limits=(
                            BudgetLimit(BudgetUnit.MODEL_REQUESTS, 5),
                            BudgetLimit(
                                BudgetUnit.COST_MICROS,
                                100,
                                mode=BudgetLimitMode.OBSERVE,
                                currency="USD",
                                price_version="price-v1",
                            ),
                        ),
                    )
                )
            )
        reservation = run(
            control.reserve_typed_budget_dispatch(
                TypedBudgetDispatch(
                    task_id=binding.task_id,
                    run_id=binding.run_id,
                    dispatch_identity="diag-dispatch-1",
                    request_instance="diag-request-1",
                    unit=BudgetUnit.MODEL_REQUESTS,
                    amount=1,
                ),
                now=120.0,
            )
        )
        run(
            control.settle_typed_budget_reservation(
                reservation.reservation_id, actual=2, now=121.0
            )
        )
        cost_reservation = run(
            control.reserve_typed_budget_dispatch(
                TypedBudgetDispatch(
                    task_id=binding.task_id,
                    run_id=binding.run_id,
                    dispatch_identity="diag-cost-dispatch-1",
                    request_instance="diag-cost-request-1",
                    unit=BudgetUnit.COST_MICROS,
                    amount=10,
                    currency="USD",
                    price_version="price-v1",
                ),
                now=122.0,
            )
        )
        run(
            control.settle_typed_budget_reservation(
                cost_reservation.reservation_id, actual=20, now=123.0
            )
        )
    finally:
        control.close()

    readonly = SqliteControlStore(
        path, read_only=True, enable_orchestration=True, enable_typed_budgets=True
    )
    try:
        plan_report = run(
            PlanInspector(control=readonly).inspect_plan(
                "inspect-plan", namespace="ns", session_id="s1"
            )
        )
        task_report = run(
            TaskInspector(control=readonly).inspect_task(
                binding.task_id, namespace="ns", session_id="s1"
            )
        )
    finally:
        readonly.close()

    plan_budget = plan_report.budget["quantitative_host_budget"]
    task_budget = task_report.typed_budget.to_record()
    plan_facts = {item["reservation_id"]: item for item in plan_budget["reservations"]}
    task_facts = {item["reservation_id"]: item for item in task_budget["reservations"]}
    assert set(plan_facts) == {reservation.reservation_id, cost_reservation.reservation_id}
    assert set(task_facts) == set(plan_facts)
    assert plan_facts == task_facts
    assert {item["owner"]["kind"] for item in plan_facts[reservation.reservation_id]["coverage"]} == {
        "plan",
        "task_root",
    }
    assert plan_budget["accounting_model"] == task_budget["accounting_model"] == "canonical_dispatches_once"
    assert next(
        item for item in plan_budget["scopes"]
        if item["owner"]["kind"] == "plan" and item["unit"] == "model_requests"
    )["actual"] == 2
    assert next(
        item for item in task_budget["scopes"]
        if item["owner"]["kind"] == "task_root" and item["unit"] == "model_requests"
    )["actual"] == 2
    cost_scope = next(
        item for item in plan_budget["scopes"]
        if item["owner"]["kind"] == "plan" and item["unit"] == "cost_micros"
    )
    assert cost_scope["actual"] == 20
    assert cost_scope["mode"] == "observe"
    assert cost_scope["hard_enforcement_available"] is False
    assert cost_scope["trusted_host_upper_bound_reference_present"] is False
    assert cost_scope["enforcement_basis"] == "observe_only"

    assert diagnostics_main(
        [
            "plan",
            "--control",
            str(path),
            "--namespace",
            "ns",
            "--session",
            "s1",
            "--plan-id",
            "inspect-plan",
            "--format",
            "json",
        ]
    ) == 0
    cli_record = json.loads(capsys.readouterr().out)
    assert cli_record["budget"]["quantitative_host_budget"]["available"] is True
    assert len(cli_record["budget"]["quantitative_host_budget"]["reservations"]) == 2


def test_plan_inspector_refuses_writable_store_and_wrong_identity(tmp_path: Path) -> None:
    path = tmp_path / "control.sqlite"
    _seed(path)
    writable = SqliteControlStore(path, enable_orchestration=True)
    try:
        with pytest.raises(ConfigurationError):
            PlanInspector(control=writable)
    finally:
        writable.close()

    readonly = SqliteControlStore(path, read_only=True, enable_orchestration=True)
    try:
        inspector = PlanInspector(control=readonly)
        with pytest.raises(ContractError, match="namespace"):
            run(inspector.inspect_plan("inspect-plan", namespace="other", session_id="s1"))
        with pytest.raises(ContractError, match="session"):
            run(inspector.inspect_plan("inspect-plan", namespace="ns", session_id="other"))
    finally:
        readonly.close()


def test_plan_inspector_marks_unbound_step_blocked_when_concurrency_is_full(
    tmp_path: Path,
) -> None:
    path = tmp_path / "control.sqlite"
    _seed(path, _definition(max_concurrency=1, parallel_steps=True))

    control = SqliteControlStore(path, read_only=True, enable_orchestration=True)
    try:
        report = run(
            PlanInspector(control=control, clock=lambda: 104.0).inspect_plan(
                "inspect-plan", namespace="ns", session_id="s1"
            )
        )
        candidates = run(control.read_ready_steps("ns", limit=8, now=104.0)).candidates
    finally:
        control.close()

    second = next(item for item in report.steps if item.step_id == "second")
    assert candidates == ()
    assert second.status == "blocked"
    assert second.blocking_reason == "plan_concurrency_limit"


def test_plan_cli_does_not_create_a_missing_database(tmp_path: Path, capsys: Any) -> None:
    missing = tmp_path / "missing.sqlite"
    result = diagnostics_main(
        ["plan", "--control", str(missing), "--namespace", "ns", "--session", "s1", "--plan-id", "p"]
    )
    assert result == 2
    assert "refusing to create" in capsys.readouterr().err
    assert not missing.exists()
