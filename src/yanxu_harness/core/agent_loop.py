"""0.6.0 P03：唯一的模型循环。

逐段对照 `agent-loop.ts` @ c7cdb46（行号注在各处）。计划 §94 的流水线在这里成形：

    AgentMessage[] → transform_context → convert_to_llm → normalize transcript
                   → StreamFn → AssistantMessage → tools → 下一轮

三条不变量（都是本文件被测的重点）：
* transform 只作用于**这次请求**，不回写持久 transcript（§94）；
* `error` / `aborted` 终态立刻收尾，且 `turn_end` 的 tool_results 为空（§221-225）；
* `length` 截断时整批工具都不执行（§232-239、§427-459）。

与上游的两处刻意差异：`max_turns` 上限（§115 要求轮数有界，上游没有）；以及 core
不调 `setDefaultStreamFn` 那种全局可变态（§28）—— `stream_fn` 必须由 config 显式给出。
"""

from __future__ import annotations

import inspect
import time
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable, Mapping, Sequence

from .events import (
    AgentEnd,
    AgentStart,
    MessageEnd,
    MessageStart,
    MessageUpdate,
    ToolExecutionEnd,
    ToolExecutionStart,
    TurnEnd,
    TurnStart,
)
from .messages import (
    get_current_tools,
    get_tool_state_changes,
    normalize_context,
)
from .tool_execution import execute_tool_calls, error_result, tool_call_blocks
from .types import (
    AgentContext,
    AssistantMessage,
    StopReason,
    SystemMessage,
    ToolResultMessage,
    TranscriptContext,
)

__all__ = ["AgentLoopConfig", "PrepareNextTurnContext", "run_agent_loop", "run_agent_loop_continue"]

EventSink = Callable[[Any], Awaitable[None]]


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
    persist_transformed_context: bool = False
    project_context: Callable[..., Any] | None = None
    before_tool_call: Callable[..., Any] | None = None
    after_tool_call: Callable[..., Any] | None = None
    should_stop_after_turn: Callable[[TurnEndContext], Any] | None = None
    prepare_next_turn: Callable[[PrepareNextTurnContext], Any] | None = None
    on_tool_batch_complete: Callable[[AssistantMessage, Sequence[ToolResultMessage]], Any] | None = None
    get_steering_messages: Callable[[], Any] | None = None
    get_follow_ups: Callable[[], Any] | None = None
    tool_execution: str = "parallel"
    argument_validator: Callable[[Any, dict], dict] | None = None
    max_turns: int | None = None
    reasoning: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def validate_max_turns(max_turns: int | None) -> None:
    """Reject ambiguous request budgets before the loop emits events or dispatches."""
    if max_turns is not None and (type(max_turns) is not int or max_turns <= 0):
        raise ValueError("max_turns must be None or a positive integer")


def _resolve_stream_fn(config: AgentLoopConfig) -> Callable[..., Any]:
    """core 必须有显式注入点；不提供「全局默认流」的兜底（计划 §28 登记的差异）。"""
    if config.stream_fn is None:
        raise ValueError("AgentLoopConfig.stream_fn 必须显式提供，core 不接受跨调用共享的全局默认流")
    return config.stream_fn


async def _maybe(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def _poll(callback: Callable[[], Any] | None) -> list[Any]:
    if callback is None:
        return []
    return list(await _maybe(callback()) or [])


def _declare_tool_changes(context: AgentContext, pending: list[Any]) -> list[Any]:
    """agent-loop.ts:291-321：把「可执行工具集 vs transcript 已声明工具集」的差写成 system 消息。

    待插入的 system 消息带工具字段时，它表达的是**意图**：用「已提交历史 vs 可执行集」的差
    替换它，保证重放结果恒等于 `context.tools`。
    """
    system_index = next((i for i in range(len(pending) - 1, -1, -1) if getattr(pending[i], "role", None) == "system"), None)
    executable = context.tools or []
    if system_index is not None:
        pending_system = pending[system_index]
        baseline = list(pending)
        baseline[system_index] = _with_tool_changes(pending_system, [], [])
        committed = get_current_tools([*context.messages, *baseline])
        changes = get_tool_state_changes(committed, executable)
        unchanged = not changes.tools_added and not changes.tools_removed
        if unchanged and not (pending_system.tools_added or pending_system.tools_removed):
            return pending
        replaced = list(pending)
        replaced[system_index] = _with_tool_changes(pending_system, changes.tools_added, changes.tools_removed)
        return replaced

    committed = get_current_tools([*context.messages, *pending])
    changes = get_tool_state_changes(committed, executable)
    if not changes.tools_added and not changes.tools_removed:
        return pending
    update = _with_tool_changes(SystemMessage(content="", timestamp=int(time.time() * 1000)), changes.tools_added, changes.tools_removed)
    insert_at = next((i for i, m in enumerate(pending) if getattr(m, "role", None) != "system"), len(pending))
    return [*pending[:insert_at], update, *pending[insert_at:]]


def _with_tool_changes(message: SystemMessage, added: Sequence[Any], removed: Sequence[Any]) -> SystemMessage:
    """agent-loop.ts:326-333：空列表不留字段，避免回放时误报变更。"""
    return replace(message, tools_added=list(added) or None, tools_removed=list(removed) or None)


async def _stream_assistant_response(
    context: AgentContext,
    active_messages: Sequence[Any],
    config: AgentLoopConfig,
    signal: Any,
    emit: EventSink,
    stream_fn: Callable[..., Any],
) -> AssistantMessage:
    messages: Sequence[Any] = context.messages
    if config.transform_context is not None:
        transformed = list(await _maybe(config.transform_context(list(context.messages), signal)))
        if config.persist_transformed_context:
            context.messages[:] = transformed
            messages = context.messages
        else:
            messages = transformed

    if config.convert_to_llm is not None:
        llm_messages = await _maybe(config.convert_to_llm(list(messages)))
    else:
        from .messages import to_llm_messages

        llm_messages = to_llm_messages(messages)

    llm_context = normalize_context(AgentContext(messages=list(llm_messages)))

    resolved_key = config.api_key
    if config.get_api_key is not None:
        resolved_key = await _maybe(config.get_api_key(getattr(config.model, "provider", ""))) or config.api_key

    options = {"api_key": resolved_key, "signal": signal, "reasoning": config.reasoning, **config.extra}
    if config.project_context is not None:
        async def project_for_budget(report: Mapping[str, Any]) -> Sequence[Any] | None:
            projected = await _maybe(
                config.project_context(
                    list(context.messages), list(active_messages), config.model, report, signal
                )
            )
            setattr(
                project_for_budget,
                "checkpoint_version",
                getattr(config.project_context, "checkpoint_version", None),
            )
            if projected is None:
                return None
            context.messages[:] = list(projected)
            if config.convert_to_llm is not None:
                projected_llm = await _maybe(config.convert_to_llm(list(context.messages)))
            else:
                from .messages import to_llm_messages

                projected_llm = to_llm_messages(context.messages)
            return list(projected_llm)

        setattr(
            project_for_budget,
            "checkpoint_version",
            getattr(config.project_context, "checkpoint_version", None),
        )
        # Consumed by the host's final request-budget gate and removed before
        # payload construction or Provider dispatch.
        options["_project_context_for_budget"] = project_for_budget
    response = await _maybe(stream_fn(config.model, llm_context, options))

    partial: AssistantMessage | None = None
    added_partial = False
    async for frame in response:
        kind = frame.get("type") if isinstance(frame, dict) else getattr(frame, "type", None)
        if kind == "start":
            partial = frame["partial"]
            context.messages.append(partial)
            added_partial = True
            await emit(MessageStart(message=_copy(partial)))
        elif kind in {"text_start", "text_delta", "text_end", "thinking_start", "thinking_delta",
                      "thinking_end", "toolcall_start", "toolcall_delta", "toolcall_end"}:
            if partial is not None:
                partial = frame["partial"]
                context.messages[-1] = partial
                await emit(MessageUpdate(message=_copy(partial), assistant_message_event=frame))
        elif kind in {"done", "error"}:
            final = await response.result()
            _place_final(context, final, added_partial)
            if not added_partial:
                await emit(MessageStart(message=_copy(final)))
            await emit(MessageEnd(message=final))
            return final

    final = await response.result()
    if added_partial:
        context.messages[-1] = final
    else:
        context.messages.append(final)
        await emit(MessageStart(message=_copy(final)))
    await emit(MessageEnd(message=final))
    return final


def _place_final(context: AgentContext, final: AssistantMessage, added_partial: bool) -> None:
    if added_partial:
        context.messages[-1] = final
    else:
        context.messages.append(final)


def _copy(message: Any) -> Any:
    """上游 emit 的是浅拷贝快照（`{ ...partialMessage }`），避免消费者持有活的 partial。"""
    if inspect.isawaitable(message):
        raise TypeError("不能把 awaitable 当消息发出去")
    try:
        return replace(message)
    except TypeError:  # 非 dataclass（自定义消息）按引用发出
        return message


async def _fail_truncated_batch(calls: Sequence[Any], emit: EventSink) -> Any:
    """agent-loop.ts:427-459：被输出上限截断的响应里，每个调用都可能带残缺参数。

    流式参数有兜底 JSON 解析，所以「参数能解析」不代表「参数完整」——一律不执行，
    并给每个调用回一条错误结果，让模型重发。
    """
    from .tool_execution import ToolBatch

    messages: list[ToolResultMessage] = []
    for call in calls:
        await emit(ToolExecutionStart(tool_call_id=call.id, tool_name=call.name, args=call.arguments))
        result = error_result(
            f'Tool call "{call.name}" was not executed: the response hit the output token limit, '
            "so its arguments may be truncated. Re-issue the tool call with complete arguments."
        )
        await emit(ToolExecutionEnd(tool_call_id=call.id, tool_name=call.name, result=result, is_error=True))
        message = ToolResultMessage(tool_call_id=call.id, tool_name=call.name, content=result["content"],
                                    details=result["details"], is_error=True,
                                    source_provenance=result.get("storyide_source_provenance"))
        await emit(MessageStart(message=message))
        await emit(MessageEnd(message=message))
        messages.append(message)
    return ToolBatch(messages=messages, terminate=False)


async def _run_loop(
    current_context: AgentContext,
    new_messages: list[Any],
    config: AgentLoopConfig,
    signal: Any,
    emit: EventSink,
    stream_fn: Callable[..., Any],
) -> None:
    # 调用方已经为第一个回合发过 turn_start，所以计数从 1 起。
    turns = 1
    last_completed: PrepareNextTurnContext | None = None
    pending = await _poll(config.get_steering_messages)

    while True:
        has_more_tool_calls = True
        while has_more_tool_calls or pending:
            prepared: list[Any] = []
            if last_completed is not None:
                if config.max_turns is not None and turns >= config.max_turns:
                    await emit(AgentEnd(messages=tuple(new_messages)))
                    return
                snapshot = await _maybe(config.prepare_next_turn(last_completed)) if config.prepare_next_turn else None
                if snapshot:
                    current_context = snapshot.get("context") or current_context
                    prepared = list(snapshot.get("messages") or [])
                    if snapshot.get("model") is not None:
                        config = replace(config, model=snapshot["model"])
                    if snapshot.get("thinking_level") is not None:
                        level = snapshot["thinking_level"]
                        config = replace(config, reasoning=None if level == "off" else level)
                if not pending:
                    pending = await _poll(config.get_steering_messages)
                await emit(TurnStart())
                turns += 1

            for message in _declare_tool_changes(current_context, [*prepared, *pending]):
                await emit(MessageStart(message=message))
                await emit(MessageEnd(message=message))
                current_context.messages.append(message)
                new_messages.append(message)
            pending = []

            message = await _stream_assistant_response(
                current_context, new_messages, config, signal, emit, stream_fn
            )
            new_messages.append(message)

            if message.stop_reason in (StopReason.ERROR, StopReason.ABORTED):
                await emit(TurnEnd(message=message, tool_results=()))
                await emit(AgentEnd(messages=tuple(new_messages)))
                return

            calls = tool_call_blocks(message)
            tool_results: list[ToolResultMessage] = []
            has_more_tool_calls = False
            if calls:
                if message.stop_reason is StopReason.LENGTH:
                    batch = await _fail_truncated_batch(calls, emit)
                else:
                    batch = await execute_tool_calls(current_context, message, config, signal, emit)
                tool_results.extend(batch.messages)
                has_more_tool_calls = not batch.terminate
                for result in tool_results:
                    current_context.messages.append(result)
                    new_messages.append(result)
                if config.on_tool_batch_complete is not None:
                    await _maybe(config.on_tool_batch_complete(message, tuple(tool_results)))

            await emit(TurnEnd(message=message, tool_results=tuple(tool_results)))
            last_completed = PrepareNextTurnContext(
                message=message, tool_results=tuple(tool_results), context=current_context, new_messages=new_messages
            )
            if config.should_stop_after_turn is not None and await _maybe(
                config.should_stop_after_turn(last_completed)
            ):
                await emit(AgentEnd(messages=tuple(new_messages)))
                return
            if config.max_turns is not None and turns >= config.max_turns:
                # The completed Nth request may settle its current tool batch, but
                # queued steering/follow-ups belong to a request that cannot run.
                await emit(AgentEnd(messages=tuple(new_messages)))
                return
            pending = await _poll(config.get_steering_messages)

        follow_ups = await _poll(config.get_follow_ups)
        if follow_ups:
            pending = list(follow_ups)
            continue
        break

    await emit(AgentEnd(messages=tuple(new_messages)))


async def run_agent_loop(
    prompts: Sequence[Any],
    context: AgentContext,
    config: AgentLoopConfig,
    signal: Any,
    emit: EventSink,
) -> list[Any]:
    """以新提示词开一轮。返回**本次新增**消息（含提示词）。"""
    validate_max_turns(config.max_turns)
    stream_fn = _resolve_stream_fn(config)
    initial = _declare_tool_changes(context, list(prompts))
    new_messages: list[Any] = list(initial)
    current = AgentContext(
        messages=[*context.messages, *initial],
        system_prompt=None,
        tools=list(context.tools or []),
    )

    await emit(AgentStart())
    await emit(TurnStart())
    for message in initial:
        await emit(MessageStart(message=message))
        await emit(MessageEnd(message=message))

    await _run_loop(current, new_messages, config, signal, emit, stream_fn)
    return new_messages


async def run_agent_loop_continue(
    context: AgentContext,
    config: AgentLoopConfig,
    signal: Any,
    emit: EventSink,
) -> list[Any]:
    """从既有 transcript 续跑（重试用）。只返回新增消息。

    拒绝条件是硬守卫（agent-loop.ts:76-82）：空历史或 assistant 尾部都没法续 ——
    「不重复追加原用户消息」这条也在这里，返回集合不含既有历史。
    """
    validate_max_turns(config.max_turns)
    stream_fn = _resolve_stream_fn(config)
    if not context.messages:
        raise ValueError("Cannot continue: no messages in context")
    if getattr(context.messages[-1], "role", None) == "assistant":
        raise ValueError("Cannot continue from message role: assistant")

    new_messages: list[Any] = []
    await emit(AgentStart())
    await emit(TurnStart())
    await _run_loop(context, new_messages, config, signal, emit, stream_fn)
    return new_messages
