"""C1: measure the whole request, and refuse honestly instead of guessing.

E01/E02/E03 of the context-engineering plan, at the level the plan puts them: the
measurement port and the budget decision. Wiring them into the harness is C3; what is
proved here is that the numbers cover every part of the request (system, all tool
schemas, protocol overhead, output configuration), that the output reserve is deducted
once, that an unknown stays unknown unless the host explicitly opts into proceeding,
and that the final-byte gate refuses before any transport call.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Mapping, Sequence

import pytest
from wire_fixture import WireFixture, delta_frame

from yanxu_harness.context import (
    ContextWindow,
    HeuristicTokenEstimator,
    MeasurementInput,
    RequestBudget,
    TokenEstimate,
    UnknownPolicy,
    payload_budget_gate,
)
from yanxu_harness.errors import BudgetExceeded, ContractError
from yanxu_harness.providers import (
    OpenAICompatibleProvider,
    ProviderConfig,
    ProviderRequest,
    ProviderToolSpec,
    collect,
)


def run(coro: Any) -> Any:
    async def guard() -> Any:
        return await asyncio.wait_for(coro, timeout=10)

    return asyncio.run(guard())


def estimator() -> HeuristicTokenEstimator:
    return HeuristicTokenEstimator()


def big_tool(name: str = "write_file", description_chars: int = 4000) -> Mapping[str, Any]:
    return {
        "name": name,
        "description": "d" * description_chars,
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        "strict": False,
    }


# -- E02: the whole request, and one deduction --------------------------


def test_e02_the_tool_schemas_are_counted_even_when_the_chat_text_is_tiny() -> None:
    """A request's size is usually its tool schemas; a chat-only measure lies."""
    chat_only = MeasurementInput(messages=[{"role": "user", "content": "hi"}])
    with_tool = MeasurementInput(
        messages=[{"role": "user", "content": "hi"}], tools=[big_tool()]
    )
    small = estimator().measure(chat_only)
    large = estimator().measure(with_tool)
    assert small.tokens is not None and large.tokens is not None
    assert large.parts["tools"] > large.parts["messages"]
    assert large.tokens > small.tokens, "the tool schema must add to the request"
    # The schema's own JSON is what is measured, not just its name.
    assert large.parts["tools"] >= 4000 // 4


def test_e02_the_estimate_covers_every_part_and_says_how_it_was_measured() -> None:
    estimate = estimator().measure(
        MeasurementInput(
            system="be brief",
            messages=[{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}],
            tools=[big_tool("read", 40), big_tool("write", 40)],
        )
    )
    assert set(estimate.parts) == {"system", "tools", "messages", "protocol"}
    assert estimate.tokens == sum(estimate.parts.values())
    assert estimate.parts["system"] > 0 and estimate.parts["tools"] > 0
    assert estimate.parts["protocol"] > 0, "the per-message/per-tool overhead is its own part"
    assert estimate.method and estimate.estimator_id and estimate.estimator_version
    assert estimate.exact is False


def test_e02_the_output_reserve_is_deducted_once() -> None:
    budget = RequestBudget(
        window=ContextWindow(limit_tokens=1000, output_reserve=200, safety_margin=50)
    )
    capacity, reserve, source = budget.capacity_for(300)
    assert (capacity, reserve, source) == (650, 300, "request")
    # Not 1000 - 200 - 300 - 50: the reserve is the larger of the two, not both.
    assert capacity != 450
    # And with no request-specific cap the policy reserve is the one deducted.
    assert budget.capacity_for(None) == (750, 200, "policy")


def test_e02_the_capacity_uses_the_one_existing_formula() -> None:
    """``ContextWindow.request_capacity`` stays the single place that does the math."""
    for window in (
        ContextWindow(limit_tokens=1000, output_reserve=200, safety_margin=50),
        ContextWindow(limit_tokens=1000),
        ContextWindow(limit_tokens=None, output_reserve=100),
        ContextWindow(limit_tokens=100, output_reserve=100, safety_margin=1),
    ):
        budget = RequestBudget(window=window)
        for request_cap in (None, 0, 7, 250):
            effective, _source = budget.effective_window(request_cap)
            assert budget.capacity_for(request_cap)[0] == effective.request_capacity


def test_e02_thinking_material_that_is_re_sent_is_counted() -> None:
    """An assistant turn can carry more than its visible text; all of it is sent."""
    plain = MeasurementInput(messages=[{"role": "assistant", "content": "hi"}])
    with_thinking = MeasurementInput(
        messages=[
            {
                "role": "assistant",
                "content": "hi",
                "reasoning_content": "t" * 400,
                "signature": "s" * 40,
            }
        ]
    )
    assert (
        estimator().measure(with_thinking).parts["messages"]
        > estimator().measure(plain).parts["messages"] + 100
    )


def test_e02_an_unknown_output_reserve_is_reported_instead_of_assumed_zero() -> None:
    budget = RequestBudget(window=ContextWindow(limit_tokens=1000))
    estimate = estimator().measure(MeasurementInput(messages=[{"role": "user", "content": "hi"}]))
    permissive = budget.assess(estimate)
    assert permissive.output_reserve_source == "unknown"
    assert "output reserve is unknown" in permissive.reason
    assert permissive.fits is None, (
        "a reserve that is not known cannot be treated as zero and then reported as fitting"
    )
    strict = RequestBudget(window=ContextWindow(limit_tokens=1000), on_unknown=UnknownPolicy.REFUSE)
    with pytest.raises(BudgetExceeded):
        strict.require(estimate)


def test_e02_a_policy_without_a_declared_window_reserve_is_explicit_about_it() -> None:
    """Zero is a policy value, not a default: ``output_reserve=0`` is declared, not unknown."""
    budget = RequestBudget(window=ContextWindow(limit_tokens=100))
    assert budget.effective_reserve(None) == (0, "unknown")
    declared = RequestBudget(window=ContextWindow(limit_tokens=100, output_reserve=0))
    assert declared.effective_reserve(None) == (0, "unknown"), (
        "a zero reserve cannot be distinguished from an undeclared one; the source "
        "field is what tells the host"
    )
    assert RequestBudget(
        window=ContextWindow(limit_tokens=100, output_reserve=10)
    ).effective_reserve(None) == (10, "policy")


# -- E03: unknown stays unknown; heuristics say so ----------------------


def test_e03_an_unknown_window_reports_unknown_and_invents_no_percentage() -> None:
    budget = RequestBudget(window=ContextWindow(limit_tokens=None))
    estimate = estimator().measure(MeasurementInput(messages=[{"role": "user", "content": "hi"}]))
    report = budget.require(estimate)
    assert report.fits is None and report.capacity is None
    assert report.known is False
    assert "unknown" in report.reason
    # No percentage field exists to be misread as a utilisation figure.
    assert not any("percent" in key or "ratio" in key for key in report.as_dict())
    strict = RequestBudget(window=ContextWindow(limit_tokens=None), on_unknown=UnknownPolicy.REFUSE)
    with pytest.raises(BudgetExceeded):
        strict.require(estimate)


def test_e03_an_image_makes_the_estimate_unknown_rather_than_zero() -> None:
    request = MeasurementInput(
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is this"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                ],
            }
        ]
    )
    estimate = estimator().measure(request)
    assert estimate.tokens is None
    assert estimate.unmeasured == ("messages[0]: image_url",)
    permissive = RequestBudget(window=ContextWindow(limit_tokens=100000)).assess(estimate)
    assert permissive.fits is None, "an unmeasured part cannot be reported as fitting"
    strict = RequestBudget(
        window=ContextWindow(limit_tokens=100000), on_unknown=UnknownPolicy.REFUSE
    )
    with pytest.raises(BudgetExceeded):
        strict.require(estimate)


def test_e03_the_default_estimator_never_claims_exactness_and_is_replaceable() -> None:
    cjk = MeasurementInput(messages=[{"role": "user", "content": "这是一段中文" * 100}])
    heuristic = estimator().measure(cjk)
    assert heuristic.exact is False
    assert "heuristic" in heuristic.method
    assert "not a provider billing figure" in heuristic.reason

    class ExactStub:
        estimator_id = "host-tokenizer"
        estimator_version = "2"
        method = "host-tokenizer/2"

        def measure(self, request: MeasurementInput) -> TokenEstimate:
            return TokenEstimate(
                tokens=7,
                method=self.method,
                estimator_id=self.estimator_id,
                estimator_version=self.estimator_version,
                exact=True,
            )

    report = RequestBudget(window=ContextWindow(limit_tokens=100)).assess(
        ExactStub().measure(cjk)
    )
    assert report.estimate.estimator_id == "host-tokenizer"
    assert report.estimate.estimator_version == "2"
    assert report.estimate.exact is True
    assert report.estimated_input_tokens == 7


def test_e03_a_partly_measurable_request_must_not_report_a_total() -> None:
    with pytest.raises(ContractError):
        TokenEstimate(tokens=10, method="h", unmeasured=("image",))
    with pytest.raises(ContractError):
        TokenEstimate(tokens=None, method="h")


# -- E01/E10: the final gate, on a real wire ----------------------------


def _provider(fixture: WireFixture, gate: Any) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        ProviderConfig(model="m", base_url=fixture.base_url, timeout_seconds=5.0),
        payload_gate=gate,
    )


def test_e01_a_zero_capacity_request_never_reaches_the_wire() -> None:
    """Capacity 0 means the input already fills the window: nothing may be sent."""
    budget = RequestBudget(window=ContextWindow(limit_tokens=100, safety_margin=100))
    report = budget.assess(
        estimator().measure(MeasurementInput(messages=[{"role": "user", "content": "hi"}]))
    )
    assert report.capacity == 0 and report.fits is False
    with WireFixture([delta_frame("ok", finish="stop")]) as fixture:
        gate = payload_budget_gate(budget, estimator())
        with pytest.raises(BudgetExceeded):
            run(
                collect(
                    _provider(fixture, gate),
                    ProviderRequest(messages=[{"role": "user", "content": "hi"}]),
                )
            )
        assert fixture.requests == [], "a refused budget must produce zero transport calls"


def test_e01_the_byte_limit_is_checked_independently_of_the_token_estimate() -> None:
    """A request can fit the window and still be too large to send."""
    budget = RequestBudget(
        window=ContextWindow(limit_tokens=1_000_000), max_payload_bytes=200
    )
    with WireFixture([delta_frame("ok", finish="stop")]) as fixture:
        gate = payload_budget_gate(budget, estimator())
        with pytest.raises(BudgetExceeded) as refusal:
            run(
                collect(
                    _provider(fixture, gate),
                    ProviderRequest(
                        messages=[{"role": "user", "content": "字" * 500}],
                        tools=[ProviderToolSpec(name="t", description="d" * 200)],
                    ),
                )
            )
        assert fixture.requests == []
    assert "byte limit" in str(refusal.value)


def test_the_gate_measures_the_same_request_and_the_same_bytes_that_are_sent() -> None:
    """One builder, one truth: the report and the wire body describe the same request."""
    seen: list[Any] = []
    budget = RequestBudget(
        window=ContextWindow(limit_tokens=100_000, output_reserve=100), max_payload_bytes=100_000
    )
    with WireFixture([delta_frame("ok", finish="stop")]) as fixture:
        gate = payload_budget_gate(budget, estimator(), on_report=seen.append)
        provider = _provider(fixture, gate)
        request = ProviderRequest(
            messages=[{"role": "user", "content": "hi"}],
            tools=[ProviderToolSpec(name="read", description="reads a file")],
            max_output_tokens=64,
        )
        # Measuring is pure: at this point nothing has been sent.
        assert fixture.requests == []
        run(collect(provider, request))
        assert len(fixture.requests) == 1

    body = fixture.body_of(0)
    assert len(seen) == 1
    report = seen[0]
    assert report.payload_bytes == len(fixture.first_request()["body"].encode("utf-8"))
    assert report.fits is True
    assert body["tools"][0]["function"]["name"] == "read"
    assert report.estimate.parts["tools"] > 0
    # The request's own output cap is the reserve, and it is in the report.
    assert (report.output_reserve, report.output_reserve_source) == (100, "request")


def test_a_request_with_no_measurable_limit_still_reports_what_it_measured() -> None:
    """No window, no cap: the caller gets numbers and a reason, not a verdict."""
    report = RequestBudget(window=ContextWindow(limit_tokens=None)).assess(
        estimator().measure(MeasurementInput(messages=[{"role": "user", "content": "hi"}]))
    )
    assert report.fits is None
    assert report.estimated_input_tokens is not None
    assert report.estimated_input_tokens > 0
    assert report.payload_limit is None and report.fits_payload is None


def test_an_invalid_budget_is_refused_before_anything_is_measured() -> None:
    with pytest.raises(ContractError):
        RequestBudget(window=ContextWindow(limit_tokens=100), max_payload_bytes=0)
    with pytest.raises(ContractError):
        HeuristicTokenEstimator(chars_per_token=0)
    with pytest.raises(ContractError):
        HeuristicTokenEstimator(per_message_overhead=-1)
