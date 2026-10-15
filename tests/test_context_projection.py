"""C2: dependency-complete, deterministic projections (E04–E07 of the plan).

The projector is exercised through the *real* conversion (`to_llm_messages` →
`to_provider_request`) and the C1 estimator, so what these tests measure is the request
that would be sent. Four claims are under test:

* **E04** — a tool call and its results are one fragment: never split, never a lone
  result, never a fabricated answer for a call that has none;
* **E05** — dropping older fragments never changes the effective instructions or the
  effective tool set: the dropped updates are replayed into one baseline and the result
  is verified, so a revoked tool cannot come back;
* **E06** — the current turn and a host-mandated constraint are required: the projection
  keeps them or refuses, it never quietly sends a different request;
* **E07** — the projection is a pure function of its inputs, it does not touch the
  transcript, it does not de-duplicate identical messages, and it explains itself.

Capacities are *measured*, never guessed: `tokens_of` reports what the real conversion
costs, and `first_fitting_capacity` finds the smallest capacity the projector accepts, so
a test that wants "as tight as possible" does not depend on a hard-coded number drifting
out of date.
"""

from __future__ import annotations

from typing import Any, Sequence

import pytest
from context_helpers import (
    assert_no_orphan_tool_results,
    assistant_call,
    assistant_text,
    real_convert,
    system,
    tool_result,
    user,
)

from yanxu_harness.context import HeuristicTokenEstimator, MeasurementInput, TokenEstimate
from yanxu_harness.context import projection as projection_module
from yanxu_harness.context.projection import (
    ASSISTANT_TURN,
    INSTRUCTIONS,
    ORPHAN_RESULT,
    TOOL_CALL,
    TOOL_ROUNDTRIP,
    USER_TURN,
    FragmentProjector,
    message_fragments,
    system_state,
)
from yanxu_harness.core.types import SystemMessage, message_to_json
from yanxu_harness.errors import BudgetExceeded, ContractError

ROOMY = 1_000_000


def projector(**kwargs: Any) -> FragmentProjector:
    return FragmentProjector(estimator=HeuristicTokenEstimator(), convert=real_convert, **kwargs)


def plan(messages: Sequence[Any], *, capacity: int | None, **kwargs: Any) -> Any:
    return projector().plan(
        messages, scope="project-a", source_head="sha256:head", capacity=capacity, **kwargs
    )


def roles(messages: Sequence[Any]) -> list[str]:
    return [getattr(message, "role", "") for message in messages]


def wire(result: Any) -> Sequence[Any]:
    """The projected view as the transport would express it."""
    return real_convert(result.messages).messages


def tokens_of(messages: Sequence[Any]) -> int:
    estimate = HeuristicTokenEstimator().measure(real_convert(list(messages)))
    assert estimate.tokens is not None, "these transcripts must be measurable"
    return estimate.tokens


def first_fitting_capacity(messages: Sequence[Any], *, mandatory: Sequence[Any] = ()) -> int:
    """The smallest capacity the projector accepts — i.e. the tightest legal projection."""
    for capacity in range(1, tokens_of(messages) + 1):
        try:
            plan(messages, capacity=capacity, mandatory=mandatory)
        except BudgetExceeded:
            continue
        return capacity
    raise AssertionError("no capacity in the range fits this transcript")


# -- E04: fragments ------------------------------------------------------


def test_e04_a_tool_round_trip_is_one_fragment_that_is_never_cut() -> None:
    transcript = [
        system("be brief", added=["read"]),
        user("go"),
        assistant_call("c1", "read", text="looking"),
        tool_result("c1", "ok"),
        user("next"),
    ]
    assert [fragment.kind for fragment in message_fragments(transcript)] == [
        INSTRUCTIONS,
        USER_TURN,
        TOOL_ROUNDTRIP,
        USER_TURN,
    ]

    roomy = plan(transcript, capacity=ROOMY)
    assert roles(roomy.messages) == ["system", "user", "assistant", "toolResult", "user"]
    assert_no_orphan_tool_results(wire(roomy))
    assert roomy.dropped == ()

    # The tightest accepted window drops the round trip whole, never the result alone.
    tight = plan(transcript, capacity=first_fitting_capacity(transcript))
    assert "toolResult" not in roles(tight.messages)
    assert_no_orphan_tool_results(wire(tight))
    assert TOOL_ROUNDTRIP in {fragment.kind for fragment in tight.dropped}
    assert all(fragment.reason for fragment in tight.dropped), "every drop explains itself"

    # With the round trip as the newest turn it is required: too small a window refuses.
    ending_in_a_call = [
        system("be brief"),
        user("go"),
        assistant_call("c1", "read"),
        tool_result("c1", "ok"),
    ]
    with pytest.raises(BudgetExceeded) as refusal:
        plan(ending_in_a_call, capacity=1)
    assert "required" in str(refusal.value)


def test_e04_parallel_results_stay_correlated_and_in_source_order() -> None:
    assistant = assistant_call(
        "c1", "read", text="three reads", extra=[("c2", "read"), ("c3", "read")]
    )
    transcript = [
        system("be brief"),
        user("read three files"),
        assistant,
        tool_result("c1", "one"),
        tool_result("c2", "two"),
        tool_result("c3", "three"),
    ]
    fragments = message_fragments(transcript)
    assert [fragment.kind for fragment in fragments] == [INSTRUCTIONS, USER_TURN, TOOL_ROUNDTRIP]
    round_trip = fragments[-1]
    assert round_trip.declared_call_ids == ("c1", "c2", "c3")
    assert round_trip.answered_call_ids == ("c1", "c2", "c3")
    assert round_trip.open_call_ids == ()

    result = plan(transcript, capacity=ROOMY)
    assert [message["tool_call_id"] for message in wire(result) if message["role"] == "tool"] == [
        "c1",
        "c2",
        "c3",
    ]
    assert_no_orphan_tool_results(wire(result))


def test_e04_an_unanswered_call_is_reported_and_never_answered_for_the_model() -> None:
    transcript = [
        system("be brief"),
        user("go"),
        assistant_call("c1", "read"),
        tool_result("c2", "a result for a call nobody declared"),
    ]
    kinds = [fragment.kind for fragment in message_fragments(transcript)]
    assert kinds == [INSTRUCTIONS, USER_TURN, TOOL_CALL, ORPHAN_RESULT]

    result = plan(transcript, capacity=ROOMY)
    assert result.open_call_ids == ("c1",)
    assert "toolResult" in roles(result.messages), "the orphan result is kept where it was"
    assert len(result.messages) == len(transcript), (
        "no message may be invented to explain an unanswered call or an orphan result"
    )
    assert [id(message) for message in result.messages] == [id(message) for message in transcript]


# -- E05: the effective state is replayed, not merely kept ----------------


def _long_history() -> list[Any]:
    return [
        system("be brief", added=["read"]),
        user("first question " * 20),
        assistant_text("first answer " * 20),
        system("more tools", added=["write"]),
        user("second question " * 20),
        assistant_text("second answer " * 20),
        system(added=["grep"]),
        user("current question"),
    ]


def test_e05_dropped_tool_declarations_are_replayed_into_the_baseline() -> None:
    transcript = _long_history()
    result = plan(transcript, capacity=first_fitting_capacity(transcript))
    assert result.dropped, "the tight window must have dropped something"
    assert result.baseline_synthesized is True
    assert [tool["name"] for tool in system_state(result.messages)["tools"]] == [
        "read",
        "write",
        "grep",
    ]
    assert system_state(result.messages) == system_state(transcript), (
        "dropping older fragments may not change the effective instructions or tools"
    )


def test_e05_a_revoked_tool_does_not_come_back_in_the_baseline() -> None:
    transcript = [
        system("be brief", added=["read", "write"]),
        user("first question " * 20),
        system(removed=["read"]),
        user("second question " * 20),
        assistant_text("an answer " * 20),
        user("current question"),
    ]
    result = plan(transcript, capacity=first_fitting_capacity(transcript))
    assert result.dropped and result.baseline_synthesized is True
    assert [tool["name"] for tool in system_state(result.messages)["tools"]] == ["write"]
    assert system_state(result.messages) == system_state(transcript), (
        "the revoked tool must not be resurrected by dropping the message that removed it"
    )


def test_e05_sections_and_prompt_survive_the_window() -> None:
    transcript = [
        system("base prompt", sections={"voice": "terse"}),
        user("first question " * 20),
        system(sections={"voice": "warm", "goal": "ship it"}),
        user("second question " * 20),
        assistant_text("an answer " * 20),
        user("current question"),
    ]
    result = plan(transcript, capacity=first_fitting_capacity(transcript))
    assert result.dropped and result.baseline_synthesized is True
    assert system_state(result.messages)["sections"] == {"voice": "warm", "goal": "ship it"}
    assert system_state(result.messages)["prompt"] == system_state(transcript)["prompt"]


def test_e05_a_projection_that_would_change_the_effective_state_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The equivalence check is a guard, not a comment: break the baseline and it fires."""
    transcript = [
        system("be brief", added=["read"]),
        user("first question " * 20),
        assistant_text("an answer " * 20),
        user("current question"),
    ]
    monkeypatch.setattr(projection_module, "_baseline_message", lambda _dropped: None)
    with pytest.raises(ContractError) as refusal:
        plan(transcript, capacity=first_fitting_capacity(transcript))
    assert "not equivalent" in str(refusal.value)


# -- E06: what must never be dropped ------------------------------------


def test_e06_the_current_turn_is_kept_or_the_projection_refuses() -> None:
    current = user("this question is far too large for the declared capacity " * 10)
    transcript = [
        system("be brief"),
        user("an older question"),
        assistant_text("an older answer"),
        current,
    ]
    with pytest.raises(BudgetExceeded) as refusal:
        plan(transcript, capacity=first_fitting_capacity(transcript) - 1)
    assert "required" in str(refusal.value)

    roomy = plan(transcript, capacity=ROOMY)
    assert roomy.messages[-1] is current


def test_e06_a_host_mandated_constraint_is_kept_or_the_projection_refuses() -> None:
    constraint = user("The response must stay under 200 words.")
    transcript = [
        system("be brief"),
        constraint,
        user("first question " * 20),
        assistant_text("first answer " * 20),
        user("second question " * 20),
        assistant_text("second answer " * 20),
        user("current question"),
    ]
    kept = plan(transcript, capacity=first_fitting_capacity(transcript, mandatory=[constraint]), mandatory=[constraint])
    assert constraint in kept.messages, "a host-mandated constraint may not be dropped"
    assert system_state(kept.messages) == system_state(transcript)

    # Too small to keep everything from the constraint onwards: refuse rather than drop it.
    with pytest.raises(BudgetExceeded) as refusal:
        plan(transcript, capacity=1, mandatory=[constraint])
    assert "required" in str(refusal.value)


# -- E07: determinism, immutability, provenance --------------------------


def test_e07_the_same_inputs_produce_the_same_projection() -> None:
    transcript = [
        system("be brief", added=["read"]),
        user("first question " * 20),
        assistant_text("an answer " * 20),
        user("current question"),
    ]
    tight = first_fitting_capacity(transcript)
    first = plan(transcript, capacity=tight)
    second = plan(transcript, capacity=tight)
    assert first.request_fingerprint == second.request_fingerprint
    assert first.as_dict() == second.as_dict()
    assert first.effective_state_fingerprint == second.effective_state_fingerprint

    roomier = plan(transcript, capacity=tokens_of(transcript))
    assert roomier.request_fingerprint != first.request_fingerprint


def test_e07_the_transcript_is_never_touched() -> None:
    transcript = [
        system("be brief", added=["read"]),
        user("first question " * 20),
        assistant_text("an answer " * 20),
        user("current question"),
    ]
    before = [message_to_json(message) for message in transcript]
    result = plan(transcript, capacity=first_fitting_capacity(transcript))
    after = [message_to_json(message) for message in transcript]
    assert before == after, "the projection must not rewrite the history it reads"
    assert result.source_fingerprint == plan(transcript, capacity=ROOMY).source_fingerprint

    transcript_ids = {id(message) for message in transcript}
    projected_ids = {id(message) for message in result.messages}
    unexpected = projected_ids - transcript_ids
    assert len(unexpected) == (1 if result.baseline_synthesized else 0), (
        "the only new object in a projection may be the synthesized baseline"
    )
    if result.baseline_synthesized:
        assert isinstance(result.messages[0], SystemMessage)
        assert all(id(message) in transcript_ids for message in result.messages[1:])


def test_e07_identical_messages_are_not_deduplicated() -> None:
    transcript = [
        system("be brief"),
        user("the same words"),
        assistant_text("an answer"),
        user("the same words"),
        assistant_text("an answer"),
        user("the same words"),
    ]
    result = plan(transcript, capacity=ROOMY)
    assert len(result.messages) == len(transcript), "identical text is not a duplicate"
    assert [id(message) for message in result.messages] == [id(message) for message in transcript]

    tight = plan(transcript, capacity=first_fitting_capacity(transcript))
    assert roles(tight.messages)[-1] == "user"
    assert tight.messages[-1] is transcript[-1]


def test_e07_the_projection_carries_its_provenance() -> None:
    transcript = [
        system("be brief", added=["read"]),
        user("first question " * 20),
        assistant_text("an answer " * 20),
        user("current question"),
    ]
    result = plan(transcript, capacity=first_fitting_capacity(transcript))
    payload = result.as_dict()
    assert payload["scope"] == "project-a"
    assert payload["source_head"] == "sha256:head"
    assert payload["policy_version"] == "1"
    assert payload["request_fingerprint"].startswith("sha256:")
    assert payload["source_fingerprint"].startswith("sha256:")
    assert payload["estimate"]["method"] == "heuristic-chars-per-token"
    assert payload["dropped"] and all(entry["reason"] for entry in payload["dropped"])
    assert result.removed_messages == sum(len(entry["positions"]) for entry in payload["dropped"])
    assert result.is_stale("sha256:moved") is True
    assert result.is_stale("sha256:head") is False


# -- boundaries, diagnostics, refusal ------------------------------------


def test_the_callers_conversion_is_what_gets_measured_and_the_cache_is_bounded() -> None:
    transcript = [
        system("be brief"),
        user("first question " * 20),
        assistant_text("an answer " * 20),
        user("current question"),
    ]
    seen: list[int] = []

    def counting_convert(messages: Sequence[Any]) -> MeasurementInput:
        seen.append(len(list(messages)))
        return real_convert(messages)

    subject = FragmentProjector(
        estimator=HeuristicTokenEstimator(), convert=counting_convert, cache_limit=4
    )
    for capacity in range(1, tokens_of(transcript) + 1):
        try:
            subject.plan(transcript, scope="s", source_head="sha256:head", capacity=capacity)
        except BudgetExceeded:
            continue
    assert seen, "the caller's conversion is what gets measured"
    assert len(subject._cache) <= 4, "an unbounded cache is a leak in a long-lived process"

    result = subject.plan(transcript, scope="s", source_head="sha256:head", capacity=ROOMY)
    fresh = HeuristicTokenEstimator().measure(real_convert(result.messages))
    assert result.estimate.tokens == fresh.tokens, (
        "the reported estimate is the estimate of the projection that was returned"
    )


def test_an_unknown_capacity_keeps_everything_and_reports_unknown() -> None:
    transcript = [
        system("be brief"),
        user("first"),
        assistant_text("answer"),
        user("current"),
    ]
    result = plan(transcript, capacity=None)
    assert result.kept_fragments == (0, 1, 2, 3)
    assert result.dropped == ()
    assert result.fits is None
    assert result.capacity is None


def test_an_empty_transcript_projects_to_nothing() -> None:
    result = plan([], capacity=100)
    assert result.messages == ()
    assert result.fits is True
    assert result.kept_fragments == () and result.dropped == ()
    assert result.estimate.tokens == 0


def test_a_projector_refuses_an_unusable_configuration() -> None:
    with pytest.raises(ContractError):
        FragmentProjector(estimator=None, convert=real_convert)  # type: ignore[arg-type]
    with pytest.raises(ContractError):
        FragmentProjector(estimator=HeuristicTokenEstimator(), convert=None)  # type: ignore[arg-type]
    with pytest.raises(ContractError):
        FragmentProjector(estimator=HeuristicTokenEstimator(), convert=real_convert, cache_limit=0)

    transcript = [user("hi")]
    with pytest.raises(ContractError):
        projector().plan(transcript, scope="", source_head="sha256:head", capacity=10)
    with pytest.raises(ContractError):
        projector().plan(transcript, scope="s", source_head="", capacity=10)
    with pytest.raises(ContractError):
        projector().plan(transcript, scope="s", source_head="sha256:head", capacity=-1)


def test_a_measured_projection_reports_the_parts_of_the_real_request() -> None:
    """The estimate of a projection is a real measurement, not a character count."""
    transcript = [
        system("be brief", added=["read"]),
        user("first"),
        assistant_text("answer"),
        user("current question"),
    ]
    result = plan(transcript, capacity=ROOMY)
    assert isinstance(result.estimate, TokenEstimate)
    assert result.estimate.tokens is not None
    assert result.estimate.parts["tools"] > 0, "the tool declarations are part of the request"
    assert result.estimate.parts["system"] > 0
    assert result.fits is True and result.capacity == ROOMY


def test_the_fragment_kinds_are_the_ones_the_reasons_name() -> None:
    transcript = [
        system("be brief"),
        user("go"),
        assistant_text("a plain answer"),
        assistant_call("c1", "read"),
        tool_result("c1", "ok"),
        user("thanks"),
    ]
    assert [fragment.kind for fragment in message_fragments(transcript)] == [
        INSTRUCTIONS,
        USER_TURN,
        ASSISTANT_TURN,
        TOOL_ROUNDTRIP,
        USER_TURN,
    ]
    assert [fragment.positions for fragment in message_fragments(transcript)] == [
        (0,),
        (1,),
        (2,),
        (3, 4),
        (5,),
    ]
