"""Context budgets: honest arithmetic, no invented percentages, safe publications."""

from __future__ import annotations

import pytest

from yanxu_harness.context import (
    ContextPolicy,
    ContextWindow,
    SummaryArtifact,
    validate_publication,
)
from yanxu_harness.errors import BudgetExceeded, ContractError


def test_capacity_subtracts_the_reserve_and_the_margin() -> None:
    window = ContextWindow(limit_tokens=8000, output_reserve=1000, safety_margin=500)
    assert window.known
    assert window.request_capacity == 6500
    assert ContextPolicy(window).assess(6500).fits is True
    assert ContextPolicy(window).assess(6501).fits is False


def test_an_unknown_window_reports_unknown_instead_of_a_percentage() -> None:
    window = ContextWindow(limit_tokens=None)
    assessment = ContextPolicy(window).assess(123)
    assert assessment.capacity is None
    assert assessment.fits is None
    assert not assessment.known
    assert "unknown" in assessment.reason
    # An unknown window may not raise a budget error either: nothing was proven.
    assert ContextPolicy(window).require(10**9).fits is None


def test_a_zero_capacity_window_refuses_any_request() -> None:
    window = ContextWindow(limit_tokens=100, output_reserve=100)
    assert window.request_capacity == 0
    with pytest.raises(BudgetExceeded):
        ContextPolicy(window).require(1)


def test_reserve_and_margin_must_not_be_negative() -> None:
    with pytest.raises(ContractError):
        ContextWindow(limit_tokens=100, output_reserve=-1)
    with pytest.raises(ContractError):
        ContextWindow(limit_tokens=0)


@pytest.mark.parametrize(
    ("artifact", "kwargs", "fragment"),
    [
        ({"scope": "project-b"}, {"scope": "project-a"}, "scope"),
        ({"source_head": "sha256:old"}, {"source_head": "sha256:new"}, "transcript head"),
        ({"lease_token": "lease-1"}, {"lease_token": "lease-2"}, "lease"),
    ],
)
def test_a_stale_summary_is_refused(artifact: dict, kwargs: dict, fragment: str) -> None:
    values = {
        "text": "summary",
        "scope": "project-a",
        "source_head": "sha256:old",
        "base_fingerprint": "sha256:base",
        "lease_token": "lease-1",
    }
    values.update(artifact)
    call = {"scope": "project-a", "source_head": "sha256:old", "lease_token": "lease-1"}
    call.update(kwargs)
    valid, reason = validate_publication(SummaryArtifact(**values), **call)
    assert not valid
    assert fragment in reason


def test_an_empty_summary_is_refused() -> None:
    artifact = SummaryArtifact(
        text="   ",
        scope="project-a",
        source_head="sha256:head",
        base_fingerprint="sha256:base",
    )
    valid, reason = validate_publication(
        artifact, scope="project-a", source_head="sha256:head"
    )
    assert not valid
    assert "empty summary" in reason


def test_a_matching_summary_is_publishable() -> None:
    artifact = SummaryArtifact(
        text="summary",
        scope="project-a",
        source_head="sha256:head",
        base_fingerprint="sha256:base",
        lease_token="lease-1",
    )
    valid, reason = validate_publication(
        artifact, scope="project-a", source_head="sha256:head", lease_token="lease-1"
    )
    assert valid and reason == ""


def test_the_artifact_fingerprint_tracks_content_and_source() -> None:
    base = {
        "text": "summary",
        "scope": "project-a",
        "source_head": "sha256:head",
        "base_fingerprint": "sha256:base",
    }
    first = SummaryArtifact(**base)
    assert first.fingerprint() == SummaryArtifact(**base).fingerprint()
    assert first.fingerprint() != SummaryArtifact(**{**base, "text": "other"}).fingerprint()
    assert first.fingerprint() != SummaryArtifact(**{**base, "source_head": "sha256:x"}).fingerprint()
