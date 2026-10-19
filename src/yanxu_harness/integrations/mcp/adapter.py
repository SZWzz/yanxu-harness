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
from .catalog import CatalogEntry, CatalogSnapshot
from .client import McpClientManager
from .config import McpServerConfig
from .errors import McpCallFailure, McpError, McpNotConnected, McpNotSent
from .results import codec_result
from .types import McpCallFacts, McpConnection, McpCallResult
from .types import McpToolDirectory  # noqa: F401  (documented re-export for hosts)

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

    # -- definitions -----------------------------------------------------

    def definitions(self, *, only: Iterable[str] | None = None) -> tuple[ToolDefinition, ...]:
        """The gateway-routed definitions for this snapshot (optionally a subset)."""
        return build_definitions_compat(self.snapshot, lambda entry: self._handler(entry), only=only)

    def _handler(self, entry: CatalogEntry) -> Callable[[Any, Any], Any]:
        async def handler(context: Any, arguments: Any) -> ToolExecutionResult:
            return await self.invoke(entry, context, arguments)

        return handler

    # -- execution -------------------------------------------------------

    async def invoke(self, entry: CatalogEntry, context: Any, arguments: Mapping[str, Any]) -> ToolExecutionResult:
        """One remote call, one neutral result."""
        if _cancelled(context):
            raise ToolCancelled("run cancelled before dispatch")
        timeout = self._timeout_for(context)
        started = time.monotonic()
        try:
            answer = await self.manager.call_tool(
                self.connection, entry.remote_name, arguments, timeout_seconds=timeout
            )
        except McpNotSent as exc:
            return self._failure(entry, started, str(exc), state=EffectState.FAILED, dispatched=False)
        except McpNotConnected as exc:
            return self._failure(entry, started, str(exc), state=EffectState.FAILED, dispatched=False)
        except McpCallFailure as exc:
            state = EffectState.UNKNOWN if exc.may_have_reached_server else EffectState.FAILED
            return self._failure(
                entry, started, str(exc), state=state, dispatched=exc.may_have_reached_server
            )
        except McpError as exc:
            # Anything else the transport raised while a call was in flight: the request
            # may have reached the server, so this is not a proven failure.
            return self._failure(entry, started, str(exc), state=EffectState.UNKNOWN, dispatched=True)

        facts = self._facts(entry, started, dispatched=True, result_source="remote")
        codec = codec_result(answer, facts=facts, mutating=entry.binding.mutating)
        # The catalog identity travels with the result, so a receipt carries "which frozen
        # definition produced this" without a second ledger.
        return ToolExecutionResult(
            state=codec.state,
            result=codec.result,
            detail=codec.detail,
            details={**entry.detail, **dict(codec.details)},
            external_id=codec.external_id,
        )

    def _failure(
        self,
        entry: CatalogEntry,
        started: float,
        detail: str,
        *,
        state: EffectState,
        dispatched: bool,
    ) -> ToolExecutionResult:
        facts = self._facts(entry, started, dispatched=dispatched, result_source="client")
        details: dict[str, Any] = {**entry.detail, **facts.as_details()}
        if not dispatched:
            details["not_sent"] = True
        return ToolExecutionResult(state=state, detail=detail, details=details)

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


def build_definitions_compat(snapshot, handler_for, *, only=None):
    """Local import indirection so this module stays free of import cycles."""
    from .tools import build_definitions

    return build_definitions(snapshot, handler_for, only=only)
