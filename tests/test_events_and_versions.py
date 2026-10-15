"""Events carry versions, refuse private payloads, and replay from a cursor."""

from __future__ import annotations

import pytest

from yanxu_harness.errors import ContractError, VersionRefused
from yanxu_harness.events import EVENT_SCHEMA_VERSION, Event, EventKind, assert_public_payload
from yanxu_harness.versioning import (
    canonical_json,
    fingerprint,
    fingerprint_value,
    require_version,
)


def test_an_event_round_trips_its_public_payload() -> None:
    event = Event(kind=EventKind.TEXT_DELTA, payload={"text": "海边的信号塔"}, sequence=3, run_id="r1")
    assert event.schema_version == EVENT_SCHEMA_VERSION
    assert event.payload["text"] == "海边的信号塔"


@pytest.mark.parametrize(
    "payload",
    [
        {"api_key": "sk-live"},
        {"Authorization": "Bearer abc"},
        {"nested": {"headers": {"x": "y"}}},
        {"frames": [{"signature": "sig"}]},
        {"reasoning_details": [{"blob": "..."}]},
    ],
)
def test_private_material_is_refused_at_any_depth(payload: dict) -> None:
    with pytest.raises(ContractError) as excinfo:
        assert_public_payload(payload)
    assert "private material" in str(excinfo.value)


def test_a_non_serialisable_payload_is_refused() -> None:
    """An event that cannot be persisted must fail at construction, not at write time."""
    with pytest.raises(ContractError):
        assert_public_payload({"handle": object()})


def test_a_future_event_version_is_refused_not_guessed() -> None:
    with pytest.raises(VersionRefused) as excinfo:
        Event(kind=EventKind.RUN_STARTED, schema_version=EVENT_SCHEMA_VERSION + 1)
    assert excinfo.value.version == EVENT_SCHEMA_VERSION + 1


def test_require_version_refuses_bool_and_non_ints() -> None:
    for bogus in (True, "1", 1.0, None):
        with pytest.raises(VersionRefused):
            require_version("checkpoint", bogus)


def test_fingerprints_are_canonical_and_stable() -> None:
    left = {"b": [1, 2], "a": "海"}
    right = {"a": "海", "b": [1, 2]}
    assert fingerprint(left) == fingerprint(right)
    assert fingerprint(left).startswith("sha256:")
    assert canonical_json(left) == '{"a":"海","b":[1,2]}'
    # Set ordering must not leak into the digest: two equal sets fingerprint alike.
    assert fingerprint({"s": {"x", "y"}}) == fingerprint({"s": {"y", "x"}})


def test_different_values_produce_different_fingerprints() -> None:
    assert fingerprint({"a": 1}) != fingerprint({"a": 2})
    assert fingerprint_value("1") != fingerprint_value(1)


def test_fingerprint_refuses_unserialisable_values() -> None:
    with pytest.raises(TypeError):
        fingerprint({"handle": object()})


def test_event_kind_must_be_a_member() -> None:
    with pytest.raises(ContractError):
        Event(kind="text_delta")  # type: ignore[arg-type]


def test_negative_sequence_is_refused() -> None:
    with pytest.raises(ContractError):
        Event(kind=EventKind.RUN_STARTED, sequence=-1)
