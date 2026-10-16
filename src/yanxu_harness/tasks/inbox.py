"""The persistent inbox and control store: real transactions, real fences, real refusals.

This is the reference implementation of :class:`yanxu_harness.tasks.ports.CoordinationStore`. It
is deliberately built **on top of** the existing SQLite persistence rather than beside it: the
task's control facts and the run's own state live in one database file, one connection and one
``BEGIN IMMEDIATE`` … ``COMMIT``. That is what makes the boundary in plan §7.2 true rather than
aspirational — the run snapshot, the inputs marked ``applied`` and the control notices become
visible together, or none of them do.

Three things this module refuses to do, each for the same reason:

* **decide from values read outside the transaction.** Every admission re-reads the duplicate
  record, the task status and the backlog counters inside the write transaction, because a
  decision taken from an earlier read is a decision about a moment that has passed;
* **let a lost claim commit.** ``commit_run_transition_with_inputs`` checks the caller's fencing
  token inside the same transaction as the write. A worker that lost the right cannot land a late
  state change, even if it never noticed;
* **evict.** A full backlog refuses the *new* admission; an input that was already accepted stays
  accepted. Capacity is enforced by counting, including bytes.

The in-memory store (:class:`MemoryControlStore`) implements the same contract and says out loud
that it is volatile and single-process. It exists so the contract can be exercised without a file
and so a deployment has an honest choice, not as evidence of durability.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..errors import ContractError
from ..storage.ports import Lease, StoredEvent
from ..storage.sqlite import SqlitePersistence
from .ports import CoordinationStore  # noqa: F401 - the protocol this class implements
from .types import (
    BACKLOG_LIMITS,
    TASK_SCHEMA_VERSION,
    AdmissionOutcome,
    AdmissionResult,
    BacklogLimits,
    BudgetRefused,
    BudgetReservation,
    BudgetUnit,
    ChildRelation,
    ChildSpawnState,
    ClaimLost,
    ControlOutboxEntry,
    DispatchClaim,
    InputAdmission,
    InputContent,
    InputKind,
    InputRecord,
    InputState,
    RefusalCode,
    InboxMoved,
    ReservationState,
    TaskAdmission,
    TaskRecord,
    TaskStatus,
    decide_admission,
)

__all__ = [
    "CONTROL_SCHEMA_VERSION",
    "MemoryControlStore",
    "SqliteControlStore",
]

#: Version of the control tables. Bumping it requires a migration and a refusal path for a
#: database written by a newer build; a reader refuses rather than reading it hopefully.
CONTROL_SCHEMA_VERSION = 1
SUPPORTED_CONTROL_SCHEMA_VERSIONS = (1,)

_CONTROL_SCHEMA = """
CREATE TABLE IF NOT EXISTS control_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    namespace TEXT NOT NULL,
    session_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    stop_code TEXT NOT NULL DEFAULT '',
    priority INTEGER NOT NULL DEFAULT 0,
    root_task_id TEXT NOT NULL,
    parent_task_id TEXT NOT NULL DEFAULT '',
    depth INTEGER NOT NULL DEFAULT 0,
    profile TEXT NOT NULL DEFAULT '',
    workspace_ref TEXT NOT NULL DEFAULT '',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    current_attempt_id TEXT NOT NULL DEFAULT '',
    cancel_requested_at REAL,
    waiting_for TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    schema_version INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS tasks_schedulable ON tasks(status, priority DESC, created_at, task_id);
CREATE TABLE IF NOT EXISTS inputs (
    task_id TEXT NOT NULL,
    input_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    session_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    content_digest TEXT NOT NULL,
    content TEXT NOT NULL,
    state TEXT NOT NULL,
    source TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    claimed_by TEXT NOT NULL DEFAULT '',
    claim_expires_at REAL,
    applied_run_revision INTEGER,
    applied_event_sequence INTEGER,
    refusal_code TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    schema_version INTEGER NOT NULL,
    PRIMARY KEY (task_id, input_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS inputs_sequence ON inputs(task_id, sequence);
CREATE INDEX IF NOT EXISTS inputs_claimable ON inputs(task_id, state, sequence);
CREATE TABLE IF NOT EXISTS inbox_counters (
    task_id TEXT PRIMARY KEY,
    next_sequence INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    pending_count INTEGER NOT NULL,
    pending_bytes INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS task_claims (
    task_id TEXT PRIMARY KEY,
    holder TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    fencing_token INTEGER NOT NULL,
    claimed_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS claim_counters (
    task_id TEXT PRIMARY KEY,
    next_fencing_token INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS children (
    spawn_identity TEXT PRIMARY KEY,
    parent_task_id TEXT NOT NULL,
    child_task_id TEXT NOT NULL,
    namespace TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL,
    operation_id TEXT NOT NULL DEFAULT '',
    budget_reservation_id TEXT NOT NULL DEFAULT '',
    authorization_digest TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    schema_version INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS children_child ON children(child_task_id);
CREATE INDEX IF NOT EXISTS children_parent ON children(parent_task_id, created_at);
CREATE TABLE IF NOT EXISTS outbox (
    outbox_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    target_task_id TEXT NOT NULL,
    namespace TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL,
    created_at REAL NOT NULL,
    delivered_at REAL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    schema_version INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS outbox_pending ON outbox(delivered_at, target_task_id, created_at);
CREATE TABLE IF NOT EXISTS budgets (
    reservation_id TEXT PRIMARY KEY,
    root_task_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    unit TEXT NOT NULL,
    amount REAL NOT NULL,
    dispatch_identity TEXT NOT NULL,
    state TEXT NOT NULL,
    settled_amount REAL,
    estimated INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    settled_at REAL,
    schema_version INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS budgets_dispatch
    ON budgets(root_task_id, unit, dispatch_identity);
CREATE INDEX IF NOT EXISTS budgets_root ON budgets(root_task_id, unit, state);
"""


def _reference_parts(reference: Any, default_task_id: str = "") -> tuple[str, str]:
    """``(task_id, input_id)`` for an applied-input reference, mapping or record alike.

    The runner's poll references are plain data by design (they travel in a versioned snapshot),
    while a caller that already has an ``InputRecord`` should not have to convert it. Both are
    accepted here and nowhere else.
    """
    if isinstance(reference, Mapping):
        return (
            str(reference.get("task_id") or default_task_id),
            str(reference.get("input_id") or ""),
        )
    return (
        str(getattr(reference, "task_id", "") or default_task_id),
        str(getattr(reference, "input_id", "") or ""),
    )


def _task_from_row(row: sqlite3.Row | tuple) -> TaskRecord:
    return TaskRecord(
        namespace=row[1],
        session_id=row[2],
        task_id=row[0],
        run_id=row[3],
        status=TaskStatus(row[4]),
        reason=row[5],
        stop_code=row[6],
        priority=int(row[7]),
        root_task_id=row[8],
        parent_task_id=row[9],
        depth=int(row[10]),
        profile=row[11],
        workspace_ref=row[12],
        attempt_count=int(row[13]),
        current_attempt_id=row[14],
        cancel_requested_at=row[15],
        waiting_for=row[16],
        created_at=float(row[17]),
        updated_at=float(row[18]),
        schema_version=int(row[19]),
    )


_TASK_COLUMNS = (
    "task_id, namespace, session_id, run_id, status, reason, stop_code, priority, root_task_id, "
    "parent_task_id, depth, profile, workspace_ref, attempt_count, current_attempt_id, "
    "cancel_requested_at, waiting_for, created_at, updated_at, schema_version"
)


def _input_from_row(row: sqlite3.Row | tuple) -> InputRecord:
    raw_content = row[7]
    if isinstance(raw_content, str):
        raw_content = json.loads(raw_content)
    content = InputContent(
        kind=InputKind(row[4]),
        text=str((raw_content or {}).get("text") or ""),
        decision=str((raw_content or {}).get("decision") or ""),
        child_id=str((raw_content or {}).get("child_id") or ""),
        parameters=dict((raw_content or {}).get("parameters") or {}),
    )
    return InputRecord(
        namespace=row[2],
        session_id=row[3],
        task_id=row[0],
        input_id=row[1],
        kind=InputKind(row[4]),
        sequence=int(row[5]),
        content_digest=row[6],
        content=content,
        state=InputState(row[8]),
        source=row[9],
        attempts=int(row[10]),
        claimed_by=row[11],
        claim_expires_at=row[12],
        applied_run_revision=row[13],
        applied_event_sequence=row[14],
        refusal_code=row[15],
        created_at=float(row[16]),
        schema_version=int(row[17]),
    )


_INPUT_COLUMNS = (
    "task_id, input_id, namespace, session_id, kind, sequence, content_digest, content, state, "
    "source, attempts, claimed_by, claim_expires_at, applied_run_revision, applied_event_sequence, "
    "refusal_code, created_at, schema_version"
)


class SqliteControlStore(SqlitePersistence):
    """The SQLite reference control store: same file, same transaction, real fences.

    Subclassing :class:`~yanxu_harness.storage.sqlite.SqlitePersistence` is the point rather than a
    convenience: the control tables and the run tables share a connection and a transaction, so
    ``commit_run_transition_with_inputs`` can write the run state, mark the inputs applied and
    enqueue the notices in one commit. A separate database would make that a two-phase problem the
    plan explicitly refuses to pretend away.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        timeout: float = 5.0,
        read_only: bool = False,
        backlog: BacklogLimits | None = None,
    ) -> None:
        super().__init__(path, timeout=timeout, read_only=read_only)
        self.backlog = backlog or BACKLOG_LIMITS
        #: The task whose inputs a commit confirms when the applied references do not each carry
        #: their own task id (the runner's poll references are plain data by design).
        self._default_task_id = ""
        if self.read_only:
            self._require_control_schema()
        else:
            self._connection.executescript(_CONTROL_SCHEMA)
            self._register_control_schema()

    # -- schema ------------------------------------------------------------------------

    def _require_control_schema(self) -> None:
        """Check the control layout without creating or migrating anything."""
        from ..versioning import require_version

        try:
            row = self._connection.execute(
                "SELECT value FROM control_meta WHERE key = 'control_schema_version'"
            ).fetchone()
        except sqlite3.OperationalError:
            row = None
        if row is None:
            raise ContractError(
                f"{self.path} has no control schema, so this build cannot read tasks or inputs "
                "from it; a reader does not create one"
            )
        require_version(
            "task control schema", json.loads(row[0]), SUPPORTED_CONTROL_SCHEMA_VERSIONS
        )

    def _register_control_schema(self) -> None:
        from ..versioning import require_version

        row = self._connection.execute(
            "SELECT value FROM control_meta WHERE key = 'control_schema_version'"
        ).fetchone()
        if row is None:
            self._connection.execute(
                "INSERT INTO control_meta(key, value) VALUES ('control_schema_version', ?)",
                (str(CONTROL_SCHEMA_VERSION),),
            )
            return
        require_version(
            "task control schema", json.loads(row[0]), SUPPORTED_CONTROL_SCHEMA_VERSIONS
        )

    @property
    def control_schema_version(self) -> int:
        row = self._connection.execute(
            "SELECT value FROM control_meta WHERE key = 'control_schema_version'"
        ).fetchone()
        return CONTROL_SCHEMA_VERSION if row is None else int(json.loads(row[0]))

    # -- async port --------------------------------------------------------------------

    async def admit_task(self, admission: TaskAdmission) -> TaskRecord:
        return await asyncio.to_thread(self._admit_task, admission)

    async def admit_input(self, admission: InputAdmission) -> AdmissionResult:
        return await asyncio.to_thread(self._admit_input, admission)

    async def claim_task(
        self,
        task_id: str,
        *,
        holder: str,
        ttl_seconds: float,
        attempt_id: str = "",
        now: float | None = None,
    ) -> DispatchClaim | None:
        return await asyncio.to_thread(
            lambda: self._claim_task(
                task_id, holder=holder, ttl_seconds=ttl_seconds, attempt_id=attempt_id, now=now
            )
        )

    async def renew_task_claim(
        self,
        task_id: str,
        *,
        holder: str,
        attempt_id: str,
        ttl_seconds: float,
        now: float | None = None,
    ) -> DispatchClaim | None:
        return await asyncio.to_thread(
            lambda: self._renew_task_claim(
                task_id,
                holder=holder,
                attempt_id=attempt_id,
                ttl_seconds=ttl_seconds,
                now=now,
            )
        )

    async def release_task_claim(
        self, task_id: str, *, holder: str, attempt_id: str, now: float | None = None
    ) -> TaskRecord | None:
        return await asyncio.to_thread(
            lambda: self._release_task_claim(
                task_id, holder=holder, attempt_id=attempt_id, now=now
            )
        )

    async def commit_run_transition_with_inputs(
        self,
        run_id: str,
        state: Mapping[str, Any],
        events: Sequence[Any] = (),
        *,
        expected_version: int | None,
        applied_inputs: Sequence[Any] = (),
        notifications: Sequence[ControlOutboxEntry] = (),
        claim: DispatchClaim | None = None,
        lease: Lease | None = None,
        stop_dispatching: bool = False,
        require_inbox_revision: int | None = None,
        task_id: str = "",
    ) -> tuple[int, tuple[StoredEvent, ...]]:
        return await asyncio.to_thread(
            lambda: self._commit_run_transition_with_inputs(
                run_id,
                state,
                expected_version=expected_version,
                events=tuple(events),
                applied_inputs=tuple(applied_inputs),
                notifications=tuple(notifications),
                claim=claim,
                lease=lease,
                stop_dispatching=stop_dispatching,
                require_inbox_revision=require_inbox_revision,
                task_id=task_id or self._default_task_id,
            )
        )

    async def reserve_budget(
        self,
        *,
        root_task_id: str,
        task_id: str,
        reservation: BudgetReservation,
        claim: DispatchClaim | None = None,
        limit: float | None = None,
        now: float | None = None,
    ) -> BudgetReservation:
        return await asyncio.to_thread(
            lambda: self._reserve_budget(
                root_task_id=root_task_id,
                task_id=task_id,
                reservation=reservation,
                claim=claim,
                limit=limit,
                now=now,
            )
        )

    async def settle_budget(
        self,
        reservation_id: str,
        *,
        state: str,
        settled_amount: float | None = None,
        note: str = "",
        now: float | None = None,
    ) -> BudgetReservation:
        return await asyncio.to_thread(
            lambda: self._settle_budget(
                reservation_id,
                state=state,
                settled_amount=settled_amount,
                note=note,
                now=now,
            )
        )

    async def register_child(
        self,
        relation: ChildRelation,
        *,
        claim: DispatchClaim | None = None,
        reservation: BudgetReservation | None = None,
        notification: ControlOutboxEntry | None = None,
        limit: float | None = None,
        now: float | None = None,
    ) -> ChildRelation:
        return await asyncio.to_thread(
            lambda: self._register_child(
                relation,
                claim=claim,
                reservation=reservation,
                notification=notification,
                limit=limit,
                now=now,
            )
        )

    async def save_task(
        self, task: TaskRecord, *, expected_status: TaskStatus | None = None
    ) -> TaskRecord:
        return await asyncio.to_thread(self._save_task, task, expected_status)

    async def read_schedulable(
        self, *, limit: int = 64, now: float | None = None
    ) -> Sequence[TaskRecord]:
        return await asyncio.to_thread(self._read_schedulable, limit, now)

    async def read_task(self, task_id: str) -> TaskRecord | None:
        return await asyncio.to_thread(self._read_task, task_id)

    async def read_inputs(
        self, task_id: str, *, after_sequence: int = -1, limit: int = 200
    ) -> Sequence[InputRecord]:
        return await asyncio.to_thread(self._read_inputs, task_id, after_sequence, limit)

    async def read_children(self, parent_task_id: str) -> Sequence[ChildRelation]:
        return await asyncio.to_thread(self._read_children, parent_task_id)

    async def read_outbox(
        self, *, task_id: str | None = None, pending_only: bool = True, limit: int = 200
    ) -> Sequence[ControlOutboxEntry]:
        return await asyncio.to_thread(self._read_outbox, task_id, pending_only, limit)

    async def read_reservations(
        self, root_task_id: str, *, unit: BudgetUnit | str | None = None
    ) -> Sequence[BudgetReservation]:
        return await asyncio.to_thread(self._read_reservations, root_task_id, unit)

    async def deliver_outbox(self, outbox_id: str, *, at: float | None = None) -> bool:
        return await asyncio.to_thread(self._deliver_outbox, outbox_id, at)

    async def fail_outbox(self, outbox_id: str, *, error: str) -> bool:
        return await asyncio.to_thread(self._fail_outbox, outbox_id, error)

    async def read_inbox_revision(self, task_id: str) -> int:
        return await asyncio.to_thread(self._read_inbox_revision, task_id)

    # -- writes ------------------------------------------------------------------------

    def _admit_task(self, admission: TaskAdmission) -> TaskRecord:
        now = admission.created_at or time.time()

        def work() -> TaskRecord:
            existing = self._load_task_locked(admission.task_id)
            if existing is not None:
                if (
                    existing.namespace != admission.namespace
                    or existing.session_id != admission.session_id
                    or existing.run_id != admission.run_id
                ):
                    raise ContractError(
                        f"task {admission.task_id!r} already exists with run "
                        f"{existing.run_id!r} in {existing.namespace!r}/{existing.session_id!r}; "
                        "a repeated submit must not create a second task, a second run or a "
                        "second budget"
                    )
                return existing
            record = TaskRecord(
                namespace=admission.namespace,
                session_id=admission.session_id,
                task_id=admission.task_id,
                run_id=admission.run_id,
                status=TaskStatus.QUEUED,
                root_task_id=admission.root_task_id or admission.task_id,
                parent_task_id=admission.parent_task_id,
                depth=admission.depth,
                priority=admission.priority,
                profile=admission.profile,
                workspace_ref=admission.workspace_ref,
                created_at=now,
                updated_at=now,
            )
            self._insert_task_locked(record)
            self._connection.execute(
                "INSERT INTO inbox_counters(task_id, next_sequence, revision, pending_count, "
                "pending_bytes) VALUES (?, 0, 0, 0, 0)",
                (record.task_id,),
            )
            if admission.initial_input is not None:
                # The initial input and the task are created in the *same* transaction: a task
                # that exists without its opening instruction would be a run with nothing to do.
                self._insert_input_locked(
                    InputRecord(
                        namespace=admission.namespace,
                        session_id=admission.session_id,
                        task_id=admission.task_id,
                        input_id="initial",
                        kind=InputKind.INITIAL,
                        sequence=0,
                        content_digest=admission.initial_input.digest,
                        content=admission.initial_input,
                        created_at=now,
                    )
                )
                self._bump_inbox_locked(admission.task_id, bytes_=admission.initial_input.size_bytes)
            return record

        return self._in_transaction(work)

    def _admit_input(self, admission: InputAdmission) -> AdmissionResult:
        now = time.time()

        def work() -> AdmissionResult:
            task = self._load_task_locked(admission.task_id)
            if task is None:
                return AdmissionResult(
                    AdmissionOutcome.REFUSED, refusal_code=RefusalCode.UNKNOWN_TARGET.value
                )
            if task.namespace != admission.namespace or task.session_id != admission.session_id:
                return AdmissionResult(
                    AdmissionOutcome.REFUSED, refusal_code=RefusalCode.IDENTITY_MISMATCH.value
                )
            existing = self._load_input_locked(admission.task_id, admission.input_id)
            counters = self._load_counters_locked(admission.task_id)
            decision = decide_admission(
                existing=existing,
                incoming=admission,
                task_status=task.status,
                cancel_requested=task.cancel_requested_at is not None,
                pending_count=int(counters[0]),
                pending_bytes=int(counters[1]),
                limits=self.backlog,
            )
            if decision.outcome is AdmissionOutcome.ACCEPTED and decision.record is not None:
                record = decision.record
                sequence = int(counters[2])
                stored = InputRecord(
                    namespace=record.namespace,
                    session_id=record.session_id,
                    task_id=record.task_id,
                    input_id=record.input_id,
                    kind=record.kind,
                    sequence=sequence,
                    content_digest=record.content_digest,
                    content=record.content,
                    state=InputState.PENDING,
                    source=record.source,
                    created_at=now,
                )
                self._insert_input_locked(stored)
                self._bump_inbox_locked(
                    admission.task_id, bytes_=admission.content.size_bytes
                )
                return AdmissionResult(AdmissionOutcome.ACCEPTED, record=stored)
            return decision

        return self._in_transaction(work)

    def _claim_task(
        self,
        task_id: str,
        *,
        holder: str,
        ttl_seconds: float,
        attempt_id: str = "",
        now: float | None = None,
    ) -> DispatchClaim | None:
        at = time.time() if now is None else now

        def work() -> DispatchClaim | None:
            task = self._load_task_locked(task_id)
            if task is None or task.status.is_terminal:
                return None
            current = self._load_claim_locked(task_id)
            # Row shape: (task_id, holder, attempt_id, fencing_token, claimed_at, expires_at).
            if current is not None and float(current[5]) > at:
                # A live claim: exactly one logical executor per task. A stale one is *not*
                # evidence that external work stopped, only that the scheduling right lapsed.
                return None
            token = self._next_fencing_token_locked(task_id)
            claim = DispatchClaim(
                task_id=task_id,
                holder=holder,
                attempt_id=attempt_id or f"{task_id}-a{token}",
                fencing_token=token,
                claimed_at=at,
                expires_at=at + ttl_seconds,
                namespace=task.namespace,
            )
            self._connection.execute(
                "INSERT INTO task_claims(task_id, holder, attempt_id, fencing_token, claimed_at, "
                "expires_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(task_id) DO UPDATE SET holder=excluded.holder, "
                "attempt_id=excluded.attempt_id, fencing_token=excluded.fencing_token, "
                "claimed_at=excluded.claimed_at, expires_at=excluded.expires_at",
                (
                    task_id,
                    claim.holder,
                    claim.attempt_id,
                    claim.fencing_token,
                    claim.claimed_at,
                    claim.expires_at,
                ),
            )
            moved = task
            if task.status is TaskStatus.WAITING:
                # A waiting task that becomes schedulable first goes back to the queue; the claim
                # then moves it to running. Two facts, in one transaction.
                moved = task.transition(
                    TaskStatus.QUEUED, reason="external_fact_persisted", at=at
                )
            if moved.status is TaskStatus.QUEUED:
                moved = moved.transition(
                    TaskStatus.RUNNING, reason="execution_right_claimed", at=at,
                    attempt_id=claim.attempt_id,
                )
            # Every new claim is a new attempt, including one taken over a task that was already
            # running (its worker died and the claim expired): the logical task and its budget
            # continue, but "which attempt did this" must not silently stay the old one.
            moved = _with_attempt(moved, claim.attempt_id)
            self._save_task_locked(moved)
            return claim

        return self._in_transaction(work)

    def _renew_task_claim(
        self,
        task_id: str,
        *,
        holder: str,
        attempt_id: str,
        ttl_seconds: float,
        now: float | None = None,
    ) -> DispatchClaim | None:
        at = time.time() if now is None else now

        def work() -> DispatchClaim | None:
            current = self._load_claim_locked(task_id)
            if current is None or current[1] != holder or current[2] != attempt_id:
                return None
            expires = at + ttl_seconds
            self._connection.execute(
                "UPDATE task_claims SET expires_at = ? WHERE task_id = ? AND holder = ? "
                "AND attempt_id = ?",
                (expires, task_id, holder, attempt_id),
            )
            return DispatchClaim(
                task_id=task_id,
                holder=holder,
                attempt_id=attempt_id,
                fencing_token=int(current[3]),
                claimed_at=float(current[4]),
                expires_at=expires,
            )

        return self._in_transaction(work)

    def _release_task_claim(
        self, task_id: str, *, holder: str, attempt_id: str, now: float | None = None
    ) -> TaskRecord | None:
        at = time.time() if now is None else now

        def work() -> TaskRecord | None:
            current = self._load_claim_locked(task_id)
            if current is None or current[1] != holder or current[2] != attempt_id:
                return None
            self._connection.execute("DELETE FROM task_claims WHERE task_id = ?", (task_id,))
            task = self._load_task_locked(task_id)
            if task is None or task.status is not TaskStatus.RUNNING:
                return task
            # Losing or giving up the right returns the task to the queue: the same logical task
            # and budget continue under a new attempt, and nothing is reported as failed.
            moved = task.transition(
                TaskStatus.QUEUED, reason="execution_right_lost", at=at
            )
            self._save_task_locked(moved)
            return moved

        return self._in_transaction(work)

    def _commit_run_transition_with_inputs(
        self,
        run_id: str,
        state: Mapping[str, Any],
        *,
        expected_version: int | None,
        events: Sequence[StoredEvent] = (),
        applied_inputs: Sequence[InputRecord] = (),
        notifications: Sequence[ControlOutboxEntry] = (),
        claim: DispatchClaim | None = None,
        lease: Lease | None = None,
        stop_dispatching: bool = False,
        require_inbox_revision: int | None = None,
        task_id: str = "",
    ) -> tuple[int, tuple[StoredEvent, ...]]:
        """One transaction: run state, its events, the applied inputs and the notices.

        Returns the new run version and the events it wrote, so the caller can publish exactly
        what committed instead of guessing or writing them twice.

        ``require_inbox_revision`` closes the end-of-run race: a caller that decided to finish
        after observing revision *N* passes it, and if the inbox moved in between (an input was
        accepted while the run was deciding) the commit is refused instead of finishing over an
        accepted input. The caller then re-enters the polling boundary, which is where the new
        input can actually be integrated.
        """
        with self._lock:
            self._begin()
            try:
                if claim is not None:
                    # Checked inside the transaction that is about to write: a claim lost between
                    # the read and the commit cannot land a late state change.
                    self._assert_claim_locked(claim, at=time.time())
                if require_inbox_revision is not None and applied_inputs:
                    raise ContractError(
                        "a terminal commit cannot confirm inputs and require the observed inbox "
                        "revision at the same time; they are different boundaries"
                    )
                if require_inbox_revision is not None:
                    observed = self._observe_inbox_revision_locked(
                        task_id=task_id, applied_inputs=applied_inputs
                    )
                    if observed is not None and observed > require_inbox_revision:
                        raise InboxMoved(observed, require_inbox_revision)
                version = self._save_run_locked(
                    run_id, state, expected_version=expected_version, lease=lease
                )
                written = self._append_events_locked(events)
                last_sequence = written[-1].sequence if written else None
                for record in applied_inputs:
                    owner, input_id = _reference_parts(record, task_id)
                    stored = self._load_input_locked(owner, input_id)
                    if stored is None:
                        raise ContractError(
                            f"input {input_id!r} of task {owner!r} is not in the control store, "
                            "so it cannot be marked applied"
                        )
                    if stored.state is InputState.APPLIED:
                        if stored.applied_run_revision == version:
                            continue  # an idempotent re-commit of the same transition
                        raise ContractError(
                            f"input {stored.input_id!r} was already applied at run revision "
                            f"{stored.applied_run_revision}; it cannot be applied a second time"
                        )
                    if stored.state in {InputState.REFUSED, InputState.CANCELLED}:
                        raise ContractError(
                            f"input {stored.input_id!r} is {stored.state.value}; it cannot be "
                            "applied"
                        )
                    self._connection.execute(
                        "UPDATE inputs SET state = ?, applied_run_revision = ?, "
                        "applied_event_sequence = ?, claimed_by = '', claim_expires_at = NULL "
                        "WHERE task_id = ? AND input_id = ?",
                        (
                            InputState.APPLIED.value,
                            version,
                            last_sequence,
                            stored.task_id,
                            stored.input_id,
                        ),
                    )
                    self._connection.execute(
                        "UPDATE inbox_counters SET pending_count = pending_count - 1, "
                        "pending_bytes = pending_bytes - ?, revision = revision + 1 "
                        "WHERE task_id = ?",
                        (stored.content.size_bytes, stored.task_id),
                    )
                for entry in notifications:
                    self._insert_outbox_locked(entry)
            except BaseException:
                self._rollback()
                raise
            self._commit()
            return version, written

    def _reserve_budget(
        self,
        *,
        root_task_id: str,
        task_id: str,
        reservation: BudgetReservation,
        claim: DispatchClaim | None = None,
        limit: float | None = None,
        now: float | None = None,
    ) -> BudgetReservation:
        at = time.time() if now is None else now

        def work() -> BudgetReservation:
            if claim is not None:
                self._assert_claim_locked(claim, at=at)
            return self._reserve_budget_locked(
                reservation, root_task_id=root_task_id, task_id=task_id, limit=limit, at=at
            )

        return self._in_transaction(work)

    def _reserve_budget_locked(
        self,
        reservation: BudgetReservation,
        *,
        root_task_id: str,
        task_id: str,
        limit: float | None,
        at: float,
    ) -> BudgetReservation:
        """The reservation itself, callable from inside an open transaction."""
        existing = self._load_reservation_by_dispatch_locked(
            root_task_id, reservation.unit, reservation.dispatch_identity
        )
        if existing is not None:
            # Idempotent by dispatch identity: a replay of the same dispatch reserves once.
            return existing
        if limit is not None:
            used = self._used_locked(root_task_id, reservation.unit)
            available = limit - used
            if reservation.amount > available:
                raise BudgetRefused(
                    root_task_id,
                    reservation.unit.value,
                    reservation.amount,
                    round(available, 6),
                    "the reservation must be granted before the dispatch window opens; it "
                    "was refused instead of spending an allowance the root never granted",
                )
        stored = BudgetReservation(
            root_task_id=root_task_id,
            task_id=task_id,
            unit=reservation.unit,
            amount=reservation.amount,
            reservation_id=reservation.reservation_id,
            dispatch_identity=reservation.dispatch_identity,
            state=reservation.state,
            settled_amount=reservation.settled_amount,
            estimated=reservation.estimated,
            note=reservation.note,
            created_at=at,
        )
        self._insert_reservation_locked(stored)
        return stored

    def _settle_budget(
        self,
        reservation_id: str,
        *,
        state: str,
        settled_amount: float | None = None,
        note: str = "",
        now: float | None = None,
    ) -> BudgetReservation:
        at = time.time() if now is None else now
        target = ReservationState(state)

        def work() -> BudgetReservation:
            stored = self._load_reservation_locked(reservation_id)
            if stored is None:
                raise ContractError(f"no budget reservation {reservation_id!r}")
            if stored.state is ReservationState.SETTLED:
                if target is ReservationState.SETTLED and stored.settled_amount == settled_amount:
                    return stored  # idempotent replay
                raise ContractError(
                    f"reservation {reservation_id!r} is already settled at "
                    f"{stored.settled_amount}; it is not settled twice and never refunded"
                )
            if target is ReservationState.SETTLED:
                updated = stored.settle(settled_amount or 0.0, at=at)
            elif target is ReservationState.RELEASED:
                updated = stored.release(at=at, note=note)
            elif target is ReservationState.UNKNOWN:
                updated = stored.mark_unknown(note=note or "the outcome could not be established")
            else:
                raise ContractError(
                    f"a reservation cannot be settled back into {target.value}"
                )
            self._connection.execute(
                "UPDATE budgets SET state = ?, settled_amount = ?, estimated = ?, note = ?, "
                "settled_at = ? WHERE reservation_id = ?",
                (
                    updated.state.value,
                    updated.settled_amount,
                    1 if updated.estimated else 0,
                    updated.note,
                    updated.settled_at,
                    reservation_id,
                ),
            )
            return updated

        return self._in_transaction(work)

    def _register_child(
        self,
        relation: ChildRelation,
        *,
        claim: DispatchClaim | None = None,
        reservation: BudgetReservation | None = None,
        notification: ControlOutboxEntry | None = None,
        limit: float | None = None,
        now: float | None = None,
    ) -> ChildRelation:
        at = time.time() if now is None else now

        def work() -> ChildRelation:
            if claim is not None:
                self._assert_claim_locked(claim, at=at)
            existing = self._load_child_locked(relation.spawn_identity)
            if existing is not None:
                if (
                    existing.parent_task_id != relation.parent_task_id
                    or existing.child_task_id != relation.child_task_id
                ):
                    raise ContractError(
                        f"spawn identity {relation.spawn_identity!r} is already registered for "
                        f"child {existing.child_task_id!r}; the same identity with a different "
                        "configuration is refused rather than creating a second child"
                    )
                return existing
            stored = ChildRelation(
                parent_task_id=relation.parent_task_id,
                child_task_id=relation.child_task_id,
                spawn_identity=relation.spawn_identity,
                namespace=relation.namespace,
                session_id=relation.session_id,
                state=relation.state,
                operation_id=relation.operation_id,
                budget_reservation_id=(
                    relation.budget_reservation_id
                    or (reservation.reservation_id if reservation is not None else "")
                ),
                authorization_digest=relation.authorization_digest,
                created_at=at,
            )
            self._connection.execute(
                "INSERT INTO children(spawn_identity, parent_task_id, child_task_id, namespace, "
                "session_id, state, operation_id, budget_reservation_id, authorization_digest, "
                "created_at, schema_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    stored.spawn_identity,
                    stored.parent_task_id,
                    stored.child_task_id,
                    stored.namespace,
                    stored.session_id,
                    stored.state.value,
                    stored.operation_id,
                    stored.budget_reservation_id,
                    stored.authorization_digest,
                    stored.created_at,
                    stored.schema_version,
                ),
            )
            if reservation is not None:
                # Inside the same transaction as the relation and the notice: a child that exists
                # without its reservation, or a notice without its child, is the split write this
                # whole layer refuses.
                created = self._reserve_budget_locked(
                    reservation,
                    root_task_id=reservation.root_task_id,
                    task_id=reservation.task_id,
                    limit=limit,
                    at=at,
                )
                stored = ChildRelation(
                    **{
                        **{
                            field: getattr(stored, field)
                            for field in stored.__dataclass_fields__
                        },
                        "budget_reservation_id": created.reservation_id,
                    }
                )
            if notification is not None:
                self._insert_outbox_locked(notification)
            return stored

        return self._in_transaction(work)

    def _deliver_outbox(self, outbox_id: str, at: float | None = None) -> bool:
        stamp = time.time() if at is None else at

        def work() -> bool:
            cursor = self._connection.execute(
                "UPDATE outbox SET delivered_at = ?, attempts = attempts + 1, last_error = '' "
                "WHERE outbox_id = ? AND delivered_at IS NULL",
                (stamp, outbox_id),
            )
            return cursor.rowcount == 1

        return self._in_transaction(work)

    def _fail_outbox(self, outbox_id: str, error: str) -> bool:
        def work() -> bool:
            cursor = self._connection.execute(
                "UPDATE outbox SET attempts = attempts + 1, last_error = ? WHERE outbox_id = ?",
                (str(error), outbox_id),
            )
            return cursor.rowcount == 1

        return self._in_transaction(work)

    # -- reads -------------------------------------------------------------------------

    def _save_task(
        self, task: TaskRecord, expected_status: TaskStatus | None = None
    ) -> TaskRecord:
        """Compare-and-set on the task's status: two workers cannot both move it.

        The status is the thing that decides who may act (a scheduler claim is only a lease on
        *trying*), so a write that names the status it read is what stops one worker's settle from
        landing on top of another's.
        """

        def work() -> TaskRecord:
            current = self._load_task_locked(task.task_id)
            if current is None:
                raise ContractError(f"no task {task.task_id!r} to save")
            if expected_status is not None and current.status is not expected_status:
                raise ContractError(
                    f"task {task.task_id!r} is {current.status.value}, not the "
                    f"{expected_status.value} this write expected; refusing to overwrite a "
                    "fact another writer reached first"
                )
            self._save_task_locked(task)
            return task

        return self._in_transaction(work)

    def _read_schedulable(
        self, limit: int = 64, now: float | None = None
    ) -> tuple[TaskRecord, ...]:
        """Tasks a worker may try to claim, in the order the plan freezes.

        Two kinds qualify: a task waiting in the queue, and a task whose *scheduling* claim has
        lapsed (no claim row, or one that expired). The second is the recoverable case — the
        worker that held it is gone — and excluding it would strand every task whose worker died,
        which is exactly the case a durable scheduler exists for. ``priority`` first, then
        admission order; the aging rule that keeps a low-priority task from starving is applied by
        the worker, which knows the current time. The store orders deterministically from the
        record's own fields, so a reader can reproduce the decision.

        A live claim is excluded: another worker is driving that task right now.
        """
        if limit <= 0:
            raise ContractError("read_schedulable needs a positive limit")
        at = 0.0 if now is None else now

        def work() -> tuple[TaskRecord, ...]:
            rows = self._connection.execute(
                f"SELECT {_TASK_COLUMNS} FROM tasks AS t WHERE t.status = ? OR (t.status = ? AND "
                "NOT EXISTS (SELECT 1 FROM task_claims AS c WHERE c.task_id = t.task_id AND "
                "c.expires_at > ?)) ORDER BY t.priority DESC, t.created_at, t.task_id LIMIT ?",
                (TaskStatus.QUEUED.value, TaskStatus.RUNNING.value, at, limit),
            ).fetchall()
            return tuple(_task_from_row(row) for row in rows)

        return self._read(work)

    def _read_task(self, task_id: str) -> TaskRecord | None:
        return self._read(lambda: self._load_task_locked(task_id))

    def _read_inputs(
        self, task_id: str, after_sequence: int = -1, limit: int = 200
    ) -> tuple[InputRecord, ...]:
        if limit <= 0:
            raise ContractError("read_inputs needs a positive limit")

        def work() -> tuple[InputRecord, ...]:
            rows = self._connection.execute(
                f"SELECT {_INPUT_COLUMNS} FROM inputs WHERE task_id = ? AND sequence > ? "
                "ORDER BY sequence LIMIT ?",
                (task_id, after_sequence, limit),
            ).fetchall()
            return tuple(_input_from_row(row) for row in rows)

        return self._read(work)

    def _read_children(self, parent_task_id: str) -> tuple[ChildRelation, ...]:
        def work() -> tuple[ChildRelation, ...]:
            rows = self._connection.execute(
                "SELECT spawn_identity, parent_task_id, child_task_id, namespace, session_id, "
                "state, operation_id, budget_reservation_id, authorization_digest, created_at, "
                "schema_version FROM children WHERE parent_task_id = ? ORDER BY created_at, "
                "spawn_identity",
                (parent_task_id,),
            ).fetchall()
            return tuple(
                ChildRelation(
                    spawn_identity=row[0],
                    parent_task_id=row[1],
                    child_task_id=row[2],
                    namespace=row[3],
                    session_id=row[4],
                    state=ChildSpawnState(row[5]),
                    operation_id=row[6],
                    budget_reservation_id=row[7],
                    authorization_digest=row[8],
                    created_at=float(row[9]),
                    schema_version=int(row[10]),
                )
                for row in rows
            )

        return self._read(work)

    def _read_outbox(
        self, task_id: str | None = None, pending_only: bool = True, limit: int = 200
    ) -> tuple[ControlOutboxEntry, ...]:
        if limit <= 0:
            raise ContractError("read_outbox needs a positive limit")

        def work() -> tuple[ControlOutboxEntry, ...]:
            clauses: list[str] = []
            parameters: list[Any] = []
            if task_id is not None:
                clauses.append("target_task_id = ?")
                parameters.append(task_id)
            if pending_only:
                clauses.append("delivered_at IS NULL")
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            parameters.append(limit)
            rows = self._connection.execute(
                "SELECT outbox_id, kind, target_task_id, namespace, payload, created_at, "
                "delivered_at, attempts, last_error, schema_version FROM outbox "
                f"{where} ORDER BY created_at, outbox_id LIMIT ?",
                parameters,
            ).fetchall()
            return tuple(
                ControlOutboxEntry(
                    outbox_id=row[0],
                    kind=row[1],
                    target_task_id=row[2],
                    namespace=row[3],
                    payload=json.loads(row[4]),
                    created_at=float(row[5]),
                    delivered_at=row[6],
                    attempts=int(row[7]),
                    last_error=row[8],
                    schema_version=int(row[9]),
                )
                for row in rows
            )

        return self._read(work)

    def _read_reservations(
        self, root_task_id: str, unit: BudgetUnit | str | None = None
    ) -> tuple[BudgetReservation, ...]:
        def work() -> tuple[BudgetReservation, ...]:
            clauses = ["root_task_id = ?"]
            parameters: list[Any] = [root_task_id]
            if unit is not None:
                clauses.append("unit = ?")
                parameters.append(unit.value if isinstance(unit, BudgetUnit) else str(unit))
            rows = self._connection.execute(
                "SELECT reservation_id, root_task_id, task_id, unit, amount, dispatch_identity, "
                "state, settled_amount, estimated, note, created_at, settled_at, schema_version "
                f"FROM budgets WHERE {' AND '.join(clauses)} ORDER BY created_at, reservation_id",
                parameters,
            ).fetchall()
            return tuple(
                BudgetReservation(
                    reservation_id=row[0],
                    root_task_id=row[1],
                    task_id=row[2],
                    unit=BudgetUnit(row[3]),
                    amount=float(row[4]),
                    dispatch_identity=row[5],
                    state=ReservationState(row[6]),
                    settled_amount=row[7],
                    estimated=bool(row[8]),
                    note=row[9],
                    created_at=float(row[10]),
                    settled_at=row[11],
                    schema_version=int(row[12]),
                )
                for row in rows
            )

        return self._read(work)

    def _observe_inbox_revision_locked(
        self, *, task_id: str, applied_inputs: Sequence[Any]
    ) -> int | None:
        """The inbox revision of the task this commit is about, if one is named."""
        for record in applied_inputs:
            owner, _input_id = _reference_parts(record, task_id)
            if owner:
                return self._read_inbox_revision_locked(owner)
        return self._read_inbox_revision_locked(task_id) if task_id else None

    def _read_inbox_revision_locked(self, task_id: str) -> int:
        counters = self._load_counters_locked(task_id)
        return int(counters[3]) if counters else 0

    def _read_inbox_revision(self, task_id: str) -> int:
        return self._read(lambda: self._read_inbox_revision_locked(task_id))

    # -- locked helpers ----------------------------------------------------------------

    def _load_task_locked(self, task_id: str) -> TaskRecord | None:
        row = self._connection.execute(
            f"SELECT {_TASK_COLUMNS} FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        return None if row is None else _task_from_row(row)

    def _insert_task_locked(self, record: TaskRecord) -> None:
        self._connection.execute(
            f"INSERT INTO tasks({_TASK_COLUMNS}) VALUES ({', '.join('?' * 20)})",
            (
                record.task_id,
                record.namespace,
                record.session_id,
                record.run_id,
                record.status.value,
                record.reason,
                record.stop_code,
                record.priority,
                record.root_task_id or record.task_id,
                record.parent_task_id,
                record.depth,
                record.profile,
                record.workspace_ref,
                record.attempt_count,
                record.current_attempt_id,
                record.cancel_requested_at,
                record.waiting_for,
                record.created_at,
                record.updated_at,
                record.schema_version,
            ),
        )

    def _save_task_locked(self, record: TaskRecord) -> None:
        self._connection.execute(
            "UPDATE tasks SET namespace = ?, session_id = ?, run_id = ?, status = ?, reason = ?, "
            "stop_code = ?, priority = ?, root_task_id = ?, parent_task_id = ?, depth = ?, "
            "profile = ?, workspace_ref = ?, attempt_count = ?, current_attempt_id = ?, "
            "cancel_requested_at = ?, waiting_for = ?, created_at = ?, updated_at = ?, "
            "schema_version = ? WHERE task_id = ?",
            (
                record.namespace,
                record.session_id,
                record.run_id,
                record.status.value,
                record.reason,
                record.stop_code,
                record.priority,
                record.root_task_id or record.task_id,
                record.parent_task_id,
                record.depth,
                record.profile,
                record.workspace_ref,
                record.attempt_count,
                record.current_attempt_id,
                record.cancel_requested_at,
                record.waiting_for,
                record.created_at,
                record.updated_at,
                record.schema_version,
                record.task_id,
            ),
        )

    def _load_input_locked(self, task_id: str, input_id: str) -> InputRecord | None:
        row = self._connection.execute(
            f"SELECT {_INPUT_COLUMNS} FROM inputs WHERE task_id = ? AND input_id = ?",
            (task_id, input_id),
        ).fetchone()
        return None if row is None else _input_from_row(row)

    def _insert_input_locked(self, record: InputRecord) -> None:
        self._connection.execute(
            f"INSERT INTO inputs({_INPUT_COLUMNS}) VALUES ({', '.join('?' * 18)})",
            (
                record.task_id,
                record.input_id,
                record.namespace,
                record.session_id,
                record.kind.value,
                record.sequence,
                record.content_digest,
                json.dumps(record.content.to_record(), ensure_ascii=False, sort_keys=True),
                record.state.value,
                record.source,
                record.attempts,
                record.claimed_by,
                record.claim_expires_at,
                record.applied_run_revision,
                record.applied_event_sequence,
                record.refusal_code,
                record.created_at,
                record.schema_version,
            ),
        )

    def _load_counters_locked(self, task_id: str) -> tuple[int, int, int, int] | None:
        row = self._connection.execute(
            "SELECT pending_count, pending_bytes, next_sequence, revision FROM inbox_counters "
            "WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            return None
        return int(row[0]), int(row[1]), int(row[2]), int(row[3])

    def _bump_inbox_locked(self, task_id: str, *, bytes_: int) -> None:
        self._connection.execute(
            "UPDATE inbox_counters SET next_sequence = next_sequence + 1, "
            "revision = revision + 1, pending_count = pending_count + 1, "
            "pending_bytes = pending_bytes + ? WHERE task_id = ?",
            (int(bytes_), task_id),
        )

    def _load_claim_locked(self, task_id: str) -> tuple | None:
        return self._connection.execute(
            "SELECT task_id, holder, attempt_id, fencing_token, claimed_at, expires_at "
            "FROM task_claims WHERE task_id = ?",
            (task_id,),
        ).fetchone()

    def _next_fencing_token_locked(self, task_id: str) -> int:
        row = self._connection.execute(
            "SELECT next_fencing_token FROM claim_counters WHERE task_id = ?", (task_id,)
        ).fetchone()
        token = 1 if row is None else int(row[0])
        self._connection.execute(
            "INSERT INTO claim_counters(task_id, next_fencing_token) VALUES (?, ?) "
            "ON CONFLICT(task_id) DO UPDATE SET next_fencing_token = excluded.next_fencing_token",
            (task_id, token + 1),
        )
        return token

    def _assert_claim_locked(self, claim: DispatchClaim, *, at: float) -> None:
        current = self._load_claim_locked(claim.task_id)
        if current is None:
            raise ClaimLost(claim.task_id, "no claim is recorded any more")
        if (
            current[1] != claim.holder
            or current[2] != claim.attempt_id
            or int(current[3]) != claim.fencing_token
        ):
            raise ClaimLost(
                claim.task_id,
                f"the current claim is {current[2]} (fence {current[3]}, holder {current[1]}), "
                f"not {claim.attempt_id} (fence {claim.fencing_token}, holder {claim.holder})",
            )
        if float(current[5]) <= at:
            raise ClaimLost(
                claim.task_id, f"the claim expired at {current[5]} and was not renewed"
            )

    def _load_child_locked(self, spawn_identity: str) -> ChildRelation | None:
        row = self._connection.execute(
            "SELECT spawn_identity, parent_task_id, child_task_id, namespace, session_id, state, "
            "operation_id, budget_reservation_id, authorization_digest, created_at, "
            "schema_version FROM children WHERE spawn_identity = ?",
            (spawn_identity,),
        ).fetchone()
        if row is None:
            return None
        return ChildRelation(
            spawn_identity=row[0],
            parent_task_id=row[1],
            child_task_id=row[2],
            namespace=row[3],
            session_id=row[4],
            state=ChildSpawnState(row[5]),
            operation_id=row[6],
            budget_reservation_id=row[7],
            authorization_digest=row[8],
            created_at=float(row[9]),
            schema_version=int(row[10]),
        )

    def _insert_outbox_locked(self, entry: ControlOutboxEntry) -> None:
        self._connection.execute(
            "INSERT INTO outbox(outbox_id, kind, target_task_id, namespace, payload, created_at, "
            "delivered_at, attempts, last_error, schema_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?) ON CONFLICT(outbox_id) DO NOTHING",
            (
                entry.outbox_id,
                entry.kind,
                entry.target_task_id,
                entry.namespace,
                json.dumps(dict(entry.payload), ensure_ascii=False, sort_keys=True),
                entry.created_at or time.time(),
                entry.delivered_at,
                entry.attempts,
                entry.last_error,
                entry.schema_version,
            ),
        )

    def _load_reservation_locked(self, reservation_id: str) -> BudgetReservation | None:
        row = self._connection.execute(
            "SELECT reservation_id, root_task_id, task_id, unit, amount, dispatch_identity, "
            "state, settled_amount, estimated, note, created_at, settled_at, schema_version "
            "FROM budgets WHERE reservation_id = ?",
            (reservation_id,),
        ).fetchone()
        if row is None:
            return None
        return BudgetReservation(
            reservation_id=row[0],
            root_task_id=row[1],
            task_id=row[2],
            unit=BudgetUnit(row[3]),
            amount=float(row[4]),
            dispatch_identity=row[5],
            state=ReservationState(row[6]),
            settled_amount=row[7],
            estimated=bool(row[8]),
            note=row[9],
            created_at=float(row[10]),
            settled_at=row[11],
            schema_version=int(row[12]),
        )

    def _load_reservation_by_dispatch_locked(
        self, root_task_id: str, unit: BudgetUnit, dispatch_identity: str
    ) -> BudgetReservation | None:
        row = self._connection.execute(
            "SELECT reservation_id FROM budgets WHERE root_task_id = ? AND unit = ? AND "
            "dispatch_identity = ?",
            (root_task_id, unit.value, dispatch_identity),
        ).fetchone()
        return None if row is None else self._load_reservation_locked(row[0])

    def _insert_reservation_locked(self, reservation: BudgetReservation) -> None:
        self._connection.execute(
            "INSERT INTO budgets(reservation_id, root_task_id, task_id, unit, amount, "
            "dispatch_identity, state, settled_amount, estimated, note, created_at, settled_at, "
            "schema_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                reservation.reservation_id,
                reservation.root_task_id,
                reservation.task_id,
                reservation.unit.value,
                reservation.amount,
                reservation.dispatch_identity,
                reservation.state.value,
                reservation.settled_amount,
                1 if reservation.estimated else 0,
                reservation.note,
                reservation.created_at,
                reservation.settled_at,
                reservation.schema_version,
            ),
        )

    def _used_locked(self, root_task_id: str, unit: BudgetUnit) -> float:
        """What the root has spent or committed: settled amounts plus live reservations.

        An ``unknown`` reservation counts at its full amount: refunding it would make the
        available allowance larger than the root ever granted (plan §9).
        """
        row = self._connection.execute(
            "SELECT COALESCE(SUM(CASE WHEN state = 'settled' THEN COALESCE(settled_amount, 0) "
            "ELSE amount END), 0) FROM budgets WHERE root_task_id = ? AND unit = ? AND state IN "
            "('reserved', 'settled', 'unknown')",
            (root_task_id, unit.value),
        ).fetchone()
        return float(row[0] or 0.0)


def _with_attempt(record: TaskRecord, attempt_id: str) -> TaskRecord:
    from dataclasses import replace

    return replace(record, attempt_count=record.attempt_count + 1, current_attempt_id=attempt_id)


class MemoryControlStore:
    """A volatile, single-process reference implementation of the same contract.

    Use it for tests and for hosts that have explicitly decided they do not need durability.
    It is **not** evidence of durability: nothing survives the process, two processes cannot share
    it, and :attr:`durability` says so. Its decisions go through the same pure functions as the
    SQLite store, so a behaviour difference between them is a bug in one of them.
    """

    def __init__(self, *, backlog: BacklogLimits | None = None) -> None:
        self.backlog = backlog or BACKLOG_LIMITS
        self._lock = threading.RLock()
        self._tasks: dict[str, TaskRecord] = {}
        self._inputs: dict[str, dict[str, InputRecord]] = {}
        self._counters: dict[str, list[int]] = {}
        self._claims: dict[str, tuple[str, str, int, float, float]] = {}
        self._tokens: dict[str, int] = {}
        self._children: dict[str, ChildRelation] = {}
        self._outbox: dict[str, ControlOutboxEntry] = {}
        self._budgets: dict[str, BudgetReservation] = {}
        self._runs: dict[str, dict[str, Any]] = {}
        self._events: list[StoredEvent] = []

    @property
    def durability(self) -> str:
        return "VOLATILE"

    async def admit_task(self, admission: TaskAdmission) -> TaskRecord:
        with self._lock:
            existing = self._tasks.get(admission.task_id)
            now = admission.created_at or time.time()
            if existing is not None:
                if (
                    existing.namespace != admission.namespace
                    or existing.session_id != admission.session_id
                    or existing.run_id != admission.run_id
                ):
                    raise ContractError(
                        f"task {admission.task_id!r} already exists with a different identity"
                    )
                return existing
            record = TaskRecord(
                namespace=admission.namespace,
                session_id=admission.session_id,
                task_id=admission.task_id,
                run_id=admission.run_id,
                root_task_id=admission.root_task_id or admission.task_id,
                parent_task_id=admission.parent_task_id,
                depth=admission.depth,
                priority=admission.priority,
                profile=admission.profile,
                workspace_ref=admission.workspace_ref,
                created_at=now,
                updated_at=now,
            )
            self._tasks[record.task_id] = record
            self._inputs[record.task_id] = {}
            self._counters[record.task_id] = [0, 0, 0, 0]  # pending, bytes, next_seq, revision
            if admission.initial_input is not None:
                self._accept(record.task_id, "initial", admission.initial_input, now)
            return record

    async def admit_input(self, admission: InputAdmission) -> AdmissionResult:
        with self._lock:
            task = self._tasks.get(admission.task_id)
            if task is None:
                return AdmissionResult(
                    AdmissionOutcome.REFUSED, refusal_code=RefusalCode.UNKNOWN_TARGET.value
                )
            if task.namespace != admission.namespace or task.session_id != admission.session_id:
                return AdmissionResult(
                    AdmissionOutcome.REFUSED, refusal_code=RefusalCode.IDENTITY_MISMATCH.value
                )
            existing = self._inputs[task.task_id].get(admission.input_id)
            counters = self._counters[task.task_id]
            decision = decide_admission(
                existing=existing,
                incoming=admission,
                task_status=task.status,
                cancel_requested=task.cancel_requested_at is not None,
                pending_count=counters[0],
                pending_bytes=counters[1],
                limits=self.backlog,
            )
            if decision.outcome is AdmissionOutcome.ACCEPTED:
                record = self._accept(
                    task.task_id, admission.input_id, admission.content, time.time(),
                    source=admission.source,
                )
                return AdmissionResult(AdmissionOutcome.ACCEPTED, record=record)
            return decision

    def _accept(
        self,
        task_id: str,
        input_id: str,
        content: InputContent,
        now: float,
        *,
        source: str = "host",
    ) -> InputRecord:
        counters = self._counters[task_id]
        task = self._tasks[task_id]
        record = InputRecord(
            namespace=task.namespace,
            session_id=task.session_id,
            task_id=task_id,
            input_id=input_id,
            kind=content.kind,
            sequence=counters[2],
            content_digest=content.digest,
            content=content,
            source=source,
            created_at=now,
        )
        self._inputs[task_id][input_id] = record
        counters[2] += 1
        counters[3] += 1
        counters[0] += 1
        counters[1] += content.size_bytes
        return record

    async def claim_task(
        self,
        task_id: str,
        *,
        holder: str,
        ttl_seconds: float,
        attempt_id: str = "",
        now: float | None = None,
    ) -> DispatchClaim | None:
        with self._lock:
            at = time.time() if now is None else now
            task = self._tasks.get(task_id)
            if task is None or task.status.is_terminal:
                return None
            current = self._claims.get(task_id)
            if current is not None and current[4] > at:
                return None
            token = self._tokens.get(task_id, 1)
            self._tokens[task_id] = token + 1
            claim = DispatchClaim(
                task_id=task_id,
                holder=holder,
                attempt_id=attempt_id or f"{task_id}-a{token}",
                fencing_token=token,
                claimed_at=at,
                expires_at=at + ttl_seconds,
                namespace=task.namespace,
            )
            self._claims[task_id] = (holder, claim.attempt_id, token, at, claim.expires_at)
            if task.status is TaskStatus.WAITING:
                task = task.transition(TaskStatus.QUEUED, reason="external_fact_persisted", at=at)
            if task.status is TaskStatus.QUEUED:
                task = task.transition(
                    TaskStatus.RUNNING, reason="execution_right_claimed", at=at,
                    attempt_id=claim.attempt_id,
                )
                task = _with_attempt(task, claim.attempt_id)
                self._tasks[task_id] = task
            return claim

    async def renew_task_claim(
        self,
        task_id: str,
        *,
        holder: str,
        attempt_id: str,
        ttl_seconds: float,
        now: float | None = None,
    ) -> DispatchClaim | None:
        with self._lock:
            at = time.time() if now is None else now
            current = self._claims.get(task_id)
            if current is None or current[0] != holder or current[1] != attempt_id:
                return None
            expires = at + ttl_seconds
            self._claims[task_id] = (holder, attempt_id, current[2], current[3], expires)
            return DispatchClaim(
                task_id=task_id,
                holder=holder,
                attempt_id=attempt_id,
                fencing_token=current[2],
                claimed_at=current[3],
                expires_at=expires,
            )

    async def release_task_claim(
        self, task_id: str, *, holder: str, attempt_id: str, now: float | None = None
    ) -> TaskRecord | None:
        with self._lock:
            at = time.time() if now is None else now
            current = self._claims.get(task_id)
            if current is None or current[0] != holder or current[1] != attempt_id:
                return None
            self._claims.pop(task_id, None)
            task = self._tasks.get(task_id)
            if task is None or task.status is not TaskStatus.RUNNING:
                return task
            task = task.transition(TaskStatus.QUEUED, reason="execution_right_lost", at=at)
            self._tasks[task_id] = task
            return task

    async def commit_run_transition_with_inputs(
        self,
        run_id: str,
        state: Mapping[str, Any],
        events: Sequence[Any] = (),
        *,
        expected_version: int | None,
        applied_inputs: Sequence[InputRecord] = (),
        notifications: Sequence[ControlOutboxEntry] = (),
        claim: DispatchClaim | None = None,
        lease: Lease | None = None,
        stop_dispatching: bool = False,
        require_inbox_revision: int | None = None,
        task_id: str = "",
    ) -> int:
        with self._lock:
            if require_inbox_revision is not None:
                current_revision = self._counters.get(task_id, [0, 0, 0, 0])[3]
                if current_revision > require_inbox_revision:
                    raise InboxMoved(current_revision, require_inbox_revision)
            if claim is not None:
                self._assert_claim(claim)
            current = self._runs.get(run_id)
            version = 1 if current is None else int(current["version"]) + 1
            if expected_version is not None and (current or {}).get("version") != expected_version:
                raise ContractError(
                    f"run {run_id!r} is at version "
                    f"{(current or {}).get('version')}, not {expected_version}"
                )
            self._runs[run_id] = {"version": version, "state": dict(state)}
            for event in events:
                self._events.append(event)
            for record in applied_inputs:
                stored = self._inputs[record.task_id].get(record.input_id)
                if stored is None:
                    raise ContractError(f"input {record.input_id!r} is not in the inbox")
                if stored.state is InputState.APPLIED:
                    raise ContractError(f"input {record.input_id!r} was already applied")
                self._inputs[record.task_id][record.input_id] = stored.apply(
                    run_revision=version
                )
                counters = self._counters[record.task_id]
                counters[0] -= 1
                counters[1] -= stored.content.size_bytes
                counters[3] += 1
            for entry in notifications:
                self._outbox.setdefault(entry.outbox_id, entry)
            return version

    def _reserve_sync(
        self,
        reservation: BudgetReservation,
        *,
        limit: float | None = None,
        now: float | None = None,
    ) -> BudgetReservation:
        """The reservation decision, synchronous so another in-memory write can join it."""
        for existing in self._budgets.values():
            if (
                existing.root_task_id == reservation.root_task_id
                and existing.unit == reservation.unit
                and existing.dispatch_identity == reservation.dispatch_identity
            ):
                return existing  # idempotent by dispatch identity
        if limit is not None:
            used = self._used(reservation.root_task_id, reservation.unit)
            available = limit - used
            if reservation.amount > available:
                raise BudgetRefused(
                    reservation.root_task_id,
                    reservation.unit.value,
                    reservation.amount,
                    round(available, 6),
                )
        stored = BudgetReservation(
            **{
                **{
                    field: getattr(reservation, field)
                    for field in reservation.__dataclass_fields__
                },
                "created_at": time.time() if now is None else now,
            }
        )
        self._budgets[stored.reservation_id] = stored
        return stored

    async def reserve_budget(
        self,
        *,
        root_task_id: str,
        task_id: str,
        reservation: BudgetReservation,
        claim: DispatchClaim | None = None,
        limit: float | None = None,
        now: float | None = None,
    ) -> BudgetReservation:
        with self._lock:
            if claim is not None:
                self._assert_claim(claim)
            return self._reserve_sync(reservation, limit=limit, now=now)

    async def settle_budget(
        self,
        reservation_id: str,
        *,
        state: str,
        settled_amount: float | None = None,
        note: str = "",
        now: float | None = None,
    ) -> BudgetReservation:
        with self._lock:
            at = time.time() if now is None else now
            stored = self._budgets.get(reservation_id)
            if stored is None:
                raise ContractError(f"no budget reservation {reservation_id!r}")
            target = ReservationState(state)
            if stored.state is ReservationState.SETTLED:
                if target is ReservationState.SETTLED and stored.settled_amount == settled_amount:
                    return stored
                raise ContractError(
                    f"reservation {reservation_id!r} is already settled; it is never refunded"
                )
            if target is ReservationState.SETTLED:
                updated = stored.settle(settled_amount or 0.0, at=at)
            elif target is ReservationState.RELEASED:
                updated = stored.release(at=at, note=note)
            else:
                updated = stored.mark_unknown(note=note or "the outcome could not be established")
            self._budgets[reservation_id] = updated
            return updated

    async def register_child(
        self,
        relation: ChildRelation,
        *,
        claim: DispatchClaim | None = None,
        reservation: BudgetReservation | None = None,
        notification: ControlOutboxEntry | None = None,
        limit: float | None = None,
        now: float | None = None,
    ) -> ChildRelation:
        with self._lock:
            if claim is not None:
                self._assert_claim(claim)
            existing = self._children.get(relation.spawn_identity)
            if existing is not None:
                if (
                    existing.parent_task_id != relation.parent_task_id
                    or existing.child_task_id != relation.child_task_id
                ):
                    raise ContractError(
                        f"spawn identity {relation.spawn_identity!r} is already registered"
                    )
                return existing
            created = None
            if reservation is not None:
                created = self._reserve_sync(reservation, limit=limit, now=now)
            stored = ChildRelation(
                **{
                    **{
                        field: getattr(relation, field)
                        for field in relation.__dataclass_fields__
                    },
                    "budget_reservation_id": (
                        relation.budget_reservation_id
                        or (created.reservation_id if created else "")
                    ),
                    "created_at": time.time() if now is None else now,
                }
            )
            self._children[stored.spawn_identity] = stored
            if notification is not None:
                self._outbox.setdefault(notification.outbox_id, notification)
            return stored

    async def save_task(
        self, task: TaskRecord, *, expected_status: TaskStatus | None = None
    ) -> TaskRecord:
        with self._lock:
            current = self._tasks.get(task.task_id)
            if current is None:
                raise ContractError(f"no task {task.task_id!r} to save")
            if expected_status is not None and current.status is not expected_status:
                raise ContractError(
                    f"task {task.task_id!r} is {current.status.value}, not "
                    f"{expected_status.value}"
                )
            self._tasks[task.task_id] = task
            return task

    async def read_schedulable(
        self, *, limit: int = 64, now: float | None = None
    ) -> Sequence[TaskRecord]:
        at = time.time() if now is None else now
        candidates = [
            task
            for task in self._tasks.values()
            if task.status is TaskStatus.QUEUED
            or (
                task.status is TaskStatus.RUNNING
                and not (
                    self._claims.get(task.task_id) is not None
                    and self._claims[task.task_id][4] > at
                )
            )
        ]
        candidates.sort(key=lambda task: (-task.priority, task.created_at, task.task_id))
        return tuple(candidates[:limit])

    async def read_task(self, task_id: str) -> TaskRecord | None:
        return self._tasks.get(task_id)

    async def read_inputs(
        self, task_id: str, *, after_sequence: int = -1, limit: int = 200
    ) -> Sequence[InputRecord]:
        records = [
            record
            for record in self._inputs.get(task_id, {}).values()
            if record.sequence > after_sequence
        ]
        records.sort(key=lambda record: record.sequence)
        return tuple(records[:limit])

    async def read_children(self, parent_task_id: str) -> Sequence[ChildRelation]:
        return tuple(
            sorted(
                (
                    relation
                    for relation in self._children.values()
                    if relation.parent_task_id == parent_task_id
                ),
                key=lambda relation: (relation.created_at, relation.spawn_identity),
            )
        )

    async def read_outbox(
        self, *, task_id: str | None = None, pending_only: bool = True, limit: int = 200
    ) -> Sequence[ControlOutboxEntry]:
        entries = [
            entry
            for entry in self._outbox.values()
            if (task_id is None or entry.target_task_id == task_id)
            and (not pending_only or entry.pending)
        ]
        entries.sort(key=lambda entry: (entry.created_at, entry.outbox_id))
        return tuple(entries[:limit])

    async def read_reservations(
        self, root_task_id: str, *, unit: BudgetUnit | str | None = None
    ) -> Sequence[BudgetReservation]:
        wanted = unit.value if isinstance(unit, BudgetUnit) else unit
        return tuple(
            reservation
            for reservation in self._budgets.values()
            if reservation.root_task_id == root_task_id
            and (wanted is None or reservation.unit.value == wanted)
        )

    async def deliver_outbox(self, outbox_id: str, *, at: float | None = None) -> bool:
        entry = self._outbox.get(outbox_id)
        if entry is None or not entry.pending:
            return False
        self._outbox[outbox_id] = entry.delivered(at=time.time() if at is None else at)
        return True

    async def fail_outbox(self, outbox_id: str, *, error: str) -> bool:
        entry = self._outbox.get(outbox_id)
        if entry is None:
            return False
        self._outbox[outbox_id] = entry.failed(error=error)
        return True

    async def read_inbox_revision(self, task_id: str) -> int:
        counters = self._counters.get(task_id)
        return 0 if counters is None else counters[3]

    def _assert_claim(self, claim: DispatchClaim) -> None:
        current = self._claims.get(claim.task_id)
        if current is None:
            raise ClaimLost(claim.task_id, "no claim is recorded any more")
        holder, attempt_id, token, _at, expires = current
        if (
            holder != claim.holder
            or attempt_id != claim.attempt_id
            or token != claim.fencing_token
        ):
            raise ClaimLost(
                claim.task_id,
                f"the current claim is {attempt_id} (fence {token}), not {claim.attempt_id}",
            )
        if expires <= time.time():
            raise ClaimLost(claim.task_id, f"the claim expired at {expires}")

    def _used(self, root_task_id: str, unit: BudgetUnit) -> float:
        total = 0.0
        for reservation in self._budgets.values():
            if reservation.root_task_id != root_task_id or reservation.unit is not unit:
                continue
            if reservation.state is ReservationState.SETTLED:
                total += float(reservation.settled_amount or 0.0)
            elif reservation.state in {ReservationState.RESERVED, ReservationState.UNKNOWN}:
                total += reservation.amount
        return total
