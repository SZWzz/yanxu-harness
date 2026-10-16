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
    "case_by_id",
    "case_set_digest",
    "recovery_script_for",
    "script_for",
    "SUITE",
]

CASES_VERSION = "1"

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
        # The second probe is settled by business evidence (as in the partial-batch case) so
        # the recovery reaches the third probe and can be stopped *inside* it.
        expectations={"blocking_probe": "probe.c", "evidence": "succeeded"},
    ),
)

SUITE = EvalSuite(name="harness-protocol", version=CASES_VERSION, cases=CASES)


def case_by_id(case_id: str) -> EvalCase:
    for case in CASES:
        if case.case_id == case_id:
            return case
    raise KeyError(case_id)


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
