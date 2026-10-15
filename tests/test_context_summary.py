"""C5: bounded summaries — optional, budgeted, strictly published (E13/E14).

Four claims, each of which was a decision rather than an implementation detail:

* **off means off** — with no summarizer configured no auxiliary request is made, and neither
  is one made when the deterministic projection already keeps everything;
* **the budget is absolute and durable** — a resume continues the recorded spend instead of
  getting a fresh allowance, and a call whose cost was not confirmed stays `usage_unknown`
  rather than becoming zero;
* **a publication must still describe the same head** — a summary produced for a moved or
  differently-scoped transcript is refused, and the refusal falls back to the deterministic
  view rather than being published anyway;
* **failures keep the original** — timeout, cancellation, exception and an unusable answer all
  leave the messages untouched and say why.

The summary also never gains system authority: it travels as a user message, so the effective
instruction and tool state stay exactly what the transcript says (plan section 3.4).
"""

from __future__ import annotations

import asyncio
from typing import Any, Mapping, Sequence

import pytest
from context_helpers import assistant_call, real_convert, system, tool_result, user

from yanxu_harness.context import (
    CompactionResult,
    ContextController,
    ContextWindow,
    HeuristicTokenEstimator,
    SummaryBudget,
    SummaryCompactor,
    SummaryResult,
    SummaryUsage,
    UnknownPolicy,
)
from yanxu_harness.context.projection import FragmentProjector, system_state
from yanxu_harness.context.policy import SummaryArtifact, validate_publication
from yanxu_harness.errors import ContractError

#: The capacity at which the deterministic projection keeps the tail but drops history.
TIGHT = 260


def run(coro: Any) -> Any:
    async def guard() -> Any:
        return await asyncio.wait_for(coro, timeout=15)

    return asyncio.run(guard())


class RecordingSummarizer:
    """A host adapter: it counts calls, records what it was asked to summarize, and can fail."""

    def __init__(
        self,
        text: str = "摘要：用户连续问了几轮，read 返回了结果。",
        *,
        usage: Mapping[str, Any] | None = None,
        failure: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        self.text = text
        self.usage = dict(usage if usage is not None else {"input_tokens": 30, "output_tokens": 12})
        self.failure = failure
        self.delay = delay
        self.calls = 0
        self.seen: list[list[Any]] = []
        self.scopes: list[str] = []

    async def summarize(
        self, messages: Sequence[Any], *, scope: str, source_head: str
    ) -> SummaryResult:
        self.calls += 1
        self.seen.append(list(messages))
        self.scopes.append(scope)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.failure is not None:
            raise self.failure
        return SummaryResult(
            text=self.text, source_head=source_head, scope=scope, usage=self.usage
        )


def history(turns: int = 6, newest: str = "final question") -> list[Any]:
    messages: list[Any] = [system("be brief", added=["read"])]
    for index in range(turns):
        messages.append(user(f"question {index} " * 40))
        messages.append(assistant_call(f"c{index}", "read"))
        messages.append(tool_result(f"c{index}", f"result {index} " * 40))
    messages.append(user(newest))
    return messages


def projector() -> FragmentProjector:
    return FragmentProjector(estimator=HeuristicTokenEstimator(), convert=real_convert)


def compactor(summarizer: Any = None, **kwargs: Any) -> SummaryCompactor:
    return SummaryCompactor(projector=projector(), summarizer=summarizer, **kwargs)


def compact(messages: Sequence[Any], subject: SummaryCompactor, *, capacity: int = TIGHT, **kwargs: Any) -> CompactionResult:
    return run(subject.compact(messages, scope="project-a/s1/r1", source_head="sha256:head", capacity=capacity, **kwargs))


# -- E13: off by default, budgeted when on ------------------------------


def test_e13_with_no_summarizer_the_deterministic_projection_answers() -> None:
    messages = history()
    result = compact(messages, compactor())
    assert result.used_summary is False
    assert result.fallback == "no summarizer is configured for this run"
    assert result.projection.dropped, "the window did drop history: that is why a summary was considered"
    assert result.messages == result.projection.messages


def test_e13_a_projection_that_keeps_everything_never_calls_the_summarizer() -> None:
    summarizer = RecordingSummarizer()
    roomy = compact(history(), compactor(summarizer), capacity=100_000)
    assert summarizer.calls == 0, "nothing was dropped, so nothing needed summarizing"
    assert roomy.used_summary is False

    # An unknown capacity is not a reason to call a model either.
    unknown = compact(history(), compactor(summarizer), capacity=None)
    assert summarizer.calls == 0
    assert unknown.used_summary is False


def test_e13_the_summary_replaces_a_stable_prefix_and_keeps_the_tail_verbatim() -> None:
    summarizer = RecordingSummarizer()
    subject = compactor(summarizer)
    messages = history(newest="this question must survive verbatim")
    result = compact(messages, subject)

    assert result.used_summary is True
    assert summarizer.calls == 1
    assert result.fits is True and result.estimate.tokens <= TIGHT
    assert summarizer.scopes == ["project-a/s1/r1"], "the auxiliary call is isolated by scope"

    # The newest user turn is in the view unchanged, and so is the round trip before it.
    assert result.messages[-1] is messages[-1]
    assert "this question must survive verbatim" in str(getattr(result.messages[-1], "content", ""))
    roles = [getattr(message, "role", None) for message in result.messages]
    assert roles[-3:] == ["assistant", "toolResult", "user"]
    # The summary is *material*, not instructions: the effective state is unchanged.
    assert system_state(result.messages) == system_state(messages)
    assert all(
        getattr(message, "role", None) != "system" or "摘要" not in str(getattr(message, "content", ""))
        for message in result.messages
    ), "a summary must never enter the system prompt"


def test_e13_the_summary_retains_more_history_than_dropping_alone() -> None:
    """The point of summarizing: the same window, more of the conversation still reachable."""
    messages = history()
    with_summary = compact(messages, compactor(RecordingSummarizer()), capacity=320)
    without = compact(messages, compactor(), capacity=320)
    assert with_summary.used_summary is True
    assert with_summary.estimate.tokens < without.estimate.tokens, (
        "the summarized view is smaller *and* carries a description of the dropped turns"
    )
    summary_text = [
        str(getattr(message, "content", ""))
        for message in with_summary.messages
        if "摘要" in str(getattr(message, "content", ""))
    ]
    assert summary_text, "the dropped turns are still described, which is the whole point"
    assert not any(
        "摘要" in str(getattr(message, "content", "")) for message in without.messages
    ), "the deterministic view has no idea what it dropped"


def test_e13_usage_is_recorded_separately_and_missing_cost_is_unknown() -> None:
    reporting = RecordingSummarizer(usage={"input_tokens": 41, "output_tokens": 7})
    subject = compactor(reporting)
    compact(history(), subject)
    assert subject.usage.calls == 1
    assert subject.usage.input_chars > 0 and subject.usage.output_chars > 0
    assert subject.usage.model_usage == {"input_tokens": 41, "output_tokens": 7}
    assert subject.usage.usage_unknown is False

    silent = RecordingSummarizer(usage={})
    silent_subject = compactor(silent)
    compact(history(), silent_subject)
    assert silent_subject.usage.usage_unknown is True, (
        "a summarizer that reports nothing is unknown, never free"
    )
    assert silent_subject.usage.as_dict()["usage_unknown"] is True


def test_e13_the_call_budget_is_absolute_and_the_cache_prevents_a_second_bill() -> None:
    summarizer = RecordingSummarizer()
    subject = compactor(summarizer, budget=SummaryBudget(max_calls=1))
    messages = history()

    first = compact(messages, subject)
    assert first.used_summary is True and summarizer.calls == 1
    assert first.cache_hit is False

    # Same stable prefix again: served from the cache, no second call.
    again = compact(messages, subject)
    assert summarizer.calls == 1, "the cache answered the identical request"
    assert again.cache_hit is True
    assert again.used_summary is True and again.summary_text == first.summary_text

    # A different prefix with the budget spent: no call, and a recorded reason.
    other = compact(history(newest="a brand new instruction"), subject)
    assert summarizer.calls == 1
    assert other.used_summary is False
    assert "budget of 1 call" in other.fallback
    assert other.messages == other.projection.messages


def test_e13_an_input_over_the_character_limit_is_refused_before_the_call() -> None:
    summarizer = RecordingSummarizer()
    subject = compactor(summarizer, budget=SummaryBudget(max_calls=3, max_input_chars=200))
    result = compact(history(), subject)
    assert summarizer.calls == 0, "the limit is checked before spending anything"
    assert result.used_summary is False
    assert "could not be proven" in result.fallback or "failed" in result.fallback


def test_e13_a_summary_longer_than_the_output_limit_is_not_used() -> None:
    summarizer = RecordingSummarizer(text="x" * 200)
    subject = compactor(summarizer, budget=SummaryBudget(max_calls=1, max_output_chars=100))
    result = compact(history(), subject)
    assert summarizer.calls == 1, "the call happened, and its cost is recorded"
    assert subject.usage.calls == 1
    assert result.used_summary is False
    assert "over the 100 character limit" in result.fallback


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("the vendor is down"),
        asyncio.TimeoutError(),
    ],
)
def test_e13_a_failed_or_timed_out_summary_keeps_the_original_data(failure: Exception) -> None:
    summarizer = RecordingSummarizer(failure=failure)
    subject = compactor(summarizer, budget=SummaryBudget(max_calls=2))
    messages = history()
    deterministic = compact(messages, compactor(), capacity=TIGHT)
    result = compact(messages, subject)

    assert result.used_summary is False
    assert result.messages == deterministic.messages, "the deterministic answer is the fallback"
    assert result.fallback and ("timed out" in result.fallback or "failed" in result.fallback)
    # The attempts are still accounted for: the budget is spent even though nothing was used.
    assert subject.usage.calls == 1


def test_e13_a_real_timeout_is_bounded() -> None:
    summarizer = RecordingSummarizer(delay=5.0)
    subject = compactor(summarizer, budget=SummaryBudget(max_calls=1, timeout_seconds=0.05))
    result = compact(history(), subject)
    assert result.used_summary is False
    assert "timed out" in result.fallback
    assert subject.usage.calls == 1


def test_e13_an_empty_answer_is_refused() -> None:
    summarizer = RecordingSummarizer(text="   ")
    result = compact(history(), compactor(summarizer))
    assert result.used_summary is False
    assert "nothing usable" in result.fallback


def test_e13_a_summarizer_must_return_a_summary_result() -> None:
    class Sloppy:
        async def summarize(self, messages: Sequence[Any], *, scope: str, source_head: str) -> str:
            return "just text"

    result = compact(history(), compactor(Sloppy()))
    assert result.used_summary is False
    assert "SummaryResult" in result.fallback


def test_e13_an_invalid_budget_or_configuration_is_refused() -> None:
    with pytest.raises(ContractError):
        SummaryBudget(max_calls=-1)
    with pytest.raises(ContractError):
        SummaryBudget(max_output_chars=0)
    with pytest.raises(ContractError):
        SummaryBudget(timeout_seconds=0)
    with pytest.raises(ContractError):
        compactor(RecordingSummarizer(), keep_verbatim=0)
    with pytest.raises(ContractError):
        compactor(RecordingSummarizer(), cache_limit=0)


# -- E14: publication is strict ----------------------------------------


def test_e14_a_summary_for_a_moved_head_is_refused() -> None:
    artifact = SummaryArtifact(
        text="something older", scope="project-a", source_head="sha256:old", base_fingerprint="sha256:base"
    )
    allowed, why = validate_publication(artifact, scope="project-a", source_head="sha256:new")
    assert allowed is False and "head" in why
    allowed, why = validate_publication(artifact, scope="other", source_head="sha256:old")
    assert allowed is False and "scope" in why
    allowed, why = validate_publication(
        artifact, scope="project-a", source_head="sha256:old", lease_token="lease-2"
    )
    # The artifact carries no lease: a lease-gated publication is refused, not assumed.
    assert allowed is False and "lease" in why
    # An empty summary is refused at publication time rather than at construction: the
    # artifact is data, and `validate_publication` is the one gate that may reject it.
    empty = SummaryArtifact(text="  ", scope="s", source_head="h", base_fingerprint="b")
    assert validate_publication(empty, scope="s", source_head="h")[0] is False


def test_e14_a_publication_is_refused_when_the_transcript_moved_under_it() -> None:
    """The check that matters in practice: newer input must not be overwritten."""
    messages = history()
    summarizer = RecordingSummarizer()

    class Racing:
        """A summarizer that lets a newer turn arrive while it works."""

        def __init__(self, inner: RecordingSummarizer) -> None:
            self.inner = inner
            self.arrived: list[Any] | None = None

        async def summarize(self, prefix: Sequence[Any], *, scope: str, source_head: str) -> SummaryResult:
            self.arrived = history(newest="a newer instruction arrived mid-summary")
            return await self.inner.summarize(prefix, scope=scope, source_head=source_head)

    racing = Racing(summarizer)
    subject = compactor(racing)
    result = compact(messages, subject)
    # Simulating "the head moved" the way the projector sees it: the transcript handed to the
    # publish step no longer matches the one the summary was derived from.
    stale = subject._publish(  # noqa: SLF001 - exercising the guard directly is the test
        result.projection,
        list(racing.arrived or []),
        messages[:3],
        "a summary of the old history",
        "sha256:window",
        cache_hit=False,
    )
    assert stale.used_summary is False
    assert "transcript moved" in stale.fallback or "not equivalent" in stale.fallback or stale.fallback


def test_e14_the_summary_records_its_source_range_and_the_budget_it_used() -> None:
    controller = ContextController.compose(
        estimator=HeuristicTokenEstimator(),
        converter=real_convert,
        window=ContextWindow(limit_tokens=TIGHT, output_reserve=16),
        summarizer=RecordingSummarizer(),
        summary_budget=SummaryBudget(max_calls=1),
    )
    prepared = run(controller.prepare_async(_Payload(history()), scope="project-a/s1/r1"))
    assert prepared["refused"] is None
    facts = prepared["context_state"]
    summary = facts["summary"]
    assert summary["used"] is True
    assert summary["calls"] == 1
    assert summary["model_usage"] == {"input_tokens": 30, "output_tokens": 12}
    assert summary["usage_unknown"] is False
    assert summary["budget"]["max_calls"] == 1
    assert summary["accounted_head"] == facts["source_head"]
    assert facts["baseline_synthesized"] is True, "the replayed baseline is in the view too"


class _Payload:
    def __init__(self, messages: Sequence[Any]) -> None:
        self.messages = tuple(messages)
        self.model = None
        self.turn = 1
        self.thinking_level = None
        self.tools = ()
        self.signal = None


def test_e14_a_resume_continues_the_recorded_budget_instead_of_refreshing_it() -> None:
    """The durability rule: a spent allowance stays spent across attempts."""
    summarizer = RecordingSummarizer()
    subject = compactor(summarizer, budget=SummaryBudget(max_calls=1))
    messages = history()
    first = compact(messages, subject)
    assert first.used_summary is True and summarizer.calls == 1

    recorded = dict(subject.usage.as_dict())
    # A new process: a fresh compactor with the same budget, handed the committed facts.
    resumed_summarizer = RecordingSummarizer()
    resumed = compactor(resumed_summarizer, budget=SummaryBudget(max_calls=1))
    assert resumed.restore({"summary": recorded}) is True
    assert resumed.usage.calls == 1
    result = compact(history(newest="the resumed attempt asks something else"), resumed)
    assert resumed_summarizer.calls == 0, "the allowance was not refreshed by restarting"
    assert result.used_summary is False
    assert "budget of 1 call" in result.fallback

    # Restoring twice is idempotent, and a lower recorded count never rewinds the ledger.
    assert resumed.restore({"summary": recorded}) is False
    assert resumed.restore({"summary": {"calls": 0}}) is False
    assert resumed.usage.calls == 1


def test_e14_an_unconfirmed_cost_survives_a_restore() -> None:
    subject = compactor(RecordingSummarizer(usage={}), budget=SummaryBudget(max_calls=2))
    compact(history(), subject)
    assert subject.usage.usage_unknown is True
    restored = SummaryUsage.from_record(subject.usage.as_dict())
    assert restored.usage_unknown is True, "a resume must not turn an unknown cost into zero"
    assert restored.calls == 1


def test_e14_a_record_without_summary_fields_reads_as_nothing_spent() -> None:
    usage = SummaryUsage.from_record(None)
    assert usage.calls == 0 and usage.usage_unknown is False
    assert SummaryUsage.from_record({}).calls == 0


def test_e14_the_controller_needs_a_summarizer_to_summarize_and_a_strict_window_can_refuse() -> None:
    strict = ContextController.compose(
        estimator=HeuristicTokenEstimator(),
        converter=real_convert,
        window=ContextWindow(limit_tokens=None),
        summarizer=RecordingSummarizer(),
        on_unknown=UnknownPolicy.REFUSE,
    )
    prepared = run(strict.prepare_async(_Payload(history()), scope="project-a/s1/r1"))
    assert prepared["refused"] is not None
    assert prepared["refused"]["error_code"] == "context_budget_exceeded"
    assert "proven to fit" in prepared["refused"]["detail"]
