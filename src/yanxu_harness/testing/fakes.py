"""Self-contained test doubles: a scripted provider and a recording backend.

These exist so that *this package's* tests, and any host's adapter tests, can
exercise the contracts without a network, a database or a real model. They are
deliberately strict: the scripted provider emits exactly the frames it was given
(including private ones, so a test can prove they are dropped) and the recording
backend keeps the full intent/receipt history, so a test can assert both the
number of effects and their order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Iterable

from ..errors import ContractError
from ..providers.protocol import (
    ProviderConfig,
    ProviderEvent,
    ProviderEventKind,
    ProviderRequest,
)
from ..runtime.context import Cancellation
from ..tools.definition import ToolDefinition
from ..tools.execution import EffectState, ToolIntent, ToolReceipt, classify_failure

__all__ = ["RecordingExecutionBackend", "ScriptedProvider", "failing_handler"]


class ScriptedProvider:
    """A provider that replays a fixed script of neutral frames."""

    name = "scripted"

    def __init__(
        self,
        events: Iterable[ProviderEvent],
        config: ProviderConfig | None = None,
    ) -> None:
        self.events: tuple[ProviderEvent, ...] = tuple(events)
        self.config = config or ProviderConfig(model="scripted")
        self.calls = 0
        self.requests: list[ProviderRequest] = []

    async def stream(
        self, request: ProviderRequest, cancellation: Cancellation | None = None
    ) -> AsyncIterator[ProviderEvent]:
        self.calls += 1
        self.requests.append(request)
        for frame in self.events:
            if cancellation is not None and cancellation.cancelled:
                yield ProviderEvent(
                    kind=ProviderEventKind.ERROR, failure_detail="cancelled"
                )
                return
            yield frame


@dataclass
class RecordingExecutionBackend:
    """A minimal backend that records intents and receipts in order.

    It never reaches the outside world, so :meth:`execute` can be exercised in a
    test without any risk: the handler is invoked exactly once per intent and the
    receipt records what the handler returned or raised.
    """

    state: EffectState = EffectState.SUCCEEDED
    ownership: str = "harness"
    durable: bool = False
    intents: list[ToolIntent] = field(default_factory=list)
    receipts: list[ToolReceipt] = field(default_factory=list)
    settled: list[ToolReceipt] = field(default_factory=list)

    async def begin(self, intent: ToolIntent) -> ToolReceipt | None:
        self.intents.append(intent)
        for receipt in self.receipts:
            if receipt.operation_id == intent.operation_id:
                return receipt
        return None

    async def execute(
        self,
        intent: ToolIntent,
        definition: ToolDefinition,
        arguments: Any,
        context: Any,
    ) -> ToolReceipt:
        try:
            result = await definition.call(context, arguments)
        except BaseException as exc:  # noqa: BLE001 - classifying it is the point
            state, detail = classify_failure(exc)
            receipt = ToolReceipt(
                operation_id=intent.operation_id,
                state=state,
                detail=detail,
                details=dict(getattr(exc, "details", {}) or {}),
            )
        else:
            receipt = ToolReceipt(operation_id=intent.operation_id, state=self.state, result=result)
        self.receipts.append(receipt)
        return receipt

    async def settle(self, receipt: ToolReceipt) -> None:
        self.settled.append(receipt)

    def result_for(self, operation_id: str) -> Any:
        for receipt in self.receipts:
            if receipt.operation_id == operation_id and receipt.state is EffectState.SUCCEEDED:
                return receipt.result
        raise ContractError(f"{operation_id}: no succeeded receipt recorded")


def failing_handler(exc: BaseException):
    """Build a handler that always raises ``exc`` (for classification tests)."""

    def handler(_context: Any, _arguments: Any) -> Any:
        raise exc

    return handler
