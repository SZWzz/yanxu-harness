"""Explicit execution state, input events, and the actions a driver must perform.

This module is the S2 half of
``docs/core-state-machine-implementation-plan.zh-CN.md``: the vocabulary of the
explicit state machine, with **no behaviour**. :mod:`yanxu_harness.core.transitions`
holds ``advance(state, event) -> Transition``; a driver (S3) executes the actions and
feeds the resulting events back.

Why the split exists: the two-layer loop keeps its position in local variables
(`turns`, `pending`, `last_completed`, `has_more_tool_calls`, a mutable context list)
and calls hooks, the model and tools inline. That is fine to run and impossible to
checkpoint, replay or test in isolation. Here, everything the loop decides becomes a
pure function of (state, event):

* an :class:`InputEvent` is a fact that already happened — a prompt was accepted, a
  queue was read, the model finished a turn, a batch settled, a host hook answered,
  cancellation arrived. Events carry their own ``at_ms``; the reducer never reads a
  clock, never creates an id and never calls out.
* an :class:`Action` is an intent the driver must carry out: read a queue, run a
  preparation hook, ask the model, run a tool batch. Actions are plain data — no
  coroutine, callable, connection, tool instance or secret — so they can be recorded
  and replayed, and the driver resolves the actual port from its own registry.
* a :class:`Transition` is the new state plus the ordered control events to dispatch
  and the actions to perform.

Three vocabularies stay separate, and this module adds nothing to them:

* :class:`~yanxu_harness.core.outcome.RunStatus` / ``LoopStop`` — how the run ended,
* :class:`~yanxu_harness.core.outcome.CallOutcome` — what happened to one tool call,
* :class:`Phase` (here) — where the execution currently is. A phase is progress, not
  a verdict, and it is never reported to a host as an outcome.

Copy conventions, because both directions are easy to get wrong:

* the state holds **tuples**; a transition copies the sequence (``tuple(event.messages)``)
  and never mutates the list it was handed, so a caller that keeps mutating its own
  list cannot silently change committed state;
* the state holds message objects **by reference** and does not deep-copy them. A
  driver must therefore not edit a message in place once it is in the state: build a
  new object instead (the one exception the current loop relies on is the streaming
  partial, which the driver owns until it reports ``ModelCompleted``).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Mapping, Sequence

from ..errors import ContractError
from .outcome import CallOutcome, LoopStop, RunStatus
from .types import AssistantMessage, ToolCall, ToolDeclaration, ToolResultMessage

__all__ = [
    "EXECUTION_STATE_SCHEMA_VERSION",
    "QUIESCENT_PHASES",
    "Action",
    "BatchCallState",
    "CancellationRequested",
    "ContextProjected",
    "ContinueRequested",
    "EvaluateStopHook",
    "ExecutionState",
    "InputEvent",
    "InputKind",
    "InputsPolled",
    "ModelCompleted",
    "Phase",
    "PollSite",
    "PrepareTurn",
    "PromptSubmitted",
    "ReadInputs",
    "RequestModel",
    "RunToolBatch",
    "StopHookEvaluated",
    "ToolBatchSettled",
    "Transition",
    "TurnPrepared",
    "WaitForExternal",
    "classify_results",
    "deferred_interaction",
    "define_state",
    "interaction_for_batch",
    "pending_work_of",
    "state_from_record",
    "state_to_record",
    "with_phase",
]

#: Bumped when the persisted shape of :class:`ExecutionState` changes in a way a
#: released reader cannot prove safe. The reducer itself is versionless; a checkpoint
#: that carries a state must declare this.
EXECUTION_STATE_SCHEMA_VERSION = 1


class Phase(str, Enum):
    """Where one execution currently is. Progress, never a run verdict."""

    #: Nothing accepted yet.
    CREATED = "created"
    #: Inputs are being collected (a queue read is outstanding).
    COLLECTING = "collecting"
    #: A preparation hook is outstanding before the next request.
    AWAITING_PREPARE = "awaiting_prepare"
    #: A model request is outstanding.
    REQUESTING_MODEL = "requesting_model"
    #: The declared tool batch is outstanding.
    RUNNING_BATCH = "running_batch"
    #: A tool batch has settled; the stop hook is outstanding.
    AWAITING_STOP_HOOK = "awaiting_stop_hook"
    #: Waiting for something outside this execution. Not terminal.
    SUSPENDED = "suspended"
    #: No further progress without a new invocation.
    FINISHED = "finished"


#: Phases from which a late event may still be a legitimate duplicate or a stale
#: callback: the reducer ignores it with a reason instead of raising.
QUIESCENT_PHASES = frozenset({Phase.SUSPENDED, Phase.FINISHED})


class PollSite(str, Enum):
    """Why a queue read is being performed — the same queue means different things."""

    #: Before the very first request of the invocation (legacy loop ``B0``).
    INITIAL = "initial"
    #: After a turn settled: steering decides whether another request happens at all.
    TURN_BOUNDARY = "turn_boundary"
    #: Only reached when a preparation hook produced no messages (legacy ``B1c``).
    AFTER_PREPARE = "after_prepare"
    #: After the inner work ran out: follow-ups are what would restart it (legacy ``B10``).
    AFTER_INNER = "after_inner"


class InputKind(str, Enum):
    STEERING = "steering"
    FOLLOW_UP = "follow_up"


# --------------------------------------------------------------------------------------
# Input events: facts that already happened, carrying their own clock.
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class InputEvent:
    """Base class for everything the reducer accepts.

    ``event_id`` is what makes a repeated delivery idempotent: content that arrives
    twice under the same id is ignored with a reason, and different content under the
    same id is refused. ``at_ms`` is the only clock the reducer is allowed to see.
    """

    event_id: str = ""
    at_ms: int = 0

    @property
    def kind(self) -> str:
        return type(self).__name__


@dataclass(frozen=True)
class PromptSubmitted(InputEvent):
    """A new prompt was accepted (legacy entry ``E1``)."""

    messages: tuple[Any, ...] = ()
    tools: tuple[ToolDeclaration, ...] = ()
    max_turns: int | None = None


@dataclass(frozen=True)
class ContinueRequested(InputEvent):
    """Continue from an existing transcript (legacy entry ``E2``)."""

    tools: tuple[ToolDeclaration, ...] = ()
    max_turns: int | None = None


@dataclass(frozen=True)
class InputsPolled(InputEvent):
    """A queue read finished. ``messages`` empty means "nothing was waiting"."""

    site: PollSite = PollSite.INITIAL
    kind: InputKind = InputKind.STEERING
    messages: tuple[Any, ...] = ()


@dataclass(frozen=True)
class TurnPrepared(InputEvent):
    """``prepare_next_turn`` finished; it may replace context, model or thinking level."""

    messages: tuple[Any, ...] = ()
    context_messages: tuple[Any, ...] | None = None
    #: The replacement context's executable tool set, when the hook swapped the context.
    #: The declaration delta is computed against it, exactly as the legacy loop did with
    #: ``snapshot["context"].tools``.
    context_tools: tuple[ToolDeclaration, ...] | None = None
    model: Any = None
    thinking_level: str | None = None


@dataclass(frozen=True)
class ModelCompleted(InputEvent):
    """The model stream produced its final assistant message."""

    message: AssistantMessage | None = None


@dataclass(frozen=True)
class ToolBatchSettled(InputEvent):
    """The declared batch of the current assistant message finished.

    ``suspend`` is the host hook's answer (``should_suspend_after_batch``), already
    evaluated by the driver. The batch's own classification decides suspension too;
    the host answer wins when it is truthy, exactly as the legacy loop does.
    """

    results: tuple[ToolResultMessage, ...] = ()
    suspend: Mapping[str, Any] | None = None
    #: Whether every finalized result of the batch asked to terminate. The result
    #: messages do not carry the raw tool payload, so the driver reports it.
    terminate: bool = False


@dataclass(frozen=True)
class StopHookEvaluated(InputEvent):
    """``should_stop_after_turn`` finished."""

    stop: bool = False


@dataclass(frozen=True)
class CancellationRequested(InputEvent):
    """The run's token was cancelled. Recorded; never rewinds a settled effect."""

    reason: str = ""


@dataclass(frozen=True)
class ContextProjected(InputEvent):
    """A persistent context transform/projection replaced the active conversation."""

    messages: tuple[Any, ...] = ()


# --------------------------------------------------------------------------------------
# Actions: plain-data intents for the driver.
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Action:
    """Base class for driver intents."""

    @property
    def kind(self) -> str:
        return type(self).__name__


@dataclass(frozen=True)
class ReadInputs(Action):
    site: PollSite = PollSite.INITIAL
    kind: InputKind = InputKind.STEERING


@dataclass(frozen=True)
class PrepareTurn(Action):
    """The driver runs ``prepare_next_turn`` with these recorded arguments."""

    message: AssistantMessage | None = None
    tool_results: tuple[ToolResultMessage, ...] = ()
    turn: int = 0


@dataclass(frozen=True)
class EvaluateStopHook(Action):
    """The driver runs ``should_stop_after_turn`` with these recorded arguments."""

    message: AssistantMessage | None = None
    tool_results: tuple[ToolResultMessage, ...] = ()
    turn: int = 0


@dataclass(frozen=True)
class RequestModel(Action):
    """The driver asks the provider for one turn.

    ``messages`` is the raw conversation *after* the tool-set delta was applied; the
    transform/convert/projection steps belong to the driver, which feeds
    :class:`ContextProjected` back when a projection is persistent.
    """

    turn: int = 0
    messages: tuple[Any, ...] = ()
    model: Any = None
    thinking_level: str | None = None


@dataclass(frozen=True)
class RunToolBatch(Action):
    """The driver executes the declared calls.

    ``truncated`` means the response hit the output limit: every call must be settled
    with an error result and none may run. A run that was already cancelled is still
    dispatched to this action — the executor decides per call, and it short-circuits
    every call it has not started yet, so no handler runs after a cancellation while the
    preflight hooks and the result framing stay identical to the previous loop. The
    parallel/sequential decision belongs to the driver too, because it depends on the
    tool registry, not on execution state.
    """

    calls: tuple[ToolCall, ...] = ()
    truncated: bool = False


@dataclass(frozen=True)
class WaitForExternal(Action):
    """The execution stopped before its next request and is waiting for a decision."""

    interaction: Mapping[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class BatchCallState:
    """One declared call of the current batch, in declared order."""

    call_id: str
    tool_name: str
    outcome: CallOutcome = CallOutcome.UNSTARTED
    status: str = ""
    #: The model-visible result record when the call is settled. Kept as a plain
    #: mapping (``message_to_json`` output) so the state stays serializable.
    result: Mapping[str, Any] | None = None
    operation_id: str = ""
    approval_binding_key: str | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.call_id or not self.tool_name:
            raise ContractError("a batch call state needs a call id and a tool name")
        if not isinstance(self.outcome, CallOutcome):
            raise ContractError(f"outcome must be a CallOutcome, got {self.outcome!r}")
        if self.outcome is CallOutcome.SETTLED and self.result is None:
            raise ContractError(f"{self.call_id}: a settled call must carry its result")

    def to_record(self) -> Mapping[str, Any]:
        return {
            "call_id": self.call_id,
            "tool_name": self.tool_name,
            "outcome": self.outcome,
            "status": self.status,
            "result": None if self.result is None else dict(self.result),
            "operation_id": self.operation_id,
            "approval_binding_key": self.approval_binding_key,
            "reason": self.reason,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "BatchCallState":
        accepted = {
            name: value for name, value in record.items() if name in cls.__dataclass_fields__
        }
        if "outcome" in accepted and not isinstance(accepted["outcome"], CallOutcome):
            accepted["outcome"] = CallOutcome(str(accepted["outcome"]))
        return cls(**accepted)

    @property
    def blocking(self) -> bool:
        return self.outcome in {CallOutcome.AWAITING, CallOutcome.UNCERTAIN}


def _empty() -> tuple[()]:
    return ()


@dataclass(frozen=True)
class ExecutionState:
    """Everything the reducer needs, and nothing it cannot prove.

    Deliberately absent: the wall clock, credentials, connections, tool callables, and
    any "history of states". ``messages`` is the committed conversation; ``new_messages``
    is the delta this invocation produced (what ``AgentEnd`` and ``LoopResult`` carry).
    """

    schema_version: int = EXECUTION_STATE_SCHEMA_VERSION
    phase: Phase = Phase.CREATED
    #: Committed conversation, in order.
    messages: tuple[Any, ...] = ()
    #: Messages produced by this invocation only.
    new_messages: tuple[Any, ...] = ()
    #: Messages that enter the next request but are not committed yet.
    pending: tuple[Any, ...] = ()
    #: Messages a preparation hook produced for the next request (legacy ``prepared``).
    prepared: tuple[Any, ...] = ()
    #: Executable tool set, as declarations.
    tools: tuple[ToolDeclaration, ...] = ()
    turns_used: int = 0
    max_turns: int | None = None
    model: Any = None
    thinking_level: str | None = None
    last_assistant: AssistantMessage | None = None
    last_tool_results: tuple[ToolResultMessage, ...] = ()
    #: Identifies the model request this state is built around (``request:<turn>``). At
    #: most one request is outstanding at a time — the transition's single action says so
    #: — and this id is what ties a later response or batch back to the request that
    #: produced it, including across a resume.
    model_action_id: str = ""
    batch: tuple[BatchCallState, ...] = ()
    has_more_tool_calls: bool = False
    batch_terminated: bool = False
    #: The site of the outstanding queue read, when ``phase`` is COLLECTING.
    poll_site: PollSite | None = None
    #: True once a turn settled and the next request needs ``prepare_next_turn``.
    prepared_for_turn: bool = False
    interaction: Mapping[str, Any] | None = None
    cancel_requested: bool = False
    cancel_reason: str = ""
    stop: LoopStop | None = None
    status: RunStatus | None = None
    error_code: str | None = None
    detail: str = ""
    #: event ids accepted so far, with a digest of their content for conflict detection.
    consumed: tuple[tuple[str, str], ...] = _empty()

    def __post_init__(self) -> None:
        if self.phase is Phase.FINISHED and self.status is None:
            raise ContractError("a finished execution must carry the status it ended with")
        if self.phase is Phase.SUSPENDED and not self.interaction:
            raise ContractError("a suspended execution must carry the interaction it waits on")
        if self.turns_used < 0:
            raise ContractError("turns_used must not be negative")
        # Freeze the sequences a caller might still be holding.
        object.__setattr__(self, "messages", tuple(self.messages))
        object.__setattr__(self, "new_messages", tuple(self.new_messages))
        object.__setattr__(self, "pending", tuple(self.pending))
        object.__setattr__(self, "prepared", tuple(self.prepared))
        object.__setattr__(self, "tools", tuple(self.tools))
        object.__setattr__(self, "batch", tuple(self.batch))

    # -- queries --------------------------------------------------------

    @property
    def finished(self) -> bool:
        return self.phase is Phase.FINISHED

    @property
    def suspended(self) -> bool:
        return self.phase is Phase.SUSPENDED

    @property
    def blocked_calls(self) -> tuple[BatchCallState, ...]:
        return tuple(call for call in self.batch if call.blocking)

    @property
    def model_requests_left(self) -> int | None:
        if self.max_turns is None:
            return None
        return max(0, self.max_turns - self.turns_used)


@dataclass(frozen=True)
class Transition:
    """The result of one ``advance``: a new state, ordered events, queued actions.

    ``events`` are the control events to dispatch, in order. Streaming progress
    (``MessageUpdate`` deltas, tool partial updates) is not here: it belongs to the
    driver's progress path, which is exactly what keeps a slow listener from delaying
    a terminal event.
    """

    state: ExecutionState
    events: tuple[Any, ...] = ()
    actions: tuple[Action, ...] = ()
    #: Set when the event was a duplicate or arrived after the execution stopped. No
    #: state change happened; the reason is kept so a driver can log it instead of
    #: pretending it applied something.
    ignored_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "events", tuple(self.events))
        object.__setattr__(self, "actions", tuple(self.actions))

    @property
    def ignored(self) -> bool:
        return self.ignored_reason is not None


def define_state(**overrides: Any) -> ExecutionState:
    """Build a state, refusing keys the state does not have (typos are bugs)."""
    unknown = set(overrides) - set(ExecutionState.__dataclass_fields__)
    if unknown:
        raise ContractError(f"unknown ExecutionState field(s): {sorted(unknown)}")
    return ExecutionState(**overrides)


def deferred_interaction(message: AssistantMessage) -> Mapping[str, Any]:
    """The interaction a ``stop_reason=deferred`` turn reports.

    The handle is optional (a provider may pause without one); the pause itself is the
    fact that must survive, which is why the kind is always present.
    """
    interaction: dict[str, Any] = {"kind": "deferred"}
    handle = message.deferred
    if handle is not None:
        interaction["handle_id"] = handle.id
        if handle.payload:
            interaction["payload"] = dict(handle.payload)
    return interaction


def interaction_for_batch(batch: Sequence[BatchCallState]) -> Mapping[str, Any] | None:
    """The primary interaction of a batch, or ``None`` when nothing blocks.

    Approval wins over reconciliation because a human decision is the cheaper question
    and the one the host must answer first; the rest stay recorded in the batch.
    """
    blocking = [call for call in batch if call.blocking]
    if not blocking:
        return None
    approval = next((call for call in blocking if call.outcome is CallOutcome.AWAITING), None)
    if approval is not None:
        interaction: dict[str, Any] = {
            "kind": "approval",
            "call_id": approval.call_id,
            "tool_name": approval.tool_name,
            "operation_id": approval.operation_id,
        }
        if approval.approval_binding_key:
            interaction["approval_key"] = approval.approval_binding_key
        if approval.reason:
            interaction["reason"] = approval.reason
        return interaction
    uncertain = blocking[0]
    return {
        "kind": "reconciliation",
        "call_id": uncertain.call_id,
        "tool_name": uncertain.tool_name,
        "operation_id": uncertain.operation_id,
        "reason": uncertain.reason or "the outcome could not be proven",
    }


def classify_results(
    calls: Sequence[ToolCall], results: Sequence[ToolResultMessage], *, derived_operation_prefix: str = ""
) -> tuple[BatchCallState, ...]:
    """Partition a settled batch against the calls the assistant declared.

    A call with no result was never reached (``UNSTARTED``). A result without a
    machine-readable status is a settled result: the loop hands it to the model, and
    only a status the gateway wrote can mean "waiting" or "unproven". A status this
    build does not know is refused — guessing whether an effect happened is the defect
    the classification exists to prevent.
    """
    by_id = {result.tool_call_id: result for result in results}
    states: list[BatchCallState] = []
    for call in calls:
        result = by_id.get(call.id)
        if result is None:
            states.append(BatchCallState(call_id=call.id, tool_name=call.name))
            continue
        details = result.details if isinstance(result.details, Mapping) else {}
        raw_status = details.get("status")
        outcome = CallOutcome.SETTLED if raw_status is None else _classify(str(raw_status))
        operation_id = str(details.get("operation_id") or (f"{derived_operation_prefix}{call.id}" if derived_operation_prefix else ""))
        approval = details.get("approval") if isinstance(details.get("approval"), Mapping) else {}
        binding = approval.get("binding_key") if approval else None
        states.append(
            BatchCallState(
                call_id=call.id,
                tool_name=call.name,
                outcome=outcome,
                status=str(raw_status or ""),
                result=_result_record(result) if outcome is CallOutcome.SETTLED else None,
                operation_id=operation_id,
                approval_binding_key=str(binding) if binding else None,
                reason=str(details.get("reason") or ""),
            )
        )
    return tuple(states)


def _classify(status: str) -> CallOutcome:
    from .outcome import classify_status

    return classify_status(status)


def _result_record(result: ToolResultMessage) -> Mapping[str, Any]:
    from .types import message_to_json

    return message_to_json(result)


def with_phase(state: ExecutionState, phase: Phase, **overrides: Any) -> ExecutionState:
    """A copy of ``state`` in ``phase`` with extra fields replaced."""
    return replace(state, phase=phase, **overrides)


# --------------------------------------------------------------------------------------
# Durable boundary: the state a host commits, and the work it implies
# --------------------------------------------------------------------------------------


def pending_work_of(state: ExecutionState) -> Mapping[str, Any]:
    """What is still outstanding, in a shape a host can store and act on.

    This is the answer to "may I dispatch anything, and if not, what am I waiting
    for?" — computed from the state alone, so a resumed process does not re-derive it
    from the transcript:

    * ``blocking`` — calls that must not be dispatched again (``awaiting`` waits for a
      decision, ``uncertain`` for evidence);
    * ``replayable`` — calls that provably never ran, in declared order;
    * ``interaction`` — the question to answer first, if any;
    * ``declared`` — calls the model asked for while the batch's own outcome is not
      recorded: a crash inside a dispatch window. ``batch_outcome_unknown`` says so,
      and the operation ledger decides what actually happened — a call in this list may
      have run, so it must not simply be dispatched again;
    * ``model_request_outstanding`` — the run died inside a model request, which is
      **not** a tool side effect: no external effect can have been produced by an
      unfinished request, so the next attempt may ask again.
    """
    declared: list[Mapping[str, Any]] = []
    if state.phase is Phase.RUNNING_BATCH and state.last_assistant is not None:
        declared = [
            {"call_id": block.id, "tool_name": block.name}
            for block in getattr(state.last_assistant, "content", ())
            if getattr(block, "type", None) == "toolCall"
        ]
    return {
        "blocking": [
            {
                "call_id": call.call_id,
                "tool_name": call.tool_name,
                "outcome": call.outcome.value,
                "operation_id": call.operation_id,
                "reason": call.reason,
            }
            for call in state.batch
            if call.blocking
        ],
        "replayable": [
            {"call_id": call.call_id, "tool_name": call.tool_name}
            for call in state.batch
            if call.outcome is CallOutcome.UNSTARTED
        ],
        "declared": declared,
        "batch_outcome_unknown": state.phase is Phase.RUNNING_BATCH,
        "interaction": None if state.interaction is None else dict(state.interaction),
        "model_request_outstanding": state.phase is Phase.REQUESTING_MODEL,
        "suspended": state.phase is Phase.SUSPENDED,
    }


def state_to_record(state: ExecutionState) -> Mapping[str, Any]:
    """Serialize an execution state for a commit.

    This is **private recovery state**, not a public event: it carries the provider
    continuation material a message holds (thinking/text signatures), because a
    resumed run needs it. A host stores it where it stores the transcript and must not
    publish it as an event payload — the public event encoder already refuses provider
    frames, and this codec existing does not change that.
    """
    from .types import declaration_to_json, message_to_json

    return {
        "schema_version": state.schema_version,
        "phase": state.phase.value,
        "messages": [message_to_json(message) for message in state.messages],
        "new_messages": [message_to_json(message) for message in state.new_messages],
        "pending": [message_to_json(message) for message in state.pending],
        "prepared": [message_to_json(message) for message in state.prepared],
        "tools": [declaration_to_json(tool) for tool in state.tools],
        "turns_used": state.turns_used,
        "max_turns": state.max_turns,
        "model": _model_to_record(state.model),
        "thinking_level": state.thinking_level,
        "last_assistant": (
            None if state.last_assistant is None else message_to_json(state.last_assistant)
        ),
        "model_action_id": state.model_action_id,
        "last_tool_results": [message_to_json(result) for result in state.last_tool_results],
        "batch": [call.to_record() for call in state.batch],
        "has_more_tool_calls": state.has_more_tool_calls,
        "batch_terminated": state.batch_terminated,
        "poll_site": None if state.poll_site is None else state.poll_site.value,
        "interaction": None if state.interaction is None else dict(state.interaction),
        "cancel_requested": state.cancel_requested,
        "cancel_reason": state.cancel_reason,
        "stop": None if state.stop is None else state.stop.value,
        "status": None if state.status is None else state.status.value,
        "error_code": state.error_code,
        "detail": state.detail,
        "consumed": [
            {"event_id": event_id, "digest": digest} for event_id, digest in state.consumed
        ],
        "pending_work": dict(pending_work_of(state)),
    }


def state_from_record(record: Mapping[str, Any]) -> ExecutionState:
    """Rebuild a state, refusing anything this build cannot prove it understands.

    Three refusals, each of which has been a real defect somewhere: a missing or
    unknown ``schema_version`` (guessing a layout), an unknown phase or status
    (guessing what a run is doing), and an unknown field in a batch call (guessing
    whether an effect happened).
    """
    from ..errors import VersionRefused
    from .types import declaration_from_json, message_from_json

    if not isinstance(record, Mapping):
        raise ContractError(
            f"an execution state record must be a mapping, got {type(record).__name__}"
        )
    if "schema_version" not in record:
        raise ContractError(
            "an execution state without a schema_version cannot be interpreted; refusing "
            "instead of guessing the layout"
        )
    version = record["schema_version"]
    if version != EXECUTION_STATE_SCHEMA_VERSION:
        raise VersionRefused("execution state", version, (EXECUTION_STATE_SCHEMA_VERSION,))
    try:
        phase = Phase(str(record["phase"]))
    except (KeyError, ValueError) as exc:
        raise ContractError(
            f"the execution state names a phase this build does not know: {record.get('phase')!r}"
        ) from exc
    try:
        resolved_status = (
            None if record.get("status") is None else RunStatus(str(record["status"]))
        )
        resolved_stop = None if record.get("stop") is None else LoopStop(str(record["stop"]))
        resolved_site = (
            None if record.get("poll_site") is None else PollSite(str(record["poll_site"]))
        )
    except ValueError as exc:
        raise ContractError(
            f"the execution state names a status or site this build does not know: {exc}"
        ) from exc
    return ExecutionState(
        schema_version=version,
        phase=phase,
        messages=tuple(message_from_json(item) for item in record.get("messages") or ()),
        new_messages=tuple(message_from_json(item) for item in record.get("new_messages") or ()),
        pending=tuple(message_from_json(item) for item in record.get("pending") or ()),
        prepared=tuple(message_from_json(item) for item in record.get("prepared") or ()),
        tools=tuple(declaration_from_json(item) for item in record.get("tools") or ()),
        turns_used=int(record.get("turns_used") or 0),
        max_turns=record.get("max_turns"),
        model=_model_from_record(record.get("model")),
        thinking_level=record.get("thinking_level"),
        last_assistant=(
            None
            if record.get("last_assistant") is None
            else message_from_json(record["last_assistant"])
        ),
        model_action_id=str(record.get("model_action_id") or ""),
        last_tool_results=tuple(
            message_from_json(item) for item in record.get("last_tool_results") or ()
        ),
        batch=tuple(BatchCallState.from_record(call) for call in record.get("batch") or ()),
        has_more_tool_calls=bool(record.get("has_more_tool_calls")),
        batch_terminated=bool(record.get("batch_terminated")),
        poll_site=resolved_site,
        interaction=record.get("interaction"),
        cancel_requested=bool(record.get("cancel_requested")),
        cancel_reason=str(record.get("cancel_reason") or ""),
        stop=resolved_stop,
        status=resolved_status,
        error_code=record.get("error_code"),
        detail=str(record.get("detail") or ""),
        consumed=tuple(
            (str(item["event_id"]), str(item["digest"])) for item in record.get("consumed") or ()
        ),
    )


def _model_to_record(model: Any) -> Mapping[str, Any] | None:
    if model is None:
        return None
    if isinstance(model, Mapping):
        return dict(model)
    names = (
        "id",
        "provider",
        "api",
        "base_url",
        "context_window",
        "max_output_tokens",
        "reasoning",
    )
    if not all(hasattr(model, name) for name in names[:3]):
        raise ContractError(
            "an execution state can only persist a model handle that names itself "
            f"(id/provider/api); got {type(model).__name__}"
        )
    return {name: getattr(model, name, None) for name in names}


def _model_from_record(record: Any) -> Any:
    if record is None:
        return None
    if not isinstance(record, Mapping):
        raise ContractError(
            f"a persisted model handle must be a mapping, got {type(record).__name__}"
        )
    from .types import Model

    accepted = {name: value for name, value in record.items() if name in Model.__dataclass_fields__}
    if "id" not in accepted:
        raise ContractError("a persisted model handle must carry an id")
    return Model(**accepted)
