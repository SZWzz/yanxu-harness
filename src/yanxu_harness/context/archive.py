"""A concrete archive and projector, so the separation is testable rather than stated.

The archive is append-only and the projector is a pure function of it. That split
is what makes compaction recoverable: a bad summary can be discarded because the
turns it was derived from are still there, byte for byte.

A projection also records the head it came from. If the head moved, the projection
is stale and must be rebuilt — publishing a stale one would silently drop the
turns that arrived meanwhile.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from ..errors import BudgetExceeded, ContractError
from ..versioning import fingerprint
from .policy import TranscriptEntry
from .projection import INSTRUCTIONS, message_fragments, visible_text

__all__ = ["InMemoryTranscriptArchive", "WindowedProjector", "ProjectionSnapshot"]


class InMemoryTranscriptArchive:
    """An append-only, in-memory transcript archive.

    ``append`` assigns the next sequence and a content fingerprint. Nothing in
    this class can rewrite or drop an entry: compaction produces a *projection*,
    never an archive edit.
    """

    def __init__(self) -> None:
        self._entries: list[TranscriptEntry] = []

    @property
    def head(self) -> str:
        if not self._entries:
            return fingerprint({"entries": 0})
        return fingerprint(
            {
                "entries": len(self._entries),
                "last": self._entries[-1].fingerprint,
            }
        )

    async def append(self, entry: TranscriptEntry) -> TranscriptEntry:
        stored = TranscriptEntry(
            sequence=len(self._entries),
            role=entry.role,
            payload=dict(entry.payload),
            at=entry.at,
            fingerprint=fingerprint({"role": entry.role, "payload": dict(entry.payload)}),
        )
        self._entries.append(stored)
        return stored

    async def append_all(self, entries: Sequence[TranscriptEntry]) -> tuple[TranscriptEntry, ...]:
        return tuple([await self.append(entry) for entry in entries])

    async def read(self, *, after: int = -1, limit: int = 200) -> Sequence[TranscriptEntry]:
        if limit <= 0:
            raise ContractError("limit must be positive")
        return tuple(entry for entry in self._entries if entry.sequence > after)[:limit]

    def entries(self) -> tuple[TranscriptEntry, ...]:
        """Every archived entry, in order — what compaction does not get to change."""
        return tuple(self._entries)

    def __len__(self) -> int:
        return len(self._entries)


@dataclass(frozen=True)
class ProjectionSnapshot:
    """A projection plus the provenance needed to decide whether it is stale."""

    scope: str
    source_head: str
    messages: tuple[Mapping[str, Any], ...]
    dropped_entries: int = 0
    summary: str = ""
    estimated_tokens: int = 0
    #: What was measured: this projector counts characters of visible text, never tokens.
    method: str = "characters"
    #: Why material is missing, one line per dropped fragment.
    dropped_reasons: tuple[str, ...] = ()

    def is_stale(self, archive_head: str) -> bool:
        return self.source_head != archive_head


@dataclass
class WindowedProjector:
    """Keep the newest complete fragments that fit a character budget.

    This is the *legacy* view, kept for the archive-based ``ContextProjector`` protocol
    and for hosts that want nothing more than a character window. Its budget is a
    character budget — ``capacity`` is a character capacity, not a token capacity — and
    it measures the visible text of each fragment. The supported managed path is
    :class:`~yanxu_harness.context.projection.FragmentProjector`, which measures the real
    request in tokens, reconstructs the effective instruction baseline and verifies that
    the projection still means the same thing.

    What it is strict about, because these were real defects:

    * it never splits a fragment, so an assistant turn and the results of the calls it
      declared are selected together (no lone ``toolResult``, no orphaned call);
    * ``keep_system_entries`` is honoured — an instruction fragment is required, and when
      the required minimum does not fit it refuses with ``BudgetExceeded`` instead of
      silently dropping the instructions;
    * the current turn is required: a request without the newest user input or tool round
      trip is a different request, so a window too small for it is refused, never
      satisfied by an empty or older view;
    * its cache is bounded (``cache_limit``), because an unbounded cache in a long-lived
      process is a leak.
    """

    char_budget: int = 32000
    keep_system_entries: bool = True
    cache_limit: int = 64
    _cache: dict[str, ProjectionSnapshot] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.char_budget <= 0:
            raise ContractError("char_budget must be positive")
        if self.cache_limit <= 0:
            raise ContractError("cache_limit must be positive")

    def project(
        self,
        archive: Sequence[TranscriptEntry],
        *,
        scope: str,
        source_head: str,
        capacity: int | None = None,
    ) -> tuple[Mapping[str, Any], ...]:
        return self.snapshot(
            archive, scope=scope, source_head=source_head, capacity=capacity
        ).messages

    def snapshot(
        self,
        archive: Sequence[TranscriptEntry],
        *,
        scope: str,
        source_head: str,
        capacity: int | None = None,
    ) -> ProjectionSnapshot:
        """Select whole fragments from the newest backwards, or refuse.

        ``capacity`` is a character capacity (the ``ContextProjector`` protocol name);
        the budget is the smaller of it and ``char_budget``. A token capacity must not be
        passed here — it belongs to the measured projection.
        """
        if capacity is not None and capacity < 0:
            raise ContractError("capacity must not be negative")
        budget = self.char_budget if capacity is None else min(self.char_budget, capacity)
        entries = tuple(archive)
        payloads = []
        for entry in entries:
            payload = dict(entry.payload)
            payload.setdefault("role", entry.role)
            payloads.append(payload)
        fragments = message_fragments(payloads)
        if not fragments:
            snapshot = ProjectionSnapshot(
                scope=scope, source_head=source_head, messages=(), method="characters"
            )
            self._remember(scope, snapshot)
            return snapshot

        required = [
            self.keep_system_entries and fragment.kind == INSTRUCTIONS for fragment in fragments
        ]
        required[-1] = True

        first = 0
        reasons: list[str] = []
        while True:
            kept = [message for fragment in fragments[first:] for message in fragment.messages]
            used = sum(len(visible_text(message)) for message in kept)
            if used <= budget:
                break
            if first >= len(fragments) or required[first]:
                where = list(fragments[first].positions) if first < len(fragments) else []
                raise BudgetExceeded(
                    "context",
                    f"the required context is {used} characters but the character budget is "
                    f"{budget}; the fragment at positions {where} is required (the effective "
                    "instructions or the current turn) and cannot be dropped",
                )
            reasons.append(
                f"dropped the {fragments[first].kind} fragment at positions "
                f"{list(fragments[first].positions)} as the oldest removable one"
            )
            first += 1

        kept_positions = {position for fragment in fragments[first:] for position in fragment.positions}
        snapshot = ProjectionSnapshot(
            scope=scope,
            source_head=source_head,
            messages=tuple(
                payload for index, payload in enumerate(payloads) if index in kept_positions
            ),
            dropped_entries=len(entries) - len(kept_positions),
            estimated_tokens=sum(len(visible_text(message)) for message in payloads) // 4,
            method="characters",
            dropped_reasons=tuple(reasons),
        )
        self._remember(scope, snapshot)
        return snapshot

    def _remember(self, scope: str, snapshot: ProjectionSnapshot) -> None:
        if len(self._cache) >= self.cache_limit and scope not in self._cache:
            self._cache.pop(next(iter(self._cache)), None)
        self._cache[scope] = snapshot

    def cached(self, scope: str) -> ProjectionSnapshot | None:
        return self._cache.get(scope)
