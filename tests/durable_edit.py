"""One deployment that a parent test and a child process can both build.

The harness's durability claims are about *processes*, so the recovery tests must
be able to construct the same arrangement twice: once in a child that pauses and
dies, once in a child that reads the pause from disk and resumes. Sharing this
builder is what makes "the same deployment" a fact rather than a hope — the tool,
its schema, the system prompt and the run identity are identical by construction,
so a fingerprint comparison in the resume path is comparing the right things.

The three stores live in three files on purpose: a run store, an operation ledger
and an approval ledger are different connections, and the package explicitly does
not claim they share a transaction. The recovery logic has to work from references
and receipts, and these tests are where that is exercised.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from yanxu_harness.core.types import (
    AssistantMessage,
    StopReason,
    TextContent,
    ToolCall,
)
from yanxu_harness.runtime import DefaultHarness, Principal, RunContext, RunIdentity
from yanxu_harness.storage import (
    SqliteApprovalStore,
    SqliteExecutionBackend,
    SqlitePersistence,
)
from yanxu_harness.tools import (
    ConservativePolicy,
    ExecutionMode,
    GatewayBindings,
    ToolDefinition,
    ToolEffect,
    ToolGateway,
    ToolRegistry,
    core_tool,
)

__all__ = [
    "DeploymentPaths",
    "PARTIAL_TOOL_NAMES",
    "ScriptedStream",
    "build",
    "build_partial",
    "partial_batch_message",
    "final_message",
    "run_context",
    "tool_call_message",
    "write_calls",
    "write_definition",
]

NAMESPACE = "project-a"
RUN_ID = "r1"
SESSION_ID = "s1"
TOOL_NAME = "notes.write"


@dataclass(frozen=True)
class DeploymentPaths:
    """Where the three ledgers live, plus the external trace a handler leaves."""

    runs: Path
    operations: Path
    approvals: Path
    counter: Path

    @classmethod
    def under(cls, directory: Path) -> "DeploymentPaths":
        directory.mkdir(parents=True, exist_ok=True)
        return cls(
            runs=directory / "runs.db",
            operations=directory / "operations.db",
            approvals=directory / "approvals.db",
            counter=directory / "write-calls.log",
        )


def run_context(attempt: str = "a1") -> RunContext:
    return RunContext(
        identity=RunIdentity(
            namespace=NAMESPACE, session_id=SESSION_ID, run_id=RUN_ID, attempt_id=attempt
        ),
        principal=Principal(subject="author"),
    )


def tool_call_message(text: str = "hello") -> AssistantMessage:
    return AssistantMessage(
        content=[ToolCall(id="call-1", name=TOOL_NAME, arguments={"path": "a.md", "text": text})],
        stop_reason=StopReason.TOOL_USE,
    )


def final_message(text: str = "写好了。") -> AssistantMessage:
    return AssistantMessage(content=[TextContent(text=text)], stop_reason=StopReason.STOP)


class ScriptedStream:
    """A ``stream_fn`` that answers with one message per request.

    The last message repeats, so a runaway loop is a visible failure (the run would
    keep making requests) rather than a hang.
    """

    def __init__(self, *messages: AssistantMessage) -> None:
        if not messages:
            raise ValueError("a script needs at least one message")
        self.messages = list(messages)
        self.calls = 0

    async def __call__(self, model: Any, context: Any, options: Any = None) -> "ScriptedStream":
        self.calls += 1
        self._final = self.messages[min(self.calls - 1, len(self.messages) - 1)]
        return self

    def __aiter__(self) -> "ScriptedStream":
        return self

    async def __anext__(self) -> Mapping[str, Any]:
        raise StopAsyncIteration

    async def result(self) -> AssistantMessage:
        return self._final


def write_calls(counter: Path) -> int:
    """How many times the write handler ran, in total, across processes."""
    if not counter.exists():
        return 0
    return len([line for line in counter.read_text(encoding="utf-8").splitlines() if line.strip()])


def write_definition(
    counter: Path, handler: Callable[..., Any] | None = None
) -> ToolDefinition:
    """The one write tool both processes share.

    ``handler`` lets a crash test replace the effect handler without changing the tool's
    name, schema, effects or preview, so the fingerprints of the two deployments still
    match and a resume is comparing the same arrangement.
    """

    def counted(context: Any, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        # Appends: the claim under test is "once in total", not "once in this process".
        with counter.open("a", encoding="utf-8") as handle:
            handle.write(f"{context.identity.attempt_id}:{arguments['text']}\n")
        return {
            "content": [TextContent(text=f"wrote {arguments['text']}")],
            "details": {"bytes": len(arguments["text"])},
        }

    return ToolDefinition(
        TOOL_NAME,
        "append text to a note",
        effects=frozenset({ToolEffect.WRITE}),
        args_validator=lambda arguments: {
            "path": arguments["path"],
            "text": arguments["text"],
        },
        handler=handler if handler is not None else counted,
        preview=lambda _context, arguments: {
            "path": arguments["path"],
            "text": arguments["text"],
        },
    )


class UnprovenReceiptBackend:
    """A real ledger that reports every effect as unproven.

    The crash window it stands in for is "the request reached the outside world and
    the receipt was never written": the effect may or may not have happened, and the
    only honest state is ``unknown``.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.ownership = inner.ownership
        self.durable = inner.durable
        self.durability = inner.durability

    async def begin(self, intent: Any) -> Any:
        return await self._inner.begin(intent)

    async def execute(self, intent: Any, definition: Any, arguments: Any, context: Any) -> Any:
        from dataclasses import replace

        from yanxu_harness.tools import EffectState

        receipt = await self._inner.execute(intent, definition, arguments, context)
        return replace(receipt, state=EffectState.UNKNOWN, detail="the outcome could not be proven")

    async def settle(self, receipt: Any) -> None:
        await self._inner.settle(receipt)

    def receipt(self, operation_id: str) -> Any:
        return self._inner.receipt(operation_id)

    def unsettled(self, **kwargs: Any) -> Any:
        return self._inner.unsettled(**kwargs)

    def counts_by_state(self) -> Any:
        return self._inner.counts_by_state()

    def close(self) -> None:
        self._inner.close()


def build(
    paths: DeploymentPaths,
    stream: Any,
    *,
    attempt: str = "a1",
    reconcile: Callable[..., Any] | None = None,
    unknown_effects: bool = False,
    auto_write: bool = False,
    handler: Callable[..., Any] | None = None,
    run_listener: Callable[..., Any] | None = None,
    clock: Callable[[], float] | None = None,
    holder: str | None = None,
    lease_ttl_seconds: float = 300.0,
    context_controller: Any = None,
    suspend_after_batch: Any = None,
    deployment_binding: Any = None,
) -> tuple[DefaultHarness, Any]:
    """Build the deployment both processes share. Returns the harness and the gateway."""
    definition = write_definition(paths.counter, handler)
    approvals = SqliteApprovalStore(paths.approvals)
    inner = SqliteExecutionBackend(paths.operations)
    backend = UnprovenReceiptBackend(inner) if unknown_effects else inner
    gateway = ToolGateway(
        registry=ToolRegistry([definition]),
        policy=ConservativePolicy(auto_write=auto_write),
        approvals=approvals,
        backend=backend,
    )
    bindings = GatewayBindings(
        gateway=gateway,
        # Resolved per call, so a resumed attempt is what the handler records.
        context=lambda: run_context(attempt),
    )
    harness = DefaultHarness(
        stream_fn=stream,
        tools=[core_tool(definition, bindings)],
        persistence=SqlitePersistence(paths.runs),
        system_prompt="be brief",
        reconcile=reconcile,
        run_listener=run_listener,
        # A recovery test may sit on the far side of a dead worker's lease; it does that
        # by moving this clock, or by claiming with a short TTL and waiting bounded time.
        # Production code is not changed to make a fence testable.
        clock=clock if clock is not None else time.time,
        holder=holder,
        lease_ttl_seconds=lease_ttl_seconds,
        context_controller=context_controller,
        suspend_after_batch=suspend_after_batch,
        deployment_binding=deployment_binding,
    )
    return harness, gateway


#: Three calls in one batch, used by the partial-settlement and recovery tests: A can
#: settle, B can be left unproven, C need never be dispatched. The names are fixed so a
#: parent process builds exactly the deployment the child used.
PARTIAL_TOOL_NAMES = ("notes.a", "notes.b", "notes.c")
#: One more call, for the recovery tests that need a call to *complete* and a later one to
#: block in the same invocation: A settles, B is proven by evidence, C runs, D blocks.
PARTIAL_FOUR_TOOL_NAMES = ("notes.a", "notes.b", "notes.c", "notes.d")


def partial_message(names: Sequence[str]) -> AssistantMessage:
    return AssistantMessage(
        content=[
            ToolCall(id=f"c-{name}", name=name, arguments={"path": "a.md"}) for name in names
        ],
        stop_reason=StopReason.TOOL_USE,
    )


def partial_batch_message() -> AssistantMessage:
    return partial_message(PARTIAL_TOOL_NAMES)


def partial_definitions(
    counter: Path,
    handlers: Mapping[str, Callable[..., Any]] | None = None,
    names: Sequence[str] = PARTIAL_TOOL_NAMES,
) -> list[ToolDefinition]:
    """Sequential read tools, each leaving one append-only trace line per call."""

    def make(name: str) -> ToolDefinition:
        def counted(context: Any, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
            with counter.open("a", encoding="utf-8") as handle:
                handle.write(f"{context.identity.attempt_id}:{name}:{arguments['path']}\n")
            return {
                "content": [TextContent(text=f"{name} did {arguments['path']}")],
                "details": {"tool": name},
            }

        return ToolDefinition(
            name,
            f"{name} test tool",
            args_validator=lambda arguments: {"path": arguments["path"]},
            handler=(handlers or {}).get(name, counted),
            # Sequential, so the batch's calls are dispatched one at a time and a crash
            # inside one of them leaves an unambiguous prefix of settled calls.
            execution_mode=ExecutionMode.SEQUENTIAL,
        )

    return [make(name) for name in names]


def build_partial(
    paths: DeploymentPaths,
    stream: Any,
    *,
    attempt: str = "a1",
    handlers: Mapping[str, Callable[..., Any]] | None = None,
    reconcile: Callable[..., Any] | None = None,
    clock: Callable[[], float] | None = None,
    names: Sequence[str] = PARTIAL_TOOL_NAMES,
    deployment_binding: Any = None,
) -> tuple[DefaultHarness, Any]:
    """The multi-call deployment a parent and a child can both build."""
    definitions = partial_definitions(paths.counter, handlers, names)
    gateway = ToolGateway(
        registry=ToolRegistry(definitions),
        policy=ConservativePolicy(auto_write=True),
        approvals=SqliteApprovalStore(paths.approvals),
        backend=SqliteExecutionBackend(paths.operations),
    )
    bindings = GatewayBindings(gateway=gateway, context=lambda: run_context(attempt))
    harness = DefaultHarness(
        stream_fn=stream,
        tools=[core_tool(definition, bindings) for definition in definitions],
        persistence=SqlitePersistence(paths.runs),
        system_prompt="be brief",
        reconcile=reconcile,
        clock=clock if clock is not None else time.time,
        deployment_binding=deployment_binding,
    )
    return harness, gateway
