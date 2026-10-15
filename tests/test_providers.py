"""Provider port: streaming, failure kinds, and what never leaves the process."""

from __future__ import annotations

import asyncio
import json

import pytest

from yanxu_harness.errors import ContractError, ProviderFailure
from yanxu_harness.providers import (
    ProviderConfig,
    ProviderEvent,
    ProviderEventKind,
    ProviderFailureKind,
    ProviderRequest,
    ProviderToolSpec,
    SecretReference,
)
from yanxu_harness.runtime import Cancellation
from yanxu_harness.testing import ScriptedProvider


def run(coro):
    return asyncio.run(coro)


def test_a_secret_reference_holds_a_name_and_never_a_value() -> None:
    reference = SecretReference("OPENAI_API_KEY")
    assert reference.name == "OPENAI_API_KEY"
    assert "sk-" not in repr(reference)
    assert reference.resolve({"OPENAI_API_KEY": "sk-live"}) == "sk-live"
    with pytest.raises(ContractError, match="not available"):
        reference.resolve({})


def test_two_references_to_the_same_secret_are_equal() -> None:
    assert SecretReference("K") == SecretReference("K")
    assert SecretReference("K") != SecretReference("OTHER")
    assert len({SecretReference("K"), SecretReference("K")}) == 1


def test_the_public_config_projection_carries_no_header_values_or_key() -> None:
    config = ProviderConfig(
        model="example-model",
        base_url="https://example.invalid/v1",
        api_key=SecretReference("EXAMPLE_KEY"),
        headers={"X-Client": "yanxu", "X-Session": "abc"},
        context_limit_tokens=128000,
        output_reserve_tokens=16384,
    )
    public = config.public()
    assert json.loads(json.dumps(public)) == public
    assert public["has_api_key"] is True
    assert public["api_key_name"] == "EXAMPLE_KEY"
    assert public["header_names"] == ["X-Client", "X-Session"]
    assert "yanxu" not in json.dumps(public)
    assert "abc" not in json.dumps(public)


def test_a_config_must_declare_a_model_and_a_positive_timeout() -> None:
    with pytest.raises(ContractError):
        ProviderConfig(model="")
    with pytest.raises(ContractError):
        ProviderConfig(model="m", timeout_seconds=0)


def test_private_provider_frames_are_dropped_from_the_public_payload() -> None:
    event = ProviderEvent(
        kind=ProviderEventKind.THINKING_DELTA,
        thinking="considering",
        private={"signature": "sig-value", "encrypted_content": "blob"},
    )
    public = event.public_payload()
    assert public["thinking"] == "considering"
    assert "signature" not in json.dumps(public)
    assert "blob" not in json.dumps(public)
    assert "private" not in public


def test_a_failure_kind_survives_into_the_public_payload() -> None:
    event = ProviderEvent(
        kind=ProviderEventKind.ERROR,
        failure_kind=ProviderFailureKind.CONTEXT_OVERFLOW,
        failure_detail="too long",
    )
    assert event.public_payload()["failure_kind"] == "context_overflow"
    with pytest.raises(ProviderFailure) as excinfo:
        event.raise_for_error()
    assert excinfo.value.kind == "context_overflow"


def test_a_tool_spec_must_be_named() -> None:
    with pytest.raises(ContractError):
        ProviderToolSpec(name="", description="x")
    assert ProviderToolSpec(name="notes.read", description="Read a note").strict


def test_a_request_refuses_a_non_positive_output_budget() -> None:
    with pytest.raises(ContractError):
        ProviderRequest(messages=(), max_output_tokens=0)


def test_a_scripted_provider_replays_its_script_and_counts_calls() -> None:
    async def scenario() -> None:
        provider = ScriptedProvider(
            [
                ProviderEvent(kind=ProviderEventKind.TEXT_DELTA, text="海"),
                ProviderEvent(kind=ProviderEventKind.TERMINAL, stop_reason="stop"),
            ]
        )
        request = ProviderRequest(messages=[{"role": "user", "content": "hi"}])
        seen = [event.kind async for event in provider.stream(request)]
        assert seen == [ProviderEventKind.TEXT_DELTA, ProviderEventKind.TERMINAL]
        assert provider.calls == 1
        assert provider.requests[0] is request

    run(scenario())


def test_a_scripted_provider_stops_on_cancellation() -> None:
    async def scenario() -> None:
        token = Cancellation()
        token.cancel("author stopped")
        provider = ScriptedProvider([ProviderEvent(kind=ProviderEventKind.TEXT_DELTA, text="never")])
        frames = [event async for event in provider.stream(ProviderRequest(messages=[]), token)]
        assert frames[0].kind is ProviderEventKind.ERROR
        assert frames[0].failure_detail == "cancelled"

    run(scenario())
