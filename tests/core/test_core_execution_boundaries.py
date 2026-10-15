"""S0：把旧双层循环的边界冻成断言，并复现 S1 必须修掉的两个缺口。

来源：`docs/core-state-machine-migration.zh-CN.md`。这些用例只描述**今天的**可观察行为：

* `test_steering_*` / `test_follow_up_*` 冻结输入消费顺序（计划 S0 第 3 条，验收矩阵 C02）；
* `test_length_*` / `test_cancellation_*` 冻结停止优先级交叉（C04）；
* `test_legacy_list_entry_*` 复现的「旧列表入口吞掉暂停」已由 S1 收紧：现在抛携带可读
  `LoopResult` 的 `LoopSuspended`（见 `tests/core/test_core_execution_outcomes.py`）。

改动这些断言就是改动契约：先看迁移表对应行，说明理由，再改。
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
from yanxu_harness.core.agent_loop import (
    AgentLoopConfig,
    run_agent_loop,
    run_agent_loop_result,
)
from yanxu_harness.core.cancellation import CancellationToken
from yanxu_harness.core.outcome import LoopStop, LoopSuspended, RunStatus
from yanxu_harness.core.types import AgentContext, StopReason, TextContent, ToolResultMessage


def context_with(tools=()) -> AgentContext:
    return AgentContext(messages=[], system_prompt="", tools=list(tools))


def user_contents(messages) -> list[str]:
    return [m.content for m in messages if getattr(m, "role", None) == "user"]


def result_messages(messages) -> list[ToolResultMessage]:
    return [m for m in messages if isinstance(m, ToolResultMessage)]


# --- C02：steering 与 follow-up 同时存在，只消费一次 ---------------------------------


def test_steering_is_consumed_at_the_turn_boundary_before_any_follow_up() -> None:
    """steering 在轮次边界先被消费；follow-up 只在内层自然结束后才被读取。"""
    steering = [user("插话 1"), user("插话 2")]
    follow_ups = [user("追问 1"), user("追问 2")]
    steering_polls = 0
    follow_up_polls = 0

    def drain_steering():
        # one-at-a-time：每次边界只取一条，剩下的留给下一次边界。
        nonlocal steering_polls
        steering_polls += 1
        return [steering.pop(0)] if steering else []

    def drain_follow_ups():
        nonlocal follow_up_polls
        follow_up_polls += 1
        return [follow_ups.pop(0)] if follow_ups else []

    stream = stepping(
        assistant(TextContent(text="第 1 答")),
        assistant(TextContent(text="第 2 答")),
        assistant(TextContent(text="第 3 答")),
        assistant(TextContent(text="第 4 答")),
        assistant(TextContent(text="第 5 答")),
        assistant(TextContent(text="第 6 答")),
    )
    sink = RecordingSink()
    result = run(
        run_agent_loop_result(
            [user("go")],
            context_with(),
            AgentLoopConfig(
                stream_fn=stream,
                get_steering_messages=drain_steering,
                get_follow_ups=drain_follow_ups,
            ),
            None,
            sink,
        )
    )

    # 4 次请求：初始 1（含循环开头读取的第一条 steering）+ 第二条 steering 一轮 + 两条 follow-up 各一轮。
    assert len(stream.calls) == 4
    assert sink.count("turn_start") == 4
    assert result.status is RunStatus.COMPLETED
    assert result.stop is LoopStop.ASSISTANT_FINAL
    assert result.turns_used == 4
    # 消费顺序：循环开头读到的那条 steering 进入首个请求；其余 steering 在轮次边界；
    # follow-up 只在内层自然结束后被读取。每条只进入一次请求。
    assert user_contents(result.messages) == ["go", "插话 1", "插话 2", "追问 1", "追问 2"]
    assert steering == [] and follow_ups == []
    assert steering_polls == 5, "循环开头一次 + 每次轮次边界一次"
    assert follow_up_polls == 3, "内层每次退出后读取一次；第 3 次为空后结束"


def test_steering_is_not_replayed_into_the_first_request_twice() -> None:
    """steering 只在轮次边界进入请求；同一轮里不会既当初始消息又被取出一次。"""
    steering = [user("只应出现一次")]
    polls = 0

    def drain_steering():
        nonlocal polls
        polls += 1
        return [steering.pop(0)] if steering else []

    stream = stepping(assistant(TextContent(text="第 1 答")), assistant(TextContent(text="第 2 答")))
    result = run(
        run_agent_loop_result(
            [user("go")],
            context_with(),
            AgentLoopConfig(stream_fn=stream, get_steering_messages=drain_steering),
            None,
            RecordingSink(),
        )
    )

    assert user_contents(result.messages) == ["go", "只应出现一次"]
    assert polls == 2


# --- C04：length / 停止 hook / batch terminate / 取消的交叉优先级 ----------------------


def test_a_length_truncated_batch_never_runs_even_when_a_stop_hook_would_fire() -> None:
    """停止优先级：length 截断的调用一律不执行；stop hook 仍决定本次调用的结束原因。"""
    executions: list[str] = []

    async def execute(call_id, _args, _signal=None, _on_update=None):
        executions.append(call_id)
        return {"content": [TextContent(text="不该执行")], "details": {}}

    tool = make_tool("t", execute)
    stream = stepping(
        assistant(tool_call("c1", "t"), stop=StopReason.LENGTH),
        assistant(TextContent(text="不应请求下一轮")),
    )
    sink = RecordingSink()
    result = run(
        run_agent_loop_result(
            [user("go")],
            context_with([tool]),
            AgentLoopConfig(
                stream_fn=stream,
                tools=[tool],
                should_stop_after_turn=lambda _turn: True,
            ),
            None,
            sink,
        )
    )

    assert executions == [], "truncated arguments must never reach the tool"
    assert len(stream.calls) == 1
    assert result.status is RunStatus.COMPLETED
    assert result.stop is LoopStop.STOP_HOOK
    assert result.turns_used == 1
    results = result_messages(result.messages)
    assert len(results) == 1 and results[0].is_error is True
    assert sink.count("tool_execution_end") == 1
    assert sink.types[-1] == "agent_end"


def test_a_terminating_batch_does_not_hide_a_cancellation_that_arrived_between_turns() -> None:
    """取消优先于「批次终止即完成」：终止批次之后到达的取消仍报 CANCELLED。"""
    token = CancellationToken("s0:terminate-cancel")

    async def execute(_call_id, _args, signal=None, _on_update=None):
        token.cancel("host stopped the run")
        return {"content": [TextContent(text="已终止")], "details": {}, "terminate": True}

    tool = make_tool("finish", execute)
    stream = stepping(
        assistant(tool_call("c1", "finish"), stop=StopReason.TOOL_USE),
        assistant(TextContent(text="不应请求下一轮")),
    )
    sink = RecordingSink()
    result = run(
        run_agent_loop_result(
            [user("go")],
            context_with([tool]),
            AgentLoopConfig(stream_fn=stream, tools=[tool]),
            token,
            sink,
        )
    )

    assert len(stream.calls) == 1
    assert result.status is RunStatus.CANCELLED
    assert result.stop is LoopStop.ABORTED
    assert result.error_code == "cancelled"
    # 真实结算过的结果不因取消而丢弃：批次已经跑完，证据要保留。
    assert [m.is_error for m in result_messages(result.messages)] == [False]


# --- 待改问题（S1）：旧列表入口无法表达暂停 --------------------------------------------


@pytest.mark.parametrize("entry", ["result", "list"])
def test_a_suspend_hook_stops_before_the_next_request_in_both_entries(entry: str) -> None:
    """结构化入口报 SUSPENDED；旧列表入口抛携带同一结果的 LoopSuspended（S1 已收紧）。"""
    interaction = {"kind": "approval", "call_id": "c1"}
    stream = stepping(
        assistant(tool_call("c1", "write_note"), stop=StopReason.TOOL_USE),
        assistant(TextContent(text="不应请求下一轮")),
    )
    tool = make_tool("write_note", ok_result)
    config = AgentLoopConfig(
        stream_fn=stream,
        tools=[tool],
        should_suspend_after_batch=lambda _m, _r: interaction,
    )

    if entry == "result":
        result = run(
            run_agent_loop_result([user("write it")], context_with([tool]), config, None, RecordingSink())
        )
        assert result.status is RunStatus.SUSPENDED
        assert result.pending_interaction == interaction
        assert result.terminal is False
    else:
        with pytest.raises(LoopSuspended) as excinfo:
            run(run_agent_loop([user("write it")], context_with([tool]), config, None, RecordingSink()))
        assert excinfo.value.result.status is RunStatus.SUSPENDED
        assert excinfo.value.result.pending_interaction == interaction

    assert len(stream.calls) == 1, "等待外部决定时不得产生第二次模型请求"
