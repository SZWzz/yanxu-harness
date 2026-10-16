"""The concrete harness: one run per identity, driven by the one model loop.

``DefaultHarness`` is the object a host actually calls. It owns nothing clever:

* **The host supplies identity.** ``RunContext`` carries namespace/session/run and
  the principal; the harness reads them and never derives them from model output.
  Tools come from the constructor, so a model cannot widen what it can see.
* **The host supplies the loop's dependencies.** ``stream_fn`` (a provider bridge),
  ``tools`` (core ``ToolSpec`` adapters), a ``Persistence`` store and optional
  listeners are all injected; there is no global default and no import of a host
  package.
* **The state is read from the store, not from memory.** ``state()`` answers from
  the persisted run record, so a *new* harness instance over the same store sees
  the same run — and a run this process never drove is still readable.
* **A duplicate submission is a refusal, not a second loop.** The same request for
  the same run id reuses the stored state; different input is refused and the stored
  run is left untouched; a run that is currently active raises ``RunBusy``.

Suspension and resume
---------------------

A batch containing a call that waits for a decision (or an operation whose outcome
is unproven) stops the run **before the next model request**. At that moment the
harness commits, in one transaction, the run state, the recorded batch — every
declared call with its classification and the interactions waiting on it — and the
durable events. That is what a restarted process reads.

Resuming never re-runs something that may already have happened:

* a call with a receipt has its recorded result reused, never re-dispatched;
* a waiting call is re-dispatched through the *same* gateway with the *same*
  operation id, so an approval the trusted host recorded makes the gateway execute
  it once and every later attempt replays that receipt;
* an unproven operation is settled only by business evidence from a host-supplied
  reconciler. An operator saying "continue" is not evidence, and a client-supplied
  ``approved`` flag is never read as authorisation — the approval store is the only
  source of that fact (checked by the gateway, not here);
* drifted fingerprints (config, tools, instructions, policy, exposure) refuse the
  resume with the drift named, and an expired approval goes back to the author.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
import socket
import time
import uuid
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable, Mapping, Sequence

from ..core.agent_loop import AgentLoopConfig, run_agent_loop_continue_result, run_agent_loop_result
from ..core.execution import (
    CONTEXT_STATE_SCHEMA_VERSION,
    QUIESCENT_PHASES,
    BatchCallState,
    CallProgress,
    ExecutionState,
    Measurement,
    Phase,
    declared_batch,
    pending_work_of,
    state_from_record,
    state_to_record,
)
from ..core.runner import Runner
from ..core.tool_execution import tool_call_blocks
from ..core.messages import content_text, create_initial_system_message
from ..core.outcome import (
    TERMINAL_STATUSES,
    LoopResult,
    LoopStop,
    RunStatus,
    assert_transition,
)
from ..core.types import (
    AgentContext,
    Model,
    StopReason,
    TextContent,
    ToolResultMessage,
    UserMessage,
    message_from_json,
    message_to_json,
    usage_to_json,
)
from ..errors import ContractError, RunBusy, RunNotFound, RunRequestDrift
from ..events import Event, EventKind
from ..storage.memory import InMemoryPersistence
from ..storage.ports import Lease, Persistence, StorageConflict, StoredEvent
from ..versioning import fingerprint
from .batch import (
    BatchCall,
    CallOutcome,
    Interaction,
    InteractionKind,
    RunBatch,
    batch_from_snapshot,
    classify_status,
    record_batch,
)
from .checkpoint import CheckpointToolCall, PendingInteractionRef, RunCheckpoint, resolve_resume
from .context import RunContext, RunIdentity
from .envelope import EventEnvelope, EventFence
from .harness import (
    RunOutcome,
    RunRecord,
    RunRequest,
    RunState,
    validate_max_concurrent_tools,
    validate_max_turns,
)

__all__ = ["DefaultHarness", "request_fingerprint", "tool_fingerprint", "primary_interaction"]

Listener = Callable[[Any], Awaitable[None] | None]
RunListener = Callable[[Event], Awaitable[None] | None]
#: ``(call, details) -> evidence or None``: the host's authoritative business check
#: for an operation the harness could not prove. Returning ``None`` means "still
#: unproven", which keeps the run suspended instead of guessing.
Reconciler = Callable[[BatchCall, Mapping[str, Any]], Any]


@dataclass(frozen=True)
class _Recovered:
    """The recovery pre-phase stopped without a verdict, and this is what it proved.

    A recovery invocation runs *inside* the run's bounded drive task (see
    ``_recover_then_continue``), because its ledger reads, its host reconciler and the
    calls it redispatches are all work the deadline and the cancellation token must
    reach. When that work ends in "a question has to be answered first" rather than in a
    loop result, it returns this instead of a verdict, and the caller commits the pause
    outside the bounded drive — a final evidence commit is not the thing a timeout is
    allowed to throw away.
    """

    batch: RunBatch
    blockers: tuple[str, ...] = ()


def request_fingerprint(request: RunRequest) -> str:
    """A stable fingerprint of everything a run's identity is bound to.

    Two submissions with the same fingerprint are the same request (a retry);
    anything else is drift. ``max_turns`` is part of it: a second call with a
    smaller budget would silently truncate a run that was already authorised to
    continue.
    """
    try:
        messages = [message_to_json(message) for message in request.messages]
    except Exception as exc:  # noqa: BLE001 - an unreportable message shape
        raise ContractError(
            "RunRequest.messages must be core messages (system/user/assistant/toolResult); "
            f"this one cannot be fingerprinted: {exc}"
        ) from exc
    return fingerprint(
        {
            "prompt": request.prompt,
            "messages": messages,
            "max_turns": request.max_turns,
            "labels": dict(request.labels),
        }
    )


def tool_fingerprint(tools: Sequence[Any]) -> str:
    """Fingerprint the *model-visible* tool set, which is what an approval binds to."""
    return fingerprint(
        sorted(
            (
                {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": dict(tool.parameters or {}),
                    "execution_mode": getattr(tool, "execution_mode", None),
                    "replay": getattr(tool, "replay", None),
                }
                for tool in tools
            ),
            key=lambda item: item["name"],
        )
    )


def primary_interaction(batch: RunBatch) -> Mapping[str, Any] | None:
    """The interaction a suspended outcome reports, as a public mapping."""
    blocking = batch.blocking()
    if not blocking or not batch.interactions:
        return None
    wanted = {call.call_id for call in blocking}
    interaction = next(item for item in batch.interactions if item.call_id in wanted)
    payload: dict[str, Any] = {
        "interaction_id": interaction.interaction_id,
        "kind": interaction.kind.value,
        "call_id": interaction.call_id,
        "tool_name": interaction.tool_name,
        "operation_id": interaction.operation_id,
        "detail": interaction.detail,
    }
    if interaction.approval_binding_key:
        payload["approval_binding_key"] = interaction.approval_binding_key
    if interaction.preview is not None:
        payload["preview"] = dict(interaction.preview)
    if interaction.expires_at is not None:
        payload["expires_at"] = interaction.expires_at
    return payload


class _Drive:
    """Everything one driven invocation owns, and everything it must release.

    Three things live here because they have the same lifetime:

    * the **fences** that decide whether an event is still current (one for core
      progress frames, one for the durable events read back from the store);
    * the **lease** this process holds on the run, so a second worker cannot also
      drive it and a writer that lost its lease cannot commit;
    * the **task** the loop runs in, so cancelling the run can interrupt a blocked
      await instead of only being noticed between steps.
    """

    def __init__(self, record: "RunRecord", token: Any, *, lease: "Lease | None" = None) -> None:
        self.record = record
        self.token = token
        self.lease = lease
        self.task: "asyncio.Task[Any] | None" = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.core_fence = EventFence(
            namespace=record.namespace, run_id=record.run_id, attempt_id=record.attempt_id
        )
        self.durable_fence = EventFence(
            namespace=record.namespace, run_id=record.run_id, attempt_id=record.attempt_id
        )
        #: Messages the loop announced, in order. Used when a run is interrupted:
        #: cancellation is not amnesia, and the partial transcript is a fact.
        self.messages: list[Any] = []
        #: Messages announced since the last successful progress commit. The final
        #: record appends exactly these when the invocation was interrupted, so a
        #: message that a commit already carries is never written twice.
        self.uncommitted: list[Any] = []
        self._sequence = 0
        self.refusals: list[str] = []
        #: Measurements already written as durable facts. In-memory on purpose: it is the
        #: driver's own bookkeeping about what it has handed to the store, not a fact about
        #: the run, so it does not belong in the execution state.
        self.measurements_emitted: set[str] = set()

    def envelope(self, kind: str, payload: Mapping[str, Any], *, durable: bool = False) -> EventEnvelope:
        self._sequence += 1
        return EventEnvelope(
            namespace=self.record.namespace,
            run_id=self.record.run_id,
            attempt_id=self.record.attempt_id,
            kind=kind,
            payload=dict(payload),
            sequence=self._sequence,
            durable=durable,
        )

    def cancel(self, reason: str) -> None:
        """Request a stop from any thread. The token is enough; the task is a bonus.

        The token is what every tool, provider stream and loop check cooperatively.
        Cancelling the task as well interrupts an await that has not reached a check
        yet — scheduled on the loop's thread, because ``Task.cancel`` is not
        thread-safe and a host may cancel from an HTTP handler.
        """
        self.token.cancel(reason)
        task, loop = self.task, self.loop
        if task is not None and loop is not None and not task.done():
            loop.call_soon_threadsafe(task.cancel)


class DefaultHarness:
    """One harness per deployment: runs model loops and persists their lifecycles."""

    def __init__(
        self,
        *,
        stream_fn: Callable[..., Any],
        tools: Sequence[Any] = (),
        persistence: Persistence | None = None,
        listener: Listener | None = None,
        #: Receives the *durable* run events, after their transaction committed.
        run_listener: RunListener | None = None,
        model: Model | None = None,
        system_prompt: str | None = None,
        stop_after_turn: Callable[[Any], Any] | None = None,
        suspend_after_batch: Callable[[Any, Any], Any] | None = None,
        fingerprints: Mapping[str, str] | None = None,
        reconcile: Reconciler | None = None,
        clock: Callable[[], float] = time.time,
        #: How long this process claims a run for. A drive that outlives its lease
        #: fails its next commit with a fence conflict instead of writing over the
        #: worker that took over, so the TTL must cover a normal drive.
        lease_ttl_seconds: float = 300.0,
        #: Identifies this worker in the lease. Defaults to host/pid/random.
        holder: str | None = None,
        #: Optional hard bound on one invocation, independent of the lease.
        max_runtime_seconds: float | None = None,
        #: Largest number of tool calls of one batch that may execute at the same time.
        #: ``None`` keeps the previous behaviour (every call of a parallel batch runs
        #: concurrently); the limit never changes event or message ordering.
        max_concurrent_tools: int | None = None,
        #: Optional context preparation (plan section 4.2). The object must expose
        #: ``prepare(payload, *, scope=...)`` — ``ContextController`` does — and is called
        #: once per turn, before anything is sent. Its result is committed with the state
        #: it describes and stored *beside* the transcript: a projection never replaces
        #: ``RunRecord.messages``. No controller means the transcript is the request view,
        #: which is the previous behaviour.
        context_controller: Any | None = None,
    ) -> None:
        if stream_fn is None:
            raise ContractError(
                "DefaultHarness requires an explicit stream_fn (a provider bridge); "
                "there is no global default stream"
            )
        self._stream_fn = stream_fn
        self._tools = tuple(tools)
        self._persistence: Persistence = (
            persistence if persistence is not None else InMemoryPersistence()
        )
        self._listener = listener
        self._run_listener = run_listener
        self._model = model
        self._system_prompt = system_prompt
        # Host policy seams the loop already supports; the harness only wires them.
        self._stop_after_turn = stop_after_turn
        self._host_suspend_hook = suspend_after_batch
        # Fingerprints the harness cannot derive (a delegated gateway's policy
        # version, a host-level configuration digest) are supplied explicitly.
        self._extra_fingerprints = dict(fingerprints or {})
        self._reconcile = reconcile
        self._clock = clock
        if lease_ttl_seconds <= 0:
            raise ContractError("lease_ttl_seconds must be positive")
        if max_runtime_seconds is not None and max_runtime_seconds <= 0:
            raise ContractError("max_runtime_seconds must be positive when declared")
        self._lease_ttl = lease_ttl_seconds
        self._max_runtime = max_runtime_seconds
        validate_max_concurrent_tools(max_concurrent_tools)
        self._max_concurrent_tools = max_concurrent_tools
        if context_controller is not None and not callable(
            getattr(context_controller, "prepare", None)
        ):
            raise ContractError(
                "context_controller must expose prepare(payload, *, scope=...); a bare "
                "callable has nowhere to put the scope the projection must be isolated by"
            )
        self._context_controller = context_controller
        self._holder = holder or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        #: Run ids this process is currently driving, mapped to the token a
        #: cancellation cancels. A run in here is active *here*; one in the store
        #: but not here belongs to another driver (or to a dead process).
        self._active: dict[str, _Drive] = {}
        self._fanout_failures: list[str] = []
        self._fence_refusals: list[str] = []

    # -- introspection --------------------------------------------------

    @property
    def durability(self) -> Any:
        """The store's durability. ``VOLATILE`` means no restart recovery."""
        return self._persistence.durability

    @property
    def tools(self) -> tuple[Any, ...]:
        return self._tools

    @property
    def fence_refusals(self) -> tuple[str, ...]:
        """Events refused by the current-attempt fence, with the reason.

        A dropped stale event is not a silent no-op: a host that wants to know why a
        progress update never arrived can read this instead of guessing.
        """
        return tuple(self._fence_refusals)

    @property
    def fanout_failures(self) -> tuple[str, ...]:
        """Event publications that failed after their transaction committed.

        A failed fanout is not a lost fact: the events are in the store and
        :meth:`replay_run_events` re-publishes them. Nothing is rolled back and no
        tool is re-run to "make up" for a missed notification.
        """
        return tuple(self._fanout_failures)

    def fingerprints(self) -> Mapping[str, str]:
        """The current arrangement's fingerprints, as a resume compares them."""
        values = {
            "config": fingerprint(
                {
                    "model": getattr(self._model, "id", "") or "",
                    "system_prompt": self._system_prompt or "",
                }
            ),
            "tools": tool_fingerprint(self._tools),
            "instructions": fingerprint(self._system_prompt or ""),
            "policy": self._extra_fingerprints.get("policy", ""),
        }
        values.update({key: value for key, value in self._extra_fingerprints.items() if key != "policy"})
        return values

    def exposure_manifest(self) -> Mapping[str, Any]:
        """The model-visible tool manifest an approval was granted against."""
        return {"tools": sorted(tool.name for tool in self._tools)}

    def arrangement(self) -> Mapping[str, Any]:
        """The deployment, as the plain data a batch is bound to.

        One method produces it, and it is called in exactly two places: when an
        invocation records where it was before dispatching, and when a recovery compares
        the recorded baseline with the deployment it is running in. Nothing else derives
        a baseline of its own, which is what keeps "recorded" and "compared against" the
        same fact instead of two projections that happen to agree today.
        """
        return {**self.fingerprints(), "exposure_manifest": dict(self.exposure_manifest())}

    # -- lifecycle ------------------------------------------------------

    async def start(self, request: RunRequest, context: RunContext) -> RunOutcome:
        validate_max_turns(request.max_turns)
        identity = context.identity
        wanted = request_fingerprint(request)
        record, version = await self._load(identity.run_id)
        if record is not None:
            self._assert_same_identity(record, context)
            if record.request_fingerprint != wanted:
                raise RunRequestDrift(
                    identity.run_id,
                    "the stored run was started with a different prompt, messages, "
                    "max_turns or labels",
                )
            if record.status is RunStatus.RUNNING:
                raise RunBusy(identity.run_id, "this run id already has an active invocation")
            # Terminal or suspended: reuse the stored state instead of running again.
            return _outcome_of(record)

        seed = self._seed_messages(request)
        fresh = RunRecord(
            run_id=identity.run_id,
            namespace=identity.namespace,
            session_id=identity.session_id,
            status=RunStatus.RUNNING,
            attempt_id=identity.attempt_id,
            labels=dict(request.labels),
            request_fingerprint=wanted,
            # The context the request is built from is durable from the first commit:
            # "before the model is asked" includes the conversation it is asked about.
            messages=tuple(_as_records(seed)),
            updated_at=self._clock(),
        )
        holder: dict[str, Any] = {"record": fresh, "version": None}
        lease = await self._claim(identity.run_id)
        holder["lease"] = lease
        scope = _Drive(fresh, context.cancellation, lease=lease)
        holder["scope"] = scope
        try:
            _version, written = await self._commit(
                holder,
                expected_version=None,
                events=(
                    (
                        "run_started",
                        {"run_id": identity.run_id, "attempt": identity.attempt_id},
                    ),
                ),
            )
        except StorageConflict as exc:
            # Someone else inserted this run id first: that is "busy", not "lost".
            await self._release(lease)
            raise RunBusy(identity.run_id, "another invocation created this run first") from exc
        await self._publish(written, scope)

        self._active[identity.run_id] = scope
        try:
            result = await self._drive(request, context, seed, holder)
            finished = _apply_result(
                holder["record"],
                result,
                seed,
                self._clock(),
                final_state=holder.get("last_state"),
                trailing=scope.uncommitted,
                context_state=holder.get("interrupted_context_state"),
            )
            assert_transition(holder["record"].status, finished.status)
            # The lease is released *after* the terminal commit: releasing it first
            # would make the run's own final write a stale (fenced) write, which is
            # exactly what the store correctly refuses.
            await self._commit_final(holder, finished)
        finally:
            self._active.pop(identity.run_id, None)
            await self._release(lease)
        return _outcome_of(finished)

    async def resume(
        self, run_id: str, resolution: Mapping[str, Any], context: RunContext
    ) -> RunOutcome:
        """Continue a suspended run under a new attempt.

        ``resolution`` is deliberately **not** a source of authority. It may carry
        audit hints (which interaction the caller thinks it is answering, the
        operator's note), but an approval only counts when the trusted host wrote it
        into the ``ApprovalStore`` the gateway reads, and an unproven operation is
        settled only by the host's reconciler.
        """
        record, version = await self._load(run_id)
        if record is None:
            raise RunNotFound(run_id)
        self._assert_same_identity(record, context)
        if record.cancel_requested is not None and not record.terminal:
            # A cancellation that arrived while no local driver could act on it is a
            # durable fact about *this* run. Whether it was paused or still running when
            # it arrived, continuing past it would run the very calls the host asked to
            # stop, so the request is turned into the cancellation it already is:
            # committed as a terminal fact, dispatching nothing, keeping the batch as
            # evidence of what was waiting.
            return await self._commit_observed_cancellation(record, context)
        if record.status is RunStatus.RUNNING:
            return await self._recover_orphan(run_id, record, context)
        if record.status is not RunStatus.SUSPENDED:
            raise ContractError(
                f"run {run_id!r} is {record.status.value}; only a suspended run can be resumed"
            )
        if record.batch is None:
            raise ContractError(
                f"run {run_id!r} is suspended but carries no recorded tool batch, so the calls "
                "it stopped in cannot be identified; refusing to guess which of them ran"
            )
        _validate_resolution(resolution)
        attempt = context.identity.attempt_id
        # The lease comes first, and every check below is made against the record read
        # *under* it: what this worker is about to drive is the record it holds the fence
        # for, not the one it happened to read a moment earlier.
        lease = await self._claim(run_id, what="resume")
        try:
            current, version = await self._load(run_id)
            if current is None:
                raise RunNotFound(run_id)
            if current.terminal:
                return _outcome_of(current)
            if current.cancel_requested is not None:
                holder: dict[str, Any] = {
                    "record": current,
                    "version": version,
                    "lease": lease,
                }
                holder["scope"] = _Drive(current, context.cancellation, lease=lease)
                return await self._write_cancellation(holder, context)
            if current.status is not RunStatus.SUSPENDED:
                raise ContractError(
                    f"run {run_id!r} became {current.status.value} while this worker was taking "
                    "its lease; refusing to resume a run that is no longer waiting"
                )
            self._assert_same_identity(current, context)
            if current.batch is None:
                raise ContractError(
                    f"run {run_id!r} carries no recorded tool batch, so the calls it stopped in "
                    "cannot be identified; refusing to guess which of them ran"
                )
            batch = RunBatch.from_record(current.batch)
            identity = RunIdentity(
                namespace=current.namespace,
                session_id=current.session_id,
                run_id=current.run_id,
                attempt_id=attempt,
            )
            if current.execution:
                snapshot = state_from_record(current.execution)
                self._assert_execution_snapshot_agrees(current, batch)
            else:
                # A record written before the snapshot existed (F10): the batch is the
                # authority on which calls ran, and the transcript is the authority on the
                # conversation. Both are committed facts; nothing here is inferred from
                # the deployment that is resuming.
                snapshot = _state_without_snapshot(current, batch, identity)
            # The arrangement is checked *before* dispatching anything: a drifted tool set
            # or policy means the approval was granted for something else, and the baseline
            # is the one the batch was declared under, read back from the durable facts —
            # never re-derived from the deployment that is resuming.
            _assert_arrangement(
                batch,
                identity,
                _recorded_arrangement(current, batch),
                self.fingerprints(),
                self.exposure_manifest(),
                self._clock(),
            )
            holder = {"record": current, "version": version, "lease": lease}
            scope = _Drive(replace(current, attempt_id=attempt), context.cancellation, lease=lease)
            holder["scope"] = scope
            self._active[run_id] = scope
            try:
                outcome = await self._run_task(
                    self._recover_then_continue(
                        holder, current, snapshot, batch, context, identity
                    ),
                    scope,
                )
                return await self._finish_recovered(holder, current, identity, outcome)
            finally:
                self._active.pop(run_id, None)
        finally:
            await self._release(lease)

    async def _commit_observed_cancellation(
        self, record: RunRecord, context: RunContext
    ) -> RunOutcome:
        """Turn a recorded cancellation into the cancellation the run already is.

        Nothing is dispatched and no tool result is fabricated: the run simply stops
        being resumable, with the reason it stopped recorded from the cancellation the
        host asked for. The recorded batch is left in place — it is the evidence of what
        was waiting, and deleting it would lose the answer to "what was about to run?".
        """
        note = dict(record.cancel_requested or {})
        reason = str(note.get("reason") or "cancelled")
        lease = await self._claim(record.run_id, what="cancel")
        try:
            current, version = await self._load(record.run_id)
            if current is None:
                raise RunNotFound(record.run_id)
            if current.terminal:
                # Another driver committed a verdict while this worker was taking the lease:
                # a terminal fact is never overwritten, not even by a cancellation.
                return _outcome_of(current)
            holder: dict[str, Any] = {"record": current, "version": version, "lease": lease}
            holder["scope"] = _Drive(current, context.cancellation, lease=lease)
            return await self._write_cancellation(holder, context, reason=reason)
        finally:
            await self._release(lease)

    async def _write_cancellation(
        self, holder: dict[str, Any], context: RunContext, *, reason: str = ""
    ) -> RunOutcome:
        """Write the recorded cancellation as this run's terminal fact.

        Shared by the two entry points that can find a pending cancellation while
        holding the lease (``resume``'s pre-check and the orphan recovery's re-read), so
        there is one description of what a cancelled run looks like and no window in
        which the lease is dropped between deciding and writing.
        """
        record: RunRecord = holder["record"]
        note = dict(record.cancel_requested or {})
        reason = reason or str(note.get("reason") or "cancelled")
        detail = (
            f"the run was cancelled while it was waiting: {reason}"
            if record.status is RunStatus.SUSPENDED
            else f"the run was cancelled while it was running: {reason}"
        )
        cancelled = replace(
            record,
            status=RunStatus.CANCELLED,
            attempt_id=context.identity.attempt_id,
            stop_code=LoopStop.ABORTED.value,
            error_code="cancelled",
            detail=detail,
            updated_at=self._clock(),
        )
        holder["record"] = cancelled
        holder["scope"] = _Drive(cancelled, context.cancellation, lease=holder.get("lease"))
        await self._commit_final(
            holder,
            cancelled,
            events=(("run_cancelled", {"reason": reason}),),
        )
        return _outcome_of(cancelled)

    async def _recover_orphan(
        self, run_id: str, record: RunRecord, context: RunContext
    ) -> RunOutcome:
        """Recover a run whose driver died, within the windows this build supports.

        A record is ``running`` with no live driver because a process died between two
        commits. What the *committed snapshot* says decides what is honest here:

        * ``finished`` — the reducer had already reached its verdict and the process died
          before the transaction that records it. The snapshot is the loop's own answer,
          so the verdict is committed as the terminal fact. Nothing is dispatched.
        * anything else — nothing can be replayed from it. The batch was never committed,
          so there is no way to tell which calls were dispatched, and marking the run
          finished would claim an outcome nobody observed. The run is moved to
          ``suspended`` with a reconciliation interaction naming what the snapshot proves
          and what the ledger should be checked for. An operator then decides, with
          evidence, rather than a resume guessing — and a resume of that suspension is
          refused, because there is no recorded batch to resume from. That limit is
          deliberate and recorded rather than papered over.
        """
        lease = await self._claim(run_id, what="recover")
        try:
            # Everything below is decided from the record read *after* the lease was
            # taken. A snapshot read before it could describe a state another worker has
            # already replaced, and acting on it is how a stale driver dispatches.
            current, version = await self._load(run_id)
            if current is None:
                raise RunNotFound(run_id)
            if current.terminal:
                # Someone committed the verdict while this worker was taking the lease: a
                # terminal fact is never overwritten by a recovery question.
                return _outcome_of(current)
            if current.cancel_requested is not None:
                # A cancellation that arrived while no local driver could act on it. It is
                # a durable fact about this run: recovering would dispatch the very calls
                # the host asked to stop, so the request becomes the cancellation it is.
                holder: dict[str, Any] = {
                    "record": current,
                    "version": version,
                    "lease": lease,
                }
                holder["scope"] = _Drive(current, context.cancellation, lease=lease)
                return await self._write_cancellation(holder, context)
            verdict = _verdict_of(current, self._clock())
            if verdict is not None:
                committed = replace(verdict, attempt_id=context.identity.attempt_id)
                holder: dict[str, Any] = {"record": current, "version": version, "lease": lease}
                holder["scope"] = _Drive(committed, context.cancellation, lease=lease)
                await self._commit_final(holder, committed)
                return _outcome_of(committed)

            snapshot = state_from_record(current.execution) if current.execution else None
            if (
                snapshot is not None
                and snapshot.phase is Phase.RUNNING_BATCH
                and snapshot.batch
                and snapshot.last_assistant is not None
            ):
                # A committed declaration partition: this build can tell the calls apart
                # and ask the ledger about each one, so the batch is recovered instead of
                # being handed to an operator as an unanswerable question. The lease is
                # handed over rather than released and re-taken: a gap there would be a
                # window in which another worker could take the run.
                return await self._recover_partial_batch(
                    run_id, current, version, lease, context, snapshot
                )

            detail = (
                "the process driving this run stopped before it recorded where it was. "
                "Nothing is retried automatically."
            )
            detail += _snapshot_evidence(current)
            interaction = {
                "interaction_id": f"{run_id}:orphaned-drive",
                "kind": "reconciliation",
                "call_id": "",
                "tool_name": "",
                "operation_id": "",
                "detail": detail,
            }
            orphaned = replace(
                current,
                status=RunStatus.SUSPENDED,
                attempt_id=context.identity.attempt_id,
                pending_interaction=interaction,
                detail=interaction["detail"],
                updated_at=self._clock(),
            )
            holder = {"record": orphaned, "version": version, "lease": lease}
            holder["scope"] = _Drive(orphaned, context.cancellation, lease=lease)
            await self._commit_final(
                holder,
                orphaned,
                events=(
                    ("approval_requested", _interaction_payload(interaction)),
                    ("run_suspended", _interaction_payload(interaction)),
                ),
            )
            return _outcome_of(orphaned)
        finally:
            await self._release(lease)

    async def cancel(self, run_id: str, reason: str = "") -> None:
        """Ask the run to stop. Idempotent; a finished run is unaffected.

        If this process drives the run, the token the loop is watching is cancelled
        and the run's own outcome records ``cancelled``. Otherwise the request is
        *recorded* on the run — a run this process cannot reach must not be marked
        cancelled, because it may still be running somewhere else.
        """
        scope = self._active.get(run_id)
        if scope is not None:
            scope.cancel(reason or "cancelled")
            return
        record, version = await self._load(run_id)
        if record is None or record.terminal:
            return
        await self._record_cancel_request(run_id, record, version, reason)

    async def state(self, run_id: str, *, context: RunContext | None = None) -> RunState:
        """Read a run's durable state.

        ``context`` is optional because the port's signature carries no identity: a
        host that serves several tenants should pass it, and then the namespace and
        session are checked before anything is returned.
        """
        record, _ = await self._load(run_id)
        if record is None:
            raise RunNotFound(run_id)
        if context is not None:
            self._assert_same_identity(record, context)
        return record.to_state()

    async def replay_run_events(
        self, run_id: str, *, after: int = -1, limit: int = 200
    ) -> tuple[EventEnvelope, ...]:
        """Re-publish stored events from a cursor, for a consumer that missed them.

        This is the repair path the contract names: the business fact is already
        committed, so a failed publication is fixed by reading the events back out
        of the store — never by rolling anything back or re-running a tool.
        """
        stored = await self._persistence.read_events(after=after, limit=limit)
        record, _ = await self._load(run_id)
        if record is None:
            raise RunNotFound(run_id)
        published: list[EventEnvelope] = []
        for item in stored:
            if item.run_id not in (None, run_id):
                continue
            envelope = EventEnvelope(
                namespace=record.namespace,
                run_id=run_id,
                attempt_id=record.attempt_id,
                kind=item.kind,
                payload=dict(item.payload or {}),
                sequence=item.sequence,
                durable=True,
            )
            try:
                await self._notify_run_listener(envelope)
            except Exception:  # noqa: BLE001 - a consumer's failure is not the store's
                self._fanout_failures.append(item.kind)
                continue
            published.append(envelope)
        return tuple(published)

    # -- internals ------------------------------------------------------

    async def _load(self, run_id: str) -> tuple[RunRecord | None, int | None]:
        stored = await self._persistence.load_run(run_id)
        if stored is None:
            return None, None
        return RunRecord.from_record(stored.state), stored.version

    def _assert_same_identity(self, record: RunRecord, context: RunContext) -> None:
        identity = context.identity
        if record.namespace != identity.namespace or record.session_id != identity.session_id:
            # The run id is globally unique by contract; a different namespace
            # reaching the same id is a collision, not a resume.
            raise ContractError(
                f"run {identity.run_id!r} belongs to namespace {record.namespace!r}/"
                f"session {record.session_id!r}, not to {identity.namespace!r}/"
                f"{identity.session_id!r}"
            )

    def _seed_messages(self, request: RunRequest) -> list[Any]:
        initial = create_initial_system_message(self._system_prompt, list(self._tools))
        seed: list[Any] = [initial] if initial is not None else []
        seed.extend(request.messages)
        return seed

    def _transcript(self, record: RunRecord) -> list[Any]:
        return [message_from_json(dict(item)) for item in record.messages]

    def _context_preparation(self, identity: Any, holder: dict[str, Any]) -> Any:
        """The loop's preparation port, bound to this run's scope (plan section 4.2).

        The controller is the host's object; the scope it must isolate projections and
        measurements by is the harness's to supply, and it is derived from the run identity
        rather than invented. ``None`` when no controller is configured, which keeps the
        transcript as the request view.

        The committed projection facts travel back in as ``prior``, which is what stops a
        resumed attempt from getting a fresh summary allowance: the auxiliary calls already
        recorded in the run record are the ones that count.
        """
        if self._context_controller is None:
            return None
        scope = f"{identity.namespace}/{identity.session_id}/{identity.run_id}"

        async def prepare(payload: Any) -> Any:
            record: RunRecord = holder["record"]
            prior = record.context_state
            try:
                if hasattr(self._context_controller, "prepare_async"):
                    return await self._context_controller.prepare_async(
                        payload, scope=scope, prior=prior
                    )
                return self._context_controller.prepare(payload, scope=scope)
            except asyncio.CancelledError:
                # A cancelled preparation may still have spent an auxiliary call. The harness
                # asks the controller what this run has spent and carries it into the terminal
                # record; the cancellation itself keeps propagating, unswallowed.
                self._record_interrupted_summary(holder, scope)
                raise

        return prepare

    def _record_interrupted_summary(self, holder: dict[str, Any], scope: str) -> None:
        """Carry a cancelled preparation's auxiliary spend into the run's terminal record.

        The controller is asked only if it offers the accessor: a host's own controller may
        have no summary path at all, and the harness does not invent facts for one. The facts
        are merged with the last committed projection (when there is one) so the record keeps
        describing the last known view while its summary accounting catches up.
        """
        account: Any = getattr(self._context_controller, "summary_facts", None)
        if not callable(account):
            return
        try:
            summary = account(scope)
        except Exception:  # noqa: BLE001 - accounting must not replace the cancellation
            return
        if not summary:
            return
        record: RunRecord = holder["record"]
        base = record.context_state
        merged = (
            dict(base)
            if base
            else {
                "context_schema_version": CONTEXT_STATE_SCHEMA_VERSION,
                "scope": scope,
                "refused": None,
            }
        )
        merged["summary"] = dict(summary)
        holder["interrupted_context_state"] = merged

    def _make_loop_config(
        self,
        holder: dict[str, Any],
        identity: RunIdentity,
        *,
        max_turns: int | None,
        initial_turns_used: int = 0,
    ) -> AgentLoopConfig:
        """One loop configuration for this invocation, wherever it enters.

        Built in one place so a recovered batch is driven by exactly the same ports as a
        fresh run: the same commit boundary, the same suspend hook, the same preparation.
        Only the attempt and the logical turn budget differ, and both are facts about
        *this* invocation rather than about the loop's behaviour.
        """
        return AgentLoopConfig(
            stream_fn=self._stream_fn,
            model=self._model,
            tools=list(self._tools),
            max_turns=max_turns,
            max_concurrent_tools=self._max_concurrent_tools,
            should_stop_after_turn=self._stop_after_turn,
            should_suspend_after_batch=self._make_suspend_hook(holder),
            state_sink=self._state_sink(holder),
            prepare_context=self._context_preparation(identity, holder),
            # The execution right this invocation runs under. Every tool-call
            # observation carries it, so a callback from an earlier attempt is refused
            # rather than allowed to update this run.
            attempt_id=identity.attempt_id,
            # Recorded in the execution snapshot before the first dispatch: the batch a
            # crash leaves behind can then be compared against the deployment it was
            # declared under.
            arrangement=self.arrangement(),
            initial_turns_used=initial_turns_used,
        )

    async def _drive(
        self, request: RunRequest, context: RunContext, seed: Sequence[Any], holder: dict[str, Any]
    ) -> LoopResult:
        prompts = [UserMessage(content=request.prompt)] if request.prompt else []
        agent_context = AgentContext(messages=list(seed), tools=list(self._tools))
        config = self._make_loop_config(
            holder, context.identity, max_turns=request.max_turns
        )
        scope: _Drive = holder["scope"]
        return await self._run_task(
            run_agent_loop_result(
                prompts, agent_context, config, context.cancellation, self._emit_core_event
            ),
            scope,
        )

    # -- leases ---------------------------------------------------------

    async def _claim(self, run_id: str, *, what: str = "start") -> "Lease | None":
        """Take the run's lease, or refuse because another worker holds it.

        The lease is what stops two workers from driving the same run: a second
        claim by a different holder returns ``None`` while the first is valid. The
        store's fencing token is checked again inside every commit, so a worker that
        loses its lease mid-run cannot land a stale write even though it still
        believes it holds it.
        """
        if not hasattr(self._persistence, "claim_lease"):
            return None
        lease = await self._persistence.claim_lease(
            f"run:{run_id}", self._holder, ttl_seconds=self._lease_ttl, now=self._clock()
        )
        if lease is None:
            raise RunBusy(
                run_id,
                f"another worker holds the lease for this run (cannot {what} it here)",
            )
        return lease

    async def _release(self, lease: "Lease | None") -> None:
        if lease is None:
            return
        with contextlib.suppress(Exception):
            await self._persistence.release_lease(lease)

    async def _emit_core_event(self, event: Any) -> None:
        """Forward one core progress frame, fenced and after validation.

        The fence runs **before** anything else can happen: a frame from another
        attempt (or another namespace) is refused and recorded, and no listener,
        message buffer or state change is touched. The buffer itself only grows for
        accepted frames, so an interrupted run's transcript cannot contain a stale
        attempt's message.
        """
        scope: _Drive | None = self._current_scope
        if scope is None:
            return
        kind = getattr(event, "type", "")
        envelope = scope.envelope(kind=str(kind), payload=_public_frame(event))
        decision = scope.core_fence.accept(envelope)
        if not decision.accepted:
            self._note_refusal(decision.reason)
            return
        if kind == "message_start":
            message = getattr(event, "message", None)
            if message is not None:
                scope.messages.append(message)
                # Announced but not yet inside a committed record: the final record adds
                # exactly these when the invocation is interrupted instead of completing.
                scope.uncommitted.append(message)
        if self._listener is None:
            return
        outcome = self._listener(event)
        if inspect.isawaitable(outcome):
            await outcome

    @property
    def _current_scope(self) -> "_Drive | None":
        """The scope of the invocation this task is running, if any."""
        task = asyncio.current_task()
        for scope in self._active.values():
            if scope.task is task:
                return scope
        return None

    def _note_refusal(self, reason: str) -> None:
        self._fence_refusals.append(reason)

    async def _run_task(self, coro: Any, scope: _Drive) -> Any:
        """Run one loop invocation as a task, with the scope registered.

        A task (rather than a bare await) is what makes cancellation able to
        interrupt a blocked await; the scope keeps the handle so ``cancel()`` can
        reach it from another thread. When the *host* asked for the stop, the
        cancellation becomes a uniform ``CANCELLED`` outcome carrying the messages
        the loop already announced — a cancelled run is not amnesia.
        """
        scope.loop = asyncio.get_running_loop()
        limit = self._max_runtime
        task = asyncio.ensure_future(coro)
        scope.task = task
        try:
            if limit is None:
                return await task
            return await asyncio.wait_for(task, timeout=limit)
        except asyncio.TimeoutError:
            scope.token.cancel("max runtime exceeded")
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
            return _interrupted_result(
                scope,
                RunStatus.USAGE_LIMITED,
                "max_runtime_exceeded",
                f"the run exceeded its {limit:g}s runtime budget",
            )
        except asyncio.CancelledError:
            if not scope.token.cancelled:
                raise
            return _interrupted_result(
                scope,
                RunStatus.CANCELLED,
                "cancelled",
                scope.token.reason or "the run was cancelled",
            )
        finally:
            scope.task = None

    def _state_sink(self, holder: dict[str, Any]):
        """The commit port the loop calls before performing each action.

        This callback is the *wired* commit port: the loop awaits it, and what it awaits
        is the store's transaction. "The runner accepted the transition" and "the store
        has it" are therefore the same fact, and a commit that fails raises here — before
        the action it was about to perform, never after it.

        Only the phases where a crash would leave a question worth answering are written:
        a model request or a tool batch is about to be dispatched, a host hook is about to
        run, or the run has just paused. A queue read (``COLLECTING``) has no external
        effect, and the terminal verdict belongs to the one owner of the final record
        (``_commit_final``), so neither is written here. The state is remembered in memory
        for both of those, because the final record is built from it.
        """
        worth_capturing = {
            Phase.REQUESTING_MODEL,
            Phase.RUNNING_BATCH,
            Phase.AWAITING_PREPARE,
            Phase.AWAITING_STOP_HOOK,
            Phase.SUSPENDED,
        }

        async def sink(state: ExecutionState) -> None:
            holder["last_state"] = state
            if state.phase in worth_capturing:
                await self._commit_execution_progress(holder, state)

        return sink

    async def _commit_execution_progress(
        self, holder: dict[str, Any], state: ExecutionState
    ) -> None:
        """Commit where the execution is, before the transition's action runs.

        The candidate record is handed to the store as a *candidate*: it becomes the
        driver's authoritative view only once the transaction committed, so a failed
        write cannot leave this process reasoning from state the store rejected. The
        expected version and the lease travel with the write, which is what stops a
        worker that lost its lease — or one racing a host's cancellation — from writing
        over a newer fact. A conflict is raised, never retried away: the run stops
        dispatching instead of guessing which version is real.
        """
        scope: "_Drive | None" = holder.get("scope")
        record: RunRecord = holder["record"]
        if record.terminal:
            raise ContractError(
                f"run {record.run_id!r} is already {record.status.value}; refusing to write "
                "execution progress over a finished run"
            )
        if state.phase is Phase.SUSPENDED:
            # A pause is one transaction: the run status, the batch partition, the
            # interaction, the transcript and the pause events are written together by the
            # side that holds the complete paused state. A reader can therefore never see
            # "suspended" carrying the previous dispatch window's snapshot.
            pending = holder.get("pending_suspend") or {}
            candidate = _record_from_state(record, state, self._clock())
            if pending.get("batch") is not None:
                candidate = replace(candidate, batch=pending["batch"])
            events: tuple[tuple[str, Mapping[str, Any]], ...] = self._measurement_events(
                state, scope
            )
            if pending.get("emit_events"):
                payload = _interaction_payload(candidate.pending_interaction or {})
                events = (*events, ("approval_requested", payload), ("run_suspended", payload))
            _version, written = await self._commit(
                holder,
                expected_version=holder.get("version"),
                events=events,
                proposed=candidate,
            )
            holder.pop("pending_suspend", None)
            _clear_uncommitted(scope)
            # The fact is committed first; a consumer's failure is repaired by replay.
            await self._publish(written, scope)
            return

        # An in-flight phase: the request or the batch is about to be dispatched, so the
        # response and its declarations (or the settled results) must be readable first.
        candidate = replace(
            record,
            status=RunStatus.RUNNING,
            turns_used=state.turns_used,
            messages=tuple(_as_records(state.messages)),
            # A resumed attempt is running again: the interaction it answered is gone.
            pending_interaction=None,
            execution=dict(state_to_record(state)),
            # The projection facts travel with the state they describe, in the same
            # transaction: the request about to be sent and the measurement of it are one
            # fact, so a reader can never find one without the other.
            context_state=_context_state_of(record, state),
            updated_at=self._clock(),
        )
        _version, _written = await self._commit(
            holder,
            expected_version=holder.get("version"),
            events=self._measurement_events(state, scope),
            proposed=candidate,
        )
        _clear_uncommitted(scope)

    def _measurement_events(
        self, state: ExecutionState, scope: "_Drive | None"
    ) -> tuple[tuple[str, Mapping[str, Any]], ...]:
        """Durable events for the measurements this state carries, written exactly once.

        A measurement is a fact about *when* something happened, and it is committed in the
        same transaction as the state it describes — so a reader can never find the second
        without the first. The identities are the request instance and the completion
        reference, which is what makes a re-entry (a recovery that commits the same state
        again) idempotent instead of duplicating the record.
        """
        if scope is None:
            return ()
        events: list[tuple[str, Mapping[str, Any]]] = []
        instance = state.request_instance
        if instance and f"dispatched:{instance}" not in scope.measurements_emitted:
            scope.measurements_emitted.add(f"dispatched:{instance}")
            events.append(
                (
                    "model_request_dispatched",
                    {
                        "request_instance": instance,
                        "model_action_id": state.model_action_id,
                        "attempt": state.attempt_id,
                        "started_at": state.request_started_at,
                    },
                )
            )
        measurement = state.last_request_measurement
        if measurement is not None and measurement.reference not in scope.measurements_emitted:
            scope.measurements_emitted.add(measurement.reference)
            events.append(("model_request_completed", self._measurement_payload(measurement)))
        # A call's span is committed with the observation that produced it, for the same
        # reason the request's is: the partition is *current state* (it is cleared when the
        # turn ends), while the timing is history that a report still has to be able to show
        # tomorrow. Public fields only — ids, numbers, statuses; never arguments or content.
        for call in state.batch:
            if not call.observed:
                continue
            if call.observation_id.endswith(":recovered"):
                # An observation a recovery read back from the ledger: the attempt that
                # actually ran the call died without committing a measurement, so there is
                # no span to report and inventing one ("observed, no duration") would be a
                # fact nobody observed. Its absence is the honest record.
                continue
            key = f"tool:{call.model_action_id or state.model_action_id}:{call.call_id}"
            if key in scope.measurements_emitted:
                continue
            scope.measurements_emitted.add(key)
            events.append(
                (
                    "tool_call_observed",
                    {
                        "call_id": call.call_id,
                        "tool_name": call.tool_name,
                        "attempt": state.attempt_id,
                        "model_action_id": call.model_action_id or state.model_action_id,
                        "operation_id": call.operation_id,
                        "outcome": call.outcome.value,
                        "status": call.status,
                        "started_at": call.started_at,
                        "duration_ms": call.duration_ms,
                        "clock": "monotonic" if call.duration_ms is not None else "",
                        "terminate": call.terminate,
                        "interrupted": call.started_at is not None and call.duration_ms is None,
                    },
                )
            )
        return tuple(events)

    @staticmethod
    def _measurement_payload(measurement: Measurement) -> Mapping[str, Any]:
        """The public half of one measurement. Ids, numbers and units — never content."""
        return {
            "request_instance": measurement.reference,
            "attempt": measurement.attempt,
            "started_at": measurement.started_at,
            "duration_ms": measurement.duration_ms,
            "clock": measurement.clock,
            "interrupted": measurement.interrupted,
            "payload_bytes": measurement.payload_bytes,
            "payload_bytes_estimator": measurement.payload_bytes_estimator,
            "usage": None if measurement.usage is None else dict(measurement.usage),
        }

    def _make_suspend_hook(self, holder: dict[str, Any]):
        """Build the loop hook that records a batch and stops at the pause boundary.

        The hook runs *inside* the loop, after a batch settled and before the reducer
        accepts the settlement. It therefore *stages* the pause — the batch facts and the
        host's answer — instead of committing it: committing here would write
        ``suspended`` together with the execution snapshot of the dispatch window, a state
        a reader could see but no resume could act on. The state sink commits the staged
        pause once the reducer's own suspended state is the thing being written.
        """

        async def hook(
            assistant_message: Any, tool_results: Sequence[Any]
        ) -> Mapping[str, Any] | None:
            host_decision = None
            if self._host_suspend_hook is not None:
                host_decision = self._host_suspend_hook(assistant_message, tool_results)
                if inspect.isawaitable(host_decision):
                    host_decision = await host_decision
            batch = self._record_batch(assistant_message, tool_results, holder)
            if batch is None:
                return host_decision or None
            # A blocking call wins over a host answer: the operator has to decide about it
            # before the run can do anything else.
            primary = primary_interaction(batch) if batch.blocking() else None
            interaction = primary or host_decision
            if interaction is None:
                return None
            holder["pending_suspend"] = {
                "batch": batch.to_record(),
                "interaction": interaction,
                # A pause caused by a waiting call is announced; a host's own suspend
                # decision keeps its previous shape (state only, no durable event).
                "emit_events": bool(primary),
            }
            return interaction

        return hook

    def _record_batch(
        self, assistant_message: Any, tool_results: Sequence[Any], holder: dict[str, Any]
    ) -> RunBatch | None:
        """Turn one settled batch into the record a resume needs."""
        declared = getattr(assistant_message, "content", None) or ()
        if not any(getattr(block, "type", None) == "toolCall" for block in declared):
            return None
        results = [item for item in tool_results if isinstance(item, ToolResultMessage)]
        record: RunRecord = holder["record"]
        return record_batch(
            assistant_message,
            results,
            run_id=record.run_id,
            attempt_id=record.attempt_id,
            fingerprints=self.fingerprints(),
            exposure_manifest=self.exposure_manifest(),
        )

    # -- resume helpers -------------------------------------------------

    def _assert_execution_snapshot_agrees(self, record: RunRecord, batch: RunBatch) -> None:
        """Refuse a resume when two committed facts disagree about the same batch.

        The execution snapshot and the recorded batch are written from different places
        (the loop's commit port and the pause hook). They must name the same blocking
        calls. A disagreement means one of them is stale, and choosing either would be a
        guess about which calls ran — so the resume stops and says so.

        A missing snapshot is not a disagreement: records written before the snapshot
        existed are read on their ``batch`` alone, which is the previous behaviour.
        """
        if not record.execution:
            return
        snapshot = state_from_record(record.execution)
        recorded = {
            call.call_id
            for call in batch.calls
            if call.outcome in {CallOutcome.AWAITING, CallOutcome.UNCERTAIN}
        }
        committed = {
            str(item.get("call_id") or "")
            for item in (snapshot and pending_work_of(snapshot)).get("blocking") or ()
        }
        if recorded != committed:
            raise ContractError(
                f"run {record.run_id!r} carries an execution snapshot and a recorded batch that "
                f"disagree about which calls are waiting (batch={sorted(recorded)}, "
                f"snapshot={sorted(committed)}); refusing to guess which of them ran"
            )

    # -- recovering a partially settled batch --------------------------

    def _tool_recovery(self, tool_name: str) -> Any:
        tool = next((item for item in self._tools if item.name == tool_name), None)
        return None if tool is None else getattr(tool, "recovery", None)

    async def _classify_recovered_batch(
        self, batch: RunBatch, context: RunContext
    ) -> tuple[RunBatch, set[str], list[str]]:
        """Read every unresolved call from the ledger *before* anything is dispatched.

        Two things happen here, and the order is the point:

        * a call whose outcome the ledger can already prove is settled from that proof,
          and its handler is **not** called;
        * every other call is only *classified*. The returned candidate set names the
          calls the ledger proved absent, which are the only ones the execute phase may
          dispatch.

        When anything is left uncertain the caller must not dispatch at all — the
        conservative barrier. A batch that runs call C while call B's effect is still
        unproven has decided, silently, that B does not matter.
        """
        # Imported here rather than at module scope: the tool bridge imports the runtime
        # context, so a module-level import would make ``runtime`` and ``tools`` circular
        # depending on which one a caller imported first.
        from ..tools.core_bridge import tool_result_from_invocation

        candidates: set[str] = set()
        blockers: list[str] = []
        for call in batch.calls:
            if call.outcome is CallOutcome.SETTLED and call.result is not None:
                continue
            if call.outcome is CallOutcome.UNCERTAIN:
                # First ask the ledger: a convergence that was recorded durably before the
                # Run commit (a crash between the two) is already a proven outcome, and
                # asking the host reconciler again would be asking a question that has an
                # answer.
                converged = await self._converged_in_ledger(call)
                if converged is not None:
                    updated, interaction = _batch_call_from_result(
                        call, tool_result_from_invocation(converged)
                    )
                    batch = _with_interaction(batch, interaction)
                    batch = batch.replace_call(updated)
                    continue
                resolved = await self._reconcile_call(call)
                if resolved is None:
                    blockers.append(
                        f"{call.call_id}: {call.reason or 'the outcome could not be proven'}"
                    )
                    continue
                batch = batch.replace_call(resolved)
                continue
            if call.outcome is CallOutcome.AWAITING:
                # A waiting call is re-dispatched through the same gateway, which re-reads
                # the trusted approval store and records the intent only when a valid
                # decision exists. That is the tested approval path; it needs no ledger
                # *read* capability, only the ledger's ordinary write path.
                candidates.add(call.call_id)
                continue
            recovery = self._tool_recovery(call.tool_name)
            if recovery is None or not getattr(recovery, "capable", False):
                blockers.append(
                    f"{call.call_id}: tool {call.tool_name!r} cannot be asked about its "
                    "outcome, so a partially settled batch cannot be recovered automatically"
                )
                continue
            reviewed = await recovery.review(
                call.call_id,
                dict(call.arguments),
                model_action_id=call.model_action_id,
                operation_id=call.operation_id or None,
            )
            if reviewed is None:
                candidates.add(call.call_id)
                continue
            updated, interaction = _batch_call_from_result(
                call, tool_result_from_invocation(reviewed)
            )
            batch = _with_interaction(batch, interaction)
            batch = batch.replace_call(updated)
            if updated.outcome is CallOutcome.UNCERTAIN:
                blockers.append(
                    f"{updated.call_id}: "
                    f"{updated.reason or 'the ledger cannot prove the outcome'}"
                )
        return batch, candidates, blockers

    async def _converged_in_ledger(self, call: BatchCall) -> Any:
        """The ledger's confirmed view of an unproven call, when it now has one.

        "Confirmed" is the *business* classification of the recorded status, not "the
        handler succeeded": a receipt the ledger holds as ``failed``, ``denied``,
        ``invalid_args`` or ``cancelled`` answers the question just as finally as
        ``executed`` does, and asking the host for evidence about a question that already
        has an answer is how a run stays stuck forever (PTR-R3).

        Two things are deliberately *not* accepted here, and the difference is the whole
        point of not reading ``receipt.settled``:

        * ``unknown`` — the ledger may well hold a receipt (an open intent, or a receipt
          that says the effect could not be proven) and still know nothing about the
          effect. It stays uncertain, so the run asks the host;
        * ``deferred``/``awaiting_approval`` — a human still has to decide, and a receipt
          is not a decision.
        """
        recovery = self._tool_recovery(call.tool_name)
        if recovery is None or not getattr(recovery, "capable", False):
            return None
        reviewed = await recovery.review(
            call.call_id,
            dict(call.arguments),
            model_action_id=call.model_action_id,
            operation_id=call.operation_id or None,
        )
        if reviewed is None:
            return None
        if classify_status(reviewed.status.value) is CallOutcome.SETTLED:
            return reviewed
        return None

    async def _recover_partial_batch(
        self,
        run_id: str,
        record: RunRecord,
        version: int | None,
        lease: "Lease | None",
        context: RunContext,
        snapshot: Any,
    ) -> RunOutcome:
        """Recover a run that died mid-batch, using the committed declaration.

        The claim this replaces is ``_recover_orphan``'s: "nothing can be replayed from
        it". For a *new-format* snapshot the committed state names every call and reports
        the observations that were accepted before the crash, so the batch can be
        classified against the authoritative ledger and continued by the same Runner —
        without re-asking the model and without guessing what ran.

        The caller has already taken the lease and re-read the record under it: ``record``
        and ``version`` are that fresh read, so nothing here acts on a snapshot that was
        read before the fence was held.
        """
        attempt = context.identity.attempt_id
        identity = RunIdentity(
            namespace=record.namespace,
            session_id=record.session_id,
            run_id=record.run_id,
            attempt_id=attempt,
        )
        # The arrangement comes from the *committed* state, so the check below compares
        # the deployment the batch was declared under with the one running it. Rebuilding
        # it from ``self`` would compare this deployment with itself and let a drifted
        # deployment dispatch the remaining calls.
        batch = batch_from_snapshot(
            snapshot,
            run_id=record.run_id,
            attempt_id=attempt,
            arrangement=snapshot.arrangement,
        )
        _assert_arrangement(
            batch,
            identity,
            _recorded_arrangement(record, batch),
            self.fingerprints(),
            self.exposure_manifest(),
            self._clock(),
        )
        holder: dict[str, Any] = {"record": record, "version": version, "lease": lease}
        scope = _Drive(replace(record, attempt_id=attempt), context.cancellation, lease=lease)
        holder["scope"] = scope
        self._active[run_id] = scope
        try:
            return await self._run_recovered_batch(
                holder, record, snapshot, batch, context, identity
            )
        finally:
            self._active.pop(run_id, None)

    async def _run_recovered_batch(
        self,
        holder: dict[str, Any],
        record: RunRecord,
        snapshot: Any,
        batch: RunBatch,
        context: RunContext,
        identity: RunIdentity,
    ) -> RunOutcome:
        outcome = await self._run_task(
            self._recover_then_continue(holder, record, snapshot, batch, context, identity),
            holder["scope"],
        )
        return await self._finish_recovered(holder, record, identity, outcome)

    async def _recover_then_continue(
        self,
        holder: dict[str, Any],
        record: RunRecord,
        snapshot: Any,
        batch: RunBatch,
        context: RunContext,
        identity: RunIdentity,
    ) -> Any:
        """One recovery invocation: read the ledger, then continue the same batch.

        Everything this coroutine awaits happens inside the run's single bounded drive
        task, so it shares one absolute deadline and one cancellable token with the model
        turn that may follow. That is the point: the ledger reads, the host reconciler,
        the calls still owed a dispatch and the next model request are one invocation, and
        a budget that only started once the tools had finished was never a budget.

        The remaining calls are *not* executed here. They are handed to the same
        :class:`~yanxu_harness.core.runner.Runner` batch session that a fresh batch uses —
        through the committed partition, which is what makes per-call commit, the
        concurrency limit, the sequential barrier, the hooks and cancellation behave
        identically on both paths.

        Returns a :class:`LoopResult` when the loop ran, or :class:`_Recovered` when the
        batch still needs an answer (the conservative barrier).
        """
        batch, candidates, blockers = await self._classify_recovered_batch(batch, context)
        if blockers:
            # The conservative barrier: every confirmed fact is kept, and nothing new is
            # dispatched while a question about this batch is still open.
            return _Recovered(batch=batch, blockers=tuple(blockers))
        dispatchable = {call.call_id for call in batch.calls if _dispatchable(call)}
        if candidates != dispatchable:
            raise ContractError(
                "the ledger classification and the recovered partition disagree about which "
                f"calls may be dispatched (ledger={sorted(candidates)}, "
                f"partition={sorted(dispatchable)}); refusing to dispatch a guess"
            )
        state = _state_from_batch(snapshot, batch, identity.attempt_id)
        holder["record"] = replace(
            record,
            attempt_id=identity.attempt_id,
            # The batch is being executed again, so the record no longer *pauses* on it;
            # the pause's question is kept until the driver's own commit says the run is
            # executing (a suspended record must carry an interaction, and the in-flight
            # commit is what clears it).
            batch=None,
        )
        return await Runner(
            self._make_loop_config(
                holder,
                identity,
                max_turns=state.max_turns,
                initial_turns_used=max(state.turns_used, record.turns_used),
            ),
            self._emit_core_event,
        ).resume_batch(
            state,
            AgentContext(messages=list(state.messages), tools=list(self._tools)),
            context.cancellation,
        )

    async def _finish_recovered(
        self,
        holder: dict[str, Any],
        record: RunRecord,
        identity: RunIdentity,
        outcome: Any,
    ) -> RunOutcome:
        """Turn a finished recovery invocation into the run's next durable state.

        A pause is committed here rather than inside the bounded drive: the deadline is
        allowed to stop dispatching, and it is not allowed to stop the evidence of what
        was already proved from being written.
        """
        if isinstance(outcome, _Recovered):
            return await self._pause_recovered_batch(
                holder, record, outcome.batch, identity, outcome.blockers
            )
        finished = _apply_result(
            holder["record"],
            outcome,
            (),
            self._clock(),
            final_state=holder.get("last_state"),
            trailing=holder["scope"].uncommitted,
            context_state=holder.get("interrupted_context_state"),
        )
        assert_transition(holder["record"].status, finished.status)
        await self._commit_final(holder, finished)
        return _outcome_of(finished)

    async def _pause_recovered_batch(
        self,
        holder: dict[str, Any],
        record: RunRecord,
        batch: RunBatch,
        identity: RunIdentity,
        blockers: Sequence[str],
    ) -> RunOutcome:
        """Commit the recovered facts and stop, asking for evidence rather than retrying."""
        primary = primary_interaction(batch)
        if primary is None and blockers:
            primary = {
                "interaction_id": f"{record.run_id}:recovery-barrier",
                "kind": "reconciliation",
                "call_id": "",
                "tool_name": "",
                "operation_id": "",
                "detail": (
                    "the batch could not be recovered automatically: " + "; ".join(blockers)
                ),
            }
        if primary is None:
            raise ContractError(
                f"run {record.run_id!r}: the recovered batch settled but nothing is waiting, "
                "which cannot be true for a paused recovery"
            )
        transcript = self._transcript(record)
        placeholder = [
            message_from_json(dict(call.result))
            if call.result is not None
            else ToolResultMessage(
                tool_call_id=call.call_id,
                tool_name=call.tool_name,
                content=[
                    TextContent(
                        text=(
                            f"{call.tool_name}: this call was not executed. The run is "
                            "waiting for a decision about another call of the same batch, "
                            "so it has not been dispatched yet."
                        )
                    )
                ],
                details={},
                is_error=True,
            )
            for call in batch.calls
        ]
        suspended = replace(
            record,
            status=RunStatus.SUSPENDED,
            attempt_id=identity.attempt_id,
            batch=batch.to_record(),
            pending_interaction=primary,
            messages=tuple(_as_records(_with_results(transcript, placeholder))),
            execution=_snapshot_for_batch(record.execution, batch, primary),
            updated_at=self._clock(),
        )
        holder["record"] = suspended
        await self._commit_final(
            holder,
            suspended,
            events=(
                ("approval_requested", _interaction_payload(primary)),
                ("run_suspended", _interaction_payload(primary)),
            ),
        )
        return _outcome_of(suspended)

    async def _resolve_blocking_calls(
        self,
        batch: RunBatch,
        context: RunContext,
        identity: RunIdentity,
        *,
        candidate_ids: set[str] | None = None,
    ) -> tuple[RunBatch, list[Any]]:
        """Settle every blocking call, or leave it blocking. Never guess.

        ``candidate_ids`` narrows the calls this may dispatch. It exists for the recovery
        barrier: when one call of the batch is uncertain, the calls the ledger *did* prove
        absent must wait too, so a partially verified batch cannot run ahead of the
        question that has to be answered first. ``None`` means "every unstarted call".
        """
        results: list[Any] = []
        tools_by_name = {tool.name: tool for tool in self._tools}
        for call in batch.calls:
            if call.outcome is CallOutcome.SETTLED:
                if call.result is not None:
                    results.append(message_from_json(dict(call.result)))
                continue
            if call.outcome is CallOutcome.UNCERTAIN:
                resolved = await self._reconcile_call(call)
                if resolved is None:
                    continue
                batch = batch.replace_call(resolved)
                if resolved.result is not None:
                    results.append(message_from_json(dict(resolved.result)))
                continue
            if candidate_ids is not None and call.call_id not in candidate_ids:
                continue
            tool = tools_by_name.get(call.tool_name)
            if tool is None:
                raise ContractError(
                    f"{call.call_id}: tool {call.tool_name!r} is not part of this deployment, so "
                    "the call it waits on can no longer be run"
                )
            outcome = await _execute_tool(
                tool,
                call.call_id,
                dict(call.arguments),
                context.cancellation,
                model_action_id=call.model_action_id,
            )
            updated, interaction = _batch_call_from_result(call, outcome)
            batch = _with_interaction(batch, interaction)
            if call.operation_id and updated.operation_id and updated.operation_id != call.operation_id:
                # A different operation id means the gateway would treat this as a
                # fresh call and could execute the effect a second time.
                raise ContractError(
                    f"{call.call_id}: resuming produced operation {updated.operation_id!r}, but the "
                    f"paused call was {call.operation_id!r}; refusing to dispatch it under a new "
                    "identity, which could duplicate the effect"
                )
            batch = batch.replace_call(updated)
            if updated.outcome is CallOutcome.SETTLED and updated.result is not None:
                results.append(message_from_json(dict(updated.result)))

        # Re-fingerprint: this is the arrangement the next resume will be checked
        # against, including the new attempt.
        batch = replace(batch, attempt_id=identity.attempt_id, **_fingerprint_fields(self.fingerprints()))
        remaining = batch.blocking()
        keep = {call.call_id for call in remaining}
        batch = replace(
            batch,
            interactions=tuple(item for item in batch.interactions if item.call_id in keep),
        )
        return batch, results

    async def _reconcile_call(self, call: BatchCall) -> BatchCall | None:
        """Ask the host's business check about an unproven operation."""
        if self._reconcile is None:
            return None
        evidence = self._reconcile(
            call, {"operation_id": call.operation_id, "call_id": call.call_id}
        )
        if inspect.isawaitable(evidence):
            evidence = await evidence
        if not evidence:
            return None
        if not isinstance(evidence, Mapping) or not evidence.get("proven"):
            raise ContractError(
                "a reconciler must return a mapping with proven=True and the observed state; "
                "an unchecked claim must not settle an unproven operation"
            )
        state = str(evidence.get("state") or "")
        if state not in {"succeeded", "failed"}:
            raise ContractError(
                f"reconciliation evidence must name state='succeeded' or 'failed', got {state!r}"
            )
        detail = str(evidence.get("detail") or "")
        # The authoritative record of the convergence belongs to the ledger owner, and it
        # is written *before* the Run is updated: if the process dies in between, the next
        # recovery reads the promoted receipt instead of asking again.
        recovery = self._tool_recovery(call.tool_name)
        promote = None if recovery is None else getattr(recovery, "promote", None)
        if callable(promote):
            await promote(
                call.call_id,
                dict(call.arguments),
                {
                    "state": state,
                    "detail": detail,
                    "source": str(evidence.get("source") or "host reconciler"),
                },
                model_action_id=call.model_action_id,
                operation_id=call.operation_id or None,
            )
        # The model must be able to read the outcome either way: a failed reconciliation
        # with an empty body would tell it nothing about why the call did not succeed.
        content = [
            TextContent(
                text=detail
                or (
                    "the effect was confirmed by business evidence"
                    if state == "succeeded"
                    else "business evidence confirmed the call did not succeed"
                )
            )
        ]
        result = ToolResultMessage(
            tool_call_id=call.call_id,
            tool_name=call.tool_name,
            content=content,
            details={
                "status": "executed" if state == "succeeded" else "failed",
                "tool": call.tool_name,
                "operation_id": call.operation_id,
                "reconciled": True,
                "reason": detail,
            },
            is_error=state != "succeeded",
        )
        return replace(
            call,
            outcome=CallOutcome.SETTLED,
            effect_state=state,
            result=message_to_json(result),
            reason=detail or "settled by business evidence",
            resolution={
                "state": state,
                "detail": detail,
                "source": str(evidence.get("source") or "host reconciler"),
                "at": self._clock(),
            },
        )

    # -- commits --------------------------------------------------------

    async def _commit(
        self,
        holder: dict[str, Any],
        *,
        expected_version: int | None,
        events: Sequence[tuple[str, Mapping[str, Any]]] = (),
        proposed: RunRecord | None = None,
    ) -> tuple[int, Sequence[StoredEvent]]:
        record: RunRecord = holder["record"] if proposed is None else proposed
        stored = _stored_events(record.run_id, events)
        version, written = await self._persistence.commit_transition(
            record.run_id,
            record.to_record(),
            expected_version=expected_version,
            # The lease travels with every commit so a worker that lost it cannot
            # write. The check happens inside the same transaction as the write.
            lease=holder.get("lease"),
            events=stored,
        )
        # Only a committed candidate becomes this driver's authoritative view: a failed
        # write must not leave it reasoning from state the store rejected.
        holder["version"] = version
        holder["record"] = record
        return version, written

    async def _commit_final(
        self,
        holder: dict[str, Any],
        record: RunRecord,
        *,
        events: Sequence[tuple[str, Mapping[str, Any]]] = (),
    ) -> None:
        """Commit a terminal or suspended record, never overwriting a terminal one."""
        final_events = tuple(events)
        scope: "_Drive | None" = holder.get("scope")
        last_state = holder.get("last_state")
        if scope is not None and isinstance(last_state, ExecutionState):
            # A measurement taken by the final turn is still a fact: the run ended, the
            # request that ended it happened, and its duration and cost belong in the record.
            final_events = (*final_events, *self._measurement_events(last_state, scope))
        if not final_events and record.status is not RunStatus.SUSPENDED:
            final_events = (
                (
                    "run_finished",
                    {
                        "status": record.status.value,
                        "stop_code": record.stop_code,
                        "turns_used": record.turns_used,
                    },
                ),
            )
        for _ in range(3):
            try:
                _version, written = await self._commit(
                    holder,
                    expected_version=holder.get("version"),
                    events=final_events,
                    proposed=record,
                )
                await self._publish(written, holder.get("scope"))
                return
            except StorageConflict:
                current, version = await self._load(record.run_id)
                if current is None:
                    raise
                if current.terminal:
                    raise StorageConflict(
                        record.run_id,
                        f"the run was already finished as {current.status.value}; not overwriting it",
                    ) from None
                holder["version"] = version
                record = replace(
                    record,
                    cancel_requested=record.cancel_requested or current.cancel_requested,
                )
        raise StorageConflict(record.run_id, "could not commit the final run state after retries")

    async def _record_cancel_request(
        self, run_id: str, record: RunRecord, version: int | None, reason: str
    ) -> None:
        for _ in range(3):
            if record.cancel_requested is not None:
                return
            note = {"reason": reason or "cancelled", "at": self._clock()}
            updated = replace(record, cancel_requested=note, updated_at=self._clock())
            try:
                await self._persistence.save_run(
                    run_id, updated.to_record(), expected_version=version
                )
                return
            except StorageConflict:
                record, version = await self._load(run_id)
                if record is None or record.terminal:
                    return
        raise StorageConflict(run_id, "could not record the cancellation request after retries")

    def _envelope_for(self, stored: StoredEvent, scope: "_Drive") -> EventEnvelope:
        return EventEnvelope(
            namespace=scope.record.namespace,
            run_id=stored.run_id or scope.record.run_id,
            attempt_id=scope.record.attempt_id,
            kind=stored.kind,
            payload=dict(stored.payload or {}),
            sequence=stored.sequence,
            durable=True,
        )

    async def _publish(self, events: Sequence[StoredEvent], scope: "_Drive | None") -> None:
        """Publish committed events as envelopes. A consumer's failure rolls nothing back."""
        if scope is None:
            # Every commit path owns a drive scope; reaching here would mean an event
            # whose identity cannot be stated, which is not worth publishing.
            for stored in events:
                self._note_refusal(f"{stored.kind}: no drive scope to publish under")
            return
        for stored in events:
            envelope = self._envelope_for(stored, scope)
            # A durable event from an older attempt is not this attempt's news.
            if not scope.durable_fence.accept(envelope).accepted:
                self._note_refusal(scope.durable_fence.refusals[-1])
                continue
            try:
                await self._notify_run_listener(envelope)
            except Exception:  # noqa: BLE001 - the store already has the fact
                self._fanout_failures.append(stored.kind)

    async def _notify_run_listener(self, envelope: EventEnvelope) -> None:
        """Hand one committed event to the host as a public envelope.

        A `Event` from the store is the durable record; the envelope adds the
        namespace and attempt and the position, so a consumer never has to guess which
        run an event belongs to. ``Event`` remains the store-level type.
        """
        if self._run_listener is None:
            return
        outcome = self._run_listener(envelope)
        if inspect.isawaitable(outcome):
            await outcome


def _public_frame(event: Any) -> Mapping[str, Any]:
    """The publishable content of one core frame, for the envelope.

    ``public_event_payload`` is the core's own projection, and it structurally drops
    the raw provider frame. A non-core object (a host's own listener payload) does
    not have one, so its envelope carries only the frame's type.
    """
    from ..core.events import AGENT_EVENT_NAMES, public_event_payload

    kind = getattr(event, "type", None)
    if kind not in AGENT_EVENT_NAMES:
        return {}
    payload = public_event_payload(event)
    payload.pop("type", None)
    return payload


def _interrupted_result(scope: "_Drive", status: RunStatus, code: str, detail: str) -> LoopResult:
    """The outcome of a run that was stopped from outside.

    The messages the loop already announced are carried, because a cancelled run is
    not amnesia: whatever the model had produced is a fact. The status is explicit —
    a cancellation is ``CANCELLED``, a runtime-budget stop is ``USAGE_LIMITED`` —
    and never ``COMPLETED``.
    """
    from ..core.messages import content_text
    from ..core.types import AssistantMessage, StopReason, usage_to_json

    final = next(
        (message for message in reversed(scope.messages) if isinstance(message, AssistantMessage)),
        None,
    )
    return LoopResult(
        status=status,
        stop=LoopStop.ABORTED if status is RunStatus.CANCELLED else LoopStop.MAX_TURNS,
        messages=tuple(scope.messages),
        turns_used=1,
        stop_reason=final.stop_reason.value if final is not None else StopReason.PENDING.value,
        error_code=code,
        detail=detail,
        final_text=content_text(final.content) if final is not None else "",
        usage=(usage_to_json(final.usage) or {}) if final is not None else {},
    )


def _validate_resolution(resolution: Mapping[str, Any]) -> None:
    """Record what the caller *claimed*. A claim is never authorisation.

    The mapping is accepted for auditability (an operator note, the id of the
    interaction they believe they answered) and nothing else. In particular an
    ``approved`` flag here changes nothing: only the approval store is a source of
    that fact, and the gateway is what reads it.
    """
    if not isinstance(resolution, Mapping):
        raise ContractError("resume resolution must be a mapping")
    claimed = resolution.get("approved")
    if claimed is not None and not isinstance(claimed, bool):
        raise ContractError("resolution['approved'] must be a boolean when present")


def _recorded_arrangement(
    record: RunRecord, batch: RunBatch
) -> Mapping[str, Any] | None:
    """The deployment the batch was declared under, from **one** authority.

    The committed execution snapshot is the authority: it is written before the first
    dispatch of the batch, so it describes the arrangement the calls were bound to. The
    recorded batch's own fingerprint fields are read only as a compatibility path for
    records written before the snapshot carried a baseline — and when neither holds a
    baseline the answer is ``None``, which a caller must refuse rather than treat as
    "nothing changed".
    """
    if record.execution:
        snapshot = state_from_record(record.execution)
        if snapshot.arrangement:
            return dict(snapshot.arrangement)
    legacy: dict[str, Any] = {
        "config": batch.config_fingerprint,
        "tools": batch.tools_fingerprint,
        "instructions": batch.instructions_fingerprint,
        "policy": batch.policy_fingerprint,
    }
    if batch.exposure_manifest is not None:
        legacy["exposure_manifest"] = dict(batch.exposure_manifest)
    if any(legacy.get(name) for name in ("config", "tools", "instructions", "policy")):
        return legacy
    if legacy.get("exposure_manifest"):
        return legacy
    return None


def _missing_baseline_reason(run_id: str) -> str:
    return (
        f"run {run_id!r} carries no recorded arrangement baseline, so the deployment it "
        "was declared under cannot be established. Dispatching its remaining calls would "
        "run them under an arrangement nobody can compare with the one they were bound "
        "to, so the recovery refuses instead of assuming nothing changed."
    )


def _assert_arrangement(
    batch: RunBatch,
    identity: RunIdentity,
    recorded: Mapping[str, Any] | None,
    current_fingerprints: Mapping[str, str],
    current_exposure_manifest: Mapping[str, Any],
    now: float,
) -> None:
    """Refuse a resume whose arrangement moved under the approval.

    ``recorded`` is the arrangement the batch was declared under; ``current_*`` are the
    deployment doing the resuming. A missing baseline is its own refusal: without one
    there is nothing to compare, and "I could not check" must not become "it is fine".

    An *expired* approval is different from a drifted one: expiry means "ask again",
    which the gateway does on re-dispatch (it supersedes the binding), so the resume
    proceeds and the run suspends with a fresh interaction. Everything else is a
    hard refusal with the drift named.
    """
    if recorded is None:
        raise ContractError(_missing_baseline_reason(identity.run_id))
    checkpoint = _checkpoint_for(batch, identity, recorded)
    decision = resolve_resume(
        checkpoint,
        config_fingerprint=current_fingerprints.get("config"),
        tools_fingerprint=current_fingerprints.get("tools"),
        instructions_fingerprint=current_fingerprints.get("instructions"),
        policy_fingerprint=current_fingerprints.get("policy") or None,
        exposure_manifest=current_exposure_manifest,
        now=now,
    )
    hard = set(decision.drift) - {"expired"}
    if not decision.ok and hard:
        raise ContractError(
            f"the run cannot resume under the current arrangement: {decision.reason} "
            f"(drifted: {', '.join(sorted(hard))})"
        )


def _with_interaction(batch: RunBatch, interaction: "Interaction | None") -> RunBatch:
    """Replace (or drop) one call's interaction after a fresh dispatch.

    Re-dispatching a waiting call produces a *fresh* binding and a *fresh* preview —
    the same call may now be approved for different arguments, or the target may have
    moved. Keeping the pause's snapshot would show the operator a preview of
    something that is no longer being asked about, so the recorded interaction is
    replaced rather than left in place (and removed once the call settles).
    """
    if interaction is None:
        return batch
    remaining = tuple(item for item in batch.interactions if item.call_id != interaction.call_id)
    return replace(batch, interactions=(*remaining, interaction))


def _batch_call_from_result(
    call: BatchCall, result: Mapping[str, Any]
) -> tuple[BatchCall, "Interaction | None"]:
    """Rebuild one recorded call from a fresh gateway result."""
    details = dict(result.get("details") or {})
    status = str(details.get("status") or "")
    if not status:
        raise ContractError(
            f"{call.call_id}: the gateway returned a result with no status, so the run cannot "
            "tell whether the call ran"
        )
    outcome = classify_status(status)
    approval = details.get("approval") if isinstance(details.get("approval"), Mapping) else {}
    operation_id = str(details.get("operation_id") or call.operation_id)
    message = ToolResultMessage(
        tool_call_id=call.call_id,
        tool_name=call.tool_name,
        content=list(result.get("content") or []),
        details=details,
        is_error=bool(result.get("is_error")),
    )
    interaction: Interaction | None = None
    if outcome is CallOutcome.AWAITING:
        interaction = Interaction(
            interaction_id=f"{operation_id}:approval",
            kind=InteractionKind.APPROVAL,
            call_id=call.call_id,
            tool_name=call.tool_name,
            operation_id=operation_id,
            approval_binding_key=(approval or {}).get("binding_key"),
            args_hash=str(details.get("args_hash") or ""),
            preview=details.get("preview") if isinstance(details.get("preview"), Mapping) else None,
            expires_at=(approval or {}).get("expires_at"),
            detail=str(details.get("reason") or ""),
        )
    elif outcome is CallOutcome.UNCERTAIN:
        interaction = Interaction(
            interaction_id=f"{operation_id}:reconciliation",
            kind=InteractionKind.RECONCILIATION,
            call_id=call.call_id,
            tool_name=call.tool_name,
            operation_id=operation_id,
            args_hash=str(details.get("args_hash") or ""),
            detail=str(details.get("reason") or "the outcome could not be proven"),
        )
    return (
        replace(
            call,
            operation_id=operation_id,
            model_action_id=str(details.get("model_action_id") or call.model_action_id),
            status=status,
            outcome=outcome,
            effect_state=details.get("effect_state"),
            approval_binding_key=(approval or {}).get("binding_key"),
            interaction_id=interaction.interaction_id if interaction else None,
            # The record is kept for every outcome, not only settled ones: a recovered
            # batch has to be able to write a well-formed transcript for a call that is
            # waiting or unproven (the model must still see an answer for every call).
            result=message_to_json(message),
            reason=str(details.get("reason") or ""),
        ),
        interaction,
    )


async def _execute_tool(
    tool: Any,
    call_id: str,
    arguments: Mapping[str, Any],
    signal: Any,
    *,
    model_action_id: str = "",
) -> Any:
    """Invoke a tool, passing the batch reference when the tool can receive it.

    Resuming must derive the *same* operation id the call paused under, which is why the
    recorded model action travels with the re-dispatch. A tool written before the
    parameter existed is called exactly as before.
    """
    from ..core.tool_execution import accepts_batch_reference

    if model_action_id and accepts_batch_reference(getattr(tool, "execute", None)):
        return await tool.execute(
            call_id, arguments, signal, None, model_action_id=model_action_id
        )
    return await tool.execute(call_id, arguments, signal, None)


def _empty_consumed() -> tuple[()]:
    """The per-invocation event-id table, empty (a new driver has consumed nothing)."""
    return ()


def _state_without_snapshot(
    record: RunRecord, batch: RunBatch, identity: RunIdentity
) -> ExecutionState:
    """A driver state for a record that predates the execution snapshot.

    Read from the committed transcript rather than invented: the assistant turn is the
    one that declares exactly these calls, the conversation is the record's own transcript
    up to and including that turn (the batch's results are re-added by the settlement),
    and the turn count is what the record already spent. A record whose declaring turn
    cannot be identified unambiguously is refused instead of guessed at.

    The turn cap is left unknown, which is this build's previous behaviour for such a
    record: the cap was never recorded, and inventing one would be a new limit on someone
    else's run.
    """
    from ..core.types import message_from_json

    messages = [message_from_json(dict(item)) for item in record.messages]
    call_ids = {call.call_id for call in batch.calls}
    declaring = [
        index
        for index, message in enumerate(messages)
        if getattr(message, "role", None) == "assistant"
        and call_ids <= {block.id for block in tool_call_blocks(message)}
    ]
    if len(declaring) != 1:
        raise ContractError(
            f"run {record.run_id!r} carries a recorded batch but no committed execution "
            f"state, and {len(declaring)} of its assistant turns declare those calls; exactly "
            "one has to, or the calls cannot be continued without guessing which turn they "
            "belong to"
        )
    index = declaring[0]
    model_action_id = next(
        (call.model_action_id for call in batch.calls if call.model_action_id), ""
    )
    return ExecutionState(
        messages=tuple(messages[: index + 1]),
        attempt_id=identity.attempt_id,
        turns_used=record.turns_used,
        model_action_id=model_action_id,
        last_assistant=messages[index],
    )


def _dispatchable(call: BatchCall) -> bool:
    """Whether a recovered batch still owes this call a dispatch (or a fresh answer).

    Only a call that is *waiting* for a human or that was never reached may be dispatched
    again. A settled call keeps its receipt, and an unproven call is a question, not a
    licence: dispatching it is how an effect gets duplicated.
    """
    return call.outcome in {CallOutcome.AWAITING, CallOutcome.UNSTARTED}


def _conversation_before_batch(snapshot: ExecutionState, batch: RunBatch) -> tuple[Any, ...]:
    """The conversation up to the assistant turn, without this batch's results.

    The batch's results enter the transcript when the batch settles (that is what
    ``ToolBatchSettled`` writes), so the state a recovery continues from must not already
    carry them: a paused record keeps a placeholder answer for every declared call so the
    turn stays well formed, and a resumed run would otherwise answer the same call twice.
    Only the trailing results that belong to *this* batch are dropped — an earlier batch's
    results stay exactly where they were committed.
    """
    messages = list(snapshot.messages)
    call_ids = {call.call_id for call in batch.calls}
    while (
        messages
        and getattr(messages[-1], "role", None) == "toolResult"
        and getattr(messages[-1], "tool_call_id", None) in call_ids
    ):
        messages.pop()
    return tuple(messages)


def _state_from_batch(
    snapshot: ExecutionState,
    batch: RunBatch,
    attempt_id: str,
    *,
    phase: Phase = Phase.RUNNING_BATCH,
) -> ExecutionState:
    """Rebuild the execution partition from a recovered batch.

    A call the batch still owes a dispatch is left ``DECLARED`` — not observed — so the
    shared batch session dispatches exactly it, with the concurrency limit, the sequential
    barrier, the hooks and the per-call commit a fresh batch gets. A call that is already
    answered is ``OBSERVED`` and is never run again. The recorded schedule progress is
    preserved where it exists, because a call that was never dispatched and is still
    unproven must not look like one that was.
    """
    prior = {call.call_id: call for call in snapshot.batch}
    states: list[BatchCallState] = []
    for index, call in enumerate(batch.calls):
        recorded = prior.get(call.call_id)
        recorded_terminate = bool(recorded.terminate) if recorded is not None else False
        observed = not _dispatchable(call) and call.result is not None
        states.append(
            BatchCallState(
                call_id=call.call_id,
                tool_name=call.tool_name,
                outcome=call.outcome,
                status=call.status,
                result=None if call.result is None else dict(call.result),
                operation_id=call.operation_id,
                approval_binding_key=call.approval_binding_key,
                reason=call.reason,
                declared_index=index,
                model_action_id=call.model_action_id or snapshot.model_action_id,
                progress=CallProgress.OBSERVED if observed else CallProgress.DECLARED,
                effect_state=call.effect_state,
                observation_id=f"{call.call_id}:{index}:recovered" if observed else "",
                observed_digest=(
                    fingerprint(
                        {
                            "call_id": call.call_id,
                            "outcome": call.outcome.value,
                            "status": call.status,
                            "result": None if call.result is None else dict(call.result),
                        }
                    )
                    if observed
                    else ""
                ),
                source_attempt=attempt_id,
                terminate=recorded_terminate,
            )
        )
    return replace(
        snapshot,
        # The batch is being executed again: the phase, the conversation and the absence
        # of a verdict are what a running batch looks like, so the shared driver accepts
        # it exactly as it accepts a freshly declared one.
        phase=phase,
        attempt_id=attempt_id,
        batch=tuple(states),
        messages=_conversation_before_batch(snapshot, batch),
        request_view=(),
        context_state=None,
        # A recovery invocation is a new driver with its own event-id space, so the ids the
        # *dead* one had consumed are not comparable with the ones this one will produce:
        # carrying them over turns a legitimate second entry ("recovered, paused again,
        # recovered again") into a false id conflict. What must survive is the committed
        # work — the partition, the operation references and the conversation — and that
        # does survive, in the fields above.
        consumed=_empty_consumed(),
        # Likewise the delta: this invocation's ``AgentEnd`` reports what *it* produced,
        # not what the process that died had already announced.
        new_messages=(),
        # And the *pending measurement* slot: the response it measured was committed (with
        # this fact as an event) by the invocation that took it, so a recovered state must
        # not announce it a second time. A measurement is a fact about one moment; the log
        # already holds it.
        last_request_measurement=None,
        cancel_requested=False,
        cancel_reason="",
        interaction=None,
        status=None,
        stop=None,
        error_code=None,
        detail="",
    )


def _checkpoint_for(
    batch: RunBatch, identity: RunIdentity, recorded: Mapping[str, Any] | None = None
) -> RunCheckpoint:
    """Express a recorded batch as a checkpoint so the tested drift rules apply."""
    executed: set[str] = set()
    unexecuted: set[str] = set()
    uncertain: set[str] = set()
    for call in batch.calls:
        if call.outcome is CallOutcome.SETTLED:
            executed.add(call.call_id)
        elif call.outcome is CallOutcome.UNCERTAIN:
            uncertain.add(call.call_id)
        else:
            unexecuted.add(call.call_id)
    interactions = tuple(
        PendingInteractionRef(
            interaction_id=item.interaction_id,
            call_id=item.call_id,
            tool_name=item.tool_name,
            args_hash=item.args_hash or f"{item.call_id}:unrecorded",
            target_version=item.target_version,
            expires_at=item.expires_at,
        )
        for item in batch.interactions
    )
    if not interactions:
        # The checkpoint type requires a question while suspended. A batch with no
        # interaction is not waiting on anything; the unstarted calls are what a
        # resume would dispatch.
        interactions = tuple(
            PendingInteractionRef(
                interaction_id=f"{call.operation_id or call.call_id}:unstarted",
                call_id=call.call_id,
                tool_name=call.tool_name,
                args_hash=f"{call.call_id}:unstarted",
            )
            for call in batch.calls
            if call.outcome is CallOutcome.UNSTARTED
        )
    # A batch whose calls are all settled and which waits on nothing is not a pause: it is
    # the window after the last observation and before the settlement. Expressing it as a
    # suspended checkpoint would require inventing a question, so it is expressed as what
    # it is — an active run with nothing left to dispatch.
    status = RunStatus.SUSPENDED if interactions else RunStatus.RUNNING
    return RunCheckpoint(
        identity=identity,
        status=status,
        pending_tool_calls=tuple(
            CheckpointToolCall(
                call_id=call.call_id,
                tool_name=call.tool_name,
                arguments=dict(call.arguments),
                operation_id=call.operation_id or None,
            )
            for call in batch.calls
        ),
        executed_tool_call_ids=frozenset(executed),
        unexecuted_tool_call_ids=frozenset(unexecuted),
        uncertain_tool_call_ids=frozenset(uncertain),
        pending_interactions=interactions,
        # The checkpoint's fingerprints are the *recorded* baseline: they are the
        # "saved" side of the comparison, while the caller passes the current deployment
        # as the "current" side.
        config_fingerprint=str((recorded or {}).get("config") or ""),
        tools_fingerprint=str((recorded or {}).get("tools") or ""),
        instructions_fingerprint=str((recorded or {}).get("instructions") or ""),
        policy_fingerprint=str((recorded or {}).get("policy") or ""),
        exposure_manifest=(recorded or {}).get("exposure_manifest"),
    )


def _fingerprint_fields(values: Mapping[str, str]) -> dict[str, Any]:
    return {
        "config_fingerprint": values.get("config", ""),
        "tools_fingerprint": values.get("tools", ""),
        "instructions_fingerprint": values.get("instructions", ""),
        "policy_fingerprint": values.get("policy", ""),
    }


def _stored_events(
    run_id: str, events: Sequence[tuple[str, Mapping[str, Any]]]
) -> tuple[StoredEvent, ...]:
    """Validate durable events; a private payload is refused by ``Event`` itself."""
    out: list[StoredEvent] = []
    for index, (kind, payload) in enumerate(events):
        event = Event(kind=EventKind(kind), payload=dict(payload or {}), sequence=index, run_id=run_id)
        out.append(
            StoredEvent(
                sequence=0, kind=event.kind.value, payload=dict(event.payload), run_id=run_id
            )
        )
    return tuple(out)


def _as_event(stored: StoredEvent) -> Event:
    return Event(
        kind=EventKind(stored.kind),
        payload=dict(stored.payload or {}),
        run_id=stored.run_id,
        at=stored.at or 0.0,
    )


def _interaction_payload(interaction: Mapping[str, Any]) -> Mapping[str, Any]:
    return {
        "interaction_id": interaction.get("interaction_id", ""),
        "kind": interaction.get("kind", ""),
        "call_id": interaction.get("call_id", ""),
        "tool_name": interaction.get("tool_name", ""),
        "operation_id": interaction.get("operation_id", ""),
        "detail": interaction.get("detail", ""),
    }


def _apply_result(
    record: RunRecord,
    result: LoopResult,
    seed: Sequence[Any],
    now: float,
    *,
    final_state: ExecutionState | None = None,
    trailing: Sequence[Any] = (),
    context_state: Mapping[str, Any] | None = None,
) -> RunRecord:
    """Map the loop's outcome onto the run record. One direction, no guessing.

    A quiescent final state is the authority: the record is built from the same state
    the loop stopped in, so the transcript — which the progress commits have been
    writing all along — is written once and the snapshot cannot describe a different
    moment than the status does.

    A *cancelled* invocation has no quiescent state (the loop was interrupted inside an
    action), so the messages it announced but never committed are appended to the
    committed prefix: appended once, in order, and never de-duplicated by content, since
    the same sentence can legitimately appear twice in one conversation.
    """
    if final_state is not None:
        if final_state.phase in QUIESCENT_PHASES:
            return _record_from_state(record, final_state, now)
        return replace(
            record,
            status=result.status,
            turns_used=max(result.turns_used, final_state.turns_used),
            stop_code=result.stop.value,
            stop_reason=result.stop_reason,
            error_code=result.error_code,
            detail=result.detail,
            final_text=result.final_text,
            usage=dict(result.usage),
            pending_interaction=result.pending_interaction,
            messages=tuple([*record.messages, *_as_records(trailing)]),
            execution=dict(state_to_record(final_state)),
            # A cancelled preparation has no prepared view of its own, so the last committed
            # one is kept; its summary accounting is replaced by the interrupted attempt's.
            context_state=(
                record.context_state if context_state is None else dict(context_state)
            ),
            updated_at=now,
        )
    # No commit port drove this invocation (a legacy caller): keep the previous rule,
    # which merges the seed and the loop's new messages into the recorded transcript.
    transcript = [*seed, *result.messages] if seed else list(result.messages)
    try:
        messages = _merge_transcript(record, transcript)
    except Exception as exc:  # noqa: BLE001 - a message this build cannot persist
        raise ContractError(
            f"the run produced a message this build cannot persist: {exc}"
        ) from exc
    return replace(
        record,
        status=result.status,
        turns_used=result.turns_used,
        stop_code=result.stop.value,
        stop_reason=result.stop_reason,
        error_code=result.error_code,
        detail=result.detail,
        final_text=result.final_text,
        usage=dict(result.usage),
        pending_interaction=result.pending_interaction,
        messages=messages,
        updated_at=now,
    )


def _record_from_state(record: RunRecord, state: ExecutionState, now: float) -> RunRecord:
    """The run record a committed execution state implies.

    Used for both sides of a pause (the commit port) and of a verdict (``_commit_final``),
    so the record and the snapshot it carries cannot describe two different moments.
    """
    final = state.last_assistant
    return replace(
        record,
        status=state.status if state.status is not None else record.status,
        turns_used=state.turns_used,
        stop_code=state.stop.value if state.stop is not None else record.stop_code,
        stop_reason=_stop_reason_of(final) if final is not None else record.stop_reason,
        error_code=state.error_code,
        detail=state.detail,
        final_text=content_text(final.content) if final is not None else record.final_text,
        usage=(usage_to_json(final.usage) or {}) if final is not None else dict(record.usage),
        pending_interaction=None if state.interaction is None else dict(state.interaction),
        messages=tuple(_as_records(state.messages)),
        execution=dict(state_to_record(state)),
        # The projection facts travel with the state they describe: one transaction, so a
        # reader can never see a request view without the measurement that produced it.
        context_state=_context_state_of(record, state),
        updated_at=now,
    )


def _context_state_of(record: RunRecord, state: ExecutionState) -> Mapping[str, Any] | None:
    """The projection facts a commit should carry: the fresh ones, or the last committed.

    A state between requests (a settled batch, a pause) has no request view of its own, and
    overwriting the record's facts with ``None`` would erase the description of the request
    that was just sent — which is exactly what a reader wants to inspect while a batch runs or
    while a run waits for an approval. So the facts are *sticky until replaced*: a state that
    prepared a new view replaces them, and any other commit keeps them.

    This cannot make a stale projection look current: the facts carry the source head and
    fingerprint they were derived from, and every consumer that may reuse them (a resume, a
    compaction) re-checks that head before trusting them.
    """
    if state.context_state is not None:
        return dict(state.context_state)
    return None if record.context_state is None else dict(record.context_state)


def _stop_reason_of(final: Any) -> str:
    if final is None:
        return ""
    reason = final.stop_reason
    return reason.value if isinstance(reason, StopReason) else str(reason or "")


def _clear_uncommitted(scope: "_Drive | None") -> None:
    """Drop the messages that a successful commit has just made part of the record.

    Called only after the transaction committed, so the invariant holds: ``uncommitted``
    is exactly the messages announced *after* the last committed record, which is what
    makes the interrupted-run append neither duplicating nor losing one.
    """
    if scope is not None:
        scope.uncommitted.clear()


def _verdict_of(record: RunRecord, now: float) -> RunRecord | None:
    """The terminal record the committed snapshot already implies, if there is one.

    A process can die between the reducer reaching its verdict and the transaction that
    records it. The snapshot then *is* the outcome — the loop's own answer — so recovery
    commits it instead of asking the operator to reconcile something that needs no
    reconciliation. An unknown snapshot schema is refused by ``state_from_record`` rather
    than guessed at.
    """
    if not record.execution:
        return None
    snapshot = state_from_record(record.execution)
    if snapshot.phase is not Phase.FINISHED or snapshot.status is None:
        return None
    if snapshot.status not in TERMINAL_STATUSES:
        return None
    return _record_from_state(record, snapshot, now)


def _snapshot_evidence(record: RunRecord) -> str:
    """What the committed execution snapshot proves, for a recovery detail string."""
    if not record.execution:
        return (
            " No execution snapshot was committed, so this record cannot say whether a "
            "tool call was dispatched."
        )
    snapshot = state_from_record(record.execution)
    work = pending_work_of(snapshot)
    if work.get("batch_outcome_unknown"):
        declared = _declared_calls_of(record)
        return (
            " The committed execution state shows the model's response was persisted and "
            f"{len(declared)} tool call(s) were declared, but no outcome was recorded for "
            "them: " + ", ".join(declared) + ". Check the operation ledger for operations "
            "of this run before deciding; a declared call may have run."
        )
    if work.get("model_request_outstanding"):
        return (
            " The committed execution state shows the run stopped inside a model request: "
            "no response was committed, so no tool call of that request can have been "
            "dispatched and no external effect can be attributed to it. That window carries "
            "no recorded batch, so this build does not resume it automatically: the request "
            "is submitted again as a new run, or the host records its own evidence."
        )
    return (
        " The committed execution state shows the run stopped in phase "
        f"{snapshot.phase.value}, and nothing in it can be proven to have been dispatched."
    )


def _snapshot_for_batch(
    execution: Mapping[str, Any] | None, batch: RunBatch, interaction: Mapping[str, Any]
) -> Mapping[str, Any] | None:
    """Rewrite a committed snapshot after a resume re-dispatched a waiting call.

    The recorded batch and the execution snapshot are two facts about the same pause, and
    a re-dispatch can change the partition they share (one call may settle while another
    still waits). Rewriting the snapshot from the batch that was just committed keeps the
    resume-side agreement check meaningful instead of leaving two facts to disagree. A
    record without a snapshot stays without one; nothing is invented.
    """
    if not execution:
        return None
    snapshot = state_from_record(execution)
    return state_to_record(
        replace(
            snapshot,
            phase=Phase.SUSPENDED,
            status=RunStatus.SUSPENDED,
            stop=LoopStop.SUSPENDED,
            batch=tuple(
                # A call that carries a result was *observed*: saying "declared" for a call
                # whose outcome and result are recorded here would describe the pause as
                # uneventful for work that already finished. The observation id follows the
                # same rule the recovery path uses, so both snapshots agree.
                BatchCallState(
                    call_id=call.call_id,
                    tool_name=call.tool_name,
                    outcome=call.outcome,
                    status=call.status,
                    result=None if call.result is None else dict(call.result),
                    operation_id=call.operation_id,
                    approval_binding_key=call.approval_binding_key,
                    reason=call.reason,
                    model_action_id=call.model_action_id or snapshot.model_action_id,
                    progress=(
                        CallProgress.OBSERVED
                        if call.result is not None
                        else CallProgress.DECLARED
                    ),
                    observation_id=(
                        f"{call.call_id}:{index}:recovered"
                        if call.result is not None
                        else ""
                    ),
                )
                for index, call in enumerate(batch.calls)
            ),
            interaction=dict(interaction),
        )
    )


def _merge_transcript(record: RunRecord, transcript: Sequence[Any]) -> tuple[Mapping[str, Any], ...]:
    """Persist the transcript, keeping any messages the record already had.

    On a resume the loop's ``messages`` are only the *new* ones, so the recorded
    prefix is what carries the original conversation. Concatenating keeps one rule
    for both entry points.
    """
    existing = [dict(item) for item in record.messages]
    return tuple([*existing, *_as_records(transcript)])


def _with_results(transcript: Sequence[Any], results: Sequence[Any]) -> list[Any]:
    """Put each resolved call's real result where its placeholder was.

    ``tool_call_id`` is the binding: one call has exactly one result message, and a
    resume replaces the pause placeholder rather than adding a second one. A result
    for a call with no placeholder (the batch paused before reaching it) is appended,
    keeping the caller's (source) order.
    """
    by_call: dict[str, Any] = {}
    for message in results:
        call_id = getattr(message, "tool_call_id", None)
        if call_id:
            by_call[call_id] = message
    placed: set[str] = set()
    merged: list[Any] = []
    for message in transcript:
        is_result = getattr(message, "role", None) == "toolResult"
        call_id = getattr(message, "tool_call_id", None) if is_result else None
        if call_id in by_call:
            merged.append(by_call[call_id])
            placed.add(call_id)
            continue
        merged.append(message)
    for call_id, message in by_call.items():
        if call_id not in placed:
            merged.append(message)
    return merged


def _as_records(messages: Sequence[Any]) -> list[Mapping[str, Any]]:
    return [
        dict(item) if isinstance(item, Mapping) else message_to_json(item) for item in messages
    ]


def _declared_calls_of(record: RunRecord) -> list[str]:
    """The tool calls the committed execution snapshot says were declared.

    Read from the snapshot's own projection, not from the transcript: the transcript's
    assistant message is what the model produced, while the snapshot is what the loop
    actually committed before it started dispatching. Only the dispatch window reports
    this (``batch_outcome_unknown``), because only there is the outcome unrecorded.
    """
    work = record.pending_work()
    if not work.get("batch_outcome_unknown"):
        return []
    return [
        f"{item.get('tool_name')}({item.get('call_id')})"
        for item in work.get("declared") or ()
        if isinstance(item, Mapping)
    ]


def _outcome_of(record: RunRecord) -> RunOutcome:
    return RunOutcome(
        status=record.status,
        run_id=record.run_id,
        final_text=record.final_text,
        usage=dict(record.usage),
        pending_interaction=record.pending_interaction,
        error_code=record.error_code,
        detail=record.detail,
        turns_used=record.turns_used,
        stop_code=record.stop_code,
        # The committed execution snapshot's projection, so a caller reading an outcome
        # can tell "waiting for a decision" from "died mid-request" without loading the
        # whole state. Empty for a record written before the snapshot existed.
        pending_work=record.pending_work(),
        context_state=None if record.context_state is None else dict(record.context_state),
    )
