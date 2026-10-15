"""The configuration of one model-loop invocation, and its hook payloads.

Split out of :mod:`yanxu_harness.core.agent_loop` in S3 (see
``docs/core-state-machine-implementation-plan.zh-CN.md``) so the driver
(:mod:`yanxu_harness.core.runner`) and the legacy entry points can share one
definition without an import cycle. The names are re-exported from
``yanxu_harness.core.agent_loop`` unchanged, so an existing import keeps working.

`AgentLoopConfig` is a *snapshot* of host intent, not of execution state: the same
object must not be carried across a restart (see `ExecutionState` in
:mod:`yanxu_harness.core.execution` for what survives). Every callable here is a
port the host provides; nothing in this module reads the environment, a clock or a
global default stream.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from .types import AssistantMessage, AgentContext, ToolResultMessage

__all__ = [
    "AgentLoopConfig",
    "PrepareNextTurnContext",
    "RequestPreparation",
    "TurnEndContext",
    "validate_initial_turns_used",
    "validate_max_concurrent_tools",
    "validate_max_turns",
]


@dataclass
class TurnEndContext:
    """`shouldStopAfterTurn` 的入参（types.ts:131-140）。"""

    message: AssistantMessage
    tool_results: tuple[ToolResultMessage, ...]
    context: AgentContext
    new_messages: Sequence[Any]


@dataclass
class PrepareNextTurnContext:
    """`prepareNextTurn` 的入参（types.ts:154）；替换只影响下一次请求。"""

    message: AssistantMessage
    tool_results: tuple[ToolResultMessage, ...]
    context: AgentContext
    new_messages: Sequence[Any]


@dataclass
class RequestPreparation:
    """What a context controller gets before a model request (plan section 4.2).

    Deliberately plain data: the transcript as it will be requested (after the tool-set
    delta), the tools that are executable this turn, and the cancellation token. Scope,
    policy and the source head belong to whoever configured the controller — the harness
    binds them, the loop does not invent them.
    """

    messages: tuple[Any, ...]
    model: Any = None
    turn: int = 0
    thinking_level: str | None = None
    tools: tuple[Any, ...] = ()
    signal: Any = None


@dataclass
class AgentLoopConfig:
    stream_fn: Callable[..., Any]
    model: Any = None
    tools: list[Any] = field(default_factory=list)
    api_key: str | None = None
    get_api_key: Callable[[str], Any] | None = None
    convert_to_llm: Callable[[Sequence[Any]], Any] | None = None
    transform_context: Callable[..., Any] | None = None
    # When enabled, the transformed request view also becomes the active context
    # for later turns. `new_messages` remains the immutable-in-practice raw delta
    # returned to the host for transcript archival.
    #
    # This is the *legacy* destructive mode and it stays that way on purpose: the
    # managed path is `prepare_context`, which records a derived view beside the
    # transcript instead of replacing it. See `docs/context-preparation-baseline.zh-CN.md`.
    persist_transformed_context: bool = False
    #: Context preparation for the next model request (plan section 4.2). Called once per
    #: turn, before anything is sent; it may be sync or async and returns either a mapping
    #: (``{"view": ..., "context_state": ..., "refused": ...}``) or ``None`` (the
    #: transcript is the view). The prepared view and its projection facts travel back to
    #: the reducer as ``RequestPrepared``, so the host's commit port confirms them before
    #: the driver sends anything. A refusal ends the run as ``usage_limited`` with
    #: ``context_budget_exceeded``: no model is asked and no turn is spent.
    #:
    #: It replaces the old ``project_context`` seam, which was written into the provider
    #: options and read by nobody. Combining it with `transform_context`,
    #: `persist_transformed_context` or `convert_to_llm` is refused before dispatch rather
    #: than silently running two different compactions.
    prepare_context: Callable[[RequestPreparation], Any] | None = None
    before_tool_call: Callable[..., Any] | None = None
    after_tool_call: Callable[..., Any] | None = None
    should_stop_after_turn: Callable[[TurnEndContext], Any] | None = None
    prepare_next_turn: Callable[[PrepareNextTurnContext], Any] | None = None
    on_tool_batch_complete: Callable[[AssistantMessage, Sequence[ToolResultMessage]], Any] | None = None
    #: Called after a tool batch settled. Returning a mapping means "this run is
    #: waiting for an external decision": the loop stops *before* the next model
    #: request and reports `SUSPENDED` with that mapping as the interaction. A
    #: waiting call is never written as a completed tool result and handed to the
    #: model, because the model would then answer as if the effect had happened.
    should_suspend_after_batch: (
        Callable[[AssistantMessage, Sequence[ToolResultMessage]], Any] | None
    ) = None
    get_steering_messages: Callable[[], Any] | None = None
    get_follow_ups: Callable[[], Any] | None = None
    tool_execution: str = "parallel"
    #: Largest number of tool calls of one batch that may be *executing* at the same
    #: time. ``None`` (the default) keeps the previous behaviour: every call of a
    #: parallel batch runs concurrently. Preflight (start events, argument preparation,
    #: ``before_tool_call``) is always sequential and in source order, and the limit does
    #: not change message correlation: end events follow completion order, result
    #: messages follow source order.
    max_concurrent_tools: int | None = None
    #: The commit port of a durable host. It is called with the accepted
    #: :class:`~yanxu_harness.core.execution.ExecutionState` **before** the actions of
    #: that transition are performed, which is what makes "the model's final response is
    #: committed before its tool calls are dispatched" true rather than hoped for. It may
    #: be sync or async; a failure propagates and ends the run (a commit that did not
    #: happen must not be silently ignored). ``None`` for an in-memory caller.
    state_sink: Callable[[Any], Any] | None = None
    argument_validator: Callable[[Any, dict], dict] | None = None
    max_turns: int | None = None
    #: Turns already spent by an earlier attempt of the *same* logical run. A resumed
    #: invocation continues the count instead of restarting it, so a budget cannot be
    #: refreshed by pausing: the plan requires the logical invocation's budget to survive
    #: a new attempt.
    initial_turns_used: int = 0
    #: The attempt this invocation runs under (``RunIdentity.attempt_id``). It is
    #: recorded in the execution state and attached to every tool-call observation, so a
    #: callback from an earlier attempt cannot update this run.
    attempt_id: str = ""
    reasoning: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def validate_max_turns(max_turns: int | None) -> None:
    """Reject ambiguous request budgets before the loop emits events or dispatches."""
    if max_turns is not None and (type(max_turns) is not int or max_turns <= 0):
        raise ValueError("max_turns must be None or a positive integer")


def validate_max_concurrent_tools(limit: int | None) -> None:
    """Reject an ambiguous tool-concurrency limit before any call is dispatched."""
    if limit is not None and (type(limit) is not int or limit <= 0):
        raise ValueError("max_concurrent_tools must be None or a positive integer")


def validate_initial_turns_used(value: int) -> None:
    """A pre-spent turn count must be a non-negative int (a bool is not a count)."""
    if type(value) is not int or value < 0:
        raise ValueError(f"initial_turns_used must be a non-negative integer, got {value!r}")
