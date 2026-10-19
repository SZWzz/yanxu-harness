"""把目录冻成真正的 ``ToolDefinition``：真 validator、宿主 preview、无身份注入。

这里是把 MCP 工具接进既有 ToolGateway 的桥。三条纪律：

* **validator 是真的。** ``args_validator`` 用远端 schema 真跑校验；JSON Schema 本身
  不等于校验器，所以工具定义不能只挂一份 schema。
* **写工具必须有宿主同步 preview。** 预览由宿主绑定提供（或由适配器生成只描述
  「调用哪个服务/哪个工具、完整参数、目标与影响」的结构化预览），绝不在同步 preview
  里发网络请求，也不冒充远端 diff。
* **本地身份不注入模型 schema。** operation id、namespace、凭据、endpoint 都不进参数
  schema；schema 里如果本来就有这类名字，``identity_shaped_fields`` 能把它们列出来
  供宿主拒绝。
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping

from ...tools.definition import Idempotency, ToolDefinition
from ...tools.outcomes import ToolArgsInvalid
from .catalog import CatalogEntry, CatalogSnapshot
from .errors import McpConfigurationError

__all__ = [
    "IDENTITY_SHAPED_NAMES",
    "build_definitions",
    "identity_shaped_fields",
    "tool_definition",
]

MAX_DESCRIPTION_CHARS = 2000

#: Field names that look like server authority. They are *never* authority here; the
#: helper exists so a host can refuse a definition instead of quietly accepting a
#: schema that asks the model for an endpoint or a token.
IDENTITY_SHAPED_NAMES = (
    "namespace",
    "session_id",
    "run_id",
    "attempt_id",
    "operation_id",
    "model_action_id",
    "authorization",
    "api_key",
    "token",
    "secret",
    "password",
    "endpoint",
    "base_url",
    "url",
    "header",
)


def identity_shaped_fields(schema: Mapping[str, Any]) -> tuple[str, ...]:
    """Property names in a schema that look like host authority."""
    found: set[str] = set()
    stack: list[Any] = [schema]
    while stack:
        node = stack.pop()
        if isinstance(node, Mapping):
            properties = node.get("properties")
            if isinstance(properties, Mapping):
                for name in properties:
                    if str(name).lower() in IDENTITY_SHAPED_NAMES:
                        found.add(str(name))
            stack.extend(node.values())
        elif isinstance(node, (list, tuple)):
            stack.extend(node)
    return tuple(sorted(found))


def _bounded_description(entry: CatalogEntry) -> str:
    text = entry.description or entry.title or f"Remote tool {entry.remote_name}"
    text = text.strip()[:MAX_DESCRIPTION_CHARS]
    return text or f"Remote tool {entry.remote_name}"


def _preview_for(snapshot: CatalogSnapshot, entry: CatalogEntry) -> Callable[[Any, Any], Mapping[str, Any]]:
    """A synchronous, local preview. It never performs I/O."""

    def preview(context: Any, arguments: Any) -> Mapping[str, Any]:
        rendered: dict[str, Any] = {
            "server": snapshot.server_id,
            "transport": snapshot.transport,
            "remote_tool": entry.remote_name,
            "tool_alias": entry.alias,
            "tool_version": entry.version,
            "arguments": dict(arguments or {}),
            "policy_revision": entry.binding.policy_revision,
            "note": "call preview: names the remote tool and the exact arguments; not a remote diff",
        }
        host_preview = entry.binding.preview
        if host_preview is not None:
            rendered["host_preview"] = dict(host_preview(context, arguments))
        return rendered

    return preview


def tool_definition(
    snapshot: CatalogSnapshot,
    entry: CatalogEntry,
    handler: Callable[[Any, Any], Any],
    *,
    dispatch_guard: Callable[[Mapping[str, Any]], str | None] | None = None,
) -> ToolDefinition:
    """One remote tool as a gateway-routed definition under its local alias."""
    binding = entry.binding
    kwargs: dict[str, Any] = {
        "effects": binding.effects,
        "version": entry.version,
        "args_validator": _validator(entry),
        "args_schema": lambda: dict(entry.schema.document),
        "execution_mode": binding.execution_mode,
        # A remote tool never claims keyed idempotency: the ledger is authoritative and
        # replay stays "never" unless a host adapter supplies its own evidence path.
        "idempotency": Idempotency.NONE,
        "result_char_limit": binding.result_char_limit,
        "capability": "mcp",
        # A host that owns a live binding (MCP here) can refuse a dispatch or a resume
        # when its frozen definition and current trusted facts disagree. The guard is
        # deliberately not called by the handler: it must run *before* admission.
        "dispatch_guard": dispatch_guard,
    }
    if binding.mutating:
        kwargs["preview"] = _preview_for(snapshot, entry)
    return ToolDefinition(entry.alias, _bounded_description(entry), handler=handler, **kwargs)


def _validator(entry: CatalogEntry) -> Callable[[Mapping[str, Any]], Mapping[str, Any]]:
    def validate(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        try:
            return entry.schema.validate(arguments)
        except ToolArgsInvalid:
            raise
        except Exception as exc:  # noqa: BLE001 - the schema layer raises its own errors
            raise ToolArgsInvalid(f"{entry.alias}: {exc}") from exc

    return validate


def build_definitions(
    snapshot: CatalogSnapshot,
    handler_for: Callable[[CatalogEntry], Callable[[Any, Any], Any]],
    *,
    only: Iterable[str] | None = None,
    guard_for: Callable[[CatalogEntry], Callable[[Mapping[str, Any]], str | None]] | None = None,
) -> tuple[ToolDefinition, ...]:
    """Build every bound definition, or one specific subset (the delegated case).

    ``only`` names local aliases or exact remote names; naming a tool the snapshot does
    not contain is refused, because a child that silently gets fewer tools than the host
    delegated would run a different job than the one that was approved.
    """
    wanted = None if only is None else set(only)
    if wanted is not None:
        available = {entry.alias for entry in snapshot.entries} | {
            entry.remote_name for entry in snapshot.entries
        }
        unknown = sorted(wanted - available)
        if unknown:
            raise McpConfigurationError(
                f"{snapshot.server_id}: these tools are not in the frozen catalog: {unknown}"
            )
    definitions = []
    for entry in snapshot.entries:
        if wanted is not None and entry.alias not in wanted and entry.remote_name not in wanted:
            continue
        definitions.append(
            tool_definition(
                snapshot,
                entry,
                handler_for(entry),
                dispatch_guard=None if guard_for is None else guard_for(entry),
            )
        )
    return tuple(definitions)
