"""Example 8 — a redelivered input is applied once, across processes.

    python examples/08_durable_input_redelivery.py

The scripted part is the model: a tiny harness reports "completed" instead of
calling a provider. Everything else is real — real SQLite control and run stores,
a real second process that exits with ``os._exit``, and the real worker's
commit boundary.

What it shows, in order:

1. a second process admits a follow-up input into the same control database and
   dies *before* the run confirms it, so the input is still ``pending``;
2. the first process drives the task; the input is applied exactly once and
   names the run revision that took it in;
3. the same ``input_id`` with the same content is a **duplicate** — the second
   submission is not a second input and does not consume backlog capacity;
4. the same ``input_id`` with different content is a **conflict**, refused.

The point is the plan's I-01/I-03: a duplicate admission is not a second
application, and a crash before the commit leaves the input claimable rather
than lost.
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
TASK_ID = "task-1"

CHILD = """
import asyncio, os, sys
from yanxu_harness.tasks import InputAdmission, InputContent, InputKind, SqliteControlStore

async def main(path):
    store = SqliteControlStore(path)
    result = await store.admit_input(InputAdmission(
        namespace={namespace!r}, session_id={session!r}, task_id={task!r},
        input_id="follow-up-1",
        content=InputContent(kind=InputKind.FOLLOW_UP, text="please also check the logs")))
    print("child admitted:", result.outcome.value,
          result.record.sequence if result.record else "")
    sys.stdout.flush()
    # Exit without confirming anything: the admission is durable, the application is not.
    os._exit(9)

asyncio.run(main(sys.argv[1]))
""".format(namespace=NAMESPACE, session=SESSION, task=TASK_ID)


@dataclass
class _Status:
    value: str


@dataclass
class _Outcome:
    status: _Status
    stop_code: str = ""
    pending_interaction: dict | None = None


class _ScriptedHarness:
    """A scripted model: it completes the run and confirms the inputs it consumed.

    The commit is the real one — ``commit_run_transition_with_inputs`` writes the
    run's state, the events and the ``applied`` marks in one transaction.
    """

    def __init__(self, store, task, attempt_id):
        self._persistence = store
        self.store = store
        self.task = task

    async def start(self, request, context):
        await self._confirm_pending()
        return _Outcome(_Status("completed"))

    async def resume(self, run_id, payload, context):
        await self._confirm_pending()
        return _Outcome(_Status("completed"))

    async def cancel(self, run_id, *, reason=""):
        return None

    async def _confirm_pending(self):
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


def _admission(store, input_id: str, text: str):
    from yanxu_harness.tasks import InputAdmission, InputContent, InputKind

    return InputAdmission(
        namespace=NAMESPACE,
        session_id=SESSION,
        task_id=TASK_ID,
        input_id=input_id,
        content=InputContent(kind=InputKind.FOLLOW_UP, text=text),
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

    directory = Path(tempfile.mkdtemp(prefix="yanxu-example08-"))
    print(f"state directory: {directory}")
    store = SqliteControlStore(directory / "control.db")
    try:
        asyncio.run(
            store.admit_task(
                TaskAdmission(
                    namespace=NAMESPACE,
                    session_id=SESSION,
                    task_id=TASK_ID,
                    run_id="run-1",
                    initial_input=InputContent(kind=InputKind.INITIAL, text="start the task"),
                )
            )
        )
        print("step: admitted")

        # A real second process admits the follow-up and dies before it is applied.
        completed = subprocess.run(
            [sys.executable, "-c", CHILD, str(directory / "control.db")],
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, "PYTHONPATH": os.environ.get("PYTHONPATH", "")},
            check=False,
        )
        assert completed.returncode == 9, completed.stderr
        pending = [
            record
            for record in asyncio.run(store.read_inputs(TASK_ID))
            if record.input_id == "follow-up-1"
        ]
        assert pending and pending[0].state.value == "pending", pending
        print("step: cross-process-admit", "the input is pending, not lost")

        # A duplicate submission of the same id with the same content is not a second input.
        result = asyncio.run(store.admit_input(_admission(store, "follow-up-1", "please also check the logs")))
        assert result.outcome.value == "duplicate", result
        count = len(asyncio.run(store.read_inputs(TASK_ID)))
        print("step: duplicate", f"{result.outcome.value}; inputs still {count}")

        # The same id with different content is a conflict, refused rather than overwritten.
        conflict = asyncio.run(store.admit_input(_admission(store, "follow-up-1", "different text")))
        assert conflict.outcome.value == "refused"
        assert conflict.refusal_code == "content_conflict"
        print("step: conflict", conflict.refusal_code)

        # Drive the task: the pending follow-up is applied exactly once.
        worker = LocalWorker(
            store,
            lambda task, attempt: _ScriptedHarness(store, task, attempt),
            limits=SchedulerLimits(max_concurrent=1),
        )
        results = asyncio.run(worker.run_until_idle())
        assert any(item.outcome == "settled:completed" for item in results), results
        records = asyncio.run(store.read_inputs(TASK_ID))
        applied = [record for record in records if record.state.value == "applied"]
        follow_up = [record for record in records if record.input_id == "follow-up-1"]
        assert len(follow_up) == 1, "the duplicate did not create a second input"
        assert follow_up[0].state.value == "applied"
        assert follow_up[0].applied_run_revision is not None
        print(
            "step: applied-once",
            f"{len(applied)} applied, revision {follow_up[0].applied_run_revision}",
        )
        print("OK: a redelivered input was applied exactly once across processes")
        return 0
    finally:
        store.close()
        shutil.rmtree(directory, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
