#!/usr/bin/env python3
"""Child process for the MCP fault-window acceptance tests (AC22/25/26).

Each mode is a *distinct OS process*: the failure windows are about what a restart
can prove from the run store and the operation ledger, not about an exception caught
in the same interpreter.

Modes
-----
``unknown-pause``
    Run a real MCP write that commits the business effect and then kills the server;
    the run must suspend with a reconciliation interaction.
``promote-die``
    In a new process, promote that unproven operation with business evidence and die
    immediately after the ledger write, before the Run is updated.
``resume``
    In another process, resume the original run; the promoted receipt is reused and
    the model continues.
``settled-commit-die``
    Run a real MCP read; die in the moment the harness is about to commit the tool
    result, so the operation receipt is durable but the Run has not recorded it.
``batch-unknown-pause`` / ``batch-resume-block`` / ``batch-promote-resume``
    The A/B/C partial batch: A settles, B is unproven and C is never sent; recovery
    barriers on B, and after evidence C runs exactly once.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

from yanxu_harness.integrations.mcp import McpToolBinding
from yanxu_harness.runtime import RunRequest
from yanxu_harness.runtime.batch import RunBatch
from yanxu_harness.runtime.harness import RunRecord
from yanxu_harness.storage import SqliteExecutionBackend, SqlitePersistence
from yanxu_harness.tools import EffectState
from yanxu_harness.tools import ExecutionMode, ToolEffect, ToolInvocation

import mcp_faults
from mcp_faults import (
    ALIAS_BUMP,
    ALIAS_ECHO,
    Paths,
    ScriptedStream,
    batch_call,
    build,
    context,
    final,
    tool_call,
)

RUN_ID = "r1"
#: A clock past a dead worker's lease. Production code is not made easier to pass.
LEASE_PAST = 400.0

def _past():
    return lambda: time.time() + LEASE_PAST

BUMP = McpToolBinding(
    "bump_then_die",
    effects=frozenset({ToolEffect.WRITE}),
    execution_mode=ExecutionMode.SEQUENTIAL,
    preview=lambda _context, arguments: {"key": arguments["key"], "effect": "increment"},
)
ECHO = McpToolBinding(
    "echo", effects=frozenset({ToolEffect.READ}), execution_mode=ExecutionMode.SEQUENTIAL
)


class ExitAfterUnknownSettle(SqliteExecutionBackend):
    """Commit B as ``unknown``, then die before the runner can admit C."""

    async def settle(self, receipt):  # type: ignore[override]
        await super().settle(receipt)
        if receipt.state is EffectState.UNKNOWN:
            sys.stdout.flush()
            os._exit(9)


class ExitBeforeToolResultCommit(SqlitePersistence):
    """Die in the commit that would record the tool result.

    The gateway has already settled its own receipt by then. The process dies *before*
    the run store transaction, which is exactly the AC22 window.
    """

    async def commit_transition(  # type: ignore[override]
        self, run_id, state, *, expected_version, lease=None, events=()
    ):
        for message in state.get("messages") or ():
            if isinstance(message, dict) and message.get("role") == "toolResult":
                sys.stdout.flush()
                os._exit(9)
        return await super().commit_transition(
            run_id,
            state,
            expected_version=expected_version,
            lease=lease,
            events=events,
        )


def _pending_batch(record: RunRecord) -> RunBatch:
    assert record.batch is not None, "the run did not record a batch to recover"
    return RunBatch.from_record(record.batch)


async def _resume(deploy, attempt: str = "a2") -> dict:
    outcome = await deploy["harness"].resume(RUN_ID, {}, context(attempt))
    return {
        "status": outcome.status.value,
        "pending": dict(outcome.pending_interaction or {}),
        "final_text": outcome.final_text,
    }


async def run(mode: str, directory: Path) -> None:
    paths = Paths(directory)
    if mode == "unknown-pause":
        deploy = await build(
            paths,
            ScriptedStream(tool_call("c1", ALIAS_BUMP, {"key": "k"})),
            bindings={"bump_then_die": BUMP},
        )
        outcome = await deploy["harness"].start(RunRequest(prompt="write it"), context("a1"))
        print(json.dumps({"status": outcome.status.value}))
        sys.stdout.flush()
        os._exit(0)

    if mode == "settled-commit-die":
        deploy = await build(
            paths,
            ScriptedStream(tool_call("c1", ALIAS_ECHO, {"text": "hello"}), final()),
            bindings={"echo": ECHO},
            persistence=ExitBeforeToolResultCommit(paths.runs),
        )
        await deploy["harness"].start(RunRequest(prompt="read it"), context("a1"))
        raise AssertionError("the child was supposed to die in the tool-result commit")

    if mode == "promote-die":
        deploy = await build(paths, ScriptedStream(final()), bindings={"bump_then_die": BUMP})
        stored = await deploy["store"].load_run(RUN_ID)
        assert stored is not None
        record = RunRecord.from_record(stored.state)
        call = _pending_batch(record).blocking()[0]
        invocation = ToolInvocation(
            call_id=call.call_id,
            namespace=record.namespace,
            tool_name=call.tool_name,
            arguments=dict(call.arguments),
            session_id=record.session_id,
            run_id=record.run_id,
            model_action_id=call.model_action_id,
            operation_id=call.operation_id or None,
        )
        receipt = await deploy["gateway"].promote(
            invocation,
            {
                "state": "succeeded",
                "source": "the fixture business counter",
                "detail": "the counter file proves the increment",
            },
        )
        print(json.dumps({"promoted": None if receipt is None else receipt.state.value}))
        sys.stdout.flush()
        os._exit(9)

    if mode == "resume":
        deploy = await build(
            paths, ScriptedStream(final("done")), bindings={"bump_then_die": BUMP}, clock=_past()
        )
        print(json.dumps(await _resume(deploy)))
        sys.stdout.flush()
        os._exit(0)

    if mode == "resume-echo":
        deploy = await build(
            paths, ScriptedStream(final("done")), bindings={"echo": ECHO}, clock=_past()
        )
        print(json.dumps(await _resume(deploy)))
        sys.stdout.flush()
        os._exit(0)

    if mode == "batch-unknown-pause":
        deploy = await build(
            paths,
            ScriptedStream(
                batch_call(
                    [
                        ("c1", ALIAS_ECHO, {"text": "a"}),
                        ("c2", ALIAS_BUMP, {"key": "k"}),
                        ("c3", ALIAS_ECHO, {"text": "c"}),
                    ]
                ),
                final("done"),
            ),
            bindings={"echo": ECHO, "bump_then_die": BUMP},
            ledger=ExitAfterUnknownSettle(paths.operations),
        )
        await deploy["harness"].start(RunRequest(prompt="do three"), context("a1"))
        raise AssertionError("the child was supposed to die after B settled as unknown")

    if mode == "batch-resume-block":
        deploy = await build(
            paths,
            ScriptedStream(final("done")),
            bindings={"echo": ECHO, "bump_then_die": BUMP},
            clock=_past(),
        )
        print(json.dumps(await _resume(deploy, "a2")))
        sys.stdout.flush()
        os._exit(0)

    if mode == "batch-promote-resume":
        deploy = await build(
            paths,
            ScriptedStream(final("done")),
            bindings={"echo": ECHO, "bump_then_die": BUMP},
            clock=_past(),
        )
        stored = await deploy["store"].load_run(RUN_ID)
        assert stored is not None
        record = RunRecord.from_record(stored.state)
        batch = _pending_batch(record)
        bump = next(
            call for call in batch.calls if call.tool_name == ALIAS_BUMP
        )
        invocation = ToolInvocation(
            call_id=bump.call_id,
            namespace=record.namespace,
            tool_name=bump.tool_name,
            arguments=dict(bump.arguments),
            session_id=record.session_id,
            run_id=record.run_id,
            model_action_id=bump.model_action_id,
            operation_id=bump.operation_id or None,
        )
        await deploy["gateway"].promote(
            invocation,
            {
                "state": "succeeded",
                "source": "the fixture business counter",
                "detail": "the counter file proves the increment",
            },
        )
        print(json.dumps(await _resume(deploy, "a3")))
        sys.stdout.flush()
        os._exit(0)

    raise SystemExit(f"unknown mode {mode!r}")


if __name__ == "__main__":
    asyncio.run(run(sys.argv[1], Path(sys.argv[2])))
