"""Turn the facts a reader collected into one explained report.

Pure: the same facts and the same boundary always produce the same report, and nothing here
reads a clock, a store or a tool. Two properties matter more than the code:

* **an explanation is labelled as one.** Anything this module concludes (the outcome's
  meaning, why a run is waiting, that a stage has no record) is a ``DERIVED`` trace entry or
  a missing span — never mixed in with a committed fact;
* **an absence is not a zero.** A request with no completion event is an incomplete span; a
  payload the build never measured has no byte count; usage the provider did not report is
  ``unknown`` with the reason, not ``0``.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .types import (
    CallDiagnostic,
    DIAGNOSTIC_SCHEMA_VERSION,
    Evidence,
    RunDiagnosticReport,
    SourceKind,
    SpanObservation,
    TraceEntry,
    UsageObservation,
    UsageQuantity,
)

__all__ = ["project_report"]

#: The fields a trace entry may publish. A whitelist, not a blacklist: a payload key nobody
#: thought about is dropped rather than exported, and the count of what was dropped is
#: stated so a reader can see that the entry is a summary.
_SUMMARY_FIELDS = frozenset(
    {
        "attempt",
        "call_id",
        "clock",
        "detail",
        "duration_ms",
        "interaction_id",
        "interrupted",
        "kind",
        "model_action_id",
        "operation_id",
        "outcome",
        "payload_bytes",
        "payload_bytes_estimator",
        "policy_version",
        "reason",
        "refused",
        "request_instance",
        "source_head",
        "state",
        "status",
        "stop_code",
        "terminate",
        "tool_name",
        "turns_used",
        "usage",
        "context_schema_version",
        "estimator_id",
        "kept_fragments",
        "removed_messages",
    }
)

#: Provider usage field → the unit it is measured in. Input and output are not one thing:
#: they price differently, and adding them produces a number nobody asked for.
_USAGE_UNITS = {
    "input": "input_tokens",
    "output": "output_tokens",
    "cache_read": "input_tokens",
    "cache_write": "input_tokens",
    "cache_write_1h": "input_tokens",
    "reasoning": "output_tokens",
    "total_tokens": "tokens",
}


def project_report(facts: Any) -> RunDiagnosticReport:
    """Build the report for one run from the facts and the boundary that produced them."""
    record: Mapping[str, Any] = dict(facts.record or {})
    run_id = facts.run_id
    integrity: list[str] = list(facts.integrity)
    unsupported: list[str] = list(facts.unsupported)
    snapshot = _snapshot(record, integrity)
    calls = _calls(record, snapshot, facts, tuple(facts.events))
    events = tuple(facts.events)

    if not snapshot:
        integrity.append(
            "this run carries no committed execution state, so the per-call partition and "
            "the outstanding dispatch are reported from the run record alone"
        )
    unsupported.append(
        "in-process facts (lease/CAS refusals and event-fanout failures) are not durable; a "
        "store-only report cannot show them"
    )

    return RunDiagnosticReport(
        namespace=facts.namespace,
        session_id=facts.session_id,
        run_id=run_id,
        observer_attempt=facts.observer_attempt,
        origin_attempts=_origin_attempts(record, snapshot, calls, events),
        status=str(record.get("status") or ""),
        stop_code=str(record.get("stop_code") or ""),
        error_code=str(record.get("error_code") or ""),
        detail=str(record.get("detail") or ""),
        waiting=_waiting(record),
        calls=calls,
        trace=_trace(record, events, integrity),
        spans=_spans(record, snapshot, events, integrity),
        usage=_usage(record, events),
        integrity=tuple(integrity),
        unsupported=tuple(unsupported),
        boundary=facts.boundary,
        artifacts=_artifacts(record),
        schema_version=DIAGNOSTIC_SCHEMA_VERSION,
    )


# --- the pieces ------------------------------------------------------------------------


def _snapshot(record: Mapping[str, Any], integrity: list[str]) -> Mapping[str, Any]:
    execution = record.get("execution")
    if not isinstance(execution, Mapping):
        return {}
    version = execution.get("schema_version")
    if version not in (1, 2, 3, 4):
        integrity.append(
            f"the execution state declares schema version {version!r}, which this build does "
            "not interpret; the partition is reported only as far as it can be read"
        )
        return {}
    return execution


def _calls(
    record: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    facts: Any,
    events: Sequence[Mapping[str, Any]],
) -> tuple[CallDiagnostic, ...]:
    """Every call the run recorded, from the current partition or from the event history.

    The partition is *current state*: a finished turn clears it (the batch belongs to the
    turn that declared it), so a report about a completed run has to read the calls from the
    `tool_call_observed` facts instead. When both exist the partition wins for the current
    view, because it is what the run itself would resume from.
    """
    partition = snapshot.get("batch")
    if not isinstance(partition, Sequence) or isinstance(partition, (str, bytes)):
        partition = ()
    if not partition:
        # A legacy record (or a paused one) keeps the batch on the run record instead.
        batch = record.get("batch")
        if isinstance(batch, Mapping):
            partition = batch.get("calls") or ()
    out: list[CallDiagnostic] = []
    for item in partition:
        if not isinstance(item, Mapping):
            continue
        operation_id = str(item.get("operation_id") or "")
        progress = item.get("progress")
        attempt = str(item.get("source_attempt") or "")
        out.append(
            CallDiagnostic(
                call_id=str(item.get("call_id") or ""),
                tool_name=str(item.get("tool_name") or ""),
                outcome=_outcome_of(item),
                progress=str(getattr(progress, "value", progress) or ""),
                model_action_id=str(item.get("model_action_id") or ""),
                batch_ref=str(snapshot.get("model_action_id") or ""),
                operation_id=operation_id,
                receipt=_receipt_evidence(operation_id, facts),
                detail=str(item.get("reason") or ""),
                # "Available" means the model has (or will get) a real outcome, not that a
                # placeholder exists: a waiting call carries the "waiting" text and nothing
                # has been decided or written.
                result_available=(
                    _outcome_of(item) == "settled" and item.get("result") is not None
                ),
                terminate=bool(item.get("terminate")),
                origin_attempt=attempt,
                origin_attempt_source="recorded" if attempt else "unknown",
            )
        )
    if not out:
        out = _calls_from_events(events, facts)
    return tuple(out)


def _calls_from_events(
    events: Sequence[Mapping[str, Any]], facts: Any
) -> list[CallDiagnostic]:
    """The calls a run's own event history observed, latest observation per call.

    One call can be observed more than once — a batch paused on an approval and was
    dispatched again by a later attempt — so the last observation is the current view and the
    earlier ones are named in the call's detail. Neither is dropped: "this call was seen
    twice, by attempts a1 and a2" is exactly what a reader needs to understand a recovery.
    """
    latest: dict[tuple[str, str], Mapping[str, Any]] = {}
    order: list[tuple[str, str]] = []
    attempts: dict[tuple[str, str], list[str]] = {}
    for event in events:
        if event.get("kind") != "tool_call_observed":
            continue
        payload = dict(event.get("payload") or {})
        key = (str(payload.get("model_action_id") or ""), str(payload.get("call_id") or ""))
        if key not in latest:
            order.append(key)
        latest[key] = payload
        attempts.setdefault(key, []).append(str(payload.get("attempt") or ""))
    out: list[CallDiagnostic] = []
    for key in order:
        payload = latest[key]
        action, call_id = key
        operation_id = str(payload.get("operation_id") or "")
        detail = str(payload.get("reason") or "")
        seen = [attempt for attempt in attempts[key] if attempt]
        if len(attempts[key]) > 1:
            detail = (
                (detail + " | " if detail else "")
                + f"observed {len(attempts[key])} times across attempts: {', '.join(seen)}"
            )
        outcome = str(payload.get("outcome") or "")
        out.append(
            CallDiagnostic(
                call_id=call_id,
                tool_name=str(payload.get("tool_name") or ""),
                outcome=outcome,
                progress="observed",
                model_action_id=action,
                batch_ref=action,
                operation_id=operation_id,
                receipt=_receipt_evidence(operation_id, facts),
                detail=detail,
                result_available=outcome == "settled",
                terminate=bool(payload.get("terminate")),
                origin_attempt=seen[-1] if seen else "",
                origin_attempt_source="recorded" if seen else "unknown",
            )
        )
    return out


def _outcome_of(item: Mapping[str, Any]) -> str:
    outcome = item.get("outcome")
    return str(getattr(outcome, "value", outcome) or "")


def _receipt_evidence(operation_id: str, facts: Any) -> Evidence | None:
    """Where the operation's own state lives, and whether this build could read it."""
    if not operation_id:
        return None
    if not facts.ledger_capable:
        return Evidence(
            SourceKind.UNSUPPORTED,
            reference=operation_id,
            detail="no read-only ledger was provided, so the operation's state was not read",
        )
    row = facts.ledger.get(operation_id)
    if row is None:
        return Evidence(
            SourceKind.MISSING,
            reference=operation_id,
            detail="the ledger holds no row for this operation",
        )
    return Evidence(
        SourceKind.RECEIPT,
        reference=operation_id,
        detail=f"the ledger records this operation as {row.get('state')!r}",
    )


def _trace(
    record: Mapping[str, Any], events: Sequence[Mapping[str, Any]], integrity: list[str]
) -> tuple[TraceEntry, ...]:
    entries: list[TraceEntry] = []
    for event in events:
        payload = dict(event.get("payload") or {})
        attempt = str(payload.get("attempt") or "")
        entries.append(
            TraceEntry(
                kind=str(event.get("kind") or ""),
                reference=f"events:{event.get('sequence')}",
                source=SourceKind.COMMITTED,
                record=f"run:{record.get('run_id')}",
                version=str(event.get("sequence")),
                durable=True,
                attempt=attempt,
                # An older event never recorded an attempt. Saying so is the point: filling
                # it in from the current run would invent provenance.
                attempt_source="recorded" if attempt else "unknown",
                at=event.get("at"),
                summary=_summary(payload),
            )
        )
    if not events:
        integrity.append(
            "no committed events were found for this run in the scanned range, so the report "
            "has no timeline; the next cursor says whether the scan simply ended"
        )
    # The explanation is this projection's own, and says so.
    entries.append(
        TraceEntry(
            kind="explanation:outcome",
            reference=f"run:{record.get('run_id')}",
            source=SourceKind.DERIVED,
            attempt_source="derived",
            summary={
                "status": str(record.get("status") or ""),
                "stop_code": str(record.get("stop_code") or ""),
                "error_code": str(record.get("error_code") or "") or None,
                "turns_used": record.get("turns_used"),
            },
        )
    )
    waiting = _waiting(record)
    if waiting:
        entries.append(
            TraceEntry(
                kind="explanation:waiting",
                reference=str(waiting.get("interaction_id") or waiting.get("call_id") or "waiting"),
                source=SourceKind.DERIVED,
                attempt_source="derived",
                summary={
                    "kind": waiting.get("kind"),
                    "call_id": waiting.get("call_id"),
                    "tool_name": waiting.get("tool_name"),
                    "interaction_id": waiting.get("interaction_id"),
                },
            )
        )
    return tuple(entries)


def _summary(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """The publishable half of one payload, with the count of what was left out."""
    summary: dict[str, Any] = {}
    for key, value in payload.items():
        if key not in _SUMMARY_FIELDS:
            continue
        if isinstance(value, Mapping):
            # Only a mapping of scalars (a usage report); anything deeper is left out.
            if any(isinstance(item, (Mapping, list, tuple)) for item in value.values()):
                continue
            summary[key] = {str(k): v for k, v in value.items()}
        elif isinstance(value, (str, int, float, bool)) or value is None:
            summary[key] = value
        elif isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
            summary[key] = list(value)
    omitted = len(payload) - len(summary)
    if omitted:
        summary["omitted_fields"] = omitted
    return summary


def _spans(
    record: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    integrity: list[str],
) -> tuple[SpanObservation, ...]:
    spans: list[SpanObservation] = []
    requests = _request_spans(record, events)
    spans.extend(requests)
    tools = _tool_spans(events)
    spans.extend(tools)
    batch = _batch_span(events)
    if batch is not None:
        spans.append(batch)
    if record.get("context_state"):
        spans.append(
            SpanObservation.missing(
                "context:prepare",
                "this build commits the projection facts but not the preparation's timing, "
                "so no duration can be stated",
            )
        )
    # A call the run itself recorded as not observed has no span at all. Saying so is
    # different from saying it took no time: nothing was measured because nothing ran (or
    # because the process died first), and either way a duration would be an invention.
    declared = _declared_calls(snapshot, record)
    observed_ids = {span.span_id.rsplit(":", 1)[-1] for span in tools}
    unaccounted = [
        call
        for call in declared
        if str(call.get("call_id") or "") not in observed_ids
        and str(getattr(call.get("progress"), "value", call.get("progress")) or "") != "observed"
    ]
    for call in unaccounted:
        spans.append(
            SpanObservation.missing(
                f"tool:{call.get('tool_name') or call.get('call_id')}",
                "no observation was recorded for this call, so it has no span",
                attempt=str(call.get("source_attempt") or call.get("attempt") or ""),
            )
        )
    if declared and not tools:
        spans.append(
            SpanObservation.missing(
                "tool:batch",
                "the run declared tool calls and nothing was dispatched, so no tool span "
                "exists for this batch",
            )
        )
    if not snapshot and record.get("batch"):
        integrity.append(
            "the run carries a recorded batch but no execution state, so the batch's own "
            "schedule progress cannot be read"
        )
    return tuple(spans)


def _declared_calls(
    snapshot: Mapping[str, Any], record: Mapping[str, Any]
) -> list[Mapping[str, Any]]:
    """Every call the run itself recorded, from the current partition or the run record."""
    partition = snapshot.get("batch")
    if not isinstance(partition, Sequence) or isinstance(partition, (str, bytes)):
        batch = record.get("batch")
        partition = batch.get("calls") if isinstance(batch, Mapping) else ()
    return [item for item in (partition or ()) if isinstance(item, Mapping)]


def _request_spans(
    record: Mapping[str, Any], events: Sequence[Mapping[str, Any]]
) -> list[SpanObservation]:
    started: dict[str, Mapping[str, Any]] = {}
    finished: dict[str, Mapping[str, Any]] = {}
    for event in events:
        payload = dict(event.get("payload") or {})
        instance = str(payload.get("request_instance") or "")
        if not instance:
            continue
        if event.get("kind") == "model_request_dispatched":
            started.setdefault(instance, event)
        elif event.get("kind") == "model_request_completed":
            finished[instance] = event
    spans: list[SpanObservation] = []
    for instance, start in started.items():
        end = finished.get(instance)
        payload = dict(end.get("payload") or {}) if end is not None else {}
        duration = payload.get("duration_ms")
        spans.append(
            SpanObservation(
                stage="model:request",
                span_id=instance,
                request_instance=instance,
                started=Evidence(
                    SourceKind.COMMITTED, reference=f"events:{start.get('sequence')}"
                ),
                ended=(
                    None
                    if end is None
                    else Evidence(SourceKind.COMMITTED, reference=f"events:{end.get('sequence')}")
                ),
                clock=str(payload.get("clock") or "") if end is not None else "",
                duration_ms=None if duration is None else float(duration),
                interrupted=end is None,
                attempt=str(dict(start.get("payload") or {}).get("attempt") or ""),
                note=(
                    "the request was dispatched and no response was recorded: the process "
                    "may have died, or the run was interrupted inside it"
                    if end is None
                    else ""
                ),
            )
        )
    # A completion whose dispatch this build cannot see (an older record) is stated as an
    # incomplete span rather than given an invented start.
    for instance, end in finished.items():
        if instance in started:
            continue
        payload = dict(end.get("payload") or {})
        spans.append(
            SpanObservation(
                stage="model:request",
                span_id=instance,
                request_instance=instance,
                started=None,
                ended=Evidence(SourceKind.COMMITTED, reference=f"events:{end.get('sequence')}"),
                duration_ms=None,
                attempt=str(payload.get("attempt") or ""),
                note="the response was recorded but its dispatch was not, so no duration exists",
            )
        )
    return spans


def _tool_spans(events: Sequence[Mapping[str, Any]]) -> list[SpanObservation]:
    spans: list[SpanObservation] = []
    for event in events:
        if event.get("kind") != "tool_call_observed":
            continue
        payload = dict(event.get("payload") or {})
        duration = payload.get("duration_ms")
        attempt = str(payload.get("attempt") or "")
        call_id = str(payload.get("call_id") or "")
        spans.append(
            SpanObservation(
                stage=f"tool:{payload.get('tool_name') or call_id}",
                span_id=f"{payload.get('model_action_id') or ''}:{call_id}",
                started=(
                    None
                    if payload.get("started_at") is None
                    else Evidence(SourceKind.COMMITTED, reference=f"events:{event.get('sequence')}")
                ),
                ended=(
                    None
                    if duration is None
                    else Evidence(SourceKind.COMMITTED, reference=f"events:{event.get('sequence')}")
                ),
                clock=str(payload.get("clock") or "") if duration is not None else "",
                duration_ms=None if duration is None else float(duration),
                interrupted=bool(payload.get("interrupted")),
                attempt=attempt,
                note=(
                    "the call was observed without a measurement; the handler's timing was "
                    "never recorded"
                    if duration is None
                    else ""
                ),
            )
        )
    return spans


def _batch_span(events: Sequence[Mapping[str, Any]]) -> SpanObservation | None:
    """The wall-clock span of one tool batch, kept separate from the calls inside it.

    A wall-clock difference between two *durable* instants is a span, not a duration: it
    says when the batch started and ended, and it must not be presented as a measured
    duration (nor must a parallel batch's calls be added up into one).
    """
    tools = [event for event in events if event.get("kind") == "tool_call_observed"]
    if not tools:
        return None
    first, last = tools[0], tools[-1]
    return SpanObservation(
        stage="tool:batch",
        span_id=str(dict(first.get("payload") or {}).get("model_action_id") or ""),
        started=Evidence(SourceKind.COMMITTED, reference=f"events:{first.get('sequence')}"),
        ended=Evidence(SourceKind.COMMITTED, reference=f"events:{last.get('sequence')}"),
        clock="wall",
        duration_ms=None,
        attempt=str(dict(first.get("payload") or {}).get("attempt") or ""),
        note=(
            "a wall-clock span across records, not a measured duration; the calls inside it "
            "have their own spans and are never summed into this one"
        ),
    )


def _usage(
    record: Mapping[str, Any], events: Sequence[Mapping[str, Any]]
) -> tuple[UsageObservation, ...]:
    observations: list[UsageObservation] = []
    for event in events:
        if event.get("kind") != "model_request_completed":
            continue
        payload = dict(event.get("payload") or {})
        instance = str(payload.get("request_instance") or f"events:{event.get('sequence')}")
        reference = f"events:{event.get('sequence')}"
        usage = payload.get("usage")
        if isinstance(usage, Mapping):
            for field, amount in usage.items():
                if amount is None:
                    continue
                unit = _USAGE_UNITS.get(str(field))
                if unit is None:
                    continue
                observations.append(
                    UsageObservation(
                        unit=unit,
                        quantity=UsageQuantity.ACTUAL,
                        amount=float(amount),
                        source=SourceKind.COMMITTED,
                        reference=reference,
                        # The identity of what was measured is the real request instance, so
                        # a retry is a second measurement rather than a duplicate.
                        dedupe_key=f"{instance}:{field}",
                        note=f"provider-reported {field}",
                    )
                )
        if payload.get("payload_bytes") is not None:
            observations.append(
                UsageObservation(
                    unit="bytes",
                    quantity=UsageQuantity.ESTIMATED,
                    amount=float(payload["payload_bytes"]),
                    source=SourceKind.COMMITTED,
                    reference=reference,
                    dedupe_key=f"{instance}:payload_bytes",
                    estimator=str(payload.get("payload_bytes_estimator") or "unrecorded"),
                    note="the size of the request body this build handed the provider",
                )
            )
        if not isinstance(usage, Mapping) or not any(
            value is not None for value in usage.values()
        ):
            observations.append(
                UsageObservation(
                    unit="tokens",
                    quantity=UsageQuantity.UNKNOWN,
                    source=SourceKind.MISSING,
                    reference=reference,
                    dedupe_key=f"{instance}:usage",
                    note="the provider did not report usage for this request",
                )
            )
    observations.extend(_context_usage(record))
    if not any(event.get("kind") == "model_request_completed" for event in events):
        # A record from a build that did not write per-request measurements: the run's own
        # figure is used, labelled with what it is (the last turn), never as a total.
        observations.append(_record_usage(record))
    return tuple(observations)


def _context_usage(record: Mapping[str, Any]) -> list[UsageObservation]:
    """The projection's own estimate, and the auxiliary path's spend, kept apart."""
    facts = record.get("context_state")
    if not isinstance(facts, Mapping):
        return []
    out: list[UsageObservation] = []
    run_id = record.get("run_id")
    estimator = "@".join(
        item
        for item in (
            str(facts.get("estimator_id") or ""),
            str(facts.get("estimator_version") or ""),
            str(facts.get("estimator_method") or ""),
        )
        if item
    )
    estimated = facts.get("estimated_input_tokens")
    if estimated is not None:
        out.append(
            UsageObservation(
                unit="input_tokens",
                quantity=UsageQuantity.ESTIMATED,
                amount=float(estimated),
                source=SourceKind.COMMITTED,
                reference=f"run:{run_id}#context_state",
                dedupe_key=f"context:{facts.get('scope') or run_id}:{facts.get('source_head') or ''}",
                estimator=estimator or "unrecorded",
                note="the projection's own estimate of the request it prepared",
            )
        )
    summary = facts.get("summary")
    if isinstance(summary, Mapping):
        scope = f"summary:{facts.get('scope') or run_id}"
        calls = summary.get("calls")
        if calls is not None:
            out.append(
                UsageObservation(
                    unit="requests",
                    quantity=UsageQuantity.ACTUAL,
                    amount=float(calls),
                    source=SourceKind.COMMITTED,
                    reference=f"run:{run_id}#summary",
                    dedupe_key=f"{scope}:calls",
                    note="auxiliary (summary) requests, counted separately from the main model",
                )
            )
        model_usage = summary.get("model_usage")
        reported = (
            isinstance(model_usage, Mapping)
            and any(value is not None for value in model_usage.values())
        )
        for field, amount in (model_usage or {}).items():
            unit = _USAGE_UNITS.get(str(field))
            if unit is None or amount is None:
                continue
            out.append(
                UsageObservation(
                    unit=unit,
                    quantity=UsageQuantity.ACTUAL,
                    amount=float(amount),
                    source=SourceKind.COMMITTED,
                    reference=f"run:{run_id}#summary",
                    dedupe_key=f"{scope}:{field}",
                    note="auxiliary (summary) usage, never merged with the main model's",
                )
            )
        if not reported:
            out.append(
                UsageObservation(
                    unit="tokens",
                    quantity=UsageQuantity.UNKNOWN,
                    source=SourceKind.MISSING,
                    reference=f"run:{run_id}#summary",
                    dedupe_key=f"{scope}:usage",
                    note=(
                        "the summarizer reported no usage"
                        + (
                            "; at least one auxiliary call is recorded as unconfirmed"
                            if summary.get("usage_unknown")
                            else ""
                        )
                    ),
                )
            )
    return out


def _record_usage(record: Mapping[str, Any], note_prefix: str = "") -> UsageObservation:
    """The fallback: the run record's own figure, labelled with what it is."""
    usage = record.get("usage")
    reported = isinstance(usage, Mapping) and any(
        value is not None for value in usage.values()
    )
    if not reported:
        return UsageObservation(
            unit="tokens",
            quantity=UsageQuantity.UNKNOWN,
            source=SourceKind.MISSING,
            reference=f"run:{record.get('run_id')}",
            note=note_prefix + "the run record carries no usage figures",
        )
    total = None
    for field in ("total_tokens", "input", "output"):
        if isinstance(usage, Mapping) and usage.get(field) is not None:
            total = float(usage[field])
            break
    if total is None:
        return UsageObservation(
            unit="tokens",
            quantity=UsageQuantity.UNKNOWN,
            source=SourceKind.MISSING,
            reference=f"run:{record.get('run_id')}",
            note=note_prefix + "the run record's usage has no comparable figure",
        )
    return UsageObservation(
        unit="tokens",
        quantity=UsageQuantity.ACTUAL,
        amount=total,
        source=SourceKind.COMMITTED,
        reference=f"run:{record.get('run_id')}",
        dedupe_key=f"run:{record.get('run_id')}:last_turn",
        note=note_prefix + "the last turn's figure as the run record stores it",
    )


def _waiting(record: Mapping[str, Any]) -> Mapping[str, Any] | None:
    interaction = record.get("pending_interaction")
    return dict(interaction) if isinstance(interaction, Mapping) and interaction else None


def _origin_attempts(
    record: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    calls: Sequence[CallDiagnostic],
    events: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    """Who actually did the work, in the order the records name them.

    Distinct from the observer: a recovery reading a crashed run's records is *not* the
    attempt that ran them, and a report that answers "who ran this" with the reader is how a
    resume looks like a rerun.
    """
    ordered: list[str] = []
    for candidate in (
        record.get("attempt_id"),
        snapshot.get("attempt_id"),
        *(call.origin_attempt for call in calls),
        *(dict(event.get("payload") or {}).get("attempt") for event in events),
    ):
        text = str(candidate or "")
        if text and text not in ordered:
            ordered.append(text)
    return tuple(ordered)


def _artifacts(record: Mapping[str, Any]) -> tuple[Evidence, ...]:
    """Controlled references to the heavier records a report points at but does not embed."""
    out: list[Evidence] = []
    run_id = record.get("run_id")
    if record.get("execution"):
        version = (record.get("execution") or {}).get("schema_version")
        out.append(
            Evidence(
                SourceKind.COMMITTED,
                reference=f"run:{run_id}#execution",
                version=str(version),
                detail="the committed execution snapshot: the per-call partition and schedule",
            )
        )
    facts = record.get("context_state")
    if isinstance(facts, Mapping):
        out.append(
            Evidence(
                SourceKind.COMMITTED,
                reference=f"run:{run_id}#context_state",
                version=str(facts.get("context_schema_version")),
                detail="the committed projection facts behind the request view",
            )
        )
        head = facts.get("source_head")
        if head:
            out.append(
                Evidence(
                    SourceKind.COMMITTED,
                    reference=f"run:{run_id}#context_state",
                    detail=f"the projection was built against source head {head}",
                )
            )
    if record.get("messages"):
        out.append(
            Evidence(
                SourceKind.COMMITTED,
                reference=f"run:{run_id}#messages",
                version=f"count:{len(record.get('messages') or ())}",
                detail="the committed transcript; the report publishes counts, not its text",
            )
        )
    return tuple(out)
