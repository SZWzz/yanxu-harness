"""传输层：有界的 stdio，以及（MCP03 落的）Streamable HTTP。

**隔离要求**：这个模块在导入时不得拉入任何第三方包——`anyio` 也要在函数内导入。
基础安装遍历导入所有子模块时必须能成功，SDK 与其依赖只能在真正启用 MCP 时才进入
进程。``tests/test_import_fence.py`` 的冷启动扫描就是这条规则的牙齿。

stdio 这里**没有**用 SDK 的 ``stdio_client``：MCP00 的 ADR 记录过它的 stdout 读取
会把不完整行累加进一个无上限的字符串，服务端只要一直灌数据就能吃光内存。本模块提供
同一条公开契约（``TransportStreams``：读流收 ``SessionMessage | Exception``，写流收
``SessionMessage``）的实现，但每一帧在解码和向下游交付之前就按字节计数，超限即协议
错误并让连接失效——截断后继续用会让「跳过了一帧」的连接冒充正常连接。

进程生命周期同样有界：新会话/进程组启动，关闭时先关 stdin，``close_grace`` 内等它
自己退出，然后 SIGTERM 整个进程组，再等，最后 SIGKILL；stderr 由我们自己读，只保留
有界尾巴。
"""

from __future__ import annotations

import os
import signal
import sys
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from .config import DEFAULT_INHERITED_ENV_VARS, McpServerConfig, McpTransport
from .errors import McpConfigurationError, McpFrameTooLarge, McpProtocolError
from .sdk import McpSdk, load_sdk

__all__ = ["BoundedStderr", "bounded_stdio_transport", "effective_environment", "transport_for"]

_EXIT_POLL_INTERVAL = 0.01
_FORCE_KILL_GRACE = 2.0


@dataclass
class BoundedStderr:
    """The tail of a child's stderr, bounded while it is being read.

    Diagnostic-only: it never carries protocol data, and the bound is enforced as
    the bytes arrive, not by slicing a string afterwards.
    """

    limit: int
    tail: bytes = b""
    total_bytes: int = 0
    truncated: bool = False

    def feed(self, chunk: bytes) -> None:
        self.total_bytes += len(chunk)
        if len(chunk) >= self.limit:
            self.tail = chunk[-self.limit :]
            self.truncated = True
            return
        combined = self.tail + chunk
        if len(combined) > self.limit:
            self.truncated = True
            combined = combined[-self.limit :]
        self.tail = combined

    def text(self) -> str:
        return self.tail.decode("utf-8", "replace")


def effective_environment(config: McpServerConfig) -> dict[str, str]:
    """The child's environment: the standard whitelist plus the host's explicit entries.

    Nothing else is inherited; an inherited ``OPENAI_API_KEY`` from the parent
    process is exactly the kind of leak the whitelist exists to stop.
    """
    env: dict[str, str] = {}
    if config.inherit_default_environment:
        for name in DEFAULT_INHERITED_ENV_VARS:
            value = os.environ.get(name)
            if value is not None:
                env[name] = value
    env.update({str(key): str(value) for key, value in dict(config.env).items()})
    return env


def transport_for(
    config: McpServerConfig,
    *,
    stderr: "BoundedStderr | None" = None,
    on_fatal: Any = None,
) -> Any:
    """The transport context manager for one configuration."""
    if config.transport is McpTransport.STDIO:
        return bounded_stdio_transport(config, stderr=stderr, on_fatal=on_fatal)
    if config.transport is McpTransport.STREAMABLE_HTTP:
        from .http import bounded_http_transport

        return bounded_http_transport(config)
    raise McpConfigurationError(f"{config.server_id}: unknown transport {config.transport!r}")


@asynccontextmanager
async def bounded_stdio_transport(
    config: McpServerConfig,
    *,
    stderr: BoundedStderr | None = None,
    on_fatal: Any = None,
) -> AsyncIterator[tuple[Any, Any]]:
    """Run one stdio MCP server and yield its bounded protocol streams."""
    import anyio

    if config.transport is not McpTransport.STDIO:
        raise McpConfigurationError(f"{config.server_id}: bounded_stdio_transport needs a stdio config")
    sdk = load_sdk()
    limit = config.limits.max_message_bytes
    stderr = stderr if stderr is not None else BoundedStderr(limit=config.limits.max_stderr_bytes)

    try:
        process = await anyio.open_process(
            [str(config.command), *config.args],
            env=effective_environment(config),
            cwd=config.cwd,
            # stdin/stdout/stderr default to pipes in anyio; stderr is drained by
            # this module's own bounded reader below.
            start_new_session=True,
        )
    except OSError as exc:
        raise McpConfigurationError(f"{config.server_id}: cannot start {config.command!r}: {exc}") from exc

    read_writer, read_stream = anyio.create_memory_object_stream[Any](0)
    write_stream, write_reader = anyio.create_memory_object_stream[Any](0)
    writer_done = anyio.Event()
    shutting_down = False

    async def stdout_reader() -> None:
        buffer = bytearray()
        assert process.stdout is not None
        try:
            async with read_writer:
                async for chunk in process.stdout:
                    start = 0
                    while True:
                        index = chunk.find(b"\n", start)
                        if index < 0:
                            buffer += chunk[start:]
                            if len(buffer) > limit:
                                await _fatal(on_fatal, _oversize(config, len(buffer), limit))
                                return
                            break
                        buffer += chunk[start:index]
                        if len(buffer) > limit:
                            await _fatal(on_fatal, _oversize(config, len(buffer), limit))
                            return
                        line = bytes(buffer)
                        buffer.clear()
                        message = _parse_line(sdk, line)
                        if isinstance(message, Exception):
                            # A peer that emits non-protocol bytes is not trustworthy for
                            # the rest of the connection either: stop instead of skipping
                            # frames and continuing as if we had not seen anything.
                            await _fatal(on_fatal, message)
                            return
                        try:
                            await read_writer.send(message)
                        except (anyio.ClosedResourceError, anyio.BrokenResourceError):
                            return
                        start = index + 1
                # anyio turns a byte-stream EOF into StopAsyncIteration, so reaching
                # here means the server's stdout ended (every fatal path returned above).
                if not shutting_down:
                    await _fatal(
                        on_fatal,
                        McpProtocolError(f"{config.server_id}: the server's stdout ended"),
                    )
        except (anyio.ClosedResourceError, anyio.BrokenResourceError):
            pass
        except (anyio.EndOfStream, ConnectionError, OSError):
            if not shutting_down:
                await _fatal(
                    on_fatal,
                    McpProtocolError(f"{config.server_id}: the server's stdout ended"),
                )

    async def stdin_writer() -> None:
        assert process.stdin is not None
        try:
            async with write_reader:
                async for session_message in write_reader:
                    payload = session_message.message.model_dump_json(
                        by_alias=True, exclude_unset=True
                    ).encode("utf-8")
                    if len(payload) > limit:
                        # Refused *before* it reaches the wire: the one provable
                        # "was not sent".
                        await _fatal(
                            on_fatal,
                            McpFrameTooLarge(
                                f"{config.server_id}: an outbound frame of {len(payload)} bytes "
                                f"exceeds the {limit}-byte limit and was not sent"
                            ),
                        )
                        return
                    await process.stdin.send(payload + b"\n")
        except (anyio.ClosedResourceError, anyio.BrokenResourceError, OSError):
            # The server may still be alive; end the read side so the session sees
            # the connection go away instead of waiting forever.
            await read_writer.aclose()
        finally:
            writer_done.set()

    async def stderr_reader() -> None:
        assert process.stderr is not None
        try:
            async for chunk in process.stderr:
                stderr.feed(bytes(chunk))
        except (anyio.ClosedResourceError, anyio.BrokenResourceError, anyio.EndOfStream, OSError):
            pass

    async with anyio.create_task_group() as tg:
        tg.start_soon(stdout_reader)
        tg.start_soon(stdin_writer)
        tg.start_soon(stderr_reader)
        try:
            yield read_stream, write_stream
        finally:
            shutting_down = True
            with anyio.CancelScope(shield=True):
                await _shutdown(config, process, read_stream, write_stream, write_reader, writer_done)
            tg.cancel_scope.cancel()


async def _report(writer: Any, error: BaseException) -> None:
    """Hand one exception to the session through the read stream.

    Only used for non-fatal observations. The SDK's dispatcher *logs* stream
exceptions by default rather than failing a request with them, so a fatal
    transport error must not rely on this path (see :func:`_fatal`).
    """
    import anyio

    with suppress(anyio.ClosedResourceError, anyio.BrokenResourceError):
        await writer.send(error)


async def _fatal(on_fatal: Any, error: BaseException) -> None:
    """Report a fatal protocol violation; the caller then stops reading.

    Stopping the reader closes the read stream, which makes the session wake every
    pending request with its own connection-closed error. `on_fatal` records *why*
    the connection died, so the manager can report the real cause (an oversize frame,
    an unparseable line, a dead server) instead of a generic closed connection.
    """
    if on_fatal is not None:
        try:
            on_fatal(error)
        except Exception:  # noqa: BLE001 - a broken callback must not mask the protocol error
            pass


def _oversize(config: McpServerConfig, observed: int, limit: int) -> McpFrameTooLarge:
    return McpFrameTooLarge(
        f"{config.server_id}: an inbound frame exceeded {limit} bytes (observed >= {observed}); "
        "the connection is not usable afterwards"
    )


def _parse_line(sdk: McpSdk, line: bytes) -> Any:
    try:
        message = sdk.jsonrpc_adapter.validate_json(line, by_name=False)
    except ValueError as exc:
        return McpProtocolError(f"unparseable protocol line: {exc}")
    return sdk.session_message(message)


async def _shutdown(
    config: McpServerConfig,
    process: Any,
    read_stream: Any,
    write_stream: Any,
    write_reader: Any,
    writer_done: Any,
) -> None:
    """Bounded teardown: flush, stop, kill, release. Every wait has a ceiling."""
    import anyio

    grace = config.limits.close_grace
    with suppress(anyio.ClosedResourceError, anyio.BrokenResourceError):
        await read_stream.aclose()
    with suppress(anyio.ClosedResourceError, anyio.BrokenResourceError):
        await write_stream.aclose()
    with anyio.move_on_after(grace):
        await writer_done.wait()

    if process.stdin is not None:
        with suppress(OSError, anyio.ClosedResourceError, anyio.BrokenResourceError):
            await process.stdin.aclose()

    if not await _wait_for_exit(process, grace):
        await _kill_process_tree(process)
        if not await _wait_for_exit(process, _FORCE_KILL_GRACE):
            raise McpProtocolError(
                f"{config.server_id}: the server process {process.pid} did not exit after SIGKILL; "
                "it is abandoned, not claimed to be stopped"
            )

    for stream in (process.stdout, write_reader, read_stream, write_stream):
        if stream is None:
            continue
        with suppress(OSError, anyio.ClosedResourceError, anyio.BrokenResourceError):
            await stream.aclose()


async def _wait_for_exit(process: Any, timeout: float) -> bool:
    import anyio

    deadline = anyio.current_time() + timeout
    while process.returncode is None:
        if anyio.current_time() >= deadline:
            return False
        await anyio.sleep(_EXIT_POLL_INTERVAL)
    return True


async def _kill_process_tree(process: Any) -> None:
    """Kill the process group this adapter started, not just the leader."""
    import anyio

    if sys.platform == "win32":  # pragma: no cover - POSIX is what this round runs
        with suppress(ProcessLookupError, OSError):
            process.kill()
        return
    with suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    with anyio.move_on_after(_FORCE_KILL_GRACE):
        while process.returncode is None:
            await anyio.sleep(_EXIT_POLL_INTERVAL)
    if process.returncode is None:
        with suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)


@dataclass
class TransportFacts:
    """What a transport observed, for diagnostics that never expose wire bytes."""

    server_id: str
    pid: int | None = None
    stderr: BoundedStderr | None = field(default=None)
