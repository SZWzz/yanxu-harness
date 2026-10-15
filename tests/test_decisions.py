"""Decision routing: bounded input, checked ids, explicit failure, no authority."""

from __future__ import annotations

import asyncio

from yanxu_harness.decisions import (
    DecisionCandidate,
    DecisionKind,
    DecisionOutcome,
    DecisionRequest,
    DecisionStatus,
    DecisionSuggestion,
    DecisionUsage,
    NoopDecisionRouter,
    RuleDecisionRouter,
    validate_suggestions,
)
from yanxu_harness.errors import BudgetExceeded, ContractError


def request(ids: tuple[str, ...] = ("a", "b", "c"), *, max_suggestions: int = 2) -> DecisionRequest:
    return DecisionRequest(
        task="choose a tool",
        candidates=tuple(DecisionCandidate(candidate_id=name, kind=DecisionKind.TOOL, description=name) for name in ids),
        kind=DecisionKind.TOOL,
        max_suggestions=max_suggestions,
    )


def run(coro):
    return asyncio.run(coro)


def test_noop_router_reports_absent_and_a_run_still_works() -> None:
    outcome = run(NoopDecisionRouter().route(request()))
    assert outcome.status is DecisionStatus.ABSENT
    assert outcome.suggestions == ()
    assert not outcome.ok


def test_rules_router_returns_ranked_suggestions() -> None:
    router = RuleDecisionRouter(lambda _request: ["c", "a"])
    outcome = run(router.route(request()))
    assert outcome.status is DecisionStatus.OK
    assert outcome.ids() == ("c", "a")
    assert [item.rank for item in outcome.suggestions] == [0, 1]
    assert outcome.usage.calls == 1


def test_a_rules_router_that_fails_degrades_instead_of_raising() -> None:
    def explode(_request):
        raise RuntimeError("rule table is missing")

    outcome = run(RuleDecisionRouter(explode).route(request()))
    assert outcome.status is DecisionStatus.DEGRADED
    assert "rule table is missing" in (outcome.failure or "")


def test_a_budget_stop_is_reported_as_such() -> None:
    def expensive(_request):
        raise BudgetExceeded("decision calls", "1 of 1 used")

    outcome = run(RuleDecisionRouter(expensive).route(request()))
    assert outcome.status is DecisionStatus.BUDGET_EXCEEDED
    assert "decision calls" in (outcome.failure or "")


def test_an_empty_selection_is_absent_not_ok() -> None:
    outcome = run(RuleDecisionRouter(lambda _request: []).route(request()))
    assert outcome.status is DecisionStatus.ABSENT


def test_an_ok_decision_must_contain_a_suggestion() -> None:
    try:
        DecisionOutcome(status=DecisionStatus.OK)
    except ContractError as exc:
        assert "at least one suggestion" in str(exc)
    else:  # pragma: no cover - the contract must hold
        raise AssertionError("an ok decision with no suggestion must be refused")


def test_a_degraded_decision_must_explain_itself() -> None:
    try:
        DecisionOutcome(status=DecisionStatus.DEGRADED)
    except ContractError as exc:
        assert "explain itself" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("a degraded decision without a reason must be refused")


def test_invented_ids_are_rejected_and_lower_the_status() -> None:
    outcome = DecisionOutcome(
        status=DecisionStatus.OK,
        suggestions=(
            DecisionSuggestion(candidate_id="ghost", kind=DecisionKind.TOOL, rank=0),
            DecisionSuggestion(candidate_id="b", kind=DecisionKind.TOOL, rank=1),
        ),
    )
    checked = validate_suggestions(outcome, request())
    assert checked.status is DecisionStatus.OK
    assert checked.ids() == ("b",)
    assert checked.rejected == {"ghost": "not in the offered candidate set"}


def test_a_decision_that_is_entirely_invented_degrades() -> None:
    outcome = DecisionOutcome(
        status=DecisionStatus.OK,
        suggestions=(DecisionSuggestion(candidate_id="ghost", kind=DecisionKind.TOOL, rank=0),),
    )
    checked = validate_suggestions(outcome, request())
    assert checked.status is DecisionStatus.DEGRADED
    assert checked.suggestions == ()
    assert "every suggested id was rejected" in (checked.failure or "")


def test_duplicate_suggestions_are_collapsed_with_a_reason() -> None:
    outcome = DecisionOutcome(
        status=DecisionStatus.OK,
        suggestions=(
            DecisionSuggestion(candidate_id="a", kind=DecisionKind.TOOL, rank=0),
            DecisionSuggestion(candidate_id="a", kind=DecisionKind.TOOL, rank=1),
        ),
    )
    checked = validate_suggestions(outcome, request())
    assert checked.ids() == ("a",)
    assert checked.rejected == {"a": "duplicate suggestion"}


def test_the_suggestion_limit_is_enforced_and_reported() -> None:
    outcome = DecisionOutcome(
        status=DecisionStatus.OK,
        suggestions=tuple(
            DecisionSuggestion(candidate_id=name, kind=DecisionKind.TOOL, rank=index)
            for index, name in enumerate(("a", "b", "c"))
        ),
    )
    checked = validate_suggestions(outcome, request(max_suggestions=1))
    assert checked.ids() == ("a",)
    assert checked.rejected["b"] == "over the request's suggestion limit"


def test_the_request_carries_only_bounded_candidate_metadata() -> None:
    req = request()
    assert req.candidate_ids() == ("a", "b", "c")
    for candidate in req.candidates:
        assert not hasattr(candidate, "body")
        assert not hasattr(candidate, "text")
    try:
        DecisionRequest(task="x", candidates=(), kind=DecisionKind.TOOL, max_suggestions=0)
    except ContractError:
        pass
    else:  # pragma: no cover
        raise AssertionError("a non-positive suggestion limit must be refused")


def test_usage_accumulates_without_inventing_missing_numbers() -> None:
    left = DecisionUsage(calls=1, input_tokens=10, duration_ms=2.0)
    right = DecisionUsage(calls=1, output_tokens=5)
    combined = left.combine(right)
    assert combined.calls == 2
    assert combined.input_tokens == 10
    assert combined.output_tokens == 5
    assert combined.duration_ms == 2.0


def test_a_decision_outcome_cannot_express_authority() -> None:
    """There must be no field a router could use to approve or grant anything."""
    fields = set(DecisionOutcome.__dataclass_fields__)
    assert fields == {"status", "suggestions", "failure", "rejected", "usage", "decision_version"}
    assert not fields & {"approved", "grant", "permissions", "writes", "applied"}
