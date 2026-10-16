"""Diagnostic report objects: what a run's facts are, and how well each one is known.

These are **report models**. They exist so a host can ask "why did this run do that?" and
get an answer that names its own sources, including the answers "I could not tell" and
"this record predates the fact you are asking about". Nothing here decides anything: the
reducer never reads these types, no ``RunStatus`` is added, and no second copy of the
operation ledger is created. The only authority they hold is the authority of the facts
they point at, and every field that could be mistaken for a fact carries where it came
from.

Four rules are enforced by construction, because each one has been a real defect in a
reporting layer somewhere:

* **an unknown quantity is never zero.** A missing usage figure is ``unknown`` with no
  amount, not ``0``. A total that silently omits what it does not know is worse than no
  total;
* **a duration needs two endpoints.** Without a recorded start there is no duration to
  report — the span is marked incomplete and the reader is told which end is missing;
* **a per-invocation fact is not a durable one.** A trace entry says whether it was
  committed, whether it was watched from this process, and which attempt it belongs to.
  An old record that never recorded an attempt says so (``unknown``/``legacy``) instead of
  borrowing the current attempt;
* **a unit is named or the measurement is not a measurement.** Tokens, bytes,
  milliseconds, requests and currency are different things; a bare number that could be any
  of them is how costs get added up wrongly.

Serialization is explicit (``to_record``/``from_record``), versioned separately from the
execution state, and refuses a version this build cannot read rather than guessing.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from ..errors import ContractError

__all__ = [
    "CallDiagnostic",
    "DIAGNOSTIC_SCHEMA_VERSION",
    "Evidence",
    "ReadBoundary",
    "RunDiagnosticReport",
    "SourceKind",
    "SpanObservation",
    "SUPPORTED_DIAGNOSTIC_SCHEMA_VERSIONS",
    "TraceEntry",
    "UsageObservation",
    "UsageQuantity",
    "count_sources",
]

#: Bumped when the persisted shape of a report changes in a way a released reader cannot
#: prove safe. A report is data a host may store beside its runs, so it is versioned the
#: same way the execution state is — and a reader that does not know a version refuses it
#: instead of interpreting the fields it happens to recognise.
DIAGNOSTIC_SCHEMA_VERSION = 1

SUPPORTED_DIAGNOSTIC_SCHEMA_VERSIONS = (1,)


class SourceKind(str, Enum):
    """How well-founded one reported fact is.

    The distinction is the reason this package exists. A report that mixes a committed
    record with a guess from the current process is not a report, and a report that cannot
    say "this ledger has no read capability" is one that will be read as if it had.
    """

    #: Read from the run store: a committed run record or a stored event.
    COMMITTED = "committed"
    #: An authoritative operation receipt, read from the ledger that owns it.
    RECEIPT = "receipt"
    #: Witnessed by this process while it is running (no durable record yet).
    OBSERVED = "observed"
    #: This projection's own explanation of the facts above (never a fact itself).
    DERIVED = "derived"
    #: The fact should exist and does not: a missing record, field or endpoint.
    MISSING = "missing"
    #: Two committed facts disagree; neither is chosen here.
    CONFLICT = "conflict"
    #: The reader could not ask (no capability, unknown format, refused version).
    UNSUPPORTED = "unsupported"


class UsageQuantity(str, Enum):
    """What kind of number a measurement is."""

    #: The provider or store reported it.
    ACTUAL = "actual"
    #: This build computed it (a token estimate, a payload size) and says so.
    ESTIMATED = "estimated"
    #: Not known. Carries no amount: an unknown cost is not a zero cost.
    UNKNOWN = "unknown"


def _require_choice(value: Any, enum: type[Enum], *, name: str) -> Any:
    try:
        return enum(value)
    except ValueError as exc:
        allowed = ", ".join(sorted(item.value for item in enum))
        raise ContractError(f"{name} must be one of {allowed}; got {value!r}") from exc


def _require_text(value: Any, *, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractError(f"{name} must be a non-empty string")
    return value


@dataclass(frozen=True)
class Evidence:
    """Where one reported fact came from, and how far it can be trusted.

    ``reference`` should identify the record (an operation id, an event cursor, a run
    revision) rather than repeat its content: a report that copies a ledger becomes a
    second ledger.
    """

    source: SourceKind
    reference: str = ""
    version: str = ""
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.source, SourceKind):
            object.__setattr__(
                self, "source", _require_choice(self.source, SourceKind, name="evidence.source")
            )
        if self.source is SourceKind.COMMITTED and not self.reference:
            raise ContractError(
                "a committed fact must name the record it was read from; without it the "
                "report cannot be checked against the store"
            )

    def to_record(self) -> Mapping[str, Any]:
        return {
            "source": self.source.value,
            "reference": self.reference,
            "version": self.version,
            "detail": self.detail,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "Evidence":
        return cls(
            source=_require_choice(record.get("source"), SourceKind, name="evidence.source"),
            reference=str(record.get("reference") or ""),
            version=str(record.get("version") or ""),
            detail=str(record.get("detail") or ""),
        )

    @classmethod
    def missing(cls, detail: str) -> "Evidence":
        return cls(SourceKind.MISSING, detail=detail)

    @classmethod
    def unsupported(cls, detail: str) -> "Evidence":
        return cls(SourceKind.UNSUPPORTED, detail=detail)


#: Units a measurement may be stated in. Named, because "12" is not a cost.
_USAGE_UNITS = (
    "tokens",
    #: Input and output tokens are separate units on purpose: they price differently, and a
    #: single "tokens" figure invites adding them into a number nobody asked for.
    "input_tokens",
    "output_tokens",
    "bytes",
    "milliseconds",
    "requests",
    "currency:USD",
    "currency:EUR",
    "currency:CNY",
)


@dataclass(frozen=True)
class UsageObservation:
    """One measurement of one thing, with the unit and the reason to believe it.

    ``dedupe_key`` is the identity of what was measured, not of the measurement: a
    streaming response reports cumulative and final usage for the same *request instance*,
    and summing both double-counts it. A summary's usage and the main model's usage have
    different keys on purpose — they are different spend, and neither may absorb the other.
    """

    unit: str
    quantity: UsageQuantity
    amount: float | None = None
    source: SourceKind = SourceKind.MISSING
    reference: str = ""
    dedupe_key: str = ""
    estimator: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        if self.unit not in _USAGE_UNITS:
            raise ContractError(
                f"a usage observation must name its unit ({', '.join(_USAGE_UNITS)}); got "
                f"{self.unit!r}. A number without a unit cannot be compared or added."
            )
        if not isinstance(self.quantity, UsageQuantity):
            object.__setattr__(
                self,
                "quantity",
                _require_choice(self.quantity, UsageQuantity, name="usage.quantity"),
            )
        if self.quantity is UsageQuantity.UNKNOWN:
            if self.amount is not None:
                raise ContractError(
                    "an unknown measurement carries no amount: reporting one would turn "
                    "'we do not know' into a number a caller will add up"
                )
            if not self.note:
                raise ContractError(
                    "an unknown measurement must say why it is unknown (a missing record, a "
                    "provider that does not report usage, an interrupted request)"
                )
        else:
            if self.amount is None or self.amount < 0:
                raise ContractError(
                    f"a {self.quantity.value} measurement needs a non-negative amount"
                )
        if self.quantity is UsageQuantity.ESTIMATED and not self.estimator:
            raise ContractError(
                "an estimated measurement must name its estimator; otherwise a guess is "
                "indistinguishable from the provider's own figure"
            )
        if self.source is SourceKind.COMMITTED and not self.reference:
            raise ContractError("a committed measurement must name the record it came from")

    @property
    def known(self) -> bool:
        return self.quantity is not UsageQuantity.UNKNOWN

    def to_record(self) -> Mapping[str, Any]:
        return {
            "unit": self.unit,
            "quantity": self.quantity.value,
            "amount": self.amount,
            "source": self.source.value,
            "reference": self.reference,
            "dedupe_key": self.dedupe_key,
            "estimator": self.estimator,
            "note": self.note,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "UsageObservation":
        return cls(
            unit=str(record.get("unit") or ""),
            quantity=_require_choice(
                record.get("quantity"), UsageQuantity, name="usage.quantity"
            ),
            amount=None if record.get("amount") is None else float(record["amount"]),
            source=_require_choice(record.get("source"), SourceKind, name="usage.source"),
            reference=str(record.get("reference") or ""),
            dedupe_key=str(record.get("dedupe_key") or ""),
            estimator=str(record.get("estimator") or ""),
            note=str(record.get("note") or ""),
        )


@dataclass(frozen=True)
class SpanObservation:
    """One stage of an invocation, and whether its duration can honestly be stated.

    ``clock`` is part of the record because a that wall clock and a monotonic clock are
    not interchangeable: a duration computed between two monotonic readings is meaningful
    only inside one process, and a report that subtracts a persisted monotonic value from
    another process's is nonsense with the appearance of precision.
    """

    stage: str
    span_id: str = ""
    #: Which *real* request this span belongs to. A retried model request has a new
    #: instance and therefore new cost, even when the logical model action is the same.
    request_instance: str = ""
    started: Evidence | None = None
    ended: Evidence | None = None
    clock: str = ""
    duration_ms: float | None = None
    interrupted: bool = False
    attempt: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        _require_text(self.stage, name="span.stage")
        if self.clock not in {"", "monotonic", "wall"}:
            raise ContractError(f"span.clock must be '', 'monotonic' or 'wall'; got {self.clock!r}")
        if self.duration_ms is not None:
            if self.started is None or self.ended is None:
                raise ContractError(
                    f"{self.stage}: a duration needs both a recorded start and a recorded end; "
                    "a span with one end is incomplete, not fast"
                )
            if self.clock != "monotonic":
                raise ContractError(
                    f"{self.stage}: only a monotonic pair of readings inside one process gives a "
                    f"duration; this span's clock is {self.clock or 'unrecorded'!r}"
                )
            if self.duration_ms < 0:
                raise ContractError(f"{self.stage}: a duration cannot be negative")

    @property
    def incomplete(self) -> bool:
        """A span that started and has no recorded end (an interruption, or a crash)."""
        return self.started is not None and self.ended is None

    @property
    def absent(self) -> bool:
        return self.started is None and self.ended is None

    @classmethod
    def missing(cls, stage: str, reason: str, **fields: Any) -> "SpanObservation":
        """A stage that should have a record and does not."""
        return cls(stage=stage, started=None, ended=None, note=reason, **fields)

    def to_record(self) -> Mapping[str, Any]:
        return {
            "stage": self.stage,
            "span_id": self.span_id,
            "request_instance": self.request_instance,
            "started": None if self.started is None else self.started.to_record(),
            "ended": None if self.ended is None else self.ended.to_record(),
            "clock": self.clock,
            "duration_ms": self.duration_ms,
            "interrupted": self.interrupted,
            "attempt": self.attempt,
            "note": self.note,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "SpanObservation":
        started = record.get("started")
        ended = record.get("ended")
        return cls(
            stage=str(record.get("stage") or ""),
            span_id=str(record.get("span_id") or ""),
            request_instance=str(record.get("request_instance") or ""),
            started=None if started is None else Evidence.from_record(started),
            ended=None if ended is None else Evidence.from_record(ended),
            clock=str(record.get("clock") or ""),
            duration_ms=(
                None if record.get("duration_ms") is None else float(record["duration_ms"])
            ),
            interrupted=bool(record.get("interrupted")),
            attempt=str(record.get("attempt") or ""),
            note=str(record.get("note") or ""),
        )


@dataclass(frozen=True)
class TraceEntry:
    """One thing that happened, with its own provenance.

    ``summary`` is a whitelist of publishable fields (ids, counts, statuses). Raw prompt
    text, tool arguments, tool output and provider continuation material never belong here:
    a report is not an export of the conversation, and a redaction pass over free text is
    not a redaction pass.
    """

    kind: str
    reference: str
    source: SourceKind
    record: str = ""
    version: str = ""
    durable: bool = False
    attempt: str = ""
    #: ``recorded`` when the record itself named the attempt, ``unknown`` when it did not
    #: (an older event), ``legacy`` when the format predates the field entirely, and
    #: ``derived`` for an entry this projection produced rather than read (an explanation is
    #: not a record of something that happened *in* an attempt).
    attempt_source: str = "recorded"
    at: float | None = None
    summary: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.kind, name="trace.kind")
        _require_text(self.reference, name="trace.reference")
        if not isinstance(self.source, SourceKind):
            object.__setattr__(
                self, "source", _require_choice(self.source, SourceKind, name="trace.source")
            )
        if self.attempt_source not in {"recorded", "unknown", "legacy", "derived"}:
            raise ContractError(
                "trace.attempt_source must be recorded/unknown/legacy/derived; got "
                f"{self.attempt_source!r}"
            )
        if self.attempt_source == "recorded" and not self.attempt:
            raise ContractError(
                f"{self.reference}: an entry claims its attempt was recorded but names none"
            )
        if self.attempt_source != "recorded" and self.attempt:
            raise ContractError(
                f"{self.reference}: {self.attempt!r} must not be attributed to an entry whose "
                "attempt is not recorded — the current run's attempt is not evidence about an "
                "older event"
            )
        if self.attempt_source == "derived" and not self.source in {
            SourceKind.DERIVED,
            SourceKind.MISSING,
        }:
            raise ContractError(
                f"{self.reference}: only a derived or missing entry may omit its attempt and "
                "still claim to be about the run rather than about an attempt"
            )
        for key in self.summary:
            if not isinstance(key, str):
                raise ContractError("trace summary keys must be strings")
        if self.source is SourceKind.COMMITTED and not self.record:
            raise ContractError(
                f"{self.reference}: a committed entry must name the record it was read from"
            )

    def to_record(self) -> Mapping[str, Any]:
        return {
            "kind": self.kind,
            "reference": self.reference,
            "source": self.source.value,
            "record": self.record,
            "version": self.version,
            "durable": self.durable,
            "attempt": self.attempt,
            "attempt_source": self.attempt_source,
            "at": self.at,
            "summary": dict(self.summary),
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "TraceEntry":
        return cls(
            kind=str(record.get("kind") or ""),
            reference=str(record.get("reference") or ""),
            source=_require_choice(record.get("source"), SourceKind, name="trace.source"),
            record=str(record.get("record") or ""),
            version=str(record.get("version") or ""),
            durable=bool(record.get("durable")),
            attempt=str(record.get("attempt") or ""),
            attempt_source=str(record.get("attempt_source") or "recorded"),
            at=None if record.get("at") is None else float(record["at"]),
            summary=dict(record.get("summary") or {}),
        )


@dataclass(frozen=True)
class CallDiagnostic:
    """One declared tool call, as the run records it, plus what the ledger can add."""

    call_id: str
    tool_name: str
    outcome: str
    progress: str = ""
    model_action_id: str = ""
    batch_ref: str = ""
    operation_id: str = ""
    receipt: Evidence | None = None
    detail: str = ""
    result_available: bool = False
    terminate: bool = False
    origin_attempt: str = ""
    origin_attempt_source: str = "recorded"

    def __post_init__(self) -> None:
        _require_text(self.call_id, name="call.call_id")
        _require_text(self.tool_name, name="call.tool_name")
        _require_text(self.outcome, name="call.outcome")
        if self.origin_attempt_source not in {"recorded", "unknown", "legacy"}:
            raise ContractError(
                "call.origin_attempt_source must be recorded/unknown/legacy; got "
                f"{self.origin_attempt_source!r}"
            )
        if self.origin_attempt_source != "recorded" and self.origin_attempt:
            raise ContractError(
                f"{self.call_id}: an unrecorded origin must not borrow the current attempt"
            )

    def to_record(self) -> Mapping[str, Any]:
        return {
            "call_id": self.call_id,
            "tool_name": self.tool_name,
            "outcome": self.outcome,
            "progress": self.progress,
            "model_action_id": self.model_action_id,
            "batch_ref": self.batch_ref,
            "operation_id": self.operation_id,
            "receipt": None if self.receipt is None else self.receipt.to_record(),
            "detail": self.detail,
            "result_available": self.result_available,
            "terminate": self.terminate,
            "origin_attempt": self.origin_attempt,
            "origin_attempt_source": self.origin_attempt_source,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "CallDiagnostic":
        receipt = record.get("receipt")
        return cls(
            call_id=str(record.get("call_id") or ""),
            tool_name=str(record.get("tool_name") or ""),
            outcome=str(record.get("outcome") or ""),
            progress=str(record.get("progress") or ""),
            model_action_id=str(record.get("model_action_id") or ""),
            batch_ref=str(record.get("batch_ref") or ""),
            operation_id=str(record.get("operation_id") or ""),
            receipt=None if receipt is None else Evidence.from_record(receipt),
            detail=str(record.get("detail") or ""),
            result_available=bool(record.get("result_available")),
            terminate=bool(record.get("terminate")),
            origin_attempt=str(record.get("origin_attempt") or ""),
            origin_attempt_source=str(record.get("origin_attempt_source") or "recorded"),
        )


@dataclass(frozen=True)
class ReadBoundary:
    """Which consistent boundary the report was taken from.

    A reader of a live run can see two partitions from two different moments. Rather than
    silently assembling them, the report states the cursor and revision it fixed, whether
    the scan hit a limit, and whether the view is non-atomic; a caller that needs one
    moment can then ask again instead of trusting a mixture.
    """

    cursor: int | None = None
    high_water: int | None = None
    next_cursor: int | None = None
    run_revision: int | None = None
    scanned_pages: int = 0
    truncated: bool = False
    scan_limit_hit: bool = False
    #: True when the scan reached the end of the store: nothing was left unread. Kept apart
    #: from ``truncated``/``scan_limit_hit`` because "there is no more" is a much stronger
    #: statement than "I carried as much as I was allowed to carry" — a page that happened to
    #: end exactly at the collection limit is not evidence that the store is empty.
    exhausted: bool = False
    non_atomic: bool = False
    store_schema: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("cursor", "high_water", "next_cursor", "run_revision", "scanned_pages"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, int):
                raise ContractError(f"read boundary {name} must be an integer or None")
        if self.scanned_pages < 0:
            raise ContractError("read boundary scanned_pages cannot be negative")

    def to_record(self) -> Mapping[str, Any]:
        return {
            "cursor": self.cursor,
            "high_water": self.high_water,
            "next_cursor": self.next_cursor,
            "run_revision": self.run_revision,
            "scanned_pages": self.scanned_pages,
            "truncated": self.truncated,
            "scan_limit_hit": self.scan_limit_hit,
            "exhausted": self.exhausted,
            "non_atomic": self.non_atomic,
            "store_schema": dict(self.store_schema),
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "ReadBoundary":
        return cls(
            cursor=record.get("cursor"),
            high_water=record.get("high_water"),
            next_cursor=record.get("next_cursor"),
            run_revision=record.get("run_revision"),
            scanned_pages=int(record.get("scanned_pages") or 0),
            truncated=bool(record.get("truncated")),
            scan_limit_hit=bool(record.get("scan_limit_hit")),
            exhausted=bool(record.get("exhausted")),
            non_atomic=bool(record.get("non_atomic")),
            store_schema=dict(record.get("store_schema") or {}),
        )


@dataclass(frozen=True)
class RunDiagnosticReport:
    """One run, explained from the facts that are available about it.

    ``origin_attempts`` names the attempts that actually did the work, in the order they
    appear in the records; ``observer_attempt`` is the attempt reading the report now. The
    two are never merged: "who ran this" and "who is looking" are different questions, and
    a report that answers the second with the first is how a recovery looks like a rerun.
    """

    namespace: str
    session_id: str
    run_id: str
    observer_attempt: str = ""
    origin_attempts: tuple[str, ...] = ()
    status: str = ""
    stop_code: str = ""
    error_code: str = ""
    detail: str = ""
    waiting: Mapping[str, Any] | None = None
    calls: tuple[CallDiagnostic, ...] = ()
    trace: tuple[TraceEntry, ...] = ()
    spans: tuple[SpanObservation, ...] = ()
    usage: tuple[UsageObservation, ...] = ()
    integrity: tuple[str, ...] = ()
    unsupported: tuple[str, ...] = ()
    boundary: ReadBoundary = field(default_factory=ReadBoundary)
    artifacts: tuple[Evidence, ...] = ()
    schema_version: int = DIAGNOSTIC_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in ("namespace", "session_id", "run_id"):
            _require_text(getattr(self, name), name=f"report.{name}")
        if self.schema_version not in SUPPORTED_DIAGNOSTIC_SCHEMA_VERSIONS:
            raise ContractError(
                f"diagnostic schema version {self.schema_version!r} is not one this build "
                f"writes ({SUPPORTED_DIAGNOSTIC_SCHEMA_VERSIONS})"
            )
        if self.observer_attempt and self.observer_attempt in self.origin_attempts:
            # Legal and common (an ordinary run reading its own record), and worth stating
            # so a caller does not have to guess whether that coincidence means anything.
            pass

    # -- derived views --------------------------------------------------

    def source_counts(self) -> Mapping[str, int]:
        """How many facts came from each kind of source.

        A span contributes one fact per endpoint: the endpoint's own source when it is
        recorded, and ``missing`` when it is not. An absent span is therefore *visible* as
        missing rather than invisible, which is the difference between a report that has
        nothing to say about a stage and one that says the stage has no record.
        """
        sources: list[SourceKind] = [entry.source for entry in self.trace]
        for span in self.spans:
            sources.append(span.started.source if span.started is not None else SourceKind.MISSING)
            sources.append(span.ended.source if span.ended is not None else SourceKind.MISSING)
        sources += [item.source for item in self.usage]
        sources += [call.receipt.source for call in self.calls if call.receipt is not None]
        sources += [item.source for item in self.artifacts]
        return count_sources(sources)

    def known_usage(self, *, unit: str, dedupe_key: str = "") -> float:
        """The total of *known* measurements for one unit, by dedupe identity.

        Measurements sharing a ``dedupe_key`` are the same thing measured more than once
        (a streaming response reports cumulative usage and then a final figure), so only
        the **last** one counts: the measurements must be in commit order, and the final
        figure supersedes the running one rather than being added to it. Measurements with
        no key are each counted — nothing claims they are the same.

        Callers that need to know how much is missing must read :meth:`usage_coverage`:
        this number is a subtotal, not a total.
        """
        latest: dict[str, float] = {}
        unkeyed = 0.0
        for item in self.usage:
            if item.unit != unit or not item.known:
                continue
            if dedupe_key and item.dedupe_key != dedupe_key:
                continue
            assert item.amount is not None
            if item.dedupe_key:
                latest[item.dedupe_key] = item.amount
            else:
                unkeyed += item.amount
        return sum(latest.values()) + unkeyed

    def usage_coverage(self, *, unit: str) -> Mapping[str, Any]:
        """How much of one unit is known: counts and the amount that is not."""
        actual = [item for item in self.usage if item.unit == unit and item.quantity is UsageQuantity.ACTUAL]
        estimated = [
            item for item in self.usage if item.unit == unit and item.quantity is UsageQuantity.ESTIMATED
        ]
        unknown = [item for item in self.usage if item.unit == unit and not item.known]
        return {
            "unit": unit,
            "actual": len(actual),
            "estimated": len(estimated),
            "unknown": len(unknown),
            "complete": not unknown and not estimated,
            "known_subtotal": self.known_usage(unit=unit),
            "unknown_reasons": sorted({item.note for item in unknown if item.note}),
        }

    def incomplete_spans(self) -> tuple[SpanObservation, ...]:
        return tuple(span for span in self.spans if span.incomplete or span.absent)

    def to_record(self) -> Mapping[str, Any]:
        return {
            "schema_version": self.schema_version,
            "identity": {
                "namespace": self.namespace,
                "session_id": self.session_id,
                "run_id": self.run_id,
            },
            "observer_attempt": self.observer_attempt,
            "origin_attempts": list(self.origin_attempts),
            "status": self.status,
            "stop_code": self.stop_code,
            "error_code": self.error_code,
            "detail": self.detail,
            "waiting": None if self.waiting is None else dict(self.waiting),
            "calls": [call.to_record() for call in self.calls],
            "trace": [entry.to_record() for entry in self.trace],
            "spans": [span.to_record() for span in self.spans],
            "usage": [item.to_record() for item in self.usage],
            "integrity": list(self.integrity),
            "unsupported": list(self.unsupported),
            "boundary": self.boundary.to_record(),
            "artifacts": [item.to_record() for item in self.artifacts],
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "RunDiagnosticReport":
        """Rebuild a report, refusing a version or shape this build cannot interpret."""
        if not isinstance(record, Mapping):
            raise ContractError(
                f"a diagnostic report must be a mapping, got {type(record).__name__}"
            )
        version = record.get("schema_version")
        if version not in SUPPORTED_DIAGNOSTIC_SCHEMA_VERSIONS:
            from ..errors import VersionRefused

            raise VersionRefused(
                "diagnostic report", version, SUPPORTED_DIAGNOSTIC_SCHEMA_VERSIONS
            )
        identity = record.get("identity") or {}
        if not isinstance(identity, Mapping):
            raise ContractError("a diagnostic report's identity must be a mapping")
        return cls(
            namespace=str(identity.get("namespace") or ""),
            session_id=str(identity.get("session_id") or ""),
            run_id=str(identity.get("run_id") or ""),
            observer_attempt=str(record.get("observer_attempt") or ""),
            origin_attempts=tuple(str(item) for item in record.get("origin_attempts") or ()),
            status=str(record.get("status") or ""),
            stop_code=str(record.get("stop_code") or ""),
            error_code=str(record.get("error_code") or ""),
            detail=str(record.get("detail") or ""),
            waiting=record.get("waiting"),
            calls=tuple(
                CallDiagnostic.from_record(item) for item in record.get("calls") or ()
            ),
            trace=tuple(TraceEntry.from_record(item) for item in record.get("trace") or ()),
            spans=tuple(
                SpanObservation.from_record(item) for item in record.get("spans") or ()
            ),
            usage=tuple(
                UsageObservation.from_record(item) for item in record.get("usage") or ()
            ),
            integrity=tuple(str(item) for item in record.get("integrity") or ()),
            unsupported=tuple(str(item) for item in record.get("unsupported") or ()),
            boundary=ReadBoundary.from_record(record.get("boundary") or {}),
            artifacts=tuple(
                Evidence.from_record(item) for item in record.get("artifacts") or ()
            ),
            schema_version=int(version),
        )

    def replace(self, **changes: Any) -> "RunDiagnosticReport":
        return replace(self, **changes)


def count_sources(sources: Iterable[SourceKind]) -> Mapping[str, int]:
    """Count facts per source kind, always including the zeroes.

    The zeroes matter: "0 unsupported" and "no unsupported key at all" mean different
    things to a reader deciding whether a report is complete.
    """
    counts = {kind.value: 0 for kind in SourceKind}
    for source in sources:
        key = source.value if isinstance(source, SourceKind) else str(source)
        counts[key] = counts.get(key, 0) + 1
    return counts
