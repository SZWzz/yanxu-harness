"""Immutable host-resolved execution bindings for approved Plan steps.

The values in this module are safe-to-persist descriptions. They deliberately contain no
credentials, filesystem paths, connections, providers, or tool handlers.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Protocol, runtime_checkable

from ..errors import ContractError
from .types import (
    ApprovalDecision,
    ExecutionEnvelope,
    PlanSnapshot,
    PlanStatus,
    StepDefinition,
    VersionedRef,
)


_STABLE_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,119}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_TEXT_BYTES = 2048
_REQUEST_DOMAIN = b"yanxu-execution-binding-request:v1\0"
_BINDING_DOMAIN = b"yanxu-execution-binding:v1\0"


def _text(value: Any, name: str, *, allow_empty: bool = False, max_bytes: int = _MAX_TEXT_BYTES) -> str:
    if not isinstance(value, str):
        raise ContractError(f"{name} must be a string")
    try:
        size = len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as exc:
        raise ContractError(f"{name} contains an isolated surrogate") from exc
    if (not allow_empty and not value.strip()) or size > max_bytes:
        qualifier = "non-empty and bounded" if not allow_empty else "bounded"
        raise ContractError(f"{name} must be {qualifier}")
    return value


def _identifier(value: Any, name: str) -> str:
    result = _text(value, name, max_bytes=120)
    if not _STABLE_ID.fullmatch(result):
        raise ContractError(f"{name} must be a stable lowercase identifier")
    return result


def _digest(value: Any, name: str) -> str:
    result = _text(value, name, max_bytes=64)
    if not _SHA256.fullmatch(result):
        raise ContractError(f"{name} must be a lowercase SHA-256 hex digest")
    return result


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ContractError(f"execution binding record is not canonical JSON: {exc}") from exc


def _record(value: Any, name: str, fields: set[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ContractError(f"{name} must be an object with exactly {sorted(fields)!r}")
    return value


def _versioned_ref_from_record(value: Any, name: str) -> VersionedRef:
    item = _record(value, name, {"ref", "version"})
    return VersionedRef(ref=item["ref"], version=item["version"])


def step_binding_id(
    namespace: str,
    plan_id: str,
    revision: int,
    step_id: str,
    retries_binding_id: str = "",
) -> str:
    """Return the exact deterministic binding identity used by Plan admission."""
    for name, value in (
        ("namespace", namespace),
        ("plan_id", plan_id),
        ("step_id", step_id),
    ):
        _text(value, name)
    if type(revision) is not int or revision < 1:
        raise ContractError("revision must be a positive integer")
    _text(retries_binding_id, "retries_binding_id", allow_empty=True)
    identity: dict[str, Any] = {
        "identity_version": 2 if retries_binding_id else 1,
        "namespace": namespace,
        "plan_id": plan_id,
        "revision": revision,
        "step_id": step_id,
    }
    if retries_binding_id:
        identity["retries_binding_id"] = retries_binding_id
    return "binding-" + hashlib.sha256(_canonical_bytes(identity)).hexdigest()


@dataclass(frozen=True)
class ExecutionBindingReferences:
    """The effective, approved versioned references for one Plan step."""

    profile_ref: VersionedRef
    workspace_ref: VersionedRef
    policy_ref: VersionedRef
    deployment_ref: VersionedRef
    budget_ref: VersionedRef

    def __post_init__(self) -> None:
        for name in ("profile_ref", "workspace_ref", "policy_ref", "deployment_ref", "budget_ref"):
            reference = getattr(self, name)
            if not isinstance(reference, VersionedRef):
                raise ContractError(f"execution binding {name} must be a VersionedRef")
            _text(reference.ref, f"{name}.ref")
            _text(reference.version, f"{name}.version")

    def to_record(self) -> dict[str, Any]:
        return {
            name: getattr(self, name).to_record()
            for name in ("profile_ref", "workspace_ref", "policy_ref", "deployment_ref", "budget_ref")
        }

    @classmethod
    def from_record(cls, value: Any) -> "ExecutionBindingReferences":
        fields = {"profile_ref", "workspace_ref", "policy_ref", "deployment_ref", "budget_ref"}
        record = _record(value, "execution binding references", fields)
        return cls(**{name: _versioned_ref_from_record(record[name], name) for name in fields})


@dataclass(frozen=True)
class ExecutionBindingRequest:
    """A bounded request to resolve one approved Plan step into host configuration."""

    namespace: str
    session_id: str
    plan_id: str
    revision: int
    step_id: str
    binding_id: str
    retries_binding_id: str
    definition_digest: str
    envelope_digest: str
    references: ExecutionBindingReferences
    principal_ref: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        for name in ("namespace", "session_id", "plan_id", "step_id", "principal_ref"):
            _text(getattr(self, name), name)
        _text(self.retries_binding_id, "retries_binding_id", allow_empty=True)
        if type(self.revision) is not int or self.revision < 1:
            raise ContractError("revision must be a positive integer")
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ContractError("unsupported execution binding request schema")
        if not isinstance(self.references, ExecutionBindingReferences):
            raise ContractError("references must be ExecutionBindingReferences")
        _digest(self.definition_digest, "definition_digest")
        _digest(self.envelope_digest, "envelope_digest")
        expected_id = step_binding_id(
            self.namespace,
            self.plan_id,
            self.revision,
            self.step_id,
            self.retries_binding_id,
        )
        if self.binding_id != expected_id:
            raise ContractError("binding_id does not match the deterministic Plan step identity")

    @classmethod
    def from_snapshot(
        cls,
        snapshot: PlanSnapshot,
        *,
        step_id: str,
        principal_ref: str,
        retries_binding_id: str = "",
        now_ms: int | None = None,
    ) -> "ExecutionBindingRequest":
        if not isinstance(snapshot, PlanSnapshot):
            raise ContractError("snapshot must be a PlanSnapshot")
        record, definition = snapshot.record, snapshot.definition
        if (
            record.namespace != definition.namespace
            or record.session_id != definition.session_id
            or record.plan_id != definition.plan_id
            or record.current_revision != definition.revision
            or record.definition_digest != definition.definition_digest
        ):
            raise ExecutionBindingRefused(
                "binding_snapshot_mismatch", "Plan record and definition do not identify one approved revision"
            )
        if record.status not in {PlanStatus.APPROVED, PlanStatus.EXECUTING}:
            raise ExecutionBindingRefused(
                "binding_plan_not_admissible", "Plan state does not admit a new step binding"
            )
        active_id = record.active_approval_id
        approval = next(
            (item for item in snapshot.approvals if item.decision_id == active_id), None
        )
        if not active_id or approval is None or approval.decision is not ApprovalDecision.APPROVED:
            raise ExecutionBindingRefused(
                "binding_approval_missing", "the current Plan revision has no active approval"
            )
        if any(item.decision_id == active_id for item in snapshot.revocations):
            raise ExecutionBindingRefused("binding_approval_revoked", "the active approval was revoked")
        if (
            approval.namespace != record.namespace
            or approval.plan_id != record.plan_id
            or approval.revision != record.current_revision
            or approval.definition_digest != record.definition_digest
            or approval.execution_envelope_digest != definition.envelope_digest
        ):
            raise ExecutionBindingRefused(
                "binding_approval_scope_mismatch", "the active approval does not cover this Plan revision"
            )
        at_ms = int(time.time() * 1000) if now_ms is None else now_ms
        if type(at_ms) is not int or at_ms < 0:
            raise ContractError("now_ms must be a non-negative integer")
        if approval.expires_at_ms is not None and approval.expires_at_ms <= at_ms:
            raise ExecutionBindingRefused("binding_approval_expired", "the active approval has expired")
        step = next((item for item in definition.steps if item.step_id == step_id), None)
        if step is None:
            raise ExecutionBindingRefused("binding_step_unknown", "the requested step is not in this revision")
        references = _references_for_step(definition.execution_envelope, step)
        return cls(
            namespace=record.namespace,
            session_id=record.session_id,
            plan_id=record.plan_id,
            revision=record.current_revision,
            step_id=step.step_id,
            binding_id=step_binding_id(
                record.namespace,
                record.plan_id,
                record.current_revision,
                step.step_id,
                retries_binding_id,
            ),
            retries_binding_id=retries_binding_id,
            definition_digest=record.definition_digest,
            envelope_digest=definition.envelope_digest,
            references=references,
            principal_ref=principal_ref,
        )

    def to_record(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "namespace": self.namespace,
            "session_id": self.session_id,
            "plan_id": self.plan_id,
            "revision": self.revision,
            "step_id": self.step_id,
            "binding_id": self.binding_id,
            "retries_binding_id": self.retries_binding_id,
            "definition_digest": self.definition_digest,
            "envelope_digest": self.envelope_digest,
            "references": self.references.to_record(),
            "principal_ref": self.principal_ref,
        }

    @classmethod
    def from_record(cls, value: Any) -> "ExecutionBindingRequest":
        fields = {
            "schema_version", "namespace", "session_id", "plan_id", "revision", "step_id",
            "binding_id", "retries_binding_id", "definition_digest", "envelope_digest",
            "references", "principal_ref",
        }
        record = _record(value, "execution binding request", fields)
        return cls(
            namespace=record["namespace"],
            session_id=record["session_id"],
            plan_id=record["plan_id"],
            revision=record["revision"],
            step_id=record["step_id"],
            binding_id=record["binding_id"],
            retries_binding_id=record["retries_binding_id"],
            definition_digest=record["definition_digest"],
            envelope_digest=record["envelope_digest"],
            references=ExecutionBindingReferences.from_record(record["references"]),
            principal_ref=record["principal_ref"],
            schema_version=record["schema_version"],
        )

    @property
    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(_REQUEST_DOMAIN + _canonical_bytes(self.to_record())).hexdigest()


def _references_for_step(
    envelope: ExecutionEnvelope, step: StepDefinition
) -> ExecutionBindingReferences:
    if not isinstance(envelope, ExecutionEnvelope) or not isinstance(step, StepDefinition):
        raise ContractError("effective execution references require an envelope and StepDefinition")
    return ExecutionBindingReferences(
        profile_ref=step.execution_profile_ref or envelope.profile_ref,
        workspace_ref=step.workspace_ref or envelope.workspace_ref,
        policy_ref=envelope.policy_ref,
        deployment_ref=envelope.deployment_ref,
        budget_ref=envelope.budget_ref,
    )


@dataclass(frozen=True)
class ExecutionDeploymentDescriptor:
    """Safe fingerprints describing the deployment actually assembled by the host."""

    profile_fingerprint: str
    tool_visibility_fingerprint: str
    policy_fingerprint: str
    deployment_fingerprint: str
    budget_fingerprint: str
    workspace_identity: str
    workspace_version: str
    capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "profile_fingerprint", "tool_visibility_fingerprint", "policy_fingerprint",
            "deployment_fingerprint", "budget_fingerprint", "workspace_identity", "workspace_version",
        ):
            _text(getattr(self, name), name)
        if not isinstance(self.capabilities, (tuple, list)):
            raise ContractError("capabilities must be a finite sequence of stable identifiers")
        values = tuple(_identifier(value, "capability") for value in self.capabilities)
        if len(values) != len(set(values)):
            raise ContractError("capabilities must not contain duplicates")
        object.__setattr__(self, "capabilities", tuple(sorted(values)))

    def to_record(self) -> dict[str, Any]:
        return {
            "profile_fingerprint": self.profile_fingerprint,
            "tool_visibility_fingerprint": self.tool_visibility_fingerprint,
            "policy_fingerprint": self.policy_fingerprint,
            "deployment_fingerprint": self.deployment_fingerprint,
            "budget_fingerprint": self.budget_fingerprint,
            "workspace_identity": self.workspace_identity,
            "workspace_version": self.workspace_version,
            "capabilities": list(self.capabilities),
        }

    @classmethod
    def from_record(cls, value: Any) -> "ExecutionDeploymentDescriptor":
        fields = {
            "profile_fingerprint", "tool_visibility_fingerprint", "policy_fingerprint",
            "deployment_fingerprint", "budget_fingerprint", "workspace_identity", "workspace_version",
            "capabilities",
        }
        record = _record(value, "execution deployment descriptor", fields)
        return cls(**record)


@dataclass(frozen=True)
class ResolvedExecutionBinding:
    """The immutable result of resolving and describing one approved step deployment."""

    request: ExecutionBindingRequest
    resolver_ref: VersionedRef
    actual: ExecutionDeploymentDescriptor
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.request, ExecutionBindingRequest):
            raise ContractError("request must be an ExecutionBindingRequest")
        if not isinstance(self.resolver_ref, VersionedRef):
            raise ContractError("resolver_ref must be a VersionedRef")
        _text(self.resolver_ref.ref, "resolver_ref.ref")
        _text(self.resolver_ref.version, "resolver_ref.version")
        if not isinstance(self.actual, ExecutionDeploymentDescriptor):
            raise ContractError("actual must be an ExecutionDeploymentDescriptor")
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ContractError("unsupported resolved execution binding schema")

    def to_record(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "request": self.request.to_record(),
            "resolver_ref": self.resolver_ref.to_record(),
            "actual": self.actual.to_record(),
        }

    @classmethod
    def from_record(cls, value: Any) -> "ResolvedExecutionBinding":
        fields = {"schema_version", "request", "resolver_ref", "actual"}
        record = _record(value, "resolved execution binding", fields)
        return cls(
            request=ExecutionBindingRequest.from_record(record["request"]),
            resolver_ref=_versioned_ref_from_record(record["resolver_ref"], "resolver_ref"),
            actual=ExecutionDeploymentDescriptor.from_record(record["actual"]),
            schema_version=record["schema_version"],
        )

    @property
    def request_digest(self) -> str:
        return self.request.digest

    @property
    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(_BINDING_DOMAIN + _canonical_bytes(self.to_record())).hexdigest()

    def assert_request_matches(self, request: ExecutionBindingRequest) -> None:
        if not isinstance(request, ExecutionBindingRequest) or self.request != request:
            raise ExecutionBindingRefused(
                "binding_request_mismatch", "resolved binding does not match the current admission request"
            )


class ExecutionBindingRefused(ContractError):
    """A resolver refused an approved reference with a stable, safe reason."""

    code = "execution_binding_refused"

    def __init__(self, reason_code: str, detail: str) -> None:
        self.reason_code = _identifier(reason_code, "reason_code")
        self.detail = _text(detail, "detail", max_bytes=_MAX_TEXT_BYTES)
        super().__init__(f"{self.reason_code}: {self.detail}")


class ExecutionDispatchBoundary(str, Enum):
    MODEL_DISPATCH = "model_dispatch"
    TOOL_DISPATCH = "tool_dispatch"


@dataclass(frozen=True)
class ExecutionBindingGuardResult:
    """A structured answer from a dispatch-time binding guard."""

    allowed: bool
    reason_code: str = ""
    detail: str = ""

    def __post_init__(self) -> None:
        if type(self.allowed) is not bool:
            raise ContractError("allowed must be a bool")
        _text(self.reason_code, "reason_code", allow_empty=True, max_bytes=120)
        _text(self.detail, "detail", allow_empty=True, max_bytes=_MAX_TEXT_BYTES)
        if self.allowed and (self.reason_code or self.detail):
            raise ContractError("an allowed guard result must not carry refusal fields")
        if not self.allowed:
            _identifier(self.reason_code, "reason_code")
            if not self.detail:
                raise ContractError("a refused guard result requires a bounded explanation")


class ExecutionBindingDispatch:
    """Transient dispatch-time checks for one persisted Plan binding.

    This object carries live host callables and must never be serialized. The
    descriptor supplier is consulted at every boundary so a mutable deployment
    cannot keep dispatching from a stale preflight description.
    """

    def __init__(
        self,
        binding: ResolvedExecutionBinding,
        guard: "ExecutionBindingGuard",
        actual_for: Any,
    ) -> None:
        if not isinstance(binding, ResolvedExecutionBinding):
            raise ContractError("binding must be a ResolvedExecutionBinding")
        if not callable(getattr(guard, "check", None)):
            raise ContractError("execution binding guard must provide check")
        if not callable(actual_for):
            raise ContractError("actual deployment supplier must be callable")
        self.binding = binding
        self._guard = guard
        self._actual_for = actual_for

    async def _actual(self) -> ExecutionDeploymentDescriptor:
        try:
            value = self._actual_for()
            if hasattr(value, "__await__"):
                value = await value
        except ExecutionBindingRefused:
            raise
        except Exception as exc:  # noqa: BLE001 - host inspection failure is a refusal
            raise ExecutionBindingRefused(
                "actual_deployment_unavailable", f"deployment inspection failed ({type(exc).__name__})"
            ) from exc
        if not isinstance(value, ExecutionDeploymentDescriptor):
            raise ExecutionBindingRefused(
                "actual_deployment_invalid", "deployment supplier returned an unsupported descriptor"
            )
        if value != self.binding.actual:
            raise ExecutionBindingRefused(
                "actual_deployment_mismatch", "the running deployment differs from the persisted Plan binding"
            )
        return value

    async def assert_actual_matches(self) -> None:
        await self._actual()

    async def check_model_dispatch(self) -> None:
        await self.check(ExecutionDispatchBoundary.MODEL_DISPATCH)

    async def check_tool_dispatch(self) -> None:
        await self.check(ExecutionDispatchBoundary.TOOL_DISPATCH)

    async def check(self, boundary: ExecutionDispatchBoundary) -> None:
        if not isinstance(boundary, ExecutionDispatchBoundary):
            raise ContractError("boundary must be an ExecutionDispatchBoundary")
        actual = await self._actual()
        try:
            result = self._guard.check(self.binding, actual, boundary=boundary)
            if hasattr(result, "__await__"):
                result = await result
        except ExecutionBindingRefused:
            raise
        except Exception as exc:  # noqa: BLE001 - an unavailable guard must fail closed
            raise ExecutionBindingRefused(
                "binding_guard_failed", f"dispatch guard failed ({type(exc).__name__})"
            ) from exc
        if not isinstance(result, ExecutionBindingGuardResult):
            raise ExecutionBindingRefused(
                "binding_guard_invalid", "dispatch guard returned an unsupported result"
            )
        if not result.allowed:
            raise ExecutionBindingRefused(result.reason_code, result.detail)


@runtime_checkable
class ExecutionBindingResolver(Protocol):
    """Read-only host port that resolves approved references outside control transactions."""

    async def resolve(self, request: ExecutionBindingRequest) -> ResolvedExecutionBinding:
        """Resolve one request or raise :class:`ExecutionBindingRefused`."""


@runtime_checkable
class ExecutionBindingGuard(Protocol):
    """Host port rechecked immediately before each external model/tool dispatch."""

    async def check(
        self,
        binding: ResolvedExecutionBinding,
        actual: ExecutionDeploymentDescriptor,
        *,
        boundary: ExecutionDispatchBoundary,
    ) -> ExecutionBindingGuardResult:
        """Return a bounded allow/refuse fact for this dispatch boundary."""


__all__ = [
    "ExecutionBindingGuard",
    "ExecutionBindingGuardResult",
    "ExecutionBindingDispatch",
    "ExecutionBindingRefused",
    "ExecutionBindingReferences",
    "ExecutionBindingRequest",
    "ExecutionBindingResolver",
    "ExecutionDeploymentDescriptor",
    "ExecutionDispatchBoundary",
    "ResolvedExecutionBinding",
    "step_binding_id",
]
