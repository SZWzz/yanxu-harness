"""S3：只有一个执行内核，Runner 驱动 reducer，工具并发有明确上限。

计划 S3 的验收：旧入口、结构化入口、Agent 与 Harness 都走同一个 Runner；不存在两套模型
请求调度；事件与结果顺序不因并发数改变；混合批次遇等待时停止推进并保留每一项的证据。
"""

from __future__ import annotations

import asyncio
from typing import Any

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
from yanxu_harness.core import agent_loop, loop_config, runner
from yanxu_harness.core.agent import Agent
from yanxu_harness.core.execution import CallOutcome
from yanxu_harness.core.outcome import LoopStop, RunStatus
from yanxu_harness.core.types import AgentContext, StopReason, TextContent, ToolResultMessage

TOOL_NAMES = [f"t{index}" for index in range(5)]


def context_with(tools=()) -> AgentContext:
    return AgentContext(messages=[], system_prompt="", tools=list(tools))


def five_call_batch() -> list[Any]:
    return [tool_call(f"c{index}", name) for index, name in enumerate(TOOL_NAMES)]


# --- one kernel, one dispatch path ----------------------------------------------------


def test_the_compatibility_module_reexports_the_same_objects() -> None:
    assert agent_loop.run_agent_loop is runner.run_agent_loop
    assert agent_loop.run_agent_loop_result is runner.run_agent_loop_result
    assert agent_loop.run_agent_loop_continue is runner.run_agent_loop_continue
    assert agent_loop.run_agent_loop_continue_result is runner.run_agent_loop_continue_result
    assert agent_loop.AgentLoopConfig is loop_config.AgentLoopConfig
    assert agent_loop.validate_max_turns is loop_config.validate_max_turns
    from yanxu_harness.runtime import default_harness

    assert default_harness.run_agent_loop_result is runner.run_agent_loop_result, (
        "the harness must drive the same kernel, not a second loop"
    )


def test_the_old_loop_body_is_gone() -> None:
    for name in ("_run_loop", "_LoopEnd", "_loop_result", "_declare_tool_changes", "_fail_truncated_batch"):
        assert not hasattr(agent_loop, name), f"{name} still exists: two loops, one of them stale"


def test_every_entry_point_calls_the_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    real_run = runner.Runner.run
    real_continue = runner.Runner.run_continue

    async def spy_run(self, prompts, context, signal):
        calls.append("run")
        return await real_run(self, prompts, context, signal)

    async def spy_continue(self, context, signal):
        calls.append("continue")
        return await real_continue(self, context, signal)

    monkeypatch.setattr(runner.Runner, "run", spy_run)
    monkeypatch.setattr(runner.Runner, "run_continue", spy_continue)

    stream = stepping(assistant(TextContent(text="答")))
    run(
        runner.run_agent_loop(
            [user("hi")], context_with(), loop_config.AgentLoopConfig(stream_fn=stream), None, RecordingSink()
        )
    )
    assert calls == ["run"]
    calls.clear()

    async def scenario() -> None:
        # run: a plain prompt. continue: a transcript whose tail is a tool result, which
        # is the only shape that reaches the continue entry point.
        from yanxu_harness.core.agent import AgentState
        from yanxu_harness.core.types import ToolResultMessage

        seeding = AgentState(messages=[user("问"), ToolResultMessage(tool_call_id="c1", tool_name="t")])
        agent = Agent(stream_fn=stream, initial_state=seeding)
        # continue first: only a non-assistant tail reaches `_run_continuation`.
        await agent.continue_run_result()
        await agent.prompt("hi")

    run(scenario())
    assert calls == ["continue", "run"]


def test_the_runner_returns_the_reason_the_state_decided() -> None:
    """A stop hook is `completed`+`stop_hook`, a budget stop is `usage_limited`: the
    runner returns what the reducer decided, it does not re-derive it."""
    stream = stepping(assistant(TextContent(text="答")))
    config = loop_config.AgentLoopConfig(stream_fn=stream, should_stop_after_turn=lambda _turn: True)
    result = run(runner.run_agent_loop_result([user("hi")], context_with(), config, None, RecordingSink()))
    assert result.status is RunStatus.COMPLETED
    assert result.stop is LoopStop.STOP_HOOK


# --- explicit tool concurrency --------------------------------------------------------


def concurrency_probe(counter: dict[str, int]) -> Any:
    async def execute(call_id: str, _args: Any, _signal: Any = None, _on_update: Any = None) -> dict:
        counter["active"] += 1
        counter["peak"] = max(counter["peak"], counter["active"])
        counter["started"].append(call_id)
        # A few yields so overlapping calls are actually observable.
        for _ in range(3):
            await asyncio.sleep(0)
        counter["active"] -= 1
        return {"content": [TextContent(text=call_id)], "details": {}}

    return execute


def _batch_config(counter: dict[str, int], limit: int | None) -> loop_config.AgentLoopConfig:
    tools = [make_tool(name, concurrency_probe(counter)) for name in TOOL_NAMES]
    stream = stepping(
        assistant(*five_call_batch(), stop=StopReason.TOOL_USE),
        assistant(TextContent(text="收尾")),
    )
    return loop_config.AgentLoopConfig(
        stream_fn=stream, tools=tools, max_concurrent_tools=limit
    )


@pytest.mark.parametrize("limit", [None, 5])
def test_without_a_limit_every_call_may_run_at_once(limit: int | None) -> None:
    counter = {"active": 0, "peak": 0, "started": []}
    config = _batch_config(counter, limit)
    sink = RecordingSink()
    result = run(
        runner.run_agent_loop_result([user("go")], context_with(config.tools), config, None, sink)
    )
    assert counter["peak"] == 5
    assert result.status is RunStatus.COMPLETED
    assert [m.tool_call_id for m in result.messages if isinstance(m, ToolResultMessage)] == [
        f"c{index}" for index in range(5)
    ]


def test_a_concurrency_limit_is_observed_and_does_not_reorder_results() -> None:
    counter = {"active": 0, "peak": 0, "started": []}
    config = _batch_config(counter, 2)
    sink = RecordingSink()
    result = run(
        runner.run_agent_loop_result([user("go")], context_with(config.tools), config, None, sink)
    )

    assert counter["peak"] == 2, "no more than the declared limit may execute at once"
    assert counter["active"] == 0
    results = [m.tool_call_id for m in result.messages if isinstance(m, ToolResultMessage)]
    assert results == [f"c{index}" for index in range(5)], "result messages stay in source order"
    starts = [e.tool_call_id for e in sink.events if e.type == "tool_execution_start"]
    assert starts == [f"c{index}" for index in range(5)], "preflight stays in source order"


def test_a_limit_of_one_serialises_execution_but_keeps_correlation() -> None:
    counter = {"active": 0, "peak": 0, "started": []}
    config = _batch_config(counter, 1)
    result = run(
        runner.run_agent_loop_result([user("go")], context_with(config.tools), config, None, RecordingSink())
    )
    assert counter["peak"] == 1
    assert counter["started"] == [f"c{index}" for index in range(5)]
    assert [m.tool_call_id for m in result.messages if isinstance(m, ToolResultMessage)] == [
        f"c{index}" for index in range(5)
    ]


@pytest.mark.parametrize("invalid", [0, -1, True, 1.5, "2"])
def test_an_invalid_concurrency_limit_is_refused_before_any_event(invalid: Any) -> None:
    counter = {"active": 0, "peak": 0, "started": []}
    config = _batch_config(counter, invalid)  # type: ignore[arg-type]
    sink = RecordingSink()
    with pytest.raises(ValueError):
        run(runner.run_agent_loop_result([user("go")], context_with(config.tools), config, None, sink))
    assert sink.events == [], "the refusal must happen before agent_start"
    assert counter["started"] == []


# --- a mixed batch keeps every call's evidence ----------------------------------------


def test_a_mixed_batch_pauses_without_a_next_request_and_keeps_each_status() -> None:
    """One completed call and two waiting/unknown ones: three distinct facts, one pause.

    The waiting call must not be reported to the model as a completed effect, and the
    unknown one must not look replayable.
    """
    statuses = {"c0": "executed", "c1": "awaiting_approval", "c2": "unknown"}

    async def execute(call_id: str, _args: Any, _signal: Any = None, _on_update: Any = None) -> dict:
        status = statuses[call_id]
        details: dict[str, Any] = {"status": status, "operation_id": f"op-{call_id}"}
        if status == "awaiting_approval":
            details["approval"] = {"binding_key": f"ns:{call_id}", "status": "pending"}
        return {
            "content": [TextContent(text=f"{call_id}:{status}")],
            "details": details,
            "is_error": status != "executed",
        }

    calls = [tool_call("c0", "work"), tool_call("c1", "work"), tool_call("c2", "work")]
    tools = [make_tool("work", execute)]
    stream = stepping(
        assistant(*calls, stop=StopReason.TOOL_USE),
        assistant(TextContent(text="不应请求下一轮")),
    )
    config = loop_config.AgentLoopConfig(stream_fn=stream, tools=tools)
    sink = RecordingSink()
    result = run(
        runner.run_agent_loop_result([user("go")], context_with(tools), config, None, sink)
    )

    assert len(stream.calls) == 1, "a waiting call must not produce another model request"
    assert result.status is RunStatus.SUSPENDED
    assert result.pending_interaction["kind"] == "approval"
    assert result.pending_interaction["call_id"] == "c1"
    reported = {m.tool_call_id: m for m in result.messages if isinstance(m, ToolResultMessage)}
    assert set(reported) == {"c0", "c1", "c2"}
    assert reported["c0"].details["status"] == "executed"
    assert reported["c1"].details["status"] == "awaiting_approval"
    assert reported["c2"].details["status"] == "unknown"
    # `is_error` for a waiting call is the gateway adapter's job (see
    # tests/test_tools_core_bridge.py); what this test pins is that the loop stops here.
    assert sink.types[-1] == "agent_end"


def test_the_reducer_partitions_a_partial_batch_into_all_four_outcomes() -> None:
    """The un-reached call of a batch is `unstarted` (replayable), not a failure."""
    from yanxu_harness.core.execution import classify_results
    from yanxu_harness.core.outcome import CallOutcome

    def result(call_id: str, status: str) -> ToolResultMessage:
        return ToolResultMessage(
            tool_call_id=call_id, tool_name="work", details={"status": status}
        )

    calls = [tool_call(name, "work") for name in ("c0", "c1", "c2", "c3")]
    batch = classify_results(
        calls, [result("c0", "executed"), result("c1", "awaiting_approval"), result("c2", "unknown")]
    )
    assert [call.outcome for call in batch] == [
        CallOutcome.SETTLED,
        CallOutcome.AWAITING,
        CallOutcome.UNCERTAIN,
        CallOutcome.UNSTARTED,
    ]
    assert batch[3].result is None
    assert batch[3].outcome.replayable is True
