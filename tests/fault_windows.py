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
``harness-prepare-then-die``
    the managed context path prepared a request view and committed it, then the
    process died *inside* the model request: another process must read the
    prepared view, its projection facts and the ``requesting_model`` phase.
``harness-artifact-die``
    a tool result large enough to be bounded was committed with a reference to a
    *durable* artifact store, and the process died inside the next model request:
    the reference and the original must both be readable afterwards.

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


def _append_line(counter: Path, line: str) -> None:
    with counter.open("a", encoding="utf-8") as handle:
        handle.write(f"{line}\n")


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

    if mode in {
        "harness-effect-then-die",
        "harness-noop-then-die",
        "harness-settled-at-request-die",
        "approval-pause-die",
    }:
        # These modes drive the *real* ``DefaultHarness`` over the real SQLite stores and
        # kill the process at one chosen boundary. ``store`` is a directory holding the
        # three ledgers (run / operations / approvals); the other two arguments are
        # unused, so the call shape matches the other modes.
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

        def dying_handler(*, effect: bool):
            def handler(_context, arguments):
                if effect:
                    with paths.counter.open("a", encoding="utf-8") as handle:
                        handle.write(f"effect:{arguments['text']}\n")
                        handle.flush()
                        os.fsync(handle.fileno())
                # The crash stands in for the process dying inside the effect: whether
                # the handler got this far is exactly what the record must not claim.
                os._exit(9)

            return handler

        class DieOnSecondRequest:
            """Answer the first request, then die as the next one starts."""

            def __init__(self, first) -> None:
                self.first = first
                self.calls = 0
                self._final = first

            async def __call__(self, model, context, options=None):
                self.calls += 1
                if self.calls >= 2:
                    # The batch has settled and its receipt is in the ledger; the run
                    # dies before the next model request can be answered.
                    os._exit(9)
                self._final = self.first
                return self

            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

            async def result(self):
                return self._final

        async def scenario() -> dict:
            listener = None
            handler = None
            if mode == "harness-effect-then-die":
                handler = dying_handler(effect=True)
                stream: object = ScriptedStream(tool_call_message("hello"))
                auto_write = True
            elif mode == "harness-noop-then-die":
                handler = dying_handler(effect=False)
                stream = ScriptedStream(tool_call_message("hello"))
                auto_write = True
            elif mode == "harness-settled-at-request-die":
                stream = DieOnSecondRequest(tool_call_message("hello"))
                auto_write = True
            else:
                stream = ScriptedStream(tool_call_message("hello"))
                auto_write = False

                def listener(envelope) -> None:
                    # Published *after* the pause transaction committed, so dying here
                    # tests the window between the durable pause and the driver's own
                    # wrap-up.
                    if getattr(envelope, "kind", "") == "run_suspended":
                        os._exit(9)

            harness, _gateway = build(
                paths,
                stream,
                attempt="a1",
                auto_write=auto_write,
                handler=handler,
                run_listener=listener,
                # This mode dies inside the pause fanout, so its lease outlives the
                # process. A short TTL lets the parent resume after a bounded wait
                # without any production change making a fence easier to pass.
                lease_ttl_seconds=1.0 if mode == "approval-pause-die" else 300.0,
            )
            outcome = await harness.start(RunRequest(prompt="write it"), run_context("a1"))
            return {
                "status": outcome.status.value,
                "writes": write_calls(paths.counter),
                "stream_calls": getattr(stream, "calls", None),
            }

        print(json.dumps(asyncio.run(scenario()), ensure_ascii=False))
        sys.stdout.flush()
        os._exit(0)

    if mode in ("harness-prepare-then-die", "harness-artifact-die"):
        # The managed context path (C3-C5 of the context plan): the preparations these tests
        # are about happen *before* the model request, so the process has to die inside that
        # request for the committed view to be the only evidence left.
        from context_helpers import real_convert
        from durable_edit import (
            DeploymentPaths,
            ScriptedStream,
            build,
            run_context,
            tool_call_message,
        )
        from yanxu_harness.context import (
            ContextController,
            ContextWindow,
            HeuristicTokenEstimator,
            InMemoryArtifactStore,
            ToolOutputPolicy,
        )
        from yanxu_harness.runtime import RunRequest
        from yanxu_harness.storage import SqliteArtifactStore

        directory = Path(store_arg)
        paths = DeploymentPaths.under(directory)

        def large_handler(_context, _arguments):
            """A tool result far larger than the window: the bounded view's case."""
            from yanxu_harness.core.types import TextContent

            body = "\n".join(f"line {index}: " + "x" * 60 for index in range(200))
            return {"content": [TextContent(text=body)], "details": {"lines": 200}}

        class DieOnNthRequest:
            """Answer the first request, then die as the next one is handed over."""

            def __init__(self, first=None) -> None:
                self.first = first
                self.calls = 0
                self._final = first

            async def __call__(self, model, context, options=None):
                self.calls += 1
                if self.calls >= 2:
                    # Everything the first request implied is committed by now: for the
                    # artifact mode that includes the stored original and the reference.
                    os._exit(9)
                self._final = self.first
                return self

            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

            async def result(self):
                return self._final

        if mode == "harness-artifact-die":
            # A durable store on purpose: the reference the dying process committed has to be
            # resolvable by the process that reads the record afterwards.
            artifacts: object = SqliteArtifactStore(paths.runs.parent / "artifacts.db")
            stream: object = DieOnNthRequest(tool_call_message("a very long note"))
            handler = large_handler
        else:
            artifacts = InMemoryArtifactStore(max_bytes=1024 * 1024)
            stream = DieOnNthRequest()
            handler = None

        controller = ContextController.compose(
            estimator=HeuristicTokenEstimator(),
            converter=real_convert,
            window=ContextWindow(limit_tokens=600, output_reserve=64),
            output_policy=ToolOutputPolicy(max_chars=1_000, head_chars=300, tail_chars=200),
            artifacts=artifacts,
        )
        # Deliberately not named ``_gateway``: that name is a module-level helper, and
        # binding it locally would shadow the function for the whole of ``main``.
        harness, _tool_gateway = build(
            paths,
            stream,
            attempt="a1",
            auto_write=True,
            handler=handler,
            context_controller=controller,
        )
        try:
            asyncio.run(harness.start(RunRequest(prompt="write it"), run_context("a1")))
        except BaseException:
            pass
        print(json.dumps({"status": "died", "stream_calls": stream.calls}, ensure_ascii=False))
        sys.stdout.flush()
        os._exit(9)

    if mode == "observed-then-die":
        # Every call of the batch has been observed and committed; the process dies on the
        # batch-settle commit, before the batch hooks and before the next model request.
        # Takes a directory.
        from durable_edit import (
            DeploymentPaths,
            ScriptedStream,
            build_partial,
            final_message,
            partial_batch_message,
        )
        from yanxu_harness.runtime import RunRequest

        paths = DeploymentPaths.under(Path(store_arg))
        harness, _gw = build_partial(
            paths, ScriptedStream(partial_batch_message(), final_message("done"))
        )
        original_commit = harness._persistence.commit_transition  # noqa: SLF001

        async def dying(run_id, state, *, expected_version, lease=None, events=()):
            phase = (dict(state).get("execution") or {}).get("phase")
            if phase == "awaiting_stop_hook":
                # The results are committed per call; the settlement that would turn them
                # into one transcript turn never is.
                os._exit(9)
            return await original_commit(
                run_id, state, expected_version=expected_version, lease=lease, events=events
            )

        harness._persistence.commit_transition = dying  # type: ignore[assignment]  # noqa: SLF001
        try:
            asyncio.run(harness.start(RunRequest(prompt="do three"), _context()))
        except BaseException:
            pass
        os._exit(9)

    if mode == "receipt-then-die":
        # The gateway settles the receipt, then the process dies before the run records
        # the observation that would carry it. Takes a directory.
        from durable_edit import (
            DeploymentPaths,
            ScriptedStream,
            final_message,
            run_context,
            tool_call_message,
            write_definition,
        )
        # Aliased on purpose: a plain local import would shadow the module-level names for
        # the whole of ``main`` and break the earlier modes that use them.
        from yanxu_harness.runtime import DefaultHarness as _Harness
        from yanxu_harness.runtime import RunRequest as _RunRequest
        from yanxu_harness.storage import SqliteApprovalStore as _Approvals
        from yanxu_harness.storage import SqlitePersistence as _Persistence
        from yanxu_harness.tools import ConservativePolicy as _Policy
        from yanxu_harness.tools import GatewayBindings as _Bindings
        from yanxu_harness.tools import ToolGateway as _ToolGateway
        from yanxu_harness.tools import ToolRegistry as _ToolRegistry
        from yanxu_harness.tools import core_tool as _core_tool

        paths = DeploymentPaths.under(Path(store_arg))

        class DieAfterSettle(SqliteExecutionBackend):
            async def settle(self, receipt):
                await super().settle(receipt)
                # The receipt is durable; the observation that would carry it into the run
                # state never is. The next process must read the receipt, not re-run.
                os._exit(9)

        definition = write_definition(paths.counter)
        gateway = _ToolGateway(
            registry=_ToolRegistry([definition]),
            policy=_Policy(auto_write=True),
            approvals=_Approvals(paths.approvals),
            backend=DieAfterSettle(paths.operations),
        )
        bindings = _Bindings(gateway=gateway, context=lambda: run_context("a1"))
        harness = _Harness(
            stream_fn=ScriptedStream(tool_call_message("hello"), final_message("done")),
            tools=[_core_tool(definition, bindings)],
            persistence=_Persistence(paths.runs),
            system_prompt="be brief",
        )
        try:
            asyncio.run(harness.start(_RunRequest(prompt="write it"), run_context("a1")))
        except BaseException:
            pass
        os._exit(9)

    if mode == "partial-batch-then-die":
        # A three-call sequential batch: A settles and is committed, B starts and dies
        # with its intent open (no receipt), C is never dispatched. This mode takes a
        # *directory*, because it needs all three ledgers plus the external trace.
        from durable_edit import (
            DeploymentPaths,
            ScriptedStream,
            build_partial,
            final_message,
            partial_batch_message,
        )
        from yanxu_harness.runtime import RunRequest

        paths = DeploymentPaths.under(Path(store_arg))

        def b_handler(_context, arguments):
            # The same trace format the default handler writes, so the parent can read one
            # uniform "which tool ran, in which attempt" list across both processes.
            _append_line(paths.counter, f"{_context.identity.attempt_id}:notes.b:{arguments['path']}")
            os._exit(9)

        harness, _gw = build_partial(
            paths,
            ScriptedStream(partial_batch_message(), final_message("done")),
            handlers={"notes.b": b_handler},
        )
        try:
            asyncio.run(harness.start(RunRequest(prompt="do three"), _context()))
        except BaseException:
            pass
        os._exit(9)

    print(json.dumps({"error": f"unknown mode {mode!r}"}))
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
