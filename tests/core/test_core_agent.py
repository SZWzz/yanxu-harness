"""0.6.0 P05：有状态 Agent 包装层，对照 `packages/agent/src/agent.ts` @ c7cdb46（607 行）。

映射对象是矩阵 §2.1 的 27 行（`test/agent.test.ts`）；每条断言注上游行号，并在
`scripts/p02_mutation_proof.py` 里配一个 A 系列变异。两行是上游 **0 例** 覆盖的
（矩阵 §2.1 末尾那条注：`all` 队列模式、监听器抛错后 busy 是否释放），本文件替它们补上，
因为 StoryIDE 的 P08 恢复路径正好踩在这两个洞上。

端口径上刻意偏离上游的三处，各有用例：
* `stream_fn` 必须显式给，core 不读任何全局默认（计划 §28）；
* 运行失败合成的 assistant 消息里 `usage` 是「未知」而不是 0（计划 §100）；
* `continue()` 是 Python 关键字，Python 侧叫 `continue_run()`（改名，不是改语义）。
"""

import asyncio

import pytest

from yanxu_harness.core.agent import Agent, AgentState
from yanxu_harness.core.agent_loop import AgentLoopConfig
from yanxu_harness.core.cancellation import CancellationToken
from yanxu_harness.core.events import MessageStart
from yanxu_harness.core.types import (
    AgentContext,
    AssistantMessage,
    StopReason,
    SystemMessage,
    TextContent,
    ToolResultMessage,
    Model,
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


def build_agent(stream, **kwargs):
    """默认只给 stream_fn：其余钩子中任何一个被 core 悄悄丢掉时，用例应当变红而不是静默通过。"""
    return Agent(stream_fn=stream, **kwargs)


# --- 构造与初始状态 -----------------------------------------------------------


def test_agent_refuses_a_global_default_stream_fn_at_construction() -> None:
    """agent.test.ts:102 判「不适用」的那行：上游 `setDefaultStreamFn` 在 Python 侧不存在。"""
    with pytest.raises(ValueError, match="stream_fn"):
        Agent(stream_fn=None)


def test_an_existing_leading_system_message_is_not_replaced() -> None:
    """agent.ts:85：`messages[0].role === "system"` 时**不**再插入新的 initial 消息。

    P09 的历史迁移会带着既有 system 消息重建 agent，要是这里按 systemPrompt 再造一条，
    重放就会声明两次工具集。
    """
    existing = SystemMessage(content="既有基线", tools_added=())
    agent = build_agent(
        ScriptedStream(assistant(TextContent(text="答"))),
        initial_state=AgentState(system_prompt="另一个基线", messages=[existing, user("问")]),
    )
    assert [m.role for m in agent.state.messages] == ["system", "user"]
    assert agent.state.messages[0].content == "既有基线"
    assert agent.state.system_prompt == "既有基线"


def test_listeners_are_awaited_in_subscription_order() -> None:
    """agent.ts:603-605：`for (const listener of this.listeners)` 是有序 await。

    宿主的顺序是「先写 operation journal，再推 SSE」，反了会让浏览器看到一个 journal 里
    还不存在的事件。上游对这条只有 0 例断言，所以它归 StoryIDE 加强。
    """
    agent = build_agent(ScriptedStream(assistant(TextContent(text="答"))))
    order: list[str] = []

    async def first(_event, _signal):
        await asyncio.sleep(0)
        order.append("first")

    def second(_event, _signal):
        order.append("second")

    agent.subscribe(first)
    agent.subscribe(second)
    run(agent.prompt("问"))

    assert order[:2] == ["first", "second"], "第一个监听器 await 期间不能让第二个先跑"
    assert order.count("first") == order.count("second") == 8


def test_default_state_is_wellformed() -> None:
    """agent.test.ts:123。"""
    agent = build_agent(ScriptedStream(assistant(TextContent(text="答"))))
    state = agent.state
    assert isinstance(state, AgentState)
    assert state.messages == [] and list(state.tools) == []
    assert state.is_streaming is False
    assert state.streaming_message is None
    assert set(state.pending_tool_calls) == set()
    assert state.error_message is None
    assert state.thinking_level == "off"


def test_custom_initial_state_takes_effect() -> None:
    """agent.test.ts:137。"""
    prompt = user("已经问过")
    agent = build_agent(
        ScriptedStream(assistant(TextContent(text="答"))),
        initial_state=AgentState(messages=[prompt], model=Model(id="m-7")),
    )
    assert [m.role for m in agent.state.messages] == ["user"]
    assert agent.state.model.id == "m-7"


def test_initial_system_prompt_and_tools_become_the_leading_system_message() -> None:
    """agent.test.ts:153：`systemPrompt` 与 tools 变成开头那条 system 消息。"""
    tools = [make_tool("t", ok_result)]
    agent = build_agent(
        ScriptedStream(assistant(TextContent(text="答"))),
        initial_state=AgentState(system_prompt="你是小说编辑", tools=tools),
    )
    first = agent.state.messages[0]
    assert isinstance(first, SystemMessage) and first.content == "你是小说编辑"
    assert [t.name for t in (first.tools_added or [])] == ["t"]
    assert agent.state.system_prompt == "你是小说编辑"


# --- transcript 状态机：订阅与事件 --------------------------------------------


def test_subscribe_receives_events_and_unsubscribe_stops_them() -> None:
    """agent.test.ts:318。"""
    stream = ScriptedStream(assistant(TextContent(text="答")))
    agent = build_agent(stream)
    seen: list[str] = []
    unsubscribe = agent.subscribe(lambda event, _signal: seen.append(event.type))

    run(agent.prompt("问"))
    first_count = len(seen)

    unsubscribe()
    run(agent.prompt("再问"))

    assert seen[:2] == ["agent_start", "turn_start"]
    assert first_count == 8, "一轮 prompt 的完整骨架"
    assert len(seen) == first_count, "退订后一个事件都不该再来"
    # 骨架条数抓不到「文本提示词没被包装成 user 消息」：失败路径也是 8 个事件（A28 第一版存活）。
    # 所以必须直接断 transcript 里躺着的是什么类型的对象。
    assert [getattr(m, "role", "?") for m in agent.state.messages[:2]] == ["user", "assistant"]
    assert agent.state.messages[0].content == "问"


def test_a_run_failure_still_emits_the_full_lifecycle_without_fabricating_usage() -> None:
    """agent.test.ts:340 + 计划 §100：合成消息有完整 8 事件，但 usage 是未知不是 0。"""

    async def explode(_model, _context, _options=None):
        raise RuntimeError("provider 挂了")

    agent = build_agent(explode)
    events: list = []
    agent.subscribe(lambda event, _signal: events.append(event))

    run(agent.prompt("问"))

    types = [getattr(e, "type", "?") for e in events]
    assert types == [
        "agent_start", "turn_start",
        "message_start", "message_end",       # 提示词已经落进历史
        "message_start", "message_end",       # 合成的失败 assistant
        "turn_end", "agent_end",
    ], "失败也必须走完 8 个事件，否则宿主的 SSE 没有终止依据"
    final = events[5].message
    assert isinstance(final, AssistantMessage) and final.stop_reason is StopReason.ERROR
    assert "provider 挂了" in (final.error_message or "")
    assert final.usage.input is None and final.usage.total_tokens is None, (
        "上游在这里填 EMPTY_USAGE=0；Python 侧填 0 会被宿主当成「真的没花 token」"
    )
    assert agent.state.error_message and "provider 挂了" in agent.state.error_message


def test_prompt_waits_for_async_listeners() -> None:
    """agent.test.ts:371：监听器还没跑完，prompt 不算完成。"""
    stream = ScriptedStream(assistant(TextContent(text="答")))
    agent = build_agent(stream)
    order: list[str] = []

    async def slow_listener(_event, _signal):
        await asyncio.sleep(0)
        order.append("listener")

    agent.subscribe(slow_listener)

    async def scenario():
        await asyncio.wait_for(agent.prompt("问"), timeout=5)
        order.append("prompt-resolved")

    run(scenario())
    assert order[-1] == "prompt-resolved", "prompt 结算前监听器必须全部跑完"
    assert order.count("listener") == 8, "一轮 prompt 的 8 个事件都要过监听器"


def test_wait_for_idle_resolves_only_after_agent_end_listeners_settle() -> None:
    """agent.test.ts:409：`agent_end` 只是「没有后续 loop 事件」，idle 在监听器结算之后。"""
    stream = ScriptedStream(assistant(TextContent(text="答")))
    agent = build_agent(stream)
    flags: dict[str, bool] = {"listener_finished": False}

    async def listener(event, _signal):
        if event.type != "agent_end":
            return
        await asyncio.sleep(0)
        flags["listener_finished"] = True

    agent.subscribe(listener)

    async def scenario():
        task = asyncio.create_task(agent.prompt("问"))
        await asyncio.wait_for(task, timeout=5)
        idle_done_before_listeners = flags["listener_finished"]
        await agent.wait_for_idle()
        return idle_done_before_listeners, flags["listener_finished"], agent.state.is_streaming

    finished_before, finished_after, streaming = run(scenario())
    assert finished_before is True, (
        "prompt() 本身已经把监听器算进结算，所以到 wait_for_idle 时监听器早跑完了 —— "
        "这条断言的价值是反过来的：如果哪天 wait_for_idle 提前 resolve，"
        "is_streaming 会在监听器还在跑时就变 False"
    )
    assert finished_after is True and streaming is False


def test_wait_for_idle_blocks_until_the_run_settles() -> None:
    """agent.ts:343-345：`wait_for_idle()` 在 run 期间必须真的挂住，而不是立刻返回。

    用 gate 卡住 provider，好让「run 还在跑」这个前提是可制造的而不是指望调度巧合。
    这条是 A21 的唯一可杀形状：run 结束后再调 `wait_for_idle()` 走的是「无 active run」
    那条捷径，摘掉 future 结算也测不出来。
    """
    gate = asyncio.Event()

    async def blocking(_model, _context, _options=None):
        await gate.wait()
        return ScriptedStream(assistant(TextContent(text="答")))

    agent = build_agent(blocking)

    async def scenario():
        run_task = asyncio.create_task(agent.prompt("问"))
        for _ in range(5):
            await asyncio.sleep(0)
        assert agent.state.is_streaming is True, "前置条件：run 还挂着"

        idle_task = asyncio.create_task(agent.wait_for_idle())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert idle_task.done() is False, "run 没结算，idle 不该已经回来"

        gate.set()
        await asyncio.wait_for(run_task, timeout=4)
        await asyncio.wait_for(idle_task, timeout=4)
        return agent.state.is_streaming

    assert run(scenario()) is False
    run(agent.wait_for_idle())  # 空闲时立刻返回，不会挂住


def test_listeners_receive_the_active_run_signal() -> None:
    """agent.test.ts:444：第二个参数是本次 run 的取消令牌，且 run 期间稳定同一个。"""
    stream = ScriptedStream(assistant(TextContent(text="答")))
    agent = build_agent(stream)
    seen: list[CancellationToken] = []
    agent.subscribe(lambda _event, signal: seen.append(signal))

    run(agent.prompt("问"))

    assert seen and all(s is seen[0] for s in seen), "一次 run 只有一个身份，迟到回调靠它做 fencing"
    assert seen[0].cancelled is False
    assert seen[0].run_id, "run_id 是宿主区分「哪一次 run 的回调」的键"
    assert agent.signal is None, "收尾后不留 signal：拿旧 signal 去取消下一次 run 会打到别处"

    run(agent.prompt("再问"))
    assert seen[-1] is not seen[0] and seen[-1].run_id != seen[0].run_id, (
        "两次 run 共用一个 run_id 的话，上一轮的迟到回调能被宿主当成这一轮的（计划 §117）"
    )


def test_tool_execution_state_tracks_pending_calls() -> None:
    """agent.test.ts:482 的 Agent 侧半边：pendingToolCalls 记账、收尾清空。"""
    stream = stepping(
        assistant(tool_call("c1", "t"), stop=StopReason.TOOL_USE),
        assistant(TextContent(text="好")),
    )
    tools = [make_tool("t", ok_result)]
    agent = build_agent(stream, initial_state=AgentState(tools=tools))
    pending_snapshots: list[set] = []

    async def listener(event, _signal):
        if event.type == "tool_execution_start":
            pending_snapshots.append(set(agent.state.pending_tool_calls))
        if event.type == "tool_execution_end":
            pending_snapshots.append(set(agent.state.pending_tool_calls))

    agent.subscribe(listener)
    run(agent.prompt("写"))

    assert pending_snapshots == [{"c1"}, set()], "start 时挂账、end 时销账"
    assert set(agent.state.pending_tool_calls) == set(), "run 收尾后不留悬挂工具"
    transcript = [m for m in agent.state.messages if isinstance(m, ToolResultMessage)]
    assert [m.tool_call_id for m in transcript] == ["c1"]


def test_a_held_pending_tool_snapshot_is_not_mutated_later() -> None:
    """agent.ts:574-586 每处都换成**新** Set：这样宿主攥着的快照才是那一刻的事实。"""
    stream = stepping(
        assistant(tool_call("c1", "t"), stop=StopReason.TOOL_USE),
        assistant(TextContent(text="好")),
    )
    tools = [make_tool("t", ok_result)]
    agent = build_agent(stream, initial_state=AgentState(tools=tools))
    held: list[set] = []

    async def listener(event, _signal):
        if event.type == "tool_execution_start":
            held.append(agent.state.pending_tool_calls)

    agent.subscribe(listener)
    run(agent.prompt("写"))

    assert held and isinstance(held[0], set)
    assert set(held[0]) == {"c1"}, "拿到的那个集合不能被后续销账改掉（同对象改写就等于没有快照）"


# --- 状态可变面与拷贝语义 ------------------------------------------------------


def test_assigning_state_messages_copies_the_top_level_list() -> None:
    """agent.test.ts:623：赋值的语义是「拷进来」，外部数组之后怎么改都不影响 agent。"""
    agent = build_agent(ScriptedStream(assistant(TextContent(text="答"))))
    external = [user("一条")]
    agent.state.messages = external
    external.append(user("第二条"))

    assert [m.content for m in agent.state.messages] == ["一条"]
    assert agent.state.messages is not external

    tools = [make_tool("t", ok_result)]
    agent.state.tools = tools
    tools.append(make_tool("u", ok_result))
    assert [t.name for t in agent.state.tools] == ["t"], "tools 同理"


def test_thinking_level_maps_to_reasoning_and_off_means_none() -> None:
    """agent.ts:465：`thinkingLevel === "off"` → reasoning undefined；其余透传。"""
    stream = ScriptedStream(assistant(TextContent(text="答")))
    agent = build_agent(stream)
    run(agent.prompt("问"))
    assert stream.calls[0][2]["reasoning"] is None

    agent.state.thinking_level = "high"
    second = ScriptedStream(assistant(TextContent(text="答")))
    agent.stream_fn = second
    run(agent.prompt("再问"))
    assert second.calls[0][2]["reasoning"] == "high"


def test_session_id_is_forwarded_to_stream_options() -> None:
    """agent.test.ts:962。"""
    stream = ScriptedStream(assistant(TextContent(text="答")))
    agent = build_agent(stream, session_id="sess-1")
    run(agent.prompt("问"))
    assert stream.calls[0][2]["session_id"] == "sess-1"

    agent.session_id = "sess-2"
    run(agent.prompt("再问"))
    assert stream.calls[1][2]["session_id"] == "sess-2"


def test_convert_and_transform_hooks_are_forwarded() -> None:
    """agent.ts:487-488：两个转换器都是逐 run 读取，构造后改也算。"""
    stream = ScriptedStream(assistant(TextContent(text="答")))
    agent = build_agent(stream)
    calls: list[str] = []

    def convert(messages):
        calls.append("convert")
        return [m for m in messages if m.role != "system"]

    async def transform(messages, _signal=None):
        calls.append("transform")
        return list(messages)

    agent.convert_to_llm = convert
    agent.transform_context = transform
    run(agent.prompt("问"))

    assert calls == ["transform", "convert"], "顺序也是契约：先摘消息再投影成 LLM 输入"


# --- 队列：steer / follow_up / 模式 -------------------------------------------


def test_steer_only_enqueues_and_does_not_touch_the_transcript() -> None:
    """agent.test.ts:658。"""
    agent = build_agent(ScriptedStream(assistant(TextContent(text="答"))))
    agent.steer(user("插一句"))

    assert agent.has_queued_messages() is True
    assert agent.state.messages == [], "入队不等于进历史"


def test_follow_up_only_enqueues() -> None:
    """agent.test.ts:668。"""
    agent = build_agent(ScriptedStream(assistant(TextContent(text="答"))))
    agent.follow_up(user("另一件事"))
    assert agent.has_queued_messages() is True
    assert agent.state.messages == []


def test_clear_queue_helpers_cover_both_queues_independently() -> None:
    agent = build_agent(ScriptedStream(assistant(TextContent(text="答"))))
    agent.steer(user("a"))
    agent.follow_up(user("b"))

    agent.clear_steering_queue()
    assert agent.has_queued_messages() is True, "follow-up 还在"
    agent.clear_follow_up_queue()
    assert agent.has_queued_messages() is False

    agent.steer(user("c"))
    agent.follow_up(user("d"))
    agent.clear_all_queues()
    assert agent.has_queued_messages() is False


def test_all_mode_puts_every_queued_message_into_the_first_request() -> None:
    """上游 0 例覆盖 `all` 模式（矩阵 §2.1 末注）；StoryIDE 的「作者连发三条」要的就是这个。"""
    stream = stepping(assistant(TextContent(text="第一段")), assistant(TextContent(text="第二段")))
    agent = build_agent(stream, steering_mode="all")
    agent.steer(user("一"))
    agent.steer(user("二"))

    run(agent.prompt("开始"))

    assert len(stream.calls) == 1, "两条一起进第一轮，没有第二轮"
    assert [m.role for m in stream.calls[0][1].messages].count("user") == 3
    assert not agent.has_queued_messages()


def test_one_at_a_time_mode_spreads_queued_messages_over_turns() -> None:
    """默认模式：每个排空点只放一条，所以上面的两条要分两轮进去。"""
    stream = stepping(
        assistant(TextContent(text="第一段")),
        assistant(TextContent(text="第二段")),
        assistant(TextContent(text="第三段")),
    )
    agent = build_agent(stream)
    agent.steer(user("一"))
    agent.steer(user("二"))

    run(agent.prompt("开始"))

    first_request = [m.role for m in stream.calls[0][1].messages]
    assert first_request.count("user") == 2, "第一轮只带上「一」"
    assert len(stream.calls) == 2, "「二」在回合末尾的排空点进第二轮"


def test_one_at_a_time_is_the_default_and_is_swappable_at_runtime() -> None:
    agent = build_agent(ScriptedStream(assistant(TextContent(text="答"))))
    assert agent.steering_mode == "one-at-a-time" and agent.follow_up_mode == "one-at-a-time"
    agent.steering_mode = "all"
    agent.follow_up_mode = "all"
    assert agent.steering_mode == "all" and agent.follow_up_mode == "all"
    with pytest.raises(ValueError):
        agent.steering_mode = "sometimes"


# --- 忙时拒绝 / 取消 / reset ---------------------------------------------------


def test_prompt_while_busy_raises() -> None:
    """agent.test.ts:719。"""
    gate = asyncio.Event()

    async def blocking(_model, _context, _options=None):
        await gate.wait()
        return ScriptedStream(assistant(TextContent(text="答")))

    agent = build_agent(blocking)

    async def scenario():
        first = asyncio.create_task(agent.prompt("问"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        # 匹配各自的原文而不是笼统的 "already processing"：`_run_with_lifecycle` 里还有
        # 一道同形状的守卫，只断「已处理中」的话，摘掉 prompt 这道也会被内层兜住（A8 第一版）。
        with pytest.raises(RuntimeError, match="Use steer\\(\\) or follow_up\\(\\)"):
            await agent.prompt("第二问")
        gate.set()
        await first

    run(scenario())


def test_continue_run_while_busy_raises() -> None:
    """agent.test.ts:759。"""
    gate = asyncio.Event()

    async def blocking(_model, _context, _options=None):
        await gate.wait()
        return ScriptedStream(assistant(TextContent(text="答")))

    agent = build_agent(blocking)
    agent.state.messages = [user("问")]

    async def scenario():
        first = asyncio.create_task(agent.prompt("问"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        with pytest.raises(RuntimeError, match="Wait for completion before continuing"):
            await agent.continue_run()
        gate.set()
        await first

    run(scenario())


def test_reset_while_busy_raises_without_corrupting_the_transcript() -> None:
    """agent.test.ts:685（计划 §115 的「忙时拒绝」）：抛错之后历史必须还是完整的。"""
    gate = asyncio.Event()

    async def blocking(_model, _context, _options=None):
        await gate.wait()
        return ScriptedStream(assistant(TextContent(text="答")))

    agent = build_agent(blocking, initial_state=AgentState(system_prompt="基线"))

    async def scenario():
        first = asyncio.create_task(agent.prompt("问"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        with pytest.raises(RuntimeError, match="Wait for completion before resetting"):
            agent.reset()
        assert [m.role for m in agent.state.messages] == ["system", "user"]
        gate.set()
        await first

    run(scenario())


def test_reset_restores_the_transcript_baseline_and_clears_runtime_state() -> None:
    """agent.test.ts:291 + agent.ts:347-361：只留那条 system，且保留它的 toolsAdded 基线。"""
    tools = [make_tool("t", ok_result)]
    stream = ScriptedStream(assistant(TextContent(text="答")))
    agent = build_agent(stream, initial_state=AgentState(system_prompt="基线", tools=tools))
    run(agent.prompt("问"))
    assert len(agent.state.messages) > 1
    baseline_added = [t.name for t in (agent.state.messages[0].tools_added or [])]

    agent.steer(user("还排着"))
    agent.follow_up(user("也排着"))
    agent.reset()

    roles = [m.role for m in agent.state.messages]
    assert roles == ["system"], "reset 只留 system 基线"
    assert [t.name for t in (agent.state.messages[0].tools_added or [])] == baseline_added == ["t"]
    assert agent.has_queued_messages() is False
    assert agent.state.is_streaming is False and agent.state.streaming_message is None
    assert set(agent.state.pending_tool_calls) == set() and agent.state.error_message is None


def test_abort_without_an_active_run_is_a_no_op() -> None:
    """agent.test.ts:678。"""
    agent = build_agent(ScriptedStream(assistant(TextContent(text="答"))))
    assert agent.signal is None
    agent.abort()
    assert agent.signal is None


def test_abort_cancels_the_active_run_and_still_leaves_the_agent_idle() -> None:
    """agent.ts:328-336 + 计划 §117：取消要能被测到到达哪一层，且收尾必须清 busy。"""
    started = asyncio.Event()

    async def cancellable(_model, _context, options=None):
        token = options["signal"]

        class Waiting:
            def __init__(self) -> None:
                self._message: AssistantMessage | None = None

            async def __call__(self):
                await token.wait_cancelled()
                self._message = AssistantMessage(
                    content=[TextContent(text="")], stop_reason=StopReason.ABORTED
                )
                return self

            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

            async def result(self):
                assert self._message is not None
                return self._message

        started.set()
        return await Waiting()()

    agent = build_agent(cancellable)

    async def scenario():
        task = asyncio.create_task(agent.prompt("问"))
        await asyncio.wait_for(started.wait(), timeout=4)
        assert agent.signal is not None and agent.state.is_streaming is True
        agent.abort()
        await asyncio.wait_for(task, timeout=4)
        await agent.wait_for_idle()
        return agent.state.is_streaming

    still_streaming = run(scenario())
    assert still_streaming is False, "取消后 busy 必须释放，否则宿主永远以为在跑"
    assert agent.signal is None
    assert agent.state.messages[-1].stop_reason is StopReason.ABORTED


# --- continue_run 的三种尾巴 ---------------------------------------------------


def test_continue_run_consumes_the_follow_up_queue_after_an_assistant_tail() -> None:
    """agent.test.ts:795：尾部是 assistant 且没有 steering 时，消费 follow-up。"""
    stream = stepping(assistant(TextContent(text="第一段")), assistant(TextContent(text="第二段")))
    agent = build_agent(stream)
    run(agent.prompt("开始"))
    assert agent.state.messages[-1].role == "assistant"

    agent.follow_up(user("还有件事"))
    run(agent.continue_run())

    roles = [m.role for m in agent.state.messages]
    assert roles == ["user", "assistant", "user", "assistant"]
    assert len(stream.calls) == 2


def test_continue_run_prefers_steering_and_keeps_one_at_a_time_semantics() -> None:
    """agent.test.ts:833：steering 优先于 follow-up，且 one-at-a-time 每个排空点只放一条。

    注意形状：一次 `continue_run()` 会把队列排空到底（loop 在每回合末尾继续轮询），
    「一次只取一条」体现在**顺序**上而不是「一次调用只放一条」——两条 steering 各占一个回合，
    follow-up 排在它们之后。要是实现改成一次全出，这里会看到两条 user 连着进同一个请求。
    """
    stream = stepping(
        assistant(TextContent(text="第二段")),
        assistant(TextContent(text="第三段")),
        assistant(TextContent(text="第四段")),
    )
    agent = build_agent(stream)
    run(agent.prompt("开始"))
    agent.steer(user("插话一"))
    agent.steer(user("插话二"))
    agent.follow_up(user("后续"))

    run(agent.continue_run())

    assert [m.role for m in agent.state.messages] == [
        "user", "assistant", "user", "assistant", "user", "assistant", "user", "assistant",
    ]
    assert [m.content for m in agent.state.messages if m.role == "user"] == ["开始", "插话一", "插话二", "后续"]
    assert agent.has_queued_messages() is False
    users_per_request = [len([m for m in call[1].messages if m.role == "user"]) for call in stream.calls]
    assert users_per_request == [1, 2, 3, 4], "每条请求只比上一条多一条插话；一次全出会跳到 4"


def test_continue_run_rejects_an_assistant_tail_with_empty_queues() -> None:
    """agent.ts:400。"""
    stream = ScriptedStream(assistant(TextContent(text="第一段")))
    agent = build_agent(stream)
    run(agent.prompt("开始"))
    with pytest.raises(RuntimeError, match="Cannot continue from message role"):
        run(agent.continue_run())


def test_continue_run_rejects_a_transcript_with_nothing_to_continue_from() -> None:
    """agent.ts:382-385：空历史，或整份都是 system，都没有「下文」可续。"""
    agent = build_agent(ScriptedStream(assistant(TextContent(text="答"))))
    with pytest.raises(RuntimeError, match="No messages to continue from"):
        run(agent.continue_run())

    only_system = build_agent(ScriptedStream(assistant(TextContent(text="答"))))
    only_system.state.messages = [SystemMessage(content="只有基线")]
    with pytest.raises(RuntimeError, match="No messages to continue from"):
        run(only_system.continue_run())


def test_continue_run_from_a_tool_result_tail_appends_only_new_messages() -> None:
    stream = ScriptedStream(assistant(TextContent(text="收尾")))
    agent = build_agent(stream)
    agent.state.messages = [
        user("问"),
        ToolResultMessage(tool_call_id="c1", tool_name="t", content=[TextContent(text="ok")]),
    ]
    run(agent.continue_run())
    assert [m.role for m in agent.state.messages] == ["user", "toolResult", "assistant"]


# --- 循环钩子的转发 -----------------------------------------------------------


def test_legacy_prepare_next_turn_still_receives_the_signal() -> None:
    """agent.test.ts:877：旧签名 `prepareNextTurn(signal)` 保留，且第二个回合起各调一次。"""
    seen: list = []

    async def prepare(signal):
        seen.append(signal)
        return None

    stream = stepping(
        assistant(tool_call("c1", "t"), stop=StopReason.TOOL_USE),
        assistant(TextContent(text="好")),
    )
    tools = [make_tool("t", ok_result)]
    agent = build_agent(stream, prepare_next_turn=prepare, initial_state=AgentState(tools=tools))
    run(agent.prompt("写"))

    assert len(seen) == 1, "两个回合的循环里 prepare 只在第二个回合前调用一次"
    assert len(stream.calls) == 2, "上游那条数的是 2 次请求"
    assert all(s is seen[0] for s in seen) and isinstance(seen[0], CancellationToken)


def test_prepare_next_turn_with_context_wins_over_the_legacy_form() -> None:
    """agent.ts:480-485：两者都给时走 with_context，旧的那个一次都不该被叫。"""
    legacy: list = []
    with_context: list = []

    stream = stepping(
        assistant(tool_call("c1", "t"), stop=StopReason.TOOL_USE),
        assistant(TextContent(text="好")),
    )
    tools = [make_tool("t", ok_result)]
    agent = build_agent(
        stream,
        prepare_next_turn=lambda signal: legacy.append(signal),
        prepare_next_turn_with_context=lambda context, signal: with_context.append(context),
        initial_state=AgentState(tools=tools),
    )
    run(agent.prompt("写"))

    assert legacy == [] and len(with_context) == 1
    assert with_context[0].message.stop_reason is StopReason.TOOL_USE


def test_should_stop_after_turn_is_forwarded_and_sees_the_run_signal() -> None:
    """agent.test.ts:918 + agent.ts:475-477：选项注入的 hook 收到 (context, signal)。"""
    seen: list = []

    async def should_stop(context, signal):
        seen.append((context, signal))
        return True

    stream = stepping(
        assistant(tool_call("c1", "t"), stop=StopReason.TOOL_USE),
        assistant(TextContent(text="不该发生")),
    )
    tools = [make_tool("t", ok_result)]
    agent = build_agent(stream, should_stop_after_turn=should_stop, initial_state=AgentState(tools=tools))
    run(agent.prompt("写"))

    assert len(stream.calls) == 1, "选项注入的 hook 生效 = 只请求一次"
    assert len(seen) == 1
    context, signal = seen[0]
    assert context.tool_results and context.message.stop_reason is StopReason.TOOL_USE
    assert isinstance(signal, CancellationToken) and signal.cancelled is False


def test_tool_execution_mode_and_argument_validator_reach_the_loop() -> None:
    """core 的校验注入点（计划 §78）必须由 Agent 透传，否则宿主没法接自己的 schema 校验。"""
    validated: list = []

    def validator(tool, args):
        validated.append(tool.name)
        return args

    order: list[str] = []

    async def slow(_id, _args, _signal=None, _on_update=None):
        order.append(f"{_id}-start")
        await asyncio.sleep(0)
        order.append(f"{_id}-end")
        return {"content": [], "details": {}}

    stream = stepping(
        assistant(tool_call("c1", "a"), tool_call("c2", "b"), stop=StopReason.TOOL_USE),
        assistant(TextContent(text="好")),
    )
    tools = [make_tool("a", slow), make_tool("b", slow)]
    agent = build_agent(
        stream, tool_execution="sequential", argument_validator=validator,
        initial_state=AgentState(tools=tools),
    )
    run(agent.prompt("写"))

    assert validated == ["a", "b"], "串行时逐个校验；并行时也是源序"
    assert order == ["c1-start", "c1-end", "c2-start", "c2-end"], (
        "配置里的 sequential 必须真的落到执行器：只回读属性等于没转发"
    )


# --- 监听器的两条边界（上游 0 例） --------------------------------------------


def test_a_raising_listener_rejects_the_prompt_but_leaves_the_agent_idle() -> None:
    """上游 0 例（矩阵 §2.1 末注）：监听器抛错后 busy 必须释放。

    StoryIDE 的 SSE 消费者就是监听器，客户端断线时它会抛。要是这里把 agent 卡在
    `active_run` 上，整条 run 就永久 busy，作者看到的是一直在转圈的会话。
    """
    stream = ScriptedStream(assistant(TextContent(text="答")))
    agent = build_agent(stream)

    def angry_listener(_event, _signal):
        raise RuntimeError("浏览器断线")

    unsubscribe = agent.subscribe(angry_listener)

    with pytest.raises(RuntimeError, match="浏览器断线"):
        run(agent.prompt("问"))

    assert agent.state.is_streaming is False
    assert agent.signal is None
    run(agent.wait_for_idle())

    # 抛错的监听器**还挂在订阅表上**（core 不替宿主退订），所以第二次 run 之前必须自己摘掉。
    # 这是 P07/P08 的宿主责任：SSE 断线的监听器要自我退订，否则整个 agent 永久不可用。
    unsubscribe()
    healthy = ScriptedStream(assistant(TextContent(text="答2")))
    agent.stream_fn = healthy
    run(agent.prompt("再问"))
    assert len(healthy.calls) == 1


def test_dispatching_an_event_outside_an_active_run_is_an_error() -> None:
    """agent.ts:599-602：脱离 run 的事件投递不能悄悄成功 —— 它没有 signal 可发。

    `dispatch` 是公开的 reducer（P08 的 SSE 重放要走同一条状态收敛路径），所以这条守卫
    必须在公开面上成立，否则重放能伪造出一次「在跑」的状态。
    """
    agent = build_agent(ScriptedStream(assistant(TextContent(text="答"))))
    with pytest.raises(RuntimeError, match="outside active run"):
        run(agent.dispatch(MessageStart(message=user("孤儿事件"))))


def test_message_events_drive_streaming_message_and_the_transcript() -> None:
    """agent.ts:561-572：start/update 期间 streaming_message 有值，end 才落进历史。"""
    partial_one = assistant(TextContent(text="还"))
    partial_two = assistant(TextContent(text="还没写完"))
    stream = ScriptedStream(
        assistant(TextContent(text="还没写完")),
        frames=[{"type": "start", "partial": partial_one}, {"type": "text_delta", "partial": partial_two}],
    )
    agent = build_agent(stream)
    snapshots: list = []

    async def listener(event, _signal):
        if event.type == "message_update":
            snapshots.append(agent.state.streaming_message)

    agent.subscribe(listener)
    run(agent.prompt("问"))

    assert snapshots and snapshots[0].content[0].text == "还没写完", (
        "update 期间 streaming_message 必须可见。注意是**等值拷贝**而不是同一个对象："
        "上游 emit 的就是 `{...partial}`（:566），宿主不能靠同一性判断流是否结束"
    )
    assert agent.state.streaming_message is None, "落定后清指针"
    assert agent.state.messages[-1].content[0].text == "还没写完"


# --- 计划 §8 第 2 条的 core 半边：关掉小说扩展，只用 Python core 跑完整任务 -------


def test_a_multi_turn_scripted_task_needs_no_host_extension_and_writes_nothing(tmp_path, monkeypatch) -> None:
    """关闭 `backend.agent_tools` / 小说扩展，只用 core + scripted StreamFn 走完：

    提示词 → 工具 → 结果 → 第二轮 → 回合中插话 → 无工具调用后收尾，外加一次取消。
    这是「不退化成必须挂着 PydanticAI 才能跑」的最小可执行证明（计划 §8 第 2 条），
    不是性能测试，也不宣称真 Provider 可用（那条归 P06/P10 的独立报告）。
    """
    import sys
    from pathlib import Path

    monkeypatch.chdir(tmp_path)
    files_before = set(Path(tmp_path).rglob("*"))
    modules_before = set(sys.modules)

    async def draft(_id, args, _signal=None, _on_update=None):
        return {"content": [TextContent(text=f"已暂存 {args['path']}")], "details": {"staged": True}}

    tools = [make_tool("stage_draft", draft)]
    stream = stepping(
        assistant(tool_call("c1", "stage_draft", {"path": "第三章.md"}), stop=StopReason.TOOL_USE),
        assistant(tool_call("c2", "stage_draft", {"path": "第四章.md"}), stop=StopReason.TOOL_USE),
        assistant(TextContent(text="两章都已暂存，等你确认")),
    )
    agent = build_agent(stream, initial_state=AgentState(system_prompt="基线", tools=tools))
    journal: list[str] = []
    agent.subscribe(lambda event, _signal: journal.append(event.type))
    agent.follow_up(user("再把结尾收一下"))

    run(agent.prompt("把三、四章收紧"))

    transcript = [m.role for m in agent.state.messages]
    assert transcript == [
        "system", "user", "assistant", "toolResult", "assistant", "toolResult", "assistant", "user", "assistant",
    ], transcript
    assert journal.count("agent_end") == 1, "整个任务只有一次终止事实"
    assert journal.count("turn_start") == 4, "三次工具/回答回合 + 一次 follow-up 回合"
    assert agent.state.is_streaming is False and set(agent.state.pending_tool_calls) == set()
    assert agent.state.messages[-2].content == "再把结尾收一下", "follow-up 在 agent 本会停下时才进来"
    written = [m for m in agent.state.messages if isinstance(m, ToolResultMessage)]
    assert [m.content[0].text for m in written] == ["已暂存 第三章.md", "已暂存 第四章.md"]

    assert set(Path(tmp_path).rglob("*")) == files_before, "core 跑完整任务不落任何文件"
    leaked = sorted(
        name for name in set(sys.modules) - modules_before
        if name.startswith("backend.") and not name.startswith(("yanxu_harness.core", "backend.agent_runtime"))
    )
    assert leaked == [], f"core 独立跑时把宿主拖进了进程：{leaked}"
