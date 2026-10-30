from __future__ import annotations

from dataclasses import replace

from yanxu_harness.evals import (
    TIMELINE_SUITE,
    EvalOutcome,
    run_timeline_case,
    run_timeline_suite,
    timeline_case_by_id,
)


def test_frozen_timeline_suite_passes_all_six_offline_protocol_cases() -> None:
    result = run_timeline_suite()
    assert result.suite.digest == "sha256:371806f3ce3c52824930c1e323dc902c65494242a9552f269e82e7a478387699"
    assert len(result.results) == 6
    assert {item.outcome for item in result.results} == {EvalOutcome.PASSED}
    assert all(item.protocol["provider_requests"] == 0 for item in result.results)
    assert [item.case_id for item in result.results] == [case.case_id for case in TIMELINE_SUITE.cases]


def test_timeline_verifier_rejects_missing_required_fact() -> None:
    case = timeline_case_by_id("timeline-normal-multi-step")
    expectations = dict(case.expectations)
    expectations["required_references"] = [
        *expectations["required_references"],
        "acceptance:fact-that-does-not-exist",
    ]
    result = run_timeline_case(replace(case, expectations=expectations))
    assert result.outcome is EvalOutcome.FAILED
    assert result.verifier is not None
    assert any("required fact references are missing" in reason for reason in result.verifier.reasons)


def test_real_model_timeline_case_is_reported_not_run_without_adapter() -> None:
    case = replace(timeline_case_by_id("timeline-normal-multi-step"), model="provider/model", repeats=2)
    result = run_timeline_case(case)
    assert result.outcome is EvalOutcome.NOT_RUN
    assert result.protocol == {}
    assert "explicit run authorization" in result.notes[0]
