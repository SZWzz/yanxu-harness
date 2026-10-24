"""The capability a store must provide for persistent tasks — and the refusal when it does not.

Every existing backend keeps working exactly as it does today: a single-run deployment needs
nothing from this module. What it must not do is *pretend*. If a host enables persistent tasks —
durable input, several queued tasks, parent/child work — and the store underneath cannot commit
the run's state, the consumed input, the events and the control notification in **one**
transaction, then the honest outcomes are "refuse before anything is dispatched" or "write twice
and hope". This module makes the first one the only option.

The required operations are the five the plan names (§5.3):

1. ``admit_task`` — create a task and its ``initial`` input, fixing the run id; a repeated submit
   returns the same record;
2. ``admit_input`` — admit an input for a target status, assign its sequence, and bump the inbox
   revision;
3. ``claim_task`` — take, renew or release the scheduler's execution right with a durable fencing
   token;
4. ``commit_run_transition_with_inputs`` — commit the run's transition, the applied inputs, the
   events and the control notifications together;
5. ``reserve_budget`` — admit a dispatch under the lease and the allowance, and settle idempotently
   by stable dispatch identity (with ``register_child`` writing the relation, its reservation and
   the scheduling notification in the same transaction).

Reads are separate: a store may offer the committed API and a narrower or wider read surface, and
a reader that only inspects is expected to use the diagnostics layer, which never claims a lease,
never consumes an inbox and never advances a task.
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from ..errors import ContractError
from .types import (
    COORDINATION_METHODS,
    COORDINATION_READ_METHODS,
    AdmissionResult,
    BudgetReservation,
    ChildRelation,
    ControlOutboxEntry,
    DispatchClaim,
    InputAdmission,
    InputKind,
    InputRead,
    InputRecord,
    TaskAdmission,
    TaskRecord,
    TaskStatus,
)

__all__ = [
    "COORDINATION_METHODS",
    "COORDINATION_READ_METHODS",
    "CoordinationStore",
    "ExecutionRight",
    "coordination_capability",
    "is_coordination_store",
    "require_coordination",
    "require_input_polling",
]


@runtime_checkable
class CoordinationStore(Protocol):
    """The atomic operations a persistent-task deployment needs from its control store.

    A store implements all of them or it is not a coordination store. Which backend it is — SQLite,
    an in-memory reference, a host's own database — is not part of the contract; what is, is that
    each method is one transaction.
    """

    async def admit_task(self, admission: TaskAdmission) -> TaskRecord:
        """Create the task and its initial input together, or return the existing record.

        A retry of the same ``task_id`` with the same content must return the record that exists;
        it must not create a second task, a second run or a second budget.
        """

    async def admit_input(self, admission: InputAdmission) -> AdmissionResult:
        """Admit one input: assign its sequence, update the inbox revision, deduplicate by key.

        Inside one transaction, this must re-read the duplicate record, the task status and the
        backlog counters. A decision taken from values read before the transaction is a decision
        about a moment that has passed.
        """

    async def claim_task(
        self,
        task_id: str,
        *,
        holder: str,
        ttl_seconds: float,
        now: float | None = None,
    ) -> DispatchClaim | None:
        """Take the execution right, or return ``None`` when another live claim holds it.

        The fencing token grows with every claim, and a claim that is not renewed stops being
        valid when it expires. Expiry proves the *scheduler's* right lapsed; it proves nothing
        about whether an external tool is still running.
        """

    async def commit_run_transition_with_inputs(
        self,
        run_id: str,
        state: Mapping[str, Any],
        events: Sequence[Any],
        *,
        expected_version: int | None,
        applied_inputs: Sequence[Any] = (),
        notifications: Sequence[ControlOutboxEntry] = (),
        claim: DispatchClaim | None = None,
        stop_dispatching: bool = False,
    ) -> tuple[int, Sequence[Any]]:
        """Commit the run's next state, the inputs it consumed and the notices it owes — together.

        This is the boundary the whole design rests on (plan §7.2): the run's snapshot, the
        ``applied`` marks and the control outbox become visible in one transaction, and no external
        work may be dispatched before it returns. ``stop_dispatching`` is how a caller whose
        snapshot exists but whose commit failed reports that no further dispatch happened.
        """

    async def poll_pending_inputs(
        self,
        task_id: str,
        *,
        kind: InputKind | None = None,
        limit: int = 8,
    ) -> InputRead:
        """The bounded read the persistent input reader performs at a poll site.

        It selects the target task's still-claimable conversation inputs (``pending`` or
        re-claimable ``claimed``), of the requested ``kind`` when one is named and of every
        conversation kind otherwise, in sequence order and bounded by ``limit``. It returns the
        candidates **and** the inbox revision observed in the same read, and confirms nothing:
        the input stays claimable until the transaction that integrates it commits (plan §7.2).

        It must **not** be built by scanning applied history from the start or from
        ``MAX(applied)``: an applied watermark is not a contiguous consumed prefix, because
        steering and follow-up are read at different sites and a newer input of one kind can be
        applied while an older one of the other is still waiting (defect DIO2-I1).
        """

    async def reserve_budget(
        self,
        *,
        root_task_id: str,
        task_id: str,
        reservation: BudgetReservation,
        claim: DispatchClaim | None = None,
        limit: float | None = None,
    ) -> BudgetReservation:
        """Reserve allowance before an external await; the same dispatch identity reserves once.

        A reservation that cannot be granted is refused *before* the dispatch window opens. A
        replay of the same ``dispatch_identity`` returns the existing reservation unchanged.
        """

    async def settle_budget(
        self,
        reservation_id: str,
        *,
        state: str,
        settled_amount: float | None = None,
        note: str = "",
        now: float | None = None,
    ) -> BudgetReservation:
        """Settle, release or mark unknown — idempotently, by reservation identity."""

    async def register_child(
        self,
        relation: ChildRelation,
        *,
        claim: DispatchClaim | None = None,
        child_task: TaskAdmission | None = None,
        reservation: BudgetReservation | None = None,
        notification: ControlOutboxEntry | None = None,
        limit: float | None = None,
        now: float | None = None,
    ) -> ChildRelation:
        """Register the relation, its child task, its reservation and its notice in one write.

        A retry with the same ``spawn_identity`` returns the existing relation, which is how a
        crash between the control commit and the Gateway receipt is settled by looking the
        relation up rather than by creating a second child. When ``child_task`` is given, the
        child task and its ``initial`` input are created in the *same* transaction, so a crash
        can never leave a child task without the relation that owns it (plan §10.2).
        """

    # -- reads -------------------------------------------------------------------------

    async def read_task(self, task_id: str) -> TaskRecord | None:
        """The task as committed. The run's own status is **not** part of this record."""

    async def read_inputs(
        self, task_id: str, *, after_sequence: int = -1, limit: int = 200
    ) -> Sequence[InputRecord]:
        """Inputs in sequence order, bounded and resumable."""

    async def read_children(self, parent_task_id: str) -> Sequence[ChildRelation]:
        """The children registered for one parent."""

    async def read_child(self, spawn_identity: str) -> ChildRelation | None:
        """The relation for one stable spawn identity — the two-database convergence point."""

    async def read_child_of(self, child_task_id: str) -> ChildRelation | None:
        """The relation that owns one child task, if any."""

    async def read_task_by_run(self, run_id: str) -> TaskRecord | None:
        """The task bound to one run id."""

    async def save_task(
        self, task: TaskRecord, *, expected_status: TaskStatus | None = None
    ) -> TaskRecord:
        """Compare-and-set the task's own state (never the run's)."""

    async def save_task_with_notices(
        self,
        task: TaskRecord,
        *,
        expected_status: TaskStatus | None = None,
        notifications: Sequence[ControlOutboxEntry] = (),
        relation: ChildRelation | None = None,
        settle_reservation: tuple[str, str, float | None, str] | None = None,
    ) -> TaskRecord:
        """One transaction: the child's terminal state, its relation, its notices and its
        reservation settlement. This is the completion boundary that makes a finished child and
        the notice its parent needs a single fact (plan §10.3).
        """

    async def read_outbox(
        self, *, task_id: str | None = None, pending_only: bool = True, limit: int = 200
    ) -> Sequence[ControlOutboxEntry]:
        """Notifications owed, so a lost callback is redelivered instead of lost."""


class ExecutionRight:
    """A held claim, with the order in which it is acquired and released.

    Order matters and is fixed here: take the scheduler's claim first, then the run's own lease;
    release the run's lease first, then the claim. Holding the claim while waiting for the lease
    means one worker per task even under a race; taking the lease first would let two workers both
    hold claims and one of them discover the conflict after it had already started work.
    """

    def __init__(self, claim: DispatchClaim) -> None:
        self.claim = claim
        self.released = False

    def __enter__(self) -> DispatchClaim:
        return self.claim

    def __exit__(self, *_exc: object) -> None:
        self.released = True


def coordination_capability(store: Any) -> str:
    """Describe what a store can do for persistent tasks.

    ``"full"`` — every required atomic operation is present, including the bounded pending-input
    read the persistent reader needs. ``"none"`` — the store is a plain single-run backend.
    ``"partial"`` — some operations are missing, which is the dangerous case: the missing one
    would have to be emulated by two writes (or by a scan of applied history), so the deployment
    must be refused before dispatch rather than degraded.
    """
    if store is None:
        return "none"
    present = [name for name in COORDINATION_METHODS if callable(getattr(store, name, None))]
    if not present:
        return "none"
    if len(present) == len(COORDINATION_METHODS) and not missing_read_methods(store):
        return "full"
    return "partial"


def missing_read_methods(store: Any) -> tuple[str, ...]:
    """The read methods the persistent input reader requires and the store does not provide."""
    return tuple(
        name for name in COORDINATION_READ_METHODS if not callable(getattr(store, name, None))
    )


def is_coordination_store(store: Any) -> bool:
    """True when ``store`` provides every required operation (a runtime-checked protocol)."""
    return coordination_capability(store) == "full"


def require_input_polling(store: Any) -> Any:
    """Refuse to read a durable inbox through a store that cannot query pending inputs.

    Called by :class:`~yanxu_harness.tasks.inputs.PersistentInputReader` before the first read.
    A store without ``poll_pending_inputs`` would have to be driven by ``read_inputs`` plus a
    guessed watermark, which is exactly the defect this port closes, so it is refused instead of
    approximated.
    """
    missing = missing_read_methods(store)
    if not missing:
        return store
    raise ContractError(
        "the persistent input reader needs a bounded pending-input query and would otherwise "
        "have to guess which inputs are still claimable from applied history; this store is "
        f"missing: {', '.join(missing)}"
    )


def require_coordination(store: Any) -> Any:
    """Refuse to enable persistent tasks on a store that cannot commit them atomically.

    Called before the first admission, not before the first dispatch: an orchestration that has
    already accepted work and only then discovers it cannot commit it atomically has already made
    the promise it cannot keep.
    """
    capability = coordination_capability(store)
    if capability == "full":
        return store
    missing = sorted(
        name
        for name in (*COORDINATION_METHODS, *COORDINATION_READ_METHODS)
        if not callable(getattr(store, name, None))
    )
    raise ContractError(
        "persistent tasks need a coordination store that commits the run's state, the applied "
        f"inputs and the control notices in one transaction; this store is {capability!r}"
        + (f" (missing: {missing})" if missing else "")
        + ". A single-run deployment may keep using it; enabling persistent tasks on it would "
        "mean two independent writes, so it is refused before anything is admitted"
    )
