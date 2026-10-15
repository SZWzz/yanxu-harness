"""0.6.0 P05：有状态 Agent 包装层，对照 `agent/src/agent.ts` @ c7cdb46（607 行）。

低层循环（`agent_loop.py`）是「给一份上下文、跑一轮、返回新消息」的纯函数形状；宿主需要的是
另一件事：**谁持有 transcript、忙/闲怎么判、插话往哪排、取消是谁的**。这些都在这层。

三条端口径决定（都有对应用例，且都是刻意偏离上游）：

1. `stream_fn` 必须显式注入，core 不读任何全局默认流（计划 §28）。构造时就拒，不留到第一次 run。
2. 运行失败时合成的 assistant 消息里 `usage` 是「未知」（全 `None`），不是上游的
   `EMPTY_USAGE`（全 0）—— 计划 §100：宿主会把 0 当成「真的没花 token」去做预算与计费。
3. `continue()` 在 Python 是关键字，这里叫 `continue_run()`；`AbortSignal` 换成显式
   `CancellationToken`（P01 契约范围 §2.1 对 chord `Context` 判「不适用」的落点）。

**不在这层的东西**（属于 StoryIDE 宿主，计划 §115/§123）：持久审批、operation/lease、
unknown 结算、跨项目的用户队列。`MessageQueue` 只是内存能力，不能当第二份真相。
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, Awaitable, Callable, Iterable, Sequence

from .agent_loop import AgentLoopConfig, run_agent_loop, run_agent_loop_continue
from .cancellation import CancellationToken
from .events import AgentEnd, MessageEnd, MessageStart, TurnEnd
from .messages import create_initial_system_message, get_current_system_prompt, to_tool_declaration
from .queues import QUEUE_MODES, MessageQueue
from .types import AssistantMessage, AgentContext, Model, StopReason, TextContent, UserMessage

__all__ = ["Agent", "AgentState", "DEFAULT_MODEL"]

Listener = Callable[[Any, CancellationToken], Awaitable[None] | None]

DEFAULT_MODEL = Model(id="unknown")


def _validate_mode(mode: str) -> str:
    if mode not in QUEUE_MODES:
        raise ValueError(f"未知 queue 模式 {mode!r}，只允许 {QUEUE_MODES}")
    return mode


class AgentState:
    """Agent 持有的可变状态（agent.ts:69-110）。

    `messages` / `tools` 的赋值语义是**拷一层**：调用方之后改自己那个数组不影响 agent，
    反过来 agent 往 transcript 里追加也不会污染调用方的数组（上游 `set messages(v){ messages = v.slice() }`）。
    `system_prompt` 是只读派生值 —— 它永远等于开头那条 system 消息，而不是另一个字段。
    """

    def __init__(
        self,
        *,
        model: Model | None = None,
        thinking_level: str = "off",
        system_prompt: str | None = None,
        tools: Sequence[Any] | None = None,
        messages: Sequence[Any] | None = None,
    ) -> None:
        self._tools: list[Any] = list(tools or [])
        self._messages: list[Any] = list(messages or [])
        initial = create_initial_system_message(system_prompt, [to_tool_declaration(t) for t in self._tools])
        if initial is not None and getattr(self._messages[0] if self._messages else None, "role", None) != "system":
            self._messages.insert(0, initial)

        self.model = model or DEFAULT_MODEL
        self.thinking_level = thinking_level
        self.is_streaming = False
        self.streaming_message: Any | None = None
        self.pending_tool_calls: set[str] = set()
        self.error_message: str | None = None

    @property
    def messages(self) -> list[Any]:
        return self._messages

    @messages.setter
    def messages(self, next_messages: Iterable[Any]) -> None:
        self._messages = list(next_messages)

    @property
    def tools(self) -> list[Any]:
        return self._tools

    @tools.setter
    def tools(self, next_tools: Iterable[Any]) -> None:
        self._tools = list(next_tools)

    @property
    def system_prompt(self) -> str | None:
        return get_current_system_prompt(self._messages)


class _ActiveRun:
    __slots__ = ("token", "future")

    def __init__(self, token: CancellationToken, future: "asyncio.Future[None]") -> None:
        self.token = token
        self.future = future


class Agent:
    """低层循环之上的状态机：transcript、监听器、两个队列、忙/闲与取消。"""

    def __init__(
        self,
        *,
        stream_fn: Any,
        initial_state: AgentState | None = None,
        convert_to_llm: Callable[[Sequence[Any]], Any] | None = None,
        transform_context: Callable[..., Any] | None = None,
        before_tool_call: Callable[..., Any] | None = None,
        after_tool_call: Callable[..., Any] | None = None,
        should_stop_after_turn: Callable[..., Any] | None = None,
        prepare_next_turn: Callable[..., Any] | None = None,
        prepare_next_turn_with_context: Callable[..., Any] | None = None,
        get_api_key: Callable[[str], Any] | None = None,
        argument_validator: Callable[[Any, dict], dict] | None = None,
        steering_mode: str = "one-at-a-time",
        follow_up_mode: str = "one-at-a-time",
        tool_execution: str = "parallel",
        session_id: str | None = None,
        api_key: str | None = None,
        max_turns: int | None = None,
        run_id_prefix: str = "agent",
    ) -> None:
        if stream_fn is None:
            raise ValueError(
                "Agent.stream_fn 必须显式提供：core 不接受上游 setDefaultStreamFn 那种跨调用共享的全局默认流（计划 §28）"
            )
        self.stream_fn = stream_fn
        self._state = initial_state if initial_state is not None else AgentState()
        self.convert_to_llm = convert_to_llm
        self.transform_context = transform_context
        self.before_tool_call = before_tool_call
        self.after_tool_call = after_tool_call
        self.should_stop_after_turn = should_stop_after_turn
        self.prepare_next_turn = prepare_next_turn
        self.prepare_next_turn_with_context = prepare_next_turn_with_context
        self.get_api_key = get_api_key
        self.argument_validator = argument_validator
        self.tool_execution = tool_execution
        self.session_id = session_id
        self.api_key = api_key
        self.max_turns = max_turns
        self.run_id_prefix = run_id_prefix

        self._steering = MessageQueue(_validate_mode(steering_mode))
        self._follow_up = MessageQueue(_validate_mode(follow_up_mode))
        self._listeners: list[Listener] = []
        self._active_run: _ActiveRun | None = None
        self._runs = 0

    # --- 状态与队列 API（agent.ts:275-336） ------------------------------------

    @property
    def state(self) -> AgentState:
        return self._state

    @property
    def steering_mode(self) -> str:
        return self._steering.mode

    @steering_mode.setter
    def steering_mode(self, mode: str) -> None:
        self._steering.mode = _validate_mode(mode)

    @property
    def follow_up_mode(self) -> str:
        return self._follow_up.mode

    @follow_up_mode.setter
    def follow_up_mode(self, mode: str) -> None:
        self._follow_up.mode = _validate_mode(mode)

    def steer(self, message: Any) -> None:
        self._steering.enqueue(message)

    def follow_up(self, message: Any) -> None:
        self._follow_up.enqueue(message)

    def clear_steering_queue(self) -> None:
        self._steering.clear()

    def clear_follow_up_queue(self) -> None:
        self._follow_up.clear()

    def clear_all_queues(self) -> None:
        self.clear_steering_queue()
        self.clear_follow_up_queue()

    def has_queued_messages(self) -> bool:
        return self._steering.has_items() or self._follow_up.has_items()

    @property
    def signal(self) -> CancellationToken | None:
        return self._active_run.token if self._active_run else None

    def abort(self) -> None:
        """没有 active run 时是 no-op（agent.ts:334-336）：取消一个不存在的 run 不该抛。"""
        if self._active_run is not None:
            self._active_run.token.cancel("agent.abort")

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        self._listeners.append(listener)

        def unsubscribe() -> None:
            try:
                self._listeners.remove(listener)
            except ValueError:  # 已被移除：退订幂等
                pass

        return unsubscribe

    async def wait_for_idle(self) -> None:
        """当前 run 与其 `agent_end` 监听器全部结算后才返回（agent.ts:343-345）。"""
        run = self._active_run
        if run is None:
            return
        await run.future

    def reset(self) -> None:
        """清历史与队列，但**保留**重放后的 system 基线（含它的 toolsAdded）。忙时拒。"""
        if self._active_run is not None:
            raise RuntimeError("Agent is already processing. Wait for completion before resetting.")
        baseline = next((m for m in self._state.messages if getattr(m, "role", None) == "system"), None)
        self._state.messages = [baseline] if baseline is not None else []
        self._state.is_streaming = False
        self._state.streaming_message = None
        self._state.pending_tool_calls = set()
        self._state.error_message = None
        self._follow_up.clear()
        self._steering.clear()

    # --- 运行入口（agent.ts:363-451） -----------------------------------------

    async def prompt(self, message: Any) -> None:
        """开一轮新提示词。忙时拒 —— 插话必须走 `steer()`/`follow_up()`，不能塞进正在跑的回合。"""
        if self._active_run is not None:
            raise RuntimeError(
                "Agent is already processing a prompt. Use steer() or follow_up() to queue messages, "
                "or wait for completion."
            )
        await self._run_prompt_messages(self._normalize_prompt_input(message))

    async def continue_run(self) -> None:
        """从既有 transcript 续跑（上游叫 `continue()`，Python 里是关键字）。

        尾巴是 assistant 时按 steering → follow-up 的优先级取队列，两个都空才报错；
        走 steering 那一路要跳过循环开头的轮询，否则刚取出的那条会被排空两次。
        """
        if self._active_run is not None:
            raise RuntimeError("Agent is already processing. Wait for completion before continuing.")
        messages = self._state.messages
        last = messages[-1] if messages else None
        if last is None or all(getattr(m, "role", None) == "system" for m in messages):
            raise RuntimeError("No messages to continue from")
        if getattr(last, "role", None) == "assistant":
            queued = self._steering.drain()
            if queued:
                await self._run_prompt_messages(queued, skip_initial_steering_poll=True)
                return
            queued = self._follow_up.drain()
            if queued:
                await self._run_prompt_messages(queued)
                return
            raise RuntimeError("Cannot continue from message role: assistant")
        await self._run_continuation()

    def _normalize_prompt_input(self, message: Any) -> list[Any]:
        if isinstance(message, str):
            return [UserMessage(content=message)]
        if isinstance(message, (list, tuple)):
            return list(message)
        return [message]

    async def _run_prompt_messages(self, messages: list[Any], skip_initial_steering_poll: bool = False) -> None:
        async def executor(signal: CancellationToken) -> None:
            await run_agent_loop(
                messages,
                self._context_snapshot(),
                self._loop_config(skip_initial_steering_poll),
                signal,
                self.dispatch,
            )

        await self._run_with_lifecycle(executor)

    async def _run_continuation(self) -> None:
        async def executor(signal: CancellationToken) -> None:
            await run_agent_loop_continue(
                self._context_snapshot(),
                self._loop_config(),
                signal,
                self.dispatch,
            )

        await self._run_with_lifecycle(executor)

    def _context_snapshot(self) -> AgentContext:
        """只带 messages/tools 的快照（agent.ts:453-458）。

        注意 `system_prompt` 不在其中 —— 上游的循环也只拿 `{messages}`（P04 记录 §5.4）。
        system 要进模型必须作为 transcript 里那条 system 消息，所以本类的构造器把它做成
        开头那条消息，而不是留一个字段给循环去猜。
        """
        return AgentContext(messages=list(self._state.messages), tools=list(self._state.tools))

    def _loop_config(self, skip_initial_steering_poll: bool = False) -> AgentLoopConfig:
        skip = skip_initial_steering_poll

        async def drain_steering() -> list[Any]:
            nonlocal skip
            if skip:
                skip = False
                return []
            return self._steering.drain()

        prepare = self._prepare_next_turn_hook()
        should_stop = self._should_stop_hook()
        extra: dict[str, Any] = {}
        if self.session_id:
            extra["session_id"] = self.session_id

        return AgentLoopConfig(
            stream_fn=self.stream_fn,
            model=self._state.model,
            tools=list(self._state.tools),
            api_key=self.api_key,
            get_api_key=self.get_api_key,
            convert_to_llm=self.convert_to_llm,
            transform_context=self.transform_context,
            before_tool_call=self.before_tool_call,
            after_tool_call=self.after_tool_call,
            should_stop_after_turn=should_stop,
            prepare_next_turn=prepare,
            get_steering_messages=drain_steering,
            get_follow_ups=self._follow_up.drain,
            tool_execution=self.tool_execution,
            argument_validator=self.argument_validator,
            max_turns=self.max_turns,
            reasoning=None if self._state.thinking_level == "off" else self._state.thinking_level,
            extra=extra,
        )

    def _prepare_next_turn_hook(self) -> Callable[[Any], Any] | None:
        if self.prepare_next_turn_with_context is None and self.prepare_next_turn is None:
            return None

        async def prepare(context: Any) -> Any:
            if self.prepare_next_turn_with_context is not None:
                return await _maybe(self.prepare_next_turn_with_context(context, self.signal))
            return await _maybe(self.prepare_next_turn(self.signal))

        return prepare

    def _should_stop_hook(self) -> Callable[[Any], Any] | None:
        if self.should_stop_after_turn is None:
            return None

        async def should_stop(context: Any) -> Any:
            return await _maybe(self.should_stop_after_turn(context, self.signal))

        return should_stop

    # --- 生命周期与事件收敛（agent.ts:501-606） --------------------------------

    async def _run_with_lifecycle(self, executor: Callable[[CancellationToken], Awaitable[None]]) -> None:
        if self._active_run is not None:
            raise RuntimeError("Agent is already processing.")

        self._runs += 1
        token = CancellationToken(f"{self.run_id_prefix}:run{self._runs}")
        future: "asyncio.Future[None]" = asyncio.get_running_loop().create_future()
        self._active_run = _ActiveRun(token=token, future=future)

        self._state.is_streaming = True
        self._state.streaming_message = None
        self._state.error_message = None

        try:
            await executor(token)
        except Exception as exc:  # 转成终态事件，而不是让宿主拿到一个没有下文的异常
            await self._handle_run_failure(exc, token.cancelled)
        finally:
            self._finish_run()

    async def _handle_run_failure(self, error: BaseException, aborted: bool) -> None:
        """失败也要有终态消息（agent.ts:526-542）。

        上游在这里填 `EMPTY_USAGE`（全 0）。本仓库填「未知」：一次没跑成的请求**没有** token
        事实可报，写成 0 会被宿主当成「这次真的没花钱」（计划 §100）。
        """
        message = AssistantMessage(
            content=[TextContent(text="")],
            api=self._state.model.api,
            provider=self._state.model.provider,
            model=self._state.model.id,
            stop_reason=StopReason.ABORTED if aborted else StopReason.ERROR,
            error_message=f"{type(error).__name__}: {error}",
        )
        await self.dispatch(MessageStart(message=message))
        await self.dispatch(MessageEnd(message=message))
        await self.dispatch(TurnEnd(message=message, tool_results=()))
        await self.dispatch(AgentEnd(messages=(message,)))

    def _finish_run(self) -> None:
        self._state.is_streaming = False
        self._state.streaming_message = None
        self._state.pending_tool_calls = set()
        run = self._active_run
        self._active_run = None
        if run is not None and not run.future.done():
            run.future.set_result(None)

    async def dispatch(self, event: Any) -> None:
        """事件 reducer + 监听器投递。`agent_end` 只是「没有后续 loop 事件」，idle 在监听器结算之后。"""
        state = self._state
        kind = getattr(event, "type", None)
        if kind in ("message_start", "message_update"):
            state.streaming_message = event.message
        elif kind == "message_end":
            state.streaming_message = None
            state.messages.append(event.message)
        elif kind == "tool_execution_start":
            state.pending_tool_calls = set(state.pending_tool_calls) | {event.tool_call_id}
        elif kind == "tool_execution_end":
            state.pending_tool_calls = {i for i in state.pending_tool_calls if i != event.tool_call_id}
        elif kind == "turn_end":
            message = event.message
            if getattr(message, "role", None) == "assistant" and getattr(message, "error_message", None):
                state.error_message = message.error_message
        elif kind == "agent_end":
            state.streaming_message = None

        run = self._active_run
        if run is None:
            raise RuntimeError("Agent listener invoked outside active run")
        for listener in list(self._listeners):  # 快照：允许监听器内部退订
            await _maybe_call(listener, event, run.token)


async def _maybe(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def _maybe_call(listener: Listener, event: Any, token: CancellationToken) -> Any:
    result = listener(event, token)
    return await result if inspect.isawaitable(result) else None
