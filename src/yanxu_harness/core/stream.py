"""0.6.0 P02：core 的异步事件流。

对照 `pi-ai@0.86.1` 的 `src/utils/event-stream.ts`（本地副本
`docs/reference/pi-0.86.1/upstream/packages/ai/src/utils/event-stream.ts`）。

照抄的三条语义：
* 终态事件本身仍要投递（:46-57）—— 消费者必须能看到 `done`/`error`，不能只靠 `result()`。
* `done` 之后再 push 静默丢弃（:44）—— 迟到帧不得追加第二个终态。
* `result()` 只有一个终态值；`end()` 不传值就永远悬着（:38,60-64,86-88）。

**登记的行为差异**：上游 `FifoQueue` 无界（:3-23），而计划 §115 要求「事件缓冲有上限」。
所以这里加 `max_events`，溢出是一等结果（`push` 返回 `False` + `overflow_count`），
不是把流静默变成 done、也不是丢中间帧。原因：StoryIDE 的 SSE 投影与作者可读的进度都
挂在这些帧上，无声丢帧会伪装成「模型没输出」。
"""

from __future__ import annotations
from typing import Any, AsyncIterator, Callable, Generic, Mapping, TypeVar

import asyncio

__all__ = ["AssistantMessageEventStream", "EventStream", "create_assistant_message_event_stream"]

T = TypeVar("T")
R = TypeVar("R")


class EventStream(Generic[T, R]):
    """单终态的异步事件流。"""

    def __init__(
        self,
        is_complete: Callable[[T], bool],
        extract_result: Callable[[T], R],
        *,
        max_events: int | None = None,
    ) -> None:
        if not callable(is_complete) or not callable(extract_result):
            raise TypeError("is_complete 与 extract_result 都必须是可调用对象")
        if max_events is not None and max_events < 0:
            raise ValueError("max_events 不能为负")
        self._is_complete = is_complete
        self._extract_result = extract_result
        self._max_events = max_events
        self._queue: list[T] = []
        self._waiters: list[asyncio.Future] = []
        self._done = False
        self._final: asyncio.Future | None = None
        self._final_resolved = False
        self._final_value: R | None = None
        self.overflow_count = 0

    @property
    def done(self) -> bool:
        return self._done

    @property
    def queued_count(self) -> int:
        return len(self._queue)

    def push(self, event: T) -> bool:
        """投递一个事件。返回 False = 被拒（流已结束或缓冲已满）。

        终态事件**永远不被上限拒**：否则一个填满的缓冲会让流再也到不了终态，消费者
        永久等待 —— 比丢一帧中间结果严重得多（本轮第一版就是这么写的，被测试挂死抓到）。
        """
        if self._done:
            return False
        terminal = bool(self._is_complete(event))
        if not terminal and self._max_events is not None and len(self._queue) >= self._max_events:
            self.overflow_count += 1
            return False
        if terminal:
            self._done = True
            self._resolve_final(self._extract_result(event))
        waiter = self._take_waiter()
        if waiter is not None:
            waiter.set_result((False, event))
        else:
            self._queue.append(event)
        return True

    def end(self, result: R | None = None) -> None:
        self._done = True
        if result is not None:
            self._resolve_final(result)
        while self._waiters:
            waiter = self._waiters.pop(0)
            if not waiter.done():
                waiter.set_result((True, None))

    async def result(self) -> R:
        if self._final_resolved:
            return self._final_value  # type: ignore[return-value]
        if self._final is None:
            loop = asyncio.get_running_loop()
            self._final = loop.create_future()
        return await self._final  # type: ignore[no-any-return]

    def __aiter__(self) -> AsyncIterator[T]:
        return self

    async def __anext__(self) -> T:
        if self._queue:
            # 先出队：上游 :74-75 优先给已入队的事件，即使同时有 waiter 也不插队。
            return self._queue.pop(0)
        if self._done:
            raise StopAsyncIteration
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future = loop.create_future()
        self._waiters.append(waiter)
        finished, event = await waiter
        if finished:
            raise StopAsyncIteration
        return event

    def _resolve_final(self, result: R) -> None:
        """终值只认第一次；先 push 终态还是先 `result()` 都不影响能取到的值。"""
        if self._final_resolved:
            return
        self._final_resolved = True
        self._final_value = result
        if self._final is not None and not self._final.done():
            self._final.set_result(result)

    def _take_waiter(self) -> asyncio.Future | None:
        while self._waiters:
            waiter = self._waiters.pop(0)
            if not waiter.done():
                return waiter
        return None


AssistantMessageEventStream = EventStream  # 上游同名子类的 Python 形态：参数化即可，不另造壳


def create_assistant_message_event_stream(*, max_events: int | None = None) -> EventStream:
    """Create the assistant-message stream the core loop consumes.

    The frame vocabulary belongs to core — ``done``/``error`` are terminal and the
    final message travels in the frame's ``partial`` — so the factory lives here
    rather than being re-derived by every adapter. (`__all__` has named this
    function since the initial import; it just was never defined, so any
    ``from yanxu_harness.core.stream import *`` raised ``AttributeError``.)

    ``max_events`` bounds the *buffer*. A terminal frame is never rejected by the
    bound, so a full buffer cannot strand a consumer waiting for the end.
    """

    def is_complete(frame: Any) -> bool:
        return isinstance(frame, Mapping) and frame.get("type") in {"done", "error"}

    def extract_result(frame: Any) -> Any:
        return frame.get("partial") if isinstance(frame, Mapping) else None

    return EventStream(is_complete, extract_result, max_events=max_events)
