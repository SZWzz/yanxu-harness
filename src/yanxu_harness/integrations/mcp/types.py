"""适配器与宿主之间的中立数据：连接身份、目录描述、调用结果。

这些类型全部是**纯数据**且只装 JSON 能装的东西：SDK 的 Pydantic 模型、socket、
session 对象一律不进这里，也不进持久记录。远端工具名保留原始大小写：它是远端事实，
不是宿主身份（本地别名在 MCP04 生成，绝不反过来当作远端名字）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .config import McpTransport
from .errors import McpConfigurationError

__all__ = [
    "McpCallFacts",
    "McpCallResult",
    "McpConnection",
    "McpToolDescriptor",
    "McpToolDirectory",
]


@dataclass(frozen=True)
class McpConnection:
    """One established (or establishing) connection, as the host may reference it.

    ``generation`` counts how many times this scope has been connected inside one
    manager: it is a transport fact, not part of any logical operation identity, and
    it exists so a stale handle can be recognised after a reconnect.
    """

    server_id: str
    transport: McpTransport
    protocol: str
    authorization_scope: str
    config_revision: str
    generation: int
    connection_id: str

    def __post_init__(self) -> None:
        if self.generation < 1:
            raise McpConfigurationError("a connection generation starts at 1")
        if not self.connection_id:
            raise McpConfigurationError("a connection needs an id")

    @property
    def scope_key(self) -> tuple[str, str, str, str, str]:
        return (
            self.server_id,
            self.authorization_scope,
            self.config_revision,
            self.protocol,
            self.transport.value,
        )


@dataclass(frozen=True)
class McpToolDescriptor:
    """One remote tool, exactly as the server named it (case preserved)."""

    name: str
    description: str = ""
    title: str | None = None
    input_schema: Mapping[str, Any] = field(default_factory=dict)
    output_schema: Mapping[str, Any] | None = None
    #: Server-declared annotations as *data*. They never grant anything.
    annotations: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name:
            raise McpConfigurationError("a remote tool must have a name")


@dataclass(frozen=True)
class McpToolDirectory:
    """One bounded read of a server's tool listing."""

    descriptors: tuple[McpToolDescriptor, ...]
    pages_read: int = 1
    complete: bool = True

    def __post_init__(self) -> None:
        if self.pages_read < 1:
            raise McpConfigurationError("a directory read has at least one page")


@dataclass(frozen=True)
class McpCallResult:
    """A complete answer from one remote tool call, in neutral terms.

    ``is_error`` is the server's own verdict about the result, not a transport
    failure: the call reached the server and the server answered. Turning it into
    a ledger state is MCP05's job.
    """

    name: str
    #: Ordered text content, already reduced to bounded strings.
    text: tuple[str, ...] = ()
    #: Structured content as JSON, when the server sent any (not necessarily an
    #: object: the 2026 revision allows arrays and scalars).
    structured: Any = None
    is_error: bool = False
    #: True when the result carried a shape this release does not interpret.
    unsupported_shape: bool = False
    #: True when the remote result said it wants another round (input required,
    #: sampling, elicitation, a task handle). The first release refuses those.
    requests_more: bool = False

    def __post_init__(self) -> None:
        if not self.name:
            raise McpConfigurationError("a call result must name the tool it answers")


@dataclass(frozen=True)
class McpCallFacts:
    """Bounded, non-secret facts about one call, for receipts and diagnostics.

    Deliberately small: the authoritative record stays the tool intent and receipt
    in the existing ledger; these are observations that may legitimately be missing
    after a crash.
    """

    connection_id: str
    generation: int
    transport: str
    protocol: str
    remote_name: str
    duration_seconds: float
    dispatched: bool
    result_source: str = ""

    def as_details(self) -> Mapping[str, Any]:
        return {
            "connection_id": self.connection_id,
            "connection_generation": self.generation,
            "transport": self.transport,
            "protocol": self.protocol,
            "remote_tool": self.remote_name,
            "duration_seconds": round(self.duration_seconds, 6),
            "dispatched": self.dispatched,
            "result_source": self.result_source,
        }
