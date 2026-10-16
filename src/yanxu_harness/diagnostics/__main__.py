"""``python -m yanxu_harness.diagnostics inspect ...`` — read one run and explain it.

The command is a *reader* and is built as one: it opens the stores read-only, refuses to
create a missing database, and never calls ``start``/``resume``/``cancel`` or a tool. It has
exactly one subcommand on purpose — a command that does not work is worse than no command,
and every flag here is exercised by the tests.

    python -m yanxu_harness.diagnostics inspect \
        --runs ./state/runs.db --operations ./state/operations.db \
        --namespace project-a --session s1 --run-id r1 --format markdown
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any, Sequence

from ..errors import ContractError
from ..runtime import RunIdentity
from ..storage import SqliteExecutionBackend, SqlitePersistence
from .reader import RunInspector
from .render import to_json, to_markdown

__all__ = ["build_parser", "main"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m yanxu_harness.diagnostics",
        description=(
            "Read a recorded run and explain it from the facts about it. Read-only: this "
            "never starts, resumes, cancels or runs anything, and it will not create a store."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    inspect = sub.add_parser("inspect", help="report on one run")
    inspect.add_argument(
        "--runs",
        required=True,
        help="path to the run store (SQLite). Must already exist; the reader will not create it.",
    )
    inspect.add_argument(
        "--operations",
        default=None,
        help=(
            "path to the operation ledger (SQLite), optional. Without it the report still "
            "describes the run and marks every operation reference as unsupported."
        ),
    )
    inspect.add_argument("--namespace", required=True, help="expected namespace of the run")
    inspect.add_argument("--session", required=True, help="expected session id of the run")
    inspect.add_argument("--run-id", required=True, help="the run to report on")
    inspect.add_argument(
        "--attempt",
        default="",
        help=(
            "the attempt reading the report (optional). It is recorded as the observer and is "
            "never merged with the attempts that did the work."
        ),
    )
    inspect.add_argument(
        "--cursor", type=int, default=None, help="resume the event scan from this global cursor"
    )
    inspect.add_argument(
        "--limit", type=int, default=200, help="maximum events to collect in one page (default 200)"
    )
    inspect.add_argument(
        "--max-scan-pages",
        type=int,
        default=20,
        help="maximum store pages to scan while looking for this run's events (default 20)",
    )
    inspect.add_argument(
        "--format",
        choices=("json", "markdown"),
        default="json",
        help="output format: json is the fact format, markdown is a view of the same report",
    )
    inspect.add_argument(
        "-o", "--output", default=None, help="write the report here instead of stdout"
    )
    return parser


def _report(args: argparse.Namespace) -> str:
    runs = Path(args.runs)
    if not runs.exists():
        raise ContractError(
            f"no run store at {runs}; refusing to create one (a reader reads what exists)"
        )
    persistence = SqlitePersistence(runs, read_only=True)
    ledger = None
    try:
        if args.operations:
            operations = Path(args.operations)
            if not operations.exists():
                raise ContractError(
                    f"no operation ledger at {operations}; refusing to create one"
                )
            ledger = SqliteExecutionBackend(operations, read_only=True)
        inspector = RunInspector(persistence=persistence, ledger=ledger)
        identity = RunIdentity(
            namespace=args.namespace,
            session_id=args.session,
            run_id=args.run_id,
            # A run identity always has an attempt; the reader's own is named as such, so it
            # is never confused with the attempts that did the work.
            attempt_id=args.attempt or "reader",
        )

        async def run() -> Any:
            return await inspector.inspect_run(
                identity,
                cursor=args.cursor,
                limit=args.limit,
                max_scan_pages=args.max_scan_pages,
            )

        report = asyncio.run(run())
        return to_markdown(report) if args.format == "markdown" else to_json(report)
    finally:
        if ledger is not None:
            ledger.close()
        persistence.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command != "inspect":  # pragma: no cover - argparse enforces the choice
        parser.error(f"unknown command {args.command!r}")
    try:
        text = _report(args)
    except ContractError as refusal:
        print(f"error: {refusal}", file=sys.stderr)
        return 2
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
