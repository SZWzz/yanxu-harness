"""0.6.0 P02：EventStream 与流终态（对照 `.../ai/src/utils/event-stream.ts` @ c7cdb46）。

上游语义逐行照抄：终态事件本身仍要投递给消费者（:46-57），done 之后再 push 静默丢弃
（:44），`result()` 只有一个终态值（:38,86）。**一处刻意的行为差异**：计划 §115 要求
「事件缓冲有上限」，而上游 `FifoQueue` 无界（:3-23）——所以这里加 `max_events`，溢出是
一个显式的、可断言的结果，不是背压下的随机丢帧。差异登记在本文件与专项进度里。
"""

import asyncio

import pytest

from yanxu_harness.core.stream import EventStream


def run(coro):
    """所有异步场景都带超时：死锁必须表现为失败，而不是把整个测试会话挂住。

    这不是防御性冗余 —— 本文件的第一版就因为「溢出把终态事件也拒了」让一个用例永久
    等待，整套测试挂到超时才被抓到。
    """

    async def guard():
        return await asyncio.wait_for(coro, timeout=2)

    return asyncio.run(guard())


def is_done(event) -> bool:
    return isinstance(event, dict) and event.get("type") == "done"


def extract(event):
    return event.get("message") if event.get("type") == "done" else None


def make(max_events: int | None = None) -> EventStream:
    return EventStream(is_complete=is_done, extract_result=extract, max_events=max_events)


def test_events_are_delivered_in_fifo_order_to_a_consumer_that_arrives_late() -> None:
    async def scenario():
        stream = make()
        for i in range(5):
            stream.push({"type": "delta", "i": i})
        stream.push({"type": "done", "message": "final"})
        return [(event["type"], event.get("i")) async for event in stream]

    assert run(scenario()) == [
        ("delta", 0), ("delta", 1), ("delta", 2), ("delta", 3), ("delta", 4), ("done", None)
    ]


def test_terminal_event_is_still_delivered_before_iteration_ends() -> None:
    """event-stream.ts:46-57 置 done 之后仍然投递/入队该事件，消费者要能看到 done。"""
    async def scenario():
        stream = make()
        stream.push({"type": "done", "message": "final"})
        types = [event["type"] async for event in stream]
        return types, await stream.result()

    types, final = run(scenario())
    assert types == ["done"]
    assert final == "final"


def test_push_after_done_is_dropped_without_raising() -> None:
    """event-stream.ts:44 `if (this.done) return`：迟到帧静默丢弃，不报错也不追加终态。"""
    async def scenario():
        stream = make()
        stream.push({"type": "done", "message": "final"})
        stream.push({"type": "delta", "i": 99})
        return [event async for event in stream]

    assert run(scenario()) == [{"type": "done", "message": "final"}]


def test_end_without_result_leaves_final_value_unset() -> None:
    """event-stream.ts:60-64 只有显式 result 才 resolve；否则 result() 永远等不到值。"""
    async def scenario():
        stream = make()
        stream.end()
        try:
            return await asyncio.wait_for(stream.result(), timeout=0.05)
        except asyncio.TimeoutError:
            return "pending"

    assert run(scenario()) == "pending"


def test_a_waiting_consumer_gets_the_next_event_immediately() -> None:
    """event-stream.ts:52-57 有 waiter 时直接交给它，不入队。"""
    async def scenario():
        stream = make()

        async def consume():
            return await stream.__anext__()

        consumer = asyncio.create_task(consume())
        await asyncio.sleep(0)
        stream.push({"type": "delta", "i": 1})
        return await consumer

    assert run(scenario()) == {"type": "delta", "i": 1}


def test_registered_divergence_event_buffer_is_bounded_and_overflow_is_explicit() -> None:
    """计划 §115：事件缓冲必须有上限。上游无界，这里是登记过的差异。

    溢出必须是可断言的显式结果，且不能吞掉已经入队的帧，也不能把流静默变成 done。
    """
    async def scenario():
        stream = make(max_events=3)
        pushed = [stream.push({"type": "delta", "i": i}) for i in range(5)]
        pushed.append(stream.push({"type": "done", "message": "final"}))
        events = [event async for event in stream]
        return pushed, [e.get("i", "done") for e in events], stream.overflow_count

    pushed, seen, overflow = run(scenario())
    assert seen == [0, 1, 2, "done"], "上限之外的帧不进缓冲，终态仍要送达"
    assert overflow == 2
    assert pushed == [True, True, True, False, False, True]


def test_overflow_never_eats_the_terminal_event() -> None:
    """填满缓冲之后，终态事件仍必须被接受 —— 否则流到不了终态，消费者永久等待。

    本文件的第一版正是这个形状：溢出检查放在终态判定之前，一个用例直接挂死。
    """
    async def scenario():
        stream = make(max_events=1)
        accepted_delta = stream.push({"type": "delta", "i": 0})
        rejected_delta = stream.push({"type": "delta", "i": 1})
        accepted_done = stream.push({"type": "done", "message": "final"})
        events = [event async for event in stream]
        return accepted_delta, rejected_delta, accepted_done, events, await stream.result()

    accepted_delta, rejected_delta, accepted_done, events, final = run(scenario())
    assert (accepted_delta, rejected_delta, accepted_done) == (True, False, True)
    assert [e["type"] for e in events] == ["delta", "done"]
    assert final == "final"


def test_zero_length_stream_resolves_result_via_end_argument() -> None:
    async def scenario():
        stream = make()
        stream.end({"message": "由 end 指定"})
        return await stream.result()

    assert run(scenario()) == {"message": "由 end 指定"}


def test_two_consumers_do_not_steal_each_others_events_when_queued() -> None:
    """队列里的事件按 FIFO 出，两个消费者各拿到不重叠的一批 —— 顺序由队列保证，不靠运气。"""
    async def scenario():
        stream = make()
        for i in range(4):
            stream.push({"type": "delta", "i": i})
        stream.end()
        first = [e async for e in stream]
        second = [e async for e in stream]
        return first, second

    first, second = run(scenario())
    assert [e["i"] for e in first] == [0, 1, 2, 3]
    assert second == [], "流已耗尽，第二个消费者不应重复拿到同一批帧"


def test_missing_is_complete_predicate_is_rejected_at_construction() -> None:
    with pytest.raises(TypeError):
        EventStream(is_complete=None, extract_result=extract)


def test_the_assistant_message_stream_factory_uses_the_loop_vocabulary() -> None:
    """The factory `__all__` advertises: `done`/`error` are terminal, `partial` is the result."""
    from yanxu_harness.core.stream import create_assistant_message_event_stream

    async def scenario():
        stream = create_assistant_message_event_stream(max_events=1)
        assert stream.push({"type": "text_delta", "partial": "a"}) is True
        assert stream.push({"type": "text_delta", "partial": "b"}) is False
        assert stream.overflow_count == 1
        # A full buffer must never reject the terminal frame, or a consumer waits forever.
        assert stream.push({"type": "done", "partial": "final"}) is True
        frames = [frame async for frame in stream]
        return frames, await stream.result()

    frames, result = run(scenario())
    assert [frame["type"] for frame in frames] == ["text_delta", "done"]
    assert result == "final"


def test_the_assistant_message_stream_factory_reports_an_error_frame_as_terminal() -> None:
    from yanxu_harness.core.stream import create_assistant_message_event_stream

    async def scenario():
        stream = create_assistant_message_event_stream()
        stream.push({"type": "error", "partial": "half"})
        return await stream.result()

    assert run(scenario()) == "half"
