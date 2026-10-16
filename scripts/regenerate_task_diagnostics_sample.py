"""Generate the committed task-diagnostics sample.

    python scripts/regenerate_task_diagnostics_sample.py

It seeds a real SQLite control store with the facts §11.1 asks a report to explain — a task and
its run/attempt, an applied steering input, a scheduling claim with its fence, a child relation
with its budget grant and completion notice, and a reservation — then reads it back through the
**read-only** ``TaskInspector`` and writes both formats next to this repository's other samples.

Nothing here is hand-written: the report is the inspector's own output.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / "src"))

from yanxu_harness.diagnostics import (  # noqa: E402
    TaskInspector,
    task_report_to_json,
    task_report_to_markdown,
)
from yanxu_harness.tasks import (  # noqa: E402
    ChildCoordinator,
    ChildOutcome,
    ChildPolicy,
    ChildSpawnRequest,
    InputAdmission,
    InputContent,
    InputKind,
    SqliteControlStore,
    TaskAdmission,
)

NAMESPACE = "project-a"
SESSION = "s1"


async def seed(path: Path) -> None:
    store = SqliteControlStore(path)
    try:
        await store.admit_task(
            TaskAdmission(
                namespace=NAMESPACE,
                session_id=SESSION,
                task_id="parent",
                run_id="run-parent",
                initial_input=InputContent(kind=InputKind.INITIAL, text="start the task"),
                profile="worker",
                workspace_ref="/work/secret-project/child-0000",
            )
        )
        steering = await store.admit_input(
            InputAdmission(
                namespace=NAMESPACE,
                session_id=SESSION,
                task_id="parent",
                input_id="steer-1",
                content=InputContent(kind=InputKind.STEERING, text="please also check the logs"),
            )
        )
        claim = await store.claim_task("parent", holder="worker-1", ttl_seconds=300.0)
        assert claim is not None and steering.record is not None
        initial = [
            record
            for record in await store.read_inputs("parent")
            if record.kind is InputKind.INITIAL
        ]
        await store.commit_run_transition_with_inputs(
            "run-parent",
            {"phase": "running"},
            (),
            expected_version=None,
            applied_inputs=(*initial, steering.record),
            claim=claim,
        )
        coordinator = ChildCoordinator(
            store,
            policy=ChildPolicy(
                profiles=frozenset({"researcher"}),
                tools=frozenset({"search"}),
                workspace_root="/work",
                budget_units={"model_requests": 5.0},
            ),
        )
        await coordinator.spawn(
            ChildSpawnRequest(
                parent_task_id="parent",
                namespace=NAMESPACE,
                session_id=SESSION,
                spawn_identity="spawn:0001",
                child_task_id="child",
                child_run_id="run-child",
                initial_input=InputContent(kind=InputKind.INITIAL, text="research the logs"),
                profile="researcher",
                budget_amount=2.0,
            )
        )
        await store.claim_task("child", holder="worker-1", ttl_seconds=300.0)
        # The child finished its own run; the parent is suspended on its committed result.
        await store.commit_run_transition_with_inputs(
            "run-child",
            {"status": "completed", "turns_used": 1},
            (),
            expected_version=None,
            task_id="child",
        )
        # The child finishes; its notice is committed but not yet delivered, so the sample shows
        # both a settled relation and a pending notification.
        await coordinator.complete_child(
            "child",
            outcome=ChildOutcome.COMPLETED,
            reason="child done",
            result={"answer": "42"},
            usage={"unit": "model_requests", "amount": 1.0},
        )
        await store.commit_run_transition_with_inputs(
            "run-parent",
            {
                "status": "suspended",
                "pending_interaction": {
                    "kind": "child_result",
                    "child_task_id": "child",
                    "spawn_identity": "spawn:0001",
                    "wait_ref": "run-parent:child_result:child",
                },
                "turns_used": 2,
            },
            (),
            expected_version=(
                (await store.load_run("run-parent")).version  # type: ignore[union-attr]
            ),
            task_id="parent",
        )
    finally:
        store.close()


async def report(path: Path) -> tuple[str, str]:
    reader = SqliteControlStore(path, read_only=True)
    try:
        inspector = TaskInspector(control=reader)
        parent = await inspector.inspect_task("parent", namespace=NAMESPACE, session_id=SESSION)
        return task_report_to_json(parent), task_report_to_markdown(parent)
    finally:
        reader.close()


def main() -> int:
    directory = Path(tempfile.mkdtemp(prefix="yanxu-task-sample-"))
    try:
        path = directory / "control.db"
        asyncio.run(seed(path))
        payload, markdown = asyncio.run(report(path))
        out = PACKAGE / "docs" / "samples"
        (out / "task-diagnostics.json").write_text(payload + "\n", encoding="utf-8")
        (out / "task-diagnostics.md").write_text(markdown, encoding="utf-8")
        print(f"wrote {out / 'task-diagnostics.json'} and {out / 'task-diagnostics.md'}")
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
