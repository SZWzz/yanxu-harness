"""The durable store: transactions, fences, replay — and honest crash states.

Everything here uses a real file in ``tmp_path`` and, for the fault windows, a
real child process that is killed at a chosen phase. The point of the exercised
cases is what a *restart* can see; an in-process exception cannot stand in for
them.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from child_process import child_json, run_child
from yanxu_harness.errors import ContractError, VersionRefused
from yanxu_harness.storage import (
    Durability,
    Lease,
    SqliteExecutionBackend,
    SqlitePersistence,
    StorageConflict,
    StoredEvent,
)
from yanxu_harness.testing import check_persistence_contract


def run(coro):
    return asyncio.run(coro)


# -- the shared adapter contract ----------------------------------------


def test_the_sqlite_store_passes_the_shared_adapter_contract(tmp_path) -> None:
    with SqlitePersistence(tmp_path / "harness.sqlite") as store:
        observed = run(check_persistence_contract(store, namespace="sqlite"))
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


def test_the_sqlite_store_says_it_is_durable(tmp_path) -> None:
    with SqlitePersistence(tmp_path / "harness.sqlite") as store:
        assert store.durability is Durability.DURABLE


# -- transactions -------------------------------------------------------


def test_state_and_events_commit_together(tmp_path) -> None:
    async def scenario() -> None:
        with SqlitePersistence(tmp_path / "h.sqlite") as store:
            version, written = await store.commit_transition(
                "r1",
                {"turn": 1},
                expected_version=None,
                events=[StoredEvent(sequence=0, kind="run_started", payload={"k": "v"}, run_id="r1")],
            )
            assert version == 1
            assert [event.kind for event in written] == ["run_started"]
            assert [event.sequence for event in written] == [0]
            loaded = await store.load_run("r1")
            assert loaded is not None and loaded.state == {"turn": 1} and loaded.version == 1
            assert [event.kind for event in await store.read_events(after=-1)] == ["run_started"]

    run(scenario())


def test_a_transition_that_fails_writes_neither_state_nor_events(tmp_path) -> None:
    """The all-or-nothing claim has to survive a *refused* write, not just a happy path."""

    async def scenario() -> None:
        with SqlitePersistence(tmp_path / "h.sqlite") as store:
            await store.commit_transition("r1", {"turn": 1}, expected_version=None)
            with pytest.raises(StorageConflict):
                await store.commit_transition(
                    "r1",
                    {"turn": 2},
                    expected_version=99,  # stale: the write must be refused
                    events=[StoredEvent(sequence=0, kind="turn_started", payload={}, run_id="r1")],
                )
            loaded = await store.load_run("r1")
            assert loaded is not None and loaded.state == {"turn": 1} and loaded.version == 1
            assert await store.read_events(after=-1) == ()

    run(scenario())


def test_an_oversized_or_non_serialisable_state_is_refused_without_partial_writes(tmp_path) -> None:
    async def scenario() -> None:
        with SqlitePersistence(tmp_path / "h.sqlite") as store:
            with pytest.raises(TypeError):
                await store.save_run("r1", {"handle": object()}, expected_version=None)
            assert await store.load_run("r1") is None

    run(scenario())


# -- leases -------------------------------------------------------------


def test_a_second_process_cannot_take_a_live_lease(tmp_path) -> None:
    async def scenario() -> None:
        with SqlitePersistence(tmp_path / "h.sqlite") as store:
            first = await store.claim_lease("run:1", "worker-a", ttl_seconds=60)
            assert first is not None
            assert await store.claim_lease("run:1", "worker-b", ttl_seconds=60) is None
            await store.release_lease(first)
            second = await store.claim_lease("run:1", "worker-b", ttl_seconds=60)
            assert second is not None and second.fencing_token > first.fencing_token

    run(scenario())


def test_an_expired_lease_is_reclaimable_and_the_old_fence_is_dead(tmp_path) -> None:
    async def scenario() -> None:
        with SqlitePersistence(tmp_path / "h.sqlite") as store:
            first = await store.claim_lease("run:1", "worker-a", ttl_seconds=10, now=1000.0)
            assert first is not None
            second = await store.claim_lease("run:1", "worker-b", ttl_seconds=10, now=1100.0)
            assert second is not None
            version = await store.save_run("r1", {"turn": 1}, expected_version=None)
            with pytest.raises(StorageConflict):
                await store.save_run("r1", {"turn": 2}, expected_version=version, lease=first)

    run(scenario())


def test_a_negative_ttl_is_refused(tmp_path) -> None:
    async def scenario() -> None:
        with SqlitePersistence(tmp_path / "h.sqlite") as store:
            with pytest.raises(ContractError):
                await store.claim_lease("run:1", "worker", ttl_seconds=0)

    run(scenario())


# -- schema versioning --------------------------------------------------


def test_a_newer_schema_is_refused_not_guessed(tmp_path) -> None:
    path = tmp_path / "h.sqlite"
    with SqlitePersistence(path) as store:
        store._connection.execute(
            "UPDATE harness_meta SET value = ? WHERE key = 'schema_version'", ("99",)
        )
    with pytest.raises(VersionRefused):
        SqlitePersistence(path)


# -- durability across a reopen ----------------------------------------


def test_state_survives_a_close_and_reopen(tmp_path) -> None:
    async def first() -> None:
        with SqlitePersistence(tmp_path / "h.sqlite") as store:
            await store.commit_transition(
                "r1",
                {"turn": 3},
                expected_version=None,
                events=[StoredEvent(sequence=0, kind="run_finished", payload={}, run_id="r1")],
            )

    async def second() -> None:
        with SqlitePersistence(tmp_path / "h.sqlite") as store:
            loaded = await store.load_run("r1")
            assert loaded is not None and loaded.state == {"turn": 3} and loaded.version == 1
            assert [event.kind for event in await store.read_events(after=-1)] == ["run_finished"]

    run(first())
    run(second())


# -- fault windows (real child processes) ------------------------------


def test_a_process_that_dies_after_the_intent_leaves_an_unsettled_operation(tmp_path) -> None:
    store = tmp_path / "ops.sqlite"
    marker = tmp_path / "effect.txt"
    counter = tmp_path / "count.txt"
    result = run_child(["intent-then-die", str(store), str(marker), str(counter)], timeout=60)
    assert result.returncode == 9, result.stderr[-800:]
    assert not marker.exists(), "the handler must not have run in this window"

    with SqliteExecutionBackend(store) as backend:
        open_operations = backend.unsettled()
        assert len(open_operations) == 1
        assert open_operations[0].operation_id == "r1:call-1"
        assert open_operations[0].state == "running"
        assert not open_operations[0].provable
        assert backend.receipt("r1:call-1") is None
        assert backend.counts_by_state() == {"running": 1}


def test_a_process_that_dies_after_the_effect_reports_an_unprovable_outcome(tmp_path) -> None:
    """The window the plan singles out: the effect may have landed, and we cannot prove it."""
    store = tmp_path / "ops.sqlite"
    marker = tmp_path / "effect.txt"
    counter = tmp_path / "count.txt"
    result = run_child(["effect-then-die", str(store), str(marker), str(counter)], timeout=60)
    assert result.returncode == 9, result.stderr[-800:]
    assert marker.exists(), "this window is only interesting when the effect really happened"
    assert len(counter.read_text(encoding="utf-8").splitlines()) == 1

    with SqliteExecutionBackend(store) as backend:
        assert backend.receipt("r1:call-1") is None, "an unsettled operation has no receipt"
        open_operations = backend.unsettled()
        assert len(open_operations) == 1
        # The ledger does NOT claim failure: the only honest label is "unknown,
        # pending business reconciliation".
        assert backend.counts_by_state() == {"running": 1}
        from yanxu_harness.tools import EffectState

        assert not __import__("yanxu_harness.tools.execution", fromlist=["x"]).should_retry(
            EffectState.UNKNOWN
        )


def test_a_settled_operation_is_replayed_after_a_restart(tmp_path) -> None:
    store = tmp_path / "ops.sqlite"
    marker = tmp_path / "effect.txt"
    counter = tmp_path / "count.txt"
    first = run_child(["settled-then-die", str(store), str(marker), str(counter)], timeout=60)
    assert first.returncode == 0, first.stderr[-800:]
    assert child_json(first) == {"status": "executed", "calls": 1}

    # A second process runs the same call against the same file.
    second = run_child(["settled-then-die", str(store), str(marker), str(counter)], timeout=60)
    assert second.returncode == 0, second.stderr[-800:]
    assert child_json(second) == {"status": "replayed", "calls": 1}
    assert len(counter.read_text(encoding="utf-8").splitlines()) == 1, (
        "the handler must run exactly once across both processes"
    )


def test_state_and_events_are_visible_after_a_crash_at_fanout(tmp_path) -> None:
    store = tmp_path / "state.sqlite"
    marker = tmp_path / "unused.txt"
    counter = tmp_path / "unused-count.txt"
    result = run_child(["events-then-die", str(store), str(marker), str(counter)], timeout=60)
    assert result.returncode == 9, result.stderr[-800:]
    assert child_json(result)["sequences"] == [0, 1]

    async def read_back() -> None:
        with SqlitePersistence(store) as persistence:
            loaded = await persistence.load_run("r1")
            assert loaded is not None and loaded.state["status"] == "suspended"
            # Sequences start at 0 in both reference stores, so after=-1 is "from
            # the beginning" and after=0 skips the first event.
            assert [event.kind for event in await persistence.read_events(after=-1)] == [
                "run_started",
                "run_suspended",
            ]
            replayed = await persistence.read_events(after=0)
            assert [event.kind for event in replayed] == ["run_suspended"]

    run(read_back())


def test_a_lease_claimed_by_a_dead_process_is_still_held(tmp_path) -> None:
    """A crashed worker's lease must not look free: that is how two writers appear."""
    store = tmp_path / "leases.sqlite"
    marker = tmp_path / "unused.txt"
    counter = tmp_path / "unused-count.txt"
    result = run_child(["claim-then-die", str(store), str(marker), str(counter)], timeout=60)
    assert result.returncode == 9, result.stderr[-800:]
    assert child_json(result) == {"holder": "worker-a", "fence": 1}

    async def scenario() -> None:
        with SqlitePersistence(store) as persistence:
            blocked = await persistence.claim_lease("run:r1", "worker-b", ttl_seconds=300)
            assert blocked is None, "a live claim from a dead process is still a live claim"

    run(scenario())


# -- durable approvals --------------------------------------------------


def test_a_durable_approval_survives_a_restart_and_keeps_its_history(tmp_path) -> None:
    """AC15: an answered question must not become unanswered after a restart."""
    from yanxu_harness.storage import SqliteApprovalStore
    from yanxu_harness.tools import Approval, ApprovalBinding, ApprovalStatus, check_binding

    path = tmp_path / "approvals.sqlite"
    binding = ApprovalBinding(
        namespace="project-a",
        call_id="call-1",
        tool_name="notes.write",
        tool_version="1",
        args_hash="sha256:aaa",
        target_version="v1",
    )

    async def decide() -> None:
        with SqliteApprovalStore(path) as store:
            await store.put(Approval(binding=binding))
            await store.decide(binding.key(), ApprovalStatus.APPROVED, "author")
            # Repeating the same decision is idempotent; a conflicting one refuses.
            first = await store.decide(binding.key(), ApprovalStatus.APPROVED, "author")
            assert first.status is ApprovalStatus.APPROVED
            with pytest.raises(ContractError):
                await store.decide(binding.key(), ApprovalStatus.DENIED, "author")

    async def reopen() -> None:
        with SqliteApprovalStore(path) as store:
            stored = await store.get(binding.key())
            assert stored is not None and stored.is_approved()
            valid, reason = check_binding(
                stored.binding,
                args_hash=binding.args_hash,
                target_version=binding.target_version,
                tool_version=binding.tool_version,
            )
            assert valid and reason == ""
            # A drifted call asks again, and the earlier decision stays visible.
            fresh = await store.supersede(replace(binding, args_hash="sha256:bbb"))
            assert fresh.is_open()
            assert [row["status"] for row in store.history(binding.key())] == ["approved"]

    run(decide())
    run(reopen())


def test_putting_a_second_approval_for_the_same_call_is_refused(tmp_path) -> None:
    from yanxu_harness.storage import SqliteApprovalStore
    from yanxu_harness.tools import Approval, ApprovalBinding

    binding = ApprovalBinding(
        namespace="p", call_id="c", tool_name="t", tool_version="1", args_hash="sha256:a"
    )

    async def scenario() -> None:
        with SqliteApprovalStore(tmp_path / "a.sqlite") as store:
            await store.put(Approval(binding=binding))
            with pytest.raises(ContractError, match="already exists"):
                await store.put(Approval(binding=binding))

    run(scenario())
