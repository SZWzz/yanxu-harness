"""0.6.0 P02：显式取消令牌与 generation fencing。

计划 §4.4 要求「取消关闭 provider 流、传播工具 token、等待有界清理」，§117 要求「迟到
回调带 run/generation fencing，不再写入终态」。

**刻意不做的一件事**：不用 `contextvars` 隐式传播。上游用 `AbortSignal` 参数一路传
（`agent.ts` 的 `ActiveRun.abortController` 与各 hook 的 `signal?:`），Python 侧对齐成显式
参数。理由：隐式上下文下「某个协程忘了把令牌传下去」不会有任何症状，而 §4.4 恰恰要求能
测出取消到了哪一层。这是 P01 契约范围 §2.1 对 chord `Context` 一族判「不适用」的落点。
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from typing import Iterator

__all__ = ["CancellationToken"]


class CancellationToken:
    """一次 run 的取消令牌。`run_id` 就是 fencing 的键。"""

    __slots__ = ("run_id", "_event", "_reason")

    def __init__(self, run_id: str) -> None:
        if not run_id:
            raise ValueError("run_id 不能为空：它是迟到回调唯一能认出的身份")
        self.run_id = run_id
        self._event: asyncio.Event | None = None
        self._reason: str | None = None

    @property
    def cancelled(self) -> bool:
        return self._reason is not None

    @property
    def reason(self) -> str | None:
        return self._reason

    def cancel(self, reason: str | None = None) -> bool:
        """请求取消。返回 False = 已经是取消态（首次原因胜出，后续覆盖无效）。"""
        if self._reason is not None:
            return False
        self._reason = reason or "cancelled"
        if self._event is not None:
            self._event.set()
        return True

    async def wait_cancelled(self) -> str:
        if self._event is None:
            self._event = asyncio.Event()
            if self._reason is not None:
                self._event.set()
        await self._event.wait()
        assert self._reason is not None
        return self._reason

    @contextmanager
    def scope(self) -> Iterator["CancellationToken"]:
        """进入一个可取消的作用域：已取消就立刻抛，正常退出时不留悬挂任务。"""
        if self.cancelled:
            raise asyncio.CancelledError(f"run {self.run_id} 已取消：{self._reason}")
        yield self

    def guard(self) -> None:
        if self.cancelled:
            raise asyncio.CancelledError(f"run {self.run_id} 已取消：{self._reason}")

    def child(self, purpose: str) -> "CancellationToken":
        """派生一个子令牌：父取消会连带取消子，子取消不碰父。

        工具执行与 provider 流各自拿一个子令牌，才能让「只停工具不停流」和
        「停流必停工具」这两种形状在 P04 里分别被测。
        """
        child = CancellationToken(f"{self.run_id}:{purpose}")
        parent = self

        async def propagate() -> None:
            await parent.wait_cancelled()
            child.cancel(f"父 run 取消：{parent.reason}")

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            loop.create_task(propagate())
        return child

