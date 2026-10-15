"""Persistence: the shared adapter contract, plus what "volatile" has to mean."""

from __future__ import annotations

import asyncio

import pytest

from yanxu_harness.errors import ContractError
from yanxu_harness.storage import Durability, InMemoryPersistence, StoredEvent, StorageConflict
from yanxu_harness.testing import check_persistence_contract


def run(coro):
    return asyncio.run(coro)


def test_the_in_memory_store_passes_the_shared_adapter_contract() -> None:
    observed = run(check_persistence_contract(InMemoryPersistence(), namespace="contract"))
    assert set(observed) >= {
        "create",
        "create-conflict",
        "cas",
        "stale-cas",
        "lease-exclusive",
        "fencing",
        "stale-fence",
        "events-ordered",
    }


def test_the_volatile_store_says_it_is_volatile() -> None:
    """A restart example must not be built on this store, so it declares itself."""
    store = InMemoryPersistence()
    assert store.durability is Durability.VOLATILE
    assert store.durability is not Durability.DURABLE


def test_a_stale_save_does_not_change_the_stored_state() -> None:
    async def scenario() -> None:
        store = InMemoryPersistence()
        version = await store.save_run("r1", {"turn": 1}, expected_version=None)
        await store.save_run("r1", {"turn": 2}, expected_version=version)
        with pytest.raises(StorageConflict):
            await store.save_run("r1", {"turn": 3}, expected_version=version)
        stored = await store.load_run("r1")
        assert stored.state == {"turn": 2}

    run(scenario())


def test_a_new_run_is_created_not_overwritten() -> None:
    async def scenario() -> None:
        store = InMemoryPersistence()
        await store.save_run("r1", {"turn": 1}, expected_version=None)
        with pytest.raises(StorageConflict):
            await store.save_run("r1", {"turn": 9}, expected_version=None)

    run(scenario())


def test_an_expired_lease_can_be_reclaimed_with_a_higher_fence() -> None:
    async def scenario() -> None:
        store = InMemoryPersistence()
        first = await store.claim_lease("run:1", "worker-a", ttl_seconds=10, now=100.0)
        assert first is not None
        assert await store.claim_lease("run:1", "worker-b", ttl_seconds=10, now=105.0) is None
        second = await store.claim_lease("run:1", "worker-b", ttl_seconds=10, now=111.0)
        assert second is not None
        assert second.fencing_token > first.fencing_token

    run(scenario())


def test_releasing_a_lease_twice_is_harmless() -> None:
    async def scenario() -> None:
        store = InMemoryPersistence()
        lease = await store.claim_lease("run:1", "worker-a", ttl_seconds=10)
        await store.release_lease(lease)
        await store.release_lease(lease)
        assert (await store.claim_lease("run:1", "worker-b", ttl_seconds=10)) is not None

    run(scenario())


def test_events_are_sequenced_and_resumable_from_a_cursor() -> None:
    async def scenario() -> None:
        store = InMemoryPersistence()
        written = await store.append_events(
            [StoredEvent(sequence=0, kind="one"), StoredEvent(sequence=0, kind="two")]
        )
        assert [event.sequence for event in written] == [0, 1]
        assert [event.kind for event in await store.read_events(after=0)] == ["two"]
        assert await store.read_events(after=1) == ()

    run(scenario())


def test_an_invalid_ttl_or_limit_is_refused() -> None:
    async def scenario() -> None:
        store = InMemoryPersistence()
        with pytest.raises(ContractError):
            await store.claim_lease("run:1", "worker", ttl_seconds=0)
        with pytest.raises(ContractError):
            await store.read_events(limit=0)

    run(scenario())
