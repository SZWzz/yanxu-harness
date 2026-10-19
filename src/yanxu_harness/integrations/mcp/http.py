"""Streamable HTTP 传输：有界响应体、宿主预置凭据、同 origin 重定向。

用的是 SDK 的公开 ``streamable_http_client``（JSON-RPC、SSE、会话与重连都复用它的
实现），外面套两件自己的东西：

* **有界响应体**：SDK 对非 SSE 响应是一次 ``aread()`` 全量读入；这里在自定义
  ``httpx2`` transport 上按字节计数，``Content-Length`` 先查，流式读取时再逐个
  chunk 累计，超限即报错并终止该响应。SSE 的单事件上限由 SDK 的
  ``max_sse_event_size`` 负责（我们把它设为配置里的消息上限）。
* **凭据边界**：header 由宿主显式给出并交给 httpx2 client；适配器不写日志、不进
  持久记录，错误里也不回显 header 值。

HTTP 错误（401/403/404/5xx）不会触发 SDK 重发 POST；attempt 计数在 MCP03 的测试里
由客户端侧请求钩子与服务端 facts 两侧核对。
"""

from __future__ import annotations

import importlib
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from .config import McpServerConfig
from .errors import McpConfigurationError, McpFrameTooLarge

__all__ = ["bounded_http_transport"]

# One long stream read may legitimately take a while (SSE), so the read timeout is
# not the call deadline; the deadline is enforced by the manager around the call.
_HTTP_CONNECT_TIMEOUT = 10.0
_HTTP_READ_TIMEOUT = 120.0


def _streamable_http_module() -> Any:
    try:
        return importlib.import_module("mcp.client.streamable_http")
    except ModuleNotFoundError as exc:  # pragma: no cover - covered by the extra-missing path
        from .errors import McpExtraMissing

        raise McpExtraMissing("the MCP client adapter needs the 'mcp' extra") from exc


class _BoundedByteStream:
    """Wrap one response body so its total size is bounded while it is read."""

    def __init__(self, inner: Any, *, limit: int, server_id: str) -> None:
        self._inner = inner
        self._limit = limit
        self._server_id = server_id
        self._seen = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._inner:
            self._seen += len(chunk)
            if self._seen > self._limit:
                raise McpFrameTooLarge(
                    f"{self._server_id}: an HTTP response body exceeded {self._limit} bytes; "
                    "the connection is not usable afterwards"
                )
            yield chunk

    async def aclose(self) -> None:
        closer = getattr(self._inner, "aclose", None)
        if closer is not None:
            await closer()


def bounded_http_client(config: McpServerConfig) -> Any:
    """An httpx2 client that pins our own timeouts and bounds every response body."""
    import httpx2

    limit = config.limits.max_message_bytes
    server_id = config.server_id

    class _BoundedTransport(httpx2.AsyncBaseTransport):
        def __init__(self) -> None:
            self._inner = httpx2.AsyncHTTPTransport(retries=0)

        async def handle_async_request(self, request: "httpx2.Request") -> "httpx2.Response":
            response = await self._inner.handle_async_request(request)
            declared = response.headers.get("content-length")
            if declared is not None:
                try:
                    if int(declared) > limit:
                        raise McpFrameTooLarge(
                            f"{server_id}: the server declared a {declared}-byte response body, "
                            f"above the {limit}-byte limit"
                        )
                except ValueError:
                    pass
            response.stream = _BoundedByteStream(response.stream, limit=limit, server_id=server_id)
            return response

        async def aclose(self) -> None:
            await self._inner.aclose()

    headers = {str(name): str(value) for name, value in dict(config.headers).items()}
    return httpx2.AsyncClient(
        headers=headers,
        timeout=httpx2.Timeout(_HTTP_READ_TIMEOUT, connect=_HTTP_CONNECT_TIMEOUT, pool=_HTTP_CONNECT_TIMEOUT),
        follow_redirects=False,
        transport=_BoundedTransport(),
        trust_env=False,
    )


@asynccontextmanager
async def bounded_http_transport(config: McpServerConfig) -> AsyncIterator[tuple[Any, Any]]:
    """Yield the SDK's transport streams for one Streamable HTTP endpoint."""
    if config.url is None:  # pragma: no cover - config validation refuses this earlier
        raise McpConfigurationError(f"{config.server_id}: a Streamable HTTP server needs a url")
    module = _streamable_http_module()
    client = bounded_http_client(config)
    try:
        async with module.streamable_http_client(
            config.url,
            http_client=client,
            terminate_on_close=True,
            max_sse_event_size=config.limits.max_message_bytes,
        ) as streams:
            yield streams
    finally:
        await client.aclose()
