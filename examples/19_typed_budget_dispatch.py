"""Offline B2 demonstration of model, auxiliary and tool dispatch accounting.

All calls are local fakes. The example opens no network connection and performs no real tool I/O.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

from yanxu_harness.core.types import AssistantMessage, StopReason, TextContent, Usage
from yanxu_harness.runtime import DefaultHarness, RunRequest
from yanxu_harness.runtime.context import Principal, RunContext, RunIdentity
from yanxu_harness.storage import SqliteExecutionBackend
from yanxu_harness.tasks import (
    BudgetLimit,
    BudgetOwnerKind,
    BudgetOwnerRef,
    BudgetPolicySnapshot,
    BudgetUnit,
    SqliteControlStore,
    TaskAdmission,
    TypedDispatchBudget,
)
from yanxu_harness.tools import (
    ConservativePolicy,
    InMemoryApprovalStore,
    ToolDefinition,
    ToolGateway,
    ToolInvocation,
    ToolRegistry,
)


def _emit(value: dict[str, object]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


class _Stream:
    def __init__(self) -> None:
        self.message = AssistantMessage(
            content=[TextContent(text="done")],
            stop_reason=StopReason.STOP,
            usage=Usage(input=24, output=4),
        )

    def __call__(self, _model: Any, _context: Any, _options: Any = None) -> Any:
        message = self.message

        class Response:
            async def result(self) -> AssistantMessage:
                return message

            def __aiter__(self) -> Any:
                async def frames() -> Any:
                    yield {"type": "done", "partial": message}

                return frames()

        return Response()


async def _run(directory: Path) -> None:
    control = SqliteControlStore(directory / "control.sqlite", enable_typed_budgets=True)
    execution = SqliteExecutionBackend(directory / "execution.sqlite")
    try:
        await control.admit_task(
            TaskAdmission(
                namespace="demo-ns",
                session_id="demo-session",
                task_id="demo-task",
                run_id="demo-run",
                root_task_id="demo-task",
            )
        )
        owner = BudgetOwnerRef(BudgetOwnerKind.TASK_ROOT, "demo-ns", "demo-task")
        await control.put_budget_policy(
            BudgetPolicySnapshot(
                owner=owner,
                session_id="demo-session",
                policy_version="dispatch-demo-v1",
                limits=(
                    BudgetLimit(BudgetUnit.MODEL_REQUESTS, 4),
                    BudgetLimit(BudgetUnit.DISPATCHES, 2),
                    BudgetLimit(
                        BudgetUnit.INPUT_TOKENS,
                        1000,
                        hard_bound_ref="demo:input-token-cap-v1",
                    ),
                    BudgetLimit(
                        BudgetUnit.OUTPUT_TOKENS,
                        1000,
                        hard_bound_ref="demo:output-token-cap-v1",
                    ),
                ),
            )
        )
        budget = TypedDispatchBudget(
            control,
            task_id="demo-task",
            upper_bounds={
                "demo:input-token-cap-v1": 256,
                "demo:output-token-cap-v1": 64,
            },
        )
        context = RunContext(
            identity=RunIdentity(
                namespace="demo-ns",
                session_id="demo-session",
                run_id="demo-run",
                attempt_id="demo-attempt",
            ),
            principal=Principal(subject="offline-demo"),
        )

        harness = DefaultHarness(
            stream_fn=_Stream(),
            persistence=control,
            control=control,
            task_id="demo-task",
            budget=budget,
        )
        outcome = await harness.start(RunRequest(prompt="answer locally"), context)
        requests = await control.read_typed_budget_usage(owner, BudgetUnit.MODEL_REQUESTS)
        input_tokens = await control.read_typed_budget_usage(owner, BudgetUnit.INPUT_TOKENS)
        output_tokens = await control.read_typed_budget_usage(owner, BudgetUnit.OUTPUT_TOKENS)
        _emit(
            {
                "stage": "primary_model",
                "status": outcome.status.value,
                "model_requests_actual": requests.actual,
                "input_tokens_actual": input_tokens.actual,
                "output_tokens_actual": output_tokens.actual,
                "provider_calls": 1,
            }
        )

        reservation = await budget.reserve_auxiliary(
            {"task_id": "demo-task", "run_id": "demo-run"}, kind="summary"
        )
        items = (reservation,) if not isinstance(reservation, tuple) else reservation
        await budget.settle_auxiliary(
            reservation, usage=None, detail="offline crash window before usage arrived"
        )
        report_id = "demo-provider-final:summary-1"
        for item in items:
            late_actual = {
                BudgetUnit.MODEL_REQUESTS: 1,
                BudgetUnit.INPUT_TOKENS: 8,
                BudgetUnit.OUTPUT_TOKENS: 3,
            }[item.dispatch.unit]
            await budget.record_late_usage(
                item.reservation_id,
                report_id=report_id,
                unit=item.dispatch.unit,
                actual=late_actual,
            )
        replay = await budget.record_late_usage(
            items[0].reservation_id,
            report_id=report_id,
            unit=items[0].dispatch.unit,
            actual={
                BudgetUnit.MODEL_REQUESTS: 1,
                BudgetUnit.INPUT_TOKENS: 8,
                BudgetUnit.OUTPUT_TOKENS: 3,
            }[items[0].dispatch.unit],
        )
        requests = await control.read_typed_budget_usage(owner, BudgetUnit.MODEL_REQUESTS)
        _emit(
            {
                "stage": "late_auxiliary_usage",
                "state": replay.state.value,
                "same_reservation": replay.reservation_id == items[0].reservation_id,
                "model_requests_actual": requests.actual,
                "usage_reports_are_idempotent": True,
            }
        )

        handler_calls: list[str] = []

        async def read_handler(_context: Any, arguments: Any) -> str:
            handler_calls.append(arguments["key"])
            return "local value"

        gateway = ToolGateway(
            registry=ToolRegistry(
                [
                    ToolDefinition(
                        "demo.read",
                        "Read a local demo value",
                        args_validator=lambda value: value,
                        handler=read_handler,
                    )
                ]
            ),
            policy=ConservativePolicy(auto_write=True),
            approvals=InMemoryApprovalStore(),
            backend=execution,
            dispatch_budget=budget,
        )
        invocation = ToolInvocation(
            call_id="demo-call-1",
            namespace="demo-ns",
            session_id="demo-session",
            run_id="demo-run",
            model_action_id="demo-model-action-1",
            tool_name="demo.read",
            arguments={"key": "alpha"},
        )
        first = await gateway.invoke(invocation, context)
        replayed = await gateway.invoke(invocation, context)
        dispatches = await control.read_typed_budget_usage(owner, BudgetUnit.DISPATCHES)
        _emit(
            {
                "stage": "gateway_receipt_replay",
                "first_status": first.status.value,
                "replay_status": replayed.status.value,
                "handler_calls": len(handler_calls),
                "dispatches_actual": dispatches.actual,
            }
        )
    finally:
        execution.close()
        control.close()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="yanxu-typed-dispatch-") as directory:
        asyncio.run(_run(Path(directory)))


if __name__ == "__main__":
    main()
