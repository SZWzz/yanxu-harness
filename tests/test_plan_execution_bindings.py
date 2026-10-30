from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError

import pytest

from yanxu_harness.errors import ContractError
from yanxu_harness.plans import (
    AcceptanceCriterion,
    ApprovalDecision,
    ExecutionBindingGuardResult,
    ExecutionBindingRefused,
    ExecutionBindingReferences,
    ExecutionBindingRequest,
    ExecutionDeploymentDescriptor,
    ExecutionDispatchBoundary,
    ExecutionEnvelope,
    PlanDefinition,
    PlanApproval,
    ApprovalRevocation,
    PlanRecord,
    PlanSnapshot,
    PlanStatus,
    ResolvedExecutionBinding,
    StepDefinition,
    VersionedRef,
    definition_digest,
    step_binding_id,
)


def _reference(name: str, version: str = "v1") -> VersionedRef:
    return VersionedRef(ref=name, version=version)


def _snapshot(
    *,
    step_overrides: bool = True,
    status: PlanStatus = PlanStatus.APPROVED,
    include_approval: bool = True,
    expires_at_ms: int | None = None,
    revoked: bool = False,
) -> PlanSnapshot:
    envelope = ExecutionEnvelope(
        profile_ref=_reference("profile/default"),
        workspace_ref=_reference("workspace/project"),
        policy_ref=_reference("policy/readonly"),
        deployment_ref=_reference("deployment/local"),
        budget_ref=_reference("budget/standard"),
    )
    step = StepDefinition(
        step_id="step-a",
        title="Inspect source",
        instruction="Inspect the selected source tree.",
        acceptance_criteria=(
            AcceptanceCriterion(
                criterion_id="source-reviewed",
                verifier_id="host.review",
                verifier_version="v1",
                config={"required": True},
            ),
        ),
        execution_profile_ref=_reference("profile/reviewer", "v3") if step_overrides else None,
        workspace_ref=_reference("workspace/checkout", "v8") if step_overrides else None,
    )
    definition = PlanDefinition(
        namespace="ns",
        session_id="session",
        plan_id="plan-1",
        revision=1,
        title="Review source",
        goal="Produce a verifiable review.",
        steps=(step,),
        execution_envelope=envelope,
    )
    digest = definition_digest(definition)
    record = PlanRecord(
        namespace="ns",
        session_id="session",
        plan_id="plan-1",
        current_revision=1,
        control_version=3,
        status=status,
        definition_digest=digest,
        active_approval_id="approval-1" if include_approval else "",
        created_at=1.0,
        updated_at=2.0,
    )
    approvals = (
        PlanApproval(
            namespace="ns",
            plan_id="plan-1",
            revision=1,
            definition_digest=digest,
            execution_envelope_digest=definition.envelope_digest,
            approval_policy_version="policy-v1",
            decision_id="approval-1",
            decision=ApprovalDecision.APPROVED,
            decided_by="host-user/reviewer",
            decided_at_ms=1,
            expires_at_ms=expires_at_ms,
        ),
    ) if include_approval else ()
    revocations = (
        ApprovalRevocation(
            namespace="ns",
            plan_id="plan-1",
            decision_id="approval-1",
            revoked_by="host-user/admin",
            revoked_at_ms=2,
        ),
    ) if revoked else ()
    return PlanSnapshot(record=record, definition=definition, approvals=approvals, revocations=revocations)


def _request(*, step_overrides: bool = True, retries_binding_id: str = "") -> ExecutionBindingRequest:
    return ExecutionBindingRequest.from_snapshot(
        _snapshot(step_overrides=step_overrides),
        step_id="step-a",
        principal_ref="host-user/alice",
        retries_binding_id=retries_binding_id,
        now_ms=10,
    )


def _descriptor(**overrides: object) -> ExecutionDeploymentDescriptor:
    fields: dict[str, object] = {
        "profile_fingerprint": "sha256:profile-config",
        "tool_visibility_fingerprint": "sha256:tool-visibility",
        "policy_fingerprint": "sha256:policy-config",
        "deployment_fingerprint": "sha256:deployment-config",
        "budget_fingerprint": "sha256:budget-config",
        "workspace_identity": "workspace:stable-42",
        "workspace_version": "tree:rev-9",
        "capabilities": ("binding_guard.model_dispatch", "binding_guard.tool_dispatch"),
    }
    fields.update(overrides)
    return ExecutionDeploymentDescriptor(**fields)  # type: ignore[arg-type]


def test_request_uses_effective_step_references_and_deterministic_binding_identity() -> None:
    request = _request()

    assert request.namespace == "ns"
    assert request.session_id == "session"
    assert request.plan_id == "plan-1"
    assert request.revision == 1
    assert request.step_id == "step-a"
    assert request.references == ExecutionBindingReferences(
        profile_ref=_reference("profile/reviewer", "v3"),
        workspace_ref=_reference("workspace/checkout", "v8"),
        policy_ref=_reference("policy/readonly"),
        deployment_ref=_reference("deployment/local"),
        budget_ref=_reference("budget/standard"),
    )
    expected_identity = {
        "identity_version": 1,
        "namespace": "ns",
        "plan_id": "plan-1",
        "revision": 1,
        "step_id": "step-a",
    }
    expected_id = "binding-" + hashlib.sha256(
        json.dumps(expected_identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert request.binding_id == expected_id
    assert request.binding_id == step_binding_id("ns", "plan-1", 1, "step-a")
    assert request.definition_digest == _snapshot().record.definition_digest
    assert request.envelope_digest == _snapshot().definition.envelope_digest
    assert request.principal_ref == "host-user/alice"


def test_request_uses_envelope_defaults_and_retry_identity() -> None:
    request = _request(step_overrides=False, retries_binding_id="binding-prior")
    identity = {
        "identity_version": 2,
        "namespace": "ns",
        "plan_id": "plan-1",
        "revision": 1,
        "step_id": "step-a",
        "retries_binding_id": "binding-prior",
    }

    assert request.references.profile_ref == _reference("profile/default")
    assert request.references.workspace_ref == _reference("workspace/project")
    assert request.retries_binding_id == "binding-prior"
    assert request.binding_id == step_binding_id("ns", "plan-1", 1, "step-a", "binding-prior")
    assert request.binding_id == "binding-" + hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def test_request_refuses_non_deterministic_binding_id_or_unsupported_digest() -> None:
    request = _request()

    with pytest.raises(ContractError):
        ExecutionBindingRequest(
            namespace=request.namespace,
            session_id=request.session_id,
            plan_id=request.plan_id,
            revision=request.revision,
            step_id=request.step_id,
            binding_id="some-other-binding",
            retries_binding_id="",
            definition_digest=request.definition_digest,
            envelope_digest=request.envelope_digest,
            references=request.references,
            principal_ref=request.principal_ref,
        )

    with pytest.raises(ContractError):
        ExecutionBindingRequest(
            namespace=request.namespace,
            session_id=request.session_id,
            plan_id=request.plan_id,
            revision=request.revision,
            step_id=request.step_id,
            binding_id=request.binding_id,
            retries_binding_id="",
            definition_digest="not-a-sha256-digest",
            envelope_digest=request.envelope_digest,
            references=request.references,
            principal_ref=request.principal_ref,
        )


def test_resolved_binding_digest_is_domain_separated_and_covers_actual_deployment() -> None:
    request = _request()
    resolved = ResolvedExecutionBinding(
        request=request,
        resolver_ref=_reference("resolver/local", "2026-10"),
        actual=_descriptor(),
    )
    changed = ResolvedExecutionBinding(
        request=request,
        resolver_ref=_reference("resolver/local", "2026-10"),
        actual=_descriptor(workspace_version="tree:rev-10"),
    )

    expected = "sha256:" + hashlib.sha256(
        b"yanxu-execution-binding:v1\0"
        + json.dumps(resolved.to_record(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert resolved.digest == expected
    assert resolved.digest != changed.digest
    assert resolved.request_digest == request.digest
    assert ResolvedExecutionBinding.from_record(resolved.to_record()).digest == resolved.digest


def test_resolved_binding_is_immutable_and_record_does_not_share_mutable_containers() -> None:
    resolved = ResolvedExecutionBinding(
        request=_request(),
        resolver_ref=_reference("resolver/local", "v1"),
        actual=_descriptor(),
    )
    record = resolved.to_record()
    record["actual"]["capabilities"].append("workspace.unverified")

    assert "workspace.unverified" not in resolved.actual.capabilities
    with pytest.raises(FrozenInstanceError):
        resolved.actual.workspace_version = "mutated"  # type: ignore[misc]


def test_resolved_binding_can_only_be_reused_for_its_exact_request() -> None:
    resolved = ResolvedExecutionBinding(
        request=_request(),
        resolver_ref=_reference("resolver/local", "v1"),
        actual=_descriptor(),
    )
    resolved.assert_request_matches(_request())
    with pytest.raises(ExecutionBindingRefused) as refusal:
        resolved.assert_request_matches(_request(retries_binding_id="binding-prior"))
    assert refusal.value.reason_code == "binding_request_mismatch"


def test_descriptor_and_guard_result_reject_unbounded_or_ambiguous_values() -> None:
    with pytest.raises(ContractError):
        _descriptor(capabilities=("binding_guard.tool_dispatch", "binding_guard.tool_dispatch"))
    with pytest.raises(ContractError):
        _descriptor(workspace_identity=" ")
    with pytest.raises(ContractError):
        ExecutionBindingGuardResult(allowed=False)
    with pytest.raises(ContractError):
        ExecutionBindingGuardResult(allowed=True, reason_code="deployment_drift")

    allowed = ExecutionBindingGuardResult(allowed=True)
    denied = ExecutionBindingGuardResult(
        allowed=False,
        reason_code="deployment_drift",
        detail="the current deployment is outside the approved reference",
    )
    assert allowed.allowed is True
    assert denied.allowed is False
    assert denied.reason_code == "deployment_drift"
    assert ExecutionDispatchBoundary.MODEL_DISPATCH.value == "model_dispatch"


def test_snapshot_mismatch_and_unknown_resolved_schema_are_structured_refusals() -> None:
    snapshot = _snapshot()
    altered_record = PlanRecord(
        namespace=snapshot.record.namespace,
        session_id=snapshot.record.session_id,
        plan_id=snapshot.record.plan_id,
        current_revision=snapshot.record.current_revision,
        control_version=snapshot.record.control_version,
        status=snapshot.record.status,
        definition_digest="0" * 64,
        created_at=snapshot.record.created_at,
        updated_at=snapshot.record.updated_at,
    )
    with pytest.raises(ExecutionBindingRefused) as refusal:
        ExecutionBindingRequest.from_snapshot(
            PlanSnapshot(record=altered_record, definition=snapshot.definition),
            step_id="step-a",
            principal_ref="host-user/alice",
        now_ms=10,
        )
    assert refusal.value.reason_code == "binding_snapshot_mismatch"

    resolved = ResolvedExecutionBinding(
        request=_request(),
        resolver_ref=_reference("resolver/local", "v1"),
        actual=_descriptor(),
    )
    record = resolved.to_record()
    record["schema_version"] = 2
    with pytest.raises(ContractError):
        ResolvedExecutionBinding.from_record(record)


@pytest.mark.parametrize(
    ("snapshot", "now_ms", "reason_code"),
    [
        (_snapshot(include_approval=False), 10, "binding_approval_missing"),
        (_snapshot(expires_at_ms=10), 10, "binding_approval_expired"),
        (_snapshot(revoked=True), 10, "binding_approval_revoked"),
        (_snapshot(status=PlanStatus.PAUSED), 10, "binding_plan_not_admissible"),
    ],
)
def test_request_refuses_unapproved_revoked_expired_or_paused_plan(
    snapshot: PlanSnapshot, now_ms: int, reason_code: str
) -> None:
    with pytest.raises(ExecutionBindingRefused) as refusal:
        ExecutionBindingRequest.from_snapshot(
            snapshot,
            step_id="step-a",
            principal_ref="host-user/alice",
            now_ms=now_ms,
        )
    assert refusal.value.reason_code == reason_code
