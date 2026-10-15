"""S4：持久化边界——模型响应先落盘再派发，恢复按已提交事实判断。

计划 S4 的验收：真实新进程可恢复每个承诺的边界；输入不丢不重复；已执行工具不因恢复重跑；
任一不可证明结果都保留等待核对。故障窗口用**真实子进程**（`tests/fault_windows.py` 的
`batch-dispatch-die`：工具执行了副作用后 `os._exit`），副作用用 append-only 文件验证。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from child_process import child_json, run_child
from core_loop_helpers import RecordingSink, assistant, make_tool, run, stepping, tool_call, user
from yanxu_harness.core.agent_loop import AgentLoopConfig, run_agent_loop_result
from yanxu_harness.core.execution import (
    EXECUTION_STATE_SCHEMA_VERSION,
    ExecutionState,
    Phase,
    pending_work_of,
    state_from_record,
    state_to_record,
)
from yanxu_harness.core.loop_config import validate_max_concurrent_tools
from yanxu_harness.core.outcome import CallOutcome, LoopStop, RunStatus
from yanxu_harness.core.types import (
    AgentContext,
    AssistantMessage,
    Model,
    StopReason,
    TextContent,
    ToolCall,
    ToolDeclaration,
    ToolResultMessage,
)
from yanxu_harness.errors import ContractError, VersionRefused


def context_with(tools=()) -> AgentContext:
    return AgentContext(messages=[], system_prompt="", tools=list(tools))


def settled_state() -> ExecutionState:
    message = assistant(TextContent(text="答"), stop=StopReason.STOP)
    return ExecutionState(
        phase=Phase.FINISHED,
        messages=(user("问"), message),
        new_messages=(message,),
        tools=(ToolDeclaration(name="t", description="t"),),
        turns_used=1,
        model=Model(id="m1", provider="p", api="a"),
        thinking_level="high",
        last_assistant=message,
        status=RunStatus.COMPLETED,
        stop=LoopStop.ASSISTANT_FINAL,
        consumed=(("e1", "digest-1"),),
    )


# --- codec: round trip, and refusals ---------------------------------------------------


def test_a_state_round_trips_through_its_record() -> None:
    state = settled_state()
    rebuilt = state_from_record(state_to_record(state))
    assert rebuilt == state
    assert rebuilt.phase is Phase.FINISHED
    assert rebuilt.model.id == "m1"
    assert rebuilt.consumed == (("e1", "digest-1"),)


def test_a_batch_partition_and_interaction_survive_the_codec() -> None:
    from yanxu_harness.core.execution import BatchCallState

    state = ExecutionState(
        phase=Phase.SUSPENDED,
        batch=(
            BatchCallState(call_id="c1", tool_name="w", outcome=CallOutcome.AWAITING,
                           status="awaiting_approval", approval_binding_key="ns:c1", operation_id="op-1"),
            BatchCallState(call_id="c2", tool_name="w", outcome=CallOutcome.UNSTARTED),
        ),
        interaction={"kind": "approval", "call_id": "c1"},
        status=RunStatus.SUSPENDED,
        stop=LoopStop.SUSPENDED,
    )
    rebuilt = state_from_record(state_to_record(state))
    assert rebuilt == state
    assert rebuilt.batch[0].approval_binding_key == "ns:c1"
    assert rebuilt.batch[1].outcome is CallOutcome.UNSTARTED


def test_a_future_or_missing_schema_version_is_refused() -> None:
    record = dict(state_to_record(settled_state()))
    record.pop("schema_version")
    with pytest.raises(ContractError):
        state_from_record(record)

    record = dict(state_to_record(settled_state()))
    record["schema_version"] = EXECUTION_STATE_SCHEMA_VERSION + 1
    with pytest.raises(VersionRefused):
        state_from_record(record)


def test_an_unknown_phase_or_status_is_refused_not_guessed() -> None:
    record = dict(state_to_record(settled_state()))
    record["phase"] = "teleporting"
    with pytest.raises(ContractError):
        state_from_record(record)

    record = dict(state_to_record(settled_state()))
    record["stop"] = "probably_done"
    with pytest.raises(ContractError):
        state_from_record(record)


def test_a_private_signature_survives_the_codec_because_a_resume_needs_it() -> None:
    """The snapshot is recovery state, not a public event: the signature is kept."""
    from yanxu_harness.core.types import ThinkingContent

    message = AssistantMessage(
        content=[ThinkingContent(thinking="内部推理", thinking_signature="sig-123")],
        stop_reason=StopReason.STOP,
    )
    state = ExecutionState(
        phase=Phase.FINISHED,
        messages=(message,),
        last_assistant=message,
        status=RunStatus.COMPLETED,
        stop=LoopStop.ASSISTANT_FINAL,
    )
    rebuilt = state_from_record(state_to_record(state))
    assert rebuilt.messages[0].content[0].thinking_signature == "sig-123"


# --- pending work: what may be dispatched, and what may not ----------------------------


def test_pending_work_names_the_blocking_call_and_never_the_replayable_one() -> None:
    from yanxu_harness.core.execution import BatchCallState

    state = ExecutionState(
        phase=Phase.SUSPENDED,
        batch=(
            BatchCallState(call_id="c1", tool_name="w", outcome=CallOutcome.AWAITING,
                           approval_binding_key="ns:c1", operation_id="op-1"),
            BatchCallState(call_id="c2", tool_name="w", outcome=CallOutcome.UNCERTAIN, operation_id="op-2"),
            BatchCallState(call_id="c3", tool_name="w", outcome=CallOutcome.UNSTARTED),
            BatchCallState(call_id="c4", tool_name="w", outcome=CallOutcome.SETTLED,
                           result={"role": "toolResult"}),
        ),
        interaction={"kind": "approval", "call_id": "c1"},
        status=RunStatus.SUSPENDED,
        stop=LoopStop.SUSPENDED,
    )
    work = pending_work_of(state)
    assert [item["call_id"] for item in work["blocking"]] == ["c1", "c2"]
    assert [item["call_id"] for item in work["replayable"]] == ["c3"]
    assert work["interaction"] == {"kind": "approval", "call_id": "c1"}
    assert work["batch_outcome_unknown"] is False
    assert work["model_request_outstanding"] is False


def test_pending_work_separates_an_unfinished_model_request_from_a_tool_effect() -> None:
    requesting = ExecutionState(phase=Phase.REQUESTING_MODEL)
    work = pending_work_of(requesting)
    assert work["model_request_outstanding"] is True
    assert work["declared"] == []
    assert work["blocking"] == []

    message = assistant(tool_call("c1", "w"), stop=StopReason.TOOL_USE)
    dispatching = ExecutionState(phase=Phase.RUNNING_BATCH, last_assistant=message)
    work = pending_work_of(dispatching)
    assert work["batch_outcome_unknown"] is True
    assert [item["call_id"] for item in work["declared"]] == ["c1"]
    assert work["model_request_outstanding"] is False


# --- the commit port: model response durable before its calls dispatch -----------------


def test_the_state_is_committed_before_the_tool_batch_is_dispatched() -> None:
    committed: list[ExecutionState] = []
    order: list[str] = []

    async def execute(call_id, _args, _signal=None, _on_update=None):
        order.append(f"tool:{call_id}")
        return {"content": [TextContent(text="ok")], "details": {}}

    def sink(state: ExecutionState) -> None:
        order.append(f"commit:{state.phase.value}")
        committed.append(state)

    tool = make_tool("t", execute)
    stream = stepping(
        assistant(tool_call("c1", "t"), stop=StopReason.TOOL_USE),
        assistant(TextContent(text="收尾")),
    )
    config = AgentLoopConfig(stream_fn=stream, tools=[tool], state_sink=sink)
    run(run_agent_loop_result([user("go")], context_with([tool]), config, None, RecordingSink()))

    # The batch boundary is committed before the tool runs.
    batch_index = order.index("commit:running_batch")
    tool_index = order.index("tool:c1")
    assert batch_index < tool_index, order

    snapshot = next(state for state in committed if state.phase is Phase.RUNNING_BATCH)
    # The model's final response and its declared calls are in the snapshot...
    assert snapshot.last_assistant is not None
    declaration = pending_work_of(snapshot)
    assert [item["call_id"] for item in declaration["declared"]] == ["c1"]
    # ...and no outcome of the batch is claimed yet.
    assert snapshot.batch == ()
    assert snapshot.last_tool_results == ()
    assert declaration["batch_outcome_unknown"] is True


def test_a_failing_commit_port_ends_the_run_instead_of_being_ignored() -> None:
    def sink(_state: ExecutionState) -> None:
        raise RuntimeError("disk full")

    stream = stepping(assistant(TextContent(text="答")))
    config = AgentLoopConfig(stream_fn=stream, state_sink=sink)
    with pytest.raises(RuntimeError, match="disk full"):
        run(run_agent_loop_result([user("go")], context_with(), config, None, RecordingSink()))
    # The failure is not swallowed into a "completed" result. It also aborts before the
    # request: the first commit is the prompt's own boundary, so a host that cannot
    # commit never gets a model call it would then have to record.
    assert len(stream.calls) == 0


def test_a_commit_failure_at_the_batch_boundary_stops_the_dispatch() -> None:
    phases: list[str] = []

    def sink(state: ExecutionState) -> None:
        phases.append(state.phase.value)
        if state.phase is Phase.RUNNING_BATCH:
            raise RuntimeError("disk full at the batch boundary")

    async def execute(_call_id, _args, _signal=None, _on_update=None):
        raise AssertionError("a batch whose boundary could not be committed must not dispatch")

    tool = make_tool("t", execute)
    stream = stepping(
        assistant(tool_call("c1", "t"), stop=StopReason.TOOL_USE),
        assistant(TextContent(text="收尾")),
    )
    config = AgentLoopConfig(stream_fn=stream, tools=[tool], state_sink=sink)
    with pytest.raises(RuntimeError, match="batch boundary"):
        run(run_agent_loop_result([user("go")], context_with([tool]), config, None, RecordingSink()))
    assert phases[-1] == "running_batch"
    assert len(stream.calls) == 1


def test_the_commit_port_sees_the_final_verdict() -> None:
    seen: list[tuple[str, str | None]] = []
    stream = stepping(assistant(TextContent(text="答")))
    config = AgentLoopConfig(
        stream_fn=stream,
        state_sink=lambda state: seen.append(
            (state.phase.value, None if state.status is None else state.status.value)
        ),
    )
    result = run(run_agent_loop_result([user("go")], context_with(), config, None, RecordingSink()))
    assert ("finished", "completed") in seen
    assert result.status is RunStatus.COMPLETED


# --- a real crash inside the batch, and what the next process can read -----------------


def test_a_crash_inside_a_tool_leaves_the_declared_call_committed_and_the_effect_unproven(
    tmp_path: Path,
) -> None:
    """The window the commit port exists for: effect happened, no receipt recorded.

    The child dies *inside* the handler with ``os._exit``, after the commit port has
    fsynced the state that names the call. So the next process reads a state that says
    "this call was declared, its outcome is unknown", and it also finds the external
    trace — which is exactly why the answer must be "check the ledger", not "replay".
    """
    commit_log = tmp_path / "commits.jsonl"
    effect_log = tmp_path / "effects.log"
    unused = tmp_path / "unused.log"

    result = run_child(
        ["batch-dispatch-die", str(commit_log), str(effect_log), str(unused)],
        timeout=60,
    )
    assert result.returncode == 9, (result.returncode, result.stderr[-2000:])
    assert effect_log.exists(), "the child must have produced its external trace before dying"

    lines = [json.loads(line) for line in commit_log.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert lines, "the commit port must have written at least one record before the crash"
    last = lines[-1]
    assert last["phase"] == Phase.RUNNING_BATCH.value, last["phase"]
    assert last["pending_work"]["batch_outcome_unknown"] is True
    assert [item["call_id"] for item in last["pending_work"]["declared"]] == ["c1"]
    assert last["batch"] == [], "no outcome may be claimed for a call that was still running"
    assert last["last_tool_results"] == []

    # A fresh process rebuilds the state from the committed bytes and can still answer
    # "may I dispatch?" without the transcript's help.
    rebuilt = state_from_record(last)
    assert rebuilt.phase is Phase.RUNNING_BATCH
    work = pending_work_of(rebuilt)
    assert work["replayable"] == []
    assert [item["call_id"] for item in work["declared"]] == ["c1"]


def test_the_same_crash_window_reports_a_replayable_batch_only_after_a_settled_commit(
    tmp_path: Path,
) -> None:
    """The counter-argument to the test above: a *recorded* partition is what makes a
    call replayable. Without one, this window is never described as replayable."""
    from yanxu_harness.core.execution import BatchCallState

    state = ExecutionState(
        phase=Phase.SUSPENDED,
        batch=(
            BatchCallState(call_id="c1", tool_name="w", outcome=CallOutcome.UNSTARTED),
            BatchCallState(
                call_id="c2",
                tool_name="w",
                outcome=CallOutcome.SETTLED,
                result={"role": "toolResult", "tool_call_id": "c2", "tool_name": "w",
                        "content": [], "details": {}, "is_error": False, "usage": None,
                        "timestamp": 1},
            ),
        ),
        interaction={"kind": "approval", "call_id": "c3"},
        status=RunStatus.SUSPENDED,
        stop=LoopStop.SUSPENDED,
    )
    work = pending_work_of(state)
    assert [item["call_id"] for item in work["replayable"]] == ["c1"]
    assert work["batch_outcome_unknown"] is False


def test_the_loop_refuses_a_bad_concurrency_limit_before_the_commit_port_runs() -> None:
    seen: list[ExecutionState] = []
    stream = stepping(assistant(TextContent(text="答")))
    config = AgentLoopConfig(
        stream_fn=stream, max_concurrent_tools=0, state_sink=seen.append
    )
    with pytest.raises(ValueError):
        validate_max_concurrent_tools(config.max_concurrent_tools)
    with pytest.raises(ValueError):
        run(run_agent_loop_result([user("go")], context_with(), config, None, RecordingSink()))
    assert seen == []
