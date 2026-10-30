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

import hashlib
import json
import math
import re
import time
import uuid
import asyncio
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Callable, Mapping

from ..errors import ContractError
from .types import (
    BudgetRefused,
    BudgetReservation,
    BudgetUnit,
    ReservationState,
)

__all__ = [
    "BudgetLedger",
    "BudgetLimit",
    "BudgetLimitMode",
    "BudgetOwnerKind",
    "BudgetOwnerRef",
    "BudgetPolicyChangeRecord",
    "BudgetPolicySnapshot",
    "BudgetScopeCoverage",
    "BudgetSnapshot",
    "DispatchBudget",
    "DispatchReservation",
    "TypedDispatchBudget",
    "TaskBudget",
    "TypedBudgetDispatch",
    "TypedBudgetReservation",
    "TypedBudgetUsage",
    "snapshot_from_task_budget",
]

_BUDGET_POLICY_DOMAIN = b"yanxu-budget-policy:v1\0"
_CURRENCY_CODE = re.compile(r"^[A-Z]{3}$")
_HARD_BUDGET_UNITS = frozenset(
    {
        BudgetUnit.TURNS,
        BudgetUnit.MODEL_REQUESTS,
        BudgetUnit.DISPATCHES,
        BudgetUnit.CONCURRENCY,
        BudgetUnit.DEPTH,
        BudgetUnit.DURATION_MILLISECONDS,
    }
)
_TOKEN_BUDGET_UNITS = frozenset({BudgetUnit.INPUT_TOKENS, BudgetUnit.OUTPUT_TOKENS})


def _budget_text(value: Any, name: str, *, max_length: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{name} must be non-empty bounded text")
    try:
        size = len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as exc:
        raise ContractError(f"{name} contains an isolated surrogate") from exc
    if size > max_length:
        raise ContractError(f"{name} must be non-empty bounded text")
    return value


def _record(value: Any, name: str, fields: set[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ContractError(f"{name} must contain exactly: {', '.join(sorted(fields))}")
    return value


def _canonical_record(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


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


class BudgetOwnerKind(str, Enum):
    """The stable entity whose cumulative allowance owns a reservation."""

    TASK_ROOT = "task_root"
    PLAN = "plan"
    TASK_GRANT = "task_grant"


@dataclass(frozen=True)
class BudgetOwnerRef:
    """A stable budget account identity; Plan revisions do not create new owners."""

    kind: BudgetOwnerKind
    namespace: str
    owner_id: str

    def __post_init__(self) -> None:
        try:
            kind = BudgetOwnerKind(self.kind)
        except (TypeError, ValueError) as exc:
            raise ContractError("budget owner kind must be task_root, plan, or task_grant") from exc
        object.__setattr__(self, "kind", kind)
        _budget_text(self.namespace, "budget owner namespace")
        _budget_text(self.owner_id, "budget owner id")

    @property
    def key(self) -> tuple[str, str, str]:
        """The account key. Session and revision are intentionally not part of it."""
        return (self.kind.value, self.namespace, self.owner_id)

    def to_record(self) -> Mapping[str, Any]:
        return {
            "kind": self.kind.value,
            "namespace": self.namespace,
            "owner_id": self.owner_id,
        }

    @classmethod
    def from_record(cls, value: Any) -> "BudgetOwnerRef":
        record = _record(value, "budget owner", {"kind", "namespace", "owner_id"})
        return cls(
            kind=record["kind"],
            namespace=record["namespace"],
            owner_id=record["owner_id"],
        )


class BudgetLimitMode(str, Enum):
    """Whether a policy amount blocks admission or only describes measured usage."""

    HARD = "hard"
    OBSERVE = "observe"


@dataclass(frozen=True)
class BudgetLimit:
    """One typed, non-negative limit and the evidence needed to call it enforceable."""

    unit: BudgetUnit
    limit: int
    mode: BudgetLimitMode = BudgetLimitMode.HARD
    hard_bound_ref: str = ""
    currency: str = ""
    price_version: str = ""

    def __post_init__(self) -> None:
        try:
            unit = BudgetUnit(self.unit)
        except (TypeError, ValueError) as exc:
            raise ContractError("budget limit unit must be a supported BudgetUnit") from exc
        if unit in {BudgetUnit.SECONDS, BudgetUnit.TOKENS}:
            raise ContractError(
                f"legacy unit {unit.value!r} is ambiguous in a typed budget policy"
            )
        object.__setattr__(self, "unit", unit)
        if type(self.limit) is not int or self.limit < 0:
            raise ContractError("budget limit must be a non-negative integer")
        try:
            mode = BudgetLimitMode(self.mode)
        except (TypeError, ValueError) as exc:
            raise ContractError("budget limit mode must be hard or observe") from exc
        object.__setattr__(self, "mode", mode)
        if self.hard_bound_ref:
            _budget_text(self.hard_bound_ref, "hard budget bound reference")

        if unit in _HARD_BUDGET_UNITS and mode is not BudgetLimitMode.HARD:
            raise ContractError(f"{unit.value} requires a hard integer limit")
        if unit in _TOKEN_BUDGET_UNITS and mode is BudgetLimitMode.HARD and not self.hard_bound_ref:
            raise ContractError(f"hard {unit.value} requires a host-enforced upper-bound reference")
        if unit is BudgetUnit.COST_MICROS:
            if not isinstance(self.currency, str) or not _CURRENCY_CODE.fullmatch(self.currency):
                raise ContractError("cost_micros requires a three-letter uppercase currency code")
            _budget_text(self.price_version, "cost price version")
            if mode is BudgetLimitMode.HARD and not self.hard_bound_ref:
                raise ContractError("hard cost_micros requires a trusted upper-bound reference")
        elif self.currency or self.price_version:
            raise ContractError("currency and price_version apply only to cost_micros")
        if unit not in _TOKEN_BUDGET_UNITS and unit is not BudgetUnit.COST_MICROS and self.hard_bound_ref:
            raise ContractError("hard_bound_ref applies only to token or cost limits")

    def to_record(self) -> Mapping[str, Any]:
        return {
            "unit": self.unit.value,
            "limit": self.limit,
            "mode": self.mode.value,
            "hard_bound_ref": self.hard_bound_ref,
            "currency": self.currency,
            "price_version": self.price_version,
        }

    @classmethod
    def from_record(cls, value: Any) -> "BudgetLimit":
        record = _record(
            value,
            "budget limit",
            {"unit", "limit", "mode", "hard_bound_ref", "currency", "price_version"},
        )
        return cls(
            unit=record["unit"],
            limit=record["limit"],
            mode=record["mode"],
            hard_bound_ref=record["hard_bound_ref"],
            currency=record["currency"],
            price_version=record["price_version"],
        )


@dataclass(frozen=True)
class BudgetPolicySnapshot:
    """Immutable policy facts for a Task-root or Plan budget account."""

    owner: BudgetOwnerRef
    session_id: str
    policy_version: str
    limits: tuple[BudgetLimit, ...] = ()
    max_tasks_per_root: int = 8
    max_depth: int = 1
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.owner, BudgetOwnerRef):
            raise ContractError("budget policy owner must be a BudgetOwnerRef")
        _budget_text(self.session_id, "budget policy session id")
        _budget_text(self.policy_version, "budget policy version")
        if type(self.max_tasks_per_root) is not int or self.max_tasks_per_root < 1:
            raise ContractError("max_tasks_per_root must be a positive integer")
        if type(self.max_depth) is not int or self.max_depth < 0:
            raise ContractError("max_depth must be a non-negative integer")
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ContractError("unsupported budget policy snapshot schema")
        if not isinstance(self.limits, (tuple, list)):
            raise ContractError("budget policy limits must be a finite sequence")
        limits = tuple(self.limits)
        if any(not isinstance(item, BudgetLimit) for item in limits):
            raise ContractError("budget policy limits must contain BudgetLimit values")
        units = [item.unit for item in limits]
        if len(units) != len(set(units)):
            raise ContractError("a budget policy may contain only one limit per unit")
        object.__setattr__(self, "limits", tuple(sorted(limits, key=lambda item: item.unit.value)))

    def limit_for(self, unit: BudgetUnit | str) -> BudgetLimit | None:
        try:
            wanted = BudgetUnit(unit)
        except (TypeError, ValueError):
            return None
        return next((item for item in self.limits if item.unit is wanted), None)

    def to_record(self) -> Mapping[str, Any]:
        return {
            "schema_version": self.schema_version,
            "owner": dict(self.owner.to_record()),
            "session_id": self.session_id,
            "policy_version": self.policy_version,
            "limits": [dict(item.to_record()) for item in self.limits],
            "max_tasks_per_root": self.max_tasks_per_root,
            "max_depth": self.max_depth,
        }

    @classmethod
    def from_record(cls, value: Any) -> "BudgetPolicySnapshot":
        record = _record(
            value,
            "budget policy snapshot",
            {
                "schema_version", "owner", "session_id", "policy_version", "limits",
                "max_tasks_per_root", "max_depth",
            },
        )
        if type(record["schema_version"]) is not int or record["schema_version"] != 1:
            raise ContractError("unsupported budget policy snapshot schema")
        if not isinstance(record["limits"], (tuple, list)):
            raise ContractError("budget policy limits must be a finite sequence")
        return cls(
            schema_version=record["schema_version"],
            owner=BudgetOwnerRef.from_record(record["owner"]),
            session_id=record["session_id"],
            policy_version=record["policy_version"],
            limits=tuple(BudgetLimit.from_record(item) for item in record["limits"]),
            max_tasks_per_root=record["max_tasks_per_root"],
            max_depth=record["max_depth"],
        )

    @property
    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(
            _BUDGET_POLICY_DOMAIN + _canonical_record(self.to_record())
        ).hexdigest()


@dataclass(frozen=True)
class BudgetPolicyChangeRecord:
    """Auditable compare-and-swap that moved one stable owner to a new policy snapshot."""

    owner: BudgetOwnerRef
    previous_digest: str
    policy_digest: str
    policy_version: str
    actor: str
    idempotency_key: str
    authorization_ref: str
    reason: str
    changed_at: float
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.owner, BudgetOwnerRef):
            raise ContractError("budget policy change owner must be a BudgetOwnerRef")
        for name in (
            "previous_digest",
            "policy_digest",
            "policy_version",
            "actor",
            "idempotency_key",
            "authorization_ref",
            "reason",
        ):
            _budget_text(getattr(self, name), f"budget policy change {name}")
        if self.previous_digest == self.policy_digest:
            raise ContractError("a budget policy change must name a new policy digest")
        if isinstance(self.changed_at, bool) or not isinstance(self.changed_at, (int, float)):
            raise ContractError("budget policy change timestamp must be finite and non-negative")
        if not math.isfinite(float(self.changed_at)) or self.changed_at < 0:
            raise ContractError("budget policy change timestamp must be finite and non-negative")
        object.__setattr__(self, "changed_at", float(self.changed_at))
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ContractError("unsupported budget policy change schema")

    def to_record(self) -> Mapping[str, Any]:
        return {
            "schema_version": self.schema_version,
            "owner": dict(self.owner.to_record()),
            "previous_digest": self.previous_digest,
            "policy_digest": self.policy_digest,
            "policy_version": self.policy_version,
            "actor": self.actor,
            "idempotency_key": self.idempotency_key,
            "authorization_ref": self.authorization_ref,
            "reason": self.reason,
            "changed_at": self.changed_at,
        }

    @classmethod
    def from_record(cls, value: Any) -> "BudgetPolicyChangeRecord":
        record = _record(
            value,
            "budget policy change",
            {
                "schema_version",
                "owner",
                "previous_digest",
                "policy_digest",
                "policy_version",
                "actor",
                "idempotency_key",
                "authorization_ref",
                "reason",
                "changed_at",
            },
        )
        return cls(
            schema_version=record["schema_version"],
            owner=BudgetOwnerRef.from_record(record["owner"]),
            previous_digest=record["previous_digest"],
            policy_digest=record["policy_digest"],
            policy_version=record["policy_version"],
            actor=record["actor"],
            idempotency_key=record["idempotency_key"],
            authorization_ref=record["authorization_ref"],
            reason=record["reason"],
            changed_at=record["changed_at"],
        )


@dataclass(frozen=True)
class TypedBudgetDispatch:
    """The immutable, single-dispatch input to the typed budget store.

    Applicable scopes are deliberately absent: the control store derives them from the committed
    Task, Plan binding and parent-child relations inside the reservation transaction.
    """

    task_id: str
    run_id: str
    dispatch_identity: str
    request_instance: str
    unit: BudgetUnit
    amount: int
    currency: str = ""
    price_version: str = ""

    def __post_init__(self) -> None:
        for name in ("task_id", "run_id", "dispatch_identity", "request_instance"):
            _budget_text(getattr(self, name), f"typed budget dispatch {name}")
        try:
            unit = BudgetUnit(self.unit)
        except (TypeError, ValueError) as exc:
            raise ContractError("typed budget dispatch unit must be supported") from exc
        if unit in {BudgetUnit.SECONDS, BudgetUnit.TOKENS}:
            raise ContractError(f"legacy unit {unit.value!r} cannot be used by typed dispatch storage")
        object.__setattr__(self, "unit", unit)
        if type(self.amount) is not int or self.amount < 0:
            raise ContractError("typed budget dispatch amount must be a non-negative integer")
        if unit is BudgetUnit.COST_MICROS:
            if not isinstance(self.currency, str) or not _CURRENCY_CODE.fullmatch(self.currency):
                raise ContractError("cost_micros dispatch requires a three-letter uppercase currency")
            _budget_text(self.price_version, "typed budget dispatch price version")
        elif self.currency or self.price_version:
            raise ContractError("currency and price_version apply only to cost_micros dispatches")

    def to_record(self) -> Mapping[str, Any]:
        return {
            "task_id": self.task_id,
            "run_id": self.run_id,
            "dispatch_identity": self.dispatch_identity,
            "request_instance": self.request_instance,
            "unit": self.unit.value,
            "amount": self.amount,
            "currency": self.currency,
            "price_version": self.price_version,
        }

    @classmethod
    def from_record(cls, value: Any) -> "TypedBudgetDispatch":
        record = _record(
            value,
            "typed budget dispatch",
            {"task_id", "run_id", "dispatch_identity", "request_instance", "unit", "amount", "currency", "price_version"},
        )
        return cls(**record)


@dataclass(frozen=True)
class BudgetScopeCoverage:
    """One reservation's coverage of a stable owner scope; it never owns usage itself."""

    owner: BudgetOwnerRef
    policy_digest: str = ""
    limit: int | None = None
    mode: BudgetLimitMode | None = None
    currency: str = ""
    price_version: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.owner, BudgetOwnerRef):
            raise ContractError("budget coverage owner must be a BudgetOwnerRef")
        if self.policy_digest:
            _budget_text(self.policy_digest, "budget coverage policy digest")
        if self.limit is not None and (type(self.limit) is not int or self.limit < 0):
            raise ContractError("budget coverage limit must be a non-negative integer")
        if self.mode is not None:
            try:
                object.__setattr__(self, "mode", BudgetLimitMode(self.mode))
            except (TypeError, ValueError) as exc:
                raise ContractError("budget coverage mode must be hard or observe") from exc
        if not self.policy_digest and (self.limit is not None or self.mode is not None):
            raise ContractError("a coverage limit requires a persisted policy snapshot")

    def to_record(self) -> Mapping[str, Any]:
        return {
            "owner": dict(self.owner.to_record()),
            "policy_digest": self.policy_digest,
            "limit": self.limit,
            "mode": None if self.mode is None else self.mode.value,
            "currency": self.currency,
            "price_version": self.price_version,
        }


@dataclass(frozen=True)
class TypedBudgetReservation:
    """One canonical dispatch fact with one or more limit-scope coverage rows."""

    reservation_id: str
    namespace: str
    dispatch: TypedBudgetDispatch
    state: ReservationState = ReservationState.RESERVED
    settled_actual: int | None = None
    note: str = ""
    created_at: float = 0.0
    settled_at: float | None = None
    coverage: tuple[BudgetScopeCoverage, ...] = ()

    def __post_init__(self) -> None:
        _budget_text(self.reservation_id, "typed budget reservation id")
        _budget_text(self.namespace, "typed budget reservation namespace")
        if not isinstance(self.dispatch, TypedBudgetDispatch):
            raise ContractError("typed budget reservation requires a TypedBudgetDispatch")
        try:
            object.__setattr__(self, "state", ReservationState(self.state))
        except (TypeError, ValueError) as exc:
            raise ContractError("typed budget reservation state is invalid") from exc
        if self.settled_actual is not None and (
            type(self.settled_actual) is not int or self.settled_actual < 0
        ):
            raise ContractError("typed budget actual must be a non-negative integer")
        if self.state is ReservationState.SETTLED and self.settled_actual is None:
            raise ContractError("a settled typed reservation must state actual usage")
        if self.state is not ReservationState.SETTLED and self.settled_actual is not None:
            raise ContractError("only a settled typed reservation may carry actual usage")
        if self.state is ReservationState.UNKNOWN and not self.note:
            raise ContractError("an unknown typed reservation must state what is unknown")
        if not isinstance(self.coverage, (tuple, list)) or any(
            not isinstance(item, BudgetScopeCoverage) for item in self.coverage
        ):
            raise ContractError("typed budget reservation coverage must be a finite sequence")
        coverage = tuple(sorted(self.coverage, key=lambda item: item.owner.key))
        keys = [item.owner.key for item in coverage]
        if len(keys) != len(set(keys)):
            raise ContractError("typed budget reservation cannot cover a scope more than once")
        object.__setattr__(self, "coverage", coverage)

    def to_record(self) -> Mapping[str, Any]:
        return {
            "reservation_id": self.reservation_id,
            "namespace": self.namespace,
            "dispatch": dict(self.dispatch.to_record()),
            "state": self.state.value,
            "settled_actual": self.settled_actual,
            "note": self.note,
            "created_at": self.created_at,
            "settled_at": self.settled_at,
            "coverage": [dict(item.to_record()) for item in self.coverage],
        }


@dataclass(frozen=True)
class TypedBudgetUsage:
    """Per-owner accounting; totals are computed from canonical dispatch facts once."""

    owner: BudgetOwnerRef
    unit: BudgetUnit
    policy_digest: str = ""
    limit: int | None = None
    mode: BudgetLimitMode | None = None
    actual: int = 0
    reserved: int = 0
    unknown: int = 0
    currency: str = ""
    price_version: str = ""
    upper_bound_overrun: int = 0

    @property
    def used(self) -> int:
        return self.actual + self.reserved + self.unknown

    @property
    def available(self) -> int | None:
        if self.limit is None or self.mode is not BudgetLimitMode.HARD:
            return None
        return max(0, self.limit - self.used)

    @property
    def over_limit(self) -> int:
        if self.limit is None or self.mode is not BudgetLimitMode.HARD:
            return 0
        return max(0, self.used - self.limit)


@dataclass(frozen=True)
class LegacyBudgetMigrationEntry:
    """One source reservation's result in an explicit legacy-ledger import."""

    source_reservation_id_digest: str
    unit: str
    status: str
    typed_reservation_id: str = ""
    reason_code: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.source_reservation_id_digest, str) or not re.fullmatch(
            r"[0-9a-f]{64}", self.source_reservation_id_digest
        ):
            raise ContractError("legacy budget source id digest must be a SHA-256 hex digest")
        _budget_text(self.unit, "legacy budget migration unit")
        if self.status not in {"migrated", "already_migrated", "skipped"}:
            raise ContractError("legacy budget migration status is unsupported")
        if self.status == "skipped":
            _budget_text(self.reason_code, "legacy budget migration reason")
            if self.typed_reservation_id:
                raise ContractError("a skipped legacy budget row cannot name a typed reservation")
        else:
            _budget_text(self.typed_reservation_id, "typed budget reservation id")
            if self.reason_code:
                raise ContractError("a migrated legacy budget row cannot have a skip reason")

    def to_record(self) -> Mapping[str, Any]:
        return {
            "source_reservation_id_digest": self.source_reservation_id_digest,
            "unit": self.unit,
            "status": self.status,
            "typed_reservation_id": self.typed_reservation_id,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True)
class LegacyBudgetMigrationReport:
    """Bounded, replayable result for an explicit per-root legacy budget import."""

    root_task_id: str
    entries: tuple[LegacyBudgetMigrationEntry, ...] = ()
    schema_version: int = 1

    def __post_init__(self) -> None:
        _budget_text(self.root_task_id, "legacy budget migration root_task_id")
        if not isinstance(self.entries, (tuple, list)) or any(
            not isinstance(item, LegacyBudgetMigrationEntry) for item in self.entries
        ):
            raise ContractError("legacy budget migration entries must be a finite sequence")
        entries = tuple(self.entries)
        digests = [item.source_reservation_id_digest for item in entries]
        if len(digests) != len(set(digests)):
            raise ContractError("legacy budget migration report cannot repeat a source reservation")
        object.__setattr__(self, "entries", entries)
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ContractError("unsupported legacy budget migration report schema")

    @property
    def migrated_count(self) -> int:
        return sum(item.status == "migrated" for item in self.entries)

    @property
    def replayed_count(self) -> int:
        return sum(item.status == "already_migrated" for item in self.entries)

    @property
    def skipped_count(self) -> int:
        return sum(item.status == "skipped" for item in self.entries)

    def to_record(self) -> Mapping[str, Any]:
        return {
            "schema_version": self.schema_version,
            "root_task_id": self.root_task_id,
            "entries": [item.to_record() for item in self.entries],
            "migrated_count": self.migrated_count,
            "replayed_count": self.replayed_count,
            "skipped_count": self.skipped_count,
        }


def snapshot_from_task_budget(
    budget: TaskBudget,
    *,
    owner: BudgetOwnerRef,
    session_id: str,
    policy_version: str,
    hard_bound_refs: Mapping[BudgetUnit | str, str] | None = None,
    cost_currency: str = "",
    price_version: str = "",
) -> BudgetPolicySnapshot:
    """Convert a legacy float TaskBudget without inventing units or rounding limits."""
    if not isinstance(budget, TaskBudget):
        raise ContractError("budget must be a TaskBudget")
    if not isinstance(budget.units, Mapping):
        raise ContractError("legacy TaskBudget units must be a mapping")
    refs: dict[BudgetUnit, str] = {}
    for raw_unit, raw_ref in (hard_bound_refs or {}).items():
        try:
            unit = BudgetUnit(raw_unit)
        except (TypeError, ValueError) as exc:
            raise ContractError(f"unknown hard-bound unit {raw_unit!r}") from exc
        if unit in refs:
            raise ContractError(f"duplicate hard-bound reference for {unit.value}")
        refs[unit] = _budget_text(raw_ref, "hard budget bound reference")

    limits: list[BudgetLimit] = []
    seen: set[BudgetUnit] = set()
    for raw_unit, raw_amount in budget.units.items():
        try:
            legacy_unit = BudgetUnit(raw_unit)
        except (TypeError, ValueError) as exc:
            raise ContractError(f"unknown legacy budget unit {raw_unit!r}") from exc
        if legacy_unit in seen:
            raise ContractError(f"duplicate legacy budget unit {legacy_unit.value}")
        seen.add(legacy_unit)
        if isinstance(raw_amount, bool) or not isinstance(raw_amount, (int, float, Decimal)):
            raise ContractError(f"{legacy_unit.value} limit must be a finite non-negative number")
        try:
            amount = Decimal(str(raw_amount))
        except (InvalidOperation, ValueError) as exc:
            raise ContractError(f"{legacy_unit.value} limit is not a valid number") from exc
        if not amount.is_finite() or amount < 0:
            raise ContractError(f"{legacy_unit.value} limit must be finite and non-negative")

        unit = legacy_unit
        if legacy_unit is BudgetUnit.TOKENS:
            raise ContractError(
                "legacy tokens do not identify input or output usage; supply a typed token policy"
            )
        if legacy_unit is BudgetUnit.SECONDS:
            amount *= Decimal(1000)
            unit = BudgetUnit.DURATION_MILLISECONDS
        if amount != amount.to_integral_value():
            raise ContractError(
                f"{legacy_unit.value} limit cannot be converted exactly to an integer unit"
            )

        if unit in _TOKEN_BUDGET_UNITS:
            mode = BudgetLimitMode.HARD if unit in refs else BudgetLimitMode.OBSERVE
            limit = BudgetLimit(
                unit=unit,
                limit=int(amount),
                mode=mode,
                hard_bound_ref=refs.get(unit, ""),
            )
        elif unit is BudgetUnit.COST_MICROS:
            if not cost_currency or not price_version:
                raise ContractError(
                    "cost_micros conversion needs explicit cost_currency and price_version"
                )
            mode = BudgetLimitMode.HARD if unit in refs else BudgetLimitMode.OBSERVE
            limit = BudgetLimit(
                unit=unit,
                limit=int(amount),
                mode=mode,
                hard_bound_ref=refs.get(unit, ""),
                currency=cost_currency,
                price_version=price_version,
            )
        else:
            limit = BudgetLimit(unit=unit, limit=int(amount), mode=BudgetLimitMode.HARD)
        limits.append(limit)

    orphaned_refs = set(refs) - {
        BudgetUnit.INPUT_TOKENS,
        BudgetUnit.OUTPUT_TOKENS,
        BudgetUnit.COST_MICROS,
    }
    if orphaned_refs:
        raise ContractError(
            "hard-bound references apply only to input_tokens, output_tokens, and cost_micros"
        )
    if not any(item.unit is BudgetUnit.COST_MICROS for item in limits) and (
        cost_currency or price_version
    ):
        raise ContractError("cost metadata was supplied without a cost_micros limit")
    if not {unit for unit in refs if unit in _TOKEN_BUDGET_UNITS or unit is BudgetUnit.COST_MICROS}.issubset(
        {item.unit for item in limits}
    ):
        raise ContractError("a hard-bound reference names a unit absent from TaskBudget")

    return BudgetPolicySnapshot(
        owner=owner,
        session_id=session_id,
        policy_version=policy_version,
        limits=tuple(limits),
        max_tasks_per_root=budget.max_tasks_per_root,
        max_depth=budget.max_depth,
    )


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


class TypedDispatchBudget:
    """Typed model/tool request accounting over the optional typed control-store capability.

    It is the adapter for :class:`DefaultHarness`'s existing reserve/settle callbacks and can also
    be installed on a :class:`ToolGateway`. The task claim is supplied by the worker that owns the
    attempt. Provider request count is exact; token and cost facts are added only when the provider
    reports them, with no estimate written as actual usage.
    """

    _MODEL_PREFIX = "model-request:"
    _MODEL_USAGE_PREFIX = "model-usage-bound:"
    _TOOL_PREFIX = "tool-dispatch:"
    _BOUNDED_USAGE_UNITS = frozenset(
        {BudgetUnit.INPUT_TOKENS, BudgetUnit.OUTPUT_TOKENS, BudgetUnit.COST_MICROS}
    )

    def __init__(
        self,
        control: Any,
        *,
        task_id: str,
        claim: Any | None = None,
        clock: Any = time.time,
        enabled: bool = True,
        upper_bounds: Mapping[str, int | Callable[[Mapping[str, Any]], int]] | None = None,
    ) -> None:
        required = (
            "reserve_typed_budget_dispatch",
            "reserve_typed_budget_dispatches",
            "settle_typed_budget_reservation",
            "read_typed_budget_reservation",
            "read_typed_budget_reservations",
            "read_budget_policies_for_task",
            "record_typed_budget_usage",
        )
        if control is None or any(not callable(getattr(control, name, None)) for name in required):
            raise ContractError(
                "TypedDispatchBudget requires the optional typed budget store capability"
            )
        _budget_text(task_id, "typed dispatch budget task_id")
        if type(enabled) is not bool:
            raise ContractError("typed dispatch budget enabled must be a bool")
        self._control = control
        self._task_id = task_id
        self._claim = claim
        self._clock = clock
        self.enabled = enabled
        if upper_bounds is not None and not isinstance(upper_bounds, Mapping):
            raise ContractError("typed dispatch upper_bounds must be a mapping keyed by hard_bound_ref")
        self._upper_bounds: dict[str, int | Callable[[Mapping[str, Any]], int]] = {}
        for reference, value in (upper_bounds or {}).items():
            _budget_text(reference, "typed dispatch hard-bound reference")
            if type(value) is not int and not callable(value):
                raise ContractError("typed dispatch upper bounds must be integers or callables")
            self._upper_bounds[reference] = value
        self.reserved: list[str] = []
        self._seen_reservation_ids: set[str] = set()
        self._model_reservation_lock = asyncio.Lock()

    @staticmethod
    def _identity(prefix: str, *parts: str) -> str:
        digest = hashlib.sha256(_canonical_record(list(parts))).hexdigest()
        return f"{prefix}{digest}"

    async def _reserve(
        self,
        *,
        run_id: str,
        request_instance: str,
        dispatch_identity: str,
        unit: BudgetUnit,
        amount: int = 1,
        currency: str = "",
        price_version: str = "",
        claim: Any | None = None,
    ) -> TypedBudgetReservation | None:
        if not self.enabled:
            return None
        reservation = await self._control.reserve_typed_budget_dispatch(
            TypedBudgetDispatch(
                task_id=self._task_id,
                run_id=run_id,
                dispatch_identity=dispatch_identity,
                request_instance=request_instance,
                unit=unit,
                amount=amount,
                currency=currency,
                price_version=price_version,
            ),
            claim=self._claim if claim is None else claim,
            now=self._clock(),
        )
        self.reserved.append(reservation.reservation_id)
        return reservation

    def _resolve_usage_bounds(
        self, payload: Mapping[str, Any], policies: tuple[BudgetPolicySnapshot, ...]
    ) -> dict[BudgetUnit, tuple[int, str, str]]:
        by_unit: dict[BudgetUnit, list[tuple[int, str, str]]] = {}
        for policy in policies:
            for limit in policy.limits:
                if limit.mode is not BudgetLimitMode.HARD or limit.unit not in self._BOUNDED_USAGE_UNITS:
                    continue
                resolver = self._upper_bounds.get(limit.hard_bound_ref)
                if resolver is None:
                    raise ContractError(
                        f"hard {limit.unit.value} policy references {limit.hard_bound_ref!r}, "
                        "but this host has no executable upper-bound capability for it"
                    )
                amount = resolver(payload) if callable(resolver) else resolver
                if type(amount) is not int or amount < 0:
                    raise ContractError(
                        f"host upper bound {limit.hard_bound_ref!r} must resolve to a "
                        "non-negative integer for this request"
                    )
                by_unit.setdefault(limit.unit, []).append(
                    (amount, limit.currency, limit.price_version)
                )

        resolved: dict[BudgetUnit, tuple[int, str, str]] = {}
        for unit, bounds in by_unit.items():
            if len(set(bounds)) != 1:
                raise ContractError(
                    f"hard {unit.value} policies in the applicable scopes resolve to "
                    "different request bounds or cost metadata"
                )
            resolved[unit] = bounds[0]
        return resolved

    async def _reserve_model(
        self, payload: Mapping[str, Any], *, call_kind: str
    ) -> TypedBudgetReservation | tuple[TypedBudgetReservation, ...] | None:
        if not self.enabled:
            return None
        if not isinstance(payload, Mapping):
            raise ContractError("typed model dispatch payload must be a mapping")
        task_id = str(payload.get("task_id") or self._task_id)
        if task_id != self._task_id:
            raise ContractError("typed model dispatch payload belongs to another Task")
        run_id = str(payload.get("run_id") or "")
        request_instance = str(payload.get("request_instance") or "")
        if not run_id or not request_instance:
            raise ContractError("typed model dispatch needs run_id and request_instance")
        policies = await self._control.read_budget_policies_for_task(task_id, run_id)
        usage_bounds = self._resolve_usage_bounds(payload, policies)
        expected_policy_digests = {policy.owner.key: policy.digest for policy in policies}
        base_identity = self._identity(
            self._MODEL_PREFIX, task_id, run_id, call_kind, request_instance
        )
        dispatches = [
            TypedBudgetDispatch(
                task_id=task_id,
                run_id=run_id,
                dispatch_identity=base_identity,
                request_instance=request_instance,
                unit=BudgetUnit.MODEL_REQUESTS,
                amount=1,
            )
        ]
        for unit, (amount, currency, price_version) in sorted(
            usage_bounds.items(), key=lambda item: item[0].value
        ):
            dispatches.append(
                TypedBudgetDispatch(
                    task_id=task_id,
                    run_id=run_id,
                    dispatch_identity=self._identity(
                        self._MODEL_USAGE_PREFIX,
                        task_id,
                        run_id,
                        call_kind,
                        request_instance,
                        unit.value,
                    ),
                    request_instance=request_instance,
                    unit=unit,
                    amount=amount,
                    currency=currency,
                    price_version=price_version,
                )
            )

        async with self._model_reservation_lock:
            # A request instance is one real call. A crash left its identity covered, so a
            # restart must use a new instance instead of replaying the old reservation as a
            # free second external request. Unresolved reservations from a prior adapter
            # instance become unknown; current-process concurrent calls remain reserved.
            existing_rows = await self._control.read_typed_budget_reservations(
                task_id=task_id, run_id=run_id
            )
            identities = {item.dispatch_identity for item in dispatches}
            if any(row.dispatch.dispatch_identity in identities for row in existing_rows):
                raise ContractError(
                    "a typed model request_instance already has durable budget coverage; "
                    "a new external call needs a new request_instance"
                )
            for previous in existing_rows:
                if (
                    previous.state is ReservationState.RESERVED
                    and previous.dispatch.dispatch_identity.startswith(
                        (self._MODEL_PREFIX, self._MODEL_USAGE_PREFIX)
                    )
                    and previous.reservation_id not in self._seen_reservation_ids
                ):
                    await self._control.settle_typed_budget_reservation(
                        previous.reservation_id,
                        unknown="recovered an unresolved model dispatch from an earlier worker",
                        note="recovered unresolved Provider dispatch",
                        now=self._clock(),
                    )
            reservations = await self._control.reserve_typed_budget_dispatches(
                tuple(dispatches),
                claim=self._claim,
                now=self._clock(),
                expected_policy_digests=expected_policy_digests,
            )
            if any(item.state is not ReservationState.RESERVED for item in reservations):
                raise ContractError(
                    "the typed model dispatch reservation is already terminal and cannot "
                    "authorize another Provider call"
                )
            self.reserved.extend(item.reservation_id for item in reservations)
            self._seen_reservation_ids.update(item.reservation_id for item in reservations)
        return reservations[0] if len(reservations) == 1 else reservations

    async def reserve(self, payload: Mapping[str, Any]) -> TypedBudgetReservation | tuple[TypedBudgetReservation, ...] | None:
        """Atomically reserve one model request and all hard token/cost bounds."""
        return await self._reserve_model(payload, call_kind="primary")

    async def reserve_auxiliary(
        self,
        payload: Mapping[str, Any],
        *,
        kind: str,
        operation_identity: str | None = None,
    ) -> TypedBudgetReservation | tuple[TypedBudgetReservation, ...] | None:
        """Reserve a summarizer or decision model request through the same typed scopes."""
        _budget_text(kind, "auxiliary model call kind")
        if not isinstance(payload, Mapping):
            raise ContractError("typed auxiliary model payload must be a mapping")
        enriched = dict(payload)
        enriched.setdefault("request_instance", operation_identity or uuid.uuid4().hex)
        return await self._reserve_model(enriched, call_kind=f"aux:{kind}")

    async def reserve_provider_retry(
        self,
        payload: Mapping[str, Any],
        *,
        retry_identity: str,
    ) -> TypedBudgetReservation | tuple[TypedBudgetReservation, ...] | None:
        """Reserve one additional billable Provider request inside a custom adapter.

        The Runner's primary reservation covers the first ``stream_fn`` request. An
        adapter that performs another remote request must call this method before each
        retry, using a stable identity unique to that retry, and settle the returned
        reservation with :meth:`settle_auxiliary`.
        """
        _budget_text(retry_identity, "Provider retry identity")
        return await self.reserve_auxiliary(
            payload,
            kind="provider-retry",
            operation_identity=retry_identity,
        )

    async def settle(
        self,
        reservation: TypedBudgetReservation | tuple[TypedBudgetReservation, ...] | None,
        outcome: Mapping[str, Any],
    ) -> None:
        """Settle exact request count and actual usage; absent hard usage stays unknown."""
        if reservation is None:
            return
        reservations = self._reservation_items(reservation)
        status = str(outcome.get("status") or "")
        if status in {"refused", "not_dispatched"}:
            for item in reservations:
                current = await self._control.read_typed_budget_reservation(item.reservation_id)
                if current is not None and current.state is ReservationState.RELEASED:
                    continue
                await self._control.settle_typed_budget_reservation(
                    item.reservation_id,
                    release=True,
                    note=str(outcome.get("detail") or "the Provider was not called"),
                    now=self._clock(),
                )
            return
        if status in {"completed", "streamed"}:
            base = next(
                (item for item in reservations if item.dispatch.unit is BudgetUnit.MODEL_REQUESTS),
                None,
            )
            if base is None:
                raise ContractError("typed model dispatch has no model_requests reservation")
            await self._control.settle_typed_budget_reservation(
                base.reservation_id,
                actual=1,
                note="the Provider returned a model response",
                now=self._clock(),
            )
            usage = outcome.get("usage")
            if isinstance(usage, Mapping):
                await self._record_provider_usage(base, reservations, usage)
            else:
                await self._settle_missing_usage(reservations)
            return
        reason = str(outcome.get("detail") or "the Provider dispatch outcome is unknown")
        for item in reservations:
            current = await self._control.read_typed_budget_reservation(item.reservation_id)
            if current is not None and current.state is ReservationState.UNKNOWN:
                continue
            await self._control.settle_typed_budget_reservation(
                item.reservation_id, unknown=reason, now=self._clock()
            )

    async def settle_auxiliary(
        self,
        reservation: TypedBudgetReservation | tuple[TypedBudgetReservation, ...] | None,
        *,
        usage: Mapping[str, Any] | None,
        dispatched: bool = True,
        detail: str = "",
    ) -> None:
        """Settle a completed auxiliary call or keep its unresolved usage covered."""
        if not dispatched:
            await self.settle(reservation, {"status": "not_dispatched", "detail": detail})
        elif usage is None:
            await self.settle(reservation, {"status": "unknown", "detail": detail or "auxiliary usage is missing"})
        else:
            normalized = dict(usage)
            if normalized.get("input") is None and normalized.get("input_tokens") is not None:
                normalized["input"] = normalized["input_tokens"]
            if normalized.get("output") is None and normalized.get("output_tokens") is not None:
                normalized["output"] = normalized["output_tokens"]
            await self.settle(reservation, {"status": "completed", "usage": normalized})

    @staticmethod
    def _reservation_items(
        reservation: TypedBudgetReservation | tuple[TypedBudgetReservation, ...] | None,
    ) -> tuple[TypedBudgetReservation, ...]:
        if reservation is None:
            return ()
        items = (reservation,) if isinstance(reservation, TypedBudgetReservation) else tuple(reservation)
        if not items or any(not isinstance(item, TypedBudgetReservation) for item in items):
            raise ContractError("typed dispatch settlement needs one or more TypedBudgetReservation values")
        return items

    async def _settle_missing_usage(
        self, reservations: tuple[TypedBudgetReservation, ...]
    ) -> None:
        for item in reservations:
            if item.dispatch.unit is BudgetUnit.MODEL_REQUESTS:
                continue
            current = await self._control.read_typed_budget_reservation(item.reservation_id)
            if current is not None and current.state in {ReservationState.UNKNOWN, ReservationState.SETTLED}:
                continue
            await self._control.settle_typed_budget_reservation(
                item.reservation_id,
                unknown=f"Provider returned no {item.dispatch.unit.value} usage report",
                now=self._clock(),
            )

    async def _record_provider_usage(
        self,
        reservation: TypedBudgetReservation,
        reservations: tuple[TypedBudgetReservation, ...],
        usage: Mapping[str, Any],
    ) -> None:
        request_instance = reservation.dispatch.request_instance
        report_id = f"provider-final:{request_instance}"
        bounded = {item.dispatch.unit: item for item in reservations}
        reported_units: set[BudgetUnit] = set()
        for field_name, unit in (
            ("input", BudgetUnit.INPUT_TOKENS),
            ("output", BudgetUnit.OUTPUT_TOKENS),
        ):
            value = usage.get(field_name)
            if value is None:
                continue
            if type(value) is not int or value < 0:
                raise ContractError(f"provider usage {field_name} must be a non-negative integer")
            reported_units.add(unit)
            if unit in bounded:
                await self._settle_bound_usage(bounded[unit], value, report_id=report_id)
            else:
                await self._control.record_typed_budget_usage(
                    reservation.reservation_id,
                    report_id=report_id,
                    unit=unit,
                    actual=value,
                    now=self._clock(),
                )
        cost = usage.get("cost")
        if isinstance(cost, Mapping) and cost.get("amount_micros") is not None:
            amount = cost.get("amount_micros")
            currency = cost.get("currency")
            price_version = cost.get("price_version")
            if type(amount) is not int or amount < 0:
                raise ContractError("provider cost amount_micros must be a non-negative integer")
            if not isinstance(currency, str) or not isinstance(price_version, str):
                raise ContractError("provider cost usage needs currency and price_version")
            reported_units.add(BudgetUnit.COST_MICROS)
            if BudgetUnit.COST_MICROS in bounded:
                bound = bounded[BudgetUnit.COST_MICROS]
                if (currency, price_version) != (
                    bound.dispatch.currency,
                    bound.dispatch.price_version,
                ):
                    raise ContractError("Provider cost usage metadata differs from its hard bound")
                await self._settle_bound_usage(bound, amount, report_id=report_id)
            else:
                await self._control.record_typed_budget_usage(
                    reservation.reservation_id,
                    report_id=report_id,
                    unit=BudgetUnit.COST_MICROS,
                    actual=amount,
                    currency=currency,
                    price_version=price_version,
                    now=self._clock(),
                )
        for unit, item in bounded.items():
            if unit not in reported_units:
                current = await self._control.read_typed_budget_reservation(item.reservation_id)
                if current is not None and current.state in {ReservationState.UNKNOWN, ReservationState.SETTLED}:
                    continue
                await self._control.settle_typed_budget_reservation(
                    item.reservation_id,
                    unknown=f"Provider returned no {unit.value} usage report",
                    now=self._clock(),
                )

    async def _settle_bound_usage(
        self, reservation: TypedBudgetReservation, actual: int, *, report_id: str
    ) -> None:
        await self._control.settle_typed_budget_reservation(
            reservation.reservation_id,
            actual=actual,
            note=self._usage_report_note(report_id),
            now=self._clock(),
        )

    @staticmethod
    def _usage_report_note(report_id: str) -> str:
        digest = hashlib.sha256(report_id.encode("utf-8")).hexdigest()
        return f"typed_usage_report:{digest}"

    async def reserve_tool(self, invocation: Any, context: Any) -> TypedBudgetReservation | None:
        """Reserve one Gateway handler dispatch after policy and approval admission."""
        identity = getattr(context, "identity", None)
        if identity is None or not getattr(identity, "run_id", ""):
            raise ContractError("typed tool dispatch needs a server-established RunIdentity")
        operation_id = str(invocation.operation_key())
        dispatch_identity = self._identity(
            self._TOOL_PREFIX,
            self._task_id,
            str(identity.run_id),
            operation_id,
        )
        return await self._reserve(
            run_id=str(identity.run_id),
            request_instance=operation_id,
            dispatch_identity=dispatch_identity,
            unit=BudgetUnit.DISPATCHES,
        )

    async def settle_tool(
        self, reservation: TypedBudgetReservation | None, receipt: Any | None
    ) -> None:
        """Settle tool dispatch count from the Gateway's durable receipt."""
        if reservation is None:
            return
        state = str(getattr(getattr(receipt, "state", ""), "value", getattr(receipt, "state", "")))
        handler_entered = getattr(receipt, "handler_entered", None)
        if handler_entered is not None and type(handler_entered) is not bool:
            raise ContractError("Gateway receipt handler_entered must be bool or None")
        current = await self._control.read_typed_budget_reservation(reservation.reservation_id)
        if current is None:
            raise ContractError("Gateway budget reservation disappeared before settlement")
        if current.state is ReservationState.SETTLED:
            if current.settled_actual == reservation.dispatch.amount and handler_entered is not False:
                return
            if current.settled_actual == 0 and handler_entered is False:
                return
            if (state == "unknown" or receipt is None) and handler_entered is not False:
                return
            raise ContractError("a settled Gateway dispatch conflicts with its durable receipt")
        if current.state is ReservationState.RELEASED:
            if handler_entered is False or (
                handler_entered is None and state in {"rejected", "cancelled", "deferred"}
            ):
                return
            raise ContractError("a released Gateway dispatch conflicts with its durable receipt")
        if current.state is ReservationState.UNKNOWN and (
            receipt is None or handler_entered is None
        ):
            return
        if handler_entered is False:
            await self._control.settle_typed_budget_reservation(
                reservation.reservation_id,
                actual=(0 if current.state is ReservationState.UNKNOWN else None),
                release=current.state is not ReservationState.UNKNOWN,
                note="the Gateway proved the tool handler was not entered",
                now=self._clock(),
            )
        elif handler_entered is True:
            await self._control.settle_typed_budget_reservation(
                reservation.reservation_id,
                actual=reservation.dispatch.amount,
                note=f"the Gateway entered the tool handler before receipt state {state or 'unknown'}",
                now=self._clock(),
            )
        else:
            await self._control.settle_typed_budget_reservation(
                reservation.reservation_id,
                unknown="the Gateway receipt does not record whether the handler was entered",
                now=self._clock(),
            )

    async def reconcile_tool(
        self, invocation: Any, context: Any, receipt: Any | None
    ) -> None:
        """Complete accounting when the Gateway reads an open intent or durable receipt."""
        if not self.enabled:
            return
        if receipt is not None and getattr(receipt, "handler_entered", None) is False:
            identity = getattr(context, "identity", None)
            if identity is None or not getattr(identity, "run_id", ""):
                raise ContractError("typed tool dispatch needs a server-established RunIdentity")
            run_id = str(identity.run_id)
            operation_id = str(invocation.operation_key())
            dispatch_identity = self._identity(
                self._TOOL_PREFIX,
                self._task_id,
                run_id,
                operation_id,
            )
            existing = await self._control.read_typed_budget_reservations(
                task_id=self._task_id,
                run_id=run_id,
            )
            matches = tuple(
                item
                for item in existing
                if item.dispatch.dispatch_identity == dispatch_identity
            )
            if len(matches) > 1:
                raise ContractError("multiple Gateway reservations share one dispatch identity")
            if matches:
                await self.settle_tool(matches[0], receipt)
            return
        reservation = await self.reserve_tool(invocation, context)
        if reservation is None:
            return
        await self.settle_tool(reservation, receipt)

    async def record_late_usage(
        self,
        reservation_id: str,
        *,
        report_id: str,
        unit: BudgetUnit | str,
        actual: int,
        currency: str = "",
        price_version: str = "",
    ) -> TypedBudgetReservation:
        """Persist a delayed Provider report through the same canonical idempotent path."""
        return await self._control.record_typed_budget_usage(
            reservation_id,
            report_id=report_id,
            unit=unit,
            actual=actual,
            currency=currency,
            price_version=price_version,
            now=self._clock(),
        )
