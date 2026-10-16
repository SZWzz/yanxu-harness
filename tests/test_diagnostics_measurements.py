"""D1: the measurements a run records about itself, at the boundaries where they happen.

The plan's rules for these facts, and the reason each one is pinned here:

* a **real request instance** is not the logical model action. A retry, a recovery and a
  re-issued request are separate requests with separate cost, and de-duplicating usage by
  the logical action would hide them;
* the **dispatch window is committed before the send**, so a request that never came back
  is readable as "started, never finished" rather than as never having happened;
* **durations are monotonic differences taken at the I/O boundary**; the reducer is handed
  the numbers and never reads a clock, which is what keeps a pure reducer pure;
* **timing is not identity**: it is not part of the observation digest, so re-delivering
  the same observation stays idempotent;
* a **payload size names how it was measured**, and an unmeasured one is absent rather than
  zero;
* per-call durations and the batch's own wall time are different facts — the calls of a
  parallel batch must not be summed into one elapsed time.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from durable_edit import (
    DeploymentPaths,
    ScriptedStream,
    build,
    final_message,
    run_context,
    tool_call_message,
)

from yanxu_harness.core.execution import Phase, state_from_record
from yanxu_harness.core.outcome import RunStatus
from yanxu_harness.runtime import RunRequest
from yanxu_harness.runtime.harness import RunRecord
from yanxu_harness.storage import SqlitePersistence


def run(coro: Any, *, timeout: float = 60.0) -> Any:
    """Run one scenario with a guard.

    A scripted stream that repeats its last message plus a run with no turn cap is an
    infinite loop, not a slow test; the guard turns that mistake into a failure.
    """

    async def guarded() -> Any:
        return await asyncio.wait_for(coro, timeout=timeout)

    return asyncio.run(guarded())


def events(paths: DeploymentPaths) -> list[dict[str, Any]]:
    """The durable events, with payloads, read through a separate connection."""
    store = SqlitePersistence(paths.runs)
    try:
        return [
            {"kind": item.kind, "payload": dict(item.payload or {}), "at": item.at}
            for item in run(store.read_events())
        ]
    finally:
        store.close()


def kinds(paths: DeploymentPaths) -> list[str]:
    return [item["kind"] for item in events(paths)]


def read_record(paths: DeploymentPaths) -> RunRecord:
    store = SqlitePersistence(paths.runs)
    try:
        stored = run(store.load_run("r1"))
    finally:
        store.close()
    assert stored is not None
    return RunRecord.from_record(stored.state)


def test_a_request_instance_is_not_the_logical_model_action(tmp_path: Path) -> None:
    """One logical action, one real request instance — and the instance is in the record."""
    paths = DeploymentPaths.under(tmp_path / "state")
    harness, _gateway = build(
        paths,
        ScriptedStream(tool_call_message("hello"), final_message("done")),
        attempt="a1",
        auto_write=True,
    )
    outcome = run(harness.start(RunRequest(prompt="write it"), run_context("a1")))
    assert outcome.status is RunStatus.COMPLETED

    dispatched = [item for item in events(paths) if item["kind"] == "model_request_dispatched"]
    completed = [item for item in events(paths) if item["kind"] == "model_request_completed"]
    assert len(dispatched) == 2, "two real requests, two dispatches"
    assert len(completed) == 2
    instances = [item["payload"]["request_instance"] for item in dispatched]
    assert len(set(instances)) == 2, "each real request has its own identity"
    assert all(instance.startswith("a1:") for instance in instances), (
        "the instance names the attempt that dispatched it, so a recovery cannot be "
        f"mistaken for the original: {instances}"
    )
    assert instances[0].startswith("a1:request:1#"), instances[0]
    assert instances[1].startswith("a1:request:2#"), instances[1]
    # The logical action is stable across turns only in the sense that it is the action's
    # name; the instance is what distinguishes the two real requests.
    assert [item["payload"]["model_action_id"] for item in dispatched] == [
        "request:1",
        "request:2",
    ]


def test_the_dispatch_window_is_durable_before_the_response(tmp_path: Path) -> None:
    """The start is committed before the send; the completion with the response."""
    paths = DeploymentPaths.under(tmp_path / "state")
    harness, _gateway = build(
        paths, ScriptedStream(tool_call_message("hello"), final_message("done")), attempt="a1", auto_write=True
    )
    run(harness.start(RunRequest(prompt="write it"), run_context("a1")))

    order = kinds(paths)
    assert order.index("model_request_dispatched") < order.index("model_request_completed"), (
        "the dispatch is a fact before the response that answers it"
    )
    assert order[0] == "run_started"

    # The start event carries a wall-clock instant (placeable in time) and no duration:
    # whether it finished is a *later* fact, and a start alone must not imply one.
    first = order.index("model_request_dispatched")
    start_event = events(paths)[first]
    assert start_event["payload"]["started_at"] is not None
    assert start_event["payload"]["attempt"] == "a1"
    assert "duration_ms" not in start_event["payload"]


def test_the_completion_carries_duration_clock_size_and_usage(tmp_path: Path) -> None:
    paths = DeploymentPaths.under(tmp_path / "state")
    harness, _gateway = build(
        paths, ScriptedStream(tool_call_message("hello"), final_message("done")), attempt="a1", auto_write=True
    )
    run(harness.start(RunRequest(prompt="write it"), run_context("a1")))

    completed = [item for item in events(paths) if item["kind"] == "model_request_completed"]
    assert completed, "the response is a measurement, not only a message"
    first = completed[0]["payload"]
    assert first["duration_ms"] is not None and first["duration_ms"] >= 0
    assert first["clock"] == "monotonic", "a duration comes from a monotonic pair"
    assert first["interrupted"] is False
    assert first["payload_bytes"] is not None and first["payload_bytes"] > 0
    assert first["payload_bytes_estimator"] == "harness:json-utf8-v1", (
        "a byte count must say how it was measured"
    )
    assert first["started_at"] is not None
    # The scripted stream reports nothing: that is *unknown*, and it is recorded as absent
    # rather than as a mapping of nulls a reader could sum into a zero.
    assert "usage" in first
    assert first["usage"] is None


def test_an_interrupted_request_leaves_a_start_and_no_duration(tmp_path: Path) -> None:
    """A request that never came back is readable as started-and-unfinished."""
    paths = DeploymentPaths.under(tmp_path / "state")
    entered = asyncio.Event()
    release = asyncio.Event()

    class Blocking:
        """A stream that blocks until the test cancels the run."""

        def __init__(self) -> None:
            self.calls = 0

        async def __call__(self, model: Any, context: Any, options: Any = None) -> Any:
            self.calls += 1
            entered.set()
            return self

        def __aiter__(self) -> Any:
            return self

        async def __anext__(self) -> Any:
            raise StopAsyncIteration

        async def result(self) -> Any:
            await release.wait()
            return final_message("never")

    stream = Blocking()
    harness, _gateway = build(paths, stream, attempt="a1", auto_write=True)

    async def scenario() -> Any:
        started = asyncio.ensure_future(
            harness.start(RunRequest(prompt="write it"), run_context("a1"))
        )
        await asyncio.wait_for(entered.wait(), timeout=10.0)
        await harness.cancel("r1", "operator stopped it")
        return await started

    outcome = run(scenario())
    assert outcome.status is RunStatus.CANCELLED

    dispatched = [item for item in events(paths) if item["kind"] == "model_request_dispatched"]
    assert len(dispatched) == 1, "the dispatch is durable even though the request never finished"
    assert not [item for item in events(paths) if item["kind"] == "model_request_completed"]

    # The committed snapshot says the same thing: the dispatch is outstanding, and there is
    # no measurement of it. Not a zero, and not a success.
    snapshot = state_from_record(read_record(paths).execution)
    assert snapshot.request_instance, "the outstanding dispatch is recorded"
    assert snapshot.request_started_at is not None
    assert snapshot.last_request_measurement is None


def test_a_recovered_request_is_a_new_instance(tmp_path: Path) -> None:
    """A request re-issued by a recovery is a second real request, not a replay."""
    paths = DeploymentPaths.under(tmp_path / "state")
    # No ``auto_write``: the call has to *wait* for a decision, which is the pause whose
    # resume is the second real request.
    harness, gateway = build(paths, ScriptedStream(tool_call_message("hello")), attempt="a1")
    paused = run(
        harness.start(RunRequest(prompt="write it", max_turns=4), run_context("a1"))
    )
    assert paused.status is RunStatus.SUSPENDED

    key = paused.pending_interaction["approval_binding_key"]
    from yanxu_harness.tools import ApprovalStatus

    async def approve() -> None:
        await gateway._approvals.decide(  # noqa: SLF001 - the trusted store is the point
            key, ApprovalStatus.APPROVED, decided_by="operator"
        )

    run(approve())

    second, _gateway = build(
        paths, ScriptedStream(final_message("done")), attempt="a2"
    )
    resumed = run(second.resume("r1", {}, run_context("a2")))
    assert resumed.status is RunStatus.COMPLETED

    instances = [
        item["payload"]["request_instance"]
        for item in events(paths)
        if item["kind"] == "model_request_dispatched"
    ]
    assert len(instances) == 2, instances
    assert instances[0].startswith("a1:request:1#"), instances[0]
    assert instances[1].startswith("a2:"), (
        "the resumed invocation dispatches its own real request, and the attempt is part of "
        f"the instance so the two are never merged: {instances[1]}"
    )
    assert instances[0] != instances[1]
    completed = [
        item["payload"]["request_instance"]
        for item in events(paths)
        if item["kind"] == "model_request_completed"
    ]
    assert sorted(completed) == sorted(instances), "each dispatch has its own completion"


def test_each_tool_call_records_its_own_span(tmp_path: Path) -> None:
    """A call's span is durable, and it survives the turn that clears the partition.

    The batch partition is *current state*: the next turn clears it. A report about a run
    tomorrow still has to be able to say how long the call took, so the span is committed
    as its own fact when the observation is — with the ids and numbers, never the content.
    """
    paths = DeploymentPaths.under(tmp_path / "state")
    harness, _gateway = build(
        paths, ScriptedStream(tool_call_message("hello"), final_message("done")), attempt="a1", auto_write=True
    )
    run(harness.start(RunRequest(prompt="write it"), run_context("a1")))

    observed = [item for item in events(paths) if item["kind"] == "tool_call_observed"]
    assert len(observed) == 1, observed
    payload = observed[0]["payload"]
    assert payload["call_id"] == "call-1"
    assert payload["outcome"] == "settled"
    assert payload["started_at"] is not None
    assert payload["duration_ms"] is not None and payload["duration_ms"] >= 0
    assert payload["clock"] == "monotonic"
    assert payload["interrupted"] is False
    assert payload["model_action_id"] == "request:1"
    assert payload["attempt"] == "a1"
    assert "arguments" not in payload and "result" not in payload, (
        "the event carries the span, not the call's content"
    )
    # The final snapshot has moved on (the batch belongs to the turn it was declared in),
    # which is exactly why the span had to be committed as its own fact.
    snapshot = state_from_record(read_record(paths).execution)
    assert snapshot.phase is Phase.FINISHED
    assert snapshot.batch == ()


def test_timing_is_not_part_of_the_observation_identity() -> None:
    """A redelivery of the same facts stays idempotent even if it was measured again."""
    from yanxu_harness.core.tool_execution import FinalizedToolCall, observation_of
    from yanxu_harness.core.types import ToolCall, ToolResultMessage

    call = ToolCall(id="call-1", name="notes.write", arguments={"path": "a.md"})
    finalized = FinalizedToolCall(
        tool_call=call,
        result={"content": [], "details": {"status": "executed", "operation_id": "op-1"}},
        is_error=False,
    )
    message = ToolResultMessage(tool_call_id="call-1", tool_name="notes.write", content=[])
    fast = observation_of(0, finalized, message, started_at=1.0, duration_ms=1.0)
    slow = observation_of(0, finalized, message, started_at=2.0, duration_ms=99.0)
    assert fast.observed_digest == slow.observed_digest, (
        "timing is not identity: the digest must not change because a call took longer"
    )
    assert fast.duration_ms != slow.duration_ms, "but the measurement itself is kept"


def test_a_call_dispatched_again_in_a_recovery_has_two_observations(tmp_path: Path) -> None:
    """Each real dispatch is its own fact, attributed to the attempt that made it.

    The paused run's call was *observed* once as "waiting for a decision" (nothing ran) and
    again, after the approval, as "executed" — by a different attempt. Neither observation
    overwrites the other: the history says what each attempt learned, and the current
    partition says which one is still true.

    Both spans exist and mean different things: the first is the dispatch that decided to
    wait (the approval check ran, no handler did), the second is the dispatch that ran the
    handler. The outcome is what distinguishes them — a span is not a claim that an effect
    happened.
    """
    paths = DeploymentPaths.under(tmp_path / "state")
    harness, gateway = build(paths, ScriptedStream(tool_call_message("hello")), attempt="a1")
    paused = run(harness.start(RunRequest(prompt="write it", max_turns=4), run_context("a1")))
    assert paused.status is RunStatus.SUSPENDED

    from yanxu_harness.tools import ApprovalStatus

    key = paused.pending_interaction["approval_binding_key"]

    async def approve() -> None:
        await gateway._approvals.decide(key, ApprovalStatus.APPROVED, decided_by="operator")  # noqa: SLF001

    run(approve())

    second, _gateway = build(paths, ScriptedStream(final_message("done")), attempt="a2")
    assert run(second.resume("r1", {}, run_context("a2"))).status is RunStatus.COMPLETED

    observed = [item["payload"] for item in events(paths) if item["kind"] == "tool_call_observed"]
    assert len(observed) == 2, observed
    waiting, executed = observed
    assert (waiting["attempt"], waiting["outcome"]) == ("a1", "awaiting")
    assert waiting["duration_ms"] is not None and waiting["duration_ms"] >= 0, (
        "the dispatch that decided to wait was measured too"
    )
    assert (executed["attempt"], executed["outcome"]) == ("a2", "settled")
    assert executed["duration_ms"] is not None and executed["duration_ms"] >= 0
    assert waiting["call_id"] == executed["call_id"] == "call-1"
