"""Adapter contract suite: the checks every host adapter must pass.

A host that plugs its own store, skill provider or decision router into the
harness does not get to reinterpret these contracts. The functions here are
plain ``async`` assertions that both this package's tests and a host's test
suite call directly — they do not import pytest, so a host can run them from any
runner, and they never touch the host's application code.
"""

from __future__ import annotations

from typing import Any

from ..errors import ContractError
from ..skills.provider import (
    SkillProvider,
    SkillResourceDenied,
)
from ..storage.ports import StorageConflict
from ..tools.execution import EffectState, should_retry

__all__ = [
    "check_persistence_contract",
    "check_skill_provider_contract",
    "check_unknown_is_terminal",
]


async def check_persistence_contract(persistence: Any, *, namespace: str = "contract") -> list[str]:
    """Exercise CAS, fencing and event ordering. Returns the observed checks.

    Raises :class:`AssertionError` on the first violation, so a host adapter test
    can simply ``await check_persistence_contract(store)``.
    """
    observed: list[str] = []

    run_id = f"{namespace}:cas"
    first = await persistence.save_run(run_id, {"state": "one"}, expected_version=None)
    assert first == 1, f"first save must produce version 1, got {first!r}"
    observed.append("create")

    try:
        await persistence.save_run(run_id, {"state": "two"}, expected_version=None)
    except StorageConflict:
        observed.append("create-conflict")
    else:
        raise AssertionError("a second create at expected_version=None must be refused")

    second = await persistence.save_run(run_id, {"state": "two"}, expected_version=first)
    assert second == first + 1, "a CAS save must advance the version"
    observed.append("cas")

    try:
        await persistence.save_run(run_id, {"state": "three"}, expected_version=first)
    except StorageConflict:
        observed.append("stale-cas")
    else:
        raise AssertionError("a stale compare-and-set must be refused")

    holder = f"{namespace}-worker"
    lease = await persistence.claim_lease(f"{run_id}:lease", holder, ttl_seconds=30)
    assert lease is not None, "a free lease must be claimable"
    blocked = await persistence.claim_lease(f"{run_id}:lease", "other-worker", ttl_seconds=30)
    assert blocked is None, "a live lease must not be claimable by a second worker"
    observed.append("lease-exclusive")

    await persistence.release_lease(lease)
    replacement = await persistence.claim_lease(f"{run_id}:lease", "other-worker", ttl_seconds=30)
    assert replacement is not None, "a released lease must be claimable"
    assert replacement.fencing_token > lease.fencing_token, "fencing tokens must increase"
    observed.append("fencing")

    try:
        await persistence.save_run(
            run_id, {"state": "late"}, expected_version=second, lease=lease
        )
    except StorageConflict:
        observed.append("stale-fence")
    else:
        raise AssertionError("a write from a superseded lease must be refused")

    events = await persistence.append_events(
        [
            _event("one", run_id),
            _event("two", run_id),
        ]
    )
    assert [event.sequence for event in events] == sorted(
        event.sequence for event in events
    ), "events must be assigned increasing sequences"
    read_back = await persistence.read_events(after=-1)
    assert [event.kind for event in read_back][-2:] == ["one", "two"], (
        "events must replay in the order they were appended"
    )
    observed.append("events-ordered")

    return observed


def _event(kind: str, run_id: str):
    from ..storage.ports import StoredEvent

    return StoredEvent(sequence=0, kind=kind, payload={"kind": kind}, run_id=run_id)


async def check_skill_provider_contract(provider: SkillProvider, *, skill_id: str, scope: str) -> list[str]:
    """Exercise the skill tiers: bounded metadata, on-demand body, path confinement."""
    observed: list[str] = []

    metadata = await provider.list_metadata(scope=scope)
    assert all(len(item.description) < 4000 for item in metadata), (
        "listing skills must not load unbounded prose"
    )
    observed.append("metadata-bounded")

    body = await provider.load_body(skill_id, scope=scope)
    assert body.text, "a loaded body must carry text"
    assert body.fingerprint, "a loaded body must carry a fingerprint"
    observed.append("body-on-demand")

    try:
        await provider.read_resource(skill_id, "../secrets.txt", scope=scope)
    except (SkillResourceDenied, ContractError):
        observed.append("path-confined")
    except Exception as exc:  # noqa: BLE001 - any refusal is acceptable, silence is not
        if "not" in str(exc).lower():
            observed.append("path-confined")
        else:
            raise AssertionError(f"an escaping resource path must be refused, got {exc!r}") from exc
    else:
        raise AssertionError("a resource path escaping the skill directory must be refused")

    digest = await provider.fingerprint(skill_id, scope=scope)
    assert digest.startswith("sha256:"), "fingerprints must name their algorithm"
    observed.append("fingerprint")

    return observed


def check_unknown_is_terminal() -> None:
    """``unknown`` must never be retried automatically and never look settled-goods.

    Kept as a plain function (not async) so a host can call it from a
    configuration check as well as from a test.
    """
    assert not should_retry(EffectState.UNKNOWN), "unknown must never be retried automatically"
    assert not should_retry(EffectState.UNKNOWN, idempotent=True), (
        "even an idempotent tool must not auto-retry an unknown outcome"
    )
    assert should_retry(EffectState.FAILED, idempotent=True), "idempotent failures may be retried"
    assert not should_retry(EffectState.FAILED), "non-idempotent failures must not be retried"
    assert not should_retry(EffectState.SUCCEEDED)
    assert not should_retry(EffectState.CANCELLED)
