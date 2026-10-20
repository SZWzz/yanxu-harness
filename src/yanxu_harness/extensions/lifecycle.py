"""Lifecycle primitives: invalidation, owner checks and bounded close.

The deployment state machine lives in :mod:`yanxu_harness.extensions.assembly`
because it is inseparable from publishing. This module holds the two pieces that
must be usable from the tool/skill wrappers without importing the assembly:

* :class:`InvalidationToken` — a synchronous, shared revocation flag. It is
  flipped *before* any ``await`` during a close, so a new tool call or skill read
  cannot win a race against admission.
* :class:`OwnerGuard` — records the task and loop that opened the resources.
  Closing from another task/loop is refused explicitly instead of exiting an
  anyio task group in the wrong task.
"""

from __future__ import annotations

import asyncio
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Sequence

from ..errors import HarnessError
from .errors import ExtensionOwnerMismatch
from .types import CloseReport, ExtensionState

__all__ = ["InvalidationToken", "OwnerGuard", "close_in_reverse"]

_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")
_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_TYPE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,79}$")


def _failure_code(exc: BaseException) -> str:
    """A bounded, stable failure code; never the exception's message."""
    if isinstance(exc, HarnessError):
        code = getattr(exc, "code", "")
        if isinstance(code, str) and _CODE_RE.fullmatch(code):
            return code
    name = type(exc).__name__
    return name if _TYPE_RE.fullmatch(name) else "Error"


def _failure_entry(name: str, exc: BaseException) -> str:
    safe_name = name if _NAME_RE.fullmatch(name) else "extension"
    return f"{safe_name}: {_failure_code(exc)}"


class InvalidationToken:
    """A one-way revocation flag shared by the deployment and every wrapper."""

    __slots__ = ("_lock", "_reason")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reason = ""

    @property
    def valid(self) -> bool:
        return not self._reason

    @property
    def reason(self) -> str:
        return self._reason

    def invalidate(self, reason: str) -> bool:
        """Revoke once. Returns ``True`` when this call changed the state."""
        with self._lock:
            if self._reason:
                return False
            self._reason = reason or "the deployment is no longer valid"
            return True


@dataclass
class OwnerGuard:
    """The task/loop that owns a deployment's async resources."""

    loop: asyncio.AbstractEventLoop | None = None
    task: "asyncio.Task[Any] | None" = None
    bound: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def bind(self) -> None:
        with self._lock:
            self.loop = asyncio.get_running_loop()
            self.task = asyncio.current_task()
            self.bound = True

    def check(self, what: str) -> None:
        if not self.bound:
            raise ExtensionOwnerMismatch(
                f"{what}: this deployment has no owner task; open it before using it"
            )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise ExtensionOwnerMismatch(
                f"{what}: no running event loop; the deployment is owned by another loop"
            ) from exc
        if loop is not self.loop:
            raise ExtensionOwnerMismatch(
                f"{what}: the deployment was opened on a different event loop; "
                "close it from its owner loop"
            )
        current = asyncio.current_task()
        if current is not self.task:
            raise ExtensionOwnerMismatch(
                f"{what}: the deployment is owned by another task; close it from the "
                "task that opened it"
            )


async def close_in_reverse(
    items: Sequence[tuple[str, Callable[[], Awaitable[Any]]]],
    *,
    timeout_seconds: float,
) -> CloseReport:
    """Close ``items`` in reverse order, bounded and independently.

    One failing close never prevents the rest from being attempted. A timeout is
    reported as *unconfirmed* rather than failed: the factory may still hold the
    resource, and "I could not prove it was released" is a different fact from
    "the close raised".
    """
    released: list[str] = []
    failed: list[str] = []
    unconfirmed: list[str] = []
    for name, close in reversed(list(items)):
        try:
            await asyncio.wait_for(close(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            unconfirmed.append(name)
        except asyncio.CancelledError:
            unconfirmed.append(name)
            raise
        except Exception as exc:  # noqa: BLE001 - one close must not hide the others
            # The public report may name the extension and the failure class/code,
            # but must not copy arbitrary exception text: factory messages can carry
            # private configuration or credential material.
            failed.append(_failure_entry(name, exc))
        else:
            released.append(name)
    return CloseReport(
        released=tuple(released),
        failed=tuple(failed),
        unconfirmed=tuple(unconfirmed),
        state=ExtensionState.CLOSED,
    )
