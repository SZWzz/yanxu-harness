"""Atomic persistence for typed dispatch reservations and their budget scope coverage.

The legacy TaskBudget ledger remains in ``budgets`` and is intentionally untouched.  This
capability has its own schema version and is enabled explicitly by the host.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
from typing import Any, Iterable, Mapping, Sequence

from ..errors import ContractError
from .budgets import (
    BudgetLimitMode,
    BudgetOwnerKind,
    BudgetOwnerRef,
    LegacyBudgetMigrationEntry,
    LegacyBudgetMigrationReport,
    BudgetPolicyChangeRecord,
    BudgetPolicySnapshot,
    BudgetScopeCoverage,
    TypedBudgetDispatch,
    TypedBudgetReservation,
    TypedBudgetUsage,
    _budget_text,
)
from .types import BudgetRefused, BudgetReservation, BudgetUnit, ReservationState


def _usage_report_note(report_id: str) -> str:
    digest = hashlib.sha256(report_id.strip().encode("utf-8")).hexdigest()
    return f"typed_usage_report:{digest}"


TYPED_BUDGET_SCHEMA_VERSION = 2
_TYPED_BUDGET_TABLES_V1 = {
    "typed_budget_policies",
    "typed_budget_dispatches",
    "typed_budget_coverage",
}
_TYPED_BUDGET_TABLES = _TYPED_BUDGET_TABLES_V1 | {
    "typed_budget_policy_versions",
    "typed_budget_policy_changes",
}
_TYPED_BUDGET_SCHEMA = """
CREATE TABLE IF NOT EXISTS typed_budget_policies (
    owner_kind TEXT NOT NULL,
    namespace TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    policy_digest TEXT NOT NULL,
    record_json TEXT NOT NULL,
    PRIMARY KEY (owner_kind, namespace, owner_id)
);
CREATE TABLE IF NOT EXISTS typed_budget_dispatches (
    reservation_id TEXT PRIMARY KEY,
    namespace TEXT NOT NULL,
    task_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    dispatch_identity TEXT NOT NULL,
    request_instance TEXT NOT NULL,
    unit TEXT NOT NULL,
    amount INTEGER NOT NULL,
    currency TEXT NOT NULL DEFAULT '',
    price_version TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL,
    settled_actual INTEGER,
    note TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    settled_at REAL,
    UNIQUE (namespace, dispatch_identity)
);
CREATE TABLE IF NOT EXISTS typed_budget_coverage (
    reservation_id TEXT NOT NULL REFERENCES typed_budget_dispatches(reservation_id),
    owner_kind TEXT NOT NULL,
    owner_namespace TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    policy_digest TEXT NOT NULL DEFAULT '',
    limit_amount INTEGER,
    mode TEXT,
    currency TEXT NOT NULL DEFAULT '',
    price_version TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (reservation_id, owner_kind, owner_namespace, owner_id)
);
CREATE INDEX IF NOT EXISTS typed_budget_coverage_owner
    ON typed_budget_coverage(owner_kind, owner_namespace, owner_id, reservation_id);
CREATE INDEX IF NOT EXISTS typed_budget_dispatches_unit_state
    ON typed_budget_dispatches(unit, state);
CREATE TABLE IF NOT EXISTS typed_budget_policy_versions (
    owner_kind TEXT NOT NULL,
    namespace TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    policy_digest TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    record_json TEXT NOT NULL,
    recorded_at REAL NOT NULL,
    PRIMARY KEY (owner_kind, namespace, owner_id, policy_digest),
    UNIQUE (owner_kind, namespace, owner_id, policy_version)
);
CREATE TABLE IF NOT EXISTS typed_budget_policy_changes (
    owner_kind TEXT NOT NULL,
    namespace TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    previous_digest TEXT NOT NULL,
    policy_digest TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    actor TEXT NOT NULL,
    authorization_ref TEXT NOT NULL,
    reason TEXT NOT NULL,
    changed_at REAL NOT NULL,
    PRIMARY KEY (owner_kind, namespace, owner_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS typed_budget_policy_changes_owner
    ON typed_budget_policy_changes(owner_kind, namespace, owner_id, changed_at);
"""

_TYPED_BUDGET_POLICY_V2_SCHEMA = """
CREATE TABLE IF NOT EXISTS typed_budget_policy_versions (
    owner_kind TEXT NOT NULL,
    namespace TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    policy_digest TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    record_json TEXT NOT NULL,
    recorded_at REAL NOT NULL,
    PRIMARY KEY (owner_kind, namespace, owner_id, policy_digest),
    UNIQUE (owner_kind, namespace, owner_id, policy_version)
);
CREATE TABLE IF NOT EXISTS typed_budget_policy_changes (
    owner_kind TEXT NOT NULL,
    namespace TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    previous_digest TEXT NOT NULL,
    policy_digest TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    actor TEXT NOT NULL,
    authorization_ref TEXT NOT NULL,
    reason TEXT NOT NULL,
    changed_at REAL NOT NULL,
    PRIMARY KEY (owner_kind, namespace, owner_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS typed_budget_policy_changes_owner
    ON typed_budget_policy_changes(owner_kind, namespace, owner_id, changed_at);
"""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _reservation_id(namespace: str, dispatch: TypedBudgetDispatch) -> str:
    identity = _json(
        {"namespace": namespace, "dispatch_identity": dispatch.dispatch_identity, "unit": dispatch.unit.value}
    ).encode("utf-8")
    return "typed-dispatch:" + hashlib.sha256(identity).hexdigest()


def _scope_probe(task_id: str, run_id: str) -> TypedBudgetDispatch:
    """A non-persisted identity used only to derive the Task's committed budget scopes."""
    digest = hashlib.sha256(f"{task_id}\0{run_id}".encode("utf-8")).hexdigest()
    return TypedBudgetDispatch(
        task_id=task_id,
        run_id=run_id,
        dispatch_identity=f"budget-scope-read:{digest}",
        request_instance=f"budget-scope-read:{digest}",
        unit=BudgetUnit.MODEL_REQUESTS,
        amount=0,
    )


class TypedBudgetStoreMixin:
    """Shared typed reservation API and scope derivation for control-store adapters."""

    _typed_budget_enabled: bool

    def _require_typed_budget_store(self) -> None:
        if not getattr(self, "_typed_budget_enabled", False):
            raise ContractError("typed budget storage is not enabled for this control store")

    async def put_budget_policy(self, policy: BudgetPolicySnapshot) -> BudgetPolicySnapshot:
        if not isinstance(policy, BudgetPolicySnapshot):
            raise ContractError("put_budget_policy expects a BudgetPolicySnapshot")
        return await asyncio.to_thread(self._put_budget_policy_sync, policy)

    async def update_budget_policy(
        self,
        policy: BudgetPolicySnapshot,
        *,
        expected_policy_digest: str,
        actor: str,
        idempotency_key: str,
        authorization_ref: str,
        reason: str,
        now: float | None = None,
    ) -> BudgetPolicyChangeRecord:
        if not isinstance(policy, BudgetPolicySnapshot):
            raise ContractError("update_budget_policy expects a BudgetPolicySnapshot")
        for name, value in (
            ("expected_policy_digest", expected_policy_digest),
            ("actor", actor),
            ("idempotency_key", idempotency_key),
            ("authorization_ref", authorization_ref),
            ("reason", reason),
        ):
            _budget_text(value, f"budget policy update {name}")
        timestamp = time.time() if now is None else now
        if (
            isinstance(timestamp, bool)
            or not isinstance(timestamp, (int, float))
            or not math.isfinite(float(timestamp))
            or timestamp < 0
        ):
            raise ContractError("budget policy update timestamp must be finite and non-negative")
        return await asyncio.to_thread(
            self._update_budget_policy_sync,
            policy,
            expected_policy_digest=expected_policy_digest,
            actor=actor,
            idempotency_key=idempotency_key,
            authorization_ref=authorization_ref,
            reason=reason,
            now=float(timestamp),
        )

    async def read_budget_policy(self, owner: BudgetOwnerRef) -> BudgetPolicySnapshot | None:
        if not isinstance(owner, BudgetOwnerRef):
            raise ContractError("read_budget_policy expects a BudgetOwnerRef")
        return await asyncio.to_thread(self._read_budget_policy_sync, owner)

    async def read_budget_policy_version(
        self, owner: BudgetOwnerRef, policy_digest: str
    ) -> BudgetPolicySnapshot | None:
        if not isinstance(owner, BudgetOwnerRef):
            raise ContractError("read_budget_policy_version expects a BudgetOwnerRef")
        if not isinstance(policy_digest, str) or not policy_digest.strip():
            raise ContractError("budget policy digest must be non-empty text")
        return await asyncio.to_thread(
            self._read_budget_policy_version_sync, owner, policy_digest
        )

    async def read_budget_policy_changes(
        self, owner: BudgetOwnerRef
    ) -> tuple[BudgetPolicyChangeRecord, ...]:
        if not isinstance(owner, BudgetOwnerRef):
            raise ContractError("read_budget_policy_changes expects a BudgetOwnerRef")
        return await asyncio.to_thread(self._read_budget_policy_changes_sync, owner)

    async def read_budget_policies_for_task(
        self, task_id: str, run_id: str
    ) -> tuple[BudgetPolicySnapshot, ...]:
        """Read policies for every currently committed scope of a Task lineage."""
        return await asyncio.to_thread(
            self._read_budget_policies_for_task_sync, task_id, run_id
        )

    async def reserve_typed_budget_dispatch(
        self,
        dispatch: TypedBudgetDispatch,
        *,
        claim: Any | None = None,
        now: float | None = None,
    ) -> TypedBudgetReservation:
        if not isinstance(dispatch, TypedBudgetDispatch):
            raise ContractError("reserve_typed_budget_dispatch expects a TypedBudgetDispatch")
        return await asyncio.to_thread(
            self._reserve_typed_budget_dispatch_sync, dispatch, claim=claim, now=now
        )

    async def reserve_typed_budget_dispatches(
        self,
        dispatches: Sequence[TypedBudgetDispatch],
        *,
        claim: Any | None = None,
        now: float | None = None,
        expected_policy_digests: Mapping[tuple[str, str, str], str] | None = None,
    ) -> tuple[TypedBudgetReservation, ...]:
        """Atomically reserve the count and any executable usage bounds for one real dispatch."""
        items = tuple(dispatches)
        if not items or any(not isinstance(item, TypedBudgetDispatch) for item in items):
            raise ContractError("typed dispatch batch must contain TypedBudgetDispatch values")
        anchor = items[0]
        if any(
            (item.task_id, item.run_id, item.request_instance)
            != (anchor.task_id, anchor.run_id, anchor.request_instance)
            for item in items
        ):
            raise ContractError("one typed dispatch batch must share Task, Run and request instance")
        if len({item.unit for item in items}) != len(items):
            raise ContractError("one typed dispatch batch may reserve each unit only once")
        if len({item.dispatch_identity for item in items}) != len(items):
            raise ContractError("one typed dispatch batch requires distinct dispatch identities")
        if expected_policy_digests is not None:
            if not isinstance(expected_policy_digests, Mapping):
                raise ContractError("expected budget policy digests must be a mapping")
            expected_policy_digests = dict(expected_policy_digests)
            if any(
                not isinstance(key, tuple)
                or len(key) != 3
                or any(not isinstance(part, str) or not part for part in key)
                or not isinstance(digest, str)
                or not digest
                for key, digest in expected_policy_digests.items()
            ):
                raise ContractError("expected budget policy digests need owner keys and non-empty digests")
        return await asyncio.to_thread(
            self._reserve_typed_budget_dispatches_sync,
            items,
            claim=claim,
            now=now,
            expected_policy_digests=expected_policy_digests,
        )

    async def settle_typed_budget_reservation(
        self,
        reservation_id: str,
        *,
        actual: int | None = None,
        unknown: str = "",
        release: bool = False,
        note: str = "",
        now: float | None = None,
    ) -> TypedBudgetReservation:
        return await asyncio.to_thread(
            self._settle_typed_budget_reservation_sync,
            reservation_id,
            actual=actual,
            unknown=unknown,
            release=release,
            note=note,
            now=now,
        )

    async def record_typed_budget_usage(
        self,
        reservation_id: str,
        *,
        report_id: str,
        unit: BudgetUnit | str,
        actual: int,
        currency: str = "",
        price_version: str = "",
        now: float | None = None,
    ) -> TypedBudgetReservation:
        """Record one provider usage report under the scopes of its source dispatch.

        Reports are keyed by source reservation, report identity and unit. Replaying the same
        report is idempotent; reusing its identity with different usage is a contract error.
        Reports are facts, so a value over a hard limit is still recorded in full.
        """
        if not isinstance(report_id, str) or not report_id.strip():
            raise ContractError("typed budget usage report_id must be non-empty")
        try:
            resolved = BudgetUnit(unit)
        except (TypeError, ValueError) as exc:
            raise ContractError("typed budget usage unit is unsupported") from exc
        if resolved in {BudgetUnit.SECONDS, BudgetUnit.TOKENS}:
            raise ContractError(f"legacy unit {resolved.value!r} has no typed usage view")
        if type(actual) is not int or actual < 0:
            raise ContractError("typed budget reported actual must be a non-negative integer")
        source = await self.read_typed_budget_reservation(reservation_id)
        if source is None:
            raise ContractError(f"no typed budget reservation {reservation_id!r}")
        if resolved is source.dispatch.unit:
            if resolved is BudgetUnit.COST_MICROS:
                if (currency, price_version) != (
                    source.dispatch.currency,
                    source.dispatch.price_version,
                ):
                    raise ContractError("late cost usage metadata differs from its reservation")
            elif currency or price_version:
                raise ContractError("currency and price_version apply only to cost_micros usage")
            return await self.settle_typed_budget_reservation(
                reservation_id,
                actual=actual,
                note=_usage_report_note(report_id),
                now=now,
            )
        return await asyncio.to_thread(
            self._record_typed_budget_usage_sync,
            reservation_id,
            report_id=report_id.strip(),
            unit=resolved,
            actual=actual,
            currency=currency,
            price_version=price_version,
            now=now,
        )

    async def read_typed_budget_reservation(
        self, reservation_id: str
    ) -> TypedBudgetReservation | None:
        return await asyncio.to_thread(self._read_typed_budget_reservation_sync, reservation_id)

    async def read_typed_budget_reservations(
        self,
        *,
        task_id: str,
        run_id: str | None = None,
        state: ReservationState | str | None = None,
    ) -> tuple[TypedBudgetReservation, ...]:
        """Read dispatch facts for recovery without changing their accounting state."""
        if not isinstance(task_id, str) or not task_id.strip():
            raise ContractError("read_typed_budget_reservations requires a task_id")
        resolved_state: ReservationState | None = None
        if state is not None:
            try:
                resolved_state = ReservationState(state)
            except (TypeError, ValueError) as exc:
                raise ContractError("typed budget reservation state is unsupported") from exc
        return await asyncio.to_thread(
            self._read_typed_budget_reservations_sync,
            task_id,
            run_id=run_id,
            state=resolved_state,
        )

    async def read_typed_budget_reservations_for_owner(
        self,
        owner: BudgetOwnerRef,
        *,
        unit: BudgetUnit | str | None = None,
        limit: int = 200,
    ) -> tuple[TypedBudgetReservation, ...]:
        if not isinstance(owner, BudgetOwnerRef):
            raise ContractError("read_typed_budget_reservations_for_owner expects a BudgetOwnerRef")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ContractError("typed budget owner reservation limit must be in [1, 1000]")
        resolved: BudgetUnit | None = None
        if unit is not None:
            try:
                resolved = BudgetUnit(unit)
            except (TypeError, ValueError) as exc:
                raise ContractError("typed budget reservation unit is unsupported") from exc
            if resolved in {BudgetUnit.SECONDS, BudgetUnit.TOKENS}:
                raise ContractError(f"legacy unit {resolved.value!r} has no typed reservation view")
        return await asyncio.to_thread(
            self._read_typed_budget_reservations_for_owner_sync,
            owner,
            unit=resolved,
            limit=limit,
        )

    async def migrate_legacy_budget_reservations(
        self, root_task_id: str
    ) -> LegacyBudgetMigrationReport:
        """Explicitly import only exactly representable legacy facts under their Task root."""
        _budget_text(root_task_id, "legacy budget migration root_task_id")
        if getattr(self, "read_only", False):
            raise ContractError("legacy budget migration requires a writable control store")
        return await asyncio.to_thread(
            self._migrate_legacy_budget_reservations_sync, root_task_id
        )

    async def read_typed_budget_usage(
        self, owner: BudgetOwnerRef, unit: BudgetUnit | str
    ) -> TypedBudgetUsage:
        if not isinstance(owner, BudgetOwnerRef):
            raise ContractError("read_typed_budget_usage expects a BudgetOwnerRef")
        try:
            resolved = BudgetUnit(unit)
        except (TypeError, ValueError) as exc:
            raise ContractError("typed budget usage unit is unsupported") from exc
        if resolved in {BudgetUnit.SECONDS, BudgetUnit.TOKENS}:
            raise ContractError(f"legacy unit {resolved.value!r} has no typed usage view")
        return await asyncio.to_thread(self._read_typed_budget_usage_sync, owner, resolved)

    def _derive_budget_scopes_locked(
        self, dispatch: TypedBudgetDispatch
    ) -> tuple[str, tuple[tuple[BudgetOwnerRef, str], ...]]:
        """Derive all scopes from committed task/plan/child facts; callers cannot omit one."""
        task = self._typed_budget_task_locked(dispatch.task_id)
        if task is None:
            raise ContractError(f"typed budget dispatch names unknown Task {dispatch.task_id!r}")
        if task.run_id != dispatch.run_id:
            raise ContractError("typed budget dispatch run_id does not match the committed Task")

        lineage = []
        grants: list[tuple[BudgetOwnerRef, str]] = []
        seen: set[str] = set()
        current = task
        while True:
            if current.task_id in seen:
                raise ContractError("committed Task parent relations contain a cycle")
            seen.add(current.task_id)
            lineage.append(current)
            if not current.parent_task_id:
                break
            relation = self._typed_budget_child_of_task_locked(current.task_id)
            if relation is None or relation.parent_task_id != current.parent_task_id:
                raise ContractError(
                    f"Task {current.task_id!r} has no committed parent-child relation for its grant scope"
                )
            parent = self._typed_budget_task_locked(relation.parent_task_id)
            if parent is None:
                raise ContractError("a committed child relation refers to a missing parent Task")
            if (
                parent.namespace != current.namespace
                or parent.session_id != current.session_id
                or relation.namespace not in {"", current.namespace}
                or relation.session_id not in {"", current.session_id}
            ):
                raise ContractError("a budgeted child Task must remain inside one namespace and session")
            grants.append(
                (
                    BudgetOwnerRef(
                        BudgetOwnerKind.TASK_GRANT,
                        current.namespace,
                        relation.spawn_identity,
                    ),
                    relation.session_id or current.session_id,
                )
            )
            current = parent

        root = lineage[-1]
        if (task.root_task_id or task.task_id) != root.task_id:
            raise ContractError("Task root_task_id disagrees with its committed parent chain")
        scopes: dict[tuple[str, str, str], tuple[BudgetOwnerRef, str]] = {}
        root_owner = BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, root.namespace, root.task_id)
        scopes[root_owner.key] = (root_owner, root.session_id)

        plan_owners: dict[tuple[str, str], tuple[BudgetOwnerRef, str]] = {}
        if self._typed_budget_plan_schema_available_locked():
            binding_reader = getattr(self, "_plan_binding_for_task_locked", None)
            if callable(binding_reader):
                for ancestor in lineage:
                    binding = binding_reader(ancestor.task_id)
                    if binding is None:
                        continue
                    if binding.namespace != task.namespace:
                        raise ContractError("a Task's Plan binding crosses its namespace")
                    owner = BudgetOwnerRef(BudgetOwnerKind.PLAN, binding.namespace, binding.plan_id)
                    plan_owners[(owner.namespace, owner.owner_id)] = (owner, ancestor.session_id)
        if len(plan_owners) > 1:
            raise ContractError("a Task lineage cannot inherit budget scope from multiple Plans")
        scopes.update({item[0].key: item for item in plan_owners.values()})
        for item in grants:
            scopes[item[0].key] = item
        return task.namespace, tuple(scopes[key] for key in sorted(scopes))

    def _typed_budget_plan_schema_available_locked(self) -> bool:
        if getattr(self, "orchestration_enabled", False):
            return True
        connection = getattr(self, "_connection", None)
        if connection is not None:
            row = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'plan_bindings'"
            ).fetchone()
            return row is not None
        return bool(getattr(self, "_plan_binding_records", {}))

    def _typed_budget_child_of_task_locked(self, child_task_id: str) -> Any | None:
        raise NotImplementedError

    def _typed_budget_task_locked(self, task_id: str) -> Any | None:
        loader = getattr(self, "_load_task_locked", None)
        if callable(loader):
            return loader(task_id)
        return getattr(self, "_tasks", {}).get(task_id)

    def _typed_budget_task_namespace_locked(self, task_id: str) -> str:
        task = self._typed_budget_task_locked(task_id)
        if task is None:
            raise ContractError(f"typed budget dispatch names unknown Task {task_id!r}")
        return str(task.namespace)

    def _validate_budget_policy_transition_locked(
        self, current: BudgetPolicySnapshot, updated: BudgetPolicySnapshot
    ) -> None:
        if current.owner != updated.owner:
            raise ContractError("a budget policy update cannot change its owner")
        if current.session_id != updated.session_id:
            raise ContractError("a budget policy update cannot change its owner's session")
        if current.digest == updated.digest or current.policy_version == updated.policy_version:
            raise ContractError("a budget policy update requires a new policy version and digest")
        old_cost = current.limit_for(BudgetUnit.COST_MICROS)
        new_cost = updated.limit_for(BudgetUnit.COST_MICROS)
        if old_cost is not None and new_cost is not None and old_cost.currency != new_cost.currency:
            raise ContractError("a stable budget owner cannot change cost currency")
        historical_costs = tuple(
            self._typed_budget_reservations_for_owner_locked(
                updated.owner, BudgetUnit.COST_MICROS
            )
        )
        currencies = {item.dispatch.currency for item in historical_costs if item.dispatch.currency}
        if len(currencies) > 1:
            raise ContractError("historical cost facts for this owner use incompatible currencies")
        if new_cost is not None and currencies and new_cost.currency not in currencies:
            raise ContractError("a budget policy update cannot change currency after cost usage exists")

    def _legacy_budget_projection_locked(
        self, root_task_id: str, namespace: str, source: BudgetReservation
    ) -> tuple[LegacyBudgetMigrationEntry, TypedBudgetReservation | None]:
        source_digest = hashlib.sha256(source.reservation_id.encode("utf-8")).hexdigest()

        def skipped(reason: str) -> tuple[LegacyBudgetMigrationEntry, None]:
            return (
                LegacyBudgetMigrationEntry(
                    source_reservation_id_digest=source_digest,
                    unit=source.unit.value,
                    status="skipped",
                    reason_code=reason,
                ),
                None,
            )

        if source.root_task_id != root_task_id:
            return skipped("source_root_mismatch")
        task = self._typed_budget_task_locked(source.task_id)
        if task is None:
            return skipped("source_task_root_mismatch")
        seen: set[str] = set()
        current = task
        while current.parent_task_id:
            if current.task_id in seen:
                return skipped("source_task_lineage_invalid")
            seen.add(current.task_id)
            relation = self._typed_budget_child_of_task_locked(current.task_id)
            if relation is None or relation.parent_task_id != current.parent_task_id:
                return skipped("source_task_lineage_invalid")
            parent = self._typed_budget_task_locked(relation.parent_task_id)
            if (
                parent is None
                or parent.namespace != task.namespace
                or parent.session_id != task.session_id
            ):
                return skipped("source_task_lineage_invalid")
            current = parent
        if current.task_id != root_task_id or (task.root_task_id or task.task_id) != root_task_id:
            return skipped("source_task_root_mismatch")
        if source.unit in {BudgetUnit.SECONDS, BudgetUnit.TOKENS}:
            return skipped("ambiguous_legacy_unit")
        if source.unit is BudgetUnit.COST_MICROS:
            return skipped("cost_metadata_missing")
        if source.estimated:
            return skipped("estimated_legacy_amount")

        def exact_integer(value: Any) -> tuple[int | None, str]:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None, "invalid_amount"
            numeric = float(value)
            if not math.isfinite(numeric) or numeric < 0:
                return None, "invalid_amount"
            if not numeric.is_integer():
                return None, "fractional_amount"
            result = int(value)
            if result > 2**63 - 1:
                return None, "integer_out_of_range"
            return result, ""

        amount, reason = exact_integer(source.amount)
        if amount is None:
            return skipped(reason)
        actual: int | None = None
        if source.state is ReservationState.SETTLED:
            if source.estimated:
                return skipped("estimated_settlement")
            actual, reason = exact_integer(source.settled_amount)
            if actual is None:
                return skipped("invalid_settled_amount" if reason == "invalid_amount" else reason)

        dispatch = TypedBudgetDispatch(
            task_id=source.task_id,
            run_id=f"legacy-budget-import:{source_digest}",
            dispatch_identity=f"legacy-budget-import:{source_digest}",
            request_instance=f"legacy-budget-import:{source_digest}",
            unit=source.unit,
            amount=amount,
        )
        owner = BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, namespace, root_task_id)
        reservation = TypedBudgetReservation(
            reservation_id=_reservation_id(namespace, dispatch),
            namespace=namespace,
            dispatch=dispatch,
            state=source.state,
            settled_actual=actual,
            note=f"legacy_source:{source_digest}",
            created_at=float(source.created_at),
            settled_at=None if source.settled_at is None else float(source.settled_at),
            coverage=(BudgetScopeCoverage(owner=owner),),
        )
        return (
            LegacyBudgetMigrationEntry(
                source_reservation_id_digest=source_digest,
                unit=source.unit.value,
                status="migrated",
                typed_reservation_id=reservation.reservation_id,
            ),
            reservation,
        )

    def _legacy_budget_import_locked(
        self, root_task_id: str, namespace: str, source: BudgetReservation
    ) -> LegacyBudgetMigrationEntry:
        entry, projection = self._legacy_budget_projection_locked(
            root_task_id, namespace, source
        )
        if projection is None:
            return entry
        existing = self._typed_budget_reservation_by_identity_locked(
            namespace, projection.dispatch
        )
        if existing is not None:
            if (
                existing.reservation_id != projection.reservation_id
                or existing.namespace != projection.namespace
                or existing.dispatch != projection.dispatch
                or existing.created_at != projection.created_at
                or existing.coverage != projection.coverage
            ):
                raise ContractError("a migrated legacy budget fact changed after import")
            return LegacyBudgetMigrationEntry(
                source_reservation_id_digest=entry.source_reservation_id_digest,
                unit=entry.unit,
                status="already_migrated",
                typed_reservation_id=existing.reservation_id,
            )
        self._typed_budget_insert_reservation_locked(projection)
        return entry

    def _put_budget_policy_sync(self, policy: BudgetPolicySnapshot) -> BudgetPolicySnapshot:
        raise NotImplementedError

    def _update_budget_policy_sync(
        self,
        policy: BudgetPolicySnapshot,
        *,
        expected_policy_digest: str,
        actor: str,
        idempotency_key: str,
        authorization_ref: str,
        reason: str,
        now: float,
    ) -> BudgetPolicyChangeRecord:
        raise NotImplementedError

    def _read_budget_policy_sync(self, owner: BudgetOwnerRef) -> BudgetPolicySnapshot | None:
        raise NotImplementedError

    def _read_budget_policy_version_sync(
        self, owner: BudgetOwnerRef, policy_digest: str
    ) -> BudgetPolicySnapshot | None:
        raise NotImplementedError

    def _read_budget_policy_changes_sync(
        self, owner: BudgetOwnerRef
    ) -> tuple[BudgetPolicyChangeRecord, ...]:
        raise NotImplementedError

    def _read_budget_policies_for_task_sync(
        self, task_id: str, run_id: str
    ) -> tuple[BudgetPolicySnapshot, ...]:
        raise NotImplementedError

    def _reserve_typed_budget_dispatch_sync(
        self, dispatch: TypedBudgetDispatch, *, claim: Any | None, now: float | None
    ) -> TypedBudgetReservation:
        raise NotImplementedError

    def _reserve_typed_budget_dispatches_sync(
        self,
        dispatches: Sequence[TypedBudgetDispatch],
        *,
        claim: Any | None,
        now: float | None,
        expected_policy_digests: Mapping[tuple[str, str, str], str] | None = None,
    ) -> tuple[TypedBudgetReservation, ...]:
        raise NotImplementedError

    def _assert_budget_policy_digests_locked(
        self,
        dispatch: TypedBudgetDispatch,
        expected_policy_digests: Mapping[tuple[str, str, str], str] | None,
    ) -> None:
        if expected_policy_digests is None:
            return
        _namespace, scopes = self._derive_budget_scopes_locked(dispatch)
        current = {
            owner.key: policy.digest
            for owner, _session_id in scopes
            if (policy := self._typed_budget_policy_locked(owner)) is not None
        }
        if current != dict(expected_policy_digests):
            raise ContractError(
                "budget policy changed after the model dispatch snapshot was read; "
                "the reservation was refused"
            )

    def _settle_typed_budget_reservation_sync(
        self,
        reservation_id: str,
        *,
        actual: int | None,
        unknown: str,
        release: bool,
        note: str,
        now: float | None,
    ) -> TypedBudgetReservation:
        raise NotImplementedError

    def _record_typed_budget_usage_sync(
        self,
        reservation_id: str,
        *,
        report_id: str,
        unit: BudgetUnit,
        actual: int,
        currency: str,
        price_version: str,
        now: float | None,
    ) -> TypedBudgetReservation:
        raise NotImplementedError

    def _read_typed_budget_reservation_sync(
        self, reservation_id: str
    ) -> TypedBudgetReservation | None:
        raise NotImplementedError

    def _read_typed_budget_reservations_sync(
        self,
        task_id: str,
        *,
        run_id: str | None,
        state: ReservationState | None,
    ) -> tuple[TypedBudgetReservation, ...]:
        raise NotImplementedError

    def _read_typed_budget_reservations_for_owner_sync(
        self,
        owner: BudgetOwnerRef,
        *,
        unit: BudgetUnit | None,
        limit: int,
    ) -> tuple[TypedBudgetReservation, ...]:
        raise NotImplementedError

    def _migrate_legacy_budget_reservations_sync(
        self, root_task_id: str
    ) -> LegacyBudgetMigrationReport:
        raise NotImplementedError

    def _read_typed_budget_usage_sync(
        self, owner: BudgetOwnerRef, unit: BudgetUnit
    ) -> TypedBudgetUsage:
        raise NotImplementedError

    def _typed_budget_policy_locked(self, owner: BudgetOwnerRef) -> BudgetPolicySnapshot | None:
        raise NotImplementedError

    def _typed_budget_reservation_by_identity_locked(
        self, namespace: str, dispatch: TypedBudgetDispatch
    ) -> TypedBudgetReservation | None:
        raise NotImplementedError

    def _typed_budget_reservation_locked(self, reservation_id: str) -> TypedBudgetReservation | None:
        raise NotImplementedError

    def _typed_budget_reservations_for_owner_locked(
        self, owner: BudgetOwnerRef, unit: BudgetUnit
    ) -> Iterable[TypedBudgetReservation]:
        raise NotImplementedError

    def _typed_budget_insert_reservation_locked(
        self, reservation: TypedBudgetReservation
    ) -> None:
        raise NotImplementedError

    def _typed_budget_rollback_insert_locked(self, reservation_id: str) -> None:
        """Undo an adapter-local partial insert before its enclosing transaction rolls back."""
        return None

    def _typed_budget_update_reservation_locked(
        self, reservation: TypedBudgetReservation
    ) -> None:
        raise NotImplementedError

    def _typed_budget_usage_locked(
        self, owner: BudgetOwnerRef, unit: BudgetUnit
    ) -> TypedBudgetUsage:
        policy = self._typed_budget_policy_locked(owner)
        limit = policy.limit_for(unit) if policy is not None else None
        actual = reserved = unknown = upper_bound_overrun = 0
        reservations = tuple(self._typed_budget_reservations_for_owner_locked(owner, unit))
        for reservation in reservations:
            if reservation.state is ReservationState.SETTLED:
                settled_actual = reservation.settled_actual or 0
                actual += settled_actual
                if limit is not None and limit.hard_bound_ref:
                    upper_bound_overrun += max(0, settled_actual - reservation.dispatch.amount)
            elif reservation.state is ReservationState.RESERVED:
                reserved += reservation.dispatch.amount
            elif reservation.state is ReservationState.UNKNOWN:
                unknown += reservation.dispatch.amount
        return TypedBudgetUsage(
            owner=owner,
            unit=unit,
            policy_digest="" if policy is None else policy.digest,
            limit=None if limit is None else limit.limit,
            mode=None if limit is None else limit.mode,
            actual=actual,
            reserved=reserved,
            unknown=unknown,
            currency="" if limit is None else limit.currency,
            price_version="" if limit is None else limit.price_version,
            upper_bound_overrun=upper_bound_overrun,
        )

    def _reserve_typed_budget_locked(
        self,
        dispatch: TypedBudgetDispatch,
        *,
        claim: Any | None,
        now: float | None,
        enforce_limits: bool = True,
    ) -> TypedBudgetReservation:
        self._require_typed_budget_store()
        task = self._typed_budget_task_locked(dispatch.task_id)
        if task is None:
            raise ContractError(f"typed budget dispatch names unknown Task {dispatch.task_id!r}")
        at = time.time() if now is None else now
        if claim is not None:
            if claim.task_id != dispatch.task_id:
                raise ContractError("typed budget dispatch claim belongs to another Task")
            assert_claim = getattr(self, "_assert_claim_locked", None)
            if callable(assert_claim):
                assert_claim(claim, at=at)
            else:
                self._assert_claim(claim)

        namespace, scopes = self._derive_budget_scopes_locked(dispatch)
        existing = self._typed_budget_reservation_by_identity_locked(namespace, dispatch)
        if existing is not None:
            if existing.dispatch != dispatch or existing.namespace != namespace:
                raise ContractError(
                    "the same typed dispatch identity was replayed with different Task/Run, "
                    "request instance, amount, unit, currency or price version"
                )
            expected = tuple(item[0].key for item in scopes)
            actual = tuple(item.owner.key for item in existing.coverage)
            if expected != actual:
                raise ContractError("the same typed dispatch identity resolved to different budget scopes")
            return existing

        coverage: list[BudgetScopeCoverage] = []
        cost_versions: set[tuple[str, str]] = set()
        for owner, session_id in scopes:
            policy = self._typed_budget_policy_locked(owner)
            if policy is not None and policy.session_id != session_id:
                raise ContractError(f"budget policy session does not match committed scope {owner.key!r}")
            limit = None if policy is None else policy.limit_for(dispatch.unit)
            coverage.append(
                BudgetScopeCoverage(
                    owner=owner,
                    policy_digest="" if policy is None else policy.digest,
                    limit=None if limit is None else limit.limit,
                    mode=None if limit is None else limit.mode,
                    currency="" if limit is None else limit.currency,
                    price_version="" if limit is None else limit.price_version,
                )
            )
            if dispatch.unit is BudgetUnit.COST_MICROS and limit is not None:
                cost_versions.add((limit.currency, limit.price_version))
        if dispatch.unit is BudgetUnit.COST_MICROS:
            cost_versions.add((dispatch.currency, dispatch.price_version))
            if len(cost_versions) > 1:
                raise ContractError("cost budget scopes must use the same currency and price version")

        for item in coverage:
            if item.mode is not BudgetLimitMode.HARD or item.limit is None:
                continue
            usage = self._typed_budget_usage_locked(item.owner, dispatch.unit)
            available = max(0, item.limit - usage.used)
            if enforce_limits and dispatch.amount > available:
                raise BudgetRefused(
                    "/".join(item.owner.key),
                    dispatch.unit.value,
                    dispatch.amount,
                    available,
                    "one dispatch must fit every applicable budget scope",
                )

        reservation = TypedBudgetReservation(
            reservation_id=_reservation_id(namespace, dispatch),
            namespace=namespace,
            dispatch=dispatch,
            created_at=at,
            coverage=tuple(coverage),
        )
        try:
            self._typed_budget_insert_reservation_locked(reservation)
        except BaseException:
            self._typed_budget_rollback_insert_locked(reservation.reservation_id)
            raise
        return reservation

    def _settle_typed_budget_locked(
        self,
        reservation_id: str,
        *,
        actual: int | None,
        unknown: str,
        release: bool,
        note: str,
        now: float | None,
    ) -> TypedBudgetReservation:
        self._require_typed_budget_store()
        if type(release) is not bool:
            raise ContractError("release must be a bool")
        if actual is not None and (type(actual) is not int or actual < 0):
            raise ContractError("typed budget actual must be a non-negative integer")
        choices = int(actual is not None) + int(bool(unknown)) + int(release)
        if choices != 1:
            raise ContractError("settle exactly one of actual, unknown, or release")
        reservation = self._typed_budget_reservation_locked(reservation_id)
        if reservation is None:
            raise ContractError(f"no typed budget reservation {reservation_id!r}")
        at = time.time() if now is None else now
        if reservation.state is ReservationState.SETTLED:
            if actual == reservation.settled_actual and not unknown and not release:
                if (
                    reservation.note.startswith("typed_usage_report:")
                    and note.startswith("typed_usage_report:")
                    and reservation.note != note
                ):
                    raise ContractError(
                        "a settled typed usage reservation was replayed with a different report identity"
                    )
                return reservation
            raise ContractError("a settled typed budget reservation cannot be changed or refunded")
        if reservation.state is ReservationState.RELEASED:
            if release:
                return reservation
            raise ContractError("a released typed budget reservation is final")
        if reservation.state is ReservationState.UNKNOWN:
            if unknown == reservation.note and not release and actual is None:
                return reservation
            if actual is None or release:
                raise ContractError("an unknown typed budget reservation needs a late usage report")
            updated = TypedBudgetReservation(
                **{
                    **reservation.__dict__,
                    "state": ReservationState.SETTLED,
                    "settled_actual": actual,
                    "note": note or "late usage report resolved the unknown dispatch",
                    "settled_at": at,
                }
            )
            self._typed_budget_update_reservation_locked(updated)
            return updated
        if release:
            updated = TypedBudgetReservation(
                **{
                    **reservation.__dict__,
                    "state": ReservationState.RELEASED,
                    "note": note or "the external dispatch was proven not to have started",
                    "settled_at": at,
                }
            )
        elif unknown:
            updated = TypedBudgetReservation(
                **{
                    **reservation.__dict__,
                    "state": ReservationState.UNKNOWN,
                    "note": unknown,
                    "settled_at": None,
                }
            )
        else:
            updated = TypedBudgetReservation(
                **{
                    **reservation.__dict__,
                    "state": ReservationState.SETTLED,
                    "settled_actual": actual,
                    "note": note,
                    "settled_at": at,
                }
            )
        self._typed_budget_update_reservation_locked(updated)
        return updated


class SqliteTypedBudgetMixin(TypedBudgetStoreMixin):
    """SQLite implementation; all read-check-write decisions share one BEGIN IMMEDIATE."""

    def initialize_typed_budget_schema(self, *, enabled: bool = False) -> None:
        if type(enabled) is not bool:
            raise ContractError("enable_typed_budgets must be a bool")
        if not enabled:
            # Typed budgets are an opt-in capability. An older or newer optional typed-budget
            # schema must not prevent ordinary Task control access when this capability is unused.
            self._typed_budget_enabled = False
            return

        def inspect() -> tuple[int | None, set[str]]:
            row = self._connection.execute(
                "SELECT value FROM control_meta WHERE key = 'typed_budget_schema_version'"
            ).fetchone()
            try:
                version = None if row is None else int(row[0])
            except (TypeError, ValueError) as exc:
                raise ContractError("typed budget schema version is not an integer") from exc
            tables = {
                str(item[0])
                for item in self._connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            return version, tables & _TYPED_BUDGET_TABLES

        version, tables = self._read(inspect)
        if self.read_only:
            if version is not None and (version != TYPED_BUDGET_SCHEMA_VERSION or tables != _TYPED_BUDGET_TABLES):
                raise ContractError("typed budget store schema is incomplete or unsupported")
            if version is None:
                raise ContractError("read-only control store has no typed budget schema")
            self._typed_budget_enabled = True
            return
        if version == 1:
            if tables != _TYPED_BUDGET_TABLES_V1:
                raise ContractError("typed budget v1 schema is incomplete; refusing migration")

            def migrate_v1() -> None:
                for statement in _TYPED_BUDGET_POLICY_V2_SCHEMA.split(";"):
                    if statement.strip():
                        self._connection.execute(statement)
                rows = self._connection.execute(
                    "SELECT owner_kind, namespace, owner_id, policy_digest, policy_version, record_json "
                    "FROM typed_budget_policies"
                ).fetchall()
                for row in rows:
                    owner = BudgetOwnerRef(BudgetOwnerKind(str(row[0])), str(row[1]), str(row[2]))
                    policy = BudgetPolicySnapshot.from_record(json.loads(str(row[5])))
                    if policy.owner != owner or policy.digest != str(row[3]) or policy.policy_version != str(row[4]):
                        raise ContractError("typed budget v1 policy identity or digest is inconsistent")
                    self._connection.execute(
                        "INSERT INTO typed_budget_policy_versions(owner_kind, namespace, owner_id, "
                        "policy_digest, policy_version, record_json, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (*owner.key, policy.digest, policy.policy_version, _json(policy.to_record()), 0.0),
                    )
                self._connection.execute(
                    "UPDATE control_meta SET value = ? WHERE key = 'typed_budget_schema_version'",
                    (str(TYPED_BUDGET_SCHEMA_VERSION),),
                )

            self._in_transaction(migrate_v1)
            version = TYPED_BUDGET_SCHEMA_VERSION
            tables = _TYPED_BUDGET_TABLES
        if version is not None:
            if version != TYPED_BUDGET_SCHEMA_VERSION or tables != _TYPED_BUDGET_TABLES:
                raise ContractError("typed budget store schema is incomplete or unsupported")
            self._typed_budget_enabled = enabled
            return
        if tables:
            raise ContractError("typed budget tables exist without a schema version")

        def create() -> None:
            for statement in _TYPED_BUDGET_SCHEMA.split(";"):
                if statement.strip():
                    self._connection.execute(statement)
            self._connection.execute(
                "INSERT INTO control_meta(key, value) VALUES ('typed_budget_schema_version', ?)",
                (str(TYPED_BUDGET_SCHEMA_VERSION),),
            )

        self._in_transaction(create)
        self._typed_budget_enabled = enabled

    def _typed_budget_child_of_task_locked(self, child_task_id: str) -> Any | None:
        row = self._connection.execute(
            "SELECT spawn_identity FROM children WHERE child_task_id = ?", (child_task_id,)
        ).fetchone()
        return None if row is None else self._load_child_locked(str(row[0]))

    def _put_budget_policy_sync(self, policy: BudgetPolicySnapshot) -> BudgetPolicySnapshot:
        self._require_typed_budget_store()

        def work() -> BudgetPolicySnapshot:
            existing = self._typed_budget_policy_locked(policy.owner)
            if existing is not None:
                if existing.digest == policy.digest:
                    return existing
                raise ContractError(
                    f"budget policy for {policy.owner.key!r} is immutable; use update_budget_policy"
                )
            self._connection.execute(
                "INSERT INTO typed_budget_policies(owner_kind, namespace, owner_id, session_id, "
                "policy_version, policy_digest, record_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    policy.owner.kind.value,
                    policy.owner.namespace,
                    policy.owner.owner_id,
                    policy.session_id,
                    policy.policy_version,
                    policy.digest,
                    _json(policy.to_record()),
                ),
            )
            self._connection.execute(
                "INSERT INTO typed_budget_policy_versions(owner_kind, namespace, owner_id, "
                "policy_digest, policy_version, record_json, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (*policy.owner.key, policy.digest, policy.policy_version, _json(policy.to_record()), time.time()),
            )
            return policy

        return self._in_transaction(work)

    def _update_budget_policy_sync(
        self,
        policy: BudgetPolicySnapshot,
        *,
        expected_policy_digest: str,
        actor: str,
        idempotency_key: str,
        authorization_ref: str,
        reason: str,
        now: float,
    ) -> BudgetPolicyChangeRecord:
        self._require_typed_budget_store()

        def work() -> BudgetPolicyChangeRecord:
            row = self._connection.execute(
                "SELECT previous_digest, policy_digest, policy_version, actor, authorization_ref, reason, changed_at "
                "FROM typed_budget_policy_changes WHERE owner_kind = ? AND namespace = ? AND owner_id = ? "
                "AND idempotency_key = ?",
                (*policy.owner.key, idempotency_key),
            ).fetchone()
            if row is not None:
                existing = BudgetPolicyChangeRecord(
                    owner=policy.owner,
                    previous_digest=str(row[0]),
                    policy_digest=str(row[1]),
                    policy_version=str(row[2]),
                    actor=str(row[3]),
                    idempotency_key=idempotency_key,
                    authorization_ref=str(row[4]),
                    reason=str(row[5]),
                    changed_at=float(row[6]),
                )
                if (
                    existing.previous_digest == expected_policy_digest
                    and existing.policy_digest == policy.digest
                    and existing.policy_version == policy.policy_version
                    and existing.actor == actor
                    and existing.authorization_ref == authorization_ref
                    and existing.reason == reason
                ):
                    return existing
                raise ContractError("budget policy update idempotency key conflicts with a prior change")

            current = self._typed_budget_policy_locked(policy.owner)
            if current is None:
                raise ContractError("cannot update a budget policy that has not been installed")
            if current.digest != expected_policy_digest:
                raise ContractError(
                    f"expected policy digest {expected_policy_digest!r}, found {current.digest!r}"
                )
            self._validate_budget_policy_transition_locked(current, policy)
            version = self._connection.execute(
                "SELECT 1 FROM typed_budget_policy_versions WHERE owner_kind = ? AND namespace = ? "
                "AND owner_id = ? AND policy_version = ?",
                (*policy.owner.key, policy.policy_version),
            ).fetchone()
            if version is not None:
                raise ContractError("budget policy version is immutable and already exists")
            change = BudgetPolicyChangeRecord(
                owner=policy.owner,
                previous_digest=current.digest,
                policy_digest=policy.digest,
                policy_version=policy.policy_version,
                actor=actor,
                idempotency_key=idempotency_key,
                authorization_ref=authorization_ref,
                reason=reason,
                changed_at=now,
            )
            self._connection.execute(
                "INSERT INTO typed_budget_policy_versions(owner_kind, namespace, owner_id, "
                "policy_digest, policy_version, record_json, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (*policy.owner.key, policy.digest, policy.policy_version, _json(policy.to_record()), now),
            )
            self._connection.execute(
                "INSERT INTO typed_budget_policy_changes(owner_kind, namespace, owner_id, idempotency_key, "
                "previous_digest, policy_digest, policy_version, actor, authorization_ref, reason, changed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    *policy.owner.key,
                    idempotency_key,
                    current.digest,
                    policy.digest,
                    policy.policy_version,
                    actor,
                    authorization_ref,
                    reason,
                    now,
                ),
            )
            self._connection.execute(
                "UPDATE typed_budget_policies SET session_id = ?, policy_version = ?, policy_digest = ?, "
                "record_json = ? WHERE owner_kind = ? AND namespace = ? AND owner_id = ?",
                (policy.session_id, policy.policy_version, policy.digest, _json(policy.to_record()), *policy.owner.key),
            )
            return change

        return self._in_transaction(work)

    def _read_budget_policy_sync(self, owner: BudgetOwnerRef) -> BudgetPolicySnapshot | None:
        self._require_typed_budget_store()
        return self._read(lambda: self._typed_budget_policy_locked(owner))

    def _read_budget_policy_version_sync(
        self, owner: BudgetOwnerRef, policy_digest: str
    ) -> BudgetPolicySnapshot | None:
        self._require_typed_budget_store()

        def read() -> BudgetPolicySnapshot | None:
            row = self._connection.execute(
                "SELECT record_json FROM typed_budget_policy_versions WHERE owner_kind = ? AND namespace = ? "
                "AND owner_id = ? AND policy_digest = ?",
                (*owner.key, policy_digest),
            ).fetchone()
            if row is None:
                return None
            policy = BudgetPolicySnapshot.from_record(json.loads(str(row[0])))
            if policy.owner != owner or policy.digest != policy_digest:
                raise ContractError("stored typed budget policy version identity or digest is inconsistent")
            return policy

        return self._read(read)

    def _read_budget_policy_changes_sync(
        self, owner: BudgetOwnerRef
    ) -> tuple[BudgetPolicyChangeRecord, ...]:
        self._require_typed_budget_store()

        def read() -> tuple[BudgetPolicyChangeRecord, ...]:
            rows = self._connection.execute(
                "SELECT idempotency_key, previous_digest, policy_digest, policy_version, actor, "
                "authorization_ref, reason, changed_at FROM typed_budget_policy_changes "
                "WHERE owner_kind = ? AND namespace = ? AND owner_id = ? ORDER BY rowid",
                owner.key,
            ).fetchall()
            return tuple(
                BudgetPolicyChangeRecord(
                    owner=owner,
                    idempotency_key=str(row[0]),
                    previous_digest=str(row[1]),
                    policy_digest=str(row[2]),
                    policy_version=str(row[3]),
                    actor=str(row[4]),
                    authorization_ref=str(row[5]),
                    reason=str(row[6]),
                    changed_at=float(row[7]),
                )
                for row in rows
            )

        return self._read(read)

    def _read_budget_policies_for_task_sync(
        self, task_id: str, run_id: str
    ) -> tuple[BudgetPolicySnapshot, ...]:
        self._require_typed_budget_store()

        def work() -> tuple[BudgetPolicySnapshot, ...]:
            namespace, scopes = self._derive_budget_scopes_locked(
                _scope_probe(task_id, run_id)
            )
            _ = namespace
            return tuple(
                policy
                for owner, _session_id in scopes
                if (policy := self._typed_budget_policy_locked(owner)) is not None
            )

        return self._read(work)

    def _read_typed_budget_reservations_for_owner_sync(
        self,
        owner: BudgetOwnerRef,
        *,
        unit: BudgetUnit | None,
        limit: int,
    ) -> tuple[TypedBudgetReservation, ...]:
        self._require_typed_budget_store()

        def work() -> tuple[TypedBudgetReservation, ...]:
            clauses = [
                "c.owner_kind = ?",
                "c.owner_namespace = ?",
                "c.owner_id = ?",
            ]
            parameters: list[Any] = list(owner.key)
            if unit is not None:
                clauses.append("d.unit = ?")
                parameters.append(unit.value)
            rows = self._connection.execute(
                "SELECT DISTINCT d.reservation_id FROM typed_budget_dispatches d "
                "JOIN typed_budget_coverage c ON c.reservation_id = d.reservation_id WHERE "
                + " AND ".join(clauses)
                + " ORDER BY d.created_at, d.reservation_id LIMIT ?",
                (*parameters, limit),
            ).fetchall()
            return tuple(
                reservation
                for row in rows
                if (reservation := self._typed_budget_reservation_locked(str(row[0]))) is not None
            )

        return self._read(work)

    def _migrate_legacy_budget_reservations_sync(
        self, root_task_id: str
    ) -> LegacyBudgetMigrationReport:
        self._require_typed_budget_store()

        def work() -> LegacyBudgetMigrationReport:
            root = self._typed_budget_task_locked(root_task_id)
            if (
                root is None
                or root.parent_task_id
                or (root.root_task_id or root.task_id) != root_task_id
            ):
                raise ContractError("legacy budget migration requires an existing root Task")
            namespace = self._typed_budget_task_namespace_locked(root_task_id)
            rows = self._connection.execute(
                "SELECT reservation_id FROM budgets WHERE root_task_id = ? "
                "ORDER BY created_at, reservation_id",
                (root_task_id,),
            ).fetchall()
            entries = []
            for row in rows:
                source = self._load_reservation_locked(str(row[0]))
                if source is None:
                    raise ContractError("a legacy budget row disappeared during migration")
                entries.append(
                    self._legacy_budget_import_locked(root_task_id, namespace, source)
                )
            return LegacyBudgetMigrationReport(root_task_id=root_task_id, entries=tuple(entries))

        return self._in_transaction(work)

    def _reserve_typed_budget_dispatch_sync(
        self, dispatch: TypedBudgetDispatch, *, claim: Any | None, now: float | None
    ) -> TypedBudgetReservation:
        def work() -> TypedBudgetReservation:
            return self._reserve_typed_budget_locked(dispatch, claim=claim, now=now)

        return self._in_transaction(work)

    def _reserve_typed_budget_dispatches_sync(
        self,
        dispatches: Sequence[TypedBudgetDispatch],
        *,
        claim: Any | None,
        now: float | None,
        expected_policy_digests: Mapping[tuple[str, str, str], str] | None = None,
    ) -> tuple[TypedBudgetReservation, ...]:
        def work() -> tuple[TypedBudgetReservation, ...]:
            self._assert_budget_policy_digests_locked(
                dispatches[0], expected_policy_digests
            )
            existing = [
                self._typed_budget_reservation_by_identity_locked(
                    self._typed_budget_task_namespace_locked(item.task_id), item
                )
                for item in dispatches
            ]
            if any(item is not None for item in existing) and not all(
                item is not None for item in existing
            ):
                raise ContractError("typed dispatch batch has only partial persisted coverage")
            return tuple(
                self._reserve_typed_budget_locked(item, claim=claim, now=now)
                for item in dispatches
            )

        return self._in_transaction(work)

    def _record_typed_budget_usage_sync(
        self,
        reservation_id: str,
        *,
        report_id: str,
        unit: BudgetUnit,
        actual: int,
        currency: str,
        price_version: str,
        now: float | None,
    ) -> TypedBudgetReservation:
        def work() -> TypedBudgetReservation:
            source = self._typed_budget_reservation_locked(reservation_id)
            if source is None:
                raise ContractError(f"no typed budget reservation {reservation_id!r}")
            report_key = hashlib.sha256(
                _json(
                    {
                        "source_reservation_id": reservation_id,
                        "report_id": report_id,
                        "unit": unit.value,
                    }
                ).encode("utf-8")
            ).hexdigest()
            dispatch = TypedBudgetDispatch(
                task_id=source.dispatch.task_id,
                run_id=source.dispatch.run_id,
                dispatch_identity=f"usage-report:{report_key}",
                request_instance=source.dispatch.request_instance,
                unit=unit,
                amount=actual,
                currency=currency,
                price_version=price_version,
            )
            existing = self._typed_budget_reservation_by_identity_locked(
                source.namespace, dispatch
            )
            if existing is not None:
                if existing.dispatch != dispatch:
                    raise ContractError(
                        "a typed usage report identity was replayed with different unit or amount"
                    )
                if existing.state is ReservationState.SETTLED:
                    return existing
                if existing.state is ReservationState.RELEASED:
                    raise ContractError("a released typed usage report cannot become actual usage")
                return self._settle_typed_budget_locked(
                    existing.reservation_id,
                    actual=actual,
                    unknown="",
                    release=False,
                    note=_usage_report_note(report_id),
                    now=now,
                )
            reservation = self._reserve_typed_budget_locked(
                dispatch, claim=None, now=now, enforce_limits=False
            )
            return self._settle_typed_budget_locked(
                reservation.reservation_id,
                actual=actual,
                unknown="",
                release=False,
                note=_usage_report_note(report_id),
                now=now,
            )

        return self._in_transaction(work)

    def _settle_typed_budget_reservation_sync(
        self,
        reservation_id: str,
        *,
        actual: int | None,
        unknown: str,
        release: bool,
        note: str,
        now: float | None,
    ) -> TypedBudgetReservation:
        return self._in_transaction(
            lambda: self._settle_typed_budget_locked(
                reservation_id,
                actual=actual,
                unknown=unknown,
                release=release,
                note=note,
                now=now,
            )
        )

    def _read_typed_budget_reservation_sync(
        self, reservation_id: str
    ) -> TypedBudgetReservation | None:
        self._require_typed_budget_store()
        return self._read(lambda: self._typed_budget_reservation_locked(reservation_id))

    def _read_typed_budget_reservations_sync(
        self,
        task_id: str,
        *,
        run_id: str | None,
        state: ReservationState | None,
    ) -> tuple[TypedBudgetReservation, ...]:
        self._require_typed_budget_store()

        def work() -> tuple[TypedBudgetReservation, ...]:
            clauses = ["task_id = ?"]
            parameters: list[Any] = [task_id]
            if run_id is not None:
                clauses.append("run_id = ?")
                parameters.append(run_id)
            if state is not None:
                clauses.append("state = ?")
                parameters.append(state.value)
            rows = self._connection.execute(
                "SELECT reservation_id FROM typed_budget_dispatches WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created_at, reservation_id",
                parameters,
            ).fetchall()
            return tuple(
                reservation
                for row in rows
                if (reservation := self._typed_budget_reservation_locked(str(row[0]))) is not None
            )

        return self._read(work)

    def _read_typed_budget_usage_sync(
        self, owner: BudgetOwnerRef, unit: BudgetUnit
    ) -> TypedBudgetUsage:
        self._require_typed_budget_store()
        return self._read(lambda: self._typed_budget_usage_locked(owner, unit))

    def _typed_budget_policy_locked(self, owner: BudgetOwnerRef) -> BudgetPolicySnapshot | None:
        row = self._connection.execute(
            "SELECT record_json, policy_digest FROM typed_budget_policies WHERE owner_kind = ? "
            "AND namespace = ? AND owner_id = ?",
            owner.key,
        ).fetchone()
        if row is None:
            return None
        policy = BudgetPolicySnapshot.from_record(json.loads(str(row[0])))
        if policy.owner != owner or policy.digest != str(row[1]):
            raise ContractError("stored typed budget policy identity or digest is inconsistent")
        return policy

    def _typed_budget_reservation_by_identity_locked(
        self, namespace: str, dispatch: TypedBudgetDispatch
    ) -> TypedBudgetReservation | None:
        row = self._connection.execute(
            "SELECT reservation_id FROM typed_budget_dispatches WHERE namespace = ? "
            "AND dispatch_identity = ?",
            (namespace, dispatch.dispatch_identity),
        ).fetchone()
        return None if row is None else self._typed_budget_reservation_locked(str(row[0]))

    def _typed_budget_reservation_locked(self, reservation_id: str) -> TypedBudgetReservation | None:
        row = self._connection.execute(
            "SELECT reservation_id, namespace, task_id, run_id, dispatch_identity, request_instance, "
            "unit, amount, currency, price_version, state, settled_actual, note, created_at, settled_at "
            "FROM typed_budget_dispatches WHERE reservation_id = ?",
            (reservation_id,),
        ).fetchone()
        if row is None:
            return None
        coverage_rows = self._connection.execute(
            "SELECT owner_kind, owner_namespace, owner_id, policy_digest, limit_amount, mode, currency, "
            "price_version FROM typed_budget_coverage WHERE reservation_id = ? "
            "ORDER BY owner_kind, owner_namespace, owner_id",
            (reservation_id,),
        ).fetchall()
        return TypedBudgetReservation(
            reservation_id=str(row[0]),
            namespace=str(row[1]),
            dispatch=TypedBudgetDispatch(
                task_id=str(row[2]),
                run_id=str(row[3]),
                dispatch_identity=str(row[4]),
                request_instance=str(row[5]),
                unit=BudgetUnit(str(row[6])),
                amount=int(row[7]),
                currency=str(row[8]),
                price_version=str(row[9]),
            ),
            state=ReservationState(str(row[10])),
            settled_actual=None if row[11] is None else int(row[11]),
            note=str(row[12]),
            created_at=float(row[13]),
            settled_at=None if row[14] is None else float(row[14]),
            coverage=tuple(
                BudgetScopeCoverage(
                    owner=BudgetOwnerRef(BudgetOwnerKind(str(item[0])), str(item[1]), str(item[2])),
                    policy_digest=str(item[3]),
                    limit=None if item[4] is None else int(item[4]),
                    mode=None if item[5] is None else BudgetLimitMode(str(item[5])),
                    currency=str(item[6]),
                    price_version=str(item[7]),
                )
                for item in coverage_rows
            ),
        )

    def _typed_budget_reservations_for_owner_locked(
        self, owner: BudgetOwnerRef, unit: BudgetUnit
    ) -> Iterable[TypedBudgetReservation]:
        rows = self._connection.execute(
            "SELECT d.reservation_id FROM typed_budget_dispatches d JOIN typed_budget_coverage c "
            "ON c.reservation_id = d.reservation_id WHERE c.owner_kind = ? AND c.owner_namespace = ? "
            "AND c.owner_id = ? AND d.unit = ?",
            (*owner.key, unit.value),
        ).fetchall()
        return tuple(
            reservation
            for row in rows
            if (reservation := self._typed_budget_reservation_locked(str(row[0]))) is not None
        )

    def _typed_budget_insert_reservation_locked(self, reservation: TypedBudgetReservation) -> None:
        dispatch = reservation.dispatch
        self._connection.execute(
            "INSERT INTO typed_budget_dispatches(reservation_id, namespace, task_id, run_id, "
            "dispatch_identity, request_instance, unit, amount, currency, price_version, state, "
            "settled_actual, note, created_at, settled_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                reservation.reservation_id,
                reservation.namespace,
                dispatch.task_id,
                dispatch.run_id,
                dispatch.dispatch_identity,
                dispatch.request_instance,
                dispatch.unit.value,
                dispatch.amount,
                dispatch.currency,
                dispatch.price_version,
                reservation.state.value,
                reservation.settled_actual,
                reservation.note,
                reservation.created_at,
                reservation.settled_at,
            ),
        )
        self._insert_typed_budget_coverage_locked(reservation)

    def _insert_typed_budget_coverage_locked(self, reservation: TypedBudgetReservation) -> None:
        for item in reservation.coverage:
            self._connection.execute(
                "INSERT INTO typed_budget_coverage(reservation_id, owner_kind, owner_namespace, owner_id, "
                "policy_digest, limit_amount, mode, currency, price_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    reservation.reservation_id,
                    item.owner.kind.value,
                    item.owner.namespace,
                    item.owner.owner_id,
                    item.policy_digest,
                    item.limit,
                    None if item.mode is None else item.mode.value,
                    item.currency,
                    item.price_version,
                ),
            )

    def _typed_budget_update_reservation_locked(self, reservation: TypedBudgetReservation) -> None:
        self._connection.execute(
            "UPDATE typed_budget_dispatches SET state = ?, settled_actual = ?, note = ?, settled_at = ? "
            "WHERE reservation_id = ?",
            (
                reservation.state.value,
                reservation.settled_actual,
                reservation.note,
                reservation.settled_at,
                reservation.reservation_id,
            ),
        )


class MemoryTypedBudgetMixin(TypedBudgetStoreMixin):
    """In-memory implementation with the same scope derivation and transaction boundary."""

    def initialize_typed_budget_schema(self, *, enabled: bool = False) -> None:
        if type(enabled) is not bool:
            raise ContractError("enable_typed_budgets must be a bool")
        self._typed_budget_enabled = enabled
        self._typed_budget_policies: dict[tuple[str, str, str], BudgetPolicySnapshot] = {}
        self._typed_budget_policy_versions: dict[
            tuple[str, str, str], dict[str, BudgetPolicySnapshot]
        ] = {}
        self._typed_budget_policy_changes: dict[
            tuple[str, str, str], dict[str, BudgetPolicyChangeRecord]
        ] = {}
        self._typed_budget_reservations: dict[str, TypedBudgetReservation] = {}
        self._typed_budget_identities: dict[tuple[str, str], str] = {}

    def _typed_budget_child_of_task_locked(self, child_task_id: str) -> Any | None:
        return next(
            (item for item in self._children.values() if item.child_task_id == child_task_id), None
        )

    def _put_budget_policy_sync(self, policy: BudgetPolicySnapshot) -> BudgetPolicySnapshot:
        self._require_typed_budget_store()
        with self._lock:
            existing = self._typed_budget_policies.get(policy.owner.key)
            if existing is not None:
                if existing.digest == policy.digest:
                    return existing
                raise ContractError(
                    f"budget policy for {policy.owner.key!r} is immutable; use update_budget_policy"
                )
            self._typed_budget_policies[policy.owner.key] = policy
            self._typed_budget_policy_versions.setdefault(policy.owner.key, {})[policy.digest] = policy
            return policy

    def _update_budget_policy_sync(
        self,
        policy: BudgetPolicySnapshot,
        *,
        expected_policy_digest: str,
        actor: str,
        idempotency_key: str,
        authorization_ref: str,
        reason: str,
        now: float,
    ) -> BudgetPolicyChangeRecord:
        self._require_typed_budget_store()
        with self._lock:
            changes = self._typed_budget_policy_changes.setdefault(policy.owner.key, {})
            prior = changes.get(idempotency_key)
            if prior is not None:
                if (
                    prior.previous_digest == expected_policy_digest
                    and prior.policy_digest == policy.digest
                    and prior.policy_version == policy.policy_version
                    and prior.actor == actor
                    and prior.authorization_ref == authorization_ref
                    and prior.reason == reason
                ):
                    return prior
                raise ContractError("budget policy update idempotency key conflicts with a prior change")

            current = self._typed_budget_policies.get(policy.owner.key)
            if current is None:
                raise ContractError("cannot update a budget policy that has not been installed")
            if current.digest != expected_policy_digest:
                raise ContractError(
                    f"expected policy digest {expected_policy_digest!r}, found {current.digest!r}"
                )
            self._validate_budget_policy_transition_locked(current, policy)
            versions = self._typed_budget_policy_versions.setdefault(policy.owner.key, {})
            if any(item.policy_version == policy.policy_version for item in versions.values()):
                raise ContractError("budget policy version is immutable and already exists")
            change = BudgetPolicyChangeRecord(
                owner=policy.owner,
                previous_digest=current.digest,
                policy_digest=policy.digest,
                policy_version=policy.policy_version,
                actor=actor,
                idempotency_key=idempotency_key,
                authorization_ref=authorization_ref,
                reason=reason,
                changed_at=now,
            )
            self._typed_budget_policy_versions[policy.owner.key][policy.digest] = policy
            self._typed_budget_policy_changes[policy.owner.key][idempotency_key] = change
            self._typed_budget_policies[policy.owner.key] = policy
            return change

    def _read_budget_policy_sync(self, owner: BudgetOwnerRef) -> BudgetPolicySnapshot | None:
        self._require_typed_budget_store()
        with self._lock:
            return self._typed_budget_policies.get(owner.key)

    def _read_budget_policy_version_sync(
        self, owner: BudgetOwnerRef, policy_digest: str
    ) -> BudgetPolicySnapshot | None:
        self._require_typed_budget_store()
        with self._lock:
            return self._typed_budget_policy_versions.get(owner.key, {}).get(policy_digest)

    def _read_budget_policy_changes_sync(
        self, owner: BudgetOwnerRef
    ) -> tuple[BudgetPolicyChangeRecord, ...]:
        self._require_typed_budget_store()
        with self._lock:
            return tuple(self._typed_budget_policy_changes.get(owner.key, {}).values())

    def _read_budget_policies_for_task_sync(
        self, task_id: str, run_id: str
    ) -> tuple[BudgetPolicySnapshot, ...]:
        self._require_typed_budget_store()
        with self._lock:
            _namespace, scopes = self._derive_budget_scopes_locked(
                _scope_probe(task_id, run_id)
            )
            return tuple(
                policy
                for owner, _session_id in scopes
                if (policy := self._typed_budget_policies.get(owner.key)) is not None
            )

    def _reserve_typed_budget_dispatch_sync(
        self, dispatch: TypedBudgetDispatch, *, claim: Any | None, now: float | None
    ) -> TypedBudgetReservation:
        with self._lock:
            return self._reserve_typed_budget_locked(dispatch, claim=claim, now=now)

    def _reserve_typed_budget_dispatches_sync(
        self,
        dispatches: Sequence[TypedBudgetDispatch],
        *,
        claim: Any | None,
        now: float | None,
        expected_policy_digests: Mapping[tuple[str, str, str], str] | None = None,
    ) -> tuple[TypedBudgetReservation, ...]:
        with self._lock:
            before_reservations = dict(self._typed_budget_reservations)
            before_identities = dict(self._typed_budget_identities)
            try:
                self._assert_budget_policy_digests_locked(
                    dispatches[0], expected_policy_digests
                )
                existing = [
                    self._typed_budget_reservation_by_identity_locked(
                        self._typed_budget_task_namespace_locked(item.task_id), item
                    )
                    for item in dispatches
                ]
                if any(item is not None for item in existing) and not all(
                    item is not None for item in existing
                ):
                    raise ContractError("typed dispatch batch has only partial persisted coverage")
                return tuple(
                    self._reserve_typed_budget_locked(item, claim=claim, now=now)
                    for item in dispatches
                )
            except BaseException:
                self._typed_budget_reservations = before_reservations
                self._typed_budget_identities = before_identities
                raise

    def _record_typed_budget_usage_sync(
        self,
        reservation_id: str,
        *,
        report_id: str,
        unit: BudgetUnit,
        actual: int,
        currency: str,
        price_version: str,
        now: float | None,
    ) -> TypedBudgetReservation:
        self._require_typed_budget_store()
        with self._lock:
            source = self._typed_budget_reservation_locked(reservation_id)
            if source is None:
                raise ContractError(f"no typed budget reservation {reservation_id!r}")
            report_key = hashlib.sha256(
                _json(
                    {
                        "source_reservation_id": reservation_id,
                        "report_id": report_id,
                        "unit": unit.value,
                    }
                ).encode("utf-8")
            ).hexdigest()
            dispatch = TypedBudgetDispatch(
                task_id=source.dispatch.task_id,
                run_id=source.dispatch.run_id,
                dispatch_identity=f"usage-report:{report_key}",
                request_instance=source.dispatch.request_instance,
                unit=unit,
                amount=actual,
                currency=currency,
                price_version=price_version,
            )
            existing = self._typed_budget_reservation_by_identity_locked(
                source.namespace, dispatch
            )
            if existing is not None:
                if existing.dispatch != dispatch:
                    raise ContractError(
                        "a typed usage report identity was replayed with different unit or amount"
                    )
                if existing.state is ReservationState.SETTLED:
                    return existing
                if existing.state is ReservationState.RELEASED:
                    raise ContractError("a released typed usage report cannot become actual usage")
                return self._settle_typed_budget_locked(
                    existing.reservation_id,
                    actual=actual,
                    unknown="",
                    release=False,
                    note=_usage_report_note(report_id),
                    now=now,
                )
            reservation = self._reserve_typed_budget_locked(
                dispatch, claim=None, now=now, enforce_limits=False
            )
            return self._settle_typed_budget_locked(
                reservation.reservation_id,
                actual=actual,
                unknown="",
                release=False,
                note=_usage_report_note(report_id),
                now=now,
            )

    def _settle_typed_budget_reservation_sync(
        self,
        reservation_id: str,
        *,
        actual: int | None,
        unknown: str,
        release: bool,
        note: str,
        now: float | None,
    ) -> TypedBudgetReservation:
        with self._lock:
            return self._settle_typed_budget_locked(
                reservation_id,
                actual=actual,
                unknown=unknown,
                release=release,
                note=note,
                now=now,
            )

    def _read_typed_budget_reservation_sync(
        self, reservation_id: str
    ) -> TypedBudgetReservation | None:
        self._require_typed_budget_store()
        with self._lock:
            return self._typed_budget_reservation_locked(reservation_id)

    def _read_typed_budget_reservations_sync(
        self,
        task_id: str,
        *,
        run_id: str | None,
        state: ReservationState | None,
    ) -> tuple[TypedBudgetReservation, ...]:
        self._require_typed_budget_store()
        with self._lock:
            return tuple(
                reservation
                for reservation in self._typed_budget_reservations.values()
                if reservation.dispatch.task_id == task_id
                and (run_id is None or reservation.dispatch.run_id == run_id)
                and (state is None or reservation.state is state)
            )

    def _read_typed_budget_reservations_for_owner_sync(
        self,
        owner: BudgetOwnerRef,
        *,
        unit: BudgetUnit | None,
        limit: int,
    ) -> tuple[TypedBudgetReservation, ...]:
        self._require_typed_budget_store()
        with self._lock:
            rows = sorted(
                (
                    reservation
                    for reservation in self._typed_budget_reservations.values()
                    if (unit is None or reservation.dispatch.unit is unit)
                    and any(item.owner == owner for item in reservation.coverage)
                ),
                key=lambda item: (item.created_at, item.reservation_id),
            )
            return tuple(rows[:limit])

    def _migrate_legacy_budget_reservations_sync(
        self, root_task_id: str
    ) -> LegacyBudgetMigrationReport:
        self._require_typed_budget_store()
        with self._lock:
            root = self._typed_budget_task_locked(root_task_id)
            if (
                root is None
                or root.parent_task_id
                or (root.root_task_id or root.task_id) != root_task_id
            ):
                raise ContractError("legacy budget migration requires an existing root Task")
            namespace = self._typed_budget_task_namespace_locked(root_task_id)
            sources = sorted(
                (item for item in self._budgets.values() if item.root_task_id == root_task_id),
                key=lambda item: (item.created_at, item.reservation_id),
            )
            reservations_before = dict(self._typed_budget_reservations)
            identities_before = dict(self._typed_budget_identities)
            try:
                entries = tuple(
                    self._legacy_budget_import_locked(root_task_id, namespace, source)
                    for source in sources
                )
            except Exception:
                self._typed_budget_reservations = reservations_before
                self._typed_budget_identities = identities_before
                raise
            return LegacyBudgetMigrationReport(root_task_id=root_task_id, entries=entries)

    def _read_typed_budget_usage_sync(
        self, owner: BudgetOwnerRef, unit: BudgetUnit
    ) -> TypedBudgetUsage:
        self._require_typed_budget_store()
        with self._lock:
            return self._typed_budget_usage_locked(owner, unit)

    def _typed_budget_policy_locked(self, owner: BudgetOwnerRef) -> BudgetPolicySnapshot | None:
        return self._typed_budget_policies.get(owner.key)

    def _typed_budget_reservation_by_identity_locked(
        self, namespace: str, dispatch: TypedBudgetDispatch
    ) -> TypedBudgetReservation | None:
        reservation_id = self._typed_budget_identities.get(
            (namespace, dispatch.dispatch_identity)
        )
        return None if reservation_id is None else self._typed_budget_reservation_locked(reservation_id)

    def _typed_budget_reservation_locked(self, reservation_id: str) -> TypedBudgetReservation | None:
        return self._typed_budget_reservations.get(reservation_id)

    def _typed_budget_reservations_for_owner_locked(
        self, owner: BudgetOwnerRef, unit: BudgetUnit
    ) -> Iterable[TypedBudgetReservation]:
        return tuple(
            reservation
            for reservation in self._typed_budget_reservations.values()
            if reservation.dispatch.unit is unit
            and any(item.owner == owner for item in reservation.coverage)
        )

    def _typed_budget_insert_reservation_locked(self, reservation: TypedBudgetReservation) -> None:
        dispatch = reservation.dispatch
        identity = (reservation.namespace, dispatch.dispatch_identity)
        if reservation.reservation_id in self._typed_budget_reservations or identity in self._typed_budget_identities:
            raise ContractError("typed budget reservation identity already exists")
        self._typed_budget_reservations[reservation.reservation_id] = reservation
        self._typed_budget_identities[identity] = reservation.reservation_id

    def _typed_budget_rollback_insert_locked(self, reservation_id: str) -> None:
        reservation = self._typed_budget_reservations.pop(reservation_id, None)
        if reservation is None:
            return
        identity = (
            reservation.namespace,
            reservation.dispatch.dispatch_identity,
        )
        self._typed_budget_identities.pop(identity, None)

    def _typed_budget_update_reservation_locked(self, reservation: TypedBudgetReservation) -> None:
        if reservation.reservation_id not in self._typed_budget_reservations:
            raise ContractError(f"no typed budget reservation {reservation.reservation_id!r}")
        self._typed_budget_reservations[reservation.reservation_id] = reservation
