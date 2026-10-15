"""Deliver frames from a blocking iterator without blocking the event loop.

The default transport is :mod:`http.client`: synchronous by design, with no
standard-library async equivalent. Iterating it inside an async generator blocks
the event loop for the whole request — measured on this repository, a one-second
server delay produced **zero** loop iterations, so a slow model would freeze
cancellation, tool execution and every other task in the process. A worker thread
is the smallest stdlib bridge, and this module is that bridge with its bounds
written down:

* **Timeout.** The socket timeout belongs to the transport
  (``ProviderConfig.timeout_seconds``): a stuck read becomes a provider failure,
  not a hung pump.
* **Stopping.** ``should_stop`` is consulted between frames. While the worker is
  blocked in a read, ``abort`` — the transport closing its connection — is what
  makes it return.
* **Cleanup.** :meth:`FramePump.pump` always runs its teardown when the consumer
  finishes *or* abandons the iterator (``aclose()``): it calls ``abort`` and then
  waits for the worker, bounded by ``settle_timeout``. If the wait expires, that
  is recorded on the pump (:attr:`FramePump.timed_out`) instead of hidden — the
  worker is still bounded by the transport's own socket timeout.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, AsyncIterator, Callable, Iterator

__all__ = ["FramePump"]

#: Internal end-of-stream marker. A sentinel object rather than ``None`` because
#: ``None`` is a legal frame value.
_DONE = object()


class FramePump:
    """One blocking iterator, pumped onto the event loop with bounded teardown."""

    def __init__(
        self,
        produce: Callable[[], Iterator[Any]],
        *,
        abort: Callable[[], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
        poll_interval: float = 0.05,
        settle_timeout: float = 5.0,
    ) -> None:
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        if settle_timeout <= 0:
            raise ValueError("settle_timeout must be positive")
        self._produce = produce
        self._abort = abort
        self._should_stop = should_stop
        self._poll_interval = poll_interval
        self._settle_timeout = settle_timeout
        #: True when the consumer asked to stop before the iterator ended.
        self.stopped_early = False
        #: True when teardown could not join the worker inside ``settle_timeout``.
        self.timed_out = False
        self._settled = False

    async def pump(self) -> AsyncIterator[Any]:
        """Yield the iterator's items; always tears the worker down before returning."""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Any] = asyncio.Queue()

        def deliver(item: Any) -> None:
            # Called on the loop thread (call_soon_threadsafe), so put_nowait is safe.
            queue.put_nowait(item)

        def run() -> None:
            try:
                for frame in self._produce():
                    loop.call_soon_threadsafe(deliver, frame)
                    if self._should_stop is not None and self._should_stop():
                        self.stopped_early = True
                        return
            except BaseException as exc:  # noqa: BLE001 - handed to the consumer as a frame-level failure
                loop.call_soon_threadsafe(deliver, exc)
            finally:
                loop.call_soon_threadsafe(deliver, _DONE)

        worker = loop.run_in_executor(None, run)
        try:
            while True:
                item = await self._next(queue)
                if item is _DONE:
                    return
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            await self._teardown(worker)

    async def _next(self, queue: "asyncio.Queue[Any]") -> Any:
        """Wait for one item, noticing an external stop request when there is one.

        The same ``get()`` task is kept across polls: cancelling a fresh
        ``queue.get()`` every interval can drop an item that arrived while the
        timeout fired.
        """
        if self._should_stop is None:
            return await queue.get()
        getter = asyncio.ensure_future(queue.get())
        try:
            while True:
                done, _ = await asyncio.wait({getter}, timeout=self._poll_interval)
                if getter in done:
                    return getter.result()
                if self._should_stop():
                    self.stopped_early = True
                    getter.cancel()
                    with contextlib.suppress(BaseException):
                        await getter
                    return _DONE
        except BaseException:
            getter.cancel()
            raise

    async def _teardown(self, worker: "asyncio.Future[Any]") -> None:
        if self._settled:
            return
        self._settled = True
        if self._abort is not None:
            with contextlib.suppress(Exception):
                self._abort()
        # `shield` so the waiter's own cancellation cannot leave the worker
        # unobserved; the timeout is what keeps this bounded.
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(asyncio.shield(worker), timeout=self._settle_timeout)
        if not worker.done():
            self.timed_out = True
