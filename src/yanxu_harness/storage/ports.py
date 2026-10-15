"""Persistence ports: atomic state, compare-and-set versions, fencing leases.

The correctness of durable execution rests on three mechanisms, and none of them
is a lock:

**Compare-and-set.** A state save names the version it read. If the stored
version moved, the save is refused (:class:`StorageConflict`) rather than
overwriting a newer fact.

**Leases with fencing tokens.** A worker that lost its lease must not be able to
write afterwards. Each lease issuance increments a fence; a save carrying an
older fence is refused even though the worker still believes it holds the lease.

**One transaction per logical transition.** State, interaction, checkpoint and
terminal event are committed together, and events are published *after* the
commit. A failed event fanout is repaired by replay, never by rolling back a
business fact that was already committed.

The port deliberately does not promise cross-database transactions: an external
side effect and a local ledger cannot share a transaction, which is why
:mod:`yanxu_harness.tools.execution` has an ``unknown`` state at all.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from ..errors import HarnessError

__all__ = [
    "Durability",
    "Lease",
    "Persistence",
    "StorageConflict",
    "StoredEvent",
    "UnitOfWork",
    "VersionedRun",
    "lease_is_valid",
]


class Durability(str, Enum):
    """Whether a store survives a process restart. Never a marketing claim."""

    DURABLE = "durable"
    VOLATILE = "volatile"


class StorageConflict(HarnessError):
    """A compare-and-set, fence or lease check refused the write."""

    code = "storage_conflict"

    def __init__(self, what: str, detail: str = "") -> None:
        super().__init__(f"{what} conflict" + (f": {detail}" if detail else ""))
        self.what = what
        self.detail = detail


@dataclass(frozen=True)
class Lease:
    """A time-bounded claim on one run, with a monotonic fence."""

    resource: str
    holder: str
    token: str
    fencing_token: int
    expires_at: float

    def __post_init__(self) -> None:
        for name in ("resource", "holder", "token"):
            if not getattr(self, name):
                raise ValueError(f"lease requires a non-empty {name}")


def lease_is_valid(lease: Lease, *, holder: str | None = None, now: float | None = None) -> bool:
    """True when the lease is unexpired and (optionally) still held by ``holder``."""
    moment = time.time() if now is None else now
    if moment > lease.expires_at:
        return False
    if holder is not None and lease.holder != holder:
        return False
    return True


@dataclass(frozen=True)
class VersionedRun:
    """A stored run state and the version it was read at."""

    run_id: str
    version: int
    state: Mapping[str, Any]
    updated_at: float = 0.0


@dataclass(frozen=True)
class StoredEvent:
    """One persisted event, in the order the store assigned."""

    sequence: int
    kind: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    run_id: str | None = None
    at: float = 0.0


@runtime_checkable
class UnitOfWork(Protocol):
    """One atomic transition. Commit makes it visible; rollback discards it."""

    async def commit(self) -> None:
        """Commit every write made through this unit."""

    async def rollback(self) -> None:
        """Discard every write made through this unit."""


@runtime_checkable
class Persistence(Protocol):
    """The storage a host lends to the harness."""

    @property
    def durability(self) -> Durability:
        """``DURABLE`` only when a restart really keeps the data."""

    async def load_run(self, run_id: str) -> VersionedRun | None:
        """Read one run state, or ``None`` when it was never written."""

    async def save_run(
        self,
        run_id: str,
        state: Mapping[str, Any],
        *,
        expected_version: int | None,
        lease: Lease | None = None,
    ) -> int:
        """Save with compare-and-set semantics; return the new version."""

    async def claim_lease(
        self, resource: str, holder: str, *, ttl_seconds: float, now: float | None = None
    ) -> Lease | None:
        """Claim ``resource`` for ``holder``, or return ``None`` when it is held."""

    async def release_lease(self, lease: Lease) -> None:
        """Release a lease this process still holds. Idempotent."""

    async def append_events(self, events: Sequence[StoredEvent]) -> Sequence[StoredEvent]:
        """Append events and return them with their assigned sequence numbers."""

    async def read_events(self, *, after: int = -1, limit: int = 200) -> Sequence[StoredEvent]:
        """Read events with ``sequence > after``, oldest first."""

    async def commit_transition(
        self,
        run_id: str,
        state: Mapping[str, Any],
        *,
        expected_version: int | None,
        lease: Lease | None = None,
        events: Sequence[StoredEvent] = (),
    ) -> tuple[int, Sequence[StoredEvent]]:
        """Commit run state **and** its events in one transaction, returning both.

        This is the operation the durable-execution contract rests on: a reader
        either sees the state change and the events, or neither. It belongs to the
        port rather than being an optional extra, because a harness that had to
        choose between writing state and writing events would need a recovery rule
        for the gap — and that invented rule is where a doubled write lives.
        """
