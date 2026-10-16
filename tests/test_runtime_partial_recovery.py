"""T4: recovering a batch the process died in the middle of.

The fault is injected by a real child process (``fault_windows.py``'s
``partial-batch-then-die``): three sequential calls, A settles and is committed, B starts
and dies with its intent open, C is never dispatched. The parent then reads the run store
and the ledger and has to recover the batch from them alone.

What recovery may do is bounded, and this file pins each bound:

* A's committed result is reused — the handler is not called again;
* B's open intent is *not* a licence to run it again, and it stops the batch (the
  conservative barrier) so C is not dispatched while B is unproven;
* business evidence settles B in the ledger *before* the run is updated;
* "failed" is a confirmed result, not something to retry;
* a ledger without the recovery capability blocks with a reason instead of guessing.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from child_process import run_child
from durable_edit import (
    DeploymentPaths,
    ScriptedStream,
    build_partial,
    final_message,
    run_context,
)

from yanxu_harness.core.outcome import RunStatus
from yanxu_harness.runtime.batch import CallOutcome, RunBatch
from yanxu_harness.runtime.harness import RunRecord
from yanxu_harness.storage import SqliteExecutionBackend, SqlitePersistence
from yanxu_harness.testing import RecordingExecutionBackend

RUN_ID = "r1"
A = "c-notes.a"
B = "c-notes.b"
C = "c-notes.c"
#: The operation id the gateway derives: namespace, session, run, **model action** and
#: tool-call id. The model action is part of it so two turns that reuse a call id are two
#: calls rather than one call to replay.
MODEL_ACTION = "request:1"


def operation_id(call_id: str) -> str:
    return f"project-a:s1:r1:{MODEL_ACTION}:{call_id}"


#: Past the default lease TTL: the worker that died in the child process still holds the
#: run's lease, and a recovery worker simply arrives after it expired. Production code is
#: not changed to make the fence easier to pass.
LEASE_PAST = 400.0


def run(coro):
    async def guard():
        return await asyncio.wait_for(coro, timeout=30)

    return asyncio.run(guard())


def rebuild(paths: DeploymentPaths, stream, *, attempt: str, reconcile=None):
    return build_partial(
        paths, stream, attempt=attempt, reconcile=reconcile, clock=lambda: time.time() + LEASE_PAST
    )


def crash_in_a_partial_batch(directory: Path) -> DeploymentPaths:
    """Run the child that commits A, starts B and dies, and return the ledgers."""
    result = run_child(["partial-batch-then-die", str(directory), "", ""], timeout=60)
    assert result.returncode == 9, (result.returncode, result.stderr[-2000:])
    return DeploymentPaths.under(directory)


def counter_lines(paths: DeploymentPaths) -> list[str]:
    if not paths.counter.exists():
        return []
    return [
        line
        for line in paths.counter.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def ran_tools(paths: DeploymentPaths) -> list[str]:
    """Which tool each trace line belongs to, across both processes."""
    return [line.split(":")[1] for line in counter_lines(paths)]


def ledger_states(paths: DeploymentPaths) -> dict[str, str]:
    backend = SqliteExecutionBackend(paths.operations)
    try:
        return {str(item["operation_id"]): str(item["state"]) for item in backend.all_operations()}
    finally:
        backend.close()


def read_record(paths: DeploymentPaths) -> RunRecord:
    store = SqlitePersistence(paths.runs)
    try:
        stored = run(store.load_run(RUN_ID))
    finally:
        store.close()
    assert stored is not None
    return RunRecord.from_record(stored.state)


def test_t4_the_crash_window_leaves_a_readable_prefix(tmp_path: Path) -> None:
    """The premise: A is durable, B is open, C is untouched."""
    paths = crash_in_a_partial_batch(tmp_path / "state")
    assert ran_tools(paths) == ["notes.a", "notes.b"], (
        "A ran to completion; B started; C never ran"
    )
    record = read_record(paths)
    assert record.status is RunStatus.RUNNING
    assert record.batch is None, "a running batch records no outcome"
    states = ledger_states(paths)
    assert states[operation_id(A)] == "succeeded"
    assert states[operation_id(B)] == "running"
    assert operation_id(C) not in states


def test_t4_recovery_barriers_on_the_unproven_call(tmp_path: Path) -> None:
    paths = crash_in_a_partial_batch(tmp_path / "state")
    harness, _gateway = rebuild(paths, ScriptedStream(final_message("done")), attempt="a2")
    outcome = run(harness.resume(RUN_ID, {}, run_context("a2")))

    assert outcome.status is RunStatus.SUSPENDED
    interaction = dict(outcome.pending_interaction or {})
    assert interaction.get("kind") == "reconciliation"
    assert interaction.get("call_id") == B
    assert "ledger" in str(interaction.get("detail") or ""), (
        "the pause must explain that the ledger is what has to answer"
    )

    # Nothing new was dispatched: A is not re-run, C waits behind B's question.
    assert ran_tools(paths) == ["notes.a", "notes.b"]
    assert operation_id(C) not in ledger_states(paths)

    # The recovered pause carries a batch that names every call, which is what makes a
    # further resume possible without guessing.
    record = read_record(paths)
    batch = RunBatch.from_record(record.batch)
    assert {
        call.call_id: call.outcome for call in batch.calls
    } == {A: CallOutcome.SETTLED, B: CallOutcome.UNCERTAIN, C: CallOutcome.UNSTARTED}
    results = [m for m in record.messages if m.get("role") == "toolResult"]
    assert [m["tool_call_id"] for m in results] == [A, B, C], (
        "the transcript answers every declared call, so the turn stays well formed"
    )
    assert "notes.a did a.md" in results[0]["content"][0]["text"]
    assert "not been dispatched" in results[2]["content"][0]["text"]


def test_t4_evidence_converges_the_call_and_the_rest_runs_once(tmp_path: Path) -> None:
    paths = crash_in_a_partial_batch(tmp_path / "state")
    first, _gateway = rebuild(paths, ScriptedStream(final_message("done")), attempt="a2")
    assert run(first.resume(RUN_ID, {}, run_context("a2"))).status is RunStatus.SUSPENDED

    asked: list[str] = []

    async def reconciler(call, details):
        asked.append(call.call_id)
        return {
            "proven": True,
            "state": "succeeded",
            "detail": "the note store shows B was written",
            "source": "business store",
        }

    second, _gateway = rebuild(
        paths, ScriptedStream(final_message("all done")), attempt="a3", reconcile=reconciler
    )
    outcome = run(second.resume(RUN_ID, {}, run_context("a3")))

    assert outcome.status is RunStatus.COMPLETED
    assert asked == [B], "only the unproven call is asked about"
    assert ran_tools(paths) == ["notes.a", "notes.b", "notes.c"], (
        "A and B are never re-run; C runs exactly once, after the question is answered"
    )
    states = ledger_states(paths)
    assert states[operation_id(B)] == "succeeded", (
        "the ledger records the convergence, it is not only in the run record"
    )
    assert states[operation_id(C)] == "succeeded"
    record = read_record(paths)
    results = [m for m in record.messages if m.get("role") == "toolResult"]
    assert [m["tool_call_id"] for m in results] == [A, B, C]
    assert "the note store shows B was written" in results[1]["content"][0]["text"]


def test_t4_failed_evidence_is_a_result_not_a_retry(tmp_path: Path) -> None:
    paths = crash_in_a_partial_batch(tmp_path / "state")
    first, _gateway = rebuild(paths, ScriptedStream(final_message("done")), attempt="a2")
    run(first.resume(RUN_ID, {}, run_context("a2")))

    async def reconciler(call, details):
        return {
            "proven": True,
            "state": "failed",
            "detail": "the note store shows the write was rejected",
            "source": "business store",
        }

    second, _gateway = rebuild(
        paths, ScriptedStream(final_message("recovered")), attempt="a3", reconcile=reconciler
    )
    outcome = run(second.resume(RUN_ID, {}, run_context("a3")))

    assert outcome.status is RunStatus.COMPLETED
    assert ran_tools(paths) == ["notes.a", "notes.b", "notes.c"], (
        "a confirmed failure is a result; the original operation is not repeated"
    )
    states = ledger_states(paths)
    assert states[operation_id(B)] == "failed"
    assert states[operation_id(C)] == "succeeded"
    results = [
        m for m in read_record(paths).messages if m.get("role") == "toolResult"
    ]
    assert results[1]["is_error"] is True
    assert "rejected" in results[1]["content"][0]["text"]


def test_t4_a_ledger_convergence_recorded_before_the_run_commit_is_read(tmp_path: Path) -> None:
    """The ledger first, the run second: a crash between them must not re-ask."""
    paths = crash_in_a_partial_batch(tmp_path / "state")
    first, _gateway = rebuild(paths, ScriptedStream(final_message("done")), attempt="a2")
    run(first.resume(RUN_ID, {}, run_context("a2")))

    # Stand in for a promotion that committed and whose Run update did not.
    from yanxu_harness.tools import EffectState, ToolReceipt

    backend = SqliteExecutionBackend(paths.operations)
    try:
        run(
            backend.promote(
                ToolReceipt(
                    operation_id=operation_id(B),
                    state=EffectState.SUCCEEDED,
                    result={"content": [{"type": "text", "text": "promoted earlier"}]},
                    detail="promoted before the crash",
                ),
                evidence={"source": "business store", "detail": "promoted before the crash"},
            )
        )
    finally:
        backend.close()

    asked: list[str] = []

    async def reconciler(call, details):  # pragma: no cover - must not be reached
        asked.append(call.call_id)
        return {"proven": True, "state": "succeeded", "source": "late"}

    second, _gateway = rebuild(
        paths, ScriptedStream(final_message("done")), attempt="a3", reconcile=reconciler
    )
    outcome = run(second.resume(RUN_ID, {}, run_context("a3")))

    assert outcome.status is RunStatus.COMPLETED
    assert asked == [], "the ledger already proved the outcome; nothing to ask"
    assert ran_tools(paths) == ["notes.a", "notes.b", "notes.c"]


def test_t4_a_ledger_without_the_capability_blocks_with_a_reason(tmp_path: Path) -> None:
    paths = crash_in_a_partial_batch(tmp_path / "state")
    harness, gateway = rebuild(paths, ScriptedStream(final_message("done")), attempt="a2")
    # Replace only the ledger behind the gateway: the deployment, the tools and the run
    # store stay exactly as the crashing process left them.
    gateway._backend = RecordingExecutionBackend()  # noqa: SLF001 - the injected ledger is the point
    outcome = run(harness.resume(RUN_ID, {}, run_context("a2")))

    assert outcome.status is RunStatus.SUSPENDED
    interaction = dict(outcome.pending_interaction or {})
    assert "cannot be queried" in str(interaction.get("detail") or ""), interaction
    assert ran_tools(paths) == ["notes.a", "notes.b"], (
        "an unanswerable ledger blocks; it does not authorise a dispatch"
    )


# --- T6: the other crash window in the same family ---------------------------------------


def test_t6_a_settled_receipt_is_filled_in_without_running_the_handler(tmp_path: Path) -> None:
    """The window between the ledger's receipt and the run's observation.

    The child settled the operation and died before the observation was committed. What
    the next process must do is read the receipt and record its result — the handler ran
    once, in the child, and must not run again.
    """
    from durable_edit import build

    directory = tmp_path / "state"
    result = run_child(["receipt-then-die", str(directory), "", ""], timeout=60)
    assert result.returncode == 9, (result.returncode, result.stderr[-2000:])
    paths = DeploymentPaths.under(directory)
    assert counter_lines(paths) == ["a1:hello"], "the handler ran once, in the child"

    # The run record never saw the outcome; the ledger did.
    record = read_record(paths)
    assert record.status is RunStatus.RUNNING
    states = ledger_states(paths)
    assert list(states.values()) == ["succeeded"]

    harness, _gateway = build(
        paths,
        ScriptedStream(final_message("read back")),
        attempt="a2",
        clock=lambda: time.time() + LEASE_PAST,
    )
    outcome = run(harness.resume(RUN_ID, {}, run_context("a2")))

    assert outcome.status is RunStatus.COMPLETED
    assert counter_lines(paths) == ["a1:hello"], (
        "the receipt is reused; the handler is not called again"
    )
    results = [
        message
        for message in read_record(paths).messages
        if message.get("role") == "toolResult"
    ]
    assert len(results) == 1
    assert "wrote hello" in results[0]["content"][0]["text"]


def test_t6_a_fully_observed_batch_is_assembled_without_running_anything(tmp_path: Path) -> None:
    """The window after the last observation and before the settlement.

    Every call is already observed and committed; only the one turn that carries the
    results into the transcript is missing. Recovery must assemble it deterministically
    and dispatch nothing — the trace is exactly the three calls the child ran.
    """
    from durable_edit import build_partial as build

    directory = tmp_path / "state"
    result = run_child(["observed-then-die", str(directory), "", ""], timeout=60)
    assert result.returncode == 9, (result.returncode, result.stderr[-2000:])
    paths = DeploymentPaths.under(directory)
    assert ran_tools(paths) == ["notes.a", "notes.b", "notes.c"]

    record = read_record(paths)
    assert record.status is RunStatus.RUNNING
    from yanxu_harness.core.execution import state_from_record

    snapshot = state_from_record(record.execution)
    assert snapshot.phase.value == "running_batch"
    assert [call.observed for call in snapshot.batch] == [True, True, True]
    assert [
        message for message in record.messages if message.get("role") == "toolResult"
    ] == [], "the results are committed per call; the turn that carries them is not"

    harness, _gateway = build(
        paths,
        ScriptedStream(final_message("assembled")),
        attempt="a2",
        clock=lambda: time.time() + LEASE_PAST,
    )
    outcome = run(harness.resume(RUN_ID, {}, run_context("a2")))

    assert outcome.status is RunStatus.COMPLETED
    assert ran_tools(paths) == ["notes.a", "notes.b", "notes.c"], (
        "nothing was re-dispatched: the batch was already fully observed"
    )
    results = [
        message
        for message in read_record(paths).messages
        if message.get("role") == "toolResult"
    ]
    assert [message["tool_call_id"] for message in results] == [A, B, C], (
        "the results are assembled once, in declaration order"
    )
