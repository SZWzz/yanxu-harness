"""The provider → core bridge, driven by a real loopback provider into the real loop.

The acceptance criterion for this step is not "the conversion function returns the
right dict": it is that a *real* HTTP streaming provider, speaking real SSE,
drives the one model loop through the adapter, with a tool call executed by the
gateway and a final answer. Everything below either does that end to end or pins
one specific refusal (a failure, a truncated stream, unparseable arguments) that
must never turn into a finished answer.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

import pytest
from wire_fixture import WireFixture, delta_frame, tool_delta_frame, usage_frame

from yanxu_harness.core.agent_loop import AgentLoopConfig, run_agent_loop
from yanxu_harness.core.events import MessageUpdate, public_event_payload
from yanxu_harness.core.messages import create_initial_system_message
from yanxu_harness.core.types import (
    AgentContext,
    AssistantMessage,
    Model,
    StopReason,
    SystemMessage,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolDeclaration,
    ToolResultMessage,
    TranscriptContext,
    Usage,
    UserMessage,
)
from yanxu_harness.errors import ContractError
from yanxu_harness.events import assert_public_payload
from yanxu_harness.providers import (
    CoreStreamBridge,
    OpenAICompatibleProvider,
    ProviderConfig,
    ProviderEvent,
    ProviderEventKind,
    SecretReference,
    core_usage,
    map_stop_reason,
    to_provider_request,
)
from yanxu_harness.runtime import Cancellation, Principal, RunContext, RunIdentity
from yanxu_harness.testing import RecordingExecutionBackend, ScriptedProvider
from yanxu_harness.tools import (
    ConservativePolicy,
    GatewayBindings,
    InMemoryApprovalStore,
    ToolGateway,
    ToolRegistry,
    core_tool,
    read_only,
)

#: Assembled at runtime so no credential-shaped literal sits in the file.
LOOPBACK_KEY = "sk-" + "loopback-test-value"


def run(coro):
    async def guard():
        return await asyncio.wait_for(coro, timeout=30)

    return asyncio.run(guard())


@dataclass
class Sink:
    events: list[Any] = field(default_factory=list)

    async def __call__(self, event: Any) -> None:
        self.events.append(event)


def provider(fixture: WireFixture, **provider_kwargs) -> OpenAICompatibleProvider:
    """A provider pointed at the fixture; extra keywords go to the provider itself."""
    config = ProviderConfig(
        model="example-model",
        base_url=fixture.base_url,
        api_key=SecretReference("EXAMPLE_KEY"),
        timeout_seconds=10.0,
    )
    return OpenAICompatibleProvider(config, secrets={"EXAMPLE_KEY": LOOPBACK_KEY}, **provider_kwargs)


def text_of(message: AssistantMessage) -> str:
    return "".join(block.text for block in message.content if isinstance(block, TextContent))


def final_assistant(transcript) -> AssistantMessage:
    return [message for message in transcript if isinstance(message, AssistantMessage)][-1]


def transcript_context(messages=()) -> TranscriptContext:
    return TranscriptContext(messages=tuple(messages))


def run_identity(namespace: str = "project-a") -> RunContext:
    return RunContext(
        identity=RunIdentity(namespace=namespace, session_id="s1", run_id="r1", attempt_id="a1"),
        principal=Principal(subject="author"),
    )


# --- the loop, driven by a real provider -------------------------------------


def test_a_loopback_provider_drives_the_real_core_loop() -> None:
    frames = [
        delta_frame("海边的"),
        delta_frame("信号塔", finish="stop"),
        usage_frame(prompt_tokens=11, completion_tokens=4, total_tokens=15),
    ]
    with WireFixture(frames) as fixture:
        sink = Sink()
        context = AgentContext(messages=[create_initial_system_message("Be brief.", [])], tools=[])
        config = AgentLoopConfig(
            model=Model(id="example-model"),
            stream_fn=CoreStreamBridge(provider(fixture)),
            max_turns=2)
        transcript = run(
            run_agent_loop([UserMessage(content="海边有什么？")], context, config, None, sink)
        )
        body = fixture.body_of()

    final = final_assistant(transcript)
    assert text_of(final) == "海边的信号塔"
    assert final.stop_reason is StopReason.STOP
    assert (final.usage.input, final.usage.output, final.usage.total_tokens) == (11, 4, 15)
    # The transcript's system message became the request's system prompt, not a
    # duplicate message; no tools were declared, so the body has no tool fields.
    assert body["messages"][0] == {"role": "system", "content": "Be brief."}
    assert body["messages"][1]["role"] == "user"
    assert "tools" not in body and "tool_choice" not in body
    assert sink.events[0].type == "agent_start"
    assert [event.type for event in sink.events].count("message_update") >= 1


def test_the_streamed_frames_and_the_final_message_agree() -> None:
    with WireFixture([delta_frame("a"), delta_frame("b", finish="stop")]) as fixture:
        sink = Sink()
        context = AgentContext(messages=[], tools=[])
        config = AgentLoopConfig(
            # One delta per provider frame: with the default batching the provider
            # would send one frame, and there would be nothing to compare.
            model=Model(id="example-model"),
            stream_fn=CoreStreamBridge(provider(fixture, batch_flush_chars=1)),
            max_turns=2,
        )
        transcript = run(run_agent_loop([UserMessage(content="hi")], context, config, None, sink))

    final = final_assistant(transcript)
    updates = [event for event in sink.events if isinstance(event, MessageUpdate)]
    snapshots = [text_of(event.message) for event in updates]
    assert text_of(final) == "ab"
    assert snapshots[-1] == "ab"
    # Every snapshot is a prefix of the next one: the deltas describe the same
    # message the final result carries, in order, with no invented text.
    assert snapshots == sorted(snapshots, key=len)
    assert all("ab".startswith(snapshot) for snapshot in snapshots)


def test_a_tool_call_is_executed_once_by_the_gateway_and_its_result_reaches_the_model() -> None:
    scripts = [
        [
            tool_delta_frame(0, call_id="call-1", name="notes.read", arguments='{"path": "a.md"}'),
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        ],
        [delta_frame("读到了。", finish="stop")],
    ]
    calls: list[dict] = []

    def validator(arguments: dict) -> dict:
        if not isinstance(arguments.get("path"), str) or not arguments["path"]:
            raise ValueError("path must be a non-empty string")
        return {"path": arguments["path"]}

    def handler(context, arguments):
        calls.append({"run_id": context.identity.run_id, "arguments": dict(arguments)})
        return {"content": [TextContent(text="a.md: hello")], "details": {"bytes": 12}}

    definition = read_only("notes.read", "read a note", validator, handler)
    backend = RecordingExecutionBackend()

    with WireFixture(scripts=scripts) as fixture:
        gateway = ToolGateway(
            registry=ToolRegistry([definition]),
            policy=ConservativePolicy(),
            approvals=InMemoryApprovalStore(),
            backend=backend,
        )
        bindings = GatewayBindings(gateway=gateway, context=run_identity())
        tool_spec = core_tool(definition, bindings)
        context = AgentContext(
            messages=[create_initial_system_message("be brief", [tool_spec])],
            tools=[tool_spec],
        )
        config = AgentLoopConfig(
            model=Model(id="example-model"),
            stream_fn=CoreStreamBridge(provider(fixture)),
            max_turns=3)
        transcript = run(run_agent_loop([UserMessage(content="读 a.md")], context, config, None, Sink()))

        assert len(fixture.requests) == 2, "one provider request per turn"
        first = fixture.body_of(0)
        second = fixture.body_of(1)

    # The gateway is the only path: one intent, one handler call, one receipt.
    assert len(calls) == 1 and calls[0]["arguments"] == {"path": "a.md"}
    assert calls[0]["run_id"] == "r1"
    assert len(backend.intents) == 1 and len(backend.receipts) == 1
    assert first["tools"][0]["function"]["name"] == "notes.read"
    assert first["tools"][0]["function"]["parameters"]["type"] == "object"
    # The model sees the tool's own result, bound to the original call id.
    tool_messages = [message for message in second["messages"] if message["role"] == "tool"]
    assert [message["tool_call_id"] for message in tool_messages] == ["call-1"]
    assert "a.md: hello" in tool_messages[0]["content"]
    # …and so does the transcript, with the gateway status preserved.
    results = [message for message in transcript if isinstance(message, ToolResultMessage)]
    assert len(results) == 1
    assert results[0].is_error is False
    assert results[0].details["status"] == "executed"
    assert text_of(final_assistant(transcript)) == "读到了。"


def test_two_tool_calls_keep_their_own_arguments_through_the_adapter() -> None:
    frames = [
        tool_delta_frame(0, call_id="c1", name="a.tool", arguments='{"x":'),
        tool_delta_frame(1, call_id="c2", name="b.tool", arguments='{"y":'),
        tool_delta_frame(0, arguments="1}"),
        tool_delta_frame(1, arguments="2}"),
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ]
    with WireFixture(frames) as fixture:
        final = run(_drive(CoreStreamBridge(provider(fixture))))

    calls = [block for block in final.content if isinstance(block, ToolCall)]
    assert [(call.id, call.name, dict(call.arguments)) for call in calls] == [
        ("c1", "a.tool", {"x": 1}),
        ("c2", "b.tool", {"y": 2}),
    ]
    assert final.stop_reason is StopReason.TOOL_USE


async def _drive(bridge: CoreStreamBridge, context: TranscriptContext | None = None) -> AssistantMessage:
    """Run one stream to its terminal frame and return the final message."""
    stream = await bridge(Model(id="example-model"), context or transcript_context())
    async for _frame in stream:
        pass
    return await stream.result()


# --- refusals ----------------------------------------------------------------


def test_an_http_failure_is_an_error_terminal_with_a_machine_readable_kind() -> None:
    with WireFixture([], status=402, body=b'{"error": {"message": "no credit"}}') as fixture:
        sink = Sink()
        context = AgentContext(messages=[], tools=[])
        config = AgentLoopConfig(
            model=Model(id="example-model"),
            stream_fn=CoreStreamBridge(provider(fixture)),
            max_turns=2)
        transcript = run(run_agent_loop([UserMessage(content="hi")], context, config, None, sink))

    final = final_assistant(transcript)
    assert final.stop_reason is StopReason.ERROR
    assert "credits_exhausted" in (final.error_message or "")
    assert text_of(final) == ""
    assert final.usage.reported is False, "a failed request must not look like a measured one"
    assert [event.type for event in sink.events][-1] == "agent_end"


def test_a_stream_without_a_terminal_frame_is_refused_rather_than_reported_as_stop() -> None:
    provider = ScriptedProvider(
        [ProviderEvent(kind=ProviderEventKind.TEXT_DELTA, text="half an answer")]
    )
    final = run(_drive(CoreStreamBridge(provider)))
    assert final.stop_reason is StopReason.ERROR
    assert text_of(final) == "half an answer", "the partial answer is kept, not invented"
    assert "terminal frame" in (final.error_message or "")


def test_unparseable_tool_arguments_become_an_error_terminal_not_a_tool_call() -> None:
    provider = ScriptedProvider(
        [
            ProviderEvent(
                kind=ProviderEventKind.TOOL_CALL,
                tool_call_id="c1",
                tool_name="a.tool",
                arguments_json="{not json",
            )
        ]
    )
    final = run(_drive(CoreStreamBridge(provider)))
    assert final.stop_reason is StopReason.ERROR
    assert [block for block in final.content if isinstance(block, ToolCall)] == []
    assert "unparseable arguments" in (final.error_message or "")


def test_an_unrecognised_finish_reason_is_not_a_finished_answer() -> None:
    assert map_stop_reason("stop") is StopReason.STOP
    assert map_stop_reason("tool_calls") is StopReason.TOOL_USE
    assert map_stop_reason("length") is StopReason.LENGTH
    assert map_stop_reason("content_filter") is StopReason.ERROR
    assert map_stop_reason("something_new") is StopReason.ERROR


def test_missing_usage_fields_stay_missing() -> None:
    with WireFixture([delta_frame("x", finish="stop"), usage_frame(prompt_tokens=3)]) as fixture:
        final = run(_drive(CoreStreamBridge(provider(fixture))))
    assert final.usage.input == 3
    assert final.usage.output is None
    assert final.usage.total_tokens is None
    assert final.usage.reasoning is None
    assert core_usage({}) == Usage(), "an absent usage frame is unknown, not zero"


def test_cancelling_a_slow_stream_is_bounded_and_reported_as_aborted() -> None:
    # Thinking deltas are not batched by the provider, so the first one arrives
    # immediately and the cancel lands while the worker is parked in a read.
    frames = [
        {"choices": [{"index": 0, "delta": {"reasoning_content": "思考一"}}]},
        {"choices": [{"index": 0, "delta": {"reasoning_content": "思考二"}}]},
        delta_frame("答", finish="stop"),
    ]
    with WireFixture(frames, chunk_delay_seconds=5.0) as fixture:
        token = Cancellation()

        async def scenario():
            bridge = CoreStreamBridge(provider(fixture))
            stream = await bridge(Model(id="m"), transcript_context(), {"signal": token})
            kinds: list[str] = []
            started = time.monotonic()
            async for frame in stream:
                kinds.append(frame["type"])
                if frame["type"] == "thinking_delta" and not token.cancelled:
                    token.cancel("author stopped")
            return kinds, time.monotonic() - started, await stream.result()

        kinds, elapsed, final = run(scenario())

    assert elapsed < 2.0, f"cancellation must be bounded, took {elapsed:.2f}s"
    assert "error" in kinds
    assert final.stop_reason is StopReason.ABORTED
    assert "cancelled" in (final.error_message or "")


def test_a_slow_provider_does_not_block_the_event_loop() -> None:
    frames = [delta_frame("a"), delta_frame("b"), delta_frame("c", finish="stop")]
    with WireFixture(frames, chunk_delay_seconds=0.2) as fixture:

        async def scenario():
            stop = asyncio.Event()
            gaps: list[float] = []

            async def ticker() -> None:
                last = time.monotonic()
                while not stop.is_set():
                    await asyncio.sleep(0.01)
                    now = time.monotonic()
                    gaps.append(now - last)
                    last = now

            task = asyncio.create_task(ticker())
            started = time.monotonic()
            bridge = CoreStreamBridge(provider(fixture))
            stream = await bridge(Model(id="m"), transcript_context())
            async for _frame in stream:
                pass
            elapsed = time.monotonic() - started
            final = await stream.result()
            stop.set()
            await task
            return gaps, elapsed, final

        gaps, elapsed, final = run(scenario())

    assert elapsed >= 0.4, f"the fixture must actually have been slow, took {elapsed:.2f}s"
    assert gaps, "the ticker never ran, so the loop was blocked"
    assert max(gaps) < 0.15, f"the event loop was blocked for {max(gaps):.3f}s"


# --- private material and request shape --------------------------------------


def test_a_thinking_signature_stays_on_the_block_and_out_of_public_payloads() -> None:
    frames = [
        {"choices": [{"index": 0, "delta": {"reasoning_content": "思考", "signature": "sig-abc"}}]},
        delta_frame("答", finish="stop"),
    ]
    with WireFixture(frames) as fixture:
        sink = Sink()
        context = AgentContext(messages=[], tools=[])
        config = AgentLoopConfig(
            model=Model(id="example-model"),
            stream_fn=CoreStreamBridge(provider(fixture)),
            max_turns=2)
        transcript = run(run_agent_loop([UserMessage(content="hi")], context, config, None, sink))

    final = final_assistant(transcript)
    thinking = [block for block in final.content if isinstance(block, ThinkingContent)]
    assert thinking and thinking[0].thinking == "思考"
    # Continuation material survives on the message, where a replayed turn can use it…
    assert thinking[0].thinking_signature == "sig-abc"
    # …and the public projection of every event that carried it does not.
    for event in sink.events:
        if isinstance(event, MessageUpdate):
            payload = public_event_payload(event)
            assert "sig-abc" not in json.dumps(payload, default=str)
            assert "assistant_message_event" not in payload
    # The durable-event boundary agrees: the bare key is refused recursively.
    with pytest.raises(ContractError):
        assert_public_payload({"message": {"content": [{"signature": "sig-abc"}]}})


def test_thinking_is_replayed_only_when_the_host_asks_for_it() -> None:
    context = transcript_context(
        (
            SystemMessage(content="be brief"),
            UserMessage(content="hi"),
            AssistantMessage(
                content=[
                    ThinkingContent(thinking="思考", thinking_signature="sig-abc"),
                    TextContent(text="答"),
                ]
            ),
        )
    )
    default = to_provider_request(Model(id="m"), context)
    assert all("reasoning_content" not in message for message in default.messages)

    replay = to_provider_request(Model(id="m"), context, send_thinking=True)
    assistant = [message for message in replay.messages if message["role"] == "assistant"][0]
    assert assistant["reasoning_content"] == "思考"
    assert assistant["signature"] == "sig-abc"


def test_tool_declarations_reach_the_wire_and_strict_is_opt_in() -> None:
    declaration = ToolDeclaration(
        name="notes.read", description="read", parameters={"type": "object", "properties": {}}
    )
    strict = ToolDeclaration(
        name="notes.write",
        description="write",
        parameters={"type": "object", "properties": {}},
        constrained_sampling=True,
    )
    context = transcript_context(
        (SystemMessage(content="", tools_added=[declaration, strict]), UserMessage(content="hi"))
    )
    request = to_provider_request(Model(id="m"), context)
    assert [spec.name for spec in request.tools] == ["notes.read", "notes.write"]
    assert [spec.strict for spec in request.tools] == [False, True]
    assert request.system == ""


def test_a_block_the_transport_cannot_express_is_refused_instead_of_dropped() -> None:
    context = transcript_context((UserMessage(content=[ToolCall(id="c", name="t")]),))
    with pytest.raises(ContractError) as excinfo:
        to_provider_request(Model(id="m"), context)
    assert "cannot carry" in str(excinfo.value)


def test_an_image_block_becomes_a_data_url() -> None:
    from yanxu_harness.core.types import ImageContent

    context = transcript_context((UserMessage(content=[ImageContent(data="QUJD", mime_type="image/png")]),))
    request = to_provider_request(Model(id="m"), context)
    parts = request.messages[0]["content"]
    assert parts == [{"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}}]


def test_a_second_credential_path_is_refused() -> None:
    """One place resolves credentials; core's resolved `api_key` is not a second one."""
    with WireFixture([delta_frame("x", finish="stop")]) as fixture:
        bridge = CoreStreamBridge(provider(fixture))
        with pytest.raises(ContractError) as excinfo:
            bridge.build_request(Model(id="m"), transcript_context(), {"api_key": "sk-from-core"})
    assert "credential" in str(excinfo.value)
    assert fixture.requests == [], "a refused request must not have been sent"


def test_the_unknown_core_model_sentinel_never_reaches_the_wire() -> None:
    from yanxu_harness.core.agent import DEFAULT_MODEL

    assert to_provider_request(DEFAULT_MODEL, transcript_context()).model == ""
    assert to_provider_request(Model(id="real-model"), transcript_context()).model == "real-model"


def test_aborting_one_response_does_not_close_a_later_one() -> None:
    """A transport-level regression: abort must be scoped to its own response.

    The first version tracked "the current connection" on the transport, so a
    late teardown of turn one closed turn two's connection — which the release
    check caught as an empty second answer.
    """
    from yanxu_harness.providers import HttpTransport

    scripts = [[delta_frame("one", finish="stop")], [delta_frame("two", finish="stop")]]
    with WireFixture(scripts=scripts) as fixture:
        transport = HttpTransport()
        url = f"{fixture.base_url}/chat/completions"
        first = transport.send(url=url, headers={}, body=b"{}", timeout=5.0)
        assert b"one" in b"".join(first.chunks)
        second = transport.send(url=url, headers={}, body=b"{}", timeout=5.0)

        assert first.abort is not None
        first.abort()  # a late teardown of the *first* request

        body = b"".join(second.chunks)
        assert b"two" in body, "aborting the previous response must not close this one"


def test_core_stops_at_the_terminal_frame_and_the_next_turn_still_works() -> None:
    """Core returns as soon as it has the final message; the next turn must survive it."""
    scripts = [[delta_frame("first", finish="stop")], [delta_frame("second", finish="stop")]]
    with WireFixture(scripts=scripts) as fixture:
        bridge = CoreStreamBridge(provider(fixture))

        async def scenario():
            first = await bridge(Model(id="m"), transcript_context())
            async for frame in first:
                if frame["type"] == "done":
                    break  # exactly what `_stream_assistant_response` does
            assert text_of(await first.result()) == "first"
            second = await bridge(Model(id="m"), transcript_context())
            async for _frame in second:
                pass
            return await second.result()

        final = run(scenario())

    assert text_of(final) == "second"
    assert len(fixture.requests) == 2
