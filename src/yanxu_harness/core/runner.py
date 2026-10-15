"""S3: the single Runner — the only thing that drives the model loop.

The reducer (:mod:`yanxu_harness.core.transitions`) decides; this module performs the
decisions. It is the one place where hooks are called, the model stream is consumed and
tool batches are executed, and every entry point in the package goes through it:

* ``run_agent_loop`` / ``run_agent_loop_continue`` (list-returning, compatibility),
* ``run_agent_loop_result`` / ``run_agent_loop_continue_result`` (structured),
* :class:`yanxu_harness.core.agent.Agent`'s prompt/continue wrappers,
* :class:`yanxu_harness.runtime.DefaultHarness`'s start/resume.

There is no second model loop and no configuration switch that selects one.

What the runner owns, and why the split is where it is:

* **progress framing.** Streaming a turn emits ``MessageStart``/``MessageUpdate`` and
  the final ``MessageEnd``; executing a batch emits ``ToolExecutionStart``/
  ``ToolExecutionUpdate``/``ToolExecutionEnd`` and the result-message framing. Those
  frames are interleaved with the work itself (a tool's ``end`` is emitted in
  completion order, its result message in source order), so they stay with the code
  that performs the work.
* **control events.** ``AgentStart``/``AgentEnd``, ``TurnStart``/``TurnEnd`` and the
  framing of the messages the reducer inserts into the transcript are produced by the
  reducer and dispatched here in order.
* **hooks.** ``prepare_next_turn`` becomes an explicit ``PrepareTurn`` action and
  ``should_stop_after_turn`` an ``EvaluateStopHook`` action: the reducer asks, the
  runner calls, the answer comes back as ``TurnPrepared``/``StopHookEvaluated``. Their
  positions, call counts and failure semantics are unchanged from the previous loop.

One property worth stating because it is easy to lose: the runner never decides *why*
a run ended. It returns :func:`~yanxu_harness.core.transitions.result_of` of the state
the reducer produced, so ``completed``/``usage_limited``/``suspended``/``cancelled``
cannot drift between the two.
"""

from __future__ import annotations

import inspect
from collections import deque
from dataclasses import replace
from typing import Any, Awaitable, Callable, Mapping, Sequence

from ..errors import ContractError
from .events import MessageEnd, MessageStart, MessageUpdate, ToolExecutionEnd, ToolExecutionStart
from .execution import (
    QUIESCENT_PHASES,
    CancellationRequested,
    ContinueRequested,
    ContextProjected,
    EvaluateStopHook,
    ExecutionState,
    InputEvent,
    InputKind,
    InputsPolled,
    ModelCompleted,
    PollSite,
    PrepareTurn,
    PromptSubmitted,
    ReadInputs,
    RequestModel,
    RunToolBatch,
    StopHookEvaluated,
    ToolBatchSettled,
    TurnPrepared,
    WaitForExternal,
)
from .loop_config import (
    AgentLoopConfig,
    PrepareNextTurnContext,
    TurnEndContext,
    validate_max_concurrent_tools,
    validate_max_turns,
)
from .messages import declare_tool_changes, normalize_context, to_tool_declaration
from .outcome import LoopResult, LoopSuspended, RunStatus
from .tool_execution import execute_tool_calls, error_result, tool_call_blocks
from .transitions import advance, result_of
from .types import AgentContext, AssistantMessage, ToolCall, ToolResultMessage

__all__ = [
    "Runner",
    "run_agent_loop",
    "run_agent_loop_continue",
    "run_agent_loop_continue_result",
    "run_agent_loop_result",
]

EventSink = Callable[[Any], Awaitable[None]]


async def _maybe(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def _poll(callback: Callable[[], Any] | None) -> list[Any]:
    if callback is None:
        return []
    return list(await _maybe(callback()) or [])


def _resolve_stream_fn(config: AgentLoopConfig) -> Callable[..., Any]:
    """core 必须有显式注入点；不提供「全局默认流」的兜底（计划 §28 登记的差异）。"""
    if config.stream_fn is None:
        raise ValueError("AgentLoopConfig.stream_fn 必须显式提供，core 不接受跨调用共享的全局默认流")
    return config.stream_fn


def _signal_cancelled(signal: Any) -> bool:
    """Accept both token shapes (a plain bool attribute and a token property)."""
    return bool(signal is not None and getattr(signal, "cancelled", False))


def _copy(message: Any) -> Any:
    """上游 emit 的是浅拷贝快照（`{ ...partialMessage }`），避免消费者持有活的 partial。"""
    if inspect.isawaitable(message):
        raise TypeError("不能把 awaitable 当消息发出去")
    try:
        return replace(message)
    except TypeError:  # 非 dataclass（自定义消息）按引用发出
        return message


class _RequestView:
    """The conversation of one request: what the model sees, and what persists.

    ``messages`` is what the request is built from. ``persisted`` is set only when a
    transform or a budget projection is configured to survive the request; the runner
    reports it to the reducer as ``ContextProjected`` once the stream has finished, so
    the next request sees the same view the previous one persisted.
    """

    def __init__(self, messages: Sequence[Any]) -> None:
        self.messages: list[Any] = list(messages)
        self.persisted: list[Any] | None = None

    def persist(self, messages: Sequence[Any]) -> None:
        self.messages = list(messages)
        self.persisted = list(messages)


class Runner:
    """Drives one reducer state to a stop, performing every action it asks for."""

    def __init__(self, config: AgentLoopConfig, emit: EventSink) -> None:
        self.config = config
        self.emit = emit
        self._tools: list[Any] = list(config.tools or [])
        self._seq = 0

    # -- entry points ---------------------------------------------------

    async def run(self, prompts: Sequence[Any], context: AgentContext, signal: Any) -> LoopResult:
        state = self._initial_state(context)
        event = PromptSubmitted(
            event_id=self._next_id("prompt"),
            at_ms=self._clock_ms(),
            messages=tuple(prompts),
            tools=state.tools,
            max_turns=self.config.max_turns,
        )
        return await self._drive(state, event, signal)

    async def run_continue(self, context: AgentContext, signal: Any) -> LoopResult:
        if not context.messages:
            raise ValueError("Cannot continue: no messages in context")
        if getattr(context.messages[-1], "role", None) == "assistant":
            raise ValueError("Cannot continue from message role: assistant")
        state = self._initial_state(context)
        event = ContinueRequested(
            event_id=self._next_id("continue"),
            at_ms=self._clock_ms(),
            tools=state.tools,
            max_turns=self.config.max_turns,
        )
        return await self._drive(state, event, signal)

    def _initial_state(self, context: AgentContext) -> ExecutionState:
        validate_max_turns(self.config.max_turns)
        validate_max_concurrent_tools(self.config.max_concurrent_tools)
        _resolve_stream_fn(self.config)
        self._tools = list(context.tools or [])
        return ExecutionState(
            messages=tuple(context.messages),
            tools=tuple(to_tool_declaration(tool) for tool in self._tools),
            max_turns=self.config.max_turns,
            model=self.config.model,
            thinking_level=self.config.reasoning,
        )

    # -- the drive loop -------------------------------------------------

    async def _drive(self, state: ExecutionState, first_event: InputEvent, signal: Any) -> LoopResult:
        queue: deque[InputEvent] = deque([first_event])
        while True:
            if _signal_cancelled(signal) and not state.cancel_requested:
                # The token is the caller's fact, not an event: report it as one exactly
                # as the previous loop did when it checked the signal, so a cancellation
                # that arrives between turns is still the run's end reason. Recorded
                # *before* the pending event is applied, which is what keeps a stop hook
                # or a model error able to win the race (their branch decides first).
                queue.appendleft(
                    CancellationRequested(
                        event_id=self._next_id("cancel"),
                        at_ms=self._clock_ms(),
                        reason=str(getattr(signal, "reason", "") or "the run was cancelled"),
                    )
                )
            if not queue:
                raise ContractError(
                    f"the execution is {state.phase.value} but no event is pending; the driver "
                    "would spin without progress"
                )
            event = queue.popleft()
            transition = advance(state, event)
            if transition.ignored_reason is not None:
                # A duplicate or a stale callback: no progress, and no second action.
                continue
            for core_event in transition.events:
                await self.emit(core_event)
            state = transition.state
            if state.phase in QUIESCENT_PHASES:
                return result_of(state)
            for action in transition.actions:
                queue.extend(await self._perform(action, state, signal))

    # -- actions --------------------------------------------------------

    async def _perform(self, action: Any, state: ExecutionState, signal: Any) -> list[InputEvent]:
        if isinstance(action, ReadInputs):
            return await self._read_inputs(action)
        if isinstance(action, PrepareTurn):
            return await self._prepare_turn(action, state)
        if isinstance(action, EvaluateStopHook):
            return await self._stop_hook(action, state)
        if isinstance(action, RequestModel):
            return await self._request_model(action, state, signal)
        if isinstance(action, RunToolBatch):
            return await self._run_batch(action, state, signal)
        if isinstance(action, WaitForExternal):
            # The reducer already returned the pause; nothing is dispatched after it.
            return []
        raise ContractError(f"the runner has no way to perform {action!r}")

    async def _read_inputs(self, action: ReadInputs) -> list[InputEvent]:
        callback = (
            self.config.get_follow_ups
            if action.kind is InputKind.FOLLOW_UP
            else self.config.get_steering_messages
        )
        messages = await _poll(callback)
        return [
            InputsPolled(
                event_id=self._next_id("poll"),
                at_ms=self._clock_ms(),
                site=action.site,
                kind=action.kind,
                messages=tuple(messages),
            )
        ]

    async def _prepare_turn(self, action: PrepareTurn, state: ExecutionState) -> list[InputEvent]:
        if self.config.prepare_next_turn is None:
            # No hook: the action is short-circuited rather than skipped, so the reducer
            # always learns the answer and the phase transition stays in one place.
            return [self._event(TurnPrepared)]
        payload = PrepareNextTurnContext(
            message=action.message,
            tool_results=tuple(action.tool_results),
            context=self._context(state),
            new_messages=list(state.new_messages),
        )
        snapshot = await _maybe(self.config.prepare_next_turn(payload))
        if not snapshot:
            return [self._event(TurnPrepared)]
        context = snapshot.get("context")
        return [
            self._event(
                TurnPrepared,
                messages=tuple(snapshot.get("messages") or ()),
                context_messages=tuple(context.messages) if context is not None else None,
                context_tools=(
                    tuple(to_tool_declaration(tool) for tool in (context.tools or []))
                    if context is not None
                    else None
                ),
                model=snapshot.get("model"),
                thinking_level=snapshot.get("thinking_level"),
            )
        ]

    async def _stop_hook(self, action: EvaluateStopHook, state: ExecutionState) -> list[InputEvent]:
        if self.config.should_stop_after_turn is None:
            return [self._event(StopHookEvaluated, stop=False)]
        payload = TurnEndContext(
            message=action.message,
            tool_results=tuple(action.tool_results),
            context=self._context(state),
            new_messages=list(state.new_messages),
        )
        stop = await _maybe(self.config.should_stop_after_turn(payload))
        return [self._event(StopHookEvaluated, stop=bool(stop))]

    async def _request_model(
        self, action: RequestModel, state: ExecutionState, signal: Any
    ) -> list[InputEvent]:
        """``B3``: build the request view, stream the turn, report the final message."""
        config = self.config
        model = action.model if action.model is not None else config.model
        view = _RequestView(action.messages)
        request_messages: list[Any] = list(action.messages)

        if config.transform_context is not None:
            transformed = list(await _maybe(config.transform_context(list(view.messages), signal)))
            if config.persist_transformed_context:
                view.persist(transformed)
            request_messages = transformed

        if config.convert_to_llm is not None:
            llm_messages = await _maybe(config.convert_to_llm(list(request_messages)))
        else:
            from .messages import to_llm_messages

            llm_messages = to_llm_messages(request_messages)
        llm_context = normalize_context(AgentContext(messages=list(llm_messages)))

        resolved_key = config.api_key
        if config.get_api_key is not None:
            resolved_key = await _maybe(config.get_api_key(getattr(model, "provider", ""))) or config.api_key

        reasoning = None if action.thinking_level in (None, "off") else action.thinking_level
        options: dict[str, Any] = {
            "api_key": resolved_key,
            "signal": signal,
            "reasoning": reasoning,
            **config.extra,
        }
        if config.project_context is not None:

            async def project_for_budget(report: Mapping[str, Any]) -> Sequence[Any] | None:
                projected = await _maybe(
                    config.project_context(
                        list(view.messages), list(state.new_messages), model, report, signal
                    )
                )
                if projected is None:
                    return None
                view.persist(projected)
                if config.convert_to_llm is not None:
                    projected_llm = await _maybe(config.convert_to_llm(list(view.messages)))
                else:
                    from .messages import to_llm_messages

                    projected_llm = to_llm_messages(view.messages)
                return list(projected_llm)

            setattr(
                project_for_budget,
                "checkpoint_version",
                getattr(config.project_context, "checkpoint_version", None),
            )
            # Consumed by the host's final request-budget gate and removed before
            # payload construction or Provider dispatch.
            options["_project_context_for_budget"] = project_for_budget

        final = await self._stream_turn(model, llm_context, options, request_messages, state, signal)
        events: list[InputEvent] = []
        if view.persisted is not None:
            events.append(
                self._event(ContextProjected, messages=tuple(view.persisted))
            )
        events.append(self._event(ModelCompleted, message=final))
        return events

    async def _stream_turn(
        self,
        model: Any,
        llm_context: Any,
        options: Mapping[str, Any],
        request_messages: Sequence[Any],
        state: ExecutionState,
        signal: Any,
    ) -> AssistantMessage:
        """Stream one turn, emitting progress frames, and return the final message.

        ``request_messages`` is the *post-transform* view the request was built from;
        the streamed partial is accumulated in a throwaway list because the transcript
        is committed by the reducer, not by the stream.
        """
        stream_fn = _resolve_stream_fn(self.config)
        response = await _maybe(stream_fn(model, llm_context, options))
        accumulated: list[Any] = list(request_messages)
        partial: AssistantMessage | None = None
        added_partial = False

        async for frame in response:
            kind = frame.get("type") if isinstance(frame, dict) else getattr(frame, "type", None)
            if kind == "start":
                partial = frame["partial"]
                accumulated.append(partial)
                added_partial = True
                await self.emit(MessageStart(message=_copy(partial)))
            elif kind in {"text_start", "text_delta", "text_end", "thinking_start", "thinking_delta",
                          "thinking_end", "toolcall_start", "toolcall_delta", "toolcall_end"}:
                if partial is not None:
                    partial = frame["partial"]
                    accumulated[-1] = partial
                    await self.emit(MessageUpdate(message=_copy(partial), assistant_message_event=frame))
            elif kind in {"done", "error"}:
                final = await response.result()
                if added_partial:
                    accumulated[-1] = final
                else:
                    accumulated.append(final)
                if not added_partial:
                    await self.emit(MessageStart(message=_copy(final)))
                await self.emit(MessageEnd(message=final))
                return final

        final = await response.result()
        if added_partial:
            accumulated[-1] = final
        else:
            accumulated.append(final)
            await self.emit(MessageStart(message=_copy(final)))
        await self.emit(MessageEnd(message=final))
        return final

    async def _run_batch(
        self, action: RunToolBatch, state: ExecutionState, signal: Any
    ) -> list[InputEvent]:
        assistant = state.last_assistant
        if assistant is None:
            raise ContractError("a tool batch was requested without a recorded assistant turn")
        if action.truncated:
            results = await self._fail_truncated_batch(action.calls)
            terminate = False
        else:
            batch = await execute_tool_calls(self._context(state), assistant, self.config, signal, self.emit)
            results = list(batch.messages)
            terminate = batch.terminate
        suspend = await self._batch_settled_hooks(assistant, results)
        return [
            self._event(
                ToolBatchSettled,
                results=tuple(results),
                terminate=terminate,
                suspend=dict(suspend) if suspend else None,
            )
        ]

    async def _batch_settled_hooks(
        self, assistant: AssistantMessage, results: Sequence[ToolResultMessage]
    ) -> Mapping[str, Any] | None:
        """``on_tool_batch_complete`` then ``should_suspend_after_batch``, in that order."""
        if self.config.on_tool_batch_complete is not None:
            await _maybe(self.config.on_tool_batch_complete(assistant, tuple(results)))
        if self.config.should_suspend_after_batch is None:
            return None
        interaction = await _maybe(self.config.should_suspend_after_batch(assistant, tuple(results)))
        return dict(interaction) if interaction else None

    async def _fail_truncated_batch(self, calls: Sequence[ToolCall]) -> list[ToolResultMessage]:
        """``B5a``：被输出上限截断的响应里，每个调用都可能带残缺参数。

        流式参数有兜底 JSON 解析，所以「参数能解析」不代表「参数完整」——一律不执行，
        并给每个调用回一条错误结果，让模型重发。
        """
        messages: list[ToolResultMessage] = []
        for call in calls:
            await self.emit(ToolExecutionStart(tool_call_id=call.id, tool_name=call.name, args=call.arguments))
            result = error_result(
                f'Tool call "{call.name}" was not executed: the response hit the output token limit, '
                "so its arguments may be truncated. Re-issue the tool call with complete arguments."
            )
            await self.emit(ToolExecutionEnd(tool_call_id=call.id, tool_name=call.name, result=result, is_error=True))
            message = ToolResultMessage(
                tool_call_id=call.id,
                tool_name=call.name,
                content=result["content"],
                details=result["details"],
                is_error=True,
                source_provenance=result.get("storyide_source_provenance"),
            )
            await self.emit(MessageStart(message=message))
            await self.emit(MessageEnd(message=message))
            messages.append(message)
        return messages

    # -- helpers --------------------------------------------------------

    def _context(self, state: ExecutionState) -> AgentContext:
        """The context a hook or a batch sees: the committed conversation plus the tool set."""
        return AgentContext(messages=list(state.messages), tools=list(self._tools))

    def _event(self, cls: type, **fields: Any) -> Any:
        return cls(event_id=self._next_id(cls.__name__), at_ms=self._clock_ms(), **fields)

    def _next_id(self, kind: str) -> str:
        self._seq += 1
        return f"{kind.lower()}:{self._seq}"

    def _clock_ms(self) -> int:
        """The only clock in the driver: wall time, used for event timestamps.

        The reducer never reads it; it arrives inside each event, so a recorded event
        stream replays to the same state without a clock at all.
        """
        import time

        return int(time.time() * 1000)


# --------------------------------------------------------------------------------------
# The entry points every caller uses
# --------------------------------------------------------------------------------------


async def run_agent_loop_result(
    prompts: Sequence[Any],
    context: AgentContext,
    config: AgentLoopConfig,
    signal: Any,
    emit: EventSink,
) -> LoopResult:
    """以新提示词开一轮，返回**结构化结果**（含本次新增消息）。

    ``run_agent_loop`` 保留列表返回契约，是同一内核的薄包装；两者的事件顺序与消息
    集合完全一致。需要知道「为什么结束」的调用方用本入口，不靠最后一条消息的 role、
    事件数量或文本去猜。
    """
    return await Runner(config, emit).run(prompts, context, signal)


async def run_agent_loop(
    prompts: Sequence[Any],
    context: AgentContext,
    config: AgentLoopConfig,
    signal: Any,
    emit: EventSink,
) -> list[Any]:
    """以新提示词开一轮。返回**本次新增**消息（含提示词）——兼容入口。

    旧契约只能用列表回答「结束了、这是新消息」。一次等待外部决定的暂停不是失败也不是
    完成，列表表达不了，所以这里抛 :class:`LoopSuspended` 并把可读的
    :class:`LoopResult` 带出去；调用方要处理暂停就必须走 ``run_agent_loop_result``。
    """
    result = await run_agent_loop_result(prompts, context, config, signal, emit)
    if result.status is RunStatus.SUSPENDED:
        raise LoopSuspended(result)
    return result.list_messages()


async def run_agent_loop_continue_result(
    context: AgentContext,
    config: AgentLoopConfig,
    signal: Any,
    emit: EventSink,
) -> LoopResult:
    """从既有 transcript 续跑，返回结构化结果。拒绝条件是硬守卫。"""
    validate_max_turns(config.max_turns)
    _resolve_stream_fn(config)
    if not context.messages:
        raise ValueError("Cannot continue: no messages in context")
    if getattr(context.messages[-1], "role", None) == "assistant":
        raise ValueError("Cannot continue from message role: assistant")
    return await Runner(config, emit).run_continue(context, signal)


async def run_agent_loop_continue(
    context: AgentContext,
    config: AgentLoopConfig,
    signal: Any,
    emit: EventSink,
) -> list[Any]:
    """从既有 transcript 续跑（重试用）。只返回新增消息——兼容入口。

    与 ``run_agent_loop`` 同样抛 :class:`LoopSuspended`，而不是把一次暂停静默当成完成。
    """
    result = await run_agent_loop_continue_result(context, config, signal, emit)
    if result.status is RunStatus.SUSPENDED:
        raise LoopSuspended(result)
    return result.list_messages()
