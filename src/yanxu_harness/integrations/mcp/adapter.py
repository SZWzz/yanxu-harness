"""适配层：把「一个已冻结的 MCP 工具」接到既有 Gateway 上。

它是 MCP 与 harness 之间唯一的执行接线，做四件事：

1. 用 ``catalog.tools.build_definitions`` 生成真的 ``ToolDefinition``（真 validator、
   写工具同步 preview）；
2. handler 只做一次远端调用（``manager.call_tool`` → 公开低层 ``ClientSession``），
   期限取「配置上限」与宿主给出的 Run 剩余期限的较小值；
3. 把结果交给 codec 变成中立 ``ToolExecutionResult``，把调用事实放进 receipt details；
4. 把可证明没派发的失败、可能已到达的失败分开：前者 FAILED + ``not_sent``，
   后者 UNKNOWN 并等待业务证据。

取消是控制信号：**派发前**取消直接抛 ``ToolCancelled``（不会产生 intent 之后的效果）；
**派发后**的取消/超时不能证明远端没干活，按 UNKNOWN 归类，交给既有 unknown 屏障。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

from ...tools.definition import ToolDefinition
from ...tools.execution import EffectState, ToolExecutionResult
from ...tools.outcomes import ToolCancelled
from .catalog import BindingCheck, CatalogEntry, CatalogSnapshot, check_binding, freeze_binding
from .client import McpClientManager
from .config import McpServerConfig
from .errors import McpCallFailure, McpDriftRefusal, McpError, McpNotConnected, McpNotSent
from .results import SecretRedactor, codec_result
from .types import McpCallFacts, McpConnection, McpCallResult, McpToolDirectory  # noqa: F401  (documented re-export for hosts)

__all__ = ["McpToolAdapter"]

#: A host that knows the run's remaining budget may pass it through this context
#: label; the adapter then takes the smaller of it and the configured call timeout.
DEADLINE_LABEL = "remaining_deadline_seconds"


@dataclass(frozen=True)
class McpToolAdapter:
    """One server's frozen catalog, wired to a manager connection."""

    manager: McpClientManager
    connection: McpConnection
    config: McpServerConfig
    snapshot: CatalogSnapshot
    #: Optional explicit deadline source; `None` uses the context label.
    deadline_seconds: Callable[[Any], float | None] | None = field(default=None)

    def __post_init__(self) -> None:
        # Install this snapshot as the connection scope's baseline *only* when the
        # manager has none. A newer discovery is never overwritten by an older adapter.
        adopt = getattr(self.manager, "adopt_catalog", None)
        if callable(adopt):
            adopt(
                self.connection,
                self.snapshot,
                {entry.remote_name: entry.binding for entry in self.snapshot.entries},
            )

    # -- catalog identity ------------------------------------------------

    def catalog_check(self, entry: CatalogEntry | None = None) -> BindingCheck:
        """Compare this adapter's frozen snapshot with the currently trusted catalog.

        This is the adapter's own promise, independent of any harness-level arrangement
        check: a snapshot that discovery has replaced is no longer dispatchable, even
        while the tool *definitions* built from it still look unchanged to a host.
        """
        snapshot = self.snapshot
        config = self.config
        connection = self.connection
        # Self-consistency first: an adapter must never be able to disagree with the
        # connection and configuration it was constructed for.
        if (
            snapshot.server_id != config.server_id
            or snapshot.server_id != connection.server_id
            or snapshot.protocol != config.protocol
            or snapshot.protocol != connection.protocol
            or snapshot.config_revision != config.config_revision
            or snapshot.config_revision != connection.config_revision
            or snapshot.authorization_scope != config.authorization_scope
            or snapshot.authorization_scope != connection.authorization_scope
            or snapshot.transport != config.transport.value
            or snapshot.transport != connection.transport.value
        ):
            return BindingCheck(
                False,
                "binding_inconsistent",
                "the adapter's snapshot, configuration and connection disagree about the "
                "server, protocol, configuration revision, authorization scope or transport",
            )
        if entry is None:
            if not snapshot.entries:
                return BindingCheck(True)
            entry = snapshot.entries[0]
        # The entry must be the one this adapter froze. Handing an adapter a fresh
        # entry from another snapshot would otherwise let the caller pull a new
        # definition through an old adapter, which is precisely the silent upgrade the
        # frozen binding exists to prevent.
        frozen_entry = snapshot.entry(entry.alias)
        if (
            frozen_entry is None
            or frozen_entry.version != entry.version
            or frozen_entry.remote_name != entry.remote_name
        ):
            return BindingCheck(
                False,
                "binding_inconsistent",
                f"{entry.remote_name!r} is not the definition this adapter froze "
                f"(adapter version {getattr(frozen_entry, 'version', None)!r}, "
                f"entry version {entry.version!r})",
            )
        frozen = freeze_binding(entry, snapshot)
        state_for = getattr(self.manager, "catalog_state", None)
        current = state_for(connection) if callable(state_for) else None
        if current is None:
            # No observation was published and no baseline could be adopted: there is
            # nothing to compare, which is not the same as a proven match. Refusing is
            # the conservative reading ("I could not check" is not "it is fine").
            return BindingCheck(
                False,
                "catalog_unavailable",
                "no trusted catalog observation exists for this connection scope",
            )
        if current.invalidated:
            return BindingCheck(False, "invalidated", current.invalidated)
        return check_binding(
            frozen,
            snapshot=current.snapshot,
            bindings=current.bindings,
            protocol=current.snapshot.protocol,
            config_revision=current.snapshot.config_revision,
            authorization_scope=current.snapshot.authorization_scope,
        )

    def _dispatch_guard(self, entry: CatalogEntry) -> Callable[[Mapping[str, Any]], str | None]:
        """A sync, no-I/O guard a host can run before dispatching (or resuming) a call."""

        def guard(payload: Mapping[str, Any]) -> str | None:
            check = self.catalog_check(entry)
            if check.ok:
                return None
            call_id = str(dict(payload or {}).get("call_id") or entry.alias)
            return f"{call_id}: {entry.remote_name}: {check.code}: {check.reason}"

        return guard

    # -- definitions -----------------------------------------------------

    def definitions(self, *, only: Iterable[str] | None = None) -> tuple[ToolDefinition, ...]:
        """The gateway-routed definitions for this snapshot (optionally a subset)."""
        return build_definitions_compat(
            self.snapshot,
            lambda entry: self._handler(entry),
            guard_for=lambda entry: self._dispatch_guard(entry),
            only=only,
        )

    def _handler(self, entry: CatalogEntry) -> Callable[[Any, Any], Any]:
        async def handler(context: Any, arguments: Any) -> ToolExecutionResult:
            return await self.invoke(entry, context, arguments)

        return handler

    # -- execution -------------------------------------------------------

    async def invoke(self, entry: CatalogEntry, context: Any, arguments: Mapping[str, Any]) -> ToolExecutionResult:
        """One remote call, one neutral result."""
        if _cancelled(context):
            raise ToolCancelled("run cancelled before dispatch")
        # The frozen binding is re-checked against the connection owner's current
        # trusted catalog *before* the first byte is sent. A catalog refresh, an
        # invalidation, a revoked binding or a changed definition version all stop here.
        check = self.catalog_check(entry)
        if not check.ok:
            raise McpDriftRefusal(f"{check.code}: {check.reason}")
        timeout = self._timeout_for(context)
        redactor = SecretRedactor.from_config(self.config)
        started = time.monotonic()
        try:
            answer = await self.manager.call_tool(
                self.connection, entry.remote_name, arguments, timeout_seconds=timeout
            )
        except McpNotSent as exc:
            return self._failure(
                entry, started, redactor.text(str(exc)), state=EffectState.FAILED, dispatched=False,
                redactor=redactor,
            )
        except McpNotConnected as exc:
            return self._failure(
                entry, started, redactor.text(str(exc)), state=EffectState.FAILED, dispatched=False,
                redactor=redactor,
            )
        except McpCallFailure as exc:
            state = EffectState.UNKNOWN if exc.may_have_reached_server else EffectState.FAILED
            return self._failure(
                entry,
                started,
                redactor.text(str(exc)),
                state=state,
                dispatched=exc.may_have_reached_server,
                redactor=redactor,
            )
        except McpError as exc:
            # Anything else the transport raised while a call was in flight: the request
            # may have reached the server, so this is not a proven failure.
            return self._failure(
                entry, started, redactor.text(str(exc)), state=EffectState.UNKNOWN, dispatched=True,
                redactor=redactor,
            )

        # Everything below derives from remote bytes. Clean them at the only boundary
        # where the host's known credentials are still identifiable, so the neutral
        # result, the ledger receipt, events and the model projection all inherit the
        # cleaned value instead of each having to remember to scan.
        answer = redactor.call_result(answer)
        facts = self._facts(entry, started, dispatched=True, result_source="remote")
        codec = codec_result(answer, facts=facts, mutating=entry.binding.mutating)
        # The catalog identity travels with the result, so a receipt carries "which frozen
        # definition produced this" without a second ledger.
        return redactor.execution_result(
            ToolExecutionResult(
                state=codec.state,
                result=codec.result,
                detail=codec.detail,
                details={**entry.detail, **dict(codec.details)},
                external_id=codec.external_id,
            )
        )

    def _failure(
        self,
        entry: CatalogEntry,
        started: float,
        detail: str,
        *,
        state: EffectState,
        dispatched: bool,
        redactor: SecretRedactor,
    ) -> ToolExecutionResult:
        facts = self._facts(entry, started, dispatched=dispatched, result_source="client")
        details: dict[str, Any] = {**entry.detail, **facts.as_details()}
        if not dispatched:
            details["not_sent"] = True
        return redactor.execution_result(
            ToolExecutionResult(state=state, detail=detail, details=details)
        )

    def _facts(
        self, entry: CatalogEntry, started: float, *, dispatched: bool, result_source: str
    ) -> McpCallFacts:
        return McpCallFacts(
            connection_id=self.connection.connection_id,
            generation=self.connection.generation,
            transport=self.config.transport.value,
            protocol=self.config.protocol,
            remote_name=entry.remote_name,
            duration_seconds=max(0.0, time.monotonic() - started),
            dispatched=dispatched,
            result_source=result_source,
        )

    def _timeout_for(self, context: Any) -> float | None:
        if self.deadline_seconds is not None:
            return self.deadline_seconds(context)
        label = getattr(context, "label", None)
        if callable(label):
            value = label(DEADLINE_LABEL)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                return float(value)
        return None


def _cancelled(context: Any) -> bool:
    cancellation = getattr(context, "cancellation", None)
    return bool(getattr(cancellation, "cancelled", False))


def build_definitions_compat(snapshot, handler_for, *, only=None, guard_for=None):
    """Local import indirection so this module stays free of import cycles."""
    from .tools import build_definitions

    return build_definitions(snapshot, handler_for, only=only, guard_for=guard_for)
