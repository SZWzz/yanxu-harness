"""0.6.0 P02：取消令牌与队列（对照 `agent.ts` 的 ActiveRun / PendingMessageQueue）。

两条计划要求：§4.4 的「取消传播到工具、有界清理、迟到结果不得再写终态」，以及 §117 的
「迟到回调带 run/generation fencing」。另外 P01 契约范围 §2.1 定了：core **不通过
`contextvars` 隐式传播**取消 —— 每个会等待的入口显式收令牌，否则「忘了传令牌」这种缺陷
会静默通过。
"""

import asyncio

import pytest

from yanxu_harness.core.cancellation import CancellationToken
from yanxu_harness.core.queues import MessageQueue


def run(coro):
    async def guard():
        return await asyncio.wait_for(coro, timeout=2)

    return asyncio.run(guard())


# --- 取消令牌 -----------------------------------------------------------------


def test_token_starts_not_cancelled_and_is_cooperative() -> None:
    token = CancellationToken("run-1")
    assert token.cancelled is False
    token.cancel("作者点了停止")
    assert token.cancelled is True
    assert token.reason == "作者点了停止"


def test_wait_returns_immediately_when_already_cancelled() -> None:
    async def scenario():
        token = CancellationToken("run-2")
        token.cancel()
        await token.wait_cancelled()
        return "unblocked"

    assert run(scenario()) == "unblocked"


def test_waiter_is_released_by_cancel() -> None:
    async def scenario():
        token = CancellationToken("run-3")
        waiter = asyncio.create_task(token.wait_cancelled())
        await asyncio.sleep(0)
        token.cancel()
        await waiter
        return True

    assert run(scenario()) is True


def test_cancel_is_idempotent_and_first_reason_wins() -> None:
    token = CancellationToken("run-4")
    token.cancel("第一次")
    token.cancel("第二次")
    assert token.reason == "第一次"


def test_unrelated_token_does_not_release_a_waiter() -> None:
    """generation fencing 的最小形状：回调只认自己那一代。"""
    async def scenario():
        current = CancellationToken("run-5")
        stale = CancellationToken("run-5-generation-older")
        stale.cancel()
        try:
            await asyncio.wait_for(current.wait_cancelled(), timeout=0.05)
            return "released"
        except asyncio.TimeoutError:
            return "still-running"

    assert run(scenario()) == "still-running"


def test_guard_raises_inside_a_cancelled_scope() -> None:
    async def scenario():
        token = CancellationToken("run-6")
        token.cancel()
        with pytest.raises(asyncio.CancelledError):
            with token.scope():
                pass
        return True

    assert run(scenario()) is True


# --- 队列：两种 drain 模式 ----------------------------------------------------


def test_one_at_a_time_leaves_the_rest_queued() -> None:
    """agent.ts:156-169 默认模式；计划 §4.4 要求默认 one-at-a-time。"""
    queue = MessageQueue("one-at-a-time")
    for i in range(3):
        queue.enqueue(i)
    assert queue.drain() == [0]
    assert queue.drain() == [1]
    assert queue.has_items() is True


def test_all_mode_drains_every_queued_message_at_once() -> None:
    queue = MessageQueue("all")
    for i in range(3):
        queue.enqueue(i)
    assert queue.drain() == [0, 1, 2]
    assert queue.drain() == []
    assert queue.has_items() is False


def test_drain_on_empty_queue_returns_empty_list_not_none() -> None:
    assert MessageQueue("all").drain() == []


def test_clear_drops_everything_and_reports_empty() -> None:
    queue = MessageQueue("all")
    queue.enqueue("x")
    queue.clear()
    assert queue.drain() == []


def test_unknown_queue_mode_is_refused_at_construction() -> None:
    with pytest.raises(ValueError):
        MessageQueue("sometimes")
