"""Explicit, rechecked assembly of bounded Plan Step materials.

The host owns source access. This module supplies the narrow request and recheck
protocols, verifies the current Plan/binding/acceptance facts around those reads,
and publishes a manifest only after the selected source vector is revalidated.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence

from ..errors import ContractError
from .step_context import (
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

__all__ = [
    "StepContextAssembler",
    "StepMaterialResolution",
    "StepMaterialResolutionRequest",
    "StepMaterialResolver",
    "StepMaterialSourceVersion",
]


def _text(value: Any, name: str, *, max_bytes: int = 4096, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ContractError(f"{name} must be text")
    try:
        size = len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as exc:
        raise ContractError(f"{name} contains invalid Unicode") from exc
    if not allow_empty and not value.strip():
        raise ContractError(f"{name} must not be empty")
    if size > max_bytes:
        raise ContractError(f"{name} exceeds {max_bytes} UTF-8 bytes")
    return value


def _json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ContractError("step context source identity must be JSON serialisable") from exc


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _digest_record(value: Any) -> str:
    return _sha256(_json(value).encode("utf-8"))


@dataclass(frozen=True)
class StepMaterialResolutionRequest:
    """One exact, scoped source lookup delegated to a host resolver.

    ``reference`` is ephemeral input for the resolver and is deliberately omitted
    from repr, equality, the request identity and every manifest record. It may be
    a Plan ``ResourceRef`` or an acceptance evidence reference.
    """

    namespace: str
    scope: str
    source: StepMaterialSource
    kind: StepMaterialKind
    source_id: str
    source_version: str
    required: bool
    reference: Any = field(default=None, repr=False, compare=False)
    reference_digest: str = ""
    authorization_ref: str = ""
    _reference_fingerprint: str = field(default="", init=False, repr=False)

    def __post_init__(self) -> None:
        for name in ("namespace", "scope", "source_id", "source_version"):
            _text(getattr(self, name), f"step material request {name}")
        _text(self.reference_digest, "reference_digest", allow_empty=True)
        _text(self.authorization_ref, "authorization_ref", allow_empty=True)
        if type(self.required) is not bool:
            raise ContractError("step material request required must be a bool")
        try:
            object.__setattr__(self, "source", StepMaterialSource(self.source))
            object.__setattr__(self, "kind", StepMaterialKind(self.kind))
        except (TypeError, ValueError) as exc:
            raise ContractError("step material request source or kind is unsupported") from exc
        reference = self.reference
        if reference is None:
            fingerprint = ""
        else:
            projector = getattr(reference, "to_record", None)
            if not callable(projector):
                projector = getattr(reference, "as_dict", None)
            payload = projector() if callable(projector) else reference
            try:
                encoded_reference = _json(payload).encode("utf-8", errors="strict")
            except UnicodeEncodeError as exc:
                raise ContractError("step material reference contains invalid Unicode") from exc
            if len(encoded_reference) > 32 * 1024:
                raise ContractError("step material source reference exceeds 32768 bytes")
            fingerprint = _sha256(encoded_reference)
        object.__setattr__(self, "_reference_fingerprint", fingerprint)

    @property
    def request_id(self) -> str:
        return _digest_record(
            {
                "namespace": self.namespace,
                "scope": self.scope,
                "source": self.source.value,
                "kind": self.kind.value,
                "source_id": self.source_id,
                "source_version": self.source_version,
                "required": self.required,
                "reference_digest": self.reference_digest,
                "reference_fingerprint": self._reference_fingerprint,
                "authorization_ref": self.authorization_ref,
            }
        )


@dataclass(frozen=True)
class StepMaterialSourceVersion:
    """The resolver's authorization and version evidence for one source read."""

    namespace: str
    scope: str
    source: StepMaterialSource
    kind: StepMaterialKind
    source_id: str
    source_version: str
    source_digest: str
    reference_digest: str
    authorization_ref: str
    validity: StepMaterialValidity
    authorized: bool

    def __post_init__(self) -> None:
        for name in (
            "namespace",
            "scope",
            "source_id",
            "source_version",
            "authorization_ref",
        ):
            _text(getattr(self, name), f"source version {name}")
        _text(self.reference_digest, "source version reference_digest", allow_empty=True)
        if (
            not isinstance(self.source_digest, str)
            or len(self.source_digest) != 71
            or not self.source_digest.startswith("sha256:")
            or any(char not in "0123456789abcdef" for char in self.source_digest[7:])
        ):
            raise ContractError("source version digest must be a sha256 digest")
        try:
            object.__setattr__(self, "source", StepMaterialSource(self.source))
            object.__setattr__(self, "kind", StepMaterialKind(self.kind))
            object.__setattr__(self, "validity", StepMaterialValidity(self.validity))
        except (TypeError, ValueError) as exc:
            raise ContractError("source version source, kind or validity is unsupported") from exc
        if type(self.authorized) is not bool:
            raise ContractError("source version authorized must be a bool")


@dataclass(frozen=True)
class StepMaterialResolution:
    material: StepMaterialRef
    source_version: StepMaterialSourceVersion

    def __post_init__(self) -> None:
        if not isinstance(self.material, StepMaterialRef):
            raise ContractError("a material resolution requires a StepMaterialRef")
        if not isinstance(self.source_version, StepMaterialSourceVersion):
            raise ContractError("a material resolution requires source version evidence")


class StepMaterialResolver(Protocol):
    """Host-owned source adapter; authorization is checked at resolve and recheck."""

    async def resolve(
        self, request: StepMaterialResolutionRequest
    ) -> StepMaterialResolution: ...

    async def recheck(
        self,
        request: StepMaterialResolutionRequest,
        resolution: StepMaterialResolution,
    ) -> StepMaterialSourceVersion: ...


class _StepContextPlanReader(Protocol):
    async def read_plan(self, namespace: str, plan_id: str) -> Any: ...

    async def read_plan_bindings(
        self, namespace: str, plan_id: str, revision: int | None = None
    ) -> Any: ...

    async def read_plan_acceptances(
        self,
        namespace: str,
        plan_id: str,
        *,
        revision: int | None = None,
        binding_id: str | None = None,
    ) -> Any: ...


@dataclass(frozen=True)
class _PlanRead:
    snapshot: Any
    bindings: tuple[Any, ...]
    latest_by_step: Mapping[str, Any]


@dataclass(frozen=True)
class _ResolvedCandidate:
    request: StepMaterialResolutionRequest
    resolution: StepMaterialResolution
    drop_reason: str = ""


class StepContextAssembler:
    """Assemble bounded, sourced context around one current approved Plan binding.

    The resolver is the only source reader. It must authorize the supplied namespace
    and scope at read time, enforce parser/count/byte limits in its own adapter, and
    return enough version evidence for the assembler to check again before publishing.
    """

    def __init__(
        self,
        plan_store: _StepContextPlanReader,
        resolver: StepMaterialResolver | None = None,
        *,
        artifact_store: Any = None,
        limits: StepContextLimits | None = None,
        read_timeout_seconds: float = 2.0,
        source_timeout_seconds: float = 2.0,
        assembly_timeout_seconds: float = 120.0,
        max_source_requests: int = 128,
        assembler_version: str = "step-context-assembler/1",
        policy_version: str = "explicit-material-selection/1",
        estimator_version: str = "host-unspecified/1",
    ) -> None:
        if plan_store is None or any(
            not callable(getattr(plan_store, name, None))
            for name in ("read_plan", "read_plan_bindings", "read_plan_acceptances")
        ):
            raise ContractError("StepContextAssembler needs Plan and acceptance read capabilities")
        if resolver is not None and any(
            not callable(getattr(resolver, name, None)) for name in ("resolve", "recheck")
        ):
            raise ContractError("a step material resolver needs resolve and recheck methods")
        if artifact_store is not None and not callable(getattr(artifact_store, "holds", None)):
            raise ContractError("artifact_store must provide a bounded holds(artifact_id) check")
        for name, value in (
            ("read_timeout_seconds", read_timeout_seconds),
            ("source_timeout_seconds", source_timeout_seconds),
            ("assembly_timeout_seconds", assembly_timeout_seconds),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not 0 < value <= (300 if name == "assembly_timeout_seconds" else 60)
            ):
                upper = 300 if name == "assembly_timeout_seconds" else 60
                raise ContractError(f"{name} must be finite and in (0, {upper}]")
        if type(max_source_requests) is not int or not 1 <= max_source_requests <= 512:
            raise ContractError("max_source_requests must be in [1, 512]")
        self._plan_store = plan_store
        self._resolver = resolver
        self._artifact_store = artifact_store
        self._limits = StepContextLimits() if limits is None else limits
        if not isinstance(self._limits, StepContextLimits):
            raise ContractError("limits must be StepContextLimits")
        self._read_timeout = float(read_timeout_seconds)
        self._source_timeout = float(source_timeout_seconds)
        self._assembly_timeout = float(assembly_timeout_seconds)
        self._max_source_requests = max_source_requests
        self._assembler_version = _text(assembler_version, "assembler_version")
        self._policy_version = _text(policy_version, "policy_version")
        self._estimator_version = _text(estimator_version, "estimator_version")

    async def assemble(
        self,
        *,
        namespace: str,
        plan_id: str,
        step_id: str,
        binding_id: str,
        scope: str,
        predecessor_steps: Sequence[str] = (),
        required_predecessor_steps: Sequence[str] = (),
        additional_sources: Sequence[StepMaterialResolutionRequest] = (),
        include_plan_overview: bool = False,
        budget: Mapping[str, Any] | None = None,
        now: float | None = None,
    ) -> StepContextManifest:
        """Build a manifest under one total wall-clock deadline."""
        try:
            return await asyncio.wait_for(
                self._assemble(
                    namespace=namespace,
                    plan_id=plan_id,
                    step_id=step_id,
                    binding_id=binding_id,
                    scope=scope,
                    predecessor_steps=predecessor_steps,
                    required_predecessor_steps=required_predecessor_steps,
                    additional_sources=additional_sources,
                    include_plan_overview=include_plan_overview,
                    budget=budget,
                    now=now,
                ),
                timeout=self._assembly_timeout,
            )
        except asyncio.TimeoutError as exc:
            raise StepContextRefused(
                "assembly_timeout", "bounded Step context assembly exceeded its total time limit"
            ) from exc

    async def _assemble(
        self,
        *,
        namespace: str,
        plan_id: str,
        step_id: str,
        binding_id: str,
        scope: str,
        predecessor_steps: Sequence[str] = (),
        required_predecessor_steps: Sequence[str] = (),
        additional_sources: Sequence[StepMaterialResolutionRequest] = (),
        include_plan_overview: bool = False,
        budget: Mapping[str, Any] | None = None,
        now: float | None = None,
    ) -> StepContextManifest:
        """Build a manifest from current Plan facts and explicit source selections."""
        for name, value in (
            ("namespace", namespace),
            ("plan_id", plan_id),
            ("step_id", step_id),
            ("binding_id", binding_id),
            ("scope", scope),
        ):
            _text(value, name)
        if type(include_plan_overview) is not bool:
            raise ContractError("include_plan_overview must be a bool")
        explicit_time = now is not None
        stamp = time.time() if now is None else now
        if (
            isinstance(stamp, bool)
            or not isinstance(stamp, (int, float))
            or not math.isfinite(float(stamp))
            or stamp < 0
        ):
            raise ContractError("step context assembly time must be finite and non-negative")
        stamp = float(stamp)
        stamp_ms = int(stamp * 1000)
        predecessor_steps = tuple(predecessor_steps)
        required_predecessor_steps = tuple(required_predecessor_steps)
        additional_sources = tuple(additional_sources)
        for name, values in (
            ("predecessor_steps", predecessor_steps),
            ("required_predecessor_steps", required_predecessor_steps),
        ):
            for value in values:
                _text(value, name)
        if len(set(predecessor_steps)) != len(predecessor_steps):
            raise ContractError("predecessor_steps must be unique")
        if len(set(required_predecessor_steps)) != len(required_predecessor_steps):
            raise ContractError("required_predecessor_steps must be unique")
        if not set(required_predecessor_steps).issubset(predecessor_steps):
            raise ContractError("required predecessor steps must also be selected predecessors")
        if any(not isinstance(item, StepMaterialResolutionRequest) for item in additional_sources):
            raise ContractError("additional_sources must contain StepMaterialResolutionRequest values")
        for request in additional_sources:
            if request.namespace != namespace or request.scope != scope:
                raise StepContextRefused(
                    "material_scope_mismatch",
                    "an additional material request crosses the assembly namespace or scope",
                )
            if request.source in {StepMaterialSource.APPROVED_PLAN, StepMaterialSource.ACCEPTANCE}:
                raise ContractError(
                    "approved Plan and acceptance materials are derived from the control store"
                )

        start = await self._read_plan(namespace, plan_id)
        self._validate_plan_snapshot(start.snapshot, namespace, plan_id, stamp_ms)
        definition = start.snapshot.definition
        step = next((item for item in definition.steps if item.step_id == step_id), None)
        if step is None:
            raise StepContextRefused("unknown_step", "the current Plan revision has no requested Step")
        if not set(predecessor_steps).issubset(step.depends_on):
            raise ContractError("selected predecessor steps must be direct dependencies of this Step")
        current_binding = start.latest_by_step.get(step_id)
        if (
            current_binding is None
            or current_binding.binding_id != binding_id
            or current_binding.binding_status not in {"admitted", "active"}
        ):
            raise StepContextRefused(
                "step_binding_stale", "the requested Step binding is not its current active binding"
            )

        active_approval = self._active_approval(start.snapshot, stamp_ms)
        approval_ref = active_approval.decision_id
        plan_materials = self._required_plan_materials(
            definition=definition,
            step=step,
            scope=scope,
            approval_ref=approval_ref,
            include_plan_overview=include_plan_overview,
        )
        required_plan_materials = tuple(item for item in plan_materials if item.required)
        self._limits.check_required(required_plan_materials)

        requests: list[StepMaterialResolutionRequest] = []
        for resource, required in (
            *((item, True) for item in step.required_context_refs),
            *((item, False) for item in step.input_refs),
        ):
            if resource.namespace != namespace:
                raise StepContextRefused(
                    "material_scope_mismatch", "a Plan resource reference crosses namespace"
                )
            reference_record = dict(resource.to_record())
            requests.append(
                StepMaterialResolutionRequest(
                    namespace=namespace,
                    scope=scope,
                    source=StepMaterialSource.HOST,
                    kind=StepMaterialKind.HOST_FACT,
                    source_id="resource:" + _digest_record(reference_record),
                    source_version=resource.version,
                    required=required,
                    reference=resource,
                    reference_digest=resource.digest,
                )
            )

        acceptances_by_binding: dict[str, tuple[Any, ...]] = {}
        selected_acceptance_ids: set[str] = set()
        omissions: list[Mapping[str, str]] = []
        predecessor_material_requests: list[StepMaterialResolutionRequest] = []
        step_by_id = {item.step_id: item for item in definition.steps}
        for predecessor_id in predecessor_steps:
            predecessor = step_by_id[predecessor_id]
            predecessor_binding = start.latest_by_step.get(predecessor_id)
            required = predecessor_id in required_predecessor_steps
            if (
                predecessor_binding is None
                or predecessor_binding.binding_status != "accepted"
                or predecessor_binding.accepted_at is None
            ):
                if required:
                    raise StepContextRefused(
                        "required_predecessor_unaccepted",
                        f"required predecessor {predecessor_id!r} has no current accepted binding",
                    )
                omissions.append(
                    {"source_id": _digest_record({"step_id": predecessor_id}), "reason_code": "no_current_acceptance"}
                )
                continue
            accepted_records = await self._read_acceptances(
                namespace,
                plan_id,
                definition.revision,
                predecessor_binding.binding_id,
            )
            acceptances_by_binding[predecessor_binding.binding_id] = accepted_records
            latest_acceptance = self._latest_acceptance_by_criterion(
                accepted_records,
                namespace=namespace,
                plan_id=plan_id,
                revision=definition.revision,
                step=predecessor,
                binding_id=predecessor_binding.binding_id,
            )
            if latest_acceptance is None:
                if required:
                    raise StepContextRefused(
                        "required_predecessor_unaccepted",
                        f"required predecessor {predecessor_id!r} lacks complete accepted criteria",
                    )
                omissions.append(
                    {"source_id": _digest_record({"step_id": predecessor_id}), "reason_code": "acceptance_incomplete"}
                )
                continue
            selected_acceptance_ids.update(item.acceptance_id for item in latest_acceptance)
            snapshots = {item.source_snapshot_digest for item in latest_acceptance}
            if len(snapshots) != 1:
                raise StepContextRefused(
                    "acceptance_source_conflict",
                    f"predecessor {predecessor_id!r} acceptance rows disagree on their source snapshot",
                )
            evidence_requests = 0
            for acceptance in latest_acceptance:
                for evidence_ref in acceptance.evidence_refs:
                    source_id = "accepted-evidence:" + _digest_record(
                        {"step_id": predecessor_id, "evidence_ref": evidence_ref}
                    )
                    predecessor_material_requests.append(
                        StepMaterialResolutionRequest(
                            namespace=namespace,
                            scope=scope,
                            source=StepMaterialSource.ACCEPTANCE,
                            kind=StepMaterialKind.ACCEPTED_OUTPUT,
                            source_id=source_id,
                            source_version=acceptance.source_snapshot_digest,
                            required=required,
                            reference=evidence_ref,
                            authorization_ref=acceptance.acceptance_id,
                        )
                    )
                    evidence_requests += 1
            if evidence_requests == 0:
                if required:
                    raise StepContextRefused(
                        "required_predecessor_evidence_missing",
                        f"required predecessor {predecessor_id!r} acceptance has no evidence references",
                    )
                omissions.append(
                    {"source_id": _digest_record({"step_id": predecessor_id}), "reason_code": "no_accepted_evidence_refs"}
                )

        requests.extend(predecessor_material_requests)
        requests.extend(additional_sources)
        unique_requests: list[StepMaterialResolutionRequest] = []
        seen_requests: set[str] = set()
        for request in requests:
            if request.request_id in seen_requests:
                omissions.append(
                    {"source_id": request.request_id, "reason_code": "duplicate_source_request"}
                )
                continue
            seen_requests.add(request.request_id)
            unique_requests.append(request)
        requests = unique_requests
        if len(requests) > self._max_source_requests:
            raise StepContextRefused(
                "source_request_count_exceeded",
                f"{len(requests)} source requests exceed the configured read limit",
                required_count=len(requests),
                limit=self._max_source_requests,
            )
        required_request_count = sum(item.required for item in requests)
        required_total_count = len(required_plan_materials) + required_request_count
        if required_total_count > self._limits.max_items:
            raise StepContextRefused(
                "required_material_count_exceeded",
                "required Plan facts and source requests exceed the context item limit",
                required_count=required_total_count,
                limit=self._limits.max_items,
            )
        if requests and self._resolver is None:
            raise StepContextRefused(
                "material_resolver_unavailable",
                "the Step declares sources but no host material resolver is configured",
                required_count=required_request_count,
                limit=self._max_source_requests,
            )

        resolved: list[_ResolvedCandidate] = []
        for request in requests:
            try:
                candidate = await self._resolve(request)
            except StepContextRefused as refusal:
                if request.required:
                    raise
                if refusal.reason_code in {
                    "material_identity_mismatch",
                    "material_source_state_mismatch",
                    "material_authorization_mismatch",
                    "material_requiredness_mismatch",
                }:
                    raise
                omissions.append({"source_id": request.request_id, "reason_code": refusal.reason_code})
                continue
            resolved.append(candidate)

        publish_stamp = stamp if explicit_time else time.time()
        publish_stamp_ms = int(publish_stamp * 1000)
        for index, candidate in enumerate(resolved):
            current = await self._recheck(candidate)
            if current:
                continue
            if candidate.request.required:
                raise StepContextRefused(
                    "required_material_changed_during_assembly",
                    "a required source changed or lost authorization before manifest publication",
                )
            resolved[index] = _ResolvedCandidate(
                candidate.request, candidate.resolution, "source_changed_during_assembly"
            )
        await self._verify_plan_boundary(
            namespace=namespace,
            plan_id=plan_id,
            revision=definition.revision,
            initial=start,
            acceptances_by_binding=acceptances_by_binding,
            stamp_ms=publish_stamp_ms,
        )

        all_candidates = list(plan_materials)
        all_candidates.extend(item.resolution.material for item in resolved)
        required_materials = tuple(item for item in all_candidates if item.required)
        self._limits.check_required(required_materials)

        decisions: list[StepMaterialDecision] = []
        drop_reasons = {
            candidate.resolution.material.material_id: candidate.drop_reason
            for candidate in resolved
            if candidate.drop_reason
        }
        kept_count = 0
        kept_bytes = 0
        for material in all_candidates:
            if material.required:
                if not material.usable_at(publish_stamp):
                    raise StepContextRefused(
                        "required_material_not_current",
                        f"required material {material.source_id!r} is stale, revoked, expired, or unverified",
                    )
                reason = (
                    "required_plan_material"
                    if material.source is StepMaterialSource.APPROVED_PLAN
                    else "required_source_material"
                )
                kept = True
            else:
                reason = "included_within_limits"
                forced_drop_reason = drop_reasons.get(material.material_id)
                kept = not forced_drop_reason and material.usable_at(publish_stamp)
                if not kept:
                    reason = forced_drop_reason or "source_not_current"
                elif material.included_bytes > self._limits.max_item_bytes:
                    kept = False
                    reason = "item_byte_limit"
                elif kept_count >= self._limits.max_items:
                    kept = False
                    reason = "item_count_limit"
                elif kept_bytes + material.included_bytes > self._limits.max_total_bytes:
                    kept = False
                    reason = "total_byte_limit"
            if kept:
                kept_count += 1
                kept_bytes += material.included_bytes
                if material.source is StepMaterialSource.ACCEPTANCE:
                    decisions.append(
                        StepMaterialDecision(
                            material_id=material.material_id,
                            disposition=StepMaterialDisposition.KEPT,
                            reason_code=("required_predecessor_output" if material.required else "accepted_predecessor_output"),
                        )
                    )
                else:
                    decisions.append(
                        StepMaterialDecision(
                            material_id=material.material_id,
                            disposition=StepMaterialDisposition.KEPT,
                            reason_code=reason,
                        )
                    )
            else:
                decisions.append(
                    StepMaterialDecision(
                        material_id=material.material_id,
                        disposition=StepMaterialDisposition.DROPPED,
                        reason_code=reason,
                    )
                )

        source_vector = [
            {
                "request_id": candidate.request.request_id,
                "namespace": candidate.resolution.material.namespace,
                "scope": candidate.resolution.material.scope,
                "source": candidate.resolution.material.source.value,
                "kind": candidate.resolution.material.kind.value,
                "source_id": candidate.resolution.material.source_id,
                "source_version": candidate.resolution.material.source_version,
                "source_digest": candidate.resolution.material.source_digest,
                "reference_digest": candidate.request.reference_digest,
                "authorization_ref": candidate.resolution.material.authorization_ref,
                "drop_reason": candidate.drop_reason,
            }
            for candidate in resolved
        ]
        kept_material_ids = {
            decision.material_id
            for decision in decisions
            if decision.disposition is StepMaterialDisposition.KEPT
        }
        accepted_predecessor_refs = tuple(
            material.material_id
            for material in all_candidates
            if material.source is StepMaterialSource.ACCEPTANCE
            and material.kind is StepMaterialKind.ACCEPTED_OUTPUT
            and material.material_id in kept_material_ids
        )
        boundary = {
            "consistency": "bracketed_non_atomic",
            "plan_control_version": start.snapshot.record.control_version,
            "source_request_count": len(requests),
            "resolved_source_count": len(resolved),
            "source_vector_digest": _digest_record(source_vector),
            "acceptance_ids": sorted(selected_acceptance_ids),
            "omissions": omissions,
        }
        return StepContextManifest(
            namespace=namespace,
            plan_id=plan_id,
            plan_revision=definition.revision,
            plan_definition_digest=definition.definition_digest,
            step_id=step_id,
            binding_id=binding_id,
            materials=tuple(all_candidates),
            decisions=tuple(decisions),
            accepted_predecessor_refs=accepted_predecessor_refs,
            limits=self._limits,
            assembler_version=self._assembler_version,
            policy_version=self._policy_version,
            estimator_version=self._estimator_version,
            budget={} if budget is None else budget,
            source_boundary=boundary,
            rendered_context_digest="sha256:" + "0" * 64,
            created_at=publish_stamp,
        )

    async def _read_plan(self, namespace: str, plan_id: str) -> _PlanRead:
        try:
            snapshot = await asyncio.wait_for(
                self._plan_store.read_plan(namespace, plan_id), timeout=self._read_timeout
            )
            if snapshot is None:
                raise StepContextRefused("plan_unavailable", "the Plan does not exist")
            from ..plans.types import PlanSnapshot, StepTaskBinding

            if not isinstance(snapshot, PlanSnapshot):
                raise StepContextRefused(
                    "plan_read_unsupported", "the Plan reader returned an unsupported snapshot"
                )
            bindings = tuple(
                await asyncio.wait_for(
                    self._plan_store.read_plan_bindings(
                        namespace, plan_id, snapshot.record.current_revision
                    ),
                    timeout=self._read_timeout,
                )
            )
            if any(not isinstance(item, StepTaskBinding) for item in bindings):
                raise StepContextRefused(
                    "plan_read_unsupported", "the Plan reader returned unsupported binding rows"
                )
        except StepContextRefused:
            raise
        except asyncio.TimeoutError as exc:
            raise StepContextRefused("plan_read_timeout", "a bounded Plan read timed out") from exc
        except Exception as exc:  # noqa: BLE001 - host store exceptions are not safe report text
            raise StepContextRefused("plan_read_failed", "the current Plan could not be read") from exc
        latest: dict[str, Any] = {}
        for binding in bindings:
            if (
                binding.namespace != namespace
                or binding.plan_id != plan_id
                or binding.revision != snapshot.record.current_revision
            ):
                raise StepContextRefused("plan_binding_corrupt", "a Plan binding has conflicting identity")
            previous = latest.get(binding.step_id)
            if previous is None or (binding.created_at, binding.binding_id) > (
                previous.created_at,
                previous.binding_id,
            ):
                latest[binding.step_id] = binding
        return _PlanRead(snapshot, bindings, latest)

    async def _read_acceptances(
        self, namespace: str, plan_id: str, revision: int, binding_id: str
    ) -> tuple[Any, ...]:
        try:
            rows = await asyncio.wait_for(
                self._plan_store.read_plan_acceptances(
                    namespace, plan_id, revision=revision, binding_id=binding_id
                ),
                timeout=self._read_timeout,
            )
        except asyncio.TimeoutError as exc:
            raise StepContextRefused("acceptance_read_timeout", "a bounded acceptance read timed out") from exc
        except Exception as exc:  # noqa: BLE001 - host store exceptions are not safe report text
            raise StepContextRefused("acceptance_read_failed", "accepted predecessor evidence could not be read") from exc
        from ..plans.types import AcceptanceRecord

        rows = tuple(rows)
        if any(not isinstance(item, AcceptanceRecord) for item in rows):
            raise StepContextRefused(
                "acceptance_read_unsupported", "the Plan reader returned unsupported acceptance rows"
            )
        return rows

    @staticmethod
    def _validate_plan_snapshot(snapshot: Any, namespace: str, plan_id: str, now_ms: int) -> None:
        from ..plans.types import PlanStatus, PlanSnapshot

        if not isinstance(snapshot, PlanSnapshot):
            raise StepContextRefused("plan_read_unsupported", "the Plan reader returned an unsupported snapshot")
        record = snapshot.record
        definition = snapshot.definition
        if (
            record.namespace != namespace
            or record.plan_id != plan_id
            or definition.namespace != namespace
            or definition.plan_id != plan_id
            or record.current_revision != definition.revision
            or record.definition_digest != definition.definition_digest
        ):
            raise StepContextRefused("plan_revision_stale", "the Plan snapshot identity is inconsistent")
        if record.status is not PlanStatus.EXECUTING:
            raise StepContextRefused("plan_not_executing", "the current Plan is not in executing state")
        StepContextAssembler._active_approval(snapshot, now_ms)

    @staticmethod
    def _active_approval(snapshot: Any, now_ms: int) -> Any:
        from ..plans.types import ApprovalDecision

        record = snapshot.record
        approval = next(
            (item for item in snapshot.approvals if item.decision_id == record.active_approval_id),
            None,
        )
        if (
            approval is None
            or approval.namespace != record.namespace
            or approval.plan_id != record.plan_id
            or approval.revision != record.current_revision
            or approval.definition_digest != snapshot.definition.definition_digest
            or approval.execution_envelope_digest != snapshot.definition.envelope_digest
            or approval.decision is not ApprovalDecision.APPROVED
            or (approval.expires_at_ms is not None and approval.expires_at_ms <= now_ms)
            or any(
                item.namespace == record.namespace
                and item.plan_id == record.plan_id
                and item.decision_id == approval.decision_id
                for item in snapshot.revocations
            )
        ):
            raise StepContextRefused(
                "plan_approval_stale", "the current Plan approval is missing, expired, or revoked"
            )
        return approval

    @staticmethod
    def _required_plan_materials(
        *, definition: Any, step: Any, scope: str, approval_ref: str, include_plan_overview: bool
    ) -> tuple[StepMaterialRef, ...]:
        from ..plans.codec import canonical_json_bytes

        version = definition.definition_digest
        materials = [
            StepMaterialRef.from_content(
                namespace=definition.namespace,
                scope=scope,
                source=StepMaterialSource.APPROVED_PLAN,
                kind=StepMaterialKind.STEP_GOAL,
                source_id=f"plan:{definition.plan_id}:r{definition.revision}:step:{step.step_id}:goal",
                source_version=version,
                authorization_ref=approval_ref,
                content=f"{step.title}\n\n{step.instruction}",
                required=True,
                trust=StepMaterialTrust.APPROVED_CONTROL,
            )
        ]
        for constraint in definition.constraints:
            materials.append(
                StepMaterialRef.from_content(
                    namespace=definition.namespace,
                    scope=scope,
                    source=StepMaterialSource.APPROVED_PLAN,
                    kind=StepMaterialKind.REQUIRED_CONSTRAINT,
                    source_id=f"plan:{definition.plan_id}:r{definition.revision}:constraint:{constraint.constraint_id}",
                    source_version=version,
                    authorization_ref=approval_ref,
                    content=f"{constraint.text}\n[declared enforcement: {constraint.enforcement.value}]",
                    required=True,
                    trust=StepMaterialTrust.APPROVED_CONTROL,
                )
            )
        for criterion in step.acceptance_criteria:
            materials.append(
                StepMaterialRef.from_content(
                    namespace=definition.namespace,
                    scope=scope,
                    source=StepMaterialSource.APPROVED_PLAN,
                    kind=StepMaterialKind.ACCEPTANCE_CRITERION,
                    source_id=f"plan:{definition.plan_id}:r{definition.revision}:step:{step.step_id}:criterion:{criterion.criterion_id}",
                    source_version=version,
                    authorization_ref=approval_ref,
                    content=canonical_json_bytes(criterion.to_record()).decode("utf-8"),
                    required=True,
                    trust=StepMaterialTrust.APPROVED_CONTROL,
                )
            )
        if include_plan_overview:
            materials.append(
                StepMaterialRef.from_content(
                    namespace=definition.namespace,
                    scope=scope,
                    source=StepMaterialSource.APPROVED_PLAN,
                    kind=StepMaterialKind.PLAN_OVERVIEW,
                    source_id=f"plan:{definition.plan_id}:r{definition.revision}:overview",
                    source_version=version,
                    authorization_ref=approval_ref,
                    content=f"{definition.title}\n\n{definition.goal}",
                    required=False,
                    trust=StepMaterialTrust.APPROVED_CONTROL,
                )
            )
        return tuple(materials)

    @staticmethod
    def _latest_acceptance_by_criterion(
        rows: Sequence[Any],
        *,
        namespace: str,
        plan_id: str,
        revision: int,
        step: Any,
        binding_id: str,
    ) -> tuple[Any, ...] | None:
        from ..plans.types import AcceptanceResult

        by_criterion: dict[str, Any] = {}
        expected = {criterion.criterion_id for criterion in step.acceptance_criteria}
        for record in sorted(rows, key=lambda item: (item.decided_at_ms, item.acceptance_id)):
            if (
                record.namespace != namespace
                or record.plan_id != plan_id
                or record.revision != revision
                or record.step_id != step.step_id
                or record.binding_id != binding_id
                or record.scope != "step"
            ):
                raise StepContextRefused("acceptance_identity_conflict", "an acceptance row has conflicting identity")
            if record.criterion_id not in expected:
                raise StepContextRefused(
                    "acceptance_criterion_changed",
                    "a predecessor acceptance names a criterion absent from the current Step",
                )
            by_criterion[record.criterion_id] = record
        if set(by_criterion) != expected:
            return None
        latest = tuple(by_criterion[key] for key in sorted(expected))
        criteria = {item.criterion_id: item for item in step.acceptance_criteria}
        if any(
            record.verifier_id != criteria[record.criterion_id].verifier_id
            or record.verifier_version != criteria[record.criterion_id].verifier_version
            for record in latest
        ):
            raise StepContextRefused(
                "acceptance_criterion_changed",
                "a predecessor acceptance does not match its current criterion version",
            )
        if any(record.result is not AcceptanceResult.ACCEPTED for record in latest):
            return None
        return latest

    async def _resolve(
        self, request: StepMaterialResolutionRequest
    ) -> _ResolvedCandidate:
        assert self._resolver is not None
        try:
            result = await asyncio.wait_for(
                self._resolver.resolve(request), timeout=self._source_timeout
            )
        except asyncio.TimeoutError as exc:
            raise StepContextRefused("material_source_timeout", "a bounded material read timed out") from exc
        except Exception as exc:  # noqa: BLE001 - resolver exception text may contain source data
            raise StepContextRefused("material_source_failed", "the host material source could not be read") from exc
        if not isinstance(result, StepMaterialResolution):
            raise StepContextRefused("material_source_unsupported", "the resolver returned an unsupported result")
        material = result.material
        state = result.source_version
        if not state.authorized:
            raise StepContextRefused("material_access_denied", "the host resolver denied this material read")
        if (
            material.namespace != request.namespace
            or material.scope != request.scope
            or material.source is not request.source
            or material.kind is not request.kind
            or material.source_id != request.source_id
        ):
            raise StepContextRefused("material_identity_mismatch", "the resolver returned a different source identity")
        if (
            state.namespace != material.namespace
            or state.scope != material.scope
            or state.source is not material.source
            or state.kind is not material.kind
            or state.source_id != material.source_id
            or state.source_version != material.source_version
            or state.source_digest != material.source_digest
            or state.authorization_ref != material.authorization_ref
            or state.validity is not material.validity
        ):
            raise StepContextRefused("material_source_state_mismatch", "resolver version evidence disagrees with its material")
        if request.authorization_ref and material.authorization_ref != request.authorization_ref:
            raise StepContextRefused("material_authorization_mismatch", "the resolver returned another authorization reference")
        if material.artifact_ref is not None:
            if self._artifact_store is None:
                raise StepContextRefused(
                    "artifact_store_unavailable",
                    "an artifact material requires a store that can confirm the reference exists",
                )
            try:
                artifact_exists = self._artifact_store.holds(material.artifact_ref.artifact_id)
            except Exception as exc:  # noqa: BLE001 - host store exception text may contain paths
                raise StepContextRefused(
                    "artifact_read_failed", "the artifact reference could not be checked"
                ) from exc
            if artifact_exists is not True:
                raise StepContextRefused(
                    "artifact_missing", "the referenced artifact is not available in this store"
                )
        reasons: list[str] = []
        if material.source_version != request.source_version:
            reasons.append("source_version_changed")
        if request.reference_digest and state.reference_digest != request.reference_digest:
            reasons.append("source_reference_digest_changed")
        if material.validity not in {
            StepMaterialValidity.CURRENT,
            StepMaterialValidity.UNVERIFIED,
        }:
            reasons.append("source_not_current")
        if material.required != request.required:
            raise StepContextRefused("material_requiredness_mismatch", "resolver changed material requiredness")
        if reasons and request.required:
            raise StepContextRefused(
                "required_material_not_current",
                "a required source does not match its declared version or validity",
            )
        return _ResolvedCandidate(request, result, reasons[0] if reasons else "")

    async def _recheck(self, candidate: _ResolvedCandidate) -> bool:
        assert self._resolver is not None
        request = candidate.request
        try:
            current = await asyncio.wait_for(
                self._resolver.recheck(request, candidate.resolution),
                timeout=self._source_timeout,
            )
        except asyncio.TimeoutError:
            return False
        except Exception:  # noqa: BLE001 - resolver exception text may contain source data
            return False
        original = candidate.resolution.source_version
        if not isinstance(current, StepMaterialSourceVersion) or not current.authorized:
            return False
        return current == original

    async def _verify_plan_boundary(
        self,
        *,
        namespace: str,
        plan_id: str,
        revision: int,
        initial: _PlanRead,
        acceptances_by_binding: Mapping[str, tuple[Any, ...]],
        stamp_ms: int,
    ) -> None:
        current = await self._read_plan(namespace, plan_id)
        self._validate_plan_snapshot(current.snapshot, namespace, plan_id, stamp_ms)
        if current.snapshot.record != initial.snapshot.record:
            raise StepContextRefused(
                "plan_changed_during_assembly", "Plan control or approval facts changed during source reads"
            )
        if current.snapshot.approvals != initial.snapshot.approvals or current.snapshot.revocations != initial.snapshot.revocations:
            raise StepContextRefused(
                "plan_changed_during_assembly", "Plan approval facts changed during source reads"
            )
        if current.bindings != initial.bindings:
            raise StepContextRefused(
                "step_binding_changed_during_assembly", "Plan Step bindings changed during source reads"
            )
        if current.snapshot.record.current_revision != revision:
            raise StepContextRefused(
                "plan_revision_changed_during_assembly", "the current Plan revision changed during source reads"
            )
        for binding_id, previous in acceptances_by_binding.items():
            refreshed = await self._read_acceptances(namespace, plan_id, revision, binding_id)
            if refreshed != previous:
                raise StepContextRefused(
                    "acceptance_changed_during_assembly",
                    "predecessor acceptance facts changed during source reads",
                )
