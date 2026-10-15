"""0.6.0 P04：工具执行器的退出条件。

计划 §6 的 P04 那一行要求「参数准备/验证、串并行、before/after、partial、terminate、
取消」且「无真实写副作用」。执行器实现随 P03 落地，所以这一层的价值全在**能不能被打破**：
每条断言都在 `scripts/p02_mutation_proof.py` 里配一个 T 系列变异（33 条，整套 55/55 全杀）。

三条写在用例之外的结论，留给后续任务：
* `test_cancellation_during_preflight_yields_an_aborted_result` 必须走**串行**批次才有意义
  —— 并行路径上排在队列里的闭包自己还会再查一次取消，把 preflight 的守卫完全遮住（T12 等价变异）。
* `test_no_real_write_side_effects` 的模块泄漏只看「这一批工具新导入了什么」，
  全量跑时宿主模块早已被别的用例导入。
* `test_before_hook_mutating_args_in_place_reaches_the_tool_without_revalidation` 钉的是
  core **没有**再校验点：计划 §108「批准后再次校验并冻结」只能在 P07 的宿主网关里做。
"""

import sys
from pathlib import Path
from typing import Any

import pytest

from yanxu_harness.core.agent_loop import AgentLoopConfig, run_agent_loop
from yanxu_harness.core.cancellation import CancellationToken
from yanxu_harness.core.events import ToolExecutionEnd, ToolExecutionUpdate
from yanxu_harness.core.messages import UnknownMessageRole, to_llm_messages
from yanxu_harness.core.types import (
    AgentContext,
    StopReason,
    TextContent,
    ToolResultMessage,
    ToolSpec,
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


DEFAULT_ARGS = {"path": "x"}


def tool_batch(
    *calls: str | tuple[str, str] | tuple[str, str, dict], stop: StopReason = StopReason.TOOL_USE
) -> ScriptedStream:
    """造一条带工具调用的响应：`"t"` 或 `(id, name)` 或 `(id, name, args)`。

    裸名是「一个工具、默认参数 `{"path": "x"}`、id 按序号」的简写，只给不关心参数形状的用例
    用。凡是断言 prepare/校验/hook 输入形状的用例都必须显式给 args —— 让默认 `{}` 悄悄流进
    三段，用例就变成在测另一件事。
    """
    blocks = []
    for index, entry in enumerate(calls):
        if isinstance(entry, str):
            blocks.append(tool_call(f"c{index}", entry, DEFAULT_ARGS))
        elif len(entry) == 2:
            call_id, name = entry
            blocks.append(tool_call(call_id, name, DEFAULT_ARGS))
        else:
            call_id, name, args = entry
            blocks.append(tool_call(call_id, name, args))
    return ScriptedStream(assistant(*blocks, stop=stop))


# --- 参数准备与验证 -----------------------------------------------------------


def test_prepare_arguments_runs_before_validation_and_feeds_the_tool() -> None:
    """agent-loop.ts:648-660：`prepare_arguments` 是校验前的兼容入口。"""
    seen: dict[str, Any] = {}

    def prepare(args: Any) -> dict:
        return {"path": args["short"], "content": args["content"]}

    def validator(tool, args: dict) -> dict:
        seen["validated"] = dict(args)
        if "path" not in args or "content" not in args:
            raise ValueError("path/content 必填")
        return args

    async def execute(_id, args, _signal=None, _on_update=None):
        seen["executed"] = dict(args)
        return {"content": [TextContent(text="ok")], "details": {}}

    stream = tool_batch(("c1", "write_chapter", {"short": "a.md", "content": "正文"}))
    tools = [make_tool("write_chapter", execute, prepare_arguments=prepare)]
    config = AgentLoopConfig(stream_fn=stream, tools=tools, argument_validator=validator)
    run(run_agent_loop([user("go")], AgentContext(messages=[], tools=tools), config, None, RecordingSink()))

    assert seen["validated"] == {"path": "a.md", "content": "正文"}
    assert seen["executed"] == {"path": "a.md", "content": "正文"}


def test_prepare_arguments_is_skipped_when_absent() -> None:
    seen: list[dict] = []

    def validator(_tool, args: dict) -> dict:
        seen.append(dict(args))
        return args

    stream = tool_batch("t")
    tools = [make_tool("t", ok_result)]
    config = AgentLoopConfig(stream_fn=stream, tools=tools, argument_validator=validator)
    run(run_agent_loop([user("go")], AgentContext(messages=[], tools=tools), config, None, RecordingSink()))

    assert seen == [dict(DEFAULT_ARGS)], "没有 prepare_arguments 时校验器收到原始参数"


def test_rejected_arguments_never_reach_execute() -> None:
    executed: list[str] = []

    async def execute(_id, _args, _signal=None, _on_update=None):
        executed.append("ran")
        return {"content": [], "details": {}}

    def validator(_tool, args: dict) -> dict:
        raise ValueError("参数不合法：缺 path")

    stream = tool_batch("t")
    tools = [make_tool("t", execute)]
    sink = RecordingSink()
    config = AgentLoopConfig(stream_fn=stream, tools=tools, argument_validator=validator)
    new_messages = run(run_agent_loop([user("go")], AgentContext(messages=[], tools=tools), config, None, sink))

    assert executed == []
    result = [m for m in new_messages if isinstance(m, ToolResultMessage)][0]
    assert result.is_error and "参数不合法" in result.content[0].text
    assert sink.of(ToolExecutionEnd)[0].is_error is True, "end 事件必须如实带 is_error"


# --- hook 语义 ---------------------------------------------------------------


def test_before_hook_receives_validated_args_and_context() -> None:
    """agent-loop.ts:102-112：before 看到的是**校验后**的参数，不是原始串。"""
    seen: dict[str, Any] = {}

    def validator(_tool, args: dict) -> dict:
        return {**args, "normalized": True}

    async def before(ctx, _signal=None):
        seen["args"] = dict(ctx["args"])
        seen["tool"] = ctx["tool_call"].name
        seen["has_context"] = "context" in ctx
        return None

    stream = tool_batch("t")
    tools = [make_tool("t", ok_result)]
    config = AgentLoopConfig(stream_fn=stream, tools=tools, argument_validator=validator, before_tool_call=before)
    run(run_agent_loop([user("go")], AgentContext(messages=[], tools=tools), config, None, RecordingSink()))

    assert seen["args"] == {"path": "x", "normalized": True}
    assert seen["tool"] == "t" and seen["has_context"] is True


def test_before_hook_terminate_flag_propagates_into_the_batch() -> None:
    """agent-loop.ts:700-702：block 时可以同时带 terminate，整批据此收尾。"""
    async def execute(_id, _args, _signal=None, _on_update=None):
        raise AssertionError("不该执行")

    async def before(_ctx, _signal=None):
        return {"block": True, "reason": "需要作者确认", "terminate": True}

    stream = tool_batch("t")
    tools = [make_tool("t", execute)]
    sink = RecordingSink()
    config = AgentLoopConfig(stream_fn=stream, tools=tools, before_tool_call=before)
    new_messages = run(run_agent_loop([user("go")], AgentContext(messages=[], tools=tools), config, None, sink))

    assert sink.count("turn_start") == 1, "block+terminate 的批次应当收尾而不是继续问模型"
    assert [m for m in new_messages if isinstance(m, ToolResultMessage)][0].content[0].text == "需要作者确认"


def test_after_hook_raising_replaces_the_result_with_an_error() -> None:
    """agent-loop.ts:809-812：after 抛错就把这一笔改成错误结果。"""
    async def execute(_id, _args, _signal=None, _on_update=None):
        return {"content": [TextContent(text="原始")], "details": {}}

    async def after(_ctx, _signal=None):
        raise RuntimeError("宿主网关拒绝：批准后参数变了")

    stream = tool_batch("t")
    tools = [make_tool("t", execute)]
    sink = RecordingSink()
    config = AgentLoopConfig(stream_fn=stream, tools=tools, after_tool_call=after)
    new_messages = run(run_agent_loop([user("go")], AgentContext(messages=[], tools=tools), config, None, sink))

    result = [m for m in new_messages if isinstance(m, ToolResultMessage)][0]
    assert result.is_error and "批准后参数变了" in result.content[0].text
    assert sink.of(ToolExecutionEnd)[0].is_error is True


def test_after_hook_sees_the_prepared_args_of_its_own_call() -> None:
    """并行路径上每个闭包必须绑自己那一笔：共享变量会让两笔都看到最后一笔的参数。"""

    async def execute(_id, _args, _signal=None, _on_update=None):
        return {"content": [], "details": {}}

    async def after(ctx, _signal=None):
        seen.append({"tool": ctx["tool_call"].name, "args": dict(ctx["args"])})
        return None

    seen: list[dict] = []
    stream = tool_batch(("c1", "a", {"path": "1.md"}), ("c2", "b", {"path": "2.md"}))
    tools = [make_tool("a", execute), make_tool("b", execute)]
    config = AgentLoopConfig(stream_fn=stream, tools=tools, after_tool_call=after)
    run(run_agent_loop([user("go")], AgentContext(messages=[], tools=tools), config, None, RecordingSink()))

    assert seen == [{"tool": "a", "args": {"path": "1.md"}}, {"tool": "b", "args": {"path": "2.md"}}]


# --- partial update ----------------------------------------------------------


def test_multiple_partial_updates_arrive_in_order_before_the_end_event() -> None:
    """agent-loop.ts:737-761：落定前要 await 已排队的 update，事件顺序才是可信的。"""
    async def execute(_id, _args, _signal=None, on_update=None):
        on_update({"content": [TextContent(text="第一段")], "details": {}})
        on_update({"content": [TextContent(text="第二段")], "details": {}})
        return {"content": [TextContent(text="完成")], "details": {}}

    stream = tool_batch("t")
    tools = [make_tool("t", execute)]
    sink = RecordingSink()
    config = AgentLoopConfig(stream_fn=stream, tools=tools)
    run(run_agent_loop([user("go")], AgentContext(messages=[], tools=tools), config, None, sink))

    updates = sink.of(ToolExecutionUpdate)
    assert [u.partial_result["content"][0].text for u in updates] == ["第一段", "第二段"]
    assert sink.types.index("tool_execution_end") > sink.types.index("tool_execution_update")


def test_update_from_a_failing_tool_still_arrives_before_its_end() -> None:
    async def execute(_id, _args, _signal=None, on_update=None):
        on_update({"content": [TextContent(text="写了一半")], "details": {}})
        raise RuntimeError("磁盘满了")

    stream = tool_batch("t")
    tools = [make_tool("t", execute)]
    sink = RecordingSink()
    config = AgentLoopConfig(stream_fn=stream, tools=tools)
    new_messages = run(run_agent_loop([user("go")], AgentContext(messages=[], tools=tools), config, None, sink))

    assert sink.count("tool_execution_update") == 1
    assert sink.types.index("tool_execution_end") > sink.types.index("tool_execution_update"), (
        "失败路径也要先把已排队的 update 落完 —— 少了这一步，事件不是丢了就是**晚到**，"
        "而晚到在「只看条数」的断言下是隐形的（T10 第一版就是这么活下来的）"
    )
    end = sink.of(ToolExecutionEnd)[0]
    assert (end.tool_call_id, end.is_error) == ("c0", True)
    assert "磁盘满了" in [m for m in new_messages if isinstance(m, ToolResultMessage)][0].content[0].text


def test_tool_raising_becomes_an_error_result_with_the_message_only() -> None:
    """P04 曾钉「类名要留着」，P07 对照上游 `agent-loop.ts:767` 推翻：只给 `error.message`。

    理由是宿主侧的既有契约，不是风格：`agent_tools._tool_failure_message` 已经把稳定域码
    （`story_state_error: ...`）拼进消息里，再套一层 Python 类名只会把 `KeyError: ...` 这种
    内部形状送进模型可见历史，而「模型看的是域码不是异常类型」正是那 700 条宿主用例在保的东西。
    """
    async def execute(_id, _args, _signal=None, _on_update=None):
        raise ValueError("章节不存在")

    stream = tool_batch("t")
    tools = [make_tool("t", execute)]
    sink = RecordingSink()
    config = AgentLoopConfig(stream_fn=stream, tools=tools)
    new_messages = run(run_agent_loop([user("go")], AgentContext(messages=[], tools=tools), config, None, sink))

    text = [m for m in new_messages if isinstance(m, ToolResultMessage)][0].content[0].text
    assert text == "章节不存在"


def test_result_without_content_normalises_to_empty_list() -> None:
    """agent-loop.ts:844-846：None 不得进入历史或 provider payload。

    两处归一（结果 dict 与结果消息）看着重复，但暴露面不同：消息里的 None 会进 provider
    payload，`ToolExecutionEnd.result` 里的 None 会进宿主 SSE。所以两条都要断言，否则摘掉
    任一条都是存活变异。
    """
    async def execute(_id, _args, _signal=None, _on_update=None):
        return {"content": None, "details": None}

    stream = tool_batch("t")
    tools = [make_tool("t", execute)]
    sink = RecordingSink()
    config = AgentLoopConfig(stream_fn=stream, tools=tools)
    new_messages = run(run_agent_loop([user("go")], AgentContext(messages=[], tools=tools), config, None, sink))

    result = [m for m in new_messages if isinstance(m, ToolResultMessage)][0]
    assert result.content == []
    assert result.details is None, "details 的 None 是合法值，不该被折成空 dict"
    assert sink.of(ToolExecutionEnd)[0].result["content"] == []


# --- 取消的两种落点 -----------------------------------------------------------


def test_cancellation_during_preflight_yields_an_aborted_result() -> None:
    """agent-loop.ts:691-696 与 710-716：before hook 之后、执行之前也要检查取消。

    必须走**串行**批次：并行路径上排在队列里的闭包自己还会再查一次取消（576-584），
    那道守卫会把 preflight 的两处检查完全遮住 —— 变异 T12 因此在并行形状下等价存活。
    串行侧 505→514 之间没有任何取消检查，preflight 的守卫是唯一的兜点。
    """
    token = CancellationToken("run-1")

    async def execute(_id, _args, _signal=None, _on_update=None):
        raise AssertionError("取消之后不该执行")

    async def before(_ctx, _signal=None):
        token.cancel("作者停止")
        return None

    stream = tool_batch("t")
    tools = [make_tool("t", execute, execution_mode="sequential")]
    sink = RecordingSink()
    config = AgentLoopConfig(stream_fn=stream, tools=tools, before_tool_call=before)
    new_messages = run(run_agent_loop([user("go")], AgentContext(messages=[], tools=tools), config, token, sink))

    result = [m for m in new_messages if isinstance(m, ToolResultMessage)][0]
    assert result.is_error and result.content[0].text == "Operation aborted"


def test_cancellation_before_any_hook_yields_an_aborted_result() -> None:
    """agent-loop.ts:710-716：没挂 before hook 时，进执行前那道检查也得拦。

    与上一条分开写是必要的：上游 691 与 710 是同一件事的两道守卫，只摘一处时另一处会兜住，
    变异因此等价存活（见 T12 / T12b 的拆法）。
    """
    token = CancellationToken("run-1b")
    started: list[str] = []

    async def execute(_id, _args, _signal=None, _on_update=None):
        started.append("ran")
        return {"content": [], "details": {}}

    token.cancel("作者停止")
    stream = tool_batch("t")
    tools = [make_tool("t", execute, execution_mode="sequential")]
    sink = RecordingSink()
    config = AgentLoopConfig(stream_fn=stream, tools=tools)
    new_messages = run(run_agent_loop([user("go")], AgentContext(messages=[], tools=tools), config, token, sink))

    assert started == [], "取消之后工具一次都不该跑"
    result = [m for m in new_messages if isinstance(m, ToolResultMessage)][0]
    assert result.is_error and result.content[0].text == "Operation aborted"


def test_parallel_batch_launches_nothing_after_the_signal_fires() -> None:
    """agent-loop.ts:575-584：闭包已排队不等于起跑 —— 取消时它回错误结果，不执行工具。

    注意断言的形状：第二笔**有**结果（`Operation aborted`），只是没被执行。上游在
    `Promise.all` 前把闭包全排好了，所以「没有结果」反而是这里的偏离 —— 而每一笔调用都必须
    有对应结果消息，否则 provider 下一轮会因为 toolCall 缺 result 直接拒答。真正的
    「结果未知」由宿主网关按 operation 证据判定，不在这一层靠留白表达。
    """
    token = CancellationToken("run-2")
    started: list[str] = []

    async def first(_id, _args, _signal=None, _on_update=None):
        started.append("first")
        token.cancel("作者停止")
        return {"content": [], "details": {}}

    async def second(_id, _args, _signal=None, _on_update=None):
        started.append("second")
        return {"content": [], "details": {}}

    stream = tool_batch(("c1", "first"), ("c2", "second"))
    tools = [make_tool("first", first), make_tool("second", second)]
    sink = RecordingSink()
    config = AgentLoopConfig(stream_fn=stream, tools=tools)
    new_messages = run(run_agent_loop([user("go")], AgentContext(messages=[], tools=tools), config, token, sink))

    assert started == ["first"], "取消后第二笔不能起跑"
    results = [m for m in new_messages if isinstance(m, ToolResultMessage)]
    assert [m.tool_call_id for m in results] == ["c1", "c2"], "每一笔调用都要有结果，顺序按源顺序"
    assert results[0].is_error is False and results[1].is_error is True
    assert results[1].content[0].text == "Operation aborted"
    ends = sink.of(ToolExecutionEnd)
    assert [e.tool_call_id for e in ends] == ["c1", "c2"], "end 事件也得逐笔齐，SSE 才有收尾依据"


# --- 上游映射里 P04 才补上的行 -------------------------------------------------


def test_before_hook_mutating_args_in_place_reaches_the_tool_without_revalidation() -> None:
    """agent-loop.test.ts:480 的原文语义：hook 就地改 `args`，工具拿到改过的值，且**不再校验**。

    这不是「我们支持改写参数」，而是**登记一个真实的漏洞面**：core 只在校验前把 args 交给
    hook，之后没有任何再校验点。所以计划 §108「作者批准后再次校验并冻结」必须在 P07 的宿主
    网关里做，不能指望 core —— 这条用例的作用是让那个前提可测：一旦哪天 core 里加了深拷贝或
    再校验（行为变了），这条会红，P07 的网关也就知道该跟着改。
    """
    validated: list[dict] = []
    executed: list[dict] = []

    def validator(_tool, args: dict) -> dict:
        validated.append(dict(args))
        return args

    async def execute(_id, args, _signal=None, _on_update=None):
        executed.append(dict(args))
        return {"content": [], "details": {}}

    async def before(ctx, _signal=None):
        ctx["args"]["chapter"] = "第二章"
        return None

    stream = tool_batch(("c1", "t", {"chapter": "第一章"}))
    tools = [make_tool("t", execute)]
    config = AgentLoopConfig(stream_fn=stream, tools=tools, argument_validator=validator, before_tool_call=before)
    run(run_agent_loop([user("go")], AgentContext(messages=[], tools=tools), config, None, RecordingSink()))

    assert validated == [{"chapter": "第一章"}], "校验只发生一次，且在 hook 之前"
    assert executed == [{"chapter": "第二章"}], "hook 的就地改写直达工具：core 不复核"


def test_after_hook_can_mark_the_batch_terminating() -> None:
    """agent-loop.test.ts:1468：after 只回 `{"terminate": True}` 也要能收尾整批。"""

    async def execute(_id, _args, _signal=None, _on_update=None):
        return {"content": [TextContent(text="原始")], "details": {}}

    async def after(_ctx, _signal=None):
        return {"terminate": True}

    stream = tool_batch("t")
    tools = [make_tool("t", execute)]
    sink = RecordingSink()
    config = AgentLoopConfig(stream_fn=stream, tools=tools, after_tool_call=after)
    run(run_agent_loop([user("go")], AgentContext(messages=[], tools=tools), config, None, sink))

    assert sink.count("turn_start") == 1, "after 标了 terminate 就不该再问模型"
    ends = sink.of(ToolExecutionEnd)
    assert ends[0].result["terminate"] is True
    assert ends[0].result["content"][0].text == "原始", "只补 terminate，其余字段保留"


def test_after_hook_patches_usage_the_same_way_it_patches_content() -> None:
    """agent-loop.test.ts:312：那条用例真正断的是 result.usage 被 after 改写后进了消息。"""

    async def execute(_id, _args, _signal=None, _on_update=None):
        return {"content": [TextContent(text="原始")], "details": {"n": 1}, "usage": {"input": 5}}

    async def after(_ctx, _signal=None):
        return {"usage": {"input": 7}}

    stream = tool_batch("t")
    tools = [make_tool("t", execute)]
    config = AgentLoopConfig(stream_fn=stream, tools=tools, after_tool_call=after)
    new_messages = run(
        run_agent_loop([user("go")], AgentContext(messages=[], tools=tools), config, None, RecordingSink())
    )

    result = [m for m in new_messages if isinstance(m, ToolResultMessage)][0]
    assert result.usage == {"input": 7}
    assert result.details == {"n": 1} and result.content[0].text == "原始"


def test_steering_arrives_only_after_every_tool_in_the_batch_finished() -> None:
    """agent-loop.test.ts:714：批次里两个工具都跑完，插话才进下一次请求。

    顺序断言而不是条数断言：宿主 SSE 与审批都按「一次请求一个自洽上下文」设计，插话夹在两个
    toolResult 之间会让 provider 直接拒答。
    """
    token_order: list[str] = []

    async def first(_id, _args, _signal=None, _on_update=None):
        token_order.append("first")
        return {"content": [], "details": {}}

    async def second(_id, _args, _signal=None, _on_update=None):
        token_order.append("second")
        return {"content": [], "details": {}}

    queue = [user("插一句")]
    polls = [0]

    async def get_steering():
        # 按轮询次数放行，而不是按「已经发过几次请求」：夹具要独立于实现内部节奏，
        # 否则变异把轮询挪个位置就同时改掉了夹具的条件（第一版就是这样，T16 因此逃过）。
        polls[0] += 1
        return [queue.pop(0)] if queue and polls[0] >= 2 else []

    stream = stepping(
        assistant(tool_call("c1", "first"), tool_call("c2", "second"), stop=StopReason.TOOL_USE),
        assistant(TextContent(text="收尾")),
    )
    tools = [make_tool("first", first), make_tool("second", second, execution_mode="sequential")]
    config = AgentLoopConfig(stream_fn=stream, tools=tools, get_steering_messages=get_steering)
    run(run_agent_loop([user("开始")], AgentContext(messages=[], tools=tools), config, None, RecordingSink()))

    assert token_order == ["first", "second"]
    assert polls[0] >= 2
    producing_request = [m.role for m in stream.calls[0][1].messages]
    assert producing_request.count("user") == 1, "跑出这批工具的那次请求里还没有插话"
    roles = [m.role for m in stream.calls[-1][1].messages]
    steering_at = max(i for i, r in enumerate(roles) if r == "user")
    assert len([r for r in roles if r == "toolResult"]) == 2
    assert all(i < steering_at for i, r in enumerate(roles) if r == "toolResult"), (
        f"两个 toolResult 都得在插话之前落账：{roles}"
    )


def test_a_registered_tool_without_execute_is_an_error_not_a_type_error() -> None:
    """`ToolSpec.execute` 是可选项：只登记未实现的工具必须回错误结果。

    这条是给 P07 的工具注册表兜底的：宿主按名字装表，漏配 execute 是个「有声明、没法执行」
    的形状，绝不能变成把 TypeError 当成模型可见内容发回去。
    """
    stream = tool_batch("t")
    tools = [ToolSpec(name="t", description="t", parameters={"type": "object"})]
    sink = RecordingSink()
    config = AgentLoopConfig(stream_fn=stream, tools=tools)
    new_messages = run(run_agent_loop([user("go")], AgentContext(messages=[], tools=tools), config, None, sink))

    result = [m for m in new_messages if isinstance(m, ToolResultMessage)][0]
    assert result.is_error and "no execute" in result.content[0].text
    assert sink.count("tool_execution_start") == 1 and sink.count("tool_execution_end") == 1


# --- 无真实写副作用 -----------------------------------------------------------


def test_no_real_write_side_effects(tmp_path: Path, monkeypatch) -> None:
    """执行器只调注入的可调用对象：不碰 vault、不碰宿主工具表。"""
    monkeypatch.chdir(tmp_path)
    before = set(Path(tmp_path).rglob("*"))

    async def fake_write(_id, args, _signal=None, _on_update=None):
        return {"content": [TextContent(text=f"假装写了 {args}")], "details": {}}

    stream = stepping(
        assistant(tool_call("c1", "write_chapter", {"path": "第三章.md"}), stop=StopReason.TOOL_USE),
        assistant(TextContent(text="好")),
    )
    tools = [make_tool("write_chapter", fake_write)]
    config = AgentLoopConfig(stream_fn=stream, tools=tools)
    sink = RecordingSink()
    modules_before = set(sys.modules)

    run(run_agent_loop([user("写")], AgentContext(messages=[], tools=tools), config, None, sink))

    assert set(Path(tmp_path).rglob("*")) == before, "跑完一批工具后目录里不该多出任何文件"
    assert not (tmp_path / "第三章.md").exists()
    # 只断言「这一批工具自己拖进了什么」：整套测试里宿主模块早就被别的用例导过了，
    # 拿全进程的 sys.modules 说事会让本条用例只在单跑时绿（全量跑必假红）。
    newly_imported = sorted(
        name
        for name in set(sys.modules) - modules_before
        if name.startswith("backend.") and not name.startswith("yanxu_harness.core")
    )
    assert newly_imported == [], f"core 执行器把宿主模块拖进了进程：{newly_imported}"


def test_executor_does_not_promote_unknown_custom_messages() -> None:
    """工具结果都是内建 role；宿主若塞进未登记 role，必须在进模型前被拦。"""
    class Stray:
        role = "notification"

    with pytest.raises(UnknownMessageRole):
        to_llm_messages([Stray()])


def test_a_failing_tool_with_an_empty_message_still_names_its_type() -> None:
    """上游的 `String(error)` 在空消息时至少留下可诊断的东西；这里退化为类型名而不是空串。"""
    async def execute(_id, _args, _signal=None, _on_update=None):
        raise RuntimeError("")

    stream = tool_batch("t")
    tools = [make_tool("t", execute)]
    sink = RecordingSink()
    config = AgentLoopConfig(stream_fn=stream, tools=tools)
    new_messages = run(run_agent_loop([user("go")], AgentContext(messages=[], tools=tools), config, None, sink))

    result = [m for m in new_messages if isinstance(m, ToolResultMessage)][0]
    assert result.is_error is True
    assert result.content[0].text == "RuntimeError"
