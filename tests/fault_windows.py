"""A child process that dies at a chosen point in the tool-call path.

Each mode kills the process with ``os._exit`` at a specific phase, which is the
only way to produce the states the durability contract is about:

``intent-then-die``
    the intent is on disk, the handler never ran.
``effect-then-die``
    the handler ran and left an external trace (a marker file), the process died
    before the receipt was settled. This is the case where the *only* honest
    answer is "unknown".
``settled-then-die``
    the whole call completed; a restarted process must replay the receipt instead
    of running the handler a second time.
``events-then-die``
    run state and its events were committed in one transaction, the process died
    before the fanout.
``claim-then-die``
    a lease was claimed and the process died holding it.

The script prints one JSON object as its last line. Anything else on stdout is
diagnostics.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from yanxu_harness.runtime import Cancellation, Principal, RunContext, RunIdentity
from yanxu_harness.storage import SqliteExecutionBackend, SqlitePersistence, StoredEvent
from yanxu_harness.tools import (
    ConservativePolicy,
    InMemoryApprovalStore,
    InvocationStatus,
    ToolDefinition,
    ToolEffect,
    ToolGateway,
    ToolInvocation,
    ToolRegistry,
)

__all__ = ["main"]


def _context() -> RunContext:
    return RunContext(
        identity=RunIdentity(namespace="project-a", session_id="s1", run_id="r1", attempt_id="a1"),
        principal=Principal(subject="author"),
        cancellation=Cancellation(),
    )


def _validator(arguments: dict) -> dict:
    return {"path": arguments["path"]}


def _definition(handler) -> ToolDefinition:
    return ToolDefinition(
        "notes.write",
        "Write a note",
        effects=frozenset({ToolEffect.WRITE}),
        args_validator=_validator,
        handler=handler,
        preview=lambda _c, a: {"path": a["path"], "action": "replace"},
    )


def _calls_so_far(counter: Path) -> int:
    if not counter.exists():
        return 0
    return len([line for line in counter.read_text(encoding="utf-8").splitlines() if line.strip()])


def _counted_handler(marker: Path, counter: Path):
    """A handler that appends one line per call and leaves an external trace.

    It **appends**: the counter must survive across processes, because the claim
    being tested is "the handler ran exactly once in total", not "once in this
    process".
    """

    def handler(_context, arguments):
        with counter.open("a", encoding="utf-8") as handle:
            handle.write(f"{arguments['path']}\n")
        marker.write_text(f"wrote {arguments['path']}", encoding="utf-8")
        return {"written": arguments["path"]}

    return handler


def _gateway(store: Path, handler) -> ToolGateway:
    return ToolGateway(
        registry=ToolRegistry([_definition(handler)]),
        policy=ConservativePolicy(auto_write=True),
        approvals=InMemoryApprovalStore(),
        backend=SqliteExecutionBackend(store),
    )


def _invocation() -> ToolInvocation:
    return ToolInvocation(
        call_id="call-1",
        namespace="project-a",
        tool_name="notes.write",
        arguments={"path": "a.md"},
        run_id="r1",
    )


def main(argv: list[str]) -> int:
    mode, store_arg, marker_arg, counter_arg = argv[1], argv[2], argv[3], argv[4]
    store = Path(store_arg)
    marker = Path(marker_arg)
    counter = Path(counter_arg)

    if mode == "settled-then-die":
        gateway = _gateway(store, _counted_handler(marker, counter))
        result = asyncio.run(gateway.invoke(_invocation(), _context()))
        print(json.dumps({"status": result.status.value, "calls": _calls_so_far(counter)}))
        sys.stdout.flush()
        os._exit(0)

    if mode == "intent-then-die":
        # Record the intent, then die before the handler runs. The ledger must
        # show an open operation and no external trace.
        async def scenario() -> None:
            backend = SqliteExecutionBackend(store)
            from yanxu_harness.tools.execution import ToolIntent

            await backend.begin(
                ToolIntent(
                    operation_id="r1:call-1",
                    namespace="project-a",
                    tool_name="notes.write",
                    tool_version="1",
                    args_hash="sha256:deadbeef",
                    call_id="call-1",
                    run_id="r1",
                )
            )

        asyncio.run(scenario())
        os._exit(9)

    if mode == "effect-then-die":
        # The handler runs and leaves an external trace, then the process dies
        # before the receipt is settled: the honest state is "unknown".
        async def scenario() -> None:
            backend = SqliteExecutionBackend(store)
            from yanxu_harness.tools.execution import ToolIntent

            intent = ToolIntent(
                operation_id="r1:call-1",
                namespace="project-a",
                tool_name="notes.write",
                tool_version="1",
                args_hash="sha256:deadbeef",
                call_id="call-1",
                run_id="r1",
            )
            await backend.begin(intent)
            await backend.execute(intent, _definition(_counted_handler(marker, counter)), {"path": "a.md"}, _context())
            # No settle(): the crash stands in for "settlement never happened".

        asyncio.run(scenario())
        os._exit(9)

    if mode == "events-then-die":
        async def scenario() -> None:
            persistence = SqlitePersistence(store)
            version, written = await persistence.commit_transition(
                "r1",
                {"turn": 1, "status": "suspended"},
                expected_version=None,
                events=[
                    StoredEvent(sequence=0, kind="run_started", payload={"kind": "run_started"}, run_id="r1"),
                    StoredEvent(sequence=0, kind="run_suspended", payload={"kind": "run_suspended"}, run_id="r1"),
                ],
            )
            print(json.dumps({"version": version, "sequences": [e.sequence for e in written]}))

        asyncio.run(scenario())
        sys.stdout.flush()
        os._exit(9)

    if mode == "claim-then-die":
        async def scenario() -> None:
            persistence = SqlitePersistence(store)
            lease = await persistence.claim_lease("run:r1", "worker-a", ttl_seconds=300)
            print(json.dumps({"holder": lease.holder, "fence": lease.fencing_token}))

        asyncio.run(scenario())
        sys.stdout.flush()
        os._exit(9)

    print(json.dumps({"error": f"unknown mode {mode!r}"}))
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
