"""Q1: the persistent inbox and control store, tested against real transactions.

Every test here uses a real SQLite database and real transactions; where the plan asks for
concurrency it uses two connections (or a real child process), not one object called twice. What
these tests are about:

* **I-01** — the same input id with the same content is a duplicate; with different content it is
  a conflict; neither consumes capacity twice;
* **I-02** — two connections racing for one task produce one claim and one fence, enforced by the
  database rather than by an in-process lock;
* capacity is refused, never evicted; a claim that is not renewed can be retaken, and a stale
  claim cannot commit; a restart keeps the sequence and the deduplication; a read-only connection
  can read the control facts and write nothing; an unknown future schema is refused.

The in-memory store is exercised through the same contract, and says out loud that it is volatile:
two instances sharing nothing is the documented behaviour, not a bug to work around.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from yanxu_harness.errors import ContractError, VersionRefused
from yanxu_harness.tasks import (
    AdmissionOutcome,
    BacklogLimits,
    BudgetRefused,
    BudgetReservation,
    BudgetUnit,
    ChildRelation,
    ClaimLost,
    ControlOutboxEntry,
    InputAdmission,
    InputContent,
    InputKind,
    InputState,
    RefusalCode,
    ReservationState,
    TaskAdmission,
    TaskStatus,
)
from yanxu_harness.tasks.inbox import CONTROL_SCHEMA_VERSION, MemoryControlStore, SqliteControlStore

NAMESPACE = "project-a"
SESSION = "s1"


def run(coro: Any, *, timeout: float = 30.0) -> Any:
    async def guarded() -> Any:
        return await asyncio.wait_for(coro, timeout=timeout)

    return asyncio.run(guarded())


def _initial(text: str = "do the thing") -> InputContent:
    return InputContent(kind=InputKind.INITIAL, text=text)


def _steering(text: str = "keep going", **parameters: Any) -> InputContent:
    return InputContent(kind=InputKind.STEERING, text=text, parameters=parameters)


def _admission(input_id: str, content: InputContent | None = None) -> InputAdmission:
    return InputAdmission(
        namespace=NAMESPACE,
        session_id=SESSION,
        task_id="t1",
        input_id=input_id,
        content=content or _steering(),
    )


def _task_admission(task_id: str = "t1", run_id: str = "r1") -> TaskAdmission:
    return TaskAdmission(
        namespace=NAMESPACE,
        session_id=SESSION,
        task_id=task_id,
        run_id=run_id,
        initial_input=_initial(),
    )


def _store(path: Path, **kwargs: Any) -> SqliteControlStore:
    store = SqliteControlStore(path, **kwargs)
    run(store.admit_task(_task_admission()))
    return store


# -- I-01: idempotent acceptance and conflicts --------------------------------------


def test_the_same_input_id_with_the_same_content_is_a_duplicate(tmp_path: Path) -> None:
    store = _store(tmp_path / "control.db")
    try:
        first = run(store.admit_input(_admission("i1")))
        again = run(store.admit_input(_admission("i1")))
        assert first.outcome is AdmissionOutcome.ACCEPTED
        assert first.record is not None and first.record.sequence == 1
        assert again.outcome is AdmissionOutcome.DUPLICATE
        assert again.duplicate_of == "i1"
        assert again.accepted is False
        # One capacity unit was used, not two.
        assert run(store.read_inbox_revision("t1")) == 2  # initial + i1
        records = run(store.read_inputs("t1"))
        assert [record.input_id for record in records] == ["initial", "i1"]
    finally:
        store.close()


def test_the_same_input_id_with_different_content_is_a_conflict(tmp_path: Path) -> None:
    store = _store(tmp_path / "control.db")
    try:
        run(store.admit_input(_admission("i1", _steering("first"))))
        conflict = run(store.admit_input(_admission("i1", _steering("second"))))
        assert conflict.outcome is AdmissionOutcome.REFUSED
        assert conflict.refusal_code == RefusalCode.CONTENT_CONFLICT.value
        # The accepted content is untouched: a conflict never overwrites.
        stored = [item for item in run(store.read_inputs("t1")) if item.input_id == "i1"][0]
        assert stored.content.text == "first"
    finally:
        store.close()


def test_a_terminal_task_refuses_new_input_and_a_cancelled_first_does_too(tmp_path: Path) -> None:
    store = _store(tmp_path / "control.db")
    try:
        # A task that ended refuses conversation input; the caller must create new work.
        store._connection.execute(
            "UPDATE tasks SET status = 'completed', reason = 'work_completed' WHERE task_id = 't1'"
        )
        refused = run(store.admit_input(_admission("late")))
        assert refused.refusal_code == RefusalCode.TASK_TERMINAL.value
        store._connection.execute(
            "UPDATE tasks SET status = 'cancelling', reason = 'cancel_requested', "
            "cancel_requested_at = 1.0 WHERE task_id = 't1'"
        )
        conversation = run(store.admit_input(_admission("during-cancel")))
        assert conversation.refusal_code == RefusalCode.CANCELLED_FIRST.value
        decision = run(
            store.admit_input(
                InputAdmission(
                    namespace=NAMESPACE,
                    session_id=SESSION,
                    task_id="t1",
                    input_id="decision-1",
                    content=InputContent(
                        kind=InputKind.APPROVAL_DECISION, decision="approved"
                    ),
                )
            )
        )
        assert decision.outcome is AdmissionOutcome.ACCEPTED, (
            "a decision that was already requested must still be recordable while cancelling"
        )
    finally:
        store.close()


# -- I-02: two connections, one claim, one fence ------------------------------------


def test_two_connections_admitting_the_same_input_accept_it_once(tmp_path: Path) -> None:
    path = tmp_path / "control.db"
    owner = _store(path)
    other = SqliteControlStore(path)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(lambda store=store: run(store.admit_input(_admission("i1"))))
                for store in (owner, other)
            ]
            results = [future.result() for future in futures]
        outcomes = sorted(result.outcome.value for result in results)
        assert outcomes == ["accepted", "duplicate"], outcomes
        assert len(run(owner.read_inputs("t1"))) == 2  # initial + one accepted input
    finally:
        owner.close()
        other.close()


def test_two_connections_racing_for_the_execution_right_yield_one_claim(tmp_path: Path) -> None:
    path = tmp_path / "control.db"
    owner = _store(path)
    other = SqliteControlStore(path)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(
                    lambda store=store, holder=holder: run(
                        store.claim_task("t1", holder=holder, ttl_seconds=30.0)
                    )
                )
                for store, holder in ((owner, "w1"), (other, "w2"))
            ]
            claims = [future.result() for future in futures]
        claimed = [claim for claim in claims if claim is not None]
        assert len(claimed) == 1, claims
        assert claimed[0].fencing_token == 1
        task = run(owner.read_task("t1"))
        assert task is not None and task.status is TaskStatus.RUNNING
        assert task.current_attempt_id == claimed[0].attempt_id
        assert task.attempt_count == 1
    finally:
        owner.close()
        other.close()


def test_a_real_child_process_admits_into_the_same_store(tmp_path: Path) -> None:
    path = tmp_path / "control.db"
    store = _store(path)
    try:
        code = (
            "import asyncio, sys\n"
            "from yanxu_harness.tasks.inbox import SqliteControlStore\n"
            "from yanxu_harness.tasks import InputAdmission, InputContent, InputKind\n"
            "async def main():\n"
            "    store = SqliteControlStore(sys.argv[1])\n"
            "    result = await store.admit_input(InputAdmission(\n"
            "        namespace='project-a', session_id='s1', task_id='t1', input_id='from-child',\n"
            "        content=InputContent(kind=InputKind.FOLLOW_UP, text='from the child')))\n"
            "    print(result.outcome.value, result.record.sequence)\n"
            "    store.close()\n"
            "asyncio.run(main())\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", code, str(path)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        assert completed.stdout.split()[0] == "accepted"
        kinds = [record.input_id for record in run(store.read_inputs("t1"))]
        assert "from-child" in kinds, "a second process's acceptance is visible to the first"
        assert run(store.read_inbox_revision("t1")) == 2
    finally:
        store.close()


# -- capacity, claims and restarts --------------------------------------------------


def test_a_full_backlog_is_refused_and_never_evicted(tmp_path: Path) -> None:
    path = tmp_path / "control.db"
    store = SqliteControlStore(path, backlog=BacklogLimits(per_task=2))
    try:
        run(store.admit_task(_task_admission()))
        accepted = run(store.admit_input(_admission("i1")))
        assert accepted.outcome is AdmissionOutcome.ACCEPTED  # initial + i1 = 2
        refused = run(store.admit_input(_admission("i2")))
        assert refused.outcome is AdmissionOutcome.REFUSED
        assert refused.refusal_code == RefusalCode.BACKLOG_FULL.value
        assert [record.input_id for record in run(store.read_inputs("t1"))] == ["initial", "i1"], (
            "reaching the limit refuses the new admission; it never drops an accepted input"
        )
    finally:
        store.close()


def test_an_oversized_payload_is_refused_before_it_is_stored(tmp_path: Path) -> None:
    path = tmp_path / "control.db"
    store = SqliteControlStore(path, backlog=BacklogLimits(max_payload_bytes=64))
    try:
        run(store.admit_task(_task_admission()))
        refused = run(store.admit_input(_admission("big", _steering("x" * 500))))
        assert refused.refusal_code == RefusalCode.PAYLOAD_TOO_LARGE.value
        assert [record.input_id for record in run(store.read_inputs("t1"))] == ["initial"]
    finally:
        store.close()


def test_an_expired_claim_can_be_retaken_and_the_stale_one_cannot_commit(tmp_path: Path) -> None:
    path = tmp_path / "control.db"
    store = _store(path)
    try:
        first = run(store.claim_task("t1", holder="w1", ttl_seconds=10.0, now=1_000.0))
        assert first is not None and first.fencing_token == 1
        # While it is live, nobody else may claim it.
        assert run(store.claim_task("t1", holder="w2", ttl_seconds=10.0, now=1_005.0)) is None
        # After it expires a new worker may take it: expiry proves the *scheduling* right lapsed.
        second = run(store.claim_task("t1", holder="w2", ttl_seconds=10.0, now=1_020.0))
        assert second is not None and second.fencing_token == 2
        assert second.attempt_id != first.attempt_id
        task = run(store.read_task("t1"))
        assert task is not None and task.status is TaskStatus.RUNNING
        assert task.attempt_count == 2, "a re-claim is a new attempt, and the logical task continues"
        # The expired claim cannot land a late write: checked inside the committing transaction.
        with pytest.raises(ClaimLost):
            run(
                store.commit_run_transition_with_inputs(
                    "r1", {"step": 1}, expected_version=None, claim=first
                )
            )
        assert run(store.load_run("r1")) is None, (
            "a lost claim must leave no run state behind; that is what the fence is for"
        )
    finally:
        store.close()


def test_renewing_and_releasing_a_claim(tmp_path: Path) -> None:
    path = tmp_path / "control.db"
    store = _store(path)
    try:
        claim = run(store.claim_task("t1", holder="w1", ttl_seconds=10.0, now=1_000.0))
        assert claim is not None
        renewed = run(
            store.renew_task_claim(
                "t1", holder="w1", attempt_id=claim.attempt_id, ttl_seconds=10.0, now=1_005.0
            )
        )
        assert renewed is not None and renewed.fencing_token == claim.fencing_token
        assert renewed.expires_at > claim.expires_at
        assert run(
            store.renew_task_claim("t1", holder="w2", attempt_id=claim.attempt_id, ttl_seconds=10.0)
        ) is None
        released = run(
            store.release_task_claim("t1", holder="w1", attempt_id=claim.attempt_id, now=1_006.0)
        )
        assert released is not None and released.status is TaskStatus.QUEUED, (
            "giving up the execution right returns the task to the queue, it does not fail it"
        )
        assert released.reason == "execution_right_lost"
        assert run(store.claim_task("t1", holder="w2", ttl_seconds=10.0, now=1_007.0)) is not None
    finally:
        store.close()


def test_a_restart_keeps_the_sequence_and_the_deduplication(tmp_path: Path) -> None:
    path = tmp_path / "control.db"
    store = _store(path)
    try:
        run(store.admit_input(_admission("i1")))
    finally:
        store.close()
    reopened = SqliteControlStore(path)
    try:
        duplicate = run(reopened.admit_input(_admission("i1")))
        assert duplicate.outcome is AdmissionOutcome.DUPLICATE
        assert duplicate.record is not None and duplicate.record.sequence == 1
        fresh = run(reopened.admit_input(_admission("i2")))
        assert fresh.outcome is AdmissionOutcome.ACCEPTED
        assert fresh.record is not None and fresh.record.sequence == 2, (
            "the sequence continues where the store left off; a restart does not reuse it"
        )
    finally:
        reopened.close()


# -- atomicity ----------------------------------------------------------------------


def test_a_commit_writes_the_run_the_inputs_and_the_notices_or_nothing(tmp_path: Path) -> None:
    path = tmp_path / "control.db"
    store = _store(path)
    try:
        accepted = run(store.admit_input(_admission("i1")))
        claim = run(store.claim_task("t1", holder="w1", ttl_seconds=30.0))
        assert accepted.record is not None and claim is not None
        notice = ControlOutboxEntry(
            outbox_id="o1", kind="input_applied", target_task_id="t1", namespace=NAMESPACE
        )
        version, written = run(
            store.commit_run_transition_with_inputs(
                "r1",
                {"step": 1},
                expected_version=None,
                applied_inputs=[accepted.record],
                notifications=[notice],
                claim=claim,
            )
        )
        assert version == 1 and written == ()
        stored = [item for item in run(store.read_inputs("t1")) if item.input_id == "i1"][0]
        assert stored.state is InputState.APPLIED
        assert stored.applied_run_revision == version, (
            "applied must name the run revision that took it into the durable state"
        )
        assert [entry.outbox_id for entry in run(store.read_outbox())] == ["o1"]
        assert run(store.read_inbox_revision("t1")) == 3  # initial + i1 + applied
    finally:
        store.close()


def test_a_failed_commit_leaves_nothing_behind(tmp_path: Path) -> None:
    path = tmp_path / "control.db"
    store = _store(path)
    try:
        accepted = run(store.admit_input(_admission("i1")))
        claim = run(store.claim_task("t1", holder="w1", ttl_seconds=30.0))
        assert accepted.record is not None and claim is not None
        # The claim is released by another worker between the read and the commit, so the commit
        # must roll back entirely: no run state, no applied input, no notice.
        run(store.release_task_claim("t1", holder="w1", attempt_id=claim.attempt_id))
        with pytest.raises(ClaimLost):
            run(
                store.commit_run_transition_with_inputs(
                    "r1",
                    {"step": 1},
                    expected_version=None,
                    applied_inputs=[accepted.record],
                    notifications=[
                        ControlOutboxEntry(outbox_id="o1", kind="x", target_task_id="t1")
                    ],
                    claim=claim,
                )
            )
        assert run(store.load_run("r1")) is None
        stored = [item for item in run(store.read_inputs("t1")) if item.input_id == "i1"][0]
        assert stored.state is InputState.PENDING
        assert run(store.read_outbox()) == ()
    finally:
        store.close()


def test_an_input_is_not_applied_twice(tmp_path: Path) -> None:
    path = tmp_path / "control.db"
    store = _store(path)
    try:
        accepted = run(store.admit_input(_admission("i1")))
        claim = run(store.claim_task("t1", holder="w1", ttl_seconds=30.0))
        assert accepted.record is not None and claim is not None
        run(
            store.commit_run_transition_with_inputs(
                "r1", {"step": 1}, expected_version=None, applied_inputs=[accepted.record],
                claim=claim,
            )
        )
        with pytest.raises(ContractError):
            run(
                store.commit_run_transition_with_inputs(
                    "r1", {"step": 2}, expected_version=1, applied_inputs=[accepted.record],
                    claim=claim,
                )
            )
    finally:
        store.close()


# -- budgets and children -----------------------------------------------------------


def test_a_budget_reservation_is_idempotent_and_refused_before_dispatch(tmp_path: Path) -> None:
    path = tmp_path / "control.db"
    store = _store(path)
    try:
        reservation = BudgetReservation(
            root_task_id="t1",
            task_id="t1",
            unit=BudgetUnit.MODEL_REQUESTS,
            amount=2,
            reservation_id="res1",
            dispatch_identity="dispatch:r1:req1",
        )
        first = run(
            store.reserve_budget(
                root_task_id="t1", task_id="t1", reservation=reservation, limit=3
            )
        )
        replay = run(
            store.reserve_budget(
                root_task_id="t1", task_id="t1", reservation=reservation, limit=3
            )
        )
        assert replay.reservation_id == first.reservation_id
        assert len(run(store.read_reservations("t1"))) == 1, (
            "the same dispatch identity reserves once: a replay is not a second spend"
        )
        with pytest.raises(BudgetRefused) as refused:
            run(
                store.reserve_budget(
                    root_task_id="t1",
                    task_id="t1",
                    reservation=BudgetReservation(
                        root_task_id="t1",
                        task_id="t1",
                        unit=BudgetUnit.MODEL_REQUESTS,
                        amount=2,
                        reservation_id="res2",
                        dispatch_identity="dispatch:r1:req2",
                    ),
                    limit=3,
                )
            )
        assert refused.value.available == 1, "the refusal states the arithmetic that decided it"
    finally:
        store.close()


def test_settling_and_unknown_reservations_are_counted_once(tmp_path: Path) -> None:
    path = tmp_path / "control.db"
    store = _store(path)
    try:
        reservation = BudgetReservation(
            root_task_id="t1", task_id="t1", unit=BudgetUnit.SECONDS, amount=5,
            reservation_id="res1", dispatch_identity="dispatch:1",
        )
        run(store.reserve_budget(root_task_id="t1", task_id="t1", reservation=reservation))
        settled = run(
            store.settle_budget("res1", state="settled", settled_amount=1.5)
        )
        assert settled.state is ReservationState.SETTLED and settled.settled_amount == 1.5
        replay = run(store.settle_budget("res1", state="settled", settled_amount=1.5))
        assert replay.settled_amount == 1.5, "an idempotent replay changes nothing"
        with pytest.raises(ContractError):
            run(store.settle_budget("res1", state="released"))
        unknown = BudgetReservation(
            root_task_id="t1", task_id="t1", unit=BudgetUnit.TOKENS, amount=100,
            reservation_id="res2", dispatch_identity="dispatch:2",
        )
        run(store.reserve_budget(root_task_id="t1", task_id="t1", reservation=unknown))
        marked = run(
            store.settle_budget("res2", state="unknown", note="no usage was reported")
        )
        assert marked.state is ReservationState.UNKNOWN and marked.note
        # An unknown reservation still counts against the allowance: unknown is not zero.
        with pytest.raises(BudgetRefused):
            run(
                store.reserve_budget(
                    root_task_id="t1",
                    task_id="t1",
                    reservation=BudgetReservation(
                        root_task_id="t1", task_id="t1", unit=BudgetUnit.TOKENS, amount=1,
                        reservation_id="res3", dispatch_identity="dispatch:3",
                    ),
                    limit=100,
                )
            )
    finally:
        store.close()


def test_a_child_is_registered_once_per_spawn_identity(tmp_path: Path) -> None:
    path = tmp_path / "control.db"
    store = _store(path)
    try:
        claim = run(store.claim_task("t1", holder="w1", ttl_seconds=30.0))
        assert claim is not None
        relation = ChildRelation(
            parent_task_id="t1",
            child_task_id="t2",
            spawn_identity="spawn:1",
            namespace=NAMESPACE,
            session_id=SESSION,
        )
        reservation = BudgetReservation(
            root_task_id="t1", task_id="t2", unit=BudgetUnit.MODEL_REQUESTS, amount=1,
            reservation_id="child-res1", dispatch_identity="spawn:1",
        )
        notice = ControlOutboxEntry(
            outbox_id="child-o1", kind="child_scheduled", target_task_id="t2"
        )
        stored = run(
            store.register_child(
                relation, claim=claim, reservation=reservation, notification=notice, limit=5
            )
        )
        assert stored.budget_reservation_id == "child-res1"
        assert [item.spawn_identity for item in run(store.read_children("t1"))] == ["spawn:1"]
        assert [item.outbox_id for item in run(store.read_outbox())] == ["child-o1"]
        assert len(run(store.read_reservations("t1"))) == 1
        # A retry with the same identity returns the same relation: no second child.
        again = run(store.register_child(relation, claim=claim))
        assert again.spawn_identity == "spawn:1"
        assert len(run(store.read_children("t1"))) == 1
        # The same identity with a different child is a conflict, not a second registration.
        with pytest.raises(ContractError):
            run(
                store.register_child(
                    ChildRelation(
                        parent_task_id="t1", child_task_id="t3", spawn_identity="spawn:1"
                    ),
                    claim=claim,
                )
            )
    finally:
        store.close()


# -- read-only connections and schema versions --------------------------------------


def test_a_read_only_control_store_reads_and_writes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "control.db"
    store = _store(path)
    try:
        run(store.admit_input(_admission("i1")))
    finally:
        store.close()
    reader = SqliteControlStore(path, read_only=True)
    try:
        assert [record.input_id for record in run(reader.read_inputs("t1"))] == ["initial", "i1"]
        assert run(reader.read_task("t1")) is not None
        with pytest.raises(ContractError):
            run(reader.admit_input(_admission("i2")))
        with pytest.raises(ContractError):
            run(reader.admit_task(_task_admission(task_id="t2", run_id="r2")))
        with pytest.raises(ContractError):
            run(reader.claim_task("t1", holder="w1", ttl_seconds=1.0))
    finally:
        reader.close()


def test_a_reader_does_not_create_a_store_and_refuses_a_missing_control_schema(
    tmp_path: Path,
) -> None:
    with pytest.raises(ContractError):
        SqliteControlStore(tmp_path / "absent.db", read_only=True)
    # A plain runs store (no control tables) is refused by a control reader rather than guessed at.
    from yanxu_harness.storage import SqlitePersistence

    plain = tmp_path / "runs.db"
    SqlitePersistence(plain).close()
    with pytest.raises(ContractError) as error:
        SqliteControlStore(plain, read_only=True)
    assert "no control schema" in str(error.value)


def test_an_unknown_future_control_schema_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "control.db"
    store = _store(path)
    store.close()
    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE control_meta SET value = ? WHERE key = 'control_schema_version'",
        (json.dumps(CONTROL_SCHEMA_VERSION + 1),),
    )
    connection.commit()
    connection.close()
    with pytest.raises(VersionRefused):
        SqliteControlStore(path)
    with pytest.raises(VersionRefused):
        SqliteControlStore(path, read_only=True)


def test_the_control_schema_version_is_recorded(tmp_path: Path) -> None:
    store = _store(tmp_path / "control.db")
    try:
        assert store.control_schema_version == CONTROL_SCHEMA_VERSION
    finally:
        store.close()


# -- the volatile reference store ---------------------------------------------------


def test_the_memory_store_implements_the_same_contract_and_is_volatile() -> None:
    store = MemoryControlStore()
    assert store.durability == "VOLATILE"
    run(store.admit_task(_task_admission()))
    first = run(store.admit_input(_admission("i1")))
    assert first.outcome is AdmissionOutcome.ACCEPTED and first.record.sequence == 1
    duplicate = run(store.admit_input(_admission("i1")))
    assert duplicate.outcome is AdmissionOutcome.DUPLICATE
    conflict = run(store.admit_input(_admission("i1", _steering("other"))))
    assert conflict.refusal_code == RefusalCode.CONTENT_CONFLICT.value
    claim = run(store.claim_task("t1", holder="w1", ttl_seconds=10.0))
    assert claim is not None
    accepted = first.record
    version = run(
        store.commit_run_transition_with_inputs(
            "r1", {"step": 1}, expected_version=None, applied_inputs=[accepted], claim=claim
        )
    )
    applied = [item for item in run(store.read_inputs("t1")) if item.input_id == "i1"][0]
    assert applied.applied_run_revision == version
    import time as _time

    later = _time.time() + 3_600.0
    assert run(store.claim_task("t1", holder="w2", ttl_seconds=10.0, now=later)) is not None
    with pytest.raises(ClaimLost):
        run(
            store.commit_run_transition_with_inputs(
                "r1", {"step": 2}, expected_version=version, claim=claim
            )
        )
    # A second instance shares nothing: that is what volatile means, and the store says so.
    other = MemoryControlStore()
    assert run(other.read_task("t1")) is None


def test_two_connections_racing_for_the_last_capacity_slot(tmp_path: Path) -> None:
    """The capacity limit is enforced by the transaction, not by counting first and hoping."""
    from concurrent.futures import ThreadPoolExecutor as _Pool

    path = tmp_path / "control.db"
    owner = SqliteControlStore(path, backlog=BacklogLimits(per_task=2))
    other = SqliteControlStore(path, backlog=BacklogLimits(per_task=2))
    try:
        run(owner.admit_task(_task_admission()))  # initial input: one slot of two is used
        with _Pool(max_workers=2) as pool:
            futures = [
                pool.submit(lambda store=store, i=input_id: run(store.admit_input(_admission(i))))
                for store, input_id in ((owner, "i1"), (other, "i2"))
            ]
            results = [future.result() for future in futures]
        outcomes = sorted(result.outcome.value for result in results)
        assert outcomes == ["accepted", "refused"], outcomes
        refused = [result for result in results if result.outcome is AdmissionOutcome.REFUSED][0]
        assert refused.refusal_code == RefusalCode.BACKLOG_FULL.value
        stored = run(owner.read_inputs("t1"))
        assert len(stored) == 2, "the accepted input is still there; capacity never evicts"
        assert [record.sequence for record in stored] == [0, 1]
    finally:
        owner.close()
        other.close()
