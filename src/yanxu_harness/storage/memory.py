"""A volatile in-memory persistence: a test double and a working single-process store.

It implements the *whole* :class:`~yanxu_harness.storage.ports.Persistence` port
— compare-and-set versions, fencing leases, ordered events — because a fake that
skips those checks cannot be used to test them. What it does not do is survive a
restart, and it says so: :attr:`InMemoryPersistence.durability` is
``VOLATILE``. The restart example and any cross-process recovery test must use
the durable SQLite store instead; substituting this one would make a recovery
claim meaningless.

Fencing works the same way it does in a real store: every lease issuance
increments a monotonic counter per resource, and a save that carries an older
fence is refused even when the lease object itself still looks valid.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ..errors import ContractError
from .ports import (
    Durability,
    Lease,
    StorageConflict,
    StoredEvent,
    VersionedRun,
    lease_is_valid,
)

__all__ = ["InMemoryPersistence"]


@dataclass
class _ResourceState:
    lease: Lease | None = None
    fencing: int = 0


class InMemoryPersistence:
    """A volatile implementation of the persistence port."""

    def __init__(self) -> None:
        self._runs: dict[str, VersionedRun] = {}
        self._resources: dict[str, _ResourceState] = {}
        self._events: list[StoredEvent] = []

    # -- capability -----------------------------------------------------

    @property
    def durability(self) -> Durability:
        return Durability.VOLATILE

    # -- run state ------------------------------------------------------

    async def load_run(self, run_id: str) -> VersionedRun | None:
        return self._runs.get(run_id)

    async def save_run(
        self,
        run_id: str,
        state: Mapping[str, Any],
        *,
        expected_version: int | None,
        lease: Lease | None = None,
    ) -> int:
        current = self._runs.get(run_id)
        current_version = current.version if current is not None else None
        if expected_version is None:
            if current_version is not None:
                raise StorageConflict(run_id, "run already exists; expected_version was None")
        elif current_version != expected_version:
            raise StorageConflict(
                run_id, f"stored version {current_version!r} does not match expected {expected_version!r}"
            )
        if lease is not None:
            self._assert_lease(run_id, lease)
        version = (current_version or 0) + 1
        self._runs[run_id] = VersionedRun(
            run_id=run_id, version=version, state=dict(state), updated_at=time.time()
        )
        return version

    # -- leases ---------------------------------------------------------

    async def claim_lease(
        self, resource: str, holder: str, *, ttl_seconds: float, now: float | None = None
    ) -> Lease | None:
        if ttl_seconds <= 0:
            raise ContractError("ttl_seconds must be positive")
        moment = time.time() if now is None else now
        state = self._resources.setdefault(resource, _ResourceState())
        if state.lease is not None and lease_is_valid(state.lease, now=moment):
            return None
        state.fencing += 1
        lease = Lease(
            resource=resource,
            holder=holder,
            token=uuid.uuid4().hex,
            fencing_token=state.fencing,
            expires_at=moment + ttl_seconds,
        )
        state.lease = lease
        return lease

    async def release_lease(self, lease: Lease) -> None:
        state = self._resources.get(lease.resource)
        if state is None or state.lease is None:
            return
        if state.lease.token == lease.token:
            state.lease = None

    def _assert_lease(self, run_id: str, lease: Lease) -> None:
        state = self._resources.get(lease.resource)
        if state is None or state.lease is None:
            raise StorageConflict(run_id, "lease is no longer held")
        current = state.lease
        if current.token != lease.token:
            raise StorageConflict(
                run_id, "lease was reassigned (fencing): a newer worker owns this run"
            )
        if lease.fencing_token != current.fencing_token:
            raise StorageConflict(run_id, "stale fencing token")
        if not lease_is_valid(current):
            raise StorageConflict(run_id, "lease expired before the write landed")

    # -- events ---------------------------------------------------------

    async def append_events(self, events: Sequence[StoredEvent]) -> Sequence[StoredEvent]:
        written: list[StoredEvent] = []
        for event in events:
            sequence = len(self._events)
            stored = StoredEvent(
                sequence=sequence,
                kind=event.kind,
                payload=dict(event.payload),
                run_id=event.run_id,
                at=event.at or time.time(),
            )
            self._events.append(stored)
            written.append(stored)
        return tuple(written)

    async def read_events(self, *, after: int = -1, limit: int = 200) -> Sequence[StoredEvent]:
        if limit <= 0:
            raise ContractError("limit must be positive")
        return tuple(event for event in self._events if event.sequence > after)[:limit]
