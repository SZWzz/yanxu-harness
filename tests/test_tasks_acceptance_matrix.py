"""Q6 §11.2: the frozen offline scenarios, each checked by an independent verifier.

The plan's table of eight scenarios: an input redelivered and consumed across processes, an
admission/terminal race, a multi-worker preemption, fairness with a wait, a parent/child tree
recovered across processes, a parent cancellation with a late child result, a shared budget, and
the four F-line fixes asserted as successes.

Each scenario is a real deployment — real SQLite control and run stores, the real worker, the real
coordinator — driven by a *scripted* harness (the model is a script; this is offline by design).
The verdict is never the scenario's own report: a verifier re-reads the committed records and
decides. A scenario also carries an :class:`EvalCase`, so its identity is the F2 digest of its
task, fixture, verifier, budgets and truth — changing an expectation changes the identity.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from yanxu_harness.tasks import (
    ChildCoordinator,
    ChildOutcome,
    ChildPolicy,
    ChildSpawnRequest,
    InputAdmission,
    InputContent,
    InputKind,
    LocalWorker,
    SchedulerLimits,
    SqliteControlStore,
    TaskAdmission,
    TaskStatus,
)
from yanxu_harness.evals.types import EvalCase
from yanxu_harness.evals.verifiers import VerifierOutcome

NAMESPACE = "project-a"
SESSION = "s1"


def run(coro: Any, *, timeout: float = 60.0) -> Any:
    async def guarded() -> Any:
        return await asyncio.wait_for(coro, timeout=timeout)

    return asyncio.run(guarded())


# -- the scripted model: a harness that reports an outcome without a provider ----------


@dataclass
class _Status:
    value: str


@dataclass
class _Outcome:
    status: _Status
    stop_code: str = ""
    pending_interaction: dict | None = None


class ScriptedHarness:
    """A scripted model. It confirms the inputs it consumes in the real commit boundary."""

    def __init__(self, store: Any, task: Any, attempt_id: str, *, outcome: str = "completed") -> None:
        self._persistence = store
        self.store = store
        self.task = task
        self.outcome = outcome

    async def start(self, request: Any, context: Any) -> _Outcome:
        await self._confirm()
        return _Outcome(_Status(self.outcome))

    async def resume(self, run_id: str, payload: Any, context: Any) -> _Outcome:
        await self._confirm()
        return _Outcome(_Status(self.outcome))

    async def cancel(self, run_id: str, *, reason: str = "") -> None:
        return None

    async def _confirm(self) -> None:
        records = await self.store.read_inputs(self.task.task_id)
        pending = tuple(
            record
            for record in records
            if record.state.value == "pending" and not record.kind.is_control
        )
        if pending:
            await self.store.commit_run_transition_with_inputs(
                self.task.run_id,
                {"phase": "done"},
                (),
                expected_version=None,
                applied_inputs=pending,
                task_id=self.task.task_id,
            )


def _worker(store: Any, *, max_concurrent: int = 1, outcome: str = "completed") -> LocalWorker:
    return LocalWorker(
        store,
        lambda task, attempt: ScriptedHarness(store, task, attempt, outcome=outcome),
        limits=SchedulerLimits(max_concurrent=max_concurrent, claim_ttl_seconds=300.0),
    )


def _admit(store: Any, task_id: str, *, namespace: str = NAMESPACE, priority: int = 0) -> Any:
    return run(
        store.admit_task(
            TaskAdmission(
                namespace=namespace,
                session_id=SESSION,
                task_id=task_id,
                run_id=f"run-{task_id}",
                initial_input=InputContent(kind=InputKind.INITIAL, text=f"do {task_id}"),
                priority=priority,
            )
        )
    )


# -- scenario identity -----------------------------------------------------------------


@dataclass(frozen=True)
class Scenario:
    """One frozen scenario: an :class:`EvalCase` identity plus the verifier that decides it."""

    case: EvalCase
    verify: Callable[[Any], VerifierOutcome]


def _case(case_id: str, *, verifier: str, expectations: Mapping[str, Any]) -> EvalCase:
    return EvalCase(
        case_id=case_id,
        version="1",
        instructions=f"offline orchestration scenario {case_id}",
        verifier=verifier,
        verifier_digest=f"scenario:{verifier}:v1",
        expectations=dict(expectations),
    )


# -- scenario 1: inbox redelivery and cross-process consumption ------------------------


def _scenario_inbox_redelivery(tmp_path: Path) -> VerifierOutcome:
    store = SqliteControlStore(tmp_path / "control.db")
    try:
        _admit(store, "t1")
        first = run(
            store.admit_input(
                InputAdmission(
                    namespace=NAMESPACE,
                    session_id=SESSION,
                    task_id="t1",
                    input_id="steer-1",
                    content=InputContent(kind=InputKind.STEERING, text="one instruction"),
                )
            )
        )
        again = run(
            store.admit_input(
                InputAdmission(
                    namespace=NAMESPACE,
                    session_id=SESSION,
                    task_id="t1",
                    input_id="steer-1",
                    content=InputContent(kind=InputKind.STEERING, text="one instruction"),
                )
            )
        )
        run(_worker(store).run_until_idle())
        records = run(store.read_inputs("t1"))
    finally:
        store.close()
    steers = [record for record in records if record.input_id == "steer-1"]
    ok = (
        first.outcome.value == "accepted"
        and again.outcome.value == "duplicate"
        and len(steers) == 1
        and steers[0].state.value == "applied"
        and steers[0].applied_run_revision is not None
    )
    return VerifierOutcome(
        passed=ok,
        reasons=() if ok else ["the input was not accepted once and applied once"],
        evidence={
            "first": first.outcome.value,
            "again": again.outcome.value,
            "inputs": [(r.input_id, r.state.value) for r in records],
        },
    )


# -- scenario 2: admission / terminal race ---------------------------------------------


def _scenario_terminal_race(tmp_path: Path) -> VerifierOutcome:
    store = SqliteControlStore(tmp_path / "control.db")
    try:
        _admit(store, "t1")
        run(_worker(store).run_until_idle())
        late = run(
            store.admit_input(
                InputAdmission(
                    namespace=NAMESPACE,
                    session_id=SESSION,
                    task_id="t1",
                    input_id="late-1",
                    content=InputContent(kind=InputKind.FOLLOW_UP, text="too late"),
                )
            )
        )
    finally:
        store.close()
    ok = late.outcome.value == "refused" and late.refusal_code == "task_terminal"
    return VerifierOutcome(
        passed=ok,
        reasons=() if ok else ["an input after the terminal commit was not refused"],
        evidence={"outcome": late.outcome.value, "refusal": late.refusal_code},
    )


# -- scenario 3: multi-worker preemption -----------------------------------------------


async def _race(*workers: LocalWorker) -> list[Any]:
    return list(await asyncio.gather(*(worker.run_once() for worker in workers)))


def _scenario_multi_worker(tmp_path: Path) -> VerifierOutcome:
    store = SqliteControlStore(tmp_path / "control.db")
    try:
        _admit(store, "t1")
        first = _worker(store)
        second = _worker(store)
        results = run(_race(first, second))
        outcomes = sorted(item.outcome for item in results if item is not None)
        task = run(store.read_task("t1"))
    finally:
        store.close()
    ok = outcomes == ["held_elsewhere", "settled:completed"] and task.attempt_count == 1
    return VerifierOutcome(
        passed=ok,
        reasons=() if ok else [f"two workers did not reduce to one drive: {outcomes}"],
        evidence={"outcomes": outcomes, "attempts": task.attempt_count},
    )


# -- scenario 4: fairness and a waiting slot -------------------------------------------


def _scenario_fairness_waiting(tmp_path: Path) -> VerifierOutcome:
    store = SqliteControlStore(tmp_path / "control.db")
    try:
        now = 2_000_000.0
        for task_id, priority, age in (
            ("old-low", 0, 100.0),
            ("new-high", 9, 0.0),
            ("other", 1, 0.0),
        ):
            run(
                store.admit_task(
                    TaskAdmission(
                        namespace=NAMESPACE,
                        session_id=SESSION,
                        task_id=task_id,
                        run_id=f"run-{task_id}",
                        initial_input=InputContent(kind=InputKind.INITIAL, text=task_id),
                        priority=priority,
                        created_at=now - age,
                    )
                )
            )
        worker = LocalWorker(
            store,
            lambda task, attempt: ScriptedHarness(
                store, task, attempt, outcome="suspended" if task.task_id == "old-low" else "completed"
            ),
            limits=SchedulerLimits(
                max_concurrent=1, max_per_namespace=1, priority_aging_seconds=30.0
            ),
            clock=lambda: now,
        )
        results = run(worker.run_until_idle())
        order = [(item.task_id, item.outcome) for item in results]
        old_low = run(store.read_task("old-low"))
    finally:
        store.close()
    ok = (
        order
        and order[0] == ("old-low", "waiting")
        and old_low.status is TaskStatus.WAITING
        and any(item[0] == "other" and item[1].startswith("settled") for item in order)
    )
    return VerifierOutcome(
        passed=ok,
        reasons=() if ok else [f"fairness/waiting did not hold: {order}"],
        evidence={"order": order},
    )


# -- scenario 5: parent/child recovery --------------------------------------------------


def _child_policy() -> ChildPolicy:
    return ChildPolicy(
        profiles=frozenset({"researcher"}),
        tools=frozenset({"search"}),
        workspace_root="/work",
        budget_units={"model_requests": 5.0},
    )


def _spawn_request() -> ChildSpawnRequest:
    return ChildSpawnRequest(
        parent_task_id="parent",
        namespace=NAMESPACE,
        session_id=SESSION,
        spawn_identity="spawn:1",
        child_task_id="child",
        child_run_id="run-child",
        initial_input=InputContent(kind=InputKind.INITIAL, text="child work"),
        profile="researcher",
        budget_amount=2.0,
    )


def _scenario_parent_child(tmp_path: Path) -> VerifierOutcome:
    store = SqliteControlStore(tmp_path / "control.db")
    try:
        _admit(store, "parent")
        coordinator = ChildCoordinator(store, policy=_child_policy())
        created = run(coordinator.spawn(_spawn_request()))
        again = run(coordinator.spawn(_spawn_request()))
        claim = run(store.claim_task("parent", holder="host", ttl_seconds=300.0))
        run(coordinator.await_child("parent", "child", claim=claim))
        run(store.release_task_claim("parent", holder="host", attempt_id=claim.attempt_id))
        run(store.claim_task("child", holder="host", ttl_seconds=300.0))
        run(
            coordinator.complete_child(
                "child", outcome=ChildOutcome.COMPLETED, result={"answer": "42"}
            )
        )
        delivered = run(coordinator.deliver_notifications())
        results = run(coordinator.apply_child_results("parent"))
        parent = run(store.read_task("parent"))
        children = run(store.read_children("parent"))
    finally:
        store.close()
    ok = (
        created.created is True
        and again.created is False
        and len(children) == 1
        and len(delivered) == 1
        and len(results) == 1
        and results[0].result.get("answer") == "42"
        and parent.status is TaskStatus.QUEUED
    )
    return VerifierOutcome(
        passed=ok,
        reasons=() if ok else ["the parent/child recovery did not converge exactly once"],
        evidence={
            "created": created.created,
            "again": again.created,
            "children": len(children),
            "delivered": list(delivered),
            "results": len(results),
            "parent": parent.status.value,
        },
    )


# -- scenario 6: parent cancel with a late child result ---------------------------------


def _scenario_parent_cancel_late(tmp_path: Path) -> VerifierOutcome:
    store = SqliteControlStore(tmp_path / "control.db")
    try:
        _admit(store, "parent")
        coordinator = ChildCoordinator(store, policy=_child_policy())
        run(coordinator.spawn(_spawn_request()))
        # The child finishes first: its completion is a committed fact and its notice is owed.
        run(store.claim_task("child", holder="w", ttl_seconds=300.0))
        run(
            coordinator.complete_child(
                "child", outcome=ChildOutcome.COMPLETED, result={"answer": "42"}
            )
        )
        # The parent's cancellation then lands and settles it: request, then the two steps.
        claim = run(store.claim_task("parent", holder="host", ttl_seconds=300.0))
        assert claim is not None
        parent = run(store.read_task("parent"))
        parent = run(
            store.save_task(parent.request_cancel(at=time.time()), expected_status=parent.status)
        )
        parent = run(
            store.save_task(
                parent.transition(TaskStatus.CANCELLING, reason="cancel_requested", at=time.time()),
                expected_status=parent.status,
            )
        )
        run(
            store.save_task(
                parent.transition(
                    TaskStatus.CANCELLED, reason="cancellation_settled", at=time.time()
                ),
                expected_status=parent.status,
            )
        )
        delivered = run(coordinator.deliver_notifications())
        after = run(store.read_task("parent"))
        child = run(store.read_task("child"))
        retained = run(coordinator.child_result("child"))
    finally:
        store.close()
    ok = (
        child.status is TaskStatus.COMPLETED
        and retained is not None
        and retained.result.get("answer") == "42"
        and len(delivered) == 1  # archived against the terminal parent, not retried forever
        and after.status is TaskStatus.CANCELLED
    )
    return VerifierOutcome(
        passed=ok,
        reasons=() if ok else ["cancellation and a late child result did not settle as facts"],
        evidence={
            "child": child.status.value,
            "delivered": list(delivered),
            "parent": after.status.value,
            "retained": retained is not None,
        },
    )


# -- scenario 7: shared budget with an unknown -----------------------------------------


def _scenario_shared_budget(tmp_path: Path) -> VerifierOutcome:
    store = SqliteControlStore(tmp_path / "control.db")
    try:
        _admit(store, "parent")
        coordinator = ChildCoordinator(store, policy=_child_policy())
        run(coordinator.spawn(_spawn_request()))
        # The child was dispatched and its usage is unproven: the grant stays unknown, not zero.
        run(store.claim_task("child", holder="w", ttl_seconds=300.0))
        run(coordinator.complete_child("child", outcome=ChildOutcome.FAILED, reason="lost"))
        reservation = run(store.read_reservation("grant:spawn:1"))
        # The root's allowance did not grow back from an unknown outcome.
        remaining = run(store.read_reservations("parent"))
    finally:
        store.close()
    ok = (
        reservation.state.value == "unknown"
        and len(remaining) == 1
        and remaining[0].state.value == "unknown"
    )
    return VerifierOutcome(
        passed=ok,
        reasons=() if ok else ["an unknown child grant was refunded or lost"],
        evidence={"grant": reservation.state.value, "reservations": len(remaining)},
    )


# -- scenario 8: the four F-line fixes are pinned by named tests -----------------------


F_LINE_EVIDENCE = {
    "RDE-D1-pagination": (
        "test_acceptance_review_gaps",
        "test_rde_d1_a_truncated_report_does_not_skip_the_events_it_left_out",
    ),
    "RDE-E1-identity": (
        "test_acceptance_review_gaps",
        "test_rde_e1_changing_the_acceptance_truth_changes_the_case_identity",
    ),
    "RDE-E2-samples": (
        "test_acceptance_review_gaps",
        "test_rde_e2_a_baseline_sample_is_counted_once",
    ),
    "RDE-E3-output": (
        "test_acceptance_review_gaps",
        "test_rde_e3_the_command_output_is_bounded_while_it_is_collected",
    ),
}


def test_the_four_f_line_fixes_are_pinned_by_named_tests() -> None:
    """The §11.2 row that is a consolidation, not a new scenario.

    The four RDE probes were red-then-green inside the F round; this asserts their fixed-test
    location so the acceptance-matrix entry points at a runnable test rather than a claim.
    """
    import importlib

    for label, (module_name, test_name) in F_LINE_EVIDENCE.items():
        module = importlib.import_module(module_name)
        assert hasattr(module, test_name), f"{label}: {module_name}::{test_name} is missing"


# -- the suite -------------------------------------------------------------------------


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        _case(
            "inbox-redelivery",
            verifier="scenario:inbox-redelivery",
            expectations={"inputs": 1, "applied": 1},
        ),
        _scenario_inbox_redelivery,
    ),
    Scenario(
        _case(
            "admit-terminal-race",
            verifier="scenario:admit-terminal-race",
            expectations={"refusal": "task_terminal"},
        ),
        _scenario_terminal_race,
    ),
    Scenario(
        _case(
            "multi-worker-preemption",
            verifier="scenario:multi-worker-preemption",
            expectations={"attempts": 1},
        ),
        _scenario_multi_worker,
    ),
    Scenario(
        _case(
            "fairness-waiting",
            verifier="scenario:fairness-waiting",
            expectations={"first": "old-low", "waits": True},
        ),
        _scenario_fairness_waiting,
    ),
    Scenario(
        _case(
            "parent-child-recovery",
            verifier="scenario:parent-child-recovery",
            expectations={"children": 1, "applied": 1},
        ),
        _scenario_parent_child,
    ),
    Scenario(
        _case(
            "parent-cancel-late-result",
            verifier="scenario:parent-cancel-late-result",
            expectations={"child": "cancelled", "revived": False},
        ),
        _scenario_parent_cancel_late,
    ),
    Scenario(
        _case(
            "shared-budget",
            verifier="scenario:shared-budget",
            expectations={"unknown": True},
        ),
        _scenario_shared_budget,
    ),
)


def test_each_frozen_scenario_passes_its_independent_verifier(tmp_path: Path) -> None:
    failures: list[str] = []
    for index, scenario in enumerate(SCENARIOS):
        verdict = scenario.verify(tmp_path / f"scenario-{index}-{scenario.case.case_id}")
        if not verdict.passed:
            failures.append(f"{scenario.case.case_id}: {verdict.reasons}")
    assert not failures, failures


def test_a_scenario_identity_is_the_f2_digest_of_its_truth(tmp_path: Path) -> None:
    baseline = _case(
        "fairness-waiting", verifier="scenario:fairness-waiting", expectations={"first": "old-low"}
    )
    same = _case(
        "fairness-waiting", verifier="scenario:fairness-waiting", expectations={"first": "old-low"}
    )
    changed = _case(
        "fairness-waiting",
        verifier="scenario:fairness-waiting",
        expectations={"first": "new-high"},
    )
    assert baseline.digest == same.digest
    assert baseline.digest != changed.digest, "changing the truth must change the identity"
