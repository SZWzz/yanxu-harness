"""C6: a long-conversation fixture, and the deterministic acceptance gates.

The plan's section 12 asks for one measurable fixture rather than a pile of unit cases: a
long history with the things that actually break projections — early constraints, tool-set
changes, many tool round trips, a large output, a late user correction — and then the
*gates* that must hold for it:

* every request view is protocol-legal (no lone result, no unanswered declared call);
* the effective instruction and tool state of every view equals the transcript's;
* the source is verifiable: the facts name the head they came from;
* the same configuration rebuilds the same request (deterministic, across instances);
* when the minimum required context does not fit, the transmission count is **zero**;
* the existing execution/approval contracts do not regress (the fixture drives the real
  harness, so a projection cannot break the pause it is standing next to).

The fixture is generated, not recorded, so it is the same on every machine and its size is a
number in the test rather than a number in a log.

What this file deliberately does **not** claim: that a model answers better with this
projection. That is the plan's semantic evaluation, it needs a real model, and it is recorded
as not executed in `docs/verification.md`.
"""

from __future__ import annotations

import asyncio
from typing import Any, Mapping, Sequence

import pytest
from context_helpers import real_convert, tool_result
from wire_fixture import WireFixture, delta_frame, tool_delta_frame

from yanxu_harness.context import (
    ContextController,
    ContextWindow,
    HeuristicTokenEstimator,
    InMemoryArtifactStore,
    ToolOutputPolicy,
    UnknownPolicy,
)
from yanxu_harness.context.projection import (
    FragmentProjector,
    message_fragments,
    system_state,
)
from yanxu_harness.core.types import (
    AssistantMessage,
    StopReason,
    SystemMessage,
    TextContent,
    ToolCall,
    UserMessage,
)
from yanxu_harness.providers import CoreStreamBridge, OpenAICompatibleProvider, ProviderConfig
from yanxu_harness.runtime import DefaultHarness, Principal, RunContext, RunIdentity, RunRequest
from yanxu_harness.storage import InMemoryPersistence

TURNS = 60
WINDOW = ContextWindow(limit_tokens=4000, output_reserve=256, safety_margin=32)


def stored_record(paths: Any) -> Mapping[str, Any]:
    """The committed run record, read from SQLite the way another process would."""
    import sqlite3

    connection = sqlite3.connect(paths.runs)
    try:
        row = connection.execute("select state from runs where run_id = ?", ("r1",)).fetchone()
    finally:
        connection.close()
    assert row is not None
    import json

    return json.loads(row[0])


def run(coro: Any) -> Any:
    async def guard() -> Any:
        return await asyncio.wait_for(coro, timeout=20)

    return asyncio.run(guard())


def long_history(turns: int = TURNS) -> list[Any]:
    """A generated long conversation with the shapes that break naive projections."""
    clock = _Clock()
    messages: list[Any] = [
        SystemMessage(
            content="You are a careful coding agent. Never edit tests without asking.",
            sections={"voice": "terse"},
            timestamp=clock.tick(),
        ),
    ]
    for index in range(turns):
        messages.append(
            UserMessage(content=f"turn {index}: please look at file_{index}.py " * 6, timestamp=clock.tick())
        )
        if index == 5:
            # A tool-set change partway through: the effective set must survive projection.
            messages.append(
                SystemMessage(content="", tools_added=[_declaration("search")], timestamp=clock.tick())
            )
        if index == 20:
            # A revoked tool: a projection that replays updates in the wrong order would
            # bring it back.
            messages.append(
                SystemMessage(content="", tools_removed=[_reference("search")], timestamp=clock.tick())
            )
        if index == 30:
            # A large output, the case a bounded view exists for.
            messages.append(_assistant_call(f"big-{index}", "read_log", at=clock.tick()))
            messages.append(
                tool_result(f"big-{index}", "log line\n" * 4000, name="read_log", timestamp=clock.tick())
            )
            continue
        if index == 45:
            # A late user correction: the newest user turn is what a projection must never
            # summarize away.
            messages.append(
                UserMessage(
                    content="Correction: the constraint changed, read before writing.",
                    timestamp=clock.tick(),
                )
            )
            messages.append(_assistant_call(f"c{index}", "read_file", at=clock.tick()))
            messages.append(
                tool_result(
                    f"c{index}", f"contents of file_{index}.py\n" * 20, name="read_file", timestamp=clock.tick()
                )
            )
            continue
        messages.append(_assistant_call(f"c{index}", "read_file", at=clock.tick()))
        messages.append(
            tool_result(f"c{index}", f"contents of file_{index}.py\n" * 8, name="read_file", timestamp=clock.tick())
        )
    messages.append(
        UserMessage(content="Final question: what did you change, and why? " * 4, timestamp=clock.tick())
    )
    return messages


class _Clock:
    """A deterministic clock: the fixture must not depend on the wall time."""

    def __init__(self) -> None:
        self.value = 1_000

    def tick(self) -> int:
        self.value += 1
        return self.value


def text_history(turns: int = 8) -> list[Any]:
    """A long history of *text* turns only.

    The harness validates the batch it starts from, and a transcript full of fabricated
    tool results is (correctly) refused: a tool result without a recorded status is not
    something a run may resume from. So the harness-driven case uses text turns and lets the
    run perform its own real tool round trip.
    """
    clock = _Clock()
    messages: list[Any] = [
        SystemMessage(content="You are a careful coding agent.", timestamp=clock.tick())
    ]
    for index in range(turns):
        messages.append(UserMessage(content=f"turn {index}: please look at file_{index}.py " * 6, timestamp=clock.tick()))
        messages.append(
            AssistantMessage(
                content=[TextContent(text=f"turn {index}: I read file_{index}.py and it looks fine. " * 6)],
                stop_reason=StopReason.STOP,
                timestamp=clock.tick(),
            )
        )
    return messages


def _declaration(name: str) -> Any:
    from yanxu_harness.core.types import ToolDeclaration

    return ToolDeclaration(name=name, description=f"{name} tool", parameters={"type": "object", "properties": {}})


def _reference(name: str) -> Any:
    from yanxu_harness.core.types import ToolReference

    return ToolReference(name=name)


def _assistant_call(call_id: str, name: str, *, at: int = 0) -> AssistantMessage:
    return AssistantMessage(
        content=[ToolCall(id=call_id, name=name, arguments={"path": "a.py"})],
        stop_reason=StopReason.TOOL_USE,
        timestamp=at,
    )


def projector(**kwargs: Any) -> FragmentProjector:
    return FragmentProjector(estimator=HeuristicTokenEstimator(), convert=real_convert, **kwargs)


def view_is_legal(messages: Sequence[Any]) -> None:
    """Section 3.2 of the plan, as a check the fixture runs on every view."""
    declared: set[str] = set()
    answered: set[str] = set()
    for message in real_convert(list(messages)).messages:
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or ():
                declared.add(call["id"])
        elif message.get("role") == "tool":
            call_id = message.get("tool_call_id")
            assert call_id in declared, f"view carries result {call_id!r} with no declaring call"
            answered.add(call_id)
    assert declared == answered, (
        f"a declared call lost its result: declared={sorted(declared)} answered={sorted(answered)}"
    )


# -- the deterministic gates --------------------------------------------


def test_a_long_conversation_projects_legally_and_equivalently() -> None:
    messages = long_history()
    full = projector().plan(messages, scope="ns/s1/r1", source_head="h", capacity=None)
    assert full.estimate.tokens and full.estimate.tokens > 10_000, "the fixture must be long"
    assert [fragment.kind for fragment in message_fragments(messages)].count("tool-roundtrip") >= TURNS - 2

    # Progressively tighter windows: every accepted projection must be legal and equivalent.
    stats: list[tuple[int, int, int]] = []
    for capacity in (full.estimate.tokens, 3000, 1500, 800, 400):
        result = projector().plan(messages, scope="ns/s1/r1", source_head="h", capacity=capacity)
        assert result.fits is True, capacity
        assert result.estimate.tokens <= capacity
        view_is_legal(result.messages)
        assert system_state(result.messages) == system_state(messages), (
            "the effective instructions and tool set must survive every window"
        )
        assert [tool["name"] for tool in system_state(result.messages)["tools"]] == [], (
            "the fixture revokes its only added tool at turn 20: it must not come back"
        )
        stats.append((capacity, len(result.kept_fragments), result.removed_messages))
    assert stats[0][1] > stats[-1][1], "a tighter window keeps fewer fragments"
    assert all(removed >= 0 for _capacity, _kept, removed in stats)


def test_a_long_conversation_projects_deterministically_across_instances() -> None:
    messages = long_history()
    first = projector().plan(messages, scope="ns/s1/r1", source_head="h", capacity=1500)
    second = projector().plan(messages, scope="ns/s1/r1", source_head="h", capacity=1500)
    assert first.request_fingerprint == second.request_fingerprint
    assert first.as_dict() == second.as_dict()
    assert first.source_fingerprint == second.source_fingerprint


def test_an_impossible_window_refuses_with_zero_transmissions() -> None:
    """The gate the plan names: a refused projection must not reach the wire."""
    messages = long_history()
    controller = ContextController.compose(
        estimator=HeuristicTokenEstimator(),
        converter=real_convert,
        window=ContextWindow(limit_tokens=64, output_reserve=32),
        on_unknown=UnknownPolicy.REFUSE,
    )
    with WireFixture([delta_frame("ok", finish="stop")]) as fixture:
        provider = OpenAICompatibleProvider(
            ProviderConfig(model="m", base_url=fixture.base_url, timeout_seconds=5.0)
        )
        harness = DefaultHarness(
            stream_fn=CoreStreamBridge(provider=provider),
            persistence=InMemoryPersistence(),
            system_prompt="be brief",
            context_controller=controller,
        )
        context = RunContext(
            identity=RunIdentity(namespace="ns", session_id="s1", run_id="r1", attempt_id="a1"),
            principal=Principal(subject="author"),
        )
        outcome = run(harness.start(RunRequest(prompt="final question", messages=tuple(messages)), context))
        assert fixture.requests == [], "a refused projection dispatched something"
    assert outcome.status.value == "usage_limited"
    assert outcome.error_code == "context_budget_exceeded"
    assert outcome.context_state is not None and outcome.context_state["refused"] is not None


def test_a_long_conversation_runs_through_the_harness_and_keeps_its_transcript(tmp_path: Path) -> None:
    """The projection is real, the tool round trip is real, and the transcript stays whole."""
    from context_helpers import real_convert as _convert
    from durable_edit import DeploymentPaths, build, run_context

    paths = DeploymentPaths.under(tmp_path / "state")
    messages = text_history(8)
    scripts = [
        [
            tool_delta_frame(0, call_id="call-1", name="notes.write", arguments='{"path":"a.md","text":"hello"}'),
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        ],
        [delta_frame("done", finish="stop")],
    ]
    with WireFixture(scripts=scripts) as fixture:
        controller = ContextController.compose(
            estimator=HeuristicTokenEstimator(),
            converter=_convert,
            window=ContextWindow(limit_tokens=1200, output_reserve=64),
            output_policy=ToolOutputPolicy(max_chars=500, head_chars=200, tail_chars=100),
            artifacts=InMemoryArtifactStore(),
        )
        provider = OpenAICompatibleProvider(
            ProviderConfig(model="m", base_url=fixture.base_url, timeout_seconds=5.0)
        )
        harness, _gateway = build(
            paths,
            CoreStreamBridge(provider=provider),
            context_controller=controller,
            auto_write=True,
        )
        outcome = run(
            harness.start(
                RunRequest(prompt="write a note", messages=tuple(messages), max_turns=3),
                run_context(),
            )
        )
        assert len(fixture.requests) >= 1
        for index in range(len(fixture.requests)):
            assert fixture.body_of(index)["messages"], "a request must carry messages"

    assert outcome.status.value == "completed", outcome.detail
    facts = outcome.context_state
    assert facts is not None
    assert facts["source_fingerprint"].startswith("sha256:")
    assert facts["kept_fragments"]
    assert facts["removed_messages"] > 0, "the long history had to be projected"
    # The transcript the run committed keeps every turn, including the ones the view dropped.
    roles = [message["role"] for message in stored_record(paths)["messages"]]
    assert roles.count("user") == len(messages) // 2 + 1
    assert "toolResult" in roles, "the real tool round trip is in the transcript"


def test_the_fixture_itself_is_stable() -> None:
    """A generated fixture is only useful if it does not drift between runs."""
    first, second = long_history(), long_history()
    assert len(first) == len(second)
    from yanxu_harness.context import transcript_fingerprint

    assert transcript_fingerprint(first) == transcript_fingerprint(second)
