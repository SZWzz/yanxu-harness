"""Bounded, read-only correlation across authoritative diagnostic sources.

The timeline is a fact view, not a new ledger. Each source keeps its own durable rows and
defines its read watermark and cursor. This module only validates, correlates and orders the
whitelisted facts a source returns. A source without stable sequence support is still visible,
but the report says so and never uses its wall clock as causal order.
"""

from __future__ import annotations

import heapq
import json
import math
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from ..errors import ConfigurationError, ContractError
from .render import escape_markdown, sanitise_text
from .types import SourceKind

__all__ = [
    "CoverageGap",
    "CoverageGapCode",
    "MetricAggregate",
    "MetricCoverage",
    "MetricName",
    "MetricObservation",
    "RootCauseCategory",
    "RootCauseFact",
    "TimelineAnalysis",
    "TimelineCursor",
    "TimelineFact",
    "TimelineInspector",
    "TimelineReadPort",
    "TimelineReport",
    "TimelineSourcePage",
    "analyze_timeline",
    "timeline_report_to_json",
    "timeline_report_to_markdown",
]

TIMELINE_SCHEMA_VERSION = 1
_MAX_PAGE_SIZE = 1000
_MAX_SOURCES = 32
_MAX_REFERENCE_HISTORY = 4096


class CoverageGapCode(str, Enum):
    SOURCE_UNAVAILABLE = "source_unavailable"
    TRUNCATED = "truncated"
    SCAN_LIMIT_HIT = "scan_limit_hit"
    MISSING_RELATIONSHIP = "missing_relationship"
    DUPLICATE_REFERENCE = "duplicate_reference"
    SEQUENCE_UNAVAILABLE = "sequence_unavailable"
    INVALID_PAGE = "invalid_page"
    NON_ATOMIC = "non_atomic"
    CAUSAL_CYCLE = "causal_cycle"
    REFERENCE_HISTORY_LIMITED = "reference_history_limited"


class RootCauseCategory(str, Enum):
    APPROVAL = "approval"
    DEPENDENCY = "dependency"
    DEPLOYMENT_DRIFT = "deployment_drift"
    BUDGET = "budget"
    CONTEXT = "context"
    PROVIDER = "provider"
    TOOL = "tool"
    OPERATION_UNKNOWN = "operation_unknown"
    STORAGE_CONFLICT = "storage_conflict"
    ACCEPTANCE = "acceptance"
    CANCELLATION = "cancellation"


class MetricName(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    INCONCLUSIVE = "inconclusive"
    READY_TO_ADMISSION_WAIT = "ready_to_admission_wait"
    MANUAL_DECISION_COUNT = "manual_decision_count"
    RECOVERY_DURATION = "recovery_duration"
    UNKNOWN_DURATION = "unknown_duration"
    BUDGET_USAGE = "budget_usage"
    RETRY_COUNT = "retry_count"


class MetricCoverage(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


_DURATION_METRICS = frozenset(
    {MetricName.READY_TO_ADMISSION_WAIT, MetricName.RECOVERY_DURATION, MetricName.UNKNOWN_DURATION}
)


@dataclass(frozen=True)
class MetricObservation:
    """A small, source-owned measurement with explicit coverage and clock evidence."""

    name: MetricName
    value: float | None
    unit: str
    coverage: MetricCoverage = MetricCoverage.COMPLETE
    clock_domain: str = ""
    endpoints_same_clock: bool = False
    dedupe_key: str = ""

    def __post_init__(self) -> None:
        for name, enum_type in (("name", MetricName), ("coverage", MetricCoverage)):
            value = getattr(self, name)
            if not isinstance(value, enum_type):
                try:
                    object.__setattr__(self, name, enum_type(value))
                except (TypeError, ValueError) as exc:
                    raise ContractError(f"metric observation has an unknown {name}") from exc
        if not isinstance(self.unit, str) or not self.unit or len(self.unit) > 32:
            raise ContractError("metric observation unit must be short non-empty text")
        if sanitise_text(self.unit) != self.unit:
            raise ContractError("metric observation unit must be display-safe")
        if self.dedupe_key:
            _safe_reference(self.dedupe_key, name="metric dedupe_key")
        if self.name is MetricName.BUDGET_USAGE and self.value is not None and not self.dedupe_key:
            raise ContractError("budget usage needs a canonical reservation dedupe_key")
        if self.value is None:
            if self.coverage is MetricCoverage.COMPLETE:
                raise ContractError("a complete metric observation needs a value")
        else:
            try:
                valid_value = (
                    not isinstance(self.value, bool)
                    and isinstance(self.value, (int, float))
                    and math.isfinite(self.value)
                    and self.value >= 0
                )
            except OverflowError:
                valid_value = False
            if not valid_value:
                raise ContractError("metric observation value must be finite and non-negative or None")
        if self.name in _DURATION_METRICS:
            if self.value is not None and (
                not self.endpoints_same_clock
                or not isinstance(self.clock_domain, str)
                or not self.clock_domain
            ):
                raise ContractError("a duration needs recorded endpoints in the same clock domain")
            if self.clock_domain:
                _safe_reference(self.clock_domain, name="metric clock_domain")
        elif self.clock_domain or self.endpoints_same_clock:
            raise ContractError("clock evidence is only valid for duration metrics")

    def to_record(self) -> Mapping[str, Any]:
        return {
            "name": self.name.value,
            "value": self.value,
            "unit": self.unit,
            "coverage": self.coverage.value,
            "clock_domain": self.clock_domain,
            "endpoints_same_clock": self.endpoints_same_clock,
            "dedupe_key": self.dedupe_key,
        }


def _cursor_token(value: Any, *, name: str, allow_none: bool = True) -> str | int | None:
    if value is None and allow_none:
        return None
    if type(value) is int or (isinstance(value, str) and value):
        return value
    raise ContractError(f"{name} must be a non-empty string, integer, or None")


def _safe_reference(value: str, *, name: str) -> str:
    if not value or len(value) > 240 or sanitise_text(value) != value:
        raise ContractError(f"{name} must be a bounded, display-safe identifier")
    return value


@dataclass(frozen=True)
class TimelineFact:
    """A source-owned fact reduced to stable IDs, enums, digests and display-safe values."""

    source_id: str
    reference: str
    kind: str
    source_kind: SourceKind
    sequence: int | None
    source_cursor: str | int
    related_references: tuple[str, ...] = ()
    causal_references: tuple[str, ...] = ()
    status: str = ""
    summary: str = ""
    display_at: float | None = None
    metrics: tuple[MetricObservation, ...] = ()
    root_cause: RootCauseCategory | None = None

    def __post_init__(self) -> None:
        for name in ("source_id", "reference", "kind"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ContractError(f"timeline fact {name} must be non-empty text")
            if name != "kind":
                _safe_reference(value, name=f"timeline fact {name}")
            elif len(value) > 64 or sanitise_text(value) != value:
                raise ContractError("timeline fact kind must be a short, display-safe label")
        if not isinstance(self.source_kind, SourceKind):
            try:
                object.__setattr__(self, "source_kind", SourceKind(self.source_kind))
            except ValueError as exc:
                raise ContractError("timeline fact has an unknown source kind") from exc
        if self.sequence is not None and (type(self.sequence) is not int or self.sequence < 0):
            raise ContractError("timeline fact sequence must be a non-negative integer or None")
        source_cursor = _cursor_token(
            self.source_cursor, name="timeline fact source_cursor", allow_none=False
        )
        if isinstance(source_cursor, str):
            _safe_reference(source_cursor, name="timeline fact source_cursor")
        for name in ("related_references", "causal_references"):
            values = tuple(getattr(self, name))
            if any(not isinstance(item, str) or not item for item in values):
                raise ContractError(f"timeline fact {name} must contain non-empty references")
            for item in values:
                _safe_reference(item, name=f"timeline fact {name}")
            if len(values) != len(set(values)):
                raise ContractError(f"timeline fact {name} contains a duplicate reference")
            object.__setattr__(self, name, values)
        if self.display_at is not None and (
            isinstance(self.display_at, bool)
            or not isinstance(self.display_at, (int, float))
            or not math.isfinite(self.display_at)
        ):
            raise ContractError("timeline fact display_at must be finite or None")
        if not isinstance(self.status, str) or not isinstance(self.summary, str):
            raise ContractError("timeline fact status and summary must be text")
        if len(self.status) > 64 or sanitise_text(self.status) != self.status:
            raise ContractError("timeline fact status must be a short, display-safe label")
        object.__setattr__(self, "summary", sanitise_text(self.summary))
        metrics = tuple(self.metrics)
        if any(not isinstance(item, MetricObservation) for item in metrics):
            raise ContractError("timeline fact metrics must be MetricObservation values")
        object.__setattr__(self, "metrics", metrics)
        if self.root_cause is not None and not isinstance(self.root_cause, RootCauseCategory):
            try:
                object.__setattr__(self, "root_cause", RootCauseCategory(self.root_cause))
            except (TypeError, ValueError) as exc:
                raise ContractError("timeline fact has an unknown root cause category") from exc
        _cursor_token(self.source_cursor, name="timeline fact source_cursor", allow_none=False)

    def to_record(self) -> Mapping[str, Any]:
        return {
            "source_id": self.source_id,
            "reference": self.reference,
            "kind": self.kind,
            "source_kind": self.source_kind.value,
            "sequence": self.sequence,
            "source_cursor": self.source_cursor,
            "related_references": list(self.related_references),
            "causal_references": list(self.causal_references),
            "status": self.status,
            "summary": self.summary,
            "display_at": self.display_at,
            "metrics": [item.to_record() for item in self.metrics],
            "root_cause": None if self.root_cause is None else self.root_cause.value,
        }


@dataclass(frozen=True)
class CoverageGap:
    code: CoverageGapCode
    source_id: str = ""
    reference: str = ""
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.code, CoverageGapCode):
            try:
                object.__setattr__(self, "code", CoverageGapCode(self.code))
            except ValueError as exc:
                raise ContractError("timeline coverage gap has an unknown code") from exc
        for name in ("source_id", "reference", "detail"):
            if not isinstance(getattr(self, name), str):
                raise ContractError(f"timeline coverage gap {name} must be text")
        if self.source_id:
            _safe_reference(self.source_id, name="timeline coverage gap source_id")
        if self.reference:
            _safe_reference(self.reference, name="timeline coverage gap reference")
        object.__setattr__(self, "detail", sanitise_text(self.detail))

    def to_record(self) -> Mapping[str, Any]:
        return {
            "code": self.code.value,
            "source_id": self.source_id,
            "reference": self.reference,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class TimelineSourcePage:
    facts: tuple[TimelineFact, ...] = ()
    #: Cursor after every row examined by the source, including unrelated rows it filtered.
    #: The inspector uses it only when every returned fact was included in the output page.
    next_cursor: str | int | None = None
    exhausted: bool = False
    truncated: bool = False
    scan_limit_hit: bool = False
    non_atomic: bool = False
    gaps: tuple[CoverageGap, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "facts", tuple(self.facts))
        object.__setattr__(self, "gaps", tuple(self.gaps))
        if any(not isinstance(item, TimelineFact) for item in self.facts):
            raise ContractError("timeline source pages may contain only TimelineFact values")
        if any(not isinstance(item, CoverageGap) for item in self.gaps):
            raise ContractError("timeline source page gaps must be CoverageGap values")
        next_cursor = _cursor_token(self.next_cursor, name="timeline source page next_cursor")
        if isinstance(next_cursor, str):
            _safe_reference(next_cursor, name="timeline source page next_cursor")
        for name in ("exhausted", "truncated", "scan_limit_hit", "non_atomic"):
            if type(getattr(self, name)) is not bool:
                raise ContractError(f"timeline source page {name} must be a bool")


@runtime_checkable
class TimelineReadPort(Protocol):
    """Optional read-only source port. It must honor the supplied fixed watermark."""

    source_id: str
    read_only: bool
    sequence_ordered: bool

    async def capture_watermark(self, identity: Any) -> str | int:
        """Return an immutable source boundary; an empty source uses a stable sentinel."""

    async def read_page(
        self,
        identity: Any,
        *,
        cursor: str | int | None,
        watermark: str | int,
        limit: int,
    ) -> TimelineSourcePage:
        """Read only facts at or below ``watermark``, after ``cursor``."""


@dataclass(frozen=True)
class TimelineCursor:
    """The fixed per-source read boundary and continuation vector."""

    watermarks: Mapping[str, str | int]
    source_cursors: Mapping[str, str | int | None]
    emitted_references: tuple[str, ...] = ()
    schema_version: int = TIMELINE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        watermarks = dict(self.watermarks)
        cursors = dict(self.source_cursors)
        if not watermarks or set(watermarks) != set(cursors):
            raise ContractError("timeline cursor needs matching source watermark and cursor vectors")
        for key, value in watermarks.items():
            if not isinstance(key, str) or not key:
                raise ContractError("timeline cursor source ids must be non-empty text")
            normalized_watermark = _cursor_token(value, name=f"watermark for {key}", allow_none=False)
            if isinstance(normalized_watermark, str):
                _safe_reference(normalized_watermark, name=f"watermark for {key}")
            _cursor_token(cursors[key], name=f"cursor for {key}")
            if isinstance(cursors[key], str):
                _safe_reference(cursors[key], name=f"cursor for {key}")
        refs = tuple(self.emitted_references)
        if len(refs) > _MAX_REFERENCE_HISTORY or any(not isinstance(item, str) or not item for item in refs):
            raise ContractError("timeline cursor reference history is invalid or exceeds its bound")
        for item in refs:
            _safe_reference(item, name="timeline cursor emitted reference")
        if len(refs) != len(set(refs)):
            raise ContractError("timeline cursor reference history contains duplicates")
        if self.schema_version != TIMELINE_SCHEMA_VERSION:
            raise ContractError("unsupported timeline cursor schema version")
        object.__setattr__(self, "watermarks", MappingProxyType(watermarks))
        object.__setattr__(self, "source_cursors", MappingProxyType(cursors))
        object.__setattr__(self, "emitted_references", refs)

    def to_record(self) -> Mapping[str, Any]:
        return {
            "schema_version": self.schema_version,
            "watermarks": dict(sorted(self.watermarks.items())),
            "source_cursors": dict(sorted(self.source_cursors.items())),
            "emitted_references": list(self.emitted_references),
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "TimelineCursor":
        if not isinstance(record, Mapping):
            raise ContractError("timeline cursor record must be a mapping")
        try:
            return cls(
                watermarks=dict(record.get("watermarks") or {}),
                source_cursors=dict(record.get("source_cursors") or {}),
                emitted_references=tuple(record.get("emitted_references") or ()),
                schema_version=int(record.get("schema_version") or 0),
            )
        except (TypeError, ValueError) as exc:
            raise ContractError("malformed timeline cursor record") from exc


@dataclass(frozen=True)
class MetricAggregate:
    name: MetricName
    unit: str
    observations: int
    known_observations: int
    unknown_observations: int
    total: float | None
    mean: float | None
    minimum: float | None
    maximum: float | None
    coverage: MetricCoverage
    duplicate_rows: int = 0
    conflicting_rows: int = 0

    def to_record(self) -> Mapping[str, Any]:
        return {
            "name": self.name.value,
            "unit": self.unit,
            "observations": self.observations,
            "known_observations": self.known_observations,
            "unknown_observations": self.unknown_observations,
            "total": self.total,
            "mean": self.mean,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "coverage": self.coverage.value,
            "duplicate_rows": self.duplicate_rows,
            "conflicting_rows": self.conflicting_rows,
        }


@dataclass(frozen=True)
class RootCauseFact:
    category: RootCauseCategory
    source_id: str
    reference: str
    source_kind: SourceKind
    inferred: bool = False

    def to_record(self) -> Mapping[str, Any]:
        return {
            "category": self.category.value,
            "source_id": self.source_id,
            "reference": self.reference,
            "source_kind": self.source_kind.value,
            "inferred": self.inferred,
        }


@dataclass(frozen=True)
class TimelineAnalysis:
    metrics: tuple[MetricAggregate, ...] = ()
    root_causes: tuple[RootCauseFact, ...] = ()

    def to_record(self) -> Mapping[str, Any]:
        return {
            "metrics": [item.to_record() for item in self.metrics],
            "root_causes": [item.to_record() for item in self.root_causes],
        }


@dataclass(frozen=True)
class TimelineReport:
    facts: tuple[TimelineFact, ...]
    cursor: TimelineCursor
    gaps: tuple[CoverageGap, ...] = ()
    has_more: bool = False
    schema_version: int = TIMELINE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "facts", tuple(self.facts))
        object.__setattr__(self, "gaps", tuple(self.gaps))
        if not isinstance(self.cursor, TimelineCursor):
            raise ContractError("timeline report needs a TimelineCursor")
        if type(self.has_more) is not bool:
            raise ContractError("timeline report has_more must be a bool")

    def to_record(self) -> Mapping[str, Any]:
        return {
            "schema_version": self.schema_version,
            "facts": [item.to_record() for item in self.facts],
            "watermarks": dict(sorted(self.cursor.watermarks.items())),
            "source_cursors": dict(sorted(self.cursor.source_cursors.items())),
            "cursor": self.cursor.to_record(),
            "gaps": [item.to_record() for item in self.gaps],
            "has_more": self.has_more,
            "analysis": analyze_timeline(self).to_record(),
        }


class TimelineInspector:
    """Merge optional read-only sources without turning them into a second event store."""

    def __init__(self, sources: Sequence[TimelineReadPort]) -> None:
        if not sources or len(sources) > _MAX_SOURCES:
            raise ContractError(f"timeline needs between 1 and {_MAX_SOURCES} read sources")
        indexed: dict[str, TimelineReadPort] = {}
        for source in sources:
            source_id = getattr(source, "source_id", "")
            if not isinstance(source_id, str) or not source_id:
                raise ContractError("timeline sources need a stable source_id")
            if source_id in indexed:
                raise ContractError(f"duplicate timeline source_id {source_id!r}")
            if getattr(source, "read_only", False) is not True:
                raise ConfigurationError(
                    f"timeline source {source_id!r} is not read-only; open its authoritative store read-only"
                )
            if type(getattr(source, "sequence_ordered", None)) is not bool:
                raise ContractError(f"timeline source {source_id!r} must declare sequence_ordered as a bool")
            if not callable(getattr(source, "capture_watermark", None)) or not callable(
                getattr(source, "read_page", None)
            ):
                raise ContractError(f"timeline source {source_id!r} lacks the read port")
            indexed[source_id] = source
        self._sources = dict(sorted(indexed.items()))

    async def inspect_page(
        self,
        identities: Mapping[str, Any],
        *,
        cursor: TimelineCursor | None = None,
        limit: int = 100,
    ) -> TimelineReport:
        if type(limit) is not int or not 1 <= limit <= _MAX_PAGE_SIZE:
            raise ContractError(f"timeline page limit must be in [1, {_MAX_PAGE_SIZE}]")
        if not isinstance(identities, Mapping):
            raise ContractError("timeline identities must map source ids to source identities")
        source_ids = set(self._sources)
        if cursor is not None and (
            set(cursor.watermarks) != source_ids or set(cursor.source_cursors) != source_ids
        ):
            raise ContractError("timeline cursor source vector does not match the configured sources")

        gaps: list[CoverageGap] = []
        watermarks: dict[str, str | int] = {}
        source_cursors: dict[str, str | int | None] = {}
        previous_refs: tuple[str, ...] = ()
        if cursor is None:
            for source_id, source in self._sources.items():
                if source_id not in identities:
                    gaps.append(CoverageGap(CoverageGapCode.SOURCE_UNAVAILABLE, source_id, detail="identity missing"))
                    continue
                try:
                    watermark = await source.capture_watermark(identities[source_id])
                    watermarks[source_id] = _cursor_token(
                        watermark, name=f"watermark for {source_id}", allow_none=False
                    )  # type: ignore[assignment]
                    source_cursors[source_id] = None
                except Exception as exc:  # a missing adapter is reportable coverage, not inferred success
                    gaps.append(
                        CoverageGap(
                            CoverageGapCode.SOURCE_UNAVAILABLE,
                            source_id,
                            detail=f"watermark read failed ({type(exc).__name__})",
                        )
                    )
            # Preserve the source vector even when one source cannot be opened. Its unavailable
            # boundary is an explicit sentinel and is never passed to that source's read method.
            for source_id in source_ids - set(watermarks):
                watermarks[source_id] = "!unavailable"
                source_cursors[source_id] = None
        else:
            watermarks = dict(cursor.watermarks)
            source_cursors = dict(cursor.source_cursors)
            previous_refs = cursor.emitted_references

        pages: dict[str, TimelineSourcePage] = {}
        candidates: list[TimelineFact] = []
        invalid_sources: set[str] = set()
        unavailable = {gap.source_id for gap in gaps if gap.code is CoverageGapCode.SOURCE_UNAVAILABLE}
        for source_id, source in self._sources.items():
            if source_id in unavailable or source_id not in identities:
                if source_id not in unavailable:
                    gaps.append(CoverageGap(CoverageGapCode.SOURCE_UNAVAILABLE, source_id, detail="identity missing"))
                continue
            watermark = watermarks[source_id]
            if watermark == "!unavailable":
                continue
            try:
                page = await source.read_page(
                    identities[source_id],
                    cursor=source_cursors[source_id],
                    watermark=watermark,
                    limit=limit,
                )
            except Exception as exc:
                gaps.append(
                    CoverageGap(
                        CoverageGapCode.SOURCE_UNAVAILABLE,
                        source_id,
                        detail=f"page read failed ({type(exc).__name__})",
                    )
                )
                continue
            if not isinstance(page, TimelineSourcePage):
                gaps.append(CoverageGap(CoverageGapCode.INVALID_PAGE, source_id, detail="source returned an unknown page type"))
                continue
            pages[source_id] = page
            gaps.extend(page.gaps)
            if page.truncated:
                gaps.append(CoverageGap(CoverageGapCode.TRUNCATED, source_id, detail="source page is truncated"))
            if page.scan_limit_hit:
                gaps.append(CoverageGap(CoverageGapCode.SCAN_LIMIT_HIT, source_id, detail="source scan limit reached"))
            if page.non_atomic:
                gaps.append(CoverageGap(CoverageGapCode.NON_ATOMIC, source_id, detail="source could not provide an atomic page"))
            source_facts = list(page.facts)
            if len(source_facts) > limit:
                source_facts = source_facts[:limit]
                gaps.append(CoverageGap(CoverageGapCode.INVALID_PAGE, source_id, detail="source exceeded the requested page bound"))
                invalid_sources.add(source_id)
            next_cursor = page.next_cursor
            if next_cursor is not None:
                after_cursor = source_cursors[source_id]
                if isinstance(watermark, int) and (
                    not isinstance(next_cursor, int)
                    or next_cursor > watermark
                    or (isinstance(after_cursor, int) and next_cursor < after_cursor)
                ):
                    gaps.append(CoverageGap(CoverageGapCode.INVALID_PAGE, source_id, detail="page next_cursor violates its fixed sequence boundary"))
                    invalid_sources.add(source_id)
                if isinstance(watermark, int) and isinstance(next_cursor, int):
                    max_fact_sequence = max(
                        (int(item.sequence) for item in source_facts if item.sequence is not None),
                        default=after_cursor if isinstance(after_cursor, int) else -1,
                    )
                    if next_cursor < max_fact_sequence:
                        gaps.append(CoverageGap(CoverageGapCode.INVALID_PAGE, source_id, detail="page next_cursor precedes a returned fact"))
                        invalid_sources.add(source_id)
            ordered = bool(getattr(source, "sequence_ordered", False))
            if not ordered:
                gaps.append(CoverageGap(CoverageGapCode.SEQUENCE_UNAVAILABLE, source_id, detail="source has no durable sequence; display uses stable references"))
            seen_sequences: set[int] = set()
            last_sequence = source_cursors[source_id] if isinstance(source_cursors[source_id], int) else None
            last_reference = str(source_cursors[source_id] or "") if not ordered else ""
            page_start = len(candidates)
            for fact in source_facts:
                if fact.source_id != source_id:
                    gaps.append(CoverageGap(CoverageGapCode.INVALID_PAGE, source_id, fact.reference, "fact source_id does not match its reader"))
                    invalid_sources.add(source_id)
                    continue
                if ordered and fact.sequence is None:
                    gaps.append(CoverageGap(CoverageGapCode.INVALID_PAGE, source_id, fact.reference, "sequence-ordered source omitted sequence"))
                    invalid_sources.add(source_id)
                    continue
                if not ordered and fact.sequence is not None:
                    gaps.append(CoverageGap(CoverageGapCode.INVALID_PAGE, source_id, fact.reference, "unsequenced source claimed a sequence"))
                    invalid_sources.add(source_id)
                    continue
                if ordered and fact.sequence is not None:
                    if fact.source_cursor != fact.sequence:
                        gaps.append(CoverageGap(CoverageGapCode.INVALID_PAGE, source_id, fact.reference, "sequence cursor must equal the durable sequence"))
                        invalid_sources.add(source_id)
                        continue
                    if fact.sequence in seen_sequences:
                        gaps.append(CoverageGap(CoverageGapCode.INVALID_PAGE, source_id, fact.reference, "duplicate source sequence"))
                        invalid_sources.add(source_id)
                        continue
                    seen_sequences.add(fact.sequence)
                    if isinstance(watermark, int) and fact.sequence > watermark:
                        gaps.append(CoverageGap(CoverageGapCode.INVALID_PAGE, source_id, fact.reference, "fact exceeds the fixed watermark"))
                        invalid_sources.add(source_id)
                        continue
                    after = source_cursors[source_id]
                    if isinstance(after, int) and fact.sequence <= after:
                        gaps.append(CoverageGap(CoverageGapCode.DUPLICATE_REFERENCE, source_id, fact.reference, "fact did not advance its source cursor"))
                        invalid_sources.add(source_id)
                        continue
                    if last_sequence is not None and fact.sequence <= last_sequence:
                        gaps.append(CoverageGap(CoverageGapCode.INVALID_PAGE, source_id, fact.reference, "source page is not ordered by sequence"))
                        invalid_sources.add(source_id)
                        continue
                    last_sequence = fact.sequence
                elif not ordered:
                    if fact.reference <= last_reference:
                        gaps.append(CoverageGap(CoverageGapCode.INVALID_PAGE, source_id, fact.reference, "unsequenced source page is not ordered by reference"))
                        invalid_sources.add(source_id)
                        continue
                    last_reference = fact.reference
                candidates.append(fact)
            if source_id in invalid_sources:
                # Do not advance past a malformed page: a caller must see the source gap and
                # retry from the previous source cursor after fixing its read adapter.
                del candidates[page_start:]

        known_prior = set(previous_refs)
        current_by_ref: dict[str, TimelineFact] = {}
        unique_candidates: list[TimelineFact] = []
        for fact in candidates:
            if fact.reference in current_by_ref or fact.reference in known_prior:
                gaps.append(CoverageGap(CoverageGapCode.DUPLICATE_REFERENCE, fact.source_id, fact.reference, "reference was already present in this view"))
                continue
            current_by_ref[fact.reference] = fact
            unique_candidates.append(fact)

        ordered_facts = _causal_order(unique_candidates, known_prior, gaps)
        selected = ordered_facts[:limit]
        next_cursors = dict(source_cursors)
        selected_by_source: dict[str, int] = {}
        for fact in selected:
            next_cursors[fact.source_id] = fact.source_cursor
            selected_by_source[fact.source_id] = selected_by_source.get(fact.source_id, 0) + 1
        candidate_counts: dict[str, int] = {}
        for fact in unique_candidates:
            candidate_counts[fact.source_id] = candidate_counts.get(fact.source_id, 0) + 1
        for source_id, page in pages.items():
            if source_id in invalid_sources:
                continue
            if selected_by_source.get(source_id, 0) == candidate_counts.get(source_id, 0):
                if page.next_cursor is not None:
                    next_cursors[source_id] = page.next_cursor
        next_refs = list(previous_refs)
        ref_limited = False
        for fact in selected:
            if len(next_refs) >= _MAX_REFERENCE_HISTORY:
                ref_limited = True
                break
            next_refs.append(fact.reference)
        if ref_limited:
            gaps.append(CoverageGap(CoverageGapCode.REFERENCE_HISTORY_LIMITED, detail="causal and duplicate-reference history reached its bound"))

        unselected = len(ordered_facts) > len(selected)
        incomplete_source = any(
            not page.exhausted or page.truncated or page.scan_limit_hit
            for page in pages.values()
        )
        has_more = (
            unselected
            or incomplete_source
            or bool(invalid_sources)
            or len(pages) != len(self._sources)
        )
        next_cursor = TimelineCursor(
            watermarks=watermarks,
            source_cursors=next_cursors,
            emitted_references=tuple(next_refs),
        )
        return TimelineReport(
            facts=tuple(selected), cursor=next_cursor, gaps=tuple(_dedupe_gaps(gaps)), has_more=has_more
        )


def _fact_key(fact: TimelineFact) -> tuple[str, int | str, str]:
    return (
        fact.source_id,
        fact.sequence if fact.sequence is not None else fact.reference,
        fact.reference,
    )


def _causal_order(
    facts: Sequence[TimelineFact], prior_references: set[str], gaps: list[CoverageGap]
) -> list[TimelineFact]:
    """Topologically order explicit predecessors; ties use source and local sequence only."""
    by_ref = {fact.reference: fact for fact in facts}
    outgoing: dict[str, set[str]] = {fact.reference: set() for fact in facts}
    indegree = {fact.reference: 0 for fact in facts}
    by_source: dict[str, list[TimelineFact]] = {}
    for fact in facts:
        by_source.setdefault(fact.source_id, []).append(fact)
        for predecessor in fact.causal_references:
            if predecessor in by_ref:
                if fact.reference not in outgoing[predecessor]:
                    outgoing[predecessor].add(fact.reference)
                    indegree[fact.reference] += 1
            elif predecessor not in prior_references:
                gaps.append(
                    CoverageGap(
                        CoverageGapCode.MISSING_RELATIONSHIP,
                        fact.source_id,
                        fact.reference,
                        f"causal predecessor {predecessor!r} is not in the current or prior page",
                    )
                )
    for source_id, source_facts in by_source.items():
        ordered = sorted(source_facts, key=_fact_key)
        for previous, current in zip(ordered, ordered[1:]):
            if current.reference not in outgoing[previous.reference]:
                outgoing[previous.reference].add(current.reference)
                indegree[current.reference] += 1

    ready: list[tuple[tuple[str, int | str, str], str]] = []
    for reference, count in indegree.items():
        if count == 0:
            heapq.heappush(ready, (_fact_key(by_ref[reference]), reference))
    out: list[TimelineFact] = []
    while ready:
        _, reference = heapq.heappop(ready)
        out.append(by_ref[reference])
        for child in sorted(outgoing[reference]):
            indegree[child] -= 1
            if indegree[child] == 0:
                heapq.heappush(ready, (_fact_key(by_ref[child]), child))
    if len(out) != len(facts):
        gaps.append(CoverageGap(CoverageGapCode.CAUSAL_CYCLE, detail="causal links contain a cycle; remaining facts use stable source ordering"))
        done = {fact.reference for fact in out}
        out.extend(sorted((fact for fact in facts if fact.reference not in done), key=_fact_key))
    return out


def _dedupe_gaps(gaps: Sequence[CoverageGap]) -> list[CoverageGap]:
    seen: set[tuple[str, str, str, str]] = set()
    out = []
    for gap in gaps:
        key = (gap.code.value, gap.source_id, gap.reference, gap.detail)
        if key not in seen:
            seen.add(key)
            out.append(gap)
    return out


def analyze_timeline(report: TimelineReport) -> TimelineAnalysis:
    """Aggregate only explicit measurements and root causes from the returned fact page."""
    observations: dict[MetricName, list[tuple[MetricObservation, str]]] = {}
    causes: list[RootCauseFact] = []
    for fact in report.facts:
        facts_metrics = list(fact.metrics)
        if (
            fact.kind == "acceptance"
            and fact.status in {"accepted", "rejected", "inconclusive"}
            and not any(item.name.value == fact.status for item in facts_metrics)
        ):
            facts_metrics.append(
                MetricObservation(
                    name=MetricName(fact.status), value=1, unit="count", coverage=MetricCoverage.COMPLETE
                )
            )
        if fact.kind == "approval_decision" and not any(
            item.name is MetricName.MANUAL_DECISION_COUNT for item in facts_metrics
        ):
            facts_metrics.append(
                MetricObservation(
                    name=MetricName.MANUAL_DECISION_COUNT,
                    value=1,
                    unit="count",
                    coverage=MetricCoverage.COMPLETE,
                )
            )
        for index, item in enumerate(facts_metrics):
            observations.setdefault(item.name, []).append((item, f"fact:{fact.reference}:{index}"))
        if fact.root_cause is not None:
            causes.append(
                RootCauseFact(
                    category=fact.root_cause,
                    source_id=fact.source_id,
                    reference=fact.reference,
                    source_kind=fact.source_kind,
                )
            )

    aggregates: list[MetricAggregate] = []
    for name in MetricName:
        raw_items = observations.get(name, [])
        if not raw_items:
            continue
        unique: dict[str, MetricObservation] = {}
        conflict_keys: set[str] = set()
        duplicate_rows = 0
        conflicting_rows = 0
        for item, local_key in raw_items:
            key = item.dedupe_key or local_key
            existing = unique.get(key)
            if key in conflict_keys:
                conflicting_rows += 1
                continue
            if existing is None:
                unique[key] = item
                continue
            duplicate_rows += 1
            if existing.to_record() != item.to_record():
                unique.pop(key, None)
                conflict_keys.add(key)
                conflicting_rows += 1
        items = list(unique.values())
        conflicting = bool(conflict_keys)
        units = {item.unit for item in items}
        values = [float(item.value) for item in items if item.value is not None]
        known = len(values)
        unknown = len(items) - known + len(conflict_keys)
        unit = next(iter(units)) if len(units) == 1 else "mixed"
        total = sum(values) if known and len(units) == 1 and not conflicting else None
        if total is not None and not math.isfinite(total):
            total = None
        mean = total / known if total is not None and known else None
        if conflicting or len(units) > 1:
            coverage = MetricCoverage.PARTIAL
        elif all(item.coverage is MetricCoverage.COMPLETE for item in items):
            coverage = MetricCoverage.COMPLETE
        elif all(item.coverage is MetricCoverage.UNKNOWN for item in items):
            coverage = MetricCoverage.UNKNOWN
        else:
            coverage = MetricCoverage.PARTIAL
        aggregates.append(
            MetricAggregate(
                name=name,
                unit=unit,
                observations=len(items) + len(conflict_keys),
                known_observations=known,
                unknown_observations=unknown,
                total=total,
                mean=mean,
                minimum=min(values) if values and len(units) == 1 and not conflicting else None,
                maximum=max(values) if values and len(units) == 1 and not conflicting else None,
                coverage=coverage,
                duplicate_rows=duplicate_rows,
                conflicting_rows=conflicting_rows,
            )
        )
    return TimelineAnalysis(tuple(aggregates), tuple(causes))


def timeline_report_to_json(report: TimelineReport) -> str:
    return json.dumps(report.to_record(), ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)


def timeline_report_to_markdown(report: TimelineReport) -> str:
    lines = ["# Correlated timeline", "", "| source | sequence | kind | status | reference | summary |", "| --- | ---: | --- | --- | --- | --- |"]
    for fact in report.facts:
        lines.append(
            "| " + " | ".join(
                escape_markdown(value)
                for value in (
                    fact.source_id,
                    "unknown" if fact.sequence is None else str(fact.sequence),
                    fact.kind,
                    fact.status,
                    fact.reference,
                    sanitise_text(fact.summary),
                )
            ) + " |"
        )
    lines.append("")
    lines.append("- fixed watermarks: " + escape_markdown(json.dumps(dict(report.cursor.watermarks), sort_keys=True)))
    lines.append("- source cursors: " + escape_markdown(json.dumps(dict(report.cursor.source_cursors), sort_keys=True)))
    lines.append(f"- more facts may exist: {'yes' if report.has_more else 'no'}")
    analysis = analyze_timeline(report)
    if analysis.metrics:
        lines.extend(["", "## Metrics", "", "| metric | value | known / observed | coverage |", "| --- | ---: | ---: | --- |"])
        for item in analysis.metrics:
            value = "unknown" if item.total is None else f"{item.total:g} {item.unit}"
            lines.append(
                "| " + " | ".join(
                    escape_markdown(value)
                    for value in (
                        item.name.value,
                        value,
                        f"{item.known_observations} / {item.observations}",
                        item.coverage.value,
                    )
                ) + " |"
            )
    if analysis.root_causes:
        lines.extend(["", "## Deterministic root causes", ""])
        lines.extend(
            f"- `{item.category.value}` from `{escape_markdown(item.source_id)}` "
            f"reference `{escape_markdown(item.reference)}` (inferred: no)"
            for item in analysis.root_causes
        )
    if report.gaps:
        lines.extend(["", "## Coverage gaps", ""])
        lines.extend(
            f"- `{gap.code.value}` source `{escape_markdown(gap.source_id)}` "
            f"reference `{escape_markdown(gap.reference)}`: {escape_markdown(sanitise_text(gap.detail))}"
            for gap in report.gaps
        )
    return "\n".join(lines) + "\n"
