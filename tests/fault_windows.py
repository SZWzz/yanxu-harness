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

Two modes drive the *harness* rather than the gateway, because the claim they test
is about a whole run crossing a process boundary:

``approval-pause``
    a run reaches a write that needs approval, persists the pause and exits.
``resume-approved``
    a second process resumes that run; the approval is already in the ledger.

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

    if mode == "batch-dispatch-die":
        # A crash *inside* a tool call of a batch. The commit port wrote the state
        # before the batch was dispatched, so a fresh process can read what the model
        # asked for even though no outcome was ever recorded. Arguments: ``store`` is
        # the commit log (one JSON record per line), ``marker`` the effect log, and
        # ``counter`` is unused (this mode does not need a second counter file).
        from yanxu_harness.core.execution import state_to_record
        from yanxu_harness.core.loop_config import AgentLoopConfig
        from yanxu_harness.core.runner import run_agent_loop_result
        from yanxu_harness.core.types import (
            AgentContext,
            AssistantMessage,
            StopReason,
            TextContent,
            ToolCall,
            ToolSpec,
        )

        commit_log = store
        effect_log = marker

        def sink(state) -> None:
            with commit_log.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(state_to_record(state), ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())

        async def handler(call_id, arguments, signal=None, on_update=None):
            # The effect happens, then the process dies: the ledger has no receipt, so
            # the only honest conclusion is "check before you act".
            with effect_log.open("a", encoding="utf-8") as handle:
                handle.write(f"{call_id}:{arguments.get('path')}\n")
                handle.flush()
                os.fsync(handle.fileno())
            os._exit(9)

        class Stream:
            def __init__(self, message: AssistantMessage) -> None:
                self.message = message

            async def __call__(self, model, context, options=None):
                return self

            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

            async def result(self) -> AssistantMessage:
                return self.message

        tool = ToolSpec(
            name="notes.write",
            description="write a note",
            parameters={"type": "object"},
            execute=handler,
        )
        call = ToolCall(id="c1", name="notes.write", arguments={"path": "a.md"})
        message = AssistantMessage(
            content=[call], stop_reason=StopReason.TOOL_USE
        )
        config = AgentLoopConfig(
            stream_fn=Stream(message), tools=[tool], state_sink=sink
        )

        async def emit(_event) -> None:
            return None

        async def scenario() -> None:
            await run_agent_loop_result(
                [], AgentContext(messages=[], tools=[tool]), config, None, emit
            )

        asyncio.run(scenario())
        os._exit(0)  # unreachable: the handler exits the process

    if mode in {"approval-pause", "resume-approved", "resume-twice"}:
        # These modes take a directory instead of a single store file.
        from durable_edit import (
            DeploymentPaths,
            ScriptedStream,
            build,
            final_message,
            run_context,
            tool_call_message,
            write_calls,
        )
        from yanxu_harness.runtime import RunRequest

        directory = Path(store_arg)
        paths = DeploymentPaths.under(directory)

        async def scenario() -> dict:
            if mode == "approval-pause":
                harness, _gateway = build(
                    paths, ScriptedStream(tool_call_message("hello")), attempt="a1"
                )
                outcome = await harness.start(RunRequest(prompt="write it"), run_context("a1"))
                return {
                    "status": outcome.status.value,
                    "attempt": (await harness.state("r1")).attempt_id,
                    "binding_key": (outcome.pending_interaction or {}).get(
                        "approval_binding_key"
                    ),
                    "kind": (outcome.pending_interaction or {}).get("kind"),
                    "writes": write_calls(paths.counter),
                }
            # The resume modes are the second process: nothing about the pause is in
            # this interpreter's memory.
            harness, _gateway = build(
                paths, ScriptedStream(final_message("写好了。")), attempt="a2"
            )
            outcome = await harness.resume("r1", {}, run_context("a2"))
            result = {
                "status": outcome.status.value,
                "attempt": (await harness.state("r1")).attempt_id,
                "final_text": outcome.final_text,
                "writes": write_calls(paths.counter),
                "stop_code": outcome.stop_code,
            }
            if mode == "resume-twice":
                # A second resume of a finished run must be refused, and the refusal
                # must not touch the record.
                try:
                    await harness.resume("r1", {}, run_context("a3"))
                except Exception as exc:  # noqa: BLE001 - the point is the refusal
                    result["second_resume"] = f"{type(exc).__name__}: {exc}"
                state = await harness.state("r1")
                result["after_second_resume"] = state.status.value
                result["writes_after_second_resume"] = write_calls(paths.counter)
            return result

        print(json.dumps(asyncio.run(scenario()), ensure_ascii=False))
        sys.stdout.flush()
        os._exit(0)

    print(json.dumps({"error": f"unknown mode {mode!r}"}))
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
