"""The local reference worker: it picks tasks, claims them and drives the existing harness.

What this module is, in one sentence: **a scheduler, not an agent**. It decides *which* task may
run and makes sure only one worker drives it; the model request, the tool batch, the commit
boundary and the recovery all still belong to the one Runner and the harness around it
(plan §8.1). A scheduler that also sent requests would be a second execution path, which is
exactly what this package refuses to grow.

The rules the plan freezes, and where each one lives:

* **one logical executor per task.** The scheduling claim in the control store is a fencing
  token with an expiry; two workers racing for one task get one claim. The claim decides who may
  *try* — the run's own lease still decides who may *commit*, and a worker that lost either one
  stops rather than writing;
* **claim, then re-read.** A claim taken on a task that has since ended, been cancelled, or is
  waiting on something else is released, not acted on. A run that already exists is classified
  (terminal / waiting / has a live executor / recoverable) and never replaced by a brand-new run
  pretending to be a retry;
* **bounded, fair, explainable.** Global and per-namespace concurrency caps; candidates ordered by
  priority, then by admission order, with **aging** so a low-priority task older than
  ``priority_aging_seconds`` is tried before a newer high-priority one; namespaces are served
  round-robin so one busy tenant cannot starve another. Every decision names the reason and the
  candidates it skipped (``ScheduleDecision``), because "why did this run" is a question an
  operator asks;
* **waiting releases the slot.** A task waiting on an approval or a child result stops occupying a
  concurrency slot and becomes schedulable again when the external fact is persisted;
* **stopping is not cancelling.** ``stop()`` stops taking new claims and gives the drives it
  already started a bounded grace. It never marks a task cancelled, and it never releases a claim
  for a drive that may still be running — an expired claim means "the scheduling right lapsed",
  never "the tool stopped".
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Mapping, Sequence

from ..errors import ContractError
from ..runtime.context import Principal, RunContext, RunIdentity
from .types import (
    REASON_AWAITING,
    REASON_CANCEL_REQUESTED,
    REASON_CANCELLED,
    REASON_CLAIMED,
    REASON_COMPLETED,
    REASON_FAILED,
    REASON_RIGHT_LOST,
    REASON_USAGE_LIMITED,
    DispatchClaim,
    InputAdmission,
    InputKind,
    InputState,
    TaskAdmission,
    TaskRecord,
    TaskStatus,
)

__all__ = [
    "LocalWorker",
    "ScheduleDecision",
    "SchedulerLimits",
    "WorkResult",
]

#: How a run outcome maps onto the task's own vocabulary. Kept as data so the mapping is one
#: place, and so a test can pin it: a task never borrows the run's status word for its own.
_RUN_TO_TASK: Mapping[str, tuple[TaskStatus, str]] = {
    "completed": (TaskStatus.COMPLETED, REASON_COMPLETED),
    "failed": (TaskStatus.FAILED, REASON_FAILED),
    "cancelled": (TaskStatus.CANCELLED, REASON_CANCELLED),
    "usage_limited": (TaskStatus.USAGE_LIMITED, REASON_USAGE_LIMITED),
}


@dataclass(frozen=True)
class SchedulerLimits:
    """The bounds one worker runs under. Explicit, because "concurrency" is not a default."""

    max_concurrent: int = 1
    max_per_namespace: int = 1
    claim_ttl_seconds: float = 60.0
    #: A task older than this is tried before a newer task of higher priority. It is what keeps a
    #: steady stream of urgent work from starving everything else.
    priority_aging_seconds: float = 30.0
    #: How many queued tasks one decision looks at. Bounded so a huge backlog does not turn one
    #: scheduling decision into a full scan.
    read_limit: int = 64
    #: How long ``stop()`` waits for the drives it already started.
    stop_grace_seconds: float = 5.0

    def __post_init__(self) -> None:
        for name in ("max_concurrent", "max_per_namespace", "read_limit"):
            if getattr(self, name) < 1:
                raise ContractError(f"scheduler limit {name} must be at least 1")
        for name in ("claim_ttl_seconds", "priority_aging_seconds", "stop_grace_seconds"):
            if getattr(self, name) < 0:
                raise ContractError(f"scheduler limit {name} cannot be negative")


@dataclass(frozen=True)
class ScheduleDecision:
    """Which task a worker chose, why, and what it passed over."""

    task_id: str = ""
    namespace: str = ""
    reason: str = ""
    skipped: tuple[Mapping[str, Any], ...] = ()
    effective_priority: float = 0.0

    @property
    def empty(self) -> bool:
        return not self.task_id


@dataclass(frozen=True)
class WorkResult:
    """What one ``run_once`` did."""

    task_id: str
    namespace: str
    outcome: str
    detail: str = ""
    run_id: str = ""
    task_status: TaskStatus | None = None
    claimed: bool = False

    def to_record(self) -> Mapping[str, Any]:
        return {
            "task_id": self.task_id,
            "namespace": self.namespace,
            "outcome": self.outcome,
            "detail": self.detail,
            "run_id": self.run_id,
            "task_status": None if self.task_status is None else self.task_status.value,
            "claimed": self.claimed,
        }


def _default_holder() -> str:
    import os
    import socket

    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class LocalWorker:
    """A worker the host runs explicitly: no daemon, no background thread, no implicit loop.

    ``harness_for`` builds the harness for one task, so the worker never constructs a model
    request itself and the same Runner/harness is used however a task is entered (fresh, resumed
    from a pause, recovered from a crash).
    """

    def __init__(
        self,
        control: Any,
        harness_for: Callable[[TaskRecord, str], Any],
        *,
        limits: SchedulerLimits | None = None,
        clock: Callable[[], float] = time.time,
        holder: str | None = None,
        principal: str = "worker",
        child_barrier: Callable[[TaskRecord], Awaitable[TaskRecord | None]] | None = None,
    ) -> None:
        if control is None:
            raise ContractError("a worker needs a control store")
        if not callable(harness_for):
            raise ContractError("a worker needs a harness factory: (task, attempt_id) -> harness")
        self._control = control
        self._harness_for = harness_for
        self.limits = limits or SchedulerLimits()
        self._clock = clock
        self.holder = holder or _default_holder()
        self._principal = principal
        #: Callable by the child coordinator: before a parent settles as completed, it may turn
        #: the parent into a durable join instead (plan §10.3). ``None`` means the host does not
        #: opt into the barrier, and the task settles as its run says.
        self._child_barrier = child_barrier
        self._stopped = False
        #: task ids this process is driving right now, with the harness that is driving them.
        self._inflight: dict[str, Any] = {}
        #: the task records of the same drives, so a namespace's in-flight count is the count of
        #: *tasks*, not of opaque harness objects (which carry no namespace).
        self._inflight_tasks: dict[str, TaskRecord] = {}
        self._last_namespace: str = ""
        self._claims: dict[str, DispatchClaim] = {}
        self.decisions: list[ScheduleDecision] = []

    # -- the small library surface the host uses ---------------------------------------

    async def submit(self, admission: TaskAdmission) -> TaskRecord:
        """Admit a task (and its initial input) — the same idempotent path the store owns."""
        return await self._control.admit_task(admission)

    async def submit_input(self, admission: InputAdmission) -> Any:
        """Admit an input for a task this worker may later pick up."""
        return await self._control.admit_input(admission)

    async def query(self, task_id: str) -> TaskRecord | None:
        return await self._control.read_task(task_id)

    @property
    def inflight(self) -> tuple[str, ...]:
        return tuple(sorted(self._inflight))

    @property
    def slots(self) -> Mapping[str, int]:
        """How many slots are in use, globally and per namespace."""
        per_namespace: dict[str, int] = {}
        for record in self._inflight_tasks.values():
            namespace = record.namespace
            per_namespace[namespace] = per_namespace.get(namespace, 0) + 1
        return {"total": len(self._inflight_tasks), **per_namespace}

    async def stop(self, *, grace_seconds: float | None = None) -> tuple[str, ...]:
        """Stop taking new claims; give the drives already running a bounded grace.

        The claims of drives that do not finish inside the grace are left to expire rather than
        released: releasing one would hand the task to another worker while a drive that might
        still be dispatching holds it. An expired claim means the *scheduling* right lapsed — it
        says nothing about whether the external work stopped.
        """
        self._stopped = True
        grace = self.limits.stop_grace_seconds if grace_seconds is None else grace_seconds
        pending = [task for task in self._inflight.values()]
        deadline = self._clock() + grace
        while pending and self._clock() < deadline:
            pending = [task for task in pending if not getattr(task, "done", lambda: True)()]
            if pending:
                await asyncio.sleep(min(0.05, max(0.0, deadline - self._clock())))
        leftover = tuple(sorted(self._inflight))
        for task_id in leftover:
            self._inflight.pop(task_id, None)
            self._inflight_tasks.pop(task_id, None)
        return leftover

    async def wake(self, task_id: str) -> TaskRecord | None:
        """Make a waiting task schedulable again, once the fact it waited for is persisted.

        The *host* (or the component that owns the awaited fact) calls this: the worker does not
        poll an approval store or a child table, because a scheduler that guesses when a wait is
        over would be inventing the very fact it is waiting for. A cancel request wins over a
        wake — the task goes to the cancellation path instead.
        """
        task = await self._control.read_task(task_id)
        if task is None or task.status is not TaskStatus.WAITING:
            return task
        if task.cancel_requested_at is not None:
            return await self.cancel(task_id, reason="cancellation was requested while waiting")
        moved = task.transition(
            TaskStatus.QUEUED, reason="external_fact_persisted", at=self._clock()
        )
        return await self._control.save_task(moved, expected_status=task.status)

    async def cancel(self, task_id: str, *, reason: str = "") -> TaskRecord | None:
        """Record a cancellation request and settle what can be settled without a driver."""
        task = await self._control.read_task(task_id)
        if task is None or task.is_terminal:
            return task
        requested = task.request_cancel(at=self._clock())
        updated = await self._control.save_task(requested, expected_status=task.status)
        if task.status in {TaskStatus.QUEUED, TaskStatus.WAITING}:
            # Nothing was dispatched, so there is nothing to stop: the request is recorded and
            # then settled. Two steps, because the table says cancellation is a request first —
            # and because a reader must be able to see that the request happened.
            return await self._settle_cancel(updated)
        if task.status is TaskStatus.RUNNING:
            harness = self._inflight.get(task_id)
            if harness is not None:
                await harness.cancel(task.run_id, reason or "cancelled")
            # A run this worker cannot reach is not marked cancelled: it may still be running
            # somewhere else, and the request stays in the record for whoever can act on it.
        return await self._control.read_task(task_id)

    # -- scheduling --------------------------------------------------------------------

    async def run_once(self, *, now: float | None = None) -> WorkResult | None:
        """Try to claim and drive one task. ``None`` means there was nothing to do."""
        if self._stopped:
            return None
        at = self._clock() if now is None else now
        decision = await self._choose(at)
        if decision.empty:
            return None
        task = await self._control.read_task(decision.task_id)
        if task is None:
            return None
        if task.status is not TaskStatus.QUEUED and not (
            task.status is TaskStatus.RUNNING
            and await self._claim_lapsed(task.task_id, at)
        ):
            return None
        claim = await self._control.claim_task(
            task.task_id,
            holder=self.holder,
            ttl_seconds=self.limits.claim_ttl_seconds,
            now=at,
        )
        if claim is None:
            # Another worker holds a live claim: this is not an error and not a retry.
            self.decisions.append(decision)
            return WorkResult(
                task_id=task.task_id,
                namespace=task.namespace,
                outcome="held_elsewhere",
                detail="another worker holds a live claim for this task",
                run_id=task.run_id,
            )
        self._claims[task.task_id] = claim
        self._inflight[task.task_id] = None
        self._inflight_tasks[task.task_id] = task
        try:
            return await self._drive_claimed(task, claim, at)
        finally:
            self._inflight.pop(task.task_id, None)
            self._inflight_tasks.pop(task.task_id, None)
            self._claims.pop(task.task_id, None)
            await self._release(claim)

    async def run_until_idle(self, *, max_rounds: int = 64) -> tuple[WorkResult, ...]:
        """Drive tasks until nothing is schedulable, or the round bound is reached."""
        results: list[WorkResult] = []
        for _round in range(max_rounds):
            result = await self.run_once()
            if result is None:
                break
            results.append(result)
            if self._stopped:
                break
        return tuple(results)

    async def _claim_lapsed(self, task_id: str, at: float) -> bool:
        """True when no live claim holds this task: the recoverable-orphan case."""
        claims = getattr(self._control, "_load_claim_locked", None)
        if callable(claims):
            row = claims(task_id)
            return row is None or float(row[5]) <= at
        return True

    async def _choose(self, at: float) -> ScheduleDecision:
        """Pick the next task: caps first, then priority with aging, then round-robin."""
        if len(self._inflight) >= self.limits.max_concurrent:
            return ScheduleDecision(reason="the global concurrency limit is full")
        candidates = list(
            await self._control.read_schedulable(limit=self.limits.read_limit, now=at)
        )
        inflight_namespaces: dict[str, int] = {}
        for record in self._inflight_tasks.values():
            namespace = record.namespace
            inflight_namespaces[namespace] = inflight_namespaces.get(namespace, 0) + 1
        eligible: list[tuple[float, TaskRecord]] = []
        skipped: list[Mapping[str, Any]] = []
        namespaces: list[str] = []
        for task in candidates:
            if task.task_id in self._inflight:
                skipped.append({"task_id": task.task_id, "reason": "already driven here"})
                continue
            if task.task_id in self._claims:
                skipped.append({"task_id": task.task_id, "reason": "already claimed here"})
                continue
            used = inflight_namespaces.get(task.namespace, 0)
            if used >= self.limits.max_per_namespace:
                skipped.append(
                    {
                        "task_id": task.task_id,
                        "reason": f"namespace {task.namespace!r} is at its concurrency limit",
                    }
                )
                continue
            aged = (at - task.created_at) >= self.limits.priority_aging_seconds > 0
            # A task past the aging window is tried before any not-yet-aged task; within a
            # bucket, priority decides, then the order the tasks were admitted.
            effective = (1.0 if aged else 0.0, float(task.priority))
            key = effective[0] * 1_000_000 + effective[1]
            eligible.append((key, task))
            if task.namespace not in namespaces:
                namespaces.append(task.namespace)
        if not eligible:
            return ScheduleDecision(reason="no queued task is eligible", skipped=tuple(skipped))
        # Round-robin over namespaces: start after the last one served, so one tenant with a long
        # backlog cannot starve the others.
        if self._last_namespace in namespaces:
            namespaces = namespaces[namespaces.index(self._last_namespace) + 1 :] + namespaces[
                : namespaces.index(self._last_namespace) + 1
            ]
        chosen_namespace = namespaces[0]
        chosen = max(
            (item for item in eligible if item[1].namespace == chosen_namespace),
            key=lambda item: (item[0], -item[1].created_at),
        )
        self._last_namespace = chosen_namespace
        return ScheduleDecision(
            task_id=chosen[1].task_id,
            namespace=chosen_namespace,
            reason=(
                "the oldest eligible task past the aging window"
                if (at - chosen[1].created_at) >= self.limits.priority_aging_seconds > 0
                else "the highest-priority eligible task, in admission order"
            ),
            skipped=tuple(skipped),
            effective_priority=chosen[0],
        )

    # -- driving -----------------------------------------------------------------------

    async def _drive_claimed(
        self, task: TaskRecord, claim: DispatchClaim, at: float
    ) -> WorkResult:
        """Re-read, classify, then drive: never start a new run to fake a retry."""
        fresh = await self._control.read_task(task.task_id)
        if fresh is None:
            return WorkResult(task.task_id, task.namespace, "vanished")
        if fresh.is_terminal:
            return WorkResult(
                task.task_id,
                fresh.namespace,
                "already_terminal",
                detail=f"the task ended as {fresh.status.value}",
                task_status=fresh.status,
                claimed=True,
            )
        if fresh.cancel_requested_at is not None and fresh.status in {
            TaskStatus.QUEUED,
            TaskStatus.WAITING,
        }:
            stored = await self._settle_cancel(fresh, at=at)
            return WorkResult(
                task.task_id,
                stored.namespace,
                "cancelled_before_start",
                detail="the cancellation request landed before anything was dispatched",
                task_status=stored.status,
                claimed=True,
            )
        if fresh.past_deadline(at):
            # The task's own deadline has passed: nothing is dispatched, not even a model
            # request, and the task settles as budget-limited rather than as a failed run.
            moved = fresh.transition(
                TaskStatus.USAGE_LIMITED,
                reason=REASON_USAGE_LIMITED,
                at=at,
                stop_code="deadline_exceeded",
            )
            stored = await self._control.save_task(moved, expected_status=fresh.status)
            return WorkResult(
                fresh.task_id,
                stored.namespace,
                "settled:usage_limited",
                detail="the task's deadline had already passed; nothing was dispatched",
                run_id=fresh.run_id,
                task_status=stored.status,
                claimed=True,
            )
        if fresh.status is TaskStatus.WAITING:
            return WorkResult(
                task.task_id,
                fresh.namespace,
                "waiting",
                detail=f"the task waits for {fresh.waiting_for or 'an external fact'}",
                task_status=fresh.status,
                claimed=True,
            )
        harness = self._harness_for(fresh, claim.attempt_id)
        self._inflight[fresh.task_id] = harness
        self._inflight_tasks[fresh.task_id] = fresh
        outcome = None
        try:
            prompt = await self._initial_prompt(fresh)
            existing = await self._run_record(harness, fresh.run_id)
            if existing is not None and not _is_terminal(existing):
                # A run already exists: recover it, do not start a second one. Whether that is a
                # pause, an orphaned batch or a live executor is the harness's classification.
                outcome = await harness.resume(fresh.run_id, {}, self._context(fresh, claim))
            elif existing is not None and _is_terminal(existing):
                # The run finished in an earlier drive (or the process died after the run's
                # terminal commit but before the task was settled). Settle the task from the
                # committed run outcome; leaving it running is what turns a completed run into
                # an endless failure to recover.
                return await self._settle(fresh, claim, existing)
            else:
                if not prompt:
                    raise ContractError(
                        f"task {fresh.task_id!r} has no initial input text to start a run with"
                    )
                outcome = await harness.start(
                    _request(prompt, fresh, now=at), self._context(fresh, claim)
                )
        except Exception as exc:  # noqa: BLE001 - a failed drive is a task fact, not a crash
            stored = await self._settle_failure(fresh, claim, exc)
            return WorkResult(
                fresh.task_id,
                fresh.namespace,
                "failed",
                detail=f"{type(exc).__name__}: {exc}",
                run_id=fresh.run_id,
                task_status=stored.status if stored else None,
                claimed=True,
            )
        finally:
            self._inflight.pop(fresh.task_id, None)
            self._inflight_tasks.pop(fresh.task_id, None)
        return await self._settle(fresh, claim, outcome)

    async def _settle(
        self, task: TaskRecord, claim: DispatchClaim, outcome: Any
    ) -> WorkResult:
        """Map the run's outcome onto the task's own state."""
        status = str(getattr(getattr(outcome, "status", None), "value", "") or "")
        stop_code = str(getattr(outcome, "stop_code", "") or "")
        current = await self._control.read_task(task.task_id)
        if current is None:
            return WorkResult(task.task_id, task.namespace, "vanished")
        if current.is_terminal:
            return WorkResult(
                task.task_id,
                current.namespace,
                "already_terminal",
                run_id=task.run_id,
                task_status=current.status,
                claimed=True,
            )
        if status == "suspended":
            # The outcome names the pending interaction; a pause without one is still a pause, so
            # the task says it waits for an external fact rather than inventing an empty reason.
            interaction = (
                getattr(outcome, "pending_interaction", None)
                or getattr(outcome, "interaction", None)
                or {}
            )
            child_task_id = (
                str(interaction.get("child_task_id") or "")
                if isinstance(interaction, Mapping)
                else ""
            )
            waiting_for = (
                f"child:{child_task_id}"
                if child_task_id
                else str(interaction.get("kind") if isinstance(interaction, Mapping) else "")
                or "external_fact"
            )
            moved = current.transition(
                TaskStatus.WAITING, reason=REASON_AWAITING, at=self._clock(), waiting_for=waiting_for
            )
            stored = await self._control.save_task(moved, expected_status=current.status)
            return WorkResult(
                task.task_id,
                stored.namespace,
                "waiting",
                detail=f"waiting for {waiting_for}",
                run_id=task.run_id,
                task_status=stored.status,
                claimed=True,
            )
        mapped = _RUN_TO_TASK.get(status)
        if (
            mapped is not None
            and mapped[0] is TaskStatus.COMPLETED
            and self._child_barrier is not None
        ):
            # A parent does not finish while its own children still run: the join is persisted
            # before any terminal state, and it is never achieved by reviving a finished task.
            held = await self._child_barrier(current)
            if held is not None:
                return WorkResult(
                    task.task_id,
                    held.namespace,
                    "waiting",
                    detail=f"waiting for {held.waiting_for}",
                    run_id=task.run_id,
                    task_status=held.status,
                    claimed=True,
                )
        if mapped is None:
            moved = current.transition(
                TaskStatus.FAILED,
                reason=REASON_FAILED,
                at=self._clock(),
                stop_code=stop_code or "unrecognised_run_outcome",
            )
        else:
            target, reason = mapped
            if current.cancel_requested_at is not None and target is TaskStatus.COMPLETED:
                # The cancellation lost the race to a run that finished: the facts win and the
                # request stays recorded, which is what "cancellation is a request" means.
                reason = REASON_COMPLETED
            moved = current.transition(
                target, reason=reason, at=self._clock(), stop_code=stop_code
            )
        try:
            stored = await self._control.save_task(moved, expected_status=current.status)
        except ContractError:
            stored = await self._control.read_task(task.task_id)
        return WorkResult(
            task.task_id,
            current.namespace,
            f"settled:{moved.status.value}",
            detail=stop_code,
            run_id=task.run_id,
            task_status=None if stored is None else stored.status,
            claimed=True,
        )

    async def _settle_cancel(self, task: TaskRecord, *, at: float | None = None) -> TaskRecord:
        """Request, then settle: ``queued|waiting -> cancelling -> cancelled``."""
        moment = self._clock() if at is None else at
        current = task
        if current.cancel_requested_at is None:
            current = current.transition(
                TaskStatus.CANCELLING,
                reason=REASON_CANCEL_REQUESTED,
                at=moment,
                cancel_requested_at=moment,
            )
        elif current.status is not TaskStatus.CANCELLING:
            current = replace(current, cancel_requested_at=current.cancel_requested_at)
            if current.status in {TaskStatus.QUEUED, TaskStatus.WAITING}:
                current = current.transition(
                    TaskStatus.CANCELLING, reason=REASON_CANCEL_REQUESTED, at=moment
                )
        try:
            stored = await self._control.save_task(current, expected_status=task.status)
        except ContractError:
            return await self._control.read_task(task.task_id) or task
        settled = stored.transition(TaskStatus.CANCELLED, reason=REASON_CANCELLED, at=moment)
        try:
            return await self._control.save_task(settled, expected_status=stored.status)
        except ContractError:
            return await self._control.read_task(task.task_id) or stored

    async def _settle_failure(
        self, task: TaskRecord, claim: DispatchClaim, exc: BaseException
    ) -> TaskRecord | None:
        """A drive that raised is a task failure — with the right reason, not a silent retry."""
        current = await self._control.read_task(task.task_id)
        if current is None or current.is_terminal:
            return current
        from .types import ClaimLost

        if isinstance(exc, ClaimLost):
            # The scheduling right was lost and the store refused the write: the task returns to
            # the queue for whoever holds it now, and nothing is reported as failed.
            moved = current.transition(TaskStatus.QUEUED, reason=REASON_RIGHT_LOST, at=self._clock())
        else:
            moved = current.transition(TaskStatus.FAILED, reason=REASON_FAILED, at=self._clock())
        try:
            return await self._control.save_task(moved, expected_status=current.status)
        except ContractError:
            return await self._control.read_task(task.task_id)

    # -- helpers -----------------------------------------------------------------------

    def _context(self, task: TaskRecord, claim: DispatchClaim) -> RunContext:
        return RunContext(
            identity=RunIdentity(
                namespace=task.namespace,
                session_id=task.session_id,
                run_id=task.run_id,
                attempt_id=claim.attempt_id,
            ),
            principal=Principal(subject=self._principal),
        )

    async def _run_record(self, harness: Any, run_id: str) -> Any:
        """The run's own record, as the authority on its status.

        ``load_run`` returns a ``VersionedRun`` (state + version); the status lives *inside* the
        state. Returning the wrapper made ``_is_terminal`` read a missing attribute and report
        every finished run as still resumable — which is how recovery turned a completed run into
        a failed task. A state that is not a full record (a host's own persistence, or a scripted
        harness) is read for the status it declares; the worker decides start/resume/settle from
        that, and never fabricates a verdict.
        """
        persistence = getattr(harness, "_persistence", None)
        if persistence is None:
            return None
        loaded = await persistence.load_run(run_id)
        if loaded is None:
            return None
        state = getattr(loaded, "state", None)
        if state is None:
            state = loaded
        if not isinstance(state, Mapping):
            return None
        from ..runtime.harness import RunRecord

        try:
            return RunRecord.from_record(state)
        except Exception:  # noqa: BLE001 - a partial state is read for its status, not rejected
            return _RunView.from_state(state)

    async def _initial_prompt(self, task: TaskRecord) -> str:
        records = await self._control.read_inputs(task.task_id, after_sequence=-1, limit=200)
        for record in records:
            if record.kind is InputKind.INITIAL and record.state in {
                InputState.PENDING,
                InputState.APPLIED,
            }:
                return str(record.content.text or "")
        return ""

    async def _release(self, claim: DispatchClaim) -> None:
        with contextlib.suppress(Exception):
            await self._control.release_task_claim(
                claim.task_id, holder=claim.holder, attempt_id=claim.attempt_id
            )


class _RunView:
    """A minimal run view for a state that is not a full ``RunRecord``.

    The worker's decision needs only the status, the stop code and any pending interaction. A
    state that declares none of them is treated as non-terminal, so the next step is a resume
    rather than a signed-off completion.
    """

    __slots__ = ("status", "stop_code", "pending_interaction", "interaction")

    def __init__(
        self,
        status: Any,
        stop_code: str = "",
        pending_interaction: Any = None,
        interaction: Any = None,
    ) -> None:
        self.status = status
        self.stop_code = stop_code
        self.pending_interaction = pending_interaction
        self.interaction = interaction

    @classmethod
    def from_state(cls, state: Mapping[str, Any]) -> "_RunView":
        raw = str(state.get("status") or "running")
        return cls(
            status=SimpleNamespace(value=raw),
            stop_code=str(state.get("stop_code") or ""),
            pending_interaction=state.get("pending_interaction"),
            interaction=state.get("interaction"),
        )


def _is_terminal(record: Any) -> bool:
    status = getattr(record, "status", None)
    value = str(getattr(status, "value", status) or "")
    return value in {"completed", "failed", "cancelled", "usage_limited"}


def _request(prompt: str, task: TaskRecord, *, now: float | None = None) -> Any:
    """The invocation request for one task: its opening instruction and its remaining budget.

    The remaining time is computed *here*, in this process, from the persisted absolute deadline
    and the current wall clock, and handed to the harness as a duration. The harness then runs the
    invocation for the smaller of that and its own configured limit.
    """
    from ..runtime.harness import RunRequest

    remaining = task.remaining_seconds(time.time() if now is None else now)
    return RunRequest(prompt=prompt, deadline_seconds=remaining)


_ = (field, Awaitable, Sequence)
