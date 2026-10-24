from __future__ import annotations

import json

import pytest

from yanxu_harness.errors import ContractError
from yanxu_harness.plans import (
    AcceptanceCriterion,
    Constraint,
    ExecutionEnvelope,
    PLAN_MAX_DEPTH,
    PLAN_MAX_STEP_EXECUTIONS,
    PLAN_MAX_STEPS,
    PlanDefinition,
    PlanRecord,
    PlanApproval,
    PlanRefused,
    PlanStatus,
    ResourceRef,
    StepDefinition,
    StepOutcome,
    VersionedRef,
    canonical_plan_bytes,
    definition_digest,
    plan_definition_from_json,
    readiness_for_step,
    topological_order,
    transition_plan,
)


def criterion(name: str = "complete") -> AcceptanceCriterion:
    return AcceptanceCriterion(
        criterion_id=name,
        verifier_id="host.result",
        verifier_version="1",
        config={"expected": "present"},
    )


def envelope() -> ExecutionEnvelope:
    return ExecutionEnvelope(
        profile_ref=VersionedRef("profile/default", "1"),
        workspace_ref=VersionedRef("workspace/main", "v1"),
        policy_ref=VersionedRef("policy/default", "1"),
        deployment_ref=VersionedRef("deployment/local", "1"),
        budget_ref=VersionedRef("budget/task-default", "1"),
    )


def step(step_id: str, *, depends_on: tuple[str, ...] = ()) -> StepDefinition:
    return StepDefinition(
        step_id=step_id,
        title=f"Step {step_id}",
        instruction=f"Perform {step_id}",
        depends_on=depends_on,
        acceptance_criteria=(criterion(),),
    )


def definition(steps: tuple[StepDefinition, ...] | None = None, **overrides: object) -> PlanDefinition:
    fields: dict[str, object] = {
        "namespace": "ns",
        "session_id": "session",
        "plan_id": "plan-1",
        "revision": 1,
        "title": "A plan",
        "goal": "Reach a verifiable result",
        "steps": steps or (step("a"), step("b", depends_on=("a",))),
        "execution_envelope": envelope(),
    }
    fields.update(overrides)
    return PlanDefinition(**fields)  # type: ignore[arg-type]


def test_digest_ignores_step_and_dependency_set_order_but_not_content() -> None:
    first = definition((step("c", depends_on=("b", "a")), step("a"), step("b")))
    reordered = definition((step("b"), step("c", depends_on=("a", "b")), step("a")))
    changed = definition((step("b"), step("c", depends_on=("a", "b")), step("a")), goal="Different")

    assert definition_digest(first) == definition_digest(reordered)
    assert canonical_plan_bytes(first) == canonical_plan_bytes(reordered)
    assert definition_digest(first) != definition_digest(changed)


def test_audit_time_is_not_part_of_the_content_digest() -> None:
    assert definition(created_at=10.0).definition_digest == definition(created_at=20.0).definition_digest
    assert definition(title="  A plan  ").title == "A plan"


def test_plan_values_are_deeply_frozen_and_reject_non_json_numeric_values() -> None:
    config = {"nested": {"items": ["x"]}}
    plan = definition((StepDefinition(
        step_id="a",
        title="Step a",
        instruction="Perform a",
        acceptance_criteria=(AcceptanceCriterion("ok", "host.result", "1", config),),
    ),))
    config["nested"]["items"].append("mutated")

    frozen = plan.steps[0].acceptance_criteria[0].config
    assert frozen["nested"]["items"] == ("x",)
    with pytest.raises(TypeError):
        frozen["nested"]["new"] = True  # type: ignore[index]
    with pytest.raises(ContractError, match="floating-point"):
        AcceptanceCriterion("bad", "host.result", "1", {"ratio": 0.5})


@pytest.mark.parametrize(
    "steps",
    [
        (step("a"), step("a")),
        (step("a", depends_on=("missing",)),),
        (step("a", depends_on=("a",)),),
        (step("a", depends_on=("b",)), step("b", depends_on=("a",))),
    ],
)
def test_invalid_dags_are_rejected_before_review(steps: tuple[StepDefinition, ...]) -> None:
    with pytest.raises(PlanRefused):
        definition(steps)


def test_invalid_step_id_is_rejected_at_construction() -> None:
    with pytest.raises(PlanRefused):
        step("bad id")


def test_schema_and_numeric_limits_are_explicit() -> None:
    with pytest.raises(PlanRefused) as unsupported:
        definition(schema_version=99)
    assert unsupported.value.reason_code == "unsupported_schema_version"
    assert definition(max_step_executions=0).max_step_executions == 0
    assert definition(max_step_executions=PLAN_MAX_STEP_EXECUTIONS).max_step_executions == PLAN_MAX_STEP_EXECUTIONS
    with pytest.raises(PlanRefused):
        definition(max_step_executions=True)
    with pytest.raises(PlanRefused, match="cannot exceed"):
        definition(max_step_executions=PLAN_MAX_STEP_EXECUTIONS + 1)
    with pytest.raises(PlanRefused):
        definition(max_concurrency=0)
    with pytest.raises(PlanRefused) as record_schema:
        PlanRecord("ns", "session", "plan", 1, 1, "draft", "digest", schema_version=2)
    assert record_schema.value.reason_code == "unsupported_schema_version"
    with pytest.raises(PlanRefused) as approval_schema:
        PlanApproval(
            "ns", "plan", 1, "definition", "envelope", "policy", "decision",
            "approved", "reviewer", 1, schema_version=2,
        )
    assert approval_schema.value.reason_code == "unsupported_schema_version"


def test_total_definition_bytes_and_nested_depth_are_bounded() -> None:
    large_steps = tuple(
        StepDefinition(
            step_id=f"s{index}",
            title=f"Step {index}",
            instruction="x" * 30_000,
            acceptance_criteria=(criterion(),),
        )
        for index in range(9)
    )
    with pytest.raises(PlanRefused, match="bytes"):
        definition(large_steps)

    nested: object = "leaf"
    for _ in range(PLAN_MAX_DEPTH + 1):
        nested = [nested]
    with pytest.raises(PlanRefused, match="depth"):
        AcceptanceCriterion("deep", "host.result", "1", {"nested": nested})


def test_dependency_edge_limit_is_enforced() -> None:
    steps = tuple(
        step(
            f"s{index}",
            depends_on=tuple(f"s{previous}" for previous in range(max(0, index - 9), index)),
        )
        for index in range(PLAN_MAX_STEPS)
    )
    with pytest.raises(PlanRefused, match="dependency edges"):
        definition(steps)


def test_graph_order_and_readiness_are_deterministic() -> None:
    steps = (step("d", depends_on=("b", "c")), step("c", depends_on=("a",)), step("b", depends_on=("a",)), step("a"))

    assert topological_order(steps) == ("a", "b", "c", "d")
    assert readiness_for_step("b", steps, {"a": StepOutcome.ACCEPTED}).ready
    assert readiness_for_step("d", steps, {"b": StepOutcome.ACCEPTED}).reason == "dependency_pending"
    assert readiness_for_step("d", steps, {"b": StepOutcome.REJECTED, "c": StepOutcome.ACCEPTED}).reason == "dependency_rejected"


def test_json_codec_rejects_duplicate_keys_and_round_trips() -> None:
    plan = definition((StepDefinition(
        step_id="a",
        title="Step a",
        instruction="Perform a",
        acceptance_criteria=(criterion(),),
        input_refs=(ResourceRef("ns", "artifact", "doc-1", "v3", "text", "abc123"),),
    ),))
    parsed = plan_definition_from_json(json.dumps(plan.to_record()))

    assert parsed.definition_digest == plan.definition_digest
    with pytest.raises(ContractError, match="duplicate JSON key"):
        plan_definition_from_json('{"schema_version":1,"schema_version":1}')


def test_plan_transition_requires_quiescence_and_completion_evidence() -> None:
    with pytest.raises(PlanRefused) as incomplete:
        transition_plan(PlanStatus.EXECUTING, PlanStatus.COMPLETED)
    assert incomplete.value.reason_code == "incomplete_plan"

    with pytest.raises(PlanRefused) as active:
        transition_plan(PlanStatus.REPLANNING, PlanStatus.DRAFT)
    assert active.value.reason_code == "active_binding"

    assert transition_plan(
        PlanStatus.REPLANNING,
        PlanStatus.DRAFT,
        quiescent=True,
    ) is PlanStatus.DRAFT
    assert transition_plan(
        PlanStatus.EXECUTING,
        PlanStatus.COMPLETED,
        all_steps_accepted=True,
        plan_acceptance_passed=True,
    ) is PlanStatus.COMPLETED
