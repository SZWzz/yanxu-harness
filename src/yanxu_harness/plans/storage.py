"""Shared Plan control operations plus SQLite and memory backend mixins."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import sqlite3
import time
from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping, Sequence

from ..errors import ContractError
from .codec import (
    canonical_json_bytes,
    definition_digest,
    execution_envelope_digest,
    plan_definition_from_json,
    plan_definition_to_record,
)
from .decisions import transition_plan
from .types import (
    ApprovalDecision,
    ApprovalRevocation,
    PlanApproval,
    PlanApprovalRequest,
    PlanDefinition,
    PlanEvent,
    PlanOutboxEntry,
    PlanRecord,
    PlanRefused,
    PlanSnapshot,
    PlanStepCandidate,
    PlanStepClaim,
    PlanStepPage,
    PlanStatus,
    StepDefinition,
    StepTaskBinding,
)

PLAN_CONTROL_SCHEMA_VERSION = 1

_PLAN_TABLES = frozenset(
    {
        "plan_records",
        "plan_revisions",
        "plan_approvals",
        "plan_approval_revocations",
        "plan_events",
        "plan_outbox",
        "plan_requests",
        "plan_bindings",
    }
)

_PLAN_SCHEMA = """
CREATE TABLE IF NOT EXISTS plan_records (
    namespace TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    current_revision INTEGER NOT NULL,
    control_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    definition_digest TEXT NOT NULL,
    active_approval_id TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    schema_version INTEGER NOT NULL,
    PRIMARY KEY (namespace, plan_id)
);
CREATE TABLE IF NOT EXISTS plan_revisions (
    namespace TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    definition_digest TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (namespace, plan_id, revision)
);
CREATE TABLE IF NOT EXISTS plan_approvals (
    namespace TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    decision TEXT NOT NULL,
    record_json TEXT NOT NULL,
    PRIMARY KEY (namespace, plan_id, decision_id)
);
CREATE INDEX IF NOT EXISTS plan_approvals_revision
    ON plan_approvals(namespace, plan_id, revision, decision_id);
CREATE TABLE IF NOT EXISTS plan_approval_revocations (
    namespace TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    revoked_at_ms INTEGER NOT NULL,
    record_json TEXT NOT NULL,
    PRIMARY KEY (namespace, plan_id, decision_id)
);
CREATE TABLE IF NOT EXISTS plan_events (
    event_id TEXT PRIMARY KEY,
    namespace TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    control_version INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE (namespace, plan_id, control_version)
);
CREATE INDEX IF NOT EXISTS plan_events_page
    ON plan_events(namespace, plan_id, control_version);
CREATE TABLE IF NOT EXISTS plan_outbox (
    outbox_id TEXT PRIMARY KEY,
    namespace TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    delivered_at REAL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS plan_outbox_pending
    ON plan_outbox(delivered_at, created_at, outbox_id);
CREATE TABLE IF NOT EXISTS plan_requests (
    namespace TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (namespace, plan_id, request_id)
);
CREATE TABLE IF NOT EXISTS plan_bindings (
    binding_id TEXT PRIMARY KEY,
    namespace TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    step_id TEXT NOT NULL,
    task_id TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL UNIQUE,
    binding_status TEXT NOT NULL,
    record_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE (namespace, plan_id, revision, step_id, binding_id)
);
CREATE INDEX IF NOT EXISTS plan_bindings_step
    ON plan_bindings(namespace, plan_id, revision, step_id, binding_status);
"""


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _encode_ready_cursor(position: tuple[str, str, int, str]) -> str:
    payload = json.dumps(list(position), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _binding_from_record(record: Mapping[str, Any]) -> StepTaskBinding:
    try:
        if type(record.get("revision")) is not int:
            raise TypeError("revision must be an integer")
        return StepTaskBinding(
            binding_id=record["binding_id"],
            namespace=record["namespace"],
            plan_id=record["plan_id"],
            revision=record["revision"],
            step_id=record["step_id"],
            task_id=record["task_id"],
            run_id=record["run_id"],
            initial_input_digest=record["initial_input_digest"],
            request_id=record["request_id"],
            request_digest=record["request_digest"],
            binding_status=record["binding_status"],
            ready_since=float(record["ready_since"]),
            created_at=float(record["created_at"]),
            accepted_at=None if record.get("accepted_at") is None else float(record["accepted_at"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PlanRefused("store_unsupported", f"stored step binding cannot be read: {exc}") from exc


def _record_from_json(payload: str) -> PlanRecord:
    try:
        record = json.loads(payload)
        for name in ("current_revision", "control_version", "schema_version"):
            if type(record.get(name)) is not int:
                raise TypeError(f"{name} must be an integer")
        return PlanRecord(
            namespace=record["namespace"],
            session_id=record["session_id"],
            plan_id=record["plan_id"],
            current_revision=record["current_revision"],
            control_version=record["control_version"],
            status=record["status"],
            definition_digest=record["definition_digest"],
            active_approval_id=record.get("active_approval_id", ""),
            reason=record.get("reason", ""),
            created_at=float(record["created_at"]),
            updated_at=float(record["updated_at"]),
            schema_version=record["schema_version"],
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PlanRefused("store_unsupported", f"stored PlanRecord cannot be read: {exc}") from exc


def _approval_from_json(payload: str) -> PlanApproval:
    record = json.loads(payload)
    for name in ("revision", "decided_at_ms", "schema_version"):
        if type(record.get(name)) is not int:
            raise PlanRefused("store_unsupported", f"stored approval {name} must be an integer")
    if record.get("expires_at_ms") is not None and type(record.get("expires_at_ms")) is not int:
        raise PlanRefused("store_unsupported", "stored approval expires_at_ms must be an integer")
    return PlanApproval(
        namespace=record["namespace"],
        plan_id=record["plan_id"],
        revision=record["revision"],
        definition_digest=record["definition_digest"],
        execution_envelope_digest=record["execution_envelope_digest"],
        approval_policy_version=record["approval_policy_version"],
        decision_id=record["decision_id"],
        decision=record["decision"],
        decided_by=record["decided_by"],
        decided_at_ms=record["decided_at_ms"],
        expires_at_ms=record.get("expires_at_ms"),
        reason=record.get("reason", ""),
        schema_version=record["schema_version"],
    )


def _revocation_from_json(payload: str) -> ApprovalRevocation:
    record = json.loads(payload)
    return ApprovalRevocation(
        namespace=record["namespace"],
        plan_id=record["plan_id"],
        decision_id=record["decision_id"],
        revoked_by=record["revoked_by"],
        revoked_at_ms=int(record["revoked_at_ms"]),
        reason=record.get("reason", ""),
    )


def _event_from_row(row: Sequence[Any]) -> PlanEvent:
    return PlanEvent(
        event_id=str(row[0]),
        namespace=str(row[1]),
        plan_id=str(row[2]),
        control_version=int(row[3]),
        event_type=str(row[4]),
        payload=json.loads(row[5]),
        created_at=float(row[6]),
    )


def _outbox_from_row(row: Sequence[Any]) -> PlanOutboxEntry:
    return PlanOutboxEntry(
        outbox_id=str(row[0]),
        namespace=str(row[1]),
        plan_id=str(row[2]),
        event_id=str(row[3]),
        kind=str(row[4]),
        payload=json.loads(row[5]),
        created_at=float(row[6]),
        delivered_at=None if row[7] is None else float(row[7]),
        attempts=int(row[8]),
        last_error=str(row[9]),
    )


def _validate_request(request_id: str, principal: str) -> None:
    if not isinstance(request_id, str) or not request_id.strip():
        raise PlanRefused("invalid_definition", "request_id must be non-empty")
    if not isinstance(principal, str) or not principal.strip():
        raise PlanRefused("invalid_definition", "trusted principal must be non-empty")


def _stamp(now: float | None) -> float:
    value = time.time() if now is None else now
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or value < 0:
        raise PlanRefused("invalid_definition", "control timestamp must be finite and non-negative")
    return float(value)


def _stamp_ms(now_ms: int | None) -> int:
    if now_ms is None:
        return int(time.time() * 1000)
    if type(now_ms) is not int or now_ms < 0:
        raise PlanRefused("invalid_definition", "control timestamp must be non-negative integer milliseconds")
    return now_ms


def _request_digest(
    operation: str,
    namespace: str,
    plan_id: str,
    request_id: str,
    principal: str,
    expected_control_version: int | None,
    payload: Mapping[str, Any],
) -> str:
    encoded = canonical_json_bytes(
        {
            "operation": operation,
            "namespace": namespace,
            "plan_id": plan_id,
            "request_id": request_id,
            "principal": principal,
            "expected_control_version": expected_control_version,
            "payload": payload,
        }
    )
    return hashlib.sha256(b"yanxu-plan-request:v1\0" + encoded).hexdigest()


def _event_id(namespace: str, plan_id: str, control_version: int) -> str:
    encoded = canonical_json_bytes(
        {"namespace": namespace, "plan_id": plan_id, "control_version": control_version}
    )
    return "pev_" + hashlib.sha256(b"yanxu-plan-event:v1\0" + encoded).hexdigest()


@dataclass(frozen=True)
class _PlanMutation:
    record: PlanRecord
    event_type: str = ""
    event_payload: Mapping[str, Any] | None = None
    revision: PlanDefinition | None = None
    approval: PlanApproval | None = None
    revocation: ApprovalRevocation | None = None


class OrchestrationMixin:
    """Backend-neutral atomic Plan operations; subclasses provide locked storage primitives."""

    async def _plan_call(self, function: Callable[..., Any], *args: Any) -> Any:
        raise NotImplementedError

    def _plan_transaction(self, work: Callable[[], Any]) -> Any:
        raise NotImplementedError

    def _plan_read(self, work: Callable[[], Any]) -> Any:
        raise NotImplementedError

    def _plan_require_schema(self) -> None:
        raise NotImplementedError

    def _plan_get_record_locked(self, namespace: str, plan_id: str) -> PlanRecord | None:
        raise NotImplementedError

    def _plan_insert_record_locked(self, record: PlanRecord) -> None:
        raise NotImplementedError

    def _plan_update_record_locked(self, record: PlanRecord) -> None:
        raise NotImplementedError

    def _plan_get_revision_locked(self, namespace: str, plan_id: str, revision: int) -> PlanDefinition | None:
        raise NotImplementedError

    def _plan_insert_revision_locked(self, definition: PlanDefinition, created_at: float) -> None:
        raise NotImplementedError

    def _plan_get_approval_locked(self, namespace: str, plan_id: str, decision_id: str) -> PlanApproval | None:
        raise NotImplementedError

    def _plan_insert_approval_locked(self, approval: PlanApproval) -> None:
        raise NotImplementedError

    def _plan_get_revocation_locked(self, namespace: str, plan_id: str, decision_id: str) -> ApprovalRevocation | None:
        raise NotImplementedError

    def _plan_insert_revocation_locked(self, revocation: ApprovalRevocation) -> None:
        raise NotImplementedError

    def _plan_approvals_locked(self, namespace: str, plan_id: str) -> tuple[PlanApproval, ...]:
        raise NotImplementedError

    def _plan_revocations_locked(self, namespace: str, plan_id: str) -> tuple[ApprovalRevocation, ...]:
        raise NotImplementedError

    def _plan_append_event_locked(self, event: PlanEvent) -> None:
        raise NotImplementedError

    def _plan_read_events_locked(
        self, namespace: str, plan_id: str, after_control_version: int, limit: int
    ) -> tuple[PlanEvent, ...]:
        raise NotImplementedError

    def _plan_has_active_bindings_locked(self, namespace: str, plan_id: str) -> bool:
        raise NotImplementedError

    def _plan_request_locked(
        self, namespace: str, plan_id: str, request_id: str
    ) -> tuple[str, PlanRecord] | None:
        raise NotImplementedError

    def _plan_save_request_locked(
        self, namespace: str, plan_id: str, request_id: str, request_digest: str,
        record: PlanRecord, created_at: float,
    ) -> None:
        raise NotImplementedError

    def _plan_read_outbox_locked(self, pending_only: bool, limit: int) -> tuple[PlanOutboxEntry, ...]:
        raise NotImplementedError

    def _plan_deliver_outbox_locked(self, outbox_id: str, at: float) -> bool:
        raise NotImplementedError

    def _plan_fail_outbox_locked(self, outbox_id: str, error: str) -> bool:
        raise NotImplementedError

    def _plan_next_record_key_locked(
        self, namespace: str, after_plan_id: str = ""
    ) -> tuple[str, str] | None:
        raise NotImplementedError

    def _plan_binding_for_step_locked(
        self, namespace: str, plan_id: str, revision: int, step_id: str
    ) -> StepTaskBinding | None:
        raise NotImplementedError

    def _plan_binding_for_task_locked(self, task_id: str) -> StepTaskBinding | None:
        raise NotImplementedError

    def _plan_bindings_locked(
        self, namespace: str, plan_id: str, revision: int | None = None
    ) -> tuple[StepTaskBinding, ...]:
        raise NotImplementedError

    def _plan_save_binding_locked(self, binding: StepTaskBinding) -> None:
        raise NotImplementedError

    def _plan_task_get_locked(self, task_id: str) -> Any:
        raise NotImplementedError

    def _plan_task_initial_digest_locked(self, task_id: str) -> str | None:
        raise NotImplementedError

    def _plan_task_is_schedulable_locked(self, task_id: str, now: float) -> bool:
        raise NotImplementedError

    def _plan_task_admit_locked(self, admission: Any, now: float) -> Any:
        raise NotImplementedError

    def _plan_task_check_admission_locked(self, namespace: str, initial_input: Any) -> None:
        raise NotImplementedError

    def _plan_task_claim_locked(
        self, task_id: str, *, holder: str, ttl_seconds: float, now: float
    ) -> Any:
        raise NotImplementedError

    def _plan_task_claim_for_holder_locked(self, task_id: str, holder: str) -> Any:
        raise NotImplementedError

    async def create_plan(
        self,
        definition: PlanDefinition,
        *,
        request_id: str,
        principal: str,
        now: float | None = None,
    ) -> PlanRecord:
        return await self._plan_call(self._create_plan_sync, definition, request_id, principal, now)

    async def read_plan(self, namespace: str, plan_id: str) -> PlanSnapshot | None:
        return await self._plan_call(self._read_plan_sync, namespace, plan_id)

    async def read_plan_revision(
        self, namespace: str, plan_id: str, revision: int
    ) -> PlanDefinition | None:
        return await self._plan_call(self._read_plan_revision_sync, namespace, plan_id, revision)

    async def read_ready_steps(
        self,
        namespace: str,
        *,
        limit: int = 64,
        cursor: str | None = None,
        now: float | None = None,
    ) -> PlanStepPage:
        """Read a bounded page of advisory ready-step candidates.

        The scan limit is bounded separately from the result limit. A cursor advances past blocked
        steps too, so a dense blocked Plan cannot hide a later ready Plan indefinitely.
        """
        if type(limit) is not int or limit <= 0:
            raise ContractError("read_ready_steps needs a positive limit")
        if not isinstance(namespace, str) or not namespace.strip():
            raise ContractError("read_ready_steps needs a non-empty namespace")
        if cursor is not None and not isinstance(cursor, str):
            raise ContractError("ready-step cursor must be a string or None")
        return await self._plan_call(
            self._read_ready_steps_sync, namespace, limit, cursor, _stamp(now)
        )

    async def claim_plan_step(
        self,
        candidate: PlanStepCandidate,
        *,
        expected_control_version: int,
        request_id: str,
        principal: str,
        holder: str,
        ttl_seconds: float,
        now: float | None = None,
    ) -> PlanStepClaim:
        """Atomically validate a ready step, bind its Task/Run and take the execution fence."""
        if not isinstance(candidate, PlanStepCandidate):
            raise ContractError("claim_plan_step requires a PlanStepCandidate")
        for name, value in (("request_id", request_id), ("principal", principal), ("holder", holder)):
            if not isinstance(value, str) or not value.strip():
                raise ContractError(f"claim_plan_step requires a non-empty {name}")
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, (int, float)) or ttl_seconds <= 0:
            raise ContractError("claim_plan_step ttl_seconds must be positive")
        if type(expected_control_version) is not int or expected_control_version < 1:
            raise ContractError("expected_control_version must be positive")
        return await self._plan_call(
            self._claim_plan_step_sync,
            candidate,
            expected_control_version,
            request_id,
            principal,
            holder,
            float(ttl_seconds),
            _stamp(now),
        )

    async def request_plan_review(
        self,
        namespace: str,
        plan_id: str,
        *,
        expected_control_version: int,
        request_id: str,
        principal: str,
        now: float | None = None,
    ) -> PlanRecord:
        stamp = _stamp(now)

        def mutate(record: PlanRecord, _definition: PlanDefinition) -> _PlanMutation:
            target = transition_plan(record.status, PlanStatus.REVIEWING)
            moved = self._advance_record(record, target, now=stamp)
            return _PlanMutation(
                moved,
                "review_requested",
                {"principal": principal, "revision": record.current_revision},
            )

        return await self._mutate(
            "request_plan_review", namespace, plan_id, expected_control_version, request_id,
            principal, {"revision": "current"}, stamp, mutate,
        )

    async def decide_plan_approval(
        self,
        namespace: str,
        plan_id: str,
        decision: PlanApprovalRequest,
        *,
        expected_control_version: int,
        request_id: str,
        principal: str,
        now_ms: int | None = None,
    ) -> PlanRecord:
        if not isinstance(decision, PlanApprovalRequest):
            raise ContractError("decision must be a PlanApprovalRequest")
        decided_at_ms = _stamp_ms(now_ms)
        approval_payload = {
            "decision_id": decision.decision_id,
            "decision": decision.decision.value,
            "approval_policy_version": decision.approval_policy_version,
            "expires_at_ms": decision.expires_at_ms,
            "reason": decision.reason,
        }

        def mutate(record: PlanRecord, definition: PlanDefinition) -> _PlanMutation:
            existing = self._plan_get_approval_locked(namespace, plan_id, decision.decision_id)
            if existing is not None:
                same = (
                    existing.revision == record.current_revision
                    and existing.definition_digest == definition_digest(definition)
                    and existing.execution_envelope_digest == execution_envelope_digest(definition.execution_envelope)
                    and existing.approval_policy_version == decision.approval_policy_version
                    and existing.decision is decision.decision
                    and existing.decided_by == principal
                    and existing.expires_at_ms == decision.expires_at_ms
                    and existing.reason == decision.reason
                )
                if not same:
                    raise PlanRefused("content_conflict", "decision_id was already used for different approval content")
                return _PlanMutation(record)
            if record.status not in {PlanStatus.REVIEWING, PlanStatus.PAUSED}:
                raise PlanRefused("invalid_transition", "approval decisions require reviewing or paused status")
            if decision.decision is ApprovalDecision.APPROVED and (
                decision.expires_at_ms is not None and decision.expires_at_ms <= decided_at_ms
            ):
                raise PlanRefused("approval_expired", "an approval must expire in the future")
            if record.status is PlanStatus.REVIEWING:
                target = (
                    PlanStatus.APPROVED
                    if decision.decision is ApprovalDecision.APPROVED
                    else PlanStatus.DRAFT
                )
            else:
                target = PlanStatus.PAUSED
            approval = PlanApproval(
                namespace=namespace,
                plan_id=plan_id,
                revision=record.current_revision,
                definition_digest=definition_digest(definition),
                execution_envelope_digest=execution_envelope_digest(definition.execution_envelope),
                approval_policy_version=decision.approval_policy_version,
                decision_id=decision.decision_id,
                decision=decision.decision,
                decided_by=principal,
                decided_at_ms=decided_at_ms,
                expires_at_ms=decision.expires_at_ms,
                reason=decision.reason,
            )
            moved = self._advance_record(
                record,
                target,
                now=decided_at_ms / 1000,
                active_approval_id=(
                    decision.decision_id
                    if decision.decision is ApprovalDecision.APPROVED
                    else ""
                ),
                reason=decision.reason,
            )
            return _PlanMutation(
                moved,
                "approval_decided",
                {"decision_id": decision.decision_id, "decision": decision.decision.value, "principal": principal},
                approval=approval,
            )

        return await self._mutate(
            "decide_plan_approval", namespace, plan_id, expected_control_version,
            request_id, principal, approval_payload, decided_at_ms / 1000, mutate,
        )

    async def revoke_plan_approval(
        self,
        namespace: str,
        plan_id: str,
        decision_id: str,
        *,
        reason: str,
        expected_control_version: int,
        request_id: str,
        principal: str,
        now_ms: int | None = None,
    ) -> PlanRecord:
        if not isinstance(reason, str) or not reason.strip():
            raise PlanRefused("invalid_definition", "revocation reason must be non-empty")
        revoked_at_ms = _stamp_ms(now_ms)
        payload = {"decision_id": decision_id, "reason": reason}

        def mutate(record: PlanRecord, _definition: PlanDefinition) -> _PlanMutation:
            existing = self._plan_get_revocation_locked(namespace, plan_id, decision_id)
            if existing is not None:
                if existing.revoked_by != principal or existing.reason != reason:
                    raise PlanRefused("content_conflict", "approval revocation already has different content")
                return _PlanMutation(record)
            approval = self._plan_get_approval_locked(namespace, plan_id, decision_id)
            if approval is None or approval.decision is not ApprovalDecision.APPROVED:
                raise PlanRefused("approval_required", "there is no approved decision to revoke")
            if decision_id != record.active_approval_id or approval.revision != record.current_revision:
                raise PlanRefused("approval_revoked", "only the current revision's active approval can be revoked")
            if record.status not in {PlanStatus.APPROVED, PlanStatus.EXECUTING, PlanStatus.PAUSED}:
                raise PlanRefused("invalid_transition", "approval cannot be revoked in this Plan state")
            revocation = ApprovalRevocation(
                namespace=namespace,
                plan_id=plan_id,
                decision_id=decision_id,
                revoked_by=principal,
                revoked_at_ms=revoked_at_ms,
                reason=reason,
            )
            target = PlanStatus.PAUSED
            moved = self._advance_record(
                record,
                target,
                now=revoked_at_ms / 1000,
                active_approval_id=decision_id,
                reason=reason,
            )
            return _PlanMutation(
                moved,
                "approval_revoked",
                {"decision_id": decision_id, "principal": principal, "reason": reason},
                revocation=revocation,
            )

        return await self._mutate(
            "revoke_plan_approval", namespace, plan_id, expected_control_version,
            request_id, principal, payload, revoked_at_ms / 1000, mutate,
        )

    async def pause_plan(
        self,
        namespace: str,
        plan_id: str,
        *,
        reason: str,
        expected_control_version: int,
        request_id: str,
        principal: str,
        now: float | None = None,
    ) -> PlanRecord:
        if not isinstance(reason, str) or not reason.strip():
            raise PlanRefused("invalid_definition", "pause reason must be non-empty")
        stamp = _stamp(now)

        def mutate(record: PlanRecord, _definition: PlanDefinition) -> _PlanMutation:
            target = transition_plan(record.status, PlanStatus.PAUSED)
            moved = self._advance_record(record, target, now=stamp, reason=reason)
            return _PlanMutation(
                moved,
                "pause_requested",
                {"principal": principal, "reason": reason},
            )

        return await self._mutate(
            "pause_plan", namespace, plan_id, expected_control_version, request_id,
            principal, {"reason": reason}, stamp, mutate,
        )

    async def resume_plan(
        self,
        namespace: str,
        plan_id: str,
        *,
        expected_control_version: int,
        request_id: str,
        principal: str,
        now: float | None = None,
    ) -> PlanRecord:
        stamp = _stamp(now)
        now_ms = int(stamp * 1000)

        def mutate(record: PlanRecord, definition: PlanDefinition) -> _PlanMutation:
            if record.status is not PlanStatus.PAUSED:
                raise PlanRefused("invalid_transition", "only a paused Plan can be resumed")
            approval = self._require_current_approval_locked(
                namespace, plan_id, record, definition, now_ms
            )
            target = (
                PlanStatus.EXECUTING
                if self._plan_has_active_bindings_locked(namespace, plan_id)
                else PlanStatus.APPROVED
            )
            moved = self._advance_record(
                record,
                transition_plan(record.status, target),
                now=stamp,
                active_approval_id=approval.decision_id,
                reason="",
            )
            return _PlanMutation(
                moved,
                "plan_resumed",
                {"principal": principal, "approval_id": approval.decision_id},
            )

        return await self._mutate(
            "resume_plan", namespace, plan_id, expected_control_version, request_id,
            principal, {}, stamp, mutate,
        )

    async def request_replan(
        self,
        namespace: str,
        plan_id: str,
        *,
        reason: str,
        expected_control_version: int,
        request_id: str,
        principal: str,
        now: float | None = None,
    ) -> PlanRecord:
        if not isinstance(reason, str) or not reason.strip():
            raise PlanRefused("invalid_definition", "replan reason must be non-empty")
        stamp = _stamp(now)

        def mutate(record: PlanRecord, _definition: PlanDefinition) -> _PlanMutation:
            target = transition_plan(record.status, PlanStatus.REPLANNING)
            moved = self._advance_record(record, target, now=stamp, reason=reason)
            return _PlanMutation(
                moved,
                "replan_requested",
                {"principal": principal, "revision": record.current_revision, "reason": reason},
            )

        return await self._mutate(
            "request_replan", namespace, plan_id, expected_control_version, request_id,
            principal, {"reason": reason}, stamp, mutate,
        )

    async def publish_plan_revision(
        self,
        definition: PlanDefinition,
        *,
        expected_control_version: int,
        request_id: str,
        principal: str,
        now: float | None = None,
    ) -> PlanRecord:
        if not isinstance(definition, PlanDefinition):
            raise ContractError("publish_plan_revision requires a PlanDefinition")
        stamp = _stamp(now)
        revision = replace(definition, created_at=stamp)

        def mutate(record: PlanRecord, current_definition: PlanDefinition) -> _PlanMutation:
            if revision.namespace != record.namespace or revision.session_id != record.session_id or revision.plan_id != record.plan_id:
                raise PlanRefused("namespace_mismatch", "a revision must keep the Plan identity")
            if revision.revision != record.current_revision + 1:
                raise PlanRefused("content_conflict", "a new Plan revision must increment by exactly one")
            if revision.supersedes_revision != record.current_revision or not revision.replan_reason.strip():
                raise PlanRefused("invalid_definition", "a published revision must name its superseded revision and reason")
            if record.status is not PlanStatus.REPLANNING:
                raise PlanRefused("invalid_transition", "a revision may only publish from replanning")
            if self._plan_has_active_bindings_locked(namespace=record.namespace, plan_id=record.plan_id):
                raise PlanRefused("active_binding", "the old revision has not reached quiescence")
            target = transition_plan(record.status, PlanStatus.DRAFT, quiescent=True)
            moved = replace(
                self._advance_record(record, target, now=stamp, active_approval_id="", reason=""),
                current_revision=revision.revision,
                definition_digest=definition_digest(revision),
            )
            return _PlanMutation(
                moved,
                "revision_published",
                {
                    "principal": principal,
                    "old_revision": current_definition.revision,
                    "revision": revision.revision,
                    "definition_digest": definition_digest(revision),
                },
                revision=revision,
            )

        return await self._mutate(
            "publish_plan_revision", revision.namespace, revision.plan_id,
            expected_control_version, request_id, principal,
            {"definition_digest": definition_digest(revision), "revision": revision.revision},
            stamp, mutate,
        )

    async def read_plan_events(
        self,
        namespace: str,
        plan_id: str,
        *,
        after_control_version: int = 0,
        limit: int = 100,
    ) -> Sequence[PlanEvent]:
        if type(after_control_version) is not int or after_control_version < 0:
            raise ContractError("after_control_version must be a non-negative integer")
        if type(limit) is not int or limit <= 0:
            raise ContractError("plan event limit must be positive")
        return await self._plan_call(
            self._read_events_sync, namespace, plan_id, after_control_version, limit
        )

    async def read_plan_approvals(
        self, namespace: str, plan_id: str
    ) -> tuple[tuple[PlanApproval, ...], tuple[ApprovalRevocation, ...]]:
        return await self._plan_call(self._read_approvals_sync, namespace, plan_id)

    async def read_plan_outbox(
        self, *, pending_only: bool = True, limit: int = 100
    ) -> Sequence[PlanOutboxEntry]:
        if type(limit) is not int or limit <= 0:
            raise ContractError("plan outbox limit must be positive")
        return await self._plan_call(self._read_outbox_sync, pending_only, limit)

    async def deliver_plan_outbox(self, outbox_id: str, *, at: float | None = None) -> bool:
        return await self._plan_call(self._deliver_outbox_sync, outbox_id, _stamp(at))

    async def fail_plan_outbox(self, outbox_id: str, *, error: str) -> bool:
        if not isinstance(error, str):
            raise ContractError("outbox error must be a string")
        return await self._plan_call(self._fail_outbox_sync, outbox_id, error)

    def _advance_record(
        self,
        record: PlanRecord,
        status: PlanStatus,
        *,
        now: float,
        active_approval_id: str | None = None,
        reason: str | None = None,
    ) -> PlanRecord:
        return replace(
            record,
            status=status,
            control_version=record.control_version + 1,
            active_approval_id=(record.active_approval_id if active_approval_id is None else active_approval_id),
            reason=record.reason if reason is None else reason,
            updated_at=now,
        )

    def _require_current_approval_locked(
        self,
        namespace: str,
        plan_id: str,
        record: PlanRecord,
        definition: PlanDefinition,
        now_ms: int,
    ) -> PlanApproval:
        if not record.active_approval_id:
            raise PlanRefused("approval_required", "Plan has no active approval")
        approval = self._plan_get_approval_locked(namespace, plan_id, record.active_approval_id)
        if approval is None or approval.decision is not ApprovalDecision.APPROVED:
            raise PlanRefused("approval_required", "active approval record is missing")
        if self._plan_get_revocation_locked(namespace, plan_id, approval.decision_id) is not None:
            raise PlanRefused("approval_revoked", "active approval was revoked")
        if approval.expires_at_ms is not None and approval.expires_at_ms <= now_ms:
            raise PlanRefused("approval_expired", "active approval has expired")
        if (
            approval.revision != record.current_revision
            or approval.definition_digest != definition_digest(definition)
            or approval.execution_envelope_digest != execution_envelope_digest(definition.execution_envelope)
        ):
            raise PlanRefused("content_conflict", "approval does not cover the current Plan definition")
        return approval

    async def _mutate(
        self,
        operation: str,
        namespace: str,
        plan_id: str,
        expected_control_version: int,
        request_id: str,
        principal: str,
        payload: Mapping[str, Any],
        now: float,
        transform: Callable[[PlanRecord, PlanDefinition], _PlanMutation],
    ) -> PlanRecord:
        _validate_request(request_id, principal)
        if type(expected_control_version) is not int or expected_control_version < 1:
            raise PlanRefused("version_conflict", "expected_control_version must be positive")
        request_digest = _request_digest(
            operation, namespace, plan_id, request_id, principal, expected_control_version, payload
        )

        def work() -> PlanRecord:
            self._plan_require_schema()
            existing_request = self._plan_request_locked(namespace, plan_id, request_id)
            if existing_request is not None:
                existing_digest, response = existing_request
                if existing_digest != request_digest:
                    raise PlanRefused("request_id_conflict", "request_id was reused with different content")
                return response
            current = self._plan_get_record_locked(namespace, plan_id)
            if current is None:
                raise PlanRefused("unknown_reference", f"unknown Plan {plan_id!r} in namespace {namespace!r}")
            if current.control_version != expected_control_version:
                raise PlanRefused(
                    "version_conflict",
                    f"Plan is at control_version {current.control_version}, not {expected_control_version}",
                )
            definition = self._plan_get_revision_locked(namespace, plan_id, current.current_revision)
            if definition is None:
                raise PlanRefused("store_unsupported", "current Plan revision is missing")
            if (
                definition.session_id != current.session_id
                or definition_digest(definition) != current.definition_digest
            ):
                raise PlanRefused("store_unsupported", "current Plan record and immutable revision disagree")
            mutation = transform(current, definition)
            if mutation.record.namespace != namespace or mutation.record.plan_id != plan_id:
                raise PlanRefused("namespace_mismatch", "a control action changed Plan identity")
            if mutation.record.control_version not in {
                current.control_version,
                current.control_version + 1,
            }:
                raise PlanRefused("store_unsupported", "control version must advance by at most one")
            if mutation.revision is not None:
                self._plan_insert_revision_locked(mutation.revision, now)
            if mutation.approval is not None:
                self._plan_insert_approval_locked(mutation.approval)
            if mutation.revocation is not None:
                self._plan_insert_revocation_locked(mutation.revocation)
            if mutation.record.control_version != current.control_version:
                self._plan_update_record_locked(mutation.record)
                if mutation.event_type:
                    payload_value = dict(mutation.event_payload or {})
                    event = PlanEvent(
                        event_id=_event_id(namespace, plan_id, mutation.record.control_version),
                        namespace=namespace,
                        plan_id=plan_id,
                        control_version=mutation.record.control_version,
                        event_type=mutation.event_type,
                        payload=payload_value,
                        created_at=now,
                    )
                    self._plan_append_event_locked(event)
            self._plan_save_request_locked(
                namespace, plan_id, request_id, request_digest, mutation.record, now
            )
            return mutation.record

        return await self._plan_call(self._plan_transaction, work)

    def _create_plan_sync(
        self, definition: PlanDefinition, request_id: str, principal: str, now: float | None
    ) -> PlanRecord:
        if not isinstance(definition, PlanDefinition):
            raise ContractError("create_plan requires a PlanDefinition")
        _validate_request(request_id, principal)
        stamp = _stamp(now)
        stored_definition = replace(definition, created_at=stamp)
        digest = definition_digest(stored_definition)
        request_digest = _request_digest(
            "create_plan",
            definition.namespace,
            definition.plan_id,
            request_id,
            principal,
            None,
            {"definition_digest": digest},
        )

        def work() -> PlanRecord:
            self._plan_require_schema()
            cached = self._plan_request_locked(definition.namespace, definition.plan_id, request_id)
            if cached is not None:
                existing_digest, response = cached
                if existing_digest != request_digest:
                    raise PlanRefused("request_id_conflict", "request_id was reused with different content")
                return response
            if definition.revision != 1:
                raise PlanRefused("content_conflict", "a newly created Plan starts at revision 1")
            current = self._plan_get_record_locked(definition.namespace, definition.plan_id)
            if current is not None:
                original = self._plan_get_revision_locked(definition.namespace, definition.plan_id, 1)
                if original is None or definition_digest(original) != digest:
                    raise PlanRefused("content_conflict", "Plan identity already exists with different content")
                self._plan_save_request_locked(
                    definition.namespace, definition.plan_id, request_id, request_digest, current, stamp
                )
                return current
            record = PlanRecord(
                namespace=stored_definition.namespace,
                session_id=stored_definition.session_id,
                plan_id=stored_definition.plan_id,
                current_revision=1,
                control_version=1,
                status=PlanStatus.DRAFT,
                definition_digest=digest,
                created_at=stamp,
                updated_at=stamp,
            )
            self._plan_insert_record_locked(record)
            self._plan_insert_revision_locked(stored_definition, stamp)
            event = PlanEvent(
                event_id=_event_id(record.namespace, record.plan_id, 1),
                namespace=record.namespace,
                plan_id=record.plan_id,
                control_version=1,
                event_type="created",
                payload={"principal": principal, "revision": 1, "definition_digest": digest},
                created_at=stamp,
            )
            self._plan_append_event_locked(event)
            self._plan_save_request_locked(
                record.namespace, record.plan_id, request_id, request_digest, record, stamp
            )
            return record

        return self._plan_transaction(work)

    def _read_plan_sync(self, namespace: str, plan_id: str) -> PlanSnapshot | None:
        def work() -> PlanSnapshot | None:
            self._plan_require_schema()
            record = self._plan_get_record_locked(namespace, plan_id)
            if record is None:
                return None
            definition = self._plan_get_revision_locked(namespace, plan_id, record.current_revision)
            if definition is None:
                raise PlanRefused("store_unsupported", "current Plan revision is missing")
            if (
                definition.session_id != record.session_id
                or definition_digest(definition) != record.definition_digest
            ):
                raise PlanRefused("store_unsupported", "current Plan record and immutable revision disagree")
            approvals = self._plan_approvals_locked(namespace, plan_id)
            revocations = self._plan_revocations_locked(namespace, plan_id)
            return PlanSnapshot(record, definition, approvals, revocations)

        return self._plan_snapshot_read(work)

    def _plan_snapshot_read(self, work: Callable[[], Any]) -> Any:
        return self._plan_read(work)

    def _read_plan_revision_sync(
        self, namespace: str, plan_id: str, revision: int
    ) -> PlanDefinition | None:
        if type(revision) is not int or revision < 1:
            raise ContractError("revision must be positive")

        def work() -> PlanDefinition | None:
            self._plan_require_schema()
            return self._plan_get_revision_locked(namespace, plan_id, revision)

        return self._plan_read(work)

    def _read_ready_steps_sync(
        self, namespace: str, limit: int, cursor: str | None, now: float
    ) -> PlanStepPage:
        after: tuple[str, str, int, str] | None = None
        if cursor is not None:
            try:
                decoded = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii") + b"=="))
                if (
                    not isinstance(decoded, list)
                    or len(decoded) != 4
                    or not isinstance(decoded[0], str)
                    or not isinstance(decoded[1], str)
                    or type(decoded[2]) is not int
                    or not isinstance(decoded[3], str)
                    or decoded[0] != namespace
                ):
                    raise ValueError("wrong shape")
                after = (decoded[0], decoded[1], decoded[2], decoded[3])
            except (ValueError, UnicodeEncodeError, json.JSONDecodeError) as exc:
                raise PlanRefused("invalid_cursor", "ready-step cursor is malformed") from exc

        def work() -> PlanStepPage:
            self._plan_require_schema()
            candidates: list[PlanStepCandidate] = []
            scanned = 0
            scan_limit = max(256, limit * 16)
            position = after
            if position is None:
                key = self._plan_next_record_key_locked(namespace)
                step_after = ""
            else:
                same = self._plan_get_record_locked(namespace, position[1])
                key = (namespace, position[1]) if same is not None else None
                if key is None:
                    key = self._plan_next_record_key_locked(namespace, position[1])
                    step_after = ""
                else:
                    step_after = position[3] if same.current_revision == position[2] else ""

            last_scanned: tuple[str, str, int, str] | None = None
            while key is not None:
                candidate_namespace, plan_id = key
                record = self._plan_get_record_locked(candidate_namespace, plan_id)
                if record is not None:
                    definition = self._plan_get_revision_locked(
                        candidate_namespace, plan_id, record.current_revision
                    )
                    if definition is None:
                        raise PlanRefused("store_unsupported", "current Plan revision is missing")
                    if (
                        definition.namespace != namespace
                        or definition.plan_id != plan_id
                        or definition.session_id != record.session_id
                        or definition.revision != record.current_revision
                        or definition_digest(definition) != record.definition_digest
                    ):
                        raise PlanRefused("store_unsupported", "current Plan revision digest is inconsistent")
                    for step in sorted(definition.steps, key=lambda item: item.step_id):
                        if step.step_id <= step_after:
                            continue
                        scanned += 1
                        last_scanned = (
                            candidate_namespace,
                            plan_id,
                            record.current_revision,
                            step.step_id,
                        )
                        candidate = self._plan_candidate_locked(record, definition, step, now)
                        if candidate is not None:
                            candidates.append(candidate)
                            if len(candidates) >= limit:
                                next_cursor = self._plan_cursor_after_locked(last_scanned)
                                return PlanStepPage(tuple(candidates), next_cursor, scanned)
                        if scanned >= scan_limit:
                            next_cursor = self._plan_cursor_after_locked(last_scanned)
                            return PlanStepPage(tuple(candidates), next_cursor, scanned)
                key = self._plan_next_record_key_locked(namespace, plan_id)
                step_after = ""
            return PlanStepPage(tuple(candidates), None, scanned)

        return self._plan_snapshot_read(work)

    def _plan_cursor_after_locked(
        self, position: tuple[str, str, int, str] | None
    ) -> str | None:
        if position is None:
            return None
        namespace, plan_id, revision, step_id = position
        record = self._plan_get_record_locked(namespace, plan_id)
        if record is not None:
            definition = self._plan_get_revision_locked(namespace, plan_id, record.current_revision)
            if definition is not None and (
                record.current_revision != revision
                or any(item.step_id > step_id for item in definition.steps)
            ):
                return _encode_ready_cursor(position)
        if self._plan_next_record_key_locked(namespace, plan_id) is not None:
            return _encode_ready_cursor(position)
        return None

    def _plan_candidate_locked(
        self,
        record: PlanRecord,
        definition: PlanDefinition,
        step: StepDefinition,
        now: float,
    ) -> PlanStepCandidate | None:
        if definition.revision != record.current_revision:
            return None
        if definition.deadline_at_ms is not None and now * 1000 >= definition.deadline_at_ms:
            return None
        try:
            approval = self._require_current_approval_locked(
                record.namespace, record.plan_id, record, definition, int(now * 1000)
            )
        except PlanRefused as exc:
            if exc.reason_code in {"approval_required", "approval_revoked", "approval_expired"}:
                return None
            raise

        bindings = self._plan_bindings_locked(record.namespace, record.plan_id)
        binding = self._plan_binding_for_step_locked(
            record.namespace, record.plan_id, record.current_revision, step.step_id
        )
        if binding is None and record.status not in {PlanStatus.APPROVED, PlanStatus.EXECUTING}:
            return None
        if binding is not None and record.status not in {PlanStatus.EXECUTING, PlanStatus.PAUSED}:
            return None
        dependency_times: list[float] = []
        for dependency in step.depends_on:
            accepted = self._plan_binding_for_step_locked(
                record.namespace, record.plan_id, record.current_revision, dependency
            )
            if accepted is None or accepted.binding_status != "accepted" or accepted.accepted_at is None:
                return None
            dependency_times.append(accepted.accepted_at)

        if binding is not None:
            if binding.binding_status not in {"admitted", "active"}:
                return None
            if not self._plan_task_is_schedulable_locked(binding.task_id, now):
                return None
            return PlanStepCandidate(
                namespace=record.namespace,
                plan_id=record.plan_id,
                revision=record.current_revision,
                step_id=step.step_id,
                priority=0,
                ready_since=binding.ready_since,
                control_version=record.control_version,
                definition_digest=record.definition_digest,
            )

        if len(bindings) >= definition.max_step_executions:
            return None
        active = sum(item.binding_status in {"admitted", "active"} for item in bindings)
        if active >= definition.max_concurrency:
            return None
        ready_since = max(
            [approval.decided_at_ms / 1000.0, *dependency_times]
        )
        return PlanStepCandidate(
            namespace=record.namespace,
            plan_id=record.plan_id,
            revision=record.current_revision,
            step_id=step.step_id,
            priority=0,
            ready_since=ready_since,
            control_version=record.control_version,
            definition_digest=record.definition_digest,
        )

    def _plan_require_claim_gates_locked(
        self,
        record: PlanRecord,
        definition: PlanDefinition,
        step: StepDefinition,
        *,
        now: float,
        existing_binding: bool,
    ) -> PlanApproval:
        allowed = (
            {PlanStatus.EXECUTING, PlanStatus.PAUSED}
            if existing_binding
            else {PlanStatus.APPROVED, PlanStatus.EXECUTING}
        )
        if record.status not in allowed:
            raise PlanRefused("plan_control_closed", f"Plan is {record.status.value}")
        if definition.deadline_at_ms is not None and now * 1000 >= definition.deadline_at_ms:
            raise PlanRefused("deadline_exceeded", "Plan deadline has passed")
        approval = self._require_current_approval_locked(
            record.namespace, record.plan_id, record, definition, int(now * 1000)
        )
        for dependency in step.depends_on:
            accepted = self._plan_binding_for_step_locked(
                record.namespace, record.plan_id, record.current_revision, dependency
            )
            if accepted is None or accepted.binding_status != "accepted" or accepted.accepted_at is None:
                raise PlanRefused("dependency_pending", f"Step {dependency!r} is not accepted")
        if not existing_binding:
            bindings = self._plan_bindings_locked(record.namespace, record.plan_id)
            if len(bindings) >= definition.max_step_executions:
                raise PlanRefused("execution_limit", "Plan execution limit has been reached")
            active = sum(item.binding_status in {"admitted", "active"} for item in bindings)
            if active >= definition.max_concurrency:
                raise PlanRefused("active_binding", "Plan concurrency limit is full")
        return approval

    def _claim_plan_step_sync(
        self,
        candidate: PlanStepCandidate,
        expected_control_version: int,
        request_id: str,
        principal: str,
        holder: str,
        ttl_seconds: float,
        now: float,
    ) -> PlanStepClaim:
        identity = {
            "identity_version": 1,
            "namespace": candidate.namespace,
            "plan_id": candidate.plan_id,
            "revision": candidate.revision,
            "step_id": candidate.step_id,
        }
        binding_id = "binding-" + hashlib.sha256(_json_dumps(identity).encode("utf-8")).hexdigest()
        request_digest = hashlib.sha256(
            _json_dumps({**identity, "principal": principal}).encode("utf-8")
        ).hexdigest()

        def work() -> PlanStepClaim:
            self._plan_require_schema()
            record = self._plan_get_record_locked(candidate.namespace, candidate.plan_id)
            if record is None:
                raise PlanRefused("unknown_reference", "Plan no longer exists")
            definition = self._plan_get_revision_locked(
                candidate.namespace, candidate.plan_id, record.current_revision
            )
            if definition is None or definition.revision != candidate.revision:
                raise PlanRefused("version_conflict", "Plan revision changed before step admission")
            if (
                definition.namespace != candidate.namespace
                or definition.plan_id != candidate.plan_id
                or definition.session_id != record.session_id
                or definition_digest(definition) != candidate.definition_digest
            ):
                raise PlanRefused("content_conflict", "candidate does not match the immutable Plan revision")

            existing = self._plan_binding_for_step_locked(
                candidate.namespace, candidate.plan_id, candidate.revision, candidate.step_id
            )
            if existing is not None:
                if existing.binding_id != binding_id:
                    raise PlanRefused("content_conflict", "step already has a different execution binding")
                if existing.request_id != request_id or existing.request_digest != request_digest:
                    raise PlanRefused("request_id_conflict", "step binding was claimed by a different request")
                task = self._plan_task_get_locked(existing.task_id)
                if task is None:
                    raise PlanRefused("store_unsupported", "bound Plan Task is missing")
                if (
                    task.namespace != existing.namespace
                    or task.session_id != definition.session_id
                    or task.run_id != existing.run_id
                ):
                    raise PlanRefused("store_unsupported", "bound Task identity is inconsistent")
                if self._plan_task_initial_digest_locked(existing.task_id) != existing.initial_input_digest:
                    raise PlanRefused("store_unsupported", "bound Task initial input digest is inconsistent")
                live_claim = self._plan_task_claim_for_holder_locked(existing.task_id, holder)
                if live_claim is not None and not live_claim.expired_at(now):
                    return PlanStepClaim(existing, task, live_claim)
                if expected_control_version != record.control_version:
                    raise PlanRefused(
                        "version_conflict",
                        f"Plan is at control_version {record.control_version}, not {expected_control_version}",
                    )
                if candidate.control_version != expected_control_version:
                    raise PlanRefused("version_conflict", "candidate control version is stale")
                step = next((item for item in definition.steps if item.step_id == candidate.step_id), None)
                if step is None:
                    raise PlanRefused("unknown_reference", "candidate step is not in the current revision")
                self._plan_require_claim_gates_locked(
                    record, definition, step, now=now, existing_binding=True
                )
                if not self._plan_task_is_schedulable_locked(existing.task_id, now):
                    raise PlanRefused("active_binding", "bound Task is not recoverable yet")
                claim = self._plan_task_claim_locked(
                    existing.task_id, holder=holder, ttl_seconds=ttl_seconds, now=now
                )
                if claim is None:
                    raise PlanRefused("active_binding", "bound Task has a live claim held elsewhere")
                return PlanStepClaim(existing, self._plan_task_get_locked(existing.task_id), claim)

            if expected_control_version != record.control_version:
                raise PlanRefused(
                    "version_conflict",
                    f"Plan is at control_version {record.control_version}, not {expected_control_version}",
                )
            if candidate.control_version != expected_control_version:
                raise PlanRefused("version_conflict", "candidate control version is stale")
            step = next((item for item in definition.steps if item.step_id == candidate.step_id), None)
            if step is None:
                raise PlanRefused("unknown_reference", "candidate step is not in the current revision")
            self._plan_require_claim_gates_locked(
                record, definition, step, now=now, existing_binding=False
            )
            fresh = self._plan_candidate_locked(record, definition, step, now)
            if fresh is None:
                raise PlanRefused("active_binding", "Plan step is no longer schedulable")

            from ..tasks.types import InputContent, InputKind, TaskAdmission

            task_digest = hashlib.sha256(_json_dumps(identity).encode("utf-8")).hexdigest()
            task_id = "plan-task-" + task_digest
            run_id = "plan-run-" + task_digest
            if self._plan_task_get_locked(task_id) is not None:
                raise PlanRefused("task_id_conflict", "derived Plan Task identity is already in use")
            initial = InputContent(
                kind=InputKind.INITIAL,
                text=f"{step.title}\n\n{step.instruction}",
                parameters={
                    "plan_step": {
                        "plan_id": record.plan_id,
                        "revision": record.current_revision,
                        "step_id": step.step_id,
                    },
                    "execution_envelope": definition.execution_envelope.to_record(),
                    "input_refs": [item.to_record() for item in step.input_refs],
                    "required_context_refs": [
                        item.to_record() for item in step.required_context_refs
                    ],
                    "acceptance_criteria": [
                        item.to_record() for item in step.acceptance_criteria
                    ],
                    "output_contract": step.output_contract,
                },
            )
            profile = step.execution_profile_ref or definition.execution_envelope.profile_ref
            workspace = step.workspace_ref or definition.execution_envelope.workspace_ref
            admission = TaskAdmission(
                namespace=record.namespace,
                session_id=record.session_id,
                task_id=task_id,
                run_id=run_id,
                initial_input=initial,
                profile=profile.ref,
                workspace_ref=workspace.ref,
                created_at=now,
                deadline_at=(None if definition.deadline_at_ms is None else definition.deadline_at_ms / 1000.0),
            )
            self._plan_task_check_admission_locked(record.namespace, initial)
            task = self._plan_task_admit_locked(admission, now)
            claim = self._plan_task_claim_locked(
                task_id, holder=holder, ttl_seconds=ttl_seconds, now=now
            )
            if claim is None:
                raise PlanRefused("active_binding", "newly admitted Plan Task could not be claimed")
            binding = StepTaskBinding(
                binding_id=binding_id,
                namespace=record.namespace,
                plan_id=record.plan_id,
                revision=record.current_revision,
                step_id=step.step_id,
                task_id=task_id,
                run_id=run_id,
                initial_input_digest=initial.digest,
                request_id=request_id,
                request_digest=request_digest,
                binding_status="admitted",
                ready_since=fresh.ready_since,
                created_at=now,
            )
            self._plan_save_binding_locked(binding)
            moved = (
                self._advance_record(record, PlanStatus.EXECUTING, now=now, reason="step_admitted")
                if record.status is PlanStatus.APPROVED
                else replace(record, control_version=record.control_version + 1, updated_at=now)
            )
            self._plan_update_record_locked(moved)
            event = PlanEvent(
                event_id=_event_id(record.namespace, record.plan_id, moved.control_version),
                namespace=record.namespace,
                plan_id=record.plan_id,
                control_version=moved.control_version,
                event_type="step_admitted",
                payload={
                    "principal": principal,
                    "revision": record.current_revision,
                    "step_id": step.step_id,
                    "binding_id": binding_id,
                    "task_id": task_id,
                    "run_id": run_id,
                },
                created_at=now,
            )
            self._plan_append_event_locked(event)
            return PlanStepClaim(binding, self._plan_task_get_locked(task_id), claim)

        return self._plan_transaction(work)

    def _read_events_sync(
        self, namespace: str, plan_id: str, after_control_version: int, limit: int
    ) -> tuple[PlanEvent, ...]:
        def work() -> tuple[PlanEvent, ...]:
            self._plan_require_schema()
            return self._plan_read_events_locked(namespace, plan_id, after_control_version, limit)

        return self._plan_read(work)

    def _read_approvals_sync(
        self, namespace: str, plan_id: str
    ) -> tuple[tuple[PlanApproval, ...], tuple[ApprovalRevocation, ...]]:
        def work() -> tuple[tuple[PlanApproval, ...], tuple[ApprovalRevocation, ...]]:
            self._plan_require_schema()
            return (
                self._plan_approvals_locked(namespace, plan_id),
                self._plan_revocations_locked(namespace, plan_id),
            )

        return self._plan_read(work)

    def _read_outbox_sync(self, pending_only: bool, limit: int) -> tuple[PlanOutboxEntry, ...]:
        def work() -> tuple[PlanOutboxEntry, ...]:
            self._plan_require_schema()
            return self._plan_read_outbox_locked(pending_only, limit)

        return self._plan_read(work)

    def _deliver_outbox_sync(self, outbox_id: str, at: float) -> bool:
        def work() -> bool:
            self._plan_require_schema()
            return self._plan_deliver_outbox_locked(outbox_id, at)

        return self._plan_transaction(work)

    def _fail_outbox_sync(self, outbox_id: str, error: str) -> bool:
        def work() -> bool:
            self._plan_require_schema()
            return self._plan_fail_outbox_locked(outbox_id, error)

        return self._plan_transaction(work)


class SqliteOrchestrationMixin(OrchestrationMixin):
    """Plan store operations over the parent ``SqliteControlStore`` connection."""

    @property
    def orchestration_enabled(self) -> bool:
        return bool(getattr(self, "_orchestration_enabled", False))

    def initialize_orchestration_schema(self, *, enabled: bool = False) -> None:
        if type(enabled) is not bool:
            raise ContractError("enable_orchestration must be a bool")
        self._orchestration_enabled = bool(enabled)
        def validate_existing() -> bool:
            row = self._connection.execute(
                "SELECT value FROM control_meta WHERE key = 'plan_schema_version'"
            ).fetchone()
            if row is not None:
                _require_plan_version(row[0])
                missing = self._missing_plan_tables()
                if missing:
                    raise PlanRefused("store_unsupported", f"Plan schema is incomplete: {', '.join(missing)}")
                return True
            present = self._existing_plan_tables()
            if present:
                raise PlanRefused(
                    "store_unsupported",
                    "Plan tables exist without a version marker; refusing to guess at their layout",
                )
            return False

        if self.read_only or not enabled:
            has_schema = self._read(validate_existing)
            if enabled and not has_schema:
                raise PlanRefused("store_unsupported", "read-only control store has no Plan schema")
            if has_schema:
                row = self._read(
                    lambda: self._connection.execute("SELECT COUNT(*) FROM plan_records").fetchone()
                )
                if int(row[0]) and not enabled:
                    raise PlanRefused(
                        "store_unsupported",
                        "this control store contains Plan records; reopen it with orchestration enabled",
                    )
            return

        def work() -> None:
            if validate_existing():
                return
            for statement in _PLAN_SCHEMA.split(";"):
                if statement.strip():
                    self._connection.execute(statement)
            self._connection.execute(
                "INSERT INTO control_meta(key, value) VALUES ('plan_schema_version', ?)",
                (str(PLAN_CONTROL_SCHEMA_VERSION),),
            )

        self._in_transaction(work)

    def _existing_plan_tables(self) -> set[str]:
        rows = self._connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'plan_%'"
        ).fetchall()
        return {str(row[0]) for row in rows}

    def _missing_plan_tables(self) -> list[str]:
        return sorted(_PLAN_TABLES - self._existing_plan_tables())

    def _plan_require_schema(self) -> None:
        if not self.orchestration_enabled:
            raise PlanRefused("store_unsupported", "Plan orchestration was not enabled for this control store")
        try:
            row = self._connection.execute(
                "SELECT value FROM control_meta WHERE key = 'plan_schema_version'"
            ).fetchone()
        except sqlite3.Error as exc:
            raise PlanRefused("store_unsupported", "this control store has no Plan schema") from exc
        if row is None:
            raise PlanRefused("store_unsupported", "this control store has no Plan schema")
        _require_plan_version(row[0])
        missing = self._missing_plan_tables()
        if missing:
            raise PlanRefused("store_unsupported", f"Plan schema is incomplete: {', '.join(missing)}")

    async def _plan_call(self, function: Callable[..., Any], *args: Any) -> Any:
        return await asyncio.to_thread(function, *args)

    def _plan_transaction(self, work: Callable[[], Any]) -> Any:
        return self._in_transaction(work)

    def _plan_read(self, work: Callable[[], Any]) -> Any:
        return self._read(work)

    def _plan_snapshot_read(self, work: Callable[[], Any]) -> Any:
        def snapshot() -> Any:
            self._connection.execute("BEGIN")
            try:
                result = work()
                self._connection.execute("COMMIT")
                return result
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise

        return self._read(snapshot)

    def _plan_get_record_locked(self, namespace: str, plan_id: str) -> PlanRecord | None:
        row = self._connection.execute(
            "SELECT namespace, session_id, plan_id, current_revision, control_version, status, "
            "definition_digest, active_approval_id, reason, created_at, updated_at, schema_version "
            "FROM plan_records WHERE namespace = ? AND plan_id = ?",
            (namespace, plan_id),
        ).fetchone()
        if row is None:
            return None
        return PlanRecord(
            namespace=str(row[0]), session_id=str(row[1]), plan_id=str(row[2]),
            current_revision=int(row[3]), control_version=int(row[4]), status=str(row[5]),
            definition_digest=str(row[6]), active_approval_id=str(row[7]), reason=str(row[8]),
            created_at=float(row[9]), updated_at=float(row[10]), schema_version=int(row[11]),
        )

    def _plan_insert_record_locked(self, record: PlanRecord) -> None:
        self._connection.execute(
            "INSERT INTO plan_records(namespace, session_id, plan_id, current_revision, control_version, "
            "status, definition_digest, active_approval_id, reason, created_at, updated_at, schema_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.namespace, record.session_id, record.plan_id, record.current_revision,
                record.control_version, record.status.value, record.definition_digest,
                record.active_approval_id, record.reason, record.created_at, record.updated_at,
                record.schema_version,
            ),
        )

    def _plan_update_record_locked(self, record: PlanRecord) -> None:
        cursor = self._connection.execute(
            "UPDATE plan_records SET current_revision = ?, control_version = ?, status = ?, "
            "definition_digest = ?, active_approval_id = ?, reason = ?, updated_at = ? "
            "WHERE namespace = ? AND plan_id = ?",
            (
                record.current_revision, record.control_version, record.status.value,
                record.definition_digest, record.active_approval_id, record.reason,
                record.updated_at, record.namespace, record.plan_id,
            ),
        )
        if cursor.rowcount != 1:
            raise PlanRefused("unknown_reference", "Plan disappeared while a control action was committing")

    def _plan_get_revision_locked(
        self, namespace: str, plan_id: str, revision: int
    ) -> PlanDefinition | None:
        row = self._connection.execute(
            "SELECT definition_digest, definition_json FROM plan_revisions "
            "WHERE namespace = ? AND plan_id = ? AND revision = ?",
            (namespace, plan_id, revision),
        ).fetchone()
        if row is None:
            return None
        definition = plan_definition_from_json(str(row[1]))
        if (
            definition.namespace != namespace
            or definition.plan_id != plan_id
            or definition.revision != revision
            or definition_digest(definition) != str(row[0])
        ):
            raise PlanRefused("store_unsupported", "stored Plan revision identity or digest is inconsistent")
        return definition

    def _plan_insert_revision_locked(self, definition: PlanDefinition, created_at: float) -> None:
        self._connection.execute(
            "INSERT INTO plan_revisions(namespace, plan_id, revision, definition_digest, definition_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                definition.namespace, definition.plan_id, definition.revision,
                definition_digest(definition), _json_dumps(plan_definition_to_record(definition)), created_at,
            ),
        )

    def _plan_get_approval_locked(
        self, namespace: str, plan_id: str, decision_id: str
    ) -> PlanApproval | None:
        row = self._connection.execute(
            "SELECT record_json FROM plan_approvals WHERE namespace = ? AND plan_id = ? AND decision_id = ?",
            (namespace, plan_id, decision_id),
        ).fetchone()
        return None if row is None else _approval_from_json(str(row[0]))

    def _plan_insert_approval_locked(self, approval: PlanApproval) -> None:
        self._connection.execute(
            "INSERT INTO plan_approvals(namespace, plan_id, decision_id, revision, decision, record_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                approval.namespace, approval.plan_id, approval.decision_id, approval.revision,
                approval.decision.value, _json_dumps(approval.to_record()),
            ),
        )

    def _plan_get_revocation_locked(
        self, namespace: str, plan_id: str, decision_id: str
    ) -> ApprovalRevocation | None:
        row = self._connection.execute(
            "SELECT record_json FROM plan_approval_revocations "
            "WHERE namespace = ? AND plan_id = ? AND decision_id = ?",
            (namespace, plan_id, decision_id),
        ).fetchone()
        return None if row is None else _revocation_from_json(str(row[0]))

    def _plan_insert_revocation_locked(self, revocation: ApprovalRevocation) -> None:
        self._connection.execute(
            "INSERT INTO plan_approval_revocations(namespace, plan_id, decision_id, revoked_at_ms, record_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                revocation.namespace, revocation.plan_id, revocation.decision_id,
                revocation.revoked_at_ms,
                _json_dumps(
                    {
                        "namespace": revocation.namespace,
                        "plan_id": revocation.plan_id,
                        "decision_id": revocation.decision_id,
                        "revoked_by": revocation.revoked_by,
                        "revoked_at_ms": revocation.revoked_at_ms,
                        "reason": revocation.reason,
                    }
                ),
            ),
        )

    def _plan_approvals_locked(self, namespace: str, plan_id: str) -> tuple[PlanApproval, ...]:
        rows = self._connection.execute(
            "SELECT record_json FROM plan_approvals WHERE namespace = ? AND plan_id = ? "
            "ORDER BY revision, decision_id",
            (namespace, plan_id),
        ).fetchall()
        return tuple(_approval_from_json(str(row[0])) for row in rows)

    def _plan_revocations_locked(self, namespace: str, plan_id: str) -> tuple[ApprovalRevocation, ...]:
        rows = self._connection.execute(
            "SELECT record_json FROM plan_approval_revocations WHERE namespace = ? AND plan_id = ? "
            "ORDER BY revoked_at_ms, decision_id",
            (namespace, plan_id),
        ).fetchall()
        return tuple(_revocation_from_json(str(row[0])) for row in rows)

    def _plan_append_event_locked(self, event: PlanEvent) -> None:
        payload_json = _json_dumps(event.payload)
        self._connection.execute(
            "INSERT INTO plan_events(event_id, namespace, plan_id, control_version, event_type, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                event.event_id, event.namespace, event.plan_id, event.control_version,
                event.event_type, payload_json, event.created_at,
            ),
        )
        self._connection.execute(
            "INSERT INTO plan_outbox(outbox_id, namespace, plan_id, event_id, kind, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                event.event_id, event.namespace, event.plan_id, event.event_id,
                event.event_type, payload_json, event.created_at,
            ),
        )

    def _plan_read_events_locked(
        self, namespace: str, plan_id: str, after_control_version: int, limit: int
    ) -> tuple[PlanEvent, ...]:
        rows = self._connection.execute(
            "SELECT event_id, namespace, plan_id, control_version, event_type, payload_json, created_at "
            "FROM plan_events WHERE namespace = ? AND plan_id = ? AND control_version > ? "
            "ORDER BY control_version LIMIT ?",
            (namespace, plan_id, after_control_version, limit),
        ).fetchall()
        return tuple(_event_from_row(row) for row in rows)

    def _plan_has_active_bindings_locked(self, namespace: str, plan_id: str) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM plan_bindings WHERE namespace = ? AND plan_id = ? "
            "AND binding_status IN ('admitted', 'active') LIMIT 1",
            (namespace, plan_id),
        ).fetchone()
        return row is not None

    def _plan_request_locked(
        self, namespace: str, plan_id: str, request_id: str
    ) -> tuple[str, PlanRecord] | None:
        row = self._connection.execute(
            "SELECT request_digest, response_json FROM plan_requests "
            "WHERE namespace = ? AND plan_id = ? AND request_id = ?",
            (namespace, plan_id, request_id),
        ).fetchone()
        return None if row is None else (str(row[0]), _record_from_json(str(row[1])))

    def _plan_save_request_locked(
        self,
        namespace: str,
        plan_id: str,
        request_id: str,
        request_digest: str,
        record: PlanRecord,
        created_at: float,
    ) -> None:
        self._connection.execute(
            "INSERT INTO plan_requests(namespace, plan_id, request_id, request_digest, response_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                namespace, plan_id, request_id, request_digest,
                _json_dumps(record.to_record()), created_at,
            ),
        )

    def _plan_read_outbox_locked(self, pending_only: bool, limit: int) -> tuple[PlanOutboxEntry, ...]:
        where = "WHERE delivered_at IS NULL" if pending_only else ""
        rows = self._connection.execute(
            "SELECT outbox_id, namespace, plan_id, event_id, kind, payload_json, created_at, "
            "delivered_at, attempts, last_error FROM plan_outbox "
            f"{where} ORDER BY created_at, outbox_id LIMIT ?",
            (limit,),
        ).fetchall()
        return tuple(_outbox_from_row(row) for row in rows)

    def _plan_deliver_outbox_locked(self, outbox_id: str, at: float) -> bool:
        cursor = self._connection.execute(
            "UPDATE plan_outbox SET delivered_at = ?, attempts = attempts + 1, last_error = '' "
            "WHERE outbox_id = ? AND delivered_at IS NULL",
            (at, outbox_id),
        )
        return cursor.rowcount == 1

    def _plan_fail_outbox_locked(self, outbox_id: str, error: str) -> bool:
        cursor = self._connection.execute(
            "UPDATE plan_outbox SET attempts = attempts + 1, last_error = ? WHERE outbox_id = ?",
            (error, outbox_id),
        )
        return cursor.rowcount == 1

    def _plan_next_record_key_locked(
        self, namespace: str, after_plan_id: str = ""
    ) -> tuple[str, str] | None:
        row = self._connection.execute(
            "SELECT namespace, plan_id FROM plan_records "
            "WHERE namespace = ? AND plan_id > ? "
            "ORDER BY namespace, plan_id LIMIT 1",
            (namespace, after_plan_id),
        ).fetchone()
        return None if row is None else (str(row[0]), str(row[1]))

    def _plan_binding_for_step_locked(
        self, namespace: str, plan_id: str, revision: int, step_id: str
    ) -> StepTaskBinding | None:
        row = self._connection.execute(
            "SELECT record_json FROM plan_bindings WHERE namespace = ? AND plan_id = ? "
            "AND revision = ? AND step_id = ? ORDER BY created_at DESC, binding_id DESC LIMIT 1",
            (namespace, plan_id, revision, step_id),
        ).fetchone()
        if row is None:
            return None
        return _binding_from_record(json.loads(str(row[0])))

    def _plan_binding_for_task_locked(self, task_id: str) -> StepTaskBinding | None:
        row = self._connection.execute(
            "SELECT record_json FROM plan_bindings WHERE task_id = ? LIMIT 1", (task_id,)
        ).fetchone()
        return None if row is None else _binding_from_record(json.loads(str(row[0])))

    def _plan_bindings_locked(
        self, namespace: str, plan_id: str, revision: int | None = None
    ) -> tuple[StepTaskBinding, ...]:
        sql = "SELECT record_json FROM plan_bindings WHERE namespace = ? AND plan_id = ?"
        values: tuple[Any, ...] = (namespace, plan_id)
        if revision is not None:
            sql += " AND revision = ?"
            values += (revision,)
        sql += " ORDER BY created_at, binding_id"
        rows = self._connection.execute(sql, values).fetchall()
        return tuple(_binding_from_record(json.loads(str(row[0]))) for row in rows)

    def _plan_save_binding_locked(self, binding: StepTaskBinding) -> None:
        self._connection.execute(
            "INSERT INTO plan_bindings(binding_id, namespace, plan_id, revision, step_id, task_id, "
            "run_id, binding_status, record_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                binding.binding_id, binding.namespace, binding.plan_id, binding.revision,
                binding.step_id, binding.task_id, binding.run_id, binding.binding_status,
                _json_dumps(binding.to_record()), binding.created_at,
            ),
        )

    def _plan_task_get_locked(self, task_id: str) -> Any:
        return self._load_task_locked(task_id)

    def _plan_task_initial_digest_locked(self, task_id: str) -> str | None:
        record = self._load_input_locked(task_id, "initial")
        return None if record is None else record.content_digest

    def _plan_task_is_schedulable_locked(self, task_id: str, now: float) -> bool:
        task = self._load_task_locked(task_id)
        if task is None:
            return False
        if task.status.value == "queued":
            return True
        if task.status.value != "running":
            return False
        claim = self._load_claim_locked(task_id)
        return claim is None or float(claim[5]) <= now

    def _plan_task_admit_locked(self, admission: Any, now: float) -> Any:
        return self._admit_task_locked(admission, now)

    def _plan_task_check_admission_locked(self, namespace: str, initial_input: Any) -> None:
        size = initial_input.size_bytes
        if size > self.backlog.max_payload_bytes or size > self.backlog.max_task_bytes:
            raise PlanRefused("backlog_full", "Plan initial input exceeds the Task input byte limit")
        row = self._connection.execute(
            "SELECT COALESCE(SUM(c.pending_count), 0) FROM inbox_counters AS c "
            "JOIN tasks AS t ON t.task_id = c.task_id WHERE t.namespace = ?",
            (namespace,),
        ).fetchone()
        namespace_count = int(row[0] or 0)
        row = self._connection.execute(
            "SELECT COALESCE(SUM(pending_count), 0) FROM inbox_counters"
        ).fetchone()
        total_count = int(row[0] or 0)
        if namespace_count >= self.backlog.per_namespace or total_count >= self.backlog.total:
            raise PlanRefused("backlog_full", "Task input backlog has no room for this Plan step")

    def _plan_task_claim_locked(
        self, task_id: str, *, holder: str, ttl_seconds: float, now: float
    ) -> Any:
        from ..tasks.types import DispatchClaim, TaskStatus

        task = self._load_task_locked(task_id)
        if task is None or task.status.is_terminal:
            return None
        current = self._load_claim_locked(task_id)
        if current is not None and float(current[5]) > now:
            return None
        token = self._next_fencing_token_locked(task_id)
        claim = DispatchClaim(
            task_id=task_id,
            holder=holder,
            attempt_id=f"{task_id}-a{token}",
            fencing_token=token,
            claimed_at=now,
            expires_at=now + ttl_seconds,
            namespace=task.namespace,
        )
        self._connection.execute(
            "INSERT INTO task_claims(task_id, holder, attempt_id, fencing_token, claimed_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(task_id) DO UPDATE SET "
            "holder=excluded.holder, attempt_id=excluded.attempt_id, "
            "fencing_token=excluded.fencing_token, claimed_at=excluded.claimed_at, "
            "expires_at=excluded.expires_at",
            (task_id, claim.holder, claim.attempt_id, claim.fencing_token, now, claim.expires_at),
        )
        if task.status is TaskStatus.WAITING:
            task = task.transition(TaskStatus.QUEUED, reason="external_fact_persisted", at=now)
        if task.status is TaskStatus.QUEUED:
            task = task.transition(
                TaskStatus.RUNNING,
                reason="execution_right_claimed",
                at=now,
                attempt_id=claim.attempt_id,
            )
        task = replace(
            task,
            attempt_count=task.attempt_count + 1,
            current_attempt_id=claim.attempt_id,
        )
        self._save_task_locked(task)
        return claim

    def _plan_task_claim_for_holder_locked(self, task_id: str, holder: str) -> Any:
        from ..tasks.types import DispatchClaim

        row = self._load_claim_locked(task_id)
        if row is None or str(row[1]) != holder:
            return None
        task = self._load_task_locked(task_id)
        return DispatchClaim(
            task_id=task_id,
            holder=str(row[1]),
            attempt_id=str(row[2]),
            fencing_token=int(row[3]),
            claimed_at=float(row[4]),
            expires_at=float(row[5]),
            namespace="" if task is None else task.namespace,
        )


class MemoryOrchestrationMixin(OrchestrationMixin):
    """Plan store operations protected by the parent ``MemoryControlStore`` lock."""

    @property
    def orchestration_enabled(self) -> bool:
        return bool(getattr(self, "_orchestration_enabled", False))

    def initialize_orchestration_schema(self, *, enabled: bool = False) -> None:
        if type(enabled) is not bool:
            raise ContractError("enable_orchestration must be a bool")
        self._orchestration_enabled = bool(enabled)
        self._plan_records: dict[tuple[str, str], PlanRecord] = {}
        self._plan_revisions: dict[tuple[str, str, int], PlanDefinition] = {}
        self._plan_approval_records: dict[tuple[str, str, str], PlanApproval] = {}
        self._plan_revocation_records: dict[tuple[str, str, str], ApprovalRevocation] = {}
        self._plan_event_records: dict[tuple[str, str], list[PlanEvent]] = {}
        self._plan_outbox_records: dict[str, PlanOutboxEntry] = {}
        self._plan_request_records: dict[tuple[str, str, str], tuple[str, PlanRecord]] = {}
        self._plan_binding_records: dict[str, Mapping[str, Any]] = {}

    async def _plan_call(self, function: Callable[..., Any], *args: Any) -> Any:
        return function(*args)

    def _plan_transaction(self, work: Callable[[], Any]) -> Any:
        with self._lock:
            mapping_names = (
                "_plan_records",
                "_plan_revisions",
                "_plan_approval_records",
                "_plan_revocation_records",
                "_plan_outbox_records",
                "_plan_request_records",
                "_plan_binding_records",
                "_tasks",
                "_inputs",
                "_counters",
                "_claims",
                "_tokens",
            )
            snapshots = {name: dict(getattr(self, name)) for name in mapping_names}
            snapshots["_inputs"] = {
                task_id: dict(records) for task_id, records in self._inputs.items()
            }
            snapshots["_counters"] = {
                task_id: list(values) for task_id, values in self._counters.items()
            }
            event_snapshot = {
                key: list(events) for key, events in self._plan_event_records.items()
            }
            try:
                return work()
            except BaseException:
                for name, value in snapshots.items():
                    setattr(self, name, value)
                self._plan_event_records = event_snapshot
                raise

    def _plan_read(self, work: Callable[[], Any]) -> Any:
        with self._lock:
            return work()

    def _plan_require_schema(self) -> None:
        if not self.orchestration_enabled:
            raise PlanRefused("store_unsupported", "Plan orchestration was not enabled for this control store")
        return None

    def _plan_get_record_locked(self, namespace: str, plan_id: str) -> PlanRecord | None:
        return self._plan_records.get((namespace, plan_id))

    def _plan_insert_record_locked(self, record: PlanRecord) -> None:
        key = (record.namespace, record.plan_id)
        if key in self._plan_records:
            raise PlanRefused("content_conflict", "Plan identity already exists")
        self._plan_records[key] = record

    def _plan_update_record_locked(self, record: PlanRecord) -> None:
        key = (record.namespace, record.plan_id)
        if key not in self._plan_records:
            raise PlanRefused("unknown_reference", "Plan disappeared while a control action was committing")
        self._plan_records[key] = record

    def _plan_get_revision_locked(
        self, namespace: str, plan_id: str, revision: int
    ) -> PlanDefinition | None:
        definition = self._plan_revisions.get((namespace, plan_id, revision))
        if definition is not None and definition_digest(definition) != definition_digest(
            PlanDefinition.from_record(definition.to_record())
        ):
            raise PlanRefused("store_unsupported", "stored Plan revision digest is inconsistent")
        return definition

    def _plan_insert_revision_locked(self, definition: PlanDefinition, created_at: float) -> None:
        del created_at
        key = (definition.namespace, definition.plan_id, definition.revision)
        if key in self._plan_revisions:
            raise PlanRefused("content_conflict", "Plan revision is immutable and already exists")
        self._plan_revisions[key] = definition

    def _plan_get_approval_locked(
        self, namespace: str, plan_id: str, decision_id: str
    ) -> PlanApproval | None:
        return self._plan_approval_records.get((namespace, plan_id, decision_id))

    def _plan_insert_approval_locked(self, approval: PlanApproval) -> None:
        key = (approval.namespace, approval.plan_id, approval.decision_id)
        if key in self._plan_approval_records:
            raise PlanRefused("content_conflict", "approval decision_id already exists")
        self._plan_approval_records[key] = approval

    def _plan_get_revocation_locked(
        self, namespace: str, plan_id: str, decision_id: str
    ) -> ApprovalRevocation | None:
        return self._plan_revocation_records.get((namespace, plan_id, decision_id))

    def _plan_insert_revocation_locked(self, revocation: ApprovalRevocation) -> None:
        key = (revocation.namespace, revocation.plan_id, revocation.decision_id)
        if key in self._plan_revocation_records:
            raise PlanRefused("content_conflict", "approval already has a revocation fact")
        self._plan_revocation_records[key] = revocation

    def _plan_approvals_locked(self, namespace: str, plan_id: str) -> tuple[PlanApproval, ...]:
        return tuple(
            sorted(
                (
                    approval
                    for (scope, identity, _), approval in self._plan_approval_records.items()
                    if scope == namespace and identity == plan_id
                ),
                key=lambda item: (item.revision, item.decision_id),
            )
        )

    def _plan_revocations_locked(self, namespace: str, plan_id: str) -> tuple[ApprovalRevocation, ...]:
        return tuple(
            sorted(
                (
                    revocation
                    for (scope, identity, _), revocation in self._plan_revocation_records.items()
                    if scope == namespace and identity == plan_id
                ),
                key=lambda item: (item.revoked_at_ms, item.decision_id),
            )
        )

    def _plan_append_event_locked(self, event: PlanEvent) -> None:
        key = (event.namespace, event.plan_id)
        events = self._plan_event_records.setdefault(key, [])
        if any(item.control_version == event.control_version for item in events):
            raise PlanRefused("content_conflict", "Plan control version already has an event")
        events.append(event)
        self._plan_outbox_records[event.event_id] = PlanOutboxEntry(
            outbox_id=event.event_id,
            namespace=event.namespace,
            plan_id=event.plan_id,
            event_id=event.event_id,
            kind=event.event_type,
            payload=event.payload,
            created_at=event.created_at,
        )

    def _plan_read_events_locked(
        self, namespace: str, plan_id: str, after_control_version: int, limit: int
    ) -> tuple[PlanEvent, ...]:
        events = self._plan_event_records.get((namespace, plan_id), ())
        return tuple(
            item
            for item in sorted(events, key=lambda event: event.control_version)
            if item.control_version > after_control_version
        )[:limit]

    def _plan_has_active_bindings_locked(self, namespace: str, plan_id: str) -> bool:
        return any(
            record.get("namespace") == namespace
            and record.get("plan_id") == plan_id
            and record.get("binding_status") in {"admitted", "active"}
            for record in self._plan_binding_records.values()
        )

    def _plan_request_locked(
        self, namespace: str, plan_id: str, request_id: str
    ) -> tuple[str, PlanRecord] | None:
        return self._plan_request_records.get((namespace, plan_id, request_id))

    def _plan_save_request_locked(
        self,
        namespace: str,
        plan_id: str,
        request_id: str,
        request_digest: str,
        record: PlanRecord,
        created_at: float,
    ) -> None:
        del created_at
        key = (namespace, plan_id, request_id)
        if key in self._plan_request_records:
            raise PlanRefused("request_id_conflict", "request_id already exists")
        self._plan_request_records[key] = (request_digest, record)

    def _plan_read_outbox_locked(self, pending_only: bool, limit: int) -> tuple[PlanOutboxEntry, ...]:
        entries = [
            item for item in self._plan_outbox_records.values()
            if not pending_only or item.pending
        ]
        entries.sort(key=lambda item: (item.created_at, item.outbox_id))
        return tuple(entries[:limit])

    def _plan_deliver_outbox_locked(self, outbox_id: str, at: float) -> bool:
        entry = self._plan_outbox_records.get(outbox_id)
        if entry is None or not entry.pending:
            return False
        self._plan_outbox_records[outbox_id] = entry.delivered(at=at)
        return True

    def _plan_fail_outbox_locked(self, outbox_id: str, error: str) -> bool:
        entry = self._plan_outbox_records.get(outbox_id)
        if entry is None:
            return False
        self._plan_outbox_records[outbox_id] = entry.failed(error=error)
        return True

    def _plan_next_record_key_locked(
        self, namespace: str, after_plan_id: str = ""
    ) -> tuple[str, str] | None:
        keys = sorted(self._plan_records)
        return next(
            (key for key in keys if key[0] == namespace and key[1] > after_plan_id),
            None,
        )

    def _plan_binding_for_step_locked(
        self, namespace: str, plan_id: str, revision: int, step_id: str
    ) -> StepTaskBinding | None:
        matches = [
            _binding_from_record(value)
            for value in self._plan_binding_records.values()
            if value.get("namespace") == namespace
            and value.get("plan_id") == plan_id
            and value.get("revision") == revision
            and value.get("step_id") == step_id
        ]
        return max(matches, key=lambda item: (item.created_at, item.binding_id), default=None)

    def _plan_binding_for_task_locked(self, task_id: str) -> StepTaskBinding | None:
        value = next(
            (item for item in self._plan_binding_records.values() if item.get("task_id") == task_id),
            None,
        )
        return None if value is None else _binding_from_record(value)

    def _plan_bindings_locked(
        self, namespace: str, plan_id: str, revision: int | None = None
    ) -> tuple[StepTaskBinding, ...]:
        return tuple(
            sorted(
                (
                    _binding_from_record(value)
                    for value in self._plan_binding_records.values()
                    if value.get("namespace") == namespace
                    and value.get("plan_id") == plan_id
                    and (revision is None or value.get("revision") == revision)
                ),
                key=lambda item: (item.created_at, item.binding_id),
            )
        )

    def _plan_save_binding_locked(self, binding: StepTaskBinding) -> None:
        if binding.binding_id in self._plan_binding_records:
            raise PlanRefused("content_conflict", "step binding identity already exists")
        self._plan_binding_records[binding.binding_id] = dict(binding.to_record())

    def _plan_task_get_locked(self, task_id: str) -> Any:
        return self._tasks.get(task_id)

    def _plan_task_initial_digest_locked(self, task_id: str) -> str | None:
        record = self._inputs.get(task_id, {}).get("initial")
        return None if record is None else record.content_digest

    def _plan_task_is_schedulable_locked(self, task_id: str, now: float) -> bool:
        from ..tasks.types import TaskStatus

        task = self._tasks.get(task_id)
        if task is None:
            return False
        if task.status is TaskStatus.QUEUED:
            return True
        if task.status is not TaskStatus.RUNNING:
            return False
        claim = self._claims.get(task_id)
        return claim is None or claim[4] <= now

    def _plan_task_admit_locked(self, admission: Any, now: float) -> Any:
        return self._admit_task_sync(admission, now)

    def _plan_task_check_admission_locked(self, namespace: str, initial_input: Any) -> None:
        size = initial_input.size_bytes
        if size > self.backlog.max_payload_bytes or size > self.backlog.max_task_bytes:
            raise PlanRefused("backlog_full", "Plan initial input exceeds the Task input byte limit")
        namespace_count = sum(
            counters[0]
            for task_id, counters in self._counters.items()
            if task_id in self._tasks and self._tasks[task_id].namespace == namespace
        )
        total_count = sum(counters[0] for counters in self._counters.values())
        if namespace_count >= self.backlog.per_namespace or total_count >= self.backlog.total:
            raise PlanRefused("backlog_full", "Task input backlog has no room for this Plan step")

    def _plan_task_claim_locked(
        self, task_id: str, *, holder: str, ttl_seconds: float, now: float
    ) -> Any:
        from ..tasks.types import DispatchClaim, TaskStatus

        task = self._tasks.get(task_id)
        if task is None or task.status.is_terminal:
            return None
        current = self._claims.get(task_id)
        if current is not None and current[4] > now:
            return None
        token = self._tokens.get(task_id, 1)
        self._tokens[task_id] = token + 1
        claim = DispatchClaim(
            task_id=task_id,
            holder=holder,
            attempt_id=f"{task_id}-a{token}",
            fencing_token=token,
            claimed_at=now,
            expires_at=now + ttl_seconds,
            namespace=task.namespace,
        )
        self._claims[task_id] = (holder, claim.attempt_id, token, now, claim.expires_at)
        if task.status is TaskStatus.WAITING:
            task = task.transition(TaskStatus.QUEUED, reason="external_fact_persisted", at=now)
        if task.status is TaskStatus.QUEUED:
            task = task.transition(
                TaskStatus.RUNNING,
                reason="execution_right_claimed",
                at=now,
                attempt_id=claim.attempt_id,
            )
        self._tasks[task_id] = replace(
            task,
            attempt_count=task.attempt_count + 1,
            current_attempt_id=claim.attempt_id,
        )
        return claim

    def _plan_task_claim_for_holder_locked(self, task_id: str, holder: str) -> Any:
        from ..tasks.types import DispatchClaim

        row = self._claims.get(task_id)
        if row is None or row[0] != holder:
            return None
        task = self._tasks.get(task_id)
        return DispatchClaim(
            task_id=task_id,
            holder=row[0],
            attempt_id=row[1],
            fencing_token=row[2],
            claimed_at=row[3],
            expires_at=row[4],
            namespace="" if task is None else task.namespace,
        )


def _require_plan_version(raw: Any) -> None:
    try:
        version = int(raw)
    except (TypeError, ValueError) as exc:
        raise PlanRefused("store_unsupported", "Plan schema version is not an integer") from exc
    if version != PLAN_CONTROL_SCHEMA_VERSION:
        raise PlanRefused(
            "store_unsupported",
            f"Plan control schema {version} is not supported; this build supports {PLAN_CONTROL_SCHEMA_VERSION}",
        )


__all__ = [
    "MemoryOrchestrationMixin",
    "OrchestrationMixin",
    "PLAN_CONTROL_SCHEMA_VERSION",
    "SqliteOrchestrationMixin",
]
