"""The frozen cases: five tasks, each with an independent verifier and a fixed script.

The offline line measures the *harness*, so the "model" is a script: what varies between
cases is which protocol situation the run is put in (a clean edit, an approval pause, a crash
mid-batch, a drifted deployment, a deadline) and how an independent verifier decides whether
the run actually did the task.

Cases are versioned and digested. A case is never edited in place to make a comparison look
better: a changed case is a new version, and results from two versions are reported as
incomparable rather than averaged together.
"""

from __future__ import annotations

from typing import Any

from ..core.types import AssistantMessage
from .fixtures import PROBE_TOOLS, call_message, final_message, fixture_by_name, probe_message
from .types import EvalCase, EvalSuite, FixtureSpec
from .verifiers import VERIFIERS, verifier_digest

__all__ = [
    "CASES",
    "CASES_VERSION",
    "DEADLINE_BUDGET_SECONDS",
    "HISTORICAL_CASES",
    "RECOVERY_BLOCKING_SECONDS",
    "case_by_id",
    "case_set_digest",
    "historical_by_id",
    "recovery_script_for",
    "script_for",
    "SUITE",
]

#: The exact defect the `locate-bug` task has to find, and the evidence it has to quote. It
#: lives with the case, not in the fixture: the fixture is what the run may read, this is the
#: truth the verifier checks the answer against.
LOCATED_DEFECT = {
    "file": "report.py",
    "function": "average",
    "line": "return total / len(values) + 1",
}

#: The defect the `review-diff` task has to identify, taken from the patch the fixture carries.
REVIEWED_DEFECT = {
    "file": "calc.py",
    "line": "for value in values[:-1]:",
    "why": "the last value is skipped, so the sum is wrong",
}

CASES_VERSION = "2"

#: The budget the two deadline cases use, and its measurement.
#:
#: The run's deadline covers *everything* one invocation does — reading the ledger, running the
#: host reconciler, committing state, admitting a call and the call itself. Version 1 of these
#: cases used 50 ms for the whole procedure, which silently required the preparation to fit in
#: 50 ms on every machine: the Windows runner needed more, timed out before the blocking tool
#: was admitted at all, and the verifier read that (correct) runtime behaviour as "the tool
#: never started". Measured here: the preparation of the recovery case takes ~3–10 ms on macOS
#: and did not fit in 50 ms on windows-latest, so the old budget was a machine-speed assumption,
#: not a deadline.
#:
#: The number is fixed, cross-platform and generous: 2 s covers the measured preparation by two
#: orders of magnitude on this machine and leaves room for a slow CI filesystem, while the
#: operation the deadline has to interrupt blocks for `RECOVERY_BLOCKING_SECONDS` (10 s), five
#: times the whole budget. It is not raised for slow hosts (no `os.name` special case, no
#: per-run adaptation) and nothing waits or sleeps for it: the block is cancelled at the budget.
DEADLINE_BUDGET_SECONDS = 2.0

#: How long the probe the recovery-deadline case dispatches blocks. It must exceed the whole
#: budget: a handler that finishes first would make the case about the wrong window.
RECOVERY_BLOCKING_SECONDS = 10.0

#: The long-context task's inputs: a transcript the projection must compact, whose only
#: auxiliary call is cancelled by the run's own deadline.
LONG_CONTEXT = {"turns": 10, "newest": "keep this final instruction verbatim"}

#: How a case asks for the blocking auxiliary path: the deadline is what ends it, and the run
#: must stop with the call's cost still accounted.
SUMMARY_CANCEL = "blocking-summarizer"

#: The corrected implementation the scripted model writes. It is the *task's* answer, and the
#: verifier does not read it: it runs the fixture's own tests, which is why a wrong answer here
#: would show up as a failed case rather than as a passed one.
FIXED_CALC = '''"""A tiny module with one bug the task has to find."""


def total(values):
    """Return the sum of ``values``."""
    result = 0
    for value in values:
        result += value
    return result
'''

_NOTES_TEXT = "the line the task asked for\n"


def _case(**fields: Any) -> EvalCase:
    return EvalCase(**fields)


CASES: tuple[EvalCase, ...] = (
    _case(
        case_id="fix-calc-tests",
        version="1",
        instructions=(
            "The project's tests fail. Fix the implementation in calc.py so they pass. Do not "
            "edit the tests."
        ),
        instructions_ref="fixture:calc/task.md",
        fixture=fixture_by_name("calc"),
        tool_config={"profile": "edit", "auto_write": True},
        verifier="fix-calc-tests",
        verifier_digest=verifier_digest("fix-calc-tests"),
        max_turns=4,
        expectations={"edited": "calc.py", "protected": "test_calc.py"},
    ),
    _case(
        case_id="approval-write-once",
        version="1",
        instructions="Write the requested line into notes.md. The write needs approval.",
        instructions_ref="fixture:notes/task.md",
        fixture=fixture_by_name("notes"),
        tool_config={"profile": "edit", "auto_write": False},
        verifier="write-once-approved",
        verifier_digest=verifier_digest("write-once-approved"),
        max_turns=4,
        expectations={"path": "notes.md", "text": _NOTES_TEXT, "approve": True},
    ),
    _case(
        case_id="partial-batch-evidence",
        version="1",
        instructions=(
            "Three probes run in one batch. The process dies with the first settled and the "
            "second started; business evidence then confirms the second and the batch continues."
        ),
        fixture=fixture_by_name("notes"),
        tool_config={"profile": "probe"},
        verifier="partial-batch-evidence",
        verifier_digest=verifier_digest("partial-batch-evidence"),
        max_turns=4,
        allowed_faults=("partial-batch-then-die",),
        expectations={"probes": list(PROBE_TOOLS), "evidence": "succeeded"},
    ),
    _case(
        case_id="drift-refusal",
        version="1",
        instructions=(
            "The same batch crashed and only the second probe's evidence exists. The deployment "
            "that resumes has different instructions, so the recovery must refuse before it "
            "dispatches the third probe."
        ),
        fixture=fixture_by_name("notes"),
        tool_config={"profile": "probe"},
        verifier="drift-refused",
        verifier_digest=verifier_digest("drift-refused"),
        max_turns=4,
        allowed_faults=("partial-batch-then-die",),
        expectations={"refused_dimension": "instructions", "changed_system_prompt": "a different deployment"},
    ),
    _case(
        case_id="deadline-during-recovery",
        version="2",
        instructions=(
            "The same crashed batch is recovered under a small wall-clock budget while the "
            "remaining probe blocks: the budget must stop the dispatch and the run."
        ),
        fixture=fixture_by_name("notes"),
        tool_config={"profile": "probe"},
        verifier="deadline-stops-recovery-v2",
        verifier_digest=verifier_digest("deadline-stops-recovery-v2"),
        max_turns=4,
        # Enough for the real preparation (ledger reads, the reconciler, the admission) and far
        # less than the 10 s the target probe blocks. Version 1's 50 ms was a machine-speed
        # assumption; see `DEADLINE_BUDGET_SECONDS`.
        max_runtime_seconds=DEADLINE_BUDGET_SECONDS,
        allowed_faults=("partial-batch-then-die",),
        # The second probe is settled by business evidence (as in the partial-batch case) so
        # the recovery reaches the third probe and can be stopped *inside* it.
        expectations={"blocking_probe": "probe.c", "evidence": "succeeded"},
    ),
    _case(
        case_id="locate-bug",
        version="1",
        instructions=(
            "Read report.py, find the one defect and report which function contains it with the "
            "exact wrong line quoted. Change nothing."
        ),
        instructions_ref="fixture:buggy/task.md",
        fixture=fixture_by_name("buggy"),
        tool_config={"profile": "edit", "auto_write": True},
        verifier="locate-bug",
        verifier_digest=verifier_digest("locate-bug"),
        max_turns=4,
        expectations=dict(LOCATED_DEFECT),
    ),
    _case(
        case_id="review-diff",
        version="1",
        instructions=(
            "Run the project's own tests, then review the change to calc.py: report the real "
            "test result and name the defect it introduces, with the file and the exact line."
        ),
        instructions_ref="fixture:review/task.md",
        fixture=fixture_by_name("review"),
        tool_config={"profile": "review", "auto_write": True},
        verifier="review-diff",
        verifier_digest=verifier_digest("review-diff"),
        max_turns=4,
        expectations=dict(REVIEWED_DEFECT),
    ),
    _case(
        case_id="long-context-summary-cancel",
        version="2",
        instructions=(
            "A long transcript has to be compacted for the next request. The auxiliary call is "
            "interrupted by the run's deadline: the run must stop, spend no main-model request, "
            "keep the original messages and account for the auxiliary call once."
        ),
        fixture=fixture_by_name("notes"),
        tool_config={"profile": "edit", "auto_write": True, "context": SUMMARY_CANCEL},
        verifier="summary-cancelled",
        verifier_digest=verifier_digest("summary-cancelled"),
        max_turns=4,
        # Small enough that the blocking summarizer cannot finish, large enough that the real
        # preparation (the projection and the auxiliary call's admission) fits. The verdict is
        # `usage_limited` / `max_runtime_exceeded`, never `completed`; version 1's 50 ms made
        # the same mistake as the recovery deadline case.
        max_runtime_seconds=DEADLINE_BUDGET_SECONDS,
        expectations=dict(LONG_CONTEXT),
    ),
)

#: The superseded versions, kept as *history* and never run by this build.
#:
#: A frozen case is a question, so "this question was wrong" is expressed as a new version, not
#: as an edit: results produced by these hold their own digests and stay readable, and the
#: comparator refuses to pair them with a version-2 result because the questions differ. They
#: are here so the old identities can still be named (and checked) instead of being deleted.
HISTORICAL_CASES: tuple[EvalCase, ...] = (
    _case(
        case_id="deadline-during-recovery",
        version="1",
        instructions=(
            "The same crashed batch is recovered under a tiny wall-clock budget while the "
            "remaining probe blocks: the budget must stop the dispatch and the run."
        ),
        fixture=fixture_by_name("notes"),
        tool_config={"profile": "probe"},
        verifier="deadline-stops-recovery",
        verifier_digest=verifier_digest("deadline-stops-recovery"),
        max_turns=4,
        max_runtime_seconds=0.05,
        allowed_faults=("partial-batch-then-die",),
        expectations={"blocking_probe": "probe.c", "evidence": "succeeded"},
    ),
    _case(
        case_id="long-context-summary-cancel",
        version="1",
        instructions=(
            "A long transcript has to be compacted for the next request. The auxiliary call is "
            "interrupted by the run's deadline: the run must stop, spend no main-model request, "
            "keep the original messages and account for the auxiliary call once."
        ),
        fixture=fixture_by_name("notes"),
        tool_config={"profile": "edit", "auto_write": True, "context": SUMMARY_CANCEL},
        verifier="summary-cancelled",
        verifier_digest=verifier_digest("summary-cancelled"),
        max_turns=4,
        max_runtime_seconds=0.05,
        expectations=dict(LONG_CONTEXT),
    ),
)

SUITE = EvalSuite(name="harness-protocol", version=CASES_VERSION, cases=CASES)


def case_by_id(case_id: str) -> EvalCase:
    for case in CASES:
        if case.case_id == case_id:
            return case
    raise KeyError(case_id)


def historical_by_id(case_id: str, version: str = "1") -> EvalCase:
    """A superseded version of a case, for reading an old result or checking its identity."""
    for case in HISTORICAL_CASES:
        if case.case_id == case_id and case.version == version:
            return case
    raise KeyError(f"{case_id}@{version}")


def case_set_digest() -> str:
    return SUITE.digest


def script_for(case: EvalCase) -> tuple[AssistantMessage, ...]:
    """The deterministic offline script for one case.

    Kept here, next to the case, because for the offline line the script *is* the model's
    behaviour: a case and the script that answers it are one frozen question-and-answer pair.
    """
    if case.case_id == "fix-calc-tests":
        return (
            call_message(
                "c-write", "write_file", {"path": "calc.py", "text": FIXED_CALC}
            ),
            final_message("fixed"),
        )
    if case.case_id == "approval-write-once":
        return (
            call_message(
                "c-write", "write_file", {"path": "notes.md", "text": _NOTES_TEXT}
            ),
            final_message("written"),
        )
    if case.case_id in {"partial-batch-evidence", "drift-refusal", "deadline-during-recovery"}:
        return (probe_message(), final_message("done"))
    if case.case_id == "locate-bug":
        # A *location* task: the answer is a report, and the verifier checks it against the
        # recorded defect rather than against a sentence like "done".
        return (
            call_message("c-read", "read_file", {"path": "report.py"}),
            final_message(
                "The defect is in `average`: `return total / len(values) + 1` adds one to the "
                "mean, so every average is wrong. `spread` is correct."
            ),
        )
    if case.case_id == "review-diff":
        # The review *states the real result* and names the defect: the verifier compares this
        # with the exit code the command actually produced, so a review that claims the tests
        # pass while they fail is a failed case rather than a passing one.
        return (
            call_message("c-tests", "run_tests", {}),
            call_message("c-read", "read_file", {"path": "patch.diff"}),
            final_message(
                "The project's own tests fail (exit 1). The change to `calc.py` skips the last "
                "value: the line `for value in values[:-1]:` means the sum never includes the "
                "final element, so `total([1, 2, 3])` returns 3 instead of 6."
            ),
        )
    if case.case_id == "long-context-summary-cancel":
        return (final_message("never reached"),)
    raise KeyError(
        f"case {case.case_id!r} has no offline script; a real-model case must supply an adapter"
    )


def recovery_script_for(case: EvalCase) -> tuple[AssistantMessage, ...]:
    """The script for the *recovering* process of a fault case.

    The model is only asked after the recovered batch has settled, so the recovery script
    starts with its answer instead of repeating the tool batch. Keeping it separate from
    :func:`script_for` is the point: the child's first request and the recovery's first request
    are different moments in the same run, and conflating them would dispatch the batch twice
    — which is exactly what a verifier would (correctly) report as a failure.
    """
    if case.model != "offline":
        raise KeyError(f"case {case.case_id!r} is a real-model case and has no fixed script")
    if not case.allowed_faults:
        return script_for(case)
    return (final_message("recovered"),)


_ = (FixtureSpec, VERIFIERS)
