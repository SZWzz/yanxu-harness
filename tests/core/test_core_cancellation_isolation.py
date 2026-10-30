"""S5：取消、运行隔离与资源生命周期。

计划 S5 的验收：取消和异常后没有悬挂异步任务、未关闭流或继续提交的旧执行者；运行身份互相
隔离；UI 慢消费者不丢终态；已取消时不新派发动作；挂起期间取消可持久记录并在恢复前被识别。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from core_loop_helpers import (
    RecordingSink,
    assistant,
    make_tool,
    run,
    stepping,
    tool_call,
    user,
)
from yanxu_harness.core.agent import Agent
from yanxu_harness.core.agent_loop import AgentLoopConfig, run_agent_loop_result
from yanxu_harness.core.cancellation import CancellationToken
from yanxu_harness.core.execution import (
    BatchCallState,
    ExecutionState,
    InputsPolled,
    InputKind,
    ModelCompleted,
    Phase,
    ToolBatchSettled,
)
from yanxu_harness.core.outcome import CallOutcome, LoopStop, RunStatus
from yanxu_harness.core.transitions import advance
from yanxu_harness.core.types import (
    AgentContext,
    AssistantMessage,
    StopReason,
    TextContent,
    ToolCall,
    ToolResultMessage,
)
from yanxu_harness.errors import ContractError


def context_with(tools=()) -> AgentContext:
    return AgentContext(messages=[], system_prompt="", tools=list(tools))


# --- no new dispatch once cancelled ----------------------------------------------------


def test_a_cancelled_run_executes_no_tool_handler() -> None:
    """The property, not the wording: after a cancellation, no handler starts."""
    executed: list[str] = []

    async def execute(call_id, _args, _signal=None, _on_update=None):
        executed.append(call_id)
        return {"content": [TextContent(text="不该执行")], "details": {}}

    tool = make_tool("t", execute)
    token = CancellationToken("s5")
    stream = stepping(
        assistant(tool_call("c1", "t"), stop=StopReason.TOOL_USE),
        assistant(TextContent(text="收尾")),
    )

    def cancel_after_turn_end(event: object) -> None:
        message = getattr(event, "message", None)
        if (
            getattr(event, "type", "") == "message_end"
            and getattr(message, "role", "") == "assistant"
        ):
            token.cancel("operator asked to stop")

    sink = RecordingSink()

    async def emit(event: object) -> None:
        await sink(event)
        cancel_after_turn_end(event)

    result = run(
        run_agent_loop_result(
            [user("go")], context_with([tool]), AgentLoopConfig(stream_fn=stream, tools=[tool]), token, emit
        )
    )

    assert result.status is RunStatus.CANCELLED
    assert result.stop is LoopStop.ABORTED
    assert executed == [], "a cancelled run must not start a tool call"
    # The declared call still gets an answer: silence would look like a call that ran.
    results = [m for m in result.messages if isinstance(m, ToolResultMessage)]
    assert [m.tool_call_id for m in results] == ["c1"]
    assert results[0].is_error is True


def test_cancellation_while_building_a_request_returns_cancelled_without_dispatch() -> None:
    token = CancellationToken("s5")
    calls: list[str] = []

    async def stream(_model, _context, _options=None):
        calls.append("provider")
        raise AssertionError("a cancelled run must not call the provider")

    async def get_api_key(_provider: str) -> str:
        token.cancel("operator asked to stop")
        return "unused"

    result = run(
        run_agent_loop_result(
            [user("go")],
            context_with(),
            AgentLoopConfig(stream_fn=stream, get_api_key=get_api_key),
            token,
            RecordingSink(),
        )
    )

    assert result.status is RunStatus.CANCELLED
    assert result.stop is LoopStop.ABORTED
    assert calls == []


def test_cancellation_during_input_event_notification_stops_before_first_request() -> None:
    token = CancellationToken("s5")
    calls: list[str] = []

    async def stream(_model, _context, _options=None):
        calls.append("provider")
        raise AssertionError("a cancelled run must not call the provider")

    async def emit(event: object) -> None:
        message = getattr(event, "message", None)
        if (
            getattr(event, "type", "") == "message_end"
            and getattr(message, "role", "") == "user"
        ):
            token.cancel("operator asked to stop")

    result = run(
        run_agent_loop_result(
            [user("go")], context_with(), AgentLoopConfig(stream_fn=stream), token, emit
        )
    )

    assert result.status is RunStatus.CANCELLED
    assert result.stop is LoopStop.ABORTED
    assert calls == []


def test_a_cancellation_does_not_discard_a_real_receipt() -> None:
    """A settled call keeps its result even when the run is cancelled right after."""

    async def execute(_call_id, _args, _signal=None, _on_update=None):
        return {"content": [TextContent(text="写入成功")], "details": {"status": "executed"}}

    tool = make_tool("t", execute)
    token = CancellationToken("s5")
    stream = stepping(
        assistant(tool_call("c1", "t"), stop=StopReason.TOOL_USE),
        assistant(TextContent(text="收尾")),
    )

    original_seen: list[str] = []

    async def emit(event: object) -> None:
        kind = getattr(event, "type", "")
        if kind == "tool_execution_end":
            original_seen.append(getattr(event, "tool_call_id", ""))
            token.cancel("operator asked to stop")
        return None

    result = run(
        run_agent_loop_result(
            [user("go")], context_with([tool]), AgentLoopConfig(stream_fn=stream, tools=[tool]), token, emit
        )
    )

    assert original_seen == ["c1"], "the real end event must have been delivered"
    assert result.status is RunStatus.CANCELLED
    receipt = next(m for m in result.messages if isinstance(m, ToolResultMessage))
    assert receipt.is_error is False
    assert receipt.details["status"] == "executed", "a receipt is never dropped by a cancellation"


def test_the_first_cancellation_reason_wins() -> None:
    token = CancellationToken("s5")
    token.cancel("first")
    token.cancel("second")
    assert token.reason == "first"
    assert token.cancelled is True


# --- stale events cannot revive or corrupt a newer state --------------------------------


def test_a_settled_batch_from_an_old_turn_is_refused() -> None:
    """A result that answers a call this turn never declared is a stale callback."""
    message = assistant(tool_call("c1", "t"), stop=StopReason.TOOL_USE)
    state = ExecutionState(
        phase=Phase.RUNNING_BATCH,
        last_assistant=message,
        messages=(message,),
    )
    stale = ToolResultMessage(tool_call_id="old-call", tool_name="t", content=[TextContent(text="旧")])
    with pytest.raises(ContractError) as excinfo:
        advance(
            state,
            ToolBatchSettled(event_id="s1", at_ms=1, results=(stale,), terminate=False),
        )
    assert "never declared" in str(excinfo.value)


def test_two_results_for_one_call_are_refused() -> None:
    message = assistant(tool_call("c1", "t"), stop=StopReason.TOOL_USE)
    state = ExecutionState(phase=Phase.RUNNING_BATCH, last_assistant=message, messages=(message,))
    duplicated = (
        ToolResultMessage(tool_call_id="c1", tool_name="t", content=[TextContent(text="a")]),
        ToolResultMessage(tool_call_id="c1", tool_name="t", content=[TextContent(text="b")]),
    )
    with pytest.raises(ContractError) as excinfo:
        advance(
            state,
            ToolBatchSettled(event_id="s1", at_ms=1, results=duplicated, terminate=False),
        )
    assert "more than one result" in str(excinfo.value)


def test_a_late_event_after_a_run_ended_cannot_start_a_new_turn() -> None:
    driver_finished = ExecutionState(
        phase=Phase.FINISHED,
        status=RunStatus.COMPLETED,
        stop=LoopStop.ASSISTANT_FINAL,
    )
    transition = advance(
        driver_finished,
        ModelCompleted(event_id="late", at_ms=5, message=assistant(TextContent(text="陈旧"))),
    )
    assert transition.ignored is True
    assert transition.state is driver_finished
    assert transition.state.messages == ()


def test_a_settled_batch_in_a_newer_phase_is_refused_not_applied() -> None:
    """Phase association is the guard: a result cannot land in a turn that is asking."""
    state = ExecutionState(phase=Phase.REQUESTING_MODEL)
    result = ToolResultMessage(tool_call_id="c1", tool_name="t")
    with pytest.raises(ContractError) as excinfo:
        advance(
            state,
            ToolBatchSettled(event_id="s1", at_ms=1, results=(result,), terminate=False),
        )
    assert "requires running_batch" in str(excinfo.value)


# --- resource lifetime and isolation between concurrent agents -------------------------


def test_two_agents_on_one_loop_do_not_block_each_other() -> None:
    """A slow observer on one run must not delay another run's terminal outcome."""

    async def scenario() -> tuple[RunStatus, RunStatus, list[str]]:
        slow_release = asyncio.Event()
        order: list[str] = []

        async def slow_listener(event, _token):
            if getattr(event, "type", "") == "agent_end":
                order.append("slow:agent_end:start")
                await slow_release.wait()
                order.append("slow:agent_end:done")

        slow_stream = stepping(assistant(TextContent(text="慢")))
        fast_stream = stepping(assistant(TextContent(text="快")))
        slow = Agent(stream_fn=slow_stream)
        fast = Agent(stream_fn=fast_stream)
        slow.subscribe(slow_listener)

        slow_task = asyncio.ensure_future(slow.prompt("慢"))
        await asyncio.sleep(0)
        fast_task = asyncio.ensure_future(fast.prompt("快"))
        # The fast run must finish while the slow run's listener is still blocked.
        for _ in range(50):
            if fast.state.messages and not fast.state.is_streaming:
                break
            await asyncio.sleep(0)
        order.append("fast:done")
        fast_status = fast.last_result.status if fast.last_result else None
        assert not slow_task.done(), "the slow observer is meant to still be blocked"
        slow_release.set()
        await asyncio.gather(slow_task, fast_task)
        return fast_status, slow.last_result.status, order

    fast_status, slow_status, order = run(scenario())
    assert fast_status is RunStatus.COMPLETED
    assert slow_status is RunStatus.COMPLETED
    assert order.index("fast:done") < order.index("slow:agent_end:done")


def test_the_event_stream_buffer_is_bounded_and_says_so_when_full() -> None:
    """A slow consumer gets an explicit overflow count; the terminal frame is never refused."""
    from yanxu_harness.core.stream import create_assistant_message_event_stream

    stream = create_assistant_message_event_stream(max_events=2)
    accepted = [stream.push({"type": "text_delta", "index": index}) for index in range(5)]
    assert accepted == [True, True, False, False, False]
    assert stream.overflow_count == 3, "a dropped frame is counted, not silent"
    # A full buffer must not be able to strand a consumer: the terminal frame gets in.
    assert stream.push({"type": "done", "partial": "final"}) is True

    consumed = run(_drain(stream))
    assert [frame.get("type") for frame in consumed][-1] == "done"
    assert len(consumed) == 3
    assert run(stream.result()) == "final"


async def _drain(stream) -> list[dict]:
    events: list[dict] = []
    async for event in stream:
        events.append(event)
    return events


def test_a_cancelled_agent_run_leaves_no_dangling_task() -> None:
    """Cancellation resumes the caller, and the agent goes idle rather than half-alive."""
    import asyncio

    async def scenario() -> tuple[RunStatus, bool, bool]:
        from yanxu_harness.core.agent import Agent
        from yanxu_harness.core.cancellation import CancellationToken

        started = asyncio.Event()
        release = asyncio.Event()

        class BlockingStream:
            def __init__(self) -> None:
                self.calls = 0

            async def __call__(self, model, context, options=None):
                self.calls += 1
                return self

            def __aiter__(self):
                return self

            async def __anext__(self):
                started.set()
                await release.wait()
                raise StopAsyncIteration

            async def result(self):
                return AssistantMessage(
                    content=[TextContent(text="")], stop_reason=StopReason.ABORTED
                )

        stream = BlockingStream()
        agent = Agent(stream_fn=stream)
        token_before = time.monotonic()
        task = asyncio.ensure_future(agent.prompt("go"))
        await started.wait()
        agent.abort()
        release.set()
        await asyncio.wait_for(task, timeout=5)
        elapsed = time.monotonic() - token_before
        return (
            agent.last_result.status if agent.last_result else None,
            agent.state.is_streaming,
            elapsed < 5,
        )

    status, streaming, bounded = run(scenario())
    assert status is RunStatus.CANCELLED
    assert streaming is False, "the agent must be idle after a cancelled run"
    assert bounded is True
    assert CancellationToken("x").cancelled is False
