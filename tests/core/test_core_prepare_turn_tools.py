"""`prepare_next_turn` may replace the context — including its tool set.

The legacy loop computed each request against the *replaced* context, and
`execute_tool_calls` resolved tools from that same context. That means a hook could
revoke a tool (a call to it must fail as "not found" and must not run) or grant one
(a call to it must run). The explicit-state refactor has to keep both directions: the
reducer records the *declarations* the model sees, and the driver keeps the executable
specs that must match them.

These tests exist because the refactor initially kept only the declarations and left
execution on the launch-time tool list, which silently inverted both cases (a revoked
tool still ran; a granted one could not). A first turn that calls a tool is what makes
the hook run at all: with no tool batch and no queued input there is no next turn to
prepare, in this loop and in the one it replaced.
"""

from __future__ import annotations

from typing import Any

from core_loop_helpers import (
    RecordingSink,
    assistant,
    make_tool,
    run,
    stepping,
    tool_call,
    user,
)
from yanxu_harness.core.agent_loop import AgentLoopConfig, run_agent_loop_result
from yanxu_harness.core.outcome import RunStatus
from yanxu_harness.core.types import AgentContext, StopReason, TextContent, ToolResultMessage


def context_with(tools=()) -> AgentContext:
    return AgentContext(messages=[], system_prompt="", tools=list(tools))


class Tool:
    """One tool with its own call log, so which handler ran is not a guess."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[str] = []

        async def execute(call_id, _args, _signal=None, _on_update=None):
            self.calls.append(call_id)
            return {"content": [TextContent(text=f"{name} ran")], "details": {}}

        self.spec = make_tool(name, execute)


def results_of(messages) -> list[ToolResultMessage]:
    return [m for m in messages if isinstance(m, ToolResultMessage)]


def script(*, first_call: str, second_call: str) -> Any:
    """Turn 1 asks for `first_call` (which opens a next turn), turn 2 for `second_call`."""
    return stepping(
        assistant(tool_call("c0", first_call), stop=StopReason.TOOL_USE),
        assistant(tool_call("c1", second_call), stop=StopReason.TOOL_USE),
        assistant(TextContent(text="收尾")),
    )


def run_with_hook(initial: list[Tool], snapshot: Any) -> Any:
    def prepare(_context: Any) -> Any:
        return snapshot

    return prepare


def test_a_tool_revoked_by_prepare_next_turn_cannot_be_called() -> None:
    """The hook removed `t1`; a call to it must fail as not found and never run."""
    revoked = Tool("t1")
    kept = Tool("t2")
    replaced = context_with([kept.spec])
    stream = script(first_call="t2", second_call="t1")

    result = run(
        run_agent_loop_result(
            [user("go")],
            context_with([revoked.spec, kept.spec]),
            AgentLoopConfig(
                stream_fn=stream,
                tools=[revoked.spec, kept.spec],
                prepare_next_turn=lambda _ctx: {"context": replaced},
            ),
            None,
            RecordingSink(),
        )
    )

    assert revoked.calls == [], "the revoked tool's handler must not run"
    assert kept.calls == ["c0"], "the tool that stayed in the set still runs"
    second = [r for r in results_of(result.messages) if r.tool_call_id == "c1"]
    assert len(second) == 1
    assert second[0].is_error is True
    assert "not found" in second[0].content[0].text
    assert result.status is RunStatus.COMPLETED


def test_a_tool_granted_by_prepare_next_turn_can_be_called() -> None:
    """The hook added `t3`; a call to it must run."""
    existing = Tool("t1")
    granted = Tool("t3")
    replaced = context_with([existing.spec, granted.spec])
    stream = script(first_call="t1", second_call="t3")

    result = run(
        run_agent_loop_result(
            [user("go")],
            context_with([existing.spec]),
            AgentLoopConfig(
                stream_fn=stream,
                tools=[existing.spec],
                prepare_next_turn=lambda _ctx: {"context": replaced},
            ),
            None,
            RecordingSink(),
        )
    )

    assert granted.calls == ["c1"], "the granted tool must be executed"
    assert existing.calls == ["c0"]
    second = [r for r in results_of(result.messages) if r.tool_call_id == "c1"]
    assert second and second[0].is_error is False
    assert result.status is RunStatus.COMPLETED


def test_an_empty_replaced_tool_set_revokes_everything() -> None:
    """`{"context": ctx}` with no tools means "no executable tools", not "keep the old ones"."""
    tool = Tool("t1")
    stream = script(first_call="t1", second_call="t1")

    result = run(
        run_agent_loop_result(
            [user("go")],
            context_with([tool.spec]),
            AgentLoopConfig(
                stream_fn=stream,
                tools=[tool.spec],
                prepare_next_turn=lambda _ctx: {"context": context_with([])},
            ),
            None,
            RecordingSink(),
        )
    )

    assert tool.calls == ["c0"], "only the call made before the swap may run"
    second = [r for r in results_of(result.messages) if r.tool_call_id == "c1"]
    assert second and second[0].is_error is True
    assert "not found" in second[0].content[0].text


def test_a_snapshot_with_messages_only_keeps_the_current_tool_set() -> None:
    """Replacing only the *messages* must not silently revoke the tools."""
    tool = Tool("t1")
    stream = script(first_call="t1", second_call="t1")

    result = run(
        run_agent_loop_result(
            [user("go")],
            context_with([tool.spec]),
            AgentLoopConfig(
                stream_fn=stream,
                tools=[tool.spec],
                prepare_next_turn=lambda _ctx: {"messages": [user("准备消息")]},
            ),
            None,
            RecordingSink(),
        )
    )

    assert tool.calls == ["c0", "c1"], "a messages-only snapshot keeps the tools already in place"
    second = [r for r in results_of(result.messages) if r.tool_call_id == "c1"]
    assert second and second[0].is_error is False


def test_the_declarations_and_the_executable_set_cannot_drift_apart() -> None:
    """The model's visible tool set and the driver's executable set must be the same set.

    This is the invariant the two halves encode: the state keeps declarations (no
    callable can enter the durable state), the driver keeps the specs, and a hook that
    replaces the context replaces both. Asserting it directly is what turns the
    divergence found above into a failure rather than a silent behaviour change.
    """
    import asyncio

    from yanxu_harness.core.execution import PrepareTurn
    from yanxu_harness.core.runner import Runner
    from yanxu_harness.core.types import ToolDeclaration

    revoked = Tool("t1")
    granted = Tool("t3")

    async def emit(_event: Any) -> None:
        return None

    runner = Runner(
        AgentLoopConfig(
            stream_fn=lambda *args, **kwargs: None,
            tools=[revoked.spec],
            prepare_next_turn=lambda _ctx: {
                "context": context_with([revoked.spec, granted.spec])
            },
        ),
        emit,
    )

    async def scenario() -> tuple[list[str], list[str], list[Any]]:
        state = runner._initial_state(context_with([revoked.spec]))  # noqa: SLF001
        events = await runner._prepare_turn(  # noqa: SLF001
            PrepareTurn(message=assistant(TextContent(text="x")), turn=1), state
        )
        prepared = events[0]
        assert prepared.context_tools is not None
        declared = [tool.name for tool in prepared.context_tools]
        executable = [tool.name for tool in runner._context(state).tools]  # noqa: SLF001
        return declared, executable, list(prepared.context_tools)

    declared, executable, carried = asyncio.run(asyncio.wait_for(scenario(), timeout=5))
    assert declared == executable == ["t1", "t3"]
    assert all(isinstance(tool, ToolDeclaration) for tool in carried), (
        "only declarations may enter an event; the specs stay with the driver"
    )
