"""The budget ledger: reservations before dispatch, idempotent settlement, honest unknowns.

One root task owns one allowance. Everything below it — every attempt, every re-issued request,
every child — spends from that same allowance, and nothing here can make it larger. The rules the
plan freezes, and what each one is protecting against:

* **a reservation precedes the external await.** The dispatch port reserves before the provider is
  called, so a run that cannot afford a request does not start one. A failed reservation commits
  nothing and dispatches nothing;
* **the identity is the dispatch, not the logical action.** The key is the *request instance*, so
  a recovery that re-issues the same logical model action reserves a **second** time — it is a
  second real request and may be charged again. Deduplicating by logical action id would hide the
  retry from the budget, which is how a runaway recovery looks free;
* **`unknown` is a state, not a zero.** A request that entered the dispatch window without a final
  usage keeps its coverage. Refunding it would make the available allowance larger than the root
  ever granted; settling it at zero would spend an allowance nobody can prove was not consumed;
* **settling is idempotent, refunding is not free.** A replay of the same settlement changes
  nothing; a *release* is allowed only for work that provably never started, and a settled
  reservation is never refunded. A cancellation request does not release a reservation whose stop
  is unconfirmed;
* **a grant to a child is bounded by what the parent has left** and does not change the root's
  total: the plan's "父任务给子任务预留有界 grant，分配不会增加 root 总额度";
* **late usage is recorded idempotently** under a deterministic identity derived from the
  reservation, so a redelivered report tops up exactly once and can neither refund nor inflate.

What this module deliberately does **not** do: price anything. Tokens and money are recorded as
`actual`/`estimated`/`unknown` with their unit; a provider's figures are never invented, and an
estimate is never presented as a report.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from ..errors import ContractError
from .types import (
    BudgetRefused,
    BudgetReservation,
    BudgetUnit,
    ReservationState,
)

__all__ = [
    "BudgetLedger",
    "BudgetSnapshot",
    "DispatchBudget",
    "DispatchReservation",
    "TaskBudget",
]


@dataclass(frozen=True)
class TaskBudget:
    """The allowance one root task is granted, in units the host can actually constrain.

    Counts and time are hard limits; tokens and money are recorded but only bounded if the host
    supplies a number, because a provider that reports nothing cannot be held to a figure this
    build guessed.
    """

    units: Mapping[BudgetUnit | str, float] = field(default_factory=dict)
    #: The most tasks one root may have (itself included) and how deep the tree may go. Enforced
    #: by the control store, because "how many tasks exist" is a fact only the transaction knows.
    max_tasks_per_root: int = 8
    max_depth: int = 1

    def limit(self, unit: BudgetUnit | str) -> float | None:
        key = unit.value if isinstance(unit, BudgetUnit) else str(unit)
        for candidate, amount in self.units.items():
            name = candidate.value if isinstance(candidate, BudgetUnit) else str(candidate)
            if name == key:
                return float(amount)
        return None

    def to_record(self) -> Mapping[str, Any]:
        return {
            "units": {
                (unit.value if isinstance(unit, BudgetUnit) else str(unit)): float(amount)
                for unit, amount in sorted(
                    self.units.items(),
                    key=lambda item: (
                        item[0].value if isinstance(item[0], BudgetUnit) else str(item[0])
                    ),
                )
            },
            "max_tasks_per_root": self.max_tasks_per_root,
            "max_depth": self.max_depth,
        }


@dataclass(frozen=True)
class DispatchReservation:
    """What the dispatch port hands back: an identity the settle call can name."""

    reservation_id: str
    dispatch_identity: str
    unit: BudgetUnit
    amount: float


@dataclass(frozen=True)
class BudgetSnapshot:
    """What a reader may say about one root's allowance, with the coverage stated."""

    root_task_id: str
    units: Mapping[str, Mapping[str, Any]]

    def to_record(self) -> Mapping[str, Any]:
        return {"root_task_id": self.root_task_id, "units": dict(self.units)}


class BudgetLedger:
    """Reservation, settlement and grants over a control store."""

    def __init__(
        self,
        control: Any,
        budget: TaskBudget | None = None,
        *,
        clock: Any = time.time,
    ) -> None:
        if control is None:
            raise ContractError("a budget ledger needs a control store")
        self._control = control
        self.budget = budget or TaskBudget()
        self._clock = clock
        self._sequence = 0

    # -- reading -----------------------------------------------------------------------

    async def used(self, root_task_id: str, unit: BudgetUnit | str) -> float:
        """What the root has spent or committed: settled amounts plus live reservations."""
        rows = await self._control.read_reservations(root_task_id, unit=unit)
        total = 0.0
        for reservation in rows:
            if reservation.state is ReservationState.SETTLED:
                total += float(reservation.settled_amount or 0.0)
            elif reservation.state in {ReservationState.RESERVED, ReservationState.UNKNOWN}:
                total += reservation.amount
        return total

    async def available(
        self, root_task_id: str, unit: BudgetUnit | str, *, limit: float | None = None
    ) -> float | None:
        declared = self.budget.limit(unit) if limit is None else limit
        if declared is None:
            return None
        return max(0.0, declared - await self.used(root_task_id, unit))

    async def snapshot(self, root_task_id: str) -> BudgetSnapshot:
        units: dict[str, Mapping[str, Any]] = {}
        for unit in BudgetUnit:
            limit = self.budget.limit(unit)
            rows = [
                row
                for row in await self._control.read_reservations(root_task_id, unit=unit)
            ]
            if limit is None and not rows:
                continue
            settled = sum(
                float(row.settled_amount or 0.0)
                for row in rows
                if row.state is ReservationState.SETTLED
            )
            reserved = sum(
                row.amount for row in rows if row.state is ReservationState.RESERVED
            )
            unknown = sum(row.amount for row in rows if row.state is ReservationState.UNKNOWN)
            released = sum(row.amount for row in rows if row.state is ReservationState.RELEASED)
            units[unit.value] = {
                "limit": limit,
                "settled": round(settled, 6),
                "reserved": round(reserved, 6),
                "unknown": round(unknown, 6),
                "released": round(released, 6),
                "used": round(settled + reserved + unknown, 6),
                "available": None if limit is None else round(max(0.0, limit - settled - reserved - unknown), 6),
                "reservations": len(rows),
            }
        return BudgetSnapshot(root_task_id=root_task_id, units=units)

    # -- writing -----------------------------------------------------------------------

    def _identity(self, prefix: str) -> str:
        self._sequence += 1
        return f"{prefix}:{self._sequence}"

    async def reserve(
        self,
        *,
        root_task_id: str,
        task_id: str,
        unit: BudgetUnit | str,
        amount: float,
        dispatch_identity: str,
        claim: Any | None = None,
        limit: float | None = None,
        reservation_id: str | None = None,
    ) -> BudgetReservation:
        """Reserve before dispatching; the same identity reserves once.

        A refusal is raised, not returned, so a caller cannot accidentally dispatch on the result
        of a failed reservation.
        """
        resolved = unit if isinstance(unit, BudgetUnit) else BudgetUnit(str(unit))
        declared = self.budget.limit(resolved) if limit is None else limit
        return await self._control.reserve_budget(
            root_task_id=root_task_id,
            task_id=task_id,
            reservation=BudgetReservation(
                root_task_id=root_task_id,
                task_id=task_id,
                unit=resolved,
                amount=float(amount),
                reservation_id=reservation_id or self._identity(f"res:{task_id}"),
                dispatch_identity=dispatch_identity,
            ),
            claim=claim,
            limit=declared,
            now=self._clock(),
        )

    async def settle(
        self,
        reservation_id: str,
        *,
        actual: float | None = None,
        unknown: str = "",
        release: bool = False,
        note: str = "",
    ) -> BudgetReservation:
        """Settle, mark unknown or release — exactly one of the three, and never a refund."""
        if release and (actual is not None or unknown):
            raise ContractError(
                "a reservation is either released (the work never started) or settled/unknown "
                "(it did); refunding a dispatched request would inflate the allowance"
            )
        if release:
            # A release is only for work that provably never started. ``unknown`` is the state
            # that says the opposite: the dispatch window opened and no outcome came back, so
            # releasing it would hand the allowance out twice.
            current = await self._control.read_reservations(
                (await self._control.read_reservation_owner(reservation_id))
                if hasattr(self._control, "read_reservation_owner")
                else ""
            )
            _ = current
            state = await self._reservation_state(reservation_id)
            if state in {ReservationState.SETTLED.value, ReservationState.UNKNOWN.value}:
                raise ContractError(
                    f"reservation {reservation_id!r} is {state}; it may not be released — the "
                    "dispatch window was open, so the work is not proven never to have started"
                )
        if unknown:
            return await self._control.settle_budget(
                reservation_id, state="unknown", note=unknown, now=self._clock()
            )
        if release:
            return await self._control.settle_budget(
                reservation_id, state="released", note=note or "the work never started", now=self._clock()
            )
        if actual is None:
            raise ContractError("a settlement needs an actual amount, an unknown reason or a release")
        return await self._control.settle_budget(
            reservation_id, state="settled", settled_amount=float(actual), note=note, now=self._clock()
        )

    async def grant_to_child(
        self,
        *,
        parent_task_id: str,
        child_task_id: str,
        unit: BudgetUnit | str,
        amount: float,
        spawn_identity: str,
        root_task_id: str | None = None,
        claim: Any | None = None,
    ) -> BudgetReservation:
        """Give a child a bounded part of what the root has left.

        ``root_task_id`` defaults to the parent (a root's own parent is itself, per the task
        record). The grant is a reservation like any other: it consumes the root's available
        allowance now, and it is settled or released when the child ends.
        """
        root = root_task_id or parent_task_id
        resolved = unit if isinstance(unit, BudgetUnit) else BudgetUnit(str(unit))
        available = await self.available(root, resolved)
        if available is not None and amount > available:
            raise BudgetRefused(
                root,
                resolved.value,
                float(amount),
                round(available, 6),
                "a child grant cannot exceed what the root has left: allocating it would make the "
                "root's total larger than it was granted",
            )
        return await self.reserve(
            root_task_id=root,
            task_id=child_task_id,
            unit=resolved,
            amount=amount,
            dispatch_identity=f"grant:{spawn_identity}",
            claim=claim,
            reservation_id=f"grant:{parent_task_id}:{child_task_id}",
        )

    async def _reservation_state(self, reservation_id: str) -> str:
        reader = getattr(self._control, "read_reservation", None)
        if callable(reader):
            reservation = await reader(reservation_id)
            return "" if reservation is None else reservation.state.value
        return ""

    async def record_late_usage(
        self,
        reservation_id: str,
        *,
        amount: float,
        root_task_id: str,
        task_id: str,
        unit: BudgetUnit | str,
        sequence: int = 0,
    ) -> BudgetReservation:
        """Record a usage report that arrived after settlement — once per report identity.

        The identity is derived from the reservation and the report's own sequence, so a
        redelivered report is idempotent while a genuinely different report is recorded.
        """
        resolved = unit if isinstance(unit, BudgetUnit) else BudgetUnit(str(unit))
        reservation = await self.reserve(
            root_task_id=root_task_id,
            task_id=task_id,
            unit=resolved,
            amount=float(amount),
            dispatch_identity=f"late:{reservation_id}:{int(sequence)}",
            reservation_id=f"late:{reservation_id}:{int(sequence)}",
        )
        return await self.settle(
            reservation.reservation_id, actual=float(amount), note="late usage report"
        )


class DispatchBudget:
    """The per-model-request port a harness calls around one dispatch.

    ``reserve`` runs **before** the provider is called, and the identity it uses is the *request
    instance* — the real request, not the logical model action. A recovery that re-sends the same
    action therefore reserves again, which is the honest accounting: it is a second request that
    may be charged a second time.
    """

    def __init__(
        self,
        ledger: BudgetLedger,
        *,
        root_task_id: str,
        task_id: str,
        unit: BudgetUnit | str = BudgetUnit.MODEL_REQUESTS,
        amount: float = 1.0,
        claim: Any | None = None,
        enabled: bool = True,
    ) -> None:
        self._ledger = ledger
        self._root_task_id = root_task_id
        self._task_id = task_id
        self._unit = unit
        self._amount = amount
        self._claim = claim
        self.enabled = enabled
        self.reserved: list[str] = []

    def identity(self, payload: Mapping[str, Any]) -> str:
        instance = str(
            payload.get("request_instance")
            or f"{payload.get('model_action_id')}:{payload.get('turn')}"
        )
        return f"request:{self._root_task_id}:{self._task_id}:{instance}"

    async def reserve(self, payload: Mapping[str, Any]) -> DispatchReservation | None:
        if not self.enabled:
            return None
        reservation = await self._ledger.reserve(
            root_task_id=self._root_task_id,
            task_id=self._task_id,
            unit=self._unit,
            amount=self._amount,
            dispatch_identity=self.identity(payload),
            claim=self._claim,
            reservation_id=f"dispatch:{self._task_id}:{payload.get('request_instance') or payload.get('model_action_id')}",
        )
        self.reserved.append(reservation.reservation_id)
        return DispatchReservation(
            reservation_id=reservation.reservation_id,
            dispatch_identity=reservation.dispatch_identity,
            unit=reservation.unit,
            amount=reservation.amount,
        )

    async def settle(
        self, reservation: DispatchReservation | None, outcome: Mapping[str, Any]
    ) -> None:
        """Settle the reservation from what is actually known about the dispatch."""
        if reservation is None:
            return
        status = str(outcome.get("status") or "")
        if status in {"completed", "streamed"}:
            # The request really was dispatched and answered: the unit is consumed. Token or cost
            # units are *not* invented here — a provider figure is recorded where it exists
            # (the run's own measurement facts), never guessed into the budget.
            await self._ledger.settle(
                reservation.reservation_id,
                actual=reservation.amount,
                note="the request was dispatched and answered",
            )
            return
        await self._ledger.settle(
            reservation.reservation_id,
            unknown=str(outcome.get("detail") or "the dispatch did not report an outcome"),
        )
