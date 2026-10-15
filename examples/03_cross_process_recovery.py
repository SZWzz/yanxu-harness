"""Example 3 — cross-process recovery, including an unknown outcome.

    python examples/03_cross_process_recovery.py

This one deliberately crashes a child process in the middle of a tool call. The
parent then reopens the same two SQLite files and shows three distinct states:

1. the child died **after the intent** and before the handler ran: an unsettled
   operation exists and nothing in the world changed;
2. the child died **after the side effect** and before the receipt was settled:
   the effect is on disk, and the ledger still says nothing. The only honest
   label is "unknown, needs reconciliation" — the harness does not guess, and it
   does not retry;
3. a call that finished before the crash is **replayed**, so the handler runs
   exactly once across both processes.

Nothing here is a simulation: the child is a real process that dies with
``os._exit``, and the state is read out of real files.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

CHILD = r'''
import asyncio, json, os, sys
from pathlib import Path
from yanxu_harness.runtime import Principal, RunContext, RunIdentity
from yanxu_harness.storage import SqliteExecutionBackend, SqliteApprovalStore
from yanxu_harness.tools import (ConservativePolicy, InvocationStatus, ToolDefinition,
                                 ToolEffect, ToolGateway, ToolInvocation, ToolRegistry)

mode, ops, approvals_path, counter, effect = sys.argv[1:6]
ops, approvals_path, counter, effect = Path(ops), Path(approvals_path), Path(counter), Path(effect)


def validate(arguments):
    return {"value": arguments["value"]}


def handler(_context, arguments):
    with counter.open("a", encoding="utf-8") as handle:
        handle.write("call\n")
    effect.write_text(arguments["value"], encoding="utf-8")
    return {"wrote": arguments["value"]}


definition = ToolDefinition("record.write", "Write a value.", effects=frozenset({ToolEffect.WRITE}),
                            args_validator=validate, handler=handler,
                            preview=lambda _c, a: {"value": a["value"]})
backend = SqliteExecutionBackend(ops)
gateway = ToolGateway(registry=ToolRegistry([definition]), policy=ConservativePolicy(auto_write=True),
                      approvals=SqliteApprovalStore(approvals_path), backend=backend)
context = RunContext(identity=RunIdentity(namespace="example", session_id="s1", run_id="r1", attempt_id="a1"),
                     principal=Principal(subject="author"))
call = ToolInvocation(call_id="call-1", namespace="example", tool_name="record.write",
                      arguments={"value": "first"}, run_id="r1")

if mode == "crash_before_effect":
    from yanxu_harness.tools.execution import ToolIntent
    asyncio.run(backend.begin(ToolIntent(operation_id="r1:call-1", namespace="example",
                                         tool_name="record.write", tool_version="1",
                                         args_hash="sha256:x", call_id="call-1", run_id="r1")))
    os._exit(9)

if mode == "crash_after_effect":
    from yanxu_harness.tools.execution import ToolIntent
    async def run():
        intent = ToolIntent(operation_id="r1:call-1", namespace="example", tool_name="record.write",
                            tool_version="1", args_hash="sha256:x", call_id="call-1", run_id="r1")
        await backend.begin(intent)
        await backend.execute(intent, definition, {"value": "first"}, context)
        # No settle(): the crash stands in for a lost settlement.
    asyncio.run(run())
    os._exit(9)

result = asyncio.run(gateway.invoke(call, context))
print(json.dumps({"status": result.status.value}))
sys.stdout.flush()
os._exit(0)
'''


def run_child(mode: str, ops: Path, approvals: Path, counter: Path, effect: Path) -> int:
    child_env = dict(os.environ)
    child_env["PYTHONIOENCODING"] = "utf-8"
    completed = subprocess.run(
        [sys.executable, "-c", CHILD, mode, str(ops), str(approvals), str(counter), str(effect)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=child_env,
        check=False,
    )
    if completed.returncode not in {0, 9}:
        raise SystemExit(f"child failed ({completed.returncode}):\n{completed.stderr[-2000:]}")
    return completed.returncode


async def main() -> int:
    from yanxu_harness.storage import SqliteExecutionBackend

    workspace = Path(tempfile.mkdtemp(prefix="yanxu-example-03-"))

    # (1) Died between the intent and the effect.
    ops, approvals = workspace / "a-ops.sqlite", workspace / "a-approvals.sqlite"
    counter, effect = workspace / "a-count.txt", workspace / "a-effect.txt"
    code = run_child("crash_before_effect", ops, approvals, counter, effect)
    assert code == 9
    with SqliteExecutionBackend(ops) as backend:
        unsettled = backend.unsettled()
        print(f"(1) after an intent-only crash: {len(unsettled)} unsettled, effect exists={effect.exists()}")
        assert len(unsettled) == 1 and not effect.exists()

    # (2) Died after the effect, before the receipt.
    ops, approvals = workspace / "b-ops.sqlite", workspace / "b-approvals.sqlite"
    counter, effect = workspace / "b-count.txt", workspace / "b-effect.txt"
    code = run_child("crash_after_effect", ops, approvals, counter, effect)
    assert code == 9
    with SqliteExecutionBackend(ops) as backend:
        receipt = backend.receipt("r1:call-1")
        print(f"(2) after an effect-then-crash: effect exists={effect.exists()} receipt={receipt}")
        assert effect.exists(), "this window is only interesting when the effect landed"
        assert receipt is None, "an unsettled operation has no receipt, and none is invented"
        assert len(backend.unsettled()) == 1
    print("    the only honest label here is unknown: reconcile with business evidence.")

    # (3) A completed call is replayed after the restart.
    ops, approvals = workspace / "c-ops.sqlite", workspace / "c-approvals.sqlite"
    counter, effect = workspace / "c-count.txt", workspace / "c-effect.txt"
    assert run_child("complete", ops, approvals, counter, effect) == 0
    assert run_child("complete", ops, approvals, counter, effect) == 0
    calls = len(counter.read_text(encoding="utf-8").splitlines())
    print(f"(3) two processes ran the same call; the handler ran {calls} time(s)")
    assert calls == 1, "the second process must replay the receipt, not write again"

    print("OK: unknown stays unknown, a settled call is replayed, and nothing is retried blindly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
