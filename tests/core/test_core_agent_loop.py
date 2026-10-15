"""0.6.0 P03：低层模型循环，对照 `packages/agent/src/agent-loop.ts` @ c7cdb46。

每条断言注上游行号。测试用 **scripted StreamFn**（计划 §157 的注入点）：不起真模型、
不碰 vault、不导入应用层。覆盖即计划 §6 的 P03 那一行：事件完整顺序、新消息集合、
拒绝非法 continue、长度截断不执行工具；工具执行器在此阶段可注入。

`max_turns` 是登记过的差异（上游无轮数上限，计划 §115 要求）。
"""

import asyncio
from dataclasses import dataclass, field, fields
from typing import Any

import pytest

from yanxu_harness.core import agent_loop as agent_loop_module
from yanxu_harness.core import messages as messages_module
from yanxu_harness.core.agent_loop import AgentLoopConfig, run_agent_loop, run_agent_loop_continue
from yanxu_harness.core.cancellation import CancellationToken
from yanxu_harness.core.events import (
    AgentEnd,
    AgentStart,
    ToolExecutionEnd,
    ToolExecutionStart,
    ToolExecutionUpdate,
    TurnEnd,
)
from yanxu_harness.core.messages import (
    CustomMessageRegistry,
    UnknownMessageRole,
    normalize_context,
    to_llm_messages,
)
from core_loop_helpers import (
    RecordingSink,
    ScriptedStream,
    assistant,
    make_tool,
    ok_result,
    run,
    stepping,
    tool_call,
    user,
)
from yanxu_harness.core.types import (
    AgentContext,
    AssistantMessage,
    StopReason,
    SystemMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
    ToolSpec,
    UserMessage,
)


def context_with(*messages: Any, tools: list[ToolSpec] | None = None) -> AgentContext:
    return AgentContext(messages=list(messages), tools=tools if tools is not None else [])



def test_provider_context_carries_nothing_but_messages() -> None:
    """agent-loop.test.ts:167：送给 provider 的对象只有 `messages` 一个字段。

    顺带钉住一个宿主容易踩的陷阱：`AgentContext.system_prompt` **不会**被 loop 折进请求
    （`agent-loop.ts:355` 是 `normalizeContext({ messages: llmMessages })`）。system 要进模型，
    必须作为 transcript 里的那条 system 消息存在 —— `normalize_context` 自己会折（见
    `test_normalize_context_still_folds_the_prompt_into_the_request`），但 loop 不走那条路。
    P07 的 envelope 因此必须自己维护开头那条 system 消息。
    """
    stream = ScriptedStream(assistant(TextContent(text="答")))
    tools = [make_tool("t", ok_result)]
    context = AgentContext(messages=[user("问")], system_prompt="基线", tools=tools)
    config = AgentLoopConfig(stream_fn=stream, tools=tools)
    run(run_agent_loop([user("再问")], context, config, None, RecordingSink()))

    sent = stream.calls[0][1]
    assert {f.name for f in fields(sent)} == {"messages"}, "工具与 system 不能作为顶层字段漏给 provider"
    assert "基线" not in [getattr(m, "content", "") for m in sent.messages], "光挂 system_prompt 不进请求"
    assert sent.messages[0].role == "user", "请求开头不是 system，说明 system_prompt 被整个丢掉了"
    # 唯一那条 system 消息只能来自工具声明
    declared = [m for m in sent.messages if m.role == "system"]
    assert [t.name for m in declared for t in (m.tools_added or [])] == ["t"]

    # prompt 轮的 `system_prompt` 是被两处丢掉的：构造 current 时不带走（:347-351），
    # 请求边界又只传 messages（:355）。continue 轮与调用方共用同一个 context，只剩后一道，
    # 所以两条都要断言 —— 只断 prompt 那条时 T25 是等价变异。
    continue_ctx = AgentContext(messages=[user("问")], system_prompt="基线", tools=tools)
    second_stream = ScriptedStream(assistant(TextContent(text="答")))
    run(run_agent_loop_continue(continue_ctx, AgentLoopConfig(stream_fn=second_stream, tools=tools), None, RecordingSink()))
    assert "基线" not in [getattr(m, "content", "") for m in second_stream.calls[0][1].messages]

    with_message = AgentContext(
        messages=[SystemMessage(content="基线"), user("问")], system_prompt="基线", tools=tools
    )
    second = ScriptedStream(assistant(TextContent(text="答")))
    run(run_agent_loop([user("再问")], with_message, AgentLoopConfig(stream_fn=second, tools=tools), None, RecordingSink()))
    assert [m.role for m in second.calls[0][1].messages][:2] == ["system", "user"]


def test_events_carry_the_host_message_not_the_llm_projection() -> None:
    """agent-loop.test.ts:121：转换只发生在请求边界，宿主那份对象不被投影替换。

    打在 **continue** 上而不是 prompt 上：prompt 轮会复制数组（:111-114），投影写回发生在调用方
    看不见的那个 list 上 —— L8 第一版就是这么活下来的，同一个坑不踩第二次。
    宿主把 `message_end` 与 transcript 直接落盘，若这里被换成投影，自定义 role 会在落盘时
    悄悄变成 user，P09 迁移再也认不出原形状。
    """
    class FindingMessage:
        role = "finding"

        def __init__(self, text: str) -> None:
            self.text = text

    registry = CustomMessageRegistry()
    registry.register("finding", to_llm=lambda message: UserMessage(content=f"[发现] {message.text}"))
    finding = FindingMessage("伏笔没收")
    context = AgentContext(messages=[user("问"), finding])
    stream = ScriptedStream(assistant(TextContent(text="答")))
    config = AgentLoopConfig(stream_fn=stream, convert_to_llm=lambda ms: to_llm_messages(ms, registry=registry))

    new_messages = run(run_agent_loop_continue(context, config, None, RecordingSink()))

    sent = [m.content for m in stream.calls[0][1].messages if m.role == "user"]
    assert sent == ["问", "[发现] 伏笔没收"], "请求里是投影"
    assert any(m is finding for m in context.messages), "跑完之后宿主 transcript 里仍是原对象"
    assert finding not in new_messages, "原历史不算新增消息"


def test_prompt_run_emits_the_upstream_event_skeleton() -> None:
    """agent-loop.ts:116-123。"""
    stream = ScriptedStream(assistant(TextContent(text="写好了")))
    sink = RecordingSink()
    prompt = user("把第三章收紧一点")

    new_messages = run(run_agent_loop([prompt], context_with(), AgentLoopConfig(stream_fn=stream), None, sink))

    assert sink.types == [
        "agent_start", "turn_start",
        "message_start", "message_end",        # 提示词
        "message_start", "message_end",        # assistant
        "turn_end", "agent_end",
    ]
    assert isinstance(sink.events[0], AgentStart) and isinstance(sink.events[-1], AgentEnd)
    assert prompt in new_messages, "prompt 轮的新消息集合含提示词本身"
    assert [m for m in new_messages if isinstance(m, AssistantMessage)][0].content[0].text == "写好了"


def test_tool_round_adds_a_second_turn_and_returns_results_in_order() -> None:
    stream = stepping(
        assistant(tool_call("c1", "write_chapter"), stop=StopReason.TOOL_USE),
        assistant(TextContent(text="已写入")),
    )
    tools = [make_tool("write_chapter", ok_result)]
    sink = RecordingSink()
    config = AgentLoopConfig(stream_fn=stream, tools=tools)

    new_messages = run(run_agent_loop([user("写")], context_with(tools=tools), config, None, sink))

    assert sink.count("turn_start") == 2 and sink.count("turn_end") == 2
    assert sink.count("tool_execution_start") == 1 and sink.count("tool_execution_end") == 1
    results = [m for m in new_messages if isinstance(m, ToolResultMessage)]
    assert [(m.tool_call_id, m.is_error) for m in results] == [("c1", False)]
    assert stream.calls[-1][1].messages[-1].role == "toolResult", "第二次请求必须带上工具结果"


def test_late_tool_updates_are_dropped() -> None:
    """agent-loop.ts:737-772：`acceptingUpdates` 在 execute 落定后置假。"""
    captured: dict[str, Any] = {}

    async def execute(_id, _args, _signal=None, on_update=None):
        on_update({"content": [TextContent(text="半截")], "details": {}})
        captured["on_update"] = on_update
        return {"content": [TextContent(text="完成")], "details": {}}

    stream = ScriptedStream(assistant(tool_call("c1", "t"), stop=StopReason.TOOL_USE))
    tools = [make_tool("t", execute)]
    sink = RecordingSink()
    config = AgentLoopConfig(stream_fn=stream, tools=tools)

    async def scenario():
        await run_agent_loop([user("go")], context_with(tools=tools), config, None, sink)
        captured["on_update"]({"content": [TextContent(text="迟到的")], "details": {}})
        await asyncio.sleep(0)

    run(scenario())
    assert sink.count("tool_execution_update") == 1
    assert isinstance(sink.of(ToolExecutionUpdate)[0], ToolExecutionUpdate)


# --- 失败与截断 ---------------------------------------------------------------


@pytest.mark.parametrize("stop", [StopReason.ERROR, StopReason.ABORTED])
def test_error_or_aborted_ends_the_run_without_executing_tools(stop: StopReason) -> None:
    """agent-loop.ts:221-225：turn_end(toolResults=[]) 之后立刻 agent_end。"""
    async def never(*_a, **_k):
        raise AssertionError("不该被执行")

    stream = ScriptedStream(assistant(tool_call("c1", "t"), stop=stop))
    tools = [make_tool("t", never)]
    sink = RecordingSink()
    config = AgentLoopConfig(stream_fn=stream, tools=tools)

    new_messages = run(run_agent_loop([user("go")], context_with(tools=tools), config, None, sink))

    assert sink.types[-2:] == ["turn_end", "agent_end"]
    assert sink.of(TurnEnd)[-1].tool_results == ()
    assert sink.of(ToolExecutionStart) == []
    assert new_messages[-1].stop_reason is stop


def test_length_truncation_fails_every_tool_call_instead_of_running_them() -> None:
    """agent-loop.ts:232-239 + 427-459。

    流式参数有 best-effort JSON 兜底解析，被截断的调用「参数能解析成功」也不安全 ——
    这正是计划 §100 那句「长度截断即使抢救出了合法 JSON，也不得执行该批工具」。
    """
    executed: list[str] = []

    async def execute(_id, _args, _signal=None, _on_update=None):
        executed.append("ran")
        return {"content": [], "details": {}}

    stream = ScriptedStream(
        assistant(
            tool_call("c1", "write_chapter", {"path": "a.md", "content": "写到一半被截"}),
            stop=StopReason.LENGTH,
        )
    )
    tools = [make_tool("write_chapter", execute)]
    sink = RecordingSink()
    # 本用例测的是「截断不执行」，不是收敛性；不封顶会因脚本流重复返回同一条消息而无限续轮
    config = AgentLoopConfig(stream_fn=stream, tools=tools, max_turns=2)

    new_messages = run(run_agent_loop([user("go")], context_with(tools=tools), config, None, sink))

    assert executed == [], "长度截断的批次里一个工具都不能跑"
    results = [m for m in new_messages if isinstance(m, ToolResultMessage)]
    assert [(m.tool_call_id, m.is_error) for m in results] == [("c1", True)]
    assert "output token limit" in results[0].content[0].text
    assert sink.count("tool_execution_end") == 1, "仍要发 end，让 UI 拿到这一笔的结论"


def test_truncated_batch_does_not_terminate_the_run() -> None:
    """agent-loop.ts:458 返回 terminate: false —— 错误结果要还给模型重发。"""
    async def never(*_a, **_k):
        raise AssertionError("不应执行")

    stream = stepping(
        assistant(tool_call("c1", "t"), stop=StopReason.LENGTH),
        assistant(TextContent(text="重发成功")),
    )
    tools = [make_tool("t", never)]
    sink = RecordingSink()
    config = AgentLoopConfig(stream_fn=stream, tools=tools)

    run(run_agent_loop([user("go")], context_with(tools=tools), config, None, sink))

    assert sink.count("turn_start") == 2
    assert sink.types[-1] == "agent_end"


# --- continue 的前置拒绝 ------------------------------------------------------


def test_continue_refuses_empty_transcript_and_assistant_tail() -> None:
    """agent-loop.ts:76-82 与 134-140。"""
    sink = RecordingSink()
    with pytest.raises(ValueError, match="Cannot continue"):
        run(run_agent_loop_continue(context_with(), AgentLoopConfig(stream_fn=ScriptedStream(assistant())), None, sink))

    bad = context_with(user("问"), assistant(TextContent(text="答")))
    with pytest.raises(ValueError, match="assistant"):
        run(run_agent_loop_continue(bad, AgentLoopConfig(stream_fn=ScriptedStream(assistant())), None, sink))

    assert sink.events == [], "被拒绝的运行不得先抛出半截事件"


def test_continue_from_tool_result_tail_runs_without_readding_history() -> None:
    result = ToolResultMessage(tool_call_id="c1", tool_name="t", content=[TextContent(text="ok")])
    stream = ScriptedStream(assistant(TextContent(text="收尾")))
    sink = RecordingSink()

    new_messages = run(
        run_agent_loop_continue(context_with(user("问"), result), AgentLoopConfig(stream_fn=stream), None, sink)
    )

    assert sink.types[:2] == ["agent_start", "turn_start"]
    assert result not in new_messages, "continue 只返回新增消息"
    assert len(new_messages) == 1


# --- 停止条件、steering 与 prepareNextTurn ------------------------------------


def test_should_stop_after_turn_stops_right_after_turn_end() -> None:
    """agent-loop.ts:258-261：判定在 turn_end 之后、消费队列之前（计划 §107）。"""
    stream = ScriptedStream(assistant(TextContent(text="一句")))
    sink = RecordingSink()
    asked: list[str] = []

    async def should_stop(_context):
        asked.append("asked")
        return True

    config = AgentLoopConfig(stream_fn=stream, should_stop_after_turn=should_stop)
    run(run_agent_loop([user("go")], context_with(), config, None, sink))

    assert asked == ["asked"]
    # 提示词与 assistant 各发一对 start/end，所以是 8 个事件而不是 6 个
    assert sink.types == [
        "agent_start", "turn_start",
        "message_start", "message_end",
        "message_start", "message_end",
        "turn_end", "agent_end",
    ]


def test_steering_polled_at_turn_end_enters_the_next_request() -> None:
    """agent-loop.ts:263 + 209-215。"""
    calls = [
        assistant(tool_call("c1", "t"), stop=StopReason.TOOL_USE),
        assistant(TextContent(text="收")),
    ]
    stream = stepping(*calls)
    queue = [user("插一句：换个视角")]
    polls: list[int] = []

    async def get_steering():
        # 第一次是循环开头的轮询（队里没有）；回合末尾那次才拿到插话
        polls.append(len(queue))
        return [queue.pop(0)] if (queue and len(polls) > 1) else []

    tools = [make_tool("t", ok_result)]
    sink = RecordingSink()
    config = AgentLoopConfig(stream_fn=stream, tools=tools, get_steering_messages=get_steering)

    new_messages = run(run_agent_loop([user("开始")], context_with(tools=tools), config, None, sink))

    assert len(polls) >= 2, "开头与回合末尾各轮询一次"
    first_roles = [m.role for m in stream.calls[0][1].messages]
    last_roles = [m.role for m in stream.calls[-1][1].messages]
    assert last_roles.count("user") == first_roles.count("user") + 1, "插话只进第二次请求"
    assert any(isinstance(m, UserMessage) and "换个视角" in str(m.content) for m in new_messages)


def test_persistent_context_transform_compacts_dispatch_but_keeps_raw_tool_result() -> None:
    """运行中投影只改后续请求视图；原始本轮增量仍完整返回给宿主归档。"""
    tool_result = "工具原文" * 300

    async def execute(_id, _args, _signal=None, _on_update=None):
        return {"content": [TextContent(text=tool_result)], "details": {"source": "raw"}}

    stream = stepping(
        assistant(tool_call("c1", "read_large"), stop=StopReason.TOOL_USE),
        assistant(TextContent(text="已根据结果继续。")),
    )
    tools = [make_tool("read_large", execute)]
    seen: list[list[Any]] = []

    async def project(messages, _signal=None):
        seen.append(list(messages))
        if not any(isinstance(message, ToolResultMessage) for message in messages):
            return messages
        # 模拟把完整工具批次纳入摘要，同时保留追加到本轮末尾的作者消息。
        return [SystemMessage(content="已读取大结果：结论是通过。"), messages[-1]]

    sink = RecordingSink()
    config = AgentLoopConfig(
        stream_fn=stream,
        tools=tools,
        transform_context=project,
        persist_transformed_context=True,
    )
    new_messages = run(run_agent_loop([user("开始")], context_with(tools=tools), config, None, sink))

    second_request = stream.calls[1][1].messages
    assert [message.content for message in second_request if isinstance(message, SystemMessage)] == [
        "已读取大结果：结论是通过。"
    ]
    assert any(isinstance(message, ToolResultMessage) and tool_result in message.content[0].text for message in new_messages)
    assert any(isinstance(message, AssistantMessage) and any(
        getattr(block, "id", None) == "c1" for block in message.content
    ) for message in new_messages)
    assert len(seen) == 2


def test_tool_batch_commit_hook_runs_before_next_turn_preparation() -> None:
    order: list[str] = []
    stream = stepping(
        assistant(tool_call("c1", "read"), stop=StopReason.TOOL_USE),
        assistant(TextContent(text="继续")),
    )
    tools = [make_tool("read", ok_result)]

    async def persist(_message, results):
        order.append("persist")
        assert [result.tool_call_id for result in results] == ["c1"]
        assert results[0].tool_name == "read"

    async def prepare(_completed):
        order.append("prepare")
        return None

    config = AgentLoopConfig(
        stream_fn=stream,
        tools=tools,
        on_tool_batch_complete=persist,
        prepare_next_turn=prepare,
    )
    run(run_agent_loop([user("开始")], context_with(tools=tools), config, None, RecordingSink()))

    assert order == ["persist", "prepare"]


def test_follow_up_restarts_the_inner_loop_when_the_agent_would_stop() -> None:
    """agent-loop.ts:266-272。"""
    stream = stepping(assistant(TextContent(text="第一段")), assistant(TextContent(text="第二段")))
    follow_ups = [[user("还有件事")]]

    async def get_follow_ups():
        return follow_ups.pop(0) if follow_ups else []

    sink = RecordingSink()
    config = AgentLoopConfig(stream_fn=stream, get_follow_ups=get_follow_ups)
    new_messages = run(run_agent_loop([user("第一问")], context_with(), config, None, sink))

    assert len([m for m in new_messages if isinstance(m, AssistantMessage)]) == 2
    assert sink.count("agent_end") == 1, "一次运行只有一个 agent_end"


def test_prepare_next_turn_can_swap_context_without_rewriting_the_finished_turn() -> None:
    """agent-loop.ts:183-198：快照取自已完成回合，替换只影响下一次请求。"""
    replaced = context_with(user("压缩后的历史"))
    stream = stepping(
        assistant(tool_call("c1", "t"), stop=StopReason.TOOL_USE),
        assistant(TextContent(text="后")),
    )
    tools = [make_tool("t", ok_result)]
    sink = RecordingSink()
    calls: list[int] = []

    async def prepare(context):
        calls.append(len(context.new_messages))
        return {"context": replaced}

    config = AgentLoopConfig(stream_fn=stream, tools=tools, prepare_next_turn=prepare)
    run(run_agent_loop([user("go")], context_with(tools=tools), config, None, sink))

    assert len(calls) == 1, "prepareNextTurn 只在第二个回合前被调用一次"
    assert stream.calls[-1][1].messages[-1].content == "压缩后的历史"


# --- 工具批次语义 -------------------------------------------------------------


def test_terminate_only_when_every_finalized_result_sets_it() -> None:
    """agent-loop.ts:644-646：混合批次继续。"""
    async def a(_id, _args, _signal=None, _on_update=None):
        return {"content": [], "details": {}, "terminate": True}

    async def b(_id, _args, _signal=None, _on_update=None):
        return {"content": [], "details": {}, "terminate": False}

    stream = stepping(
        assistant(tool_call("c1", "a"), tool_call("c2", "b"), stop=StopReason.TOOL_USE),
        assistant(TextContent(text="继续跑完")),
    )
    tools = [make_tool("a", a), make_tool("b", b)]
    sink = RecordingSink()
    config = AgentLoopConfig(stream_fn=stream, tools=tools)
    run(run_agent_loop([user("go")], context_with(tools=tools), config, None, sink))

    assert sink.count("turn_start") == 2


def test_all_terminate_ends_the_run_after_that_turn() -> None:
    async def a(_id, _args, _signal=None, _on_update=None):
        return {"content": [], "details": {}, "terminate": True}

    stream = ScriptedStream(assistant(tool_call("c1", "a"), stop=StopReason.TOOL_USE))
    tools = [make_tool("a", a)]
    sink = RecordingSink()
    config = AgentLoopConfig(stream_fn=stream, tools=tools)
    run(run_agent_loop([user("go")], context_with(tools=tools), config, None, sink))

    assert sink.count("turn_start") == 1
    assert sink.types[-1] == "agent_end"


def test_parallel_batch_emits_end_in_completion_order_but_results_in_source_order() -> None:
    """agent-loop.ts:542-616（计划 §105）。"""
    release = asyncio.Event()
    fast_done = asyncio.Event()

    async def slow(_id, _args, _signal=None, _on_update=None):
        await release.wait()
        return {"content": [TextContent(text="慢")], "details": {}}

    async def fast(_id, _args, _signal=None, _on_update=None):
        fast_done.set()
        return {"content": [TextContent(text="快")], "details": {}}

    stream = stepping(
        assistant(tool_call("c1", "slow"), tool_call("c2", "fast"), stop=StopReason.TOOL_USE),
        assistant(TextContent(text="批次收尾")),
    )
    tools = [make_tool("slow", slow), make_tool("fast", fast)]
    sink = RecordingSink()
    config = AgentLoopConfig(stream_fn=stream, tools=tools)

    async def scenario():
        task = asyncio.create_task(
            run_agent_loop([user("go")], context_with(tools=tools), config, None, sink)
        )
        # 等事件，不用忙轮询：忙等会把「快的那个先跑完」变成看运气
        await asyncio.wait_for(fast_done.wait(), timeout=4)
        release.set()
        return await task

    new_messages = run(scenario())

    assert [e.tool_name for e in sink.of(ToolExecutionEnd)] == ["fast", "slow"], "end 按完成顺序"
    results = [m for m in new_messages if isinstance(m, ToolResultMessage)]
    assert [m.tool_call_id for m in results] == ["c1", "c2"], "结果消息按调用源顺序"
    assert [e.tool_name for e in sink.of(ToolExecutionStart)] == ["slow", "fast"], "start/preflight 按源顺序"


def test_any_sequential_tool_makes_the_whole_batch_sequential() -> None:
    """agent-loop.ts:471-478（计划 §105）。"""
    order: list[str] = []

    async def p(_id, _args, _signal=None, _on_update=None):
        order.append("p-start")
        await asyncio.sleep(0)
        order.append("p-end")
        return {"content": [], "details": {}}

    async def s(_id, _args, _signal=None, _on_update=None):
        order.append("s-start")
        await asyncio.sleep(0)
        order.append("s-end")
        return {"content": [], "details": {}}

    stream = ScriptedStream(assistant(tool_call("c1", "p"), tool_call("c2", "s"), stop=StopReason.TOOL_USE))
    tools = [make_tool("p", p), make_tool("s", s, execution_mode="sequential")]
    sink = RecordingSink()
    config = AgentLoopConfig(stream_fn=stream, tools=tools)
    run(run_agent_loop([user("go")], context_with(tools=tools), config, None, sink))

    assert order == ["p-start", "p-end", "s-start", "s-end"]


def test_before_tool_call_block_produces_an_error_result_without_executing() -> None:
    """agent-loop.ts:698-708。block 只是错误工具结果，不是 StoryIDE 的持久审批（计划 §123）。"""
    executed: list[str] = []

    async def execute(_id, _args, _signal=None, _on_update=None):
        executed.append("ran")
        return {"content": [], "details": {}}

    async def before(_ctx, _signal=None):
        return {"block": True, "reason": "作者拒绝"}

    stream = ScriptedStream(assistant(tool_call("c1", "t"), stop=StopReason.TOOL_USE))
    tools = [make_tool("t", execute)]
    sink = RecordingSink()
    config = AgentLoopConfig(stream_fn=stream, tools=tools, before_tool_call=before)
    new_messages = run(run_agent_loop([user("go")], context_with(tools=tools), config, None, sink))

    assert executed == []
    result = [m for m in new_messages if isinstance(m, ToolResultMessage)][0]
    assert result.is_error and "作者拒绝" in result.content[0].text


def test_after_tool_call_rewrites_model_visible_content_only() -> None:
    """agent-loop.ts:786-813：没给的字段保留原值，无深合并。"""
    async def execute(_id, _args, _signal=None, _on_update=None):
        return {"content": [TextContent(text="原始")], "details": {"n": 1}}

    async def after(_ctx, _signal=None):
        return {"content": [TextContent(text="改写后")]}

    stream = ScriptedStream(assistant(tool_call("c1", "t"), stop=StopReason.TOOL_USE))
    tools = [make_tool("t", execute)]
    sink = RecordingSink()
    config = AgentLoopConfig(stream_fn=stream, tools=tools, after_tool_call=after)
    new_messages = run(run_agent_loop([user("go")], context_with(tools=tools), config, None, sink))

    result = [m for m in new_messages if isinstance(m, ToolResultMessage)][0]
    assert result.content[0].text == "改写后"
    assert result.details == {"n": 1}


def test_unknown_tool_and_rejected_arguments_become_error_results() -> None:
    """agent-loop.ts:669-676（找不到工具）与 680（参数校验）。"""
    def validator(tool, args):
        if "missing" not in args:
            raise ValueError("缺少必填参数 missing")
        return args

    stream = ScriptedStream(
        assistant(tool_call("c1", "ghost"), tool_call("c2", "t", {"a": 1}), stop=StopReason.TOOL_USE)
    )
    tools = [make_tool("t", ok_result)]
    sink = RecordingSink()
    config = AgentLoopConfig(stream_fn=stream, tools=tools, argument_validator=validator)
    new_messages = run(run_agent_loop([user("go")], context_with(tools=tools), config, None, sink))

    results = [m for m in new_messages if isinstance(m, ToolResultMessage)]
    assert [m.is_error for m in results] == [True, True]
    assert "not found" in results[0].content[0].text
    assert "缺少必填参数" in results[1].content[0].text


def test_cancellation_stops_the_batch_between_calls() -> None:
    """agent-loop.ts:531-533。"""
    token = CancellationToken("run-1")
    seen: list[str] = []

    async def execute(call_id, _args, _signal=None, _on_update=None):
        seen.append(call_id)
        token.cancel("作者停止")
        return {"content": [], "details": {}}

    stream = ScriptedStream(
        assistant(tool_call("c1", "t"), tool_call("c2", "t"), tool_call("c3", "t"), stop=StopReason.TOOL_USE)
    )
    tools = [make_tool("t", execute, execution_mode="sequential")]
    sink = RecordingSink()
    config = AgentLoopConfig(stream_fn=stream, tools=tools)
    new_messages = run(run_agent_loop([user("go")], context_with(tools=tools), config, token, sink))

    assert seen == ["c1"], "已取消后不再启动下一笔"
    assert len([m for m in new_messages if isinstance(m, ToolResultMessage)]) == 1


def test_transform_context_affects_the_request_not_the_persisted_history() -> None:
    """计划 §94：不能用 transform 悄悄重写持久历史。"""
    stream = ScriptedStream(assistant(TextContent(text="答")))
    sink = RecordingSink()
    seen: list[int] = []

    async def transform(messages, _signal=None):
        seen.append(len(messages))
        return [m for m in messages if m.role != "user"]

    context = context_with(user("会被摘掉"))
    config = AgentLoopConfig(stream_fn=stream, transform_context=transform)
    new_messages = run(run_agent_loop([user("go")], context, config, None, sink))

    assert seen == [2]
    assert any(m.role == "user" for m in context.messages), "持久 transcript 不被 transform 改写"
    assert len(new_messages) == 2


def test_max_turns_cap_stops_a_runaway_loop() -> None:
    """登记差异：上游无轮数上限，计划 §115 要求有。"""
    executions: list[str] = []

    async def execute(call_id, _args, _signal=None, _on_update=None):
        executions.append(call_id)
        return {"content": [TextContent(text="执行")], "details": {}}

    stream = stepping(*[assistant(tool_call(f"c{i}", "t"), stop=StopReason.TOOL_USE) for i in range(10)])
    tools = [make_tool("t", execute)]
    sink = RecordingSink()
    config = AgentLoopConfig(stream_fn=stream, tools=tools, max_turns=3)
    run(run_agent_loop([user("go")], context_with(tools=tools), config, None, sink))

    assert len(stream.calls) == 3
    assert executions == ["c0", "c1", "c2"]
    assert sink.count("turn_start") == 3
    assert sink.types[-1] == "agent_end"


def test_max_turns_one_dispatches_the_final_answer_once() -> None:
    stream = stepping(assistant(TextContent(text="完成")))
    sink = RecordingSink()
    config = AgentLoopConfig(stream_fn=stream, max_turns=1)

    run(run_agent_loop([user("go")], context_with(), config, None, sink))

    assert len(stream.calls) == 1
    assert sink.count("turn_start") == 1
    assert sink.types[-1] == "agent_end"


def test_max_turns_counts_tool_requests_and_executes_the_last_authorized_batch() -> None:
    executions: list[str] = []

    async def execute(call_id, _args, _signal=None, _on_update=None):
        executions.append(call_id)
        return {"content": [TextContent(text="执行")], "details": {}}

    stream = stepping(
        assistant(tool_call("c1", "t"), stop=StopReason.TOOL_USE),
        assistant(tool_call("c2", "t"), stop=StopReason.TOOL_USE),
        assistant(TextContent(text="不会请求第三轮")),
    )
    tools = [make_tool("t", execute)]
    sink = RecordingSink()

    run(
        run_agent_loop(
            [user("go")], context_with(tools=tools),
            AgentLoopConfig(stream_fn=stream, tools=tools, max_turns=2), None, sink,
        )
    )

    assert len(stream.calls) == 2
    assert executions == ["c1", "c2"]
    assert sink.count("turn_start") == 2
    assert sink.types[-1] == "agent_end"


def test_max_turns_does_not_drain_messages_after_the_last_request() -> None:
    steering = [user("待处理的 steering")]
    follow_ups = [user("待处理的 follow-up")]
    steering_polls = 0
    follow_up_polls = 0

    def get_steering():
        nonlocal steering_polls
        steering_polls += 1
        if steering_polls == 1:
            return []
        return [steering.pop(0)] if steering else []

    def get_follow_ups():
        nonlocal follow_up_polls
        follow_up_polls += 1
        return [follow_ups.pop(0)] if follow_ups else []

    stream = stepping(assistant(TextContent(text="完成")))
    sink = RecordingSink()

    run(
        run_agent_loop(
            [user("go")], context_with(),
            AgentLoopConfig(
                stream_fn=stream,
                max_turns=1,
                get_steering_messages=get_steering,
                get_follow_ups=get_follow_ups,
            ),
            None,
            sink,
        )
    )

    assert len(stream.calls) == 1
    assert steering_polls == 1
    # 只比内容：`UserMessage.timestamp` 默认取当前墙钟（`now_ms`），拿整个对象和刚构造的
    # 期望值比较会在跨毫秒时随机变红（实测 8 次里 4 次失败），而这条用例要证明的是「没被消费」。
    assert [message.content for message in steering] == ["待处理的 steering"]
    assert follow_up_polls == 0
    assert [message.content for message in follow_ups] == ["待处理的 follow-up"]


@pytest.mark.parametrize("queue_kind", ["steering", "follow-up"])
def test_queued_steering_and_follow_up_use_only_the_remaining_turn_budget(queue_kind) -> None:
    queued = [user(f"待处理的 {queue_kind}")]
    polls = 0

    def drain():
        nonlocal polls
        polls += 1
        if queue_kind == "steering" and polls == 1:
            return []
        return [queued.pop(0)] if queued else []

    stream = stepping(
        assistant(TextContent(text="第一轮")),
        assistant(TextContent(text="第二轮")),
        assistant(TextContent(text="不能有第三轮")),
    )
    sink = RecordingSink()
    queue_config = (
        {"get_steering_messages": drain}
        if queue_kind == "steering"
        else {"get_follow_ups": drain}
    )

    run(
        run_agent_loop(
            [user("go")],
            context_with(),
            AgentLoopConfig(stream_fn=stream, max_turns=2, **queue_config),
            None,
            sink,
        )
    )

    assert len(stream.calls) == 2
    assert sink.count("turn_start") == 2
    assert queued == []


def test_none_max_turns_keeps_a_multi_request_invocation_unbounded() -> None:
    follow_ups = [user(f"继续 {index}") for index in range(3)]
    stream = stepping(*[assistant(TextContent(text=f"第 {index} 轮")) for index in range(4)])
    sink = RecordingSink()

    run(
        run_agent_loop(
            [user("go")],
            context_with(),
            AgentLoopConfig(
                stream_fn=stream,
                max_turns=None,
                get_follow_ups=lambda: [follow_ups.pop(0)] if follow_ups else [],
            ),
            None,
            sink,
        )
    )

    assert len(stream.calls) == 4
    assert sink.count("turn_start") == 4
    assert follow_ups == []


@pytest.mark.parametrize("entrypoint", [run_agent_loop, run_agent_loop_continue])
@pytest.mark.parametrize("invalid", [0, -1, True, 1.5, "1", []])
def test_invalid_max_turns_fails_before_events_or_provider_dispatch(entrypoint, invalid) -> None:
    stream = stepping(assistant(TextContent(text="不应发送")))
    sink = RecordingSink()
    config = AgentLoopConfig(stream_fn=stream, max_turns=invalid)
    context = context_with(user("已有消息"))

    with pytest.raises(ValueError, match="max_turns"):
        if entrypoint is run_agent_loop:
            run(entrypoint([user("go")], context, config, None, sink))
        else:
            run(entrypoint(context, config, None, sink))

    assert sink.events == []
    assert stream.calls == []


def test_tool_set_change_is_declared_as_a_system_message_before_the_request() -> None:
    """agent-loop.ts:291-321 + 计划 §87。"""
    stream = ScriptedStream(assistant(TextContent(text="答")))
    sink = RecordingSink()
    tools = [make_tool("t", ok_result)]
    config = AgentLoopConfig(stream_fn=stream, tools=tools)
    run(run_agent_loop([user("go")], context_with(tools=tools), config, None, sink))

    sent = stream.calls[0][1]
    assert [m.role for m in sent.messages][0] == "system"
    assert [t.name for t in (sent.messages[0].tools_added or [])] == ["t"]


def test_tool_change_projection_does_not_normalize_current_tools_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = [make_tool(f"tool_{index}", ok_result) for index in range(3)]
    normalize = messages_module.to_tool_declaration
    calls = 0

    def counting_normalize(item):
        nonlocal calls
        calls += 1
        return normalize(item)

    monkeypatch.setattr(messages_module, "to_tool_declaration", counting_normalize)
    monkeypatch.setattr(agent_loop_module, "to_tool_declaration", counting_normalize, raising=False)
    stream = ScriptedStream(assistant(TextContent(text="答")))
    context = AgentContext(messages=[], tools=tools)

    run(run_agent_loop([user("go")], context, AgentLoopConfig(stream_fn=stream, tools=tools), None, RecordingSink()))

    assert calls == 3 * len(tools)


def test_transform_context_on_a_continue_run_does_not_rewrite_the_shared_transcript() -> None:
    """continue 路径与调用方**共用同一个 transcript 对象**（agent-loop.ts:143 `{...context}`，
    数组不复制），所以「把 transform 结果写回 context」会直接改掉宿主正在持久化的历史。

    上一版的测试打在 prompt 运行上，而 prompt 运行会复制数组（:111-114），于是这条不变量
    根本不可观测 —— 变异因此存活。
    """
    stream = ScriptedStream(assistant(TextContent(text="答")))
    sink = RecordingSink()

    async def transform(messages, _signal=None):
        return [m for m in messages if m.role != "user"]

    context = AgentContext(messages=[user("必须留在历史里"), ToolResultMessage(tool_call_id="c1", tool_name="t",
                                                                                 content=[TextContent(text="ok")])])
    config = AgentLoopConfig(stream_fn=stream, transform_context=transform)
    run(run_agent_loop_continue(context, config, None, sink))

    roles_seen_by_model = [m.role for m in stream.calls[0][1].messages]
    assert "user" not in roles_seen_by_model, "transform 要作用于请求"
    assert any(m.role == "user" for m in context.messages), "但不得改写宿主共用的那份 transcript"


def test_normalize_context_still_folds_the_prompt_into_the_request() -> None:
    transcript = normalize_context(AgentContext(messages=[user("问")], system_prompt="基线"))
    assert transcript.messages[0].role == "system"
    assert transcript.messages[0].content == "基线"


# --- 映射表里三行「判不适用 / 须另证」的落点 ------------------------------------


def test_missing_stream_fn_is_refused_before_any_event() -> None:
    """agent-loop.test.ts:87 判「不适用」的替代用例：core 没有全局默认流（计划 §28）。"""
    sink = RecordingSink()
    with pytest.raises(ValueError, match="stream_fn"):
        run(run_agent_loop([user("问")], context_with(), AgentLoopConfig(stream_fn=None), None, sink))
    assert sink.events == [], "拒绝必须发生在 agent_start 之前：先开一轮再报错，宿主会收到一个没有下文的开始"


def test_continue_from_a_registered_custom_tail_converts_instead_of_rejecting() -> None:
    """agent-loop.test.ts:1576：自定义 role 结尾**不**被 continue 拒绝（原文注明 caller responsibility）。

    Python 侧的「责任」落点：宿主给了转换器，core 就用它；这条用例同时钉住转换后的消息
    确实进了请求，而不是被静默丢掉。
    """
    class FindingMessage:
        role = "finding"

        def __init__(self, text: str) -> None:
            self.text = text

    registry = CustomMessageRegistry()
    registry.register("finding", to_llm=lambda message: UserMessage(content=f"[发现] {message.text}"))
    stream = ScriptedStream(assistant(TextContent(text="答")))
    config = AgentLoopConfig(stream_fn=stream, convert_to_llm=lambda ms: to_llm_messages(ms, registry=registry))
    context = AgentContext(messages=[user("问"), FindingMessage("伏笔没收")])

    new_messages = run(run_agent_loop_continue(context, config, None, RecordingSink()))

    assert len(stream.calls) == 1
    sent = [m.content for m in stream.calls[0][1].messages if m.role == "user"]
    assert sent == ["问", "[发现] 伏笔没收"]
    assert len(new_messages) == 1, "continue 只返回新增消息，不把原历史吐回去"


def test_continue_from_an_unregistered_custom_tail_fails_before_the_provider_call() -> None:
    """同上一行的 Python 侧加强：没登记就**报错**，不像上游那样静默过滤掉再发请求。

    上游 `defaultConvertToLlm` 会把不认识的 role 直接滤掉，请求照样发出去 —— 那是一次
    「看起来成功」的续跑。计划 §8 第 3 条不允许这种形状，所以这里断言 provider 一次都没被叫。
    """
    class Notice:
        role = "notification"

    stream = ScriptedStream(assistant(TextContent(text="答")))
    sink = RecordingSink()
    config = AgentLoopConfig(stream_fn=stream)
    context = AgentContext(messages=[user("问"), Notice()])

    with pytest.raises(UnknownMessageRole):
        run(run_agent_loop_continue(context, config, None, sink))

    assert stream.calls == [], "不能把一条宿主没登记过的消息凑成一次成功的请求"
    assert "agent_end" not in sink.types
