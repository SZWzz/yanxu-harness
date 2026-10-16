"""The durable reference store: one SQLite file, real transactions, real fences.

This is the store a restart example must use; the in-memory one says out loud that
it is volatile. It exists to make three properties *provable* rather than
asserted:

**Compare-and-set.** ``save_run`` names the version it read, and the update is a
single ``UPDATE … WHERE version = ?`` whose row count decides success. A lost
race is a :class:`StorageConflict`, never a silent overwrite.

**Fences.** Every lease issuance increments a counter, and a save carrying an
older fence is refused *inside the same transaction* as the write. A worker that
lost its lease therefore cannot land a late write at all, even if it never
noticed it lost the lease.

**One transaction per transition.** :meth:`SqlitePersistence.commit_transition`
writes run state, the lease check and the events in a single
``BEGIN IMMEDIATE`` … ``COMMIT``. Events are fanned out *after* the commit; a
failed fanout is repaired by replay, never by rolling back a business fact that
already happened.

The store opens one connection per process and guards it with a lock, running
each operation in a worker thread so an async caller does not block its event
loop. It is still a *single-writer* design: that is honest for a local-first
package, and ``BEGIN IMMEDIATE`` makes two processes contend correctly rather
than corrupting each other.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
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

__all__ = ["SqlitePersistence", "SCHEMA_VERSION"]

#: Schema version of this store's own tables. Bumping it requires a migration and
#: a refusal path for databases written by a newer build.
SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS harness_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    state TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS leases (
    resource TEXT PRIMARY KEY,
    holder TEXT NOT NULL,
    token TEXT NOT NULL,
    fencing_token INTEGER NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    sequence INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    run_id TEXT,
    at REAL NOT NULL
);
"""


class SqlitePersistence:
    """A durable implementation of the persistence port."""

    def __init__(
        self, path: str | Path, *, timeout: float = 5.0, read_only: bool = False
    ) -> None:
        """Open the store. ``read_only=True`` opens an existing file without touching it.

        A reader must not be able to change what it reads: read-only mode does not set the
        journal mode, does not create the file, does not create or migrate a table, and
        every write path below refuses. The schema version is still *checked*, because
        reading a newer layout hopefully is how a report ends up describing the wrong run.
        """
        self.path = Path(path)
        self.read_only = bool(read_only)
        self._lock = threading.RLock()
        if self.read_only:
            if str(self.path) == ":memory:" or not self.path.exists():
                raise ContractError(
                    f"no runs store at {self.path}; a reader does not create one"
                )
            self._connection = sqlite3.connect(
                f"file:{self.path}?mode=ro",
                uri=True,
                timeout=timeout,
                isolation_level=None,
                check_same_thread=False,
            )
            self._require_schema_version()
            return
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            str(self.path), timeout=timeout, isolation_level=None, check_same_thread=False
        )
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.executescript(_SCHEMA)
        self._register_schema_version()

    def _require_schema_version(self) -> None:
        """Verify the stored schema version without writing anything."""
        from ..versioning import require_version

        row = self._connection.execute(
            "SELECT value FROM harness_meta WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            raise ContractError(
                f"{self.path} has no recorded storage schema version, so this build cannot "
                "tell whether it understands the layout; refusing to guess at it"
            )
        require_version("harness storage schema", json.loads(row[0]), (SCHEMA_VERSION,))

    # -- lifecycle ------------------------------------------------------

    def _register_schema_version(self) -> None:
        from ..versioning import require_version

        row = self._connection.execute(
            "SELECT value FROM harness_meta WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            self._connection.execute(
                "INSERT INTO harness_meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            return
        # A database written by a newer build is refused, not "read hopefully".
        require_version("harness storage schema", json.loads(row[0]), (SCHEMA_VERSION,))

    @property
    def durability(self) -> Durability:
        return Durability.DURABLE

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "SqlitePersistence":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- transactions ---------------------------------------------------

    def _begin(self) -> None:
        self._connection.execute("BEGIN IMMEDIATE")

    def _commit(self) -> None:
        self._connection.execute("COMMIT")

    def _rollback(self) -> None:
        self._connection.execute("ROLLBACK")

    def _in_transaction(self, work: Any) -> Any:
        if self.read_only:
            raise ContractError(
                f"{self.path} was opened read-only: a reader must not write to what it reads"
            )
        with self._lock:
            self._begin()
            try:
                result = work()
            except BaseException:
                self._rollback()
                raise
            self._commit()
            return result

    def _read(self, work: Any) -> Any:
        with self._lock:
            return work()

    # -- run state ------------------------------------------------------

    def _load_run(self, run_id: str) -> VersionedRun | None:
        row = self._connection.execute(
            "SELECT version, state, updated_at FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        return VersionedRun(
            run_id=run_id, version=int(row[0]), state=json.loads(row[1]), updated_at=float(row[2])
        )

    def _save_run(
        self,
        run_id: str,
        state: Mapping[str, Any],
        *,
        expected_version: int | None,
        lease: Lease | None,
    ) -> int:
        return self._in_transaction(
            lambda: self._save_run_locked(
                run_id, state, expected_version=expected_version, lease=lease
            )
        )

    def _assert_lease(self, run_id: str, lease: Lease) -> None:
        row = self._connection.execute(
            "SELECT holder, token, fencing_token, expires_at FROM leases WHERE resource = ?",
            (lease.resource,),
        ).fetchone()
        if row is None:
            raise StorageConflict(run_id, "lease is no longer held")
        holder, token, fencing, expires_at = row
        if not token:
            raise StorageConflict(run_id, "lease was released")
        current = Lease(
            resource=lease.resource,
            holder=holder,
            token=token,
            fencing_token=int(fencing),
            expires_at=float(expires_at),
        )
        if token != lease.token:
            raise StorageConflict(run_id, "lease was reassigned (fencing): a newer worker owns this run")
        if lease.fencing_token != current.fencing_token:
            raise StorageConflict(run_id, "stale fencing token")
        if not lease_is_valid(current):
            raise StorageConflict(run_id, "lease expired before the write landed")

    # -- leases ---------------------------------------------------------

    def _claim_lease(
        self, resource: str, holder: str, *, ttl_seconds: float, now: float | None
    ) -> Lease | None:
        if ttl_seconds <= 0:
            raise ContractError("ttl_seconds must be positive")
        moment = time.time() if now is None else now

        def work() -> Lease | None:
            row = self._connection.execute(
                "SELECT holder, token, fencing_token, expires_at FROM leases WHERE resource = ?",
                (resource,),
            ).fetchone()
            if row is not None and row[1]:
                existing = Lease(
                    resource=resource,
                    holder=row[0],
                    token=row[1],
                    fencing_token=int(row[2]),
                    expires_at=float(row[3]),
                )
                if lease_is_valid(existing, now=moment):
                    return None
            # The counter is monotonic **across releases**: a released lease leaves
            # its row behind with an empty token so the next holder gets a strictly
            # higher fence. Deleting the row would hand fence 1 out twice, and a
            # stale worker holding fence 1 would then be able to write again.
            fencing = (int(row[2]) if row is not None else 0) + 1
            lease = Lease(
                resource=resource,
                holder=holder,
                token=uuid.uuid4().hex,
                fencing_token=fencing,
                expires_at=moment + ttl_seconds,
            )
            self._connection.execute(
                "INSERT INTO leases(resource, holder, token, fencing_token, expires_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(resource) DO UPDATE SET holder=excluded.holder, token=excluded.token, "
                "fencing_token=excluded.fencing_token, expires_at=excluded.expires_at",
                (resource, lease.holder, lease.token, lease.fencing_token, lease.expires_at),
            )
            return lease

        return self._in_transaction(work)

    def _release_lease(self, lease: Lease) -> None:
        def work() -> None:
            # Empty the claim but keep the fence counter.
            self._connection.execute(
                "UPDATE leases SET holder = '', token = '', expires_at = 0 "
                "WHERE resource = ? AND token = ?",
                (lease.resource, lease.token),
            )

        self._in_transaction(work)

    # -- events ---------------------------------------------------------

    def _append_events(self, events: Sequence[StoredEvent]) -> tuple[StoredEvent, ...]:
        return self._in_transaction(lambda: self._append_events_locked(events))

    def _read_events(self, after: int, limit: int) -> tuple[StoredEvent, ...]:
        if limit <= 0:
            raise ContractError("limit must be positive")
        rows = self._connection.execute(
            "SELECT sequence, kind, payload, run_id, at FROM events WHERE sequence > ? "
            "ORDER BY sequence LIMIT ?",
            (after, limit),
        ).fetchall()
        return tuple(
            StoredEvent(
                sequence=int(row[0]),
                kind=row[1],
                payload=json.loads(row[2]),
                run_id=row[3],
                at=float(row[4]),
            )
            for row in rows
        )

    # -- one transition, one transaction --------------------------------

    def _commit_transition(
        self,
        run_id: str,
        state: Mapping[str, Any],
        *,
        expected_version: int | None,
        lease: Lease | None = None,
        events: Sequence[StoredEvent] = (),
    ) -> tuple[int, tuple[StoredEvent, ...]]:
        """Write run state **and** its events in one transaction.

        This is the operation the durable-execution contract is about: a reader
        either sees both the state change and the events, or neither. The fanout
        happens after this returns, so a lost fanout is repaired by replay.

        The ``_..._locked`` helpers below are shared with the single-write
        methods so both paths run the *same* CAS and lease checks inside their own
        transaction rather than nesting ``BEGIN IMMEDIATE``.
        """
        with self._lock:
            self._begin()
            try:
                version = self._save_run_locked(
                    run_id, state, expected_version=expected_version, lease=lease
                )
                written = self._append_events_locked(events)
            except BaseException:
                self._rollback()
                raise
            self._commit()
            return version, written

    def _save_run_locked(
        self,
        run_id: str,
        state: Mapping[str, Any],
        *,
        expected_version: int | None,
        lease: Lease | None,
    ) -> int:
        if lease is not None:
            self._assert_lease(run_id, lease)
        payload = json.dumps(dict(state), ensure_ascii=False, sort_keys=True)
        now = time.time()
        if expected_version is None:
            try:
                self._connection.execute(
                    "INSERT INTO runs(run_id, version, state, updated_at) VALUES (?, 1, ?, ?)",
                    (run_id, payload, now),
                )
            except sqlite3.IntegrityError as exc:
                raise StorageConflict(run_id, "run already exists; expected_version was None") from exc
            return 1
        cursor = self._connection.execute(
            "UPDATE runs SET version = version + 1, state = ?, updated_at = ? "
            "WHERE run_id = ? AND version = ?",
            (payload, now, run_id, expected_version),
        )
        if cursor.rowcount != 1:
            current = self._load_run(run_id)
            raise StorageConflict(
                run_id,
                f"stored version {current.version if current else None} does not match "
                f"expected {expected_version}",
            )
        return expected_version + 1

    def _append_events_locked(self, events: Sequence[StoredEvent]) -> tuple[StoredEvent, ...]:
        written: list[StoredEvent] = []
        for event in events:
            at = event.at or time.time()
            # Sequences start at 0 and never repeat, so ``read_events(after=-1)``
            # means "from the beginning" in both reference stores.
            next_sequence = int(
                self._connection.execute(
                    "SELECT COALESCE(MAX(sequence), -1) + 1 FROM events"
                ).fetchone()[0]
            )
            self._connection.execute(
                "INSERT INTO events(sequence, kind, payload, run_id, at) VALUES (?, ?, ?, ?, ?)",
                (
                    next_sequence,
                    event.kind,
                    json.dumps(dict(event.payload), ensure_ascii=False, sort_keys=True),
                    event.run_id,
                    at,
                ),
            )
            written.append(
                StoredEvent(
                    sequence=next_sequence,
                    kind=event.kind,
                    payload=dict(event.payload),
                    run_id=event.run_id,
                    at=at,
                )
            )
        return tuple(written)

    # -- async port -----------------------------------------------------

    async def load_run(self, run_id: str) -> VersionedRun | None:
        return await asyncio.to_thread(self._read, lambda: self._load_run(run_id))

    async def save_run(
        self,
        run_id: str,
        state: Mapping[str, Any],
        *,
        expected_version: int | None,
        lease: Lease | None = None,
    ) -> int:
        return await asyncio.to_thread(
            lambda: self._save_run(
                run_id, state, expected_version=expected_version, lease=lease
            )
        )

    async def claim_lease(
        self, resource: str, holder: str, *, ttl_seconds: float, now: float | None = None
    ) -> Lease | None:
        return await asyncio.to_thread(
            lambda: self._claim_lease(resource, holder, ttl_seconds=ttl_seconds, now=now)
        )

    async def release_lease(self, lease: Lease) -> None:
        await asyncio.to_thread(self._release_lease, lease)

    async def append_events(self, events: Sequence[StoredEvent]) -> Sequence[StoredEvent]:
        return await asyncio.to_thread(self._append_events, tuple(events))

    async def read_events(self, *, after: int = -1, limit: int = 200) -> Sequence[StoredEvent]:
        return await asyncio.to_thread(self._read_events, after, limit)

    async def commit_transition(
        self,
        run_id: str,
        state: Mapping[str, Any],
        *,
        expected_version: int | None,
        lease: Lease | None = None,
        events: Sequence[StoredEvent] = (),
    ) -> tuple[int, Sequence[StoredEvent]]:
        """The async form of :meth:`_commit_transition`."""
        return await asyncio.to_thread(
            lambda: self._commit_transition(
                run_id,
                state,
                expected_version=expected_version,
                lease=lease,
                events=tuple(events),
            )
        )
