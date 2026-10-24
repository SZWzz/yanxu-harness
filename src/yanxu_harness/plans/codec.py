"""Versioned canonical mapping and JSON codec for plan definitions."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from ..errors import ContractError
from .types import (
    AcceptanceCriterion,
    Constraint,
    ExecutionEnvelope,
    PLAN_MAX_BYTES,
    PlanDefinition,
    PlanRefused,
    ResourceRef,
    RunReference,
    StepDefinition,
    VersionedRef,
    freeze_json,
)

_DEFINITION_PREFIX = b"yanxu-plan-definition:v1\0"
_ENVELOPE_PREFIX = b"yanxu-plan-envelope:v1\0"


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ContractError("canonical plan JSON object keys must be strings")
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize the restricted JSON value with stable UTF-8 and object key order."""
    try:
        frozen = freeze_json(value)
        text = json.dumps(
            _plain(frozen),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return text.encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ContractError(f"value cannot be encoded as canonical plan JSON: {exc}") from exc


def plan_definition_to_record(definition: PlanDefinition) -> Mapping[str, Any]:
    return {
        "schema_version": definition.schema_version,
        "namespace": definition.namespace,
        "session_id": definition.session_id,
        "plan_id": definition.plan_id,
        "revision": definition.revision,
        "title": definition.title.strip(),
        "goal": definition.goal.strip(),
        "constraints": [_constraint_to_record(item) for item in definition.constraints],
        "acceptance_criteria": [_criterion_to_record(item) for item in definition.acceptance_criteria],
        "steps": [step.to_record() for step in definition.steps],
        "execution_envelope": definition.execution_envelope.to_record(),
        "risks": list(definition.risks),
        "expected_changes": list(definition.expected_changes),
        "created_by": definition.created_by,
        "created_at": definition.created_at,
        "source": definition.source.value,
        "proposal_run_ref": None if definition.proposal_run_ref is None else definition.proposal_run_ref.to_record(),
        "supersedes_revision": definition.supersedes_revision,
        "replan_reason": definition.replan_reason,
        "max_step_executions": definition.max_step_executions,
        "deadline_at_ms": definition.deadline_at_ms,
        "max_concurrency": definition.max_concurrency,
    }


def _constraint_to_record(value: Constraint) -> Mapping[str, Any]:
    return value.to_record()


def _criterion_to_record(value: AcceptanceCriterion) -> Mapping[str, Any]:
    return value.to_record()


def canonical_plan_record(definition: PlanDefinition) -> Mapping[str, Any]:
    record = dict(plan_definition_to_record(definition))
    record.pop("created_at", None)
    record["steps"] = [
        {
            **step.to_record(),
            "depends_on": sorted(step.depends_on),
            "acceptance_criteria": [
                _criterion_to_record(item)
                for item in sorted(step.acceptance_criteria, key=lambda item: item.criterion_id)
            ],
        }
        for step in sorted(definition.steps, key=lambda item: item.step_id)
    ]
    record["acceptance_criteria"] = [
        _criterion_to_record(item)
        for item in sorted(definition.acceptance_criteria, key=lambda item: item.criterion_id)
    ]
    return record


def canonical_plan_bytes(definition: PlanDefinition) -> bytes:
    encoded = canonical_json_bytes(canonical_plan_record(definition))
    if len(encoded) > PLAN_MAX_BYTES:
        raise PlanRefused("invalid_definition", f"definition exceeds {PLAN_MAX_BYTES} UTF-8 bytes")
    return encoded


def definition_digest(definition: PlanDefinition) -> str:
    return hashlib.sha256(_DEFINITION_PREFIX + canonical_plan_bytes(definition)).hexdigest()


def execution_envelope_digest(envelope: ExecutionEnvelope) -> str:
    return hashlib.sha256(_ENVELOPE_PREFIX + canonical_json_bytes(envelope.to_record())).hexdigest()


def _require_object(value: Any, where: str, fields: set[str] | None = None) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{where} must be a JSON object")
    if fields is not None:
        unknown = sorted(set(value) - fields)
        if unknown:
            raise PlanRefused("invalid_definition", f"{where} has unknown field(s): {', '.join(unknown)}")
    return value


def _required_text(record: Mapping[str, Any], name: str) -> str:
    value = record.get(name)
    if not isinstance(value, str):
        raise ContractError(f"{name} must be a string")
    return value


def _versioned_ref_from_record(value: Any, where: str) -> VersionedRef:
    record = _require_object(value, where, {"ref", "version"})
    return VersionedRef(_required_text(record, "ref"), _required_text(record, "version"))


def _resource_ref_from_record(value: Any) -> ResourceRef:
    record = _require_object(
        value,
        "resource ref",
        {"namespace", "owner_kind", "owner_id", "version", "kind", "digest"},
    )
    return ResourceRef(*(_required_text(record, name) for name in (
        "namespace", "owner_kind", "owner_id", "version", "kind", "digest"
    )))


def _run_ref_from_record(value: Any) -> RunReference:
    record = _require_object(value, "run reference", {"namespace", "session_id", "run_id"})
    return RunReference(*(_required_text(record, name) for name in ("namespace", "session_id", "run_id")))


def _criterion_from_record(value: Any) -> AcceptanceCriterion:
    record = _require_object(value, "acceptance criterion", {"criterion_id", "verifier_id", "verifier_version", "config"})
    return AcceptanceCriterion(
        criterion_id=_required_text(record, "criterion_id"),
        verifier_id=_required_text(record, "verifier_id"),
        verifier_version=_required_text(record, "verifier_version"),
        config=_require_object(record.get("config", {}), "criterion config"),
    )


def _constraint_from_record(value: Any) -> Constraint:
    record = _require_object(value, "constraint", {"constraint_id", "text", "enforcement", "enforcer_ref"})
    ref = record.get("enforcer_ref")
    return Constraint(
        constraint_id=_required_text(record, "constraint_id"),
        text=_required_text(record, "text"),
        enforcement=_required_text(record, "enforcement"),
        enforcer_ref=None if ref is None else _versioned_ref_from_record(ref, "enforcer_ref"),
    )


def _step_from_record(value: Any) -> StepDefinition:
    fields = {
        "step_id", "title", "instruction", "depends_on", "acceptance_criteria", "input_refs",
        "required_context_refs", "execution_profile_ref", "workspace_ref", "output_contract",
    }
    record = _require_object(value, "step", fields)
    profile_ref = record.get("execution_profile_ref")
    workspace_ref = record.get("workspace_ref")
    output_contract = record.get("output_contract")
    return StepDefinition(
        step_id=_required_text(record, "step_id"),
        title=_required_text(record, "title"),
        instruction=_required_text(record, "instruction"),
        depends_on=tuple(record.get("depends_on", ())),
        acceptance_criteria=tuple(_criterion_from_record(item) for item in record.get("acceptance_criteria", ())),
        input_refs=tuple(_resource_ref_from_record(item) for item in record.get("input_refs", ())),
        required_context_refs=tuple(
            _resource_ref_from_record(item) for item in record.get("required_context_refs", ())
        ),
        execution_profile_ref=None if profile_ref is None else _versioned_ref_from_record(profile_ref, "execution_profile_ref"),
        workspace_ref=None if workspace_ref is None else _versioned_ref_from_record(workspace_ref, "workspace_ref"),
        output_contract=None if output_contract is None else _require_object(output_contract, "output_contract"),
    )


def plan_definition_from_record(value: Mapping[str, Any]) -> PlanDefinition:
    fields = {
        "schema_version", "namespace", "session_id", "plan_id", "revision", "title", "goal",
        "constraints", "acceptance_criteria", "steps", "execution_envelope", "risks",
        "expected_changes", "created_by", "created_at", "source", "proposal_run_ref",
        "supersedes_revision", "replan_reason", "max_step_executions", "deadline_at_ms", "max_concurrency",
    }
    record = _require_object(value, "plan definition", fields)
    envelope_record = _require_object(
        record.get("execution_envelope"),
        "execution_envelope",
        {"profile_ref", "workspace_ref", "policy_ref", "deployment_ref", "budget_ref"},
    )
    envelope = ExecutionEnvelope(
        profile_ref=_versioned_ref_from_record(envelope_record.get("profile_ref"), "profile_ref"),
        workspace_ref=_versioned_ref_from_record(envelope_record.get("workspace_ref"), "workspace_ref"),
        policy_ref=_versioned_ref_from_record(envelope_record.get("policy_ref"), "policy_ref"),
        deployment_ref=_versioned_ref_from_record(envelope_record.get("deployment_ref"), "deployment_ref"),
        budget_ref=_versioned_ref_from_record(envelope_record.get("budget_ref"), "budget_ref"),
    )
    proposal = record.get("proposal_run_ref")
    return PlanDefinition(
        namespace=_required_text(record, "namespace"),
        session_id=_required_text(record, "session_id"),
        plan_id=_required_text(record, "plan_id"),
        revision=record.get("revision"),
        title=_required_text(record, "title"),
        goal=_required_text(record, "goal"),
        steps=tuple(_step_from_record(item) for item in record.get("steps", ())),
        execution_envelope=envelope,
        constraints=tuple(_constraint_from_record(item) for item in record.get("constraints", ())),
        acceptance_criteria=tuple(_criterion_from_record(item) for item in record.get("acceptance_criteria", ())),
        risks=tuple(record.get("risks", ())),
        expected_changes=tuple(record.get("expected_changes", ())),
        created_by=_required_text(record, "created_by"),
        created_at=record.get("created_at", 0.0),
        source=_required_text(record, "source"),
        proposal_run_ref=None if proposal is None else _run_ref_from_record(proposal),
        supersedes_revision=record.get("supersedes_revision"),
        replan_reason=_required_text(record, "replan_reason"),
        max_step_executions=record.get("max_step_executions"),
        deadline_at_ms=record.get("deadline_at_ms"),
        max_concurrency=record.get("max_concurrency"),
        schema_version=record.get("schema_version"),
    )


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ContractError(f"non-finite JSON number {value!r} is not allowed")


def plan_definition_from_json(payload: str | bytes) -> PlanDefinition:
    if isinstance(payload, bytes):
        try:
            payload = payload.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ContractError("plan JSON must be UTF-8") from exc
    if not isinstance(payload, str):
        raise ContractError("plan JSON must be text or UTF-8 bytes")
    try:
        record = json.loads(
            payload,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, UnicodeEncodeError) as exc:
        raise ContractError(f"invalid plan JSON: {exc}") from exc
    return plan_definition_from_record(_require_object(record, "plan definition"))


__all__ = [
    "canonical_json_bytes",
    "canonical_plan_bytes",
    "canonical_plan_record",
    "definition_digest",
    "execution_envelope_digest",
    "plan_definition_from_json",
    "plan_definition_from_record",
    "plan_definition_to_record",
]
