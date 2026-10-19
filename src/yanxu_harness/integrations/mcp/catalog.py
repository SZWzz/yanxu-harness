"""目录、本地别名、冻结版本与漂移判定。

三件事在这里定死：

1. **远端名字是远端事实。** 原始大小写原样保留；本地别名是宿主侧的稳定小写 ID，
   由 ``mcp.<server_slug>.<tool_slug>.<digest12>`` 组成（总长 <= 64），摘要同时覆盖
   server_id 与原始名，所以两个服务、两个大小写不同但小写后相同的名字都不会互相覆盖。
2. **目录不是授权。** 服务端的 ``readOnlyHint``/``destructiveHint``/``idempotentHint``、
   描述和 instructions 只作为数据保存；效果、审批、policy revision 只能来自宿主绑定。
3. **冻结版本参与原 arrangement。** 定义版本是一个带版本号的 canonical fingerprint，
   覆盖服务身份、协议、原始工具名、输入/输出 schema、模型可见说明、annotations（数据）
   与绑定事实；恢复时拿当前目录去核对**原冻结版本**，不一致就结构化拒绝，绝不用新目录
   覆盖旧审批。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

from ...tools.definition import ExecutionMode, Idempotency, ToolEffect
from .config import McpServerConfig
from .errors import McpConfigurationError, McpDriftRefusal, McpSchemaError
from .schema import RemoteToolSchema
from .types import McpToolDescriptor

__all__ = [
    "ALIAS_PREFIX",
    "BindingCheck",
    "CatalogEntry",
    "CatalogExclusion",
    "CatalogSnapshot",
    "FrozenToolBinding",
    "McpToolBinding",
    "build_snapshot",
    "check_binding",
    "definition_version",
    "discover_catalog",
    "freeze_binding",
    "local_alias",
]

ALIAS_PREFIX = "mcp"
MAX_ALIAS_LENGTH = 64
_SERVER_SLUG_LENGTH = 16
_TOOL_SLUG_LENGTH = 24

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(value: str, *, limit: int, fallback: str) -> str:
    slug = _SLUG_RE.sub("-", value.lower()).strip("-")
    slug = slug[:limit].strip("-")
    return slug or fallback


def local_alias(server_id: str, remote_name: str) -> str:
    """The local stable id for one remote tool.

    The trailing digest covers ``server_id`` and the *exact* remote name, so
    truncation and case folding can never make two different tools share one alias.
    """
    server_slug = _slug(server_id, limit=_SERVER_SLUG_LENGTH, fallback="server")
    tool_slug = _slug(remote_name, limit=_TOOL_SLUG_LENGTH, fallback="tool")
    digest = hashlib.sha256(f"{server_id}\x00{remote_name}".encode("utf-8")).hexdigest()[:12]
    alias = f"{ALIAS_PREFIX}.{server_slug}.{tool_slug}.{digest}"
    if len(alias) > MAX_ALIAS_LENGTH:  # pragma: no cover - the slug limits prevent this
        raise McpConfigurationError(f"alias {alias!r} exceeds {MAX_ALIAS_LENGTH} characters")
    return alias


@dataclass(frozen=True)
class McpToolBinding:
    """The host's explicit decision about one remote tool.

    Everything the deployment has to grant lives here: the effects, whether a write
    needs a preview, the execution mode, the idempotency claim and the policy
    revision. None of it can come from the server's annotations.
    """

    remote_name: str
    effects: frozenset[ToolEffect]
    execution_mode: ExecutionMode = ExecutionMode.PARALLEL
    idempotency: Idempotency = Idempotency.NONE
    preview: Callable[[Any, Any], Mapping[str, Any]] | None = None
    policy_revision: str = "1"
    result_char_limit: int = 8000
    #: Server-side version of the target, when the host has one. Never taken from
    #: model arguments.
    target_version: Callable[[str, Mapping[str, Any]], str | None] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "effects", frozenset(self.effects))
        if not self.remote_name:
            raise McpConfigurationError("a binding must name the remote tool")
        if not self.effects:
            raise McpConfigurationError(f"{self.remote_name}: a binding must classify the tool's effects")
        if ToolEffect.WRITE in self.effects and self.preview is None:
            raise McpConfigurationError(
                f"{self.remote_name}: a write binding needs a synchronous preview so an approval "
                "binds to something concrete"
            )
        if self.idempotency is Idempotency.KEYED:
            raise McpConfigurationError(
                f"{self.remote_name}: this release does not let a remote tool claim keyed idempotency; "
                "the ledger stays authoritative"
            )
        if not self.policy_revision:
            raise McpConfigurationError(f"{self.remote_name}: policy_revision must be non-empty")

    @property
    def mutating(self) -> bool:
        return bool(self.effects & {ToolEffect.WRITE, ToolEffect.PROPOSE})


@dataclass(frozen=True)
class CatalogExclusion:
    """One remote tool that was deliberately not exposed, and why."""

    remote_name: str
    reason: str


@dataclass(frozen=True)
class CatalogEntry:
    """One exposed remote tool with its local identity already resolved."""

    alias: str
    remote_name: str
    description: str
    title: str | None
    schema: RemoteToolSchema
    output_schema: Mapping[str, Any] | None
    annotations: Mapping[str, Any]
    binding: McpToolBinding
    version: str

    @property
    def detail(self) -> Mapping[str, Any]:
        """Safe, bounded facts for receipts and diagnostics (never the schemas)."""
        return {
            "alias": self.alias,
            "remote_tool": self.remote_name,
            "tool_version": self.version,
            "effects": sorted(effect.value for effect in self.binding.effects),
            "policy_revision": self.binding.policy_revision,
        }


@dataclass(frozen=True)
class CatalogSnapshot:
    """An immutable view of one server's tools at one point in time."""

    server_id: str
    protocol: str
    authorization_scope: str
    config_revision: str
    transport: str
    entries: tuple[CatalogEntry, ...]
    exclusions: tuple[CatalogExclusion, ...] = ()
    complete: bool = True
    pages_read: int = 1
    fingerprint: str = ""

    def entry(self, name: str) -> CatalogEntry | None:
        """Find an entry by local alias or by exact remote name."""
        for entry in self.entries:
            if entry.alias == name or entry.remote_name == name:
                return entry
        return None

    def describe(self) -> Mapping[str, Any]:
        """The public, secret-free summary of this snapshot."""
        return {
            "server_id": self.server_id,
            "protocol": self.protocol,
            "authorization_scope": self.authorization_scope,
            "config_revision": self.config_revision,
            "transport": self.transport,
            "complete": self.complete,
            "pages_read": self.pages_read,
            "fingerprint": self.fingerprint,
            "tools": [
                {"alias": entry.alias, "remote_tool": entry.remote_name, "version": entry.version}
                for entry in self.entries
            ],
            "excluded": [
                {"remote_tool": item.remote_name, "reason": item.reason} for item in self.exclusions
            ],
        }


@dataclass(frozen=True)
class FrozenToolBinding:
    """What a run commits to when it dispatches: the exact tool version it approved.

    This is the value that must travel into the run arrangement, so recovery can compare
    the *original* binding against the current catalog instead of re-freezing silently.
    """

    alias: str
    remote_name: str
    version: str
    server_id: str
    protocol: str
    config_revision: str
    authorization_scope: str
    policy_revision: str

    def as_details(self) -> Mapping[str, Any]:
        return {
            "alias": self.alias,
            "remote_tool": self.remote_name,
            "tool_version": self.version,
            "server_id": self.server_id,
            "protocol": self.protocol,
            "mcp_config_revision": self.config_revision,
            "mcp_authorization_scope": self.authorization_scope,
            "policy_revision": self.policy_revision,
        }


@dataclass(frozen=True)
class BindingCheck:
    """The verdict of comparing a frozen binding against the live catalog."""

    ok: bool
    code: str = ""
    reason: str = ""

    def raise_for_refusal(self) -> None:
        if not self.ok:
            raise McpDriftRefusal(f"{self.code}: {self.reason}")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def definition_version(
    *,
    config: McpServerConfig,
    descriptor: McpToolDescriptor,
    schema: RemoteToolSchema,
    binding: McpToolBinding,
) -> str:
    """A versioned canonical fingerprint of everything the definition *means*.

    Excluded on purpose: credentials, connection ids, request ids, timestamps and any
    runtime observation. Two calls that would be approved identically must hash
    identically, and a re-deployed server must hash differently.
    """
    payload = {
        "version": 1,
        "server_id": config.server_id,
        "config_revision": config.config_revision,
        "authorization_scope": config.authorization_scope,
        "transport": config.transport.value,
        "protocol": config.protocol,
        "remote_tool": descriptor.name,
        "description": descriptor.description,
        "title": descriptor.title,
        "input_schema": dict(schema.document),
        "output_schema": dict(descriptor.output_schema) if descriptor.output_schema else None,
        "dialect": schema.dialect,
        # Annotations are data, and they participate in the version: a server that
        # changes its hints has changed the definition, even though the change cannot
        # grant anything.
        "annotations": dict(descriptor.annotations),
        "binding": {
            "effects": sorted(effect.value for effect in binding.effects),
            "execution_mode": binding.execution_mode.value,
            "idempotency": binding.idempotency.value,
            "policy_revision": binding.policy_revision,
            "has_preview": binding.preview is not None,
            "preview_rule": getattr(binding.preview, "__qualname__", None),
        },
    }
    return "sha256:" + hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def build_snapshot(
    descriptors: Iterable[McpToolDescriptor],
    *,
    config: McpServerConfig,
    bindings: Mapping[str, McpToolBinding],
    pages_read: int = 1,
    complete: bool = True,
) -> CatalogSnapshot:
    """Freeze a catalog from descriptors + the host's bindings.

    Tools are *discovered* here, not authorized: an entry only exists when the host
    named it in ``bindings``. Everything else is reported as an exclusion. A tool the
    host did bind but whose schema cannot be compiled is a hard failure — silently
    dropping a bound tool is how a run ends up dispatching an unapproved variant.
    """
    entries: list[CatalogEntry] = []
    exclusions: list[CatalogExclusion] = []
    aliases: dict[str, str] = {}

    for descriptor in descriptors:
        binding = bindings.get(descriptor.name)
        if binding is None:
            exclusions.append(
                CatalogExclusion(descriptor.name, "not bound by the host (the catalog grants nothing)")
            )
            continue
        alias = local_alias(config.server_id, descriptor.name)
        owner = aliases.get(alias)
        if owner is not None and owner != descriptor.name:
            raise McpConfigurationError(
                f"{config.server_id}: local alias {alias!r} would be shared by {owner!r} and "
                f"{descriptor.name!r}; refusing instead of overwriting one definition with another"
            )
        aliases[alias] = descriptor.name
        try:
            schema = RemoteToolSchema.register(
                descriptor.name,
                descriptor.input_schema,
                max_bytes=config.limits.max_schema_bytes,
            ).with_limits(max_argument_bytes=config.limits.max_message_bytes)
        except McpSchemaError as exc:
            raise McpSchemaError(
                f"{config.server_id}: the host bound {descriptor.name!r} but its schema is unusable: {exc}"
            ) from exc
        entry = CatalogEntry(
            alias=alias,
            remote_name=descriptor.name,
            description=descriptor.description,
            title=descriptor.title,
            schema=schema,
            output_schema=dict(descriptor.output_schema) if descriptor.output_schema else None,
            annotations=dict(descriptor.annotations),
            binding=binding,
            version=definition_version(
                config=config, descriptor=descriptor, schema=schema, binding=binding
            ),
        )
        entries.append(entry)

    snapshot = CatalogSnapshot(
        server_id=config.server_id,
        protocol=config.protocol,
        authorization_scope=config.authorization_scope,
        config_revision=config.config_revision,
        transport=config.transport.value,
        entries=tuple(entries),
        exclusions=tuple(exclusions),
        complete=complete,
        pages_read=pages_read,
    )
    fingerprint = "sha256:" + hashlib.sha256(
        _canonical(
            {
                "version": 1,
                "server_id": snapshot.server_id,
                "protocol": snapshot.protocol,
                "config_revision": snapshot.config_revision,
                "authorization_scope": snapshot.authorization_scope,
                "complete": snapshot.complete,
                "tools": [
                    {"alias": entry.alias, "remote_tool": entry.remote_name, "version": entry.version}
                    for entry in snapshot.entries
                ],
            }
        ).encode("utf-8")
    ).hexdigest()
    return CatalogSnapshot(
        server_id=snapshot.server_id,
        protocol=snapshot.protocol,
        authorization_scope=snapshot.authorization_scope,
        config_revision=snapshot.config_revision,
        transport=snapshot.transport,
        entries=snapshot.entries,
        exclusions=snapshot.exclusions,
        complete=snapshot.complete,
        pages_read=snapshot.pages_read,
        fingerprint=fingerprint,
    )


async def discover_catalog(
    manager: Any,
    connection: Any,
    *,
    config: McpServerConfig,
    bindings: Mapping[str, McpToolBinding],
    timeout_seconds: float | None = None,
) -> CatalogSnapshot:
    """Read one server's directory and freeze it into a snapshot.

    A thin, explicit join: the manager owns the connection and the bounds, this module
    owns the freezing. Nothing here writes, and nothing here authorizes: a discovered
    tool only becomes an entry when the host's ``bindings`` name it.
    """
    directory = await manager.list_tools(connection, timeout_seconds=timeout_seconds)
    return build_snapshot(
        directory.descriptors,
        config=config,
        bindings=bindings,
        pages_read=directory.pages_read,
        complete=directory.complete,
    )


def freeze_binding(entry: CatalogEntry, snapshot: CatalogSnapshot) -> FrozenToolBinding:
    return FrozenToolBinding(
        alias=entry.alias,
        remote_name=entry.remote_name,
        version=entry.version,
        server_id=snapshot.server_id,
        protocol=snapshot.protocol,
        config_revision=snapshot.config_revision,
        authorization_scope=snapshot.authorization_scope,
        policy_revision=entry.binding.policy_revision,
    )


def check_binding(
    frozen: FrozenToolBinding,
    *,
    snapshot: CatalogSnapshot | None,
    bindings: Mapping[str, McpToolBinding] | None,
    protocol: str,
    config_revision: str,
    authorization_scope: str,
) -> BindingCheck:
    """Compare one frozen binding with the live catalog, before any dispatch.

    Ordering is deliberate: the deployment-level differences (protocol, config
    revision, authorization scope) are reported before the tool-level ones, because
    "the deployment changed" is a different decision from "this tool's schema changed".
    """
    if protocol != frozen.protocol:
        return BindingCheck(
            False,
            "protocol_changed",
            f"the run committed to protocol {frozen.protocol!r}, the deployment now speaks {protocol!r}",
        )
    if config_revision != frozen.config_revision:
        return BindingCheck(
            False,
            "config_revision_changed",
            f"the run committed to configuration revision {frozen.config_revision!r}, "
            f"the deployment is now at {config_revision!r}",
        )
    if authorization_scope != frozen.authorization_scope:
        return BindingCheck(
            False,
            "authorization_revoked",
            f"the run committed to authorization scope {frozen.authorization_scope!r}, "
            f"the deployment is now at {authorization_scope!r}",
        )
    if bindings is not None and frozen.remote_name not in bindings:
        return BindingCheck(
            False,
            "revoked",
            f"{frozen.remote_name!r} is no longer bound by the host",
        )
    if snapshot is None:
        return BindingCheck(False, "catalog_unavailable", "no current catalog snapshot to compare against")
    entry = snapshot.entry(frozen.alias)
    if entry is None:
        return BindingCheck(
            False,
            "missing",
            f"{frozen.remote_name!r} ({frozen.alias}) is not in the current catalog",
        )
    if entry.version != frozen.version:
        return BindingCheck(
            False,
            "changed",
            f"{frozen.remote_name!r} now has definition version {entry.version}, "
            f"the run committed to {frozen.version}",
        )
    if entry.binding.policy_revision != frozen.policy_revision:
        return BindingCheck(
            False,
            "policy_changed",
            f"policy revision moved from {frozen.policy_revision!r} to {entry.binding.policy_revision!r}",
        )
    return BindingCheck(True)
