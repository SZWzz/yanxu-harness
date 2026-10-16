"""RF5: real new-process failures in the two windows that matter.

The plan asks for process evidence, not just simulated hooks (plan §9.2). Two windows are covered
here, each with a **real** child process that dies at a chosen point:

* **the input commit** — a process dies *after* the transaction that applied a steering input
  committed. The run and the applied mark survive; a further attempt cannot apply the same input
  again. (The companion "dies before the commit" case lives in ``test_tasks_input_flow``.)
* **the child-result commit** — a process suspends a parent on its child, completes the child and
  delivers the notice, then dies *before* the parent's snapshot applies the result. A second
  process resumes the same run from the committed wait and applies the result exactly once.

Both use real ``DefaultHarness``, real SQLite and real workers; only the provider is scripted.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import test_tasks_children as children
import test_tasks_runner_recovery as recovery
from durable_edit import final_message

from yanxu_harness.tasks import (
    InputAdmission,
    InputContent,
    InputKind,
    InputState,
    SqliteControlStore,
    TaskAdmission,
    TaskStatus,
)

NAMESPACE = "project-a"
SESSION = "s1"


def run(coro: Any, *, timeout: float = 60.0) -> Any:
    async def guarded() -> Any:
        return await asyncio.wait_for(coro, timeout=timeout)

    return asyncio.run(guarded())


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(Path.cwd() / "src"), str(Path.cwd() / "tests"), env.get("PYTHONPATH", "")]
    )
    return env


# -- window 1: the process dies after the input commit ----------------------------------


def test_a_process_dying_after_the_input_commit_does_not_reapply_the_input(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runs.db"
    code = (
        "import asyncio, os, sys\n"
        "from yanxu_harness.tasks import (\n"
        "    InputAdmission, InputContent, InputKind, SqliteControlStore, TaskAdmission,\n"
        ")\n"
        "store = SqliteControlStore(sys.argv[1])\n"
        "async def go():\n"
        "    await store.admit_task(TaskAdmission(\n"
        "        namespace='project-a', session_id='s1', task_id='t1', run_id='r1',\n"
        "        initial_input=InputContent(kind=InputKind.INITIAL, text='do the thing')))\n"
        "    admitted = await store.admit_input(InputAdmission(\n"
        "        namespace='project-a', session_id='s1', task_id='t1', input_id='s1',\n"
        "        content=InputContent(kind=InputKind.STEERING, text='keep going')))\n"
        "    claim = await store.claim_task('t1', holder='w', ttl_seconds=300.0)\n"
        "    await store.commit_run_transition_with_inputs(\n"
        "        'r1', {'phase': 'collected'}, (), expected_version=None,\n"
        "        applied_inputs=[admitted.record], claim=claim, task_id='t1')\n"
        "asyncio.run(go())\n"
        "os._exit(9)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code, str(path)],
        capture_output=True,
        text=True,
        timeout=120,
        env=_env(),
        check=False,
    )
    assert completed.returncode == 9, completed.stderr

    store = SqliteControlStore(path)
    try:
        loaded = run(store.load_run("r1"))
        assert loaded is not None, "the run state committed with the input"
        records = {record.input_id: record for record in run(store.read_inputs("t1"))}
        applied = records["s1"]
        assert applied.state is InputState.APPLIED
        assert applied.applied_run_revision == loaded.version
        # A retry cannot apply the same input again: the committed mark is the guard.
        with pytest.raises(Exception) as error:
            run(
                store.commit_run_transition_with_inputs(
                    "r1",
                    {"phase": "again"},
                    (),
                    expected_version=loaded.version,
                    applied_inputs=[applied],
                    task_id="t1",
                )
            )
        assert "already applied" in str(error.value)
        assert len([r for r in run(store.read_inputs("t1")) if r.input_id == "s1"]) == 1
    finally:
        store.close()


# -- window 2: the process dies before the parent applies the child result ---------------


def test_a_process_dying_before_the_parent_applies_the_child_result(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state-root"
    code = (
        "import os, sys\n"
        "from pathlib import Path\n"
        "import test_tasks_children as children\n"
        "import test_tasks_runner_recovery as recovery\n"
        "from durable_edit import final_message\n"
        "from yanxu_harness.tasks import ChildCoordinator, ChildOutcome\n"
        "fleet = recovery._Fleet(Path(sys.argv[1]))\n"
        "recovery._admit(fleet.store, 'parent')\n"
        "coordinator = ChildCoordinator(fleet.store, policy=children._policy())\n"
        "recovery.run(coordinator.spawn(children._request('parent', spawn='spawn:1', child='child')))\n"
        "fleet.coordinator = coordinator\n"
        "fleet.stream('parent', final_message('before child'), final_message('after child'))\n"
        "fleet.stream('child', final_message('child done'))\n"
        "worker = fleet.worker(holder='first', coordinator=coordinator)\n"
        "recovery.run(worker.run_once())\n"
        "recovery.run(worker.run_until_idle())\n"
        "recovery.run(coordinator.complete_child('child', outcome=ChildOutcome.COMPLETED, result={'ok': True}))\n"
        "recovery.run(coordinator.deliver_notifications())\n"
        "os._exit(9)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code, str(root)],
        capture_output=True,
        text=True,
        timeout=180,
        env=_env(),
        check=False,
    )
    assert completed.returncode == 9, completed.stderr

    # The committed facts survived: the child ended, the notice is committed, the parent waits.
    fleet = recovery._Fleet(root)
    try:
        assert recovery.run(fleet.store.read_task("child")).status is TaskStatus.COMPLETED
        parent = recovery.run(fleet.store.read_task("parent"))
        assert parent.status is TaskStatus.QUEUED, parent.status
        coordinator = children.ChildCoordinator(fleet.store, policy=children._policy())
        fleet.coordinator = coordinator
        fleet.stream("parent", final_message("after child"))
        worker = fleet.worker(holder="second", coordinator=coordinator)
        result = recovery.run(worker.run_once())
        assert result is not None and result.task_id == "parent", result
        final_task = recovery.run(fleet.store.read_task("parent"))
        assert final_task.status is TaskStatus.COMPLETED, final_task
        final_run = recovery.run(fleet.store.load_run("run-parent"))
        assert final_run is not None and final_run.state.get("status") == "completed"
        transcript = json.dumps(final_run.state.get("messages") or [])
        assert transcript.count("child_result (untrusted task data") == 1, transcript[-500:]
    finally:
        fleet.close()


def test_the_release_probe_enumerates_the_same_windows() -> None:
    """The probe file remains the cross-check entry point (plan §9.2)."""
    probe = Path.cwd() / "scripts" / "probe_orchestration_release_review.py"
    assert probe.exists()
    text = probe.read_text(encoding="utf-8")
    for name in ("DIO2-I1", "DIO2-R1", "DIO2-R2", "DIO2-B1", "DIO2-C1"):
        assert name in text, name
