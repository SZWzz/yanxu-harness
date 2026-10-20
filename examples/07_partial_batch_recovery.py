"""Example 7 — recovering a batch per call, not per batch.

    python examples/07_partial_batch_recovery.py

A model declares three calls: A, B and C. A finishes, B starts and the process dies,
C never runs. The parent process then has to answer, for each call separately:

1. read the run store and the operation ledger and print what they actually say;
2. recover the batch — and stop, because B's outcome is unproven. A is not re-run and
   C is *not* dispatched while B's question is open (the conservative barrier);
3. answer B with business evidence, then continue: C runs exactly once, A and B are
   replayed from what was already proven.

Nothing here is simulated. The crashing step is a real child process that exits with
``os._exit``, and every state is read out of real SQLite files.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TOOLS = ("notes.a", "notes.b", "notes.c")
RUN_ID = "r1"
#: The model action the batch was declared in. It is part of the operation id the
#: gateway derives, so a later turn of the same run that reused a call id would be a
#: different operation rather than a replay of this one.
MODEL_ACTION = "request:1"


def _operation(call_id: str) -> str:
    return f"example:s1:r1:{MODEL_ACTION}:{call_id}"


def _paths(directory: Path) -> tuple[Path, Path, Path, Path]:
    return (
        directory / "runs.db",
        directory / "operations.db",
        directory / "approvals.db",
        directory / "calls.log",
    )


def _deployment(directory: Path, *, attempt: str, stream, reconcile=None, clock=None):
    """The same three-call deployment the child ran and the parent resumes.

    Sequential on purpose: A, B and C are dispatched one at a time, so what the crash
    left behind is an unambiguous prefix (A settled, B open, C untouched).
    """
    import time

    from yanxu_harness.core.types import TextContent
    from yanxu_harness.runtime import (
        DefaultHarness,
        Principal,
        RunContext,
        RunIdentity,
    )
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
        ToolGateway,
        ToolRegistry,
        core_tool,
    )

    runs, operations, approvals, counter = _paths(directory)

    def make(name: str) -> ToolDefinition:
        def handler(context, arguments):
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
            handler=handler,
            execution_mode=ExecutionMode.SEQUENTIAL,
        )

    definitions = [make(name) for name in TOOLS]
    approval_store = SqliteApprovalStore(approvals)
    execution_backend = SqliteExecutionBackend(operations)
    persistence = SqlitePersistence(runs)
    gateway = ToolGateway(
        registry=ToolRegistry(definitions),
        policy=ConservativePolicy(auto_write=True),
        approvals=approval_store,
        backend=execution_backend,
    )
    bindings = GatewayBindings(gateway=gateway, context=lambda: _context(attempt))
    harness = DefaultHarness(
        stream_fn=stream,
        tools=[core_tool(definition, bindings) for definition in definitions],
        persistence=persistence,
        system_prompt="be brief",
        reconcile=reconcile,
        clock=clock if clock is not None else time.time,
    )
    return harness, gateway, (persistence, execution_backend, approval_store)


def _close_deployment(stores) -> None:
    for store in stores:
        store.close()


def _context(attempt: str):
    from yanxu_harness.runtime import Principal, RunContext, RunIdentity

    return RunContext(
        identity=RunIdentity(
            namespace="example", session_id="s1", run_id=RUN_ID, attempt_id=attempt
        ),
        principal=Principal(subject="author"),
    )


def _three_calls():
    from yanxu_harness.core.types import AssistantMessage, StopReason, ToolCall

    return AssistantMessage(
        content=[
            ToolCall(id=f"c-{name}", name=name, arguments={"path": "a.md"}) for name in TOOLS
        ],
        stop_reason=StopReason.TOOL_USE,
    )


def _final(text: str):
    from yanxu_harness.core.types import AssistantMessage, StopReason, TextContent

    return AssistantMessage(content=[TextContent(text=text)], stop_reason=StopReason.STOP)


class _Stream:
    """One scripted answer per model request; the last one repeats."""

    def __init__(self, *messages) -> None:
        self.messages = list(messages)
        self.calls = 0

    async def __call__(self, model, context, options=None):
        self.calls += 1
        self._final = self.messages[min(self.calls - 1, len(self.messages) - 1)]
        return self

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def result(self):
        return self._final


def _run(coro):
    async def guard():
        return await asyncio.wait_for(coro, timeout=60)

    return asyncio.run(guard())


def _traces(counter: Path) -> list[str]:
    if not counter.exists():
        return []
    return [line for line in counter.read_text(encoding="utf-8").splitlines() if line.strip()]


def child(directory: Path) -> int:
    """The crashing step: A settles, B starts, the process dies inside B."""
    from yanxu_harness.runtime import RunRequest

    harness, _gateway = _deployment_with_dying_b(directory)
    try:
        _run(harness.start(RunRequest(prompt="do three"), _context("a1")))
    except BaseException:
        pass
    os._exit(9)


def _deployment_with_dying_b(directory: Path):
    """The child's deployment, with ``notes.b`` exiting the process from the handler."""
    from yanxu_harness.core.types import TextContent
    from yanxu_harness.runtime import DefaultHarness
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
        ToolGateway,
        ToolRegistry,
        core_tool,
    )

    runs, operations, approvals, counter = _paths(directory)

    def make(name: str) -> ToolDefinition:
        def handler(context, arguments):
            with counter.open("a", encoding="utf-8") as handle:
                handle.write(f"{context.identity.attempt_id}:{name}:{arguments['path']}\n")
            if name == "notes.b":
                os._exit(9)
            return {
                "content": [TextContent(text=f"{name} did {arguments['path']}")],
                "details": {"tool": name},
            }

        return ToolDefinition(
            name,
            f"{name} test tool",
            args_validator=lambda arguments: {"path": arguments["path"]},
            handler=handler,
            execution_mode=ExecutionMode.SEQUENTIAL,
        )

    definitions = [make(name) for name in TOOLS]
    gateway = ToolGateway(
        registry=ToolRegistry(definitions),
        policy=ConservativePolicy(auto_write=True),
        approvals=SqliteApprovalStore(approvals),
        backend=SqliteExecutionBackend(operations),
    )
    bindings = GatewayBindings(gateway=gateway, context=lambda: _context("a1"))
    harness = DefaultHarness(
        stream_fn=_Stream(_three_calls(), _final("unused")),
        tools=[core_tool(definition, bindings) for definition in definitions],
        persistence=SqlitePersistence(runs),
        system_prompt="be brief",
    )
    return harness, gateway


def parent(directory: Path) -> int:
    import time

    from yanxu_harness.core.execution import Phase, state_from_record
    from yanxu_harness.runtime.harness import RunRecord
    from yanxu_harness.storage import SqliteExecutionBackend, SqlitePersistence

    runs, operations, _, counter = _paths(directory)

    def read_record() -> RunRecord:
        store = SqlitePersistence(runs)
        try:
            stored = _run(store.load_run(RUN_ID))
        finally:
            store.close()
        assert stored is not None
        return RunRecord.from_record(stored.state)

    def ledger() -> dict[str, str]:
        backend = SqliteExecutionBackend(operations)
        try:
            return {str(item["operation_id"]): str(item["state"]) for item in backend.all_operations()}
        finally:
            backend.close()

    print("step: crash")
    traces = _traces(counter)
    assert traces == ["a1:notes.a:a.md", "a1:notes.b:a.md"], traces
    record = read_record()
    snapshot = state_from_record(record.execution)
    assert snapshot.phase is Phase.RUNNING_BATCH
    states = ledger()
    assert states[_operation("c-notes.a")] == "succeeded"
    assert states[_operation("c-notes.b")] == "running"
    assert _operation("c-notes.c") not in states
    print("  A settled and was committed; B is open; C never ran")

    print("step: recover-barrier")
    harness, _gateway, stores = _deployment(
        directory,
        attempt="a2",
        stream=_Stream(_final("unused")),
        clock=lambda: time.time() + 400.0,
    )
    try:
        outcome = _run(harness.resume(RUN_ID, {}, _context("a2")))
        assert outcome.status.value == "suspended", outcome.status
        interaction = dict(outcome.pending_interaction or {})
        assert interaction["kind"] == "reconciliation", interaction
        assert interaction["call_id"] == "c-notes.b", interaction
        assert _traces(counter) == traces, "nothing new ran behind the open question"
        assert _operation("c-notes.c") not in ledger(), "C waits for B's answer"
    finally:
        _close_deployment(stores)
    print("  resumed, then stopped: B is unproven, so A was not re-run and C was not started")

    print("step: evidence")
    asked: list[str] = []

    async def reconciler(call, details):
        asked.append(call.call_id)
        return {
            "proven": True,
            "state": "succeeded",
            "detail": "the note store shows B was written",
            "source": "business store",
        }

    harness, _gateway, stores = _deployment(
        directory,
        attempt="a3",
        stream=_Stream(_final("all done")),
        reconcile=reconciler,
        clock=lambda: time.time() + 800.0,
    )
    try:
        outcome = _run(harness.resume(RUN_ID, {}, _context("a3")))
        assert outcome.status.value == "completed", outcome.status
        assert asked == ["c-notes.b"], asked

        print("step: finish")
        assert _traces(counter) == [
            "a1:notes.a:a.md",
            "a1:notes.b:a.md",
            "a3:notes.c:a.md",
        ], _traces(counter)
        assert ledger()[_operation("c-notes.b")] == "succeeded"
        results = [
            message
            for message in read_record().messages
            if message.get("role") == "toolResult"
        ]
        assert [message["tool_call_id"] for message in results] == list(
            f"c-{name}" for name in TOOLS
        )
        print("  A replayed, B proven by evidence, C run exactly once; the model saw all three")
        print("OK: a partially settled batch was recovered call by call")
        print(f"state directory: {directory}")
    finally:
        _close_deployment(stores)
    return 0


def main(argv: list[str]) -> int:
    if len(argv) >= 3 and argv[1] == "--child":
        return child(Path(argv[2]))
    directory = Path(tempfile.mkdtemp(prefix="yanxu-partial-batch-"))
    try:
        completed = subprocess.run(
            [sys.executable, __file__, "--child", str(directory)],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if completed.returncode != 9:
            print(completed.stdout + completed.stderr, file=sys.stderr)
            raise SystemExit(f"the crashing child exited {completed.returncode}, expected 9")
        return parent(directory)
    finally:
        # Running it twice must not collide with itself, and no ledgers are left behind.
        shutil.rmtree(directory, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
