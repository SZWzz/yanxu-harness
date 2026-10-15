"""S2：显式状态与纯迁移函数的确定性测试。

计划 S2 的验收：给定记录过的规范化事件能重建同一逻辑状态；无效迁移与重复结果不会重复
创建模型/工具动作；Core 不新增存储或网络依赖；相同状态 + 相同输入得到相同迁移，且 reducer
不读时钟、不建随机 ID、不修改外部列表。

这里不启动真实循环：``Drive`` 只把 ``advance`` 的动作翻译成下一个事件，因此每条断言都直接
对应 ``docs/core-state-machine-migration.zh-CN.md`` 的一行。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import pytest

from core_loop_helpers import assistant, user
from yanxu_harness.core.events import AgentEnd, MessageEnd, MessageStart, TurnEnd, TurnStart
from yanxu_harness.core.execution import (
    CancellationRequested,
    ContinueRequested,
    EvaluateStopHook,
    ExecutionState,
    InputKind,
    InputsPolled,
    ModelCompleted,
    Phase,
    PollSite,
    PrepareTurn,
    PromptSubmitted,
    ReadInputs,
    RequestModel,
    RunToolBatch,
    StopHookEvaluated,
    ToolBatchSettled,
    TurnPrepared,
    WaitForExternal,
    classify_results,
    interaction_for_batch,
)
from yanxu_harness.core.outcome import CallOutcome, LoopStop, RunStatus
from yanxu_harness.core.transitions import advance, result_of, validate_max_turns
from yanxu_harness.core.types import (
    AssistantMessage,
    DeferredHandle,
    StopReason,
    SystemMessage,
    TextContent,
    ToolCall,
    ToolDeclaration,
    ToolResultMessage,
)
from yanxu_harness.errors import ContractError


def declaration(name: str = "t") -> ToolDeclaration:
    return ToolDeclaration(name=name, description=name, parameters={"type": "object"})


def tool_result(
    call_id: str,
    name: str = "t",
    *,
    status: str | None = None,
    text: str = "ok",
    terminate: bool = False,
) -> ToolResultMessage:
    details: dict[str, Any] = {"terminate": terminate}
    if status is not None:
        details["status"] = status
    return ToolResultMessage(
        tool_call_id=call_id,
        tool_name=name,
        content=[TextContent(text=text)],
        details=details,
        # mirrors the gateway: only a settled success is not an error
        is_error=status is not None and status not in {"executed", "replayed"},
    )


def fresh(*, messages: Sequence[Any] = (), tools: Sequence[ToolDeclaration] = ()) -> ExecutionState:
    return ExecutionState(messages=tuple(messages), tools=tuple(tools))


def submit(messages=(), *, tools=(), max_turns=None, at_ms=1, event_id="e-prompt") -> PromptSubmitted:
    return PromptSubmitted(
        event_id=event_id, at_ms=at_ms, messages=tuple(messages), tools=tuple(tools), max_turns=max_turns
    )


# --------------------------------------------------------------------------------------
# A scripted driver: applies advance, records events, answers actions.
# --------------------------------------------------------------------------------------


@dataclass
class Drive:
    answers: list[AssistantMessage]
    batches: list[tuple[tuple[ToolResultMessage, ...], bool]]
    steering: list[tuple[Any, ...]]
    follow_ups: list[tuple[Any, ...]]
    prepares: list[tuple[Any, ...]]
    stops: list[bool]
    suspends: list[Mapping[str, Any] | None]
    clock: int = 10

    def __init__(
        self,
        *,
        answers: Sequence[AssistantMessage] = (),
        batches: Sequence[tuple[Sequence[ToolResultMessage], bool]] = (),
        steering: Sequence[Sequence[Any]] = (),
        follow_ups: Sequence[Sequence[Any]] = (),
        prepares: Sequence[Sequence[Any]] = (),
        stops: Sequence[bool] = (False,),
        suspends: Sequence[Mapping[str, Any] | None] = (None,),
    ) -> None:
        self.answers = list(answers)
        self.batches = [(tuple(r), t) for r, t in batches]
        self.steering = [tuple(s) for s in steering]
        self.follow_ups = [tuple(f) for f in follow_ups]
        self.prepares = [tuple(p) for p in prepares]
        self.stops = list(stops)
        self.suspends = list(suspends)
        self.clock = 10
        self.emitted: list[Any] = []
        self.actions: list[Any] = []
        self.event_log: list[Any] = []
        self.state: ExecutionState | None = None
        self._n = 0

    def _event(self, cls: type, **fields: Any) -> Any:
        self._n += 1
        self.clock += 1
        return cls(event_id=f"e{self._n}", at_ms=self.clock, **fields)

    def run(self, state: ExecutionState, first: Any, limit: int = 40) -> ExecutionState:
        self.state = state
        event = first
        for _ in range(limit):
            transition = advance(self.state, event)
            self.event_log.append(event)
            self.emitted.extend(transition.events)
            if transition.ignored_reason is not None:
                raise AssertionError(f"unexpected ignored event: {transition.ignored_reason}")
            self.state = transition.state
            if self.state.phase in (Phase.SUSPENDED, Phase.FINISHED):
                return self.state
            if not transition.actions:
                return self.state
            assert len(transition.actions) == 1, transition.actions
            action = transition.actions[0]
            self.actions.append(action)
            event = self.answer(action)
        raise AssertionError("the scripted driver did not converge")

    def answer(self, action: Any) -> Any:
        if isinstance(action, ReadInputs):
            if action.site is PollSite.AFTER_INNER:
                queue, source = self.follow_ups, InputKind.FOLLOW_UP
            else:
                queue, source = self.steering, InputKind.STEERING
            messages = queue.pop(0) if queue else ()
            return self._event(InputsPolled, site=action.site, kind=source, messages=messages)
        if isinstance(action, PrepareTurn):
            messages = self.prepares.pop(0) if self.prepares else ()
            return self._event(TurnPrepared, messages=messages)
        if isinstance(action, EvaluateStopHook):
            stop = self.stops.pop(0) if self.stops else False
            return self._event(StopHookEvaluated, stop=stop)
        if isinstance(action, RequestModel):
            message = self.answers.pop(0) if self.answers else assistant(TextContent(text="收尾"))
            return self._event(ModelCompleted, message=message)
        if isinstance(action, RunToolBatch):
            results, terminate = self.batches.pop(0) if self.batches else ((), False)
            suspend = self.suspends.pop(0) if self.suspends else None
            return self._event(
                ToolBatchSettled, results=tuple(results), terminate=terminate, suspend=suspend
            )
        if isinstance(action, WaitForExternal):
            raise AssertionError("the driver must stop on a pause before performing this")
        raise AssertionError(f"the driver has no answer for {action!r}")


def replay(initial: ExecutionState, events: Sequence[Any]) -> tuple[ExecutionState, list[Any]]:
    state = initial
    actions: list[Any] = []
    for event in events:
        transition = advance(state, event)
        state = transition.state
        actions.extend(transition.actions)
    return state, actions


# --------------------------------------------------------------------------------------
# Every exit path has exactly one result
# --------------------------------------------------------------------------------------


def test_a_plain_answer_completes_the_execution() -> None:
    driver = Drive(answers=[assistant(TextContent(text="好了"))])
    state = driver.run(fresh(), submit([user("你好")]))

    assert state.phase is Phase.FINISHED
    result = result_of(state)
    assert result.status is RunStatus.COMPLETED
    assert result.stop is LoopStop.ASSISTANT_FINAL
    assert result.turns_used == 1
    assert result.final_text == "好了"
    assert result.terminal is True
    kinds = [type(action).__name__ for action in driver.actions]
    # a plain answer still passes the stop hook and both queue reads before it ends
    assert kinds == ["ReadInputs", "RequestModel", "EvaluateStopHook", "ReadInputs", "ReadInputs"]
    assert driver.emitted[0].type == "agent_start"
    assert driver.emitted[-1].type == "agent_end"


def test_the_turn_budget_is_usage_limited_and_keeps_the_authorised_batch() -> None:
    call = ToolCall(id="c1", name="t")
    driver = Drive(
        answers=[assistant(call, stop=StopReason.TOOL_USE)],
        batches=[([tool_result("c1")], False)],
        stops=[False],
    )
    state = driver.run(fresh(tools=[declaration()]), submit([user("go")], max_turns=1))

    result = result_of(state)
    assert result.status is RunStatus.USAGE_LIMITED
    assert result.stop is LoopStop.MAX_TURNS
    assert result.error_code == "max_turns"
    assert result.turns_used == 1
    # the authorised batch was executed and settled, and no further request was made
    assert [type(a).__name__ for a in driver.actions] == [
        "ReadInputs",
        "RequestModel",
        "RunToolBatch",
        "EvaluateStopHook",
    ]
    assert sum(isinstance(m, ToolResultMessage) for m in state.new_messages) == 1


def test_a_model_error_and_an_abort_are_different_facts() -> None:
    for stop, status, loop_stop in (
        (StopReason.ERROR, RunStatus.FAILED, LoopStop.MODEL_ERROR),
        (StopReason.ABORTED, RunStatus.CANCELLED, LoopStop.ABORTED),
    ):
        message = assistant(TextContent(text=""), stop=stop)
        driver = Drive(answers=[message])
        state = driver.run(fresh(), submit([user("go")]))
        result = result_of(state)
        assert result.status is status
        assert result.stop is loop_stop
        assert result.turns_used == 1
        assert driver.emitted[-1].type == "agent_end"
        # TurnEnd carries no tool results on this path.
        turn_end = [e for e in driver.emitted if isinstance(e, TurnEnd)]
        assert turn_end and turn_end[-1].tool_results == ()


def test_a_stop_hook_completes_the_run_with_its_own_stop_code() -> None:
    driver = Drive(answers=[assistant(TextContent(text="答"))], stops=[True])
    state = driver.run(fresh(), submit([user("go")]))
    result = result_of(state)
    assert result.status is RunStatus.COMPLETED
    assert result.stop is LoopStop.STOP_HOOK
    stop_action = next(a for a in driver.actions if isinstance(a, EvaluateStopHook))
    assert stop_action.turn == 1
    assert stop_action.message is not None


def test_a_terminating_batch_completes_with_batch_terminate() -> None:
    call = ToolCall(id="c1", name="finish")
    driver = Drive(
        answers=[assistant(call, stop=StopReason.TOOL_USE)],
        batches=[([tool_result("c1", "finish", terminate=True)], True)],
    )
    state = driver.run(fresh(tools=[declaration("finish")]), submit([user("go")]))
    result = result_of(state)
    assert result.status is RunStatus.COMPLETED
    assert result.stop is LoopStop.BATCH_TERMINATE
    assert result.turns_used == 1
    assert [type(a).__name__ for a in driver.actions] == [
        "ReadInputs",
        "RequestModel",
        "RunToolBatch",
        "EvaluateStopHook",
        "ReadInputs",
        "ReadInputs",
    ]


def test_a_waiting_call_suspends_before_the_next_request() -> None:
    call = ToolCall(id="c1", name="write_note")
    waiting = tool_result("c1", "write_note", status="awaiting_approval")
    driver = Drive(
        answers=[assistant(call, stop=StopReason.TOOL_USE)],
        batches=[([waiting], False)],
    )
    state = driver.run(fresh(tools=[declaration("write_note")]), submit([user("go")]))

    assert state.phase is Phase.SUSPENDED
    result = result_of(state)
    assert result.status is RunStatus.SUSPENDED
    assert result.stop is LoopStop.SUSPENDED
    assert result.terminal is False
    assert result.pending_interaction["kind"] == "approval"
    assert result.pending_interaction["call_id"] == "c1"
    # No stop hook and no second request: a pause is not a turn boundary.
    assert [type(a).__name__ for a in driver.actions] == ["ReadInputs", "RequestModel", "RunToolBatch"]
    assert driver.emitted[-1].type == "agent_end"


def test_an_unproven_call_suspends_for_reconciliation_not_as_success() -> None:
    call = ToolCall(id="c1", name="write_note")
    unknown = tool_result("c1", "write_note", status="unknown")
    driver = Drive(
        answers=[assistant(call, stop=StopReason.TOOL_USE)],
        batches=[([unknown], False)],
    )
    state = driver.run(fresh(tools=[declaration("write_note")]), submit([user("go")]))
    result = result_of(state)
    assert result.status is RunStatus.SUSPENDED
    assert result.pending_interaction["kind"] == "reconciliation"
    call_state = state.batch[0]
    assert call_state.outcome is CallOutcome.UNCERTAIN
    assert call_state.result is None, "an unproven effect must not carry a success result"
    assert state.new_messages[-1].is_error is True


def test_a_host_suspension_wins_over_the_batch_classification() -> None:
    call = ToolCall(id="c1", name="t")
    told = {"kind": "approval", "call_id": "c1", "reason": "host policy"}
    driver = Drive(
        answers=[assistant(call, stop=StopReason.TOOL_USE)],
        batches=[([tool_result("c1", status="executed")], False)],
        suspends=[told],
    )
    state = driver.run(fresh(tools=[declaration()]), submit([user("go")]))
    assert result_of(state).pending_interaction == told


def test_a_deferred_model_turn_is_a_pause() -> None:
    message = AssistantMessage(
        content=[TextContent(text="稍等")],
        stop_reason=StopReason.DEFERRED,
        deferred=DeferredHandle(id="h1", payload={"why": "human"}),
    )
    driver = Drive(answers=[message])
    state = driver.run(fresh(), submit([user("go")]))
    result = result_of(state)
    assert result.status is RunStatus.SUSPENDED
    assert result.pending_interaction == {"kind": "deferred", "handle_id": "h1", "payload": {"why": "human"}}
    assert [type(a).__name__ for a in driver.actions] == ["ReadInputs", "RequestModel"]


def test_cancellation_between_turns_is_reported_as_cancelled() -> None:
    state = fresh()
    transition = advance(state, submit([user("go")]))
    state = transition.state
    state = advance(state, InputsPolled(event_id="p1", at_ms=2, site=PollSite.INITIAL, kind=InputKind.STEERING)).state
    state = advance(state, CancellationRequested(event_id="c1", at_ms=3, reason="user")).state
    assert state.cancel_requested is True and state.phase is Phase.REQUESTING_MODEL

    state = advance(state, ModelCompleted(event_id="m1", at_ms=4, message=assistant(TextContent(text="答")))).state
    # a plain answer still needs the stop hook before the natural end
    state = advance(state, StopHookEvaluated(event_id="s1", at_ms=5, stop=False)).state
    state = advance(state, InputsPolled(event_id="p2", at_ms=6, site=PollSite.TURN_BOUNDARY, kind=InputKind.STEERING)).state
    state = advance(state, InputsPolled(event_id="p3", at_ms=7, site=PollSite.AFTER_INNER, kind=InputKind.FOLLOW_UP)).state

    result = result_of(state)
    assert result.status is RunStatus.CANCELLED
    assert result.stop is LoopStop.ABORTED
    assert result.error_code == "cancelled"


# --------------------------------------------------------------------------------------
# Determinism, purity, immutability
# --------------------------------------------------------------------------------------


def test_the_same_state_and_event_give_the_same_transition() -> None:
    event = submit([user("你好")], tools=[declaration()], at_ms=7)
    left = advance(fresh(), event)
    right = advance(fresh(), event)
    assert left.state == right.state
    assert [type(a) for a in left.actions] == [type(a) for a in right.actions]
    assert [getattr(e, "type", None) for e in left.events] == [getattr(e, "type", None) for e in right.events]


def test_the_reducer_never_reads_a_clock() -> None:
    early = advance(fresh(), submit([user("hi")], tools=[declaration()], at_ms=10))
    late = advance(fresh(), submit([user("hi")], tools=[declaration()], at_ms=999_999))
    assert early.state.phase is late.state.phase
    assert [type(a) for a in early.actions] == [type(a) for a in late.actions]
    # the only place the clock is visible: the synthesized tool-declaration timestamp
    def stamps(transition):
        return sorted(
            {
                e.message.timestamp
                for e in transition.events
                if isinstance(getattr(e, "message", None), SystemMessage)
            }
        )

    assert stamps(early) == [10]
    assert stamps(late) == [999_999]


def test_the_state_copies_sequences_it_is_given() -> None:
    messages = [user("a")]
    state = fresh(messages=messages)
    messages.append(user("b"))
    assert [m.content for m in state.messages] == ["a"]

    transition = advance(state, submit([user("c")], at_ms=3))
    assert isinstance(transition.state.messages, tuple)
    assert [m.content for m in transition.state.messages] == ["a", "c"]
    # the previous state is untouched
    assert [m.content for m in state.messages] == ["a"]


def test_a_list_instead_of_a_tuple_is_refused() -> None:
    event = PromptSubmitted(event_id="e1", at_ms=1, messages=[user("hi")])  # type: ignore[arg-type]
    with pytest.raises(ContractError):
        advance(fresh(), event)


# --------------------------------------------------------------------------------------
# Duplicates, conflicts, unknown and late events
# --------------------------------------------------------------------------------------


def test_a_repeated_event_is_ignored_with_a_reason_and_creates_no_second_action() -> None:
    event = submit([user("hi")])
    first = advance(fresh(), event)
    second = advance(first.state, event)
    assert second.ignored is True
    assert "already applied" in (second.ignored_reason or "")
    assert second.state == first.state
    assert second.actions == () and second.events == ()


def test_the_same_event_id_with_different_content_is_refused() -> None:
    first = advance(fresh(), submit([user("hi")], event_id="same"))
    with pytest.raises(ContractError) as excinfo:
        advance(first.state, submit([user("different")], event_id="same"))
    assert "different content" in str(excinfo.value)


def test_an_unknown_event_type_is_refused() -> None:
    class Sneaky:
        event_id = "x"
        at_ms = 1

    with pytest.raises(ContractError) as excinfo:
        advance(fresh(), Sneaky())  # type: ignore[arg-type]
    assert "not an InputEvent" in str(excinfo.value)


def test_an_event_without_an_id_or_clock_is_refused() -> None:
    with pytest.raises(ContractError):
        advance(fresh(), PromptSubmitted(event_id="", at_ms=1))
    with pytest.raises(ContractError):
        advance(fresh(), PromptSubmitted(event_id="e", at_ms=-1))


def test_an_event_in_the_wrong_phase_is_refused() -> None:
    with pytest.raises(ContractError) as excinfo:
        advance(fresh(), ModelCompleted(event_id="m", at_ms=1, message=assistant(TextContent(text="x"))))
    assert "cannot be applied in phase created" in str(excinfo.value)


def test_a_late_event_after_the_end_is_ignored_not_applied() -> None:
    driver = Drive(answers=[assistant(TextContent(text="答"))])
    state = driver.run(fresh(), submit([user("go")]))
    late = advance(state, ModelCompleted(event_id="late", at_ms=99, message=assistant(TextContent(text="stale"))))
    assert late.ignored is True
    assert "already finished" in (late.ignored_reason or "")
    assert late.state == state


def test_cancellation_while_suspended_is_recorded_without_changing_the_phase() -> None:
    call = ToolCall(id="c1", name="t")
    driver = Drive(
        answers=[assistant(call, stop=StopReason.TOOL_USE)],
        batches=[([tool_result("c1", status="awaiting_approval")], False)],
    )
    state = driver.run(fresh(tools=[declaration()]), submit([user("go")]))
    assert state.phase is Phase.SUSPENDED
    transition = advance(state, CancellationRequested(event_id="c", at_ms=50, reason="operator"))
    assert transition.state.phase is Phase.SUSPENDED
    assert transition.state.cancel_requested is True
    assert transition.state.cancel_reason == "operator"


# --------------------------------------------------------------------------------------
# Rebuild from recorded events
# --------------------------------------------------------------------------------------


def _multi_turn_script() -> Drive:
    return Drive(
        answers=[
            assistant(ToolCall(id="c1", name="t"), stop=StopReason.TOOL_USE),
            assistant(TextContent(text="第二轮答")),
        ],
        batches=[([tool_result("c1")], False)],
        steering=[(user("插话"),)],
        prepares=[()],
        stops=[False],
    )


def test_a_recorded_event_stream_rebuilds_the_same_state_and_actions() -> None:
    driver = _multi_turn_script()
    final = driver.run(fresh(tools=[declaration()]), submit([user("go")]))
    rebuilt, actions = replay(fresh(tools=[declaration()]), driver.event_log)

    assert rebuilt == final
    assert [type(action).__name__ for action in actions] == [
        type(action).__name__ for action in driver.actions
    ]
    assert final.turns_used == 2
    assert result_of(final).status is RunStatus.COMPLETED
    # the steering message entered the second request exactly once
    assert [m.content for m in final.messages if getattr(m, "role", None) == "user"] == ["go", "插话"]


def test_replay_never_creates_a_second_request_or_batch() -> None:
    driver = _multi_turn_script()
    driver.run(fresh(tools=[declaration()]), submit([user("go")]))
    _state, actions = replay(fresh(tools=[declaration()]), driver.event_log)
    assert sum(isinstance(a, RequestModel) for a in actions) == 2
    assert sum(isinstance(a, RunToolBatch) for a in actions) == 1


def test_replaying_the_same_event_twice_changes_nothing() -> None:
    driver = _multi_turn_script()
    state = driver.run(fresh(tools=[declaration()]), submit([user("go")]))
    events = list(driver.event_log)
    once, actions_once = replay(fresh(tools=[declaration()]), events)
    twice, actions_twice = replay(fresh(tools=[declaration()]), events + events)
    assert twice == once
    assert actions_twice == actions_once


# --------------------------------------------------------------------------------------
# Batch classification and interaction derivation
# --------------------------------------------------------------------------------------


def test_every_gateway_status_is_classifiable_and_only_three_are_pending() -> None:
    from yanxu_harness.tools.gateway import InvocationStatus

    mapping = {
        status.value: classify_results(
            [ToolCall(id="c", name="t")], [tool_result("c", status=status.value)]
        )[0].outcome
        for status in InvocationStatus
    }
    assert mapping == {
        "executed": CallOutcome.SETTLED,
        "replayed": CallOutcome.SETTLED,
        "denied": CallOutcome.SETTLED,
        "invalid_args": CallOutcome.SETTLED,
        "failed": CallOutcome.SETTLED,
        "cancelled": CallOutcome.SETTLED,
        "awaiting_approval": CallOutcome.AWAITING,
        "deferred": CallOutcome.AWAITING,
        "unknown": CallOutcome.UNCERTAIN,
    }


def test_an_unknown_status_is_refused_rather_than_treated_as_settled() -> None:
    with pytest.raises(ContractError):
        classify_results([ToolCall(id="c", name="t")], [tool_result("c", status="probably_fine")])


def test_a_result_without_a_status_is_a_settled_result() -> None:
    batch = classify_results([ToolCall(id="c", name="t")], [tool_result("c")])
    assert batch[0].outcome is CallOutcome.SETTLED
    assert batch[0].result is not None


def test_a_call_the_batch_never_reached_is_unstarted_and_replayable() -> None:
    calls = [ToolCall(id="c1", name="t"), ToolCall(id="c2", name="t")]
    batch = classify_results(calls, [tool_result("c1", status="executed")])
    assert [call.outcome for call in batch] == [CallOutcome.SETTLED, CallOutcome.UNSTARTED]
    assert batch[1].result is None
    assert batch[1].outcome.replayable is True
    assert interaction_for_batch(batch) is None


def test_approval_wins_over_reconciliation_as_the_primary_interaction() -> None:
    calls = [ToolCall(id="c1", name="t"), ToolCall(id="c2", name="t")]
    results = [
        tool_result("c1", status="unknown"),
        tool_result("c2", status="awaiting_approval"),
    ]
    batch = classify_results(calls, results)
    batch = tuple(
        call if call.call_id != "c2" else type(call)(**{**call.__dict__, "approval_binding_key": "ns:c2"})
        for call in batch
    )
    interaction = interaction_for_batch(batch)
    assert interaction is not None and interaction["kind"] == "approval"
    assert interaction["call_id"] == "c2"


# --------------------------------------------------------------------------------------
# Result projection and budget validation
# --------------------------------------------------------------------------------------


def test_result_of_refuses_a_state_that_is_still_running() -> None:
    with pytest.raises(ContractError):
        result_of(fresh())


def test_max_turns_is_validated_before_anything_happens() -> None:
    for invalid in (0, -1, True, 1.5, "1"):
        with pytest.raises(ValueError):
            validate_max_turns(invalid)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            advance(fresh(), submit([user("go")], max_turns=invalid))  # type: ignore[arg-type]
    validate_max_turns(None)
    validate_max_turns(3)


def test_continue_refuses_an_empty_or_assistant_tail() -> None:
    with pytest.raises(ValueError):
        advance(fresh(), ContinueRequested(event_id="e", at_ms=1))
    with pytest.raises(ValueError):
        advance(fresh(messages=[user("a"), assistant(TextContent(text="b"))]), ContinueRequested(event_id="e", at_ms=1))


def test_a_continue_from_a_user_tail_issues_the_first_request_without_prepare() -> None:
    driver = Drive(answers=[assistant(TextContent(text="答"))])
    state = driver.run(fresh(messages=[user("a")]), ContinueRequested(event_id="e", at_ms=1))
    assert [type(a).__name__ for a in driver.actions] == [
        "ReadInputs",
        "RequestModel",
        "EvaluateStopHook",
        "ReadInputs",
        "ReadInputs",
    ]
    assert not any(isinstance(a, PrepareTurn) for a in driver.actions), "the first request needs no prepare hook"
    assert result_of(state).stop is LoopStop.ASSISTANT_FINAL
    assert sum(1 for e in driver.emitted if isinstance(e, TurnStart)) == 1


def test_tool_set_changes_are_declared_once_per_request_not_once_per_turn() -> None:
    driver = _multi_turn_script()
    state = driver.run(fresh(tools=[declaration()]), submit([user("go")], tools=[declaration()]))
    system_messages = [m for m in state.messages if getattr(m, "role", None) == "system"]
    assert len(system_messages) == 1, "the tool declaration must not be repeated every turn"
    starts = [e for e in driver.emitted if isinstance(e, MessageStart)]
    assert [m for m in starts if getattr(m.message, "role", None) == "system"] == starts[:1]
