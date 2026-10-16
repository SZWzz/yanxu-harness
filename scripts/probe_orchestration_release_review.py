"""Independent pre-release probes, using the installed package and the real test deployments.

Run from this checkout with an interpreter that has .[dev,yaml,pydantic] installed:
    python scripts/probe_orchestration_release_review.py

Only the provider is scripted. These probes intentionally live outside pytest's default
collection until their failures are resolved; exit 1 means the release review is blocked.
The checkout supplies deployment helpers, not the package implementation.
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

import test_tasks_children as children
import test_tasks_input_flow as inputs
import test_tasks_runner_recovery as recovery
from durable_edit import final_message

from yanxu_harness.errors import ResumeRefused
from yanxu_harness.runtime import RunRequest
from yanxu_harness.runtime.context import Principal, RunContext, RunIdentity
from yanxu_harness.tasks import ChildCoordinator, ChildOutcome, ChildRefused, InputContent, InputKind


def _context(attempt: str) -> RunContext:
    return RunContext(
        identity=RunIdentity(
            namespace=recovery.NAMESPACE, session_id=recovery.SESSION,
            run_id="run-parent", attempt_id=attempt,
        ),
        principal=Principal(subject="release-review"),
    )


def _family(root: Path) -> tuple[Any, ChildCoordinator]:
    fleet = recovery._Fleet(root)
    recovery._admit(fleet.store, "parent")
    coordinator = ChildCoordinator(fleet.store, policy=children._policy())
    recovery.run(coordinator.spawn(children._request("parent", spawn="spawn:1", child="child")))
    fleet.coordinator = coordinator
    fleet.stream("parent", final_message("before child"), final_message("after child"))
    fleet.stream("child", final_message("child done"))
    return fleet, coordinator


def _pause(fleet: Any, coordinator: ChildCoordinator) -> Any:
    worker = fleet.worker(holder="first", coordinator=coordinator)
    first = recovery.run(worker.run_once())
    assert first is not None and first.task_id == "parent" and first.outcome == "waiting", first
    stored = recovery.run(fleet.store.load_run("run-parent"))
    assert stored.state["status"] == "suspended", stored.state["status"]
    return worker


def _complete_child(fleet: Any, coordinator: ChildCoordinator, worker: Any) -> None:
    recovery.run(worker.run_until_idle())
    recovery.run(coordinator.complete_child("child", outcome=ChildOutcome.COMPLETED, result={"ok": True}))
    recovery.run(coordinator.deliver_notifications())


def mixed_input_watermark(root: Path) -> None:
    deployment = inputs._deploy(root, final_message("first"), final_message("second"))
    try:
        inputs._admit(deployment, "follow-first", InputKind.FOLLOW_UP, "follow-up accepted first")
        inputs._admit(deployment, "steer-second", InputKind.STEERING, "steering accepted second")
        outcome = inputs.run(deployment.harness.start(RunRequest(prompt="do the thing"), inputs._context()))
        records = inputs._inputs(deployment)
        observed = {key: record.state.value for key, record in records.items()}
        assert records["follow-first"].state.value == "applied", (
            f"run={outcome.status.value}, model_calls={deployment.stream.calls}, inputs={observed}"
        )
    finally:
        deployment.close()


def child_resume_drift(root: Path) -> None:
    fleet, coordinator = _family(root)
    try:
        worker = _pause(fleet, coordinator)
        _complete_child(fleet, coordinator, worker)
        task = recovery.run(fleet.store.read_task("parent"))
        harness = fleet._harness(task, "drift-review")
        original = harness.fingerprints()
        harness._system_prompt = "different trusted system instructions"
        assert original != harness.fingerprints()
        calls = fleet.streams["parent"].calls
        try:
            outcome = recovery.run(harness.resume("run-parent", {}, _context("drift-review")))
        except ResumeRefused:
            assert fleet.streams["parent"].calls == calls
            return
        assert False, (
            f"drift accepted: outcome={outcome.status.value}, "
            f"model_calls={calls}->{fleet.streams['parent'].calls}"
        )
    finally:
        fleet.close()


def child_resume_without_result(root: Path) -> None:
    fleet, coordinator = _family(root)
    try:
        _pause(fleet, coordinator)
        assert recovery.run(fleet.store.read_task("child")).status.value == "queued"
        assert recovery.run(coordinator.child_result("child")) is None
        task = recovery.run(fleet.store.read_task("parent"))
        harness = fleet._harness(task, "early-review")
        calls = fleet.streams["parent"].calls
        try:
            recovery.run(harness.resume("run-parent", {}, _context("early-review")))
        except ResumeRefused:
            pass
        assert fleet.streams["parent"].calls == calls, (
            f"child still queued, no committed result; model_calls={calls}->{fleet.streams['parent'].calls}"
        )
    finally:
        fleet.close()


def child_resume_turn_budget(root: Path) -> None:
    """DIO2-B1, corrected by the RF acceptance review (RF-A3).

    A parent that *exhausts* its turn budget settles ``usage_limited`` and is **not** turned into a
    wait on its active child; a parent that still has a turn when it suspends on a child must
    resume with the inherited budget, so its total requests cannot exceed ``max_turns``.
    """
    # Part 1: an exhausted budget is terminal, not a join.
    fleet, coordinator = _family(root / "exhausted")

    async def exhausted() -> None:
        claim = await fleet.store.claim_task("parent", holder="first", ttl_seconds=300)
        task = await fleet.store.read_task("parent")
        harness = fleet._harness(task, claim.attempt_id)
        outcome = await harness.start(
            RunRequest(prompt="do parent", max_turns=1), _context(claim.attempt_id)
        )
        before = await fleet.store.load_run("run-parent")
        assert outcome.status.value == "usage_limited", (
            f"an exhausted budget must stay terminal, got {outcome.status.value!r}"
        )
        assert before.state["status"] == "usage_limited"
        assert before.state["execution"]["max_turns"] == 1
        assert before.state["turns_used"] == 1
        assert fleet.streams["parent"].calls == 1, (
            f"the parent must not be re-called, calls={fleet.streams['parent'].calls}"
        )

    try:
        recovery.run(exhausted())
    finally:
        fleet.close()

    # Part 2: a resumed (still-in-budget) parent inherits its budget and stops at the limit.
    fleet, coordinator = _family(root / "resumed")

    async def resumed() -> None:
        claim = await fleet.store.claim_task("parent", holder="first", ttl_seconds=300)
        task = await fleet.store.read_task("parent")
        harness = fleet._harness(task, claim.attempt_id)
        await harness.start(RunRequest(prompt="do parent", max_turns=2), _context(claim.attempt_id))
        before = await fleet.store.load_run("run-parent")
        assert before.state["status"] == "suspended"
        assert before.state["execution"]["max_turns"] == 2
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
        resumed_harness = fleet._harness(task, "budget-review")
        outcome = await resumed_harness.resume("run-parent", {}, _context("budget-review"))
        after = await fleet.store.load_run("run-parent")
        assert fleet.streams["parent"].calls == 2, (
            f"declared max_turns=2, actual model calls={fleet.streams['parent'].calls}, "
            f"recorded turns_used={after.state['turns_used']}, outcome={outcome.status.value}"
        )
        assert outcome.status.value == "usage_limited", outcome.status.value
        assert after.state["turns_used"] == 2, after.state["turns_used"]

    try:
        recovery.run(resumed())
    finally:
        fleet.close()


def spawn_payload_conflict(root: Path) -> None:
    store = children._store(root)
    try:
        children._admit(store, "parent")
        coordinator = ChildCoordinator(store, policy=children._policy())
        original = children._request("parent", spawn="spawn:stable", child="child", tools=("search",))
        children.run(coordinator.spawn(original))
        variants = {
            "initial_input": replace(original, initial_input=InputContent(kind=InputKind.INITIAL, text="another task")),
            "child_run_id": replace(original, child_run_id="different-run"),
            "deadline_at": replace(original, deadline_at=time.time() + 300),
        }
        accepted = []
        for name, request in variants.items():
            try:
                result = children.run(coordinator.spawn(request))
            except ChildRefused:
                continue
            accepted.append({"field": name, "created": result.created})
        assert not accepted, f"changed spawn payload accepted as duplicate: {accepted}"
    finally:
        store.close()


def main() -> int:
    checks: tuple[tuple[str, Callable[[Path], None]], ...] = (
        ("DIO2-I1 mixed input watermark", mixed_input_watermark),
        ("DIO2-R1 child resume drift", child_resume_drift),
        ("DIO2-R2 child resume without result", child_resume_without_result),
        ("DIO2-B1 child resume turn budget", child_resume_turn_budget),
        ("DIO2-C1 spawn payload conflict", spawn_payload_conflict),
    )
    failed = 0
    with tempfile.TemporaryDirectory(prefix="yanxu-release-probes-") as directory:
        root = Path(directory)
        for index, (name, check) in enumerate(checks):
            location = root / str(index)
            location.mkdir()
            try:
                check(location)
            except Exception as exc:
                failed += 1
                print(json.dumps({"check": name, "status": "FAIL", "detail": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
            else:
                print(json.dumps({"check": name, "status": "PASS"}, ensure_ascii=False))
    print(f"release probes: {len(checks) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

