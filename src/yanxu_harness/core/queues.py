"""0.6.0 P02：steering / follow-up 队列。

对照 `agent/src/agent.ts:140-174` 的 `PendingMessageQueue`：两种 `QueueMode`，
`one-at-a-time` 只出队头一条、其余留下一次排空点，`all` 一次全出。计划 §4.4 明确默认
`one-at-a-time`，并强调「core 队列是内存能力」—— StoryIDE 的持久用户消息必须走
AgentService 的既有持久队列，不能拿这个类当第二份真相（§115）。
"""

from __future__ import annotations

from typing import Any, Iterator

__all__ = ["QUEUE_MODES", "MessageQueue"]

QUEUE_MODES = ("all", "one-at-a-time")


class MessageQueue:
    __slots__ = ("mode", "_messages")

    def __init__(self, mode: str = "one-at-a-time") -> None:
        if mode not in QUEUE_MODES:
            raise ValueError(f"未知 queue 模式 {mode!r}，只允许 {QUEUE_MODES}")
        self.mode = mode
        self._messages: list[Any] = []

    def enqueue(self, message: Any) -> None:
        self._messages.append(message)

    def has_items(self) -> bool:
        return len(self._messages) > 0

    def drain(self) -> list[Any]:
        """按模式取走一批；空队列返回空列表（不是 None），调用方不需要判空指针。"""
        if self.mode == "all":
            drained = self._messages[:]
            self._messages = []
            return drained
        if not self._messages:
            return []
        return [self._messages.pop(0)]

    def clear(self) -> None:
        self._messages = []

    def __len__(self) -> int:
        return len(self._messages)

    def __iter__(self) -> Iterator[Any]:
        return iter(list(self._messages))
