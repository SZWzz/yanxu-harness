"""S1：每条退出路径都有唯一、可断言的结果，暂停不再被静默当成完成。

计划 S1 的验收要求：旧列表 API 与结构化入口走同一内核；等待审批/外部结果/未知核对
必须能被读出来；`Agent.prompt()` 的调用习惯不变但可以取得结构化结果；一次审批等待
不产生额外模型请求。

这些用例替换 S0 中「旧入口静默返回列表」的复现断言（`test_core_execution_boundaries.py`）。
"""

from __future__ import annotations

import pytest

from core_loop_helpers import (
    RecordingSink,
    assistant,
    make_tool,
    ok_result,
    run,
    stepping,
    tool_call,
    user,
)
from yanxu_harness.core.agent import Agent
from yanxu_harness.core.agent_loop import (
    AgentLoopConfig,
    run_agent_loop,
    run_agent_loop_continue,
    run_agent_loop_continue_result,
    run_agent_loop_result,
)
from yanxu_harness.core.outcome import LoopResult, LoopStop, LoopSuspended, RunStatus
from yanxu_harness.core.types import (
    AgentContext,
    AssistantMessage,
    DeferredHandle,
    StopReason,
    TextContent,
)
from yanxu_harness.errors import ContractError, HarnessError


def context_with(tools=()) -> AgentContext:
    return AgentContext(messages=[], system_prompt="", tools=list(tools))


def suspended_config(**kwargs) -> tuple[AgentLoopConfig, dict, object]:
    """A loop config whose first batch suspends, plus the interaction it reports."""
    interaction = {"kind": "approval", "call_id": "c1", "approval_key": "ns:c1"}
    stream = stepping(
        assistant(tool_call("c1", "write_note"), stop=StopReason.TOOL_USE),
        assistant(TextContent(text="不应请求下一轮")),
    )
    tool = make_tool("write_note", ok_result)
    config = AgentLoopConfig(
        stream_fn=stream,
        tools=[tool],
        should_suspend_after_batch=lambda _m, _r: interaction,
        **kwargs,
    )
    return config, interaction, stream


# --- 旧列表入口：暂停必须显式，不能冒充完成 -------------------------------------------


def test_the_list_entry_raises_a_control_exception_carrying_the_pause() -> None:
    config, interaction, stream = suspended_config()

    with pytest.raises(LoopSuspended) as excinfo:
        run(run_agent_loop([user("write it")], context_with(), config, None, RecordingSink()))

    raised = excinfo.value
    assert isinstance(raised, HarnessError), "the control signal is a harness type, never application-specific"
    assert raised.result.status is RunStatus.SUSPENDED
    assert raised.result.pending_interaction == interaction
    assert "approval" in str(raised)
    assert len(stream.calls) == 1, "waiting for a decision must not produce another request"
    assert stream.calls and raised.result.turns_used == 1


def test_the_continue_entry_raises_the_same_control_exception() -> None:
    config, interaction, stream = suspended_config()
    context = AgentContext(messages=[user("先说一句")], tools=list(config.tools or []))

    with pytest.raises(LoopSuspended) as excinfo:
        run(run_agent_loop_continue(context, config, None, RecordingSink()))

    assert excinfo.value.result.pending_interaction == interaction
    assert len(stream.calls) == 1


def test_the_structured_continue_entry_returns_the_pause_instead() -> None:
    config, interaction, _stream = suspended_config()
    context = AgentContext(messages=[user("先说一句")], tools=list(config.tools or []))
    result = run(
        run_agent_loop_continue_result(context, config, None, RecordingSink())
    )
    assert result.status is RunStatus.SUSPENDED
    assert result.pending_interaction == interaction


def test_a_suspension_result_cannot_be_asserted_for_a_finished_run() -> None:
    finished = LoopResult(status=RunStatus.COMPLETED, stop=LoopStop.ASSISTANT_FINAL)
    with pytest.raises(ContractError):
        LoopSuspended(finished)
    with pytest.raises(ContractError):
        LoopSuspended("not a result")  # type: ignore[arg-type]


# --- 模型自己说「这一轮没完」：deferred 也是暂停 ---------------------------------------


def test_a_deferred_assistant_turn_is_a_pause_not_a_completed_answer() -> None:
    message = AssistantMessage(
        content=[TextContent(text="需要人工确认")],
        stop_reason=StopReason.DEFERRED,
        deferred=DeferredHandle(id="h1", payload={"reason": "human review"}),
    )
    stream = stepping(message)
    sink = RecordingSink()

    result = run(
        run_agent_loop_result([user("go")], context_with(), AgentLoopConfig(stream_fn=stream), None, sink)
    )

    assert result.status is RunStatus.SUSPENDED
    assert result.stop is LoopStop.SUSPENDED
    assert result.terminal is False
    assert result.pending_interaction == {
        "kind": "deferred",
        "handle_id": "h1",
        "payload": {"reason": "human review"},
    }
    assert result.turns_used == 1
    assert len(stream.calls) == 1
    assert sink.count("tool_execution_start") == 0
    assert sink.types[-1] == "agent_end"


def test_a_deferred_turn_without_a_handle_still_reports_the_pause() -> None:
    stream = stepping(
        AssistantMessage(content=[TextContent(text="等一下")], stop_reason=StopReason.DEFERRED)
    )
    result = run(
        run_agent_loop_result([user("go")], context_with(), AgentLoopConfig(stream_fn=stream), None, RecordingSink())
    )
    assert result.status is RunStatus.SUSPENDED
    assert result.pending_interaction == {"kind": "deferred"}


def test_a_deferred_turn_with_tool_calls_does_not_dispatch_them() -> None:
    message = AssistantMessage(
        content=[tool_call("c1", "t")],
        stop_reason=StopReason.DEFERRED,
        deferred=DeferredHandle(id="h1"),
    )
    executions: list[str] = []

    async def execute(call_id, _args, _signal=None, _on_update=None):
        executions.append(call_id)
        return {"content": [], "details": {}}

    tool = make_tool("t", execute)
    result = run(
        run_agent_loop_result(
            [user("go")],
            context_with([tool]),
            AgentLoopConfig(stream_fn=stepping(message), tools=[tool]),
            None,
            RecordingSink(),
        )
    )
    assert result.status is RunStatus.SUSPENDED
    assert executions == [], "a deferred turn's calls are not dispatched"


# --- Agent：习惯不变，结果可读 --------------------------------------------------------


def test_agent_prompt_result_returns_the_structured_outcome_of_a_plain_answer() -> None:
    stream = stepping(assistant(TextContent(text="好了")))

    async def scenario() -> LoopResult:
        agent = Agent(stream_fn=stream, initial_state=None)
        result = await agent.prompt_result("你好")
        assert agent.last_result is result
        return result

    result = run(scenario())
    assert result.status is RunStatus.COMPLETED
    assert result.stop is LoopStop.ASSISTANT_FINAL
    assert result.turns_used == 1
    assert result.final_text == "好了"


def test_agent_prompt_keeps_its_signature_and_still_returns_none() -> None:
    stream = stepping(assistant(TextContent(text="好了")))

    async def scenario() -> None:
        agent = Agent(stream_fn=stream)
        assert await agent.prompt("你好") is None
        assert agent.last_result is not None
        assert agent.last_result.status is RunStatus.COMPLETED
        assert agent.state.is_streaming is False

    run(scenario())


def test_agent_prompt_raises_the_pause_and_leaves_no_fabricated_failure() -> None:
    interaction = {"kind": "approval", "call_id": "c1"}

    async def scenario() -> tuple[LoopSuspended, Agent]:
        stream = stepping(
            assistant(tool_call("c1", "write_note"), stop=StopReason.TOOL_USE),
            assistant(TextContent(text="不应请求下一轮")),
        )
        agent = Agent(
            stream_fn=stream,
            should_suspend_after_batch=lambda _m, _r: interaction,
        )
        with pytest.raises(LoopSuspended) as excinfo:
            await agent.prompt("写吧")
        assert len(stream.calls) == 1
        return excinfo.value, agent

    raised, agent = run(scenario())
    assert raised.result.status is RunStatus.SUSPENDED
    assert raised.result.pending_interaction == interaction
    assert agent.last_result is not None and agent.last_result.status is RunStatus.SUSPENDED
    # 暂停不是失败：没有合成的失败消息，也没有 error_message。
    assert all(getattr(m, "error_message", None) is None for m in agent.state.messages)
    assert agent.state.error_message is None
    assert agent.state.is_streaming is False


def test_agent_prompt_result_returns_the_pause_without_raising() -> None:
    interaction = {"kind": "approval", "call_id": "c1"}

    async def scenario() -> tuple[LoopResult, int]:
        stream = stepping(
            assistant(tool_call("c1", "write_note"), stop=StopReason.TOOL_USE),
            assistant(TextContent(text="不应请求下一轮")),
        )
        agent = Agent(stream_fn=stream, should_suspend_after_batch=lambda _m, _r: interaction)
        result = await agent.prompt_result("写吧")
        return result, len(stream.calls)

    result, calls = run(scenario())
    assert result.status is RunStatus.SUSPENDED
    assert result.pending_interaction == interaction
    assert calls == 1


def test_agent_continue_run_result_reports_the_pause_too() -> None:
    interaction = {"kind": "reconciliation", "operation_id": "op-1"}

    async def scenario() -> LoopResult:
        stream = stepping(
            assistant(tool_call("c1", "write_note"), stop=StopReason.TOOL_USE),
            assistant(tool_call("c2", "write_note"), stop=StopReason.TOOL_USE),
            assistant(TextContent(text="不应请求第三轮")),
        )
        agent = Agent(stream_fn=stream, should_suspend_after_batch=lambda _m, _r: interaction)
        await agent.prompt_result("写吧")  # ends suspended; the tail is a tool result
        return await agent.continue_run_result()

    result = run(scenario())
    assert result.status is RunStatus.SUSPENDED
    assert result.pending_interaction == interaction


def test_an_internal_failure_keeps_its_own_stop_code() -> None:
    """宿主 hook 抛异常 ≠ 模型失败：结束原因必须能区分，重试策略才不会互相污染。"""

    async def scenario() -> tuple[LoopResult, Agent]:
        stream = stepping(assistant(TextContent(text="不应到达")))

        def explode(_turn, _signal):
            raise RuntimeError("hook bug")

        agent = Agent(stream_fn=stream, should_stop_after_turn=explode)
        assert await agent.prompt("go") is None  # 旧契约不变：不向宿主抛
        return agent.last_result, agent

    result, agent = run(scenario())
    assert result is not None
    assert result.status is RunStatus.FAILED
    assert result.stop is LoopStop.INTERNAL_ERROR
    assert result.error_code == "internal_error"
    assert "hook bug" in result.detail
    assert agent.state.error_message is not None


def test_agent_reports_no_result_before_the_first_run() -> None:
    agent = Agent(stream_fn=stepping(assistant(TextContent(text="x"))))
    assert agent.last_result is None
