"""The context-preparation boundaries: C0's frozen probes, kept as regression cases.

``docs/context-engineering-implementation-plan.zh-CN.md`` section 2.2 records four
projection boundaries and two wiring gaps. C0 turned them into cases that assert the
*target* behaviour under ``xfail(strict=True)`` with the phase that has to make each one
pass; the pointer is what makes the marker temporary: the suite stayed green while the
defect was real (a marker, not a lie), and ``strict`` turns an unexpected pass into a
failure that forces the marker — and this docstring — to be revisited.

The four projection boundaries are now green (C2 changed the projector), and they assert:

1. ``keep_system_entries=True`` does not silently drop the effective system entry: a
   window that cannot hold both the instructions and the current turn refuses, and with
   the flag off the instruction is the fragment that gets dropped.
2. one message larger than the whole budget is refused, not kept while claiming to fit.
3. zero capacity refuses instead of returning an empty view — an empty request is a
   different request, not a smaller one.
4. an assistant tool call and its ``toolResult`` are selected together or refused whole;
   a lone result can never be produced.

Cases 5 and 6 stay marked until C3: a persistent projection replaces the authoritative
conversation, and a configured ``project_context`` hook still has no consumer on the
provider path.
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


@pytest.mark.xfail(
    strict=True,
    reason=(
        "C3: a persistent projection must not delete the authoritative conversation "
        "(plan sections 3.1 and 4.2)"
    ),
)
def test_boundary_5_a_persistent_projection_does_not_delete_the_conversation() -> None:
    """The host's view of the conversation is what a later turn and the record use.

    ``persist_transformed_context=True`` writes the transformed view back as the active
    conversation through the ``ContextProjected`` input event, so the driver's own view of
    the conversation — the one a later turn, a hook and the durable record are built from —
    is the truncated one. The plan's section 3.1 states the invariant that a *request*
    projection never shrinks ``ExecutionState.messages`` / ``RunRecord.messages``, and
    section 4.2 says the managed mode must not use this replacement at all. Either
    resolution changes what this case observes: if the legacy flag stays destructive on
    purpose, move the case to the managed configuration and assert the durable run record
    instead of the driver's view.
    """

    async def scenario() -> None:
        seen: list[list[Any]] = []

        def transform(messages: Sequence[Any], _signal: Any) -> list[Any]:
            # A "compaction" that keeps only the tail: the two oldest turns are gone.
            return list(messages)[2:]

        def stop_hook(payload: Any) -> bool:
            seen.append(list(payload.context.messages))
            return True

        stream = _text_stream("A2")
        config = AgentLoopConfig(
            stream_fn=stream,
            transform_context=transform,
            persist_transformed_context=True,
            should_stop_after_turn=stop_hook,
        )
        context = AgentContext(
            messages=[
                UserMessage(content="Q1"),
                AssistantMessage(content=[TextContent(text="A1")], stop_reason=StopReason.STOP),
                UserMessage(content="Q2"),
            ]
        )
        sink = Sink()
        await run_agent_loop([UserMessage(content="Q2b")], context, config, None, sink)

        # ``ContextProjected`` is an input event the reducer applies, not something the
        # sink observes: the replacement is only visible in the state the driver uses.
        assert seen, "the stop hook must have run"
        texts = [getattr(message, "content", "") for message in seen[0]]
        assert "Q1" in texts and "A1" in texts, (
            "the request projection replaced the authoritative conversation: the messages "
            f"it dropped are no longer reachable as history, only {texts!r} is"
        )

    run(scenario())


@pytest.mark.xfail(
    strict=True,
    reason=(
        "C3: the project_context hook has no consumer on the provider path "
        "(plan section 2.2)"
    ),
)
def test_boundary_6_a_configured_projection_hook_is_consulted_before_dispatch() -> None:
    """``options['_project_context_for_budget']`` is set by the runner and read by nobody.

    The runner documents the closure as "consumed by the host's final request-budget
    gate"; ``CoreStreamBridge`` never looks at it, and ``PayloadGate`` receives only the
    serialized body. So a host that configures a compaction policy gets no compaction
    and no error — the same shape of gap the durable-commit round found in
    ``_state_sink``. This case drives a real loopback request, so it fails until the
    hook is either consumed end to end or refused at configuration time.
    """
    consulted: list[int] = []

    async def project_context(
        messages: Sequence[Any], _new_messages: Sequence[Any], _model: Any, _report: Any, _signal: Any
    ) -> Any:
        consulted.append(len(list(messages)))
        return None

    with WireFixture([delta_frame("ok", finish="stop")]) as fixture:
        provider = OpenAICompatibleProvider(
            ProviderConfig(model="m", base_url=fixture.base_url, timeout_seconds=5.0)
        )
        config = AgentLoopConfig(
            stream_fn=CoreStreamBridge(provider=provider),
            project_context=project_context,
        )
        context = AgentContext(messages=[UserMessage(content="hi")])
        run(run_agent_loop([UserMessage(content="hi")], context, config, None, Sink()))
        assert fixture.requests, "the request must still reach the wire"

    assert consulted, (
        "the configured project_context hook was never consulted before the payload was "
        "dispatched, so the host's compaction policy is silently ignored"
    )


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
