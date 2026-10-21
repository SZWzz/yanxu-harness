"""The recovery claim, proven across real process boundaries.

A child process starts a run, reaches a write that needs approval, persists the
pause and **exits**. The parent then only touches files: it writes the operator's
decision into the approval ledger and starts a *second* child process that resumes
the run. Nothing about the pause is carried in memory, so what the second process
sees is exactly what a restarted service would see.

The evidence is deliberately about the outside world, not about calls into a mock:

* the handler appends one line per invocation to a file that survives processes, so
  "the write happened once" is counted rather than asserted;
* the operation ledger is opened from the parent and its receipts are counted;
* the tool result the model saw is read back out of the run record.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any, Mapping

import pytest
from child_process import child_json, run_child
from durable_edit import (
    DeploymentPaths,
    ScriptedStream,
    build,
    final_message,
    run_context,
    write_calls,
)

from yanxu_harness.core.outcome import RunStatus
from yanxu_harness.runtime import RunRequest
from yanxu_harness.runtime.harness import RunRecord
from yanxu_harness.storage import SqliteApprovalStore, SqlitePersistence
from yanxu_harness.tools import ApprovalStatus

MODES = ("approval-pause", "resume-approved", "resume-twice")


def run(coro):
    async def guard():
        return await asyncio.wait_for(coro, timeout=20)

    return asyncio.run(guard())


def pausing_child(directory: Path) -> dict:
    return child_json(run_child(["approval-pause", str(directory), "", ""]))


def resuming_child(directory: Path, mode: str = "resume-approved") -> dict:
    return child_json(run_child([mode, str(directory), "", ""]))


def approve(directory: Path, binding_key: str) -> None:
    """Write the decision through the trusted local entry the host owns."""

    async def decide() -> None:
        store = SqliteApprovalStore(directory / "approvals.db")
        await store.decide(binding_key, ApprovalStatus.APPROVED, decided_by="operator")
        store.close()

    run(decide())


def read_record(directory: Path) -> RunRecord:
    store = SqlitePersistence(directory / "runs.db")
    stored = run(store.load_run("r1"))
    store.close()
    assert stored is not None
    return RunRecord.from_record(stored.state)


# --- the chain ---------------------------------------------------------------


def test_a_paused_run_survives_the_process_that_paused_it(tmp_path: Path) -> None:
    directory = tmp_path / "state"
    paused = pausing_child(directory)

    assert paused["status"] == "suspended"
    assert paused["kind"] == "approval"
    assert paused["attempt"] == "a1"
    assert paused["binding_key"]
    assert paused["writes"] == 0, "nothing may be written before the decision"

    record = read_record(directory)
    assert record.status is RunStatus.SUSPENDED
    assert record.batch is not None
    blocked = [item for item in record.batch["calls"] if item["outcome"] == "awaiting"]
    assert [item["call_id"] for item in blocked] == ["call-1"]
    assert record.batch["interactions"][0]["approval_binding_key"] == paused["binding_key"]
    # The pause was committed with its events, not just its state.
    store = SqlitePersistence(directory / "runs.db")
    kinds = [event.kind for event in run(store.read_events())]
    store.close()
    assert "run_suspended" in kinds and "approval_requested" in kinds

    # T03: a pause leaves no operation intent behind — the ledger is empty, so a
    # restart cannot find a half-started operation to "resume".
    from yanxu_harness.storage import SqliteExecutionBackend

    backend = SqliteExecutionBackend(directory / "operations.db")
    try:
        assert backend.all_operations() == (), "an unauthorised call must leave no ledger row"
    finally:
        backend.close()


def test_a_second_process_resumes_and_the_write_happens_exactly_once(tmp_path: Path) -> None:
    directory = tmp_path / "state"
    paused = pausing_child(directory)
    approve(directory, paused["binding_key"])

    resumed = resuming_child(directory)

    assert resumed["status"] == "completed"
    assert resumed["final_text"] == "写好了。"
    assert resumed["attempt"] == "a2", "the resume advanced the attempt"
    assert resumed["writes"] == 1, "the handler ran exactly once, in the second process"

    # The handler's own trace names the attempt it ran under.
    counter = (directory / "write-calls.log").read_text(encoding="utf-8").splitlines()
    assert counter == ["a2:hello"]

    record = read_record(directory)
    assert record.status is RunStatus.COMPLETED
    assert record.attempt_id == "a2"
    tool_results = [item for item in record.messages if item["role"] == "toolResult"]
    assert [item["tool_call_id"] for item in tool_results] == ["call-1"], (
        "the original tool call has exactly one result"
    )
    assert "wrote hello" in tool_results[0]["content"][0]["text"]
    assert "waiting for an approval decision" not in tool_results[0]["content"][0]["text"]


def test_the_operation_ledger_shows_one_receipt_for_the_resumed_call(tmp_path: Path) -> None:
    from yanxu_harness.storage import SqliteExecutionBackend

    directory = tmp_path / "state"
    paused = pausing_child(directory)
    approve(directory, paused["binding_key"])
    # The operation id is recorded in the pause; after the resume the batch is
    # cleared, so it is read before continuing.
    paused_record = read_record(directory)
    operation_id = paused_record.batch["calls"][0]["operation_id"]
    assert operation_id, "the paused record names the operation it dispatched"
    resuming_child(directory)

    backend = SqliteExecutionBackend(directory / "operations.db")
    unsettled = backend.unsettled()
    receipt = backend.receipt(operation_id)
    counts = backend.counts_by_state()
    backend.close()
    assert unsettled == (), "the resumed call settled; nothing is left open"
    assert receipt is not None and receipt.state.value == "succeeded"
    assert counts.get("succeeded") == 1, counts


def test_resuming_a_finished_run_is_refused_and_does_not_write_again(tmp_path: Path) -> None:
    directory = tmp_path / "state"
    paused = pausing_child(directory)
    approve(directory, paused["binding_key"])

    result = resuming_child(directory, "resume-twice")

    assert result["status"] == "completed"
    assert result["writes"] == 1
    assert "second_resume" in result, "a second resume must be refused with a reason"
    assert "only a suspended run can be resumed" in result["second_resume"]
    assert result["after_second_resume"] == "completed"
    assert result["writes_after_second_resume"] == 1, "the refusal wrote nothing"


def test_a_denied_decision_reaches_the_model_without_running_the_tool(tmp_path: Path) -> None:
    directory = tmp_path / "state"
    paused = pausing_child(directory)

    async def deny() -> None:
        store = SqliteApprovalStore(directory / "approvals.db")
        await store.decide(
            paused["binding_key"], ApprovalStatus.DENIED, decided_by="operator", note="not now"
        )
        store.close()

    run(deny())
    resumed = resuming_child(directory)

    assert resumed["status"] == "completed"
    assert resumed["writes"] == 0, "a denied write never runs"
    record = read_record(directory)
    tool_result = [item for item in record.messages if item["role"] == "toolResult"][0]
    assert tool_result["is_error"] is True
    assert "denied" in tool_result["content"][0]["text"]
    assert "not now" in tool_result["content"][0]["text"], "the operator's note is preserved"


def test_an_unapproved_resume_asks_again_and_writes_nothing(tmp_path: Path) -> None:
    """No decision in the ledger means the run stays paused, in the new process too."""
    directory = tmp_path / "state"
    pausing_child(directory)

    resumed = resuming_child(directory)

    assert resumed["status"] == "suspended"
    assert resumed["writes"] == 0
    assert resumed["attempt"] == "a2", "the attempt advanced; the pause is the same"
    record = read_record(directory)
    assert record.status is RunStatus.SUSPENDED
    assert record.batch is not None
    blocked = [item for item in record.batch["calls"] if item["outcome"] == "awaiting"]
    assert len(blocked) == 1


def test_a_reconciliation_resume_settles_only_with_business_evidence(tmp_path: Path) -> None:
    """An unproven effect stays unproven unless the *host* can prove it."""
    from durable_edit import (
        DeploymentPaths,
        ScriptedStream,
        build,
        run_context as ctx,
        tool_call_message,
        write_calls,
    )
    from yanxu_harness.runtime import RunRequest

    directory = tmp_path / "state"
    paths = DeploymentPaths.under(directory)

    # First process: the effect is performed but its receipt is never settled.
    harness, _gateway = build(
        paths,
        ScriptedStream(tool_call_message("hello")),
        attempt="a1",
        unknown_effects=True,
        auto_write=True,
    )
    paused = run(harness.start(RunRequest(prompt="write it"), ctx("a1")))

    assert paused.status is RunStatus.SUSPENDED
    assert paused.pending_interaction is not None
    assert paused.pending_interaction["kind"] == "reconciliation"
    assert write_calls(paths.counter) == 1, "the handler ran once; the outcome is what is unproven"

    # A second process with no business check keeps the operation unproven and does
    # not retry it.
    resumed = resuming_child(directory)
    assert resumed["status"] == "suspended"
    assert resumed["writes"] == 1, "an unproven operation is never retried"

    # A third process with business evidence settles it, and still does not re-run it.
    asked: list[str] = []

    async def reconciler(call: Any, details: Mapping[str, Any]) -> Mapping[str, Any]:
        asked.append(details["operation_id"])
        return {
            "proven": True,
            "state": "succeeded",
            "detail": "the note contains hello",
            "source": "business store",
        }

    harness_three, _gateway_three = build(
        paths,
        ScriptedStream(final_message("确认了。")),
        attempt="a2",
        reconcile=reconciler,
        unknown_effects=True,
        auto_write=True,
    )
    settled = run(harness_three.resume("r1", {}, ctx("a2")))

    assert settled.status is RunStatus.COMPLETED
    assert settled.final_text == "确认了。"
    assert len(asked) == 1 and asked[0].endswith(":call-1")
    assert write_calls(paths.counter) == 1, "evidence settles the operation; nothing re-runs it"
    record = read_record(directory)
    tool_result = [item for item in record.messages if item["role"] == "toolResult"][0]
    assert "the note contains hello" in tool_result["content"][0]["text"]


def test_each_mode_is_reachable() -> None:
    """The child modes this file relies on are the ones the script implements."""
    source = (Path(__file__).resolve().parent / "fault_windows.py").read_text(encoding="utf-8")
    for mode in MODES:
        assert f'"{mode}"' in source, mode
    assert json.dumps({"status": "suspended"})


# --- the window before the pause was recorded --------------------------------


@pytest.mark.parametrize("claim_delay", [0.0, 0.25])
def test_an_orphaned_run_enters_reconciliation_instead_of_staying_stuck(
    tmp_path: Path, monkeypatch, claim_delay: float
) -> None:
    """A crash *before* the pause was committed leaves a `running` record.

    There is no recorded batch, so nothing can be replayed; the run must become an
    explicit reconciliation wait rather than a run that looks alive forever or a
    resume that guesses which calls were dispatched.
    """
    directory = tmp_path / "state"
    paths = DeploymentPaths.under(directory)
    release = asyncio.Event()
    entered = asyncio.Event()

    class Blocking:
        async def __call__(self, model, context, options=None):
            return self

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def result(self):
            entered.set()
            await asyncio.wait_for(release.wait(), timeout=10)
            return final_message()

    async def scenario() -> dict:
        # A run driven by a process that never finishes committing: the lease is
        # released here to stand in for that process having died (a real crash leaves
        # it held until it expires, which is what the next test covers).
        harness, gateway = build(paths, Blocking(), attempt="a1", auto_write=True)
        original_claim = harness._claim

        async def delayed_claim(run_id):
            await asyncio.sleep(claim_delay)
            return await original_claim(run_id)

        monkeypatch.setattr(harness, "_claim", delayed_claim)
        task = asyncio.ensure_future(harness.start(RunRequest(prompt="write it"), run_context("a1")))
        # Observe the committed, active driver rather than assuming startup and
        # disk I/O complete within a fixed number of milliseconds.
        await asyncio.wait_for(entered.wait(), timeout=10)
        lease = harness._active["r1"].lease  # noqa: SLF001
        await harness._release(lease)  # noqa: SLF001 - the driver "died"
        record, _version = await harness._load("r1")  # noqa: SLF001
        assert record.status is RunStatus.RUNNING, "the pause was never committed"
        assert record.batch is None

        # (The record was read above, synchronously, because `read_record` starts its
        # own event loop and this scenario is already inside one.)
        # A second worker resumes: it can take the lease, so the first is gone.
        second, _gateway = build(paths, ScriptedStream(final_message()), attempt="a2")
        outcome = await second.resume("r1", {}, run_context("a2"))
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        return {
            "status": outcome.status.value,
            "kind": (outcome.pending_interaction or {}).get("kind"),
            "detail": (outcome.pending_interaction or {}).get("detail", ""),
            "writes": write_calls(paths.counter),
            "attempt": (await second.state("r1")).attempt_id,
        }

    result = run(scenario())
    assert result["status"] == "suspended", result
    assert result["kind"] == "reconciliation", result
    assert "nothing is retried automatically" in result["detail"].lower()
    assert result["writes"] == 0, "nothing may be replayed from a state that cannot prove it"
    assert result["attempt"] == "a2"

    # And the record now says so on disk, with its events.
    record = read_record(directory)
    assert record.status is RunStatus.SUSPENDED
    assert record.pending_interaction is not None
    store = SqlitePersistence(directory / "runs.db")
    kinds = [event.kind for event in run(store.read_events())]
    store.close()
    assert "run_suspended" in kinds


def test_a_live_driver_cannot_be_recovered_from_under(tmp_path: Path) -> None:
    """The lease is what makes "the driver is gone" a fact rather than a guess."""
    from yanxu_harness.errors import RunBusy

    directory = tmp_path / "state"
    paths = DeploymentPaths.under(directory)
    release = asyncio.Event()
    entered = asyncio.Event()

    class Blocking:
        async def __call__(self, model, context, options=None):
            return self

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def result(self):
            entered.set()
            await asyncio.wait_for(release.wait(), timeout=10)
            return final_message()

    async def scenario() -> dict:
        harness, _gateway = build(paths, Blocking(), attempt="a1")
        task = asyncio.ensure_future(harness.start(RunRequest(prompt="write it"), run_context("a1")))
        await asyncio.wait_for(entered.wait(), timeout=10)
        second, _gateway_two = build(paths, ScriptedStream(final_message()), attempt="a2")
        try:
            await second.resume("r1", {}, run_context("a2"))
            result = {"refused": False}
        except RunBusy as exc:
            result = {"refused": True, "detail": exc.detail}
        release.set()
        await asyncio.wait_for(task, timeout=10)
        return result

    result = run(scenario())
    assert result["refused"] is True, result
    assert "lease" in result["detail"]
