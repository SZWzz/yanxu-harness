"""``python -m yanxu_harness.evals`` — run the offline suite, or compare two results.

Offline is the default and the only mode that runs without an explicit opt-in: the scripted
adapter is deterministic, needs no network and no credentials, and measures the harness's
protocol. A real-model run has to be asked for by name, with a model, a repeat count and a
budget — and if no adapter is configured it reports the case as ``not_run`` instead of quietly
falling back to the scripted one.

    python -m yanxu_harness.evals run --out build/evals
    python -m yanxu_harness.evals compare before.json after.json --format markdown
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..versioning import fingerprint
from .cases import SUITE
from .compare import compare_reports
from .runner import EvalSuiteResult, run_suite
from .types import EvalOutcome

__all__ = ["build_parser", "main", "render_comparison", "render_summary"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m yanxu_harness.evals",
        description=(
            "Run the frozen harness-protocol cases offline (default), or compare two result "
            "files. A real-model run must be asked for explicitly, with a model, repeats and a "
            "budget; without a configured adapter those cases are reported as not_run."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the suite offline (or a real-model case explicitly)")
    run.add_argument("--case", action="append", default=None, help="run only this case id (repeatable)")
    run.add_argument("--root", default=None, help="where the per-case directories live")
    run.add_argument("--out", default=None, help="directory for results.json and summary.md")
    run.add_argument("--format", choices=("json", "markdown", "both"), default="both")
    run.add_argument(
        "--real-model",
        default=None,
        help="provider/model id to evaluate with a real adapter (requires --repeats and --budget-seconds)",
    )
    run.add_argument("--repeats", type=int, default=0, help="how many times to repeat each case")
    run.add_argument("--budget-seconds", type=float, default=0.0, help="wall-clock budget per case")
    run.add_argument(
        "--allow-real-model",
        action="store_true",
        help="confirm that this run may contact a provider (never the default)",
    )

    compare = sub.add_parser("compare", help="compare two results.json files")
    compare.add_argument("baseline", help="the earlier results.json")
    compare.add_argument("candidate", help="the later results.json")
    compare.add_argument("--format", choices=("json", "markdown"), default="markdown")
    compare.add_argument("--real-model", action="store_true", help="state the sample caveat")
    compare.add_argument("-o", "--output", default=None, help="write the comparison here")
    return parser


def _environment() -> Mapping[str, Any]:
    """What the run was, so two results can be compared honestly."""
    commit, dirty = _git_state()
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "implementation": platform.python_implementation(),
        "framework_commit": commit,
        "framework_dirty": dirty,
        "suite_digest": SUITE.digest,
        "case_set_version": SUITE.version,
        "offline": True,
    }


def _git_state() -> tuple[str, bool]:
    """The commit and dirtiness, when the code is in a checkout. Never fatal."""
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False, timeout=10
        )
        status = subprocess.run(
            ["git", "status", "--porcelain"], capture_output=True, text=True, check=False, timeout=10
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - no git available
        return ("", False)
    commit = head.stdout.strip() if head.returncode == 0 else ""
    return (commit, bool(status.stdout.strip()) if status.returncode == 0 else False)


def render_summary(
    result: EvalSuiteResult, environment: Mapping[str, Any] | None = None
) -> str:
    """The Markdown summary, derived from the same records the JSON holds."""
    environment = environment or result.to_record().get("environment") or {}
    lines = [f"# Eval suite `{result.suite.name}` v{result.suite.version}", ""]
    lines.append(f"- suite digest: `{result.suite.digest}`")
    lines.append(f"- framework commit: `{environment.get('framework_commit') or 'unknown'}`"
                 f"{' (dirty)' if environment.get('framework_dirty') else ''}")
    lines.append(f"- python: {environment.get('python')} on {environment.get('platform')}")
    lines.append("- mode: **offline** (scripted adapter; measures the harness, not a model)")
    lines.append("")
    lines.append("| case | repeat | outcome | verifier | elapsed | notes |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for item in result.results:
        verifier = item.verifier
        verdict = "passed" if verifier is not None and verifier.passed else (
            verifier.infra_error if verifier is not None and verifier.infra_error else
            ("; ".join(verifier.reasons) if verifier is not None else "")
        )
        lines.append(
            "| "
            + " | ".join(
                _cell(value)
                for value in (
                    item.case_id,
                    item.repeat_index,
                    item.outcome.value,
                    verdict,
                    "" if item.elapsed_seconds is None else f"{item.elapsed_seconds:.2f}s",
                    " ".join(item.notes),
                )
            )
            + " |"
        )
    lines.append("")
    counts = {outcome.value: 0 for outcome in EvalOutcome}
    for item in result.results:
        counts[item.outcome.value] += 1
    lines.append(
        "Outcomes: " + ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))
    )
    lines.append("")
    lines.append(
        "`infra_error` and `invalid_result` are this framework's own failures and are **not** "
        "counted as the task failing."
    )
    lines.append("")
    return "\n".join(lines)


def render_comparison(comparison: Mapping[str, Any]) -> str:
    lines = ["# Eval comparison", ""]
    quality = comparison.get("quality") or {}
    before = quality.get("before") or {}
    after = quality.get("after") or {}
    lines.append(
        f"- task verdicts: before {before.get('task_verdicts')} "
        f"(pass rate {before.get('pass_rate')}), after {after.get('task_verdicts')} "
        f"(pass rate {after.get('pass_rate')})"
    )
    reliability = comparison.get("reliability") or {}
    lines.append(
        "- framework failures: before "
        f"{reliability.get('before_infra_errors')} infra / "
        f"{reliability.get('before_invalid_results')} invalid, after "
        f"{reliability.get('after_infra_errors')} infra / "
        f"{reliability.get('after_invalid_results')} invalid"
    )
    cost = comparison.get("cost") or {}
    for side in ("before", "after"):
        block = (cost.get(side) or {}).get("elapsed_seconds") or {}
        lines.append(
            f"- elapsed ({side}, paired samples only): n={block.get('count')} "
            f"min={block.get('min')} max={block.get('max')} mean={block.get('mean')}"
        )
        for unit, bucket in ((cost.get(side) or {}).get("units") or {}).items():
            lines.append(
                f"  - {unit}: actual={bucket.get('actual')} estimated={bucket.get('estimated')} "
                f"unknown={bucket.get('unknown')} known_subtotal={bucket.get('known_subtotal')} "
                f"complete={bucket.get('complete')}"
            )
    # Each side's own raw samples, and what is missing from them, are stated apart from the
    # paired cost above: a comparison may only claim what it paired, while the distribution has
    # to show everything each side really ran.
    distribution = comparison.get("distribution") or {}
    for side in ("before", "after"):
        block = (distribution.get(side) or {}).get("elapsed_seconds") or {}
        lines.append(
            f"- raw observations ({side}): n={block.get('count')} "
            f"min={block.get('min')} max={block.get('max')} mean={block.get('mean')}"
        )
    samples = comparison.get("samples") or {}
    if samples:
        for side in ("before", "after"):
            counts = samples.get(side) or {}
            lines.append(
                f"- samples ({side}): listed={counts.get('listed')} valid={counts.get('usable')} "
                f"paired={counts.get('matched')} duplicates={counts.get('duplicates')} "
                f"not_run={counts.get('not_run')} "
                f"infra_or_invalid={counts.get('infra_or_invalid')}"
            )
        per_case = samples.get("per_case") or {}
        if per_case:
            lines.append("")
            lines.append("| case | samples before | samples after | not run (b/a) | infra or invalid (b/a) |")
            lines.append("| --- | --- | --- | --- | --- |")
            for case_id, counts in sorted(per_case.items()):
                lines.append(
                    "| "
                    + " | ".join(
                        _cell(value)
                        for value in (
                            case_id,
                            counts.get("samples_before"),
                            counts.get("samples_after"),
                            f"{counts.get('not_run_before')}/{counts.get('not_run_after')}",
                            f"{counts.get('infra_or_invalid_before')}/"
                            f"{counts.get('infra_or_invalid_after')}",
                        )
                    )
                    + " |"
                )
    lines.append("")
    lines.append("| case | repeat | before | after | changed |")
    lines.append("| --- | --- | --- | --- | --- |")
    for row in comparison.get("comparable") or ():
        lines.append(
            "| "
            + " | ".join(
                _cell(value)
                for value in (
                    row.get("case_id"),
                    row.get("repeat_index"),
                    row.get("before"),
                    row.get("after"),
                    "yes" if row.get("changed") else "no",
                )
            )
            + " |"
        )
    incomparable = comparison.get("incomparable") or ()
    if incomparable:
        lines.append("")
        lines.append("## Not comparable")
        lines.append("")
        for item in incomparable:
            lines.append(f"- `{_cell(item.get('case_id'))}`: {_cell(item.get('reason'))}")
    for caveat in comparison.get("caveats") or ():
        lines.append("")
        lines.append(f"> {_cell(caveat)}")
    lines.append("")
    return "\n".join(lines)


_ESCAPE = {"\\": "\\\\", "`": "\\`", "|": "\\|", "<": "&lt;", ">": "&gt;", "\n": " ", "\r": " "}


def _cell(value: Any) -> str:
    return "".join(_ESCAPE.get(character, character) for character in str(value))


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _run(args: argparse.Namespace) -> int:
    if args.real_model and not args.allow_real_model:
        print(
            "refusing to run a real-model evaluation without --allow-real-model: the offline "
            "entry point never contacts a provider implicitly",
            file=sys.stderr,
        )
        return 2
    if args.real_model and (args.repeats <= 0 or args.budget_seconds <= 0):
        print(
            "--real-model requires --repeats and --budget-seconds: a model is not measured by "
            "one unfunded run",
            file=sys.stderr,
        )
        return 2
    if args.real_model:
        print(
            "no real-model adapter is configured in this build, so the cases are reported as "
            "not_run rather than silently falling back to the scripted adapter",
            file=sys.stderr,
        )
    root = Path(args.root) if args.root else Path("build") / "evals" / SUITE.name
    result = run_suite(
        SUITE,
        root,
        case_ids=args.case,
        allow_real_model=bool(args.allow_real_model),
        model_override=args.real_model,
        repeats=args.repeats,
    )
    payload = dict(result.to_record())
    payload["environment"] = _environment()
    if args.real_model:
        payload["environment"]["offline"] = False
        payload["environment"]["requested_model"] = args.real_model
        payload["environment"]["budget_seconds"] = args.budget_seconds
    out = Path(args.out) if args.out else root
    if args.format in {"json", "both"}:
        _write(
            out / "results.json",
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False),
        )
    summary = render_summary(result, payload["environment"])
    if args.format in {"markdown", "both"}:
        _write(out / "summary.md", summary)
    print(summary)
    verdicts = payload["results"]
    failed = [item for item in verdicts if item["outcome"] == "failed"]
    infra = [item for item in verdicts if item["outcome"] in {"infra_error", "invalid_result"}]
    if infra:
        return 3
    return 1 if failed else 0


def _compare(args: argparse.Namespace) -> int:
    baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    candidate = json.loads(Path(args.candidate).read_text(encoding="utf-8"))
    comparison = compare_reports(baseline, candidate, real_model=bool(args.real_model))
    text = (
        json.dumps(comparison, ensure_ascii=False, indent=2, sort_keys=True)
        if args.format == "json"
        else render_comparison(comparison)
    )
    if args.output:
        _write(Path(args.output), text + "\n")
    else:
        print(text)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command == "run":
        return _run(args)
    if args.command == "compare":
        return _compare(args)
    parser.error(f"unknown command {args.command!r}")  # pragma: no cover
    return 2


_ = fingerprint

if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
