from __future__ import annotations

from copy import deepcopy

import pytest

from yanxu_harness.errors import ContractError
from yanxu_harness.tasks import (
    BudgetLimit,
    BudgetLimitMode,
    BudgetOwnerKind,
    BudgetOwnerRef,
    BudgetPolicySnapshot,
    BudgetUnit,
    TaskBudget,
    snapshot_from_task_budget,
)


def _owner(kind: BudgetOwnerKind = BudgetOwnerKind.PLAN) -> BudgetOwnerRef:
    return BudgetOwnerRef(kind, "project-a", "plan-shared-budget")


def test_plan_budget_owner_is_stable_across_revisions_and_session_is_metadata() -> None:
    first = BudgetPolicySnapshot(
        owner=_owner(),
        session_id="session-original",
        policy_version="budget-policy/v1",
        limits=(BudgetLimit(BudgetUnit.MODEL_REQUESTS, 5),),
    )
    later = BudgetOwnerRef(BudgetOwnerKind.PLAN, "project-a", "plan-shared-budget")

    assert first.owner.key == later.key == ("plan", "project-a", "plan-shared-budget")
    assert first.to_record()["session_id"] == "session-original"
    assert "revision" not in first.owner.to_record()


def test_budget_policy_snapshot_is_stable_versioned_and_content_addressed() -> None:
    snapshot = BudgetPolicySnapshot(
        owner=_owner(),
        session_id="session-1",
        policy_version="budget-policy/v4",
        limits=(
            BudgetLimit(BudgetUnit.OUTPUT_TOKENS, 20, BudgetLimitMode.OBSERVE),
            BudgetLimit(BudgetUnit.MODEL_REQUESTS, 4),
        ),
        max_tasks_per_root=9,
        max_depth=3,
    )

    record = snapshot.to_record()
    restored = BudgetPolicySnapshot.from_record(record)
    assert [item.unit for item in restored.limits] == [
        BudgetUnit.MODEL_REQUESTS,
        BudgetUnit.OUTPUT_TOKENS,
    ]
    assert restored == snapshot
    assert restored.digest == snapshot.digest
    changed = deepcopy(record)
    changed["limits"][0]["limit"] = 5
    assert BudgetPolicySnapshot.from_record(changed).digest != snapshot.digest

    future = deepcopy(record)
    future["schema_version"] = 2
    with pytest.raises(ContractError, match="schema"):
        BudgetPolicySnapshot.from_record(future)


@pytest.mark.parametrize("value", [-1, 1.0, True, float("nan"), float("inf")])
def test_typed_budget_limits_reject_negative_non_integer_or_non_finite_values(value: object) -> None:
    with pytest.raises(ContractError, match="non-negative integer"):
        BudgetLimit(BudgetUnit.MODEL_REQUESTS, value)  # type: ignore[arg-type]


def test_zero_is_a_valid_hard_limit_and_duplicate_units_are_refused() -> None:
    zero = BudgetLimit(BudgetUnit.MODEL_REQUESTS, 0)
    with pytest.raises(ContractError, match="only one limit per unit"):
        BudgetPolicySnapshot(
            owner=_owner(),
            session_id="session-1",
            policy_version="v1",
            limits=(zero, BudgetLimit(BudgetUnit.MODEL_REQUESTS, 1)),
        )


def test_tokens_are_observation_only_without_a_host_bound() -> None:
    observed = BudgetLimit(BudgetUnit.INPUT_TOKENS, 100, BudgetLimitMode.OBSERVE)
    assert observed.mode is BudgetLimitMode.OBSERVE
    with pytest.raises(ContractError, match="upper-bound reference"):
        BudgetLimit(BudgetUnit.OUTPUT_TOKENS, 50)

    hard = BudgetLimit(
        BudgetUnit.OUTPUT_TOKENS,
        50,
        BudgetLimitMode.HARD,
        hard_bound_ref="provider/output-token-cap/v3",
    )
    assert hard.mode is BudgetLimitMode.HARD
    with pytest.raises(ContractError, match="legacy unit"):
        BudgetLimit(BudgetUnit.TOKENS, 100)


def test_cost_limit_requires_currency_price_version_and_bound_for_hard_mode() -> None:
    observed = BudgetLimit(
        BudgetUnit.COST_MICROS,
        1_000_000,
        BudgetLimitMode.OBSERVE,
        currency="USD",
        price_version="price-sheet/v7",
    )
    assert observed.currency == "USD"
    with pytest.raises(ContractError, match="trusted upper-bound reference"):
        BudgetLimit(
            BudgetUnit.COST_MICROS,
            1_000_000,
            currency="USD",
            price_version="price-sheet/v7",
        )
    with pytest.raises(ContractError, match="currency code"):
        BudgetLimit(
            BudgetUnit.COST_MICROS,
            1_000_000,
            BudgetLimitMode.OBSERVE,
            currency="usd",
            price_version="price-sheet/v7",
        )


def test_legacy_task_budget_conversion_is_exact_and_preserves_unit_evidence() -> None:
    converted = snapshot_from_task_budget(
        TaskBudget(
            units={
                BudgetUnit.MODEL_REQUESTS: 5,
                BudgetUnit.SECONDS: 0.125,
                BudgetUnit.INPUT_TOKENS: 80,
                BudgetUnit.OUTPUT_TOKENS: 20,
                BudgetUnit.COST_MICROS: 2500,
            },
            max_tasks_per_root=6,
            max_depth=2,
        ),
        owner=BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, "project-a", "root-task"),
        session_id="session-1",
        policy_version="legacy-import/v1",
        hard_bound_refs={
            BudgetUnit.OUTPUT_TOKENS: "provider/output-token-cap/v3",
            BudgetUnit.COST_MICROS: "billing/upper-bound/v7",
        },
        cost_currency="USD",
        price_version="price-sheet/v7",
    )

    assert converted.owner.key == ("task_root", "project-a", "root-task")
    assert converted.max_tasks_per_root == 6
    assert converted.max_depth == 2
    assert converted.limit_for(BudgetUnit.MODEL_REQUESTS).limit == 5
    assert converted.limit_for(BudgetUnit.DURATION_MILLISECONDS).limit == 125
    assert converted.limit_for(BudgetUnit.INPUT_TOKENS).mode is BudgetLimitMode.OBSERVE
    assert converted.limit_for(BudgetUnit.OUTPUT_TOKENS).mode is BudgetLimitMode.HARD
    assert converted.limit_for(BudgetUnit.COST_MICROS).mode is BudgetLimitMode.HARD
    assert converted.limit_for(BudgetUnit.COST_MICROS).currency == "USD"


@pytest.mark.parametrize(
    ("units", "options", "message"),
    [
        ({BudgetUnit.MODEL_REQUESTS: 1.5}, {}, "converted exactly"),
        ({BudgetUnit.SECONDS: 0.0001}, {}, "converted exactly"),
        ({BudgetUnit.TOKENS: 100}, {}, "input or output"),
        ({BudgetUnit.COST_MICROS: 1000}, {}, "cost_currency"),
        ({BudgetUnit.MODEL_REQUESTS: float("nan")}, {}, "finite and non-negative"),
        ({BudgetUnit.MODEL_REQUESTS: -1}, {}, "finite and non-negative"),
    ],
)
def test_legacy_budget_conversion_refuses_ambiguous_or_lossy_limits(units, options, message) -> None:
    with pytest.raises(ContractError, match=message):
        snapshot_from_task_budget(
            TaskBudget(units=units),
            owner=_owner(BudgetOwnerKind.TASK_ROOT),
            session_id="session-1",
            policy_version="migration/v1",
            **options,
        )
