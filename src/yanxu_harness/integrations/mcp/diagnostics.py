"""诊断事实：只读、有界、无秘密，且不连接任何一个 MCP 服务。

诊断的用途是「事后看发生了什么」，不是「顺手再调一次」。所以这里只有纯函数：
输入是已有 receipt details / host config，输出是白名单字段，不发网络、不起进程、
不读环境变量之外的任何东西，也不把远端自由文本原样带出去。

默认不公开（计划 §7）：原始 endpoint、argv/env、headers、token、SDK 对象、原始协议
帧、参数/结果全文、服务端日志。``safe_config_facts`` 连 command/url 都不给，只给
「有几个 env 键 / 几个 header 名字」这种计数。
"""

from __future__ import annotations

from typing import Any, Mapping

from .config import McpServerConfig

__all__ = ["safe_call_facts", "safe_config_facts"]

#: Which receipt-detail keys are safe to surface, and the bound for each string.
_CALL_FACT_KEYS: tuple[tuple[str, int], ...] = (
    ("alias", 64),
    ("remote_tool", 200),
    ("tool_version", 80),
    ("server_id", 64),
    ("transport", 32),
    ("protocol", 16),
    ("connection_id", 64),
    ("connection_generation", 0),
    ("dispatched", 0),
    ("duration_seconds", 0),
    ("result_source", 32),
    ("policy_revision", 32),
    ("not_sent", 0),
    ("remote_is_error", 0),
    ("unsupported_kind", 32),
    ("effects", 0),
)


def _bounded(value: Any, limit: int) -> Any:
    if limit <= 0:
        return value
    text = value if isinstance(value, str) else str(value)
    text = text.replace("\n", " ").replace("\r", " ")
    return text if len(text) <= limit else text[:limit] + "…"


def safe_call_facts(details: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """The MCP half of a receipt's details, allowlisted and bounded.

    Free text (an error message, a result, a remote payload) is deliberately not in
    the allowlist: it stays where it already was — in the receipt — and out of the
    diagnostic projection.
    """
    if not details:
        return {}
    selected: dict[str, Any] = {}
    for key, limit in _CALL_FACT_KEYS:
        if key not in details:
            continue
        value = details[key]
        if isinstance(value, (list, tuple)):
            value = [_bounded(item, 64) for item in list(value)[:8]]
        elif isinstance(value, Mapping):
            continue
        selected[key] = _bounded(value, limit)
    return selected


def safe_config_facts(config: McpServerConfig) -> Mapping[str, Any]:
    """A connection's identity without its launch details or its credentials.

    No url, no command, no argv, no environment values, no header values. A reader
    can see *that* credentials exist (a count) without seeing which ones.
    """
    return {
        "server_id": config.server_id,
        "transport": config.transport.value,
        "protocol": config.protocol,
        "authorization_scope": config.authorization_scope,
        "config_revision": config.config_revision,
        "env_key_count": len(config.env),
        "header_name_count": len(config.headers),
        "environment_keys": sorted(str(name)[:64] for name in config.env)[:16],
        "header_names": sorted(str(name)[:64] for name in config.headers)[:16],
    }
