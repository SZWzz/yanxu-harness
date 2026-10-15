"""T0–T3: one call's result is a durable fact while its siblings still run.

The claim under test is the plan's headline: a tool that finished must be readable from
the run store *before* the rest of its batch is finished, with the call identity and the
argument digest committed before anything was dispatched. On the baseline this file
fails: the whole batch was one `gather`, so nothing about a single call reached the store
until every call had settled.

The deployment is the real `DefaultHarness` over `SqlitePersistence` and a real
`SqliteExecutionBackend`, with two tools going through `core_tool -> ToolGateway`. The
"independent reader" is a *second* `SqlitePersistence` connection, the way a new process
would read it — not the harness's own in-memory record.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

from durable_edit import DeploymentPaths, ScriptedStream, build, run_context

from yanxu_harness.core.execution import (
    CallProgress,
    Phase,
    ToolCallObserved,
    declared_batch,
    define_state,
    pending_work_of,
    state_from_record,
)
from yanxu_harness.core.outcome import CallOutcome, RunStatus
from yanxu_harness.core.transitions import advance
from yanxu_harness.core.types import (
    AssistantMessage,
    StopReason,
    TextContent,
    ToolCall,
)
from yanxu_harness.errors import ContractError
from yanxu_harness.runtime import DefaultHarness, RunRequest
from yanxu_harness.runtime.harness import RunRecord
from yanxu_harness.storage import (
    SqliteApprovalStore,
    SqliteExecutionBackend,
    SqlitePersistence,
)
from yanxu_harness.tools import (
    ConservativePolicy,
    GatewayBindings,
    ToolDefinition,
    ToolGateway,
    ToolRegistry,
    core_tool,
)

READ = "notes.read"
GATE = "notes.gate"
RUN_ID = "r1"


def run(coro):
    async def guard():
        return await asyncio.wait_for(coro, timeout=30)

    return asyncio.run(guard())


def read_record(path: Path, run_id: str = RUN_ID) -> RunRecord:
    """Read the run through a separate connection, the way a new process would."""
    store = SqlitePersistence(path)
    try:
        stored = run(store.load_run(run_id))
    finally:
        store.close()
    assert stored is not None, "the run must be readable from another connection"
    return RunRecord.from_record(stored.state)


async def until(predicate, *, timeout: float = 5.0, interval: float = 0.01) -> None:
    """Wait for a convergence condition; a timeout is a test failure, not a guess."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        outcome = predicate()
        if inspect.isawaitable(outcome):
            outcome = await outcome
        if outcome:
            return
        await asyncio.sleep(interval)
    raise AssertionError("the condition did not become true within the timeout")


async def read_record_async(path: Path, run_id: str = RUN_ID) -> RunRecord:
    """The same read as :func:`read_record`, from inside a running loop."""
    store = SqlitePersistence(path)
    try:
        stored = await store.load_run(run_id)
    finally:
        store.close()
    assert stored is not None, "the run must be readable from another connection"
    return RunRecord.from_record(stored.state)


class TwoCallDeployment:
    """A read call and a gate call, both through the real gateway and ledger."""

    def __init__(self, paths: DeploymentPaths, *, stream, auto_write: bool = True) -> None:
        self.paths = paths
        self.read_done = asyncio.Event()
        self.gate_entered = asyncio.Event()
        self.release = asyncio.Event()
        self.read_calls = 0
        self.gate_calls = 0

        async def read_handler(_context, arguments):
            self.read_calls += 1
            payload = {"content": [TextContent(text=f"read {arguments['path']}")], "details": {"ok": True}}
            self.read_done.set()
            return payload

        async def gate_handler(_context, arguments):
            self.gate_calls += 1
            # The read call has finished by the time the gate is entered, so the test's
            # observation of the store is about a completed call, not a race.
            await self.read_done.wait()
            self.gate_entered.set()
            await self.release.wait()
            return {"content": [TextContent(text=f"gated {arguments['text']}")], "details": {"ok": True}}

        read = ToolDefinition(
            READ,
            "read a note",
            args_validator=lambda arguments: {"path": arguments["path"]},
            handler=read_handler,
        )
        gate = ToolDefinition(
            GATE,
            "wait for the operator, then act",
            args_validator=lambda arguments: {
                "path": arguments["path"],
                "text": arguments["text"],
            },
            handler=gate_handler,
        )
        self.gateway = ToolGateway(
            registry=ToolRegistry([read, gate]),
            policy=ConservativePolicy(auto_write=auto_write),
            approvals=SqliteApprovalStore(paths.approvals),
            backend=SqliteExecutionBackend(paths.operations),
        )
        bindings = GatewayBindings(gateway=self.gateway, context=lambda: run_context())
        self.harness = DefaultHarness(
            stream_fn=stream,
            tools=[core_tool(read, bindings), core_tool(gate, bindings)],
            persistence=SqlitePersistence(paths.runs),
            system_prompt="be brief",
        )


def two_call_turn() -> AssistantMessage:
    return AssistantMessage(
        content=[
            ToolCall(id="c-read", name=READ, arguments={"path": "a.md"}),
            ToolCall(id="c-gate", name=GATE, arguments={"path": "a.md", "text": "hi"}),
        ],
        stop_reason=StopReason.TOOL_USE,
    )


def final_turn(text: str = "done") -> AssistantMessage:
    return AssistantMessage(content=[TextContent(text=text)], stop_reason=StopReason.STOP)


# --- T3: the finished call is durable while its sibling is still running ----------------


def test_t3_a_finished_call_is_durable_while_its_sibling_still_runs(tmp_path: Path) -> None:
    paths = DeploymentPaths.under(tmp_path / "state")
    stream = ScriptedStream(two_call_turn(), final_turn())
    deployment = TwoCallDeployment(paths, stream=stream)

    async def scenario():
        task = asyncio.ensure_future(
            deployment.harness.start(RunRequest(prompt="do both"), run_context())
        )
        await asyncio.wait_for(deployment.gate_entered.wait(), timeout=10)

        async def committed() -> bool:
            record = await read_record_async(paths.runs)
            if not record.execution:
                return False
            snapshot = state_from_record(record.execution)
            return any(call.observed for call in snapshot.batch)

        await until(committed)
        record = await read_record_async(paths.runs)
        snapshot = state_from_record(record.execution)
        calls = {call.call_id: call for call in snapshot.batch}
        assert set(calls) == {"c-read", "c-gate"}
        read = calls["c-read"]
        assert read.observed is True, "the finished call must be a committed fact"
        assert read.progress is CallProgress.OBSERVED
        assert read.outcome is CallOutcome.SETTLED
        assert read.declared_index == 0
        assert read.model_action_id == "request:1"
        assert read.arguments_digest.startswith("sha256:")
        assert read.result is not None
        assert read.result["content"][0]["text"] == "read a.md"
        assert read.observation_id and read.observed_digest
        assert read.source_attempt == "a1"

        gate = calls["c-gate"]
        assert gate.observed is False, "the blocked call has no outcome to claim"
        assert gate.progress is CallProgress.DECLARED
        assert gate.result is None
        assert gate.declared_index == 1
        assert gate.arguments_digest.startswith("sha256:")

        work = pending_work_of(snapshot)
        assert [item["call_id"] for item in work["unobserved"]] == ["c-gate"]
        assert work["replayable"] == [], (
            "an in-flight call is not proven un-run; only the ledger may say that"
        )
        assert work["batch_outcome_unknown"] is True
        assert [item["call_id"] for item in work["calls"]] == ["c-read", "c-gate"]

        # No model-visible result message may be in the transcript yet: the batch has not
        # settled, and the model must not be handed half a turn.
        assert [m["role"] for m in record.messages][-1] == "assistant"

        deployment.release.set()
        outcome = await asyncio.wait_for(task, timeout=10)
        return outcome

    outcome = run(scenario())
    assert outcome.status is RunStatus.COMPLETED
    assert deployment.read_calls == 1
    assert deployment.gate_calls == 1
    record = read_record(paths.runs)
    results = [m for m in record.messages if m["role"] == "toolResult"]
    assert [m["tool_call_id"] for m in results] == ["c-read", "c-gate"], (
        "the results are appended once, in declared order"
    )
    assert "read a.md" in results[0]["content"][0]["text"]
    assert "gated hi" in results[1]["content"][0]["text"]


def test_t3_the_ledger_records_both_operations_once(tmp_path: Path) -> None:
    paths = DeploymentPaths.under(tmp_path / "state")
    stream = ScriptedStream(two_call_turn(), final_turn())
    deployment = TwoCallDeployment(paths, stream=stream)

    async def scenario():
        task = asyncio.ensure_future(
            deployment.harness.start(RunRequest(prompt="do both"), run_context())
        )
        await asyncio.wait_for(deployment.gate_entered.wait(), timeout=10)
        deployment.release.set()
        return await asyncio.wait_for(task, timeout=10)

    assert run(scenario()).status is RunStatus.COMPLETED
    backend = SqliteExecutionBackend(paths.operations)
    try:
        operations = backend.all_operations()
    finally:
        backend.close()
    assert {item["operation_id"] for item in operations} == {
        "project-a:s1:r1:c-read",
        "project-a:s1:r1:c-gate",
    }
    assert [item["state"] for item in operations] == ["succeeded", "succeeded"]


# --- T2: the reducer's per-call rules ---------------------------------------------------


def _declared_state(*calls: ToolCall):
    """A RUNNING_BATCH state whose declaration partition is already committed."""
    return define_state(
        phase=Phase.RUNNING_BATCH,
        model_action_id="request:1",
        attempt_id="a1",
        batch=declared_batch(list(calls), model_action_id="request:1"),
    )


def test_t2_an_observation_updates_only_its_own_call() -> None:
    state = _declared_state(
        ToolCall(id="c1", name="t", arguments={"a": 1}),
        ToolCall(id="c2", name="t", arguments={"a": 2}),
    )
    transition = advance(
        state,
        ToolCallObserved(
            event_id="e1",
            at_ms=1,
            model_action_id="request:1",
            call_id="c1",
            tool_name="t",
            outcome="settled",
            status="executed",
            result={"role": "toolResult", "tool_call_id": "c1", "content": [], "details": {}},
            observation_id="c1:0:observed",
            observed_digest="sha256:x",
            source_attempt="a1",
        ),
    )
    assert not transition.ignored
    assert transition.state.call("c1").observed is True
    assert transition.state.call("c2").observed is False
    assert transition.actions[0].kind == "AwaitToolObservation"
    assert transition.actions[0].awaiting == ("c2",)


def test_t2_a_repeated_observation_is_ignored_and_a_conflict_is_refused() -> None:
    state = _declared_state(ToolCall(id="c1", name="t", arguments={}))
    first = ToolCallObserved(
        event_id="e1",
        at_ms=1,
        model_action_id="request:1",
        call_id="c1",
        tool_name="t",
        outcome="settled",
        result={"role": "toolResult", "tool_call_id": "c1", "content": [], "details": {}},
        observation_id="c1:0:observed",
        observed_digest="sha256:same",
        source_attempt="a1",
    )
    applied = advance(state, first)
    assert applied.state.call("c1").observed

    repeat = ToolCallObserved(
        event_id="e2",
        at_ms=2,
        model_action_id="request:1",
        call_id="c1",
        tool_name="t",
        outcome="settled",
        result={"role": "toolResult", "tool_call_id": "c1", "content": [], "details": {}},
        observation_id="c1:0:observed",
        observed_digest="sha256:same",
        source_attempt="a1",
    )
    again = advance(applied.state, repeat)
    assert again.ignored, "the same observation twice is the same fact"
    assert again.actions == ()

    conflicting = ToolCallObserved(
        event_id="e3",
        at_ms=3,
        model_action_id="request:1",
        call_id="c1",
        tool_name="t",
        outcome="uncertain",
        status="unknown",
        result={"role": "toolResult", "tool_call_id": "c1", "content": [], "details": {}},
        observation_id="c1:0:observed",
        observed_digest="sha256:different",
        source_attempt="a1",
    )
    with pytest.raises(ContractError, match="second, different observation"):
        advance(applied.state, conflicting)


def test_t2_an_observation_from_another_batch_or_attempt_is_refused() -> None:
    base = _declared_state(ToolCall(id="c1", name="t", arguments={}))
    other_action = ToolCallObserved(
        event_id="e1",
        at_ms=1,
        model_action_id="request:9",
        call_id="c1",
        tool_name="t",
        outcome="settled",
        result={"role": "toolResult", "tool_call_id": "c1", "content": [], "details": {}},
        observed_digest="sha256:a",
    )
    with pytest.raises(ContractError, match="observation belongs to model action"):
        advance(base, other_action)

    old_attempt = ToolCallObserved(
        event_id="e2",
        at_ms=2,
        model_action_id="request:1",
        call_id="c1",
        tool_name="t",
        outcome="settled",
        result={"role": "toolResult", "tool_call_id": "c1", "content": [], "details": {}},
        observed_digest="sha256:b",
        source_attempt="a0",
    )
    with pytest.raises(ContractError, match="stale callback"):
        advance(base, old_attempt)

    unknown_call = ToolCallObserved(
        event_id="e3",
        at_ms=3,
        model_action_id="request:1",
        call_id="nope",
        tool_name="t",
        outcome="settled",
        result={"role": "toolResult", "tool_call_id": "nope", "content": [], "details": {}},
        observed_digest="sha256:c",
    )
    with pytest.raises(ContractError, match="not part of the declared batch"):
        advance(base, unknown_call)


def test_t2_a_settled_observation_must_carry_its_result() -> None:
    base = _declared_state(ToolCall(id="c1", name="t", arguments={}))
    with pytest.raises(ContractError, match="must carry its result"):
        advance(
            base,
            ToolCallObserved(
                event_id="e1",
                at_ms=1,
                model_action_id="request:1",
                call_id="c1",
                tool_name="t",
                outcome="settled",
                observed_digest="sha256:a",
            ),
        )


def test_t3_the_declaration_is_committed_before_any_dispatch(tmp_path: Path) -> None:
    """The commit port sees the declared partition before the handler is called."""
    paths = DeploymentPaths.under(tmp_path / "state")
    order: list[str] = []

    deployment = TwoCallDeployment(paths, stream=ScriptedStream(two_call_turn(), final_turn()))

    original = deployment.harness._persistence.commit_transition  # noqa: SLF001

    async def recording(run_id, state, *, expected_version, lease=None, events=()):
        phase = (dict(state).get("execution") or {}).get("phase")
        batch = (dict(state).get("execution") or {}).get("batch")
        if phase == Phase.RUNNING_BATCH.value and batch:
            observed = [call.get("progress") for call in batch]
            order.append(f"commit:{phase}:{','.join(observed)}")
        return await original(
            run_id, state, expected_version=expected_version, lease=lease, events=events
        )

    deployment.harness._persistence.commit_transition = recording  # type: ignore[assignment]  # noqa: SLF001

    async def scenario():
        task = asyncio.ensure_future(
            deployment.harness.start(RunRequest(prompt="do both"), run_context())
        )
        await asyncio.wait_for(deployment.gate_entered.wait(), timeout=10)
        deployment.release.set()
        return await asyncio.wait_for(task, timeout=10)

    assert run(scenario()).status is RunStatus.COMPLETED
    assert order[0] == "commit:running_batch:declared,declared", (
        "the declaration — and nothing else — is committed before the batch action runs"
    )
    assert "commit:running_batch:observed,declared" in order
