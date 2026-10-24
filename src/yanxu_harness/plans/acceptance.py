"""Read-only verifier orchestration for bound Plan Task/Run evidence."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Protocol

from ..errors import ContractError
from .codec import canonical_json_bytes
from .types import (
    AcceptanceCriterion,
    AcceptanceRecord,
    AcceptanceResult,
    PlanRefused,
    PlanStatus,
    QuiescenceProof,
    StepTaskBinding,
    freeze_json,
)

MAX_EVIDENCE_BYTES = 256 * 1024
MAX_VERIFIER_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class EvidenceSnapshot:
    """Immutable, bounded evidence collected by the embedding host.

    The host reader owns external artifacts and operation-ledger access. The
    coordinator only consumes this snapshot and never dispatches a tool.
    """

    namespace: str
    plan_id: str
    revision: int
    step_id: str
    binding_id: str
    task_id: str
    run_id: str
    run_version: int
    evidence_refs: tuple[str, ...]
    payload: Mapping[str, Any]
    unresolved_operations: tuple[str, ...] = ()
    source_versions: Mapping[str, str] = field(default_factory=dict)
    read_error: str = ""

    def __post_init__(self) -> None:
        for name in ("namespace", "plan_id", "step_id", "binding_id"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ContractError(f"evidence snapshot requires a non-empty {name}")
        for name in ("task_id", "run_id"):
            if not isinstance(getattr(self, name), str):
                raise ContractError(f"evidence snapshot {name} must be a string")
        if type(self.revision) is not int or self.revision < 1:
            raise ContractError("evidence snapshot revision must be positive")
        if type(self.run_version) is not int or self.run_version < 0:
            raise ContractError("evidence snapshot run_version must be non-negative")
        if not isinstance(self.payload, Mapping):
            raise ContractError("evidence snapshot payload must be a JSON object")
        object.__setattr__(self, "payload", freeze_json(self.payload, where="evidence payload"))
        refs = tuple(self.evidence_refs)
        unresolved = tuple(self.unresolved_operations)
        for label, values in (("evidence_refs", refs), ("unresolved_operations", unresolved)):
            if any(not isinstance(item, str) or not item.strip() for item in values):
                raise ContractError(f"evidence snapshot {label} must contain non-empty strings")
        object.__setattr__(self, "evidence_refs", refs)
        object.__setattr__(self, "unresolved_operations", unresolved)
        versions = dict(self.source_versions)
        if any(not isinstance(key, str) or not key.strip() or not isinstance(value, str) or not value.strip()
               for key, value in versions.items()):
            raise ContractError("evidence source_versions must map non-empty strings to non-empty strings")
        object.__setattr__(self, "source_versions", freeze_json(versions, where="evidence source_versions"))
        if not isinstance(self.read_error, str):
            raise ContractError("evidence read_error must be a string")
        if len(canonical_json_bytes(self._record())) > MAX_EVIDENCE_BYTES:
            raise PlanRefused("evidence_too_large", "evidence snapshot exceeds the configured size limit")

    def _record(self) -> Mapping[str, Any]:
        return {
            "namespace": self.namespace,
            "plan_id": self.plan_id,
            "revision": self.revision,
            "step_id": self.step_id,
            "binding_id": self.binding_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "run_version": self.run_version,
            "evidence_refs": list(self.evidence_refs),
            "payload": self.payload,
            "unresolved_operations": list(self.unresolved_operations),
            "source_versions": self.source_versions,
            "read_error": self.read_error,
        }

    @property
    def source_snapshot_digest(self) -> str:
        return "sha256:" + hashlib.sha256(canonical_json_bytes(self._record())).hexdigest()


@dataclass(frozen=True)
class VerificationDecision:
    result: AcceptanceResult
    reason_code: str = ""

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "result", AcceptanceResult(self.result))
        except (TypeError, ValueError) as exc:
            raise ContractError("verifier returned an unknown acceptance result") from exc
        if not isinstance(self.reason_code, str):
            raise ContractError("verifier reason_code must be a string")


@dataclass(frozen=True)
class ReconcileReport:
    """Bounded summary of evidence settlement and cancellation delivery."""

    scanned: int
    accepted: int = 0
    rejected: int = 0
    inconclusive: int = 0
    quiesced: int = 0
    cancellation_requests_dispatched: int = 0
    pending: tuple[str, ...] = ()
    issues: tuple[tuple[str, str], ...] = ()
    next_cursor: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "scanned", "accepted", "rejected", "inconclusive", "quiesced",
            "cancellation_requests_dispatched",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ContractError(f"reconcile {name} must be a non-negative integer")
        pending = tuple(self.pending)
        if any(not isinstance(item, str) or not item for item in pending):
            raise ContractError("reconcile pending items must be binding ids")
        issues = tuple(tuple(item) for item in self.issues)
        if any(
            len(item) != 2 or not all(isinstance(part, str) and part for part in item)
            for item in issues
        ):
            raise ContractError("reconcile issues must contain a binding id and stable reason")
        object.__setattr__(self, "pending", pending)
        object.__setattr__(self, "issues", issues)
        if self.next_cursor is not None and not isinstance(self.next_cursor, str):
            raise ContractError("reconcile next_cursor must be a string or None")

    @property
    def complete(self) -> bool:
        return self.next_cursor is None


class ReadOnlyVerifier(Protocol):
    async def verify(
        self, criterion: AcceptanceCriterion, evidence: EvidenceSnapshot
    ) -> VerificationDecision: ...


@dataclass(frozen=True)
class _VerifierEntry:
    verifier: Any
    timeout_seconds: float
    max_input_bytes: int


class VerifierRegistry:
    """Host-owned allowlist of bounded, asynchronous, read-only verifiers."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], _VerifierEntry] = {}

    def register(
        self,
        verifier_id: str,
        version: str,
        verifier: ReadOnlyVerifier | Callable[..., Awaitable[VerificationDecision]],
        *,
        timeout_seconds: float = 2.0,
        max_input_bytes: int = MAX_EVIDENCE_BYTES,
    ) -> None:
        if not isinstance(verifier_id, str) or not verifier_id.strip():
            raise ContractError("verifier_id must be non-empty")
        if not isinstance(version, str) or not version.strip():
            raise ContractError("verifier version must be non-empty")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise ContractError("verifier timeout must be a finite positive number")
        if not math.isfinite(float(timeout_seconds)) or not 0 < timeout_seconds <= MAX_VERIFIER_TIMEOUT_SECONDS:
            raise ContractError(f"verifier timeout must be in (0, {MAX_VERIFIER_TIMEOUT_SECONDS}] seconds")
        if type(max_input_bytes) is not int or not 1 <= max_input_bytes <= MAX_EVIDENCE_BYTES:
            raise ContractError(f"verifier max_input_bytes must be in [1, {MAX_EVIDENCE_BYTES}]")
        method = getattr(verifier, "verify", verifier)
        if not callable(method) or not inspect.iscoroutinefunction(method):
            raise ContractError("a verifier must be an asynchronous read-only verify function")
        key = (verifier_id, version)
        if key in self._items:
            raise ContractError(f"verifier {verifier_id!r} version {version!r} is already registered")
        self._items[key] = _VerifierEntry(verifier, float(timeout_seconds), max_input_bytes)

    async def verify(
        self, criterion: AcceptanceCriterion, evidence: EvidenceSnapshot
    ) -> VerificationDecision:
        entry = self._items.get((criterion.verifier_id, criterion.verifier_version))
        if entry is None:
            return VerificationDecision(AcceptanceResult.INCONCLUSIVE, "verifier_unavailable")
        size = len(canonical_json_bytes({"criterion": criterion.to_record(), "evidence": evidence._record()}))
        if size > entry.max_input_bytes:
            return VerificationDecision(AcceptanceResult.INCONCLUSIVE, "verifier_input_too_large")
        method = getattr(entry.verifier, "verify", entry.verifier)
        try:
            result = await asyncio.wait_for(
                method(criterion, evidence), timeout=entry.timeout_seconds
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            return VerificationDecision(AcceptanceResult.INCONCLUSIVE, "verifier_timeout")
        except Exception:
            return VerificationDecision(AcceptanceResult.INCONCLUSIVE, "verifier_error")
        if not isinstance(result, VerificationDecision):
            raise ContractError("a verifier must return VerificationDecision")
        return result


EvidenceReader = Callable[[StepTaskBinding, Any, Any], Awaitable[EvidenceSnapshot]]


class AcceptanceCoordinator:
    """Collect evidence outside storage transactions, then CAS its acceptance."""

    def __init__(
        self,
        store: Any,
        evidence_reader: EvidenceReader,
        verifiers: VerifierRegistry,
        *,
        run_store: Any | None = None,
        evidence_timeout_seconds: float = 10.0,
        cancel_task: Callable[..., Any] | None = None,
    ) -> None:
        if not callable(evidence_reader):
            raise ContractError("AcceptanceCoordinator needs an evidence reader")
        if not isinstance(verifiers, VerifierRegistry):
            raise ContractError("AcceptanceCoordinator needs a VerifierRegistry")
        if isinstance(evidence_timeout_seconds, bool) or not 0 < evidence_timeout_seconds <= 60:
            raise ContractError("evidence_timeout_seconds must be in (0, 60]")
        self._store = store
        self._run_store = store if run_store is None else run_store
        if not callable(getattr(self._run_store, "load_run", None)):
            raise ContractError("AcceptanceCoordinator needs a Run persistence reader")
        self._evidence_reader = evidence_reader
        self._verifiers = verifiers
        self._evidence_timeout = float(evidence_timeout_seconds)
        if cancel_task is not None and not callable(cancel_task):
            raise ContractError("cancel_task must be callable when provided")
        self._cancel_task = cancel_task

    async def accept_step(
        self,
        namespace: str,
        plan_id: str,
        step_id: str,
        *,
        request_id: str,
        principal: str,
        now_ms: int | None = None,
    ) -> tuple[AcceptanceRecord, ...]:
        _validate_request(request_id, principal)
        snapshot = await self._required_plan(namespace, plan_id)
        step = next((item for item in snapshot.definition.steps if item.step_id == step_id), None)
        if step is None:
            raise PlanRefused("unknown_reference", f"unknown step {step_id!r}")
        bindings = await self._store.read_plan_bindings(namespace, plan_id, snapshot.record.current_revision)
        binding = next((item for item in reversed(bindings) if item.step_id == step_id), None)
        if binding is None:
            raise PlanRefused("unknown_reference", "step has no Task/Run binding")
        task, run = await self._completed_sources(binding, snapshot.definition.session_id)
        evidence = await self._read_evidence(binding, task, run)
        self._require_evidence_identity(evidence, binding, task, run)

        decisions: list[VerificationDecision] = []
        for criterion in step.acceptance_criteria:
            decision = self._precheck(step.output_contract, evidence)
            if decision is None:
                decision = await self._verifiers.verify(criterion, evidence)

            decisions.append(decision)

        current_task = await self._store.read_task(binding.task_id)
        current_run = await self._run_store.load_run(binding.run_id)
        current_evidence: EvidenceSnapshot | None = None
        sources_match = current_task == task and current_run == run
        if current_task is not None and current_run is not None:
            current_evidence = await self._read_evidence(binding, current_task, current_run)
            self._require_evidence_identity(current_evidence, binding, current_task, current_run)
            sources_match = (
                sources_match
                and current_evidence.source_snapshot_digest == evidence.source_snapshot_digest
            )
        else:
            sources_match = False
        if not sources_match:
            evidence = current_evidence or evidence
            decisions = [
                VerificationDecision(AcceptanceResult.INCONCLUSIVE, "source_changed")
                for _criterion in step.acceptance_criteria
            ]

        outcomes: list[AcceptanceRecord] = []
        for criterion, decision in zip(step.acceptance_criteria, decisions, strict=True):
            record = _acceptance_record(
                criterion,
                evidence,
                request_id=request_id,
                principal=principal,
                decided_at_ms=_stamp_ms(now_ms),
                result=decision.result,
                reason_code=decision.reason_code,
                scope="step",
            )
            current = await self._required_plan(namespace, plan_id)
            await self._store.record_acceptance(
                record,
                expected_control_version=current.record.control_version,
                principal=principal,
            )
            outcomes.append(record)
        return tuple(outcomes)

    async def reconcile(
        self,
        namespace: str,
        plan_id: str,
        *,
        limit: int = 64,
        cursor: str | None = None,
        principal: str,
        now: float | None = None,
    ) -> ReconcileReport:
        """Settle persisted outcomes and dispatch durable cancellation intent.

        This method never starts a Task. A cancellation callback may ask an existing
        worker to stop; publishing a new revision remains a separate host action.
        """
        _validate_request("reconcile", principal)
        if type(limit) is not int or limit <= 0 or limit > 100:
            raise ContractError("reconcile limit must be in [1, 100]")
        if cursor is not None and (not isinstance(cursor, str) or not cursor):
            raise ContractError("reconcile cursor must be a non-empty binding id or None")
        if now is not None and (
            isinstance(now, bool)
            or not isinstance(now, (int, float))
            or not math.isfinite(float(now))
            or now < 0
        ):
            raise ContractError("reconcile timestamp must be finite and non-negative")

        snapshot = await self._required_plan(namespace, plan_id)
        all_bindings = tuple(
            sorted(
                await self._store.read_plan_bindings(
                    namespace, plan_id, snapshot.record.current_revision
                ),
                key=lambda item: item.binding_id,
            )
        )
        pending_page = tuple(item for item in all_bindings if cursor is None or item.binding_id > cursor)
        page = pending_page[:limit]
        next_cursor = page[-1].binding_id if len(pending_page) > len(page) and page else None
        counts = {"accepted": 0, "rejected": 0, "inconclusive": 0, "quiesced": 0, "dispatched": 0}
        pending: list[str] = []
        issues: list[tuple[str, str]] = []
        dispatch_outcomes: dict[str, bool] = {}
        close_gate = snapshot.record.status in {PlanStatus.REPLANNING, PlanStatus.CANCELLING}
        active_statuses = {"admitted", "active", "awaiting_validation", "inconclusive"}

        for binding in page:
            if binding.binding_status not in active_statuses:
                continue
            task = await self._store.read_task(binding.task_id)
            if task is None:
                pending.append(binding.binding_id)
                issues.append((binding.binding_id, "task_unavailable"))
                continue

            if close_gate and task.cancel_requested_at is not None and not task.status.is_terminal:
                if self._cancel_task is not None:
                    try:
                        delivered = self._cancel_task(binding.task_id, reason=snapshot.record.reason)
                        if inspect.isawaitable(delivered):
                            await delivered
                        counts["dispatched"] += 1
                        dispatch_outcomes[binding.binding_id] = True
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        dispatch_outcomes[binding.binding_id] = False
                        issues.append((binding.binding_id, "cancel_dispatch_failed"))
                else:
                    dispatch_outcomes[binding.binding_id] = False
                    issues.append((binding.binding_id, "cancel_dispatch_unavailable"))
                task = await self._store.read_task(binding.task_id)
                if task is None:
                    pending.append(binding.binding_id)
                    issues.append((binding.binding_id, "task_unavailable"))
                    continue
            elif close_gate and not task.status.is_terminal:
                pending.append(binding.binding_id)
                issues.append((binding.binding_id, "cancellation_request_missing"))
                continue

            run = await self._run_store.load_run(binding.run_id)
            if run is None:
                pending.append(binding.binding_id)
                issues.append((binding.binding_id, "run_unavailable"))
                continue
            run_state = getattr(run, "state", None)
            run_state = run_state if isinstance(run_state, Mapping) else {}
            run_status = str(run_state.get("status") or "")

            if close_gate:
                if not task.status.is_terminal or run_status not in {
                    "completed", "failed", "cancelled", "usage_limited"
                }:
                    pending.append(binding.binding_id)
                    issues.append((binding.binding_id, "execution_not_terminal"))
                    continue
                evidence = await self._read_evidence(binding, task, run)
                try:
                    self._require_evidence_identity(evidence, binding, task, run)
                except PlanRefused:
                    pending.append(binding.binding_id)
                    issues.append((binding.binding_id, "evidence_identity_mismatch"))
                    continue
                if evidence.read_error:
                    pending.append(binding.binding_id)
                    issues.append((binding.binding_id, f"evidence_unavailable:{evidence.read_error}"))
                    continue
                if evidence.unresolved_operations:
                    pending.append(binding.binding_id)
                    issues.append((binding.binding_id, "operation_unknown"))
                    continue
                proof = QuiescenceProof(
                    namespace=binding.namespace,
                    plan_id=binding.plan_id,
                    revision=binding.revision,
                    step_id=binding.step_id,
                    binding_id=binding.binding_id,
                    task_id=task.task_id,
                    run_id=run.run_id,
                    task_status=task.status.value,
                    run_status=run_status,
                    run_version=run.version,
                    source_snapshot_digest=evidence.source_snapshot_digest,
                    unresolved_operations=evidence.unresolved_operations,
                    read_error=evidence.read_error,
                )
                current = await self._required_plan(namespace, plan_id)
                try:
                    settled = await self._store.settle_plan_binding(
                        proof,
                        expected_control_version=current.record.control_version,
                        request_id=_reconcile_request_id(binding.binding_id, proof.source_snapshot_digest),
                        principal=principal,
                        now=now,
                    )
                except PlanRefused as exc:
                    pending.append(binding.binding_id)
                    issues.append((binding.binding_id, exc.reason_code))
                    continue
                if settled.binding_status.startswith("settled_"):
                    counts["quiesced"] += 1
                else:
                    pending.append(binding.binding_id)
                continue

            if task.status.value == "completed" and run_status == "completed":
                evidence = await self._read_evidence(binding, task, run)
                try:
                    self._require_evidence_identity(evidence, binding, task, run)
                except PlanRefused:
                    pending.append(binding.binding_id)
                    issues.append((binding.binding_id, "evidence_identity_mismatch"))
                    continue
                try:
                    records = await self.accept_step(
                        namespace,
                        plan_id,
                        binding.step_id,
                        request_id=_reconcile_request_id(binding.binding_id, evidence.source_snapshot_digest),
                        principal=principal,
                        now_ms=None if now is None else int(now * 1000),
                    )
                except PlanRefused as exc:
                    pending.append(binding.binding_id)
                    issues.append((binding.binding_id, exc.reason_code))
                    continue
                for item in records:
                    counts[item.result.value] += 1
                latest = await self._store.read_plan_bindings(
                    namespace, plan_id, snapshot.record.current_revision
                )
                current_binding = next((item for item in latest if item.binding_id == binding.binding_id), None)
                if current_binding is None or current_binding.binding_status not in {"accepted", "rejected"}:
                    pending.append(binding.binding_id)
                    if current_binding is not None and current_binding.binding_status == "inconclusive":
                        issues.append((binding.binding_id, "acceptance_inconclusive"))
            elif task.status.is_terminal:
                if snapshot.record.status is PlanStatus.EXECUTING:
                    current = await self._required_plan(namespace, plan_id)
                    try:
                        await self._store.pause_plan(
                            namespace,
                            plan_id,
                            reason=f"step_{task.status.value}",
                            expected_control_version=current.record.control_version,
                            request_id=_reconcile_terminal_request_id(binding.binding_id, task.status.value),
                            principal=principal,
                            now=now,
                        )
                    except PlanRefused as exc:
                        issues.append((binding.binding_id, exc.reason_code))
                pending.append(binding.binding_id)
                issues.append((binding.binding_id, "task_terminal_without_acceptance"))
            else:
                pending.append(binding.binding_id)
                issues.append((binding.binding_id, "execution_not_terminal"))

        if close_gate:
            # Cancellation intent is part of the Plan transaction; this outbox is the durable
            # retry point for notifying the host's worker. One event is drained per call, and its
            # target list is bounded by PLAN_MAX_STEPS. Periodic binding reconciliation remains
            # the recovery path after the notification itself has been delivered.
            outbox = await self._store.read_plan_outbox(
                pending_only=True, limit=100, namespace=namespace, plan_id=plan_id
            )
            for entry in outbox:
                if entry.kind not in {"replan_requested", "cancel_requested"}:
                    continue
                raw_binding_ids = entry.payload.get("binding_ids", ())
                raw_task_ids = entry.payload.get("task_ids", ())
                revision = entry.payload.get("revision")
                if (
                    not isinstance(raw_binding_ids, (tuple, list))
                    or not isinstance(raw_task_ids, (tuple, list))
                    or type(revision) is not int
                    or revision < 1
                    or any(not isinstance(item, str) or not item for item in raw_binding_ids)
                    or any(not isinstance(item, str) or not item for item in raw_task_ids)
                ):
                    await self._store.fail_plan_outbox(
                        entry.outbox_id, error="cancellation_targets_invalid"
                    )
                    issues.append(("plan", "cancellation_targets_invalid"))
                    break
                target_bindings = set(raw_binding_ids)
                target_tasks = set(raw_task_ids)
                bindings = await self._store.read_plan_bindings(
                    namespace, plan_id, revision
                )
                by_id = {item.binding_id: item for item in bindings}
                if (
                    not target_bindings.issubset(by_id)
                    or not target_tasks.issubset({by_id[item].task_id for item in target_bindings if item in by_id})
                ):
                    await self._store.fail_plan_outbox(
                        entry.outbox_id, error="cancellation_targets_inconsistent"
                    )
                    issues.append(("plan", "cancellation_targets_inconsistent"))
                    break

                failure = ""
                for binding_id in sorted(target_bindings):
                    binding = by_id[binding_id]
                    task = await self._store.read_task(binding.task_id)
                    if task is None:
                        failure = "task_unavailable"
                        break
                    if task.status.is_terminal:
                        continue
                    if task.cancel_requested_at is None:
                        failure = "cancellation_request_missing"
                        break
                    if dispatch_outcomes.get(binding_id) is True:
                        continue
                    if binding_id in dispatch_outcomes:
                        failure = "cancel_dispatch_failed"
                        break
                    if self._cancel_task is None:
                        failure = "cancel_dispatch_unavailable"
                        break
                    try:
                        delivered = self._cancel_task(
                            binding.task_id, reason=snapshot.record.reason
                        )
                        if inspect.isawaitable(delivered):
                            await delivered
                        dispatch_outcomes[binding_id] = True
                        counts["dispatched"] += 1
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        dispatch_outcomes[binding_id] = False
                        failure = "cancel_dispatch_failed"
                        break
                if failure:
                    await self._store.fail_plan_outbox(entry.outbox_id, error=failure)
                    issues.append(("plan", failure))
                else:
                    await self._store.deliver_plan_outbox(entry.outbox_id, at=now)
                break

        latest = await self._required_plan(namespace, plan_id)
        if latest.record.status is PlanStatus.CANCELLING and not any(
            item.binding_status in active_statuses
            for item in await self._store.read_plan_bindings(
                namespace, plan_id, latest.record.current_revision
            )
        ):
            try:
                await self._store.finalize_plan_cancellation(
                    namespace,
                    plan_id,
                    expected_control_version=latest.record.control_version,
                    request_id=_cancel_finalize_request_id(namespace, plan_id),
                    principal=principal,
                    now=now,
                )
            except PlanRefused as exc:
                issues.append(("plan", exc.reason_code))

        return ReconcileReport(
            scanned=len(page),
            accepted=counts["accepted"],
            rejected=counts["rejected"],
            inconclusive=counts["inconclusive"],
            quiesced=counts["quiesced"],
            cancellation_requests_dispatched=counts["dispatched"],
            pending=tuple(sorted(set(pending))),
            issues=tuple(sorted(set(issues))),
            next_cursor=next_cursor,
        )

    async def finalize_plan(
        self,
        namespace: str,
        plan_id: str,
        *,
        expected_control_version: int,
        request_id: str,
        principal: str,
        now: float | None = None,
    ) -> Any:
        _validate_request(request_id, principal)
        snapshot = await self._required_plan(namespace, plan_id)
        if snapshot.record.status is PlanStatus.COMPLETED:
            return await self._store.finalize_plan(
                namespace,
                plan_id,
                expected_control_version=expected_control_version,
                request_id=request_id,
                principal=principal,
                now=now,
            )
        if snapshot.record.control_version != expected_control_version:
            raise PlanRefused("version_conflict", "Plan control version changed before finalize")
        bindings = await self._store.read_plan_bindings(namespace, plan_id, snapshot.record.current_revision)
        acceptances = await self._store.read_plan_acceptances(
            namespace, plan_id, revision=snapshot.record.current_revision
        )
        by_step: dict[str, list[AcceptanceRecord]] = {}
        for item in acceptances:
            if item.scope == "step":
                by_step.setdefault(item.step_id, []).append(item)
        for step in snapshot.definition.steps:
            binding = next((item for item in reversed(bindings) if item.step_id == step.step_id), None)
            if binding is None or binding.binding_status != "accepted":
                raise PlanRefused("steps_pending", f"step {step.step_id!r} is not accepted")
            current = {item.criterion_id: item for item in by_step.get(step.step_id, ()) if item.binding_id == binding.binding_id}
            if any(
                current.get(criterion.criterion_id) is None
                or current[criterion.criterion_id].result is not AcceptanceResult.ACCEPTED
                for criterion in step.acceptance_criteria
            ):
                raise PlanRefused("steps_pending", f"step {step.step_id!r} has incomplete acceptance")
            task, run = await self._completed_sources(binding, snapshot.definition.session_id)
            current_evidence = await self._read_evidence(binding, task, run)
            self._require_evidence_identity(current_evidence, binding, task, run)
            accepted_digests = {
                item.source_snapshot_digest for item in current.values()
                if item.result is AcceptanceResult.ACCEPTED
            }
            if current_evidence.unresolved_operations:
                raise PlanRefused("unknown_operations", "a bound Run still has unresolved operations")
            if current_evidence.read_error or current_evidence.source_snapshot_digest not in accepted_digests:
                raise PlanRefused("evidence_changed", "the accepted evidence snapshot is no longer current")

        if snapshot.definition.acceptance_criteria:
            plan_binding_id = _plan_binding_id(namespace, plan_id, snapshot.record.current_revision)
            aggregate = _plan_evidence_snapshot(snapshot, bindings, acceptances)
            for criterion in snapshot.definition.acceptance_criteria:
                decision = await self._verifiers.verify(criterion, aggregate)
                record = _acceptance_record(
                    criterion,
                    aggregate,
                    request_id=request_id,
                    principal=principal,
                    decided_at_ms=_stamp_ms(None),
                    result=decision.result,
                    reason_code=decision.reason_code,
                    scope="plan",
                    binding_id=plan_binding_id,
                    step_id="plan",
                )
                current = await self._required_plan(namespace, plan_id)
                await self._store.record_acceptance(
                    record,
                    expected_control_version=current.record.control_version,
                    principal=principal,
                )
                if decision.result is not AcceptanceResult.ACCEPTED:
                    raise PlanRefused("plan_acceptance_pending", decision.reason_code or decision.result.value)

        latest = await self._required_plan(namespace, plan_id)
        return await self._store.finalize_plan(
            namespace,
            plan_id,
            expected_control_version=latest.record.control_version,
            request_id=request_id,
            principal=principal,
            now=now,
        )

    async def _required_plan(self, namespace: str, plan_id: str) -> Any:
        snapshot = await self._store.read_plan(namespace, plan_id)
        if snapshot is None:
            raise PlanRefused("unknown_reference", f"unknown Plan {plan_id!r}")
        return snapshot

    async def _completed_sources(self, binding: StepTaskBinding, session_id: str) -> tuple[Any, Any]:
        task = await self._store.read_task(binding.task_id)
        if task is None or (
            task.namespace != binding.namespace or task.session_id != session_id
            or task.run_id != binding.run_id or task.status.value != "completed"
        ):
            raise PlanRefused("run_not_completed", "bound Task is missing, mismatched, or not completed")
        run = await self._run_store.load_run(binding.run_id)
        if run is None:
            raise PlanRefused("run_not_completed", "the bound Run has no committed outcome")
        state = run.state
        if (
            run.run_id != binding.run_id or state.get("run_id") != binding.run_id
            or state.get("namespace") != binding.namespace
            or state.get("session_id") != session_id or state.get("status") != "completed"
        ):
            raise PlanRefused("run_not_completed", "the persisted Run identity or status does not match the binding")
        return task, run

    async def _read_evidence(self, binding: StepTaskBinding, task: Any, run: Any) -> EvidenceSnapshot:
        try:
            evidence = await asyncio.wait_for(
                self._evidence_reader(binding, task, run), timeout=self._evidence_timeout
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # Evidence-reader failures are inconclusive, not business rejection.
            return EvidenceSnapshot(
                namespace=binding.namespace, plan_id=binding.plan_id, revision=binding.revision,
                step_id=binding.step_id, binding_id=binding.binding_id, task_id=task.task_id,
                run_id=run.run_id, run_version=run.version, evidence_refs=(), payload={},
                read_error=type(exc).__name__,
            )
        if not isinstance(evidence, EvidenceSnapshot):
            raise ContractError("evidence reader must return EvidenceSnapshot")
        return evidence

    @staticmethod
    def _require_evidence_identity(
        evidence: EvidenceSnapshot, binding: StepTaskBinding, task: Any, run: Any
    ) -> None:
        expected = (
            binding.namespace, binding.plan_id, binding.revision, binding.step_id,
            binding.binding_id, task.task_id, run.run_id, run.version,
        )
        observed = (
            evidence.namespace, evidence.plan_id, evidence.revision, evidence.step_id,
            evidence.binding_id, evidence.task_id, evidence.run_id, evidence.run_version,
        )
        if observed != expected:
            raise PlanRefused("evidence_identity_mismatch", "evidence snapshot does not match the current Task/Run binding")

    @staticmethod
    def _precheck(output_contract: Mapping[str, Any] | None, evidence: EvidenceSnapshot) -> VerificationDecision | None:
        if evidence.read_error:
            return VerificationDecision(AcceptanceResult.INCONCLUSIVE, "evidence_unavailable")
        if evidence.unresolved_operations:
            return VerificationDecision(AcceptanceResult.INCONCLUSIVE, "unknown_operations")
        if not evidence.evidence_refs:
            return VerificationDecision(AcceptanceResult.INCONCLUSIVE, "evidence_missing")
        if output_contract is None:
            return None
        allowed = {"required_keys", "equals", "evidence_refs"}
        if set(output_contract) - allowed:
            return VerificationDecision(AcceptanceResult.REJECTED, "output_contract_unsupported")
        for key in output_contract.get("required_keys", ()):
            if not isinstance(key, str) or _lookup_path(evidence.payload, key) is _MISSING:
                return VerificationDecision(AcceptanceResult.REJECTED, "output_contract_failed")
        equals = output_contract.get("equals", {})
        if not isinstance(equals, Mapping):
            return VerificationDecision(AcceptanceResult.REJECTED, "output_contract_invalid")
        for key, expected in equals.items():
            if not isinstance(key, str) or _lookup_path(evidence.payload, key) != expected:
                return VerificationDecision(AcceptanceResult.REJECTED, "output_contract_failed")
        required_refs = output_contract.get("evidence_refs", ())
        if any(not isinstance(ref, str) or ref not in evidence.evidence_refs for ref in required_refs):
            return VerificationDecision(AcceptanceResult.REJECTED, "output_contract_failed")
        return None


def criterion_digest(criterion: AcceptanceCriterion) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(criterion.to_record())).hexdigest()


def _acceptance_record(
    criterion: AcceptanceCriterion,
    evidence: EvidenceSnapshot,
    *,
    request_id: str,
    principal: str,
    decided_at_ms: int,
    result: AcceptanceResult,
    reason_code: str,
    scope: str,
    binding_id: str | None = None,
    step_id: str | None = None,
) -> AcceptanceRecord:
    identity = {
        "namespace": evidence.namespace,
        "plan_id": evidence.plan_id,
        "revision": evidence.revision,
        "step_id": step_id or evidence.step_id,
        "binding_id": binding_id or evidence.binding_id,
        "criterion_id": criterion.criterion_id,
        "criterion_digest": criterion_digest(criterion),
        "source_snapshot_digest": evidence.source_snapshot_digest,
        "verifier_id": criterion.verifier_id,
        "verifier_version": criterion.verifier_version,
        "request_id": request_id,
        "result": result.value,
    }
    acceptance_id = "accept-" + hashlib.sha256(
        b"yanxu-plan-acceptance:v1\0" + canonical_json_bytes(identity)
    ).hexdigest()
    return AcceptanceRecord(
        acceptance_id=acceptance_id,
        namespace=evidence.namespace,
        plan_id=evidence.plan_id,
        revision=evidence.revision,
        step_id=step_id or evidence.step_id,
        binding_id=binding_id or evidence.binding_id,
        criterion_id=criterion.criterion_id,
        verifier_id=criterion.verifier_id,
        verifier_version=criterion.verifier_version,
        criterion_digest=criterion_digest(criterion),
        result=result,
        evidence_refs=evidence.evidence_refs,
        source_snapshot_digest=evidence.source_snapshot_digest,
        decided_by=principal,
        decided_at_ms=decided_at_ms,
        reason_code=reason_code,
        scope=scope,
        run_version=evidence.run_version,
        request_id=request_id,
    )


def _plan_binding_id(namespace: str, plan_id: str, revision: int) -> str:
    raw = canonical_json_bytes({"namespace": namespace, "plan_id": plan_id, "revision": revision})
    return "plan-acceptance-" + hashlib.sha256(raw).hexdigest()


def _reconcile_request_id(binding_id: str, source_digest: str) -> str:
    raw = canonical_json_bytes({"binding_id": binding_id, "source_digest": source_digest})
    return "reconcile-" + hashlib.sha256(raw).hexdigest()


def _reconcile_terminal_request_id(binding_id: str, task_status: str) -> str:
    raw = canonical_json_bytes({"binding_id": binding_id, "task_status": task_status})
    return "reconcile-terminal-" + hashlib.sha256(raw).hexdigest()


def _cancel_finalize_request_id(namespace: str, plan_id: str) -> str:
    raw = canonical_json_bytes({"namespace": namespace, "plan_id": plan_id, "operation": "cancel-finalize:v1"})
    return "cancel-finalize-" + hashlib.sha256(raw).hexdigest()


def _plan_evidence_snapshot(snapshot: Any, bindings: tuple[Any, ...], acceptances: tuple[AcceptanceRecord, ...]) -> EvidenceSnapshot:
    current = [item for item in acceptances if item.scope == "step" and item.result is AcceptanceResult.ACCEPTED]
    refs = tuple(sorted({ref for item in current for ref in item.evidence_refs}))
    payload = {
        "definition_digest": snapshot.record.definition_digest,
        "steps": [
            {
                "step_id": binding.step_id,
                "binding_id": binding.binding_id,
                "acceptances": [
                    {
                        "criterion_id": item.criterion_id,
                        "source_snapshot_digest": item.source_snapshot_digest,
                        "acceptance_id": item.acceptance_id,
                    }
                    for item in sorted(current, key=lambda value: (value.step_id, value.criterion_id))
                    if item.binding_id == binding.binding_id
                ],
            }
            for binding in bindings if binding.binding_status == "accepted"
        ],
    }
    return EvidenceSnapshot(
        namespace=snapshot.record.namespace,
        plan_id=snapshot.record.plan_id,
        revision=snapshot.record.current_revision,
        step_id="plan",
        binding_id=_plan_binding_id(snapshot.record.namespace, snapshot.record.plan_id, snapshot.record.current_revision),
        task_id="",
        run_id="",
        run_version=0,
        evidence_refs=refs,
        payload=payload,
        source_versions={"definition": snapshot.record.definition_digest},
    )


def _validate_request(request_id: str, principal: str) -> None:
    if not isinstance(request_id, str) or not request_id.strip():
        raise ContractError("request_id must be non-empty")
    if not isinstance(principal, str) or not principal.strip():
        raise ContractError("trusted principal must be non-empty")


def _stamp_ms(value: int | None) -> int:
    if value is None:
        return int(time.time() * 1000)
    if type(value) is not int or value < 0:
        raise ContractError("timestamp must be a non-negative integer number of milliseconds")
    return value


_MISSING = object()


def _lookup_path(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return _MISSING
        current = current[part]
    return current


__all__ = [
    "AcceptanceCoordinator",
    "EvidenceSnapshot",
    "MAX_EVIDENCE_BYTES",
    "ReadOnlyVerifier",
    "VerificationDecision",
    "VerifierRegistry",
    "criterion_digest",
]
