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
import contextlib
import inspect
from collections import deque
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable, Mapping, Sequence

from ..errors import ContractError
from .outcome import CallOutcome
from .types import AgentContext, AssistantMessage, TextContent, ToolCall, ToolResultMessage, ToolSpec

__all__ = [
    "FinalizedToolCall",
    "PrecomputedToolBatch",
    "ToolBatch",
    "ToolBatchSession",
    "ToolCallObservation",
    "execute_tool_calls",
    "observation_of",
    "tool_call_blocks",
]

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
    #: Whether this tool's ``execute`` accepts the batch reference. Decided by signature
    #: inspection at preparation time, so a tool written before the parameter existed is
    #: called exactly as before.
    accepts_batch_ref: bool = False


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
        return (
            _Prepared(
                tool=tool,
                tool_call=tool_call,
                args=args,
                accepts_batch_ref=_accepts_batch_reference(tool.execute),
            ),
            None,
        )
    except Exception as exc:  # 参数不合法/宿主拒绝：转成错误结果，不掀翻整批
        return None, error_result(_error_text(exc))


def _accepts_batch_reference(execute: Any) -> bool:
    """True when a tool's ``execute`` can receive the batch reference.

    A positional-name check plus ``**kwargs`` support, decided without calling anything:
    an old tool keeps the old four-argument call, and nothing here catches a ``TypeError``
    from inside a handler (which would be a real failure, not a signature mismatch).
    """
    if execute is None:
        return False
    try:
        signature = inspect.signature(execute)
    except (TypeError, ValueError):
        return False
    if "model_action_id" in signature.parameters:
        return True
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )


async def _execute_prepared(
    prepared: _Prepared, signal: Any, emit: EventSink, batch_ref: str = ""
) -> tuple[dict, bool]:
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
        extra = (
            {"model_action_id": batch_ref}
            if prepared.accepts_batch_ref and batch_ref
            else {}
        )
        raw = await prepared.tool.execute(
            prepared.tool_call.id, prepared.args, signal, on_update, **extra
        )
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


def _result_record(
    finalized: FinalizedToolCall, message: ToolResultMessage | None = None
) -> Mapping[str, Any]:
    from .types import message_to_json

    return message_to_json(message if message is not None else _result_message(finalized))


def _starts(tool_call: ToolCall) -> Any:
    from .events import ToolExecutionStart

    return ToolExecutionStart(tool_call_id=tool_call.id, tool_name=tool_call.name, args=tool_call.arguments)


def _should_terminate(finalized: Sequence[FinalizedToolCall]) -> bool:
    """agent-loop.ts:644-646：非空且**每一个**都 terminate。"""
    return len(finalized) > 0 and all(f.result.get("terminate") is True for f in finalized)


# --------------------------------------------------------------------------------------
# The batch session: bounded execution with one observation at a time
# --------------------------------------------------------------------------------------



# --------------------------------------------------------------------------------------
# The batch session: bounded execution with one observation at a time
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolCallObservation:
    """What the executor learned about one declared call, as plain data.

    This is the value the driver hands to the reducer (wrapped in a
    ``ToolCallObserved`` input). It is deliberately *not* a verdict: the same shape
    carries ``executed``, ``awaiting_approval``, ``unknown``, ``denied``, ``failed``
    and the explicit "not executed because the run was cancelled" answer. What it does
    guarantee is that exactly one of these exists for every declared call before the
    batch is finished, so the model never receives a turn whose tool calls are only
    half answered.
    """

    call_id: str
    tool_name: str
    declared_index: int
    outcome: str
    status: str = ""
    effect_state: str | None = None
    operation_id: str = ""
    tool_version: str = ""
    result: Mapping[str, Any] | None = None
    receipt: Mapping[str, Any] | None = None
    terminate: bool = False
    reason: str = ""
    observation_id: str = ""
    observed_digest: str = ""
    aborted: bool = False

    def digest(self) -> str:
        return _observation_digest(self)


def _observation_digest(observation: ToolCallObservation) -> str:
    """A content digest: same call, same facts → same digest.

    Computed over canonical JSON of everything a reader could act on, so a repeated
    delivery of the same observation is provably the same fact and a different one is
    provably a conflict.
    """
    import hashlib

    from ..versioning import canonical_json

    payload = {
        "call_id": observation.call_id,
        "tool_name": observation.tool_name,
        "outcome": observation.outcome,
        "status": observation.status,
        "effect_state": observation.effect_state,
        "operation_id": observation.operation_id,
        "tool_version": observation.tool_version,
        "result": observation.result,
        "receipt": observation.receipt,
        "terminate": observation.terminate,
        "reason": observation.reason,
        "aborted": observation.aborted,
    }
    try:
        text = canonical_json(payload)
    except TypeError:
        text = repr(payload)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _receipt_reference(result: Mapping[str, Any], operation_id: str) -> Mapping[str, Any] | None:
    """A small reference to the authoritative ledger record, never a copy of it."""
    details = result.get("details")
    if not isinstance(details, Mapping):
        return None
    reference: dict[str, Any] = {"operation_id": operation_id or str(details.get("operation_id") or "")}
    for key in ("effect_state", "external_id", "args_hash", "tool_version"):
        if details.get(key) is not None:
            reference[key] = details[key]
    return reference or None


@dataclass
class _Slot:
    """One declared call's state inside the session (in-memory only)."""

    index: int
    tool_call: ToolCall
    prepared: _Prepared | None = None
    finalized: FinalizedToolCall | None = None
    observation: ToolCallObservation | None = None
    message: ToolResultMessage | None = None
    preflighted: bool = False


def _batch_is_sequential(context: AgentContext, calls: Sequence[ToolCall], config: Any) -> bool:
    by_name = {tool.name: tool for tool in (context.tools or [])}
    return getattr(config, "tool_execution", "parallel") == "sequential" or any(
        by_name.get(call.name) is not None
        and by_name[call.name].execution_mode == "sequential"
        for call in calls
    )


class ToolBatchSession:
    """One batch, executed under a bound, delivering observations one at a time.

    The session owns the tasks, the bounded queue and the concurrency limit; none of
    that is execution state and none of it is committed. What the driver commits is the
    *observation*, after the reducer accepted it and the host's commit port confirmed it.

    Bounds, stated because "bounded" is the whole reason this exists:

    * at most ``limit`` calls execute at once (``limit`` is 1 for a sequential batch);
    * the observation queue holds at most ``capacity`` un-consumed observations, so a
      slow committer applies backpressure instead of making the driver buffer without
      bound;
    * at most ``1 + min(limit, declared calls)`` tasks exist — the batch declaration is
      itself bounded by the request boundary, and no task is created per call while a
      semaphore is already limiting them.
    """

    def __init__(
        self,
        *,
        session_id: str,
        calls: Sequence[ToolCall],
        sequential: bool,
        limit: int | None,
        capacity: int | None = None,
        model_action_id: str = "",
    ) -> None:
        self.session_id = session_id
        #: The model action this batch belongs to; travels into the gateway invocation so
        #: the ledger binds the call to the batch that declared it.
        self.model_action_id = model_action_id
        self.sequential = bool(sequential)
        declared = list(calls)
        self.calls: tuple[ToolCall, ...] = tuple(declared)
        resolved = 1 if self.sequential else (len(declared) or 1 if limit is None else limit)
        self._limit = max(1, min(resolved, len(declared) or 1))
        self._capacity = max(1, capacity if capacity is not None else self._limit)
        self._slots: list[_Slot] = [_Slot(index=index, tool_call=call) for index, call in enumerate(declared)]
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=self._capacity)
        self._scheduler: asyncio.Task | None = None
        self._closed = False
        self._received = 0
        self._exhausted = False

    # -- construction ---------------------------------------------------

    @classmethod
    async def open(
        cls,
        context: AgentContext,
        assistant_message: AssistantMessage,
        config: Any,
        signal: Any,
        emit: EventSink,
        *,
        session_id: str = "",
        model_action_id: str = "",
        calls: Sequence[ToolCall] | None = None,
        limit: int | None = None,
    ) -> "ToolBatchSession":
        """Create a session and start it.

        A parallel batch does all of its preflight first (start events, argument
        preparation and ``before_tool_call`` are sequential and in source order, exactly
        as the previous loop did) and only then starts executing. A sequential batch
        prepares each call immediately before running it, which is the previous loop's
        interleaving.
        """
        declared = list(calls) if calls is not None else tool_call_blocks(assistant_message)
        if not declared:
            raise ContractError("a tool batch session needs at least one declared call")
        resolved_limit = limit if limit is not None else getattr(config, "max_concurrent_tools", None)
        session = cls(
            session_id=session_id or "batch",
            model_action_id=model_action_id,
            calls=declared,
            sequential=_batch_is_sequential(context, declared, config),
            limit=resolved_limit,
        )
        if not session.sequential:
            for slot in session._slots:
                await session._preflight(slot, context, assistant_message, config, signal, emit)
        session._scheduler = asyncio.ensure_future(
            session._run(context, assistant_message, config, signal, emit)
        )
        return session

    # -- preflight ------------------------------------------------------

    async def _preflight(
        self,
        slot: _Slot,
        context: AgentContext,
        assistant_message: AssistantMessage,
        config: Any,
        signal: Any,
        emit: EventSink,
    ) -> None:
        if slot.preflighted:
            return
        slot.preflighted = True
        await emit(_starts(slot.tool_call))
        prepared, immediate = await _prepare_call(
            context, assistant_message, slot.tool_call, config, signal
        )
        if prepared is None:
            outcome = FinalizedToolCall(
                tool_call=slot.tool_call,
                result=immediate or error_result("blocked"),
                is_error=True,
            )
            slot.finalized = outcome
            await _emit_end(outcome, emit)
        else:
            slot.prepared = prepared

    # -- the scheduler --------------------------------------------------

    async def _run(
        self,
        context: AgentContext,
        assistant_message: AssistantMessage,
        config: Any,
        signal: Any,
        emit: EventSink,
    ) -> None:
        """Drive the batch to completion, enqueueing one observation per call."""
        try:
            if self.sequential:
                await self._run_sequential(context, assistant_message, config, signal, emit)
            else:
                await self._run_parallel(context, assistant_message, config, signal, emit)
        finally:
            await self._queue.put(None)

    async def _run_sequential(
        self,
        context: AgentContext,
        assistant_message: AssistantMessage,
        config: Any,
        signal: Any,
        emit: EventSink,
    ) -> None:
        for slot in self._slots:
            # Cancellation is checked *between* calls, after the current one has been
            # answered — the same point the previous loop checked it. A call whose
            # preflight observes the cancellation still gets its explicit aborted result.
            await self._preflight(slot, context, assistant_message, config, signal, emit)
            if slot.finalized is None:
                slot.finalized = await self._execute_slot(
                    slot, context, assistant_message, config, signal, emit
                )
            await self._enqueue(slot)
            if _cancelled(signal):
                break
        # A sequential batch that was cancelled between calls stops there: the calls it
        # never reached stay unstarted rather than being reported as having run. That is
        # the previous loop's behaviour, and it is recorded by ``ToolBatchDrained`` with
        # ``complete=False`` — which is not the same claim as "this call did not happen".

    async def _run_parallel(
        self,
        context: AgentContext,
        assistant_message: AssistantMessage,
        config: Any,
        signal: Any,
        emit: EventSink,
    ) -> None:
        pending = deque(slot for slot in self._slots if slot.finalized is None)
        running: set[asyncio.Task] = set()
        while pending or running:
            while pending and len(running) < self._limit:
                slot = pending.popleft()
                running.add(
                    asyncio.ensure_future(
                        self._execute_and_enqueue(
                            slot, context, assistant_message, config, signal, emit
                        )
                    )
                )
            if running:
                _done, running = await asyncio.wait(
                    running, return_when=asyncio.FIRST_COMPLETED
                )
        # Preflight-failed calls and the calls cancelled before they started are enqueued
        # after the running ones have all reported; each still gets exactly one answer.
        for slot in self._slots:
            if slot.observation is None:
                if slot.finalized is None:
                    slot.finalized = FinalizedToolCall(
                        tool_call=slot.tool_call,
                        result=error_result("Operation aborted"),
                        is_error=True,
                    )
                await self._enqueue(slot)

    async def _execute_and_enqueue(
        self,
        slot: _Slot,
        context: AgentContext,
        assistant_message: AssistantMessage,
        config: Any,
        signal: Any,
        emit: EventSink,
    ) -> None:
        if _cancelled(signal):
            slot.finalized = FinalizedToolCall(
                tool_call=slot.tool_call,
                result=error_result("Operation aborted"),
                is_error=True,
            )
            # The start event was already emitted for every call of a parallel batch
            # during preflight, so this one needs its end too: SSE consumers pair them.
            await _emit_end(slot.finalized, emit)
        else:
            slot.finalized = await self._execute_slot(
                slot, context, assistant_message, config, signal, emit
            )
        await self._enqueue(slot)

    async def _execute_slot(
        self,
        slot: _Slot,
        context: AgentContext,
        assistant_message: AssistantMessage,
        config: Any,
        signal: Any,
        emit: EventSink,
    ) -> FinalizedToolCall:
        prepared = slot.prepared
        if prepared is None:
            # Preflight already emitted this call's end event (it produced an immediate
            # conclusion) or never started it at all; either way there is nothing to run.
            return slot.finalized or FinalizedToolCall(
                tool_call=slot.tool_call,
                result=error_result("Operation aborted"),
                is_error=True,
            )
        result, is_error = await _execute_prepared(
            prepared, signal, emit, self.model_action_id
        )
        outcome = await _finalize(
            context, assistant_message, prepared, result, is_error, config, signal
        )
        await _emit_end(outcome, emit)
        return outcome

    async def _enqueue(self, slot: _Slot) -> None:
        if slot.observation is not None:
            return
        if slot.finalized is None:
            slot.finalized = FinalizedToolCall(
                tool_call=slot.tool_call,
                result=error_result("Operation aborted"),
                is_error=True,
            )
        # One message object, shared by the committed record and the transcript: building
        # two would give the same call two timestamps and turn a routine settlement into a
        # contradiction the reducer correctly refuses.
        slot.message = _result_message(slot.finalized)
        observation = observation_of(slot.index, slot.finalized, slot.message)
        slot.observation = observation
        await self._queue.put((slot.index, observation))

    @staticmethod
    def _observation_of(index: int, outcome: FinalizedToolCall) -> ToolCallObservation:
        return observation_of(index, outcome)

    # -- consumption ----------------------------------------------------

    async def next_observation(self) -> ToolCallObservation:
        """Return the next observation, waiting for it if necessary.

        Every declared call produces exactly one observation, so a caller that keeps
        asking until :attr:`drained` is true always has a complete batch. Asking after
        that is a driver bug, not a pause, so it is refused.
        """
        item = await self._queue.get()
        if item is None:
            # Put the sentinel back so a later ask returns immediately rather than
            # blocking forever on a session that will produce nothing more.
            self._queue.put_nowait(None)
            self._exhausted = True
            return None
        _index, observation = item
        self._received += 1
        return observation

    @property
    def received(self) -> int:
        return self._received

    @property
    def complete(self) -> bool:
        """True when every declared call produced an observation."""
        return self._received >= len(self._slots)

    @property
    def drained(self) -> bool:
        """True once the session has told the consumer that nothing more will come."""
        return self._exhausted

    @property
    def terminate(self) -> bool:
        return bool(self._slots) and all(
            slot.observation is not None and slot.observation.terminate for slot in self._slots
        )

    def message_for(self, call_id: str) -> ToolResultMessage | None:
        slot = next((item for item in self._slots if item.tool_call.id == call_id), None)
        return None if slot is None else slot.message

    def messages_in_order(self) -> list[ToolResultMessage]:
        return [slot.message for slot in self._slots if slot.message is not None]

    async def aclose(self) -> None:
        """Stop the session and every task it owns. Idempotent."""
        if self._closed:
            return
        self._closed = True
        scheduler = self._scheduler
        self._scheduler = None
        if scheduler is None:
            return
        if not scheduler.done():
            scheduler.cancel()
        with contextlib.suppress(BaseException):
            await asyncio.gather(scheduler, return_exceptions=True)

    def __repr__(self) -> str:
        return (
            f"ToolBatchSession(session_id={self.session_id!r}, calls={len(self._slots)}, "
            f"sequential={self.sequential}, limit={self._limit}, capacity={self._capacity})"
        )


def observation_of(
    index: int, outcome: FinalizedToolCall, message: ToolResultMessage | None = None
) -> ToolCallObservation:
    """Build the one observation of one finalized call.

    Shared by the incremental session, the eager compatibility wrapper and the
    truncated-response path, so all three describe a call in exactly the same shape.
    ``message`` is the model-visible message the record is derived from; passing the
    session's own object (rather than building a second one) is what keeps the committed
    record and the transcript message byte-identical.
    """
    details = outcome.result.get("details")
    details = details if isinstance(details, Mapping) else {}
    raw_status = details.get("status")
    from .outcome import classify_status

    classification = (
        classify_status(str(raw_status)) if raw_status is not None else CallOutcome.SETTLED
    )
    observation = ToolCallObservation(
        call_id=outcome.tool_call.id,
        tool_name=outcome.tool_call.name,
        declared_index=index,
        outcome=classification.value,
        status=str(raw_status or ""),
        effect_state=(
            str(details["effect_state"]) if details.get("effect_state") is not None else None
        ),
        operation_id=str(details.get("operation_id") or ""),
        tool_version=str(details.get("tool_version") or ""),
        result=_result_record(outcome, message),
        receipt=_receipt_reference(outcome.result, str(details.get("operation_id") or "")),
        terminate=outcome.result.get("terminate") is True,
        reason=str(details.get("reason") or ""),
    )
    observation = replace(observation, observed_digest=_observation_digest(observation))
    return replace(observation, observation_id=_observation_id(observation))


def _observation_id(observation: ToolCallObservation) -> str:
    return f"{observation.call_id}:{observation.declared_index}:observed"


class PrecomputedToolBatch:
    """A batch whose observations are already known (a truncated response).

    It offers the same consumption surface as :class:`ToolBatchSession` so the driver
    has one code path for "ask the session for the next observation", without a second
    settlement implementation for the case where nothing may run.
    """

    def __init__(
        self,
        *,
        session_id: str,
        observations: Sequence[ToolCallObservation],
        messages: Sequence[ToolResultMessage],
    ) -> None:
        self.session_id = session_id
        self._observations = list(observations)
        self._messages = list(messages)
        self._index = 0

    @property
    def complete(self) -> bool:
        return True

    @property
    def drained(self) -> bool:
        return self._index >= len(self._observations)

    @property
    def terminate(self) -> bool:
        return bool(self._observations) and all(item.terminate for item in self._observations)

    async def next_observation(self) -> ToolCallObservation | None:
        if self.drained:
            return None
        observation = self._observations[self._index]
        self._index += 1
        return observation

    def messages_in_order(self) -> list[ToolResultMessage]:
        return list(self._messages)

    def message_for(self, call_id: str) -> ToolResultMessage | None:
        return next(
            (message for message in self._messages if message.tool_call_id == call_id), None
        )

    async def aclose(self) -> None:
        return None


async def execute_tool_calls(
    context: AgentContext,
    assistant_message: AssistantMessage,
    config: Any,
    signal: Any,
    emit: EventSink,
) -> ToolBatch:
    """Compatibility wrapper: run one batch and return its result messages.

    This is the same execution engine the Runner drives incrementally — one session,
    one scheduler and one observation per call — drained eagerly. It exists so a caller
    that only wants the finished batch keeps its old signature; there is no second
    parallel/sequential implementation behind it.
    """
    session = await ToolBatchSession.open(
        context, assistant_message, config, signal, emit, session_id="compat"
    )
    messages: list[ToolResultMessage] = []
    try:
        if session.sequential:
            while True:
                observation = await session.next_observation()
                if observation is None:
                    break
                message = session.message_for(observation.call_id)
                if message is not None:
                    await _emit_result_message(message, emit)
                    messages.append(message)
        else:
            while await session.next_observation() is not None:
                pass
            for message in session.messages_in_order():
                await _emit_result_message(message, emit)
                messages.append(message)
    finally:
        await session.aclose()
    return ToolBatch(messages=messages, terminate=session.terminate)
