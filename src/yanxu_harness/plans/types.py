"""Immutable plan definitions and the small vocabulary used by the DAG layer."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from ..errors import ContractError

PLAN_SCHEMA_VERSION = 1
PLAN_MAX_STEPS = 128
PLAN_MAX_EDGES = 1024
PLAN_MAX_BYTES = 256 * 1024
PLAN_MAX_DEPTH = 32
PLAN_MAX_CRITERIA = 64
PLAN_MAX_STEP_EXECUTIONS = 256
PLAN_DEFAULT_MAX_STEP_EXECUTIONS = 256
PLAN_DEFAULT_MAX_CONCURRENCY = 1

_STABLE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,119}$")


class PlanRefused(ContractError):
    """A plan payload or control action was refused with a stable reason code."""

    code = "plan_refused"

    def __init__(self, reason_code: str, detail: str = "") -> None:
        self.reason_code = reason_code
        self.detail = detail or reason_code
        super().__init__(self.detail)


class FrozenJSONDict(dict):
    """A JSON-serializable dict that refuses mutation after construction."""

    def _immutable(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("this plan value is frozen")

    __setitem__ = _immutable  # type: ignore[assignment]
    __delitem__ = _immutable  # type: ignore[assignment]
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable

    def copy(self) -> "FrozenJSONDict":
        return self

    def __reduce__(self):  # pragma: no cover - defensive; definitions stay in-process values
        raise TypeError("a frozen plan mapping is not picklable")


def _text(value: Any, name: str, *, max_bytes: int | None = None, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise PlanRefused("invalid_definition", f"{name} must be a string")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise PlanRefused("invalid_definition", f"{name} contains an isolated surrogate") from exc
    if not allow_empty and not value.strip():
        raise PlanRefused("invalid_definition", f"{name} must not be empty")
    if max_bytes is not None and len(encoded) > max_bytes:
        raise PlanRefused("invalid_definition", f"{name} exceeds {max_bytes} UTF-8 bytes")
    return value


def _stable_id(value: Any, name: str) -> str:
    text = _text(value, name)
    if not _STABLE_ID_RE.fullmatch(text):
        raise PlanRefused("invalid_definition", f"{name} is not a stable ID: {text!r}")
    return text


def _tuple_of(value: Any, name: str, expected_type: type | None = None) -> tuple[Any, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (tuple, list)):
        raise ContractError(f"{name} must be a tuple or list")
    result = tuple(value)
    if expected_type is not None and any(not isinstance(item, expected_type) for item in result):
        raise ContractError(f"every {name} item must be {expected_type.__name__}")
    return result


def freeze_json(value: Any, *, where: str = "value", depth: int = 0) -> Any:
    """Copy and deeply freeze the restricted JSON subset used in signed definitions."""
    if depth > PLAN_MAX_DEPTH:
        raise PlanRefused("invalid_definition", f"{where} exceeds the maximum structure depth")
    if value is None or isinstance(value, bool):
        return value
    if type(value) is int:
        return value
    if isinstance(value, float):
        raise PlanRefused("invalid_definition", f"{where} cannot contain floating-point values")
    if isinstance(value, str):
        _text(value, where, allow_empty=True)
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise PlanRefused("invalid_definition", f"{where} object keys must be strings")
            _text(key, f"{where} key", allow_empty=True)
            frozen[key] = freeze_json(item, where=f"{where}.{key}", depth=depth + 1)
        return FrozenJSONDict(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(
            freeze_json(item, where=f"{where}[{index}]", depth=depth + 1)
            for index, item in enumerate(value)
        )
    raise PlanRefused(
        "invalid_definition", f"{where} contains a non-JSON value ({type(value).__name__})"
    )


class PlanStatus(str, Enum):
    DRAFT = "draft"
    REVIEWING = "reviewing"
    APPROVED = "approved"
    EXECUTING = "executing"
    PAUSED = "paused"
    REPLANNING = "replanning"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in {PlanStatus.COMPLETED, PlanStatus.FAILED, PlanStatus.CANCELLED}


class StepViewStatus(str, Enum):
    BLOCKED = "blocked"
    READY = "ready"
    RUNNING = "running"
    WAITING = "waiting"
    AWAITING_VALIDATION = "awaiting_validation"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


class StepOutcome(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INCONCLUSIVE = "inconclusive"


class AcceptanceResult(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class AcceptanceRecord:
    """An immutable verifier decision tied to one evidence snapshot and criterion."""

    acceptance_id: str
    namespace: str
    plan_id: str
    revision: int
    step_id: str
    binding_id: str
    criterion_id: str
    verifier_id: str
    verifier_version: str
    criterion_digest: str
    result: AcceptanceResult
    evidence_refs: tuple[str, ...]
    source_snapshot_digest: str
    decided_by: str
    decided_at_ms: int
    request_id: str
    reason_code: str = ""
    scope: str = "step"
    run_version: int = 0

    def __post_init__(self) -> None:
        for name in (
            "acceptance_id", "namespace", "plan_id", "step_id", "binding_id",
            "criterion_id", "verifier_id", "verifier_version", "criterion_digest",
            "source_snapshot_digest", "decided_by", "request_id",
        ):
            _text(getattr(self, name), name)
        _positive_or_zero_int(self.revision, "revision", allow_zero=False)
        _positive_or_zero_int(self.decided_at_ms, "decided_at_ms", allow_zero=True)
        _positive_or_zero_int(self.run_version, "run_version", allow_zero=True)
        if self.scope not in {"step", "plan"}:
            raise PlanRefused("invalid_definition", "acceptance scope must be 'step' or 'plan'")
        if self.scope == "plan" and self.step_id != "plan":
            raise PlanRefused("invalid_definition", "plan-scope acceptance must use step_id='plan'")
        try:
            object.__setattr__(self, "result", AcceptanceResult(self.result))
        except (TypeError, ValueError) as exc:
            raise PlanRefused("invalid_definition", "unknown acceptance result") from exc
        refs = _tuple_of(self.evidence_refs, "evidence_refs")
        for index, reference in enumerate(refs):
            _text(reference, f"evidence_refs[{index}]")
        object.__setattr__(self, "evidence_refs", refs)
        _text(self.reason_code, "reason_code", max_bytes=256, allow_empty=True)

    def to_record(self) -> Mapping[str, Any]:
        return {
            "acceptance_id": self.acceptance_id,
            "namespace": self.namespace,
            "plan_id": self.plan_id,
            "revision": self.revision,
            "step_id": self.step_id,
            "binding_id": self.binding_id,
            "criterion_id": self.criterion_id,
            "verifier_id": self.verifier_id,
            "verifier_version": self.verifier_version,
            "criterion_digest": self.criterion_digest,
            "result": self.result.value,
            "evidence_refs": list(self.evidence_refs),
            "source_snapshot_digest": self.source_snapshot_digest,
            "decided_by": self.decided_by,
            "decided_at_ms": self.decided_at_ms,
            "request_id": self.request_id,
            "reason_code": self.reason_code,
            "scope": self.scope,
            "run_version": self.run_version,
        }


@dataclass(frozen=True)
class QuiescenceProof:
    """Host evidence that one terminal Task/Run binding can no longer execute."""

    namespace: str
    plan_id: str
    revision: int
    step_id: str
    binding_id: str
    task_id: str
    run_id: str
    task_status: str
    run_status: str
    run_version: int
    source_snapshot_digest: str
    unresolved_operations: tuple[str, ...] = ()
    read_error: str = ""

    def __post_init__(self) -> None:
        for name in (
            "namespace", "plan_id", "step_id", "binding_id", "task_id", "run_id",
            "task_status", "run_status", "source_snapshot_digest",
        ):
            _text(getattr(self, name), name)
        _positive_or_zero_int(self.revision, "revision", allow_zero=False)
        _positive_or_zero_int(self.run_version, "run_version", allow_zero=False)
        operations = _tuple_of(self.unresolved_operations, "unresolved_operations")
        for index, operation in enumerate(operations):
            _text(operation, f"unresolved_operations[{index}]")
        object.__setattr__(self, "unresolved_operations", operations)
        _text(self.read_error, "read_error", max_bytes=256, allow_empty=True)


class PlanSource(str, Enum):
    HOST = "host"
    USER = "user"
    MODEL = "model"


class ApprovalDecision(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"


class ConstraintEnforcement(str, Enum):
    DECLARATIVE = "declarative"
    HOST_ENFORCED = "host_enforced"


@dataclass(frozen=True)
class VersionedRef:
    ref: str
    version: str

    def __post_init__(self) -> None:
        _text(self.ref, "reference")
        _text(self.version, "reference version")

    def to_record(self) -> Mapping[str, str]:
        return {"ref": self.ref, "version": self.version}


@dataclass(frozen=True)
class RunReference:
    namespace: str
    session_id: str
    run_id: str

    def __post_init__(self) -> None:
        for name in ("namespace", "session_id", "run_id"):
            _text(getattr(self, name), name)

    def to_record(self) -> Mapping[str, str]:
        return {
            "namespace": self.namespace,
            "session_id": self.session_id,
            "run_id": self.run_id,
        }


@dataclass(frozen=True)
class ResourceRef:
    namespace: str
    owner_kind: str
    owner_id: str
    version: str
    kind: str
    digest: str

    def __post_init__(self) -> None:
        for name in ("namespace", "owner_kind", "owner_id", "version", "kind", "digest"):
            _text(getattr(self, name), name)

    def to_record(self) -> Mapping[str, str]:
        return {
            "namespace": self.namespace,
            "owner_kind": self.owner_kind,
            "owner_id": self.owner_id,
            "version": self.version,
            "kind": self.kind,
            "digest": self.digest,
        }


@dataclass(frozen=True)
class AcceptanceCriterion:
    criterion_id: str
    verifier_id: str
    verifier_version: str
    config: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _stable_id(self.criterion_id, "criterion_id")
        _stable_id(self.verifier_id, "verifier_id")
        _text(self.verifier_version, "verifier_version")
        if not isinstance(self.config, Mapping):
            raise ContractError("criterion config must be a JSON object")
        object.__setattr__(self, "config", freeze_json(self.config, where="criterion config"))

    def to_record(self) -> Mapping[str, Any]:
        return {
            "criterion_id": self.criterion_id,
            "verifier_id": self.verifier_id,
            "verifier_version": self.verifier_version,
            "config": self.config,
        }


@dataclass(frozen=True)
class Constraint:
    constraint_id: str
    text: str
    enforcement: ConstraintEnforcement | str = ConstraintEnforcement.DECLARATIVE
    enforcer_ref: VersionedRef | None = None

    def __post_init__(self) -> None:
        _stable_id(self.constraint_id, "constraint_id")
        _text(self.text, "constraint text", max_bytes=4096)
        try:
            enforcement = ConstraintEnforcement(self.enforcement)
        except (TypeError, ValueError) as exc:
            raise PlanRefused("invalid_definition", "unknown constraint enforcement mode") from exc
        object.__setattr__(self, "enforcement", enforcement)
        if enforcement is ConstraintEnforcement.HOST_ENFORCED and self.enforcer_ref is None:
            raise PlanRefused("invalid_definition", "host-enforced constraints require an enforcer_ref")
        if self.enforcer_ref is not None and not isinstance(self.enforcer_ref, VersionedRef):
            raise ContractError("enforcer_ref must be a VersionedRef")

    def to_record(self) -> Mapping[str, Any]:
        return {
            "constraint_id": self.constraint_id,
            "text": self.text,
            "enforcement": self.enforcement.value,
            "enforcer_ref": None if self.enforcer_ref is None else self.enforcer_ref.to_record(),
        }


@dataclass(frozen=True)
class ExecutionEnvelope:
    profile_ref: VersionedRef
    workspace_ref: VersionedRef
    policy_ref: VersionedRef
    deployment_ref: VersionedRef
    budget_ref: VersionedRef

    def __post_init__(self) -> None:
        for name in ("profile_ref", "workspace_ref", "policy_ref", "deployment_ref", "budget_ref"):
            if not isinstance(getattr(self, name), VersionedRef):
                raise ContractError(f"execution envelope {name} must be a VersionedRef")

    def to_record(self) -> Mapping[str, Any]:
        return {
            name: getattr(self, name).to_record()
            for name in ("profile_ref", "workspace_ref", "policy_ref", "deployment_ref", "budget_ref")
        }

    @property
    def digest(self) -> str:
        from .codec import execution_envelope_digest

        return execution_envelope_digest(self)


@dataclass(frozen=True)
class StepDefinition:
    step_id: str
    title: str
    instruction: str
    acceptance_criteria: tuple[AcceptanceCriterion, ...]
    depends_on: tuple[str, ...] = ()
    input_refs: tuple[ResourceRef, ...] = ()
    required_context_refs: tuple[ResourceRef, ...] = ()
    execution_profile_ref: VersionedRef | None = None
    workspace_ref: VersionedRef | None = None
    output_contract: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        _stable_id(self.step_id, "step_id")
        title = _text(self.title, "step title", max_bytes=200).strip()
        if not title:
            raise PlanRefused("invalid_definition", "step title must not be empty")
        object.__setattr__(self, "title", title)
        _text(self.instruction, "step instruction", max_bytes=32 * 1024)
        for name in ("acceptance_criteria",):
            items = _tuple_of(getattr(self, name), name, AcceptanceCriterion)
            if not 1 <= len(items) <= PLAN_MAX_CRITERIA:
                raise PlanRefused("invalid_definition", f"a step must have 1–{PLAN_MAX_CRITERIA} criteria")
            _assert_unique((item.criterion_id for item in items), name)
            object.__setattr__(self, name, items)
        for name, item_type in (("input_refs", ResourceRef), ("required_context_refs", ResourceRef)):
            object.__setattr__(self, name, _tuple_of(getattr(self, name), name, item_type))
        dependencies = _tuple_of(self.depends_on, "depends_on")
        for dependency in dependencies:
            _stable_id(dependency, "dependency step_id")
        object.__setattr__(self, "depends_on", dependencies)
        for name in ("execution_profile_ref", "workspace_ref"):
            reference = getattr(self, name)
            if reference is not None and not isinstance(reference, VersionedRef):
                raise ContractError(f"{name} must be a VersionedRef when provided")
        if self.output_contract is not None:
            if not isinstance(self.output_contract, Mapping):
                raise ContractError("output_contract must be a JSON object")
            object.__setattr__(
                self,
                "output_contract",
                freeze_json(self.output_contract, where="output_contract"),
            )

    def to_record(self) -> Mapping[str, Any]:
        return {
            "step_id": self.step_id,
            "title": self.title.strip(),
            "instruction": self.instruction,
            "depends_on": list(self.depends_on),
            "acceptance_criteria": [item.to_record() for item in self.acceptance_criteria],
            "input_refs": [item.to_record() for item in self.input_refs],
            "required_context_refs": [item.to_record() for item in self.required_context_refs],
            "execution_profile_ref": None if self.execution_profile_ref is None else self.execution_profile_ref.to_record(),
            "workspace_ref": None if self.workspace_ref is None else self.workspace_ref.to_record(),
            "output_contract": self.output_contract,
        }


def _assert_unique(values: Any, name: str) -> None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise PlanRefused("invalid_definition", f"{name} contains duplicate identity {value!r}")
        seen.add(value)


@dataclass(frozen=True)
class PlanDefinition:
    namespace: str
    session_id: str
    plan_id: str
    revision: int
    title: str
    goal: str
    steps: tuple[StepDefinition, ...]
    execution_envelope: ExecutionEnvelope
    constraints: tuple[Constraint, ...] = ()
    acceptance_criteria: tuple[AcceptanceCriterion, ...] = ()
    risks: tuple[str, ...] = ()
    expected_changes: tuple[str, ...] = ()
    created_by: str = "host"
    created_at: float = 0.0
    source: PlanSource | str = PlanSource.HOST
    proposal_run_ref: RunReference | None = None
    supersedes_revision: int | None = None
    replan_reason: str = ""
    max_step_executions: int = PLAN_DEFAULT_MAX_STEP_EXECUTIONS
    deadline_at_ms: int | None = None
    max_concurrency: int = PLAN_DEFAULT_MAX_CONCURRENCY
    schema_version: int = PLAN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != PLAN_SCHEMA_VERSION:
            raise PlanRefused(
                "unsupported_schema_version",
                f"plan schema {self.schema_version!r} is not supported",
            )
        for name in ("namespace", "session_id", "plan_id", "created_by"):
            _text(getattr(self, name), name)
        title = _text(self.title, "title", max_bytes=200).strip()
        goal = _text(self.goal, "goal", max_bytes=4000).strip()
        if not title:
            raise PlanRefused("invalid_definition", "title must not be empty")
        if not goal:
            raise PlanRefused("invalid_definition", "goal must not be empty")
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "goal", goal)
        if type(self.revision) is not int or self.revision < 1:
            raise PlanRefused("invalid_definition", "revision must be a positive integer")
        if not isinstance(self.execution_envelope, ExecutionEnvelope):
            raise ContractError("execution_envelope must be an ExecutionEnvelope")
        if isinstance(self.created_at, bool) or not isinstance(self.created_at, (int, float)):
            raise ContractError("created_at must be a finite non-negative timestamp")
        if not math.isfinite(float(self.created_at)) or self.created_at < 0:
            raise ContractError("created_at must be a finite non-negative timestamp")
        object.__setattr__(self, "created_at", float(self.created_at))
        try:
            object.__setattr__(self, "source", PlanSource(self.source))
        except (TypeError, ValueError) as exc:
            raise PlanRefused("invalid_definition", "unknown plan source") from exc
        for name, expected_type, maximum in (
            ("steps", StepDefinition, PLAN_MAX_STEPS),
            ("constraints", Constraint, PLAN_MAX_CRITERIA),
            ("acceptance_criteria", AcceptanceCriterion, PLAN_MAX_CRITERIA),
        ):
            items = _tuple_of(getattr(self, name), name, expected_type)
            if name == "steps" and not 1 <= len(items) <= maximum:
                raise PlanRefused("invalid_definition", f"a plan must contain 1–{maximum} steps")
            if name != "steps" and len(items) > maximum:
                raise PlanRefused("invalid_definition", f"{name} exceeds {maximum} entries")
            object.__setattr__(self, name, items)
        _assert_unique((item.step_id for item in self.steps), "steps")
        _assert_unique((item.constraint_id for item in self.constraints), "constraints")
        _assert_unique((item.criterion_id for item in self.acceptance_criteria), "acceptance_criteria")
        for name in ("risks", "expected_changes"):
            values = _tuple_of(getattr(self, name), name)
            if len(values) > 64:
                raise PlanRefused("invalid_definition", f"{name} exceeds 64 entries")
            for index, value in enumerate(values):
                _text(value, f"{name}[{index}]", max_bytes=4096)
            object.__setattr__(self, name, values)
        if self.proposal_run_ref is not None and not isinstance(self.proposal_run_ref, RunReference):
            raise ContractError("proposal_run_ref must be a RunReference")
        if self.supersedes_revision is not None and (
            type(self.supersedes_revision) is not int
            or self.supersedes_revision < 1
            or self.supersedes_revision >= self.revision
        ):
            raise PlanRefused("invalid_definition", "supersedes_revision must precede the current revision")
        _text(self.replan_reason, "replan_reason", max_bytes=4096, allow_empty=True)
        _positive_or_zero_int(self.max_step_executions, "max_step_executions", allow_zero=True)
        if self.max_step_executions > PLAN_MAX_STEP_EXECUTIONS:
            raise PlanRefused(
                "invalid_definition",
                f"max_step_executions cannot exceed {PLAN_MAX_STEP_EXECUTIONS}",
            )
        _positive_or_zero_int(self.max_concurrency, "max_concurrency", allow_zero=False)
        if self.max_concurrency > PLAN_MAX_STEPS:
            raise PlanRefused("invalid_definition", f"max_concurrency cannot exceed {PLAN_MAX_STEPS}")
        if self.deadline_at_ms is not None:
            _positive_or_zero_int(self.deadline_at_ms, "deadline_at_ms", allow_zero=True)

        # Graph checks are pure and intentionally run during construction, before review.
        from .graph import validate_graph

        validate_graph(self.steps)

        from .codec import canonical_plan_bytes

        if len(canonical_plan_bytes(self)) > PLAN_MAX_BYTES:
            raise PlanRefused("invalid_definition", f"definition exceeds {PLAN_MAX_BYTES} UTF-8 bytes")

    @property
    def definition_digest(self) -> str:
        from .codec import definition_digest

        return definition_digest(self)

    @property
    def envelope_digest(self) -> str:
        return self.execution_envelope.digest

    def to_record(self) -> Mapping[str, Any]:
        from .codec import plan_definition_to_record

        return plan_definition_to_record(self)

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "PlanDefinition":
        from .codec import plan_definition_from_record

        return plan_definition_from_record(record)


@dataclass(frozen=True)
class PlanRecord:
    namespace: str
    session_id: str
    plan_id: str
    current_revision: int
    control_version: int
    status: PlanStatus
    definition_digest: str
    active_approval_id: str = ""
    reason: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    schema_version: int = 1

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise PlanRefused("unsupported_schema_version", "stored PlanRecord schema is not supported")
        for name in ("namespace", "session_id", "plan_id", "definition_digest"):
            _text(getattr(self, name), name)
        _positive_or_zero_int(self.current_revision, "current_revision", allow_zero=False)
        _positive_or_zero_int(self.control_version, "control_version", allow_zero=False)
        _text(self.active_approval_id, "active_approval_id", allow_empty=True)
        _text(self.reason, "reason", max_bytes=4096, allow_empty=True)
        try:
            object.__setattr__(self, "status", PlanStatus(self.status))
        except (TypeError, ValueError) as exc:
            raise ContractError("PlanRecord has an unknown status") from exc
        for name in ("created_at", "updated_at"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise ContractError(f"{name} must be a finite timestamp")
            object.__setattr__(self, name, float(value))

    def to_record(self) -> Mapping[str, Any]:
        return {
            "schema_version": self.schema_version,
            "namespace": self.namespace,
            "session_id": self.session_id,
            "plan_id": self.plan_id,
            "current_revision": self.current_revision,
            "control_version": self.control_version,
            "status": self.status.value,
            "definition_digest": self.definition_digest,
            "active_approval_id": self.active_approval_id,
            "reason": self.reason,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class PlanApprovalRequest:
    decision_id: str
    decision: ApprovalDecision | str
    approval_policy_version: str
    expires_at_ms: int | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        _text(self.decision_id, "decision_id")
        _text(self.approval_policy_version, "approval_policy_version")
        try:
            object.__setattr__(self, "decision", ApprovalDecision(self.decision))
        except (TypeError, ValueError) as exc:
            raise PlanRefused("invalid_definition", "approval decision must be approved or rejected") from exc
        if self.expires_at_ms is not None:
            _positive_or_zero_int(self.expires_at_ms, "expires_at_ms", allow_zero=True)
        _text(self.reason, "approval reason", max_bytes=4096, allow_empty=True)


@dataclass(frozen=True)
class PlanApproval:
    namespace: str
    plan_id: str
    revision: int
    definition_digest: str
    execution_envelope_digest: str
    approval_policy_version: str
    decision_id: str
    decision: ApprovalDecision
    decided_by: str
    decided_at_ms: int
    expires_at_ms: int | None = None
    reason: str = ""
    schema_version: int = 1

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise PlanRefused("unsupported_schema_version", "stored PlanApproval schema is not supported")
        for name in (
            "namespace", "plan_id", "definition_digest", "execution_envelope_digest",
            "approval_policy_version", "decision_id", "decided_by",
        ):
            _text(getattr(self, name), name)
        _positive_or_zero_int(self.revision, "revision", allow_zero=False)
        _positive_or_zero_int(self.decided_at_ms, "decided_at_ms", allow_zero=True)
        if self.expires_at_ms is not None:
            _positive_or_zero_int(self.expires_at_ms, "expires_at_ms", allow_zero=True)
        _text(self.reason, "approval reason", max_bytes=4096, allow_empty=True)
        try:
            object.__setattr__(self, "decision", ApprovalDecision(self.decision))
        except (TypeError, ValueError) as exc:
            raise ContractError("approval has an unknown decision") from exc

    def to_record(self) -> Mapping[str, Any]:
        return {
            "schema_version": self.schema_version,
            "namespace": self.namespace,
            "plan_id": self.plan_id,
            "revision": self.revision,
            "definition_digest": self.definition_digest,
            "execution_envelope_digest": self.execution_envelope_digest,
            "approval_policy_version": self.approval_policy_version,
            "decision_id": self.decision_id,
            "decision": self.decision.value,
            "decided_by": self.decided_by,
            "decided_at_ms": self.decided_at_ms,
            "expires_at_ms": self.expires_at_ms,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ApprovalRevocation:
    namespace: str
    plan_id: str
    decision_id: str
    revoked_by: str
    revoked_at_ms: int
    reason: str = ""

    def __post_init__(self) -> None:
        for name in ("namespace", "plan_id", "decision_id", "revoked_by"):
            _text(getattr(self, name), name)
        _positive_or_zero_int(self.revoked_at_ms, "revoked_at_ms", allow_zero=True)
        _text(self.reason, "revocation reason", max_bytes=4096, allow_empty=True)


@dataclass(frozen=True)
class PlanEvent:
    event_id: str
    namespace: str
    plan_id: str
    control_version: int
    event_type: str
    payload: Mapping[str, Any]
    created_at: float

    def __post_init__(self) -> None:
        for name in ("event_id", "namespace", "plan_id", "event_type"):
            _text(getattr(self, name), name)
        _positive_or_zero_int(self.control_version, "control_version", allow_zero=False)
        if not isinstance(self.payload, Mapping):
            raise ContractError("plan event payload must be a JSON object")
        object.__setattr__(self, "payload", freeze_json(self.payload, where="plan event payload"))
        if isinstance(self.created_at, bool) or not isinstance(self.created_at, (int, float)) or not math.isfinite(float(self.created_at)):
            raise ContractError("plan event created_at must be a finite timestamp")
        object.__setattr__(self, "created_at", float(self.created_at))

    def to_record(self) -> Mapping[str, Any]:
        return {
            "event_id": self.event_id,
            "namespace": self.namespace,
            "plan_id": self.plan_id,
            "control_version": self.control_version,
            "event_type": self.event_type,
            "payload": self.payload,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class PlanOutboxEntry:
    outbox_id: str
    namespace: str
    plan_id: str
    event_id: str
    kind: str
    payload: Mapping[str, Any]
    created_at: float
    delivered_at: float | None = None
    attempts: int = 0
    last_error: str = ""

    def __post_init__(self) -> None:
        for name in ("outbox_id", "namespace", "plan_id", "event_id", "kind"):
            _text(getattr(self, name), name)
        if not isinstance(self.payload, Mapping):
            raise ContractError("plan outbox payload must be a JSON object")
        object.__setattr__(self, "payload", freeze_json(self.payload, where="plan outbox payload"))
        _positive_or_zero_int(self.attempts, "attempts", allow_zero=True)
        _text(self.last_error, "last_error", max_bytes=4096, allow_empty=True)

    @property
    def pending(self) -> bool:
        return self.delivered_at is None

    def delivered(self, *, at: float) -> "PlanOutboxEntry":
        from dataclasses import replace

        return replace(self, delivered_at=at, attempts=self.attempts + 1, last_error="")

    def failed(self, *, error: str) -> "PlanOutboxEntry":
        from dataclasses import replace

        return replace(self, attempts=self.attempts + 1, last_error=error)

    def to_record(self) -> Mapping[str, Any]:
        return {
            "outbox_id": self.outbox_id,
            "namespace": self.namespace,
            "plan_id": self.plan_id,
            "event_id": self.event_id,
            "kind": self.kind,
            "payload": self.payload,
            "created_at": self.created_at,
            "delivered_at": self.delivered_at,
            "attempts": self.attempts,
            "last_error": self.last_error,
        }


@dataclass(frozen=True)
class PlanSnapshot:
    record: PlanRecord
    definition: PlanDefinition
    approvals: tuple[PlanApproval, ...] = ()
    revocations: tuple[ApprovalRevocation, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.record, PlanRecord) or not isinstance(self.definition, PlanDefinition):
            raise ContractError("a plan snapshot requires its record and current definition")
        object.__setattr__(self, "approvals", _tuple_of(self.approvals, "approvals", PlanApproval))
        object.__setattr__(self, "revocations", _tuple_of(self.revocations, "revocations", ApprovalRevocation))


@dataclass(frozen=True)
class PlanStepCandidate:
    """An advisory scheduling row; it contains Plan identity, never a fabricated Task or Run."""

    namespace: str
    plan_id: str
    revision: int
    step_id: str
    priority: int
    ready_since: float
    control_version: int
    definition_digest: str

    def __post_init__(self) -> None:
        for name in ("namespace", "plan_id", "step_id", "definition_digest"):
            _text(getattr(self, name), name)
        _positive_or_zero_int(self.revision, "revision", allow_zero=False)
        _positive_or_zero_int(self.control_version, "control_version", allow_zero=False)
        if type(self.priority) is not int:
            raise ContractError("Plan step priority must be an integer")
        if isinstance(self.ready_since, bool) or not isinstance(self.ready_since, (int, float)):
            raise ContractError("ready_since must be a finite timestamp")
        if not math.isfinite(float(self.ready_since)):
            raise ContractError("ready_since must be a finite timestamp")
        object.__setattr__(self, "ready_since", float(self.ready_since))


@dataclass(frozen=True)
class PlanStepPage:
    """A bounded page of ready Plan steps and the continuation position after the scan."""

    candidates: tuple[PlanStepCandidate, ...]
    next_cursor: str | None
    scanned: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidates", _tuple_of(self.candidates, "candidates", PlanStepCandidate))
        if self.next_cursor is not None:
            _text(self.next_cursor, "next_cursor")
        _positive_or_zero_int(self.scanned, "scanned", allow_zero=True)


@dataclass(frozen=True)
class StepTaskBinding:
    """The immutable identity connecting one Plan step to its single Task and Run."""

    binding_id: str
    namespace: str
    plan_id: str
    revision: int
    step_id: str
    task_id: str
    run_id: str
    initial_input_digest: str
    request_id: str
    request_digest: str
    binding_status: str
    ready_since: float
    created_at: float
    accepted_at: float | None = None
    retries_binding_id: str = ""

    def __post_init__(self) -> None:
        for name in (
            "binding_id", "namespace", "plan_id", "step_id", "task_id", "run_id",
            "initial_input_digest", "request_id", "request_digest", "binding_status",
        ):
            _text(getattr(self, name), name)
        _text(self.retries_binding_id, "retries_binding_id", allow_empty=True)
        _positive_or_zero_int(self.revision, "revision", allow_zero=False)
        for name in ("ready_since", "created_at"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise ContractError(f"{name} must be a finite timestamp")
            object.__setattr__(self, name, float(value))
        if self.accepted_at is not None:
            value = self.accepted_at
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise ContractError("accepted_at must be a finite timestamp")
            object.__setattr__(self, "accepted_at", float(value))

    def to_record(self) -> Mapping[str, Any]:
        return {
            "binding_id": self.binding_id,
            "namespace": self.namespace,
            "plan_id": self.plan_id,
            "revision": self.revision,
            "step_id": self.step_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "initial_input_digest": self.initial_input_digest,
            "request_id": self.request_id,
            "request_digest": self.request_digest,
            "binding_status": self.binding_status,
            "ready_since": self.ready_since,
            "created_at": self.created_at,
            "accepted_at": self.accepted_at,
            "retries_binding_id": self.retries_binding_id,
        }


@dataclass(frozen=True)
class PlanStepClaim:
    """The Task and scheduling fence created by an atomic Plan step admission."""

    binding: StepTaskBinding
    task: Any
    claim: Any

    def __post_init__(self) -> None:
        if not isinstance(self.binding, StepTaskBinding):
            raise ContractError("a Plan step claim requires its durable binding")
        if self.task is None or self.claim is None:
            raise ContractError("a Plan step claim requires a Task and its dispatch claim")


def _positive_or_zero_int(value: Any, name: str, *, allow_zero: bool) -> int:
    minimum = 0 if allow_zero else 1
    if type(value) is not int or value < minimum:
        qualifier = "non-negative" if allow_zero else "positive"
        raise PlanRefused("invalid_definition", f"{name} must be a {qualifier} integer")
    return value


__all__ = [
    "AcceptanceRecord",
    "AcceptanceCriterion",
    "AcceptanceResult",
    "ApprovalDecision",
    "ApprovalRevocation",
    "Constraint",
    "ConstraintEnforcement",
    "ExecutionEnvelope",
    "FrozenJSONDict",
    "PLAN_DEFAULT_MAX_CONCURRENCY",
    "PLAN_DEFAULT_MAX_STEP_EXECUTIONS",
    "PLAN_MAX_BYTES",
    "PLAN_MAX_CRITERIA",
    "PLAN_MAX_DEPTH",
    "PLAN_MAX_EDGES",
    "PLAN_MAX_STEPS",
    "PLAN_MAX_STEP_EXECUTIONS",
    "PLAN_SCHEMA_VERSION",
    "PlanDefinition",
    "PlanApproval",
    "PlanApprovalRequest",
    "PlanEvent",
    "PlanOutboxEntry",
    "PlanRecord",
    "PlanRefused",
    "PlanSnapshot",
    "PlanStepCandidate",
    "PlanStepClaim",
    "PlanStepPage",
    "PlanSource",
    "PlanStatus",
    "ResourceRef",
    "RunReference",
    "StepDefinition",
    "StepTaskBinding",
    "StepOutcome",
    "StepViewStatus",
    "VersionedRef",
    "freeze_json",
]
