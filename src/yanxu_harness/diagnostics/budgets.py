"""Read-only budget facts shared by Task and Plan diagnostic reports."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ..tasks.budgets import BudgetLimitMode, BudgetOwnerKind, BudgetOwnerRef
from ..tasks.types import BudgetUnit

__all__ = ["TypedBudgetView", "read_typed_budget_view"]

_RESERVATION_LIMIT = 200
_PROVIDER_BOUND_UNITS = {
    BudgetUnit.INPUT_TOKENS,
    BudgetUnit.OUTPUT_TOKENS,
    BudgetUnit.COST_MICROS,
}


@dataclass(frozen=True)
class TypedBudgetView:
    """Canonical dispatch facts plus per-owner/unit accounting references."""

    available: bool
    reason: str = ""
    scopes: tuple[Mapping[str, Any], ...] = ()
    reservations: tuple[Mapping[str, Any], ...] = ()
    truncated: bool = False
    accounting_model: str = "canonical_dispatches_once"
    schema_version: int = 1

    def to_record(self) -> Mapping[str, Any]:
        return {
            "schema_version": self.schema_version,
            "available": self.available,
            "reason": self.reason,
            "accounting_model": self.accounting_model,
            "scopes": [dict(item) for item in self.scopes],
            "reservations": [dict(item) for item in self.reservations],
            "truncated": self.truncated,
        }


def _owner_record(owner: BudgetOwnerRef) -> Mapping[str, str]:
    is_grant = owner.kind is BudgetOwnerKind.TASK_GRANT
    return {
        "kind": owner.kind.value,
        "namespace": owner.namespace,
        "owner_id": "" if is_grant else owner.owner_id,
        "owner_id_digest": (
            "sha256:" + hashlib.sha256(owner.owner_id.encode("utf-8")).hexdigest()
            if is_grant
            else ""
        ),
    }


def _reservation_fact(reservation: Any) -> Mapping[str, Any]:
    legacy_digest = ""
    legacy_prefix = "legacy-budget-import:"
    if reservation.dispatch.dispatch_identity.startswith(legacy_prefix):
        legacy_digest = reservation.dispatch.dispatch_identity[len(legacy_prefix) :]
    return {
        "reservation_id": reservation.reservation_id,
        "task_id": reservation.dispatch.task_id,
        "run_id": reservation.dispatch.run_id,
        "unit": reservation.dispatch.unit.value,
        "amount": reservation.dispatch.amount,
        "currency": reservation.dispatch.currency,
        "price_version": reservation.dispatch.price_version,
        "state": reservation.state.value,
        "settled_actual": reservation.settled_actual,
        "created_at": reservation.created_at,
        "settled_at": reservation.settled_at,
        "legacy_source_reservation_id_digest": legacy_digest,
        "coverage": [
            {
                "owner": dict(_owner_record(item.owner)),
                "policy_digest": item.policy_digest,
                "limit": item.limit,
                "mode": None if item.mode is None else item.mode.value,
                "currency": item.currency,
                "price_version": item.price_version,
            }
            for item in reservation.coverage
        ],
    }


async def read_typed_budget_view(
    control: Any,
    *,
    owners: Sequence[BudgetOwnerRef],
    task_id: str = "",
    run_id: str = "",
    expand_coverage: bool = False,
) -> TypedBudgetView:
    """Read budget policies, scope totals and canonical dispatches without mutating the store."""
    forced_reason = str(getattr(control, "_typed_budget_diagnostic_unsupported", "") or "")
    if forced_reason:
        return TypedBudgetView(available=False, reason=forced_reason)
    if getattr(control, "_typed_budget_enabled", None) is False:
        return TypedBudgetView(
            available=False,
            reason="typed budget capability is not enabled on this read-only connection",
        )

    policy_reader = getattr(control, "read_budget_policy", None)
    owner_reservation_reader = getattr(
        control, "read_typed_budget_reservations_for_owner", None
    )
    usage_reader = getattr(control, "read_typed_budget_usage", None)
    if not all(callable(item) for item in (policy_reader, owner_reservation_reader, usage_reader)):
        return TypedBudgetView(
            available=False,
            reason="the read-only control store does not expose the typed budget view",
        )

    owner_by_key: dict[tuple[str, str, str], BudgetOwnerRef] = {}
    for owner in owners:
        owner_by_key[owner.key] = owner

    if task_id and run_id:
        task_policy_reader = getattr(control, "read_budget_policies_for_task", None)
        if callable(task_policy_reader):
            try:
                for policy in await task_policy_reader(task_id, run_id):
                    owner_by_key[policy.owner.key] = policy.owner
            except Exception:
                return TypedBudgetView(
                    available=False,
                    reason="typed budget scopes could not be read (policy lookup failed)",
                )

    reservations_by_id: dict[str, Any] = {}
    rows_by_owner: dict[tuple[str, str, str], tuple[Any, ...]] = {}
    policies: dict[tuple[str, str, str], Any] = {}
    truncated = False
    processed: set[tuple[str, str, str]] = set()

    try:
        while True:
            pending = [key for key in owner_by_key if key not in processed]
            if not pending:
                break
            if len(processed) >= 100:
                truncated = True
                break
            key = pending[0]
            owner = owner_by_key[key]
            processed.add(key)
            policy = await policy_reader(owner)
            policies[key] = policy
            rows = tuple(
                await owner_reservation_reader(
                    owner, limit=_RESERVATION_LIMIT + 1
                )
            )
            if len(rows) > _RESERVATION_LIMIT:
                truncated = True
                rows = rows[:_RESERVATION_LIMIT]
            rows_by_owner[key] = rows
            for reservation in rows:
                reservations_by_id[reservation.reservation_id] = reservation
                if expand_coverage:
                    for coverage in reservation.coverage:
                        owner_by_key[coverage.owner.key] = coverage.owner
    except Exception:
        return TypedBudgetView(
            available=False,
            reason="typed budget facts could not be read consistently",
        )

    reservations = sorted(
        reservations_by_id.values(),
        key=lambda item: (item.created_at, item.reservation_id),
    )
    if len(reservations) > _RESERVATION_LIMIT:
        truncated = True
        reservations = reservations[:_RESERVATION_LIMIT]
    scopes: list[Mapping[str, Any]] = []
    try:
        for key in sorted(processed):
            owner = owner_by_key[key]
            policy = policies.get(key)
            owner_rows = rows_by_owner.get(key, ())
            units = {item.dispatch.unit for item in owner_rows}
            if policy is not None:
                units.update(item.unit for item in policy.limits)
            for unit in sorted(units, key=lambda item: item.value):
                usage = await usage_reader(owner, unit)
                limit = None if policy is None else policy.limit_for(unit)
                hard_mode = usage.mode is BudgetLimitMode.HARD
                provider_bound_unit = unit in _PROVIDER_BOUND_UNITS
                bound_ref = "" if limit is None else limit.hard_bound_ref
                if not hard_mode:
                    enforcement_basis = "observe_only" if usage.limit is not None else "no_hard_limit"
                elif provider_bound_unit:
                    enforcement_basis = (
                        "referenced_host_upper_bound" if bound_ref else "upper_bound_unavailable"
                    )
                else:
                    enforcement_basis = "host_dispatch_reservation_gate"
                scopes.append(
                    {
                        "owner": dict(_owner_record(owner)),
                        "unit": unit.value,
                        "policy_digest": usage.policy_digest,
                        "limit": usage.limit,
                        "mode": None if usage.mode is None else usage.mode.value,
                        "actual": usage.actual,
                        "reserved": usage.reserved,
                        "unknown": usage.unknown,
                        "used": usage.used,
                        "available": usage.available,
                        "over_limit": usage.over_limit,
                        "currency": usage.currency,
                        "price_version": usage.price_version,
                        "upper_bound_overrun": usage.upper_bound_overrun,
                        "hard_enforcement_available": hard_mode
                        and (not provider_bound_unit or bool(bound_ref)),
                        "enforcement_basis": enforcement_basis,
                        "trusted_host_upper_bound_reference_present": bool(bound_ref),
                        "coverage_complete": not truncated,
                        "reservation_ids": sorted(
                            item.reservation_id
                            for item in reservations
                            if item.dispatch.unit is unit
                            and any(coverage.owner == owner for coverage in item.coverage)
                        ),
                    }
                )
    except Exception:
        return TypedBudgetView(
            available=False,
            reason="typed budget scope totals could not be read",
        )

    return TypedBudgetView(
        available=True,
        scopes=tuple(scopes),
        reservations=tuple(_reservation_fact(item) for item in reservations),
        truncated=truncated,
    )
