"""R0: the three recovery defects an independent review reproduced, as target tests.

The review (`docs/per-tool-settlement-recovery-acceptance-review.zh-CN.md` §2–§4) ran
probes against `2c8a324` and found that a partially settled batch recovers incorrectly in
three ways. Each test below is one of those probes turned into the assertion the plan
requires, so the suite states what the run **must** do rather than what it happened to do:

* **R1** — a recovery under a *changed* deployment must refuse before it dispatches
  anything. The recorded batch has to be rebuilt from the old durable facts and compared
  with the current ones; comparing the current values with themselves proves nothing.
* **R2** — the run's deadline has to cover the recovered work, including the tool calls
  the recovery redispatches. A limit that only starts once the tools have finished is not
  a limit.
* **R3** — a receipt the ledger *confirmed as failed* is a result, not an open question.
  A resume must read it and carry on instead of asking for business evidence again.

The fixtures are the same real ones the partial-recovery tests use: a child process that
dies mid-batch, a separate SQLite connection for the ledger, and a scripted model. No
sleep-based scheduling is relied on for the deadline test: the handler blocks far longer
than the budget, and the driver's own guard is a generous backstop.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

import test_runtime_partial_recovery as fixture
from durable_edit import ScriptedStream, build_partial, final_message, run_context

from yanxu_harness.core.outcome import RunStatus
from yanxu_harness.errors import ContractError
from yanxu_harness.runtime import DefaultHarness
from yanxu_harness.storage import SqliteExecutionBackend, SqlitePersistence
from yanxu_harness.tools import EffectState, ToolReceipt

RUN_ID = fixture.RUN_ID
A = fixture.A
B = fixture.B
C = fixture.C


def promote(paths: Any, state: EffectState) -> None:
    """Record business evidence for B directly in the ledger.

    This stands in for a convergence that committed before the Run was updated — the
    window R3 is about. Its own detail text is what the model should end up reading.
    """
    ledger = SqliteExecutionBackend(paths.operations)
    try:
        fixture.run(
            ledger.promote(
                ToolReceipt(
                    operation_id=fixture.operation_id(B),
                    state=state,
                    result=(
                        {"content": [{"type": "text", "text": "confirmed earlier"}]}
                        if state is EffectState.SUCCEEDED
                        else None
                    ),
                    detail="confirmed earlier",
                ),
                evidence={"source": "audit business evidence", "detail": "confirmed earlier"},
            )
        )
    finally:
        ledger.close()


def _attempt(coro: Any) -> tuple[Any, str]:
    """Run one resume, returning ``(outcome, "")`` or ``(None, "Type: message")``."""
    try:
        return fixture.run(coro), ""
    except Exception as exc:  # noqa: BLE001 - a refusal is a legal answer, recorded as one
        return None, f"{type(exc).__name__}: {exc}"


# --- PTR-R1: the arrangement a batch was declared under is frozen ---------------------


def test_r1_an_orphan_recovered_under_drifted_instructions_is_refused(tmp_path: Path) -> None:
    """A changed system prompt must stop the recovery *before* C is dispatched.

    The recorded batch carries the arrangement the crash happened under. Recovery has to
    rebuild the batch from those durable values and compare them with the deployment it
    is running in. Rebuilding it from the *current* values compares a deployment with
    itself, which is how the original defect let C run and the model answer as if the
    arrangement had never moved.
    """
    paths = fixture.crash_in_a_partial_batch(tmp_path / "state")
    promote(paths, EffectState.SUCCEEDED)
    stream = ScriptedStream(final_message())
    old, _gateway = fixture.rebuild(paths, stream, attempt="a2")
    changed = DefaultHarness(
        stream_fn=stream,
        tools=old.tools,
        persistence=SqlitePersistence(paths.runs),
        system_prompt="a changed deployment instruction",
        clock=lambda: time.time() + fixture.LEASE_PAST,
    )

    assert (
        old.fingerprints()["instructions"] != changed.fingerprints()["instructions"]
    ), "the premise: the two deployments disagree about the instructions"

    with pytest.raises(ContractError) as refusal:
        fixture.run(changed.resume(RUN_ID, {}, run_context("a2")))

    assert "instruction" in str(refusal.value), (
        f"the refusal must name what drifted: {refusal.value}"
    )
    assert fixture.ran_tools(paths) == ["notes.a", "notes.b"], (
        "a drifted deployment must not dispatch the remaining call"
    )
    assert stream.calls == 0, "and it must not ask the model for another turn"
    assert fixture.read_record(paths).status is RunStatus.RUNNING, (
        "a refusal writes no verdict: the run is still the interrupted one it was, and "
        "remains recoverable once the original deployment is back"
    )


# --- PTR-R2: the deadline covers the recovered work -----------------------------------


def test_r2_the_runtime_limit_covers_the_recovered_handler(tmp_path: Path) -> None:
    """The budget must reach the tool call a recovery redispatches.

    The batch is recovered with `max_runtime_seconds=0.03` and C's handler blocks for
    0.16s. If the handler runs outside the bounded drive task, the limit only starts
    after it has already finished and the run completes; the run must instead stop with
    `usage_limited / max_runtime_exceeded`, dispatch nothing further, and leave the
    admitted operation in the ledger as evidence that C may have run.
    """
    paths = fixture.crash_in_a_partial_batch(tmp_path / "state")
    promote(paths, EffectState.SUCCEEDED)
    called: list[str] = []

    async def slow_c(context: Any, arguments: Any) -> Any:
        called.append("start")
        # Far longer than the budget, so the outcome cannot depend on scheduler luck.
        await asyncio.sleep(3.0)
        called.append("finished")
        return {"content": [{"type": "text", "text": "C completed"}]}

    stream = ScriptedStream(final_message())
    wired, _gateway = build_partial(
        paths,
        stream,
        attempt="a2",
        handlers={"notes.c": slow_c},
        clock=lambda: time.time() + fixture.LEASE_PAST,
    )
    harness = DefaultHarness(
        stream_fn=stream,
        tools=wired.tools,
        persistence=SqlitePersistence(paths.runs),
        system_prompt="be brief",
        max_runtime_seconds=0.03,
        clock=lambda: time.time() + fixture.LEASE_PAST,
    )

    outcome = fixture.run(harness.resume(RUN_ID, {}, run_context("a2")))

    assert outcome.status is RunStatus.USAGE_LIMITED, (
        f"the deadline must end the run, got {outcome.status.value!r}"
    )
    assert outcome.error_code == "max_runtime_exceeded"
    assert stream.calls == 0, "the limit must stop the follow-up model request"
    assert called == ["start"], (
        "the handler is interrupted rather than allowed to finish past the budget"
    )
    states = fixture.ledger_states(paths)
    assert fixture.operation_id(C) in states, (
        "the admission is written before the handler runs, so an interrupted call is "
        "evidence, not 'never started'"
    )


# --- PTR-R3: a confirmed failure is a result ------------------------------------------


def test_r3_a_confirmed_failed_receipt_converges_without_a_reconciler(tmp_path: Path) -> None:
    """`failed` in the ledger is a settled outcome; recovery must not re-ask.

    The ledger is promoted to `failed` after the Run was paused with B unproven — the
    crash-between-the-two window. The next recovery has no reconciler, and must still be
    able to finish: a proven failure is a result the model reads, not a question the host
    has to answer twice.
    """
    paths = fixture.crash_in_a_partial_batch(tmp_path / "state")
    first, _gateway = fixture.rebuild(paths, ScriptedStream(final_message()), attempt="a2")
    assert fixture.run(first.resume(RUN_ID, {}, run_context("a2"))).status is RunStatus.SUSPENDED
    promote(paths, EffectState.FAILED)
    assert fixture.ledger_states(paths)[fixture.operation_id(B)] == "failed"

    stream = ScriptedStream(final_message("recovered"))
    second, _gateway = fixture.rebuild(paths, stream, attempt="a3")
    outcome, error = _attempt(second.resume(RUN_ID, {}, run_context("a3")))

    assert error == "", f"the confirmed failure must not need a human: {error}"
    assert outcome.status is RunStatus.COMPLETED, (
        "a confirmed failure settles the call; it does not fail the run"
    )
    assert fixture.ran_tools(paths) == ["notes.a", "notes.b", "notes.c"], (
        "A and B are never re-run, and C runs once the question is answered"
    )
    assert stream.calls == 1, "the model sees the recovered batch"
    results = [
        message
        for message in fixture.read_record(paths).messages
        if message.get("role") == "toolResult"
    ]
    assert results[1]["is_error"] is True, "a failed call is reported to the model as an error"
    assert not results[2]["is_error"], "C succeeded after the failure was settled"
