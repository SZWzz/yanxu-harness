"""失败词汇：适配器对宿主说的话，以及每句话能证明多少。

这里刻意区分三件事，因为 MCP05 的 receipt 必须凭它们归类：

* **配置/协议错误**：还没发生远端调用，或者远端破坏了协议；
* **可证明没派发**（:class:`McpNotSent`）：请求在写出之前就被拒绝，远端不可能执行过；
* **可能已到达远端**（``may_have_reached_server=True``）：超时、断流、无法解析——
  它**只有**可能已经执行，绝不能被说成「没执行」而自动重放。

没有任何一条错误把未知说成失败，也没有任何一条把失败说成未知。
"""

from __future__ import annotations

__all__ = [
    "McpCallFailure",
    "McpClosed",
    "McpConfigurationError",
    "McpConnectError",
    "McpConnectionLost",
    "McpDriftRefusal",
    "McpError",
    "McpExtraMissing",
    "McpFrameTooLarge",
    "McpInvalidArguments",
    "McpNotConnected",
    "McpNotSent",
    "McpProtocolError",
    "McpSchemaError",
    "McpToolError",
]


class McpError(RuntimeError):
    """This adapter's base error. Nothing here is a plain ``RuntimeError`` by accident."""


class McpExtraMissing(McpError):
    """The optional ``mcp`` extra is not installed.

    Raised by the lazy SDK boundary, never at import time: a base install must be
    able to import the package and only see this when it actually enables MCP.
    """


class McpConfigurationError(McpError):
    """The host's configuration cannot be used. Refused before anything is spawned."""


class McpProtocolError(McpError):
    """The peer violated the protocol, or negotiated a revision we did not pin."""


class McpFrameTooLarge(McpProtocolError):
    """One frame exceeded the configured byte limit.

    The connection is unusable afterwards: a transport that skipped a frame can no
    longer claim to be speaking the protocol. Nothing may be silently truncated.
    """


class McpNotConnected(McpError):
    """There is no live connection for this scope (nothing was sent)."""


class McpConnectError(McpError):
    """No usable connection could be established (handshake, launch or timeout).

    Distinct from a *call* failure on purpose: connect happens before any tool call
    is admitted, so nothing was dispatched for the run.
    """


class McpNotSent(McpError):
    """The request provably never reached the server.

    Only raised where that is provable: for example a request this adapter refused
    to serialize because it exceeded the frame limit, or a call on a connection
    that was already known closed before dispatch.
    """


class McpConnectionLost(McpError):
    """The connection failed while the call was in flight.

    ``may_have_reached_server`` says whether the request had been written to the
    transport when the failure was observed. ``True`` means the effect is unknown,
    not absent.
    """


class McpCallFailure(McpError):
    """A call did not produce a usable answer, with what can be proven about dispatch."""

    def __init__(self, message: str, *, may_have_reached_server: bool) -> None:
        super().__init__(message)
        self.may_have_reached_server = bool(may_have_reached_server)


class McpClosed(McpError):
    """The manager was closed; no further call may be dispatched."""


class McpToolError(McpError):
    """The server reported a *tool-level* error (a complete ``isError`` answer).

    This is a result, not a transport failure: the call reached the server, the
    server answered, and the answer says the tool failed. It is kept as an
    exception-free path by the handler and only exists for callers that ask the
    manager for a failure to be raised.
    """

    def __init__(self, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail


class McpSchemaError(McpConfigurationError):
    """A remote tool's JSON Schema cannot be used as declared.

    Raised at registration, never at call time: a schema we cannot compile is a tool we
    do not expose. Unknown dialects, remote ``$ref`` values, unusable structures and
    over-complex documents all land here.
    """


class McpInvalidArguments(McpError):
    """The call's arguments do not satisfy the tool's declared schema.

    Nothing was sent: this is decided before dispatch, and the remote call count stays
    zero.
    """


class McpDriftRefusal(McpError):
    """The frozen tool binding and the current catalog no longer agree.

    Raised before any dispatch when a recovered run finds the tool missing, changed,
    revoked or moved to another protocol. The message names the specific difference;
    the caller must not silently re-freeze the binding and continue.
    """
