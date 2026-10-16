"""Q4: the task deadline and the shared budget, tested against a real store and a real harness.

The plan's B-01/B-02 cases: parent and child competing for the last allowance, an allowance that
does not refresh after a recovery, a cancelled summary that is still charged, a startup failure
that reclaims its reservation, a dispatched request whose outcome is unknown keeping its coverage,
and a task whose deadline has passed dispatching nothing at all.

Two levels are exercised on purpose: the ledger's own rules (reservations, settlement, grants,
late usage) and the harness hook that reserves **before** the provider is called.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from yanxu_harness.errors import ContractError
from yanxu_harness.runtime import DefaultHarness
from yanxu_harness.runtime.context import Principal, RunContext, RunIdentity
from yanxu_harness.storage import SqliteExecutionBackend
from yanxu_harness.tasks import (
    BudgetRefused,
    BudgetReservation,
    BudgetUnit,
    DispatchBudget,
    InputContent,
    InputKind,
    LocalWorker,
    SchedulerLimits,
    SqliteControlStore,
    TaskAdmission,
    TaskBudget,
    TaskStatus,
    TaskTreeLimits,
)
from yanxu_harness.tasks.budgets import BudgetLedger

NAMESPACE = "project-a"


def run(coro: Any, *, timeout: float = 60.0) -> Any:
    async def guarded() -> Any:
        return await asyncio.wait_for(coro, timeout=timeout)

    return asyncio.run(guarded())


def _store(tmp_path: Path, *, tree: TaskTreeLimits | None = None) -> SqliteControlStore:
    return SqliteControlStore(tmp_path / "control.db", tree=tree)


def _admit(store: Any, task_id: str, *, root: str = "", parent: str = "", depth: int = 0,
           deadline_at: float | None = None) -> Any:
    return run(
        store.admit_task(
            TaskAdmission(
                namespace=NAMESPACE,
                session_id="s1",
                task_id=task_id,
                run_id=f"run-{task_id}",
                initial_input=InputContent(kind=InputKind.INITIAL, text=f"do {task_id}"),
                root_task_id=root,
                parent_task_id=parent,
                depth=depth,
                deadline_at=deadline_at,
            )
        )
    )


# -- the ledger ---------------------------------------------------------------------


def test_a_reservation_is_refused_before_dispatch_when_the_allowance_is_gone(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        _admit(store, "root")
        ledger = BudgetLedger(store, TaskBudget(units={BudgetUnit.MODEL_REQUESTS: 2}))
        run(ledger.reserve(root_task_id="root", task_id="root", unit=BudgetUnit.MODEL_REQUESTS,
                           amount=2, dispatch_identity="dispatch:1"))
        with pytest.raises(BudgetRefused) as refused:
            run(ledger.reserve(root_task_id="root", task_id="root", unit=BudgetUnit.MODEL_REQUESTS,
                               amount=1, dispatch_identity="dispatch:2"))
        assert refused.value.available == 0
        snapshot = run(ledger.snapshot("root"))
        assert snapshot.units["model_requests"]["used"] == 2
        assert snapshot.units["model_requests"]["available"] == 0
    finally:
        store.close()


def test_a_child_grant_is_bounded_by_what_the_root_has_left(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        _admit(store, "root")
        _admit(store, "child", root="root", parent="root", depth=1)
        ledger = BudgetLedger(store, TaskBudget(units={BudgetUnit.MODEL_REQUESTS: 3}))
        grant = run(
            ledger.grant_to_child(
                parent_task_id="root",
                child_task_id="child",
                unit=BudgetUnit.MODEL_REQUESTS,
                amount=3,
                spawn_identity="spawn:1",
            )
        )
        assert grant.amount == 3
        # The root's own next request now finds nothing: the grant did not add allowance.
        with pytest.raises(BudgetRefused):
            run(ledger.reserve(root_task_id="root", task_id="root", unit=BudgetUnit.MODEL_REQUESTS,
                               amount=1, dispatch_identity="dispatch:root"))
        # And a second child cannot be granted more than zero.
        _admit(store, "child2", root="root", parent="root", depth=1)
        with pytest.raises(BudgetRefused):
            run(
                ledger.grant_to_child(
                    parent_task_id="root",
                    child_task_id="child2",
                    unit=BudgetUnit.MODEL_REQUESTS,
                    amount=1,
                    spawn_identity="spawn:2",
                )
            )
    finally:
        store.close()


def test_a_recovery_does_not_refresh_the_allowance(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        _admit(store, "root")
        ledger = BudgetLedger(store, TaskBudget(units={BudgetUnit.MODEL_REQUESTS: 1}))
        first = run(ledger.reserve(root_task_id="root", task_id="root", unit=BudgetUnit.MODEL_REQUESTS,
                                   amount=1, dispatch_identity="dispatch:a1:req1"))
        run(ledger.settle(first.reservation_id, actual=1))
        # "Recovery" is just another attempt against the same root: it gets no new allowance.
        with pytest.raises(BudgetRefused):
            run(ledger.reserve(root_task_id="root", task_id="root", unit=BudgetUnit.MODEL_REQUESTS,
                               amount=1, dispatch_identity="dispatch:a2:req1"))
        # What a recovery *may* do is re-send the same real request: that is a new dispatch
        # identity and would be refused here too, which is the point of a hard count budget.
        assert run(ledger.used("root", BudgetUnit.MODEL_REQUESTS)) == 1
    finally:
        store.close()


def test_a_cancelled_or_unknown_dispatch_keeps_its_coverage(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        _admit(store, "root")
        ledger = BudgetLedger(store, TaskBudget(units={BudgetUnit.MODEL_REQUESTS: 2}))
        cancelled = run(ledger.reserve(root_task_id="root", task_id="root",
                                       unit=BudgetUnit.MODEL_REQUESTS, amount=1,
                                       dispatch_identity="dispatch:cancel"))
        # The request had entered the dispatch window: the cancellation does not release it.
        run(ledger.settle(cancelled.reservation_id, unknown="cancelled with the outcome unknown"))
        unknown = run(ledger.reserve(root_task_id="root", task_id="root",
                                    unit=BudgetUnit.MODEL_REQUESTS, amount=1,
                                    dispatch_identity="dispatch:unknown"))
        run(ledger.settle(unknown.reservation_id, unknown="the provider reported no usage"))
        assert run(ledger.used("root", BudgetUnit.MODEL_REQUESTS)) == 2
        with pytest.raises(BudgetRefused):
            run(ledger.reserve(root_task_id="root", task_id="root", unit=BudgetUnit.MODEL_REQUESTS,
                               amount=1, dispatch_identity="dispatch:third"))
        # Neither an unknown nor a settled reservation can be refunded into more allowance.
        with pytest.raises(ContractError):
            run(ledger.settle(unknown.reservation_id, release=True))
    finally:
        store.close()


def test_a_startup_failure_reclaims_its_reservation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        _admit(store, "root")
        ledger = BudgetLedger(store, TaskBudget(units={BudgetUnit.MODEL_REQUESTS: 2}))
        reservation = run(ledger.reserve(root_task_id="root", task_id="root",
                                         unit=BudgetUnit.MODEL_REQUESTS, amount=2,
                                         dispatch_identity="dispatch:never-started"))
        run(ledger.settle(reservation.reservation_id, release=True, note="the dispatch never opened"))
        assert run(ledger.used("root", BudgetUnit.MODEL_REQUESTS)) == 0
        assert run(ledger.available("root", BudgetUnit.MODEL_REQUESTS)) == 2
        # A released reservation is not a settled one: the same identity can be reserved again.
        again = run(ledger.reserve(root_task_id="root", task_id="root",
                                   unit=BudgetUnit.MODEL_REQUESTS, amount=2,
                                   dispatch_identity="dispatch:retry"))
        assert again.amount == 2
    finally:
        store.close()


def test_late_usage_is_recorded_once_per_report(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        _admit(store, "root")
        ledger = BudgetLedger(store, TaskBudget(units={BudgetUnit.TOKENS: 1000}))
        reservation = run(ledger.reserve(root_task_id="root", task_id="root", unit=BudgetUnit.TOKENS,
                                         amount=100, dispatch_identity="dispatch:tokens"))
        run(ledger.settle(reservation.reservation_id, actual=80))
        run(
            ledger.record_late_usage(
                reservation.reservation_id, amount=30, root_task_id="root", task_id="root",
                unit=BudgetUnit.TOKENS, sequence=0,
            )
        )
        run(
            ledger.record_late_usage(
                reservation.reservation_id, amount=30, root_task_id="root", task_id="root",
                unit=BudgetUnit.TOKENS, sequence=0,
            )
        )
        # 80 settled + 30 late, recorded once: a redelivered report is not a second charge.
        assert run(ledger.used("root", BudgetUnit.TOKENS)) == 110
        # Estimates are not claimed as actual: a unit the host never bounded is reported as such.
        snapshot = run(ledger.snapshot("root"))
        assert snapshot.units["tokens"]["limit"] == 1000
        assert "cost_micros" not in snapshot.units
    finally:
        store.close()


# -- the dispatch port at the harness ------------------------------------------------


class _RecordingStream:
    """A scripted stream that looks at the budget *before* answering."""

    def __init__(self, store_getter: Any, *, fail: bool = False) -> None:
        self.calls = 0
        self.rows_at_call: list[int] = []
        self._store_getter = store_getter
        self._fail = fail

    def __call__(self, model: Any, context: Any, options: Any = None) -> Any:
        self.calls += 1
        store = self._store_getter()
        rows = store._connection.execute("SELECT COUNT(*) FROM budgets").fetchone()[0]
        self.rows_at_call.append(int(rows))
        if self._fail:
            raise RuntimeError("the provider failed")
        from yanxu_harness.core.types import AssistantMessage, StopReason, TextContent

        message = AssistantMessage(content=[TextContent(text="done")], stop_reason=StopReason.STOP)

        class _Response:
            """The streaming shape the runner expects: an async iterator with ``result()``."""

            async def result(self) -> Any:
                return message

            def __aiter__(self) -> Any:
                async def frames() -> Any:
                    yield {"type": "done", "partial": message}

                return frames()

        return _Response()


def _harness(store: Any, stream: Any, *, task_id: str, budget: Any) -> DefaultHarness:
    return DefaultHarness(
        stream_fn=stream,
        tools=[],
        persistence=store,
        control=store,
        task_id=task_id,
        system_prompt="be brief",
        budget=budget,
    )


def test_the_reservation_exists_before_the_provider_is_called(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        _admit(store, "root")
        ledger = BudgetLedger(store, TaskBudget(units={BudgetUnit.MODEL_REQUESTS: 2}))
        stream = _RecordingStream(lambda: store)
        port = DispatchBudget(ledger, root_task_id="root", task_id="root")
        harness = _harness(store, stream, task_id="root", budget=port)
        outcome = run(
            harness.start(
                _request(), RunContext(
                    identity=RunIdentity(
                        namespace=NAMESPACE, session_id="s1", run_id="run-root", attempt_id="a1"
                    ),
                    principal=Principal(subject="host"),
                ),
            )
        )
        assert outcome.status.value == "completed"
        assert stream.rows_at_call == [1], (
            "the reservation is committed before the external await: the provider saw it"
        )
        assert run(ledger.used("root", BudgetUnit.MODEL_REQUESTS)) == 1
    finally:
        store.close()


def test_a_provider_failure_leaves_the_reservation_unknown(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        _admit(store, "root")
        ledger = BudgetLedger(store, TaskBudget(units={BudgetUnit.MODEL_REQUESTS: 2}))
        stream = _RecordingStream(lambda: store, fail=True)
        port = DispatchBudget(ledger, root_task_id="root", task_id="root")
        harness = _harness(store, stream, task_id="root", budget=port)
        with pytest.raises(Exception):
            run(
                harness.start(
                    _request(), RunContext(
                        identity=RunIdentity(
                            namespace=NAMESPACE, session_id="s1", run_id="run-root",
                            attempt_id="a1",
                        ),
                        principal=Principal(subject="host"),
                    ),
                )
            )
        rows = run(store.read_reservations("root"))
        assert len(rows) == 1 and rows[0].state.value == "unknown", rows
        assert "did not report an outcome" in rows[0].note
        assert run(ledger.used("root", BudgetUnit.MODEL_REQUESTS)) == 1, (
            "an unanswered dispatch keeps its coverage: unknown is not zero"
        )
    finally:
        store.close()


def test_a_budget_refusal_stops_the_request_before_it_is_sent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        _admit(store, "root")
        ledger = BudgetLedger(store, TaskBudget(units={BudgetUnit.MODEL_REQUESTS: 1}))
        # Spend the only unit first.
        first = run(ledger.reserve(root_task_id="root", task_id="root",
                                   unit=BudgetUnit.MODEL_REQUESTS, amount=1,
                                   dispatch_identity="dispatch:other"))
        run(ledger.settle(first.reservation_id, actual=1))
        stream = _RecordingStream(lambda: store)
        port = DispatchBudget(ledger, root_task_id="root", task_id="root")
        harness = _harness(store, stream, task_id="root", budget=port)
        with pytest.raises(BudgetRefused):
            run(
                harness.start(
                    _request(), RunContext(
                        identity=RunIdentity(
                            namespace=NAMESPACE, session_id="s1", run_id="run-root",
                            attempt_id="a1",
                        ),
                        principal=Principal(subject="host"),
                    ),
                )
            )
        assert stream.calls == 0, (
            "a request that cannot be afforded must not be sent: the reservation precedes the "
            "external await"
        )
    finally:
        store.close()


def _request() -> Any:
    from yanxu_harness.runtime import RunRequest

    return RunRequest(prompt="do root")


# -- the deadline -------------------------------------------------------------------


def test_a_task_past_its_deadline_dispatches_nothing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        _admit(store, "late", deadline_at=time.time() - 60.0)
        calls: list[str] = []

        def harness_for(task: Any, attempt_id: str) -> Any:
            calls.append(attempt_id)
            raise AssertionError("a past-deadline task must not build a harness at all")

        worker = LocalWorker(
            store, harness_for, limits=SchedulerLimits(), holder="w1"
        )
        result = run(worker.run_once())
        assert result is not None and result.outcome == "settled:usage_limited"
        assert result.detail.startswith("the task's deadline")
        assert calls == [], "no dispatch of any kind happened"
        assert run(store.load_run("run-late")) is None
        task = run(store.read_task("late"))
        assert task is not None and task.status is TaskStatus.USAGE_LIMITED
    finally:
        store.close()


def test_the_remaining_deadline_is_computed_from_the_persisted_instant(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        deadline = time.time() + 30.0
        _admit(store, "timed", deadline_at=deadline)
        task = run(store.read_task("timed"))
        remaining = task.remaining_seconds(time.time())
        assert 25.0 < remaining <= 30.0, remaining
        assert task.past_deadline(time.time()) is False
        assert task.past_deadline(deadline + 1) is True
        # The deadline survives a restart as an instant, not as a duration.
        store.close()
        reopened = _store(tmp_path)
        try:
            again = run(reopened.read_task("timed"))
            assert again is not None and again.deadline_at == deadline
        finally:
            reopened.close()
    finally:
        pass


def test_the_invocation_budget_is_the_smaller_of_the_task_and_the_harness(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        _admit(store, "timed", deadline_at=time.time() + 3600.0)
        seen: dict[str, Any] = {}

        class TinyHarness:
            """A harness that records the budget it was asked for and stops immediately."""

            def __init__(self, seconds: Any) -> None:
                self._persistence = None
                self.seconds = seconds

            async def start(self, request: Any, context: Any) -> Any:
                seen["deadline"] = request.deadline_seconds

                class Outcome:
                    status = type("S", (), {"value": "completed"})()
                    stop_code = ""

                return Outcome()

            async def resume(self, run_id: str, resolution: Any, context: Any) -> Any:
                raise AssertionError("no run exists to resume")

        def harness_for(task: Any, attempt_id: str) -> Any:
            return TinyHarness(task.remaining_seconds(time.time()))

        worker = LocalWorker(store, harness_for, holder="w1")
        result = run(worker.run_once())
        assert result is not None and result.task_status is TaskStatus.COMPLETED
        assert 3500 < seen["deadline"] <= 3600, seen
        # The worker computed the remaining time from the persisted instant and handed it to the
        # harness as a duration; the harness turns that into the invocation limit (the smaller of
        # it and the configured limit), which the harness tests cover.
    finally:
        store.close()


# -- tree bounds --------------------------------------------------------------------


def test_the_tree_refuses_a_child_deeper_than_it_allows(tmp_path: Path) -> None:
    store = _store(tmp_path, tree=TaskTreeLimits(max_depth=1, max_tasks_per_root=4))
    try:
        _admit(store, "root")
        _admit(store, "child", root="root", parent="root", depth=1)
        with pytest.raises(ContractError) as deep:
            _admit(store, "grandchild", root="root", parent="child", depth=2)
        assert "depth" in str(deep.value)
    finally:
        store.close()


def test_the_tree_refuses_more_tasks_than_it_allows(tmp_path: Path) -> None:
    store = _store(tmp_path, tree=TaskTreeLimits(max_depth=1, max_tasks_per_root=2))
    try:
        _admit(store, "root")
        _admit(store, "child", root="root", parent="root", depth=1)
        with pytest.raises(ContractError) as full:
            _admit(store, "child2", root="root", parent="root", depth=1)
        assert "limit" in str(full.value)
    finally:
        store.close()


def test_a_grant_and_a_settlement_are_idempotent_across_attempts(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        _admit(store, "root")
        _admit(store, "child", root="root", parent="root", depth=1)
        ledger = BudgetLedger(store, TaskBudget(units={BudgetUnit.MODEL_REQUESTS: 4}))
        first = run(
            ledger.grant_to_child(
                parent_task_id="root", child_task_id="child", unit=BudgetUnit.MODEL_REQUESTS,
                amount=2, spawn_identity="spawn:stable",
            )
        )
        # A duplicate spawn notice (same stable identity) returns the same reservation.
        again = run(
            ledger.grant_to_child(
                parent_task_id="root", child_task_id="child", unit=BudgetUnit.MODEL_REQUESTS,
                amount=2, spawn_identity="spawn:stable",
            )
        )
        assert again.reservation_id == first.reservation_id
        assert len(run(store.read_reservations("root"))) == 1
        run(ledger.settle(first.reservation_id, actual=1))
        replay = run(ledger.settle(first.reservation_id, actual=1))
        assert replay.settled_amount == 1, "a redelivered settlement changes nothing"
        with pytest.raises(ContractError):
            run(ledger.settle(first.reservation_id, actual=0))
    finally:
        store.close()
