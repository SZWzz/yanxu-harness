"""The structured result of one loop invocation: every exit has one, and it is exact.

The compatibility list APIs (`run_agent_loop`, `run_agent_loop_continue`) must keep
working; the `*_result` entries return the same messages plus a machine-readable
answer to "why did it stop". These tests pin every exit path, because a caller that
has to infer the reason from the last message's role, the event count or the answer
text is how "unfinished" quietly becomes "completed".
"""

from __future__ import annotations

import pytest

from core_loop_helpers import (
    RecordingSink,
    assistant,
    make_tool,
    ok_result,
    run,
    stepping,
    tool_call,
    user,
)
from yanxu_harness.core.agent_loop import (
    AgentLoopConfig,
    run_agent_loop,
    run_agent_loop_continue,
    run_agent_loop_continue_result,
    run_agent_loop_result,
)
from yanxu_harness.core.outcome import (
    MAX_TURNS_STOP_CODE,
    LoopResult,
    LoopStop,
    RunStatus,
    assert_transition,
    can_transition,
)
from yanxu_harness.core.types import (
    AgentContext,
    AssistantMessage,
    StopReason,
    TextContent,
    Usage,
)
from yanxu_harness.errors import ContractError


def context_with(tools=()) -> AgentContext:
    return AgentContext(messages=[], system_prompt="", tools=list(tools))


def answer(text: str = "done") -> AssistantMessage:
    return assistant(TextContent(text=text), stop=StopReason.STOP)


def test_a_plain_answer_is_a_completed_run_with_its_text_and_usage() -> None:
    stream = stepping(
        AssistantMessage(
            content=[TextContent(text="海边有信号塔。")], stop_reason=StopReason.STOP,
            usage=Usage(input=11, output=4, total_tokens=15),
        )
    )
    result = run(
        run_agent_loop_result([user("hi")], context_with(), AgentLoopConfig(stream_fn=stream), None, RecordingSink())
    )
    assert result.status is RunStatus.COMPLETED
    assert result.stop is LoopStop.ASSISTANT_FINAL
    assert result.terminal is True
    assert result.final_text == "海边有信号塔。"
    assert result.turns_used == 1
    assert result.stop_reason == "stop"
    assert result.usage["input"] == 11 and result.usage["output"] == 4
    assert result.error_code is None and result.pending_interaction is None


def test_hitting_the_turn_budget_is_usage_limited_and_not_completed() -> None:
    """A budget stop is reported as such, with the turns that were actually used."""
    stream = stepping(
        assistant(tool_call("c1", "probe"), stop=StopReason.TOOL_USE),
        assistant(tool_call("c2", "probe"), stop=StopReason.TOOL_USE),
        answer(),
    )
    tool = make_tool("probe", ok_result)
    result = run(
        run_agent_loop_result(
            [user("go")], context_with([tool]), AgentLoopConfig(stream_fn=stream, max_turns=2), None, RecordingSink()
        )
    )
    assert result.status is RunStatus.USAGE_LIMITED
    assert result.stop is LoopStop.MAX_TURNS
    assert result.error_code == MAX_TURNS_STOP_CODE
    assert result.turns_used == 2
    assert result.terminal is True
    assert len(stream.calls) == 2, "the budget must stop the run before a third request"


def test_a_model_failure_keeps_its_provider_kind_as_the_error_code() -> None:
    failure = AssistantMessage(
        content=[],
        stop_reason=StopReason.ERROR,
        error_message="invalid_output: provider stream ended without a terminal frame",
        raw_stop_reason="invalid_output",
    )
    result = run(
        run_agent_loop_result(
            [user("hi")], context_with(), AgentLoopConfig(stream_fn=stepping(failure)), None, RecordingSink()
        )
    )
    assert result.status is RunStatus.FAILED
    assert result.stop is LoopStop.MODEL_ERROR
    assert result.error_code == "invalid_output"
    assert "terminal frame" in result.detail
    assert result.final_text == "", "a failed turn must not carry an invented answer"


def test_an_aborted_turn_is_cancelled_rather_than_failed() -> None:
    aborted = AssistantMessage(content=[], stop_reason=StopReason.ABORTED, error_message="cancelled")
    result = run(
        run_agent_loop_result(
            [user("hi")], context_with(), AgentLoopConfig(stream_fn=stepping(aborted)), None, RecordingSink()
        )
    )
    assert result.status is RunStatus.CANCELLED
    assert result.stop is LoopStop.ABORTED
    assert result.terminal is True


def test_cancellation_between_turns_is_reported_as_cancelled() -> None:
    """The turn happened to finish, but the caller asked for a stop."""
    sink = RecordingSink()

    class CancellingStream:
        calls = 0

        async def __call__(self, model, context, options=None):
            CancellingStream.calls += 1
            return self

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def result(self):
            return answer("finished before the cancel arrived")

    token = _cancelled_token()

    result = run(
        run_agent_loop_result([user("hi")], context_with(), AgentLoopConfig(stream_fn=CancellingStream()), token, sink)
    )
    assert result.status is RunStatus.CANCELLED
    assert result.stop is LoopStop.ABORTED
    assert result.error_code == "cancelled"
    # The message that did arrive is still reported: cancellation is not amnesia.
    assert result.final_text == "finished before the cancel arrived"


def _cancelled_token():
    from yanxu_harness.core.cancellation import CancellationToken

    token = CancellationToken("run-1")
    token.cancel("author stopped")
    return token


def test_a_stop_hook_is_completed_but_has_its_own_stop_code() -> None:
    stream = stepping(
        assistant(tool_call("c1", "probe"), stop=StopReason.TOOL_USE),
        answer("should never be requested"),
    )
    tool = make_tool("probe", ok_result)
    config = AgentLoopConfig(
        stream_fn=stream,
        tools=[tool],
        should_stop_after_turn=lambda _turn: True,
    )
    result = run(run_agent_loop_result([user("go")], context_with([tool]), config, None, RecordingSink()))
    assert result.status is RunStatus.COMPLETED
    assert result.stop is LoopStop.STOP_HOOK
    assert result.turns_used == 1
    assert len(stream.calls) == 1, "the stop hook must prevent the next request"


def test_a_terminating_batch_is_completed_with_its_own_stop_code() -> None:
    stream = stepping(assistant(tool_call("c1", "finish"), stop=StopReason.TOOL_USE), answer("unused"))
    async def finish(*_args, **_kwargs):
        return {"content": [], "details": {}, "terminate": True}

    tool = make_tool("finish", finish)
    result = run(
        run_agent_loop_result([user("go")], context_with([tool]), AgentLoopConfig(stream_fn=stream, tools=[tool]), None, RecordingSink())
    )
    assert result.status is RunStatus.COMPLETED
    assert result.stop is LoopStop.BATCH_TERMINATE
    assert len(stream.calls) == 1


def test_a_suspend_hook_stops_before_the_next_model_request() -> None:
    """Waiting is not a tool result handed to the model: it ends the invocation."""
    stream = stepping(assistant(tool_call("c1", "write_note"), stop=StopReason.TOOL_USE), answer("unused"))
    tool = make_tool("write_note", ok_result)
    interaction = {"kind": "approval", "call_id": "c1", "approval_key": "project-a:c1"}

    def suspend(_message, _results):
        return interaction

    result = run(
        run_agent_loop_result(
            [user("write it")],
            context_with([tool]),
            AgentLoopConfig(stream_fn=stream, tools=[tool], should_suspend_after_batch=suspend),
            None,
            RecordingSink(),
        )
    )
    assert result.status is RunStatus.SUSPENDED
    assert result.stop is LoopStop.SUSPENDED
    assert result.pending_interaction == interaction
    assert result.terminal is False, "a suspended run has work left; it is not finished"
    assert len(stream.calls) == 1, "an unanswered interaction must not produce another request"


def test_the_legacy_list_entry_points_return_the_same_messages() -> None:
    def scenario(entry):
        stream = stepping(
            assistant(tool_call("c1", "probe"), stop=StopReason.TOOL_USE),
            answer("第二次"),
        )
        tool = make_tool("probe", ok_result)
        sink = RecordingSink()
        messages = run(entry([user("go")], context_with([tool]), AgentLoopConfig(stream_fn=stream, tools=[tool]), None, sink))
        return messages, sink.types

    structured_messages, structured_events = scenario(_structured_entry)
    legacy_messages, legacy_events = scenario(run_agent_loop)
    assert _without_timestamps(legacy_messages) == _without_timestamps(structured_messages)
    assert legacy_events == structured_events


def _without_timestamps(messages):
    """Compare messages without their wall-clock timestamp (each run stamps ``now``)."""
    from yanxu_harness.core.types import message_to_json

    return [
        {key: value for key, value in message_to_json(message).items() if key != "timestamp"}
        for message in messages
    ]


def _structured_entry(prompts, context, config, signal, emit):
    async def call():
        result = await run_agent_loop_result(prompts, context, config, signal, emit)
        return result.list_messages()

    return call()


def test_the_continue_entry_has_a_structured_form_too() -> None:
    context = AgentContext(messages=[user("first")], system_prompt="", tools=[])
    sink = RecordingSink()
    result = run(
        run_agent_loop_continue_result(context, AgentLoopConfig(stream_fn=stepping(answer())), None, sink)
    )
    assert result.status is RunStatus.COMPLETED
    assert result.stop is LoopStop.ASSISTANT_FINAL
    assert len(result.messages) == 1

    legacy = run(
        run_agent_loop_continue(
            AgentContext(messages=[user("first")], system_prompt="", tools=[]),
            AgentLoopConfig(stream_fn=stepping(answer())),
            None,
            RecordingSink(),
        )
    )
    # Each run stamps its own wall-clock timestamp, so compare the message content.
    assert _without_timestamps(legacy) == _without_timestamps(result.list_messages())


def test_the_continue_guards_are_unchanged() -> None:
    with pytest.raises(ValueError):
        run(run_agent_loop_continue_result(AgentContext(messages=[]), AgentLoopConfig(stream_fn=stepping(answer())), None, RecordingSink()))
    with pytest.raises(ValueError):
        run(
            run_agent_loop_continue_result(
                AgentContext(messages=[assistant(TextContent(text="x"))]),
                AgentLoopConfig(stream_fn=stepping(answer())),
                None,
                RecordingSink(),
            )
        )


def test_a_loop_result_cannot_claim_a_phase_as_an_outcome() -> None:
    with pytest.raises(ContractError):
        LoopResult(status=RunStatus.RUNNING, stop=LoopStop.ASSISTANT_FINAL)
    with pytest.raises(ContractError):
        LoopResult(status=RunStatus.SUSPENDED, stop=LoopStop.SUSPENDED)
    with pytest.raises(ContractError):
        LoopResult(status=RunStatus.USAGE_LIMITED, stop=LoopStop.MAX_TURNS)
    with pytest.raises(ContractError):
        LoopResult(status=RunStatus.COMPLETED, stop="not-a-stop")  # type: ignore[arg-type]
    assert LoopResult(status=RunStatus.COMPLETED, stop=LoopStop.ASSISTANT_FINAL).terminal


def test_the_transition_table_forbids_reviving_a_finished_run() -> None:
    assert can_transition(RunStatus.RUNNING, RunStatus.SUSPENDED)
    assert can_transition(RunStatus.SUSPENDED, RunStatus.RUNNING)
    assert can_transition(RunStatus.SUSPENDED, RunStatus.SUSPENDED), "a run may pause again"
    assert can_transition(RunStatus.RUNNING, RunStatus.RUNNING)
    for target in (
        RunStatus.RUNNING,
        RunStatus.COMPLETED,
        RunStatus.SUSPENDED,
        RunStatus.FAILED,
    ):
        assert not can_transition(RunStatus.COMPLETED, target)
    with pytest.raises(ContractError):
        assert_transition(RunStatus.CANCELLED, RunStatus.RUNNING)
