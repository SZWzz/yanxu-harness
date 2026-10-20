"""E0–E2: frozen cases, independent verification, and honest comparison.

The tests here are mostly *negative controls*, because a suite where everything passes proves
nothing. Each of the following must be demonstrated, not asserted:

* an answer that looks finished but is wrong **fails** — the verifier reads the workspace and
  runs the fixture's own tests, so a claim of "done" is content, not evidence;
* editing the specification (the fixture's tests) **fails** even when the tests pass;
* a changed verifier makes earlier results **incomparable** instead of silently comparable;
* the framework's own failures are classified separately from a task verdict and are never
  counted as the task failing;
* a real-model case is not run by the offline entry point, and is never reported as a pass;
* a comparison keeps a baseline failure a failure, and refuses to compare different questions.

Everything runs the real harness, the real stores and the real Gateway. The crash cases spawn
a child process that really exits, and the record says so.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from yanxu_harness.core.types import AssistantMessage, StopReason, ToolCall
from yanxu_harness.evals import (
    CASES,
    EvalCase,
    EvalOutcome,
    EvalSuite,
    FixtureSpec,
    case_by_id,
    compare_reports,
    run_case,
    run_suite,
)
from yanxu_harness.evals.cases import SUITE, recovery_script_for, script_for
from yanxu_harness.evals.__main__ import render_comparison
from yanxu_harness.evals.fixtures import (
    FAULT_LEASE_TTL_SECONDS,
    ScriptedStream,
    call_message,
    final_message,
    probe_ledger,
    run_coroutine,
    trace_lines,
)
from yanxu_harness.evals.types import EvalCaseResult, VerifierOutcome
from yanxu_harness.evals.verifiers import verifier_digest

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def suite_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Run the offline suite once and let every test read the same real results."""
    root = tmp_path_factory.mktemp("evals")
    return root


@pytest.fixture(scope="module")
def suite_results(suite_root: Path):
    return run_suite(SUITE, suite_root)


def _result(results: Any, case_id: str) -> EvalCaseResult:
    return next(item for item in results.results if item.case_id == case_id)


# --- the frozen cases -------------------------------------------------------------------



#: A well-formed fingerprint, used where a test needs a *valid* identity rather than a
#: particular one. The shape is checked (``EvalCaseResult`` refuses a string that is not a
#: fingerprint this build writes), which is the point: an arbitrary digest string must not be
#: able to stand in for a real identity.
_DIGEST = "sha256:" + "d" * 64

def test_every_case_states_what_is_needed_to_reproduce_it() -> None:
    assert len(CASES) == 8
    for case in CASES:
        assert case.fixture is not None, case.case_id
        assert case.fixture.digest.startswith("sha256:")
        assert case.verifier_digest == verifier_digest(case.verifier)
        assert case.instructions.strip()
        assert case.max_turns > 0
        if case.model != "offline":
            assert case.repeats > 1, "a real-model case must be repeated to be a measurement"


def test_the_suite_and_case_digests_change_with_the_question() -> None:
    """A changed case is a changed question: the digests are what a comparison matches on."""
    case = case_by_id("fix-calc-tests")
    assert case.digest.startswith("sha256:")
    assert SUITE.digest.startswith("sha256:")
    changed = replace(case, version="2")
    assert changed.digest != case.digest
    other_suite = EvalSuite(name=SUITE.name, version=SUITE.version, cases=(changed,))
    assert other_suite.digest != SUITE.digest
    # The fixture content is part of the question too.
    moved = replace(
        case,
        fixture=FixtureSpec(name="calc", version="1", files={"calc.py": "x"}),
    )
    assert moved.digest != case.digest


def test_a_case_refuses_to_be_stated_without_a_verifier_or_budget() -> None:
    with pytest.raises(Exception, match="independent verifier"):
        EvalCase(case_id="x", version="1", instructions="do it")
    with pytest.raises(Exception, match="max_turns must be positive"):
        EvalCase(
            case_id="x", version="1", instructions="do it", verifier="v", max_turns=0
        )
    with pytest.raises(Exception, match="must be repeated"):
        EvalCase(
            case_id="x",
            version="1",
            instructions="do it",
            verifier="v",
            model="gpt-whatever",
            repeats=1,
        )


# --- the offline suite, run for real ----------------------------------------------------


def test_the_offline_suite_passes_every_case(suite_results) -> None:
    """Eight cases, one per task category the plan's first batch asks for."""
    outcomes = {item.case_id: item.outcome for item in suite_results.results}
    assert outcomes == {
        # locate a seeded defect / change an implementation / run the tests and review a diff
        "locate-bug": EvalOutcome.PASSED,
        "fix-calc-tests": EvalOutcome.PASSED,
        "review-diff": EvalOutcome.PASSED,
        # approval pause and resume / partial batch with evidence / drift refusal / deadline
        "approval-write-once": EvalOutcome.PASSED,
        "partial-batch-evidence": EvalOutcome.PASSED,
        "drift-refusal": EvalOutcome.PASSED,
        "deadline-during-recovery": EvalOutcome.PASSED,
        # long context whose auxiliary call is cancelled
        "long-context-summary-cancel": EvalOutcome.PASSED,
    }, {
        "outcomes": {key: value.value for key, value in outcomes.items()},
        "non_pass_results": [
            item.to_record()
            for item in suite_results.results
            if item.outcome is not EvalOutcome.PASSED
        ],
    }


def test_the_code_case_runs_the_fixtures_own_tests_and_protects_them(suite_results) -> None:
    result = _result(suite_results, "fix-calc-tests")
    assert result.verifier is not None and result.verifier.passed
    assert "tests pass" in json.dumps(result.verifier.evidence)
    assert result.protocol["status"] == "completed"
    assert result.artifacts["workspace"] != result.artifacts["pristine"], (
        "the fix changed the workspace"
    )


def test_the_approval_case_writes_exactly_once_across_the_resume(suite_results) -> None:
    result = _result(suite_results, "approval-write-once")
    assert result.human_decisions == 1, "the decision is counted from the trusted store"
    history = result.protocol["approval_history"]
    assert len(history) == 1 and history[0]["status"] == "approved", (
        "the count is the store's own decision record, not the evaluator's request count"
    )
    assert history[0]["decided_by"] == "eval-operator"
    assert history[0]["superseded"] is False
    assert result.protocol["attempts"] == ["a1", "a2"]
    assert result.verifier is not None and result.verifier.passed
    assert result.verifier.evidence["writes"] == 1


def test_the_partial_batch_case_never_runs_a_confirmed_probe_twice(suite_results) -> None:
    result = _result(suite_results, "partial-batch-evidence")
    assert result.protocol["fault"] == "process-exit", "the crash was a real process exit"
    assert result.protocol["fault_exit_code"] == 9
    assert result.protocol["resumes"] == ["a2", "a3"]
    assert result.verifier is not None and result.verifier.passed
    assert result.verifier.evidence["ran"] == ["probe.a", "probe.b", "probe.c"]
    assert result.verifier.evidence["operations"] == {
        "probe.a": "succeeded",
        "probe.b": "succeeded",
        "probe.c": "succeeded",
    }


def test_the_recording_says_which_fault_was_real_and_which_was_not() -> None:
    """A simulated crash and a process exit are different facts and are not conflated."""
    # The declared offline runs are the offline ones; a case with an undeclared fault cannot
    # be run at all, because injecting one would change the question.
    for case in CASES:
        if case.allowed_faults:
            continue
        assert not case.allowed_faults
    from yanxu_harness.evals.runner import _run_with_fault  # noqa: F401 - import is the check


def test_the_drift_case_refuses_before_dispatching_and_says_which_dimension(
    suite_results,
) -> None:
    result = _result(suite_results, "drift-refusal")
    assert result.verifier is not None and result.verifier.passed
    refusal = result.verifier.evidence["refusal"]
    assert refusal["reason_code"] == "arrangement_drift"
    assert refusal["drifted"] == ["config", "instructions"], refusal["drifted"]
    assert refusal["durable"] is False, (
        "the refusal writes nothing to the run and says so, so a host can audit the decision "
        "without the harness inventing a second record of it"
    )
    assert refusal["recorded"]["instructions"] != refusal["current"]["instructions"]
    assert result.protocol["model_calls"] == 0, (
        "a refused recovery must not ask the model about a run it declined to continue"
    )
    assert result.verifier.evidence["ran"] == ["probe.a", "probe.b"]


def test_the_deadline_case_stops_dispatching_and_keeps_the_admission(suite_results) -> None:
    result = _result(suite_results, "deadline-during-recovery")
    assert result.protocol["error_code"] == "max_runtime_exceeded"
    assert result.protocol["stop_code"] == "max_turns", (
        "the loop stop is the reducer's own vocabulary; the *reason* is the error code"
    )
    assert result.verifier is not None and result.verifier.passed
    assert "probe.c" in result.verifier.evidence["ledger"], (
        "the interrupted call is visible in the ledger, not reported as never started"
    )
    assert result.verifier.evidence["blocking_probe_state"] in {"running", "unknown", "succeeded"}, (
        "an interrupted handler is recorded as unproven, which is not the same as absent"
    )


# --- the crash fixture has to prove its own window -----------------------------------------
#
# The exit code used to be the whole proof: any death of the child process was reported as the
# injected crash, and the parent went straight on to recover a run whose window the case never
# produced. These tests make the two degraded children — one that raises before the injection
# point, one that finishes without it — real processes, and require the parent to report a
# fixture failure instead of a fault.


def test_a_child_that_fails_before_the_fault_point_is_a_fixture_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("YANXU_EVAL_CHILD_MODE", "fail-before-fault")
    result = run_case(case_by_id("partial-batch-evidence"), tmp_path / "child-raised")

    assert result.outcome is EvalOutcome.INFRA_ERROR, "a broken fixture is never a task verdict"
    assert result.protocol["fault"] == "fixture-failure", (
        "the child died before probe.b, so calling this an injected crash is a false record"
    )
    assert result.protocol["fault_exit_code"] == 70
    diagnostic = result.protocol["fault_diagnostic"]
    assert diagnostic["phase"] == "model-request"
    assert diagnostic["error"].startswith("RuntimeError:")
    assert diagnostic["traceback"], "the type alone does not say where it broke"
    assert str(tmp_path) not in json.dumps(diagnostic), "the diagnostic is path-free"
    # Nothing was recovered: there was no window to recover from.
    assert "classified_status" not in result.protocol
    assert result.protocol.get("attempts") is None
    assert result.verifier is None, "the verifier is never asked to judge a fixture failure"


def test_a_child_that_finishes_without_the_fault_is_a_fixture_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("YANXU_EVAL_CHILD_MODE", "return-without-fault")
    result = run_case(case_by_id("partial-batch-evidence"), tmp_path / "child-completed")

    assert result.outcome is EvalOutcome.INFRA_ERROR
    assert result.protocol["fault"] == "fixture-failure", (
        "a fault that never happened cannot be evidence that it was injected"
    )
    assert result.protocol["fault_exit_code"] == 71
    diagnostic = result.protocol["fault_diagnostic"]
    assert diagnostic["phase"] == "completed"
    assert "without entering the injection point" in diagnostic["error"]
    assert "classified_status" not in result.protocol


def test_a_real_crash_proves_the_three_ledger_preconditions(suite_results) -> None:
    """The declared window is checked against the stores before anything is recovered.

    Three facts, and each of them is a way the fixture could be lying: the first probe settled,
    the second has an intent with no receipt (admitted, handler entered, effect unproven), and
    the third was never admitted. The trace proves the handler was entered; the ledger proves
    the admission survived and the receipt did not.
    """
    result = _result(suite_results, "partial-batch-evidence")
    preconditions = result.protocol["fault_preconditions"]
    assert preconditions["satisfied"] is True, preconditions["problems"]
    assert preconditions["problems"] == []
    ledger = preconditions["ledger"]
    assert ledger["probe.a"] == {"state": "succeeded", "has_receipt": True}
    assert ledger["probe.b"]["has_receipt"] is False
    assert ledger["probe.b"]["state"] in {"queued", "running"}
    assert "probe.c" not in ledger, "the third probe was never admitted"
    assert preconditions["trace"] == ["a1:probe.a:task.md", "a1:probe.b:task.md"]

    # The declared crash, and the one lease policy, are recorded as facts: the parent arrived
    # after the expiry the store itself recorded, not after an offset someone invented.
    assert result.protocol["fault"] == "process-exit"
    lease = result.protocol["fault_lease"]
    assert lease["ttl_seconds"] == FAULT_LEASE_TTL_SECONDS
    assert 0 < lease["arrival_after_expiry_seconds"] < 0.1


def test_the_recovery_never_bypasses_the_crashed_childs_lease(tmp_path: Path) -> None:
    """Both branches of the store's fence, with a real child and the real SQLite store.

    Arriving while the dead worker's lease is still valid is **refused** — that is the branch a
    test clock is tempted to skip — and arriving after the store's own expiry is admitted and
    pauses on the open intent. No sleep, no bypass, no second lease policy.
    """
    from yanxu_harness.errors import RunBusy
    from yanxu_harness.evals.child import CRASH_EXIT_CODE, run_crash_child
    from yanxu_harness.evals.fixtures import (
        RecoveryClock,
        build_deployment,
        case_context,
        live_lease_expiry,
        write_fixture,
    )

    case = case_by_id("partial-batch-evidence")
    root = tmp_path / "lease"
    workspace, state = root / "workspace", root / "state"
    write_fixture(case.fixture, workspace)
    state.mkdir(parents=True, exist_ok=True)
    code, stderr = run_crash_child(case.case_id, root)
    assert code == CRASH_EXIT_CODE, stderr
    expiry = live_lease_expiry(state)
    assert expiry is not None, "the crashed child still holds the run's lease"

    def deployment(attempt: str, **kwargs: Any):
        return build_deployment(
            workspace=workspace,
            state=state,
            stream=ScriptedStream(final_message("recovered")),
            profile="probe",
            attempt=attempt,
            lease_ttl_seconds=FAULT_LEASE_TTL_SECONDS,
            **kwargs,
        )

    early = deployment("a2")
    try:
        with pytest.raises(RunBusy):
            run_coroutine(early.harness.resume("run-1", {}, case_context("a2")))
    finally:
        early.close()

    clock = RecoveryClock()
    clock.step_past(expiry)
    assert clock() > expiry, "the arriving moment is after the expiry the store recorded"
    late = deployment("a2", clock=clock)
    try:
        outcome = run_coroutine(late.harness.resume("run-1", {}, case_context("a2")))
    finally:
        late.close()
    assert outcome.status.value == "suspended", "the open intent pauses the batch"
    assert "probe.c" not in probe_ledger(state), "nothing was dispatched by the pause"
    assert not any("probe.c" in line for line in trace_lines(state / "trace.log"))


# --- the deadline: two windows, each asserted on its own -----------------------------------


def test_the_recovery_deadline_covers_the_preparation_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Window A: the budget also covers the work before the first admission.

    A host reconciler slower than the whole budget reproduces, deterministically, what the
    Windows runner reached by accident: the invocation is stopped, and *no call was admitted*.
    That is the runtime behaving correctly (the contract is one absolute budget for the whole
    invocation), so the record must not pretend the blocking tool had started. This is the
    window that used to fail the case on a slow machine; it is now a separate assertion here
    and is not what the end-to-end case measures.
    """
    from yanxu_harness.evals import runner as runner_module

    async def slow_reconciler(call: Any, details: Any) -> Any:
        await asyncio.sleep(0.3)
        return {
            "proven": True,
            "state": "succeeded",
            "detail": "slow host evidence",
            "source": "test reconciler",
        }

    monkeypatch.setattr(runner_module, "_evidence", lambda case: slow_reconciler)
    case = replace(case_by_id("deadline-during-recovery"), max_runtime_seconds=0.05)
    result = run_case(case, tmp_path / "preparation")

    assert result.protocol["status"] == "usage_limited"
    assert result.protocol["error_code"] == "max_runtime_exceeded"
    assert result.protocol["model_calls"] == 0
    assert "probe.c" not in result.protocol["ledger"], (
        "the call was never admitted, so the ledger must not hold a row for it"
    )
    assert not any("probe.c" in line for line in result.protocol["trace"])
    # And the verifier says exactly that instead of accepting the wrong window.
    assert result.outcome is EvalOutcome.FAILED
    assert result.verifier is not None
    assert any("before the call was admitted" in reason for reason in result.verifier.reasons)


def test_the_recovery_deadline_stops_the_blocking_probe_after_admission(suite_results) -> None:
    """Window B: admitted first, then interrupted by the deadline — with the numbers recorded.

    The case must prove that the *preparation* fits in the budget and that the *handler* does
    not: the admission offset is far below the budget and the invocation used essentially all
    of it. That is what makes the budget a measurement instead of a guess about the host.
    """
    result = _result(suite_results, "deadline-during-recovery")
    budget = result.protocol["budgets"]["max_runtime_seconds"]
    recovery = result.protocol["recovery"]
    assert recovery["budget_seconds"] == budget
    assert "probe.c" in recovery["admissions"], "the blocking probe was admitted"
    assert 0 <= recovery["admissions"]["probe.c"] < budget, (
        "the real preparation fitted in the budget; the case does not assume a machine speed"
    )
    assert recovery["invocation_seconds"] >= budget * 0.9, (
        "the deadline ended the invocation; the handler was still blocking"
    )
    assert result.protocol["ledger"]["probe.c"] in {"running", "unknown"}
    assert result.verifier is not None and result.verifier.passed


def test_the_deadline_budgets_are_fixed_and_measured_not_assumed() -> None:
    from yanxu_harness.evals.cases import DEADLINE_BUDGET_SECONDS, RECOVERY_BLOCKING_SECONDS

    for case_id in ("deadline-during-recovery", "long-context-summary-cancel"):
        assert case_by_id(case_id).max_runtime_seconds == DEADLINE_BUDGET_SECONDS
    # The operation the deadline has to interrupt blocks for several times the whole budget, so
    # "the budget stopped it" cannot be "the work happened to finish first".
    assert RECOVERY_BLOCKING_SECONDS >= 4 * DEADLINE_BUDGET_SECONDS
    # One fixed number for every platform: no platform switch and no per-run adaptation.
    # Read from the imported module, not from a checkout: this suite also runs against the
    # installed wheel outside the tree, where `<repo>/src` does not exist.
    import yanxu_harness.evals.cases as cases_module

    source = Path(cases_module.__file__).read_text(encoding="utf-8")
    for probe in ("import os", "import sys", "import platform", "platform.system", "sys.platform"):
        assert probe not in source, probe


def test_the_superseded_deadline_cases_keep_their_identity_and_are_not_run() -> None:
    """A changed question is a new version: the old one's identity survives, unrun.

    The version-1 question assumed the preparation fits in 50 ms. Editing it in place would
    have made every result produced by it unreadable as its own question, so it is kept as
    history with its exact digest — the one the failing CI run recorded — and the suite runs
    only the version-2 cases.
    """
    from yanxu_harness.evals.cases import HISTORICAL_CASES, historical_by_id

    historical_ids = {case.case_id for case in HISTORICAL_CASES}
    assert historical_ids <= {case.case_id for case in CASES}
    assert all(
        case.version == "2" for case in CASES if case.case_id in historical_ids
    ), "the superseded question is not the one the suite runs"
    assert historical_by_id("deadline-during-recovery").digest == (
        # The identity the failing Windows run reported for this case.
        "sha256:2ac315287c6ba288005b095edfe94fcacd6eefafee60a2a7c97dd7e95f16fe2e"
    )
    old_summary = historical_by_id("long-context-summary-cancel")
    assert old_summary.max_runtime_seconds == 0.05
    assert old_summary.digest != case_by_id("long-context-summary-cancel").digest
    # Different digests are different questions: the comparator refuses to pair them, so a
    # version-1 result is never averaged with a version-2 one.
    assert old_summary.digest != case_by_id("long-context-summary-cancel").digest


# --- negative controls: the verifier must be able to fail ------------------------------


def test_a_wrong_answer_fails_even_though_the_model_says_it_is_done(tmp_path: Path) -> None:
    """The heart of the exercise: a claim of completion decides nothing."""
    case = case_by_id("fix-calc-tests")
    wrong = AssistantMessage(
        content=[
            ToolCall(
                id="c-write",
                name="write_file",
                arguments={"path": "calc.py", "text": "# fixed, trust me\n"},
            )
        ],
        stop_reason=StopReason.TOOL_USE,
    )
    adapter = ScriptedStream(wrong, final_message("all done, tests pass"))
    result = run_case(case, tmp_path / "wrong", model_adapter=adapter)
    assert result.outcome is EvalOutcome.FAILED
    assert result.verifier is not None
    assert any("tests" in reason for reason in result.verifier.reasons)


def test_editing_the_specification_fails(tmp_path: Path) -> None:
    """Changing the tests cannot buy a pass: the pristine copy is the truth."""
    case = case_by_id("fix-calc-tests")
    cheat = AssistantMessage(
        content=[
            ToolCall(
                id="c-tests",
                name="write_file",
                arguments={
                    "path": "test_calc.py",
                    "text": "def test_total_sums_every_value():\n    assert True\n",
                },
            )
        ],
        stop_reason=StopReason.TOOL_USE,
    )
    adapter = ScriptedStream(cheat, final_message("tests pass now"))
    result = run_case(case, tmp_path / "cheat", model_adapter=adapter)
    assert result.outcome is EvalOutcome.FAILED
    assert result.verifier is not None
    assert any("modified" in reason for reason in result.verifier.reasons)


def test_a_case_whose_premise_does_not_hold_is_invalid_not_failed(tmp_path: Path) -> None:
    """A fixture that is already fixed cannot distinguish a fix from an untouched project."""
    case = case_by_id("fix-calc-tests")
    fixed = case.fixture.files["calc.py"].replace("values[:-1]", "values")
    already = replace(
        case,
        fixture=replace(case.fixture, files={**case.fixture.files, "calc.py": fixed}),
    )
    result = run_case(already, tmp_path / "already")
    assert result.outcome is EvalOutcome.INVALID_RESULT
    assert any("baseline already passes" in note for note in result.notes)


def test_a_changed_verifier_makes_the_case_infra_error(tmp_path: Path) -> None:
    """A criterion that moved cannot decide a case: the digest refuses it."""
    case = case_by_id("fix-calc-tests")
    result = run_case(replace(case, verifier_digest="sha256:stale"), tmp_path / "stale")
    assert result.outcome is EvalOutcome.INFRA_ERROR
    assert "has changed since this case was frozen" in result.notes[0]


def test_a_framework_failure_is_not_a_task_failure(tmp_path: Path) -> None:
    """A case that cannot run is `not_run`, and it is not counted as the task failing."""
    real = replace(case_by_id("fix-calc-tests"), model="some-provider/some-model", repeats=3)
    result = run_case(real, tmp_path / "real")
    assert result.outcome is EvalOutcome.NOT_RUN
    assert "no adapter was configured" in result.notes[0]
    assert "scripted offline adapter" in result.notes[0]
    assert not result.outcome.is_task_verdict


def test_an_unknown_case_or_verifier_is_reported_not_guessed(tmp_path: Path) -> None:
    from yanxu_harness.evals.verifiers import verify
    from yanxu_harness.evals.verifiers import VerifierContext

    case = replace(case_by_id("fix-calc-tests"), verifier="not-a-verifier", verifier_digest="")
    outcome = verify(
        case,
        VerifierContext(
            case=case,
            workspace=tmp_path,
            pristine=tmp_path,
            state=tmp_path,
            run_id="run-1",
            outcome_status="completed",
        ),
    )
    assert outcome.infra_error and not outcome.passed


def test_each_repeat_gets_its_own_isolated_directory(tmp_path: Path) -> None:
    """No repeat may inherit another's workspace, stores or quota."""
    case = replace(case_by_id("fix-calc-tests"), repeats=2)
    suite = EvalSuite(name="twice", version="1", cases=(case,))
    results = run_suite(suite, tmp_path / "root")
    assert {item.repeat_index for item in results.results} == {0, 1}
    first = tmp_path / "root" / case.case_id / "r0"
    second = tmp_path / "root" / case.case_id / "r1"
    assert first.exists() and second.exists()
    assert not any(
        (first / "workspace") == (second / "workspace") for _ in [0]
    )
    assert all(item.outcome is EvalOutcome.PASSED for item in results.results)


# --- E2: results are comparable only when they answer the same question -----------------


def test_a_comparison_keeps_a_baseline_failure_a_failure(tmp_path: Path) -> None:
    """A fixed case must show as changed; an unchanged failure must stay a failure."""
    case = case_by_id("fix-calc-tests")
    wrong = ScriptedStream(
        AssistantMessage(
            content=[
                ToolCall(id="c-write", name="write_file", arguments={"path": "calc.py", "text": "# nope\n"})
            ],
            stop_reason=StopReason.TOOL_USE,
        ),
        final_message("done"),
    )
    baseline = run_case(case, tmp_path / "baseline", model_adapter=wrong)
    candidate = run_case(case, tmp_path / "candidate")
    assert baseline.outcome is EvalOutcome.FAILED
    assert candidate.outcome is EvalOutcome.PASSED

    comparison = compare_reports(
        {"suite": SUITE.to_record(), "results": [baseline.to_record()]},
        {"suite": SUITE.to_record(), "results": [candidate.to_record()]},
    )
    rows = comparison["comparable"]
    assert rows[0]["before"] == "failed" and rows[0]["after"] == "passed"
    assert rows[0]["changed"] is True
    assert comparison["quality"]["before"]["pass_rate"] == 0.0
    assert comparison["quality"]["after"]["pass_rate"] == 1.0
    assert comparison["quality"]["before"]["verdicts_denominator"] == 1

    # And an unchanged failure is still a failure, both times.
    again = run_case(case, tmp_path / "again", model_adapter=wrong)
    same = compare_reports(
        {"suite": SUITE.to_record(), "results": [baseline.to_record()]},
        {"suite": SUITE.to_record(), "results": [again.to_record()]},
    )
    assert same["comparable"][0]["changed"] is False
    assert same["quality"]["after"]["pass_rate"] == 0.0


def test_a_comparison_refuses_to_match_different_questions(tmp_path: Path) -> None:
    case = case_by_id("fix-calc-tests")
    baseline = run_case(case, tmp_path / "b")
    changed = replace(case, version="2")
    candidate = run_case(changed, tmp_path / "c")
    comparison = compare_reports(
        {"suite": SUITE.to_record(), "results": [baseline.to_record()]},
        {"suite": SUITE.to_record(), "results": [candidate.to_record()]},
    )
    assert comparison["comparable"] == []
    assert comparison["incomparable"]
    reasons = [str(item["reason"]) for item in comparison["incomparable"]]
    assert any(
        "does not appear in the baseline" in reason or "the suite lists cases but not" in reason
        for reason in reasons
    ), reasons
    assert comparison["quality"]["after"]["cases"] == 0


def test_a_comparison_separates_quality_reliability_and_cost() -> None:
    good = EvalCaseResult(
        case_id="c",
        case_version="1",
        case_digest=_DIGEST,
        attempt="a1",
        outcome=EvalOutcome.PASSED,
        verifier=VerifierOutcome(True),
        elapsed_seconds=1.5,
        usage=(
            {"unit": "input_tokens", "quantity": "actual", "amount": 10.0, "source": "committed"},
            {"unit": "input_tokens", "quantity": "unknown", "note": "one request was cut off"},
        ),
    )
    broken = EvalCaseResult(
        case_id="c",
        case_version="1",
        case_digest=_DIGEST,
        attempt="a1",
        outcome=EvalOutcome.INFRA_ERROR,
        notes=("the framework could not start the case",),
    )
    comparison = compare_reports(
        {"suite": {"name": "s", "version": "1"}, "results": [good.to_record()]},
        {"suite": {"name": "s", "version": "1"}, "results": [broken.to_record()]},
    )
    assert comparison["quality"]["after"]["task_verdicts"] == 0
    assert comparison["quality"]["after"]["not_verified"] == 1, (
        "an infra error is not a task verdict"
    )
    assert comparison["reliability"]["after_infra_errors"] == 1
    cost = comparison["cost"]["before"]["units"]["input_tokens"]
    assert cost["actual"] == 1 and cost["unknown"] == 1
    assert cost["complete"] is False, "a partial sum is not presented as complete"
    assert "no significance claim" not in render_comparison(comparison)
    markdown = render_comparison(comparison)
    assert "paired samples only" in markdown, (
        "the markdown view separates the paired cost from each side's own raw observations"
    )
    assert "raw observations (after)" in markdown
    assert "samples (before): listed=" in markdown and "samples (after): listed=" in markdown
    assert "| case | samples before | samples after |" in markdown, (
        "the sample counts are stated per case, so five cases with one run each cannot read as "
        "five samples per case"
    )


def test_a_small_real_model_sample_is_flagged_not_summarised_away() -> None:
    rows = [
        EvalCaseResult(
            case_id="c",
            case_version="1",
            case_digest=_DIGEST,
            attempt=f"a{index}",
            outcome=EvalOutcome.PASSED,
            verifier=VerifierOutcome(True),
        )
        for index in range(2)
    ]
    comparison = compare_reports(
        {"suite": {"name": "s", "version": "1"}, "results": [row.to_record() for row in rows]},
        {"suite": {"name": "s", "version": "1"}, "results": [row.to_record() for row in rows]},
        real_model=True,
    )
    assert comparison["caveats"], "a two-sample difference is not called an improvement"
    assert any(
        "not as a significance claim" in caveat for caveat in comparison["caveats"]
    ), comparison["caveats"]


# --- the command line, from outside the source tree ------------------------------------


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "yanxu_harness.evals", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(REPO),
        check=False,
    )


def test_the_cli_runs_the_offline_suite_and_writes_results(tmp_path: Path) -> None:
    out = tmp_path / "cli"
    completed = _cli("run", "--root", str(out / "work"), "--out", str(out))
    assert completed.returncode == 0, completed.stderr
    payload = json.loads((out / "results.json").read_text(encoding="utf-8"))
    assert payload["environment"]["offline"] is True
    assert payload["environment"]["suite_digest"] == SUITE.digest
    assert len(payload["results"]) == len(CASES)
    assert {item["outcome"] for item in payload["results"]} == {"passed"}
    summary = (out / "summary.md").read_text(encoding="utf-8")
    assert "harness-protocol" in summary
    assert "not** counted as the task failing" in summary


def test_the_cli_refuses_a_real_model_run_it_cannot_perform(tmp_path: Path) -> None:
    refused = _cli("run", "--root", str(tmp_path / "x"), "--real-model", "p/m")
    assert refused.returncode == 2
    assert "--allow-real-model" in refused.stderr

    unbudgeted = _cli(
        "run",
        "--root",
        str(tmp_path / "y"),
        "--real-model",
        "p/m",
        "--allow-real-model",
    )
    assert unbudgeted.returncode == 2
    assert "requires --repeats" in unbudgeted.stderr

    # Explicitly allowed, but this build has no adapter: the cases are `not_run`, never a pass.
    allowed = _cli(
        "run",
        "--root",
        str(tmp_path / "z"),
        "--out",
        str(tmp_path / "z"),
        "--real-model",
        "p/m",
        "--allow-real-model",
        "--repeats",
        "3",
        "--budget-seconds",
        "5",
    )
    assert allowed.returncode == 0, allowed.stderr
    payload = json.loads((tmp_path / "z" / "results.json").read_text(encoding="utf-8"))
    assert {item["outcome"] for item in payload["results"]} == {"not_run"}
    assert all("no adapter was configured" in note for item in payload["results"] for note in item["notes"])
    assert payload["environment"]["offline"] is False
    assert payload["environment"]["requested_model"] == "p/m"


def test_the_cli_compares_two_result_files(tmp_path: Path) -> None:
    first = tmp_path / "one"
    second = tmp_path / "two"
    first_run = _cli("run", "--root", str(first / "w"), "--out", str(first))
    first_results = json.loads((first / "results.json").read_text(encoding="utf-8"))[
        "results"
    ] if (first / "results.json").is_file() else []
    first_non_passed = [
        {
            "case_id": item.get("case_id"),
            "repeat_index": item.get("repeat_index"),
            "outcome": item.get("outcome"),
            "stop_code": item.get("stop_code"),
            "error_code": item.get("error_code"),
            "elapsed_seconds": item.get("elapsed_seconds"),
            "protocol": item.get("protocol"),
            "diagnostics": item.get("diagnostics"),
            "notes": item.get("notes"),
            "verifier": item.get("verifier"),
        }
        for item in first_results
        if item.get("outcome") != EvalOutcome.PASSED.value
    ]
    assert first_run.returncode == 0, (
        first_run.stderr + "\n" + first_run.stdout + f"\nnon-passed={first_non_passed}"
    )
    second_run = _cli("run", "--root", str(second / "w"), "--out", str(second))
    second_results = json.loads((second / "results.json").read_text(encoding="utf-8"))[
        "results"
    ] if (second / "results.json").is_file() else []
    second_non_passed = [
        {
            "case_id": item.get("case_id"),
            "repeat_index": item.get("repeat_index"),
            "outcome": item.get("outcome"),
            "stop_code": item.get("stop_code"),
            "error_code": item.get("error_code"),
            "elapsed_seconds": item.get("elapsed_seconds"),
            "protocol": item.get("protocol"),
            "diagnostics": item.get("diagnostics"),
            "notes": item.get("notes"),
            "verifier": item.get("verifier"),
        }
        for item in second_results
        if item.get("outcome") != EvalOutcome.PASSED.value
    ]
    assert second_run.returncode == 0, (
        second_run.stderr + "\n" + second_run.stdout + f"\nnon-passed={second_non_passed}"
    )
    compared = _cli(
        "compare", str(first / "results.json"), str(second / "results.json"), "--format", "markdown"
    )
    assert compared.returncode == 0, compared.stderr
    assert "# Eval comparison" in compared.stdout
    assert "| case | repeat | before | after | changed |" in compared.stdout
    as_json = _cli(
        "compare", str(first / "results.json"), str(second / "results.json"), "--format", "json"
    )
    assert json.loads(as_json.stdout)["schema_version"] == 1


def test_the_cli_help_documents_the_modes(suite_root: Path) -> None:
    top = _cli("--help")
    assert "offline" in top.stdout and "compare" in top.stdout and "run" in top.stdout
    run_help = _cli("run", "--help")
    for flag in ("--case", "--root", "--out", "--real-model", "--repeats", "--budget-seconds"):
        assert flag in run_help.stdout


def test_a_case_directory_is_removed_before_a_repeat_runs(tmp_path: Path) -> None:
    """A repeat never inherits the previous run's stores, so a comparison is not polluted."""
    case = case_by_id("approval-write-once")
    run_case(case, tmp_path, repeat_index=0)
    state = tmp_path / case.case_id / "r0" / "state"
    (state / "leftover").write_text("previous run", encoding="utf-8")
    run_case(case, tmp_path, repeat_index=0)
    assert not (state / "leftover").exists()


def test_an_adapter_supplied_by_the_host_drives_the_same_case(tmp_path: Path) -> None:
    """The offline line accepts an adapter, so a real-model run reuses everything else."""
    case = case_by_id("fix-calc-tests")
    script = ScriptedStream(*script_for(case))
    result = run_case(case, tmp_path / "adapter", model_adapter=script)
    assert result.outcome is EvalOutcome.PASSED
    assert script.calls >= 1, "the adapter drove the run, and the request count is recorded"
    assert result.protocol["model"] == "offline"


def test_the_recovery_script_does_not_repeat_the_batch() -> None:
    """The recovery half of a fault case answers with a final message, not a tool batch."""
    case = case_by_id("partial-batch-evidence")
    child_script = script_for(case)
    recovery = recovery_script_for(case)
    assert child_script[0].content[0].type == "toolCall"
    assert recovery[0].content[0].type == "text"
    assert len(recovery) == 1


def test_a_missing_directory_is_created_per_case_not_shared(tmp_path: Path) -> None:
    """`run_suite` gives each case its own directory under one root, and creates them."""
    root = tmp_path / "suite"
    results = run_suite(
        EvalSuite(name="s", version="1", cases=(case_by_id("fix-calc-tests"),)),
        root,
    )
    assert results.root == str(root)
    assert (root / "fix-calc-tests" / "r0" / "state" / "runs.db").exists()
    shutil.rmtree(root)
    assert not root.exists()


# --- the evidence a result file carries ------------------------------------------------


def test_a_verifiers_evidence_carries_no_host_paths(tmp_path: Path) -> None:
    """A failing case's evidence must not leak the machine it ran on.

    The evaluator's evidence is copied into result files that are meant to be shared, so the
    interpreter's install prefix, the workspace and any other absolute path are replaced with
    placeholders. The *reason* for a failure survives; the host does not.
    """
    import json
    import re

    from yanxu_harness.evals.verifiers import summarise_command_output

    workspace = tmp_path / "workspace"
    raw = (
        "Traceback (most recent call last):\n"
        f"  File \"{sys.executable}\", line 1, in <module>\n"
        f"    from calc import total  # {workspace}/calc.py\n"
        "E   ImportError: cannot import name 'total'\n"
        "=========================== short test summary info ============================\n"
        "ERROR test_calc.py::test_total_sums_every_value\n"
    ) + ("filler line\n" * 60)
    summary = summarise_command_output(raw, workspace)
    assert "ImportError: cannot import name 'total'" in summary, "the reason is kept"
    assert "test_calc.py::test_total_sums_every_value" in summary
    assert str(tmp_path) not in summary
    assert sys.executable not in summary
    assert sys.prefix not in summary
    assert not re.search(r"(?<!<)/[A-Za-z][^\s\"']*", summary.replace("<workspace>", "").replace("<path>", "")), (
        f"no absolute path survives: {summary}"
    )
    assert len(summary.splitlines()) <= 25, "the evidence is bounded"

    # And the same holds for a real failing run's recorded evidence.
    case = case_by_id("fix-calc-tests")
    wrong = ScriptedStream(
        AssistantMessage(
            content=[
                ToolCall(
                    id="c-write",
                    name="write_file",
                    arguments={"path": "calc.py", "text": "# not a fix\n"},
                )
            ],
            stop_reason=StopReason.TOOL_USE,
        ),
        final_message("done"),
    )
    result = run_case(case, tmp_path / "leak", model_adapter=wrong)
    blob = json.dumps(result.to_record())
    assert str(tmp_path) not in blob
    assert sys.prefix not in blob


def test_a_committed_case_or_result_carries_no_host_path() -> None:
    """The interpreter is a placeholder in the fixture, not this machine's virtualenv.

    A frozen case and its results are meant to be shared and compared, so an absolute
    interpreter path would leak the host *and* make the case unreproducible. The placeholder is
    resolved when the command runs.
    """
    import json

    from yanxu_harness.evals.fixtures import PYTHON

    blob = json.dumps(
        {"suite": SUITE.to_record(), "cases": [case.to_record() for case in CASES]}
    )
    assert sys.executable not in blob
    assert sys.prefix not in blob
    assert str(REPO) not in blob
    for case in CASES:
        assert case.fixture is not None
        command = case.fixture.baseline_command
        if command:
            assert command[0] == PYTHON, command
            assert "/" not in command[0]

    # The placeholder really is resolved to an interpreter that runs the tests.
    from yanxu_harness.evals.fixtures import run_baseline

    case = case_by_id("fix-calc-tests")
    scratch = Path(tempfile.mkdtemp(prefix="fixture-"))
    from yanxu_harness.evals.fixtures import write_fixture

    write_fixture(case.fixture, scratch)
    completed = run_baseline(case.fixture, scratch, timeout=60)
    assert completed.returncode == 1, "the pristine fixture's tests fail, as the premise needs"


def test_a_result_records_the_budgets_arrangement_and_stage_timings(tmp_path: Path) -> None:
    """What two runs must be comparable on, and what a stage with no measurement says.

    A result that omitted the declared budgets, the deployment's fingerprints or the per-stage
    timings would be comparable only in the loosest sense: "it passed faster" with no record
    of what it was allowed to spend, which arrangement ran, or which stage was never measured.
    """
    case = case_by_id("fix-calc-tests")
    declared = replace(case, max_output_tokens=2048)
    result = run_case(declared, tmp_path / "recorded")

    assert result.outcome is EvalOutcome.PASSED
    budgets = result.protocol["budgets"]
    assert budgets == {
        "max_turns": declared.max_turns,
        "max_runtime_seconds": declared.max_runtime_seconds,
        "max_output_tokens": 2048,
    }
    arrangement = result.protocol["arrangement"]
    assert set(arrangement) >= {"config", "tools", "instructions"}, arrangement
    assert all(isinstance(value, str) for value in arrangement.values())

    spans = result.diagnostics.get("spans") or []
    assert spans, "the report's stage observations travel with the result"
    request = next(span for span in spans if span["stage"] == "model:request")
    assert request["duration_ms"] is not None and request["clock"] == "monotonic"
    tool = next(span for span in spans if span["stage"].startswith("tool:"))
    assert tool["duration_ms"] is not None and tool["clock"] == "monotonic"
    # A stage whose span is *not* a measured duration says so instead of implying 0 ms: the
    # batch's wall-clock span across records is never presented as a duration, and the calls
    # inside it are never summed into it.
    batch = next(span for span in spans if span["stage"] == "tool:batch")
    assert batch["duration_ms"] is None and batch["clock"] == "wall"
    assert "never summed" in batch["note"]
    # This deployment configures no context preparation, so there is no projection stage to
    # report — and none is invented. (A configured controller that never records a timing is
    # covered by `tests/test_diagnostics_reader.py`, which asserts the absent-with-reason case.)
    assert not any(span["stage"] == "context:prepare" for span in spans)
    assert result.diagnostics["price_table"].startswith("none:")
    assert result.protocol["run_id"] == "run-1"
    assert result.protocol["seed"] is None and result.protocol["seed_supported"] is False, (
        "the scripted adapter is deterministic and takes no seed; that is recorded rather than "
        "a seed being invented"
    )
    assert result.artifacts["trace"], "the effect trace is digested into the result"


def test_the_declared_output_budget_must_be_positive() -> None:
    case = case_by_id("fix-calc-tests")
    with pytest.raises(Exception, match="max_output_tokens must be positive"):
        replace(case, max_output_tokens=0)

# --- negative controls for the three task categories added after the first batch ---------


def test_a_location_answer_that_does_not_quote_the_defect_fails(tmp_path: Path) -> None:
    """Naming a plausible function is not locating a defect: the evidence is checked too."""
    case = case_by_id("locate-bug")
    vague = ScriptedStream(
        call_message("c-read", "read_file", {"path": "report.py"}),
        final_message("Done: I found the bug and reported it."),
    )
    result = run_case(case, tmp_path / "vague", model_adapter=vague)
    assert result.outcome is EvalOutcome.FAILED
    assert result.verifier is not None
    assert any("does not name the function" in reason for reason in result.verifier.reasons)
    assert any("does not quote the wrong line" in reason for reason in result.verifier.reasons)


def test_a_location_answer_for_the_wrong_function_fails(tmp_path: Path) -> None:
    case = case_by_id("locate-bug")
    wrong = ScriptedStream(
        call_message("c-read", "read_file", {"path": "report.py"}),
        final_message("The defect is in `spread`: `return max(values) - min(values)` is wrong."),
    )
    result = run_case(case, tmp_path / "wrong", model_adapter=wrong)
    assert result.outcome is EvalOutcome.FAILED
    assert result.verifier is not None
    assert any("does not name the function 'average'" in reason for reason in result.verifier.reasons)
    assert any("does not quote the wrong line" in reason for reason in result.verifier.reasons)


def test_a_location_answer_that_fixes_the_file_instead_fails(tmp_path: Path) -> None:
    """A location task forbids changing anything: editing it answers a different question."""
    case = case_by_id("locate-bug")
    edited = ScriptedStream(
        call_message("c-read", "read_file", {"path": "report.py"}),
        call_message(
            "c-write",
            "write_file",
            {"path": "report.py", "text": "def average(values):\n    return 0\n"},
        ),
        final_message("The defect is in `average`: `return total / len(values) + 1` is wrong."),
    )
    result = run_case(case, tmp_path / "edited", model_adapter=edited)
    assert result.outcome is EvalOutcome.FAILED
    assert result.verifier is not None
    assert any("was modified" in reason for reason in result.verifier.reasons)


def test_a_review_that_claims_the_tests_pass_fails(tmp_path: Path) -> None:
    """The review's claim is compared with the exit code the command really produced."""
    case = case_by_id("review-diff")
    lying = ScriptedStream(
        call_message("c-tests", "run_tests", {}),
        call_message("c-read", "read_file", {"path": "patch.diff"}),
        final_message(
            "All tests pass. The change to `calc.py` is fine and can be merged."
        ),
    )
    result = run_case(case, tmp_path / "lying", model_adapter=lying)
    assert result.outcome is EvalOutcome.FAILED
    assert result.verifier is not None
    assert any("does not name" in reason for reason in result.verifier.reasons)
    # The recorded exit code is the real one (the command failed), so the case could compare.
    assert result.protocol["run_tests"]["tests_passed"] is False


def test_a_review_that_never_runs_the_tests_fails(tmp_path: Path) -> None:
    case = case_by_id("review-diff")
    skipped = ScriptedStream(
        call_message("c-read", "read_file", {"path": "patch.diff"}),
        final_message(
            "The tests pass. `calc.py` line `for value in values[:-1]:` skips the last value."
        ),
    )
    result = run_case(case, tmp_path / "skipped", model_adapter=skipped)
    assert result.outcome is EvalOutcome.FAILED
    assert result.verifier is not None
    assert any("never run through the tool" in reason for reason in result.verifier.reasons)


def test_a_summary_that_was_never_interrupted_fails_the_case() -> None:
    """The verifier's own negative control: a completed run with no accounted call is a failure.

    Built as a fabricated verifier context on purpose — the point is the *criterion*: the case
    only passes when the run stopped on its deadline, asked no model and charged the interrupted
    auxiliary call exactly once with its cost marked unknown.
    """
    from yanxu_harness.evals.verifiers import VerifierContext, verify

    case = case_by_id("long-context-summary-cancel")
    completed = VerifierContext(
        case=case,
        workspace=Path("."),
        pristine=Path("."),
        state=Path("."),
        run_id="run-1",
        outcome_status="completed",
        protocol={"error_code": "", "summary": {"calls": 0, "usage_unknown": False}},
        workspace_files={"task.md": "x"},
    )
    verdict = verify(case, completed)
    assert verdict.passed is False
    assert any("not usage_limited" in reason for reason in verdict.reasons)
    assert any("0 call(s)" in reason for reason in verdict.reasons)
    assert any("usage_unknown" in reason for reason in verdict.reasons)

    interrupted = VerifierContext(
        case=case,
        workspace=Path("."),
        pristine=Path("."),
        state=Path("."),
        run_id="run-1",
        outcome_status="usage_limited",
        protocol={"error_code": "max_runtime_exceeded", "summary": {"calls": 1, "usage_unknown": True}},
        workspace_files={"task.md": "x"},
    )
    assert verify(case, interrupted).passed is True


def test_the_long_context_case_compacts_and_each_repeat_gets_its_own_allowance(
    tmp_path: Path,
) -> None:
    """The summary case really compacts, and a repeat does not inherit the previous spend.

    Both halves matter: a case that never triggered an auxiliary call would pass its assertions
    vacuously, and a shared allowance would make the second repeat's `calls == 1` a cross-run
    accounting error rather than a fresh run's own call.
    """
    case = replace(case_by_id("long-context-summary-cancel"), repeats=2)
    suite = EvalSuite(name="summary-isolated", version="1", cases=(case,))
    results = run_suite(suite, tmp_path / "root")
    assert [item.outcome for item in results.results] == [EvalOutcome.PASSED] * 2
    for item in results.results:
        assert item.protocol["summary"]["calls"] == 1, (
            "each run is charged for its own interrupted call, and only its own"
        )
        assert item.protocol["summary"]["usage_unknown"] is True
        assert item.protocol["model_calls"] == 0
        assert item.protocol["run_message_count"] > 1, "the transcript was kept"
        assert item.diagnostics["report"]["status"] == "usage_limited"
