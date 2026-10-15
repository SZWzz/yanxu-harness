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

from ..errors import ContractError
from ..versioning import fingerprint
from .policy import TranscriptEntry

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

    def is_stale(self, archive_head: str) -> bool:
        return self.source_head != archive_head


@dataclass
class WindowedProjector:
    """Keep the most recent entries that fit a character budget.

    Deliberately crude about *what* it keeps (a real host order-projects by
    importance, POV and scope) and deliberately strict about two things:

    * it never mutates the archive it reads;
    * it reports how many entries it dropped, so a caller cannot mistake a
      projection for the whole history.
    """

    char_budget: int = 32000
    keep_system_entries: bool = True
    _cache: dict[str, ProjectionSnapshot] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.char_budget <= 0:
            raise ContractError("char_budget must be positive")

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
        budget = self.char_budget if capacity is None else min(self.char_budget, capacity)
        kept: list[Mapping[str, Any]] = []
        used = 0
        dropped = 0
        for entry in reversed(tuple(archive)):
            size = len(str(entry.payload.get("content", "")))
            if kept and used + size > budget:
                dropped = entry.sequence + 1
                break
            kept.append(dict(entry.payload))
            used += size
        kept.reverse()
        snapshot = ProjectionSnapshot(
            scope=scope,
            source_head=source_head,
            messages=tuple(kept),
            dropped_entries=len(archive) - len(kept),
            estimated_tokens=used // 4,
        )
        self._cache[scope] = snapshot
        return snapshot

    def cached(self, scope: str) -> ProjectionSnapshot | None:
        return self._cache.get(scope)
