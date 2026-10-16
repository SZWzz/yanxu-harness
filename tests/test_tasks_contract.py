"""Q0: the task/input/execution-right contract, tested as a contract.

Nothing here touches a store, a clock or a worker. That is the point: the vocabulary the rest of
the orchestration is built from — the status table, the admission decisions, the input order, the
claim and reservation rules, the capability refusal — is decided by pure functions and validated
records, so a later implementation can be checked against a fixed contract rather than against
whatever it happened to do.

The tests are grouped as the plan's §5 states the contract:

* the status table: every legal edge allowed, every illegal edge refused, terminal states final;
* tasks: what a record must carry, and what it must not (the run's status is not here);
* inputs: kinds, sources, admission outcomes, refusal codes, state invariants, ordering limits;
* execution rights, children and budgets: the ordering and idempotency rules;
* the capability port: a plain single-run backend is refused *before* admission.
"""

from __future__ import annotations

from dataclasses import replace
from itertools import product

import pytest

from yanxu_harness.errors import ContractError, VersionRefused
from yanxu_harness.tasks import (
    COORDINATION_METHODS,
    COORDINATION_READ_METHODS,
    AdmissionOutcome,
    AdmissionResult,
    BacklogLimits,
    BudgetReservation,
    BudgetUnit,
    ChildRelation,
    ChildSpawnState,
    ControlOutboxEntry,
    DispatchClaim,
    InputAdmission,
    InputContent,
    InputKind,
    InputRead,
    InputRecord,
    InputState,
    MemoryControlStore,
    RefusalCode,
    ReservationState,
    TaskAdmission,
    TaskRecord,
    TaskStatus,
    assert_transition,
    can_transition,
    coordination_capability,
    decide_admission,
    is_coordination_store,
    require_coordination,
    require_input_polling,
    select_inputs,
    transition_table,
)


# -- the status table ---------------------------------------------------------------


def _all_pairs() -> list[tuple[TaskStatus, TaskStatus]]:
    return [(start, end) for start, end in product(TaskStatus, TaskStatus) if start is not end]


def test_every_documented_edge_is_allowed_and_nothing_else_is() -> None:
    table = transition_table()
    documented = {
        (start, end): reasons for (start, end), reasons in table.items()
    }
    seen: set[tuple[str, str]] = set()
    for start, end in _all_pairs():
        key = (start.value, end.value)
        reasons = documented.get(key)
        if reasons is None:
            # Not an edge: no reason may make it legal.
            assert not can_transition(start, end, "anything"), key
            with pytest.raises(ContractError):
                assert_transition(start, end, "anything")
            continue
        seen.add(key)
        for reason in reasons:
            assert can_transition(start, end, reason)
            assert_transition(start, end, reason)
        assert not can_transition(start, end, "not-a-reason"), key
        with pytest.raises(ContractError):
            assert_transition(start, end, "not-a-reason")
    assert seen == set(documented)


def test_terminal_states_have_no_outgoing_edge() -> None:
    for status in TaskStatus:
        if not status.is_terminal:
            continue
        for end in TaskStatus:
            if end is status:
                continue
            assert not can_transition(status, end, "anything"), (status, end)
        with pytest.raises(ContractError) as error:
            assert_transition(status, TaskStatus.RUNNING, "work_completed")
        assert "terminal" in str(error.value)


def test_staying_put_is_not_a_transition() -> None:
    for status in TaskStatus:
        assert_transition(status, status, "anything")


def test_the_table_covers_the_states_the_plan_names() -> None:
    assert {status.value for status in TaskStatus} == {
        "queued",
        "running",
        "waiting",
        "cancelling",
        "completed",
        "failed",
        "cancelled",
        "usage_limited",
    }
    assert {status.value for status in TaskStatus if status.is_terminal} == {
        "completed",
        "failed",
        "cancelled",
        "usage_limited",
    }
    # Only a running task occupies a worker slot.
    assert [status.value for status in TaskStatus if status.holds_execution_slot] == ["running"]


# -- tasks --------------------------------------------------------------------------


def _task(**changes: object) -> TaskRecord:
    base = TaskRecord(
        namespace="project-a",
        session_id="s1",
        task_id="t1",
        run_id="r1",
        root_task_id="t1",
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def test_a_running_task_must_name_the_attempt_that_holds_the_right() -> None:
    with pytest.raises(ContractError):
        _task(status=TaskStatus.RUNNING)
    running = _task(status=TaskStatus.RUNNING, current_attempt_id="a1")
    assert running.current_attempt_id == "a1"


def test_a_waiting_task_must_name_what_it_waits_for() -> None:
    with pytest.raises(ContractError):
        _task(status=TaskStatus.WAITING)
    assert _task(status=TaskStatus.WAITING, waiting_for="child:t2").waiting_for == "child:t2"


def test_a_cancelling_task_must_record_when_cancellation_was_requested() -> None:
    with pytest.raises(ContractError):
        _task(status=TaskStatus.CANCELLING)
    assert _task(status=TaskStatus.CANCELLING, cancel_requested_at=10.0).cancel_requested_at == 10.0


def test_a_terminal_task_must_say_why_it_ended() -> None:
    with pytest.raises(ContractError):
        _task(status=TaskStatus.COMPLETED, reason="")
    assert _task(status=TaskStatus.COMPLETED, reason="work_completed").is_terminal


def test_a_task_record_does_not_carry_the_runs_status() -> None:
    """The run is the authority on the run; a cached copy is how two truths disagree."""
    fields = set(TaskRecord.__dataclass_fields__)
    assert "run_status" not in fields
    assert not {"status_detail", "run_state", "run_outcome"} & fields
    assert "run_id" in fields


def test_an_admission_that_retries_must_be_recognisable_as_the_same_task() -> None:
    request = TaskAdmission(
        namespace="project-a",
        session_id="s1",
        task_id="t1",
        run_id="r1",
        initial_input=InputContent(kind=InputKind.INITIAL, text="do the thing"),
    )
    assert request.identity["root_task_id"] == "t1"
    with pytest.raises(ContractError):
        TaskAdmission(
            namespace="project-a",
            session_id="s1",
            task_id="t1",
            run_id="r1",
            initial_input=InputContent(kind=InputKind.FOLLOW_UP, text="later"),
        )


def test_a_child_task_must_name_its_parent() -> None:
    with pytest.raises(ContractError):
        TaskAdmission(
            namespace="project-a", session_id="s1", task_id="t2", run_id="r2", depth=1
        )
    with pytest.raises(ContractError):
        TaskAdmission(
            namespace="project-a",
            session_id="s1",
            task_id="t2",
            run_id="r2",
            depth=0,
            parent_task_id="t1",
        )
    child = TaskAdmission(
        namespace="project-a",
        session_id="s1",
        task_id="t2",
        run_id="r2",
        depth=1,
        parent_task_id="t1",
        root_task_id="t1",
    )
    assert child.depth == 1


def test_a_transition_keeps_the_attempt_that_finished_the_task() -> None:
    running = _task(status=TaskStatus.RUNNING, current_attempt_id="a2")
    done = running.transition(TaskStatus.COMPLETED, reason="work_completed", at=5.0)
    assert done.current_attempt_id == "a2", "which claim finished the work is a fact to keep"
    assert done.waiting_for == ""


def test_cancellation_is_a_request_and_is_idempotent() -> None:
    task = _task()
    once = task.request_cancel(at=1.0)
    assert once.cancel_requested_at == 1.0
    assert once.request_cancel(at=2.0).cancel_requested_at == 1.0
    with pytest.raises(ContractError):
        _task(status=TaskStatus.COMPLETED, reason="work_completed").request_cancel(at=3.0)


def test_task_records_round_trip_and_refuse_a_future_version() -> None:
    record = _task(status=TaskStatus.WAITING, waiting_for="child:t2")
    again = TaskRecord.from_record(record.to_record())
    assert again.to_record() == record.to_record()
    bad = dict(record.to_record())
    bad["schema_version"] = 99
    with pytest.raises(VersionRefused):
        TaskRecord.from_record(bad)


# -- inputs -------------------------------------------------------------------------


def _admission(
    *,
    kind: InputKind = InputKind.STEERING,
    text: str = "keep going",
    input_id: str = "i1",
    source: str = "host",
    parameters: dict | None = None,
) -> InputAdmission:
    return InputAdmission(
        namespace="project-a",
        session_id="s1",
        task_id="t1",
        input_id=input_id,
        content=InputContent(kind=kind, text=text, parameters=parameters or {}),
        source=source,
    )


def _record(admission: InputAdmission, **changes: object) -> InputRecord:
    base = InputRecord(
        namespace=admission.namespace,
        session_id=admission.session_id,
        task_id=admission.task_id,
        input_id=admission.input_id,
        kind=admission.content.kind,
        sequence=0,
        content_digest=admission.content.digest,
        content=admission.content,
        source=admission.source,
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def test_a_new_input_is_accepted_and_a_retry_is_a_duplicate() -> None:
    admission = _admission()
    first = decide_admission(existing=None, incoming=admission, task_status=TaskStatus.QUEUED)
    assert first.outcome is AdmissionOutcome.ACCEPTED
    assert first.accepted is True and first.applied is False
    duplicate = decide_admission(
        existing=_record(admission, sequence=3, state=InputState.PENDING),
        incoming=admission,
        task_status=TaskStatus.QUEUED,
    )
    assert duplicate.outcome is AdmissionOutcome.DUPLICATE
    assert duplicate.duplicate_of == "i1"
    assert duplicate.record is not None and duplicate.record.sequence == 3
    assert duplicate.accepted is False, (
        "a duplicate is not a second acceptance: it must not consume backlog twice"
    )


def test_the_same_key_with_different_content_is_a_conflict() -> None:
    admission = _admission()
    other = _admission(text="something else")
    result = decide_admission(
        existing=_record(admission), incoming=other, task_status=TaskStatus.RUNNING
    )
    assert result.outcome is AdmissionOutcome.REFUSED
    assert result.refusal_code == RefusalCode.CONTENT_CONFLICT.value


def test_a_model_sourced_input_cannot_become_a_control_entry() -> None:
    with pytest.raises(ContractError):
        _admission(kind=InputKind.CANCEL_REQUEST, source="model")
    # And if such a record somehow arrives, the decision refuses it rather than trusting it.
    forged = InputAdmission(
        namespace="project-a",
        session_id="s1",
        task_id="t1",
        input_id="i9",
        content=InputContent(kind=InputKind.APPROVAL_DECISION, decision="approved"),
        source="host",
    )
    object.__setattr__(forged, "source", "model")
    result = decide_admission(existing=None, incoming=forged, task_status=TaskStatus.RUNNING)
    assert result.refusal_code == RefusalCode.MODEL_CANNOT_CONTROL.value


def test_a_terminal_task_refuses_new_input() -> None:
    result = decide_admission(
        existing=None, incoming=_admission(), task_status=TaskStatus.COMPLETED
    )
    assert result.refusal_code == RefusalCode.TASK_TERMINAL.value
    assert result.record is None


def test_a_cancel_request_first_refuses_conversation_but_not_control() -> None:
    conversation = decide_admission(
        existing=None,
        incoming=_admission(),
        task_status=TaskStatus.CANCELLING,
        cancel_requested=True,
    )
    assert conversation.refusal_code == RefusalCode.CANCELLED_FIRST.value
    decision = decide_admission(
        existing=None,
        incoming=_admission(kind=InputKind.APPROVAL_DECISION, text="", input_id="i2"),
        task_status=TaskStatus.CANCELLING,
        cancel_requested=True,
    )
    assert decision.outcome is AdmissionOutcome.ACCEPTED, (
        "a decision that was already requested must still be recordable while cancelling"
    )


def test_a_full_or_oversized_backlog_is_refused_without_dropping_anything() -> None:
    limits = BacklogLimits(per_task=2, per_namespace=4, total=8, max_payload_bytes=256)
    full = decide_admission(
        existing=None,
        incoming=_admission(),
        task_status=TaskStatus.RUNNING,
        pending_count=2,
        pending_bytes=0,
        limits=limits,
    )
    assert full.refusal_code == RefusalCode.BACKLOG_FULL.value
    heavy = decide_admission(
        existing=None,
        incoming=_admission(text="x" * 500),
        task_status=TaskStatus.RUNNING,
        pending_count=0,
        pending_bytes=0,
        limits=limits,
    )
    assert heavy.refusal_code == RefusalCode.PAYLOAD_TOO_LARGE.value
    byte_full = decide_admission(
        existing=None,
        incoming=_admission(text="y" * 30),
        task_status=TaskStatus.RUNNING,
        pending_count=0,
        pending_bytes=limits.max_task_bytes,
        limits=limits,
    )
    assert byte_full.refusal_code == RefusalCode.BACKLOG_FULL.value


def test_the_content_digest_ignores_encoding_noise_but_not_meaning() -> None:
    a = InputContent(kind=InputKind.STEERING, text="keep going", parameters={"b": 1, "a": 2})
    b = InputContent(kind=InputKind.STEERING, text="keep going", parameters={"a": 2, "b": 1})
    assert a.digest == b.digest, "a parameter mapping is not ordered"
    assert a.digest != replace(a, text="keep going!").digest
    assert a.digest != replace(a, kind=InputKind.FOLLOW_UP).digest, (
        "the same text as a follow-up is a different input from the same text as steering"
    )


def test_claimed_is_a_temporary_right_and_applied_needs_a_run_revision() -> None:
    record = _record(_admission())
    claimed = record.claim("worker-1", expires_at=100.0, now=0.0)
    assert claimed.state is InputState.CLAIMED and claimed.attempts == 1
    with pytest.raises(ContractError):
        claimed.claim("worker-2", expires_at=200.0, now=1.0)
    # An expired claim can be taken again: a worker that died mid-consumption must not strand the
    # input. This is *not* evidence about whether the run applied it.
    retaken = claimed.claim("worker-2", expires_at=200.0, now=101.0)
    assert retaken.claimed_by == "worker-2" and retaken.attempts == 2
    with pytest.raises(ContractError):
        replace(retaken, state=InputState.APPLIED)
    applied = retaken.apply(run_revision=7, event_sequence=42)
    assert applied.applied_run_revision == 7 and applied.claimed_by == ""
    with pytest.raises(ContractError):
        applied.claim("worker-3", expires_at=300.0)
    with pytest.raises(ContractError):
        applied.cancel()
    with pytest.raises(ContractError):
        applied.apply(run_revision=8)


def test_refused_and_cancelled_inputs_keep_their_fact() -> None:
    record = _record(_admission())
    refused = record.refuse(RefusalCode.BACKLOG_FULL)
    assert refused.state is InputState.REFUSED and refused.refusal_code == "backlog_full"
    with pytest.raises(ContractError):
        replace(record, state=InputState.REFUSED)
    cancelled = record.cancel()
    assert cancelled.state is InputState.CANCELLED
    assert InputRecord.from_record(cancelled.to_record()).state is InputState.CANCELLED


def test_an_admission_result_that_is_refused_must_say_why() -> None:
    with pytest.raises(ContractError):
        AdmissionResult(AdmissionOutcome.REFUSED)
    with pytest.raises(ContractError):
        AdmissionResult(AdmissionOutcome.ACCEPTED, refusal_code="backlog_full")
    with pytest.raises(ContractError):
        AdmissionResult(AdmissionOutcome.ACCEPTED)
    with pytest.raises(ContractError):
        AdmissionResult(AdmissionOutcome.DUPLICATE)
    assert AdmissionResult(
        AdmissionOutcome.DUPLICATE, duplicate_of="i1"
    ).outcome is AdmissionOutcome.DUPLICATE


def test_steering_is_taken_whole_and_a_follow_up_one_at_a_time() -> None:
    records = [
        _record(_admission(input_id="s1", text="a"), sequence=0),
        _record(_admission(input_id="s2", text="b"), sequence=1),
        _record(_admission(kind=InputKind.FOLLOW_UP, input_id="f1"), sequence=2),
        _record(_admission(kind=InputKind.FOLLOW_UP, input_id="f2"), sequence=3),
    ]
    steering = select_inputs(records)
    assert [item.input_id for item in steering.taken] == ["s1", "s2"]
    assert [item.input_id for item in steering.waiting] == ["f1", "f2"]
    remaining = select_inputs([item for item in records if item.kind is InputKind.FOLLOW_UP])
    assert [item.input_id for item in remaining.taken] == ["f1"]


def test_a_cancel_request_is_never_taken_as_conversation() -> None:
    records = [
        _record(
            _admission(kind=InputKind.CANCEL_REQUEST, text="", input_id="c1"), sequence=0
        )
    ]
    selection = select_inputs(records)
    assert selection.taken == ()
    assert "no claimable conversation input" in selection.reason


def test_selection_is_bounded_and_keeps_the_accepted_order() -> None:
    records = [
        _record(_admission(input_id=f"s{index}"), sequence=index) for index in range(5)
    ]
    selection = select_inputs(records, limit=2)
    assert [item.sequence for item in selection.taken] == [0, 1]
    assert [item.sequence for item in selection.waiting] == [2, 3, 4]


# -- claims, children, budgets, outbox ----------------------------------------------


def test_a_claim_is_fenced_and_expires_against_a_caller_supplied_clock() -> None:
    claim = DispatchClaim(
        task_id="t1", holder="worker-1", attempt_id="a1", fencing_token=1,
        claimed_at=0.0, expires_at=10.0,
    )
    assert claim.holds(holder="worker-1", attempt_id="a1")
    assert not claim.holds(holder="worker-2", attempt_id="a1")
    assert claim.expired_at(9.9) is False and claim.expired_at(10.0) is True
    with pytest.raises(ContractError):
        replace(claim, fencing_token=0)
    with pytest.raises(ContractError):
        replace(claim, expires_at=0.0)
    assert DispatchClaim.from_record(claim.to_record()).to_record() == claim.to_record()


def test_a_child_cannot_be_its_own_parent() -> None:
    with pytest.raises(ContractError):
        ChildRelation(parent_task_id="t1", child_task_id="t1", spawn_identity="sp1")
    relation = ChildRelation(
        parent_task_id="t1",
        child_task_id="t2",
        spawn_identity="sp1",
        state=ChildSpawnState.REGISTERED,
    )
    assert ChildRelation.from_record(relation.to_record()).state is ChildSpawnState.REGISTERED


def test_a_budget_reservation_settles_once_and_releases_only_unstarted_work() -> None:
    reservation = BudgetReservation(
        root_task_id="t1",
        task_id="t1",
        unit=BudgetUnit.MODEL_REQUESTS,
        amount=2,
        reservation_id="res1",
        dispatch_identity="dispatch:1",
    )
    settled = reservation.settle(1, at=5.0)
    assert settled.state is ReservationState.SETTLED and settled.settled_amount == 1
    with pytest.raises(ContractError):
        settled.settle(1, at=6.0)
    with pytest.raises(ContractError):
        settled.release(at=6.0)
    unknown = reservation.mark_unknown(note="the provider did not report usage")
    assert unknown.state is ReservationState.UNKNOWN
    assert BudgetReservation.from_record(unknown.to_record()).note
    released = reservation.release(at=3.0, note="the dispatch never opened")
    assert released.state is ReservationState.RELEASED
    with pytest.raises(ContractError):
        BudgetReservation(
            root_task_id="t1", task_id="t1", unit=BudgetUnit.SECONDS, amount=1,
            reservation_id="res2", dispatch_identity="dispatch:2",
            state=ReservationState.UNKNOWN,
        )


def test_a_reservation_is_keyed_by_dispatch_not_by_logical_action() -> None:
    """A recovery may re-issue the same logical action, and that is a second real request."""
    first = BudgetReservation(
        root_task_id="t1", task_id="t1", unit=BudgetUnit.MODEL_REQUESTS, amount=1,
        reservation_id="res1", dispatch_identity="dispatch:r1:a1",
    )
    second = BudgetReservation(
        root_task_id="t1", task_id="t1", unit=BudgetUnit.MODEL_REQUESTS, amount=1,
        reservation_id="res2", dispatch_identity="dispatch:r1:a2",
    )
    assert first.dispatch_identity != second.dispatch_identity


def test_the_outbox_is_durable_and_redeliverable() -> None:
    entry = ControlOutboxEntry(outbox_id="o1", kind="child_completed", target_task_id="t1")
    assert entry.pending is True
    delivered = entry.delivered(at=9.0)
    assert delivered.pending is False and delivered.attempts == 1
    failed = entry.failed(error="the store was busy")
    assert failed.pending is True and failed.attempts == 1 and failed.last_error
    assert ControlOutboxEntry.from_record(delivered.to_record()).delivered_at == 9.0


# -- the capability port -------------------------------------------------------------


def test_a_single_run_backend_is_not_a_coordination_store() -> None:
    class PlainBackend:
        async def load_run(self, run_id: str) -> None:  # pragma: no cover - never called
            return None

        async def commit_transition(self, *args: object, **kwargs: object) -> int:
            return 0

    plain = PlainBackend()
    assert coordination_capability(plain) == "none"
    assert is_coordination_store(plain) is False
    with pytest.raises(ContractError) as error:
        require_coordination(plain)
    assert "refused before anything is admitted" in str(error.value)
    assert coordination_capability(None) == "none"


def test_a_partial_store_is_refused_and_the_missing_methods_are_named() -> None:
    class HalfStore:
        async def admit_task(self, admission: TaskAdmission) -> TaskRecord:
            raise NotImplementedError

        async def admit_input(self, admission: InputAdmission) -> AdmissionResult:
            raise NotImplementedError

        async def claim_task(self, task_id: str, **kwargs: object) -> DispatchClaim | None:
            return None

    half = HalfStore()
    assert coordination_capability(half) == "partial"
    assert is_coordination_store(half) is False
    with pytest.raises(ContractError) as error:
        require_coordination(half)
    message = str(error.value)
    assert "'partial'" in message
    assert "commit_run_transition_with_inputs" in message
    assert "two independent writes" in message


def test_a_complete_store_passes_the_capability_check() -> None:
    class FullStore:
        async def admit_task(self, admission: TaskAdmission) -> TaskRecord:
            raise NotImplementedError

        async def admit_input(self, admission: InputAdmission) -> AdmissionResult:
            raise NotImplementedError

        async def claim_task(self, task_id: str, **kwargs: object) -> DispatchClaim | None:
            return None

        async def commit_run_transition_with_inputs(self, *args: object, **kwargs: object) -> int:
            return 0

        async def reserve_budget(self, **kwargs: object) -> BudgetReservation:
            raise NotImplementedError

        async def settle_budget(self, reservation_id: str, **kwargs: object) -> BudgetReservation:
            raise NotImplementedError

        async def register_child(self, relation: ChildRelation, **kwargs: object) -> ChildRelation:
            raise NotImplementedError

        async def poll_pending_inputs(
            self, task_id: str, **kwargs: object
        ) -> InputRead:
            return InputRead()

    store = FullStore()
    assert coordination_capability(store) == "full"
    assert is_coordination_store(store) is True
    assert require_coordination(store) is store


def test_a_store_without_the_pending_input_read_is_partial_and_the_reader_refuses() -> None:
    """The bounded pending-input query is required, not optional (plan §5.1).

    A store that can commit atomically but cannot query pending inputs would push the reader back
    to scanning applied history with a guessed watermark — the defect DIO2-I1 — so it is refused
    before the first read rather than approximated.
    """

    class NoPollStore:
        async def admit_task(self, admission: TaskAdmission) -> TaskRecord:
            raise NotImplementedError

        async def admit_input(self, admission: InputAdmission) -> AdmissionResult:
            raise NotImplementedError

        async def claim_task(self, task_id: str, **kwargs: object) -> DispatchClaim | None:
            return None

        async def commit_run_transition_with_inputs(self, *args: object, **kwargs: object) -> int:
            return 0

        async def reserve_budget(self, **kwargs: object) -> BudgetReservation:
            raise NotImplementedError

        async def settle_budget(self, reservation_id: str, **kwargs: object) -> BudgetReservation:
            raise NotImplementedError

        async def register_child(self, relation: ChildRelation, **kwargs: object) -> ChildRelation:
            raise NotImplementedError

    store = NoPollStore()
    assert coordination_capability(store) == "partial"
    assert is_coordination_store(store) is False
    with pytest.raises(ContractError) as error:
        require_input_polling(store)
    assert "poll_pending_inputs" in str(error.value)
    assert require_input_polling(MemoryControlStore()) is not None


def test_the_required_method_set_is_the_one_the_plan_names() -> None:
    assert set(COORDINATION_METHODS) >= {
        "admit_task",
        "admit_input",
        "claim_task",
        "commit_run_transition_with_inputs",
        "reserve_budget",
        "settle_budget",
        "register_child",
    }
    assert set(COORDINATION_READ_METHODS) == {"poll_pending_inputs"}
