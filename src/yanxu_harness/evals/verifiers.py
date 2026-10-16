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
    error: str = ""
    protocol: Mapping[str, Any] = field(default_factory=dict)
    workspace_files: Mapping[str, str] = field(default_factory=dict)
    pristine_files: Mapping[str, str] = field(default_factory=dict)
    ledger: Mapping[str, str] = field(default_factory=dict)
    trace: tuple[str, ...] = ()
    model_calls: int = 0
    report: Mapping[str, Any] = field(default_factory=dict)


def _run_tests(context: VerifierContext) -> tuple[bool, str]:
    from .fixtures import run_baseline

    spec = context.case.fixture
    if spec is None:
        return False, "the case has no fixture, so its tests cannot be run"
    completed = run_baseline(spec, context.workspace)
    output = (completed.stdout or "") + (completed.stderr or "")
    return completed.returncode == 0, output[-2000:]


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
    if "ContractError" not in context.error:
        reasons.append(f"the recovery did not refuse: {context.error or context.outcome_status!r}")
    if dimension and dimension not in context.error:
        reasons.append(f"the refusal did not name the {dimension} drift: {context.error!r}")
    if context.model_calls:
        reasons.append("the resuming harness asked the model after the refusal")
    if reasons:
        return VerifierOutcome(False, reasons=reasons, evidence={"ran": ran, "error": context.error})
    return VerifierOutcome(
        True,
        evidence={"refused": dimension, "ran": ran, "error": context.error},
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


VERIFIERS: Mapping[str, Callable[[VerifierContext], VerifierOutcome]] = {
    "fix-calc-tests": _verify_fix_calc_tests,
    "write-once-approved": _verify_write_once_approved,
    "partial-batch-evidence": _verify_partial_batch_evidence,
    "drift-refused": _verify_drift_refused,
    "deadline-stops-recovery": _verify_deadline_stops_recovery,
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
