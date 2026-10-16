"""D2–D3: the read-only report, its pagination, its boundaries and its output.

The tests here are about what a *reader* may and may not do, and about the three ways a
report can lie without a single wrong field in it:

* **under-reporting** — stopping at a page that happened to belong to another run, or
  presenting a partial total as a total;
* **over-claiming** — turning "I could not read the ledger" into "there is no operation",
  or an unmeasured stage into a zero;
* **leaking** — publishing a field nobody vetted, or rendering a record's text as markup.

The stores are real (the same SQLite files a run writes), opened read-only, and the run's
record is compared byte for byte before and after to show the reader changed nothing.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from durable_edit import (
    DeploymentPaths,
    ScriptedStream,
    build,
    final_message,
    run_context,
    tool_call_message,
)

from yanxu_harness.diagnostics import (
    RunInspector,
    SourceKind,
    project_report,
    to_json,
    to_markdown,
)
from yanxu_harness.diagnostics.reader import DiagnosticFacts
from yanxu_harness.errors import ContractError
from yanxu_harness.runtime import RunIdentity, RunRequest
from yanxu_harness.storage import SqliteExecutionBackend, SqlitePersistence

RUN_ID = "r1"
NAMESPACE = "project-a"
SESSION = "s1"


def run(coro: Any, *, timeout: float = 60.0) -> Any:
    async def guarded() -> Any:
        return await asyncio.wait_for(coro, timeout=timeout)

    return asyncio.run(guarded())


def identity(attempt: str = "reader") -> RunIdentity:
    return RunIdentity(
        namespace=NAMESPACE, session_id=SESSION, run_id=RUN_ID, attempt_id=attempt
    )


def inspector(paths: DeploymentPaths, *, ledger: bool = True, **kwargs: Any) -> RunInspector:
    return RunInspector(
        persistence=SqlitePersistence(paths.runs, read_only=True),
        ledger=SqliteExecutionBackend(paths.operations, read_only=True) if ledger else None,
        **kwargs,
    )


def completed_run(tmp_path: Path, *, name: str = "state") -> DeploymentPaths:
    """A real run that dispatched one tool call and finished."""
    paths = DeploymentPaths.under(tmp_path / name)
    harness, _gateway = build(
        paths,
        ScriptedStream(tool_call_message("hello"), final_message("done")),
        attempt="a1",
        auto_write=True,
    )
    outcome = run(harness.start(RunRequest(prompt="write it"), run_context("a1")))
    assert outcome.status.value == "completed"
    return paths


# --- the report ------------------------------------------------------------------------


def test_a_completed_run_is_explained_from_its_own_records(tmp_path: Path) -> None:
    paths = completed_run(tmp_path)
    report = run(inspector(paths).inspect_run(identity("a9")))

    assert report.run_id == RUN_ID
    assert report.status == "completed"
    assert report.observer_attempt == "a9"
    assert "a1" in report.origin_attempts, "who ran it is not who read it"
    assert report.origin_attempts[0] == "a1"
    assert len(report.calls) == 1
    call = report.calls[0]
    assert (call.call_id, call.tool_name, call.outcome) == ("call-1", "notes.write", "settled")
    assert call.receipt is not None and call.receipt.source is SourceKind.RECEIPT
    assert call.result_available is True
    assert call.origin_attempt == "a1"

    kinds = [entry.kind for entry in report.trace]
    assert "run_started" in kinds and "run_finished" in kinds
    assert "explanation:outcome" in kinds, "the projection's own conclusion is labelled as one"
    explained = next(entry for entry in report.trace if entry.kind == "explanation:outcome")
    assert explained.source is SourceKind.DERIVED

    # Two real requests, two spans, each with a measured monotonic duration.
    requests = [span for span in report.spans if span.stage == "model:request"]
    assert len(requests) == 2
    assert all(span.duration_ms is not None and span.clock == "monotonic" for span in requests)
    tool_spans = [span for span in report.spans if span.stage.startswith("tool:notes.write")]
    assert len(tool_spans) == 1 and tool_spans[0].duration_ms is not None
    batch = [span for span in report.spans if span.stage == "tool:batch"]
    assert len(batch) == 1
    assert batch[0].duration_ms is None and batch[0].clock == "wall", (
        "a wall-clock span across records is not a measured duration"
    )

    assert report.boundary.run_revision is not None
    assert report.boundary.next_cursor is not None
    counts = report.source_counts()
    assert counts["receipt"] >= 1
    assert counts["derived"] >= 1
    # The scripted stream reports no usage: that is unknown, and it is said, not zeroed.
    assert report.usage_coverage(unit="tokens")["unknown"] >= 1
    assert report.usage_coverage(unit="tokens")["complete"] is False


def test_the_reader_changes_nothing(tmp_path: Path) -> None:
    """Inspecting a run must not advance it, write it, or run any of its effects."""
    paths = completed_run(tmp_path)
    before = _store_bytes(paths)
    ledger_before = _ledger_rows(paths)
    counter_before = paths.counter.read_text(encoding="utf-8") if paths.counter.exists() else ""

    report = run(inspector(paths).inspect_run(identity("a2")))
    again = run(inspector(paths).inspect_run(identity("a2")))

    after = _store_bytes(paths)
    assert (after.runs, after.events) == (before.runs, before.events)
    assert _ledger_rows(paths) == ledger_before
    counter = paths.counter.read_text(encoding="utf-8") if paths.counter.exists() else ""
    assert counter == counter_before, "no tool ran while reading"
    assert to_json(report) == to_json(again), "the same facts and boundary give the same report"


def test_the_ledger_is_read_through_a_read_only_connection(tmp_path: Path) -> None:
    paths = completed_run(tmp_path)
    ledger = SqliteExecutionBackend(paths.operations, read_only=True)
    try:
        assert ledger.read_only is True
        assert ledger.all_operations(), "reads work"
        with pytest.raises(ContractError, match="read-only"):
            run(ledger.promote(_receipt(paths), evidence={"source": "test"}))
        with pytest.raises(ContractError, match="read-only"):
            run(ledger.settle(_receipt(paths)))
    finally:
        ledger.close()

    missing = tmp_path / "nope.db"
    with pytest.raises(ContractError, match="does not create"):
        SqliteExecutionBackend(missing, read_only=True)
    assert not missing.exists(), "a reader must not create the database it was asked for"
    store = tmp_path / "noruns.db"
    with pytest.raises(ContractError, match="does not create"):
        SqlitePersistence(store, read_only=True)
    assert not store.exists()


def test_a_run_is_not_readable_under_another_tenants_identity(tmp_path: Path) -> None:
    paths = completed_run(tmp_path)
    with pytest.raises(ContractError, match="not readable|refusing to read"):
        run(
            inspector(paths).inspect_run(
                RunIdentity(
                    namespace="project-b",
                    session_id=SESSION,
                    run_id=RUN_ID,
                    attempt_id="reader",
                )
            )
        )
    with pytest.raises(ContractError, match="refusing to read"):
        run(
            inspector(paths).inspect_run(
                RunIdentity(
                    namespace=NAMESPACE,
                    session_id="other-session",
                    run_id=RUN_ID,
                    attempt_id="reader",
                )
            )
        )


def test_an_unreadable_ledger_is_unsupported_not_absent(tmp_path: Path) -> None:
    """No ledger capability means "I could not ask", which is not "there is no operation"."""
    paths = completed_run(tmp_path)
    report = run(inspector(paths, ledger=False).inspect_run(identity()))
    call = report.calls[0]
    assert call.receipt is not None
    assert call.receipt.source is SourceKind.UNSUPPORTED
    assert any("ledger" in note for note in report.unsupported)
    # ...and the parts the run store itself knows are still there.
    assert report.trace and report.status == "completed"


def test_a_suspended_run_shows_what_it_waits_for(tmp_path: Path) -> None:
    paths = DeploymentPaths.under(tmp_path / "state")
    harness, _gateway = build(paths, ScriptedStream(tool_call_message("hello")), attempt="a1")
    paused = run(harness.start(RunRequest(prompt="write it", max_turns=4), run_context("a1")))
    assert paused.status.value == "suspended"

    report = run(inspector(paths).inspect_run(identity("a2")))
    assert report.status == "suspended"
    assert report.waiting and report.waiting.get("kind") == "approval"
    assert report.calls[0].outcome == "awaiting"
    assert report.calls[0].progress == "observed", (
        "the driver *did* observe this call: it observed that it is waiting"
    )
    assert report.calls[0].result_available is False
    # The dispatch that decided to wait *was* measured: the span exists and says when, so
    # this report has nothing absent to report for that stage. What it must not do is claim
    # the call ran — the outcome is what distinguishes the two.
    assert [span.stage for span in report.spans if span.duration_ms is not None]
    assert not any(span.interrupted for span in report.spans)
    kinds = [entry.kind for entry in report.trace]
    assert "approval_requested" in kinds
    # An old-style event (no attempt in its payload) must not borrow the reader's attempt.
    approval = next(entry for entry in report.trace if entry.kind == "approval_requested")
    assert approval.attempt == "" and approval.attempt_source == "unknown"


# --- pagination ------------------------------------------------------------------------


def test_a_page_without_this_runs_events_still_advances(tmp_path: Path) -> None:
    """Other runs' events must not stop the scan or be counted for this run."""
    first = completed_run(tmp_path, name="a")
    second = DeploymentPaths.under(tmp_path / "b")
    harness, _gateway = build(
        paths=second,
        stream=ScriptedStream(tool_call_message("other"), final_message("other done")),
        attempt="a1",
        auto_write=True,
    )
    from yanxu_harness.runtime import Principal, RunContext

    other_context = RunContext(
        identity=RunIdentity(
            namespace=NAMESPACE, session_id=SESSION, run_id="other", attempt_id="a1"
        ),
        principal=Principal(subject="author"),
    )
    run(harness.start(RunRequest(prompt="write other"), other_context))

    # A store that does not hold this run says so, rather than reporting an empty run.
    stranger = RunInspector(
        persistence=SqlitePersistence(second.runs, read_only=True),
        ledger=None,
        event_limit=1,
        max_scan_pages=50,
    )
    with pytest.raises(ContractError, match="no run"):
        run(stranger.inspect_run(identity()))

    # The run the store does have, read with a one-event page: the scan must continue past
    # the pages that do not match rather than stopping at the first.
    other_identity = RunIdentity(
        namespace=NAMESPACE, session_id=SESSION, run_id="other", attempt_id="reader"
    )
    report = run(stranger.inspect_run(other_identity))
    assert len(report.trace) >= 3, "the scan crossed pages to collect this run's events"
    assert report.boundary.scanned_pages >= 1

    # A larger page size gives the same *facts*: pagination changes how the store was read,
    # which the boundary states, not what the run did.
    wide = RunInspector(
        persistence=SqlitePersistence(second.runs, read_only=True), ledger=None, event_limit=50
    )
    wider = run(wide.inspect_run(other_identity))
    assert [entry.to_record() for entry in wider.trace] == [
        entry.to_record() for entry in report.trace
    ]
    assert [call.to_record() for call in wider.calls] == [
        call.to_record() for call in report.calls
    ]
    assert wider.boundary.scanned_pages < report.boundary.scanned_pages, (
        "the boundary is where the two reads differ, and it says so"
    )
    assert first.runs.exists()


def test_reading_the_same_cursor_twice_does_not_double_count(tmp_path: Path) -> None:
    paths = completed_run(tmp_path)
    inspection = inspector(paths)
    one = run(inspection.inspect_run(identity(), limit=2))
    two = run(inspection.inspect_run(identity(), limit=2))
    assert len(one.trace) == len(two.trace)
    assert one.boundary.next_cursor == two.boundary.next_cursor


def test_the_scan_bound_is_reported_not_hidden(tmp_path: Path) -> None:
    paths = completed_run(tmp_path)
    bounded = RunInspector(
        persistence=SqlitePersistence(paths.runs, read_only=True),
        ledger=None,
        event_limit=1,
        max_scan_pages=1,
    )
    report = run(bounded.inspect_run(identity()))
    assert report.boundary.scan_limit_hit is True
    assert report.boundary.next_cursor is not None
    assert any("scan stopped" in note for note in report.integrity)


# --- usage and measurement -------------------------------------------------------------


def test_a_reported_usage_is_actual_and_kept_per_request_instance(tmp_path: Path) -> None:
    paths = DeploymentPaths.under(tmp_path / "state")
    harness, _gateway = build(
        paths, ScriptedStream(tool_call_message("hello"), final_message("done")), attempt="a1", auto_write=True
    )
    run(harness.start(RunRequest(prompt="write it"), run_context("a1")))

    # Rewrite the run's stored events the way the driver would have written them with a
    # provider that reports usage: two real requests, one figure each.
    store = SqlitePersistence(paths.runs)
    try:
        events = run(store.read_events())
        for item in events:
            if item.kind != "model_request_completed":
                continue
            payload = dict(item.payload)
            payload["usage"] = {"input": 11, "output": 7, "total_tokens": 18}
            store._connection.execute(  # noqa: SLF001 - the test writes the fact it needs
                "UPDATE events SET payload = ? WHERE sequence = ?",
                (json.dumps(payload), item.sequence),
            )
    finally:
        store.close()

    report = run(inspector(paths).inspect_run(identity()))
    assert report.known_usage(unit="input_tokens") == 22.0, "two requests, two figures"
    assert report.known_usage(unit="output_tokens") == 14.0
    coverage = report.usage_coverage(unit="input_tokens")
    assert coverage["actual"] == 2 and coverage["complete"] is True
    # Input and output are not the same thing and are never added together.
    assert report.known_usage(unit="input_tokens") != report.known_usage(unit="output_tokens")


def test_an_interrupted_request_reports_an_incomplete_span(tmp_path: Path) -> None:
    paths = DeploymentPaths.under(tmp_path / "state")
    entered = asyncio.Event()

    class Blocking:
        async def __call__(self, model: Any, context: Any, options: Any = None) -> Any:
            entered.set()
            return self

        def __aiter__(self) -> Any:
            return self

        async def __anext__(self) -> Any:
            raise StopAsyncIteration

        async def result(self) -> Any:
            await asyncio.sleep(3600)
            return final_message("never")

    harness, _gateway = build(paths, Blocking(), attempt="a1", auto_write=True)

    async def scenario() -> Any:
        started = asyncio.ensure_future(
            harness.start(RunRequest(prompt="write it"), run_context("a1"))
        )
        await asyncio.wait_for(entered.wait(), timeout=10.0)
        await harness.cancel(RUN_ID, "operator stopped it")
        return await started

    assert run(scenario()).status.value == "cancelled"

    report = run(inspector(paths).inspect_run(identity()))
    spans = [span for span in report.spans if span.stage == "model:request"]
    assert len(spans) == 1
    assert spans[0].duration_ms is None
    assert spans[0].incomplete is True
    assert "no response was recorded" in spans[0].note
    assert report.status == "cancelled"


# --- output and the public boundary ----------------------------------------------------


def test_detail_text_is_escaped_in_the_markdown_view() -> None:
    """A record's free text must not be able to inject markup into the rendered report."""
    from yanxu_harness.diagnostics import RunDiagnosticReport

    hostile = "| <script>alert('x')</script> `boom` *x* [link](http://evil.example/?k=secret)\nnext"
    report = RunDiagnosticReport(
        namespace=NAMESPACE,
        session_id=SESSION,
        run_id=RUN_ID,
        detail=hostile,
        calls=(),
    )
    text = to_markdown(report)
    assert "<script>" not in text
    assert "&lt;script&gt;" in text
    assert "\\|" in text, "a pipe cannot split a table row"
    assert "\nnext" not in text, "a newline cannot escape its cell"


def test_a_stage_with_no_record_is_missing_not_instant() -> None:
    """A fact that should exist and does not is stated as missing, never as a zero.

    Two real shapes: a preparation whose timing this build never records, and an older run
    whose response was recorded but whose dispatch was not. Both must come out as a span with
    no start/end and a reason — a duration for either would be an invention.
    """
    facts = DiagnosticFacts(
        namespace=NAMESPACE,
        session_id=SESSION,
        run_id=RUN_ID,
        record={
            "run_id": RUN_ID,
            "namespace": NAMESPACE,
            "session_id": SESSION,
            "status": "completed",
            "context_state": {"context_schema_version": 1, "scope": "s1", "source_head": "h1"},
            "usage": {"total_tokens": None},
        },
        events=(
            {
                "sequence": 4,
                "kind": "model_request_completed",
                "run_id": RUN_ID,
                "at": 2.0,
                "payload": {"request_instance": "a1:request:1#1", "duration_ms": 5.0},
            },
        ),
    )
    report = project_report(facts)
    absent = [span for span in report.spans if span.absent]
    stages = {span.stage for span in absent}
    assert "context:prepare" in stages
    preparation = next(span for span in absent if span.stage == "context:prepare")
    assert "no duration can be stated" in preparation.note
    request = next(span for span in report.spans if span.stage == "model:request")
    assert request.started is None and request.duration_ms is None, (
        "the response exists but its dispatch does not, so there is no duration to state"
    )
    assert "dispatch was not" in request.note
    assert report.usage_coverage(unit="tokens")["complete"] is False


def test_the_json_view_is_valid_and_stable() -> None:
    facts = DiagnosticFacts(
        namespace=NAMESPACE,
        session_id=SESSION,
        run_id=RUN_ID,
        observer_attempt="a2",
        record={"run_id": RUN_ID, "namespace": NAMESPACE, "session_id": SESSION, "status": "completed"},
    )
    report = project_report(facts)
    first = to_json(report)
    second = to_json(project_report(facts))
    assert first == second
    parsed = json.loads(first)
    assert parsed["identity"]["run_id"] == RUN_ID
    assert parsed["schema_version"] == report.schema_version
    assert "NaN" not in first and "Infinity" not in first


def test_a_report_only_publishes_whitelisted_fields() -> None:
    """A committed fact with a field nobody vetted is summarised, not exported."""
    facts = DiagnosticFacts(
        namespace=NAMESPACE,
        session_id=SESSION,
        run_id=RUN_ID,
        record={"run_id": RUN_ID, "namespace": NAMESPACE, "session_id": SESSION, "status": "completed"},
        events=(
            {
                "sequence": 3,
                "kind": "tool_call_started",
                "run_id": RUN_ID,
                "at": 1.0,
                "payload": {
                    "call_id": "call-1",
                    "tool_name": "notes.write",
                    "arguments": {"path": "a.md", "text": "a secret"},
                    "prompt": "the whole system prompt",
                    "authorization": "Bearer sk-live-123",
                },
            },
        ),
    )
    report = project_report(facts)
    entry = next(item for item in report.trace if item.kind == "tool_call_started")
    assert entry.summary["call_id"] == "call-1"
    assert "arguments" not in entry.summary
    assert "prompt" not in entry.summary
    assert "authorization" not in entry.summary
    assert entry.summary["omitted_fields"] == 3
    text = to_json(report) + to_markdown(report)
    assert "a secret" not in text and "sk-live-123" not in text


def test_nested_private_values_in_a_usage_like_mapping_are_not_published() -> None:
    facts = DiagnosticFacts(
        namespace=NAMESPACE,
        session_id=SESSION,
        run_id=RUN_ID,
        record={"run_id": RUN_ID, "namespace": NAMESPACE, "session_id": SESSION, "status": "completed"},
        events=(
            {
                "sequence": 1,
                "kind": "model_request_completed",
                "run_id": RUN_ID,
                "payload": {
                    "request_instance": "a1:request:1#1",
                    "usage": {"input": 3, "headers": {"authorization": "Bearer sk-live-1"}},
                },
            },
        ),
    )
    text = to_json(project_report(facts))
    assert "sk-live-1" not in text, "a nested mapping must not be published wholesale"


# --- the command line ------------------------------------------------------------------


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "yanxu_harness.diagnostics", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        cwd=Path(__file__).resolve().parents[1],
    )


def test_the_cli_reports_json_and_markdown_and_refuses_a_missing_store(tmp_path: Path) -> None:
    paths = completed_run(tmp_path)
    common = (
        "--runs",
        str(paths.runs),
        "--operations",
        str(paths.operations),
        "--namespace",
        NAMESPACE,
        "--session",
        SESSION,
        "--run-id",
        RUN_ID,
    )
    as_json = _cli("inspect", *common)
    assert as_json.returncode == 0, as_json.stderr
    report = json.loads(as_json.stdout)
    assert report["identity"]["run_id"] == RUN_ID

    as_markdown = _cli("inspect", *common, "--format", "markdown")
    assert as_markdown.returncode == 0, as_markdown.stderr
    assert as_markdown.stdout.startswith(f"# Run `{RUN_ID}`")
    assert "## Sources" in as_markdown.stdout

    # Without the ledger the report still describes the run and says what it could not read.
    without = _cli("inspect", *(arg for arg in common if arg not in ("--operations", str(paths.operations))))
    assert without.returncode == 0
    assert "unsupported" in json.loads(without.stdout)

    missing = tmp_path / "absent.db"
    refused = _cli(
        "inspect", "--runs", str(missing), "--namespace", NAMESPACE, "--session", SESSION,
        "--run-id", RUN_ID,
    )
    assert refused.returncode == 2
    assert "refusing to create" in refused.stderr
    assert not missing.exists()

    to_file = tmp_path / "report.json"
    written = _cli("inspect", *common, "-o", str(to_file))
    assert written.returncode == 0
    assert json.loads(to_file.read_text(encoding="utf-8"))["identity"]["run_id"] == RUN_ID


def test_the_cli_help_documents_every_flag() -> None:
    completed = _cli("inspect", "--help")
    assert completed.returncode == 0
    for flag in ("--runs", "--operations", "--namespace", "--session", "--run-id", "--format", "--limit"):
        assert flag in completed.stdout


# --- helpers ---------------------------------------------------------------------------


class _StoreBytes:
    def __init__(self, runs: bytes, events: bytes) -> None:
        self.runs = runs
        self.events = events


def _store_bytes(paths: DeploymentPaths) -> _StoreBytes:
    """The run store's bytes and the event rows, as a *second* connection sees them."""
    store = SqlitePersistence(paths.runs)
    try:
        rows = store._connection.execute(  # noqa: SLF001 - a byte-level before/after check
            "SELECT sequence, kind, run_id, payload FROM events ORDER BY sequence"
        ).fetchall()
        state = store._connection.execute(  # noqa: SLF001
            "SELECT version, state FROM runs ORDER BY run_id"
        ).fetchall()
    finally:
        store.close()
    return _StoreBytes(repr(state).encode(), repr(rows).encode())


def _ledger_rows(paths: DeploymentPaths) -> Any:
    ledger = SqliteExecutionBackend(paths.operations)
    try:
        return repr(
            ledger._connection.execute(  # noqa: SLF001 - the durable rows, compared as they are
                "SELECT operation_id, state, settled_at FROM operations ORDER BY operation_id"
            ).fetchall()
        )
    finally:
        ledger.close()


def _receipt(paths: DeploymentPaths) -> Any:
    from yanxu_harness.tools import EffectState, ToolReceipt

    ledger = SqliteExecutionBackend(paths.operations)
    try:
        row = ledger.all_operations()[0]
    finally:
        ledger.close()
    return ToolReceipt(
        operation_id=str(row["operation_id"]), state=EffectState.SUCCEEDED, result={"ok": True}
    )


# --- the real partial-recovery sample --------------------------------------------------


def test_a_partial_recovery_sample_is_explained_across_attempts(tmp_path: Path) -> None:
    """The sample the delivery report cites: a real crashed batch, read after a recovery.

    It has to contain all four things the plan asks a sample to contain — cross-attempt
    attribution, a receipt's source, the reason the run is waiting, and a measurement that is
    *unknown* rather than zero — so the test asserts each one on a real recorded run.
    """
    import test_runtime_partial_recovery as fixture

    paths = fixture.crash_in_a_partial_batch(tmp_path / "state")
    first, _gateway = fixture.rebuild(paths, ScriptedStream(final_message("done")), attempt="a2")
    paused = fixture.run(first.resume(RUN_ID, {}, run_context("a2")))
    assert paused.status.value == "suspended"

    report = run(inspector(paths).inspect_run(identity("a3")))
    # 1. Cross-attempt: the attempt that ran the calls and the attempt that recovered.
    assert set(report.origin_attempts) >= {"a1", "a2"}, report.origin_attempts
    assert report.observer_attempt == "a3"
    # 2. Receipts: A is proven by the ledger, B is an open intent the ledger cannot decide.
    by_call = {call.call_id: call for call in report.calls}
    assert by_call[fixture.A].receipt is not None
    assert by_call[fixture.A].receipt.source is SourceKind.RECEIPT
    assert "succeeded" in by_call[fixture.A].receipt.detail
    assert by_call[fixture.B].outcome == "uncertain"
    assert by_call[fixture.B].receipt is not None
    assert by_call[fixture.B].receipt.source is SourceKind.RECEIPT
    assert "running" in by_call[fixture.B].receipt.detail
    assert by_call[fixture.C].outcome in {"unstarted", "declared"}
    # 3. Why it is waiting, in the run's own words.
    assert report.waiting and report.waiting.get("kind") == "reconciliation"
    assert report.waiting.get("call_id") == fixture.B
    # 4. What is not known is unknown: no usage was measured, and the report says so.
    assert report.usage_coverage(unit="tokens")["complete"] is False
    assert report.usage_coverage(unit="tokens")["unknown_reasons"]
    # The timeline and the spans explain the same run from its own events.
    assert any(span.stage == "model:request" for span in report.spans)
    assert report.boundary.run_revision is not None
    # And the output is the same report in both formats.
    assert json.loads(to_json(report))["status"] == "suspended"
    markdown = to_markdown(report)
    assert "## Unsupported here" in markdown, (
        "the report says what it could not read (here: the in-process counters)"
    )
    assert "not durable" in markdown


# --- secrets inside ordinary text ------------------------------------------------------


def test_credentials_and_host_paths_inside_plain_text_are_stripped() -> None:
    """A blacklist of key *names* is not enough: the secret is usually a value.

    The plan is explicit about this, so the report bounds free text and strips the shapes that
    carry credentials or host layout — in payload values, in a run's own `detail`, and in a
    nested usage-like mapping — while leaving an ordinary refusal reason readable.
    """
    from yanxu_harness.diagnostics.render import REDACTED, sanitise_text

    # The literal values are assembled here on purpose: a repository that contains
    # credential-shaped strings is a repository whose own export check refuses to ship — as it
    # should — and a test is no exception.
    openai_key = "sk-" + "live-" + "A1b2C3d4E5f6G7h8"
    bearer = "Bearer " + openai_key
    home = "/" + "Users/" + "someone"
    venv_path = home + "/.venv/lib/python3.12/site-packages/mod.py"
    workspace_path = "/" + "Volumes/etx/coding/private/project"

    assert REDACTED in sanitise_text("Authorization: " + bearer)
    assert openai_key not in sanitise_text(f"GET https://api.example.com/v1?token={openai_key} failed")
    assert "someone" not in sanitise_text("stopped at " + venv_path)
    assert "private" not in sanitise_text("cwd=" + workspace_path)
    assert "hunter2" not in sanitise_text("password=hunter2 rejected")
    assert openai_key not in sanitise_text("key=" + openai_key)
    assert openai_key not in sanitise_text("note=" + openai_key)
    # An ordinary reason survives: a report that redacted everything would be useless.
    assert sanitise_text("target version changed after approval") == (
        "target version changed after approval"
    )
    long_text = sanitise_text("x" * 400)
    assert len(long_text) < 300 and "more characters" in long_text

    facts = DiagnosticFacts(
        namespace=NAMESPACE,
        session_id=SESSION,
        run_id=RUN_ID,
        record={
            "run_id": RUN_ID,
            "namespace": NAMESPACE,
            "session_id": SESSION,
            "status": "suspended",
            "detail": f"refused: cannot resume under https://host/v1?api_key={openai_key}",
        },
        events=(
            {
                "sequence": 2,
                "kind": "error",
                "run_id": RUN_ID,
                "at": 1.0,
                "payload": {
                    "detail": (
                        f"provider error: 401 for url https://api.example.com/v1?token={openai_key} "
                        f"at {home}/checkout/src/module.py"
                    ),
                    "usage": {"input": 3, "note": "key=" + openai_key},
                },
            },
        ),
    )
    report = project_report(facts)
    blob = to_json(report) + to_markdown(report)
    for secret in (openai_key, home):
        assert secret not in blob, secret
    assert "provider error" in blob, "the reason is still readable"
    assert REDACTED in blob and "<path>" in blob
    # ...and the report still explains itself.
    entry = next(item for item in report.trace if item.kind == "error")
    assert "provider error" in json.dumps(entry.summary)
