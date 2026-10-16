"""Regressions for the runner-level orchestration defects.

Four of the reported problems live below the control plane, where a scripted stub is not enough:

* a run that finished but whose task was never settled made recovery report a failure;
* an input admitted after more than a page of history stayed ``pending`` while the run finished;
* a follow-up admitted before the run started was merged into the opening turn;
* a parent whose run finished while its child was still active failed to settle once the child
  returned.

The last one uses a **real** ``DefaultHarness`` for both the parent and the child: scripted model,
real Runner, real SQLite and the real terminal barrier. That is the proof the stubbed examples
cannot give.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
from durable_edit import DeploymentPaths, ScriptedStream, final_message, write_definition

from yanxu_harness.core.execution import InputKind as CoreInputKind
from yanxu_harness.core.execution import PollSite
from yanxu_harness.runtime import DefaultHarness
from yanxu_harness.runtime.harness import RunRecord, RunStatus
from yanxu_harness.runtime.context import Principal, RunContext, RunIdentity
from yanxu_harness.storage import SqliteExecutionBackend
from yanxu_harness.tasks import (
    BacklogLimits,
    ChildCoordinator,
    ChildOutcome,
    ChildPolicy,
    ChildSpawnRequest,
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
from yanxu_harness.tasks.inputs import PersistentInputReader
from yanxu_harness.tools import (
    ConservativePolicy,
    GatewayBindings,
    ToolGateway,
    ToolRegistry,
    core_tool,
)

NAMESPACE = "project-a"
SESSION = "s1"


def run(coro: Any, *, timeout: float = 60.0) -> Any:
    async def guarded() -> Any:
        return await asyncio.wait_for(coro, timeout=timeout)

    return asyncio.run(guarded())


def _admit(store: Any, task_id: str, *, profile: str = "worker") -> Any:
    return run(
        store.admit_task(
            TaskAdmission(
                namespace=NAMESPACE,
                session_id=SESSION,
                task_id=task_id,
                run_id=f"run-{task_id}",
                initial_input=InputContent(kind=InputKind.INITIAL, text=f"do {task_id}"),
                profile=profile,
            )
        )
    )


# -- defect: a finished run whose task was not settled ---------------------------------


def test_a_run_that_finished_before_its_task_was_settled_is_settled_on_recovery(
    tmp_path: Path,
) -> None:
    store = SqliteControlStore(tmp_path / "control.db")
    try:
        _admit(store, "t1")

        class _Harness:
            """The run store is reachable, so the terminal run is found; start must not run."""

            def __init__(self, store: Any, task: Any) -> None:
                self._persistence = store
                self.store = store
                self.task = task
                self.started = False

            async def start(self, request: Any, context: Any) -> Any:
                self.started = True
                raise AssertionError("a finished run must be settled, not started again")

            async def resume(self, run_id: str, payload: Any, context: Any) -> Any:
                raise AssertionError("a finished run must be settled, not resumed")

            async def cancel(self, run_id: str, *, reason: str = "") -> None:
                return None

        harnesses: list[_Harness] = []

        def harness_for(task: Any, attempt_id: str) -> _Harness:
            harness = _Harness(store, task)
            harnesses.append(harness)
            return harness

        # A worker claimed the task, committed the run as completed, then died before settling.
        claim = run(store.claim_task("t1", holder="dead", ttl_seconds=1.0, now=1_000.0))
        assert claim is not None
        finished = RunRecord(
            run_id="run-t1",
            namespace=NAMESPACE,
            session_id=SESSION,
            status=RunStatus.COMPLETED,
            attempt_id="a1",
            updated_at=1_000.0,
        )
        run(
            store.commit_run_transition_with_inputs(
                "run-t1",
                finished.to_record() if hasattr(finished, "to_record") else finished.__dict__,
                (),
                expected_version=None,
                task_id="t1",
            )
        )
        worker = LocalWorker(
            store,
            harness_for,
            limits=SchedulerLimits(max_concurrent=1, claim_ttl_seconds=300.0),
            holder="w2",
            clock=lambda: 2_000.0,
        )
        result = run(worker.run_once(now=2_000.0))
        assert result is not None
        assert result.outcome == "settled:completed", result
        task = run(store.read_task("t1"))
        assert task.status is TaskStatus.COMPLETED
        assert all(not harness.started for harness in harnesses)
    finally:
        store.close()


# -- defect: a new input hidden behind more than a page of history ----------------------


def test_a_new_input_is_seen_after_more_than_a_page_of_history(tmp_path: Path) -> None:
    store = SqliteControlStore(
        tmp_path / "control.db",
        # A raised per-task bound on purpose: the point is a *long history*, not the default
        # backlog, and the reader must still find the newest input.
        backlog=BacklogLimits(
            per_task=1000, per_namespace=5000, total=5000,
            max_payload_bytes=1_048_576, max_task_bytes=8_388_608,
        ),
    )
    try:
        _admit(store, "t1")
        claim = run(store.claim_task("t1", holder="w", ttl_seconds=300.0))
        assert claim is not None
        # 250 steering inputs are admitted and applied: the run has more than one page of history.
        for index in range(250):
            admitted = run(
                store.admit_input(
                    InputAdmission(
                        namespace=NAMESPACE,
                        session_id=SESSION,
                        task_id="t1",
                        input_id=f"steer-{index}",
                        content=InputContent(kind=InputKind.STEERING, text=f"text {index}"),
                    )
                )
            )
            assert admitted.record is not None
        records = run(store.read_inputs("t1", limit=1000))
        applied = tuple(record for record in records if record.kind is InputKind.STEERING)
        run(
            store.commit_run_transition_with_inputs(
                "run-t1",
                {"phase": "collected"},
                (),
                expected_version=None,
                applied_inputs=applied,
                claim=claim,
                task_id="t1",
            )
        )
        assert run(store.read_applied_watermark("t1")) == 250
        # A brand-new input must still be visible to the reader, not hidden behind the history.
        run(
            store.admit_input(
                InputAdmission(
                    namespace=NAMESPACE,
                    session_id=SESSION,
                    task_id="t1",
                    input_id="the-new-one",
                    content=InputContent(kind=InputKind.STEERING, text="the new one"),
                )
            )
        )
        reader = PersistentInputReader(store, "t1")
        poll = run(reader(PollSite.AFTER_PREPARE, CoreInputKind.STEERING))
        texts = [getattr(message, "content", "") for message in poll.messages]
        assert texts == ["the new one"], texts
    finally:
        store.close()


# -- defect: a follow-up merged into the opening turn ----------------------------------


def test_a_follow_up_is_not_read_at_the_opening_steering_site(tmp_path: Path) -> None:
    store = SqliteControlStore(tmp_path / "control.db")
    try:
        _admit(store, "t1")
        run(
            store.admit_input(
                InputAdmission(
                    namespace=NAMESPACE,
                    session_id=SESSION,
                    task_id="t1",
                    input_id="follow-1",
                    content=InputContent(kind=InputKind.FOLLOW_UP, text="a later follow-up"),
                )
            )
        )
        reader = PersistentInputReader(store, "t1")
        opening = run(reader(PollSite.INITIAL, CoreInputKind.STEERING))
        assert opening.messages == (), (
            "a follow-up must not be merged into the opening (steering) turn"
        )
        boundary = run(reader(PollSite.AFTER_INNER, CoreInputKind.FOLLOW_UP))
        texts = [getattr(message, "content", "") for message in boundary.messages]
        assert texts == ["a later follow-up"], texts
    finally:
        store.close()


# -- defect: a parent whose run finished while a child was active -----------------------


class _Fleet:
    """One control store, one worker, and a real ``DefaultHarness`` for every task."""

    def __init__(self, root: Path) -> None:
        self.paths = DeploymentPaths.under(root / "state")
        self.store = SqliteControlStore(self.paths.runs)
        self.streams: dict[str, Any] = {}
        self.coordinator: ChildCoordinator | None = None
        self.limits = SchedulerLimits(max_concurrent=1, max_per_namespace=1, claim_ttl_seconds=300.0)

    def stream(self, task_id: str, *messages: Any) -> None:
        self.streams[task_id] = ScriptedStream(*messages)

    def worker(self, *, holder: str, coordinator: ChildCoordinator | None = None) -> LocalWorker:
        return LocalWorker(
            self.store,
            self._harness,
            limits=self.limits,
            holder=holder,
            child_barrier=None if coordinator is None else coordinator.hold_for_children,
        )

    def _harness(self, task: Any, attempt_id: str) -> DefaultHarness:
        stream = self.streams.get(task.task_id) or ScriptedStream(final_message("done"))
        definition = write_definition(self.paths.counter)
        gateway = ToolGateway(
            registry=ToolRegistry([definition]),
            policy=ConservativePolicy(auto_write=True),
            approvals=None,
            backend=SqliteExecutionBackend(self.paths.operations),
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
        return DefaultHarness(
            stream_fn=stream,
            tools=[core_tool(definition, bindings)],
            persistence=self.store,
            control=self.store,
            task_id=task.task_id,
            system_prompt="be brief",
            finish_barrier=(
                None if self.coordinator is None else self.coordinator.finish_interaction
            ),
        )

    def close(self) -> None:
        self.store.close()


def test_a_parent_settles_after_its_child_completes_using_the_real_runner(tmp_path: Path) -> None:
    fleet = _Fleet(tmp_path)
    fleet.stream("parent", final_message("parent done"))
    fleet.stream("child", final_message("child done"))
    try:
        _admit(fleet.store, "parent", profile="worker")
        coordinator = ChildCoordinator(
            fleet.store,
            policy=ChildPolicy(
                profiles=frozenset({"researcher"}),
                tools=frozenset({"search"}),
                workspace_root="/work",
                budget_units={"model_requests": 5.0},
            ),
        )
        request = ChildSpawnRequest(
            parent_task_id="parent",
            namespace=NAMESPACE,
            session_id=SESSION,
            spawn_identity="spawn:1",
            child_task_id="child",
            child_run_id="run-child",
            initial_input=InputContent(kind=InputKind.INITIAL, text="child work"),
            profile="researcher",
            budget_amount=2.0,
        )
        run(coordinator.spawn(request))
        fleet.coordinator = coordinator
        worker = fleet.worker(holder="w1", coordinator=coordinator)

        # The parent's real run must *suspend on the child* before finishing, not finish and be
        # revived later (plan §10.3).
        first = run(worker.run_once())
        assert first is not None and first.task_id == "parent", first
        parent_record = run(fleet.store.read_task("parent"))
        assert parent_record.status is TaskStatus.WAITING, parent_record
        parent_run = run(fleet.store.load_run("run-parent"))
        assert parent_run is not None and parent_run.state.get("status") == "suspended", (
            "the parent run pauses before its terminal commit, it does not finish early"
        )
        # With the parent paused and its slot released, the child runs on the same worker.
        run(worker.run_until_idle())
        child = run(fleet.store.read_task("child"))
        assert child.status is TaskStatus.COMPLETED, child

        # The completion notice is written from the child's committed outcome and wakes the parent.
        run(coordinator.complete_child("child", outcome=ChildOutcome.COMPLETED, result={"ok": True}))
        run(coordinator.deliver_notifications())
        woken = run(fleet.store.read_task("parent"))
        assert woken.status is TaskStatus.QUEUED, woken

        # Recovery resumes the suspended run with the child result as untrusted data, and the
        # parent finishes; it never starts a second run.
        settled = run(worker.run_once())
        assert settled is not None and settled.task_id == "parent", settled
        final = run(fleet.store.read_task("parent"))
        assert final.status is TaskStatus.COMPLETED, final
        final_run = run(fleet.store.load_run("run-parent"))
        assert final_run is not None and final_run.state.get("status") == "completed"
        assert len(run(fleet.store.read_children("parent"))) == 1
        results = run(coordinator.apply_child_results("parent"))
        assert len(results) == 1 and results[0].result.get("ok") is True
    finally:
        fleet.close()
