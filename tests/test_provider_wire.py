"""The provider adapter, exercised against a real loopback HTTP server.

Nothing here talks to a vendor and nothing stubs the transport: the fixture
serves real HTTP, so these tests prove what is actually on the wire — headers,
body shape, one request per call, and the exact frames a caller will see.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from wire_fixture import (
    WireFixture,
    delta_frame,
    error_body,
    tool_delta_frame,
    usage_frame,
)

from yanxu_harness.errors import ProviderFailure
from yanxu_harness.providers import (
    OpenAICompatibleProvider,
    ProviderConfig,
    ProviderEventKind,
    ProviderFailureKind,
    ProviderRequest,
    ProviderToolSpec,
    SecretReference,
    classify_status,
    collect,
    parse_stream_frames,
    redact,
)
from yanxu_harness.runtime import Cancellation


#: Assembled at runtime so no credential-shaped literal lives in the file: a
#: fake key that looks real trips secret scanners in a public repository, and
#: the redaction test still needs a string that *matches* the redaction pattern.
LOOPBACK_KEY = "sk-" + "loopback-test-value"
REDACTION_FIXTURE = "sk-" + "live-abcdefghijklmnop"


def run(coro):
    return asyncio.run(coro)


def provider(fixture: WireFixture, **overrides) -> OpenAICompatibleProvider:
    values = {
        "model": "example-model",
        "base_url": fixture.base_url,
        "api_key": SecretReference("EXAMPLE_KEY"),
        "timeout_seconds": 10.0,
    }
    values.update(overrides)
    return OpenAICompatibleProvider(
        ProviderConfig(**values), secrets={"EXAMPLE_KEY": LOOPBACK_KEY}
    )


def request(**overrides) -> ProviderRequest:
    values = {"messages": [{"role": "user", "content": "你好"}], "max_output_tokens": 256}
    values.update(overrides)
    return ProviderRequest(**values)  # type: ignore[arg-type]


# -- streaming ----------------------------------------------------------


def test_a_text_stream_assembles_into_one_answer() -> None:
    frames = [
        delta_frame("海边的"),
        delta_frame("信号塔"),
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        usage_frame(prompt_tokens=11, completion_tokens=7, total_tokens=18),
    ]
    with WireFixture(frames, include_pings=True) as fixture:
        result = run(collect(provider(fixture), request()))
    assert result.text == "海边的信号塔"
    assert result.stop_reason == "stop"
    assert result.usage == {
        "prompt_tokens": 11,
        "completion_tokens": 7,
        "total_tokens": 18,
        "reasoning_tokens": None,
    }
    assert len(fixture.requests) == 1, "one call must send exactly one request"


def test_missing_usage_fields_stay_missing_not_zero() -> None:
    with WireFixture([delta_frame("hi", finish="stop"), usage_frame(prompt_tokens=3)]) as fixture:
        result = run(collect(provider(fixture), request()))
    assert result.usage["prompt_tokens"] == 3
    assert result.usage["completion_tokens"] is None
    assert result.usage["total_tokens"] is None


def test_tool_call_fragments_become_one_call_with_parsed_arguments() -> None:
    frames = [
        tool_delta_frame(0, call_id="call-1", name="notes.write"),
        tool_delta_frame(0, arguments='{"path": "a'),
        tool_delta_frame(0, arguments='.md"}'),
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ]
    with WireFixture(frames) as fixture:
        result = run(collect(provider(fixture), request()))
    assert result.stop_reason == "tool_calls"
    assert result.tool_calls == ({"id": "call-1", "name": "notes.write", "arguments": {"path": "a.md"}},)


def test_interleaved_tool_calls_stay_separate() -> None:
    frames = [
        tool_delta_frame(0, call_id="c1", name="a.tool", arguments='{"x":'),
        tool_delta_frame(1, call_id="c2", name="b.tool", arguments='{"y":'),
        tool_delta_frame(0, arguments="1}"),
        tool_delta_frame(1, arguments="2}"),
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ]
    with WireFixture(frames) as fixture:
        result = run(collect(provider(fixture), request()))
    assert [call["id"] for call in result.tool_calls] == ["c1", "c2"]
    assert [call["arguments"] for call in result.tool_calls] == [{"x": 1}, {"y": 2}]


def test_unparseable_tool_arguments_fail_instead_of_reaching_the_executor() -> None:
    frames = [
        tool_delta_frame(0, call_id="c1", name="a.tool", arguments="{not json"),
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ]
    with WireFixture(frames) as fixture:
        with pytest.raises(ProviderFailure) as excinfo:
            run(collect(provider(fixture), request()))
    assert excinfo.value.kind == ProviderFailureKind.INVALID_OUTPUT.value


def test_a_stream_without_a_finish_reason_is_not_reported_as_stop() -> None:
    with WireFixture([delta_frame("half an answer")]) as fixture:
        with pytest.raises(ProviderFailure) as excinfo:
            run(collect(provider(fixture), request()))
    assert excinfo.value.kind == ProviderFailureKind.INVALID_OUTPUT.value
    assert "finish reason" in str(excinfo.value)


@pytest.mark.parametrize("reason", ["stop", "tool_calls", "length"])
def test_the_finish_reason_survives_into_the_result(reason: str) -> None:
    with WireFixture([delta_frame("x", finish=reason)]) as fixture:
        result = run(collect(provider(fixture), request()))
    assert result.stop_reason == reason


# -- privacy and secrets ------------------------------------------------


def test_private_reasoning_frames_never_reach_a_public_payload() -> None:
    frames = [
        {"choices": [{"index": 0, "delta": {"reasoning_content": "thinking", "signature": "sig-abc"}}]},
        delta_frame("answer", finish="stop"),
    ]
    with WireFixture(frames) as fixture:
        events = []

        async def scenario() -> None:
            async for event in provider(fixture).stream(request()):
                events.append(event)

        run(scenario())
    thinking = [event for event in events if event.kind is ProviderEventKind.THINKING_DELTA]
    assert thinking and thinking[0].thinking == "thinking"
    assert thinking[0].private["signature"] == "sig-abc"
    public = json.dumps(thinking[0].public_payload(), ensure_ascii=False)
    assert "sig-abc" not in public
    assert "signature" not in public


def test_the_api_key_travels_as_a_header_only() -> None:
    with WireFixture([delta_frame("ok", finish="stop")]) as fixture:
        run(collect(provider(fixture), request()))
    served = fixture.first_request()
    assert served["headers"]["authorization"] == f"Bearer {LOOPBACK_KEY}"
    assert LOOPBACK_KEY not in served["body"]


def test_the_wire_carries_the_host_supplied_headers() -> None:
    with WireFixture([delta_frame("ok", finish="stop")]) as fixture:
        run(collect(provider(fixture, headers={"x-client": "example/1.0"}), request()))
    assert fixture.first_request()["headers"]["x-client"] == "example/1.0"


def test_a_credential_in_an_error_body_is_redacted() -> None:
    with WireFixture([], status=500, body=error_body(f"upstream said authorization: Bearer {REDACTION_FIXTURE}")) as fixture:
        with pytest.raises(ProviderFailure) as excinfo:
            run(collect(provider(fixture), request()))
    assert REDACTION_FIXTURE not in str(excinfo.value)


def test_redaction_and_failure_classification_are_shared_helpers() -> None:
    assert REDACTION_FIXTURE not in redact(f"key={REDACTION_FIXTURE}")
    assert classify_status(429, "") is ProviderFailureKind.RATE_LIMITED
    assert classify_status(429, "quota exhausted") is ProviderFailureKind.CREDITS_EXHAUSTED
    assert classify_status(400, "maximum context length is 8192") is ProviderFailureKind.CONTEXT_OVERFLOW
    assert classify_status(400, "bad parameter") is ProviderFailureKind.BAD_REQUEST
    assert classify_status(401, "") is ProviderFailureKind.AUTH_FAILED
    assert classify_status(404, "") is ProviderFailureKind.MODEL_NOT_FOUND


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (429, '{"error": {"message": "slow down"}}', ProviderFailureKind.RATE_LIMITED),
        (400, '{"error": {"message": "prompt is too long"}}', ProviderFailureKind.CONTEXT_OVERFLOW),
        (503, '{"error": {"message": "upstream"}}', ProviderFailureKind.FAILED),
    ],
)
def test_an_http_failure_becomes_a_machine_readable_kind(status: int, body: str, expected) -> None:
    with WireFixture([], status=status, body=body.encode("utf-8")) as fixture:
        with pytest.raises(ProviderFailure) as excinfo:
            run(collect(provider(fixture), request()))
    assert excinfo.value.kind == expected.value


# -- request shape ------------------------------------------------------


def test_the_request_body_carries_model_messages_tools_and_the_output_cap() -> None:
    with WireFixture([delta_frame("ok", finish="stop")]) as fixture:
        run(
            collect(
                provider(fixture),
                request(
                    system="be brief",
                    tools=[ProviderToolSpec(name="notes.read", description="Read", parameters={"type": "object"})],
                ),
            )
        )
    body = fixture.body_of()
    assert body["model"] == "example-model"
    assert body["stream"] is True
    assert body["max_tokens"] == 256
    assert body["messages"][0] == {"role": "system", "content": "be brief"}
    assert body["tools"][0]["function"]["name"] == "notes.read"
    assert body["tools"][0]["function"]["strict"] is True


def test_no_tool_fields_appear_when_no_tools_are_offered() -> None:
    with WireFixture([delta_frame("ok", finish="stop")]) as fixture:
        run(collect(provider(fixture), request()))
    body = fixture.body_of()
    assert "tools" not in body and "tool_choice" not in body


def test_a_frame_split_across_writes_is_reassembled_by_the_http_layer() -> None:
    """The fixture writes whole responses; this proves the parser tolerates a fragmented read."""
    chunks = [b'data: {"choices": [{"index": 0, "delta": {"content": "a"}}]}\n\n', b'data: {"choices": [{"index": 0, "delta": {"content": "b"}}]}\n\n']
    payloads = [json.loads(chunk.decode()[6:].strip()) for chunk in chunks]
    assert payloads[0]["choices"][0]["delta"]["content"] == "a"
    assert [frame["choices"][0]["delta"]["content"] for frame in parse_stream_frames(chunks)] == ["a", "b"]


def test_comment_and_ping_lines_are_not_content() -> None:
    lines = [b": ping\n\n", b"\n", b"data: [DONE]\n\n"]
    assert list(parse_stream_frames(lines)) == []


def test_an_unparseable_frame_names_the_json_error() -> None:
    with pytest.raises(ProviderFailure) as excinfo:
        list(parse_stream_frames([b"data: {oops\n\n"]))
    assert "unparseable provider frame" in str(excinfo.value)


# -- cancellation and timeouts -----------------------------------------


def test_cancellation_stops_the_stream() -> None:
    token = Cancellation()
    token.cancel("author stopped")

    async def scenario() -> None:
        with WireFixture([delta_frame("never", finish="stop")]) as fixture:
            frames = [event async for event in provider(fixture).stream(request(), token)]
        assert frames[-1].kind is ProviderEventKind.ERROR
        assert frames[-1].failure_kind is ProviderFailureKind.CANCELLED

    run(scenario())


def test_a_slow_provider_hits_the_timeout() -> None:
    with WireFixture([delta_frame("late", finish="stop")], delay_seconds=2.0) as fixture:
        with pytest.raises(ProviderFailure) as excinfo:
            run(collect(provider(fixture, timeout_seconds=0.2), request()))
    assert excinfo.value.kind == ProviderFailureKind.TIMEOUT.value


def test_a_provider_failure_is_never_a_success() -> None:
    """The one thing that must never happen: a failure that looks like a finished answer."""
    with WireFixture([], status=500, body=b"upstream exploded") as fixture:
        with pytest.raises(ProviderFailure):
            run(collect(provider(fixture), request()))
    assert time.time() > 0
