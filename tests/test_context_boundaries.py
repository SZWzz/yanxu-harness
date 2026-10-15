"""C0 of the context-engineering plan: the frozen baseline of context preparation.

``docs/context-engineering-implementation-plan.zh-CN.md`` section 2.2 records four
projection boundaries that exist today, and section 2.2's second half records the
wiring gaps behind them. This file turns those observations into cases. Each case
asserts the *target* behaviour and is marked ``xfail(strict=True)`` with the phase
that has to make it pass, so:

* the suite stays green while the defect is real (a marker, not a lie);
* the case starts failing the moment the phase lands (``strict`` turns XPASS into a
  failure), which forces the marker — and this docstring — to be revisited;
* nobody can claim the boundary is handled by pointing at the interface, because
  the interface is asserted here as a *behaviour*, not as an import.

The four measured boundaries, re-run against this tree (``.venv/bin/python
/tmp/c0_probes.py`` -> see ``docs/context-preparation-baseline.zh-CN.md``):

1. ``keep_system_entries=True`` does not keep the system entry — the window drops it
   silently and reports ``dropped_entries=1`` as if that were the requested result.
2. one 100-character message against ``char_budget=10`` is kept whole and reported as
   ``estimated_tokens=25`` with ``dropped_entries=0`` — a projection that fits nothing
   claims success.
3. ``capacity=0`` still keeps one non-empty message.
4. an assistant tool call and its toolResult are separated: only the lone
   ``toolResult`` survives.

Cases 5 and 6 cover the two registered wiring gaps: a persistent projection replaces
the authoritative conversation, and a configured ``project_context`` hook has no
consumer on the real provider path.
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


def assert_no_orphan_tool_results(
    messages: Sequence[Mapping[str, Any]], *, require_results: bool = True
) -> None:
    """Every call/result pair of a request view must be closed inside that view.

    This is the acceptance contract of section 3.2 of the plan (a harness rule, not a
    claim about what a provider returns for a broken transcript): a result without its
    declaring assistant message, or a declared call whose result was dropped, is not a
    smaller valid request — it is a different, invalid one. ``require_results=False``
    is for the one legitimate exception: a batch that is still awaiting an external
    decision never becomes a model request at all.
    """
    declared: dict[str, Mapping[str, Any]] = {}
    for message in messages:
        if message.get("role") == "assistant":
            for index, call in enumerate(message.get("tool_calls") or ()):
                declared[call["id"]] = call
        elif message.get("role") == "toolResult":
            call_id = message.get("tool_call_id")
            assert call_id in declared, (
                f"toolResult {call_id!r} has no declaring assistant message in this view: "
                "the call and its result were split"
            )
    if require_results:
        answered = {
            message.get("tool_call_id")
            for message in messages
            if message.get("role") == "toolResult"
        }
        for call_id in declared:
            assert call_id in answered, (
                f"call {call_id!r} was declared in this view but its result is missing"
            )


# -- the four measured boundaries (plan section 2.2) --------------------


@pytest.mark.xfail(
    strict=True,
    reason="C2: a declared system entry must survive or the projection must refuse (plan section 2.2)",
)
def test_boundary_1_a_declared_system_entry_is_not_silently_dropped() -> None:
    async def scenario() -> None:
        head, entries = await archived(
            {"role": "system", "content": "you are terse"},
            {"role": "user", "content": "hello"},
        )
        projector = WindowedProjector(char_budget=10, keep_system_entries=True)
        snapshot = projector.snapshot(entries, scope="project-a", source_head=head)
        kept = [message.get("role") for message in snapshot.messages]
        assert "system" in kept, (
            "the window dropped the effective system instruction although "
            "keep_system_entries=True and the entry fits; the parameter is silently inert"
        )

    run(scenario())


@pytest.mark.xfail(
    strict=True,
    reason="C1/C2: an unrepresentable request must be refused, not reported as fitting (plan section 2.2)",
)
def test_boundary_2_a_message_over_the_whole_budget_is_refused() -> None:
    async def scenario() -> None:
        head, entries = await archived({"role": "user", "content": "x" * 100})
        projector = WindowedProjector(char_budget=10)
        with pytest.raises((BudgetExceeded, ContractError)):
            projector.snapshot(entries, scope="project-a", source_head=head)

    run(scenario())


@pytest.mark.xfail(
    strict=True,
    reason=(
        "C2: the window projector still keeps a message at capacity=0; C1 refuses at "
        "dispatch instead (plan sections 2.2 and 5)"
    ),
)
def test_boundary_3_a_zero_capacity_yields_nothing_to_send() -> None:
    async def scenario() -> None:
        head, entries = await archived({"role": "user", "content": "hello"})
        projector = WindowedProjector(char_budget=32000)
        snapshot = projector.snapshot(
            entries, scope="project-a", source_head=head, capacity=0
        )
        assert snapshot.messages == (), (
            "capacity=0 must not produce a non-empty request view; a token capacity is "
            "also not a character budget, and this projector spends it as one"
        )

    run(scenario())


@pytest.mark.xfail(
    strict=True,
    reason="C2: a tool call and its result must be selected together (plan section 2.2)",
)
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
        projector = WindowedProjector(char_budget=2)
        snapshot = projector.snapshot(entries, scope="project-a", source_head=head)
        assert_no_orphan_tool_results(snapshot.messages)

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
