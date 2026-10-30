"""Real subprocess recovery of a Plan after Run commit and before Task settlement.

Run from the repository root with ``python examples/16_plan_recovery.py``. A child
process executes the first Step and exits immediately after DefaultHarness commits
the terminal Run. The parent reopens the SQLite store, recovers that same binding,
lets the coordinator accept its committed receipt, and runs the dependent Step.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from yanxu_harness.plans import PlanDefinition
from yanxu_harness.runtime import DefaultHarness, RunContext, RunOutcome, RunRequest
from yanxu_harness.storage import SqliteExecutionBackend
from yanxu_harness.tasks.inbox import SqliteControlStore

from plan_dag_support import (
    NAMESPACE,
    PLAN_ID,
    SESSION_ID,
    acceptance_coordinator,
    approve_plan,
    deliver_plan_notices,
    make_harness,
    make_worker,
    parse_plan_candidate,
)


class ExitAfterRunCommitHarness(DefaultHarness):
    """Stop the child at the durable Run/Task boundary used by the recovery example."""

    async def start(self, request: RunRequest, context: RunContext) -> RunOutcome:
        outcome = await super().start(request, context)
        print(
            json.dumps(
                {"crash_window": "run_committed_before_task_settlement", "run_id": outcome.run_id,
                 "run_status": outcome.status.value},
                ensure_ascii=False,
            ),
            flush=True,
        )
        os._exit(9)


async def child(control_path: Path, operations_path: Path, workspace: Path) -> None:
    control = SqliteControlStore(control_path, enable_orchestration=True)
    backend = SqliteExecutionBackend(operations_path)
    worker = make_worker(
        control,
        backend,
        workspace,
        holder="crash-window-child",
        claim_ttl_seconds=0.1,
    )
    def crashing_factory(task: Any, attempt_id: str) -> DefaultHarness:
        return make_harness(
            control,
            backend,
            workspace,
            task,
            attempt_id,
            harness_type=ExitAfterRunCommitHarness,
        )

    worker._harness_for = crashing_factory
    result = await worker.run_once()
    # Reaching this line means the intended Run/Task crash boundary was not reached.
    print(json.dumps({"unexpected_child_result": None if result is None else result.outcome}), flush=True)
    raise SystemExit(2)


async def parent(root: Path) -> None:
    workspace = root / "workspace"
    workspace.mkdir()
    (workspace / "source.txt").write_text(
        "This file is read once before a process crash and once after recovery.\n",
        encoding="utf-8",
    )
    control_path = root / "control.sqlite"
    operations_path = root / "operations.sqlite"
    control = SqliteControlStore(control_path, enable_orchestration=True)
    now = time.time()
    definition: PlanDefinition = parse_plan_candidate(
        json.dumps(
            {
                "title": "Recover a two-step source review",
                "goal": "Prove that committed Run and Gateway facts survive a worker process exit.",
                "steps": [
                    {
                        "step_id": "inspect",
                        "title": "Inspect source",
                        "instruction": "Read source.txt through the declared read tool.",
                        "depends_on": [],
                    },
                    {
                        "step_id": "verify",
                        "title": "Verify source",
                        "instruction": "Read source.txt after the first Step is accepted.",
                        "depends_on": ["inspect"],
                    },
                ],
            },
            ensure_ascii=False,
        ),
        now=now,
    )
    try:
        approved = await approve_plan(control, definition)
        print(
            json.dumps(
                {"approved_revision": approved.record.current_revision,
                 "approval": approved.record.active_approval_id},
                ensure_ascii=False,
            )
        )
    finally:
        control.close()

    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--child",
        str(control_path),
        str(operations_path),
        str(workspace),
    ]
    child_result = subprocess.run(command, capture_output=True, text=True, check=False)
    if child_result.returncode != 9:
        raise RuntimeError(
            f"crash child exited {child_result.returncode}, expected 9\n"
            f"stdout:\n{child_result.stdout}\nstderr:\n{child_result.stderr}"
        )
    print(child_result.stdout.strip())

    control = SqliteControlStore(control_path, enable_orchestration=True)
    backend = SqliteExecutionBackend(operations_path)
    try:
        bindings = await control.read_plan_bindings(NAMESPACE, PLAN_ID, 1)
        first_binding = next(item for item in bindings if item.step_id == "inspect")
        first_task = await control.read_task(first_binding.task_id)
        first_run = await control.load_run(first_binding.run_id)
        if first_task is None or first_task.status.value != "running":
            raise RuntimeError("the child did not leave the Task running at the crash boundary")
        if first_run is None or first_run.state.get("status") != "completed":
            raise RuntimeError("the child did not commit a terminal Run before exit")
        operations = [
            item for item in backend.all_operations()
            if item["run_id"] == first_binding.run_id and item["state"] == "succeeded"
        ]
        if len(operations) != 1 or backend.receipt(operations[0]["operation_id"]) is None:
            raise RuntimeError("the real ToolGateway receipt was not durable across the process exit")
        old_claim = await control.read_claim(first_binding.task_id)
        if old_claim is None:
            raise RuntimeError("the child process did not leave its scheduler claim for recovery")

        recovery_at = max(time.time(), old_claim.expires_at + 0.01)
        worker = make_worker(
            control,
            backend,
            workspace,
            holder="recovery-parent",
            clock=lambda: recovery_at,
            claim_ttl_seconds=0.1,
        )
        recovered = await worker.run_once(now=recovery_at)
        if recovered is None or recovered.task_id != first_binding.task_id:
            raise RuntimeError("the parent did not reclaim the original bound Task")
        settled_task = await control.read_task(first_binding.task_id)
        if settled_task is None or settled_task.status.value != "completed":
            raise RuntimeError("the recovered worker did not settle the original Task")
        print(
            json.dumps(
                {"recovered_binding": first_binding.binding_id,
                 "task_id": first_binding.task_id, "run_id": first_binding.run_id,
                 "run_status": first_run.state["status"],
                 "task_status_after_recovery": settled_task.status.value,
                 "gateway_receipt": operations[0]["operation_id"]},
                ensure_ascii=False,
            )
        )

        coordinator = acceptance_coordinator(control, backend)
        first_reconciliation = await coordinator.reconcile(
            NAMESPACE, PLAN_ID, principal="offline-host", now=recovery_at + 0.01
        )
        if first_reconciliation.accepted != 1:
            raise RuntimeError(f"first binding was not accepted: {first_reconciliation}")
        await deliver_plan_notices(control, PLAN_ID)

        second_now = recovery_at + 1.0
        second_worker = make_worker(
            control,
            backend,
            workspace,
            holder="recovery-parent",
            clock=lambda: second_now,
            claim_ttl_seconds=0.1,
        )
        second = await second_worker.run_once(now=second_now)
        if second is None:
            raise RuntimeError("acceptance did not unlock the dependent Step")
        second_reconciliation = await coordinator.reconcile(
            NAMESPACE, PLAN_ID, principal="offline-host", now=second_now + 0.01
        )
        if second_reconciliation.accepted != 1:
            raise RuntimeError(f"second binding was not accepted: {second_reconciliation}")
        await deliver_plan_notices(control, PLAN_ID)

        latest = await control.read_plan(NAMESPACE, PLAN_ID)
        assert latest is not None
        completed = await coordinator.finalize_plan(
            NAMESPACE,
            PLAN_ID,
            expected_control_version=latest.record.control_version,
            request_id="finalize-recovered-source-review",
            principal="offline-host",
            now=second_now + 0.02,
        )
        final_bindings = await control.read_plan_bindings(NAMESPACE, PLAN_ID, 1)
        print(
            json.dumps(
                {"plan_status": completed.status.value,
                 "bindings": [item.binding_id for item in final_bindings],
                 "tasks": [item.task_id for item in final_bindings],
                 "runs": [item.run_id for item in final_bindings]},
                ensure_ascii=False,
            )
        )
    finally:
        backend.close()
        control.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", action="store_true")
    parser.add_argument("control_path", nargs="?")
    parser.add_argument("operations_path", nargs="?")
    parser.add_argument("workspace", nargs="?")
    args = parser.parse_args()
    if args.child:
        if not args.control_path or not args.operations_path or not args.workspace:
            parser.error("--child requires control_path, operations_path, and workspace")
        asyncio.run(child(Path(args.control_path), Path(args.operations_path), Path(args.workspace)))
        return
    with tempfile.TemporaryDirectory(prefix="yanxu-plan-recovery-") as temporary:
        asyncio.run(parent(Path(temporary)))


if __name__ == "__main__":
    main()
