"""The model-backed router: budget, cache, fallback, and separately checked ids."""

from __future__ import annotations

import asyncio

import pytest

from yanxu_harness.decisions import (
    DecisionBudget,
    DecisionCache,
    DecisionCandidate,
    DecisionKind,
    DecisionOutcome,
    DecisionRequest,
    DecisionStatus,
    DecisionSuggestion,
    ModelDecisionRouter,
    NoopDecisionRouter,
    build_payload,
)
from yanxu_harness.errors import BudgetExceeded, ContractError
from yanxu_harness.runtime import Cancellation
from yanxu_harness.versioning import canonical_json


def run(coro):
    return asyncio.run(coro)


def request(kind: DecisionKind = DecisionKind.TOOL, ids: tuple[str, ...] = ("a", "b"), **overrides) -> DecisionRequest:
    values = {
        "task": "choose something",
        "candidates": tuple(
            DecisionCandidate(candidate_id=name, kind=kind, description=f"{name} does a thing", version="1")
            for name in ids
        ),
        "kind": kind,
        "max_suggestions": 2,
        "scope": "project-a",
    }
    values.update(overrides)
    return DecisionRequest(**values)  # type: ignore[arg-type]


class Recorder:
    """A stand-in for the host's model call, with a scripted reply."""

    def __init__(self, reply=None, *, raises: BaseException | None = None, delay: float = 0.0) -> None:
        self.reply = reply
        self.raises = raises
        self.delay = delay
        self.payloads: list[dict] = []

    async def __call__(self, payload):
        self.payloads.append(dict(payload))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raises is not None:
            raise self.raises
        return self.reply


def reply(*ids: str, kind: str = "tool") -> dict:
    return {
        "suggestions": [
            {"candidate_id": candidate_id, "kind": kind, "rank": index, "score": 0.9 - index * 0.1}
            for index, candidate_id in enumerate(ids)
        ]
    }


def test_a_valid_reply_becomes_an_ok_decision() -> None:
    call = Recorder(reply("b", "a"))
    router = ModelDecisionRouter(call)
    outcome = run(router.route(request()))
    assert outcome.status is DecisionStatus.OK
    assert outcome.ids() == ("b", "a")
    assert outcome.usage.calls == 1
    assert router.calls == 1


def test_an_empty_reply_is_absent_not_ok() -> None:
    router = ModelDecisionRouter(Recorder({"suggestions": []}))
    outcome = run(router.route(request()))
    assert outcome.status is DecisionStatus.ABSENT
    assert outcome.suggestions == ()


def test_the_host_call_receives_only_bounded_candidate_metadata() -> None:
    call = Recorder(reply("a"))
    run(ModelDecisionRouter(call).route(request()))
    payload = call.payloads[0]
    assert set(payload) == {"requests"}
    entry = payload["requests"][0]
    assert set(entry) == {"kind", "task", "scope", "max_suggestions", "candidates"}
    assert set(entry["candidates"][0]) == {"id", "version", "description"}
    serialized = canonical_json(payload)
    for banned in ("transcript", "api_key", "secret", "body"):
        assert banned not in serialized


def test_the_call_budget_is_enforced_and_degrades_instead_of_raising() -> None:
    call = Recorder(reply("a"))
    router = ModelDecisionRouter(call, budget=DecisionBudget(max_calls_per_run=1))
    assert run(router.route(request())).status is DecisionStatus.OK
    second = run(router.route(request(task="a different task")))
    assert second.status is DecisionStatus.BUDGET_EXCEEDED
    assert "budget" in (second.failure or "")
    assert len(call.payloads) == 1


def test_an_oversized_payload_is_never_dispatched() -> None:
    call = Recorder(reply("a"))
    router = ModelDecisionRouter(call, budget=DecisionBudget(max_input_chars=200))
    outcome = run(router.route(request()))
    assert outcome.status is DecisionStatus.BUDGET_EXCEEDED
    assert call.payloads == []


def test_a_slow_call_degrades_with_a_reason() -> None:
    ticks = iter([0.0, 5.0])

    def clock() -> float:
        return next(ticks)

    router = ModelDecisionRouter(Recorder(reply("a")), budget=DecisionBudget(max_seconds=1.0), clock=clock)
    outcome = run(router.route(request()))
    assert outcome.status is DecisionStatus.DEGRADED
    assert "took" in (outcome.failure or "")


def test_a_host_failure_degrades_and_never_looks_like_a_choice() -> None:
    router = ModelDecisionRouter(Recorder(raises=RuntimeError("provider said no")))
    outcome = run(router.route(request()))
    assert outcome.status is DecisionStatus.DEGRADED
    assert "provider said no" in (outcome.failure or "")


def test_a_host_budget_stop_is_reported_as_a_budget_stop() -> None:
    router = ModelDecisionRouter(Recorder(raises=BudgetExceeded("decision tokens")))
    outcome = run(router.route(request()))
    assert outcome.status is DecisionStatus.BUDGET_EXCEEDED


def test_a_cancelled_run_never_dispatches() -> None:
    call = Recorder(reply("a"))
    token = Cancellation()
    token.cancel("author stopped")
    outcome = run(ModelDecisionRouter(call).route(request(), cancellation=token))
    assert outcome.status is DecisionStatus.CANCELLED
    assert call.payloads == []


def test_a_cancelled_run_is_never_answered_from_the_cache() -> None:
    """A cache hit must not let a cancelled run skip the cancellation check.

    The entry only proves *what the model said earlier*; it says nothing about
    whether this run is still allowed to use it. So the cancellation check has
    to happen before the cache lookup, not after.
    """

    call = Recorder(reply("a"))
    router = ModelDecisionRouter(call)
    run(router.route(request()))
    assert len(call.payloads) == 1

    token = Cancellation()
    token.cancel("author stopped")
    outcome = run(router.route(request(), cancellation=token))

    assert outcome.status is DecisionStatus.CANCELLED, "a cancelled run got a cached answer"
    assert call.payloads == [call.payloads[0]], "a cancelled run dispatched again"


def test_the_cache_expires_entries_after_its_ttl() -> None:
    now = [1000.0]
    call = Recorder(reply("a"))
    router = ModelDecisionRouter(
        call,
        budget=DecisionBudget(max_calls_per_run=2),
        cache=DecisionCache(ttl_seconds=30.0, clock=lambda: now[0]),
    )

    run(router.route(request()))
    now[0] += 29.0
    fresh = run(router.route(request()))
    assert fresh.usage.calls == 0, "an entry inside its ttl must be reused"
    assert router.cache.hits == 1

    now[0] += 2.0
    stale = run(router.route(request()))
    assert stale.usage.calls == 1, "an entry past its ttl must be asked again"
    assert router.calls == 2, "an expired entry must not count as a hit"


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        ("not a mapping", "not an object"),
        ({"nope": []}, "no 'suggestions' key"),
        ({"suggestions": "a"}, "must be a list"),
    ],
)
def test_unparseable_output_degrades(raw, fragment: str) -> None:
    router = ModelDecisionRouter(Recorder(raw))
    outcome = run(router.route(request()))
    assert outcome.status is DecisionStatus.DEGRADED
    assert fragment in (outcome.failure or "")


def test_invented_ids_are_rejected_with_a_reason() -> None:
    router = ModelDecisionRouter(Recorder(reply("ghost", "a")))
    outcome = run(router.route(request()))
    assert outcome.ids() == ("a",)
    assert outcome.rejected == {"ghost": "not in the offered candidate set"}


def test_a_reply_that_is_entirely_invented_degrades() -> None:
    router = ModelDecisionRouter(Recorder(reply("ghost")))
    outcome = run(router.route(request()))
    assert outcome.status is DecisionStatus.DEGRADED


def test_a_bounded_input_is_never_served_from_an_identical_looking_unbounded_entry() -> None:
    """同一个 `task` 可能是“完整输入”，也可能是“长输入被截断后的视图”。

    两者是不同的输入：“模型对这段文字说了不”与“文字在模型看到之前就被剪了”
    不能互相复用缓存。
    """

    call = Recorder(reply("a"))
    router = ModelDecisionRouter(call, budget=DecisionBudget(max_calls_per_run=2))

    run(router.route(request(truncated=False)))
    bounded = run(router.route(request(truncated=True)))

    assert bounded.usage.calls == 1, "截断输入不得命中未截断条目的缓存"
    assert len(call.payloads) == 2, "两种输入各问一次，不得互相冒充"


def test_a_success_is_cached_and_costs_no_further_call() -> None:
    call = Recorder(reply("a"))
    router = ModelDecisionRouter(call)
    first = run(router.route(request()))
    second = run(router.route(request()))
    assert first.ids() == second.ids()
    assert router.calls == 1
    assert second.usage.calls == 0
    assert router.cache.hits == 1


def test_a_failure_is_not_cached() -> None:
    call = Recorder(raises=RuntimeError("boom"))
    # Two calls allowed, so the second one proves "not cached" rather than
    # "refused by the budget".
    router = ModelDecisionRouter(call, budget=DecisionBudget(max_calls_per_run=2))
    first = run(router.route(request()))
    second = run(router.route(request()))
    assert first.status is second.status is DecisionStatus.DEGRADED
    assert router.calls == 2
    assert len(call.payloads) == 2, "a degraded decision must be asked again, not replayed"
    assert len(router.cache) == 0


def test_the_cache_key_covers_everything_that_could_change_the_answer() -> None:
    base = request()
    key = DecisionCache.key_for(base, decision_version="model/1")
    assert key == DecisionCache.key_for(base, decision_version="model/1")
    assert key != DecisionCache.key_for(request(task="another task"), decision_version="model/1")
    assert key != DecisionCache.key_for(base, decision_version="model/2")
    assert key != DecisionCache.key_for(request(scope="project-b"), decision_version="model/1")
    assert key != DecisionCache.key_for(
        request(policy_version="2"), decision_version="model/1"
    )
    # 同一个 task 可能是一个长输入被截断后的视图；那是不同的输入。
    assert key != DecisionCache.key_for(request(truncated=True), decision_version="model/1")
    # A candidate version bump invalidates the entry as well.
    bumped = request()
    bumped = DecisionRequest(
        task=bumped.task,
        candidates=tuple(
            DecisionCandidate(candidate_id=item.candidate_id, kind=item.kind, description=item.description, version="2")
            for item in bumped.candidates
        ),
        kind=bumped.kind,
        max_suggestions=bumped.max_suggestions,
        scope=bumped.scope,
    )
    assert key != DecisionCache.key_for(bumped, decision_version="model/1")


def test_one_call_serves_two_kinds_with_separately_validated_ids() -> None:
    call = Recorder(
        {
            "suggestions": [
                {"candidate_id": "tool.a", "kind": "tool", "rank": 0},
                {"candidate_id": "skill.x", "kind": "skill", "rank": 0},
                {"candidate_id": "ghost", "kind": "skill", "rank": 1},
            ]
        }
    )
    router = ModelDecisionRouter(call)
    tools = request(DecisionKind.TOOL, ids=("tool.a", "tool.b"))
    skills = request(DecisionKind.SKILL, ids=("skill.x", "skill.y"))
    tool_outcome, skill_outcome = run(router.route_many([tools, skills]))
    assert len(call.payloads) == 1, "one model call must serve both kinds"
    assert tool_outcome.ids() == ("tool.a",)
    assert skill_outcome.ids() == ("skill.x",)
    assert skill_outcome.rejected == {"ghost": "not in the offered candidate set"}


def test_a_skill_suggestion_cannot_be_used_as_a_tool_suggestion() -> None:
    """Ids are validated per kind, so a cross-kind id is a rejection, not a selection."""
    call = Recorder({"suggestions": [{"candidate_id": "skill.x", "kind": "skill", "rank": 0}]})
    router = ModelDecisionRouter(call)
    outcome = run(router.route(request(DecisionKind.TOOL, ids=("tool.a",))))
    assert outcome.status is DecisionStatus.ABSENT


def test_the_noop_router_still_lets_everything_run() -> None:
    outcome = run(NoopDecisionRouter().route(request()))
    assert outcome.status is DecisionStatus.ABSENT
    assert outcome.suggestions == ()


def test_a_decision_outcome_cannot_grant_authority() -> None:
    fields = set(DecisionOutcome.__dataclass_fields__)
    assert not fields & {"approved", "grant", "permissions", "writes", "applied"}


def test_an_empty_payload_is_refused() -> None:
    with pytest.raises(ContractError):
        build_payload([])
    with pytest.raises(ContractError):
        run(ModelDecisionRouter(Recorder(reply("a"))).route_many([]))


def test_budget_bounds_must_be_positive() -> None:
    for kwargs in (
        {"max_calls_per_run": 0},
        {"max_seconds": 0},
        {"max_input_chars": 0},
        {"max_output_chars": 0},
    ):
        with pytest.raises(ContractError):
            DecisionBudget(**kwargs)


def test_a_huge_output_degrades_instead_of_being_used() -> None:
    router = ModelDecisionRouter(
        Recorder(reply("a")), budget=DecisionBudget(max_output_chars=50)
    )
    outcome = run(router.route(request()))
    assert outcome.status is DecisionStatus.DEGRADED


def test_an_ok_decision_from_the_router_never_carries_a_write_claim() -> None:
    router = ModelDecisionRouter(Recorder(reply("a")))
    outcome = run(router.route(request()))
    assert outcome.status is DecisionStatus.OK
    for suggestion in outcome.suggestions:
        assert set(suggestion.__dataclass_fields__) == {"candidate_id", "kind", "rank", "score", "reason"}
