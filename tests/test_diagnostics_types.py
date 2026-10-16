"""D0: the diagnostic report contract, as pure objects.

These tests pin the rules that make a report trustworthy rather than merely well-formed.
Each rule exists because the opposite behaviour is a real way to mislead a reader:

* an unknown measurement is ``unknown`` and carries **no** amount (a zero is a claim);
* a duration needs two recorded endpoints and a monotonic clock (one endpoint gives an
  age, not a duration; a monotonic value subtracted across processes is not a duration at
  all);
* an entry whose attempt was never recorded says so and must not borrow the reading
  process's attempt;
* a report states the boundary it was read from, and refuses a schema version this build
  cannot interpret.

Nothing here reads a store: D0 is the shape, D1–D2 fill it in.
"""

from __future__ import annotations

import pytest

from yanxu_harness.diagnostics import (
    CallDiagnostic,
    DIAGNOSTIC_SCHEMA_VERSION,
    Evidence,
    ReadBoundary,
    RunDiagnosticReport,
    SourceKind,
    SpanObservation,
    TraceEntry,
    UsageObservation,
    UsageQuantity,
    count_sources,
)
from yanxu_harness.errors import ContractError, VersionRefused


def _report(**changes: object) -> RunDiagnosticReport:
    base: dict[str, object] = {
        "namespace": "project-a",
        "session_id": "s1",
        "run_id": "r1",
    }
    base.update(changes)
    return RunDiagnosticReport(**base)  # type: ignore[arg-type]


# --- usage: an unknown is not a zero ---------------------------------------------------


def test_a_missing_usage_is_unknown_and_carries_no_amount() -> None:
    observation = UsageObservation(
        unit="tokens",
        quantity=UsageQuantity.UNKNOWN,
        source=SourceKind.MISSING,
        note="the provider did not report usage for this request",
    )
    assert observation.amount is None
    assert observation.known is False

    with pytest.raises(ContractError, match="carries no amount"):
        UsageObservation(unit="tokens", quantity=UsageQuantity.UNKNOWN, amount=0.0, note="x")
    with pytest.raises(ContractError, match="must say why it is unknown"):
        UsageObservation(unit="tokens", quantity=UsageQuantity.UNKNOWN)


def test_a_measurement_must_name_its_unit() -> None:
    with pytest.raises(ContractError, match="must name its unit"):
        UsageObservation(unit="", quantity=UsageQuantity.ACTUAL, amount=3.0)
    with pytest.raises(ContractError, match="must name its unit"):
        UsageObservation(unit="things", quantity=UsageQuantity.ACTUAL, amount=3.0)


def test_an_estimate_must_name_its_estimator() -> None:
    with pytest.raises(ContractError, match="must name its estimator"):
        UsageObservation(unit="tokens", quantity=UsageQuantity.ESTIMATED, amount=120.0)
    UsageObservation(
        unit="tokens",
        quantity=UsageQuantity.ESTIMATED,
        amount=120.0,
        estimator="chars/4 v1",
        source=SourceKind.DERIVED,
    )


def test_actual_and_estimated_need_an_amount() -> None:
    with pytest.raises(ContractError, match="needs a non-negative amount"):
        UsageObservation(unit="tokens", quantity=UsageQuantity.ACTUAL)
    with pytest.raises(ContractError, match="needs a non-negative amount"):
        UsageObservation(unit="tokens", quantity=UsageQuantity.ACTUAL, amount=-1.0)


def test_the_same_request_instance_is_not_counted_twice() -> None:
    """Streaming reports cumulative usage and a final figure for one real request."""
    report = _report(
        usage=(
            UsageObservation(
                unit="tokens",
                quantity=UsageQuantity.ACTUAL,
                amount=40.0,
                source=SourceKind.COMMITTED,
                reference="request:1",
                dedupe_key="request:1",
            ),
            UsageObservation(
                unit="tokens",
                quantity=UsageQuantity.ACTUAL,
                amount=52.0,
                source=SourceKind.COMMITTED,
                reference="request:1",
                dedupe_key="request:1",
            ),
        )
    )
    assert report.known_usage(unit="tokens") == 52.0, "one request instance, counted once"


def test_a_summary_and_the_main_model_are_separate_spend() -> None:
    report = _report(
        usage=(
            UsageObservation(
                unit="tokens",
                quantity=UsageQuantity.ACTUAL,
                amount=100.0,
                source=SourceKind.COMMITTED,
                reference="request:1",
                dedupe_key="request:1",
            ),
            UsageObservation(
                unit="tokens",
                quantity=UsageQuantity.ACTUAL,
                amount=7.0,
                source=SourceKind.COMMITTED,
                reference="summary:r1",
                dedupe_key="summary:project-a/s1/r1",
            ),
        )
    )
    assert report.known_usage(unit="tokens") == 107.0
    assert report.known_usage(unit="tokens", dedupe_key="summary:project-a/s1/r1") == 7.0


def test_coverage_says_what_is_missing_and_calls_the_total_a_subtotal() -> None:
    report = _report(
        usage=(
            UsageObservation(
                unit="tokens",
                quantity=UsageQuantity.ACTUAL,
                amount=10.0,
                source=SourceKind.COMMITTED,
                reference="request:1",
                dedupe_key="request:1",
            ),
            UsageObservation(
                unit="tokens",
                quantity=UsageQuantity.UNKNOWN,
                source=SourceKind.MISSING,
                note="the run was interrupted inside the request",
            ),
        )
    )
    coverage = report.usage_coverage(unit="tokens")
    assert coverage["known_subtotal"] == 10.0
    assert coverage["complete"] is False
    assert coverage["unknown"] == 1
    assert coverage["unknown_reasons"] == ["the run was interrupted inside the request"]
    assert "total" not in coverage, "a partial sum is not offered under the name 'total'"


# --- spans: no start means no duration --------------------------------------------------


def test_a_span_without_an_end_is_incomplete_not_instant() -> None:
    span = SpanObservation(
        stage="tool:notes.write",
        started=Evidence(SourceKind.COMMITTED, reference="event:12"),
        clock="monotonic",
        interrupted=True,
        attempt="a1",
    )
    assert span.duration_ms is None
    assert span.incomplete is True


def test_a_duration_needs_both_endpoints_and_a_monotonic_clock() -> None:
    both = {
        "started": Evidence(SourceKind.COMMITTED, reference="event:12"),
        "ended": Evidence(SourceKind.COMMITTED, reference="event:13"),
    }
    with pytest.raises(ContractError, match="needs both a recorded start and a recorded end"):
        SpanObservation(stage="model:request:1", started=both["started"], clock="monotonic", duration_ms=5.0)
    with pytest.raises(ContractError, match="only a monotonic pair"):
        SpanObservation(stage="model:request:1", clock="wall", duration_ms=5.0, **both)
    SpanObservation(stage="model:request:1", clock="monotonic", duration_ms=5.0, **both)


def test_a_span_that_should_exist_and_does_not_is_marked_missing() -> None:
    span = SpanObservation.missing("context:prepare", "no projection facts were committed")
    assert span.absent is True
    assert "no projection facts" in span.note


# --- provenance: an old event does not borrow today's attempt ---------------------------


def test_an_entry_without_a_recorded_attempt_does_not_borrow_the_current_one() -> None:
    entry = TraceEntry(
        kind="tool_started",
        reference="call-1",
        source=SourceKind.COMMITTED,
        record="events:9",
        attempt_source="legacy",
    )
    assert entry.attempt == ""
    assert entry.attempt_source == "legacy"

    with pytest.raises(ContractError, match="must not be attributed"):
        TraceEntry(
            kind="tool_started",
            reference="call-1",
            source=SourceKind.COMMITTED,
            record="events:9",
            attempt="a9",
            attempt_source="legacy",
        )
    with pytest.raises(ContractError, match="names none"):
        TraceEntry(
            kind="tool_started",
            reference="call-1",
            source=SourceKind.COMMITTED,
            record="events:9",
            attempt_source="recorded",
        )
    with pytest.raises(ContractError, match="must name the record"):
        TraceEntry(
            kind="tool_started",
            reference="call-1",
            source=SourceKind.COMMITTED,
            attempt="a1",
        )


def test_a_committed_fact_names_the_record_it_came_from() -> None:
    with pytest.raises(ContractError, match="must name the record"):
        TraceEntry(kind="run_started", reference="r1", source=SourceKind.COMMITTED, attempt="a1")
    with pytest.raises(ContractError, match="must name the record"):
        Evidence(SourceKind.COMMITTED)
    Evidence(SourceKind.OBSERVED, detail="watched in this process")


def test_a_call_reports_where_its_receipt_lives() -> None:
    call = CallDiagnostic(
        call_id="call-1",
        tool_name="notes.write",
        outcome="settled",
        progress="observed",
        model_action_id="request:1",
        batch_ref="request:1",
        operation_id="project-a:s1:r1:request:1:call-1",
        receipt=Evidence(
            SourceKind.RECEIPT,
            reference="project-a:s1:r1:request:1:call-1",
            detail="the ledger holds this operation",
        ),
        result_available=True,
        origin_attempt="a1",
    )
    assert call.receipt.source is SourceKind.RECEIPT
    assert call.to_record()["receipt"]["reference"].endswith("call-1")


# --- the report as a whole --------------------------------------------------------------


def test_a_report_states_the_boundary_it_was_read_from() -> None:
    report = _report(
        observer_attempt="a2",
        origin_attempts=("a1",),
        status="suspended",
        boundary=ReadBoundary(
            cursor=41,
            high_water=41,
            run_revision=7,
            scanned_pages=2,
            truncated=False,
            store_schema={"run_record": "1", "execution_state": "3", "diagnostic": "1"},
        ),
    )
    record = report.to_record()
    assert record["observer_attempt"] == "a2"
    assert record["origin_attempts"] == ["a1"]
    assert record["boundary"]["run_revision"] == 7
    assert record["schema_version"] == DIAGNOSTIC_SCHEMA_VERSION


def test_identity_is_required() -> None:
    with pytest.raises(ContractError, match="report.namespace"):
        RunDiagnosticReport(namespace="", session_id="s1", run_id="r1")
    with pytest.raises(ContractError, match="report.run_id"):
        RunDiagnosticReport(namespace="p", session_id="s1", run_id="")


def test_a_report_round_trips_through_its_record() -> None:
    report = _report(
        observer_attempt="a2",
        origin_attempts=("a1", "a2"),
        status="usage_limited",
        stop_code="max_turns",
        error_code="max_runtime_exceeded",
        detail="the run exceeded its runtime budget",
        waiting={"kind": "reconciliation", "call_id": "call-1"},
        calls=(
            CallDiagnostic(
                call_id="call-1",
                tool_name="notes.write",
                outcome="uncertain",
                progress="declared",
                model_action_id="request:1",
                batch_ref="request:1",
                receipt=Evidence(SourceKind.RECEIPT, reference="op-1", detail="open intent"),
                detail="the outcome could not be proven",
                origin_attempt="a1",
            ),
        ),
        trace=(
            TraceEntry(
                kind="run_started",
                reference="r1",
                source=SourceKind.COMMITTED,
                record="run:r1",
                version="1",
                durable=True,
                attempt="a1",
                at=1.0,
                summary={"attempt": "a1"},
            ),
            TraceEntry(
                kind="tool_started",
                reference="call-1",
                source=SourceKind.COMMITTED,
                record="events:9",
                attempt_source="legacy",
                durable=True,
            ),
        ),
        spans=(
            SpanObservation(
                stage="model:request:1",
                span_id="request:1",
                started=Evidence(SourceKind.COMMITTED, reference="event:3"),
                ended=Evidence(SourceKind.COMMITTED, reference="event:4"),
                clock="monotonic",
                duration_ms=12.5,
                attempt="a1",
            ),
            SpanObservation.missing("tool:call-1", "the handler is still running", attempt="a2"),
        ),
        usage=(
            UsageObservation(
                unit="tokens",
                quantity=UsageQuantity.ACTUAL,
                amount=52.0,
                source=SourceKind.COMMITTED,
                reference="request:1",
                dedupe_key="request:1",
            ),
            UsageObservation(
                unit="tokens",
                quantity=UsageQuantity.UNKNOWN,
                source=SourceKind.MISSING,
                note="the provider returned no usage for the final request",
            ),
        ),
        integrity=("the execution snapshot has no recorded arrangement for this run",),
        unsupported=("the operation ledger behind notes.write offers no read capability",),
        artifacts=(Evidence(SourceKind.COMMITTED, reference="artifact:7", detail="a projection record"),),
    )
    again = RunDiagnosticReport.from_record(report.to_record())
    assert again.to_record() == report.to_record()
    assert again.calls[0].receipt is not None
    assert again.spans[1].absent is True
    assert again.usage[1].known is False
    assert again.boundary == report.boundary
    assert again.replace(detail="other").detail == "other"


def test_an_unknown_report_version_is_refused() -> None:
    record = _report().to_record()
    record["schema_version"] = DIAGNOSTIC_SCHEMA_VERSION + 1
    with pytest.raises(VersionRefused):
        RunDiagnosticReport.from_record(record)

    missing = _report().to_record()
    missing.pop("schema_version")
    with pytest.raises(VersionRefused):
        RunDiagnosticReport.from_record(missing)


def test_an_unknown_source_or_quantity_is_refused() -> None:
    with pytest.raises(ContractError, match="must be one of"):
        Evidence("guessed")  # type: ignore[arg-type]
    with pytest.raises(ContractError, match="must be one of"):
        UsageObservation.from_record(
            {
                "unit": "tokens",
                "quantity": "approximately",
                "amount": 1.0,
                "source": "committed",
            }
        )


def test_source_counts_include_the_zeroes() -> None:
    counts = count_sources([SourceKind.COMMITTED, SourceKind.COMMITTED, SourceKind.MISSING])
    assert counts["committed"] == 2
    assert counts["missing"] == 1
    assert counts["unsupported"] == 0
    assert counts["conflict"] == 0


def test_the_report_can_summarise_which_facts_are_incomplete() -> None:
    report = _report(
        spans=(
            SpanObservation.missing("model:request:1", "no request was ever sent"),
            SpanObservation(
                stage="tool:call-1",
                started=Evidence(SourceKind.COMMITTED, reference="event:5"),
                clock="monotonic",
                interrupted=True,
            ),
        )
    )
    assert [span.stage for span in report.incomplete_spans()] == [
        "model:request:1",
        "tool:call-1",
    ]
    counts = report.source_counts()
    assert counts["missing"] == 3, (
        "two for the stage with no record at all, one for the interruption's missing end"
    )
    assert counts["committed"] == 1
