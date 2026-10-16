"""The read-only side: gather the facts about one run, and nothing else.

An inspector is a **reader**. It never starts, resumes or cancels a run, never advances a
reducer and performs an action, never claims a lease, never invokes a tool and never calls
a host reconciler. The stores it is handed are opened read-only, so "it did not write" is a
property of the connection rather than a promise in a docstring.

Three boundaries shape the code:

* **the run's identity is checked, not assumed.** ``run_id`` alone is not permission:
  the record's namespace and session must match the identity the caller presented, or the
  read is refused. A report that leaks another tenant's run is worse than no report;
* **pagination is global.** Events are read from the store's global cursor, so a page that
  contains no event of the target run still advances the position. A reader that stopped at
  the first page full of other runs' events would silently under-report;
* **what could not be read is reported.** A ledger without a read capability, a missing
  projection record, an event kind this build does not know and a revision that moved while
  reading all become explicit facts (``unsupported``/``integrity``/``non_atomic``) instead of
  being inferred or dropped.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Mapping

from ..errors import ContractError
from .projection import project_report
from .types import ReadBoundary, RunDiagnosticReport

__all__ = ["DiagnosticFacts", "RunInspector"]

#: Event kinds this build can explain. An event of another kind is still counted and
#: reported as a trace entry, but it is listed under ``unsupported`` so a reader knows the
#: report does not explain everything it saw.
KNOWN_EVENT_KINDS = frozenset(
    {
        "run_started",
        "turn_started",
        "text_delta",
        "thinking_delta",
        "tool_call_started",
        "tool_call_finished",
        "approval_requested",
        "run_suspended",
        "run_cancelled",
        "usage_reported",
        "run_finished",
        "error",
        "model_request_dispatched",
        "model_request_completed",
        "tool_call_observed",
    }
)


@dataclass(frozen=True)
class DiagnosticFacts:
    """Everything the reader could establish, with the boundary it read at.

    This is deliberately *raw*: the projection (a pure function in
    :mod:`yanxu_harness.diagnostics.projection`) decides what the facts mean. Keeping the
    two apart is what makes a report reproducible from the same facts and boundary.
    """

    namespace: str
    session_id: str
    run_id: str
    observer_attempt: str = ""
    record: Mapping[str, Any] | None = None
    revision: int | None = None
    events: tuple[Mapping[str, Any], ...] = ()
    ledger: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    ledger_capable: bool = False
    unsupported: tuple[str, ...] = ()
    integrity: tuple[str, ...] = ()
    boundary: ReadBoundary = field(default_factory=ReadBoundary)

    def replace(self, **changes: Any) -> "DiagnosticFacts":
        return replace(self, **changes)


class RunInspector:
    """Read one run's committed facts through a read-only connection."""

    def __init__(
        self,
        *,
        persistence: Any,
        ledger: Any | None = None,
        event_limit: int = 200,
        max_scan_pages: int = 20,
        max_events: int = 2000,
    ) -> None:
        if persistence is None:
            raise ContractError("a run inspector needs a persistence store to read")
        if event_limit <= 0:
            raise ContractError("event_limit must be positive")
        if max_scan_pages <= 0:
            raise ContractError("max_scan_pages must be positive")
        if max_events <= 0:
            raise ContractError("max_events must be positive")
        self._persistence = persistence
        self._ledger = ledger
        #: How many store rows one page may hold (the scan's step size).
        self._event_limit = event_limit
        self._max_scan_pages = max_scan_pages
        #: How many of *this run's* events one report may carry. Separate from the page size
        #: on purpose: a small page bounds how much is read at once, not how much is collected.
        self._max_events = max_events

    @property
    def read_only(self) -> bool:
        """True when every store this inspector holds refuses writes."""
        return bool(getattr(self._persistence, "read_only", False)) and (
            self._ledger is None or bool(getattr(self._ledger, "read_only", False))
        )

    async def facts(
        self,
        identity: Any,
        *,
        cursor: int | None = None,
        limit: int | None = None,
        max_scan_pages: int | None = None,
    ) -> DiagnosticFacts:
        """Read the run record, its events and its ledger rows, read-only."""
        if cursor is not None and cursor < -1:
            raise ContractError("a cursor cannot be less than -1 (which means 'from the start')")
        if limit is not None and limit <= 0:
            raise ContractError("the page limit must be positive when given")
        if max_scan_pages is not None and max_scan_pages <= 0:
            raise ContractError("max_scan_pages must be positive when given")
        run_id = getattr(identity, "run_id", "")
        namespace = getattr(identity, "namespace", "")
        session_id = getattr(identity, "session_id", "")
        unsupported: list[str] = []
        integrity: list[str] = []

        record, revision = await self._read_record(run_id)
        if record is None:
            raise ContractError(f"no run {run_id!r} in this store")
        # Identity is a check, not a formality: a run id is not permission to read a run.
        if str(record.get("namespace") or "") != namespace:
            raise ContractError(
                f"run {run_id!r} belongs to namespace {record.get('namespace')!r}, not "
                f"{namespace!r}; refusing to read it under this identity"
            )
        if session_id and str(record.get("session_id") or "") != session_id:
            raise ContractError(
                f"run {run_id!r} belongs to session {record.get('session_id')!r}, not "
                f"{session_id!r}; refusing to read it under this identity"
            )
        events, boundary, event_notes, unknown_kinds = await self._read_events(
            run_id,
            cursor=cursor,
            limit=limit,
            max_scan_pages=max_scan_pages,
            revision=revision,
        )
        integrity.extend(event_notes)
        if boundary.non_atomic:
            integrity.append(
                "the run was still being written while its events were read; the report is "
                "marked non-atomic"
            )
        if unknown_kinds:
            unsupported.append(
                "event kinds this build does not explain: " + ", ".join(sorted(unknown_kinds))
            )

        ledger_rows: dict[str, Mapping[str, Any]] = {}
        capable = self._ledger is not None and callable(getattr(self._ledger, "all_operations", None))
        if capable:
            ledger_rows = self._read_ledger(run_id, namespace)
        else:
            unsupported.append(
                "no read-only operation ledger was provided, so every operation reference is "
                "reported without the ledger's own view of it"
            )

        return DiagnosticFacts(
            namespace=namespace,
            session_id=session_id,
            run_id=run_id,
            observer_attempt=str(getattr(identity, "attempt_id", "") or ""),
            record=dict(record),
            revision=revision,
            events=events,
            ledger=ledger_rows,
            ledger_capable=capable,
            unsupported=tuple(unsupported),
            integrity=tuple(integrity),
            boundary=boundary,
        )

    async def inspect_run(
        self,
        identity: Any,
        *,
        cursor: int | None = None,
        limit: int | None = None,
        max_scan_pages: int | None = None,
    ) -> RunDiagnosticReport:
        """The report for one run: read the facts, then project them purely."""
        facts = await self.facts(
            identity, cursor=cursor, limit=limit, max_scan_pages=max_scan_pages
        )
        return project_report(facts)

    # -- reading --------------------------------------------------------

    async def _read_record(self, run_id: str) -> tuple[Mapping[str, Any] | None, int | None]:
        """Read the run record and its revision.

        The revision is read again after the event scan. If it moved, the report is marked
        ``non_atomic`` rather than assembled from two different moments — a reader of a live
        run cannot get a single instant, and claiming one would be the lie, not the mixture.
        """
        loaded = await self._load(run_id)
        if loaded is None:
            return None, None
        state, revision = loaded
        return state, revision

    async def _load(self, run_id: str) -> tuple[Mapping[str, Any], int] | None:
        loaded = await self._persistence.load_run(run_id)
        if loaded is None:
            return None
        state = getattr(loaded, "state", None)
        version = getattr(loaded, "version", None)
        if state is None:
            state = dict(loaded) if isinstance(loaded, Mapping) else None
        if state is None:
            raise ContractError(
                f"{type(loaded).__name__} does not expose a run state this build can read"
            )
        return dict(state), int(version or 0)

    async def _read_events(
        self,
        run_id: str,
        *,
        cursor: int | None,
        limit: int | None,
        max_scan_pages: int | None,
        revision: int | None,
    ) -> tuple[tuple[Mapping[str, Any], ...], ReadBoundary, list[str], set[str]]:
        """Walk the store's global cursor until the run's events are collected or a bound hits.

        Three positions are tracked and never conflated:

        * ``read_after``/``resume`` — where the next page is read from. It advances only past
          events this reader has *consumed*, which is what makes the returned cursor safe: a
          page that fills the collection stops the scan mid-page and leaves the rest of that
          page for the next call;
        * ``high_water`` — the furthest sequence the scan looked at, returned or not. It is
          what the report states as "how far the store was read";
        * ``last_carried`` — the last matching event the report actually carries, which is the
          position a continuation must start after.

        The cursor stays global on purpose: a page full of *other* runs' events still moves
        the position, so a reader never concludes "this run has no more events" from a page
        that simply belonged to someone else.
        """
        page_limit = limit or self._event_limit
        pages = max_scan_pages or self._max_scan_pages
        collect_limit = self._max_events
        read_after = -1 if cursor is None else cursor
        resume = read_after
        matched: list[Mapping[str, Any]] = []
        notes: list[str] = []
        unknown: set[str] = set()
        scanned_pages = 0
        scan_limit_hit = False
        truncated = False
        exhausted = False
        high_water = read_after
        last_carried: int | None = None

        while scanned_pages < pages:
            page = await self._persistence.read_events(after=resume, limit=page_limit)
            page = list(page or ())
            if not page:
                # The store returned nothing after this position: the scan really is at the end.
                exhausted = True
                break
            scanned_pages += 1
            collection_full = False
            for item in page:
                sequence = int(getattr(item, "sequence", 0) or 0)
                high_water = max(high_water, sequence)
                if str(getattr(item, "run_id", "") or "") != run_id:
                    # Not this run's fact, but the global cursor still crosses it.
                    resume = max(resume, sequence)
                    continue
                if len(matched) >= collect_limit:
                    # The report is full. This event, and the rest of the page, belong to the
                    # next read: consuming them here is exactly how a cursor skips them.
                    truncated = True
                    collection_full = True
                    break
                matched.append(_event_record(item))
                resume = max(resume, sequence)
                last_carried = sequence
            if collection_full:
                break
            if len(page) < page_limit:
                # A short page is the store's own "this is the end" signal.
                exhausted = True
                break
        else:
            scan_limit_hit = True
            notes.append(
                f"the scan stopped after {pages} page(s) without reaching the end of the "
                "event store; later events may exist"
            )
        for item in matched:
            kind = str(item.get("kind") or "")
            if kind and kind not in KNOWN_EVENT_KINDS:
                unknown.add(kind)

        if truncated:
            notes.append(
                f"more than {collect_limit} events match this run; the report carries the first "
                f"{len(matched)} and its cursor continues from the last one it carries"
            )

        revision_after = revision
        loaded = await self._load(run_id)
        if loaded is not None:
            revision_after = loaded[1]
        non_atomic = bool(revision is not None and revision_after != revision)
        boundary = ReadBoundary(
            cursor=read_after,
            high_water=high_water,
            # A truncated report continues from the last event it *carries*, not from the
            # last one it scanned: the caller must be able to pick up what was left out.
            next_cursor=(
                last_carried if truncated and last_carried is not None else high_water
            ),
            run_revision=revision_after,
            scanned_pages=scanned_pages,
            truncated=truncated,
            scan_limit_hit=scan_limit_hit,
            exhausted=exhausted,
            non_atomic=non_atomic,
            store_schema=_schema_versions(self._persistence),
        )
        return tuple(matched), boundary, notes, unknown

    def _read_ledger(self, run_id: str, namespace: str = "") -> Mapping[str, Mapping[str, Any]]:
        """The ledger's own rows for this run, read through its read-only capability.

        Scoped by namespace *and* run id: the ledger is one table for every tenant, so a
        filter on the run id alone would let one project's operation appear in another
        project's report.
        """
        rows = self._ledger.all_operations()
        out: dict[str, Mapping[str, Any]] = {}
        for item in rows or ():
            item = dict(item)
            if str(item.get("run_id") or "") != run_id:
                continue
            if namespace and str(item.get("namespace") or "") not in ("", namespace):
                continue
            operation_id = str(item.get("operation_id") or "")
            if operation_id:
                out[operation_id] = item
        return out


def _event_record(stored: Any) -> Mapping[str, Any]:
    """One stored event, as plain data. Payloads are copied, never interpreted here."""
    return {
        "sequence": int(getattr(stored, "sequence", 0) or 0),
        "kind": str(getattr(stored, "kind", "") or ""),
        "run_id": str(getattr(stored, "run_id", "") or ""),
        "at": getattr(stored, "at", None),
        "payload": dict(getattr(stored, "payload", None) or {}),
    }


def _schema_versions(persistence: Any) -> Mapping[str, str]:
    """The store's own schema versions, so a report says what it read."""
    versions = {"run_record": "1"}
    connection = getattr(persistence, "_connection", None)
    if connection is None:
        return versions
    try:
        row = connection.execute(
            "SELECT value FROM harness_meta WHERE key = 'schema_version'"
        ).fetchone()
    except Exception:  # noqa: BLE001 - a store without that table simply states less
        return versions
    if row is not None:
        versions["harness_storage"] = str(row[0])
    return versions
