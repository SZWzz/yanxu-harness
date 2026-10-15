"""R1/R2 regression: cancellation propagation and per-run summary budgets.

These tests exist because the original gate was green while both defects were real:

* **R1** — the summary path caught ``asyncio.CancelledError`` and returned the deterministic
  projection, so a run whose *runtime deadline* expired during a summary went on to request
  the main model. An external cancellation is a control signal; it keeps propagating after
  the auxiliary task is collected and its cost accounted for.
* **R2** — the summary allowance lived in one ``SummaryCompactor.usage`` shared by the whole
  deployment, so a second run either lost its own call or reported the first run's spend. The
  budget belongs to one logical run (``namespace + session + run``), and an interrupted run's
  spend still reaches the terminal record.

The direct controller and the real ``DefaultHarness`` are both exercised: the first proves the
library object, the second proves the wiring that decides whether the main model is asked.
"""

from __future__ import annotations

import asyncio
from typing import Any, Mapping, Sequence

import pytest
import test_context_summary as fixture

from yanxu_harness.context import (
    ContextController,
    ContextWindow,
    HeuristicTokenEstimator,
    SummaryBudget,
    SummaryCompactor,
    SummaryResult,
    SummaryUsage,
)
from yanxu_harness.core.types import AssistantMessage, StopReason, TextContent
from yanxu_harness.errors import ContractError
from yanxu_harness.runtime import (
    DefaultHarness,
    Principal,
    RunContext,
    RunIdentity,
    RunRequest,
)

TIGHT = fixture.TIGHT


def run(coro: Any) -> Any:
    async def guard() -> Any:
        return await asyncio.wait_for(coro, timeout=15)

    return asyncio.run(guard())


def controller(
    summarizer: Any,
    *,
    max_calls: int = 1,
    timeout_seconds: float = 5.0,
    window: int = TIGHT,
) -> ContextController:
    return ContextController.compose(
        estimator=HeuristicTokenEstimator(),
        converter=fixture.real_convert,
        window=ContextWindow(limit_tokens=window, output_reserve=16),
        summarizer=summarizer,
        summary_budget=SummaryBudget(max_calls=max_calls, timeout_seconds=timeout_seconds),
    )


def run_context(run_id: str = "r1", attempt_id: str = "a1") -> RunContext:
    return RunContext(
        identity=RunIdentity(
            namespace="n", session_id="s", run_id=run_id, attempt_id=attempt_id
        ),
        principal=Principal(subject="operator"),
    )


class AnswerStream:
    """A main-model stream that counts its invocations and then answers with text."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, *args: Any, **kwargs: Any) -> "AnswerStream":
        self.calls += 1
        return self

    def __aiter__(self) -> "AnswerStream":
        return self

    async def __anext__(self) -> Any:
        raise StopAsyncIteration

    async def result(self) -> AssistantMessage:
        return AssistantMessage(content=[TextContent(text="done")], stop_reason=StopReason.STOP)


class GatedSummarizer:
    """A summarizer that announces it has been entered and waits for a handshake.

    The handshake is what makes these tests deterministic: a cancellation is issued while the
    call is provably in flight, not after a guessed number of milliseconds.
    """

    def __init__(self, *, expected: int = 1, usage: Mapping[str, Any] | None = None) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.entered_count = 0
        self.expected = expected
        self.calls = 0
        self.scopes: list[str] = []
        self.usage = dict(usage if usage is not None else {"input_tokens": 7, "output_tokens": 3})

    async def summarize(
        self, messages: Sequence[Any], *, scope: str, source_head: str
    ) -> SummaryResult:
        self.calls += 1
        self.entered_count += 1
        self.scopes.append(scope)
        if self.entered_count >= self.expected:
            self.entered.set()
        await self.release.wait()
        return SummaryResult(
            text="摘要：一段较早的对话。",
            source_head=source_head,
            scope=scope,
            usage=self.usage,
        )


async def _until(predicate: Any, *, tries: int = 200) -> None:
    """Yield to the loop until a predicate holds; no wall clock is involved."""
    for _ in range(tries):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("the awaited condition never became true")


def _in_flight(compactor: SummaryCompactor, scope: str) -> Mapping[str, Any]:
    state = compactor._runs[scope]  # noqa: SLF001 - the handshake needs the live table
    return state.in_flight


# -- T1: cancelling prepare_async propagates and charges the attempt ----------


def test_t1_cancelling_a_summary_in_flight_propagates_and_accounts_for_the_call() -> None:
    async def scenario() -> None:
        summarizer = GatedSummarizer()
        subject = controller(summarizer)
        task = asyncio.ensure_future(
            subject.prepare_async(fixture._Payload(fixture.history()), scope="n/s/r1")
        )
        await asyncio.wait_for(summarizer.entered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # The auxiliary task was collected, not left running in the background, and a call
        # that was made is not reported as "never happened".
        state_usage = subject.compactor.usage_for("n/s/r1")  # type: ignore[union-attr]
        assert summarizer.calls == 1
        assert state_usage is not None and state_usage.calls == 1
        assert state_usage.usage_unknown is True
        assert subject.compactor.single_flight == {}  # type: ignore[union-attr]

        # No late publication and no free retry: the same run's allowance is spent, so the
        # next preparation falls back instead of calling the model again.
        again = await subject.prepare_async(
            fixture._Payload(fixture.history()), scope="n/s/r1"
        )
        assert summarizer.calls == 1
        assert again["refused"] is None
        assert again["context_state"]["summary"]["used"] is False
        assert again["context_state"]["summary"]["cache_hit"] is False
        assert "budget of 1 call" in again["context_state"]["summary"]["fallback"]

    run(scenario())


# -- T2: a real runtime deadline during a summary ----------------------------


def test_t2_a_runtime_deadline_during_a_summary_never_asks_the_main_model() -> None:
    summarizer = fixture.RecordingSummarizer(delay=3)
    stream = AnswerStream()
    harness = DefaultHarness(
        stream_fn=stream,
        context_controller=controller(summarizer),
        max_runtime_seconds=0.05,
    )
    outcome = run(
        harness.start(
            RunRequest(prompt="finish", messages=fixture.history()), run_context("deadline")
        )
    )

    assert stream.calls == 0, "the expired deadline must not be converted into a model request"
    assert outcome.status.value == "usage_limited"
    assert outcome.error_code == "max_runtime_exceeded"
    assert summarizer.calls == 1
    facts = outcome.context_state or {}
    assert facts["summary"]["calls"] == 1, "the interrupted call is recorded, not forgotten"
    assert facts["summary"]["usage_unknown"] is True
    assert facts["summary"]["used"] is False


# -- T3: a real Harness.cancel during a summary ------------------------------


def test_t3_harness_cancel_during_a_summary_keeps_the_cancellation() -> None:
    summarizer = GatedSummarizer()
    stream = AnswerStream()
    harness = DefaultHarness(
        stream_fn=stream, context_controller=controller(summarizer)
    )

    async def scenario() -> Any:
        task = asyncio.ensure_future(
            harness.start(
                RunRequest(prompt="finish", messages=fixture.history()), run_context("cancel-me")
            )
        )
        await asyncio.wait_for(summarizer.entered.wait(), timeout=2)
        await harness.cancel("cancel-me", "operator changed their mind")
        return await asyncio.wait_for(task, timeout=5)

    outcome = run(scenario())
    assert outcome.status.value == "cancelled"
    assert outcome.error_code == "cancelled"
    assert stream.calls == 0
    assert summarizer.calls == 1
    facts = outcome.context_state or {}
    assert facts["summary"]["calls"] == 1
    assert facts["summary"]["usage_unknown"] is True


# -- T4: a summary-only timeout still falls back -----------------------------


def test_t4_a_summary_only_timeout_falls_back_and_keeps_the_original_messages() -> None:
    summarizer = fixture.RecordingSummarizer(delay=1)
    subject = controller(summarizer, timeout_seconds=0.02)
    prepared = run(
        subject.prepare_async(fixture._Payload(fixture.history()), scope="n/s/run-a")
    )
    assert prepared["refused"] is None, "the parent run is still valid, so this is not a refusal"
    summary = prepared["context_state"]["summary"]
    assert summary["used"] is False
    assert "timed out" in summary["fallback"]
    assert summary["calls"] == 1, "the attempt is spent, not refunded"
    assert summary["usage_unknown"] is True
    assert prepared["view"], "the deterministic projection is still a usable view"


# -- T5: the no-summary fast path is untouched -------------------------------


def test_t5_a_projection_that_fits_never_reaches_the_summarizer() -> None:
    summarizer = fixture.RecordingSummarizer()
    subject = controller(summarizer, window=100_000)
    prepared = run(subject.prepare_async(fixture._Payload(fixture.history()), scope="n/s/r1"))
    assert prepared["refused"] is None
    assert summarizer.calls == 0
    assert prepared["context_state"]["summary"]["calls"] == 0
    assert prepared["context_state"]["summary"]["used"] is False


# -- T6/T7: independent runs do not spend each other's allowance -------------


def test_t6_sequential_runs_each_get_their_own_call_and_their_own_facts() -> None:
    summarizer = fixture.RecordingSummarizer()
    subject = controller(summarizer)
    first = run(subject.prepare_async(fixture._Payload(fixture.history()), scope="n/s/run-a"))
    after_a = summarizer.calls
    second = run(subject.prepare_async(fixture._Payload(fixture.history()), scope="n/s/run-b"))

    assert after_a == 1
    assert summarizer.calls - after_a == 1, "run B gets its own allowance, not run A's leftovers"
    assert first["context_state"]["summary"]["calls"] == 1
    assert second["context_state"]["summary"]["calls"] == 1
    assert second["context_state"]["summary"]["used"] is True
    assert first["context_state"]["summary"]["scope"] == "n/s/run-a"
    assert second["context_state"]["summary"]["scope"] == "n/s/run-b"


def test_t7_concurrent_runs_keep_their_content_scope_and_usage_apart() -> None:
    async def scenario() -> None:
        summarizer = GatedSummarizer(expected=2)
        subject = controller(summarizer, max_calls=1)
        left = asyncio.ensure_future(
            subject.prepare_async(fixture._Payload(fixture.history()), scope="n/s/run-a")
        )
        right = asyncio.ensure_future(
            subject.prepare_async(fixture._Payload(fixture.history()), scope="n/s/run-b")
        )
        await asyncio.wait_for(summarizer.entered.wait(), timeout=2)
        summarizer.release.set()
        a, b = await asyncio.gather(left, right)

        assert summarizer.calls == 2, "two runs, two calls: neither is refused by the other"
        assert sorted(summarizer.scopes) == ["n/s/run-a", "n/s/run-b"]
        assert a["context_state"]["summary"]["calls"] == 1
        assert b["context_state"]["summary"]["calls"] == 1
        assert a["context_state"]["scope"] == "n/s/run-a"
        assert b["context_state"]["scope"] == "n/s/run-b"

    run(scenario())


# -- T8: the next turn, a new attempt and a rebuilt controller ----------------


def test_t8_a_next_turn_and_a_new_attempt_continue_the_recorded_budget() -> None:
    summarizer = fixture.RecordingSummarizer()
    subject = controller(summarizer)
    first = run(subject.prepare_async(fixture._Payload(fixture.history()), scope="n/s/r1"))
    recorded = dict(first["context_state"]["summary"])
    assert recorded["calls"] == 1 and summarizer.calls == 1

    # Same controller, next turn: the committed facts travel back in as ``prior``.
    second = run(
        subject.prepare_async(
            fixture._Payload(fixture.history(newest="a different turn")),
            scope="n/s/r1",
            prior=first["context_state"],
        )
    )
    assert summarizer.calls == 1, "a spent allowance is not refreshed by the next turn"
    assert second["context_state"]["summary"]["calls"] == 1
    assert "budget of 1 call" in second["context_state"]["summary"]["fallback"]

    # A rebuilt controller (a restarted process), handed the same committed facts, behaves
    # identically: the numbers are the record's, not the object's.
    fresh_summarizer = fixture.RecordingSummarizer()
    rebuilt = controller(fresh_summarizer)
    third = run(
        rebuilt.prepare_async(
            fixture._Payload(fixture.history(newest="a new attempt")),
            scope="n/s/r1",
            prior=first["context_state"],
        )
    )
    assert fresh_summarizer.calls == 0
    assert third["context_state"]["summary"]["calls"] == 1


# -- T9: high-count and low-count runs restored interleaved ------------------


def test_t9_interleaved_restores_never_take_a_cross_run_maximum() -> None:
    subject = fixture.compactor(fixture.RecordingSummarizer(), budget=SummaryBudget(max_calls=5))
    assert subject.restore({"summary": {"calls": 4, "accounted_head": "h-a"}}, scope="n/s/run-a")
    subject.restore({"summary": {"calls": 0}}, scope="n/s/run-b")
    assert subject.usage_for("n/s/run-a").calls == 4  # type: ignore[union-attr]
    assert subject.usage_for("n/s/run-b").calls == 0  # type: ignore[union-attr]

    # An older prior of the *same* run must not rewind what it already spent.
    assert subject.restore({"summary": {"calls": 1}}, scope="n/s/run-a") is False
    assert subject.usage_for("n/s/run-a").calls == 4  # type: ignore[union-attr]
    # And run B's small count is not compared with run A's large one.
    assert subject.usage_for("n/s/run-b").calls == 0  # type: ignore[union-attr]


# -- T10: same run, different sources, one call -------------------------------


def test_t10_one_run_with_two_sources_starts_at_most_one_call() -> None:
    async def scenario() -> None:
        summarizer = GatedSummarizer()
        subject = controller(summarizer, max_calls=1)
        first = asyncio.ensure_future(
            subject.prepare_async(fixture._Payload(fixture.history()), scope="n/s/r1")
        )
        await asyncio.wait_for(summarizer.entered.wait(), timeout=2)
        second = asyncio.ensure_future(
            subject.prepare_async(
                fixture._Payload(fixture.history(newest="a different instruction")),
                scope="n/s/r1",
            )
        )
        await _until(lambda: subject.compactor._runs["n/s/r1"].reserved_calls == 1)  # noqa: SLF001
        summarizer.release.set()
        a, b = await asyncio.gather(first, second)

        assert summarizer.calls == 1, "max_calls=1 means one external call for the run"
        outcomes = [a["context_state"]["summary"], b["context_state"]["summary"]]
        assert sum(1 for entry in outcomes if entry["used"] is True) == 1
        assert sum(1 for entry in outcomes if "budget of 1 call" in entry["fallback"]) == 1
        assert all(entry["calls"] == 1 for entry in outcomes), (
            "the refused preparation is not charged a phantom second call, and the caller is "
            "not told the call was never made"
        )

    run(scenario())


# -- T11: same run, same source, one waiter cancels --------------------------


def test_t11_cancelling_one_waiter_leaves_the_shared_call_for_the_others() -> None:
    async def scenario() -> None:
        summarizer = GatedSummarizer()
        subject = controller(summarizer, max_calls=1)
        messages = fixture.history()
        owner = asyncio.ensure_future(
            subject.prepare_async(fixture._Payload(messages), scope="n/s/r1")
        )
        await asyncio.wait_for(summarizer.entered.wait(), timeout=2)
        waiter = asyncio.ensure_future(
            subject.prepare_async(fixture._Payload(messages), scope="n/s/r1")
        )
        await _until(
            lambda: any(call.waiters == 2 for call in _in_flight(subject.compactor, "n/s/r1").values())  # type: ignore[arg-type]
        )

        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner

        summarizer.release.set()
        survivor = await asyncio.wait_for(waiter, timeout=5)
        assert summarizer.calls == 1, "the surviving waiter reused the call, it did not start one"
        assert survivor["context_state"]["summary"]["calls"] == 1
        state = subject.compactor._runs["n/s/r1"]  # noqa: SLF001
        assert state.usage.calls == 1, "one shared call settles exactly once"
        assert state.reserved_calls == 0
        assert subject.compactor.single_flight == {}  # type: ignore[union-attr]

    run(scenario())


def test_t11_a_run_cancellation_collects_the_shared_call() -> None:
    async def scenario() -> None:
        summarizer = GatedSummarizer()
        subject = controller(summarizer, max_calls=4)
        messages = fixture.history()
        first = asyncio.ensure_future(
            subject.prepare_async(fixture._Payload(messages), scope="n/s/r1")
        )
        await asyncio.wait_for(summarizer.entered.wait(), timeout=2)
        second = asyncio.ensure_future(
            subject.prepare_async(fixture._Payload(messages), scope="n/s/r1")
        )
        await _until(
            lambda: any(call.waiters == 2 for call in _in_flight(subject.compactor, "n/s/r1").values())  # type: ignore[arg-type]
        )
        first.cancel()
        second.cancel()
        for task in (first, second):
            with pytest.raises(asyncio.CancelledError):
                await task
        assert subject.compactor.single_flight == {}  # type: ignore[union-attr]
        state = subject.compactor._runs["n/s/r1"]  # noqa: SLF001
        assert state.reserved_calls == 0
        assert state.usage.calls == 1, "the one shared call is accounted for once"

    run(scenario())


# -- T12: cache hits, released state and no cross-scope leakage --------------


def test_t12_releasing_a_run_does_not_refund_its_budget() -> None:
    summarizer = fixture.RecordingSummarizer()
    subject = controller(summarizer)
    messages = fixture.history()
    first = run(subject.prepare_async(fixture._Payload(messages), scope="n/s/run-a"))
    assert first["context_state"]["summary"]["calls"] == 1

    # A cache hit for the identical source range costs nothing and is not a second call.
    cached = run(subject.prepare_async(fixture._Payload(messages), scope="n/s/run-a"))
    assert summarizer.calls == 1
    assert cached["context_state"]["summary"]["cache_hit"] is True

    assert subject.compactor.release_run("n/s/run-a") is True  # type: ignore[union-attr]
    assert subject.compactor.usage_for("n/s/run-a") is None  # type: ignore[union-attr]

    # Re-entering with the recorded facts restores the spend; releasing memory is not a reset.
    again = run(
        subject.prepare_async(
            fixture._Payload(fixture.history(newest="a later turn")),
            scope="n/s/run-a",
            prior=first["context_state"],
        )
    )
    assert summarizer.calls == 1
    assert again["context_state"]["summary"]["calls"] == 1
    assert "budget of 1 call" in again["context_state"]["summary"]["fallback"]

    # Another scope still has its own untouched allowance.
    other = run(subject.prepare_async(fixture._Payload(fixture.history()), scope="n/s/run-b"))
    assert summarizer.calls == 2
    assert other["context_state"]["summary"]["calls"] == 1


# -- T13: legacy priors, wrong identities and unknown versions ---------------


def test_t13_the_legacy_single_run_restore_still_works() -> None:
    summarizer = fixture.RecordingSummarizer()
    subject = fixture.compactor(summarizer, budget=SummaryBudget(max_calls=1))
    recorded = SummaryUsage(calls=1, accounted_head="sha256:head").as_dict()
    assert subject.restore({"summary": recorded}) is True
    assert subject.usage.calls == 1
    result = fixture.compact(fixture.history(newest="a later turn"), subject)
    assert summarizer.calls == 0
    assert "budget of 1 call" in result.fallback


def test_t13_an_id_mismatch_or_unknown_version_is_refused_not_merged() -> None:
    summarizer = fixture.RecordingSummarizer()
    subject = controller(summarizer)
    with pytest.raises(ContractError):
        subject.compactor.restore(  # type: ignore[union-attr]
            {"summary": {"calls": 1, "scope": "n/s/other-run"}}, scope="n/s/run-a"
        )
    with pytest.raises(ContractError):
        subject.compactor.restore(  # type: ignore[union-attr]
            {"summary": {"calls": 1, "summary_schema_version": 99}}, scope="n/s/run-a"
        )
    # A legacy prior with no identity is bound by the host record: it is accepted for the
    # scope it is restored into, because the caller already checked which run it belongs to.
    assert subject.compactor.restore(  # type: ignore[union-attr]
        {"summary": {"calls": 1, "accounted_head": "h"}}, scope="n/s/run-a"
    ) is True


def test_t13_an_ambiguous_legacy_restore_is_refused() -> None:
    subject = fixture.compactor(fixture.RecordingSummarizer(), budget=SummaryBudget(max_calls=5))
    assert subject.restore({"summary": {"calls": 1}}, scope="n/s/run-a")
    assert subject.restore({"summary": {"calls": 1}}, scope="n/s/run-b")
    with pytest.raises(ContractError):
        subject.restore({"summary": {"calls": 1}})
    with pytest.raises(ContractError):
        subject.usage


# -- T14: charged before admission and after -------------------------------


def test_t14_a_refusal_before_admission_is_not_charged() -> None:
    summarizer = fixture.RecordingSummarizer()
    subject = controller(summarizer, max_calls=0)
    prepared = run(subject.prepare_async(fixture._Payload(fixture.history()), scope="n/s/r1"))
    assert summarizer.calls == 0
    summary = prepared["context_state"]["summary"]
    assert summary["calls"] == 0, "a call that was never made is not billed"
    assert summary["used"] is False
    assert "budget of 0 call(s)" in summary["fallback"]


def test_t14_a_call_started_before_cancellation_is_still_charged() -> None:
    async def scenario() -> None:
        summarizer = GatedSummarizer()
        subject = controller(summarizer)
        task = asyncio.ensure_future(
            subject.prepare_async(fixture._Payload(fixture.history()), scope="n/s/r1")
        )
        await asyncio.wait_for(summarizer.entered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The memory the harness would read while unwinding and the eventually settled usage
        # agree: one call, unconfirmed cost.
        reported = subject.compactor.reported_usage_for("n/s/r1")  # type: ignore[union-attr]
        settled = subject.compactor.usage_for("n/s/r1")  # type: ignore[union-attr]
        assert reported.calls == 1 and settled.calls == 1
        assert reported.usage_unknown is True and settled.usage_unknown is True

    run(scenario())
