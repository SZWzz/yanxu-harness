"""The fault-injection child: a real process that dies inside a real batch.

Run as ``python -m yanxu_harness.evals.child <case_id> <directory>``. It builds the same
deployment the parent will recover, starts the run, and exits with ``os._exit(9)`` from inside
the second probe — leaving exactly the state the recovery cases are about: the first probe
settled and committed, the second admitted with its intent open, the third never dispatched.

Why a real process instead of a simulated crash: "the process died between two commits" is the
fact under test. Mutating an in-memory state and calling it a crash would test the test.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from ..runtime import RunRequest
from .cases import script_for, case_by_id
from .fixtures import PROBE_TOOLS, build_deployment, case_context

__all__ = ["main", "run_crash_child"]


def _probe_b_handler(workspace: Path, trace: Path, exit_code: int):
    def handler(context, arguments):  # type: ignore[no-untyped-def]
        with trace.open("a", encoding="utf-8") as handle:
            handle.write(
                f"{context.identity.attempt_id}:probe.b:{arguments['path']}\n"
            )
        os._exit(exit_code)

    return handler


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

    root = Path(directory)
    workspace = root / "workspace"
    state = root / "state"
    state.mkdir(parents=True, exist_ok=True)
    deployment = build_deployment(
        workspace=workspace,
        state=state,
        stream=_crashing_stream(case),
        profile="probe",
        handlers={"probe.b": _probe_b_handler(workspace, state / "trace.log", 9)},
        attempt="a1",
        lease_ttl_seconds=1.0,
    )
    try:
        asyncio.run(
            deployment.harness.start(
                RunRequest(prompt=case.instructions, max_turns=case.max_turns),
                case_context("a1"),
            )
        )
    except BaseException:  # noqa: BLE001 - the crash is the point; nothing may be reported
        pass
    os._exit(9)
    return 9  # pragma: no cover - os._exit never returns


def _crashing_stream(case) -> object:  # type: ignore[no-untyped-def]
    from .fixtures import ScriptedStream

    return ScriptedStream(*script_for(case))


def run_crash_child(case_id: str, directory: Path, *, timeout: float = 120.0) -> tuple[int, str]:
    """Run the child for real and return its (exit code, stderr tail)."""
    import subprocess

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
    return completed.returncode, (completed.stderr or "")[-2000:]


_ = PROBE_TOOLS

if __name__ == "__main__":  # pragma: no cover - exercised through run_crash_child
    raise SystemExit(main())
