from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

import pytest

from yanxu_harness.core.types import AssistantMessage, StopReason, TextContent, Usage
from yanxu_harness.decisions import (
    DecisionCandidate,
    DecisionKind,
    DecisionRequest,
    DecisionStatus,
    ModelDecisionRouter,
)
from yanxu_harness.errors import BudgetExceeded, ContractError
from yanxu_harness.runtime import DefaultHarness, RunRequest
from yanxu_harness.runtime.context import Principal, RunContext, RunIdentity
from yanxu_harness.storage import SqliteExecutionBackend
from yanxu_harness.tasks import (
    BudgetLimit,
    BudgetLimitMode,
    BudgetOwnerKind,
    BudgetOwnerRef,
    BudgetPolicySnapshot,
    BudgetRefused,
    BudgetUnit,
    ReservationState,
    SqliteControlStore,
    TaskAdmission,
    TypedDispatchBudget,
)
from yanxu_harness.tools.outcomes import ToolCancelled, ToolDeferred, ToolRejected
from yanxu_harness.tools import (
    ConservativePolicy,
    EffectState,
    InMemoryApprovalStore,
    InvocationStatus,
    ToolDefinition,
    ToolGateway,
    ToolInvocation,
    ToolRegistry,
)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def _store(path: Path) -> SqliteControlStore:
    store = SqliteControlStore(path, enable_typed_budgets=True)
    run(
        store.admit_task(
            TaskAdmission(
                namespace="ns",
                session_id="session",
                task_id="task",
                run_id="run-task",
                root_task_id="task",
            )
        )
    )
    return store


def _policy(
    owner: BudgetOwnerRef,
    unit: BudgetUnit,
    limit: int,
    *,
    mode: BudgetLimitMode = BudgetLimitMode.HARD,
) -> BudgetPolicySnapshot:
    return BudgetPolicySnapshot(
        owner=owner,
        session_id="session",
        policy_version="dispatch-v1",
        limits=(BudgetLimit(unit, limit, mode=mode),),
    )


def _snapshot(owner: BudgetOwnerRef, *limits: BudgetLimit) -> BudgetPolicySnapshot:
    return BudgetPolicySnapshot(
        owner=owner,
        session_id="session",
        policy_version="dispatch-v1",
        limits=tuple(limits),
    )


class _RecordingStream:
    def __init__(self, *, usage: Usage | None = None, failure: BaseException | None = None) -> None:
        self.calls = 0
        self.usage = usage
        self.failure = failure

    def __call__(self, _model: Any, _context: Any, _options: Any = None) -> Any:
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        message = AssistantMessage(
            content=[TextContent(text="done")],
            stop_reason=StopReason.STOP,
            usage=self.usage,
        )

        class Response:
            async def result(self) -> AssistantMessage:
                return message

            def __aiter__(self) -> Any:
                async def frames() -> Any:
                    yield {"type": "done", "partial": message}

                return frames()

        return Response()


def _identity() -> RunIdentity:
    return RunIdentity(namespace="ns", session_id="session", run_id="run-task", attempt_id="attempt-1")


def _run_context() -> RunContext:
    return RunContext(identity=_identity(), principal=Principal(subject="worker"))


def test_default_harness_reserves_request_and_records_reported_token_usage(tmp_path: Path) -> None:
    store = _store(tmp_path / "control.sqlite")
    owner = BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, "ns", "task")
    run(
        store.put_budget_policy(
            BudgetPolicySnapshot(
                owner=owner,
                session_id="session",
                policy_version="dispatch-v1",
                limits=(
                    BudgetLimit(BudgetUnit.MODEL_REQUESTS, 2),
                    BudgetLimit(BudgetUnit.INPUT_TOKENS, 100, mode=BudgetLimitMode.OBSERVE),
                    BudgetLimit(BudgetUnit.OUTPUT_TOKENS, 100, mode=BudgetLimitMode.OBSERVE),
                ),
            )
        )
    )
    stream = _RecordingStream(usage=Usage(input=13, output=5))
    harness = DefaultHarness(
        stream_fn=stream,
        persistence=store,
        control=store,
        task_id="task",
        budget=TypedDispatchBudget(store, task_id="task"),
    )
    try:
        outcome = run(harness.start(RunRequest(prompt="hello"), _run_context()))
        assert outcome.status.value == "completed"
        assert stream.calls == 1
        assert run(store.read_typed_budget_usage(owner, BudgetUnit.MODEL_REQUESTS)).actual == 1
        assert run(store.read_typed_budget_usage(owner, BudgetUnit.INPUT_TOKENS)).actual == 13
        assert run(store.read_typed_budget_usage(owner, BudgetUnit.OUTPUT_TOKENS)).actual == 5
    finally:
        store.close()


def test_provider_retry_gets_a_new_budgeted_request_identity(tmp_path: Path) -> None:
    store = _store(tmp_path / "control.sqlite")
    owner = BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, "ns", "task")
    run(store.put_budget_policy(_policy(owner, BudgetUnit.MODEL_REQUESTS, 2)))
    budget = TypedDispatchBudget(store, task_id="task")
    payload = {
        "task_id": "task",
        "run_id": "run-task",
        "model": "fake-model",
        "messages": [{"role": "user", "content": "retry safely"}],
        "max_output_tokens": 32,
    }
    sends: list[str] = []

    async def scenario() -> None:
        primary = await budget.reserve({**payload, "request_instance": "primary-request"})
        sends.append("primary")
        await budget.settle(primary, {"status": "unknown", "detail": "first response was lost"})

        retry = await budget.reserve_provider_retry(payload, retry_identity="retry-request-1")
        sends.append("retry")
        await budget.settle_auxiliary(retry, dispatched=True, usage={"input_tokens": 7})

        with pytest.raises(ContractError, match="already has durable budget coverage"):
            await budget.reserve_provider_retry(payload, retry_identity="retry-request-1")

    try:
        run(scenario())
        usage = run(store.read_typed_budget_usage(owner, BudgetUnit.MODEL_REQUESTS))
        assert sends == ["primary", "retry"]
        assert (usage.actual, usage.reserved, usage.unknown) == (1, 0, 1)
        assert usage.used == 2
    finally:
        store.close()


@pytest.mark.parametrize("mode", ["reserved", "sent", "usage_committed", "usage_reply_lost"])
def test_dead_provider_process_recovers_budget_without_free_retry(tmp_path: Path, mode: str) -> None:
    database = tmp_path / "control.sqlite"
    store = _store(database)
    owner = BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, "ns", "task")
    run(
        store.put_budget_policy(
            _snapshot(
                owner,
                BudgetLimit(BudgetUnit.MODEL_REQUESTS, 1),
                BudgetLimit(BudgetUnit.INPUT_TOKENS, 0, mode=BudgetLimitMode.OBSERVE),
            )
        )
    )
    store.close()
    child = """
import asyncio, os, sys
from yanxu_harness.tasks import SqliteControlStore, TypedDispatchBudget

async def main(path, mode, marker):
    store = SqliteControlStore(path, enable_typed_budgets=True)
    try:
        budget = TypedDispatchBudget(store, task_id='task')
        reservation = await budget.reserve({
            'task_id': 'task', 'run_id': 'run-task', 'request_instance': 'crashed-request',
            'model': 'fake-model', 'messages': [], 'max_output_tokens': 16,
        })
        if mode in {'sent', 'usage_committed', 'usage_reply_lost'}:
            with open(marker, 'w', encoding='utf-8') as handle:
                handle.write('request reached provider')
        if mode == 'usage_reply_lost':
            original = store.record_typed_budget_usage
            async def commit_then_drop_reply(*args, **kwargs):
                await original(*args, **kwargs)
                os._exit(24)
            store.record_typed_budget_usage = commit_then_drop_reply
        if mode in {'usage_committed', 'usage_reply_lost'}:
            await budget.settle(
                reservation, {'status': 'completed', 'usage': {'input': 9}}
            )
    finally:
        store.close()

asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3]))
os._exit(23)
"""
    marker = tmp_path / "provider-sent.txt"
    crashed = subprocess.run(
        [sys.executable, "-c", child, str(database), mode, str(marker)],
        capture_output=True,
        text=True,
        check=False,
    )
    expected_exit = 24 if mode == "usage_reply_lost" else 23
    assert crashed.returncode == expected_exit, crashed.stderr
    assert marker.exists() is (mode in {"sent", "usage_committed", "usage_reply_lost"})

    recovered = SqliteControlStore(database, enable_typed_budgets=True)
    budget = TypedDispatchBudget(recovered, task_id="task")
    try:
        if mode in {"usage_committed", "usage_reply_lost"}:
            requests = run(recovered.read_typed_budget_usage(owner, BudgetUnit.MODEL_REQUESTS))
            input_tokens = run(recovered.read_typed_budget_usage(owner, BudgetUnit.INPUT_TOKENS))
            assert (requests.actual, requests.reserved, requests.unknown) == (1, 0, 0)
            assert input_tokens.actual == 9
            if mode == "usage_reply_lost":
                rows = run(recovered.read_typed_budget_reservations(task_id="task"))
                primary = next(row for row in rows if row.dispatch.unit is BudgetUnit.MODEL_REQUESTS)
                input_row = next(row for row in rows if row.dispatch.unit is BudgetUnit.INPUT_TOKENS)
                replay = run(
                    recovered.record_typed_budget_usage(
                        primary.reservation_id,
                        report_id="provider-final:crashed-request",
                        unit=BudgetUnit.INPUT_TOKENS,
                        actual=9,
                    )
                )
                assert replay.reservation_id == input_row.reservation_id
                assert run(
                    recovered.read_typed_budget_usage(owner, BudgetUnit.INPUT_TOKENS)
                ).actual == 9
        else:
            with pytest.raises(BudgetRefused):
                run(
                    budget.reserve(
                        {
                            "task_id": "task",
                            "run_id": "run-task",
                            "request_instance": "recovered-retry",
                            "model": "fake-model",
                            "messages": [],
                            "max_output_tokens": 16,
                        }
                    )
                )
            usage = run(recovered.read_typed_budget_usage(owner, BudgetUnit.MODEL_REQUESTS))
            assert (usage.actual, usage.reserved, usage.unknown) == (0, 0, 1)
            assert usage.used == 1
    finally:
        recovered.close()


def test_provider_failure_keeps_unknown_and_pre_dispatch_payload_refusal_releases(tmp_path: Path) -> None:
    store = _store(tmp_path / "control.sqlite")
    owner = BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, "ns", "task")
    run(store.put_budget_policy(_policy(owner, BudgetUnit.MODEL_REQUESTS, 4)))
    budget = TypedDispatchBudget(store, task_id="task")
    failed = _RecordingStream(failure=RuntimeError("connection ended without a result"))
    harness = DefaultHarness(
        stream_fn=failed, persistence=store, control=store, task_id="task", budget=budget
    )
    try:
        with pytest.raises(RuntimeError, match="connection ended"):
            run(harness.start(RunRequest(prompt="hello"), _run_context()))
        rows = run(
            store.read_typed_budget_reservations(
                task_id="task", run_id="run-task", state=ReservationState.UNKNOWN
            )
        )
        assert len(rows) == 1
        assert rows[0].dispatch.unit is BudgetUnit.MODEL_REQUESTS
        assert rows[0].dispatch.amount == 1

        # A fresh Task is used to exercise a separate request after the first run's terminal
        # failure. The final payload gate raises before opening a transport request, so the
        # reservation is releasable.
        run(
            store.admit_task(
                TaskAdmission(
                    namespace="ns",
                    session_id="session",
                    task_id="task-2",
                    run_id="run-task-2",
                    root_task_id="task-2",
                )
            )
        )
        second_owner = BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, "ns", "task-2")
        run(store.put_budget_policy(_policy(second_owner, BudgetUnit.MODEL_REQUESTS, 4)))
        second = RunContext(
            identity=RunIdentity(
                namespace="ns", session_id="session", run_id="run-task-2", attempt_id="attempt-2"
            ),
            principal=Principal(subject="worker"),
        )
        gate_stream = _RecordingStream(failure=BudgetExceeded("final payload refused"))
        gate_harness = DefaultHarness(
            stream_fn=gate_stream,
            persistence=store,
            control=store,
            task_id="task-2",
            budget=TypedDispatchBudget(store, task_id="task-2"),
        )
        run(gate_harness.start(RunRequest(prompt="hello"), second))
        released = run(
            store.read_typed_budget_reservations(
                task_id="task-2", run_id="run-task-2", state=ReservationState.RELEASED
            )
        )
        assert len(released) == 1
        assert gate_stream.calls == 1
    finally:
        store.close()


def test_cancellation_after_reservation_but_before_stream_entry_releases_coverage(tmp_path: Path) -> None:
    store = _store(tmp_path / "control.sqlite")
    owner = BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, "ns", "task")
    run(store.put_budget_policy(_policy(owner, BudgetUnit.MODEL_REQUESTS, 2)))
    context = _run_context()
    typed_budget = TypedDispatchBudget(store, task_id="task")

    class CancelAfterReserve:
        async def reserve(self, payload: Mapping[str, Any]) -> Any:
            reservation = await typed_budget.reserve(payload)
            context.cancellation.cancel("injected before stream entry")
            return reservation

        async def settle(self, reservation: Any, outcome: Mapping[str, Any]) -> None:
            await typed_budget.settle(reservation, outcome)

    stream = _RecordingStream()
    harness = DefaultHarness(
        stream_fn=stream,
        persistence=store,
        control=store,
        task_id="task",
        budget=CancelAfterReserve(),
    )
    try:
        run(harness.start(RunRequest(prompt="hello"), context))
        rows = run(store.read_typed_budget_reservations(task_id="task", run_id="run-task"))
        assert stream.calls == 0
        assert len(rows) == 1
        assert rows[0].state is ReservationState.RELEASED
        assert run(store.read_typed_budget_usage(owner, BudgetUnit.MODEL_REQUESTS)).used == 0
    finally:
        store.close()


def test_budget_refusal_blocks_model_provider_call(tmp_path: Path) -> None:
    store = _store(tmp_path / "control.sqlite")
    owner = BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, "ns", "task")
    run(store.put_budget_policy(_policy(owner, BudgetUnit.MODEL_REQUESTS, 0)))
    stream = _RecordingStream()
    harness = DefaultHarness(
        stream_fn=stream,
        persistence=store,
        control=store,
        task_id="task",
        budget=TypedDispatchBudget(store, task_id="task"),
    )
    try:
        with pytest.raises(BudgetRefused):
            run(harness.start(RunRequest(prompt="hello"), _run_context()))
        assert stream.calls == 0
        assert run(store.read_typed_budget_reservations(task_id="task")) == ()
    finally:
        store.close()


def test_hard_usage_bounds_are_reserved_atomically_and_reported_actuals_replace_them(tmp_path: Path) -> None:
    store = _store(tmp_path / "control.sqlite")
    owner = BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, "ns", "task")
    run(
        store.put_budget_policy(
            _snapshot(
                owner,
                BudgetLimit(BudgetUnit.MODEL_REQUESTS, 2),
                BudgetLimit(
                    BudgetUnit.INPUT_TOKENS,
                    100,
                    hard_bound_ref="host:input-bound-v1",
                ),
                BudgetLimit(
                    BudgetUnit.OUTPUT_TOKENS,
                    100,
                    hard_bound_ref="host:output-bound-v1",
                ),
            )
        )
    )
    budget = TypedDispatchBudget(
        store,
        task_id="task",
        upper_bounds={"host:input-bound-v1": 20, "host:output-bound-v1": 8},
    )
    stream = _RecordingStream(usage=Usage(input=13, output=5))
    harness = DefaultHarness(
        stream_fn=stream,
        persistence=store,
        control=store,
        task_id="task",
        budget=budget,
    )
    try:
        outcome = run(harness.start(RunRequest(prompt="hello"), _run_context()))
        assert outcome.status.value == "completed"
        assert stream.calls == 1
        input_usage = run(store.read_typed_budget_usage(owner, BudgetUnit.INPUT_TOKENS))
        output_usage = run(store.read_typed_budget_usage(owner, BudgetUnit.OUTPUT_TOKENS))
        assert (input_usage.actual, input_usage.reserved, input_usage.unknown) == (13, 0, 0)
        assert (output_usage.actual, output_usage.reserved, output_usage.unknown) == (5, 0, 0)
        rows = run(store.read_typed_budget_reservations(task_id="task", run_id="run-task"))
        assert {row.dispatch.unit for row in rows} == {
            BudgetUnit.MODEL_REQUESTS,
            BudgetUnit.INPUT_TOKENS,
            BudgetUnit.OUTPUT_TOKENS,
        }
        assert all(row.state is ReservationState.SETTLED for row in rows)
    finally:
        store.close()


def test_missing_usage_keeps_hard_bound_unknown_and_actual_overage_closes_admission(tmp_path: Path) -> None:
    store = _store(tmp_path / "control.sqlite")
    owner = BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, "ns", "task")
    run(
        store.put_budget_policy(
            _snapshot(
                owner,
                BudgetLimit(BudgetUnit.MODEL_REQUESTS, 3),
                BudgetLimit(
                    BudgetUnit.OUTPUT_TOKENS,
                        2,
                    hard_bound_ref="host:output-bound-v1",
                ),
            )
        )
    )
    budget = TypedDispatchBudget(
        store, task_id="task", upper_bounds={"host:output-bound-v1": 2}
    )
    missing_usage_stream = _RecordingStream(usage=None)
    harness = DefaultHarness(
        stream_fn=missing_usage_stream,
        persistence=store,
        control=store,
        task_id="task",
        budget=budget,
    )
    try:
        run(harness.start(RunRequest(prompt="hello"), _run_context()))
        unknown = run(store.read_typed_budget_usage(owner, BudgetUnit.OUTPUT_TOKENS))
        assert (unknown.actual, unknown.reserved, unknown.unknown) == (0, 0, 2)
        with pytest.raises(BudgetRefused):
            run(
                budget.reserve(
                    {"task_id": "task", "run_id": "run-task", "request_instance": "next-request"}
                )
            )
        assert missing_usage_stream.calls == 1
    finally:
        store.close()


def test_actual_token_overage_is_kept_in_full_and_closes_next_admission(tmp_path: Path) -> None:
    store = _store(tmp_path / "control.sqlite")
    owner = BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, "ns", "task")
    run(
        store.put_budget_policy(
            _snapshot(
                owner,
                BudgetLimit(BudgetUnit.MODEL_REQUESTS, 3),
                BudgetLimit(
                    BudgetUnit.OUTPUT_TOKENS,
                    4,
                    hard_bound_ref="host:output-bound-v1",
                ),
            )
        )
    )
    budget = TypedDispatchBudget(
        store, task_id="task", upper_bounds={"host:output-bound-v1": 2}
    )
    stream = _RecordingStream(usage=Usage(input=4, output=7))
    harness = DefaultHarness(
        stream_fn=stream,
        persistence=store,
        control=store,
        task_id="task",
        budget=budget,
    )
    try:
        run(harness.start(RunRequest(prompt="hello"), _run_context()))
        usage = run(store.read_typed_budget_usage(owner, BudgetUnit.OUTPUT_TOKENS))
        assert (usage.actual, usage.over_limit, usage.available) == (7, 3, 0)
        assert usage.upper_bound_overrun == 5
        with pytest.raises(BudgetRefused):
            run(
                budget.reserve(
                    {"task_id": "task", "run_id": "run-task", "request_instance": "next-request"}
                )
            )
        assert stream.calls == 1
    finally:
        store.close()


def test_hard_usage_bound_missing_capability_refuses_before_reserving(tmp_path: Path) -> None:
    store = _store(tmp_path / "control.sqlite")
    owner = BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, "ns", "task")
    run(
        store.put_budget_policy(
            _snapshot(
                owner,
                BudgetLimit(BudgetUnit.MODEL_REQUESTS, 2),
                BudgetLimit(
                    BudgetUnit.INPUT_TOKENS,
                    100,
                    hard_bound_ref="host:missing-input-bound",
                ),
            )
        )
    )
    budget = TypedDispatchBudget(store, task_id="task")
    try:
        with pytest.raises(ContractError, match="no executable upper-bound capability"):
            run(
                budget.reserve(
                    {"task_id": "task", "run_id": "run-task", "request_instance": "request-1"}
                )
            )
        assert run(store.read_typed_budget_reservations(task_id="task")) == ()
    finally:
        store.close()


def test_any_hard_scope_refusal_rolls_back_model_and_usage_bound_reservations(tmp_path: Path) -> None:
    store = _store(tmp_path / "control.sqlite")
    owner = BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, "ns", "task")
    run(
        store.put_budget_policy(
            _snapshot(
                owner,
                BudgetLimit(BudgetUnit.MODEL_REQUESTS, 2),
                BudgetLimit(
                    BudgetUnit.INPUT_TOKENS,
                    10,
                    hard_bound_ref="host:input-bound-v1",
                ),
            )
        )
    )
    budget = TypedDispatchBudget(
        store, task_id="task", upper_bounds={"host:input-bound-v1": 11}
    )
    try:
        with pytest.raises(BudgetRefused):
            run(
                budget.reserve(
                    {"task_id": "task", "run_id": "run-task", "request_instance": "request-1"}
                )
            )
        assert run(store.read_typed_budget_reservations(task_id="task")) == ()
    finally:
        store.close()


@pytest.mark.parametrize(
    ("failure_factory", "expected_state"),
    [
        (lambda: ToolRejected("handler refused"), EffectState.REJECTED),
        (lambda: ToolCancelled("handler cancelled"), EffectState.CANCELLED),
        (lambda: ToolDeferred("handler deferred"), EffectState.DEFERRED),
    ],
    ids=("rejected", "cancelled", "deferred"),
)
def test_handler_outcomes_consume_tool_dispatch_budget(
    tmp_path: Path, failure_factory: Any, expected_state: EffectState
) -> None:
    control = _store(tmp_path / "control.sqlite")
    execution = SqliteExecutionBackend(tmp_path / "execution.sqlite")
    owner = BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, "ns", "task")
    run(control.put_budget_policy(_policy(owner, BudgetUnit.DISPATCHES, 1)))
    calls: list[str] = []

    async def handler(_context: Any, arguments: Any) -> str:
        calls.append(arguments["term"])
        raise failure_factory()

    gateway = ToolGateway(
        registry=ToolRegistry(
            [ToolDefinition("notes.read", "Read a note", args_validator=lambda value: value, handler=handler)]
        ),
        policy=ConservativePolicy(auto_write=True),
        approvals=InMemoryApprovalStore(),
        backend=execution,
        dispatch_budget=TypedDispatchBudget(control, task_id="task"),
    )
    try:
        results = [
            run(
                gateway.invoke(
                    ToolInvocation(
                        call_id=f"call-{index}",
                        namespace="ns",
                        session_id="session",
                        run_id="run-task",
                        model_action_id="model-action",
                        tool_name="notes.read",
                        arguments={"term": "alpha"},
                    ),
                    _run_context(),
                )
            )
            for index in range(3)
        ]
        usage = run(control.read_typed_budget_usage(owner, BudgetUnit.DISPATCHES))
        assert results[0].receipt is not None and results[0].receipt.state is expected_state
        assert results[0].receipt.handler_entered is True
        assert calls == ["alpha"]
        assert (usage.actual, usage.reserved, usage.unknown) == (1, 0, 0)
        from yanxu_harness.tools.execution import ToolIntent

        reloaded = run(
            execution.begin(
                ToolIntent(
                    operation_id=results[0].operation_id,
                    namespace="ns",
                    tool_name="notes.read",
                    tool_version="1",
                    args_hash="already-recorded-operation",
                )
            )
        )
        assert reloaded is not None and reloaded.handler_entered is True
    finally:
        execution.close()
        control.close()


def test_unmarked_neutral_receipt_keeps_tool_dispatch_budget_unknown(tmp_path: Path) -> None:
    from yanxu_harness.tools.execution import ToolReceipt

    control = _store(tmp_path / "control.sqlite")
    owner = BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, "ns", "task")
    run(control.put_budget_policy(_policy(owner, BudgetUnit.DISPATCHES, 1)))
    budget = TypedDispatchBudget(control, task_id="task")
    invocation = ToolInvocation(
        call_id="legacy-neutral-receipt",
        namespace="ns",
        session_id="session",
        run_id="run-task",
        model_action_id="model-action",
        tool_name="notes.read",
        arguments={"term": "alpha"},
    )
    reservation = run(budget.reserve_tool(invocation, _run_context()))
    receipt = ToolReceipt(
        operation_id=invocation.operation_key(),
        state=EffectState.REJECTED,
        detail="legacy receipt has no dispatch phase",
    )

    try:
        run(budget.settle_tool(reservation, receipt))
        usage = run(control.read_typed_budget_usage(owner, BudgetUnit.DISPATCHES))
        assert (usage.actual, usage.reserved, usage.unknown) == (0, 0, 1)
    finally:
        control.close()


def test_policy_change_after_snapshot_read_refuses_before_provider_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path / "control.sqlite")
    owner = BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, "ns", "task")
    original = _snapshot(owner, BudgetLimit(BudgetUnit.MODEL_REQUESTS, 2))
    run(store.put_budget_policy(original))
    read_policies = store.read_budget_policies_for_task
    changed = False

    async def read_then_add_hard_output_limit(task_id: str, run_id: str) -> Any:
        nonlocal changed
        policies = await read_policies(task_id, run_id)
        if not changed:
            changed = True
            updated = _snapshot(
                owner,
                BudgetLimit(BudgetUnit.MODEL_REQUESTS, 2),
                BudgetLimit(
                    BudgetUnit.OUTPUT_TOKENS,
                    0,
                    hard_bound_ref="host:zero-output-v1",
                ),
            )
            updated = BudgetPolicySnapshot(
                owner=updated.owner,
                session_id=updated.session_id,
                policy_version="dispatch-v2",
                limits=updated.limits,
            )
            await store.update_budget_policy(
                updated,
                expected_policy_digest=original.digest,
                actor="operator",
                idempotency_key="add-output-hard-limit",
                authorization_ref="test-authorization",
                reason="the output allowance was withdrawn",
            )
        return policies

    monkeypatch.setattr(store, "read_budget_policies_for_task", read_then_add_hard_output_limit)
    stream = _RecordingStream()
    harness = DefaultHarness(
        stream_fn=stream,
        persistence=store,
        control=store,
        task_id="task",
        budget=TypedDispatchBudget(store, task_id="task"),
    )
    try:
        with pytest.raises(ContractError, match="budget policy changed"):
            run(harness.start(RunRequest(prompt="hello"), _run_context()))
        assert changed is True
        assert stream.calls == 0
        assert run(store.read_typed_budget_reservations(task_id="task")) == ()
    finally:
        store.close()


def test_binding_revocation_while_tool_budget_reserves_blocks_handler_and_releases_budget(
    tmp_path: Path,
) -> None:
    from yanxu_harness.plans import ExecutionBindingRefused

    control = _store(tmp_path / "control.sqlite")
    execution = SqliteExecutionBackend(tmp_path / "execution.sqlite")
    owner = BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, "ns", "task")
    run(control.put_budget_policy(_policy(owner, BudgetUnit.DISPATCHES, 1)))
    handler_calls: list[str] = []

    async def handler(_context: Any, arguments: Any) -> str:
        handler_calls.append(arguments["term"])
        return "read"

    class RevocableBinding:
        revoked = False
        checks = 0

        async def check_tool_dispatch(self) -> None:
            self.checks += 1
            if self.revoked:
                raise ExecutionBindingRefused("deployment_revoked", "deployment was revoked")

    binding = RevocableBinding()
    reservation_ready = asyncio.Event()
    release_reservation = asyncio.Event()
    typed_budget = TypedDispatchBudget(control, task_id="task")

    class BlockingDispatchBudget:
        async def reserve_tool(self, invocation: Any, context: Any) -> Any:
            reservation = await typed_budget.reserve_tool(invocation, context)
            reservation_ready.set()
            await release_reservation.wait()
            return reservation

        async def settle_tool(self, reservation: Any, receipt: Any) -> None:
            await typed_budget.settle_tool(reservation, receipt)

        async def reconcile_tool(self, invocation: Any, context: Any, receipt: Any) -> None:
            await typed_budget.reconcile_tool(invocation, context, receipt)

    gateway = ToolGateway(
        registry=ToolRegistry(
            [ToolDefinition("notes.read", "Read a note", args_validator=lambda value: value, handler=handler)]
        ),
        policy=ConservativePolicy(auto_write=True),
        approvals=InMemoryApprovalStore(),
        backend=execution,
        dispatch_budget=BlockingDispatchBudget(),
    )
    context = RunContext(
        identity=_identity(),
        principal=Principal(subject="worker"),
        execution_binding=binding,
    )
    invocation = ToolInvocation(
        call_id="call-revoked-during-reserve",
        namespace="ns",
        session_id="session",
        run_id="run-task",
        model_action_id="model-action-revoked",
        tool_name="notes.read",
        arguments={"term": "alpha"},
    )

    async def scenario() -> Any:
        task = asyncio.create_task(gateway.invoke(invocation, context))
        try:
            await asyncio.wait_for(reservation_ready.wait(), timeout=5)
            binding.revoked = True
        finally:
            release_reservation.set()
        return await asyncio.wait_for(task, timeout=5)

    try:
        result = run(scenario())
        usage = run(control.read_typed_budget_usage(owner, BudgetUnit.DISPATCHES))
        assert result.status is InvocationStatus.DENIED
        assert result.receipt is not None and result.receipt.state is EffectState.REJECTED
        assert result.receipt.handler_entered is False
        assert result.receipt.details["reason_code"] == "deployment_revoked"
        assert binding.checks >= 2
        assert handler_calls == []
        assert (usage.actual, usage.reserved, usage.unknown) == (0, 0, 0)
    finally:
        release_reservation.set()
        execution.close()
        control.close()


def test_gateway_cancellation_during_post_reservation_binding_check_settles_no_dispatch(
    tmp_path: Path,
) -> None:
    control = _store(tmp_path / "control.sqlite")
    execution = SqliteExecutionBackend(tmp_path / "execution.sqlite")
    owner = BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, "ns", "task")
    run(control.put_budget_policy(_policy(owner, BudgetUnit.DISPATCHES, 1)))
    binding_check_waiting = asyncio.Event()
    handler_calls: list[str] = []

    class BlockingBinding:
        checks = 0

        async def check_tool_dispatch(self) -> None:
            self.checks += 1
            if self.checks == 2:
                binding_check_waiting.set()
                await asyncio.Event().wait()

    binding = BlockingBinding()

    async def handler(_context: Any, arguments: Any) -> str:
        handler_calls.append(arguments["term"])
        return "read"

    gateway = ToolGateway(
        registry=ToolRegistry(
            [ToolDefinition("notes.read", "Read a note", args_validator=lambda value: value, handler=handler)]
        ),
        policy=ConservativePolicy(auto_write=True),
        approvals=InMemoryApprovalStore(),
        backend=execution,
        dispatch_budget=TypedDispatchBudget(control, task_id="task"),
    )
    context = RunContext(
        identity=_identity(),
        principal=Principal(subject="worker"),
        execution_binding=binding,
    )
    invocation = ToolInvocation(
        call_id="call-cancelled-during-binding-check",
        namespace="ns",
        session_id="session",
        run_id="run-task",
        model_action_id="model-action-cancelled-binding-check",
        tool_name="notes.read",
        arguments={"term": "alpha"},
    )

    async def scenario() -> None:
        task = asyncio.create_task(gateway.invoke(invocation, context))
        await asyncio.wait_for(binding_check_waiting.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)

    try:
        run(scenario())
        receipt = execution.receipt(invocation.operation_key())
        usage = run(control.read_typed_budget_usage(owner, BudgetUnit.DISPATCHES))
        assert receipt is not None and receipt.state is EffectState.CANCELLED
        assert receipt.handler_entered is False
        assert execution.unsettled(run_id="run-task") == ()
        assert handler_calls == []
        assert (usage.actual, usage.reserved, usage.unknown) == (0, 0, 0)
    finally:
        execution.close()
        control.close()


def test_gateway_pre_cancel_before_admission_leaves_no_sqlite_intent_or_budget_fact(
    tmp_path: Path,
) -> None:
    control = _store(tmp_path / "control.sqlite")
    execution = SqliteExecutionBackend(tmp_path / "execution.sqlite")
    owner = BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, "ns", "task")
    run(control.put_budget_policy(_policy(owner, BudgetUnit.DISPATCHES, 1)))
    handler_calls: list[str] = []

    async def handler(_context: Any, arguments: Any) -> str:
        handler_calls.append(arguments["term"])
        return "read"

    gateway = ToolGateway(
        registry=ToolRegistry(
            [ToolDefinition("notes.read", "Read a note", args_validator=lambda value: value, handler=handler)]
        ),
        policy=ConservativePolicy(auto_write=True),
        approvals=InMemoryApprovalStore(),
        backend=execution,
        dispatch_budget=TypedDispatchBudget(control, task_id="task"),
    )
    context = _run_context()
    context.cancellation.cancel("cancelled before admission")
    invocation = ToolInvocation(
        call_id="call-pre-cancelled-before-admission",
        namespace="ns",
        session_id="session",
        run_id="run-task",
        model_action_id="model-action-pre-cancelled-before-admission",
        tool_name="notes.read",
        arguments={"term": "alpha"},
    )

    try:
        result = run(gateway.invoke(invocation, context))
        usage = run(control.read_typed_budget_usage(owner, BudgetUnit.DISPATCHES))
        assert result.status is InvocationStatus.CANCELLED
        assert result.receipt is None
        assert execution.receipt(invocation.operation_key()) is None
        assert execution.unsettled(run_id="run-task") == ()
        assert execution.counts_by_state() == {}
        assert run(control.read_typed_budget_reservations(task_id="task")) == ()
        assert (usage.actual, usage.reserved, usage.unknown) == (0, 0, 0)
        assert handler_calls == []
    finally:
        execution.close()
        control.close()


def test_gateway_cancellation_after_handler_receipt_finishes_budget_settlement(
    tmp_path: Path,
) -> None:
    control = _store(tmp_path / "control.sqlite")
    execution = SqliteExecutionBackend(tmp_path / "execution.sqlite")
    owner = BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, "ns", "task")
    run(control.put_budget_policy(_policy(owner, BudgetUnit.DISPATCHES, 1)))
    settlement_started = asyncio.Event()
    release_settlement = asyncio.Event()
    handler_calls: list[str] = []
    typed_budget = TypedDispatchBudget(control, task_id="task")

    class BlockingSettlementBudget:
        async def reserve_tool(self, invocation: Any, context: Any) -> Any:
            return await typed_budget.reserve_tool(invocation, context)

        async def settle_tool(self, reservation: Any, receipt: Any) -> None:
            settlement_started.set()
            await release_settlement.wait()
            await typed_budget.settle_tool(reservation, receipt)

        async def reconcile_tool(self, invocation: Any, context: Any, receipt: Any) -> None:
            await typed_budget.reconcile_tool(invocation, context, receipt)

    async def handler(_context: Any, arguments: Any) -> str:
        handler_calls.append(arguments["term"])
        return "read"

    gateway = ToolGateway(
        registry=ToolRegistry(
            [ToolDefinition("notes.read", "Read a note", args_validator=lambda value: value, handler=handler)]
        ),
        policy=ConservativePolicy(auto_write=True),
        approvals=InMemoryApprovalStore(),
        backend=execution,
        dispatch_budget=BlockingSettlementBudget(),
    )
    invocation = ToolInvocation(
        call_id="call-cancelled-after-handler-receipt",
        namespace="ns",
        session_id="session",
        run_id="run-task",
        model_action_id="model-action-cancelled-after-handler-receipt",
        tool_name="notes.read",
        arguments={"term": "alpha"},
    )

    async def scenario() -> None:
        task = asyncio.create_task(gateway.invoke(invocation, _run_context()))
        await asyncio.wait_for(settlement_started.wait(), timeout=5)
        receipt = execution.receipt(invocation.operation_key())
        assert receipt is not None and receipt.handler_entered is True
        task.cancel()
        await asyncio.sleep(0)
        release_settlement.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)

    try:
        run(scenario())
        receipt = execution.receipt(invocation.operation_key())
        usage = run(control.read_typed_budget_usage(owner, BudgetUnit.DISPATCHES))
        assert receipt is not None and receipt.state is EffectState.SUCCEEDED
        assert receipt.handler_entered is True
        assert execution.unsettled(run_id="run-task") == ()
        assert len(handler_calls) == 1
        assert (usage.actual, usage.reserved, usage.unknown) == (1, 0, 0)
    finally:
        execution.close()
        control.close()


def test_gateway_cancellation_during_pre_handler_budget_release_finishes_release(
    tmp_path: Path,
) -> None:
    from yanxu_harness.plans import ExecutionBindingRefused

    control = _store(tmp_path / "control.sqlite")
    execution = SqliteExecutionBackend(tmp_path / "execution.sqlite")
    owner = BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, "ns", "task")
    run(control.put_budget_policy(_policy(owner, BudgetUnit.DISPATCHES, 1)))
    settlement_started = asyncio.Event()
    release_settlement = asyncio.Event()
    handler_calls: list[str] = []
    typed_budget = TypedDispatchBudget(control, task_id="task")

    class BlockingSettlementBudget:
        async def reserve_tool(self, invocation: Any, context: Any) -> Any:
            return await typed_budget.reserve_tool(invocation, context)

        async def settle_tool(self, reservation: Any, receipt: Any) -> None:
            settlement_started.set()
            await release_settlement.wait()
            await typed_budget.settle_tool(reservation, receipt)

        async def reconcile_tool(self, invocation: Any, context: Any, receipt: Any) -> None:
            await typed_budget.reconcile_tool(invocation, context, receipt)

    class RevokedBinding:
        checks = 0

        async def check_tool_dispatch(self) -> None:
            self.checks += 1
            if self.checks == 2:
                raise ExecutionBindingRefused("deployment_revoked", "deployment was revoked")

    async def handler(_context: Any, arguments: Any) -> str:
        handler_calls.append(arguments["term"])
        return "read"

    gateway = ToolGateway(
        registry=ToolRegistry(
            [ToolDefinition("notes.read", "Read a note", args_validator=lambda value: value, handler=handler)]
        ),
        policy=ConservativePolicy(auto_write=True),
        approvals=InMemoryApprovalStore(),
        backend=execution,
        dispatch_budget=BlockingSettlementBudget(),
    )
    context = RunContext(
        identity=_identity(),
        principal=Principal(subject="worker"),
        execution_binding=RevokedBinding(),
    )
    invocation = ToolInvocation(
        call_id="call-cancelled-during-pre-handler-budget-release",
        namespace="ns",
        session_id="session",
        run_id="run-task",
        model_action_id="model-action-cancelled-during-pre-handler-budget-release",
        tool_name="notes.read",
        arguments={"term": "alpha"},
    )

    async def scenario() -> None:
        task = asyncio.create_task(gateway.invoke(invocation, context))
        await asyncio.wait_for(settlement_started.wait(), timeout=5)
        task.cancel()
        await asyncio.sleep(0)
        release_settlement.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)

    try:
        run(scenario())
        receipt = execution.receipt(invocation.operation_key())
        usage = run(control.read_typed_budget_usage(owner, BudgetUnit.DISPATCHES))
        assert receipt is not None and receipt.state is EffectState.REJECTED
        assert receipt.handler_entered is False
        assert execution.unsettled(run_id="run-task") == ()
        assert handler_calls == []
        assert (usage.actual, usage.reserved, usage.unknown) == (0, 0, 0)
    finally:
        execution.close()
        control.close()


def test_gateway_cancellation_while_budget_reservation_waits_settles_no_dispatch(
    tmp_path: Path,
) -> None:
    control = _store(tmp_path / "control.sqlite")
    execution = SqliteExecutionBackend(tmp_path / "execution.sqlite")
    owner = BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, "ns", "task")
    run(control.put_budget_policy(_policy(owner, BudgetUnit.DISPATCHES, 1)))
    reservation_ready = asyncio.Event()
    release_reservation = asyncio.Event()
    handler_calls: list[str] = []
    typed_budget = TypedDispatchBudget(control, task_id="task")

    class BlockingDispatchBudget:
        async def reserve_tool(self, invocation: Any, context: Any) -> Any:
            reservation = await typed_budget.reserve_tool(invocation, context)
            reservation_ready.set()
            await release_reservation.wait()
            return reservation

        async def settle_tool(self, reservation: Any, receipt: Any) -> None:
            await typed_budget.settle_tool(reservation, receipt)

        async def reconcile_tool(self, invocation: Any, context: Any, receipt: Any) -> None:
            await typed_budget.reconcile_tool(invocation, context, receipt)

    async def handler(_context: Any, arguments: Any) -> str:
        handler_calls.append(arguments["term"])
        return "read"

    gateway = ToolGateway(
        registry=ToolRegistry(
            [ToolDefinition("notes.read", "Read a note", args_validator=lambda value: value, handler=handler)]
        ),
        policy=ConservativePolicy(auto_write=True),
        approvals=InMemoryApprovalStore(),
        backend=execution,
        dispatch_budget=BlockingDispatchBudget(),
    )
    invocation = ToolInvocation(
        call_id="call-cancelled-during-budget-reservation",
        namespace="ns",
        session_id="session",
        run_id="run-task",
        model_action_id="model-action-cancelled-budget-reservation",
        tool_name="notes.read",
        arguments={"term": "alpha"},
    )

    async def scenario() -> None:
        task = asyncio.create_task(gateway.invoke(invocation, _run_context()))
        await asyncio.wait_for(reservation_ready.wait(), timeout=5)
        task.cancel()
        await asyncio.sleep(0)
        release_reservation.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)

    try:
        run(scenario())
        receipt = execution.receipt(invocation.operation_key())
        usage = run(control.read_typed_budget_usage(owner, BudgetUnit.DISPATCHES))
        assert receipt is not None and receipt.state is EffectState.CANCELLED
        assert receipt.handler_entered is False
        assert execution.unsettled(run_id="run-task") == ()
        reservations = run(control.read_typed_budget_reservations(task_id="task"))
        assert len(reservations) == 1
        assert reservations[0].state is ReservationState.RELEASED
        assert handler_calls == []
        assert (usage.actual, usage.reserved, usage.unknown) == (0, 0, 0)
    finally:
        execution.close()
        control.close()


@pytest.mark.parametrize("failure", ["cancelled", "error"])
def test_gateway_reconciles_reservation_when_reserve_fails_before_returning_handle(
    tmp_path: Path, failure: str
) -> None:
    control = _store(tmp_path / "control.sqlite")
    execution = SqliteExecutionBackend(tmp_path / "execution.sqlite")
    owner = BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, "ns", "task")
    run(control.put_budget_policy(_policy(owner, BudgetUnit.DISPATCHES, 1)))
    handler_calls: list[str] = []
    typed_budget = TypedDispatchBudget(control, task_id="task")

    class FailingAfterReserveBudget:
        async def reserve_tool(self, invocation: Any, context: Any) -> Any:
            await typed_budget.reserve_tool(invocation, context)
            if failure == "cancelled":
                raise asyncio.CancelledError("reservation response was cancelled")
            raise RuntimeError("reservation response was lost")

        async def settle_tool(self, reservation: Any, receipt: Any) -> None:
            await typed_budget.settle_tool(reservation, receipt)

        async def reconcile_tool(self, invocation: Any, context: Any, receipt: Any) -> None:
            await typed_budget.reconcile_tool(invocation, context, receipt)

    async def handler(_context: Any, arguments: Any) -> str:
        handler_calls.append(arguments["term"])
        return "read"

    gateway = ToolGateway(
        registry=ToolRegistry(
            [ToolDefinition("notes.read", "Read a note", args_validator=lambda value: value, handler=handler)]
        ),
        policy=ConservativePolicy(auto_write=True),
        approvals=InMemoryApprovalStore(),
        backend=execution,
        dispatch_budget=FailingAfterReserveBudget(),
    )
    invocation = ToolInvocation(
        call_id=f"call-reservation-handle-lost-{failure}",
        namespace="ns",
        session_id="session",
        run_id="run-task",
        model_action_id=f"model-action-reservation-handle-lost-{failure}",
        tool_name="notes.read",
        arguments={"term": "alpha"},
    )

    async def scenario() -> None:
        if failure == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await gateway.invoke(invocation, _run_context())
        else:
            result = await gateway.invoke(invocation, _run_context())
            assert result.status is InvocationStatus.DENIED
            assert result.receipt is not None
            assert result.receipt.handler_entered is False

    try:
        run(scenario())
        receipt = execution.receipt(invocation.operation_key())
        usage = run(control.read_typed_budget_usage(owner, BudgetUnit.DISPATCHES))
        reservations = run(control.read_typed_budget_reservations(task_id="task"))
        assert receipt is not None
        assert receipt.handler_entered is False
        assert receipt.state is (
            EffectState.CANCELLED if failure == "cancelled" else EffectState.REJECTED
        )
        assert len(reservations) == 1
        assert reservations[0].state is ReservationState.RELEASED
        assert execution.unsettled(run_id="run-task") == ()
        assert handler_calls == []
        assert (usage.actual, usage.reserved, usage.unknown) == (0, 0, 0)
    finally:
        execution.close()
        control.close()


def test_gateway_records_context_cancellation_after_admission_before_reservation(
    tmp_path: Path,
) -> None:
    control = _store(tmp_path / "control.sqlite")
    owner = BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, "ns", "task")
    run(control.put_budget_policy(_policy(owner, BudgetUnit.DISPATCHES, 1)))
    context = _run_context()
    handler_calls: list[str] = []

    class CancelAfterAdmission(SqliteExecutionBackend):
        async def admit(self, intent: Any) -> Any:
            admission = await super().admit(intent)
            context.cancellation.cancel("injected after durable admission")
            return admission

    execution = CancelAfterAdmission(tmp_path / "execution.sqlite")

    async def handler(_context: Any, arguments: Any) -> str:
        handler_calls.append(arguments["term"])
        return "read"

    gateway = ToolGateway(
        registry=ToolRegistry(
            [ToolDefinition("notes.read", "Read a note", args_validator=lambda value: value, handler=handler)]
        ),
        policy=ConservativePolicy(auto_write=True),
        approvals=InMemoryApprovalStore(),
        backend=execution,
        dispatch_budget=TypedDispatchBudget(control, task_id="task"),
    )
    invocation = ToolInvocation(
        call_id="call-context-cancelled-after-admission",
        namespace="ns",
        session_id="session",
        run_id="run-task",
        model_action_id="model-action-context-cancelled-after-admission",
        tool_name="notes.read",
        arguments={"term": "alpha"},
    )

    try:
        result = run(gateway.invoke(invocation, context))
        receipt = execution.receipt(invocation.operation_key())
        usage = run(control.read_typed_budget_usage(owner, BudgetUnit.DISPATCHES))
        assert result.status is InvocationStatus.CANCELLED
        assert result.receipt is not None and result.receipt.handler_entered is False
        assert receipt is not None and receipt.state is EffectState.CANCELLED
        assert receipt.handler_entered is False
        assert execution.unsettled(run_id="run-task") == ()
        assert run(control.read_typed_budget_reservations(task_id="task")) == ()
        assert handler_calls == []
        assert (usage.actual, usage.reserved, usage.unknown) == (0, 0, 0)
    finally:
        execution.close()
        control.close()


def test_pre_reservation_binding_refusal_replays_when_dispatch_quota_is_exhausted(
    tmp_path: Path,
) -> None:
    from yanxu_harness.plans import ExecutionBindingRefused

    control = _store(tmp_path / "control.sqlite")
    execution = SqliteExecutionBackend(tmp_path / "execution.sqlite")
    owner = BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, "ns", "task")
    run(control.put_budget_policy(_policy(owner, BudgetUnit.DISPATCHES, 0)))
    handler_calls: list[str] = []

    class RefusingBinding:
        async def check_tool_dispatch(self) -> None:
            raise ExecutionBindingRefused("deployment_revoked", "deployment was revoked")

    async def handler(_context: Any, arguments: Any) -> str:
        handler_calls.append(arguments["term"])
        return "read"

    gateway = ToolGateway(
        registry=ToolRegistry(
            [ToolDefinition("notes.read", "Read a note", args_validator=lambda value: value, handler=handler)]
        ),
        policy=ConservativePolicy(auto_write=True),
        approvals=InMemoryApprovalStore(),
        backend=execution,
        dispatch_budget=TypedDispatchBudget(control, task_id="task"),
    )
    context = RunContext(
        identity=_identity(),
        principal=Principal(subject="worker"),
        execution_binding=RefusingBinding(),
    )
    invocation = ToolInvocation(
        call_id="call-binding-refused-before-budget",
        namespace="ns",
        session_id="session",
        run_id="run-task",
        model_action_id="model-action-binding-refused-before-budget",
        tool_name="notes.read",
        arguments={"term": "alpha"},
    )

    try:
        first = run(gateway.invoke(invocation, context))
        reviewed = run(gateway.review(invocation, context))
        usage = run(control.read_typed_budget_usage(owner, BudgetUnit.DISPATCHES))
        assert first.status is InvocationStatus.DENIED
        assert first.receipt is not None and first.receipt.handler_entered is False
        assert reviewed is not None and reviewed.status is InvocationStatus.DENIED
        assert reviewed.receipt is not None and reviewed.receipt.handler_entered is False
        assert run(control.read_typed_budget_reservations(task_id="task")) == ()
        assert (usage.actual, usage.reserved, usage.unknown) == (0, 0, 0)
        assert handler_calls == []
    finally:
        execution.close()
        control.close()


def test_late_usage_resolves_a_hard_bound_reservation_idempotently(tmp_path: Path) -> None:
    store = _store(tmp_path / "control.sqlite")
    owner = BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, "ns", "task")
    run(
        store.put_budget_policy(
            _snapshot(
                owner,
                BudgetLimit(BudgetUnit.MODEL_REQUESTS, 3),
                BudgetLimit(
                    BudgetUnit.OUTPUT_TOKENS,
                    20,
                    hard_bound_ref="host:output-bound-v1",
                ),
            )
        )
    )
    budget = TypedDispatchBudget(
        store, task_id="task", upper_bounds={"host:output-bound-v1": 8}
    )
    try:
        reservation = run(
            budget.reserve(
                {"task_id": "task", "run_id": "run-task", "request_instance": "late-request"}
            )
        )
        items = budget._reservation_items(reservation)  # noqa: SLF001 - inspect source IDs
        output = next(item for item in items if item.dispatch.unit is BudgetUnit.OUTPUT_TOKENS)
        run(budget.settle(reservation, {"status": "unknown", "detail": "lost response"}))
        late = run(
            budget.record_late_usage(
                output.reservation_id,
                report_id="provider-final:late-request",
                unit=BudgetUnit.OUTPUT_TOKENS,
                actual=9,
            )
        )
        replay = run(
            budget.record_late_usage(
                output.reservation_id,
                report_id="provider-final:late-request",
                unit=BudgetUnit.OUTPUT_TOKENS,
                actual=9,
            )
        )
        with pytest.raises(ContractError, match="different report identity"):
            run(
                budget.record_late_usage(
                    output.reservation_id,
                    report_id="provider-final:another-report",
                    unit=BudgetUnit.OUTPUT_TOKENS,
                    actual=9,
                )
            )
        usage = run(store.read_typed_budget_usage(owner, BudgetUnit.OUTPUT_TOKENS))
        assert late == replay
        assert late.state is ReservationState.SETTLED and late.settled_actual == 9
        assert (usage.actual, usage.over_limit, usage.unknown) == (9, 0, 0)
    finally:
        store.close()


def test_hard_cost_bound_records_cost_metadata_and_full_overage(tmp_path: Path) -> None:
    store = _store(tmp_path / "control.sqlite")
    owner = BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, "ns", "task")
    run(
        store.put_budget_policy(
            _snapshot(
                owner,
                BudgetLimit(BudgetUnit.MODEL_REQUESTS, 2),
                BudgetLimit(
                    BudgetUnit.COST_MICROS,
                    2000,
                    hard_bound_ref="host:cost-cap-v1",
                    currency="USD",
                    price_version="price-2026-10",
                ),
            )
        )
    )
    budget = TypedDispatchBudget(
        store, task_id="task", upper_bounds={"host:cost-cap-v1": 2000}
    )
    stream = _RecordingStream(
        usage=Usage(
            input=4,
            output=5,
            cost={
                "amount_micros": 2300,
                "currency": "USD",
                "price_version": "price-2026-10",
            },
        )
    )
    harness = DefaultHarness(
        stream_fn=stream,
        persistence=store,
        control=store,
        task_id="task",
        budget=budget,
    )
    try:
        run(harness.start(RunRequest(prompt="cost test"), _run_context()))
        assert stream.calls == 1
        usage = run(store.read_typed_budget_usage(owner, BudgetUnit.COST_MICROS))
        assert (usage.actual, usage.currency, usage.price_version) == (
            2300,
            "USD",
            "price-2026-10",
        )
        assert usage.upper_bound_overrun == 300
        with pytest.raises(BudgetRefused):
            run(
                budget.reserve(
                    {"task_id": "task", "run_id": "run-task", "request_instance": "cost-request-2"}
                )
            )
    finally:
        store.close()


def test_auxiliary_summary_model_request_uses_typed_budget(tmp_path: Path) -> None:
    import test_context_summary as summary_fixture

    store = _store(tmp_path / "control.sqlite")
    owner = BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, "ns", "task")
    run(store.put_budget_policy(_policy(owner, BudgetUnit.MODEL_REQUESTS, 1)))
    budget = TypedDispatchBudget(store, task_id="task")
    summarizer = summary_fixture.RecordingSummarizer(
        usage={"input_tokens": 30, "output_tokens": 12}
    )
    compact = summary_fixture.compactor(summarizer)
    try:
        result = run(
            compact.compact(
                summary_fixture.history(),
                scope="ns/session/run-task",
                source_head="sha256:summary-head",
                capacity=summary_fixture.TIGHT,
                dispatch_budget=budget,
                dispatch_context={"task_id": "task", "run_id": "run-task"},
            )
        )
        assert result.used_summary
        assert summarizer.calls == 1
        model_usage = run(store.read_typed_budget_usage(owner, BudgetUnit.MODEL_REQUESTS))
        input_usage = run(store.read_typed_budget_usage(owner, BudgetUnit.INPUT_TOKENS))
        output_usage = run(store.read_typed_budget_usage(owner, BudgetUnit.OUTPUT_TOKENS))
        assert model_usage.actual == 1
        assert input_usage.actual == 30
        assert output_usage.actual == 12
    finally:
        store.close()


def test_default_harness_passes_typed_budget_into_context_summary_dispatch(tmp_path: Path) -> None:
    import test_context_summary as summary_fixture
    from yanxu_harness.context import (
        ContextController,
        ContextWindow,
        HeuristicTokenEstimator,
        SummaryBudget,
    )

    store = _store(tmp_path / "control.sqlite")
    owner = BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, "ns", "task")
    run(store.put_budget_policy(_policy(owner, BudgetUnit.MODEL_REQUESTS, 2)))
    budget = TypedDispatchBudget(store, task_id="task")
    summarizer = summary_fixture.RecordingSummarizer()
    stream = _RecordingStream(usage=Usage(input=40, output=6))
    harness = DefaultHarness(
        stream_fn=stream,
        persistence=store,
        control=store,
        task_id="task",
        budget=budget,
        context_controller=ContextController.compose(
            estimator=HeuristicTokenEstimator(),
            converter=summary_fixture.real_convert,
            window=ContextWindow(limit_tokens=summary_fixture.TIGHT, output_reserve=16),
            summarizer=summarizer,
            summary_budget=SummaryBudget(max_calls=1),
        ),
    )
    try:
        outcome = run(
            harness.start(
                RunRequest(prompt="finish", messages=summary_fixture.history()),
                _run_context(),
            )
        )
        usage = run(store.read_typed_budget_usage(owner, BudgetUnit.MODEL_REQUESTS))
        assert outcome.status.value == "completed"
        assert summarizer.calls == 1
        assert stream.calls == 1
        assert (usage.actual, usage.reserved, usage.unknown) == (2, 0, 0)
    finally:
        store.close()


def test_model_decision_router_uses_typed_budget_before_host_call(tmp_path: Path) -> None:
    store = _store(tmp_path / "control.sqlite")
    owner = BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, "ns", "task")
    run(store.put_budget_policy(_policy(owner, BudgetUnit.MODEL_REQUESTS, 1)))
    budget = TypedDispatchBudget(store, task_id="task")
    calls: list[Mapping[str, Any]] = []

    async def decide(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        calls.append(dict(payload))
        return {"suggestions": [{"candidate_id": "a", "kind": "tool", "rank": 0}]}

    request = DecisionRequest(
        task="choose",
        candidates=(DecisionCandidate("a", DecisionKind.TOOL, "read", "1"),),
        kind=DecisionKind.TOOL,
        scope="ns/session/run-task",
    )
    router = ModelDecisionRouter(
        decide,
        dispatch_budget=budget,
        dispatch_context=lambda _requests: {"task_id": "task", "run_id": "run-task"},
    )
    try:
        outcome = run(router.route(request))
        usage = run(store.read_typed_budget_usage(owner, BudgetUnit.MODEL_REQUESTS))
        assert outcome.status is DecisionStatus.OK
        assert len(calls) == 1
        assert (usage.actual, usage.reserved, usage.unknown) == (1, 0, 0)
    finally:
        store.close()


def test_gateway_tool_dispatch_is_budgeted_once_and_receipt_replay_is_free(tmp_path: Path) -> None:
    control = _store(tmp_path / "control.sqlite")
    execution = SqliteExecutionBackend(tmp_path / "execution.sqlite")
    owner = BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, "ns", "task")
    run(control.put_budget_policy(_policy(owner, BudgetUnit.DISPATCHES, 2)))
    calls: list[str] = []

    async def handler(_context: Any, arguments: Any) -> str:
        calls.append(arguments["term"])
        return "read"

    definition = ToolDefinition(
        "notes.read",
        "Read a note",
        args_validator=lambda value: value,
        handler=handler,
    )
    gateway = ToolGateway(
        registry=ToolRegistry([definition]),
        policy=ConservativePolicy(auto_write=True),
        approvals=InMemoryApprovalStore(),
        backend=execution,
        dispatch_budget=TypedDispatchBudget(control, task_id="task"),
    )
    invocation = ToolInvocation(
        call_id="call-1",
        namespace="ns",
        session_id="session",
        run_id="run-task",
        model_action_id="model-action-1",
        tool_name="notes.read",
        arguments={"term": "alpha"},
    )
    try:
        first = run(gateway.invoke(invocation, _run_context()))
        replay = run(gateway.invoke(invocation, _run_context()))
        usage = run(control.read_typed_budget_usage(owner, BudgetUnit.DISPATCHES))
        assert first.status is InvocationStatus.EXECUTED
        assert replay.status is InvocationStatus.REPLAYED
        assert first.receipt is not None and first.receipt.state is EffectState.SUCCEEDED
        assert calls == ["alpha"]
        assert (usage.actual, usage.reserved, usage.unknown) == (1, 0, 0)
    finally:
        execution.close()
        control.close()


def test_gateway_review_reconciles_unknown_intent_after_admission_response_is_lost(
    tmp_path: Path,
) -> None:
    control = _store(tmp_path / "control.sqlite")

    class CommitThenLoseAdmissionResponse(SqliteExecutionBackend):
        lose_response = True

        async def admit(self, intent: Any) -> Any:
            admission = await super().admit(intent)
            if self.lose_response:
                self.lose_response = False
                raise RuntimeError("admission response was lost after commit")
            return admission

    execution = CommitThenLoseAdmissionResponse(tmp_path / "execution.sqlite")
    owner = BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, "ns", "task")
    run(control.put_budget_policy(_policy(owner, BudgetUnit.DISPATCHES, 1)))
    calls: list[str] = []

    async def handler(_context: Any, arguments: Any) -> str:
        calls.append(arguments["term"])
        return "read"

    gateway = ToolGateway(
        registry=ToolRegistry(
            [
                ToolDefinition(
                    "notes.read",
                    "Read a note",
                    args_validator=lambda value: value,
                    handler=handler,
                )
            ]
        ),
        policy=ConservativePolicy(auto_write=True),
        approvals=InMemoryApprovalStore(),
        backend=execution,
        dispatch_budget=TypedDispatchBudget(control, task_id="task"),
    )
    invocation = ToolInvocation(
        call_id="call-admission-response-lost",
        namespace="ns",
        session_id="session",
        run_id="run-task",
        model_action_id="model-action-recovery",
        tool_name="notes.read",
        arguments={"term": "alpha"},
    )
    try:
        with pytest.raises(RuntimeError, match="response was lost"):
            run(gateway.invoke(invocation, _run_context()))

        before_review = run(control.read_typed_budget_usage(owner, BudgetUnit.DISPATCHES))
        assert before_review.used == 0
        assert calls == []

        reviewed = run(gateway.review(invocation, _run_context()))
        after_review = run(control.read_typed_budget_usage(owner, BudgetUnit.DISPATCHES))
        assert reviewed is not None
        assert reviewed.status is InvocationStatus.UNKNOWN
        assert reviewed.receipt is not None and reviewed.receipt.state is EffectState.UNKNOWN
        assert (after_review.actual, after_review.reserved, after_review.unknown) == (0, 0, 1)
        assert after_review.used == 1
        assert calls == []
    finally:
        execution.close()
        control.close()


def test_gateway_budget_refusal_runs_no_handler_and_writes_no_receipt(tmp_path: Path) -> None:
    control = _store(tmp_path / "control.sqlite")
    execution = SqliteExecutionBackend(tmp_path / "execution.sqlite")
    owner = BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, "ns", "task")
    run(control.put_budget_policy(_policy(owner, BudgetUnit.DISPATCHES, 0)))
    calls: list[str] = []

    async def handler(_context: Any, arguments: Any) -> str:
        calls.append(arguments["term"])
        return "read"

    gateway = ToolGateway(
        registry=ToolRegistry(
            [
                ToolDefinition(
                    "notes.read",
                    "Read a note",
                    args_validator=lambda value: value,
                    handler=handler,
                )
            ]
        ),
        policy=ConservativePolicy(auto_write=True),
        approvals=InMemoryApprovalStore(),
        backend=execution,
        dispatch_budget=TypedDispatchBudget(control, task_id="task"),
    )
    invocation = ToolInvocation(
        call_id="call-blocked",
        namespace="ns",
        session_id="session",
        run_id="run-task",
        model_action_id="model-action-blocked",
        tool_name="notes.read",
        arguments={"term": "alpha"},
    )
    try:
        outcome = run(gateway.invoke(invocation, _run_context()))
        assert outcome.status is InvocationStatus.DENIED
        assert calls == []
        assert run(control.read_typed_budget_reservations(task_id="task")) == ()
        assert run(control.read_typed_budget_usage(owner, BudgetUnit.DISPATCHES)).used == 0
    finally:
        execution.close()
        control.close()
