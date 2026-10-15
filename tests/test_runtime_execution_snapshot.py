"""S4：提交的显式执行状态（snapshot）与它对恢复的含义。

`RunRecord.execution` 是新增的**可选**字段：老记录（无 snapshot）照旧只靠
`messages`/`batch` 恢复，新记录多一份“我当时在哪”的事实。两条已提交事实不一致时，
恢复必须停下来，而不是挑一个相信。
"""

from __future__ import annotations

import asyncio

import pytest

from yanxu_harness.core.execution import (
    EXECUTION_STATE_SCHEMA_VERSION,
    BatchCallState,
    ExecutionState,
    Phase,
    pending_work_of,
    state_from_record,
    state_to_record,
)
from yanxu_harness.core.outcome import LoopStop, RunStatus
from yanxu_harness.core.types import AssistantMessage, StopReason, TextContent, ToolCall
from yanxu_harness.errors import ContractError
from yanxu_harness.runtime import (
    DefaultHarness,
    Principal,
    RunContext,
    RunIdentity,
    RunRequest,
)
from yanxu_harness.runtime.batch import CallOutcome, InteractionKind, RunBatch
from yanxu_harness.runtime.harness import RunRecord
from yanxu_harness.storage import InMemoryPersistence
from yanxu_harness.testing import RecordingExecutionBackend
from yanxu_harness.tools import (
    ConservativePolicy,
    GatewayBindings,
    InMemoryApprovalStore,
    ToolDefinition,
    ToolEffect,
    ToolGateway,
    ToolRegistry,
    core_tool,
)

NAMESPACE = "project-a"


def run(coro):
    async def guard():
        return await asyncio.wait_for(coro, timeout=20)

    return asyncio.run(guard())


def context(attempt: str = "a1"):
    return RunContext(
        identity=RunIdentity(namespace=NAMESPACE, session_id="s1", run_id="r1", attempt_id=attempt),
        principal=Principal(subject="author"),
    )


class Script:
    def __init__(self, *messages: AssistantMessage) -> None:
        self.messages = list(messages)
        self.calls = 0

    async def __call__(self, model, context, options=None):
        await asyncio.sleep(0)
        self.calls += 1
        self._final = self.messages[min(self.calls - 1, len(self.messages) - 1)]
        return self

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def result(self):
        return self._final


def write_definition(handler) -> ToolDefinition:
    return ToolDefinition(
        "notes.write",
        "append to a note",
        effects=frozenset({ToolEffect.WRITE}),
        args_validator=lambda arguments: {"path": arguments["path"]},
        handler=handler,
        preview=lambda _ctx, arguments: {"path": arguments["path"]},
    )


def pausing_deployment(store: InMemoryPersistence) -> DefaultHarness:
    definition = write_definition(lambda _ctx, _args: {"content": [TextContent(text="wrote")]})
    gateway = ToolGateway(
        registry=ToolRegistry([definition]),
        policy=ConservativePolicy(),
        approvals=InMemoryApprovalStore(),
        backend=RecordingExecutionBackend(),
    )
    bindings = GatewayBindings(gateway=gateway, context=lambda: context())
    tool = core_tool(definition, bindings)
    message = AssistantMessage(
        content=[ToolCall(id="call-1", name="notes.write", arguments={"path": "a.md"})],
        stop_reason=StopReason.TOOL_USE,
    )
    return DefaultHarness(
        stream_fn=Script(message),
        tools=[tool],
        persistence=store,
        system_prompt="be brief",
    )


def test_a_pause_commits_the_explicit_state_alongside_the_batch() -> None:
    store = InMemoryPersistence()
    harness = pausing_deployment(store)
    outcome = run(harness.start(RunRequest(prompt="write it"), context()))

    assert outcome.status is RunStatus.SUSPENDED
    record, _ = run(harness._load("r1"))  # noqa: SLF001 - reading the durable record
    assert record is not None and record.execution, "the pause must commit where the run was"

    snapshot = state_from_record(record.execution)
    assert snapshot.schema_version == EXECUTION_STATE_SCHEMA_VERSION
    assert snapshot.phase is Phase.SUSPENDED
    # The model's response is in the committed state, and the batch it asked for is
    # partitioned: one waiting call, nothing settled.
    assert snapshot.last_assistant is not None
    assert [call.outcome for call in snapshot.batch] == [CallOutcome.AWAITING]
    assert snapshot.interaction is not None and snapshot.interaction["kind"] == "approval"


def test_the_outcome_reports_the_pending_work_projection() -> None:
    store = InMemoryPersistence()
    harness = pausing_deployment(store)
    outcome = run(harness.start(RunRequest(prompt="write it"), context()))

    work = outcome.pending_work
    assert [item["call_id"] for item in work["blocking"]] == ["call-1"]
    assert work["blocking"][0]["outcome"] == CallOutcome.AWAITING.value
    assert work["replayable"] == []
    assert work["batch_outcome_unknown"] is False
    assert work["suspended"] is True
    assert work["model_request_outstanding"] is False


def test_the_read_state_view_reports_the_same_projection() -> None:
    store = InMemoryPersistence()
    harness = pausing_deployment(store)
    run(harness.start(RunRequest(prompt="write it"), context()))
    state = run(harness.state("r1"))
    assert state.status is RunStatus.SUSPENDED
    assert [item["call_id"] for item in state.pending_work["blocking"]] == ["call-1"]


def test_a_record_without_a_snapshot_is_still_readable() -> None:
    """A ``v1`` record written before the snapshot existed keeps working."""
    store = InMemoryPersistence()
    harness = pausing_deployment(store)
    run(harness.start(RunRequest(prompt="write it"), context()))
    record, version = run(harness._load("r1"))  # noqa: SLF001
    assert record is not None

    without = record.to_record()
    without["execution"] = None
    rebuilt = RunRecord.from_record(without)
    assert rebuilt.execution is None
    assert rebuilt.pending_work() == {}
    # The batch alone still answers the question the pause needs answered.
    batch = RunBatch.from_record(rebuilt.batch)
    assert [call.outcome for call in batch.calls] == [CallOutcome.AWAITING]
    assert version is not None


def test_a_snapshot_that_disagrees_with_the_batch_is_refused_on_resume() -> None:
    """Two committed facts, one batch: a disagreement stops the resume."""
    store = InMemoryPersistence()
    harness = pausing_deployment(store)
    run(harness.start(RunRequest(prompt="write it"), context()))
    record, version = run(harness._load("r1"))  # noqa: SLF001
    assert record is not None and record.execution

    # Rewrite the snapshot's own batch partition to name a *different* waiting call.
    # Nothing else about the record changes, so the recorded batch and the committed
    # state now disagree about which call is waiting.
    tampered = dict(record.execution)
    tampered["batch"] = [dict(tampered["batch"][0])]
    tampered["batch"][0]["call_id"] = "call-other"
    damaged = record.to_record()
    damaged["execution"] = tampered
    run(
        store.save_run(
            "r1", RunRecord.from_record(damaged).to_record(), expected_version=version
        )
    )

    async def scenario() -> None:
        fresh = DefaultHarness(
            stream_fn=Script(AssistantMessage(content=[TextContent(text="ok")], stop_reason=StopReason.STOP)),
            tools=list(harness.tools),
            persistence=store,
            system_prompt="be brief",
        )
        with pytest.raises(ContractError, match="disagree"):
            await fresh.resume("r1", {}, context("a2"))

    run(scenario())


def test_a_tampered_projection_cannot_make_the_snapshot_disagree() -> None:
    """``pending_work`` is derived, not authoritative: the check reads the state.

    Editing only the stored projection must change nothing, because the authoritative
    half is the batch partition. A test that tampered with the projection and expected a
    refusal would be asserting on the wrong half.
    """
    from yanxu_harness.core.execution import BatchCallState

    state = ExecutionState(
        phase=Phase.SUSPENDED,
        batch=(
            BatchCallState(
                call_id="c1", tool_name="w", outcome=CallOutcome.AWAITING,
                approval_binding_key="ns:c1",
            ),
        ),
        interaction={"kind": "approval", "call_id": "c1"},
        status=RunStatus.SUSPENDED,
        stop=LoopStop.SUSPENDED,
    )
    record = state_to_record(state)
    record["pending_work"] = {"blocking": [{"call_id": "other"}]}
    rebuilt = state_from_record(record)
    assert [call.call_id for call in rebuilt.batch] == ["c1"]
    assert [item["call_id"] for item in pending_work_of(rebuilt)["blocking"]] == ["c1"]


def test_a_suspended_state_cannot_claim_a_succeeded_call_without_a_result() -> None:
    from yanxu_harness.core.execution import BatchCallState

    with pytest.raises(ContractError):
        BatchCallState(call_id="c1", tool_name="w", outcome=CallOutcome.SETTLED)


def test_pending_work_of_a_running_record_is_the_dispatch_window() -> None:
    from yanxu_harness.core.execution import ExecutionState

    message = AssistantMessage(
        content=[ToolCall(id="c1", name="w", arguments={})], stop_reason=StopReason.TOOL_USE
    )
    state = ExecutionState(phase=Phase.RUNNING_BATCH, last_assistant=message)
    record = RunRecord(
        run_id="r1",
        namespace=NAMESPACE,
        session_id="s1",
        status=RunStatus.RUNNING,
        execution=dict(state_to_record(state)),
    )
    work = record.pending_work()
    assert work["batch_outcome_unknown"] is True
    assert [item["call_id"] for item in work["declared"]] == ["c1"]
    assert pending_work_of(state)["declared"] == work["declared"]
    # No outcome is claimed for the declared call, so nothing is replayable by default.
    assert work["replayable"] == []


# --- cancellation recorded while suspended (S5.6) --------------------------------------


def _cancel_pair(store: InMemoryPersistence):
    """A pausing deployment whose handler records every call it receives."""
    seen: list[str] = []

    def handler(ctx, arguments):
        seen.append(ctx.tool_call_id)
        return {"content": [TextContent(text="wrote")]}

    definition = write_definition(handler)
    gateway = ToolGateway(
        registry=ToolRegistry([definition]),
        policy=ConservativePolicy(),
        approvals=InMemoryApprovalStore(),
        backend=RecordingExecutionBackend(),
    )
    bindings = GatewayBindings(gateway=gateway, context=lambda: context())
    tool = core_tool(definition, bindings)
    message = AssistantMessage(
        content=[ToolCall(id="call-1", name="notes.write", arguments={"path": "a.md"})],
        stop_reason=StopReason.TOOL_USE,
    )
    harness = DefaultHarness(
        stream_fn=Script(message), tools=[tool], persistence=store, system_prompt="be brief"
    )
    return harness, seen


def test_a_cancellation_while_suspended_is_recorded_and_recognized_before_resume() -> None:
    """The pause stays durable, the cancellation is durable, and a resume obeys it."""
    store = InMemoryPersistence()
    harness, seen = _cancel_pair(store)
    outcome = run(harness.start(RunRequest(prompt="write it"), context()))
    assert outcome.status is RunStatus.SUSPENDED

    run(harness.cancel("r1", "operator stopped it while it waited"))
    record, _ = run(harness._load("r1"))  # noqa: SLF001 - the durable record is the claim
    assert record is not None and record.cancel_requested is not None
    assert record.cancel_requested["reason"] == "operator stopped it while it waited"
    assert record.status is RunStatus.SUSPENDED, (
        "recording the request is not the same as the run having stopped"
    )

    resumed = run(harness.resume("r1", {}, context("a2")))
    assert resumed.status is RunStatus.CANCELLED
    assert resumed.error_code == "cancelled"
    assert resumed.stop_code == LoopStop.ABORTED.value
    assert seen == [], "resuming a cancelled pause must not dispatch anything"

    after, _ = run(harness._load("r1"))  # noqa: SLF001
    assert after is not None and after.batch is not None, "the batch stays as evidence"
    with pytest.raises(ContractError):
        run(harness.resume("r1", {}, context("a3")))


def test_the_cancellation_record_keeps_the_first_reason() -> None:
    store = InMemoryPersistence()
    harness, _seen = _cancel_pair(store)
    run(harness.start(RunRequest(prompt="write it"), context()))
    run(harness.cancel("r1", "first reason"))
    run(harness.cancel("r1", "second reason"))
    record, _ = run(harness._load("r1"))  # noqa: SLF001
    assert record is not None and record.cancel_requested is not None
    assert record.cancel_requested["reason"] == "first reason"


def test_cancelling_a_finished_run_does_nothing() -> None:
    store = InMemoryPersistence()
    definition = write_definition(lambda _ctx, _args: {"content": []})
    gateway = ToolGateway(
        registry=ToolRegistry([definition]),
        policy=ConservativePolicy(),
        approvals=InMemoryApprovalStore(),
        backend=RecordingExecutionBackend(),
    )
    bindings = GatewayBindings(gateway=gateway, context=lambda: context())
    final = AssistantMessage(content=[TextContent(text="done")], stop_reason=StopReason.STOP)
    harness = DefaultHarness(
        stream_fn=Script(final),
        tools=[core_tool(definition, bindings)],
        persistence=store,
        system_prompt="be brief",
    )
    outcome = run(harness.start(RunRequest(prompt="hi"), context()))
    assert outcome.status is RunStatus.COMPLETED
    run(harness.cancel("r1", "too late"))
    record, _ = run(harness._load("r1"))  # noqa: SLF001
    assert record is not None and record.cancel_requested is None
    assert record.status is RunStatus.COMPLETED
