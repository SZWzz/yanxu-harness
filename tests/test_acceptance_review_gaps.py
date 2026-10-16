"""F0: the four defects the independent acceptance review reproduced, as target tests.

`docs/runtime-diagnostics-evaluation-acceptance-review.zh-CN.md` §2–§5 ran probes against
`745fb3b` and found four P2 defects behind a green suite. Each test below is one probe turned
into the assertion the plan (`docs/durable-input-orchestration-implementation-plan.zh-CN.md`
§4) requires, using the review's own numbering:

* **RDE-D1** — a truncated report handed the caller a cursor pointing at the last event it
  *scanned* rather than the last one it *carried*, so continuing from it skipped 2–5. Seven
  matching events, a limit of two: the pages must concatenate to the whole set, with no gap
  and no duplicate.
* **RDE-E1** — the case digest did not include the acceptance truth, and the fixture digest did
  not include the test command, so a run that changed what "correct" means was still reported
  as comparable with the old one. A changed question must be a changed identity, and a
  comparison across two identities must say so rather than produce a row.
* **RDE-E2** — a baseline with one sample was matched three times against three candidate
  repeats, turning one 10-token observation into "3 cases, 30 tokens". A sample is counted
  once, and a sample that has no partner is reported as unmatched.
* **RDE-E3** — the command runner read the child's whole output into memory and only trimmed it
  after the command finished (262145 characters kept, 2000 published). The limit has to hold
  while the output is being collected, and the result has to say it was cut.

These four are deliberately plain assertions about observable behaviour, so that a fix that
merely changes a field name cannot make them pass.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import test_diagnostics_reader as reader_helpers
from durable_edit import DeploymentPaths

from yanxu_harness.diagnostics import RunInspector
from yanxu_harness.evals import compare_reports
from yanxu_harness.evals.cases import case_by_id
from yanxu_harness.evals.fixtures import fixture_by_name, run_baseline
from yanxu_harness.evals.runner import run_case
from yanxu_harness.evals.types import EvalCaseResult, EvalOutcome, FixtureSpec
from yanxu_harness.storage import SqlitePersistence

#: The bound `run_baseline` must hold while collecting, in bytes of each stream. The exact
#: number is F4's to pick; the test only needs it to be far below what the probe printed.
OUTPUT_BOUND = 64 * 1024


def _run(coro: Any, *, timeout: float = 120.0) -> Any:
    async def guarded() -> Any:
        return await asyncio.wait_for(coro, timeout=timeout)

    return asyncio.run(guarded())


def _all_sequences(paths: DeploymentPaths, run_id: str) -> list[int]:
    """Ground truth: every stored event of the run, read independently of the inspector."""
    store = SqlitePersistence(paths.runs, read_only=True)
    try:
        sequences: list[int] = []
        position = -1
        while True:
            page = _run(store.read_events(after=position, limit=50))
            page = list(page or ())
            if not page:
                break
            for item in page:
                position = max(position, int(getattr(item, "sequence", 0) or 0))
                if str(getattr(item, "run_id", "") or "") == run_id:
                    sequences.append(int(getattr(item, "sequence", 0) or 0))
        return sequences
    finally:
        store.close()


def _read_every_page(
    paths: DeploymentPaths, *, event_limit: int, max_events: int
) -> tuple[list[int], list[Any], int]:
    """Follow `next_cursor` until it stops advancing, the way a caller would."""
    store = SqlitePersistence(paths.runs, read_only=True)
    try:
        inspector = RunInspector(
            persistence=store,
            event_limit=event_limit,
            max_scan_pages=100,
            max_events=max_events,
        )

        async def walk() -> tuple[list[int], list[Any], int]:
            carried: list[int] = []
            boundaries: list[Any] = []
            cursor: int | None = None
            steps = 0
            while True:
                facts = await inspector.facts(reader_helpers.identity(), cursor=cursor)
                carried.extend(int(event["sequence"]) for event in facts.events)
                boundaries.append(facts.boundary)
                steps += 1
                nxt = facts.boundary.next_cursor
                if nxt is None or (cursor is not None and nxt <= cursor):
                    return carried, boundaries, steps
                cursor = nxt
                if steps > 50:  # a cursor that never reaches the end is the defect
                    raise AssertionError("the cursor never reached the end of the run's events")

        return _run(walk())
    finally:
        store.close()


# -- RDE-D1: pagination advances by what was returned ------------------------------


def test_rde_d1_a_truncated_report_does_not_skip_the_events_it_left_out(tmp_path: Path) -> None:
    paths = reader_helpers.completed_run(tmp_path / "pagination")
    expected = _all_sequences(paths, reader_helpers.RUN_ID)
    assert len(expected) == 7, "the probe assumes a completed run with seven matching events"

    carried, boundaries, _steps = _read_every_page(paths, event_limit=2, max_events=2)

    assert carried == expected, (
        "following the report's own cursor must return every matching event exactly once; "
        f"got {carried} for stored {expected}"
    )
    assert boundaries[0].truncated is True
    assert boundaries[0].next_cursor == expected[1], (
        "a truncated page must continue from the last event it carries, not from the last "
        f"event it scanned; got {boundaries[0].next_cursor}"
    )
    assert boundaries[0].high_water >= boundaries[0].next_cursor


# -- RDE-E1: the acceptance truth and the test command are part of the identity -----


def test_rde_e1_changing_the_acceptance_truth_changes_the_case_identity(tmp_path: Path) -> None:
    original = case_by_id("locate-bug")
    changed = replace(
        original,
        expectations={**original.expectations, "line": "DIFFERENT ACCEPTANCE TRUTH"},
    )
    assert original.digest != changed.digest, (
        "the verifier decides pass/fail from `expectations`, so a case that changes them is a "
        "different question and must not keep the same digest"
    )
    if original.fixture is not None:
        assert original.fixture.digest == changed.fixture.digest

    baseline = run_case(original, tmp_path / "original")
    candidate = run_case(changed, tmp_path / "changed")
    compared = compare_reports(
        {"results": [baseline.to_record()]}, {"results": [candidate.to_record()]}
    )
    assert compared["comparable"] == [], (
        "a run with different acceptance truth is not the same question; reporting it as a "
        "comparable row turns a changed standard into a framework regression"
    )
    assert compared["incomparable"], "the refusal must say why, not be silence"


def test_rde_e1_changing_the_test_command_changes_the_fixture_identity() -> None:
    spec = fixture_by_name("calc")
    changed = replace(
        spec, baseline_command=("python", "-c", "raise SystemExit(1)")
    )
    assert spec.digest != changed.digest, (
        "the fixture's own tests are both the precondition and the final check; replacing the "
        "command changes the fixture, so two results must not be compared as the same one"
    )


# -- RDE-E2: one sample is one sample ----------------------------------------------


def _sample(index: int) -> dict[str, Any]:
    return EvalCaseResult(
        case_id="same",
        case_version="1",
        case_digest="same-question",
        attempt="a1",
        outcome=EvalOutcome.PASSED,
        repeat_index=index,
        elapsed_seconds=1.0,
        usage=({"unit": "input_tokens", "quantity": "actual", "amount": 10},),
    ).to_record()


def test_rde_e2_a_baseline_sample_is_counted_once() -> None:
    compared = compare_reports(
        {"results": [_sample(0)]},
        {"results": [_sample(index) for index in range(3)]},
    )

    assert compared["quality"]["before"]["cases"] == 1, (
        "the baseline has one real sample; a missing partner is not a copy of it"
    )
    assert compared["cost"]["before"]["units"]["input_tokens"]["known_subtotal"] == 10.0, (
        "one 10-token observation among the baseline's samples is 10 tokens, not 30"
    )
    assert compared["cost"]["before"]["elapsed_seconds"]["count"] == 1
    assert len(compared["comparable"]) == 1
    assert len(compared["incomparable"]) == 2, (
        "candidate repeats 1 and 2 have no baseline sample; they are unmatched, not a match "
        "against sample 0"
    )


# -- RDE-E3: the output limit holds while collecting -------------------------------


def _spec(command: tuple[str, ...]) -> FixtureSpec:
    return FixtureSpec(
        name="output-probe",
        version="1",
        files={},
        baseline_command=command,
    )


def test_rde_e3_the_command_output_is_bounded_while_it_is_collected(tmp_path: Path) -> None:
    completed = run_baseline(
        _spec((sys.executable, "-c", "print('x' * 262144)")), tmp_path
    )
    kept = len(completed.stdout or "")
    assert kept <= OUTPUT_BOUND, (
        f"the runner kept {kept} characters of a command's stdout; the limit has to apply "
        "while the output is read, not only to what is published afterwards"
    )
    assert getattr(completed, "stdout_truncated", False) is True, (
        "a cut stream must be marked as cut, with what was kept and what was dropped"
    )
    assert (completed.stderr or "") == ""
    assert completed.returncode == 0, "the command succeeded; the cut output is not a failure"


def test_rde_e3_the_kept_amount_does_not_grow_with_the_total(tmp_path: Path) -> None:
    small = run_baseline(_spec((sys.executable, "-c", "print('y' * 8192)")), tmp_path)
    large = run_baseline(_spec((sys.executable, "-c", "print('y' * 1048576)")), tmp_path)
    assert len(large.stdout or "") <= max(len(small.stdout or ""), OUTPUT_BOUND)
    assert large.stdout == small.stdout or getattr(large, "stdout_truncated", False) is True


def test_rde_e3_a_command_that_keeps_writing_past_the_limit_still_ends(tmp_path: Path) -> None:
    # Draining rather than killing: the first version's strategy keeps reading to the end so
    # the child is not left blocked on a full pipe. It must not wait for the whole output.
    completed = run_baseline(
        _spec((sys.executable, "-c", "import sys\nfor _ in range(400):\n    sys.stdout.write('z' * 8192)")),
        tmp_path,
        timeout=60.0,
    )
    assert completed.returncode == 0
    assert len(completed.stdout or "") <= OUTPUT_BOUND
    assert getattr(completed, "stdout_truncated", False) is True


_ = pytest, tempfile


# -- F1: the acceptance cases for advancing by what was returned -------------------
#
# The review's own probe (above) only shows the gap. These are the cases the plan's §4
# acceptance list asks for: small limits, the default bound, several runs interleaved in one
# store, a tail with no matching event, a collection that ends exactly on a page boundary, and
# the parameter validation. The synthetic runs are written through the store's own public
# append API, so the scan really walks one SQLite events table shared by every run.

SYNTHETIC_NAMESPACE = "project-synthetic"
SYNTHETIC_SESSION = "sl"
SYNTHETIC_RUN = "r1"


def _identity(run_id: str = SYNTHETIC_RUN) -> Any:
    from yanxu_harness.runtime import RunIdentity

    return RunIdentity(
        namespace=SYNTHETIC_NAMESPACE,
        session_id=SYNTHETIC_SESSION,
        run_id=run_id,
        attempt_id="reader",
    )


def _synthetic_store(path: Path, order: list[str]) -> Path:
    """One events table holding the runs in ``order``, one event each, interleaved."""
    from yanxu_harness.storage import StoredEvent

    store = SqlitePersistence(path)
    try:
        for run_id in dict.fromkeys(order):
            _run(
                store.save_run(
                    run_id,
                    {
                        "namespace": SYNTHETIC_NAMESPACE,
                        "session_id": SYNTHETIC_SESSION,
                        "status": "completed",
                    },
                    expected_version=None,
                )
            )
        _run(
            store.append_events(
                [
                    StoredEvent(sequence=0, kind="usage_reported", payload={}, run_id=run_id)
                    for run_id in order
                ]
            )
        )
    finally:
        store.close()
    return path


def _carried(path: Path, *, event_limit: int, max_events: int, max_scan_pages: int = 100,
             run_id: str = SYNTHETIC_RUN) -> tuple[list[int], list[Any]]:
    store = SqlitePersistence(path, read_only=True)
    try:
        inspector = RunInspector(
            persistence=store,
            event_limit=event_limit,
            max_scan_pages=max_scan_pages,
            max_events=max_events,
        )

        async def walk() -> tuple[list[int], list[Any]]:
            carried: list[int] = []
            boundaries: list[Any] = []
            cursor: int | None = None
            while True:
                facts = await inspector.facts(_identity(run_id), cursor=cursor)
                carried.extend(int(event["sequence"]) for event in facts.events)
                boundaries.append(facts.boundary)
                nxt = facts.boundary.next_cursor
                if nxt is None or (cursor is not None and nxt <= cursor):
                    return carried, boundaries
                cursor = nxt

        return _run(walk())
    finally:
        store.close()


def test_pagination_concatenates_to_the_independent_read(tmp_path: Path) -> None:
    paths = reader_helpers.completed_run(tmp_path / "state")
    expected = _all_sequences(paths, reader_helpers.RUN_ID)
    for event_limit in (1, 2, 3, 7, 50):
        carried, boundaries, _steps = _read_every_page(
            paths, event_limit=event_limit, max_events=2
        )
        assert carried == expected, f"event_limit={event_limit} lost or repeated events"
        assert boundaries[-1].exhausted is True, (
            f"event_limit={event_limit}: the last page reached the end of the store and must "
            "say so instead of leaving the caller to guess"
        )


def test_a_page_full_of_another_runs_events_still_advances(tmp_path: Path) -> None:
    order = ["r1", "r2", "r2", "r1", "r2", "r1", "r2", "r2", "r1", "r2"]
    path = _synthetic_store(tmp_path / "interleaved.db", order)
    carried, boundaries = _carried(path, event_limit=2, max_events=50)
    assert carried == [0, 3, 5, 8], (
        "a page belonging to another run must not end this run's scan; the run's own events "
        f"are 0, 3, 5, 8, got {carried}"
    )
    assert boundaries[-1].exhausted is True


def test_a_tail_without_matching_events_is_not_an_empty_report(tmp_path: Path) -> None:
    order = ["r1", "r1", "r2", "r2", "r2"]
    path = _synthetic_store(tmp_path / "tail.db", order)
    store = SqlitePersistence(path, read_only=True)
    try:
        inspector = RunInspector(persistence=store, event_limit=2, max_scan_pages=100)
        first = _run(inspector.facts(_identity()))
        assert [int(event["sequence"]) for event in first.events] == [0, 1]
        # The caller continues at the tail of another run's events: the scan must walk past
        # them, find nothing more of this run, and say the store is exhausted.
        second = _run(inspector.facts(_identity(), cursor=first.boundary.next_cursor))
        assert list(second.events) == []
        assert second.boundary.exhausted is True
        assert second.boundary.scan_limit_hit is False
        assert second.boundary.truncated is False
    finally:
        store.close()


def test_the_collection_bound_and_the_page_bound_are_separate(tmp_path: Path) -> None:
    paths = reader_helpers.completed_run(tmp_path / "state")
    expected = _all_sequences(paths, reader_helpers.RUN_ID)
    # A page that ends exactly at the collection limit carries everything and still is not
    # evidence that the store is empty until the scan reads past it.
    carried, boundaries, _steps = _read_every_page(
        paths, event_limit=len(expected), max_events=len(expected)
    )
    assert carried == expected
    assert boundaries[0].truncated is False
    assert boundaries[0].exhausted is True


def test_a_scan_that_hits_its_page_bound_says_so(tmp_path: Path) -> None:
    order = ["r1", "r2", "r1", "r2", "r1", "r2", "r1", "r2"]
    path = _synthetic_store(tmp_path / "scan.db", order)
    store = SqlitePersistence(path, read_only=True)
    try:
        inspector = RunInspector(
            persistence=store, event_limit=2, max_scan_pages=1, max_events=50
        )
        first = _run(inspector.facts(_identity()))
        assert [int(event["sequence"]) for event in first.events] == [0]
        assert first.boundary.scan_limit_hit is True, (
            "stopping at the page bound is not the same statement as reaching the end"
        )
        assert first.boundary.exhausted is False
        assert first.boundary.high_water == 1
        # The caller can continue from where it left off, so the bound is a limit on how much
        # one read does, not a hole in the run's history.
        second = _run(inspector.facts(_identity(), cursor=first.boundary.next_cursor))
        assert [int(event["sequence"]) for event in second.events] == [2]
    finally:
        store.close()


def test_the_default_collection_bound_is_applied_and_continues(tmp_path: Path) -> None:
    order = [SYNTHETIC_RUN] * 2100 + ["r2"]
    path = _synthetic_store(tmp_path / "many.db", order)
    store = SqlitePersistence(path, read_only=True)
    try:
        inspector = RunInspector(persistence=store, event_limit=200, max_scan_pages=100)
        first = _run(inspector.facts(_identity()))
        assert len(first.events) == 2000, "the default collection bound is 2000 events"
        assert first.boundary.truncated is True
        assert first.boundary.exhausted is False
        assert first.boundary.next_cursor == int(first.events[-1]["sequence"])
        rest = _run(inspector.facts(_identity(), cursor=first.boundary.next_cursor))
        assert [int(event["sequence"]) for event in rest.events] == list(range(2000, 2100))
        assert rest.boundary.truncated is False
        assert rest.boundary.exhausted is True
    finally:
        store.close()


def test_rereading_the_same_boundary_gives_the_same_report(tmp_path: Path) -> None:
    order = ["r1", "r2", "r2", "r1", "r1", "r2", "r1"]
    path = _synthetic_store(tmp_path / "stable.db", order)
    store = SqlitePersistence(path, read_only=True)
    try:
        inspector = RunInspector(persistence=store, event_limit=2, max_scan_pages=100, max_events=2)
        first = _run(inspector.facts(_identity()))
        again = _run(inspector.facts(_identity()))
        assert [int(e["sequence"]) for e in first.events] == [int(e["sequence"]) for e in again.events]
        assert first.boundary.to_record() == again.boundary.to_record()
    finally:
        store.close()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"event_limit": 0},
        {"event_limit": -1},
        {"max_scan_pages": 0},
        {"max_events": 0},
    ],
)
def test_an_invalid_inspector_bound_is_refused(tmp_path: Path, kwargs: dict[str, Any]) -> None:
    from yanxu_harness.errors import ContractError

    store = SqlitePersistence(tmp_path / "bounds.db")
    try:
        with pytest.raises(ContractError):
            RunInspector(persistence=store, **kwargs)
    finally:
        store.close()


@pytest.mark.parametrize(
    "kwargs",
    [{"limit": 0}, {"limit": -3}, {"max_scan_pages": 0}, {"cursor": -2}],
)
def test_an_invalid_request_bound_is_refused(tmp_path: Path, kwargs: dict[str, Any]) -> None:
    from yanxu_harness.errors import ContractError

    paths = reader_helpers.completed_run(tmp_path / "state")
    store = SqlitePersistence(paths.runs, read_only=True)
    try:
        inspector = RunInspector(persistence=store)
        with pytest.raises(ContractError):
            _run(inspector.facts(reader_helpers.identity(), **kwargs))
    finally:
        store.close()
