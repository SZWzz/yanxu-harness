"""The concrete harness: one run per identity, driven by the one model loop.

``DefaultHarness`` is the object a host actually calls. It owns nothing clever:

* **The host supplies identity.** ``RunContext`` carries namespace/session/run and
  the principal; the harness reads them and never derives them from model output.
  Tools come from the constructor, so a model cannot widen what it can see.
* **The host supplies the loop's dependencies.** ``stream_fn`` (a provider bridge),
  ``tools`` (core ``ToolSpec`` adapters), a ``Persistence`` store and an optional
  event listener are all injected; there is no global default and no import of a
  host package.
* **The state is read from the store, not from memory.** ``state()`` answers from
  the persisted run record, so a *new* harness instance over the same store sees
  the same run — and a run that this process never drove is still readable.
* **A duplicate submission is a refusal, not a second loop.** The same request for
  the same run id reuses the stored state (so a host may retry a call safely);
  different input is refused and the stored run is left untouched; a run that is
  currently active raises ``RunBusy``.

Everything the loop reports is a :class:`~yanxu_harness.core.outcome.LoopResult`,
which is mapped onto ``RunStatus`` without guessing: turn exhaustion becomes
``usage_limited``, a model failure ``failed``, a cancellation ``cancelled``, a
pending external decision ``suspended``, and a normal end ``completed``.
"""

from __future__ import annotations

import inspect
import time
from dataclasses import replace
from typing import Any, Awaitable, Callable, Mapping, Sequence

from ..core.agent_loop import AgentLoopConfig, run_agent_loop_result
from ..core.messages import create_initial_system_message
from ..core.outcome import LoopResult, RunStatus, assert_transition
from ..core.types import AgentContext, Model, UserMessage, message_to_json
from ..errors import ContractError, RunBusy, RunNotFound, RunRequestDrift
from ..storage.memory import InMemoryPersistence
from ..storage.ports import Persistence, StorageConflict
from ..versioning import fingerprint
from .context import RunContext
from .harness import RunOutcome, RunRecord, RunRequest, RunState, validate_max_turns

__all__ = ["DefaultHarness", "request_fingerprint"]

Listener = Callable[[Any], Awaitable[None] | None]


def request_fingerprint(request: RunRequest) -> str:
    """A stable fingerprint of everything a run's identity is bound to.

    Two submissions with the same fingerprint are the same request (a retry);
    anything else is drift. ``max_turns`` is part of it: a second call with a
    smaller budget would silently truncate a run that was already authorised to
    continue.
    """
    try:
        messages = [message_to_json(message) for message in request.messages]
    except Exception as exc:  # noqa: BLE001 - unreportable message shape
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


class DefaultHarness:
    """One harness per deployment: runs model loops and persists their lifecycles."""

    def __init__(
        self,
        *,
        stream_fn: Callable[..., Any],
        tools: Sequence[Any] = (),
        persistence: Persistence | None = None,
        listener: Listener | None = None,
        model: Model | None = None,
        system_prompt: str | None = None,
        stop_after_turn: Callable[[Any], Any] | None = None,
        suspend_after_batch: Callable[[Any, Any], Any] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if stream_fn is None:
            raise ContractError(
                "DefaultHarness requires an explicit stream_fn (a provider bridge); "
                "there is no global default stream"
            )
        self._stream_fn = stream_fn
        self._tools = tuple(tools)
        self._persistence: Persistence = persistence if persistence is not None else InMemoryPersistence()
        self._listener = listener
        self._model = model
        self._system_prompt = system_prompt
        # Host policy seams the loop already supports; the harness only wires them.
        # `suspend_after_batch` is how a run reports that it is waiting for an
        # external decision: the loop stops before the next model request and the
        # record carries the interaction.
        self._stop_after_turn = stop_after_turn
        self._suspend_after_batch = suspend_after_batch
        self._clock = clock
        #: Run ids this process is currently driving, mapped to the token a
        #: cancellation cancels. A run in here is active *here*; one in the store
        #: but not here belongs to another driver (or to a dead process).
        self._active: dict[str, Any] = {}

    # -- introspection --------------------------------------------------

    @property
    def durability(self) -> Any:
        """The store's durability. ``VOLATILE`` means no restart recovery."""
        return self._persistence.durability

    @property
    def tools(self) -> tuple[Any, ...]:
        return self._tools

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
                raise RunBusy(
                    identity.run_id, "this run id already has an active invocation"
                )
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
            updated_at=self._clock(),
        )
        try:
            version = await self._persistence.save_run(
                identity.run_id, fresh.to_record(), expected_version=None
            )
        except StorageConflict as exc:
            # Someone else inserted this run id first: that is "busy", not "lost".
            raise RunBusy(identity.run_id, "another invocation created this run first") from exc

        self._active[identity.run_id] = context.cancellation
        try:
            result = await self._drive(request, context, seed)
        finally:
            self._active.pop(identity.run_id, None)

        finished = _apply_result(fresh, result, seed, self._clock())
        assert_transition(fresh.status, finished.status)
        await self._commit_final(identity.run_id, finished, version)
        return _outcome_of(finished)

    async def resume(
        self, run_id: str, resolution: Mapping[str, Any], context: RunContext
    ) -> RunOutcome:
        """Continue a suspended run.

        A run that is not suspended has nothing to resume, and pretending it did
        would overwrite an outcome that was already produced. The suspended path
        (approval-bound checkpoint, fingerprint validation, replay of only the
        provably unexecuted calls) is implemented together with the suspension
        machinery in this same phase; until it exists, this refuses explicitly
        rather than risking a second side effect.
        """
        record, _ = await self._load(run_id)
        if record is None:
            raise RunNotFound(run_id)
        self._assert_same_identity(record, context)
        if record.status is not RunStatus.SUSPENDED:
            raise ContractError(
                f"run {run_id!r} is {record.status.value}; only a suspended run can be resumed"
            )
        raise ContractError(
            f"run {run_id!r} is suspended, but this build cannot resume it yet: resuming requires "
            "the approval-bound checkpoint, and guessing which calls already ran would risk a "
            "second side effect"
        )

    async def cancel(self, run_id: str, reason: str = "") -> None:
        """Ask the run to stop. Idempotent; a finished run is unaffected.

        If this process drives the run, the token the loop is watching is cancelled
        and the run's own outcome records ``cancelled``. Otherwise the request is
        *recorded* on the run — a run this process cannot reach must not be marked
        cancelled, because it may still be running somewhere else.
        """
        token = self._active.get(run_id)
        if token is not None:
            token.cancel(reason or "cancelled")
            return
        record, version = await self._load(run_id)
        if record is None or record.terminal:
            return
        await self._record_cancel_request(run_id, record, version, reason)

    async def state(self, run_id: str, *, context: RunContext | None = None) -> RunState:
        """Read a run's durable state.

        ``context`` is optional because the port's signature carries no identity:
        a host that serves several tenants should pass it, and then the namespace
        and session are checked before anything is returned.
        """
        record, _ = await self._load(run_id)
        if record is None:
            raise RunNotFound(run_id)
        if context is not None:
            self._assert_same_identity(record, context)
        return record.to_state()

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

    async def _drive(self, request: RunRequest, context: RunContext, seed: Sequence[Any]) -> LoopResult:
        prompts = [UserMessage(content=request.prompt)] if request.prompt else []
        agent_context = AgentContext(messages=list(seed), tools=list(self._tools))
        config = AgentLoopConfig(
            stream_fn=self._stream_fn,
            model=self._model,
            tools=list(self._tools),
            max_turns=request.max_turns,
            should_stop_after_turn=self._stop_after_turn,
            should_suspend_after_batch=self._suspend_after_batch,
        )

        async def emit(event: Any) -> None:
            listener = self._listener
            if listener is None:
                return
            outcome = listener(event)
            if inspect.isawaitable(outcome):
                await outcome

        return await run_agent_loop_result(
            prompts, agent_context, config, context.cancellation, emit
        )

    async def _commit_final(self, run_id: str, record: RunRecord, version: int | None) -> None:
        """Commit the terminal or suspended record, never overwriting a terminal one."""
        for _ in range(3):
            try:
                await self._persistence.save_run(run_id, record.to_record(), expected_version=version)
                return
            except StorageConflict:
                current, version = await self._load(run_id)
                if current is None:
                    raise
                if current.terminal:
                    raise StorageConflict(
                        run_id,
                        f"the run was already finished as {current.status.value}; "
                        "not overwriting it",
                    ) from None
                # Only a non-terminal concurrent write can be here (a cancellation
                # request from another driver). Keep our outcome, keep their note.
                record = replace(
                    record,
                    cancel_requested=record.cancel_requested or current.cancel_requested,
                )
        raise StorageConflict(run_id, "could not commit the final run state after retries")

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


def _apply_result(
    record: RunRecord, result: LoopResult, seed: Sequence[Any], now: float
) -> RunRecord:
    """Map a loop result onto the run record. One direction, no guessing."""
    transcript = [*seed, *result.messages]
    try:
        messages = tuple(message_to_json(message) for message in transcript)
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
    )
