"""RF lifecycle acceptance: the three windows the post-RF review opened.

These are the review's RF-A1/A2/A3 scenarios as default regressions (plan RF §5): they drive the
real ``DefaultHarness``/Runner/SQLite/Gateway with only the provider scripted, and RF-A1 uses a
real child process that dies after the result event's commit.

* **RF-A1** — the result event commits (the snapshot is ``awaiting_prepare`` with the result in
  ``consumed`` and in ``pending``), the process dies, and a new executor must continue the run to
  its final outcome instead of parking it in an unusable reconciliation.
* **RF-A2** — a parent with two finished children must see *both* results; applying only the one
  named by the wait would drop the other.
* **RF-A3** — ``usage_limited`` is a stop, never a wait on a child.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

import test_tasks_children as children
import test_tasks_release_regressions as regressions
import test_tasks_runner_recovery as recovery
from durable_edit import final_message, tool_call_message

from yanxu_harness.core.execution import Phase
from yanxu_harness.runtime import RunRequest
from yanxu_harness.tasks import ChildCoordinator, ChildOutcome, TaskStatus

ROOT = Path(__file__).resolve().parents[1]


def run(coro: Any, *, timeout: float = 60.0) -> Any:
    return recovery.run(coro, timeout=timeout)


# -- RF-A1: the result is committed, then the process dies ------------------------------


def test_a_run_that_died_after_applying_its_child_result_is_recovered(tmp_path: Path) -> None:
    root = tmp_path / "state"
    child = (
        "import os, sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import test_tasks_release_regressions as regressions\n"
        "import test_tasks_runner_recovery as recovery\n"
        "from yanxu_harness.core.execution import Phase\n"
        "fleet, coordinator = regressions._family(Path(sys.argv[2]))\n"
        "worker = regressions._pause(fleet, coordinator)\n"
        "regressions._complete_child(fleet, coordinator, worker)\n"
        "task = recovery.run(fleet.store.read_task('parent'))\n"
        "harness = fleet._harness(task, 'crash-after-apply')\n"
        "harness._lease_ttl = 1.0\n"
        "original = harness._state_sink\n"
        "def factory(holder):\n"
        "    persist = original(holder)\n"
        "    async def sink(state):\n"
        "        await persist(state)\n"
        "        if (\n"
        "            state.phase is Phase.AWAITING_PREPARE\n"
        "            and any(key.startswith('child-result:') for key, _ in state.consumed)\n"
        "        ):\n"
        "            print('crash-after-result-commit', flush=True)\n"
        "            os._exit(19)\n"
        "    return sink\n"
        "harness._state_sink = factory\n"
        "recovery.run(harness.resume('run-parent', {}, regressions._context('crash-after-apply')))\n"
        "raise SystemExit('fault point not reached')\n"
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        [
            str(ROOT / "src"),
            str(ROOT / "tests"),
            environment.get("PYTHONPATH", ""),
        ]
    )
    result = subprocess.run(
        [sys.executable, "-c", child, str(ROOT / "tests"), str(root)],
        capture_output=True,
        text=True,
        timeout=60,
        env=environment,
    )
    assert result.returncode == 19 and "crash-after-result-commit" in result.stdout, (
        result.returncode,
        result.stdout,
        result.stderr,
    )
    # The wait only lets the dead executor's declared one-second lease expire.
    time.sleep(1.1)
    fleet = recovery._Fleet(root)
    try:
        coordinator = ChildCoordinator(fleet.store, policy=children._policy())
        fleet.coordinator = coordinator
        fleet.stream("parent", final_message("after crash"))
        before = recovery.run(fleet.store.load_run("run-parent"))
        snapshot = before.state["execution"]
        assert snapshot["phase"] == "awaiting_prepare"
        assert any("child-result:" in str(item) for item in snapshot["consumed"])
        assert "child_result (untrusted" in json.dumps(snapshot)
        task = recovery.run(fleet.store.read_task("parent"))
        harness = fleet._harness(task, "new-executor")
        outcome = recovery.run(
            harness.resume("run-parent", {}, regressions._context("new-executor"))
        )
        stored = recovery.run(fleet.store.load_run("run-parent"))
        assert outcome.status.value == "completed", {
            "recovered_status": outcome.status.value,
            "interaction": (stored.state.get("pending_interaction") or {}).get("kind"),
        }
        messages = json.dumps(stored.state["messages"])
        assert messages.count("child_result (untrusted") == 1, messages
    finally:
        fleet.close()


# -- RF-A2: every owned child result reaches the parent ---------------------------------


def test_every_owned_child_result_reaches_the_parent(tmp_path: Path) -> None:
    fleet, coordinator = regressions._family(tmp_path)
    try:
        recovery.run(
            coordinator.spawn(children._request("parent", spawn="spawn:2", child="child-2"))
        )
        worker = regressions._pause(fleet, coordinator)
        recovery.run(worker.run_until_idle())
        for child, marker in (("child", "FIRST_RESULT"), ("child-2", "SECOND_RESULT")):
            recovery.run(
                coordinator.complete_child(
                    child, outcome=ChildOutcome.COMPLETED, result={"marker": marker}
                )
            )
        recovery.run(coordinator.deliver_notifications())
        assert len(recovery.run(coordinator.apply_child_results("parent"))) == 2
        task = recovery.run(fleet.store.read_task("parent"))
        harness = fleet._harness(task, "resume-two")
        outcome = recovery.run(harness.resume("run-parent", {}, regressions._context("resume-two")))
        stored = recovery.run(fleet.store.load_run("run-parent"))
        transcript = json.dumps(stored.state["messages"])
        assert outcome.status.value == "completed", outcome.status.value
        assert "FIRST_RESULT" in transcript and "SECOND_RESULT" in transcript, transcript[-500:]
        assert transcript.count("FIRST_RESULT") == transcript.count("SECOND_RESULT") == 1
    finally:
        fleet.close()


# -- RF-A3: usage_limited is a stop, not a wait -----------------------------------------


def test_an_exhausted_budget_is_terminal_and_never_a_wait(tmp_path: Path) -> None:
    fleet, coordinator = regressions._family(tmp_path)

    async def scenario() -> None:
        claim = await fleet.store.claim_task("parent", holder="first", ttl_seconds=300)
        task = await fleet.store.read_task("parent")
        fleet.stream("parent", tool_call_message("call-1"))
        harness = fleet._harness(task, claim.attempt_id)
        outcome = await harness.start(
            RunRequest(prompt="do parent", max_turns=1),
            regressions._context(claim.attempt_id),
        )
        stored = await fleet.store.load_run("run-parent")
        assert outcome.status.value == "usage_limited", {
            "run_status": outcome.status.value,
            "stop_code": outcome.stop_code,
            "child_status": (await fleet.store.read_task("child")).status.value,
            "interaction": (stored.state.get("pending_interaction") or {}).get("kind"),
        }
        assert stored.state["status"] == "usage_limited"
        assert fleet.streams["parent"].calls == 1

    try:
        recovery.run(scenario())
    finally:
        fleet.close()


def test_a_stopped_parent_records_and_settles_its_children(tmp_path: Path) -> None:
    """A parent that stops without completing cancels its queued child and releases its grant."""
    store = children._store(tmp_path)
    try:
        children._admit(store, "parent")
        coordinator = ChildCoordinator(store, policy=children._policy())
        recovery.run(
            coordinator.spawn(
                children._request("parent", spawn="spawn:1", child="child", amount=2.0)
            )
        )
        affected = recovery.run(
            coordinator.settle_children_on_parent_stop("parent", reason="usage limited")
        )
        assert len(affected) == 1, affected
        child = recovery.run(store.read_task("child"))
        assert child.status is TaskStatus.CANCELLED
        assert child.cancel_requested_at is not None
        result = recovery.run(coordinator.child_result("child"))
        assert result is not None and result.outcome is ChildOutcome.CANCELLED
        grant = recovery.run(store.read_reservation("grant:spawn:1"))
        assert grant.state.value == "released", grant.state.value
    finally:
        store.close()


if __name__ == "__main__":  # pragma: no cover - convenience only
    raise SystemExit(pytest.main([__file__]))
