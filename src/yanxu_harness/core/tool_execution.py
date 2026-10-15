"""0.6.0 P03–P04 的前半：工具批次执行器。

对照 `agent-loop.ts:461-820` @ c7cdb46（sequential / parallel 两条路径、prepare/execute/
finalize 三段、terminate 判定）。计划 §105–§108 的口径都在这里：

* 批次里有任一工具标 `sequential` 就整批串行；
* 并行时 start 与 preflight 按调用源顺序，`tool_execution_end` 按完成顺序，
  而工具结果消息按源顺序发；
* 只有整批 finalized 结果都 `terminate` 才算批次终止；
* 取消只在调用之间收口（已在跑的调用由宿主网关按 operation 证据判定，不在此层撤销）。

参数校验是**注入**的（计划 §78）：core 不引 TypeBox，也不允许「没有校验器就当成合法」——
所以 `argument_validator` 缺省时只放行已经带好参数的调用，任何异常都转成错误结果。
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Sequence

from .types import AgentContext, AssistantMessage, TextContent, ToolCall, ToolResultMessage, ToolSpec

__all__ = ["ToolBatch", "execute_tool_calls", "tool_call_blocks"]

EventSink = Callable[[Any], Awaitable[None]]
ArgumentValidator = Callable[[ToolSpec, dict], dict]


def tool_call_blocks(message: AssistantMessage) -> list[ToolCall]:
    return [block for block in message.content if getattr(block, "type", None) == "toolCall"]


@dataclass
class FinalizedToolCall:
    tool_call: ToolCall
    result: dict
    is_error: bool


@dataclass
class ToolBatch:
    messages: list[ToolResultMessage] = field(default_factory=list)
    terminate: bool = False


@dataclass
class _Prepared:
    tool: ToolSpec
    tool_call: ToolCall
    args: Any


def _as_result(raw: Any) -> dict:
    """工具可以返回 dict 或 AgentToolResult；None 一律归一，绝不让 None 进历史。"""
    if raw is None:
        return {"content": [], "details": None}
    if isinstance(raw, dict):
        merged = dict(raw)
        merged.setdefault("content", [])
        merged.setdefault("details", None)
        if merged["content"] is None:
            merged["content"] = []
        return merged
    return {
        "content": getattr(raw, "content", []) or [],
        "details": getattr(raw, "details", None),
        "usage": getattr(raw, "usage", None),
        "terminate": getattr(raw, "terminate", None),
    }


def error_result(message: str) -> dict:
    return {"content": [TextContent(text=message)], "details": {}, "terminate": None}


def _error_text(exc: BaseException) -> str:
    """agent-loop.ts:767：`error.message`，空消息才退化到类型名（Python 的 `str(exc)` 不含类型名）。"""
    return str(exc) or exc.__class__.__name__


def _cancelled(signal: Any) -> bool:
    return bool(signal is not None and getattr(signal, "cancelled", False))


async def _maybe(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _prepare_call(
    context: AgentContext,
    assistant_message: AssistantMessage,
    tool_call: ToolCall,
    config: Any,
    signal: Any,
) -> tuple[_Prepared | None, dict | None]:
    """返回 (可执行, 立即结论)。二者互斥，对应上游的 prepared / immediate。"""
    tool = next((t for t in (context.tools or []) if t.name == tool_call.name), None)
    if tool is None:
        return None, error_result(f"Tool {tool_call.name} not found")

    try:
        raw_args = tool_call.arguments
        if tool.prepare_arguments is not None:
            raw_args = await _maybe(tool.prepare_arguments(raw_args))
        validator: ArgumentValidator | None = getattr(config, "argument_validator", None)
        args = validator(tool, raw_args) if validator is not None else raw_args
        if config.before_tool_call is not None:
            before = await _maybe(
                config.before_tool_call(
                    {
                        "assistant_message": assistant_message,
                        "tool_call": tool_call,
                        "args": args,
                        "context": context,
                    },
                    signal,
                )
            )
            if _cancelled(signal):
                return None, error_result("Operation aborted")
            if before and before.get("block"):
                result = error_result(before.get("reason") or "Tool execution was blocked")
                if before.get("terminate") is True:
                    result["terminate"] = True
                return None, result
        if _cancelled(signal):
            return None, error_result("Operation aborted")
        return _Prepared(tool=tool, tool_call=tool_call, args=args), None
    except Exception as exc:  # 参数不合法/宿主拒绝：转成错误结果，不掀翻整批
        return None, error_result(_error_text(exc))


async def _execute_prepared(prepared: _Prepared, signal: Any, emit: EventSink) -> tuple[dict, bool]:
    """执行并投递 partial update；落定之后的 update 一律丢弃（types.ts:398-403）。

    `on_update` 与上游一样是**同步**回调：工具不需要 await 它，update 事件被排成任务，
    在 execute 落定时统一 await —— 这样「工具忘了 await」不会把事件丢掉。
    """
    pending_updates: list[Any] = []
    accepting = {"open": True}

    def on_update(partial: Any) -> None:
        if not accepting["open"]:
            return
        pending_updates.append(asyncio.ensure_future(emit(_update_event(prepared, partial))))

    if prepared.tool.execute is None:
        accepting["open"] = False
        return error_result(f"Tool {prepared.tool.name} has no execute"), True
    try:
        raw = await prepared.tool.execute(prepared.tool_call.id, prepared.args, signal, on_update)
        accepting["open"] = False
        await _drain(pending_updates)
        return _as_result(raw), False
    except asyncio.CancelledError:
        accepting["open"] = False
        await _drain(pending_updates)
        return error_result("Operation aborted"), True
    except Exception as exc:
        accepting["open"] = False
        await _drain(pending_updates)
        return error_result(_error_text(exc)), True


async def _drain(items: Sequence[Awaitable[None]]) -> None:
    if items:
        await asyncio.gather(*items)


def _update_event(prepared: _Prepared, partial: Any) -> Any:
    from .events import ToolExecutionUpdate

    return ToolExecutionUpdate(
        tool_call_id=prepared.tool_call.id,
        tool_name=prepared.tool_call.name,
        args=prepared.tool_call.arguments,
        partial_result=partial,
    )


async def _finalize(
    context: AgentContext,
    assistant_message: AssistantMessage,
    prepared: _Prepared,
    result: dict,
    is_error: bool,
    config: Any,
    signal: Any,
) -> FinalizedToolCall:
    if config.after_tool_call is None:
        return FinalizedToolCall(tool_call=prepared.tool_call, result=result, is_error=is_error)
    try:
        override = await _maybe(
            config.after_tool_call(
                {
                    "assistant_message": assistant_message,
                    "tool_call": prepared.tool_call,
                    "args": prepared.args,
                    "result": result,
                    "is_error": is_error,
                    "context": context,
                },
                signal,
            )
        )
    except Exception as exc:
        return FinalizedToolCall(tool_call=prepared.tool_call, result=error_result(_error_text(exc)), is_error=True)
    if not override:
        return FinalizedToolCall(tool_call=prepared.tool_call, result=result, is_error=is_error)
    merged = dict(result)
    for key in ("content", "details", "usage", "terminate", "storyide_source_provenance"):
        if override.get(key) is not None:
            merged[key] = override[key]
    return FinalizedToolCall(
        tool_call=prepared.tool_call,
        result=merged,
        is_error=bool(override.get("is_error", is_error)),
    )


async def _emit_end(finalized: FinalizedToolCall, emit: EventSink) -> None:
    from .events import ToolExecutionEnd

    await emit(
        ToolExecutionEnd(
            tool_call_id=finalized.tool_call.id,
            tool_name=finalized.tool_call.name,
            result=finalized.result,
            is_error=finalized.is_error,
        )
    )


def _result_message(finalized: FinalizedToolCall) -> ToolResultMessage:
    return ToolResultMessage(
        tool_call_id=finalized.tool_call.id,
        tool_name=finalized.tool_call.name,
        content=finalized.result.get("content") or [],
        details=finalized.result.get("details"),
        usage=finalized.result.get("usage"),
        is_error=finalized.is_error,
        source_provenance=finalized.result.get("storyide_source_provenance"),
    )


async def _emit_result_message(message: ToolResultMessage, emit: EventSink) -> None:
    from .events import MessageEnd, MessageStart

    await emit(MessageStart(message=message))
    await emit(MessageEnd(message=message))


def _starts(tool_call: ToolCall) -> Any:
    from .events import ToolExecutionStart

    return ToolExecutionStart(tool_call_id=tool_call.id, tool_name=tool_call.name, args=tool_call.arguments)


def _should_terminate(finalized: Sequence[FinalizedToolCall]) -> bool:
    """agent-loop.ts:644-646：非空且**每一个**都 terminate。"""
    return len(finalized) > 0 and all(f.result.get("terminate") is True for f in finalized)


async def execute_tool_calls(
    context: AgentContext,
    assistant_message: AssistantMessage,
    config: Any,
    signal: Any,
    emit: EventSink,
) -> ToolBatch:
    calls = tool_call_blocks(assistant_message)
    by_name = {tool.name: tool for tool in (context.tools or [])}
    batch_is_sequential = getattr(config, "tool_execution", "parallel") == "sequential" or any(
        by_name.get(call.name) is not None and by_name[call.name].execution_mode == "sequential" for call in calls
    )
    if batch_is_sequential:
        return await _execute_sequential(context, assistant_message, calls, config, signal, emit)
    return await _execute_parallel(context, assistant_message, calls, config, signal, emit)


async def _execute_sequential(
    context: AgentContext,
    assistant_message: AssistantMessage,
    calls: list[ToolCall],
    config: Any,
    signal: Any,
    emit: EventSink,
) -> ToolBatch:
    finalized: list[FinalizedToolCall] = []
    messages: list[ToolResultMessage] = []
    for tool_call in calls:
        await emit(_starts(tool_call))
        prepared, immediate = await _prepare_call(context, assistant_message, tool_call, config, signal)
        if prepared is None:
            outcome = FinalizedToolCall(tool_call=tool_call, result=immediate or error_result("blocked"), is_error=True)
        else:
            result, is_error = await _execute_prepared(prepared, signal, emit)
            outcome = await _finalize(context, assistant_message, prepared, result, is_error, config, signal)
        await _emit_end(outcome, emit)
        message = _result_message(outcome)
        await _emit_result_message(message, emit)
        finalized.append(outcome)
        messages.append(message)
        if _cancelled(signal):
            break
    return ToolBatch(messages=messages, terminate=_should_terminate(finalized))


async def _execute_parallel(
    context: AgentContext,
    assistant_message: AssistantMessage,
    calls: list[ToolCall],
    config: Any,
    signal: Any,
    emit: EventSink,
) -> ToolBatch:
    """start / preflight 按源顺序，`tool_execution_end` 按完成顺序，结果消息按源顺序。"""
    entries: list[Any] = []
    for tool_call in calls:
        await emit(_starts(tool_call))
        prepared, immediate = await _prepare_call(context, assistant_message, tool_call, config, signal)
        if prepared is None:
            outcome = FinalizedToolCall(tool_call=tool_call, result=immediate or error_result("blocked"), is_error=True)
            await _emit_end(outcome, emit)
            entries.append(_already(outcome))
            if _cancelled(signal):
                break
            continue

        async def run_one(prepared: _Prepared = prepared) -> FinalizedToolCall:
            if _cancelled(signal):
                outcome = FinalizedToolCall(tool_call=prepared.tool_call, result=error_result("Operation aborted"), is_error=True)
                await _emit_end(outcome, emit)
                return outcome
            result, is_error = await _execute_prepared(prepared, signal, emit)
            outcome = await _finalize(context, assistant_message, prepared, result, is_error, config, signal)
            await _emit_end(outcome, emit)
            return outcome

        entries.append(run_one)
        if _cancelled(signal):
            break

    ordered: list[FinalizedToolCall] = list(
        await asyncio.gather(*[entry() if callable(entry) else entry for entry in entries])
    )
    messages: list[ToolResultMessage] = []
    for outcome in ordered:
        message = _result_message(outcome)
        await _emit_result_message(message, emit)
        messages.append(message)
    return ToolBatch(messages=messages, terminate=_should_terminate(ordered))


async def _already(outcome: FinalizedToolCall) -> FinalizedToolCall:
    return outcome
