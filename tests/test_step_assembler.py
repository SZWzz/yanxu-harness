from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from typing import Any

import pytest

from yanxu_harness.context import (
    ArtifactRef,
    StepContextAssembler,
    StepContextLimits,
    StepContextRefused,
    StepMaterialKind,
    StepMaterialRef,
    StepMaterialResolution,
    StepMaterialResolutionRequest,
    StepMaterialSource,
    StepMaterialSourceVersion,
    StepMaterialTrust,
    StepMaterialValidity,
)
from yanxu_harness.errors import ContractError
from yanxu_harness.plans.types import (
    AcceptanceCriterion,
    AcceptanceRecord,
    AcceptanceResult,
    ApprovalDecision,
    ApprovalRevocation,
    Constraint,
    ExecutionEnvelope,
    PlanApproval,
    PlanDefinition,
    PlanRecord,
    PlanSnapshot,
    PlanStatus,
    ResourceRef,
    StepDefinition,
    StepTaskBinding,
    VersionedRef,
)


def _digest(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def _criterion(criterion_id: str) -> AcceptanceCriterion:
    return AcceptanceCriterion(criterion_id, "check.output", "v1", {"strict": True})


def _resource(owner_id: str, version: str, content: str, *, namespace: str = "ns") -> ResourceRef:
    return ResourceRef(
        namespace=namespace,
        owner_kind="file",
        owner_id=owner_id,
        version=version,
        kind="text",
        digest=_digest(content),
    )


def _definition(
    *,
    required_context_refs: tuple[ResourceRef, ...] | None = None,
    input_refs: tuple[ResourceRef, ...] | None = None,
) -> PlanDefinition:
    previous = StepDefinition(
        step_id="prepare",
        title="Prepare evidence",
        instruction="Read and summarize the source.",
        acceptance_criteria=(_criterion("prepare-reviewed"),),
    )
    current = StepDefinition(
        step_id="review",
        title="Review change",
        instruction="Review the proposed change.",
        acceptance_criteria=(_criterion("review-accepted"),),
        depends_on=("prepare",),
        input_refs=input_refs if input_refs is not None else (_resource("optional-input", "v1", "optional input"),),
        required_context_refs=(
            required_context_refs
            if required_context_refs is not None
            else (_resource("required-input", "v1", "required input"),)
        ),
    )
    refs = {name: VersionedRef(name, "v1") for name in ("profile", "workspace", "policy", "deployment", "budget")}
    return PlanDefinition(
        namespace="ns",
        session_id="session-1",
        plan_id="plan-1",
        revision=1,
        title="Evidence review",
        goal="Review one proposed change against the source.",
        steps=(previous, current),
        constraints=(Constraint("no-network", "Do not access the public network."),),
        execution_envelope=ExecutionEnvelope(
            profile_ref=refs["profile"],
            workspace_ref=refs["workspace"],
            policy_ref=refs["policy"],
            deployment_ref=refs["deployment"],
            budget_ref=refs["budget"],
        ),
    )


def _binding(step_id: str, binding_id: str, *, status: str, created_at: float, accepted_at: float | None = None) -> StepTaskBinding:
    return StepTaskBinding(
        binding_id=binding_id,
        namespace="ns",
        plan_id="plan-1",
        revision=1,
        step_id=step_id,
        task_id=f"task-{binding_id}",
        run_id=f"run-{binding_id}",
        initial_input_digest=_digest(binding_id),
        request_id=f"request-{binding_id}",
        request_digest=_digest("request-" + binding_id),
        binding_status=status,
        ready_since=created_at,
        created_at=created_at,
        accepted_at=accepted_at,
    )


def _acceptance(binding_id: str) -> AcceptanceRecord:
    return AcceptanceRecord(
        acceptance_id="acceptance-prepare",
        namespace="ns",
        plan_id="plan-1",
        revision=1,
        step_id="prepare",
        binding_id=binding_id,
        criterion_id="prepare-reviewed",
        verifier_id="check.output",
        verifier_version="v1",
        criterion_digest=_digest("criterion"),
        result=AcceptanceResult.ACCEPTED,
        evidence_refs=("evidence:prepared-output",),
        source_snapshot_digest=_digest("source-snapshot"),
        decided_by="verifier",
        decided_at_ms=50_000,
        request_id="accept-request-1",
    )


class _PlanReader:
    def __init__(self, *, definition: PlanDefinition | None = None) -> None:
        definition = definition or _definition()
        approval = PlanApproval(
            namespace="ns",
            plan_id="plan-1",
            revision=1,
            definition_digest=definition.definition_digest,
            execution_envelope_digest=definition.envelope_digest,
            approval_policy_version="approval-v1",
            decision_id="approval-1",
            decision=ApprovalDecision.APPROVED,
            decided_by="operator",
            decided_at_ms=1,
        )
        self.snapshot = PlanSnapshot(
            record=PlanRecord(
                namespace="ns",
                session_id="session-1",
                plan_id="plan-1",
                current_revision=1,
                control_version=2,
                status=PlanStatus.EXECUTING,
                definition_digest=definition.definition_digest,
                active_approval_id="approval-1",
            ),
            definition=definition,
            approvals=(approval,),
        )
        self.bindings = (
            _binding("prepare", "prepare-binding", status="accepted", created_at=10, accepted_at=20),
            _binding("review", "review-binding", status="active", created_at=30),
        )
        self.acceptances = {"prepare-binding": (_acceptance("prepare-binding"),)}
        self.plan_reads = 0
        self.binding_reads = 0
        self.acceptance_reads: dict[str, int] = {}
        self.drift_plan = False
        self.drift_acceptance = False

    async def read_plan(self, namespace: str, plan_id: str) -> PlanSnapshot:
        assert namespace == "ns" and plan_id == "plan-1"
        self.plan_reads += 1
        if self.drift_plan and self.plan_reads > 1:
            record = replace(self.snapshot.record, control_version=self.snapshot.record.control_version + 1)
            return replace(self.snapshot, record=record)
        return self.snapshot

    async def read_plan_bindings(self, namespace: str, plan_id: str, revision: int | None = None) -> tuple[StepTaskBinding, ...]:
        assert namespace == "ns" and plan_id == "plan-1" and revision == 1
        self.binding_reads += 1
        return self.bindings

    async def read_plan_acceptances(
        self, namespace: str, plan_id: str, *, revision: int | None = None, binding_id: str | None = None
    ) -> tuple[AcceptanceRecord, ...]:
        assert namespace == "ns" and plan_id == "plan-1" and revision == 1
        assert binding_id is not None
        self.acceptance_reads[binding_id] = self.acceptance_reads.get(binding_id, 0) + 1
        records = self.acceptances.get(binding_id, ())
        if self.drift_acceptance and self.acceptance_reads[binding_id] > 1:
            return ()
        return records


class _Resolver:
    def __init__(self, contents: dict[str, str] | None = None) -> None:
        self.contents = {
            "required-input": "required input",
            "optional-input": "optional input",
            "evidence:prepared-output": "accepted prior output",
        }
        self.contents.update(contents or {})
        self.requests: list[StepMaterialResolutionRequest] = []
        self.rechecks: list[str] = []
        self.delay_for: set[str] = set()
        self.changed_on_recheck: set[str] = set()
        self.version_overrides: dict[str, tuple[str, str]] = {}
        self.artifact_requests: set[str] = set()

    async def resolve(self, request: StepMaterialResolutionRequest) -> StepMaterialResolution:
        self.requests.append(request)
        if request.source_id in self.delay_for:
            await asyncio.sleep(0.05)
        reference = request.reference
        if isinstance(reference, ResourceRef):
            key = reference.owner_id
        else:
            key = str(reference)
        content = self.contents[key]
        source_version = request.source_version
        reference_digest = request.reference_digest
        if request.source_id in self.version_overrides:
            source_version, reference_digest = self.version_overrides[request.source_id]
        material_kwargs = dict(
            namespace=request.namespace,
            scope=request.scope,
            source=request.source,
            kind=request.kind,
            source_id=request.source_id,
            source_version=source_version,
            authorization_ref=request.authorization_ref or "host-read-1",
            required=request.required,
            trust=StepMaterialTrust.EVIDENCE,
            validity=StepMaterialValidity.CURRENT,
        )
        if request.source_id in self.artifact_requests:
            artifact = ArtifactRef(
                artifact_id=_digest(content),
                byte_length=len(content.encode("utf-8")),
                media_type="text/plain",
            )
            material = StepMaterialRef.from_artifact(
                **material_kwargs,
                artifact_ref=artifact,
            )
        else:
            material = StepMaterialRef.from_content(**material_kwargs, content=content)
        state = StepMaterialSourceVersion(
            namespace=material.namespace,
            scope=material.scope,
            source=material.source,
            kind=material.kind,
            source_id=material.source_id,
            source_version=material.source_version,
            source_digest=material.source_digest,
            reference_digest=reference_digest,
            authorization_ref=material.authorization_ref,
            validity=material.validity,
            authorized=True,
        )
        return StepMaterialResolution(material, state)

    async def recheck(
        self, request: StepMaterialResolutionRequest, resolution: StepMaterialResolution
    ) -> StepMaterialSourceVersion:
        self.rechecks.append(request.source_id)
        state = resolution.source_version
        if request.source_id in self.changed_on_recheck:
            return replace(state, source_version=state.source_version + "-changed")
        return state


def _assembler(
    reader: _PlanReader | None = None,
    resolver: _Resolver | None = None,
    *,
    artifact_store: Any = None,
    limits: StepContextLimits | None = None,
    source_timeout_seconds: float = 1.0,
    assembly_timeout_seconds: float = 30.0,
    max_source_requests: int = 128,
) -> StepContextAssembler:
    return StepContextAssembler(
        reader or _PlanReader(),
        resolver or _Resolver(),
        artifact_store=artifact_store,
        limits=limits,
        source_timeout_seconds=source_timeout_seconds,
        assembly_timeout_seconds=assembly_timeout_seconds,
        read_timeout_seconds=1.0,
        max_source_requests=max_source_requests,
    )


class _ArtifactPresence:
    def __init__(self, held: bool) -> None:
        self.held = held

    def holds(self, artifact_id: str) -> bool:
        return self.held


async def _assemble(
    assembler: StepContextAssembler,
    *,
    binding_id: str = "review-binding",
    **kwargs: Any,
):
    return await assembler.assemble(
        namespace="ns",
        plan_id="plan-1",
        step_id="review",
        binding_id=binding_id,
        scope="project-a",
        now=100.0,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_assembler_reads_only_explicit_scoped_sources_and_rechecks_before_manifest() -> None:
    reader = _PlanReader()
    resolver = _Resolver()
    manifest = await _assemble(
        _assembler(reader, resolver),
        predecessor_steps=("prepare",),
        required_predecessor_steps=("prepare",),
        include_plan_overview=True,
        budget={"input_tokens": 500},
    )

    assert manifest.plan_revision == 1
    assert manifest.binding_id == "review-binding"
    assert manifest.source_boundary["consistency"] == "bracketed_non_atomic"
    assert manifest.source_boundary["plan_control_version"] == 2
    assert manifest.source_boundary["source_request_count"] == 3
    assert manifest.source_boundary["resolved_source_count"] == 3
    assert len(resolver.rechecks) == 3
    assert reader.plan_reads == 2
    assert reader.binding_reads == 2
    assert reader.acceptance_reads["prepare-binding"] == 2
    assert manifest.accepted_predecessor_refs
    kept = {
        item.material_id: item
        for item in manifest.materials
        if next(
            decision.disposition.value == "kept"
            for decision in manifest.decisions
            if decision.material_id == item.material_id
        )
    }
    accepted = [item for item in kept.values() if item.source is StepMaterialSource.ACCEPTANCE]
    assert len(accepted) == 1
    assert accepted[0].trust is StepMaterialTrust.EVIDENCE
    assert all(item.namespace == "ns" and item.scope == "project-a" for item in manifest.materials)
    record = manifest.to_record()
    assert "required input" not in str(record)
    assert "accepted prior output" not in str(record)
    assert record["source_boundary"]["source_vector_digest"].startswith("sha256:")


@pytest.mark.asyncio
async def test_assembler_does_not_read_dependency_acceptance_unless_requested() -> None:
    reader = _PlanReader()
    manifest = await _assemble(_assembler(reader))

    assert reader.acceptance_reads == {}
    assert not manifest.accepted_predecessor_refs
    assert all(item.source is not StepMaterialSource.ACCEPTANCE for item in manifest.materials)


@pytest.mark.asyncio
async def test_old_binding_cannot_supply_context_after_a_retry_supersedes_it() -> None:
    reader = _PlanReader()
    reader.bindings = (
        reader.bindings[0],
        _binding("review", "review-old", status="active", created_at=20),
        _binding("review", "review-new", status="active", created_at=30),
    )
    resolver = _Resolver()
    assembler = _assembler(reader, resolver)

    with pytest.raises(StepContextRefused) as refusal:
        await _assemble(assembler, binding_id="review-old")

    assert refusal.value.reason_code == "step_binding_stale"
    assert resolver.requests == []


@pytest.mark.asyncio
async def test_required_predecessor_uses_latest_binding_not_a_prior_accepted_attempt() -> None:
    reader = _PlanReader()
    reader.bindings = (
        reader.bindings[0],
        reader.bindings[1],
        _binding("prepare", "prepare-retry", status="active", created_at=40),
    )
    resolver = _Resolver()

    with pytest.raises(StepContextRefused) as refusal:
        await _assemble(
            _assembler(reader, resolver),
            predecessor_steps=("prepare",),
            required_predecessor_steps=("prepare",),
        )

    assert refusal.value.reason_code == "required_predecessor_unaccepted"
    assert resolver.requests == []


@pytest.mark.asyncio
async def test_plan_or_acceptance_drift_during_resolution_refuses_publication() -> None:
    reader = _PlanReader()
    reader.drift_plan = True
    resolver = _Resolver()

    with pytest.raises(StepContextRefused) as refusal:
        await _assemble(_assembler(reader, resolver), predecessor_steps=("prepare",))

    assert refusal.value.reason_code == "plan_changed_during_assembly"

    reader = _PlanReader()
    reader.drift_acceptance = True
    resolver = _Resolver()
    with pytest.raises(StepContextRefused) as refusal:
        await _assemble(_assembler(reader, resolver), predecessor_steps=("prepare",))
    assert refusal.value.reason_code == "acceptance_changed_during_assembly"


@pytest.mark.asyncio
async def test_source_version_drift_drops_optional_material_but_refuses_required_material() -> None:
    reader = _PlanReader()
    resolver = _Resolver()
    optional_request_id = "resource:" + _digest_record_for_resource(
        _resource("optional-input", "v1", "optional input")
    )
    resolver.version_overrides[optional_request_id] = ("v2", _digest("changed input"))

    manifest = await _assemble(_assembler(reader, resolver))
    optional = next(item for item in manifest.materials if item.source_id == optional_request_id)
    decision = next(item for item in manifest.decisions if item.material_id == optional.material_id)
    assert decision.disposition.value == "dropped"
    assert decision.reason_code == "source_version_changed"

    definition = _definition(
        required_context_refs=(_resource("required-input", "v1", "required input"),),
        input_refs=(),
    )
    reader = _PlanReader(definition=definition)
    resolver = _Resolver()
    required_id = "resource:" + _digest_record_for_resource(
        _resource("required-input", "v1", "required input")
    )
    resolver.version_overrides[required_id] = ("v2", _digest("changed input"))
    with pytest.raises(StepContextRefused) as refusal:
        await _assemble(_assembler(reader, resolver))
    assert refusal.value.reason_code == "required_material_not_current"


def _digest_record_for_resource(resource: ResourceRef) -> str:
    from yanxu_harness.context.step_assembler import _digest_record

    return _digest_record(dict(resource.to_record()))


@pytest.mark.asyncio
async def test_resolver_rechecks_required_material_and_timeout_is_bounded() -> None:
    reader = _PlanReader()
    resolver = _Resolver()
    required_request_id = "resource:" + _digest_record_for_resource(
        _resource("required-input", "v1", "required input")
    )
    resolver.changed_on_recheck.add(required_request_id)
    with pytest.raises(StepContextRefused) as refusal:
        await _assemble(_assembler(reader, resolver))
    assert refusal.value.reason_code == "required_material_changed_during_assembly"

    reader = _PlanReader()
    resolver = _Resolver()
    optional_request_id = "resource:" + _digest_record_for_resource(
        _resource("optional-input", "v1", "optional input")
    )
    resolver.delay_for.add(optional_request_id)
    manifest = await _assemble(
        _assembler(reader, resolver, source_timeout_seconds=0.01)
    )
    assert any(
        item["source_id"] == _request_id_for_resource("optional-input")
        and item["reason_code"] == "material_source_timeout"
        for item in manifest.source_boundary["omissions"]
    )


@pytest.mark.asyncio
async def test_total_assembly_deadline_cancels_a_slow_source_read() -> None:
    reader = _PlanReader()
    resolver = _Resolver()
    required_request_id = "resource:" + _digest_record_for_resource(
        _resource("required-input", "v1", "required input")
    )
    resolver.delay_for.add(required_request_id)

    with pytest.raises(StepContextRefused) as refusal:
        await _assemble(
            _assembler(
                reader,
                resolver,
                source_timeout_seconds=1.0,
                assembly_timeout_seconds=0.01,
            )
        )

    assert refusal.value.reason_code == "assembly_timeout"
    assert resolver.requests and resolver.rechecks == []


def _request_id_for_resource(owner_id: str) -> str:
    from yanxu_harness.context.step_assembler import StepMaterialResolutionRequest, _digest_record

    resource = _resource(owner_id, "v1", "optional input" if owner_id == "optional-input" else "required input")
    return StepMaterialResolutionRequest(
        namespace="ns",
        scope="project-a",
        source=StepMaterialSource.HOST,
        kind=StepMaterialKind.HOST_FACT,
        source_id="resource:" + _digest_record(dict(resource.to_record())),
        source_version=resource.version,
        required=owner_id == "required-input",
        reference=resource,
        reference_digest=resource.digest,
    ).request_id


@pytest.mark.asyncio
async def test_oversized_required_plan_material_refuses_before_source_read() -> None:
    reader = _PlanReader()
    resolver = _Resolver()
    assembler = _assembler(
        reader,
        resolver,
        limits=StepContextLimits(max_items=16, max_item_bytes=8, max_total_bytes=128),
    )

    with pytest.raises(StepContextRefused) as refusal:
        await _assemble(assembler)

    assert refusal.value.reason_code == "required_material_too_large"
    assert resolver.requests == []


@pytest.mark.asyncio
async def test_source_request_count_is_bounded_before_the_resolver_runs() -> None:
    reader = _PlanReader()
    resolver = _Resolver()

    with pytest.raises(StepContextRefused) as refusal:
        await _assemble(_assembler(reader, resolver, max_source_requests=1))

    assert refusal.value.reason_code == "source_request_count_exceeded"
    assert resolver.requests == []


@pytest.mark.asyncio
async def test_current_plan_approval_must_still_be_live() -> None:
    reader = _PlanReader()
    approval = replace(reader.snapshot.approvals[0], expires_at_ms=99_999)
    reader.snapshot = replace(reader.snapshot, approvals=(approval,))
    resolver = _Resolver()

    with pytest.raises(StepContextRefused) as refusal:
        await _assemble(_assembler(reader, resolver))

    assert refusal.value.reason_code == "plan_approval_stale"
    assert resolver.requests == []

    reader = _PlanReader()
    reader.snapshot = replace(
        reader.snapshot,
        revocations=(
            ApprovalRevocation(
                namespace="ns",
                plan_id="plan-1",
                decision_id="approval-1",
                revoked_by="operator",
                revoked_at_ms=99_000,
            ),
        ),
    )
    with pytest.raises(StepContextRefused) as refusal:
        await _assemble(_assembler(reader, _Resolver()))
    assert refusal.value.reason_code == "plan_approval_stale"


@pytest.mark.asyncio
async def test_plan_resource_namespace_mismatch_refuses_before_source_read() -> None:
    reader = _PlanReader(
        definition=_definition(
            required_context_refs=(
                _resource("foreign", "v1", "foreign bytes", namespace="other"),
            ),
            input_refs=(),
        )
    )
    resolver = _Resolver(contents={"foreign": "foreign bytes"})

    with pytest.raises(StepContextRefused) as refusal:
        await _assemble(_assembler(reader, resolver))

    assert refusal.value.reason_code == "material_scope_mismatch"
    assert resolver.requests == []


@pytest.mark.asyncio
async def test_optional_item_over_limit_is_recorded_as_a_drop_reason() -> None:
    reader = _PlanReader()
    resolver = _Resolver(contents={"optional-input": "x" * 1000})
    assembler = _assembler(
        reader,
        resolver,
        limits=StepContextLimits(max_items=16, max_item_bytes=512, max_total_bytes=16_000),
    )

    manifest = await _assemble(assembler)

    optional_id = "resource:" + _digest_record_for_resource(
        _resource("optional-input", "v1", "optional input")
    )
    optional = next(item for item in manifest.materials if item.source_id == optional_id)
    decision = next(item for item in manifest.decisions if item.material_id == optional.material_id)
    assert decision.disposition.value == "dropped"
    assert decision.reason_code == "item_byte_limit"


@pytest.mark.asyncio
async def test_artifact_material_requires_a_resolvable_store_reference() -> None:
    reader = _PlanReader()
    resolver = _Resolver()
    required_id = "resource:" + _digest_record_for_resource(
        _resource("required-input", "v1", "required input")
    )
    resolver.artifact_requests.add(required_id)

    with pytest.raises(StepContextRefused) as refusal:
        await _assemble(_assembler(reader, resolver))
    assert refusal.value.reason_code == "artifact_store_unavailable"

    with pytest.raises(StepContextRefused) as refusal:
        await _assemble(_assembler(reader, resolver, artifact_store=_ArtifactPresence(False)))
    assert refusal.value.reason_code == "artifact_missing"

    manifest = await _assemble(_assembler(reader, resolver, artifact_store=_ArtifactPresence(True)))
    material = next(item for item in manifest.materials if item.source_id == required_id)
    assert material.artifact_ref is not None


@pytest.mark.asyncio
async def test_additional_material_requests_cannot_claim_approved_plan_or_acceptance_authority() -> None:
    reader = _PlanReader()
    resolver = _Resolver()
    assembler = _assembler(reader, resolver)
    forged = StepMaterialResolutionRequest(
        namespace="ns",
        scope="project-a",
        source=StepMaterialSource.APPROVED_PLAN,
        kind=StepMaterialKind.STEP_GOAL,
        source_id="forged-plan-goal",
        source_version="v1",
        required=True,
    )

    with pytest.raises(ContractError, match="derived from the control store"):
        await _assemble(
            assembler,
            additional_sources=(forged,),
        )
