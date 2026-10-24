"""Optional storage capability for durable plan control facts."""

from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable

from .types import (
    AcceptanceRecord,
    ApprovalRevocation,
    PlanApproval,
    PlanApprovalRequest,
    PlanDefinition,
    PlanEvent,
    PlanOutboxEntry,
    PlanRecord,
    PlanStepCandidate,
    PlanStepClaim,
    PlanStepPage,
    PlanSnapshot,
    StepTaskBinding,
)


@runtime_checkable
class OrchestrationStore(Protocol):
    """Optional Plan capability implemented by the reference task stores.

    These methods are atomic with Task control data when the backend is durable.
    Older task adapters remain valid and simply do not satisfy this protocol.
    """

    async def create_plan(
        self, definition: PlanDefinition, *, request_id: str, principal: str, now: float | None = None
    ) -> PlanRecord: ...

    async def read_plan(self, namespace: str, plan_id: str) -> PlanSnapshot | None: ...

    async def read_plan_revision(
        self, namespace: str, plan_id: str, revision: int
    ) -> PlanDefinition | None: ...

    async def read_ready_steps(
        self,
        namespace: str,
        *,
        limit: int = 64,
        cursor: str | None = None,
        now: float | None = None,
    ) -> PlanStepPage: ...

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
    ) -> PlanStepClaim: ...

    async def retry_step(
        self,
        namespace: str,
        plan_id: str,
        step_id: str,
        *,
        retries_binding_id: str,
        expected_control_version: int,
        request_id: str,
        principal: str,
        holder: str,
        ttl_seconds: float,
        now: float | None = None,
    ) -> PlanStepClaim: ...

    async def read_plan_bindings(
        self, namespace: str, plan_id: str, revision: int | None = None
    ) -> tuple[StepTaskBinding, ...]: ...

    async def read_plan_acceptances(
        self,
        namespace: str,
        plan_id: str,
        *,
        revision: int | None = None,
        binding_id: str | None = None,
    ) -> tuple[AcceptanceRecord, ...]: ...

    async def record_acceptance(
        self,
        acceptance: AcceptanceRecord,
        *,
        expected_control_version: int,
        principal: str,
    ) -> AcceptanceRecord: ...

    async def finalize_plan(
        self,
        namespace: str,
        plan_id: str,
        *,
        expected_control_version: int,
        request_id: str,
        principal: str,
        now: float | None = None,
    ) -> PlanRecord: ...

    async def request_plan_review(
        self, namespace: str, plan_id: str, *, expected_control_version: int,
        request_id: str, principal: str, now: float | None = None,
    ) -> PlanRecord: ...

    async def decide_plan_approval(
        self, namespace: str, plan_id: str, decision: PlanApprovalRequest, *,
        expected_control_version: int, request_id: str, principal: str,
        now_ms: int | None = None,
    ) -> PlanRecord: ...

    async def revoke_plan_approval(
        self, namespace: str, plan_id: str, decision_id: str, *, reason: str,
        expected_control_version: int, request_id: str, principal: str,
        now_ms: int | None = None,
    ) -> PlanRecord: ...

    async def pause_plan(
        self, namespace: str, plan_id: str, *, reason: str,
        expected_control_version: int, request_id: str, principal: str,
        now: float | None = None,
    ) -> PlanRecord: ...

    async def resume_plan(
        self, namespace: str, plan_id: str, *, expected_control_version: int,
        request_id: str, principal: str, now: float | None = None,
    ) -> PlanRecord: ...

    async def request_replan(
        self, namespace: str, plan_id: str, *, reason: str,
        expected_control_version: int, request_id: str, principal: str,
        now: float | None = None,
    ) -> PlanRecord: ...

    async def publish_plan_revision(
        self, definition: PlanDefinition, *, expected_control_version: int,
        request_id: str, principal: str, now: float | None = None,
    ) -> PlanRecord: ...

    async def read_plan_events(
        self, namespace: str, plan_id: str, *, after_control_version: int = 0, limit: int = 100
    ) -> Sequence[PlanEvent]: ...

    async def read_plan_approvals(
        self, namespace: str, plan_id: str
    ) -> tuple[tuple[PlanApproval, ...], tuple[ApprovalRevocation, ...]]: ...

    async def read_plan_outbox(
        self, *, pending_only: bool = True, limit: int = 100
    ) -> Sequence[PlanOutboxEntry]: ...

    async def deliver_plan_outbox(
        self, outbox_id: str, *, at: float | None = None
    ) -> bool: ...

    async def fail_plan_outbox(self, outbox_id: str, *, error: str) -> bool: ...


def require_orchestration(store: object) -> OrchestrationStore:
    """Return an optional orchestration port or refuse without changing the store."""
    required = (
        "create_plan",
        "read_plan",
        "read_plan_revision",
        "read_ready_steps",
        "claim_plan_step",
        "retry_step",
        "read_plan_bindings",
        "read_plan_acceptances",
        "record_acceptance",
        "finalize_plan",
        "request_plan_review",
        "decide_plan_approval",
        "revoke_plan_approval",
        "pause_plan",
        "resume_plan",
        "request_replan",
        "publish_plan_revision",
        "read_plan_events",
        "read_plan_approvals",
        "read_plan_outbox",
        "deliver_plan_outbox",
        "fail_plan_outbox",
    )
    missing = [name for name in required if not callable(getattr(store, name, None))]
    if getattr(store, "orchestration_enabled", None) is False:
        missing.append("orchestration_enabled")
    if missing:
        from .types import PlanRefused

        raise PlanRefused(
            "store_unsupported",
            f"control store lacks orchestration capability: {', '.join(missing)}",
        )
    return store  # type: ignore[return-value]


__all__ = ["OrchestrationStore", "require_orchestration"]
