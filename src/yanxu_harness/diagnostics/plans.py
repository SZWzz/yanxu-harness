"""Read-only Plan diagnostics built from committed orchestration facts.

This reader does not claim work, consume notifications, reconcile a Plan, or run a
Task. It requires a read-only control connection and publishes a whitelist of
identifiers, statuses, versions and digests; plan instructions, evidence payloads
and arbitrary event payloads are never rendered.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from ..errors import ConfigurationError, ContractError
from ..plans.graph import readiness_for_step
from ..plans.types import AcceptanceResult, PlanStatus, StepOutcome, StepViewStatus
from ..tasks.budgets import BudgetOwnerKind, BudgetOwnerRef
from .budgets import read_typed_budget_view
from .tasks import ClaimFact
from .timeline import TimelineCursor, TimelineInspector, TimelineReport
from .types import ReadBoundary

__all__ = [
    "AcceptanceFact",
    "PlanApprovalFact",
    "PlanDiagnosticReport",
    "PlanEventFact",
    "PlanInspector",
    "PlanOutboxFact",
    "PlanStepFact",
    "plan_report_to_json",
    "plan_report_to_markdown",
]

PLAN_DIAGNOSTIC_SCHEMA_VERSION = 2


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AcceptanceFact:
    acceptance_id: str
    criterion_id: str
    result: str
    scope: str
    binding_id: str
    run_version: int
    decided_at_ms: int
    evidence_count: int
    evidence_refs_digest: str

    def to_record(self) -> Mapping[str, Any]:
        return {
            "acceptance_id": self.acceptance_id,
            "criterion_id": self.criterion_id,
            "result": self.result,
            "scope": self.scope,
            "binding_id": self.binding_id,
            "run_version": self.run_version,
            "decided_at_ms": self.decided_at_ms,
            "evidence_count": self.evidence_count,
            "evidence_refs_digest": self.evidence_refs_digest,
        }


@dataclass(frozen=True)
class PlanApprovalFact:
    decision_id: str
    decision: str
    revision: int
    approval_policy_version: str
    decided_by: str
    decided_at_ms: int
    expires_at_ms: int | None
    revoked: bool
    covers_current_definition: bool
    active: bool

    def to_record(self) -> Mapping[str, Any]:
        return {
            "decision_id": self.decision_id,
            "decision": self.decision,
            "revision": self.revision,
            "approval_policy_version": self.approval_policy_version,
            "decided_by": self.decided_by,
            "decided_at_ms": self.decided_at_ms,
            "expires_at_ms": self.expires_at_ms,
            "revoked": self.revoked,
            "covers_current_definition": self.covers_current_definition,
            "active": self.active,
        }


@dataclass(frozen=True)
class PlanEventFact:
    event_id: str
    control_version: int
    event_type: str
    created_at: float
    payload_digest: str

    def to_record(self) -> Mapping[str, Any]:
        return {
            "event_id": self.event_id,
            "control_version": self.control_version,
            "event_type": self.event_type,
            "created_at": self.created_at,
            "payload_digest": self.payload_digest,
        }


@dataclass(frozen=True)
class PlanOutboxFact:
    outbox_id: str
    event_id: str
    kind: str
    pending: bool
    attempts: int
    created_at: float
    delivered_at: float | None
    payload_digest: str

    def to_record(self) -> Mapping[str, Any]:
        return {
            "outbox_id": self.outbox_id,
            "event_id": self.event_id,
            "kind": self.kind,
            "pending": self.pending,
            "attempts": self.attempts,
            "created_at": self.created_at,
            "delivered_at": self.delivered_at,
            "payload_digest": self.payload_digest,
        }


@dataclass(frozen=True)
class PlanStepFact:
    step_id: str
    title: str
    status: str
    depends_on: tuple[str, ...] = ()
    blocking_reason: str = ""
    blocked_by: tuple[str, ...] = ()
    binding_id: str = ""
    binding_status: str = ""
    task_id: str = ""
    task_status: str = ""
    run_id: str = ""
    run_status: str = ""
    run_version: int | None = None
    attempt_count: int | None = None
    current_attempt_id: str = ""
    claim: ClaimFact | None = None
    acceptances: tuple[AcceptanceFact, ...] = ()

    def to_record(self) -> Mapping[str, Any]:
        return {
            "step_id": self.step_id,
            "title": self.title,
            "status": self.status,
            "depends_on": list(self.depends_on),
            "blocking_reason": self.blocking_reason,
            "blocked_by": list(self.blocked_by),
            "binding_id": self.binding_id,
            "binding_status": self.binding_status,
            "task_id": self.task_id,
            "task_status": self.task_status,
            "run_id": self.run_id,
            "run_status": self.run_status,
            "run_version": self.run_version,
            "attempt_count": self.attempt_count,
            "current_attempt_id": self.current_attempt_id,
            "claim": None if self.claim is None else self.claim.to_record(),
            "acceptances": [item.to_record() for item in self.acceptances],
        }


@dataclass(frozen=True)
class PlanDiagnosticReport:
    namespace: str
    session_id: str
    plan_id: str
    revision: int
    control_version: int
    status: str
    definition_digest: str
    active_approval_id: str
    approval_coverage: tuple[PlanApprovalFact, ...] = ()
    steps: tuple[PlanStepFact, ...] = ()
    budget: Mapping[str, Any] = field(default_factory=dict)
    events: tuple[PlanEventFact, ...] = ()
    outbox: tuple[PlanOutboxFact, ...] = ()
    pending_notifications: int | None = None
    unsupported: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()
    inconsistent: tuple[str, ...] = ()
    boundary: ReadBoundary = field(default_factory=ReadBoundary)
    schema_version: int = PLAN_DIAGNOSTIC_SCHEMA_VERSION

    def to_record(self) -> Mapping[str, Any]:
        return {
            "schema_version": self.schema_version,
            "namespace": self.namespace,
            "session_id": self.session_id,
            "plan_id": self.plan_id,
            "revision": self.revision,
            "control_version": self.control_version,
            "status": self.status,
            "definition_digest": self.definition_digest,
            "active_approval_id": self.active_approval_id,
            "approval_coverage": [item.to_record() for item in self.approval_coverage],
            "steps": [item.to_record() for item in self.steps],
            "budget": dict(self.budget),
            "events": [item.to_record() for item in self.events],
            "outbox": [item.to_record() for item in self.outbox],
            "pending_notifications": self.pending_notifications,
            "unsupported": list(self.unsupported),
            "unknown": list(self.unknown),
            "inconsistent": list(self.inconsistent),
            "boundary": self.boundary.to_record(),
        }


class PlanInspector:
    """Explain one Plan from committed facts through an already read-only store."""

    def __init__(
        self,
        *,
        control: Any,
        event_limit: int = 100,
        outbox_limit: int = 100,
        clock: Any = None,
        linked_timeline: TimelineInspector | None = None,
    ) -> None:
        if control is None:
            raise ContractError("a Plan inspector needs a control store to read")
        if not getattr(control, "read_only", False):
            raise ConfigurationError(
                "the Plan inspector only reads: open the control store with read_only=True"
            )
        if type(event_limit) is not int or event_limit <= 0 or event_limit > 1000:
            raise ContractError("Plan event limit must be in [1, 1000]")
        if type(outbox_limit) is not int or outbox_limit <= 0 or outbox_limit > 1000:
            raise ContractError("Plan outbox limit must be in [1, 1000]")
        self._control = control
        self._event_limit = event_limit
        self._outbox_limit = outbox_limit
        self._clock = clock or time.time
        self._linked_timeline = linked_timeline

    async def inspect_linked_timeline(
        self,
        identities: Mapping[str, Any],
        *,
        cursor: TimelineCursor | None = None,
        limit: int = 100,
    ) -> TimelineReport:
        """Read a correlated page through an optional, separately scoped read-only port."""
        if self._linked_timeline is None:
            raise ConfigurationError("no linked timeline read port was configured")
        return await self._linked_timeline.inspect_page(identities, cursor=cursor, limit=limit)

    async def inspect_plan(
        self,
        plan_id: str,
        *,
        namespace: str,
        session_id: str,
        now: float | None = None,
    ) -> PlanDiagnosticReport:
        if not isinstance(plan_id, str) or not plan_id.strip():
            raise ContractError("plan_id must be non-empty")
        if not isinstance(namespace, str) or not namespace.strip():
            raise ContractError("expected namespace must be non-empty")
        if not isinstance(session_id, str) or not session_id.strip():
            raise ContractError("expected session_id must be non-empty")
        at = self._clock() if now is None else now
        if isinstance(at, bool) or not isinstance(at, (int, float)):
            raise ContractError("diagnostic time must be finite")

        read_plan = getattr(self._control, "read_plan", None)
        if not callable(read_plan):
            raise ConfigurationError("the control store cannot read Plan snapshots")
        snapshot = await read_plan(namespace, plan_id)
        if snapshot is None:
            raise ContractError(f"no Plan {plan_id!r} in namespace {namespace!r}")
        record = snapshot.record
        definition = snapshot.definition
        if record.namespace != namespace:
            raise ContractError("Plan namespace does not match the requested identity")
        if record.session_id != session_id:
            raise ContractError("Plan session does not match the requested identity")

        unsupported: list[str] = []
        unknown: list[str] = []
        inconsistent: list[str] = []
        all_bindings = await self._call_optional(
            getattr(self._control, "read_plan_bindings", None),
            namespace,
            plan_id,
            unsupported=unsupported,
            message="Plan bindings are not readable",
        ) or ()

        acceptances = await self._call_optional(
            getattr(self._control, "read_plan_acceptances", None),
            namespace,
            plan_id,
            revision=record.current_revision,
            unsupported=unsupported,
            message="acceptance records are not readable",
        ) or ()
        by_binding: dict[str, list[Any]] = {}
        for item in acceptances:
            by_binding.setdefault(item.binding_id, []).append(item)

        bindings_by_step: dict[str, list[Any]] = {}
        valid_bindings: list[Any] = []
        for binding in all_bindings:
            if binding.namespace != namespace or binding.plan_id != plan_id:
                inconsistent.append("a binding belongs to a different Plan identity")
                continue
            valid_bindings.append(binding)
            if binding.revision == record.current_revision:
                bindings_by_step.setdefault(binding.step_id, []).append(binding)
        for rows in bindings_by_step.values():
            rows.sort(key=lambda item: (item.created_at, item.binding_id))

        execution_limit_reached = len(valid_bindings) >= definition.max_step_executions
        concurrency_limit_reached = sum(
            item.binding_status in {"admitted", "active"} for item in valid_bindings
        ) >= definition.max_concurrency

        step_outcomes: dict[str, StepOutcome] = {}
        for step in definition.steps:
            rows = bindings_by_step.get(step.step_id, ())
            latest = rows[-1] if rows else None
            status = str(getattr(latest, "binding_status", ""))
            if status == "accepted":
                step_outcomes[step.step_id] = StepOutcome.ACCEPTED
            elif status == "rejected":
                step_outcomes[step.step_id] = StepOutcome.REJECTED
            elif status == "inconclusive":
                step_outcomes[step.step_id] = StepOutcome.INCONCLUSIVE
            elif status in {"settled_failed", "settled_cancelled", "settled_usage_limited"}:
                step_outcomes[step.step_id] = StepOutcome.FAILED
            else:
                step_outcomes[step.step_id] = StepOutcome.PENDING

        claims_reader = getattr(self._control, "read_claim", None)
        task_reader = getattr(self._control, "read_task", None)
        run_reader = getattr(self._control, "load_run", None)
        if not callable(task_reader):
            unsupported.append("bound Task status and attempt facts are not readable")
        if not callable(run_reader):
            unsupported.append("bound Run status and revision facts are not readable")
        if not callable(claims_reader):
            unsupported.append("active Task claims are not readable")

        step_facts: list[PlanStepFact] = []
        accepted_latest = {
            step_id: outcome for step_id, outcome in step_outcomes.items()
        }
        for step in definition.steps:
            rows = bindings_by_step.get(step.step_id, ())
            binding = rows[-1] if rows else None
            claim_fact = None
            task_status = ""
            run_status = ""
            run_version = None
            attempt_count = None
            current_attempt_id = ""
            task = None
            if binding is not None:
                if callable(task_reader):
                    try:
                        task = await task_reader(binding.task_id)
                    except Exception as exc:  # read coverage remains explicit and bounded
                        unknown.append(f"task:{binding.task_id}:{type(exc).__name__}")
                    if task is None:
                        unknown.append(f"task_missing:{binding.task_id}")
                    else:
                        task_status = task.status.value
                        attempt_count = task.attempt_count
                        current_attempt_id = task.current_attempt_id
                        if (
                            task.namespace != namespace
                            or task.session_id != session_id
                            or task.run_id != binding.run_id
                        ):
                            inconsistent.append(f"task_binding_mismatch:{binding.binding_id}")
                if callable(claims_reader):
                    try:
                        claim = await claims_reader(binding.task_id)
                    except Exception as exc:
                        unknown.append(f"claim:{binding.task_id}:{type(exc).__name__}")
                        claim = None
                    if claim is not None:
                        claim_fact = ClaimFact(
                            holder=claim.holder,
                            attempt_id=claim.attempt_id,
                            fencing_token=claim.fencing_token,
                            claimed_at=claim.claimed_at,
                            expires_at=claim.expires_at,
                            live=claim.expires_at > float(at),
                        )
                if callable(run_reader):
                    try:
                        run = await run_reader(binding.run_id)
                    except Exception as exc:
                        unknown.append(f"run:{binding.run_id}:{type(exc).__name__}")
                        run = None
                    if run is None:
                        unknown.append(f"run_missing:{binding.run_id}")
                    else:
                        state = getattr(run, "state", None)
                        state = state if isinstance(state, Mapping) else {}
                        run_status = str(state.get("status") or "")
                        run_version = getattr(run, "version", None)
                        if (
                            getattr(run, "run_id", binding.run_id) != binding.run_id
                            or state.get("run_id", binding.run_id) != binding.run_id
                            or state.get("namespace", namespace) != namespace
                            or state.get("session_id", session_id) != session_id
                        ):
                            inconsistent.append(f"run_binding_mismatch:{binding.binding_id}")

            readiness = readiness_for_step(step.step_id, definition.steps, step_outcomes)
            if binding is None:
                if not readiness.ready:
                    view_status = StepViewStatus.BLOCKED.value
                    blocking_reason = readiness.reason
                    blocked_by = readiness.blocked_by
                elif record.status not in {PlanStatus.APPROVED, PlanStatus.EXECUTING}:
                    view_status = StepViewStatus.BLOCKED.value
                    blocking_reason = f"plan_{record.status.value}"
                    blocked_by = ()
                elif execution_limit_reached:
                    view_status = StepViewStatus.BLOCKED.value
                    blocking_reason = "step_execution_limit"
                    blocked_by = ()
                elif concurrency_limit_reached:
                    view_status = StepViewStatus.BLOCKED.value
                    blocking_reason = "plan_concurrency_limit"
                    blocked_by = ()
                else:
                    view_status = StepViewStatus.READY.value
                    blocking_reason = ""
                    blocked_by = ()
            else:
                view_status, blocking_reason = self._binding_view_status(
                    binding.binding_status, task_status, run_status
                )
                blocked_by = ()
                if binding.binding_status == "accepted":
                    accepted_latest[step.step_id] = StepOutcome.ACCEPTED

            facts = []
            for acceptance in sorted(
                by_binding.get("" if binding is None else binding.binding_id, ()),
                key=lambda item: (item.decided_at_ms, item.acceptance_id),
            ):
                facts.append(
                    AcceptanceFact(
                        acceptance_id=acceptance.acceptance_id,
                        criterion_id=acceptance.criterion_id,
                        result=acceptance.result.value,
                        scope=acceptance.scope,
                        binding_id=acceptance.binding_id,
                        run_version=acceptance.run_version,
                        decided_at_ms=acceptance.decided_at_ms,
                        evidence_count=len(acceptance.evidence_refs),
                        evidence_refs_digest=_digest(list(acceptance.evidence_refs)),
                    )
                )
            step_facts.append(
                PlanStepFact(
                    step_id=step.step_id,
                    title=step.title,
                    status=view_status,
                    depends_on=tuple(step.depends_on),
                    blocking_reason=blocking_reason,
                    blocked_by=tuple(blocked_by),
                    binding_id="" if binding is None else binding.binding_id,
                    binding_status="" if binding is None else binding.binding_status,
                    task_id="" if binding is None else binding.task_id,
                    task_status=task_status,
                    run_id="" if binding is None else binding.run_id,
                    run_status=run_status,
                    run_version=run_version,
                    attempt_count=attempt_count,
                    current_attempt_id=current_attempt_id,
                    claim=claim_fact,
                    acceptances=tuple(facts),
                )
            )

        revoked_ids = {item.decision_id for item in snapshot.revocations}
        approval_facts = tuple(
            PlanApprovalFact(
                decision_id=item.decision_id,
                decision=item.decision.value,
                revision=item.revision,
                approval_policy_version=item.approval_policy_version,
                decided_by=item.decided_by,
                decided_at_ms=item.decided_at_ms,
                expires_at_ms=item.expires_at_ms,
                revoked=item.decision_id in revoked_ids,
                covers_current_definition=(
                    item.revision == record.current_revision
                    and item.definition_digest == record.definition_digest
                    and item.execution_envelope_digest == definition.envelope_digest
                ),
                active=(
                    item.decision_id == record.active_approval_id
                    and item.decision.value == "approved"
                    and item.decision_id not in revoked_ids
                    and item.revision == record.current_revision
                    and item.definition_digest == record.definition_digest
                    and item.execution_envelope_digest == definition.envelope_digest
                    and (item.expires_at_ms is None or item.expires_at_ms > int(float(at) * 1000))
                ),
            )
            for item in snapshot.approvals
        )

        event_reader = getattr(self._control, "read_plan_events", None)
        events: tuple[PlanEventFact, ...] = ()
        event_cursor = max(0, record.control_version - self._event_limit)
        if callable(event_reader):
            try:
                rows = tuple(
                    await event_reader(
                        namespace,
                        plan_id,
                        after_control_version=event_cursor,
                        limit=self._event_limit,
                    )
                )
                events = tuple(
                    PlanEventFact(
                        event_id=item.event_id,
                        control_version=item.control_version,
                        event_type=item.event_type,
                        created_at=item.created_at,
                        payload_digest=_digest(dict(item.payload)),
                    )
                    for item in rows
                )
            except Exception as exc:
                unsupported.append(f"Plan events could not be read ({type(exc).__name__})")
        else:
            unsupported.append("Plan control events are not readable")

        outbox_reader = getattr(self._control, "read_plan_outbox", None)
        outbox_facts: tuple[PlanOutboxFact, ...] = ()
        pending_notifications: int | None = None
        if callable(outbox_reader):
            try:
                rows = tuple(
                    await outbox_reader(
                        pending_only=False,
                        limit=self._outbox_limit,
                        namespace=namespace,
                        plan_id=plan_id,
                    )
                )
                outbox_facts = tuple(
                    PlanOutboxFact(
                        outbox_id=item.outbox_id,
                        event_id=item.event_id,
                        kind=item.kind,
                        pending=item.pending,
                        attempts=item.attempts,
                        created_at=item.created_at,
                        delivered_at=item.delivered_at,
                        payload_digest=_digest(dict(item.payload)),
                    )
                    for item in rows
                )
                pending_notifications = sum(item.pending for item in rows)
            except Exception as exc:
                unsupported.append(f"Plan outbox could not be read ({type(exc).__name__})")
        else:
            unsupported.append("Plan notification outbox is not readable")

        typed_budget = await read_typed_budget_view(
            self._control,
            owners=(BudgetOwnerRef(BudgetOwnerKind.PLAN, namespace, plan_id),),
        )
        budget = {
            "max_step_executions": definition.max_step_executions,
            "max_concurrency": definition.max_concurrency,
            "deadline_at_ms": definition.deadline_at_ms,
            "references": {
                name: getattr(definition.execution_envelope, name).to_record()
                for name in (
                    "profile_ref", "workspace_ref", "policy_ref", "deployment_ref", "budget_ref"
                )
            },
            "quantitative_host_budget": typed_budget.to_record(),
        }
        if not typed_budget.available:
            unsupported.append(
                "typed budget diagnostics are unavailable: " + typed_budget.reason
            )
        unsupported.append(
            "tool operation unknown-state coverage belongs to the host execution ledger and is not read by this control-store inspector"
        )

        truncated = len(events) == self._event_limit
        boundary = ReadBoundary(
            cursor=event_cursor,
            high_water=record.control_version,
            next_cursor=events[-1].control_version if truncated and events else None,
            scanned_pages=1,
            truncated=truncated,
            scan_limit_hit=truncated,
            exhausted=not truncated,
            non_atomic=True,
        )
        return PlanDiagnosticReport(
            namespace=namespace,
            session_id=session_id,
            plan_id=plan_id,
            revision=record.current_revision,
            control_version=record.control_version,
            status=record.status.value,
            definition_digest=record.definition_digest,
            active_approval_id=record.active_approval_id,
            approval_coverage=approval_facts,
            steps=tuple(step_facts),
            budget=budget,
            events=events,
            outbox=outbox_facts,
            pending_notifications=pending_notifications,
            unsupported=tuple(sorted(set(unsupported))),
            unknown=tuple(sorted(set(unknown))),
            inconsistent=tuple(sorted(set(inconsistent))),
            boundary=boundary,
        )

    async def _call_optional(
        self,
        reader: Any,
        *args: Any,
        unsupported: list[str],
        message: str,
        **kwargs: Any,
    ) -> Any:
        if not callable(reader):
            unsupported.append(message)
            return None
        try:
            return await reader(*args, **kwargs)
        except Exception as exc:
            unsupported.append(f"{message} ({type(exc).__name__})")
            return None

    @staticmethod
    def _binding_view_status(binding_status: str, task_status: str, run_status: str) -> tuple[str, str]:
        if binding_status == "accepted":
            return StepViewStatus.ACCEPTED.value, ""
        if binding_status == "rejected":
            return StepViewStatus.REJECTED.value, "acceptance_rejected"
        if binding_status in {"inconclusive", "awaiting_validation", "settled_completed"}:
            return StepViewStatus.AWAITING_VALIDATION.value, "acceptance_pending"
        if binding_status == "settled_failed":
            return StepViewStatus.FAILED.value, "task_failed"
        if binding_status == "settled_cancelled":
            return StepViewStatus.CANCELLED.value, "task_cancelled"
        if binding_status == "superseded":
            return StepViewStatus.SUPERSEDED.value, "revision_superseded"
        if task_status == "waiting":
            return StepViewStatus.WAITING.value, "task_waiting"
        if task_status in {"queued", "running", "cancelling"} or run_status == "running":
            return StepViewStatus.RUNNING.value, ""
        if binding_status in {"admitted", "active"}:
            return StepViewStatus.READY.value, ""
        return StepViewStatus.BLOCKED.value, "binding_status_unknown"


def plan_report_to_json(report: PlanDiagnosticReport) -> str:
    return json.dumps(report.to_record(), indent=2, sort_keys=True, ensure_ascii=False)


def plan_report_to_markdown(report: PlanDiagnosticReport) -> str:
    lines = [
        f"# Plan `{report.plan_id}`",
        "",
        f"- Namespace: `{report.namespace}`",
        f"- Session: `{report.session_id}`",
        f"- Revision / control version: `{report.revision}` / `{report.control_version}`",
        f"- Status: **{report.status}**",
        f"- Definition digest: `{report.definition_digest}`",
        f"- Active approval: `{report.active_approval_id or 'none'}`",
        "",
        "## Steps",
        "",
        "| Step | Status | Dependencies | Blocking reason | Task / Run | Attempt |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for item in report.steps:
        deps = ", ".join(item.depends_on) or "—"
        reason = item.blocking_reason or "—"
        task_run = f"{item.task_id or '—'} / {item.run_id or '—'}"
        attempt = item.current_attempt_id or "—"
        lines.append(
            f"| {item.step_id} | {item.status} | {deps} | {reason} | {task_run} | {attempt} |"
        )
    lines.extend(["", "## Approval coverage", "", "| Decision | Result | Revision | Current definition | Active |", "| --- | --- | ---: | --- | --- |"])
    for item in report.approval_coverage:
        lines.append(
            f"| {item.decision_id} | {item.decision} | {item.revision} | "
            f"{str(item.covers_current_definition).lower()} | {str(item.active).lower()} |"
        )
    lines.extend(["", "## Budget references", "", "```json", json.dumps(report.budget, indent=2, sort_keys=True), "```", ""])
    lines.extend(["## Recent events", "", "| Version | Event | Time | Payload digest |", "| ---: | --- | ---: | --- |"])
    for event in report.events:
        lines.append(f"| {event.control_version} | {event.event_type} | {event.created_at:.3f} | `{event.payload_digest}` |")
    lines.append("")
    for heading, values in (
        ("Unsupported coverage", report.unsupported),
        ("Unknown facts", report.unknown),
        ("Inconsistencies", report.inconsistent),
    ):
        lines.extend([f"## {heading}", ""])
        lines.extend(f"- {item}" for item in values) if values else lines.append("- None recorded.")
        lines.append("")
    return "\n".join(lines).rstrip()
