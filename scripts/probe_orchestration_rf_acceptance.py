"""Post-RF acceptance probes for the remaining RF3/RF5 lifecycle windows.

Run with an interpreter that has this checkout and its dev extras installed:
    python scripts/probe_orchestration_rf_acceptance.py

Uses real Harness/Runner/SQLite/Gateway test deployments. Only the provider is scripted.
The first probe terminates a real child process after the result event's commit.
Exit 1 means these lifecycle acceptance checks remain blocked.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

import test_tasks_children as children
import test_tasks_release_regressions as regressions
import test_tasks_runner_recovery as recovery
from durable_edit import final_message, tool_call_message

from yanxu_harness.runtime import RunRequest
from yanxu_harness.tasks import ChildCoordinator, ChildOutcome


def after_result_commit(root: Path) -> None:
    child = """
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import test_tasks_release_regressions as regressions
import test_tasks_runner_recovery as recovery
from yanxu_harness.core.execution import Phase
fleet, coordinator = regressions._family(Path(sys.argv[2]))
worker = regressions._pause(fleet, coordinator)
regressions._complete_child(fleet, coordinator, worker)
task = recovery.run(fleet.store.read_task("parent"))
harness = fleet._harness(task, "crash-after-apply")
harness._lease_ttl = 1.0
original = harness._state_sink
def factory(holder):
    persist = original(holder)
    async def sink(state):
        await persist(state)
        if (
            state.phase is Phase.AWAITING_PREPARE
            and any(key.startswith("child-result:") for key, _ in state.consumed)
        ):
            print("crash-after-result-commit", flush=True)
            os._exit(19)
    return sink
harness._state_sink = factory
recovery.run(harness.resume("run-parent", {}, regressions._context("crash-after-apply")))
raise SystemExit("fault point not reached")
"""
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, "-c", child, str(ROOT / "tests"), str(root)],
        capture_output=True, text=True, timeout=30, env=environment,
    )
    assert result.returncode == 19 and "crash-after-result-commit" in result.stdout, (
        result.returncode, result.stdout, result.stderr
    )
    # The committed-state hook locates the fault window. This wait only allows the dead
    # executor's declared one-second lease to expire; it does not schedule the crash.
    time.sleep(1.1)
    fleet = recovery._Fleet(root)
    try:
        coordinator = ChildCoordinator(fleet.store, policy=children._policy())
        fleet.coordinator = coordinator
        fleet.stream("parent", final_message("after crash"))
        before = recovery.run(fleet.store.load_run("run-parent"))
        snapshot = before.state["execution"]
        assert any("child-result:" in str(item) for item in snapshot["consumed"])
        assert "child_result (untrusted" in json.dumps(snapshot)
        task = recovery.run(fleet.store.read_task("parent"))
        harness = fleet._harness(task, "new-executor")
        outcome = recovery.run(harness.resume("run-parent", {}, regressions._context("new-executor")))
        stored = recovery.run(fleet.store.load_run("run-parent"))
        observed = {
            "child_exit": result.returncode,
            "committed_phase": snapshot["phase"],
            "recovered_status": outcome.status.value,
            "interaction": (stored.state.get("pending_interaction") or {}).get("kind"),
            "recovery_model_calls": fleet.streams["parent"].calls,
        }
        assert outcome.status.value == "completed", observed
        messages = json.dumps(stored.state["messages"])
        assert messages.count("child_result (untrusted") == 1, messages
    finally:
        fleet.close()


def all_owned_child_results(root: Path) -> None:
    fleet, coordinator = regressions._family(root)
    try:
        recovery.run(coordinator.spawn(children._request("parent", spawn="spawn:2", child="child-2")))
        worker = regressions._pause(fleet, coordinator)
        recovery.run(worker.run_until_idle())
        for child, marker in (("child", "FIRST_RESULT"), ("child-2", "SECOND_RESULT")):
            recovery.run(coordinator.complete_child(
                child, outcome=ChildOutcome.COMPLETED, result={"marker": marker},
            ))
        recovery.run(coordinator.deliver_notifications())
        assert len(recovery.run(coordinator.apply_child_results("parent"))) == 2
        task = recovery.run(fleet.store.read_task("parent"))
        harness = fleet._harness(task, "resume-two")
        outcome = recovery.run(harness.resume("run-parent", {}, regressions._context("resume-two")))
        stored = recovery.run(fleet.store.load_run("run-parent"))
        transcript = json.dumps(stored.state["messages"])
        observed = {
            "run_status": outcome.status.value,
            "committed_child_results": 2,
            "first_in_parent": "FIRST_RESULT" in transcript,
            "second_in_parent": "SECOND_RESULT" in transcript,
        }
        assert "FIRST_RESULT" in transcript and "SECOND_RESULT" in transcript, observed
        assert transcript.count("FIRST_RESULT") == transcript.count("SECOND_RESULT") == 1, observed
    finally:
        fleet.close()


def budget_stop_is_not_a_join(root: Path) -> None:
    fleet, coordinator = regressions._family(root)

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
        observed = {
            "run_status": outcome.status.value,
            "stop_code": outcome.stop_code,
            "error_code": stored.state.get("error_code"),
            "child_status": (await fleet.store.read_task("child")).status.value,
            "interaction": (stored.state.get("pending_interaction") or {}).get("kind"),
        }
        assert outcome.status.value == "usage_limited", observed
        assert fleet.streams["parent"].calls == 1

    try:
        recovery.run(scenario())
    finally:
        fleet.close()


def main() -> int:
    checks: tuple[tuple[str, Callable[[Path], None]], ...] = (
        ("RF-A1 result applied then process exits", after_result_commit),
        ("RF-A2 all owned child results reach parent", all_owned_child_results),
        ("RF-A3 usage limit remains terminal", budget_stop_is_not_a_join),
    )
    failed = 0
    with tempfile.TemporaryDirectory(prefix="yanxu-rf-acceptance-") as directory:
        for index, (name, check) in enumerate(checks):
            root = Path(directory) / str(index)
            root.mkdir()
            try:
                check(root)
            except Exception as exc:
                failed += 1
                print(json.dumps({"check": name, "status": "FAIL", "detail": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
            else:
                print(json.dumps({"check": name, "status": "PASS"}, ensure_ascii=False))
    print(f"RF lifecycle probes: {len(checks) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

