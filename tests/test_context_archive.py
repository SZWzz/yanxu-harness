"""The archive/projection split, and the payload gate in front of dispatch."""

from __future__ import annotations

import asyncio
import json

import pytest
from wire_fixture import WireFixture, delta_frame

from yanxu_harness.context import (
    ContextPolicy,
    ContextWindow,
    InMemoryTranscriptArchive,
    SummaryArtifact,
    TranscriptEntry,
    WindowedProjector,
    validate_publication,
)
from yanxu_harness.errors import BudgetExceeded, ContractError
from yanxu_harness.providers import (
    OpenAICompatibleProvider,
    ProviderConfig,
    ProviderRequest,
    collect,
)


def run(coro):
    return asyncio.run(coro)


def entry(role: str, content: str) -> TranscriptEntry:
    return TranscriptEntry(sequence=0, role=role, payload={"role": role, "content": content})


def test_the_archive_is_append_only_and_numbers_its_entries() -> None:
    async def scenario() -> None:
        archive = InMemoryTranscriptArchive()
        written = await archive.append_all([entry("user", "one"), entry("assistant", "two")])
        assert [item.sequence for item in written] == [0, 1]
        assert all(item.fingerprint.startswith("sha256:") for item in written)
        head = archive.head
        await archive.append(entry("user", "three"))
        assert archive.head != head, "the head must move when the archive grows"

    run(scenario())


def test_a_projection_never_rewrites_the_archive() -> None:
    async def scenario() -> None:
        archive = InMemoryTranscriptArchive()
        await archive.append_all([entry("user", f"turn {index}") for index in range(50)])
        before = archive.entries()
        projector = WindowedProjector(char_budget=40)
        snapshot = projector.snapshot(before, scope="project-a", source_head=archive.head)
        # The projection is smaller and says so; the archive is untouched.
        assert len(snapshot.messages) < len(before)
        assert snapshot.dropped_entries == len(before) - len(snapshot.messages)
        assert archive.entries() == before
        assert len(archive) == 50

    run(scenario())


def test_a_projection_is_stale_once_the_head_moves() -> None:
    async def scenario() -> None:
        archive = InMemoryTranscriptArchive()
        await archive.append(entry("user", "one"))
        projector = WindowedProjector()
        snapshot = projector.snapshot(
            archive.entries(), scope="s", source_head=archive.head
        )
        assert not snapshot.is_stale(archive.head)
        await archive.append(entry("assistant", "two"))
        assert snapshot.is_stale(archive.head), "publishing this would drop the new turn"

    run(scenario())


def test_the_projection_budget_is_never_exceeded_by_the_kept_entries() -> None:
    async def scenario() -> None:
        archive = InMemoryTranscriptArchive()
        await archive.append_all([entry("user", "x" * 100) for _ in range(10)])
        projector = WindowedProjector(char_budget=250)
        snapshot = projector.snapshot(archive.entries(), scope="s", source_head=archive.head)
        assert sum(len(m["content"]) for m in snapshot.messages) <= 250

    run(scenario())


def test_a_summary_publication_needs_a_matching_head_and_lease() -> None:
    artifact = SummaryArtifact(
        text="summary",
        scope="project-a",
        source_head="sha256:head",
        base_fingerprint="sha256:base",
        lease_token="lease-1",
    )
    assert validate_publication(
        artifact, scope="project-a", source_head="sha256:head", lease_token="lease-1"
    )[0]
    assert not validate_publication(
        artifact, scope="project-a", source_head="sha256:moved", lease_token="lease-1"
    )[0]


def test_an_unbounded_window_reports_unknown_rather_than_a_percentage() -> None:
    policy = ContextPolicy(ContextWindow(limit_tokens=None))
    assessment = policy.require(10**6)
    assert assessment.fits is None and assessment.capacity is None


def test_a_negative_projection_budget_is_refused() -> None:
    with pytest.raises(ContractError):
        WindowedProjector(char_budget=0)


# -- the payload gate ---------------------------------------------------


def _provider(fixture: WireFixture, gate=None) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        ProviderConfig(model="m", base_url=fixture.base_url, timeout_seconds=5.0),
        payload_gate=gate,
    )


def test_an_oversized_payload_never_reaches_the_wire() -> None:
    """The gate measures the *final* bytes, not an earlier estimate."""
    sent: list[int] = []

    def gate(body: bytes, _request: ProviderRequest) -> None:
        sent.append(len(body))
        if len(body) > 200:
            raise BudgetExceeded("context", f"payload is {len(body)} bytes")

    with WireFixture([delta_frame("ok", finish="stop")]) as fixture:
        with pytest.raises(BudgetExceeded):
            run(
                collect(
                    _provider(fixture, gate),
                    ProviderRequest(messages=[{"role": "user", "content": "字" * 500}]),
                )
            )
        assert fixture.requests == [], "a refused payload must not be dispatched"

    assert sent and sent[0] > 200


def test_a_payload_that_fits_is_dispatched_normally() -> None:
    def gate(body: bytes, _request: ProviderRequest) -> None:
        if len(body) > 10_000:
            raise BudgetExceeded("context")

    with WireFixture([delta_frame("ok", finish="stop")]) as fixture:
        result = run(collect(_provider(fixture, gate), ProviderRequest(messages=[{"role": "user", "content": "hi"}])))
    assert result.text == "ok"
    assert len(fixture.requests) == 1


def test_the_gate_sees_exactly_the_bytes_that_would_be_sent() -> None:
    seen: list[bytes] = []

    def gate(body: bytes, _request: ProviderRequest) -> None:
        seen.append(body)

    with WireFixture([delta_frame("ok", finish="stop")]) as fixture:
        run(collect(_provider(fixture, gate), ProviderRequest(messages=[{"role": "user", "content": "海"}])))
    assert json.loads(seen[0].decode("utf-8")) == json.loads(fixture.first_request()["body"])
