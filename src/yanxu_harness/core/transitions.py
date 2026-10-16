"""S2: ``advance(state, event) -> Transition`` — the pure execution reducer.

This is the state machine the two-layer loop in :mod:`yanxu_harness.core.agent_loop`
implements implicitly, made explicit. Every row of the migration table in
``docs/core-state-machine-migration.zh-CN.md`` (``B0``–``B11``, ``E1``–``E3``) is a
branch here, and the table is the contract this file must keep.

What this function may not do, and does not:

* read a clock — every event carries ``at_ms`` and the synthesized tool-declaration
  message uses it;
* create an id — ids come from the events (``event_id``) and from the messages the
  driver already built;
* call a hook, the model or a tool — those are :class:`Action` intents the driver
  executes, feeding the result back as the next event;
* write storage or touch the network;
* mutate its inputs — sequences are copied into tuples, and :class:`ExecutionState`
  is frozen.

Event classification is explicit, because the three cases have different answers:

``accepted``
    the state moved; the transition carries events and actions.
``ignored``
    a duplicate of an event already consumed, or an event that arrived after the
    execution stopped (a stale callback). The state is unchanged and
    ``Transition.ignored_reason`` says why. Idempotent: the same duplicate never
    creates a second model request or a second tool batch.
``refused``
    a :class:`~yanxu_harness.errors.ContractError` is raised: the same ``event_id``
    with different content, an event type this build does not know, or an event that
    cannot be valid in the current phase. Guessing here is how a finished run gets
    revived or a settled batch gets executed twice.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import Any, Callable, Mapping, Sequence

from ..errors import ContractError
from .events import AgentEnd, AgentStart, MessageEnd, MessageStart, TurnEnd, TurnStart
from .execution import (
    Measurement,
    QUIESCENT_PHASES,
    AwaitToolObservation,
    BatchCallState,
    BatchResumed,
    CallProgress,
    CancellationRequested,
    ContextProjected,
    ContinueRequested,
    EvaluateStopHook,
    ExecutionState,
    FinishToolBatch,
    InputEvent,
    InputKind,
    InputsPolled,
    ModelCompleted,
    Phase,
    PollSite,
    PrepareRequest,
    PrepareTurn,
    PromptSubmitted,
    ReadInputs,
    RequestPrepared,
    RunToolBatch,
    SendModelRequest,
    StopHookEvaluated,
    ToolBatchDrained,
    ToolBatchSettled,
    ToolCallObserved,
    Transition,
    TurnPrepared,
    WaitForExternal,
    apply_observation,
    classify_results,
    declared_batch,
    deferred_interaction,
    interaction_for_batch,
    with_phase,
)
from .messages import content_text, declare_tool_changes
from .outcome import (
    CONTEXT_BUDGET_STOP_CODE,
    MAX_TURNS_STOP_CODE,
    CallOutcome,
    LoopResult,
    LoopStop,
    RunStatus,
)
from .tool_execution import tool_call_blocks
from .types import (
    AgentContext,
    AssistantMessage,
    StopReason,
    ToolResultMessage,
    message_from_json,
    usage_to_json,
)

__all__ = ["advance", "result_of", "validate_max_turns"]

_SITE_KIND: Mapping[PollSite, InputKind] = {
    PollSite.INITIAL: InputKind.STEERING,
    PollSite.TURN_BOUNDARY: InputKind.STEERING,
    PollSite.AFTER_PREPARE: InputKind.STEERING,
    PollSite.AFTER_INNER: InputKind.FOLLOW_UP,
}


def validate_max_turns(max_turns: int | None) -> None:
    """Refuse ambiguous request budgets before any event is accepted (legacy ``E3``)."""
    if max_turns is not None and (type(max_turns) is not int or max_turns <= 0):
        raise ValueError("max_turns must be None or a positive integer")


def advance(state: ExecutionState, event: InputEvent) -> Transition:
    """The reducer. Deterministic: the same state and event always give the same answer."""
    if not isinstance(event, InputEvent):
        raise ContractError(
            f"{event!r} is not an InputEvent; the reducer refuses to guess what it means"
        )
    if not isinstance(event.event_id, str) or not event.event_id:
        raise ContractError(f"{event.kind}: every input event needs a non-empty event_id")
    if type(event.at_ms) is not int or event.at_ms < 0:
        raise ContractError(f"{event.kind}: at_ms must be a non-negative integer")
    for name in ("messages", "results", "prepared"):
        value = getattr(event, name, None)
        if value is not None and not isinstance(value, tuple):
            raise ContractError(f"{event.kind}.{name} must be a tuple, not {type(value).__name__}")
    for name in ("context_messages", "context_tools"):
        value = getattr(event, name, None)
        if value is not None and not isinstance(value, tuple):
            raise ContractError(f"{event.kind}.{name} must be a tuple or None, not {type(value).__name__}")

    digest = _digest(event)
    recorded = dict(state.consumed)
    if event.event_id in recorded:
        if recorded[event.event_id] == digest:
            reason = f"{event.kind} {event.event_id} was already applied; ignoring the repeat"
            return Transition(state=state, ignored_reason=reason)
        raise ContractError(
            f"event {event.event_id} was already applied with different content "
            f"({event.kind}); refusing to apply two different facts under one id"
        )

    if state.phase in QUIESCENT_PHASES:
        if isinstance(event, CancellationRequested) and state.phase is Phase.SUSPENDED:
            # A pause can be cancelled while it waits: that is about *this* run, not a
            # stale callback from a previous attempt, so it is recorded. The phase stays
            # suspended until something decides what the recorded cancellation means.
            return Transition(
                state=replace(
                    state, cancel_requested=True, cancel_reason=event.reason or state.cancel_reason
                )
            )
        reason = (
            f"the execution is already {state.phase.value}; {event.kind} {event.event_id} "
            "did not change committed state"
        )
        return Transition(state=state, ignored_reason=reason)

    handler = _HANDLERS.get(type(event))
    if handler is None:
        raise ContractError(
            f"{type(event).__name__} is not an input event this build knows; refusing to "
            "change state for an unknown event"
        )
    transition = handler(state, event)
    if transition.ignored_reason is not None:
        return transition
    consumed = (*state.consumed, (event.event_id, digest))
    return replace(transition, state=replace(transition.state, consumed=consumed))


# --------------------------------------------------------------------------------------
# handlers, one per migration-table row group
# --------------------------------------------------------------------------------------


def _prompt_submitted(state: ExecutionState, event: PromptSubmitted) -> Transition:
    """``E1``: a prompt opens the first turn and the tool delta is committed with it."""
    _require_phase(state, event, Phase.CREATED)
    validate_max_turns(event.max_turns)
    tools = tuple(event.tools) or state.tools
    inserted = tuple(
        declare_tool_changes(
            AgentContext(messages=list(state.messages), tools=list(tools)),
            list(event.messages),
            at_ms=event.at_ms,
        )
    )
    committed = (*state.messages, *inserted)
    events = [AgentStart(), TurnStart()]
    for message in inserted:
        events.extend((MessageStart(message=message), MessageEnd(message=message)))
    next_state = with_phase(
        state,
        Phase.COLLECTING,
        tools=tools,
        max_turns=event.max_turns,
        messages=committed,
        new_messages=(*state.new_messages, *inserted),
        poll_site=PollSite.INITIAL,
    )
    return Transition(
        state=next_state,
        events=tuple(events),
        actions=(ReadInputs(PollSite.INITIAL, InputKind.STEERING),),
    )


def _continue_requested(state: ExecutionState, event: ContinueRequested) -> Transition:
    """``E2``: continue from an existing transcript; the guards are hard, not advisory."""
    _require_phase(state, event, Phase.CREATED)
    validate_max_turns(event.max_turns)
    if not state.messages:
        raise ValueError("Cannot continue: no messages in context")
    if getattr(state.messages[-1], "role", None) == "assistant":
        raise ValueError("Cannot continue from message role: assistant")
    tools = tuple(event.tools) or state.tools
    next_state = with_phase(
        state,
        Phase.COLLECTING,
        tools=tools,
        max_turns=event.max_turns,
        poll_site=PollSite.INITIAL,
    )
    return Transition(
        state=next_state,
        events=(AgentStart(), TurnStart()),
        actions=(ReadInputs(PollSite.INITIAL, InputKind.STEERING),),
    )


def _inputs_polled(state: ExecutionState, event: InputsPolled) -> Transition:
    """``B0`` / ``B1c`` / ``B9`` / ``B10``: one queue read, at a known site."""
    if state.phase is not Phase.COLLECTING or state.poll_site is None:
        raise ContractError(
            f"{event.kind} arrived while the execution is {state.phase.value}; no queue read "
            "was outstanding"
        )
    if event.site is not state.poll_site:
        raise ContractError(
            f"{event.kind} reports site {event.site.value} but the outstanding read was "
            f"{state.poll_site.value}"
        )
    expected = _SITE_KIND[state.poll_site]
    if event.kind is not expected:
        raise ContractError(
            f"{event.kind} reports {event.kind.value} but site {event.site.value} reads "
            f"{expected.value}"
        )
    messages = tuple(event.messages)
    if state.poll_site is PollSite.INITIAL:
        return _issue_request(replace(state, pending=messages), at_ms=event.at_ms, first=True)
    if state.poll_site is PollSite.AFTER_PREPARE:
        return _issue_request(replace(state, pending=messages), at_ms=event.at_ms, first=False)
    if state.poll_site is PollSite.TURN_BOUNDARY:
        # The inner work continues when the batch asked for another turn, even with no
        # queued input; a queued steering message also continues it.
        if messages or state.has_more_tool_calls:
            return _need_prepare(replace(state, pending=messages))
        return Transition(
            state=with_phase(state, Phase.COLLECTING, poll_site=PollSite.AFTER_INNER),
            actions=(ReadInputs(PollSite.AFTER_INNER, InputKind.FOLLOW_UP),),
        )
    if messages:
        return _need_prepare(replace(state, pending=messages))
    return _finish_natural(state)


def _turn_prepared(state: ExecutionState, event: TurnPrepared) -> Transition:
    """``B1b``/``B1c``: the preparation hook answered; the next request may follow."""
    _require_phase(state, event, Phase.AWAITING_PREPARE)
    messages = state.messages if event.context_messages is None else tuple(event.context_messages)
    prepared = tuple(event.messages)
    updated = replace(
        state,
        messages=messages,
        prepared=prepared,
        tools=state.tools if event.context_tools is None else tuple(event.context_tools),
        model=state.model if event.model is None else event.model,
        thinking_level=(
            state.thinking_level if event.thinking_level is None else event.thinking_level
        ),
    )
    if state.pending:
        return _issue_request(updated, at_ms=event.at_ms, first=False)
    return Transition(
        state=with_phase(updated, Phase.COLLECTING, poll_site=PollSite.AFTER_PREPARE),
        actions=(ReadInputs(PollSite.AFTER_PREPARE, InputKind.STEERING),),
    )


def _model_completed(state: ExecutionState, event: ModelCompleted) -> Transition:
    """``B3``–``B5b``: the turn's final message decides what happens next."""
    _require_phase(state, event, Phase.REQUESTING_MODEL)
    message = event.message
    if message is None:
        raise ContractError("ModelCompleted must carry the final assistant message")
    if not isinstance(message, AssistantMessage):
        raise ContractError(f"ModelCompleted needs an AssistantMessage, got {type(message).__name__}")
    committed = (*state.messages, message)
    # The dispatch is over: whatever the driver measured about it is now a fact waiting for
    # the next commit, and the outstanding-dispatch slots are free again. The reducer
    # records the numbers it was handed; it does not read a clock.
    request_instance = event.request_instance or state.request_instance
    measurement = (
        None
        if not request_instance
        else Measurement(
            stage="model:request",
            reference=request_instance,
            attempt=state.attempt_id,
            started_at=state.request_started_at,
            duration_ms=event.duration_ms,
            clock="monotonic" if event.duration_ms is not None else "",
            payload_bytes=event.payload_bytes,
            payload_bytes_estimator=event.payload_bytes_estimator,
            usage=None if event.usage is None else dict(event.usage),
            source="observed",
            note=(
                ""
                if event.duration_ms is not None
                else "the driver did not measure this dispatch, so it has no duration"
            ),
        )
    )
    base = replace(
        state,
        messages=committed,
        new_messages=(*state.new_messages, message),
        last_assistant=message,
        last_tool_results=(),
        request_instance="" if request_instance else state.request_instance,
        request_started_at=None if request_instance else state.request_started_at,
        last_request_measurement=(
            measurement
            if measurement is not None
            else state.last_request_measurement
        ),
        # A new turn resets the previous batch's continuation flags, exactly as the
        # legacy loop does at the top of every iteration.
        has_more_tool_calls=False,
        batch_terminated=False,
        batch=(),
    )
    # ``MessageEnd`` and every streaming frame belong to the driver's progress path;
    # the reducer owns the turn boundary. What it emits here is only what the driver
    # cannot emit for itself without guessing the outcome.
    events: list[Any] = []

    if message.stop_reason is StopReason.DEFERRED:
        # The model itself said this turn is not finished: a paused run, not an answer.
        interaction = deferred_interaction(message)
        next_state = with_phase(
            base,
            Phase.SUSPENDED,
            interaction=interaction,
            status=RunStatus.SUSPENDED,
            stop=LoopStop.SUSPENDED,
        )
        events.extend((TurnEnd(message=message, tool_results=()), AgentEnd(messages=next_state.new_messages)))
        return Transition(
            state=next_state,
            events=tuple(events),
            actions=(WaitForExternal(interaction),),
        )
    if message.stop_reason in (StopReason.ERROR, StopReason.ABORTED):
        aborted = message.stop_reason is StopReason.ABORTED
        next_state = with_phase(
            base,
            Phase.FINISHED,
            status=RunStatus.CANCELLED if aborted else RunStatus.FAILED,
            stop=LoopStop.ABORTED if aborted else LoopStop.MODEL_ERROR,
            error_code="cancelled" if aborted else (message.raw_stop_reason or "model_error"),
            detail=message.error_message or (
                "the run was cancelled" if aborted else "the model request failed"
            ),
        )
        events.extend((TurnEnd(message=message, tool_results=()), AgentEnd(messages=next_state.new_messages)))
        return Transition(state=next_state, events=tuple(events))

    calls = tool_call_blocks(message)
    if calls:
        truncated = message.stop_reason is StopReason.LENGTH
        # The declaration partition is part of the *state*, so the host's commit port
        # commits the identity, source order and argument digest of every declared call
        # before the batch action runs. Nothing is dispatched from an uncommitted
        # declaration.
        declared = declared_batch(calls, model_action_id=base.model_action_id)
        return Transition(
            state=with_phase(base, Phase.RUNNING_BATCH, batch=declared),
            events=tuple(events),
            actions=(
                RunToolBatch(
                    calls=tuple(calls),
                    truncated=truncated,
                    model_action_id=base.model_action_id,
                ),
            ),
        )
    events.append(TurnEnd(message=message, tool_results=()))
    return Transition(
        state=with_phase(base, Phase.AWAITING_STOP_HOOK),
        events=tuple(events),
        actions=(EvaluateStopHook(message=message, tool_results=(), turn=state.turns_used),),
    )


def _tool_batch_settled(state: ExecutionState, event: ToolBatchSettled) -> Transition:
    """``B5b``–``B5d``: results are committed, then the batch decides whether to pause."""
    _require_phase(state, event, Phase.RUNNING_BATCH)
    assistant = state.last_assistant
    if assistant is None:
        raise ContractError("ToolBatchSettled arrived without a recorded assistant turn")
    declared = tool_call_blocks(assistant)
    # The incremental branch is taken only when the driver actually reported per-call
    # observations. A driver that hands over one whole-batch payload (the compatibility
    # path) has no observed calls, and is classified exactly as before.
    if state.batch and any(call.observed for call in state.batch):
        # The incremental path: each call's result was already accepted and committed as
        # an observation, so the settlement is derived from that committed partition. A
        # batch-shaped payload that disagrees with it is refused rather than allowed to
        # overwrite facts the store has already accepted.
        if event.results:
            provided = {result.tool_call_id: _result_record(result) for result in event.results}
            for call in state.batch:
                if call.result is None or call.call_id not in provided:
                    continue
                if provided[call.call_id] != dict(call.result):
                    raise ContractError(
                        f"{call.call_id}: the settled batch contradicts the committed observation "
                        "for this call; refusing to overwrite an accepted result"
                    )
            # The driver's own messages are used for the transcript: they are the same
            # facts the store accepted, and keeping them avoids a lossy second encoding.
            results = tuple(event.results)
        else:
            results = tuple(
                message_from_json(dict(call.result))
                for call in state.batch
                if call.result is not None
            )
        _assert_results_belong_to_this_batch(declared, results)
        batch = state.batch
        terminate = _batch_terminated(batch)
    else:
        results = tuple(event.results)
        _assert_results_belong_to_this_batch(declared, results)
        batch = classify_results(declared, results)
        terminate = event.terminate
    base = replace(
        state,
        messages=(*state.messages, *results),
        new_messages=(*state.new_messages, *results),
        last_tool_results=results,
        batch=batch,
        has_more_tool_calls=not terminate,
        batch_terminated=bool(results) and terminate,
    )
    events: list[Any] = []
    events.append(TurnEnd(message=assistant, tool_results=results))

    interaction = dict(event.suspend) if event.suspend else interaction_for_batch(batch)
    if interaction:
        next_state = with_phase(
            base,
            Phase.SUSPENDED,
            interaction=interaction,
            status=RunStatus.SUSPENDED,
            stop=LoopStop.SUSPENDED,
        )
        events.append(AgentEnd(messages=next_state.new_messages))
        return Transition(
            state=next_state,
            events=tuple(events),
            actions=(WaitForExternal(interaction),),
        )
    return Transition(
        state=with_phase(base, Phase.AWAITING_STOP_HOOK),
        events=tuple(events),
        actions=(
            EvaluateStopHook(message=assistant, tool_results=results, turn=state.turns_used),
        ),
    )


def _stop_hook_evaluated(state: ExecutionState, event: StopHookEvaluated) -> Transition:
    """``B7``/``B8``/``B9``: a host stop, the turn budget, then the next queue read."""
    _require_phase(state, event, Phase.AWAITING_STOP_HOOK)
    if event.stop:
        next_state = with_phase(
            state, Phase.FINISHED, status=RunStatus.COMPLETED, stop=LoopStop.STOP_HOOK
        )
        return Transition(state=next_state, events=(AgentEnd(messages=next_state.new_messages),))
    if state.max_turns is not None and state.turns_used >= state.max_turns:
        next_state = with_phase(
            state,
            Phase.FINISHED,
            status=RunStatus.USAGE_LIMITED,
            stop=LoopStop.MAX_TURNS,
            error_code=MAX_TURNS_STOP_CODE,
            detail=f"the turn budget of {state.max_turns} was reached",
        )
        return Transition(state=next_state, events=(AgentEnd(messages=next_state.new_messages),))
    return Transition(
        state=with_phase(state, Phase.COLLECTING, poll_site=PollSite.TURN_BOUNDARY),
        actions=(ReadInputs(PollSite.TURN_BOUNDARY, InputKind.STEERING),),
    )


def _cancellation_requested(state: ExecutionState, event: CancellationRequested) -> Transition:
    """Cancellation is recorded, never rewinds a settled effect.

    The legacy loop checks its token in the model stream and between tool calls, and
    reports the cancellation at the end of the invocation; this records the same fact
    and lets the following events end the run.
    """
    return Transition(
        state=replace(
            state,
            cancel_requested=True,
            cancel_reason=event.reason or state.cancel_reason,
        )
    )


def _context_projected(state: ExecutionState, event: ContextProjected) -> Transition:
    """A persistent transform/projection replaced the active conversation."""
    _require_phase(state, event, Phase.REQUESTING_MODEL)
    return Transition(state=replace(state, messages=tuple(event.messages)))


def _request_prepared(state: ExecutionState, event: RequestPrepared) -> Transition:
    """``B3a``: record the prepared request view before anything is dispatched.

    Two facts decide this transition's shape:

    * The view is *derived*. It is stored beside ``messages`` (the authoritative
      conversation), never instead of it, so a projection can never shrink the history —
      the defect that ``persist_transformed_context`` has and that the managed path must
      not repeat.
    * A refusal is a result, not a failure. It ends the run ``usage_limited`` with its own
      code, spending no turn and asking no model, which is why it can never be confused
      with a provider error or with a turn-budget stop.

    The host's commit port runs after this transition and before the driver's
    ``SendModelRequest``, which is what makes "the projection facts are durable before
    the request is sent" true by construction rather than by hoping.
    """
    _require_phase(state, event, Phase.REQUESTING_MODEL)
    if event.refused:
        refused = dict(event.refused)
        # A refusal carries the facts the preparation did produce (its versioned refusal
        # record). A refusal at a *later* checkpoint — the final byte gate — arrives with
        # no facts of its own, so the ones already committed are amended instead: a reader
        # must be able to see both what was prepared and why it was not sent.
        facts = event.context_state if event.context_state is not None else state.context_state
        context_state = None if facts is None else dict(facts)
        if context_state is not None:
            context_state["refused"] = dict(refused)
        next_state = with_phase(
            state,
            Phase.FINISHED,
            status=RunStatus.USAGE_LIMITED,
            stop=LoopStop.CONTEXT_BUDGET_EXCEEDED,
            error_code=str(refused.get("error_code") or CONTEXT_BUDGET_STOP_CODE),
            detail=str(refused.get("detail") or "context preparation refused the request"),
            context_state=context_state,
        )
        return Transition(state=next_state, events=(AgentEnd(messages=next_state.new_messages),))
    view = tuple(event.view) if event.view else tuple(state.messages)
    next_state = with_phase(
        state,
        Phase.REQUESTING_MODEL,
        request_view=view,
        context_state=None if event.context_state is None else dict(event.context_state),
        # The dispatch window opens here, and this transition is committed before the
        # driver sends: a request that never comes back still leaves a start behind.
        request_instance=event.request_instance or state.request_instance,
        request_started_at=(
            event.started_at if event.started_at is not None else state.request_started_at
        ),
    )
    return Transition(
        state=next_state,
        actions=(
            SendModelRequest(
                turn=state.turns_used,
                view=view,
                model=state.model,
                thinking_level=state.thinking_level,
            ),
        ),
    )


def _tool_call_observed(state: ExecutionState, event: ToolCallObserved) -> Transition:
    """One call of the outstanding batch was observed; record exactly that call.

    Nothing here appends to the transcript: the model-visible result messages are
    written once, in declared order, when the batch settles. What this transition does
    is turn one driver observation into a committed per-call fact — outcome, ledger
    reference, result and observation digest — and then ask for the next one.
    """
    _require_phase(state, event, Phase.RUNNING_BATCH)
    call = state.call(event.call_id)
    if call is None:
        raise ContractError(
            f"ToolCallObserved names {event.call_id!r}, which is not part of the declared "
            f"batch of {state.model_action_id or 'this model action'}; refusing to apply an "
            "observation from another batch or turn"
        )
    if event.model_action_id and call.model_action_id and event.model_action_id != call.model_action_id:
        raise ContractError(
            f"{event.call_id}: the observation belongs to model action "
            f"{event.model_action_id!r}, not {call.model_action_id!r}"
        )
    if event.source_attempt and state.attempt_id and event.source_attempt != state.attempt_id:
        raise ContractError(
            f"{event.call_id}: the observation came from attempt {event.source_attempt!r}, "
            f"but this execution runs under {state.attempt_id!r}; a stale callback must not "
            "update a new attempt"
        )
    if event.tool_name and event.tool_name != call.tool_name:
        raise ContractError(
            f"{event.call_id}: the observation names tool {event.tool_name!r}, but the declared "
            f"call is {call.tool_name!r}"
        )
    try:
        outcome = CallOutcome(str(event.outcome))
    except ValueError as exc:
        raise ContractError(
            f"{event.call_id}: {event.outcome!r} is not a call outcome this build knows"
        ) from exc
    if outcome is CallOutcome.UNSTARTED:
        raise ContractError(
            f"{event.call_id}: an observation may not report `unstarted`; an unobserved call "
            "is simply not reported"
        )
    if outcome is CallOutcome.SETTLED and event.result is None:
        raise ContractError(f"{event.call_id}: a settled observation must carry its result")
    if call.observed:
        if call.observed_digest and call.observed_digest == event.observed_digest:
            return Transition(
                state=state,
                ignored_reason=(
                    f"the observation of {event.call_id} was already applied; ignoring the repeat"
                ),
            )
        raise ContractError(
            f"{event.call_id}: a second, different observation arrived for a call that already "
            "has one; refusing to merge two answers to the same call"
        )
    updated = replace(
        call,
        outcome=outcome,
        status=event.status,
        result=None if event.result is None else dict(event.result),
        operation_id=event.operation_id or call.operation_id,
        effect_state=event.effect_state,
        tool_version=event.tool_version or call.tool_version,
        progress=CallProgress.OBSERVED,
        observation_id=event.observation_id or call.observation_id,
        observed_digest=event.observed_digest,
        source_attempt=event.source_attempt or state.attempt_id,
        started_at=event.started_at,
        duration_ms=event.duration_ms,
        receipt=None if event.receipt is None else dict(event.receipt),
        terminate=bool(event.terminate),
        reason=event.reason or call.reason,
    )
    batch = tuple(updated if item.call_id == call.call_id else item for item in state.batch)
    next_state = replace(state, batch=batch)
    remaining = tuple(item.call_id for item in batch if not item.observed)
    if remaining:
        return Transition(
            state=next_state,
            actions=(
                AwaitToolObservation(
                    model_action_id=state.model_action_id, awaiting=remaining
                ),
            ),
        )
    return Transition(
        state=next_state,
        actions=(FinishToolBatch(model_action_id=state.model_action_id),),
    )


def _batch_resumed(state: ExecutionState, event: BatchResumed) -> Transition:
    """Continue a committed, partially settled batch under a new attempt.

    Nothing is re-declared and no model request is made: the partition and the accepted
    observations are already in the state, and this only asks the driver for the calls it
    has not observed yet. The declaration the batch is resumed under is the one that was
    committed before the interruption, which is what keeps the resumed attempts on the
    same logical calls.
    """
    _require_phase(state, event, Phase.RUNNING_BATCH)
    if not state.batch:
        raise ContractError(
            "BatchResumed needs a committed declaration partition; there is none, so the "
            "calls of this batch cannot be identified and must not be guessed at"
        )
    if event.model_action_id and state.model_action_id and event.model_action_id != state.model_action_id:
        raise ContractError(
            f"the batch was resumed as model action {event.model_action_id!r}, but this "
            f"execution is {state.model_action_id!r}"
        )
    assistant = state.last_assistant
    declared = tool_call_blocks(assistant) if assistant is not None else []
    by_id = {call.id: call for call in declared}
    remaining = tuple(item for item in state.batch if not item.observed)
    if not remaining:
        return Transition(
            state=state, actions=(FinishToolBatch(model_action_id=state.model_action_id),)
        )
    missing = [item.call_id for item in remaining if item.call_id not in by_id]
    if missing:
        raise ContractError(
            f"the committed batch names call(s) {missing} that the recorded assistant turn "
            "does not declare; refusing to dispatch a call whose arguments are unknown"
        )
    return Transition(
        state=state,
        actions=(
            RunToolBatch(
                calls=tuple(by_id[item.call_id] for item in remaining),
                truncated=False,
                model_action_id=state.model_action_id,
            ),
        ),
    )


def _tool_batch_drained(state: ExecutionState, event: ToolBatchDrained) -> Transition:
    """The session has nothing more to report; finish the batch.

    This is the path a sequential batch takes when it was cancelled between calls: the
    calls it never reached stay ``unstarted``, and nothing here claims they did not run.
    """
    _require_phase(state, event, Phase.RUNNING_BATCH)
    if event.model_action_id and state.model_action_id and event.model_action_id != state.model_action_id:
        raise ContractError(
            f"a drained signal for model action {event.model_action_id!r} arrived while "
            f"{state.model_action_id!r} is outstanding"
        )
    return Transition(
        state=state,
        actions=(FinishToolBatch(model_action_id=event.model_action_id or state.model_action_id),),
    )


def _batch_terminated(batch: Sequence[BatchCallState]) -> bool:
    """Only a non-empty set of observed calls that all asked to terminate ends the batch."""
    observed = [call for call in batch if call.observed]
    return bool(observed) and all(call.terminate for call in observed)


def _result_record(result: ToolResultMessage) -> Mapping[str, Any]:
    from .types import message_to_json

    return message_to_json(result)


_HANDLERS: Mapping[type, Callable[[ExecutionState, Any], Transition]] = {
    PromptSubmitted: _prompt_submitted,
    ContinueRequested: _continue_requested,
    InputsPolled: _inputs_polled,
    TurnPrepared: _turn_prepared,
    ModelCompleted: _model_completed,
    ToolCallObserved: _tool_call_observed,
    ToolBatchDrained: _tool_batch_drained,
    BatchResumed: _batch_resumed,
    ToolBatchSettled: _tool_batch_settled,
    StopHookEvaluated: _stop_hook_evaluated,
    CancellationRequested: _cancellation_requested,
    ContextProjected: _context_projected,
    RequestPrepared: _request_prepared,
}


# --------------------------------------------------------------------------------------
# small pieces
# --------------------------------------------------------------------------------------


def _need_prepare(state: ExecutionState) -> Transition:
    """``B1b``: hand the finished turn to the preparation hook."""
    return Transition(
        state=with_phase(state, Phase.AWAITING_PREPARE, prepared=()),
        actions=(
            PrepareTurn(
                message=state.last_assistant,
                tool_results=state.last_tool_results,
                turn=state.turns_used,
            ),
        ),
    )


def _issue_request(state: ExecutionState, *, at_ms: int, first: bool) -> Transition:
    """``B2``/``B1d``: declare the tool delta, then ask the model for one turn.

    The budget is checked here as well as after a turn, because a *resumed* invocation
    starts with ``turns_used`` already spent: without this check, pausing would be a way
    to buy another model request. A request that is over budget is not made; the state
    reports the budget stop instead, and the recorded batch results stay committed.
    """
    if state.max_turns is not None and state.turns_used >= state.max_turns:
        next_state = with_phase(
            state,
            Phase.FINISHED,
            status=RunStatus.USAGE_LIMITED,
            stop=LoopStop.MAX_TURNS,
            error_code=MAX_TURNS_STOP_CODE,
            detail=f"the turn budget of {state.max_turns} was reached",
        )
        return Transition(state=next_state, events=(AgentEnd(messages=next_state.new_messages),))
    inserted = tuple(
        declare_tool_changes(
            AgentContext(messages=list(state.messages), tools=list(state.tools)),
            [*state.prepared, *state.pending],
            at_ms=at_ms,
        )
    )
    events: list[Any] = []
    if not first:
        events.append(TurnStart())
    for message in inserted:
        events.extend((MessageStart(message=message), MessageEnd(message=message)))
    turns = state.turns_used + 1
    messages = (*state.messages, *inserted)
    next_state = with_phase(
        state,
        Phase.REQUESTING_MODEL,
        messages=messages,
        new_messages=(*state.new_messages, *inserted),
        prepared=(),
        pending=(),
        turns_used=turns,
        model_action_id=f"request:{turns}",
        # A new turn prepares its own view: the previous turn's view must never be sent
        # for a request it was not built for.
        request_view=(),
        context_state=None,
    )
    return Transition(
        state=next_state,
        events=tuple(events),
        actions=(
            PrepareRequest(
                turn=turns,
                messages=messages,
                model=state.model,
                thinking_level=state.thinking_level,
            ),
        ),
    )


def _finish_natural(state: ExecutionState) -> Transition:
    """``B11``: nothing left to do — unless the caller asked for a stop meanwhile."""
    if state.cancel_requested:
        next_state = with_phase(
            state,
            Phase.FINISHED,
            status=RunStatus.CANCELLED,
            stop=LoopStop.ABORTED,
            error_code="cancelled",
            detail="the run was cancelled between turns",
        )
        return Transition(state=next_state, events=(AgentEnd(messages=next_state.new_messages),))
    next_state = with_phase(
        state,
        Phase.FINISHED,
        status=RunStatus.COMPLETED,
        stop=LoopStop.BATCH_TERMINATE if state.batch_terminated else LoopStop.ASSISTANT_FINAL,
    )
    return Transition(state=next_state, events=(AgentEnd(messages=next_state.new_messages),))


def _require_phase(state: ExecutionState, event: InputEvent, phase: Phase) -> None:
    if state.phase is not phase:
        raise ContractError(
            f"{event.kind} {event.event_id} cannot be applied in phase {state.phase.value}; "
            f"it requires {phase.value}"
        )


def _assert_results_belong_to_this_batch(
    declared: Sequence[ToolCall], results: Sequence[ToolResultMessage]
) -> None:
    """A settled batch must answer the calls of *this* turn, exactly once each.

    Without this check a stale result (an old attempt, or a result for a call of a
    previous turn) would be silently dropped by the partition — and a dropped result is
    indistinguishable from a call that never ran, which is how a settled effect gets
    replayed. Extra results are refused rather than ignored.
    """
    declared_ids = [call.id for call in declared]
    result_ids = [result.tool_call_id for result in results]
    unknown = [call_id for call_id in result_ids if call_id not in declared_ids]
    if unknown:
        raise ContractError(
            f"a settled batch reported results for call(s) this turn never declared: "
            f"{unknown}; refusing to apply a result that belongs to another attempt or turn"
        )
    repeated = {call_id for call_id in result_ids if result_ids.count(call_id) > 1}
    if repeated:
        raise ContractError(
            f"a settled batch reported more than one result for {sorted(repeated)}; a call has "
            "one outcome, and two answers to it is not something to merge"
        )


def _digest(event: InputEvent) -> str:
    return hashlib.sha256(repr(event).encode("utf-8")).hexdigest()


def result_of(state: ExecutionState) -> LoopResult:
    """Project a finished or suspended state into the public :class:`LoopResult`.

    The status and stop code come from the state, never from the last message's role,
    the answer's text or the number of events.
    """
    if state.phase not in QUIESCENT_PHASES:
        raise ContractError(
            f"the execution is still {state.phase.value}; a loop result is an outcome, "
            "not a phase"
        )
    if state.status is None or state.stop is None:
        raise ContractError("a stopped execution must carry its status and stop code")
    final = state.last_assistant
    return LoopResult(
        status=state.status,
        stop=state.stop,
        messages=state.new_messages,
        turns_used=state.turns_used,
        stop_reason=_stop_reason_of(final),
        error_code=state.error_code,
        detail=state.detail,
        pending_interaction=state.interaction,
        final_text=content_text(final.content) if final is not None else "",
        usage=(usage_to_json(final.usage) or {}) if final is not None else {},
    )


def _stop_reason_of(final: AssistantMessage | None) -> str:
    if final is None:
        return StopReason.PENDING.value
    reason = final.stop_reason
    return reason.value if isinstance(reason, StopReason) else str(reason)
