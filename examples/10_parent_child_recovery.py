"""Example 10 — a parent/child tree recovered across processes.

    python examples/10_parent_child_recovery.py

The scripted part is the model: a tiny harness reports "completed" instead of
calling a provider. Everything else is real — real SQLite, a real second
process, the real coordinator and the real worker barrier.

What it shows, in order:

1. the parent spawns a child; the relation, the child task, its bounded grant and
   the notice are one commit, so nothing is half-created;
2. the parent waits on the child (``running -> waiting``) and releases its slot;
3. a **real child process** re-submits the same stable spawn identity — it
   converges on the original child (``created=False``) instead of creating a
   second one, which is how the control-commit/receipt gap is settled — completes
   the child, then dies with ``os._exit`` *before* the notice is delivered;
4. the parent process re-reads the committed facts: it delivers the notice once,
   the parent wakes, and ``apply_child_results`` returns the child's result from
   the durable record rather than from a live callback;
5. the worker's terminal barrier sees no active child and lets the parent finish.

The child result is task data, so it is read back from the committed notice; a
late result is archived and never revives a terminal parent.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

NAMESPACE = "project-a"
SESSION = "s1"
POLICY = {
    "profiles": ["researcher"],
    "tools": ["search"],
    "workspace_root": "/work",
}

CHILD = """
import asyncio, os, sys
from yanxu_harness.tasks import (
    ChildCoordinator, ChildOutcome, ChildPolicy, ChildSpawnRequest,
    InputContent, InputKind, SqliteControlStore,
)

async def main(path):
    store = SqliteControlStore(path)
    policy = ChildPolicy(
        profiles=frozenset([%(profiles)r]), tools=frozenset([%(tools)r]),
        workspace_root=%(workspace)r, budget_units={"model_requests": 5.0})
    coordinator = ChildCoordinator(store, policy=policy)
    request = ChildSpawnRequest(
        parent_task_id="parent", namespace=%(namespace)r, session_id=%(session)r,
        spawn_identity="spawn:stable", child_task_id="child", child_run_id="run-child",
        initial_input=InputContent(kind=InputKind.INITIAL, text="research the logs"),
        profile="researcher", budget_amount=2.0)
    # A retry of the same identity converges on the relation the parent already committed.
    again = await coordinator.spawn(request)
    print("child spawn created:", again.created)
    await store.claim_task("child", holder="child-proc", ttl_seconds=300.0)
    result = await coordinator.complete_child(
        "child", outcome=ChildOutcome.COMPLETED, reason="child done",
        result={"answer": "42"}, usage={"unit": "model_requests", "amount": 1.0})
    print("child completed:", result.outcome.value, "accepted:", result.accepted)
    sys.stdout.flush()
    # Die between the child's terminal commit and the notice delivery.
    os._exit(9)

asyncio.run(main(sys.argv[1]))
""" % {
    "namespace": NAMESPACE,
    "session": SESSION,
    "profiles": POLICY["profiles"][0],
    "tools": POLICY["tools"][0],
    "workspace": POLICY["workspace_root"],
}


@dataclass
class _Status:
    value: str


@dataclass
class _Outcome:
    status: _Status
    stop_code: str = ""
    pending_interaction: dict | None = None


class _ScriptedHarness:
    def __init__(self, store, task, attempt_id):
        self._persistence = store
        self.store = store
        self.task = task

    async def start(self, request, context):
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
        return _Outcome(_Status("completed"))

    async def resume(self, run_id, payload, context):
        return _Outcome(_Status("completed"))

    async def cancel(self, run_id, *, reason=""):
        return None


def main() -> int:
    from yanxu_harness.tasks import (
        ChildCoordinator,
        ChildPolicy,
        ChildSpawnRequest,
        InputContent,
        InputKind,
        LocalWorker,
        SchedulerLimits,
        SqliteControlStore,
        TaskAdmission,
    )

    directory = Path(tempfile.mkdtemp(prefix="yanxu-example10-"))
    print(f"state directory: {directory}")
    store = SqliteControlStore(directory / "control.db")
    try:
        asyncio.run(
            store.admit_task(
                TaskAdmission(
                    namespace=NAMESPACE,
                    session_id=SESSION,
                    task_id="parent",
                    run_id="run-parent",
                    initial_input=InputContent(kind=InputKind.INITIAL, text="coordinate the work"),
                    profile="worker",
                )
            )
        )
        coordinator = ChildCoordinator(
            store,
            policy=ChildPolicy(
                profiles=frozenset(POLICY["profiles"]),
                tools=frozenset(POLICY["tools"]),
                workspace_root=POLICY["workspace_root"],
                budget_units={"model_requests": 5.0},
            ),
        )
        request = ChildSpawnRequest(
            parent_task_id="parent",
            namespace=NAMESPACE,
            session_id=SESSION,
            spawn_identity="spawn:stable",
            child_task_id="child",
            child_run_id="run-child",
            initial_input=InputContent(kind=InputKind.INITIAL, text="research the logs"),
            profile="researcher",
            budget_amount=2.0,
        )
        created = asyncio.run(coordinator.spawn(request))
        assert created.created is True
        children = asyncio.run(store.read_children("parent"))
        assert len(children) == 1 and children[0].budget_reservation_id == "grant:spawn:stable"
        assert asyncio.run(store.read_task("child")).status.value == "queued"
        print("step: spawned", "relation + child task + grant + notice in one commit")

        claim = asyncio.run(store.claim_task("parent", holder="host", ttl_seconds=300.0))
        assert claim is not None
        held = asyncio.run(coordinator.await_child("parent", "child", claim=claim))
        assert held.status.value == "waiting" and held.waiting_for == "child:child"
        asyncio.run(
            store.release_task_claim("parent", holder="host", attempt_id=claim.attempt_id)
        )
        print("step: waiting", "the parent released its slot and waits on child:child")

        completed = subprocess.run(
            [sys.executable, "-c", CHILD, str(directory / "control.db")],
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, "PYTHONPATH": os.environ.get("PYTHONPATH", "")},
            check=False,
        )
        assert completed.returncode == 9, completed.stderr
        assert "child spawn created: False" in completed.stdout, completed.stdout
        child = asyncio.run(store.read_task("child"))
        assert child.status.value == "completed"
        print("step: child-process", "converged on the original child, then died before delivery")
        assert len(asyncio.run(store.read_children("parent"))) == 1, "no second child"

        delivered = asyncio.run(coordinator.deliver_notifications())
        assert len(delivered) == 1, delivered
        parent = asyncio.run(store.read_task("parent"))
        assert parent.status.value == "queued", parent
        print("step: notified", "the notice was delivered once and the parent woke")

        results = asyncio.run(coordinator.apply_child_results("parent"))
        assert len(results) == 1 and results[0].accepted is True
        assert results[0].result["answer"] == "42"
        print("step: applied-once", "the result came from the committed child record")

        worker = LocalWorker(
            store,
            lambda task, attempt: _ScriptedHarness(store, task, attempt),
            limits=SchedulerLimits(max_concurrent=1),
            child_barrier=coordinator.hold_for_children,
        )
        final = asyncio.run(worker.run_once())
        assert final is not None and final.task_id == "parent"
        assert asyncio.run(store.read_task("parent")).status.value == "completed"
        print("step: parent-finished", "the barrier saw no active child and let it complete")
        print("OK: the parent/child tree recovered across processes exactly once")
        return 0
    finally:
        store.close()
        shutil.rmtree(directory, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
