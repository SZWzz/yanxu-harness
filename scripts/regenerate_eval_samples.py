#!/usr/bin/env python
"""Regenerate the committed evaluation samples under ``docs/samples/evals``.

Two artifacts come out of this, and they are regenerated rather than hand-edited so that a
reviewer can reproduce them from this checkout:

* ``results.json`` / ``summary.md`` — the offline suite run as this build records it, through
  the same code path the CLI uses;
* ``comparison.json`` / ``comparison.md`` — a comparison of *one frozen case* between a run
  whose scripted answer was wrong (write nothing, claim success) and the correct run above. It
  is the sample that shows the comparator keeping a baseline failure a failure and separating
  quality, reliability and cost.

The bad baseline is produced by replacing ``runner.script_for`` for exactly one case, so the
question (case digest, fixture, verifier) is unchanged: only the answer differs. That is the
only honest way to show a before/after pair under the identity rules — a baseline with a
different case digest would now be reported as incomparable, which is a different sample.

Usage (from the repository root, in an environment with the package installed):

    python scripts/regenerate_eval_samples.py
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from yanxu_harness.evals import cases, runner  # noqa: E402
from yanxu_harness.evals.__main__ import _environment, render_summary  # noqa: E402
from yanxu_harness.evals.compare import compare_reports  # noqa: E402
from yanxu_harness.evals.fixtures import final_message  # noqa: E402
from yanxu_harness.evals.types import EvalSuite  # noqa: E402

SAMPLES = ROOT / "docs" / "samples" / "evals"
BUILD = ROOT / "build" / "eval-samples"
CASE_ID = "fix-calc-tests"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(f"wrote {path.relative_to(ROOT)}")


def _bad_answer(case):  # noqa: ANN001 - the signature of runner.script_for
    """The wrong answer for one case: claim the work is done, having done nothing."""
    if case.case_id == CASE_ID:
        return (final_message("All done: the tests pass now."),)
    return cases.script_for(case)


def main() -> int:
    if BUILD.exists():
        shutil.rmtree(BUILD)
    result = runner.run_suite(EvalSuite(name=cases.SUITE.name, version=cases.SUITE.version,
                                        cases=cases.SUITE.cases), BUILD / "current")
    payload = dict(result.to_record())
    payload["environment"] = _environment()
    _write(
        SAMPLES / "results.json",
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
    )
    _write(SAMPLES / "summary.md", render_summary(result, payload["environment"]))

    case = cases.case_by_id(CASE_ID)
    original = runner.script_for
    runner.script_for = _bad_answer
    try:
        baseline = runner.run_suite(
            EvalSuite(
                name=cases.SUITE.name,
                version=cases.SUITE.version,
                cases=tuple(item for item in cases.SUITE.cases if item.case_id == CASE_ID),
            ),
            BUILD / "baseline",
        )
    finally:
        runner.script_for = original
    candidate = EvalSuite(
        name=cases.SUITE.name,
        version=cases.SUITE.version,
        cases=(case,),
    )
    current = runner.EvalSuiteResult(
        suite=candidate,
        results=tuple(
            row for row in result.results if row.case_id == CASE_ID
        ),
        root=result.root,
    )
    comparison = compare_reports(baseline.to_record(), current.to_record())
    _write(
        SAMPLES / "comparison.json",
        json.dumps(comparison, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    from yanxu_harness.evals.__main__ import render_comparison

    _write(SAMPLES / "comparison.md", render_comparison(comparison) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
