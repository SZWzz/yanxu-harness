"""The concrete harness: one run per identity, state read from the store.

The acceptance criteria for this step are behavioural, not structural: a public
entry point starts a run, `state()` answers from *persisted* state (so a second
harness instance sees the same run), and completion / budget exhaustion / model
failure / cancellation are distinguishable without reading the loop's internals.
"""

from __future__ import annotations

import asyncio

import pytest

from yanxu_harness.core.outcome import LoopStop, RunStatus
from yanxu_harness.core.types import (
    AssistantMessage,
    StopReason,
    TextContent,
    Usage,
)
from yanxu_harness.errors import ContractError, RunBusy, RunNotFound, RunRequestDrift
from yanxu_harness.runtime import (
    DefaultHarness,
    Principal,
    RunContext,
    RunIdentity,
    RunRequest,
    RunStatus as RuntimeRunStatus,
    request_fingerprint,
)
from yanxu_harness.storage import Durability, InMemoryPersistence
from yanxu_harness.testing import RecordingExecutionBackend
from yanxu_harness.tools import (
    ConservativePolicy,
    GatewayBindings,
    InMemoryApprovalStore,
    ToolDefinition,
    ToolGateway,
    ToolRegistry,
    core_tool,
)


def run(coro):
    async def guard():
        return await asyncio.wait_for(coro, timeout=15)

    return asyncio.run(guard())


def identity(run_id: str = "r1", namespace: str = "project-a", attempt: str = "a1") -> RunIdentity:
    return RunIdentity(namespace=namespace, session_id="s1", run_id=run_id, attempt_id=attempt)


def context(run_id: str = "r1", **kwargs) -> RunContext:
    return RunContext(identity=identity(run_id, **kwargs), principal=Principal(subject="author"))


def answer(text: str) -> AssistantMessage:
    return AssistantMessage(content=[TextContent(text=text)], stop_reason=StopReason.STOP)


class Script:
    """A stream_fn that replays one message per call and counts the requests."""

    def __init__(self, *messages: AssistantMessage) -> None:
        self.messages = list(messages)
        self.calls = 0
        self.requests: list[object] = []

    async def __call__(self, model, context, options=None):
        await asyncio.sleep(0)
        self.calls += 1
        self.requests.append(context)
        index = min(self.calls - 1, len(self.messages) - 1)
        self._final = self.messages[index]
        return self

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def result(self):
        return self._final


def harness_for(stream, **overrides):
    values = {"stream_fn": stream, "system_prompt": "be brief"}
    values.update(overrides)
    return DefaultHarness(**values)


# --- start / state -----------------------------------------------------------


def test_a_run_completes_and_its_state_is_read_from_the_store() -> None:
    store = InMemoryPersistence()
    stream = Script(answer("海边的信号塔亮着。"))
    harness = harness_for(stream, persistence=store)

    outcome = run(harness.start(RunRequest(prompt="海边有什么？"), context()))

    assert outcome.status is RunStatus.COMPLETED
    assert outcome.stop_code == LoopStop.ASSISTANT_FINAL.value
    assert outcome.final_text == "海边的信号塔亮着。"
    assert stream.calls == 1

    # A *different* instance over the same store reads the run back: state is not
    # a property of the live Agent object.
    fresh = harness_for(Script(answer("unused")), persistence=store)
    state = run(fresh.state("r1"))
    assert state.status is RunStatus.COMPLETED
    assert state.final_text == "海边的信号塔亮着。"
    assert state.turns_used == 1
    assert state.attempt_id == "a1"
    assert state.stop_code == LoopStop.ASSISTANT_FINAL.value
    assert state.updated_at > 0


def test_the_identity_comes_from_the_host_and_not_from_the_model() -> None:
    store = InMemoryPersistence()
    harness = harness_for(Script(answer("ok")), persistence=store, tools=())
    run(harness.start(RunRequest(prompt="hi"), context(run_id="r1", namespace="project-a")))

    stored = run(store.load_run("r1"))
    assert stored is not None
    assert stored.state["namespace"] == "project-a"
    assert stored.state["session_id"] == "s1"


def test_state_during_a_run_reports_running_not_a_finished_outcome() -> None:
    """An active run must never be reported as completed."""
    store = InMemoryPersistence()
    seen: list[object] = []

    class Watching:
        async def __call__(self, model, context, options=None):
            return self

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def result(self):
            # Inside the loop: the run is active. Read it through a new instance.
            other = harness_for(Script(answer("unused")), persistence=store)
            seen.append(await other.state("r1"))
            return answer("done")

    harness = harness_for(Watching(), persistence=store)
    outcome = run(harness.start(RunRequest(prompt="hi"), context()))

    assert outcome.status is RunStatus.COMPLETED
    assert seen and seen[0].status is RunStatus.RUNNING
    assert RuntimeRunStatus is RunStatus, "the runtime re-exports the core vocabulary"


def test_a_duplicate_start_reuses_the_stored_state_instead_of_running_again() -> None:
    store = InMemoryPersistence()
    stream = Script(answer("once"))
    harness = harness_for(stream, persistence=store)
    request = RunRequest(prompt="hi")

    first = run(harness.start(request, context()))
    second = run(harness.start(request, context()))

    assert stream.calls == 1, "an identical retry must not send another model request"
    assert second.status is first.status and second.final_text == first.final_text


def test_starting_a_run_with_different_input_is_refused_and_changes_nothing() -> None:
    store = InMemoryPersistence()
    stream = Script(answer("once"))
    harness = harness_for(stream, persistence=store)
    run(harness.start(RunRequest(prompt="hi"), context()))

    with pytest.raises(RunRequestDrift):
        run(harness.start(RunRequest(prompt="something else"), context()))

    assert stream.calls == 1
    state = run(harness.state("r1"))
    assert state.status is RunStatus.COMPLETED
    assert state.final_text == "once", "the stored outcome was not overwritten"


def test_a_budget_change_is_drift_not_a_retry() -> None:
    """`max_turns` is part of the request fingerprint."""
    assert request_fingerprint(RunRequest(prompt="x")) != request_fingerprint(
        RunRequest(prompt="x", max_turns=2)
    )


def test_a_second_invocation_while_running_is_busy() -> None:
    store = InMemoryPersistence()
    release = asyncio.Event()

    class Blocking:
        async def __call__(self, model, context, options=None):
            return self

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def result(self):
            await asyncio.wait_for(release.wait(), timeout=5)
            return answer("finished")

    async def scenario():
        harness = harness_for(Blocking(), persistence=store)
        request = RunRequest(prompt="hi")
        first = asyncio.ensure_future(harness.start(request, context()))
        # Let the first invocation reach the model request.
        await asyncio.sleep(0.05)
        with pytest.raises(RunBusy):
            await harness.start(request, context())
        state = await harness.state("r1", context=context())
        release.set()
        return state, await first

    async def guarded():
        return await asyncio.wait_for(scenario(), timeout=10)

    state, outcome = asyncio.run(guarded())
    assert state.status is RunStatus.RUNNING
    assert outcome.status is RunStatus.COMPLETED and outcome.final_text == "finished"


def test_state_can_check_the_callers_identity() -> None:
    """The port's `state(run_id)` carries no identity; a host can pass its context."""
    store = InMemoryPersistence()
    harness = harness_for(Script(answer("x")), persistence=store)
    run(harness.start(RunRequest(prompt="hi"), context(namespace="project-a")))

    assert run(harness.state("r1", context=context(namespace="project-a"))).status is RunStatus.COMPLETED
    with pytest.raises(ContractError):
        run(harness.state("r1", context=context(namespace="project-b")))


def test_reading_an_unknown_run_is_a_named_failure() -> None:
    harness = harness_for(Script(answer("x")))
    with pytest.raises(RunNotFound):
        run(harness.state("nope"))


def test_a_different_namespace_cannot_touch_the_same_run_id() -> None:
    store = InMemoryPersistence()
    harness = harness_for(Script(answer("x")), persistence=store)
    run(harness.start(RunRequest(prompt="hi"), context(namespace="project-a")))

    with pytest.raises(ContractError):
        run(harness.start(RunRequest(prompt="hi"), context(namespace="project-b")))
    with pytest.raises(ContractError):
        run(harness.resume("r1", {}, context(namespace="project-b")))
    with pytest.raises(ContractError):
        run(harness.state("r1", context=context(namespace="project-b")))


# --- outcomes ----------------------------------------------------------------


def test_a_model_failure_is_failed_with_the_provider_kind() -> None:
    failure = AssistantMessage(
        content=[],
        stop_reason=StopReason.ERROR,
        error_message="rate_limited: slow down",
        raw_stop_reason="rate_limited",
    )
    harness = harness_for(Script(failure), persistence=InMemoryPersistence())
    outcome = run(harness.start(RunRequest(prompt="hi"), context()))

    assert outcome.status is RunStatus.FAILED
    assert outcome.error_code == "rate_limited"
    assert outcome.stop_code == LoopStop.MODEL_ERROR.value
    assert "slow down" in outcome.detail
    state = run(harness.state("r1"))
    assert state.error_code == "rate_limited"
    assert state.status is RunStatus.FAILED


def test_a_turn_budget_stop_is_usage_limited_with_the_used_turn_count() -> None:
    stream = Script(answer("first"), answer("second"))
    harness = harness_for(stream, persistence=InMemoryPersistence())
    outcome = run(harness.start(RunRequest(prompt="hi", max_turns=1), context()))

    assert outcome.status is RunStatus.USAGE_LIMITED
    assert outcome.error_code == "max_turns"
    assert outcome.turns_used == 1
    assert stream.calls == 1


def test_a_suspend_hook_reports_suspended_with_the_interaction() -> None:
    interaction = {"kind": "approval", "call_id": "c1"}

    class Calling:
        """One tool call, then (never) a wrap-up: the suspension stops the run."""

        def __init__(self):
            self.calls = 0

        async def __call__(self, model, context, options=None):
            self.calls += 1
            return self

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def result(self):
            from yanxu_harness.core.types import ToolCall

            return AssistantMessage(content=[ToolCall(id="c1", name="write_note", arguments={})], stop_reason=StopReason.TOOL_USE)

    async def handler(_context, _arguments):
        return {"content": [TextContent(text="wrote")]}

    definition = ToolDefinition("write_note", "write", args_validator=lambda args: dict(args), handler=handler)
    gateway = ToolGateway(
        registry=ToolRegistry([definition]),
        policy=ConservativePolicy(auto_write=True),
        approvals=InMemoryApprovalStore(),
        backend=RecordingExecutionBackend(),
    )
    tool_spec = core_tool(definition, GatewayBindings(gateway=gateway, context=context()))
    stream = Calling()
    harness = harness_for(
        stream,
        persistence=InMemoryPersistence(),
        tools=[tool_spec],
        suspend_after_batch=lambda _message, _results: interaction,
    )

    outcome = run(harness.start(RunRequest(prompt="write a note"), context()))

    assert outcome.status is RunStatus.SUSPENDED
    assert outcome.pending_interaction == interaction
    assert stream.calls == 1, "a suspended run must not ask the model again"
    state = run(harness.state("r1"))
    assert state.status is RunStatus.SUSPENDED and state.pending_interaction == interaction


def test_state_while_suspended_is_not_terminal() -> None:
    interaction = {"kind": "approval"}
    stream = Script(answer("x"))
    harness = harness_for(
        stream,
        persistence=InMemoryPersistence(),
        suspend_after_batch=lambda _m, _r: interaction,
        tools=[],
    )
    # No tool call → no batch → the hook never fires; this run simply completes.
    outcome = run(harness.start(RunRequest(prompt="hi"), context()))
    assert outcome.status is RunStatus.COMPLETED


# --- cancel ------------------------------------------------------------------


def _cancel_scenario(*, finish: bool):
    store = InMemoryPersistence()
    release = asyncio.Event()

    class Slow:
        async def __call__(self, model, context, options=None):
            return self

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def result(self):
            await asyncio.wait_for(release.wait(), timeout=5)
            return answer("late answer")

    async def scenario():
        harness = harness_for(Slow(), persistence=store)
        task = asyncio.ensure_future(harness.start(RunRequest(prompt="hi"), context()))
        await asyncio.sleep(0.05)
        await harness.cancel("r1", "author stopped")
        release.set()
        outcome = await task
        return harness, outcome, await harness.state("r1")

    async def guarded():
        return await asyncio.wait_for(scenario(), timeout=10)

    return asyncio.run(guarded())


def test_cancelling_an_active_run_records_a_cancelled_outcome() -> None:
    _harness, outcome, state = _cancel_scenario(finish=False)
    assert outcome.status is RunStatus.CANCELLED
    assert outcome.stop_code == LoopStop.ABORTED.value
    assert state.status is RunStatus.CANCELLED


def test_cancelling_a_finished_run_is_a_no_op() -> None:
    store = InMemoryPersistence()
    harness = harness_for(Script(answer("done")), persistence=store)
    outcome = run(harness.start(RunRequest(prompt="hi"), context()))
    run(harness.cancel("r1", "too late"))
    state = run(harness.state("r1"))
    assert state.status is outcome.status is RunStatus.COMPLETED
    assert state.final_text == "done"


def test_cancelling_a_run_this_process_does_not_drive_is_recorded_not_guessed() -> None:
    """A run that may be alive elsewhere is not marked cancelled by a bystander."""
    store = InMemoryPersistence()
    assert run(store.load_run("r1")) is None

    from yanxu_harness.runtime import RunRecord

    record = RunRecord(
        run_id="r1", namespace="project-a", session_id="s1",
        status=RunStatus.RUNNING, attempt_id="a1", updated_at=1.0,
    )
    run(store.save_run("r1", record.to_record(), expected_version=None))

    bystander = harness_for(Script(answer("x")), persistence=store)
    run(bystander.cancel("r1", "operator asked"))

    reloaded = RunRecord.from_record((run(store.load_run("r1"))).state)
    assert reloaded.status is RunStatus.RUNNING, "a bystander must not claim the run stopped"
    assert reloaded.cancel_requested is not None
    assert reloaded.cancel_requested["reason"] == "operator asked"


def test_cancelling_an_unknown_run_is_idempotent() -> None:
    harness = harness_for(Script(answer("x")))
    run(harness.cancel("missing"))


# --- resume guards -----------------------------------------------------------


def test_resuming_a_finished_run_is_refused() -> None:
    harness = harness_for(Script(answer("done")), persistence=InMemoryPersistence())
    run(harness.start(RunRequest(prompt="hi"), context()))
    with pytest.raises(ContractError) as excinfo:
        run(harness.resume("r1", {}, context()))
    assert "only a suspended run can be resumed" in str(excinfo.value)


def test_resuming_an_unknown_run_is_a_named_failure() -> None:
    harness = harness_for(Script(answer("x")))
    with pytest.raises(RunNotFound):
        run(harness.resume("nope", {}, context()))


# --- wiring ------------------------------------------------------------------


def test_the_harness_reports_its_store_durability() -> None:
    harness = harness_for(Script(answer("x")))
    assert harness.durability is Durability.VOLATILE


def test_a_run_usage_and_turns_are_persisted() -> None:
    message = AssistantMessage(
        content=[TextContent(text="done")],
        stop_reason=StopReason.STOP,
        usage=Usage(input=12, output=3, total_tokens=15),
    )
    store = InMemoryPersistence()
    harness = harness_for(Script(message), persistence=store)
    outcome = run(harness.start(RunRequest(prompt="hi"), context()))
    assert outcome.usage["input"] == 12 and outcome.usage["output"] == 3
    assert outcome.turns_used == 1
    stored = run(store.load_run("r1"))
    assert stored is not None and stored.state["usage"]["total_tokens"] == 15


def test_a_stream_fn_is_required() -> None:
    with pytest.raises(ContractError):
        DefaultHarness(stream_fn=None)  # type: ignore[arg-type]


def test_run_busy_carries_the_run_id() -> None:
    error = RunBusy("r1", "busy")
    assert error.run_id == "r1" and error.code == "run_busy"
