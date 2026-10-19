"""MCP Client 适配（可选）。

范围：MCP **Tools**，两种传输（stdio、Streamable HTTP），两代协议（2025-11-25 与
2026-07-28）。做成可选 extra：基础安装没有任何第三方依赖，SDK 只在真正启用时导入。

不会做（首版明确不做）：Resources/Prompts 主动读取、交互式 OAuth 登录、自动安装
服务端、sampling/elicitation、MCP Tasks、自动重试工具调用、第二份操作账本或第二个
模型循环。工具调用必须经过既有 ``ToolGateway`` 与唯一账本。
"""

from __future__ import annotations

from .client import McpClientManager
from .config import (
    DEFAULT_INHERITED_ENV_VARS,
    SUPPORTED_PROTOCOLS,
    McpLimits,
    McpServerConfig,
    McpTransport,
)
from .errors import (
    McpCallFailure,
    McpClosed,
    McpConfigurationError,
    McpConnectError,
    McpConnectionLost,
    McpError,
    McpExtraMissing,
    McpFrameTooLarge,
    McpNotConnected,
    McpNotSent,
    McpProtocolError,
    McpToolError,
)
from .sdk import load_sdk, sdk_available
from .types import McpCallFacts, McpCallResult, McpConnection, McpToolDescriptor

__all__ = [
    "DEFAULT_INHERITED_ENV_VARS",
    "SUPPORTED_PROTOCOLS",
    "McpCallFacts",
    "McpCallFailure",
    "McpCallResult",
    "McpClientManager",
    "McpClosed",
    "McpConfigurationError",
    "McpConnectError",
    "McpConnection",
    "McpConnectionLost",
    "McpError",
    "McpExtraMissing",
    "McpFrameTooLarge",
    "McpLimits",
    "McpNotConnected",
    "McpNotSent",
    "McpProtocolError",
    "McpServerConfig",
    "McpToolDescriptor",
    "McpToolError",
    "McpTransport",
    "load_sdk",
    "sdk_available",
]
