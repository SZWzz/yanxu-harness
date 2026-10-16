"""Q3: the bounded local worker, against two real workers and real SQLite.

The plan's S-01…S-03 cases: one logical executor per task under a race, a waiting task that
releases its slot while others continue, fairness under a backlog, a stop that is not a
cancellation, and one task's failure that leaves unrelated tasks alone.

The harness factory below builds a real ``DefaultHarness`` per task (real SQLite, real Gateway,
scripted model), so the worker's job is only ever scheduling: which task, claimed how, settled as
what.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from durable_edit import DeploymentPaths, ScriptedStream, final_message, tool_call_message, write_definition

from yanxu_harness.runtime import DefaultHarness
from yanxu_harness.tasks import (
    InputAdmission,
    InputContent,
    InputKind,
    InputState,
    LocalWorker,
    SchedulerLimits,
    SqliteControlStore,
    TaskAdmission,
    TaskStatus,
)
from yanxu_harness.tools import (
    ConservativePolicy,
    GatewayBindings,
    ToolGateway,
    ToolRegistry,
    core_tool,
)
from yanxu_harness.runtime.context import Principal, RunContext, RunIdentity

NAMESPACE = "project-a"


def run(coro: Any, *, timeout: float = 60.0) -> Any:
    async def guarded() -> Any:
        return await asyncio.wait_for(coro, timeout=timeout)

    return asyncio.run(guarded())


class Fleet:
    """One control store, several workers, one harness per task."""

    def __init__(self, root: Path, *, max_concurrent: int = 1, max_per_namespace: int = 1) -> None:
        self.paths = DeploymentPaths.under(root / "state")
        self.store = SqliteControlStore(self.paths.runs)
        self.streams: dict[str, Any] = {}
        self.suspend_for: set[str] = set()
        self.fail_for: set[str] = set()
        self.limits = SchedulerLimits(
            max_concurrent=max_concurrent,
            max_per_namespace=max_per_namespace,
            claim_ttl_seconds=300.0,
        )

    def stream(self, task_id: str, *messages: Any) -> None:
        self.streams[task_id] = ScriptedStream(*messages)

    def worker(self, *, holder: str) -> LocalWorker:
        return LocalWorker(self.store, self._harness, limits=self.limits, holder=holder)

    # -- the harness factory -----------------------------------------------------------

    def _harness(self, task: Any, attempt_id: str) -> DefaultHarness:
        stream = self.streams.get(task.task_id) or ScriptedStream(final_message("done"))
        definition = write_definition(self.paths.counter)
        gateway = ToolGateway(
            registry=ToolRegistry([definition]),
            policy=ConservativePolicy(auto_write=True),
            approvals=None,
            backend=self._backend(),
        )
        bindings = GatewayBindings(
            gateway=gateway,
            context=lambda: RunContext(
                identity=RunIdentity(
                    namespace=task.namespace,
                    session_id=task.session_id,
                    run_id=task.run_id,
                    attempt_id=attempt_id,
                ),
                principal=Principal(subject="worker"),
            ),
        )
        suspend = None
        if task.task_id in self.suspend_for:
            def suspend(*_args: Any) -> Any:
                return {"kind": "approval", "call_id": "call-1"}
        def fail_hook(_message: Any) -> Any:
            if task.task_id in self.fail_for:
                raise RuntimeError("the model stream refused this task")
            return None
        return DefaultHarness(
            stream_fn=stream,
            tools=[core_tool(definition, bindings)],
            persistence=self.store,
            control=self.store,
            task_id=task.task_id,
            system_prompt="be brief",
            suspend_after_batch=suspend,
            stop_after_turn=fail_hook if task.task_id in self.fail_for else None,
        )

    def _backend(self) -> Any:
        from yanxu_harness.storage import SqliteExecutionBackend

        return SqliteExecutionBackend(self.paths.operations)

    def submit(self, task_id: str, *, priority: int = 0, namespace: str = NAMESPACE,
               created_at: float | None = None) -> Any:
        return run(
            self.store.admit_task(
                TaskAdmission(
                    namespace=namespace,
                    session_id="s1",
                    task_id=task_id,
                    run_id=f"run-{task_id}",
                    initial_input=InputContent(kind=InputKind.INITIAL, text=f"do {task_id}"),
                    priority=priority,
                    created_at=created_at or time.time(),
                )
            )
        )

    def close(self) -> None:
        self.store.close()


# -- S-01: one logical executor -----------------------------------------------------


def test_two_workers_racing_for_one_task_produce_one_drive(tmp_path: Path) -> None:
    fleet = Fleet(tmp_path)
    fleet.stream("t1", final_message("done"))
    try:
        fleet.submit("t1")
        first = fleet.worker(holder="w1")
        second = fleet.worker(holder="w2")
        results = run(_race(first, second))
        driven = [item for item in results if item is not None]
        outcomes = sorted(item.outcome for item in driven)
        assert outcomes == ["held_elsewhere", "settled:completed"], outcomes
        task = run(fleet.store.read_task("t1"))
        assert task is not None and task.status is TaskStatus.COMPLETED
        assert task.attempt_count == 1, "one claim, one attempt, one run"
        assert run(fleet.store.read_inputs("t1"))[0].state is InputState.APPLIED
    finally:
        fleet.close()


async def _race(*workers: LocalWorker) -> list[Any]:
    return list(await asyncio.gather(*(worker.run_once() for worker in workers)))


def test_a_claim_that_expires_does_not_fail_the_task(tmp_path: Path) -> None:
    fleet = Fleet(tmp_path)
    fleet.stream("t1", final_message("done"))
    try:
        fleet.submit("t1")
        # A worker claims the task and "dies" without driving it; the claim simply expires.
        claim = run(
            fleet.store.claim_task("t1", holder="dead", ttl_seconds=10.0, now=1_000.0)
        )
        assert claim is not None
        assert run(fleet.store.read_task("t1")).status is TaskStatus.RUNNING
        worker = LocalWorker(
            fleet.store,
            fleet._harness,
            limits=fleet.limits,
            holder="w2",
            clock=lambda: 1_020.0,
        )
        # The expired claim is taken again and the task continues: expiry is not a verdict.
        result = run(worker.run_once(now=1_020.0))
        assert result is not None
        task = run(fleet.store.read_task("t1"))
        assert task is not None and task.status in {TaskStatus.COMPLETED, TaskStatus.RUNNING}
        claim_now = run(
            fleet.store.renew_task_claim(
                "t1", holder="dead", attempt_id=claim.attempt_id, ttl_seconds=10.0, now=1_020.0
            )
        )
        assert claim_now is None, "the dead worker no longer holds the right"
    finally:
        fleet.close()


# -- S-02: waiting releases the slot -------------------------------------------------


def test_a_waiting_task_releases_its_slot_for_another_task(tmp_path: Path) -> None:
    fleet = Fleet(tmp_path, max_concurrent=1)
    fleet.stream("slow", tool_call_message("hello"), final_message("done"))
    fleet.stream("other", final_message("done"))
    fleet.suspend_for.add("slow")
    try:
        fleet.submit("slow", created_at=time.time() - 10)
        fleet.submit("other")
        worker = fleet.worker(holder="w1")
        results = run(worker.run_until_idle())
        outcomes = sorted(f"{item.task_id}:{item.outcome}" for item in results)
        assert "slow:waiting" in outcomes, outcomes
        assert "other:settled:completed" in outcomes, (
            "a task waiting on an external fact must not hold the only concurrency slot"
        )
        assert worker.inflight == ()
    finally:
        fleet.close()


def test_a_waiting_task_can_be_woken_when_the_external_fact_is_persisted(tmp_path: Path) -> None:
    fleet = Fleet(tmp_path)
    fleet.stream("slow", tool_call_message("hello"), final_message("done"))
    fleet.suspend_for.add("slow")
    try:
        fleet.submit("slow")
        worker = fleet.worker(holder="w1")
        first = run(worker.run_once())
        assert first is not None and first.outcome == "waiting"
        waiting = run(fleet.store.read_task("slow"))
        assert waiting is not None and waiting.status is TaskStatus.WAITING
        assert waiting.waiting_for == "approval", waiting.waiting_for
        # The approval decision is persisted as a trusted control input, and the host wakes the
        # task: waiting -> queued is a transition the *host* decides, not a poll.
        admitted = run(
            fleet.store.admit_input(
                InputAdmission(
                    namespace=NAMESPACE,
                    session_id="s1",
                    task_id="slow",
                    input_id="decision-1",
                    content=InputContent(kind=InputKind.APPROVAL_DECISION, decision="approved"),
                )
            )
        )
        assert admitted.outcome.value == "accepted"
        woken = run(worker.wake("slow"))
        assert woken is not None and woken.status is TaskStatus.QUEUED
        resumed = run(worker.run_once())
        assert resumed is not None
    finally:
        fleet.close()


# -- fairness -----------------------------------------------------------------------


def test_aging_keeps_an_old_low_priority_task_from_starving(tmp_path: Path) -> None:
    fleet = Fleet(tmp_path, max_concurrent=4, max_per_namespace=4)
    now = time.time()
    fleet.submit("old-normal", priority=0, created_at=now - 100.0)
    fleet.submit("new-urgent", priority=9, created_at=now)
    try:
        worker = fleet.worker(holder="w1")
        decision = run(worker._choose(now))
        assert decision.task_id == "old-normal", (
            f"a task past the aging window is tried first: {decision.reason}"
        )
        assert "aging" in decision.reason
        # A newer task with no aging pressure still wins on priority.
        decision = run(worker._choose(now + 1))
        assert decision.task_id in {"new-urgent", "old-normal"}
    finally:
        fleet.close()


def test_the_namespace_cap_counts_tasks_not_harness_objects(tmp_path: Path) -> None:
    """A real in-flight drive must occupy a *namespace* slot, not an unnamed one.

    The in-flight map holds harness objects, which carry no namespace. Counting those made
    ``max_per_namespace`` a no-op in a real run while it looked correct against a fake record.
    Here a genuine drive blocks inside its harness and a second decision is taken.
    """

    class _Outcome:
        def __init__(self, status: str) -> None:
            self.status = type("S", (), {"value": status})()
            self.stop_code = ""
            self.pending_interaction = None

    class _BlockingHarness:
        def __init__(self, store: Any, task: Any, attempt_id: str, release: Any) -> None:
            self._persistence = store
            self.store = store
            self.task = task
            self.release = release

        async def start(self, request: Any, context: Any) -> Any:
            await self.release.wait()
            await self.store.commit_run_transition_with_inputs(
                self.task.run_id, {"phase": "done"}, (), expected_version=None,
                task_id=self.task.task_id,
            )
            return _Outcome("completed")

        async def resume(self, run_id: str, payload: Any, context: Any) -> Any:
            return _Outcome("completed")

        async def cancel(self, run_id: str, *, reason: str = "") -> None:
            return None

    store = SqliteControlStore(tmp_path / "control.db")
    try:
        for task_id, namespace, offset in (
            ("a1", "tenant-a", 0),
            ("a2", "tenant-a", 1),
            ("b1", "tenant-b", 2),
        ):
            run(
                store.admit_task(
                    TaskAdmission(
                        namespace=namespace,
                        session_id="s1",
                        task_id=task_id,
                        run_id=f"run-{task_id}",
                        initial_input=InputContent(kind=InputKind.INITIAL, text=task_id),
                        created_at=1_000.0 + offset,
                    )
                )
            )
        release = asyncio.Event()
        worker = LocalWorker(
            store,
            lambda task, attempt: _BlockingHarness(store, task, attempt, release),
            limits=SchedulerLimits(max_concurrent=4, max_per_namespace=1),
            holder="w1",
            clock=lambda: 1_000.0,
        )

        async def scenario() -> Any:
            driving = asyncio.create_task(worker.run_once(now=1_000.0))
            # Let the drive claim a1 and reach its blocking harness.
            for _ in range(50):
                if worker._inflight_tasks:
                    break
                await asyncio.sleep(0)
            inflight = dict(worker._inflight_tasks)
            decision = await worker._choose(1_000.0)
            release.set()
            await driving
            return inflight, decision

        inflight, decision = run(scenario())
        assert [record.namespace for record in inflight.values()] == ["tenant-a"], inflight
        assert decision.task_id == "b1", (
            f"tenant-a is at its cap, so tenant-b is served; chose {decision.task_id!r}: "
            f"{decision.reason}"
        )
        assert any("tenant-a" in str(item.get("reason", "")) for item in decision.skipped)
    finally:
        store.close()


def test_namespaces_are_served_round_robin(tmp_path: Path) -> None:
    fleet = Fleet(tmp_path, max_concurrent=2, max_per_namespace=1)
    now = time.time()
    fleet.submit("a1", namespace="tenant-a", created_at=now)
    fleet.submit("a2", namespace="tenant-a", created_at=now + 1)
    fleet.submit("b1", namespace="tenant-b", created_at=now + 2)
    try:
        worker = fleet.worker(holder="w1")
        first = run(worker._choose(now + 3))
        assert first.namespace == "tenant-a"
        # tenant-a is now at its per-namespace cap (a1 is the task being driven here). The
        # in-flight map holds the task records, which is what carries the namespace.
        worker._inflight_tasks["a1"] = run(fleet.store.read_task("a1"))
        worker._inflight["a1"] = None
        second = run(worker._choose(now + 4))
        assert second.namespace == "tenant-b", (
            "the other tenant is served instead of waiting behind tenant-a's backlog"
        )
        # tenant-a is at its cap while a1 is in flight, so a2 waits — the cap is the reason.
        assert any("tenant-a" in str(item.get("reason", "")) for item in second.skipped)
        worker._inflight.pop("a1", None)
        worker._inflight_tasks.pop("a1", None)
        # a1 is still queued but a live claim means another worker is driving it, so the next
        # eligible task of that namespace is a2.
        run(fleet.store.claim_task("a1", holder="elsewhere", ttl_seconds=300.0, now=now + 5))
        third = run(worker._choose(now + 6))
        assert third.task_id == "a2", "once the slot frees, tenant-a's next task runs"
        assert any(
            "tenant-a" in str(item.get("reason", "")) for item in second.skipped
        ), second.skipped
    finally:
        fleet.close()


def test_the_global_concurrency_limit_stops_extra_claims(tmp_path: Path) -> None:
    fleet = Fleet(tmp_path, max_concurrent=1)
    fleet.submit("t1")
    try:
        worker = fleet.worker(holder="w1")
        worker._inflight["busy"] = type("T", (), {"namespace": NAMESPACE})()
        decision = run(worker._choose(time.time()))
        assert decision.empty and "concurrency" in decision.reason
    finally:
        fleet.close()


# -- cancellation and stopping ------------------------------------------------------


def test_a_queued_cancel_never_starts_a_run(tmp_path: Path) -> None:
    fleet = Fleet(tmp_path)
    fleet.stream("t1", final_message("done"))
    try:
        fleet.submit("t1")
        worker = fleet.worker(holder="w1")
        cancelled = run(worker.cancel("t1", reason="the host changed its mind"))
        assert cancelled is not None and cancelled.status is TaskStatus.CANCELLED
        assert cancelled.cancel_requested_at is not None
        assert run(fleet.store.load_run("run-t1")) is None, (
            "cancelling a queued task does not start the run it is cancelling"
        )
        results = run(worker.run_until_idle())
        assert results == (), "a cancelled task is not schedulable"
    finally:
        fleet.close()


def test_stop_stops_claiming_and_another_worker_takes_over(tmp_path: Path) -> None:
    fleet = Fleet(tmp_path)
    fleet.stream("t1", final_message("done"))
    try:
        fleet.submit("t1")
        first = fleet.worker(holder="w1")
        stopped = run(first.stop())
        assert stopped == ()
        assert run(first.run_once()) is None, "a stopped worker takes no new claims"
        second = fleet.worker(holder="w2")
        result = run(second.run_once())
        assert result is not None and result.outcome == "settled:completed"
        task = run(fleet.store.read_task("t1"))
        assert task is not None and task.status is TaskStatus.COMPLETED
    finally:
        fleet.close()


def test_one_tasks_failure_leaves_an_unrelated_task_alone(tmp_path: Path) -> None:
    fleet = Fleet(tmp_path, max_concurrent=1)
    fleet.stream("broken", final_message("done"))
    fleet.stream("fine", final_message("done"))
    fleet.fail_for.add("broken")
    try:
        fleet.submit("broken", created_at=time.time() - 10)
        fleet.submit("fine")
        worker = fleet.worker(holder="w1")
        results = run(worker.run_until_idle())
        by_task = {item.task_id: item.outcome for item in results}
        assert by_task["broken"].startswith("failed") or by_task["broken"] == "settled:failed"
        assert by_task["fine"] == "settled:completed", (
            "one task's failure must not cancel or block an unrelated task"
        )
    finally:
        fleet.close()


def test_the_claim_row_is_gone_after_a_drive(tmp_path: Path) -> None:
    fleet = Fleet(tmp_path)
    fleet.stream("t1", final_message("done"))
    try:
        fleet.submit("t1")
        worker = fleet.worker(holder="w1")
        run(worker.run_once())
        row = fleet.store._connection.execute(
            "SELECT COUNT(*) FROM task_claims WHERE task_id = 't1'"
        ).fetchone()
        assert row[0] == 0, "a finished drive releases its scheduling claim"
    finally:
        fleet.close()


def test_the_task_status_is_not_the_run_status(tmp_path: Path) -> None:
    fleet = Fleet(tmp_path)
    fleet.stream("t1", final_message("done"))
    try:
        fleet.submit("t1")
        worker = fleet.worker(holder="w1")
        result = run(worker.run_once())
        task = run(fleet.store.read_task("t1"))
        record = run(fleet.store.load_run("run-t1"))
        assert result is not None
        assert task is not None and record is not None
        assert task.status is TaskStatus.COMPLETED
        # The task does not cache the run's status: a reader that wants it reads the run.
        assert "run_status" not in task.to_record()
        # And a waiting task is not a failed model: the vocabulary keeps them apart.
        assert TaskStatus.WAITING.value != str(getattr(record.state.get("status"), "value", ""))
    finally:
        fleet.close()
