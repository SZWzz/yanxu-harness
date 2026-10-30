from __future__ import annotations

import hashlib

import pytest

from yanxu_harness.context import (
    StepContextLimits,
    StepContextManifest,
    StepContextRefused,
    StepMaterialDecision,
    StepMaterialDisposition,
    StepMaterialKind,
    StepMaterialRef,
    StepMaterialSource,
    StepMaterialTrust,
    StepMaterialValidity,
)
from yanxu_harness.context.artifacts import ArtifactRef
from yanxu_harness.errors import ContractError


def _material(
    content: str,
    *,
    source: StepMaterialSource = StepMaterialSource.HOST,
    kind: StepMaterialKind = StepMaterialKind.HOST_FACT,
    trust: StepMaterialTrust = StepMaterialTrust.EVIDENCE,
    validity: StepMaterialValidity = StepMaterialValidity.CURRENT,
    required: bool = False,
    source_id: str = "fact-1",
    authorization_ref: str = "authz-1",
) -> StepMaterialRef:
    return StepMaterialRef.from_content(
        namespace="ns",
        scope="project-a",
        source=source,
        kind=kind,
        source_id=source_id,
        source_version="v1",
        authorization_ref=authorization_ref,
        content=content,
        required=required,
        trust=trust,
        validity=validity,
    )


def _manifest_for(
    materials: tuple[StepMaterialRef, ...],
    *,
    accepted_predecessor_refs: tuple[str, ...] = (),
    budget: dict[str, object] | None = None,
    limits: StepContextLimits | None = None,
) -> StepContextManifest:
    return StepContextManifest(
        namespace="ns",
        plan_id="plan-1",
        plan_revision=1,
        plan_definition_digest="sha256:" + "b" * 64,
        step_id="step-1",
        binding_id="binding-1",
        materials=materials,
        decisions=tuple(
            StepMaterialDecision(
                material_id=material.material_id,
                disposition=StepMaterialDisposition.KEPT,
                reason_code="included",
            )
            for material in materials
        ),
        accepted_predecessor_refs=accepted_predecessor_refs,
        limits=limits or StepContextLimits(),
        assembler_version="assembler-v1",
        policy_version="policy-v1",
        estimator_version="estimator-v1",
        budget=budget or {},
        rendered_context_digest="sha256:" + "c" * 64,
        created_at=10.0,
    )


def test_step_material_binds_source_scope_authorization_and_content_digest() -> None:
    material = _material("host-provided fact", required=True)
    expected = "sha256:" + hashlib.sha256(b"host-provided fact").hexdigest()

    assert material.source_digest == expected
    assert material.byte_length == len(b"host-provided fact")
    assert material.valid_at(10.0)
    assert material.to_record()["authorization_ref"] == "authz-1"
    assert "content" not in material.to_record()


def test_step_material_requires_exactly_one_content_or_artifact_reference() -> None:
    artifact = ArtifactRef(
        artifact_id="sha256:" + "a" * 64,
        byte_length=1024,
        media_type="text/plain",
    )
    with pytest.raises(ContractError, match="exactly one"):
        StepMaterialRef(
            namespace="ns",
            scope="project-a",
            source=StepMaterialSource.ARTIFACT,
            kind=StepMaterialKind.HOST_FACT,
            source_id="fact-1",
            source_version="v1",
            source_digest=artifact.artifact_id,
            authorization_ref="authz-1",
        )
    with pytest.raises(ContractError, match="exactly one"):
        StepMaterialRef(
            namespace="ns",
            scope="project-a",
            source=StepMaterialSource.ARTIFACT,
            kind=StepMaterialKind.HOST_FACT,
            source_id="fact-1",
            source_version="v1",
            source_digest=artifact.artifact_id,
            authorization_ref="authz-1",
            content="inline",
            artifact_ref=artifact,
        )


def test_material_provenance_cannot_turn_model_inference_into_control_or_output_into_instruction() -> None:
    with pytest.raises(ContractError, match="unverified"):
        _material(
            "model guess",
            source=StepMaterialSource.MODEL,
            kind=StepMaterialKind.MODEL_INFERENCE,
            trust=StepMaterialTrust.EVIDENCE,
        )
    with pytest.raises(ContractError, match="approved control"):
        _material(
            "accepted output",
            source=StepMaterialSource.ACCEPTANCE,
            kind=StepMaterialKind.ACCEPTED_OUTPUT,
            trust=StepMaterialTrust.APPROVED_CONTROL,
        )

    inferred = _material(
        "model guess",
        source=StepMaterialSource.MODEL,
        kind=StepMaterialKind.MODEL_INFERENCE,
        trust=StepMaterialTrust.UNVERIFIED,
        validity=StepMaterialValidity.UNVERIFIED,
    )
    accepted = _material(
        "accepted output",
        source=StepMaterialSource.ACCEPTANCE,
        kind=StepMaterialKind.ACCEPTED_OUTPUT,
        trust=StepMaterialTrust.EVIDENCE,
        source_id="acceptance-1",
    )
    assert not inferred.valid_at(10.0)
    assert accepted.trust is StepMaterialTrust.EVIDENCE
    assert not hasattr(accepted, "tool_permissions")


def test_required_material_limits_refuse_structurally_without_silent_truncation() -> None:
    required = _material("x" * 20, required=True)
    limits = StepContextLimits(max_items=2, max_item_bytes=16, max_total_bytes=32)

    with pytest.raises(StepContextRefused) as refusal:
        limits.check_required((required,))
    assert refusal.value.reason_code == "required_material_too_large"
    assert refusal.value.to_record()["network_dispatch_allowed"] is False


def test_manifest_requires_a_decision_for_every_material_and_keeps_required_refs() -> None:
    required = _material("must remain", required=True, source_id="required-1")
    optional = _material("optional", source_id="optional-1")
    with pytest.raises(StepContextRefused, match="required"):
        StepContextManifest(
            namespace="ns",
            plan_id="plan-1",
            plan_revision=1,
            plan_definition_digest="sha256:" + "b" * 64,
            step_id="step-1",
            binding_id="binding-1",
            materials=(required, optional),
            decisions=(
                StepMaterialDecision(
                    material_id=required.material_id,
                    disposition=StepMaterialDisposition.DROPPED,
                    reason_code="budget_optional",
                ),
                StepMaterialDecision(
                    material_id=optional.material_id,
                    disposition=StepMaterialDisposition.DROPPED,
                    reason_code="budget_optional",
                ),
            ),
            limits=StepContextLimits(),
            assembler_version="assembler-v1",
            policy_version="policy-v1",
            estimator_version="estimator-v1",
            rendered_context_digest="sha256:" + "c" * 64,
            created_at=10.0,
        )


def test_manifest_digest_is_deterministic_and_record_hides_material_content() -> None:
    required = _material(
        "SECRET raw material",
        source=StepMaterialSource.APPROVED_PLAN,
        kind=StepMaterialKind.STEP_GOAL,
        trust=StepMaterialTrust.APPROVED_CONTROL,
        required=True,
        source_id="goal-1",
    )
    decision = StepMaterialDecision(
        material_id=required.material_id,
        disposition=StepMaterialDisposition.KEPT,
        reason_code="required",
    )
    kwargs = dict(
        namespace="ns",
        plan_id="plan-1",
        plan_revision=1,
        plan_definition_digest="sha256:" + "b" * 64,
        step_id="step-1",
        binding_id="binding-1",
        materials=(required,),
        decisions=(decision,),
        accepted_predecessor_refs=(),
        limits=StepContextLimits(),
        assembler_version="assembler-v1",
        policy_version="policy-v1",
        estimator_version="estimator-v1",
        budget={"included_bytes": required.byte_length, "estimated_tokens": 12},
        rendered_context_digest="sha256:" + "c" * 64,
        created_at=10.0,
    )
    first = StepContextManifest(**kwargs)
    second = StepContextManifest(**kwargs)

    assert first.digest == second.digest
    assert "SECRET raw material" not in str(first.to_record())
    assert first.to_record()["digest"] == first.digest
    assert first.to_record()["schema_version"] == 2


def test_artifact_material_records_reference_without_counting_original_payload() -> None:
    artifact = ArtifactRef(
        artifact_id="sha256:" + "d" * 64,
        byte_length=10_000_000,
        media_type="application/octet-stream",
    )
    material = StepMaterialRef.from_artifact(
        namespace="ns",
        scope="project-a",
        source=StepMaterialSource.ARTIFACT,
        kind=StepMaterialKind.HOST_FACT,
        source_id="artifact-1",
        source_version="v2",
        authorization_ref="authz-2",
        artifact_ref=artifact,
        required=True,
    )

    manifest = _manifest_for(
        (material,),
        limits=StepContextLimits(max_items=2, max_item_bytes=1024, max_total_bytes=1024),
    )

    record = manifest.to_record()
    assert material.byte_length == 10_000_000
    assert material.included_bytes < 1024
    assert record["materials"][0]["artifact_ref"]["artifact_id"] == artifact.artifact_id
    assert "content" not in record["materials"][0]


def test_manifest_accepts_only_kept_accepted_output_as_predecessor_reference() -> None:
    accepted = _material(
        "reviewed output",
        source=StepMaterialSource.ACCEPTANCE,
        kind=StepMaterialKind.ACCEPTED_OUTPUT,
        source_id="acceptance-1",
    )

    manifest = _manifest_for(
        (accepted,),
        accepted_predecessor_refs=(accepted.material_id,),
    )

    assert manifest.to_record()["accepted_predecessor_refs"] == [accepted.material_id]
    with pytest.raises(ContractError, match="kept accepted-output"):
        _manifest_for((accepted,), accepted_predecessor_refs=("sha256:" + "e" * 64,))


def test_manifest_freezes_nested_budget_values_before_digesting_them() -> None:
    goal = _material(
        "current goal",
        source=StepMaterialSource.APPROVED_PLAN,
        kind=StepMaterialKind.STEP_GOAL,
        trust=StepMaterialTrust.APPROVED_CONTROL,
        required=True,
        source_id="goal-1",
    )
    budget: dict[str, object] = {"included": ["goal", "constraints"], "tokens": {"estimate": 12}}
    manifest = _manifest_for((goal,), budget=budget)
    digest = manifest.digest

    budget["included"].append("mutated")  # type: ignore[union-attr]
    budget["tokens"]["estimate"] = 999  # type: ignore[index]

    assert manifest.digest == digest
    assert manifest.to_record()["budget"] == {
        "included": ["goal", "constraints"],
        "tokens": {"estimate": 12},
    }
    with pytest.raises(TypeError):
        manifest.budget["new"] = "value"  # type: ignore[index]
