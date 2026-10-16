"""The independent verifiers: how a case is decided, outside the run's control.

A verifier is independent in three concrete ways, and each one is a defect it prevents:

* it reads the **workspace and the stores** itself, rather than the run's own description of
  what happened — a model (or a script) saying "done" is content, not evidence;
* its truth is the **pristine copy** of the fixture, kept outside the workspace, so editing
  the tests or the expectations cannot make a case pass;
* it **runs the fixture's own tests** in a subprocess for the cases where the task is a code
  change, because "the project's tests pass" is the acceptance criterion the task states.

Every verifier returns evidence with the verdict, and a verifier that cannot run returns an
infrastructure error instead of failing the task.
"""

from __future__ import annotations

import inspect
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from ..versioning import fingerprint
from .types import EvalCase, VerifierOutcome

__all__ = ["VerifierContext", "VERIFIERS", "verifier_digest"]


@dataclass(frozen=True)
class VerifierContext:
    """Everything a verifier is allowed to look at."""

    case: EvalCase
    workspace: Path
    pristine: Path
    state: Path
    run_id: str
    outcome_status: str
    #: The run's own final answer text. A verifier may check it against the recorded truth, but
    #: never treats it as the verdict: a claim of completion is content, not evidence.
    answer: str = ""
    error: str = ""
    protocol: Mapping[str, Any] = field(default_factory=dict)
    workspace_files: Mapping[str, str] = field(default_factory=dict)
    pristine_files: Mapping[str, str] = field(default_factory=dict)
    ledger: Mapping[str, str] = field(default_factory=dict)
    trace: tuple[str, ...] = ()
    model_calls: int = 0
    report: Mapping[str, Any] = field(default_factory=dict)
    #: Every command this verification ran, so the runner can check that the policy the case
    #: *declared* is the policy the executor actually used. A list (not a tuple) on purpose: the
    #: verifier appends, and nobody may insert or reorder what it recorded.
    commands: list[Any] = field(default_factory=list)


#: How much of a test command's output is kept as evidence, and how it is sanitised.
_MAX_OUTPUT_LINES = 25
_MAX_OUTPUT_CHARS = 2000


def scrub_command_text(text: str, workspace: Path) -> str:
    """Path-free text without the summary's line filtering.

    Used for short fields a result publishes verbatim — a command's argv, above all. The summary
    only keeps the lines that state a reason, which is right for a log and wrong for an argument
    list: an argument has to survive intact, with the machine's paths replaced and nothing else
    changed.
    """
    return "\n".join(_strip_paths(line, workspace) for line in text.splitlines())


def summarise_command_output(text: str, workspace: Path) -> str:
    """A bounded, path-free summary of a test command's output.

    Two things must not travel: the **host's paths** (the interpreter's install prefix, the
    evaluation's own directories) and unbounded text. Both are stripped here rather than at
    the point where a report is written, because a verifier's evidence is copied into result
    files that are meant to be shared. The *reason* for a failure survives — that is the
    evidence — while the machine it happened on does not.
    """
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    interesting = [
        line
        for line in lines
        # The lines that state *why* something failed, wherever they appear: a long traceback
        # can bury the reason above the tail, and a summary that only kept the tail would
        # report "1 error" and nothing else.
        if any(
            marker in line
            for marker in ("Error", "error", "assert", "FAILED", "ERROR", "Exception", "raise ")
        )
    ]
    tail = lines[-3:]
    kept: list[str] = []
    for line in [*interesting, *tail]:
        if line in kept:
            continue
        kept.append(line)
    kept = kept[-_MAX_OUTPUT_LINES:]
    return "\n".join(_strip_paths(line, workspace) for line in kept)[:_MAX_OUTPUT_CHARS]


def _strip_paths(line: str, workspace: Path) -> str:
    """Replace absolute paths in one line with placeholders.

    The workspace is named as such (a reader can tell *where* in the fixture something
    happened); every other absolute path becomes ``<path>``, which covers the interpreter, the
    virtual environment and anything a traceback mentions.
    """
    text = line.replace(str(workspace), "<workspace>")
    for prefix in (sys.executable, sys.prefix, str(Path(sys.executable).parent)):
        if prefix:
            text = text.replace(prefix, "<python>")
    out: list[str] = []
    index = 0
    while index < len(text):
        character = text[index]
        if character == "/" and (index == 0 or text[index - 1] in " \t\"'=([:"):
            end = index
            while end < len(text) and text[end] not in " \t\"',)]":
                end += 1
            candidate = text[index:end]
            out.append("<workspace>" if candidate.startswith(str(workspace)) else "<path>")
            index = end
            continue
        out.append(character)
        index += 1
    return "".join(out)


def _run_tests(context: VerifierContext) -> tuple[bool, str]:
    from .fixtures import run_baseline

    spec = context.case.fixture
    if spec is None:
        return False, "the case has no fixture, so its tests cannot be run"
    completed = run_baseline(spec, context.workspace, policy=context.case.command_policy)
    context.commands.append(completed)
    output = (completed.stdout or "") + (completed.stderr or "")
    return completed.returncode == 0, summarise_command_output(output, context.workspace)


def _verify_fix_calc_tests(context: VerifierContext) -> VerifierOutcome:
    """The task's own tests pass, and the tests themselves were not touched."""
    edited = str(context.case.expectations.get("edited") or "")
    protected = str(context.case.expectations.get("protected") or "")
    if not edited or not protected:
        return VerifierOutcome(False, infra_error="the case does not name what may be edited")
    if context.pristine_files.get(protected) != context.workspace_files.get(protected):
        return VerifierOutcome(
            False,
            reasons=[f"{protected} was modified; the tests are the specification"],
            evidence={"protected": protected},
        )
    if context.workspace_files.get(edited) == context.pristine_files.get(edited):
        return VerifierOutcome(
            False,
            reasons=[f"{edited} is unchanged, so nothing was fixed"],
            evidence={"edited": edited},
        )
    passed, output = _run_tests(context)
    if passed:
        return VerifierOutcome(
            True,
            evidence={
                "tests": "the project's own tests pass",
                "edited": edited,
                "protected_unchanged": protected,
            },
        )
    return VerifierOutcome(
        False,
        reasons=["the project's own tests still fail"],
        evidence={"output": output},
    )


def _verify_write_once_approved(context: VerifierContext) -> VerifierOutcome:
    """The write happened exactly once, across the pause and the resume."""
    path = str(context.case.expectations.get("path") or "")
    text = str(context.case.expectations.get("text") or "")
    if not path:
        return VerifierOutcome(False, infra_error="the case does not name the file to write")
    writes = [line for line in context.trace if ":write_file:" in line]
    target = context.workspace / path
    actual = target.read_text(encoding="utf-8") if target.exists() else None
    reasons: list[str] = []
    if actual != text:
        reasons.append(f"{path} holds {actual!r}, not the requested text")
    if len(writes) != 1:
        reasons.append(f"the write handler ran {len(writes)} time(s), not exactly once")
    if context.outcome_status != "completed":
        reasons.append(f"the run ended as {context.outcome_status!r}, not completed")
    if reasons:
        return VerifierOutcome(False, reasons=reasons, evidence={"writes": writes})
    return VerifierOutcome(
        True,
        evidence={"writes": len(writes), "file": path, "attempts": context.protocol.get("attempts")},
    )


def _verify_partial_batch_evidence(context: VerifierContext) -> VerifierOutcome:
    """No probe ran twice, the evidence converged, and the batch finished.

    The trace is the effect log: exactly one line per probe, in one pass, whatever the number
    of attempts. The ledger says what each operation is: a re-run would show up as an extra
    trace line even if the outcome looked right.
    """
    probes = [str(item) for item in context.case.expectations.get("probes") or ()]
    ran = [line.split(":")[1] for line in context.trace if line.count(":") >= 2]
    reasons: list[str] = []
    for name in probes:
        if ran.count(name) != 1:
            reasons.append(f"{name} ran {ran.count(name)} time(s), expected exactly one")
    if ran != probes:
        reasons.append(f"the probes ran in the order {ran}, not {probes}")
    missing = [name for name in probes if name not in context.ledger]
    if missing:
        reasons.append(f"the ledger records no operation for {missing}")
    wrong = {
        name: context.ledger.get(name)
        for name in probes
        if name in context.ledger and context.ledger[name] != "succeeded"
    }
    if wrong:
        reasons.append(f"the ledger does not hold these operations as succeeded: {wrong}")
    if context.outcome_status != "completed":
        reasons.append(f"the run ended as {context.outcome_status!r}, not completed")
    if reasons:
        return VerifierOutcome(False, reasons=reasons, evidence={"ran": ran, "ledger": dict(context.ledger)})
    return VerifierOutcome(
        True,
        evidence={
            "ran": ran,
            "operations": dict(context.ledger),
            "attempts": context.protocol.get("attempts"),
        },
    )


def _verify_drift_refused(context: VerifierContext) -> VerifierOutcome:
    """Nothing was dispatched under the drifted deployment, and the refusal named the drift."""
    dimension = str(context.case.expectations.get("refused_dimension") or "")
    ran = [line.split(":")[1] for line in context.trace if line.count(":") >= 2]
    reasons: list[str] = []
    if "probe.c" in ran:
        reasons.append("the third probe ran even though the deployment had drifted")
    if ran.count("probe.c"):
        reasons.append("the drifted deployment dispatched a call")
    refusal = context.protocol.get("refusal")
    if not isinstance(refusal, Mapping):
        reasons.append(f"the recovery did not refuse: {context.error or context.outcome_status!r}")
        refusal = {}
    else:
        if refusal.get("reason_code") != "arrangement_drift":
            reasons.append(f"the refusal reason was {refusal.get('reason_code')!r}")
        if dimension and dimension not in (refusal.get("drifted") or ()):
            reasons.append(f"the refusal did not name the {dimension} drift: {refusal!r}")
        if refusal.get("durable") is not False:
            reasons.append(
                "a refusal that writes nothing to the run must say so (durable=False); a host "
                "that needs an audit record writes one where it may"
            )
        recorded, current = refusal.get("recorded") or {}, refusal.get("current") or {}
        if dimension and recorded.get(dimension) == current.get(dimension):
            reasons.append(
                f"the refusal does not show the {dimension} baseline and the current value differ"
            )
    if context.model_calls:
        reasons.append("the resuming harness asked the model after the refusal")
    if reasons:
        return VerifierOutcome(
            False, reasons=reasons, evidence={"ran": ran, "error": context.error, "refusal": refusal}
        )
    return VerifierOutcome(
        True,
        evidence={"refused": dimension, "ran": ran, "refusal": refusal},
    )


def _verify_deadline_stops_recovery(context: VerifierContext) -> VerifierOutcome:
    """The budget stopped the run, and the interrupted call is still visible as admitted."""
    blocking = str(context.case.expectations.get("blocking_probe") or "")
    reasons: list[str] = []
    if context.outcome_status != "usage_limited":
        reasons.append(f"the run ended as {context.outcome_status!r}, not usage_limited")
    if context.protocol.get("error_code") != "max_runtime_exceeded":
        reasons.append(
            f"the stop code was {context.protocol.get('error_code')!r}, not max_runtime_exceeded"
        )
    if context.model_calls:
        reasons.append("the model was asked after the budget was exhausted")
    if blocking and blocking not in context.ledger:
        reasons.append(
            f"the ledger holds no admission for {blocking}, so an interrupted call looks like "
            "one that never started"
        )
    elif blocking and context.ledger[blocking] in {"", "absent"}:
        reasons.append(f"{blocking} is recorded as {context.ledger[blocking]!r}, not as admitted")
    if reasons:
        return VerifierOutcome(False, reasons=reasons, evidence={"ledger": dict(context.ledger)})
    return VerifierOutcome(
        True,
        evidence={
            "stop": "max_runtime_exceeded",
            # The state is whatever the ledger honestly holds — `unknown` after an interrupted
            # handler is the correct answer, and it is *not* the same as "no row".
            "blocking_probe_state": context.ledger.get(blocking, ""),
            "ledger": dict(context.ledger),
        },
    )


def _verify_locate_bug(context: VerifierContext) -> VerifierOutcome:
    """The answer names the *recorded* defect with its real evidence, and nothing was changed.

    Three ways this could be faked, and each is checked: naming the right function without the
    evidence, quoting the line without having read the file, and "fixing" the file instead of
    locating the defect in a task that forbids changes.
    """
    expected = dict(context.case.expectations)
    function = str(expected.get("function") or "")
    line = str(expected.get("line") or "")
    file = str(expected.get("file") or "")
    reasons: list[str] = []
    answer = context.answer
    if function not in answer:
        reasons.append(f"the answer does not name the function {function!r}")
    if line not in answer:
        reasons.append(f"the answer does not quote the wrong line {line!r}")
    reads = [line_ for line_ in context.trace if ":read_file:" in line_]
    if not any(file in line_ for line_ in reads):
        reasons.append(f"the run never read the file the defect is in: reads={reads!r}")
    if context.workspace_files.get(file) != context.pristine_files.get(file):
        reasons.append(f"{file} was modified; the task forbids changing anything")
    if reasons:
        return VerifierOutcome(False, reasons=reasons, evidence={"answer": answer})
    return VerifierOutcome(
        True,
        evidence={
            "located": function,
            "quoted": line,
            "read": reads,
            "unchanged": file,
        },
    )


def _verify_review_diff(context: VerifierContext) -> VerifierOutcome:
    """The review reports the *real* test result and names the seeded defect.

    The verifier runs the fixture's own command itself and compares it with what the review
    says, so a review that claims "tests pass" while they fail is a failure — the recorded
    `run_tests` result is compared with the independent one, action by action.
    """
    expected = dict(context.case.expectations)
    file = str(expected.get("file") or "")
    line = str(expected.get("line") or "")
    reasons: list[str] = []
    passed, output = _run_tests(context)
    if passed:
        reasons.append(
            "the fixture's tests pass, so this case no longer measures a failed review"
        )
    recorded = context.protocol.get("run_tests") or {}
    if not recorded:
        reasons.append("the project's tests were never run through the tool")
    elif bool(recorded.get("tests_passed")) != passed:
        reasons.append(
            f"the review reported tests_passed={recorded.get('tests_passed')!r} while the "
            f"command exits {recorded.get('exit_code')!r} (independently: passed={passed})"
        )
    answer = context.answer
    if file not in answer or line not in answer:
        reasons.append(f"the review does not name {file} and the exact line {line!r}")
    if reasons:
        return VerifierOutcome(
            False, reasons=reasons, evidence={"recorded": recorded, "output": output}
        )
    return VerifierOutcome(
        True,
        evidence={
            "tests_exit_code": recorded.get("exit_code"),
            "reviewed": {"file": file, "line": line},
            "independent_output": output,
        },
    )


def _verify_summary_cancelled(context: VerifierContext) -> VerifierOutcome:
    """The deadline stopped the auxiliary call, and its cost was still accounted for once.

    Four facts, and each of them was a defect once: the run stops (`usage_limited` /
    `max_runtime_exceeded`) instead of completing; no main-model request is made after the
    interrupted auxiliary call; the original messages are kept, because a summary that never
    arrived must not shrink the transcript; and the auxiliary call is charged exactly once with
    its cost marked unknown instead of becoming free. A second run (this case's other repeat)
    starts from its own allowance, which is why the count here is exactly one.
    """
    reasons: list[str] = []
    if context.outcome_status != "usage_limited":
        reasons.append(f"the run ended as {context.outcome_status!r}, not usage_limited")
    if str(context.protocol.get("error_code") or "") != "max_runtime_exceeded":
        reasons.append(
            f"the stop reason was {context.protocol.get('error_code')!r}, not max_runtime_exceeded"
        )
    if context.model_calls:
        reasons.append("the main model was asked after the auxiliary call was interrupted")
    summary = context.protocol.get("summary")
    if not isinstance(summary, Mapping):
        reasons.append("the terminal record carries no summary accounting at all")
        summary = {}
    calls = summary.get("calls")
    if calls != 1:
        reasons.append(
            f"the auxiliary path is recorded as {calls!r} call(s); an interrupted call is one "
            "call, never zero and never two"
        )
    if summary.get("usage_unknown") is not True:
        reasons.append(
            "an unconfirmed auxiliary call must stay usage_unknown; recording it as confirmed "
            "would turn 'we could not prove the cost' into a number"
        )
    if not context.trace and not context.workspace_files:
        reasons.append("the fixture was not materialised, so the premise cannot be checked")
    if reasons:
        return VerifierOutcome(False, reasons=reasons, evidence={"summary": dict(summary)})
    return VerifierOutcome(
        True,
        evidence={
            "stop": "max_runtime_exceeded",
            "summary": dict(summary),
            "main_model_calls": context.model_calls,
            "attempts": context.protocol.get("attempts"),
        },
    )


VERIFIERS: Mapping[str, Callable[[VerifierContext], VerifierOutcome]] = {
    "fix-calc-tests": _verify_fix_calc_tests,
    "write-once-approved": _verify_write_once_approved,
    "partial-batch-evidence": _verify_partial_batch_evidence,
    "drift-refused": _verify_drift_refused,
    "deadline-stops-recovery": _verify_deadline_stops_recovery,
    "locate-bug": _verify_locate_bug,
    "review-diff": _verify_review_diff,
    "summary-cancelled": _verify_summary_cancelled,
}


def verifier_digest(name: str) -> str:
    """A digest of the verifier's own source.

    Recorded on the case so that a result produced by an older verifier is never compared with
    one produced by a newer one: the verifier *is* the acceptance criterion, and a changed
    criterion is a changed question.
    """
    verifier = VERIFIERS.get(name)
    if verifier is None:
        raise KeyError(f"no verifier named {name!r}")
    return fingerprint({"verifier": name, "source": inspect.getsource(verifier)})


def verify(case: EvalCase, context: VerifierContext) -> VerifierOutcome:
    """Run the case's verifier, converting a broken verifier into an infrastructure error."""
    verifier = VERIFIERS.get(case.verifier)
    if verifier is None:
        return VerifierOutcome(False, infra_error=f"unknown verifier {case.verifier!r}")
    if case.verifier_digest and case.verifier_digest != verifier_digest(case.verifier):
        return VerifierOutcome(
            False,
            infra_error=(
                f"the verifier {case.verifier!r} has changed since this case was frozen, so the "
                "result would not be comparable with earlier ones"
            ),
        )
    try:
        return verifier(context)
    except Exception as exc:  # noqa: BLE001 - a broken verifier is not a failed task
        return VerifierOutcome(False, infra_error=f"{type(exc).__name__}: {exc}")


_ = (Any,)
