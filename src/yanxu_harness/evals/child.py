"""The fault-injection child: a real process that dies inside a real batch.

Run as ``python -m yanxu_harness.evals.child <case_id> <directory>``. It builds the same
deployment the parent will recover, starts the run, and exits with ``os._exit(9)`` from inside
the second probe — leaving exactly the state the recovery cases are about: the first probe
settled and committed, the second admitted with its intent open, the third never dispatched.

Why a real process instead of a simulated crash: "the process died between two commits" is the
fact under test. Mutating an in-memory state and calling it a crash would test the test.

The exit code is evidence, so only the injection point may use the crash code. A child that
raises before it gets there, or that finishes the run without reaching it, exits with its own
*fixture-failure* code and a sanitised diagnostic (fault, phase, exception type, traceback) on
stderr. The parent refuses to treat either of those as the declared crash: an exit code that
means "some process died here" would otherwise turn any earlier failure into a passing fault
injection, and the recovery case would be measuring a suspend window it never produced.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback
from pathlib import Path

from ..runtime import RunRequest
from .cases import case_by_id, script_for
from .fixtures import (
    FAULT_LEASE_TTL_SECONDS,
    PROBE_TOOLS,
    build_deployment,
    case_context,
)

__all__ = ["main", "run_crash_child"]

#: The exit code of the declared crash. Only :func:`_probe_b_handler` may produce it.
CRASH_EXIT_CODE = 9
#: The child raised (or could not start) *before* reaching the injection point.
FAULT_SETUP_FAILED_EXIT_CODE = 70
#: The run finished without ever entering the injection point. A fault that did not happen
#: is a failed fixture, never a passed one.
FAULT_NOT_TRIGGERED_EXIT_CODE = 71

#: Test-only control of the child's behaviour. Absent or ``crash`` is the real thing; the
#: other two exist so the parent's "the fault did not happen" paths can be exercised against
#: a real process instead of being asserted about a mock.
CHILD_MODE_ENV = "YANXU_EVAL_CHILD_MODE"
_CHILD_MODES = ("crash", "fail-before-fault", "return-without-fault")


class _Phase:
    """Where the child had got to when it stopped, for the diagnostic and for tests."""

    def __init__(self) -> None:
        self.name = "setup"

    def set(self, name: str) -> None:
        self.name = name


def _sanitise(text: str, root: Path) -> str:
    """Strip host paths from a child diagnostic before it reaches a result file."""
    from .verifiers import scrub_command_text

    return scrub_command_text(text, root)


def _diagnostic(phase: str, error: BaseException | None, root: Path) -> dict[str, str]:
    """The fault fixture's own failure, in a form a result file may carry.

    The exception *type* is the fact that matters (``StorageConflict`` and ``ContractError``
    point at different repairs), the phase says where it happened, and the traceback is kept
    because "it failed somewhere" is not diagnosable. Paths are removed with the same scrubber
    the verifiers use, so the record can be shared.
    """
    payload: dict[str, str] = {"fault": "not-reached", "phase": phase}
    if error is None:
        payload["error"] = "the run finished without entering the injection point"
        return payload
    payload["error"] = f"{type(error).__name__}: {_sanitise(str(error), root)}"[:2000]
    payload["traceback"] = _sanitise(
        "".join(traceback.format_exception(type(error), error, error.__traceback__)), root
    )[-4000:]
    return payload


def _report(payload: dict[str, str]) -> None:
    """Write the diagnostic on stderr: stdout stays free of process bookkeeping."""
    print(json.dumps(payload, sort_keys=True), file=sys.stderr, flush=True)


def _probe_b_handler(trace: Path, exit_code: int = CRASH_EXIT_CODE):
    def handler(context, arguments):  # type: ignore[no-untyped-def]
        with trace.open("a", encoding="utf-8") as handle:
            handle.write(
                f"{context.identity.attempt_id}:probe.b:{arguments['path']}\n"
            )
        os._exit(exit_code)

    return handler


def _failing_stream(messages, phase: _Phase):
    """A stream that dies before the batch is declared: the "earlier failure" control.

    It raises the way a real provider failure would — out of the model request, after the
    request itself was committed, so the durable state the parent reads is "stopped inside a
    model request", which is exactly the state the CI evidence showed for a child that never
    reached probe.b.
    """

    _ = messages

    async def call(model, context, options=None):  # type: ignore[no-untyped-def]
        phase.set("model-request")
        raise RuntimeError("the fixture's own stream failed before the batch was declared")

    return call


def _crashing_stream(case) -> object:  # type: ignore[no-untyped-def]
    from .fixtures import ScriptedStream

    return ScriptedStream(*script_for(case))


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        print(json.dumps({"error": "usage: child <case_id> <directory>"}))
        return 2
    case_id, directory = args
    try:
        case = case_by_id(case_id)
    except KeyError:
        print(json.dumps({"error": f"unknown case {case_id!r}"}))
        return 2
    if "partial-batch-then-die" not in case.allowed_faults:
        print(json.dumps({"error": f"case {case_id!r} does not allow a process crash"}))
        return 2
    mode = os.environ.get(CHILD_MODE_ENV, "crash")
    if mode not in _CHILD_MODES:
        print(json.dumps({"error": f"unknown child mode {mode!r}"}))
        return 2

    root = Path(directory)
    workspace = root / "workspace"
    state = root / "state"
    state.mkdir(parents=True, exist_ok=True)
    trace = state / "trace.log"
    phase = _Phase()
    stream = (
        _failing_stream(script_for(case), phase)
        if mode == "fail-before-fault"
        else _crashing_stream(case)
    )
    # ``return-without-fault`` keeps the ordinary probes, so probe.b really runs and the run
    # really finishes: the fault point is simply never entered.
    handlers = {"probe.b": _probe_b_handler(trace)} if mode == "crash" else None
    deployment = None
    try:
        phase.set("deployment")
        deployment = build_deployment(
            workspace=workspace,
            state=state,
            stream=stream,
            profile="probe",
            handlers=handlers,
            attempt="a1",
            # One lease policy for the whole evaluation: the child used to hold a one-second
            # lease, so a commit that took longer than that on a slow filesystem failed its
            # own fence and turned the child's early death into a fake "crash".
            lease_ttl_seconds=FAULT_LEASE_TTL_SECONDS,
        )
        phase.set("run")
        asyncio.run(
            deployment.harness.start(
                RunRequest(prompt=case.instructions, max_turns=case.max_turns),
                case_context("a1"),
            )
        )
    except BaseException as exc:  # noqa: BLE001 - reported as the fixture's failure below
        # Nothing here is the declared fault: the injection point is a probe.b handler that
        # never returns, so reaching this line means the fixture broke first. It exits with
        # its own code and its own diagnostic, and the parent reports an `infra_error`
        # instead of recovering a run whose crash window does not exist.
        _report(_diagnostic(phase.name, exc, root))
        return FAULT_SETUP_FAILED_EXIT_CODE
    else:
        # The run completed, so probe.b ran as an ordinary tool and the fault never
        # happened. A fixture that injects nothing cannot be evidence of a crash.
        _report(_diagnostic("completed", None, root))
        return FAULT_NOT_TRIGGERED_EXIT_CODE
    finally:
        if deployment is not None:
            # Every path except the real crash releases what it owns: a leaked SQLite handle
            # would keep the store locked for the parent that reads it next. The crash path
            # never reaches this line, which is the point of it.
            deployment.close()


def run_crash_child(case_id: str, directory: Path, *, timeout: float = 120.0) -> tuple[int, str]:
    """Run the child for real and return its (exit code, stderr tail).

    The child is a real process with a real timeout: a fixture that hangs is killed and
    reported, not waited on until the CI job gives up. ``stderr`` is the child's own
    diagnostic channel; the tail is enough for the structured payload and for a human.
    """
    import subprocess

    try:
        completed = subprocess.run(
            [sys.executable, "-m", "yanxu_harness.evals.child", case_id, str(directory)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"the fault-injection child did not finish within {timeout:g}s; the fixture hangs "
            "instead of crashing"
        ) from exc
    return completed.returncode, (completed.stderr or "")[-4000:]


_ = (PROBE_TOOLS, FAULT_SETUP_FAILED_EXIT_CODE, FAULT_NOT_TRIGGERED_EXIT_CODE)

if __name__ == "__main__":  # pragma: no cover - exercised through run_crash_child
    raise SystemExit(main())
