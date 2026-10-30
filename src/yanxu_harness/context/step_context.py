"""Versioned, bounded and provenance-aware material for an approved Plan Step."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence

from ..errors import ContractError
from .artifacts import ArtifactRef

__all__ = [
    "StepContextLimits",
    "StepContextManifest",
    "StepContextRefused",
    "StepMaterialDecision",
    "StepMaterialDisposition",
    "StepMaterialKind",
    "StepMaterialRef",
    "StepMaterialSource",
    "StepMaterialTrust",
    "StepMaterialValidity",
]

_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_STEP_CONTEXT_SCHEMA_VERSION = 2


class StepMaterialSource(str, Enum):
    APPROVED_PLAN = "approved_plan"
    RUN = "run"
    ACCEPTANCE = "acceptance"
    HOST = "host"
    USER = "user"
    TOOL = "tool"
    MODEL = "model"
    ARTIFACT = "artifact"


class StepMaterialKind(str, Enum):
    STEP_GOAL = "step_goal"
    REQUIRED_CONSTRAINT = "required_constraint"
    ACCEPTANCE_CRITERION = "acceptance_criterion"
    RUN_CONTEXT = "run_context"
    ACCEPTED_OUTPUT = "accepted_output"
    PLAN_OVERVIEW = "plan_overview"
    HOST_FACT = "host_fact"
    USER_PREFERENCE = "user_preference"
    MODEL_INFERENCE = "model_inference"


class StepMaterialTrust(str, Enum):
    APPROVED_CONTROL = "approved_control"
    EVIDENCE = "evidence"
    UNVERIFIED = "unverified"


class StepMaterialValidity(str, Enum):
    CURRENT = "current"
    UNVERIFIED = "unverified"
    STALE = "stale"
    REVOKED = "revoked"


class StepMaterialDisposition(str, Enum):
    KEPT = "kept"
    DROPPED = "dropped"


class StepContextRefused(ContractError):
    """A material set cannot be used without violating its declared completeness limits."""

    code = "step_context_refused"

    def __init__(
        self,
        reason_code: str,
        detail: str,
        *,
        required_count: int = 0,
        required_bytes: int = 0,
        limit: int = 0,
    ) -> None:
        self.reason_code = reason_code
        self.detail = detail
        self.required_count = required_count
        self.required_bytes = required_bytes
        self.limit = limit
        super().__init__(detail)

    def to_record(self) -> Mapping[str, Any]:
        return {
            "kind": self.code,
            "reason_code": self.reason_code,
            "detail": self.detail,
            "required_count": self.required_count,
            "required_bytes": self.required_bytes,
            "limit": self.limit,
            "network_dispatch_allowed": False,
        }


def _text(value: Any, name: str, *, max_bytes: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{name} must be non-empty text")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ContractError(f"{name} contains invalid Unicode") from exc
    if len(encoded) > max_bytes:
        raise ContractError(f"{name} exceeds {max_bytes} UTF-8 bytes")
    return value


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _freeze_json(value: Any, *, where: str = "step context budget") -> Any:
    if value is None or isinstance(value, (str, bool)) or type(value) is int:
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContractError(f"{where} cannot contain a non-finite number")
        return value
    if isinstance(value, Mapping):
        return _FrozenJSONDict(
            {
                _text(key, f"{where} key"): _freeze_json(item, where=f"{where}.{key}")
                for key, item in value.items()
            }
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item, where=where) for item in value)
    raise ContractError(f"{where} contains unsupported value {type(value).__name__}")


class _FrozenJSONDict(dict):
    def _immutable(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("this step context value is frozen")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


@dataclass(frozen=True)
class StepMaterialRef:
    """One scoped source fact; the material itself grants no instruction or tool authority."""

    namespace: str
    scope: str
    source: StepMaterialSource
    kind: StepMaterialKind
    source_id: str
    source_version: str
    source_digest: str
    authorization_ref: str
    content: str | None = field(default=None, repr=False)
    artifact_ref: ArtifactRef | None = None
    required: bool = False
    trust: StepMaterialTrust = StepMaterialTrust.EVIDENCE
    validity: StepMaterialValidity = StepMaterialValidity.CURRENT
    valid_until: float | None = None
    max_inline_bytes: int = 32 * 1024

    def __post_init__(self) -> None:
        for name in (
            "namespace",
            "scope",
            "source_id",
            "source_version",
            "authorization_ref",
        ):
            _text(getattr(self, name), f"step material {name}")
        try:
            source = StepMaterialSource(self.source)
            kind = StepMaterialKind(self.kind)
            trust = StepMaterialTrust(self.trust)
            validity = StepMaterialValidity(self.validity)
        except (TypeError, ValueError) as exc:
            raise ContractError("step material source, kind, trust, or validity is unsupported") from exc
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "trust", trust)
        object.__setattr__(self, "validity", validity)
        if not isinstance(self.source_digest, str) or not _SHA256_RE.fullmatch(self.source_digest):
            raise ContractError("step material source_digest must be a sha256:<hex> digest")
        if type(self.required) is not bool:
            raise ContractError("step material required must be a bool")
        if type(self.max_inline_bytes) is not int or self.max_inline_bytes <= 0:
            raise ContractError("step material max_inline_bytes must be a positive integer")
        if (self.content is None) == (self.artifact_ref is None):
            raise ContractError("step material must carry exactly one of content or artifact_ref")
        if self.content is not None:
            if not isinstance(self.content, str):
                raise ContractError("inline step material content must be text")
            try:
                encoded = self.content.encode("utf-8", errors="strict")
            except UnicodeEncodeError as exc:
                raise ContractError("inline step material contains invalid Unicode") from exc
            if _digest(encoded) != self.source_digest:
                raise ContractError("step material source_digest does not match its content")
            if len(encoded) > self.max_inline_bytes:
                raise StepContextRefused(
                    "required_material_too_large" if self.required else "material_too_large",
                    f"inline material is {len(encoded)} bytes; the item limit is {self.max_inline_bytes}",
                    required_count=int(self.required),
                    required_bytes=len(encoded) if self.required else 0,
                    limit=self.max_inline_bytes,
                )
        elif not isinstance(self.artifact_ref, ArtifactRef):
            raise ContractError("artifact_ref must be an ArtifactRef")
        elif self.artifact_ref.artifact_id != self.source_digest:
            raise ContractError("step material source_digest does not match its artifact_ref")
        if self.valid_until is not None and (
            isinstance(self.valid_until, bool)
            or not isinstance(self.valid_until, (int, float))
            or not math.isfinite(float(self.valid_until))
            or self.valid_until < 0
        ):
            raise ContractError("step material valid_until must be finite and non-negative")

        if source is StepMaterialSource.MODEL:
            if kind is not StepMaterialKind.MODEL_INFERENCE:
                raise ContractError("model sourced material must be marked model_inference")
            if trust is not StepMaterialTrust.UNVERIFIED or validity is not StepMaterialValidity.UNVERIFIED:
                raise ContractError("model inference must remain unverified")
            if self.required:
                raise ContractError("model inference cannot be a required Step material")
        if kind is StepMaterialKind.MODEL_INFERENCE and source is not StepMaterialSource.MODEL:
            raise ContractError("model_inference material must identify the model as its source")
        if kind is StepMaterialKind.ACCEPTED_OUTPUT:
            if source is not StepMaterialSource.ACCEPTANCE:
                raise ContractError("accepted output must identify its acceptance source")
            if trust is StepMaterialTrust.APPROVED_CONTROL:
                raise ContractError("accepted output is evidence data, not approved control")
        if trust is StepMaterialTrust.APPROVED_CONTROL and source is not StepMaterialSource.APPROVED_PLAN:
            raise ContractError("only a currently approved Plan source can be approved control")

    @classmethod
    def from_content(
        cls,
        *,
        namespace: str,
        scope: str,
        source: StepMaterialSource,
        kind: StepMaterialKind,
        source_id: str,
        source_version: str,
        authorization_ref: str,
        content: str,
        **kwargs: Any,
    ) -> "StepMaterialRef":
        if not isinstance(content, str):
            raise ContractError("inline step material content must be text")
        return cls(
            namespace=namespace,
            scope=scope,
            source=source,
            kind=kind,
            source_id=source_id,
            source_version=source_version,
            source_digest=_digest(content.encode("utf-8", errors="strict")),
            authorization_ref=authorization_ref,
            content=content,
            **kwargs,
        )

    @classmethod
    def from_artifact(
        cls,
        *,
        namespace: str,
        scope: str,
        source: StepMaterialSource,
        kind: StepMaterialKind,
        source_id: str,
        source_version: str,
        authorization_ref: str,
        artifact_ref: ArtifactRef,
        **kwargs: Any,
    ) -> "StepMaterialRef":
        return cls(
            namespace=namespace,
            scope=scope,
            source=source,
            kind=kind,
            source_id=source_id,
            source_version=source_version,
            source_digest=artifact_ref.artifact_id,
            authorization_ref=authorization_ref,
            artifact_ref=artifact_ref,
            **kwargs,
        )

    @property
    def byte_length(self) -> int:
        if self.content is not None:
            return len(self.content.encode("utf-8"))
        assert self.artifact_ref is not None
        return self.artifact_ref.byte_length

    @property
    def included_bytes(self) -> int:
        if self.content is not None:
            return len(self.content.encode("utf-8"))
        assert self.artifact_ref is not None
        return len(_canonical_json(self.artifact_ref.as_dict()).encode("utf-8"))

    @property
    def material_id(self) -> str:
        return _digest(_canonical_json(self.to_record()).encode("utf-8"))

    def valid_at(self, now: float) -> bool:
        if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(float(now)):
            return False
        return (
            self.validity is StepMaterialValidity.CURRENT
            and (self.valid_until is None or now < self.valid_until)
        )

    def usable_at(self, now: float) -> bool:
        if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(float(now)):
            return False
        return (
            self.validity in {StepMaterialValidity.CURRENT, StepMaterialValidity.UNVERIFIED}
            and (self.valid_until is None or now < self.valid_until)
        )

    def to_record(self) -> Mapping[str, Any]:
        """Return provenance only; inline material content is never copied into the manifest."""
        return {
            "namespace": self.namespace,
            "scope": self.scope,
            "source": self.source.value,
            "kind": self.kind.value,
            "source_id": self.source_id,
            "source_version": self.source_version,
            "source_digest": self.source_digest,
            "authorization_ref": self.authorization_ref,
            "artifact_ref": None if self.artifact_ref is None else dict(self.artifact_ref.as_dict()),
            "required": self.required,
            "trust": self.trust.value,
            "validity": self.validity.value,
            "valid_until": self.valid_until,
            "byte_length": self.byte_length,
            "included_bytes": self.included_bytes,
            "max_inline_bytes": self.max_inline_bytes,
        }


@dataclass(frozen=True)
class StepContextLimits:
    max_items: int = 64
    max_item_bytes: int = 32 * 1024
    max_total_bytes: int = 256 * 1024

    def __post_init__(self) -> None:
        for name in ("max_items", "max_item_bytes", "max_total_bytes"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ContractError(f"{name} must be a positive integer")

    def check_required(self, materials: Sequence[StepMaterialRef]) -> None:
        items = tuple(materials)
        if any(not isinstance(item, StepMaterialRef) for item in items):
            raise ContractError("materials must be StepMaterialRef values")
        required = tuple(item for item in items if item.required)
        if len(required) > self.max_items:
            raise StepContextRefused(
                "required_material_count_exceeded",
                f"{len(required)} required materials exceed the item limit {self.max_items}",
                required_count=len(required),
                required_bytes=sum(item.included_bytes for item in required),
                limit=self.max_items,
            )
        oversized = next((item for item in required if item.included_bytes > self.max_item_bytes), None)
        if oversized is not None:
            raise StepContextRefused(
                "required_material_too_large",
                f"required material {oversized.source_id!r} exceeds the item byte limit",
                required_count=len(required),
                required_bytes=oversized.included_bytes,
                limit=self.max_item_bytes,
            )
        total = sum(item.included_bytes for item in required)
        if total > self.max_total_bytes:
            raise StepContextRefused(
                "required_material_total_exceeded",
                f"required materials need {total} bytes; the total byte limit is {self.max_total_bytes}",
                required_count=len(required),
                required_bytes=total,
                limit=self.max_total_bytes,
            )

    def to_record(self) -> Mapping[str, int]:
        return {
            "max_items": self.max_items,
            "max_item_bytes": self.max_item_bytes,
            "max_total_bytes": self.max_total_bytes,
        }


@dataclass(frozen=True)
class StepMaterialDecision:
    material_id: str
    disposition: StepMaterialDisposition
    reason_code: str

    def __post_init__(self) -> None:
        _text(self.material_id, "step material decision material_id", max_bytes=256)
        try:
            disposition = StepMaterialDisposition(self.disposition)
        except (TypeError, ValueError) as exc:
            raise ContractError("step material disposition must be kept or dropped") from exc
        object.__setattr__(self, "disposition", disposition)
        _text(self.reason_code, "step material decision reason_code", max_bytes=128)

    def to_record(self) -> Mapping[str, str]:
        return {
            "material_id": self.material_id,
            "disposition": self.disposition.value,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True)
class StepContextManifest:
    namespace: str
    plan_id: str
    plan_revision: int
    plan_definition_digest: str
    step_id: str
    binding_id: str
    materials: tuple[StepMaterialRef, ...]
    decisions: tuple[StepMaterialDecision, ...]
    limits: StepContextLimits
    assembler_version: str
    policy_version: str
    estimator_version: str
    rendered_context_digest: str
    created_at: float
    accepted_predecessor_refs: tuple[str, ...] = ()
    budget: Mapping[str, Any] = field(default_factory=dict)
    source_boundary: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = _STEP_CONTEXT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in (
            "namespace",
            "plan_id",
            "plan_definition_digest",
            "step_id",
            "binding_id",
            "assembler_version",
            "policy_version",
            "estimator_version",
        ):
            _text(getattr(self, name), f"step context manifest {name}")
        if type(self.plan_revision) is not int or self.plan_revision < 1:
            raise ContractError("step context plan_revision must be a positive integer")
        if type(self.schema_version) is not int or self.schema_version != _STEP_CONTEXT_SCHEMA_VERSION:
            raise ContractError("unsupported step context manifest schema version")
        if not isinstance(self.limits, StepContextLimits):
            raise ContractError("step context manifest limits must be StepContextLimits")
        if not _SHA256_RE.fullmatch(self.rendered_context_digest):
            raise ContractError("rendered_context_digest must be a sha256:<hex> digest")
        if (
            isinstance(self.created_at, bool)
            or not isinstance(self.created_at, (int, float))
            or not math.isfinite(float(self.created_at))
            or self.created_at < 0
        ):
            raise ContractError("step context created_at must be finite and non-negative")
        materials = tuple(self.materials)
        decisions = tuple(self.decisions)
        predecessors = tuple(self.accepted_predecessor_refs)
        if any(not isinstance(item, StepMaterialRef) for item in materials):
            raise ContractError("step context materials must contain StepMaterialRef values")
        self.limits.check_required(materials)
        if any(not isinstance(item, StepMaterialDecision) for item in decisions):
            raise ContractError("step context decisions must contain StepMaterialDecision values")
        if len({item.material_id for item in materials}) != len(materials):
            raise ContractError("step context cannot contain duplicate materials")
        if len({item.material_id for item in decisions}) != len(decisions):
            raise ContractError("step context cannot contain duplicate material decisions")
        if {item.material_id for item in materials} != {item.material_id for item in decisions}:
            raise ContractError("every material must have exactly one manifest decision")
        object.__setattr__(self, "materials", materials)
        object.__setattr__(self, "decisions", decisions)
        object.__setattr__(self, "accepted_predecessor_refs", predecessors)
        decision_by_id = {item.material_id: item for item in decisions}
        kept = tuple(
            item
            for item in materials
            if decision_by_id[item.material_id].disposition is StepMaterialDisposition.KEPT
        )
        for material in materials:
            if material.namespace != self.namespace:
                raise ContractError("step context material crosses the Plan namespace")
            decision = decision_by_id[material.material_id]
            if material.required and decision.disposition is StepMaterialDisposition.DROPPED:
                raise StepContextRefused(
                    "required_material_dropped",
                    f"required material {material.source_id!r} cannot be dropped",
                    required_count=1,
                    required_bytes=material.included_bytes,
                    limit=self.limits.max_total_bytes,
                )
            if material.required and not material.usable_at(float(self.created_at)):
                raise StepContextRefused(
                    "required_material_not_current",
                    f"required material {material.source_id!r} is stale, revoked, or expired",
                )
            if decision.disposition is StepMaterialDisposition.KEPT and not material.usable_at(
                float(self.created_at)
            ):
                raise StepContextRefused(
                    "kept_material_not_current",
                    f"material {material.source_id!r} is stale, revoked, expired, or unverified",
                )
        if len(kept) > self.limits.max_items:
            raise StepContextRefused(
                "kept_material_count_exceeded",
                f"{len(kept)} kept materials exceed the item limit {self.limits.max_items}",
                required_count=len(kept),
                required_bytes=sum(item.included_bytes for item in kept),
                limit=self.limits.max_items,
            )
        oversized = next((item for item in kept if item.included_bytes > self.limits.max_item_bytes), None)
        if oversized is not None:
            raise StepContextRefused(
                "kept_material_too_large",
                f"kept material {oversized.source_id!r} exceeds the per-item byte limit",
                required_count=len(kept),
                required_bytes=oversized.included_bytes,
                limit=self.limits.max_item_bytes,
            )
        total_bytes = sum(item.included_bytes for item in kept)
        if total_bytes > self.limits.max_total_bytes:
            raise StepContextRefused(
                "kept_material_total_exceeded",
                f"kept materials need {total_bytes} bytes; the total limit is {self.limits.max_total_bytes}",
                required_count=len(kept),
                required_bytes=total_bytes,
                limit=self.limits.max_total_bytes,
            )
        if len(set(predecessors)) != len(predecessors):
            raise ContractError("accepted predecessor references must be unique")
        accepted_ids = {
            item.material_id
            for item in kept
            if item.kind is StepMaterialKind.ACCEPTED_OUTPUT
            and item.source is StepMaterialSource.ACCEPTANCE
        }
        if not set(predecessors).issubset(accepted_ids):
            raise ContractError("accepted predecessor refs must name kept accepted-output materials")
        object.__setattr__(self, "budget", _freeze_json(self.budget))
        if not isinstance(self.source_boundary, Mapping):
            raise ContractError("step context source_boundary must be a JSON object")
        object.__setattr__(
            self,
            "source_boundary",
            _freeze_json(self.source_boundary, where="step context source_boundary"),
        )

    def _record(self) -> Mapping[str, Any]:
        return {
            "schema_version": self.schema_version,
            "namespace": self.namespace,
            "plan_id": self.plan_id,
            "plan_revision": self.plan_revision,
            "plan_definition_digest": self.plan_definition_digest,
            "step_id": self.step_id,
            "binding_id": self.binding_id,
            "accepted_predecessor_refs": list(self.accepted_predecessor_refs),
            "materials": [item.to_record() for item in self.materials],
            "decisions": [item.to_record() for item in self.decisions],
            "limits": dict(self.limits.to_record()),
            "assembler_version": self.assembler_version,
            "policy_version": self.policy_version,
            "estimator_version": self.estimator_version,
            "budget": _thaw_json(self.budget),
            "source_boundary": _thaw_json(self.source_boundary),
            "rendered_context_digest": self.rendered_context_digest,
            "created_at": float(self.created_at),
        }

    @property
    def digest(self) -> str:
        return _digest(_canonical_json(self._record()).encode("utf-8"))

    def to_record(self) -> Mapping[str, Any]:
        return {**self._record(), "digest": self.digest}
