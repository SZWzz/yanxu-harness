"""The context-preparation boundaries: C0's frozen probes, kept as regression cases.

``docs/context-engineering-implementation-plan.zh-CN.md`` section 2.2 records four
projection boundaries and two wiring gaps. C0 turned them into cases that assert the
*target* behaviour under ``xfail(strict=True)`` with the phase that had to make each one
pass. That mechanism did its job: the marker was the record of a live defect, the suite
stayed green while the defect was real, and ``strict`` turned an unexpected pass into a
failure that forced the marker to be removed — which is what happened, phase by phase.

The four projection boundaries are now green (C2 changed the projector), and they assert:
1. ``keep_system_entries=True`` does not silently drop the effective system entry: a
   window that cannot hold both the instructions and the current turn refuses, and with
   the flag off the instruction is the fragment that gets dropped.
2. one message larger than the whole budget is refused, not kept while claiming to fit.
3. zero capacity refuses instead of returning an empty view — an empty request is a
   different request, not a smaller one.
4. an assistant tool call and its ``toolResult`` are selected together or refused whole;
   a lone result can never be produced.

Cases 5 and 6 covered the two wiring gaps and were released by C3: a managed projection is
now a *request view* beside the authoritative conversation (never a replacement of it), and
the dead ``project_context`` seam was removed in favour of ``prepare_context``, which the
harness wires and ``tests/test_context_wiring.py`` asserts end to end.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

import pytest
from wire_fixture import WireFixture, delta_frame

from yanxu_harness.context import (
    InMemoryTranscriptArchive,
    TranscriptEntry,
    WindowedProjector,
)
from yanxu_harness.core.agent_loop import AgentLoopConfig, run_agent_loop
from yanxu_harness.core.types import (
    AgentContext,
    AssistantMessage,
    StopReason,
    TextContent,
    UserMessage,
)
from yanxu_harness.errors import BudgetExceeded, ContractError
from yanxu_harness.providers import (
    CoreStreamBridge,
    OpenAICompatibleProvider,
    ProviderConfig,
)


from context_helpers import assert_no_orphan_tool_results  # noqa: E402  (shared with the projection tests)


def run(coro: Any) -> Any:
    import asyncio

    async def guard() -> Any:
        return await asyncio.wait_for(coro, timeout=5)

    return asyncio.run(guard())


class Sink:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def __call__(self, event: Any) -> None:
        self.events.append(event)

    def of(self, cls: type) -> list[Any]:
        return [event for event in self.events if isinstance(event, cls)]


async def archived(*payloads: Mapping[str, Any]) -> tuple[str, tuple[TranscriptEntry, ...]]:
    """The entries as the archive would number them, plus its head."""
    archive = InMemoryTranscriptArchive()
    stored = [
        await archive.append(TranscriptEntry(sequence=0, role=payload["role"], payload=dict(payload)))
        for payload in payloads
    ]
    return archive.head, tuple(stored)




# -- the four measured boundaries (plan section 2.2) --------------------


def test_boundary_1_a_declared_system_entry_is_not_silently_dropped() -> None:
    """The four C0 probes, re-asserted as the behaviour C2 must produce.

    A window that cannot hold the effective instructions *and* the current turn refuses;
    it does not quietly drop the instructions and report success. The flag is what
    decides that, which is the part that used to be silently inert.
    """

    async def scenario() -> None:
        head, entries = await archived(
            {"role": "system", "content": "you are terse"},
            {"role": "user", "content": "hello"},
        )
        with pytest.raises(BudgetExceeded) as refusal:
            WindowedProjector(char_budget=10, keep_system_entries=True).snapshot(
                entries, scope="project-a", source_head=head
            )
        assert "required" in str(refusal.value)

        permissive = WindowedProjector(char_budget=10, keep_system_entries=False).snapshot(
            entries, scope="project-a", source_head=head
        )
        assert [message["role"] for message in permissive.messages] == ["user"]
        assert permissive.dropped_reasons, "a dropped instruction fragment says so"

        roomy = WindowedProjector(char_budget=100, keep_system_entries=True).snapshot(
            entries, scope="project-a", source_head=head
        )
        assert [message["role"] for message in roomy.messages] == ["system", "user"]
        assert roomy.dropped_entries == 0

    run(scenario())


def test_boundary_2_a_message_over_the_whole_budget_is_refused() -> None:
    async def scenario() -> None:
        head, entries = await archived({"role": "user", "content": "x" * 100})
        projector = WindowedProjector(char_budget=10)
        with pytest.raises((BudgetExceeded, ContractError)) as refusal:
            projector.snapshot(entries, scope="project-a", source_head=head)
        assert "100 characters" in str(refusal.value)

    run(scenario())


def test_boundary_3_a_zero_capacity_yields_nothing_to_send() -> None:
    """Zero capacity cannot be satisfied by an empty view: that is a different request."""

    async def scenario() -> None:
        head, entries = await archived({"role": "user", "content": "hello"})
        with pytest.raises(BudgetExceeded):
            WindowedProjector(char_budget=32000).snapshot(
                entries, scope="project-a", source_head=head, capacity=0
            )
        with pytest.raises(ContractError):
            WindowedProjector(char_budget=32000).snapshot(
                entries, scope="project-a", source_head=head, capacity=-1
            )

    run(scenario())


def test_boundary_4_a_tool_call_and_its_result_are_never_split() -> None:
    async def scenario() -> None:
        head, entries = await archived(
            {
                "role": "assistant",
                "content": "looking it up",
                "tool_calls": [{"id": "call-1", "name": "read", "arguments": {"path": "a.md"}}],
            },
            {"role": "toolResult", "content": "ok", "tool_call_id": "call-1"},
        )
        snapshot = WindowedProjector(char_budget=1000).snapshot(
            entries, scope="project-a", source_head=head
        )
        assert [message["role"] for message in snapshot.messages] == ["assistant", "toolResult"]
        assert_no_orphan_tool_results(snapshot.messages)
        # A window too small for the round trip refuses it whole: a lone result would be a
        # different, invalid request.
        with pytest.raises(BudgetExceeded):
            WindowedProjector(char_budget=2).snapshot(entries, scope="project-a", source_head=head)

    run(scenario())


# -- the registered wiring gaps (plan section 2.2, second half) ---------


def test_boundary_5_a_managed_projection_does_not_delete_the_conversation() -> None:
    """The managed path derives a view; the authoritative conversation keeps everything.

    This is the same phenomenon the case was written for in C0 — a request projection must
    not shrink the conversation a later turn, a hook or the durable record is built from —
    asserted on the configuration the plan actually wants (`prepare_context`, section 4.2)
    instead of on the legacy `persist_transformed_context` flag, which stays destructive on
    purpose and is now refused together with the managed port.
    """
    from context_helpers import real_convert

    from yanxu_harness.context import (
        ContextController,
        ContextWindow,
        HeuristicTokenEstimator,
    )
    from yanxu_harness.core.messages import content_text

    async def scenario() -> None:
        seen: list[list[Any]] = []
        views: list[list[Any]] = []

        class RecordingStream:
            def __init__(self) -> None:
                self.calls: list[Any] = []

            async def __call__(self, model: Any, context: Any, options: Any = None) -> Any:
                import asyncio

                await asyncio.sleep(0)
                self.calls.append((model, context, options))
                views.append([getattr(message, "role", "") for message in context.messages])
                return self

            def __aiter__(self) -> Any:
                return self

            async def __anext__(self) -> Any:
                raise StopAsyncIteration

            async def result(self) -> AssistantMessage:
                return AssistantMessage(content=[TextContent(text="A2")], stop_reason=StopReason.STOP)

        def stop_hook(payload: Any) -> bool:
            seen.append(list(payload.context.messages))
            return True

        controller = ContextController.compose(
            estimator=HeuristicTokenEstimator(),
            converter=real_convert,
            # A window that forces the projection to drop the older turns.
            window=ContextWindow(limit_tokens=40, output_reserve=8),
        )
        stream = RecordingStream()
        config = AgentLoopConfig(
            stream_fn=stream,
            prepare_context=lambda payload: controller.prepare(payload, scope="project-a/s1/r1"),
            should_stop_after_turn=stop_hook,
        )
        context = AgentContext(
            messages=[
                UserMessage(content="Q1 " * 30),
                AssistantMessage(content=[TextContent(text="A1 " * 30)], stop_reason=StopReason.STOP),
                UserMessage(content="Q2"),
            ]
        )
        await run_agent_loop([UserMessage(content="Q2b")], context, config, None, Sink())

        assert seen, "the stop hook must have run"
        authoritative = [
            content_text(getattr(message, "content", "")) for message in seen[0]
        ]
        assert any("Q1" in text for text in authoritative) and any(
            "A1" in text for text in authoritative
        ), (
            "the request projection replaced the authoritative conversation: what it dropped "
            f"is no longer reachable as history, only {authoritative!r} is"
        )
        assert views and len(views[0]) < len(seen[0]), (
            "the projected view is what the model saw, and it is smaller than the conversation"
        )

    run(scenario())


def test_boundary_6_the_dead_budget_hook_is_replaced_by_a_real_port() -> None:
    """The closure the runner wrote into the provider options and nobody read is gone.

    C0 recorded `options["_project_context_for_budget"]` as a written-and-never-read seam.
    The plan allows either a real consumer or an explicit deprecated path; this build
    removes the field and refuses the combination, and the replacement
    (`AgentLoopConfig.prepare_context`, wired end to end in `tests/test_context_wiring.py`)
    is what a host uses instead. A refused option is stronger evidence than a half-consumed
    one: nothing silently ignores a compaction policy any more.
    """
    import dataclasses

    config = AgentLoopConfig(stream_fn=_text_stream("ok"))
    assert "prepare_context" in {field.name for field in dataclasses.fields(config)}
    assert not hasattr(config, "project_context")
    with pytest.raises(TypeError):
        AgentLoopConfig(stream_fn=_text_stream("ok"), project_context=lambda *_: None)  # type: ignore[call-arg]

    # The dead seam is not written into the provider options any more either.
    assert "_project_context_for_budget" not in dataclasses.asdict(config)


def _text_stream(text: str) -> Any:
    """A stream_fn that answers every request with one final text message."""

    class Stream:
        def __init__(self) -> None:
            self.calls: list[Any] = []

        async def __call__(self, model: Any, context: Any, options: Any = None) -> "Stream":
            import asyncio

            await asyncio.sleep(0)
            self.calls.append((model, context, options))
            return self

        def __aiter__(self) -> "Stream":
            return self

        async def __anext__(self) -> Mapping[str, Any]:
            raise StopAsyncIteration

        async def result(self) -> AssistantMessage:
            return AssistantMessage(content=[TextContent(text=text)], stop_reason=StopReason.STOP)

    return Stream()
