"""MCP Client 适配（可选）。

范围：MCP **Tools**，两种传输（stdio、Streamable HTTP），两代协议（2025-11-25 与
2026-07-28）。做成可选 extra：基础安装没有任何第三方依赖，SDK 只在真正启用时导入。

不会做（首版明确不做）：Resources/Prompts 主动读取、交互式 OAuth 登录、自动安装
服务端、sampling/elicitation、MCP Tasks、自动重试工具调用、第二份操作账本或第二个
模型循环。工具调用必须经过既有 ``ToolGateway`` 与唯一账本。
"""

from __future__ import annotations

from .adapter import McpToolAdapter
from .client import McpClientManager
from .catalog import (
    BindingCheck,
    CatalogEntry,
    CatalogExclusion,
    CatalogSnapshot,
    FrozenToolBinding,
    McpToolBinding,
    build_snapshot,
    check_binding,
    discover_catalog,
    freeze_binding,
    local_alias,
)
from .config import (
    DEFAULT_INHERITED_ENV_VARS,
    SUPPORTED_PROTOCOLS,
    McpLimits,
    McpServerConfig,
    McpTransport,
)
from .diagnostics import safe_call_facts, safe_config_facts
from .errors import (
    McpCallFailure,
    McpClosed,
    McpConfigurationError,
    McpConnectError,
    McpConnectionLost,
    McpDriftRefusal,
    McpError,
    McpExtraMissing,
    McpFrameTooLarge,
    McpInvalidArguments,
    McpNotConnected,
    McpNotSent,
    McpProtocolError,
    McpSchemaError,
    McpToolError,
)
from .results import MAX_MODEL_TEXT_CHARS, codec_result
from .schema import RemoteToolSchema
from .sdk import load_sdk, sdk_available
from .tools import build_definitions, identity_shaped_fields, tool_definition
from .types import (
    McpCallFacts,
    McpCallResult,
    McpConnection,
    McpToolDescriptor,
    McpToolDirectory,
)

__all__ = [
    "DEFAULT_INHERITED_ENV_VARS",
    "SUPPORTED_PROTOCOLS",
    "BindingCheck",
    "CatalogEntry",
    "CatalogExclusion",
    "CatalogSnapshot",
    "FrozenToolBinding",
    "MAX_MODEL_TEXT_CHARS",
    "McpCallFacts",
    "McpCallFailure",
    "McpCallResult",
    "McpClientManager",
    "McpClosed",
    "McpConfigurationError",
    "McpConnectError",
    "McpConnection",
    "McpConnectionLost",
    "McpDriftRefusal",
    "McpError",
    "McpExtraMissing",
    "McpFrameTooLarge",
    "McpInvalidArguments",
    "McpLimits",
    "McpNotConnected",
    "McpNotSent",
    "McpProtocolError",
    "McpSchemaError",
    "McpServerConfig",
    "McpToolBinding",
    "McpToolDescriptor",
    "McpToolDirectory",
    "McpToolError",
    "McpToolAdapter",
    "McpTransport",
    "RemoteToolSchema",
    "build_definitions",
    "build_snapshot",
    "check_binding",
    "codec_result",
    "discover_catalog",
    "freeze_binding",
    "identity_shaped_fields",
    "load_sdk",
    "local_alias",
    "safe_call_facts",
    "safe_config_facts",
    "sdk_available",
    "tool_definition",
]
