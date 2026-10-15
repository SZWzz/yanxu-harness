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
from typing import Any, Callable, Mapping

from ..errors import ContractError
from .events import AgentEnd, AgentStart, MessageEnd, MessageStart, TurnEnd, TurnStart
from .execution import (
    QUIESCENT_PHASES,
    CancellationRequested,
    ContextProjected,
    ContinueRequested,
    EvaluateStopHook,
    ExecutionState,
    InputEvent,
    InputKind,
    InputsPolled,
    ModelCompleted,
    Phase,
    PollSite,
    PrepareTurn,
    PromptSubmitted,
    ReadInputs,
    RequestModel,
    RunToolBatch,
    StopHookEvaluated,
    ToolBatchSettled,
    Transition,
    TurnPrepared,
    classify_results,
    deferred_interaction,
    interaction_for_batch,
    with_phase,
)
from .messages import content_text, declare_tool_changes
from .outcome import MAX_TURNS_STOP_CODE, LoopResult, LoopStop, RunStatus
from .tool_execution import tool_call_blocks
from .types import AgentContext, AssistantMessage, StopReason, ToolResultMessage, usage_to_json

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
    base = replace(
        state,
        messages=committed,
        new_messages=(*state.new_messages, message),
        last_assistant=message,
        last_tool_results=(),
        # A new turn resets the previous batch's continuation flags, exactly as the
        # legacy loop does at the top of every iteration.
        has_more_tool_calls=False,
        batch_terminated=False,
        batch=(),
    )
    events: list[Any] = [MessageEnd(message=message)]

    if message.stop_reason is StopReason.DEFERRED:
        # The model itself said this turn is not finished: a paused run, not an answer.
        events.extend((TurnEnd(message=message, tool_results=()), AgentEnd()))
        return Transition(
            state=with_phase(
                base,
                Phase.SUSPENDED,
                interaction=deferred_interaction(message),
                status=RunStatus.SUSPENDED,
                stop=LoopStop.SUSPENDED,
            ),
            events=tuple(events),
        )
    if message.stop_reason in (StopReason.ERROR, StopReason.ABORTED):
        aborted = message.stop_reason is StopReason.ABORTED
        events.extend((TurnEnd(message=message, tool_results=()), AgentEnd()))
        return Transition(
            state=with_phase(
                base,
                Phase.FINISHED,
                status=RunStatus.CANCELLED if aborted else RunStatus.FAILED,
                stop=LoopStop.ABORTED if aborted else LoopStop.MODEL_ERROR,
                error_code="cancelled" if aborted else (message.raw_stop_reason or "model_error"),
                detail=message.error_message or (
                    "the run was cancelled" if aborted else "the model request failed"
                ),
            ),
            events=tuple(events),
        )

    calls = tool_call_blocks(message)
    if calls:
        truncated = message.stop_reason is StopReason.LENGTH
        return Transition(
            state=with_phase(base, Phase.RUNNING_BATCH),
            events=tuple(events),
            actions=(RunToolBatch(calls=tuple(calls), truncated=truncated),),
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
    results = tuple(event.results)
    batch = classify_results(tool_call_blocks(assistant), results)
    base = replace(
        state,
        messages=(*state.messages, *results),
        new_messages=(*state.new_messages, *results),
        last_tool_results=results,
        batch=batch,
        has_more_tool_calls=not event.terminate,
        batch_terminated=bool(results) and event.terminate,
    )
    events: list[Any] = []
    for result in results:
        events.extend((MessageStart(message=result), MessageEnd(message=result)))
    events.append(TurnEnd(message=assistant, tool_results=results))

    interaction = dict(event.suspend) if event.suspend else interaction_for_batch(batch)
    if interaction:
        events.append(AgentEnd())
        return Transition(
            state=with_phase(
                base,
                Phase.SUSPENDED,
                interaction=interaction,
                status=RunStatus.SUSPENDED,
                stop=LoopStop.SUSPENDED,
            ),
            events=tuple(events),
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
        return Transition(
            state=with_phase(
                state, Phase.FINISHED, status=RunStatus.COMPLETED, stop=LoopStop.STOP_HOOK
            ),
            events=(AgentEnd(),),
        )
    if state.max_turns is not None and state.turns_used >= state.max_turns:
        return Transition(
            state=with_phase(
                state,
                Phase.FINISHED,
                status=RunStatus.USAGE_LIMITED,
                stop=LoopStop.MAX_TURNS,
                error_code=MAX_TURNS_STOP_CODE,
                detail=f"the turn budget of {state.max_turns} was reached",
            ),
            events=(AgentEnd(),),
        )
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


_HANDLERS: Mapping[type, Callable[[ExecutionState, Any], Transition]] = {
    PromptSubmitted: _prompt_submitted,
    ContinueRequested: _continue_requested,
    InputsPolled: _inputs_polled,
    TurnPrepared: _turn_prepared,
    ModelCompleted: _model_completed,
    ToolBatchSettled: _tool_batch_settled,
    StopHookEvaluated: _stop_hook_evaluated,
    CancellationRequested: _cancellation_requested,
    ContextProjected: _context_projected,
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
    """``B2``/``B1d``: declare the tool delta, then ask the model for one turn."""
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
    )
    return Transition(
        state=next_state,
        events=tuple(events),
        actions=(
            RequestModel(
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
        return Transition(
            state=with_phase(
                state,
                Phase.FINISHED,
                status=RunStatus.CANCELLED,
                stop=LoopStop.ABORTED,
                error_code="cancelled",
                detail="the run was cancelled between turns",
            ),
            events=(AgentEnd(),),
        )
    return Transition(
        state=with_phase(
            state,
            Phase.FINISHED,
            status=RunStatus.COMPLETED,
            stop=LoopStop.BATCH_TERMINATE if state.batch_terminated else LoopStop.ASSISTANT_FINAL,
        ),
        events=(AgentEnd(),),
    )


def _require_phase(state: ExecutionState, event: InputEvent, phase: Phase) -> None:
    if state.phase is not phase:
        raise ContractError(
            f"{event.kind} {event.event_id} cannot be applied in phase {state.phase.value}; "
            f"it requires {phase.value}"
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
