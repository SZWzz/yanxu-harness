"""Run-level outcomes: one vocabulary for how a run ended.

Three things that must never be conflated, and which this module keeps apart:

* **The run's status** (:class:`RunStatus`) — how the run ended, or that it is
  still active. ``RUNNING`` is the only non-terminal active phase; it exists
  because a caller that asks "what happened?" while a run is alive must not be
  told ``completed``.
* **Why the loop stopped** (:class:`LoopStop`) — the exit the loop took inside one
  invocation. A host stop hook and a natural answer both end as ``completed``, but
  they are not the same fact, so the reason stays visible.
* **A tool call's outcome** (``yanxu_harness.tools.outcomes``) — a different
  vocabulary for a different question; nothing here replaces it.

``RunStatus`` lives in the core because the model loop is what produces it: a
loop result carries a status, and the harness must not have to translate between
two enums that mean the same thing. ``yanxu_harness.runtime`` re-exports it, so
both import paths are the same object.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from ..errors import ContractError, HarnessError

__all__ = [
    "CallOutcome",
    "LoopResult",
    "LoopStop",
    "LoopSuspended",
    "MAX_TURNS_STOP_CODE",
    "RUN_STATUS_TRANSITIONS",
    "RunStatus",
    "TERMINAL_STATUSES",
    "assert_transition",
    "can_transition",
    "classify_status",
    "is_terminal_status",
]

#: The machine-readable ``error_code`` a usage-limited run carries. A budget stop
#: is not a failure, but it is also not a completion, so it needs its own code.
MAX_TURNS_STOP_CODE = "max_turns"


class RunStatus(str, Enum):
    """How a run ended — or that it is still active."""

    #: The run is active. Never returned as a finished outcome.
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    #: Paused waiting for an external decision. Not terminal: a resume continues
    #: the same namespace/session/run under a new attempt.
    SUSPENDED = "suspended"
    USAGE_LIMITED = "usage_limited"
    REJECTED = "rejected"


#: Statuses after which no further progress can happen without a new invocation.
#: ``suspended`` and ``running`` are deliberately absent: neither has ended.
TERMINAL_STATUSES = frozenset(
    {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.USAGE_LIMITED, RunStatus.REJECTED}
)


def is_terminal_status(status: RunStatus | str) -> bool:
    return RunStatus(status) in TERMINAL_STATUSES


#: Allowed run-state transitions. ``RUNNING -> RUNNING`` is an update of an active
#: run (turns/usage change); a terminal status has no outgoing edge, because a
#: finished run must not be revived by a late callback or a new prompt.
RUN_STATUS_TRANSITIONS: Mapping[RunStatus, frozenset[RunStatus]] = {
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.RUNNING,
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.SUSPENDED,
            RunStatus.USAGE_LIMITED,
            RunStatus.REJECTED,
        }
    ),
    RunStatus.SUSPENDED: frozenset(
        {
            RunStatus.RUNNING,
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.REJECTED,
            # A run may pause *again*: a resumed attempt can meet a second approval
            # or a second unproven operation. That is a new pause under a new
            # attempt, not the same fact repeated, which is why it is allowed here
            # while a terminal status has no outgoing edge at all.
            RunStatus.SUSPENDED,
        }
    ),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
    RunStatus.USAGE_LIMITED: frozenset(),
    RunStatus.REJECTED: frozenset(),
}


def can_transition(current: RunStatus | str, next_status: RunStatus | str) -> bool:
    """Whether a stored run may move from ``current`` to ``next_status``."""
    return RunStatus(next_status) in RUN_STATUS_TRANSITIONS[RunStatus(current)]


def assert_transition(current: RunStatus | str, next_status: RunStatus | str, *, what: str = "run") -> None:
    """Raise :class:`ContractError` for a transition the state machine forbids."""
    if not can_transition(current, next_status):
        raise ContractError(
            f"a {what} in status {RunStatus(current).value!r} cannot move to "
            f"{RunStatus(next_status).value!r}"
        )


class LoopStop(str, Enum):
    """Why one loop invocation stopped.

    Distinct from :class:`RunStatus` on purpose: the status is the run's verdict,
    the stop is the loop's exit. ``STOP_HOOK`` and ``ASSISTANT_FINAL`` are both
    ``COMPLETED``, but a host that asked to stop is not the same as a model that
    finished, and the difference must not have to be reconstructed from text.
    """

    #: The model produced an answer with no tool calls and no queued input left.
    ASSISTANT_FINAL = "assistant_final"
    #: Every result of the last batch asked to terminate the run.
    BATCH_TERMINATE = "batch_terminate"
    #: A host hook (`should_stop_after_turn`) returned True.
    STOP_HOOK = "stop_hook"
    #: The turn budget was reached; the last authorised batch was settled first.
    MAX_TURNS = "max_turns"
    #: The model request failed (`stop_reason=error`).
    MODEL_ERROR = "model_error"
    #: The invocation itself failed (a hook, a bridge or the driver raised).
    #: Deliberately distinct from :attr:`MODEL_ERROR`: a broken host hook is not the
    #: provider's failure, and a host that retries provider errors must not retry
    #: this one as if the model had answered badly.
    INTERNAL_ERROR = "internal_error"
    #: The run was cancelled (`stop_reason=aborted`, or cancelled between turns).
    ABORTED = "aborted"
    #: The run is waiting for an external decision — either a tool batch reported
    #: `awaiting_approval`/`deferred`/`unknown`, or the model ended its turn with
    #: `stop_reason=deferred`.
    SUSPENDED = "suspended"


class CallOutcome(str, Enum):
    """The run's view of one tool call: settled, waiting, unproven or not reached.

    This vocabulary lives in the core because the loop is what has to decide whether a
    batch lets it ask the model again; ``yanxu_harness.runtime.batch`` re-exports this
    same object, so a run record and a reducer cannot drift into two classifications.

    ``settled``
        the call has a real result (executed, replayed, denied, invalid args, failed,
        cancelled). It has a model-visible result and does not block a resume.
    ``awaiting``
        a human has to decide. Nothing was written, and the call must not be reported
        to the model as if it had been.
    ``uncertain``
        the outcome could not be proven. Neither settled nor replayable: it must not
        be dispatched again and must not be reported as a success.
    ``unstarted``
        the batch stopped before reaching this call. Replayable, in source order.
    """

    SETTLED = "settled"
    AWAITING = "awaiting"
    UNCERTAIN = "uncertain"
    UNSTARTED = "unstarted"

    @property
    def replayable(self) -> bool:
        """Only an unstarted call may be dispatched again on resume."""
        return self is CallOutcome.UNSTARTED


#: Recorded tool status → the run's classification. Keyed by the status *string* the
#: gateway wrote, because the core must classify without importing the tool layer.
#: A status this table does not name is refused in :func:`classify_status` rather than
#: defaulted to a benign value, and ``tests/core`` pins that every status the gateway
#: can produce is named here.
_STATUS_OUTCOMES: Mapping[str, CallOutcome] = {
    "executed": CallOutcome.SETTLED,
    "replayed": CallOutcome.SETTLED,
    "denied": CallOutcome.SETTLED,
    "invalid_args": CallOutcome.SETTLED,
    "failed": CallOutcome.SETTLED,
    "cancelled": CallOutcome.SETTLED,
    "awaiting_approval": CallOutcome.AWAITING,
    "deferred": CallOutcome.AWAITING,
    "unknown": CallOutcome.UNCERTAIN,
}


def classify_status(status: str) -> CallOutcome:
    """Classify one recorded tool status, refusing an unknown one."""
    try:
        return _STATUS_OUTCOMES[str(status)]
    except KeyError as exc:
        raise ContractError(
            f"a recorded tool status {status!r} is not one this build can classify; "
            "refusing to guess whether the call ran"
        ) from exc


@dataclass(frozen=True)
class LoopResult:
    """The structured outcome of one model-loop invocation.

    ``run_agent_loop``/``run_agent_loop_continue`` keep returning a plain list of
    messages; this type is what a driver needs in order to know *why* the list
    ended, without guessing from the last message's role, the number of events or
    the text of an answer.
    """

    status: RunStatus
    stop: LoopStop
    messages: tuple[Any, ...] = ()
    turns_used: int = 0
    #: The model's own stop reason for the last assistant message (``stop``,
    #: ``toolUse``, ``length``, ``error``, ``aborted``, ...).
    stop_reason: str = "pending"
    error_code: str | None = None
    detail: str = ""
    pending_interaction: Mapping[str, Any] | None = None
    final_text: str = ""
    usage: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.status, RunStatus):
            raise ContractError(f"loop result status must be a RunStatus, got {self.status!r}")
        if not isinstance(self.stop, LoopStop):
            raise ContractError(f"loop result stop must be a LoopStop, got {self.stop!r}")
        if self.status is RunStatus.RUNNING:
            raise ContractError(
                "a loop result is an outcome: 'running' is a phase, not a way for an "
                "invocation to end"
            )
        if self.status is RunStatus.SUSPENDED and self.pending_interaction is None:
            raise ContractError("a suspended loop result must carry the pending interaction")
        if self.status is RunStatus.USAGE_LIMITED and not self.error_code:
            raise ContractError("a usage-limited loop result must carry a machine-readable error_code")
        if self.turns_used < 0:
            raise ContractError(f"turns_used must not be negative, got {self.turns_used}")
        object.__setattr__(self, "messages", tuple(self.messages))
        object.__setattr__(self, "usage", dict(self.usage or {}))
        if self.pending_interaction is not None:
            object.__setattr__(self, "pending_interaction", dict(self.pending_interaction))

    @property
    def terminal(self) -> bool:
        """False for a suspended run: it has work left, it is just not being driven."""
        return is_terminal_status(self.status)

    def list_messages(self) -> list[Any]:
        """The legacy list-returning shape, for the compatibility entry points."""
        return list(self.messages)


class LoopSuspended(HarnessError):
    """Control signal: the run is waiting, and a plain list cannot say so.

    ``run_agent_loop``/``run_agent_loop_continue`` keep their historical contract
    (a list of the new messages, or an exception). A suspension is not a failure
    and not a completion, so it must not be expressed as either: the list entry
    raises this, carrying the :class:`LoopResult` a structured caller can act on.

    It derives from :class:`~yanxu_harness.errors.HarnessError` so it can never be
    confused with an application's own exceptions, and it is deliberately **not** a
    subclass of any failure type. A driver that maps loop exceptions onto run
    statuses must catch this one *before* its generic failure handling — the
    package does that itself in :class:`~yanxu_harness.core.agent.Agent`.
    """

    code = "loop_suspended"

    def __init__(self, result: LoopResult) -> None:
        if not isinstance(result, LoopResult):
            raise ContractError(f"LoopSuspended needs a LoopResult, got {result!r}")
        if result.status is not RunStatus.SUSPENDED:
            raise ContractError(
                f"LoopSuspended is only for a suspended run, got {result.status.value!r}"
            )
        interaction = result.pending_interaction or {}
        kind = interaction.get("kind", "external_decision")
        super().__init__(
            f"the run is waiting for {kind}; use the structured entry point "
            "(run_agent_loop_result) to read the pause"
        )
        self.result = result
