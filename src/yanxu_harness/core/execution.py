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
    "arrangement_from_record",
    "arrangement_to_record",
    "EXECUTION_STATE_SCHEMA_VERSION",
    "SUPPORTED_EXECUTION_STATE_SCHEMA_VERSIONS",
    "CONTEXT_STATE_SCHEMA_VERSION",
    "QUIESCENT_PHASES",
    "Action",
    "AwaitToolObservation",
    "BatchCallState",
    "BatchResumed",
    "CallProgress",
    "CancellationRequested",
    "ContextProjected",
    "ContinueRequested",
    "EvaluateStopHook",
    "ExecutionState",
    "FinishToolBatch",
    "InputEvent",
    "InputKind",
    "InputsPolled",
    "ModelCompleted",
    "Phase",
    "PollSite",
    "PrepareRequest",
    "PrepareTurn",
    "PromptSubmitted",
    "ReadInputs",
    "RequestPrepared",
    "RunToolBatch",
    "SendModelRequest",
    "StopHookEvaluated",
    "ToolBatchDrained",
    "ToolBatchSettled",
    "ToolCallObserved",
    "Transition",
    "TurnPrepared",
    "WaitForExternal",
    "apply_observation",
    "classify_results",
    "context_state_from_record",
    "declared_batch",
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
#:
#: Version 2 adds the per-call settlement fields to :class:`BatchCallState` (declared
#: index, model-action reference, schedule progress, ledger reference and observation
#: digest). A version-1 record has none of them, and is read conservatively: an
#: unstarted call keeps its outcome, and only the operation ledger may say whether it
#: was ever dispatched.
#:
#: Version 3 adds :attr:`ExecutionState.arrangement`: the deployment the declared batch
#: was bound to, frozen *before* the first dispatch. Recovery compares that recorded
#: baseline with the deployment it is running in; a version-1/2 record has no baseline,
#: and a recovery that cannot prove the deployment is unchanged must not dispatch.
EXECUTION_STATE_SCHEMA_VERSION = 3

#: Versions this build can still read. A version in this set is interpreted; anything
#: else is refused rather than guessed at.
SUPPORTED_EXECUTION_STATE_SCHEMA_VERSIONS = (1, 2, 3)

#: Bumped when the *shape of the projection facts* in ``ExecutionState.context_state``
#: changes. It is versioned separately from the execution state for the same reason the
#: record and the snapshot are: a reader that can read a run must not be forced to guess
#: what a projection means.
CONTEXT_STATE_SCHEMA_VERSION = 1


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
class ToolCallObserved(InputEvent):
    """One call of the outstanding batch was observed, and the observation was accepted.

    This is the incremental half of a batch settlement. The driver performs the batch as
    a bounded session, delivers one observation at a time, and the host's commit port
    confirms each one before the next is awaited — so a call that finished while its
    siblings are still running is already a durable fact, not something the transcript
    only learns once the whole batch is over.

    An observation is **not** a success. ``awaiting``/``unknown``/``denied``/``failed``
    travel through the same input, which is why the name says "observed" and not
    "completed": the only thing the reducer is told is that the driver now knows
    something about this call that it must record.

    ``observed_digest`` is what makes a repeated delivery idempotent and a conflicting
    one a refusal: the same call may be reported twice with the same content (ignored),
    never twice with different content (that is a contract violation, not a merge).
    """

    model_action_id: str = ""
    call_id: str = ""
    tool_name: str = ""
    outcome: str = CallOutcome.UNSTARTED.value
    status: str = ""
    effect_state: str | None = None
    result: Mapping[str, Any] | None = None
    operation_id: str = ""
    tool_version: str = ""
    receipt: Mapping[str, Any] | None = None
    observation_id: str = ""
    observed_digest: str = ""
    source_attempt: str = ""
    terminate: bool = False
    reason: str = ""
    #: The approval binding a waiting call waits on, so a resume can re-check the
    #: decision against the trusted store instead of the resolution payload.
    approval_binding_key: str | None = None
    #: Set when the trailing part of the batch was settled without running because the
    #: run was cancelled. The result is then an explicit "not executed" answer.
    aborted: bool = False


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
class ToolBatchDrained(InputEvent):
    """The batch session has no further observations to deliver.

    ``complete`` is true when every declared call was observed, and false when the
    session stopped early (a sequential batch that was cancelled between calls). The
    difference is recorded rather than inferred: an unobserved call is *not* a call
    that was reported as not having run, and only the ledger may decide what happened
    to it on recovery.
    """

    model_action_id: str = ""
    complete: bool = True
    reason: str = ""


@dataclass(frozen=True)
class BatchResumed(InputEvent):
    """A committed, partially settled batch is being continued.

    The state this is applied to already carries the declaration partition and the
    observations that were committed before the interruption. This event asks the driver
    to continue the calls that are still unobserved — not to rebuild the batch and not to
    re-ask the model. It is applied by the *same* Runner that drove the original batch.
    """

    model_action_id: str = ""
    reason: str = ""


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


@dataclass(frozen=True)
class RequestPrepared(InputEvent):
    """The request view the next model request must send.

    This is the explicit preparation result the plan asks for: the driver prepares (a
    context controller measures and projects, or nothing is configured and the transcript
    is the view), the reducer records it, the host's commit port confirms it, and *then*
    the driver may dispatch. ``view`` is a derived request view — it never replaces
    :attr:`ExecutionState.messages`, which stays the authoritative conversation.

    ``refused`` is a preparation that decided the request must not be made (the minimum
    required context does not fit). It is a *result*, not an exception: a budget problem
    is not a model failure and must not be recorded as one.
    """

    view: tuple[Any, ...] = ()
    context_state: Mapping[str, Any] | None = None
    refused: Mapping[str, Any] | None = None


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
class PrepareRequest(Action):
    """The driver runs context preparation for one turn. No network, no tool call.

    Kept separate from :class:`SendModelRequest` on purpose: the prepared view (and the
    projection facts that describe it) must be committed *before* anything is sent, and a
    single action cannot both prepare and dispatch without committing in between.
    """

    turn: int = 0
    messages: tuple[Any, ...] = ()
    model: Any = None
    thinking_level: str | None = None


@dataclass(frozen=True)
class SendModelRequest(Action):
    """The driver sends an already prepared request view.

    ``view`` is the prepared request view; the transcript itself is never sent directly
    when a preparation step ran.
    """

    turn: int = 0
    view: tuple[Any, ...] = ()
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

    ``model_action_id`` names the model action the batch belongs to. It is the batch
    reference: the driver keys its session by it, and every observation must name the
    same one, so a stale callback from an earlier turn cannot be applied here.
    """

    calls: tuple[ToolCall, ...] = ()
    truncated: bool = False
    model_action_id: str = ""


@dataclass(frozen=True)
class AwaitToolObservation(Action):
    """The driver waits for the next observation of the outstanding batch.

    It reads the session named by ``model_action_id`` — it never rebuilds or restarts
    the batch — and reports exactly one :class:`ToolCallObserved` back.
    """

    model_action_id: str = ""
    awaiting: tuple[str, ...] = ()


@dataclass(frozen=True)
class FinishToolBatch(Action):
    """Every declared call has been observed; the driver finishes the batch.

    The driver runs the batch-completed hooks here (they belong to the batch boundary,
    not to any single call), emits the result-message framing in declared order and
    reports the settlement back. The persisted per-call facts are not rewritten: the
    reducer derives the results from the calls it already committed.
    """

    model_action_id: str = ""
    suspend: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class WaitForExternal(Action):
    """The execution stopped before its next request and is waiting for a decision."""

    interaction: Mapping[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------------------


class CallProgress(str, Enum):
    """How far the driver got with one declared call.

    This is *schedule* progress, not a business verdict: ``DECLARED`` says the driver
    knows the call and has committed its identity, ``DISPATCHING`` says the driver
    handed it to its executor, ``OBSERVED`` says an observation was accepted. Only the
    operation ledger can prove whether the handler ever ran — which is why a
    ``DECLARED`` call of an interrupted batch is *not* replayable on this field alone.
    """

    DECLARED = "declared"
    DISPATCHING = "dispatching"
    OBSERVED = "observed"


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
    #: The index of the call in the model's declaration. Source order is a property of
    #: the batch, so it is recorded rather than re-derived from a transcript a resume
    #: may be reading in a different shape.
    declared_index: int = -1
    #: The model action (_not_ the attempt) this call belongs to. Two turns of one run
    #: may reuse a tool-call id; the action reference is what keeps them apart.
    model_action_id: str = ""
    progress: CallProgress = CallProgress.DECLARED
    effect_state: str | None = None
    tool_version: str = ""
    #: A digest over the declared arguments. Recovery compares it with the ledger
    #: binding before it reuses a receipt: "same call id" is not "same call".
    arguments_digest: str = ""
    #: The identity of the accepted observation, and a digest of its content. A repeat
    #: with the same digest is idempotent; different content under the same call is a
    #: conflict, refused rather than merged.
    observation_id: str = ""
    observed_digest: str = ""
    #: The attempt that produced the observation, so a stale callback from an earlier
    #: attempt cannot update this run.
    source_attempt: str = ""
    #: A reference to the authoritative ledger record (operation id, state, external id).
    #: A reference, never a second copy of the truth.
    receipt: Mapping[str, Any] | None = None
    #: The finalized result asked for batch termination.
    terminate: bool = False

    def __post_init__(self) -> None:
        if not self.call_id or not self.tool_name:
            raise ContractError("a batch call state needs a call id and a tool name")
        if not isinstance(self.outcome, CallOutcome):
            raise ContractError(f"outcome must be a CallOutcome, got {self.outcome!r}")
        if not isinstance(self.progress, CallProgress):
            raise ContractError(f"progress must be a CallProgress, got {self.progress!r}")
        if self.outcome is CallOutcome.SETTLED and self.result is None:
            raise ContractError(f"{self.call_id}: a settled call must carry its result")
        if self.progress is CallProgress.OBSERVED and not self.observation_id:
            raise ContractError(f"{self.call_id}: an observed call must name its observation")

    @property
    def observed(self) -> bool:
        return self.progress is CallProgress.OBSERVED

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
            "declared_index": self.declared_index,
            "model_action_id": self.model_action_id,
            "progress": self.progress.value,
            "effect_state": self.effect_state,
            "tool_version": self.tool_version,
            "arguments_digest": self.arguments_digest,
            "observation_id": self.observation_id,
            "observed_digest": self.observed_digest,
            "source_attempt": self.source_attempt,
            "receipt": None if self.receipt is None else dict(self.receipt),
            "terminate": self.terminate,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "BatchCallState":
        accepted = {
            name: value for name, value in record.items() if name in cls.__dataclass_fields__
        }
        if "outcome" in accepted and not isinstance(accepted["outcome"], CallOutcome):
            accepted["outcome"] = CallOutcome(str(accepted["outcome"]))
        if "progress" in accepted and not isinstance(accepted["progress"], CallProgress):
            accepted["progress"] = CallProgress(str(accepted["progress"]))
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
    #: The execution right this invocation runs under (``RunIdentity.attempt_id``). It is
    #: recorded so an observation carrying a different attempt is refused instead of
    #: updating a run it does not belong to. Empty for a driver with no attempt concept.
    attempt_id: str = ""
    #: Committed conversation, in order.
    messages: tuple[Any, ...] = ()
    #: Messages produced by this invocation only.
    new_messages: tuple[Any, ...] = ()
    #: Messages that enter the next request but are not committed yet.
    pending: tuple[Any, ...] = ()
    #: Messages a preparation hook produced for the next request (legacy ``prepared``).
    prepared: tuple[Any, ...] = ()
    #: The prepared request view of the outstanding model request. Empty means "the
    #: transcript is the view". It is derived state: a reader that ignores it can always
    #: prepare again from ``messages``, and it never replaces ``messages``.
    request_view: tuple[Any, ...] = ()
    #: The versioned projection facts behind ``request_view`` (source head, fingerprints,
    #: estimator identity, kept/dropped counts, refusal reason). Plain data only: no
    #: callables, credentials or provider objects. Committed in the same transaction as
    #: the request it describes.
    context_state: Mapping[str, Any] | None = None
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
    #: The deployment this invocation was bound to, recorded *before* the first dispatch
    #: (model fingerprints plus the model-visible tool manifest). It is the one
    #: authoritative baseline a recovery compares against: a resume that re-derives it
    #: from the deployment it is running in would be comparing that deployment with
    #: itself. Plain data only — no callables, no credentials, no provider objects.
    arrangement: Mapping[str, Any] | None = None

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
        object.__setattr__(self, "request_view", tuple(self.request_view))
        if self.context_state is not None and not isinstance(self.context_state, Mapping):
            raise ContractError("context_state must be a plain mapping or None")

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
    def unobserved_calls(self) -> tuple[BatchCallState, ...]:
        """Declared calls the driver has not reported an observation for."""
        return tuple(call for call in self.batch if not call.observed)

    def call(self, call_id: str) -> BatchCallState | None:
        return next((call for call in self.batch if call.call_id == call_id), None)

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


def declared_batch(calls: Sequence[ToolCall], *, model_action_id: str) -> tuple[BatchCallState, ...]:
    """The declaration partition: one ``DECLARED`` state per model-declared call.

    Built by the reducer the moment the model turn is accepted, so the identity, the
    source order and the argument digest of every call are committed by the host's
    commit port *before* the batch action runs. That ordering is what makes "the call's
    identity is durable before anything was dispatched" true by construction.
    """
    from ..versioning import fingerprint

    states: list[BatchCallState] = []
    for index, call in enumerate(calls):
        arguments = call.arguments if isinstance(call.arguments, Mapping) else {}
        states.append(
            BatchCallState(
                call_id=call.id,
                tool_name=call.name,
                declared_index=index,
                model_action_id=model_action_id,
                arguments_digest=fingerprint(
                    {"tool": call.name, "args": _fingerprintable(arguments)}
                ),
            )
        )
    return tuple(states)


def _fingerprintable(value: Any) -> Any:
    """Coerce declared arguments into something canonical JSON can hash."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _fingerprintable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_fingerprintable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_fingerprintable(item) for item in value), key=repr)
    to_json = getattr(value, "to_json", None)
    if callable(to_json):
        return to_json()
    return str(value)


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


def apply_observation(
    call: BatchCallState,
    *,
    outcome: CallOutcome,
    status: str = "",
    result: Mapping[str, Any] | None = None,
    operation_id: str = "",
    effect_state: str | None = None,
    tool_version: str = "",
    receipt: Mapping[str, Any] | None = None,
    terminate: bool = False,
    reason: str = "",
    approval_binding_key: str | None = None,
    observation_id: str = "",
    observed_digest: str = "",
    source_attempt: str = "",
) -> BatchCallState:
    """Turn one observation into the committed per-call fact.

    Pure, and shared on purpose: the reducer applies observations that arrive while a
    batch runs, and the recovery path applies the observations it *reads* from the ledger.
    Both must produce exactly the same shape, or a resumed call would carry facts a
    normally-settled one does not.

    A factory-ish default is avoided: fields the observation does not carry keep what the
    call already had, so an observation never erases identity it cannot restate.
    """
    return replace(
        call,
        outcome=outcome,
        status=status,
        result=None if result is None else dict(result),
        operation_id=operation_id or call.operation_id,
        effect_state=effect_state,
        tool_version=tool_version or call.tool_version,
        progress=CallProgress.OBSERVED,
        observation_id=observation_id or call.observation_id,
        observed_digest=observed_digest,
        source_attempt=source_attempt or call.source_attempt,
        receipt=None if receipt is None else dict(receipt),
        terminate=bool(terminate),
        reason=reason or call.reason,
        approval_binding_key=approval_binding_key or call.approval_binding_key,
    )


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
    calls = [
        {
            "call_id": call.call_id,
            "tool_name": call.tool_name,
            "declared_index": call.declared_index,
            "outcome": call.outcome.value,
            "progress": call.progress.value,
            "status": call.status,
            "effect_state": call.effect_state,
            "operation_id": call.operation_id,
            "has_result": call.result is not None,
            "observed": call.observed,
            "reason": call.reason,
        }
        for call in state.batch
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
        #: Calls the driver has not observed yet. Inside a dispatch window this is *not*
        #: "never ran": only the operation ledger can prove that, and recovery must ask
        #: it per call before anything is dispatched again.
        "unobserved": [
            {
                "call_id": call.call_id,
                "tool_name": call.tool_name,
                "progress": call.progress.value,
                "operation_id": call.operation_id,
            }
            for call in state.batch
            if not call.observed
        ],
        "calls": calls,
        "replayable": [
            {"call_id": call.call_id, "tool_name": call.tool_name}
            for call in state.batch
            # A call of the dispatch window in progress is never replayable from the state
            # alone; that is the whole point of the ledger check on recovery.
            if call.outcome is CallOutcome.UNSTARTED
            and call.progress is not CallProgress.OBSERVED
            and state.phase is not Phase.RUNNING_BATCH
        ],
        "declared": declared,
        "batch_outcome_unknown": state.phase is Phase.RUNNING_BATCH,
        "interaction": None if state.interaction is None else dict(state.interaction),
        "model_request_outstanding": state.phase is Phase.REQUESTING_MODEL,
        #: True when the outstanding request was already prepared and committed. The
        #: preparation is derived state: the next attempt may reuse it after checking the
        #: source fingerprint, or prepare again — it grants no execution right either way.
        "request_prepared": bool(state.request_view),
        "context_source_fingerprint": (
            None
            if not state.context_state
            else state.context_state.get("source_fingerprint")
        ),
        "suspended": state.phase is Phase.SUSPENDED,
    }


#: The string fields an arrangement record may hold: the fingerprints a resume compares,
#: plus the model-visible manifest an approval was granted against.
_ARRANGEMENT_STRING_FIELDS = ("config", "tools", "instructions", "policy")


def arrangement_to_record(arrangement: Mapping[str, Any]) -> Mapping[str, Any]:
    """Serialize an arrangement, refusing anything but plain data.

    The value is committed next to the transcript, so "it is only strings and lists" is
    a contract rather than a hope: a host that put a live object in here would make the
    record unserializable and the recovery unverifiable at the same time.
    """
    if not isinstance(arrangement, Mapping):
        raise ContractError(
            f"an arrangement must be a mapping, got {type(arrangement).__name__}"
        )
    out: dict[str, Any] = {}
    for name in _ARRANGEMENT_STRING_FIELDS:
        value = arrangement.get(name)
        if value is None or value == "":
            continue
        if not isinstance(value, str):
            raise ContractError(
                f"arrangement[{name!r}] must be a string, got {type(value).__name__}"
            )
        out[name] = value
    manifest = arrangement.get("exposure_manifest")
    if manifest is not None:
        if not isinstance(manifest, Mapping):
            raise ContractError("arrangement['exposure_manifest'] must be a mapping")
        out["exposure_manifest"] = {
            str(key): _plain_arrangement_value(value, str(key)) for key, value in manifest.items()
        }
    return out


def _plain_arrangement_value(value: Any, path: str) -> Any:
    """Coerce one manifest value into committable plain data, or refuse it."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Mapping):
        return {
            str(key): _plain_arrangement_value(item, f"{path}.{key}")
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_plain_arrangement_value(item, path) for item in value]
    raise ContractError(
        f"arrangement[{path!r}] holds a {type(value).__name__}, which cannot be committed "
        "as a plain fact"
    )


def arrangement_from_record(value: Any) -> Mapping[str, Any] | None:
    """Read a committed arrangement, refusing a shape this build cannot interpret.

    ``None`` means "this record predates the baseline". That is a fact a recovery has to
    handle explicitly; treating it as "nothing changed" is how a drifted deployment
    would be allowed to dispatch.
    """
    if value is None:
        return None
    return arrangement_to_record(value)


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
        "attempt_id": state.attempt_id,
        "messages": [message_to_json(message) for message in state.messages],
        "new_messages": [message_to_json(message) for message in state.new_messages],
        "pending": [message_to_json(message) for message in state.pending],
        "prepared": [message_to_json(message) for message in state.prepared],
        "request_view": [message_to_json(message) for message in state.request_view],
        "context_state": None if state.context_state is None else dict(state.context_state),
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
        "arrangement": (
            None if state.arrangement is None else arrangement_to_record(state.arrangement)
        ),
        "pending_work": dict(pending_work_of(state)),
    }


def context_state_from_record(value: Any) -> Mapping[str, Any] | None:
    """Read the projection facts a commit carried, refusing an unknown version.

    Absent is legitimate and means "this run has no prepared request view": a record
    written before this field existed, or a run with no preparation configured. An
    *unknown* version is refused rather than interpreted — the same rule the execution
    state itself follows, and the reason the projection carries its own schema version
    instead of borrowing the record's.
    """
    from ..errors import VersionRefused

    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ContractError(f"context_state must be a mapping, got {type(value).__name__}")
    version = value.get("context_schema_version")
    if version is None:
        raise ContractError(
            "a context_state without a context_schema_version cannot be interpreted; "
            "refusing instead of guessing what the projection means"
        )
    if version != CONTEXT_STATE_SCHEMA_VERSION:
        raise VersionRefused("context state", version, (CONTEXT_STATE_SCHEMA_VERSION,))
    return dict(value)


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
    if version not in SUPPORTED_EXECUTION_STATE_SCHEMA_VERSIONS:
        raise VersionRefused(
            "execution state", version, SUPPORTED_EXECUTION_STATE_SCHEMA_VERSIONS
        )
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
        attempt_id=str(record.get("attempt_id") or ""),
        messages=tuple(message_from_json(item) for item in record.get("messages") or ()),
        new_messages=tuple(message_from_json(item) for item in record.get("new_messages") or ()),
        pending=tuple(message_from_json(item) for item in record.get("pending") or ()),
        prepared=tuple(message_from_json(item) for item in record.get("prepared") or ()),
        request_view=tuple(message_from_json(item) for item in record.get("request_view") or ()),
        context_state=context_state_from_record(record.get("context_state")),
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
        arrangement=arrangement_from_record(record.get("arrangement")),
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
