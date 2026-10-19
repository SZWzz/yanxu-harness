"""宿主配置：MCP 服务怎么起、允许说到哪一代、资源上限是多少。

两条硬规则：

* **模型和远端都不能改这份配置。** 它由宿主构造，适配器只读；服务端返回的
  ``serverInfo``、``instructions`` 或工具自身声明都不能覆盖 server_id、协议、端点或上限。
* **上限必须有限且为正。** 一个「无限」的字段等于没有上限；``math.isfinite``
  连同正值判断一起在构造时拒绝，而不是等到运行时才发现。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Mapping

from .errors import McpConfigurationError

__all__ = [
    "DEFAULT_INHERITED_ENV_VARS",
    "McpLimits",
    "McpServerConfig",
    "McpTransport",
    "SUPPORTED_PROTOCOLS",
]

#: The protocol revisions this adapter pins and verifies (MCP00 ADR). Anything
#: else is refused at construction: "we sent the string" is not support.
SUPPORTED_PROTOCOLS: tuple[str, ...] = ("2025-11-25", "2026-07-28")

#: The environment a stdio server inherits when the host asks for the default
#: environment. Mirrors the SDK's own whitelist; the host may add explicit
#: entries, and nothing else is passed through.
DEFAULT_INHERITED_ENV_VARS: tuple[str, ...] = ("HOME", "LOGNAME", "PATH", "SHELL", "TERM", "USER")

_SERVER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


class McpTransport(str, Enum):
    """How this adapter reaches one server."""

    STDIO = "stdio"
    STREAMABLE_HTTP = "streamable_http"


@dataclass(frozen=True)
class McpLimits:
    """Every bound the adapter enforces, with the plan's defaults.

    ``connect``/``discovery``/``call``/``close_grace`` are seconds; the rest are
    bytes, counts or slots. A host may raise them explicitly; a server may not.
    """

    connect_timeout: float = 10.0
    discovery_timeout: float = 15.0
    call_timeout: float = 30.0
    close_grace: float = 5.0
    max_concurrent_calls: int = 4
    max_scope_calls: int = 8
    max_pages: int = 16
    max_tools: int = 256
    max_catalog_bytes: int = 2 * 1024 * 1024
    max_schema_bytes: int = 64 * 1024
    max_message_bytes: int = 1024 * 1024
    max_stderr_bytes: int = 64 * 1024

    def __post_init__(self) -> None:
        for name in (
            "connect_timeout",
            "discovery_timeout",
            "call_timeout",
            "close_grace",
        ):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise McpConfigurationError(f"{name} must be a number, got {value!r}")
            if not math.isfinite(value) or value <= 0:
                raise McpConfigurationError(f"{name} must be positive and finite, got {value!r}")
        for name in (
            "max_concurrent_calls",
            "max_scope_calls",
            "max_pages",
            "max_tools",
            "max_catalog_bytes",
            "max_schema_bytes",
            "max_message_bytes",
            "max_stderr_bytes",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise McpConfigurationError(f"{name} must be a positive integer, got {value!r}")

    def with_overrides(self, **changes: Any) -> "McpLimits":
        return replace(self, **changes)


@dataclass(frozen=True)
class McpServerConfig:
    """One host-configured MCP server.

    ``authorization_scope`` and ``config_revision`` are the host's own facts: two
    runs may share a process and a server but not a credential scope, and a
    connection must never be reused across either boundary.
    """

    server_id: str
    transport: McpTransport
    #: The single protocol revision this connection is allowed to negotiate.
    protocol: str
    authorization_scope: str
    limits: McpLimits = field(default_factory=McpLimits)
    config_revision: str = "1"

    # stdio
    command: str | None = None
    args: tuple[str, ...] = ()
    cwd: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    inherit_default_environment: bool = True

    # streamable HTTP
    url: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    allow_loopback_http: bool = False

    def __post_init__(self) -> None:
        if not _SERVER_ID_RE.match(self.server_id or ""):
            raise McpConfigurationError(
                f"server_id {self.server_id!r} is not a stable id "
                "(lowercase letters, digits, dot, dash, underscore; max 64 chars)"
            )
        if not isinstance(self.transport, McpTransport):
            raise McpConfigurationError(f"transport must be an McpTransport, got {self.transport!r}")
        if self.protocol not in SUPPORTED_PROTOCOLS:
            raise McpConfigurationError(
                f"protocol {self.protocol!r} is not one of the pinned revisions {SUPPORTED_PROTOCOLS}; "
                "an unverified revision is refused instead of being attempted"
            )
        for name in ("authorization_scope", "config_revision"):
            if not getattr(self, name):
                raise McpConfigurationError(f"{name} must be non-empty")
        if not isinstance(self.limits, McpLimits):
            raise McpConfigurationError("limits must be an McpLimits instance")

        if self.transport is McpTransport.STDIO:
            self._validate_stdio()
        else:
            self._validate_http()

    def _validate_stdio(self) -> None:
        if not self.command:
            raise McpConfigurationError("a stdio server requires an explicit executable in `command`")
        if self.url is not None:
            raise McpConfigurationError("a stdio server must not also carry a url")
        for label, value in (("command", self.command), ("cwd", self.cwd or "")):
            if "\x00" in value:
                raise McpConfigurationError(f"{label} must not contain NUL bytes")
        for index, argument in enumerate(self.args):
            if not isinstance(argument, str) or "\x00" in argument:
                raise McpConfigurationError(f"args[{index}] must be a string without NUL bytes")
        for key, value in dict(self.env).items():
            if not isinstance(key, str) or not key or "=" in key:
                raise McpConfigurationError(f"environment key {key!r} is not usable")
            if not isinstance(value, str) or "\x00" in value:
                raise McpConfigurationError(f"environment value for {key!r} must be a string without NUL bytes")

    def _validate_http(self) -> None:
        if not self.url:
            raise McpConfigurationError("a Streamable HTTP server requires a `url`")
        if self.command is not None or self.args or self.cwd is not None or self.env:
            raise McpConfigurationError("a Streamable HTTP server must not carry stdio launch fields")
        scheme = self.url.split("://", 1)[0].lower() if "://" in self.url else ""
        if scheme not in {"http", "https"}:
            raise McpConfigurationError(f"url {self.url!r} must be http(s)")
        if "@" in self.url.split("://", 1)[1].split("/", 1)[0]:
            raise McpConfigurationError("credentials must not be embedded in the url; pass a header instead")
        if "?" in self.url:
            raise McpConfigurationError("the endpoint must not carry a query string (it may hold a secret)")
        if scheme == "http" and not _is_loopback(self.url) and not self.allow_loopback_http:
            raise McpConfigurationError(
                "plain http is only allowed for loopback endpoints; "
                "set allow_loopback_http explicitly for a local service"
            )
        if scheme == "http" and not _is_loopback(self.url) and self.allow_loopback_http:
            raise McpConfigurationError("allow_loopback_http only permits loopback hosts, not a remote one")
        for name, value in dict(self.headers).items():
            if not isinstance(name, str) or not name or "\r" in name or "\n" in name:
                raise McpConfigurationError(f"header name {name!r} is not usable")
            if not isinstance(value, str) or "\r" in value or "\n" in value:
                raise McpConfigurationError(f"header {name!r} must be a single-line string")

    def connection_scope(self) -> tuple[str, ...]:
        """The identity a connection is keyed by.

        Namespace, authorization scope, server id, configuration revision, protocol
        and transport all participate: reusing a connection across any of them would
        let one host context speak with another's credentials or pin.
        """
        return (
            self.server_id,
            self.authorization_scope,
            self.config_revision,
            self.protocol,
            self.transport.value,
        )

    def describe(self) -> Mapping[str, Any]:
        """A safe, non-secret description (never the env values or headers)."""
        return {
            "server_id": self.server_id,
            "transport": self.transport.value,
            "protocol": self.protocol,
            "authorization_scope": self.authorization_scope,
            "config_revision": self.config_revision,
            "command": self.command,
            "args": list(self.args),
            "url": self.url,
            "env_keys": sorted(self.env),
            "header_names": sorted(self.headers),
        }


def _is_loopback(url: str) -> bool:
    authority = url.split("://", 1)[1].split("/", 1)[0]
    host = authority.split(":", 1)[0].strip("[]").lower()
    return host in {"127.0.0.1", "::1", "localhost"}
