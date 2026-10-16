"""RF0: the five release-blocking defects, frozen as default-collected regressions.

These are the independent probe's five scenarios (``scripts/probe_orchestration_release_review.py``)
moved into the default suite so a fix cannot pass by only making the probe green. Each one drives
the **real** deployment the probe used — real ``DefaultHarness``, real Runner, real SQLite control
store and Gateway — with only the provider scripted, and imports the same test helper modules
rather than copying a second Harness.

The defect names are the review's:

* ``DIO2-I1`` — a follow-up admitted before a newer steering input is skipped forever, because the
  reader treated ``MAX(applied)`` as a contiguous consumed prefix;
* ``DIO2-R1`` — a ``child_result`` resume skips the arrangement check and dispatches after the
  system instructions drifted;
* ``DIO2-R2`` — a resume with no committed child result still calls the parent model;
* ``DIO2-B1`` — a resume runs the parent with a fresh turn budget and overwrites the accumulated
  ``turns_used``;
* ``DIO2-C1`` — the same spawn identity with a changed initial input, child run or deadline is
  accepted as a duplicate.

Before the fix every test here fails; the plan's RF0 commit records them red on purpose.
"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import test_tasks_children as children
import test_tasks_input_flow as inputs
import test_tasks_runner_recovery as recovery
from durable_edit import final_message

from yanxu_harness.errors import ContractError, ResumeRefused
from yanxu_harness.runtime import RunRequest
from yanxu_harness.runtime.context import Principal, RunContext, RunIdentity
from yanxu_harness.tasks import (
    ChildCoordinator,
    ChildOutcome,
    ChildRefused,
    InputContent,
    InputKind,
    InputState,
    SpawnRefusal,
    TaskStatus,
)


def _context(attempt: str) -> RunContext:
    return RunContext(
        identity=RunIdentity(
            namespace=recovery.NAMESPACE,
            session_id=recovery.SESSION,
            run_id="run-parent",
            attempt_id=attempt,
        ),
        principal=Principal(subject="release-review"),
    )


def _family(root: Path) -> tuple[Any, ChildCoordinator]:
    """The real parent/child deployment the probe builds, with only the provider scripted."""
    fleet = recovery._Fleet(root)
    recovery._admit(fleet.store, "parent")
    coordinator = ChildCoordinator(fleet.store, policy=children._policy())
    recovery.run(coordinator.spawn(children._request("parent", spawn="spawn:1", child="child")))
    fleet.coordinator = coordinator
    fleet.stream("parent", final_message("before child"), final_message("after child"))
    fleet.stream("child", final_message("child done"))
    return fleet, coordinator


def _pause(fleet: Any, coordinator: ChildCoordinator) -> Any:
    """Run the parent until its run suspends on the active child."""
    worker = fleet.worker(holder="first", coordinator=coordinator)
    first = recovery.run(worker.run_once())
    assert first is not None and first.task_id == "parent" and first.outcome == "waiting", first
    stored = recovery.run(fleet.store.load_run("run-parent"))
    assert stored.state["status"] == "suspended", stored.state["status"]
    return worker


def _complete_child(fleet: Any, coordinator: ChildCoordinator, worker: Any) -> None:
    recovery.run(worker.run_until_idle())
    recovery.run(
        coordinator.complete_child("child", outcome=ChildOutcome.COMPLETED, result={"ok": True})
    )
    recovery.run(coordinator.deliver_notifications())


# -- DIO2-I1: a follow-up admitted before a newer steering input is not lost -------------


def test_a_follow_up_admitted_before_a_steering_input_is_not_lost(tmp_path: Path) -> None:
    deployment = inputs._deploy(tmp_path, final_message("first"), final_message("second"))
    try:
        admitted_follow = inputs._admit(
            deployment, "follow-first", InputKind.FOLLOW_UP, "follow-up accepted first"
        )
        admitted_steer = inputs._admit(
            deployment, "steer-second", InputKind.STEERING, "steering accepted second"
        )
        assert admitted_follow.record is not None and admitted_steer.record is not None
        assert admitted_follow.record.sequence < admitted_steer.record.sequence, (
            "the follow-up must be the earlier sequence for this to be the reported defect"
        )
        outcome = inputs.run(
            deployment.harness.start(RunRequest(prompt="do the thing"), inputs._context())
        )
        assert outcome.status.value == "completed"
        records = inputs._inputs(deployment)
        observed = {key: record.state.value for key, record in records.items()}
        assert records["follow-first"].state is InputState.APPLIED, (
            "an accepted follow-up was skipped when the newer steering input's MAX(applied) "
            f"watermark advanced: {observed}"
        )
        assert records["steer-second"].state is InputState.APPLIED, observed
        record = inputs._stored_record(deployment)
        texts = inputs._texts(record.get("messages"))
        assert texts.count("follow-up accepted first") == 1, texts
        assert texts.count("steering accepted second") == 1, texts
        # The steering is read at the opening site; the follow-up is read at the after-inner
        # site, so it forces a later turn rather than being merged into the first one.
        assert deployment.stream.calls >= 2, (
            "the follow-up did not reach the conversation at its own boundary: "
            f"model calls={deployment.stream.calls}"
        )
    finally:
        deployment.close()


# -- DIO2-I1 (RF1): admission order never hides an earlier pending input ------------------


def _apply_order(case: str, *, kinds: tuple[InputKind, ...]) -> dict[str, str]:
    """Admit one input per kind, in the given order, and report the resulting states."""
    return {}


def _apply_order(case: str, *, kinds: tuple[InputKind, ...]) -> dict[str, str]:
    """Admit one input per kind, in the given order, and report the resulting states."""
    return {}


def test_a_steering_input_admitted_before_a_follow_up_is_still_applied(tmp_path: Path) -> None:
    deployment = inputs._deploy(tmp_path, final_message("first"), final_message("second"))
    try:
        inputs._admit(deployment, "steer-first", InputKind.STEERING, "steering accepted first")
        inputs._admit(deployment, "follow-second", InputKind.FOLLOW_UP, "follow-up accepted second")
        outcome = inputs.run(
            deployment.harness.start(RunRequest(prompt="do the thing"), inputs._context())
        )
        assert outcome.status.value == "completed"
        records = inputs._inputs(deployment)
        observed = {key: record.state.value for key, record in records.items()}
        assert records["steer-first"].state is InputState.APPLIED, observed
        assert records["follow-second"].state is InputState.APPLIED, observed
    finally:
        deployment.close()


def test_multiple_follow_ups_each_become_their_own_turn(tmp_path: Path) -> None:
    deployment = inputs._deploy(
        tmp_path, final_message("one"), final_message("two"), final_message("three")
    )
    try:
        inputs._admit(deployment, "follow-a", InputKind.FOLLOW_UP, "follow-up a")
        inputs._admit(deployment, "follow-b", InputKind.FOLLOW_UP, "follow-up b")
        outcome = inputs.run(
            deployment.harness.start(RunRequest(prompt="do the thing"), inputs._context())
        )
        assert outcome.status.value == "completed"
        records = inputs._inputs(deployment)
        observed = {key: record.state.value for key, record in records.items()}
        assert records["follow-a"].state is InputState.APPLIED, observed
        assert records["follow-b"].state is InputState.APPLIED, observed
        record = inputs._stored_record(deployment)
        texts = inputs._texts(record.get("messages"))
        assert texts.count("follow-up a") == 1, texts
        assert texts.count("follow-up b") == 1, texts
        # One follow-up per turn: two follow-ups force two extra requests, not one merged prompt.
        assert deployment.stream.calls >= 3, deployment.stream.calls
    finally:
        deployment.close()


# -- DIO2-R1: a drifted arrangement is refused before any dispatch -----------------------


def test_a_child_result_resume_after_instruction_drift_is_refused(tmp_path: Path) -> None:
    fleet, coordinator = _family(tmp_path)
    try:
        worker = _pause(fleet, coordinator)
        _complete_child(fleet, coordinator, worker)
        task = recovery.run(fleet.store.read_task("parent"))
        harness = fleet._harness(task, "drift-review")
        original = harness.fingerprints()
        harness._system_prompt = "different trusted system instructions"
        assert original != harness.fingerprints(), "the deployment really did drift"
        calls = fleet.streams["parent"].calls
        with pytest.raises(ResumeRefused) as refused:
            recovery.run(harness.resume("run-parent", {}, _context("drift-review")))
        assert refused.value.reason_code == "arrangement_drift", refused.value.to_record()
        assert fleet.streams["parent"].calls == calls, (
            "a refused drift must not call the parent model: "
            f"calls {calls} -> {fleet.streams['parent'].calls}"
        )
        stored = recovery.run(fleet.store.load_run("run-parent"))
        assert stored.state["status"] == "suspended", stored.state["status"]
    finally:
        fleet.close()


# -- DIO2-R2: no committed child result means no dispatch -------------------------------


def test_a_resume_without_a_committed_child_result_dispatches_nothing(tmp_path: Path) -> None:
    fleet, coordinator = _family(tmp_path)
    try:
        _pause(fleet, coordinator)
        assert recovery.run(fleet.store.read_task("child")).status is TaskStatus.QUEUED
        assert recovery.run(coordinator.child_result("child")) is None
        calls = fleet.streams["parent"].calls
        task = recovery.run(fleet.store.read_task("parent"))
        harness = fleet._harness(task, "early-review")
        launched: Any = None
        try:
            launched = recovery.run(harness.resume("run-parent", {}, _context("early-review")))
        except (ResumeRefused, ContractError):
            # A structured, retryable refusal is an acceptable outcome; what is not acceptable
            # is dispatching the parent model over a result that does not exist yet.
            pass
        assert fleet.streams["parent"].calls == calls, (
            "the child is still queued and no result is committed, yet the parent model was "
            f"called: {calls} -> {fleet.streams['parent'].calls}"
        )
        assert launched is None or launched.status.value != "completed", launched
        stored = recovery.run(fleet.store.load_run("run-parent"))
        assert stored.state["status"] == "suspended", stored.state["status"]
        interaction = stored.state.get("pending_interaction") or {}
        assert interaction.get("kind") == "child_result", interaction
        assert "no committed result" not in json.dumps(stored.state.get("messages") or []), (
            "a missing result must not be turned into a placeholder conversation message"
        )
    finally:
        fleet.close()


# -- RF2 acceptance: drift dimensions, forged hints, unprovable results -------------------


def _drift(harness: Any, dimension: str) -> None:
    if dimension == "instructions":
        harness._system_prompt = "different trusted system instructions"
    elif dimension == "tools":
        harness._tools = (*harness._tools, harness._tools[0])
    elif dimension == "policy":
        harness._extra_fingerprints = {
            **harness._extra_fingerprints,
            "policy": "policy:v2",
        }
    else:  # pragma: no cover - the parametrization is fixed
        raise AssertionError(dimension)


@pytest.mark.parametrize("dimension", ["instructions", "tools"])
def test_a_child_result_resume_refuses_each_drifted_dimension(
    dimension: str, tmp_path: Path
) -> None:
    """The child_result wait shares ``_compare_arrangement`` with the batch path.

    Only dimensions with a non-empty recorded baseline are exercised here (the batch path's
    runtime-resume tests cover the others); what matters is that this branch now runs the same
    comparison *before* any dispatch, which is defect DIO2-R1.
    """
    fleet, coordinator = _family(tmp_path)
    try:
        worker = _pause(fleet, coordinator)
        _complete_child(fleet, coordinator, worker)
        task = recovery.run(fleet.store.read_task("parent"))
        harness = fleet._harness(task, f"drift-{dimension}")
        _drift(harness, dimension)
        calls = fleet.streams["parent"].calls
        with pytest.raises(ResumeRefused) as refused:
            recovery.run(harness.resume("run-parent", {}, _context(f"drift-{dimension}")))
        assert refused.value.reason_code == "arrangement_drift", refused.value.to_record()
        assert dimension in refused.value.drifted, (dimension, refused.value.drifted)
        assert fleet.streams["parent"].calls == calls, "a refused drift must not dispatch"
        stored = recovery.run(fleet.store.load_run("run-parent"))
        assert stored.state["status"] == "suspended"
    finally:
        fleet.close()


def test_a_forged_resolution_hint_does_not_change_what_is_applied(tmp_path: Path) -> None:
    """``resolution`` is an audit hint: it cannot name another child or claim approval."""
    fleet, coordinator = _family(tmp_path)
    try:
        worker = _pause(fleet, coordinator)
        _complete_child(fleet, coordinator, worker)
        task = recovery.run(fleet.store.read_task("parent"))
        harness = fleet._harness(task, "forged-review")
        calls = fleet.streams["parent"].calls
        outcome = recovery.run(
            harness.resume(
                "run-parent",
                {
                    "child_task_id": "a-forged-child",
                    "spawn_identity": "forged",
                    "approved": True,
                    "ready": True,
                    "result": {"forged": True},
                },
                _context("forged-review"),
            )
        )
        assert outcome.status.value == "completed"
        assert fleet.streams["parent"].calls == calls + 1, "the real child result is applied once"
        stored = recovery.run(fleet.store.load_run("run-parent"))
        transcript = json.dumps(stored.state.get("messages") or [])
        assert "a-forged-child" not in transcript
        assert "forged" not in transcript
        # The parent's own committed result is the one that reached the conversation.
        records = recovery.run(fleet.store.read_inputs("parent"))
        assert all(record.content.child_id != "a-forged-child" for record in records)
    finally:
        fleet.close()


def test_a_terminal_child_without_a_committed_notice_is_refused_not_fabricated(
    tmp_path: Path,
) -> None:
    """The child's own run settles its task; without the completion notice the parent refuses."""
    fleet, coordinator = _family(tmp_path)
    try:
        worker = _pause(fleet, coordinator)
        # The child runs and its task settles as completed, but ``complete_child`` is never called:
        # there is a real terminal child and no committed result notice.
        recovery.run(worker.run_until_idle())
        assert recovery.run(fleet.store.read_task("child")).status is TaskStatus.COMPLETED
        assert recovery.run(coordinator.child_result("child")) is None
        task = recovery.run(fleet.store.read_task("parent"))
        harness = fleet._harness(task, "no-notice-review")
        calls = fleet.streams["parent"].calls
        with pytest.raises(ResumeRefused) as refused:
            recovery.run(harness.resume("run-parent", {}, _context("no-notice-review")))
        assert refused.value.reason_code == "child_result_not_ready", refused.value.reason_code
        assert fleet.streams["parent"].calls == calls
        stored = recovery.run(fleet.store.load_run("run-parent"))
        assert stored.state["status"] == "suspended"
        assert "no committed result" not in json.dumps(stored.state.get("messages") or []).lower()
    finally:
        fleet.close()


def test_read_child_result_classifies_queued_failed_and_mismatched(tmp_path: Path) -> None:
    """The structured readiness: not_ready, a real failed result, and an inconsistent pointer."""
    from yanxu_harness.tasks import read_child_result

    store = children._store(tmp_path)
    try:
        children._admit(store, "parent")
        coordinator = ChildCoordinator(store, policy=children._policy())
        children.run(coordinator.spawn(children._request("parent", spawn="spawn:1", child="child")))
        # Queued child, no result: not ready, never a placeholder.
        queued = children.run(
            read_child_result(store, parent_task_id="parent", child_task_id="child", run_store=store)
        )
        assert queued.state == "not_ready", queued
        assert queued.result is None
        # A real failed child whose run also failed is a *visible* result, not "missing".
        claim = children.run(store.claim_task("child", holder="w", ttl_seconds=60.0))
        children.run(
            store.commit_run_transition_with_inputs(
                "run-child",
                {"status": "failed"},
                expected_version=None,
                claim=claim,
                task_id="child",
            )
        )
        children.run(
            coordinator.complete_child(
                "child",
                outcome=ChildOutcome.FAILED,
                reason="the tool failed",
                result={"ok": False},
                accepted=False,
                acceptance_detail="the business rule rejected it",
            )
        )
        failed = children.run(
            read_child_result(store, parent_task_id="parent", child_task_id="child", run_store=store)
        )
        assert failed.ready, failed
        assert failed.result is not None
        assert failed.result.outcome is ChildOutcome.FAILED
        assert failed.result.accepted is False
        assert failed.result.acceptance_detail == "the business rule rejected it"
        # A pointer that does not name this relation is inconsistent, not repaired.
        mismatched = children.run(
            read_child_result(
                store,
                parent_task_id="parent",
                child_task_id="child",
                spawn_identity="spawn:forged",
                run_store=store,
            )
        )
        assert mismatched.state == "inconsistent", mismatched
        unknown = children.run(
            read_child_result(
                store, parent_task_id="parent", child_task_id="not-a-child", run_store=store
            )
        )
        assert unknown.state == "inconsistent", unknown
    finally:
        store.close()


# -- DIO2-B1: the logical run's turn budget is not refreshed on resume ------------------


def test_resuming_a_parent_that_used_its_last_turn_does_not_refresh_the_budget(
    tmp_path: Path,
) -> None:
    fleet, coordinator = _family(tmp_path)

    async def scenario() -> None:
        claim = await fleet.store.claim_task("parent", holder="first", ttl_seconds=300)
        assert claim is not None
        task = await fleet.store.read_task("parent")
        harness = fleet._harness(task, claim.attempt_id)
        await harness.start(RunRequest(prompt="do parent", max_turns=1), _context(claim.attempt_id))
        before = await fleet.store.load_run("run-parent")
        assert before.state["execution"]["max_turns"] == 1
        assert before.state["turns_used"] == 1
        await coordinator.hold_for_children(await fleet.store.read_task("parent"))
        await fleet.store.release_task_claim(
            "parent", holder=claim.holder, attempt_id=claim.attempt_id
        )
        worker = fleet.worker(holder="child-worker", coordinator=coordinator)
        await worker.run_once()
        await coordinator.complete_child(
            "child", outcome=ChildOutcome.COMPLETED, result={"ok": True}
        )
        await coordinator.deliver_notifications()
        task = await fleet.store.read_task("parent")
        resumed = fleet._harness(task, "budget-review")
        outcome = await resumed.resume("run-parent", {}, _context("budget-review"))
        after = await fleet.store.load_run("run-parent")
        assert fleet.streams["parent"].calls == 1, (
            f"declared max_turns=1, actual parent model calls={fleet.streams['parent'].calls}, "
            f"recorded turns_used={after.state['turns_used']}, outcome={outcome.status.value}"
        )
        assert outcome.status.value == "usage_limited", (
            f"the resumed run must stop usage_limited, not {outcome.status.value}"
        )
        assert after.state["turns_used"] == 1, (
            f"the accumulated turns were overwritten: {after.state['turns_used']}"
        )

    try:
        recovery.run(scenario())
    finally:
        fleet.close()


def test_the_committed_wait_snapshot_is_a_legal_suspended_state(tmp_path: Path) -> None:
    """The barrier leaves a Core snapshot that really means "paused", not a relabelled finish."""
    fleet, coordinator = _family(tmp_path)
    try:
        _pause(fleet, coordinator)
        stored = recovery.run(fleet.store.load_run("run-parent"))
        execution = stored.state["execution"]
        assert execution["phase"] == "suspended", execution["phase"]
        assert (execution.get("interaction") or {}).get("kind") == "child_result"
        # The budget is preserved by the wait, not reset by it.
        assert execution["turns_used"] == stored.state["turns_used"]
        assert execution.get("max_turns") == stored.state["execution"].get("max_turns")
        assert execution.get("arrangement"), "the wait snapshot keeps the arrangement baseline"
    finally:
        fleet.close()


def test_the_child_result_is_applied_once_across_repeated_resumes(tmp_path: Path) -> None:
    fleet, coordinator = _family(tmp_path)
    try:
        worker = _pause(fleet, coordinator)
        _complete_child(fleet, coordinator, worker)
        task = recovery.run(fleet.store.read_task("parent"))
        harness = fleet._harness(task, "once-review")
        first = recovery.run(harness.resume("run-parent", {}, _context("once-review")))
        assert first.status.value == "completed"
        calls = fleet.streams["parent"].calls
        # A second resume of the now-terminal run must not dispatch again.
        launched: Any = None
        try:
            launched = recovery.run(harness.resume("run-parent", {}, _context("once-review-2")))
        except ContractError:
            pass
        assert launched is None or launched.status.value == "completed"
        assert fleet.streams["parent"].calls == calls, (
            f"the result was applied a second time: calls {calls} -> {fleet.streams['parent'].calls}"
        )
        stored = recovery.run(fleet.store.load_run("run-parent"))
        transcript = json.dumps(stored.state.get("messages") or [])
        assert transcript.count("child_result (untrusted task data") == 1, transcript[-500:]
    finally:
        fleet.close()


# -- DIO2-C1: a changed spawn payload is a conflict, not a duplicate --------------------


def test_the_same_spawn_identity_with_a_changed_payload_is_a_conflict(tmp_path: Path) -> None:
    store = children._store(tmp_path)
    try:
        children._admit(store, "parent")
        coordinator = ChildCoordinator(store, policy=children._policy())
        original = children._request(
            "parent", spawn="spawn:stable", child="child", tools=("search",), amount=2.0
        )
        children.run(coordinator.spawn(original))
        before_children = len(children.run(store.read_children("parent")))
        before_outbox = len(
            children.run(store.read_outbox(task_id="parent", pending_only=False, limit=100))
        )
        before_reservations = children.run(store.read_reservations("parent"))
        variants = {
            "initial_input": replace(
                original,
                initial_input=InputContent(kind=InputKind.INITIAL, text="another task"),
            ),
            "child_run_id": replace(original, child_run_id="different-run"),
            "deadline_at": replace(original, deadline_at=time.time() + 300),
            "priority": replace(original, priority=5),
            "tools": replace(original, tools=("read_file",)),
            "workspace": replace(original, workspace_ref="/work/elsewhere"),
            "schema": replace(original, result_schema={"answer": "string"}),
            "budget": replace(original, budget_amount=3.0),
        }
        for name, request in variants.items():
            with pytest.raises(ChildRefused) as refused:
                children.run(coordinator.spawn(request))
            assert refused.value.refusal == SpawnRefusal.CONFIG_CONFLICT.value, (
                f"changing only {name} was accepted as a duplicate: "
                f"refusal={refused.value.refusal!r}"
            )
        assert len(children.run(store.read_children("parent"))) == before_children
        assert (
            len(children.run(store.read_outbox(task_id="parent", pending_only=False, limit=100)))
            == before_outbox
        )
        after_reservations = children.run(store.read_reservations("parent"))
        assert len(after_reservations) == len(before_reservations) == 1
        assert after_reservations[0].amount == before_reservations[0].amount
        assert children.run(store.read_task("different-run")) is None, (
            "a conflicting spawn must not create a second child run"
        )
    finally:
        store.close()


def test_spawn_spec_normalization_ignores_key_and_set_order(tmp_path: Path) -> None:
    """Semantics that differ only in dict/tool order are one child, not two."""
    store = children._store(tmp_path)
    try:
        children._admit(store, "parent")
        coordinator = ChildCoordinator(store, policy=children._policy())
        base = replace(
            children._request(
                "parent", spawn="spawn:norm", child="child", tools=("search", "read_file")
            ),
            result_schema={"b": "number", "a": "string"},
        )
        first = children.run(coordinator.spawn(base))
        reordered = replace(
            base,
            tools=("read_file", "search"),
            result_schema={"a": "string", "b": "number"},
        )
        again = children.run(coordinator.spawn(reordered))
        assert first.created is True and again.created is False
        assert again.relation.spawn_digest == first.relation.spawn_digest
        assert len(children.run(store.read_children("parent"))) == 1
    finally:
        store.close()


def test_a_legacy_relation_without_a_spawn_spec_refuses_a_resubmit(tmp_path: Path) -> None:
    """A relation predating the spawn spec is diagnosable, never silently re-used."""
    store = children._store(tmp_path)
    try:
        children._admit(store, "parent")
        coordinator = ChildCoordinator(store, policy=children._policy())
        request = children._request("parent", spawn="spawn:legacy", child="child")
        children.run(coordinator.spawn(request))
        # Simulate a database written before spawn semantics were recorded.
        store._connection.execute(
            "UPDATE children SET spawn_digest = '', spawn_spec = '{}' "
            "WHERE spawn_identity = 'spawn:legacy'"
        )
        with pytest.raises(ChildRefused) as refused:
            children.run(coordinator.spawn(request))
        assert refused.value.refusal == SpawnRefusal.CONFIG_CONFLICT.value
        assert "legacy evidence insufficient" in refused.value.detail
        assert len(children.run(store.read_children("parent"))) == 1
    finally:
        store.close()
