"""懒加载边界：整份适配器里唯一 import SDK 的地方。

基础安装不含 ``mcp``，所以这个模块在导入时**不得**碰 SDK；真正需要时由
:func:`load_sdk` 导入，缺依赖时给出一条能照做的错误，而不是一条 ImportError 栈。

同时它是 import fence 的豁免点：``importlib.import_module("mcp...")`` 的字符串
只出现在函数体内，冷启动遍历导入不会把它们拉起来（``tests/test_import_fence.py``
既允许这条豁免，也要求它保持惰性）。
"""

from __future__ import annotations

import importlib
import importlib.util
from dataclasses import dataclass
from typing import Any

from .errors import McpExtraMissing

__all__ = ["McpSdk", "load_sdk", "sdk_available"]

#: What a host must install to enable this adapter.
INSTALL_HINT = "pip install 'yanxu-harness[mcp]'"

_SDK_CACHE: "McpSdk | None" = None


@dataclass(frozen=True)
class McpSdk:
    """The SDK surface this adapter actually uses, resolved once.

    Only public entry points are named here; nothing below reaches into SDK
    privates, and the adapter never hands an SDK object to the ledger.
    """

    mcp_types: Any
    session_class: Any
    stdio_server_parameters: Any
    jsonrpc_adapter: Any
    session_message: Any
    protocol_versions: Any
    #: The SDK's JSON-RPC error type, so callers can recognise a protocol-level
    #: refusal without importing the SDK at module scope.
    rpc_error: Any

    def supported_protocols(self) -> tuple[str, ...]:
        return tuple(self.protocol_versions.HANDSHAKE_PROTOCOL_VERSIONS) + tuple(
            self.protocol_versions.MODERN_PROTOCOL_VERSIONS
        )


def _import(module: str) -> Any:
    try:
        return importlib.import_module(module)
    except ModuleNotFoundError as exc:  # pragma: no cover - exercised via subprocess
        if exc.name and (exc.name == "mcp" or exc.name.startswith("mcp.") or exc.name == "mcp_types"):
            raise McpExtraMissing(
                f"the MCP client adapter needs the 'mcp' extra: {INSTALL_HINT}"
            ) from exc
        raise


def sdk_available() -> bool:
    """True when the pinned SDK can be imported, without importing it in this process.

    ``importlib.util`` is imported explicitly: in a cold process `import importlib`
    alone leaves the attribute unset, and a probe that only works because pytest
    happened to import it first is not a probe.
    """
    try:
        spec = importlib.util.find_spec("mcp.client.session")
    except (ImportError, ModuleNotFoundError):  # pragma: no cover - defensive
        return False
    return spec is not None


def load_sdk() -> McpSdk:
    """Import and cache the pinned SDK, or raise :class:`McpExtraMissing`."""
    global _SDK_CACHE
    if _SDK_CACHE is not None:
        return _SDK_CACHE

    mcp_types = _import("mcp_types")
    session_module = _import("mcp.client.session")
    stdio_module = _import("mcp.client.stdio")
    message_module = _import("mcp.shared.message")
    exceptions_module = _import("mcp.shared.exceptions")
    version_module = _import("mcp_types.version")

    _SDK_CACHE = McpSdk(
        mcp_types=mcp_types,
        session_class=session_module.ClientSession,
        stdio_server_parameters=stdio_module.StdioServerParameters,
        jsonrpc_adapter=mcp_types.jsonrpc_message_adapter,
        session_message=message_module.SessionMessage,
        protocol_versions=version_module,
        rpc_error=exceptions_module.MCPError,
    )
    return _SDK_CACHE
