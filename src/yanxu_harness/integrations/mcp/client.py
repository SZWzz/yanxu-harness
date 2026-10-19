"""连接管理器：连接、握手、发现、单次调用、失效与关闭。

它是适配器唯一的生命周期所有者，也是唯一能起进程的地方。三条不变量：

1. **连接隔离**：连接按 ``(server_id, 授权 scope, 配置 revision, 协议, 传输)`` 隔离。
   两个 scope 不会共用一条连接，因此不会互相借用凭据或 pin；child 只能拿到宿主
   显式传入的 config。
2. **一次调用一条请求**：调用走公开低层 ``ClientSession.call_tool``。高层
   ``Client.call_tool`` 在 HEADER_MISMATCH 后会重发同一个业务调用（MCP00 ADR），
   本管理器不使用它，也不做任何 SDK 私有修补。
3. **取消与关闭有界**：每个调用有自己的期限（并发槽等待计入其中），关闭先关 stdin、
   再等 ``close_grace``、再 SIGTERM 进程组、最后 SIGKILL；证明不了远端已停就如实记录，
   不假装已停。

连接对象的生命周期由本管理器的 task group 持有，所以 ``connect``/``close`` 可以在
同一个事件循环的任意任务里调用；关一条连接不会波及其他仍有效的调用。

**导入规则**：本模块导入时不得拉入任何第三方包（含 ``anyio``），因为基础安装会
遍历导入所有子模块。第三方 import 一律在函数/方法内部完成。
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Mapping

from .config import McpLimits, McpServerConfig, McpTransport
from .errors import (
    McpCallFailure,
    McpClosed,
    McpConfigurationError,
    McpConnectError,
    McpError,
    McpFrameTooLarge,
    McpNotConnected,
    McpNotSent,
    McpProtocolError,
)
from .sdk import McpSdk, load_sdk
from .transports import BoundedStderr, transport_for
from .types import McpCallResult, McpConnection, McpToolDescriptor, McpToolDirectory

__all__ = ["McpClientManager"]

#: Slack over the supervisor's own handshake timeout, so the caller reports the real
#: cause instead of racing it.
_READY_SLACK = 1.0

#: Hard ceiling on content blocks this release will surface from one answer.
MAX_CONTENT_BLOCKS = 256


class _Live:
    """One connection's live state, owned by the manager's task group."""

    def __init__(self, config: McpServerConfig, connection: McpConnection) -> None:
        import anyio

        self.config = config
        self.connection = connection
        self.ready = anyio.Event()
        self.stop = anyio.Event()
        self.done = anyio.Event()
        self.session: Any = None
        self.failure: BaseException | None = None
        self.stderr = BoundedStderr(limit=config.limits.max_stderr_bytes)
        self.opened_at = 0.0
        self.closed = False

    def live_session(self) -> Any:
        if self.session is None or self.failure is not None:
            raise McpNotConnected(
                f"{self.config.server_id}: no live connection "
                f"({type(self.failure).__name__ if self.failure else 'never established'})"
            )
        return self.session


class McpClientManager:
    """Async owner of every MCP connection this adapter opens."""

    def __init__(self, *, limits: McpLimits | None = None, clock: Any = time.monotonic) -> None:
        self._limits = limits
        self._clock = clock
        self._task_group: Any = None
        self._connections: dict[tuple[str, ...], _Live] = {}
        self._server_slots: dict[str, Any] = {}
        self._scope_slots: dict[str, Any] = {}
        self._entered = False

    # -- lifecycle ------------------------------------------------------

    async def __aenter__(self) -> "McpClientManager":
        import anyio

        if self._entered:
            raise McpConfigurationError("this manager is already open; it is not reentrant")
        self._entered = True
        self._task_group = anyio.create_task_group()
        await self._task_group.__aenter__()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()
        if self._task_group is not None:
            group = self._task_group
            self._task_group = None
            group.cancel_scope.cancel()
            await group.__aexit__(*exc)
        self._entered = False

    async def close(self) -> None:
        """Ask every connection to stop, bounded; never hangs on a stuck server."""
        import anyio

        for live in list(self._connections.values()):
            live.stop.set()
        for live in list(self._connections.values()):
            grace = self._limit_for(live.config).close_grace
            with anyio.move_on_after(grace):
                await live.done.wait()
        stuck = [live for live in self._connections.values() if not live.done.is_set()]
        for live in stuck:
            # Honest outcome: the group is cancelled next and these servers are
            # reported as not-provably-stopped rather than silently dropped.
            live.failure = live.failure or McpProtocolError(
                f"{live.config.server_id}: close did not complete within the grace period"
            )
        self._connections.clear()

    def _require_open(self) -> None:
        if not self._entered or self._task_group is None:
            raise McpClosed("the manager is not open; use `async with` or `await __aenter__()`")

    def _limit_for(self, config: McpServerConfig) -> McpLimits:
        return config.limits if self._limits is None else self._limits

    # -- connections ----------------------------------------------------

    async def connect(self, config: McpServerConfig) -> McpConnection:
        """Establish (or reuse) the connection for one configuration."""
        import anyio

        self._require_open()
        if not isinstance(config, McpServerConfig):
            raise McpConfigurationError(f"connect expects an McpServerConfig, got {type(config).__name__}")
        key = config.connection_scope()
        existing = self._connections.get(key)
        if existing is not None and existing.failure is None and not existing.closed:
            return existing.connection

        sdk = load_sdk()
        generation = 1 + max(
            (
                live.connection.generation
                for live in self._connections.values()
                if live.connection.scope_key == key
            ),
            default=0,
        )
        connection = McpConnection(
            server_id=config.server_id,
            transport=config.transport,
            protocol=config.protocol,
            authorization_scope=config.authorization_scope,
            config_revision=config.config_revision,
            generation=generation,
            connection_id=uuid.uuid4().hex,
        )
        live = _Live(config, connection)
        self._connections[key] = live
        self._task_group.start_soon(self._supervise, live, sdk)

        limits = self._limit_for(config)
        with anyio.move_on_after(limits.connect_timeout + _READY_SLACK) as scope:
            await live.ready.wait()
        if scope.cancelled_caught:
            await self._stop(live)
            raise McpConnectError(
                f"{config.server_id}: connecting took longer than {limits.connect_timeout}s"
            )
        if live.failure is not None:
            failure = _root_error(live.failure)
            self._connections.pop(key, None)
            await self._stop(live)
            if isinstance(failure, McpError):
                raise failure
            raise McpConnectError(f"{config.server_id}: {type(failure).__name__}: {failure}") from failure
        return connection

    async def _supervise(self, live: _Live, sdk: McpSdk) -> None:
        """Own one connection's context managers for its whole life."""
        import anyio

        config = live.config
        try:
            async with transport_for(
                config, stderr=live.stderr, on_fatal=lambda error: self._on_transport_fatal(live, error)
            ) as (read_stream, write_stream):
                async with sdk.session_class(read_stream, write_stream) as session:
                    with anyio.fail_after(self._limit_for(config).connect_timeout):
                        await self._handshake(session, config)
                    live.session = session
                    live.opened_at = self._clock()
                    live.ready.set()
                    await live.stop.wait()
        except anyio.get_cancelled_exc_class():
            raise
        except BaseException as exc:  # noqa: BLE001 - a broken server must not kill the manager
            live.failure = live.failure or exc
            live.session = None
            live.ready.set()
        finally:
            live.closed = True
            live.done.set()

    def _on_transport_fatal(self, live: _Live, error: BaseException) -> None:
        """A transport violation makes the connection unusable, immediately and visibly."""
        live.failure = live.failure or error
        live.stop.set()

    async def _handshake(self, session: Any, config: McpServerConfig) -> None:
        """Verify the negotiated revision instead of trusting the configured string."""
        if config.protocol == "2026-07-28":
            await session.discover()
        else:
            await session.initialize()
        negotiated = getattr(session, "protocol_version", None)
        if negotiated != config.protocol:
            raise McpProtocolError(
                f"{config.server_id}: the server negotiated {negotiated!r}, "
                f"but this configuration pins {config.protocol!r}"
            )

    async def _stop(self, live: _Live) -> None:
        import anyio

        live.stop.set()
        with anyio.move_on_after(self._limit_for(live.config).close_grace):
            await live.done.wait()

    async def close_connection(self, connection: McpConnection) -> None:
        """Close exactly one connection; other in-flight calls keep working."""
        self._require_open()
        live = self._connections.get(connection.scope_key)
        if live is None or live.connection.connection_id != connection.connection_id:
            return
        await self._stop(live)
        self._connections.pop(connection.scope_key, None)

    def connection_state(self, connection: McpConnection) -> str:
        """``"live"``, ``"failed"`` or ``"absent"`` — for diagnostics and tests."""
        live = self._connections.get(connection.scope_key)
        if live is None or live.connection.connection_id != connection.connection_id:
            return "absent"
        if live.failure is not None:
            return "failed"
        return "live" if live.session is not None else "absent"

    def stderr_tail(self, connection: McpConnection) -> str:
        live = self._connections.get(connection.scope_key)
        if live is None or live.stderr is None:
            return ""
        return live.stderr.text()

    def _live(self, connection: McpConnection) -> _Live:
        live = self._connections.get(connection.scope_key)
        if live is None or live.connection.connection_id != connection.connection_id:
            raise McpNotConnected(
                f"{connection.server_id}: this connection handle is no longer current"
            )
        return live

    # -- discovery ------------------------------------------------------

    async def list_tools(
        self, connection: McpConnection, *, timeout_seconds: float | None = None
    ) -> McpToolDirectory:
        """Read the tool directory, bounded by pages, tool count and total bytes.

        Returns what was read *and* whether the listing was complete: an incomplete
        directory must never be presented as a complete one, and a cursor cycle is a
        refusal rather than a loop.
        """
        import anyio

        self._require_open()
        live = self._live(connection)
        session = live.live_session()
        config = live.config
        sdk = load_sdk()
        limits = self._limit_for(config)
        budget = self._budget(limits.discovery_timeout, timeout_seconds)

        tools: list[McpToolDescriptor] = []
        seen_cursors: set[str] = set()
        cursor: str | None = None
        total_bytes = 0
        pages_read = 0
        try:
            with anyio.fail_after(budget):
                for _ in range(limits.max_pages):
                    params = (
                        None
                        if cursor is None
                        else sdk.mcp_types.PaginatedRequestParams(cursor=cursor)
                    )
                    result = await session.list_tools(params=params)
                    pages_read += 1
                    for tool in result.tools:
                        descriptor = self._descriptor(tool)
                        total_bytes += len(str(descriptor.input_schema)) + len(str(descriptor.output_schema))
                        if total_bytes > limits.max_catalog_bytes:
                            raise McpProtocolError(
                                f"{config.server_id}: the tool directory exceeded "
                                f"{limits.max_catalog_bytes} bytes"
                            )
                        tools.append(descriptor)
                        if len(tools) > limits.max_tools:
                            raise McpProtocolError(
                                f"{config.server_id}: the server offered more than {limits.max_tools} tools"
                            )
                    cursor = getattr(result, "next_cursor", None)
                    if not cursor:
                        return McpToolDirectory(tuple(tools), pages_read=pages_read, complete=True)
                    if cursor in seen_cursors:
                        raise McpProtocolError(
                            f"{config.server_id}: the tool directory repeated a pagination cursor"
                        )
                    seen_cursors.add(cursor)
            raise McpProtocolError(
                f"{config.server_id}: the tool directory did not finish within {limits.max_pages} pages"
            )
        except TimeoutError as exc:
            raise McpProtocolError(
                f"{config.server_id}: discovery exceeded its {budget:g}s budget without an answer"
            ) from exc
        except BaseException as exc:  # noqa: BLE001 - the session raises several shapes
            if isinstance(exc, anyio.get_cancelled_exc_class()):
                raise
            recorded = _root_error(live.failure) if live.failure is not None else _root_error(exc)
            if isinstance(recorded, McpError):
                raise recorded from exc
            raise McpProtocolError(
                f"{config.server_id}: discovery failed ({type(exc).__name__}: {exc})"
            ) from exc

    @staticmethod
    def _descriptor(tool: Any) -> McpToolDescriptor:
        annotations = getattr(tool, "annotations", None)
        return McpToolDescriptor(
            name=str(tool.name),
            description=str(getattr(tool, "description", "") or ""),
            title=getattr(tool, "title", None),
            input_schema=dict(getattr(tool, "input_schema", None) or {}),
            output_schema=(
                dict(tool.output_schema) if getattr(tool, "output_schema", None) is not None else None
            ),
            annotations=(
                annotations.model_dump(by_alias=True, exclude_none=True) if annotations is not None else {}
            ),
        )

    # -- calls ----------------------------------------------------------

    async def call_tool(
        self,
        connection: McpConnection,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> McpCallResult:
        """One call, one request, under one deadline that includes slot waiting."""
        import anyio

        self._require_open()
        live = self._live(connection)
        session = live.live_session()
        config = live.config
        limits = self._limit_for(config)
        budget = self._budget(limits.call_timeout, timeout_seconds)
        server_slot = self._server_slots.setdefault(
            config.server_id, anyio.Semaphore(limits.max_concurrent_calls)
        )
        scope_slot = self._scope_slots.setdefault(
            config.authorization_scope, anyio.Semaphore(limits.max_scope_calls)
        )

        dispatched = False
        try:
            with anyio.fail_after(budget):
                async with server_slot, scope_slot:
                    if live.failure is not None:
                        raise McpNotConnected(
                            f"{config.server_id}: the connection failed before dispatch "
                            f"({type(live.failure).__name__})"
                        )
                    # From here on the request may reach the server; nothing below may
                    # be reported as "not sent".
                    dispatched = True
                    result = await session.call_tool(
                        name,
                        dict(arguments or {}),
                        read_timeout_seconds=budget,
                    )
        except TimeoutError as exc:
            raise McpCallFailure(
                f"{config.server_id}: {name} exceeded its {budget:g}s budget",
                may_have_reached_server=dispatched,
            ) from exc
        except BaseException as exc:  # noqa: BLE001 - the SDK raises several shapes
            if isinstance(exc, anyio.get_cancelled_exc_class()):
                raise
            raise self._classify_call_exception(config, name, exc, dispatched=dispatched, live=live) from exc
        return _call_result(name, result)

    def _budget(self, configured: float, requested: float | None) -> float:
        if requested is None:
            return configured
        if not isinstance(requested, (int, float)) or isinstance(requested, bool) or requested <= 0:
            raise McpConfigurationError(f"timeout_seconds must be positive, got {requested!r}")
        return min(configured, float(requested))

    def _classify_call_exception(
        self,
        config: McpServerConfig,
        name: str,
        exc: BaseException,
        *,
        dispatched: bool,
        live: _Live,
    ) -> McpError:
        """Turn any in-flight failure into one call failure with a provable dispatch flag.

        ``may_have_reached_server=False`` means the adapter can *prove* the request never
        left: it was refused while waiting for a slot, or the connection was already
        known dead. Everything else is ``True``; a failure this adapter cannot explain
        must never be reported as "nothing happened".

        When the transport already recorded *why* the connection died (an oversize
        frame, an unparseable line, a server that went away), that reason is preferred:
        the session's own "connection closed" error says what happened, not why.
        """
        recorded = _root_error(live.failure) if live.failure is not None else None
        for candidate in (recorded, _root_error(exc)):
            if isinstance(candidate, McpFrameTooLarge):
                return candidate
        if isinstance(recorded, McpNotSent):
            return recorded
        if isinstance(exc, McpNotSent):
            return exc
        if isinstance(exc, McpNotConnected):
            return exc
        if isinstance(exc, McpCallFailure):
            return exc
        if isinstance(recorded, McpProtocolError):
            return McpCallFailure(str(recorded), may_have_reached_server=dispatched)
        if isinstance(_root_error(exc), McpProtocolError):
            return McpCallFailure(str(_root_error(exc)), may_have_reached_server=dispatched)
        kind = type(exc).__name__
        if kind in {"InputRequiredRoundsExceededError", "UnexpectedClaimedResult"} or "input_required" in str(exc):
            return McpCallFailure(
                f"{config.server_id}: {name} asked for another interaction round; unsupported in this release",
                may_have_reached_server=True,
            )
        return McpCallFailure(
            f"{config.server_id}: {name} failed in the client ({kind}: {exc})",
            may_have_reached_server=dispatched,
        )


def _root_error(error: BaseException) -> BaseException:
    """Find the adapter's own error inside anyio's exception groups and chains.

    A failure raised inside a transport task arrives wrapped in one or more
    ``ExceptionGroup``s; reporting the group as the cause would hide the message that
    says what actually happened.
    """
    seen: set[int] = set()

    def walk(candidate: BaseException) -> BaseException:
        if id(candidate) in seen:
            return candidate
        seen.add(id(candidate))
        if isinstance(candidate, McpError):
            return candidate
        # Duck-typed on purpose: Python 3.10 has no builtin exception group, and the
        # one anyio raises there comes from the `exceptiongroup` backport. Asking for
        # the attribute keeps this file importable on 3.10 without naming a 3.11 class.
        members = getattr(candidate, "exceptions", None)
        if isinstance(members, (tuple, list)):
            for item in members:
                if isinstance(item, BaseException):
                    found = walk(item)
                    if isinstance(found, McpError):
                        return found
        for nested in (candidate.__cause__, candidate.__context__):
            if nested is not None:
                found = walk(nested)
                if isinstance(found, McpError):
                    return found
        return candidate

    return walk(error)


def _call_result(name: str, result: Any) -> McpCallResult:
    """Reduce one SDK result to the neutral shape, bounded and without vendor types."""
    texts: list[str] = []
    unsupported = False
    content = list(getattr(result, "content", None) or [])
    if len(content) > MAX_CONTENT_BLOCKS:
        content = content[:MAX_CONTENT_BLOCKS]
        unsupported = True
    for block in content:
        if getattr(block, "type", None) == "text":
            texts.append(str(getattr(block, "text", "")))
        else:
            unsupported = True
    return McpCallResult(
        name=name,
        text=tuple(texts),
        structured=getattr(result, "structured_content", None),
        is_error=bool(getattr(result, "is_error", False)),
        unsupported_shape=unsupported,
        requests_more=type(result).__name__ != "CallToolResult",
    )
