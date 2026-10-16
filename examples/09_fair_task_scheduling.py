"""Example 9 — a bounded, fair worker: aging, round-robin and a waiting slot.

    python examples/09_fair_task_scheduling.py

The scripted part is the model: a tiny harness reports "completed", or
"suspended" for one task, instead of calling a provider. Everything else is real
— real SQLite, the real worker, its real fenced claims and the real
``queued``/``waiting`` state machine.

What it shows:

1. **aging** — a low-priority task older than the aging window is tried before a
   newer high-priority one, so a steady stream of urgent work cannot starve the
   rest;
2. **waiting releases the slot** — a task that suspends on an external fact
   becomes ``waiting`` and stops occupying the only concurrency slot, so another
   task runs;
3. **round-robin** — with two namespaces, the next pick starts after the one last
   served, so one tenant's backlog does not starve the other;
4. **resume** — once the awaited fact is persisted, ``wake`` makes the waiting
   task schedulable again and it finishes, with its original attempt identity.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

T0 = 1_000_000.0


@dataclass
class _Status:
    value: str


@dataclass
class _Outcome:
    status: _Status
    stop_code: str = ""
    pending_interaction: dict | None = None


class _ScriptedHarness:
    """Completes, or suspends on an approval, without a model provider."""

    def __init__(self, store, task, attempt_id, suspend_for: set[str]) -> None:
        self._persistence = store
        self.store = store
        self.task = task
        self.suspend_for = suspend_for

    async def start(self, request, context):
        await self._confirm_initial()
        if self.task.task_id in self.suspend_for:
            return _Outcome(
                _Status("suspended"), pending_interaction={"kind": "approval"}
            )
        return _Outcome(_Status("completed"))

    async def resume(self, run_id, payload, context):
        return _Outcome(_Status("completed"))

    async def cancel(self, run_id, *, reason=""):
        return None

    async def _confirm_initial(self):
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


def main() -> int:
    from yanxu_harness.tasks import (
        InputContent,
        InputKind,
        LocalWorker,
        SchedulerLimits,
        SqliteControlStore,
        TaskAdmission,
    )

    directory = Path(tempfile.mkdtemp(prefix="yanxu-example09-"))
    print(f"state directory: {directory}")
    store = SqliteControlStore(directory / "control.db")
    suspend_for = {"a-old"}
    try:
        def admit(task_id: str, namespace: str, *, priority: int, age: float) -> None:
            asyncio.run(
                store.admit_task(
                    TaskAdmission(
                        namespace=namespace,
                        session_id="s1",
                        task_id=task_id,
                        run_id=f"run-{task_id}",
                        initial_input=InputContent(
                            kind=InputKind.INITIAL, text=f"do {task_id}"
                        ),
                        priority=priority,
                        created_at=T0 - age,
                    )
                )
            )

        # An old low-priority task, a brand-new high-priority one, and a second tenant.
        admit("a-old", "tenant-a", priority=0, age=100.0)
        admit("a-new", "tenant-a", priority=9, age=0.0)
        admit("b-1", "tenant-b", priority=1, age=0.0)
        print("step: admitted", "3 tasks across 2 namespaces")

        worker = LocalWorker(
            store,
            lambda task, attempt: _ScriptedHarness(store, task, attempt, suspend_for),
            limits=SchedulerLimits(
                max_concurrent=1,
                max_per_namespace=1,
                priority_aging_seconds=30.0,
                claim_ttl_seconds=300.0,
            ),
            clock=lambda: T0,
        )
        results = asyncio.run(worker.run_until_idle())
        order = [(item.task_id, item.outcome) for item in results]
        assert order[0] == ("a-old", "waiting"), order
        print("step: aging", "the old low-priority task was tried before the newer urgent one")
        served = [task_id for task_id, outcome in order if outcome.startswith("settled")]
        assert served == ["b-1", "a-new"], served
        print(
            "step: round-robin",
            f"tenant-b served before tenant-a's next task: {served}",
        )
        waiting = asyncio.run(store.read_task("a-old"))
        assert waiting.status.value == "waiting" and waiting.waiting_for == "approval"
        assert worker.slots == {"total": 0}, "waiting holds no concurrency slot"
        print("step: waiting-releases-slot", "the read is still one slot; a-old holds none")

        resumed = asyncio.run(worker.wake("a-old"))
        assert resumed is not None and resumed.status.value == "queued"
        final = asyncio.run(worker.run_once())
        assert final is not None and final.task_id == "a-old"
        assert asyncio.run(store.read_task("a-old")).status.value == "completed"
        print("step: resumed", "the external fact was persisted and the task finished")
        print("OK: the local worker is bounded, fair, and a wait does not block it")
        return 0
    finally:
        store.close()
        shutil.rmtree(directory, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
