"""Offline Plan DAG walkthrough using the existing Task / Run / Gateway path.

Run from the repository root with ``python examples/15_plan_dag.py``. The fixed
Provider first emits a candidate JSON Plan, which the host parses and validates.
The host then reviews and approves it. LocalWorker drives its two dependent Steps
through DefaultHarness, Core Runner, ToolGateway, and SQLite receipts.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
from pathlib import Path

from yanxu_harness.plans import definition_digest, topological_order
from yanxu_harness.storage import SqliteExecutionBackend
from yanxu_harness.tasks.inbox import SqliteControlStore

from plan_dag_support import (
    FixedPlanProvider,
    NAMESPACE,
    PLAN_ID,
    SESSION_ID,
    acceptance_coordinator,
    approve_plan,
    deliver_plan_notices,
    make_worker,
    parse_plan_candidate,
    run_proposal,
)


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="yanxu-plan-dag-") as temporary:
        root = Path(temporary)
        workspace = root / "workspace"
        workspace.mkdir()
        (workspace / "source.txt").write_text(
            "Offline Plan DAG example: this exact text is read by ToolGateway.\n",
            encoding="utf-8",
        )
        control = SqliteControlStore(root / "control.sqlite", enable_orchestration=True)
        backend = SqliteExecutionBackend(root / "operations.sqlite")
        try:
            proposal_run_id = "plan-proposal-1"
            proposal = await run_proposal(FixedPlanProvider(), control, proposal_run_id)
            if proposal.status.value != "completed":
                raise RuntimeError(f"candidate Provider run did not complete: {proposal.status.value}")
            definition = parse_plan_candidate(
                proposal.final_text,
                proposal_run_id=proposal_run_id,
                now=time.time(),
            )
            order = topological_order(definition.steps)
            snapshot = await approve_plan(control, definition)
            print(
                json.dumps(
                    {
                        "proposal_run": proposal.run_id,
                        "candidate_digest": definition_digest(definition),
                        "approved_revision": snapshot.record.current_revision,
                        "approval": snapshot.record.active_approval_id,
                        "topological_order": list(order),
                    },
                    ensure_ascii=False,
                )
            )

            worker = make_worker(control, backend, workspace, holder="offline-worker")
            coordinator = acceptance_coordinator(control, backend)

            first = await worker.run_once()
            if first is None:
                raise RuntimeError("approved Plan did not yield its first ready Step")
            bindings = await control.read_plan_bindings(NAMESPACE, PLAN_ID, 1)
            first_binding = next(item for item in bindings if item.step_id == "inspect")
            ready_before_acceptance = await control.read_ready_steps(NAMESPACE, limit=8)
            if any(item.step_id == "verify" for item in ready_before_acceptance.candidates):
                raise RuntimeError("Task completion alone unlocked the dependent Step")
            first_acceptance = await coordinator.accept_step(
                NAMESPACE,
                PLAN_ID,
                "inspect",
                request_id="accept-inspect",
                principal="offline-host",
            )
            if first_acceptance[0].result.value != "accepted":
                raise RuntimeError(f"first Step was not accepted: {first_acceptance[0].result.value}")
            first_notices = await deliver_plan_notices(control, PLAN_ID)

            second = await worker.run_once()
            if second is None:
                raise RuntimeError("accepted dependency did not unlock the second Step")
            second_acceptance = await coordinator.accept_step(
                NAMESPACE,
                PLAN_ID,
                "verify",
                request_id="accept-verify",
                principal="offline-host",
            )
            if second_acceptance[0].result.value != "accepted":
                raise RuntimeError(f"second Step was not accepted: {second_acceptance[0].result.value}")
            second_notices = await deliver_plan_notices(control, PLAN_ID)

            latest = await control.read_plan(NAMESPACE, PLAN_ID)
            assert latest is not None
            completed = await coordinator.finalize_plan(
                NAMESPACE,
                PLAN_ID,
                expected_control_version=latest.record.control_version,
                request_id="finalize-source-review",
                principal="offline-host",
                now=time.time(),
            )
            final_bindings = await control.read_plan_bindings(NAMESPACE, PLAN_ID, 1)
            print(
                json.dumps(
                    {
                        "plan_status": completed.status.value,
                        "steps": [
                            {
                                "step_id": item.step_id,
                                "task_id": item.task_id,
                                "run_id": item.run_id,
                                "binding_id": item.binding_id,
                                "acceptance": "accepted",
                            }
                            for item in final_bindings
                        ],
                        "outbox_notices_delivered": first_notices + second_notices,
                    },
                    ensure_ascii=False,
                )
            )
        finally:
            backend.close()
            control.close()


if __name__ == "__main__":
    asyncio.run(main())
