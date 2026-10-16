"""Q6/D-01–D-02: the task diagnostics reader.

The plan's rows: a report that links task/run/attempt, inputs and their applied revision, the
scheduling claim and its fence, child relations, budget reservations and the control outbox —
read **without changing anything**, and with the raw task text, the child result and the absolute
workspace path kept out of the public output.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest

from yanxu_harness.diagnostics import (
    TaskInspector,
    task_report_to_json,
    task_report_to_markdown,
)
from yanxu_harness.diagnostics.__main__ import main
from yanxu_harness.errors import ConfigurationError, ContractError
from yanxu_harness.tasks import (
    ChildCoordinator,
    ChildOutcome,
    ChildPolicy,
    ChildSpawnRequest,
    InputAdmission,
    InputContent,
    InputKind,
    SqliteControlStore,
    TaskAdmission,
    TaskStatus,
)

NAMESPACE = "project-a"
SESSION = "s1"
SECRET_PROMPT = "SECRET-PROMPT-TEXT-do-the-thing"
SECRET_RESULT = "SECRET-CHILD-RESULT"
SECRET_WORKSPACE = "/work/secret-project-abc/child"


def run(coro: Any, *, timeout: float = 60.0) -> Any:
    async def guarded() -> Any:
        return await asyncio.wait_for(coro, timeout=timeout)

    return asyncio.run(guarded())


def _seed(path: Path) -> None:
    async def main_() -> None:
        store = SqliteControlStore(path)
        try:
            await store.admit_task(
                TaskAdmission(
                    namespace=NAMESPACE,
                    session_id=SESSION,
                    task_id="parent",
                    run_id="run-parent",
                    initial_input=InputContent(kind=InputKind.INITIAL, text=SECRET_PROMPT),
                    profile="worker",
                    workspace_ref="/work/secret-project-abc",
                )
            )
            follow_up = await store.admit_input(
                InputAdmission(
                    namespace=NAMESPACE,
                    session_id=SESSION,
                    task_id="parent",
                    input_id="steer-1",
                    content=InputContent(kind=InputKind.STEERING, text="another secret"),
                )
            )
            claim = await store.claim_task("parent", holder="w1", ttl_seconds=300.0)
            assert claim is not None
            assert follow_up.record is not None
            await store.commit_run_transition_with_inputs(
                "run-parent",
                {"phase": "running"},
                (),
                expected_version=None,
                applied_inputs=(follow_up.record,),
                claim=claim,
            )
            policy = ChildPolicy(
                profiles=frozenset({"researcher"}),
                tools=frozenset({"search"}),
                workspace_root="/work",
                budget_units={"model_requests": 5.0},
            )
            coordinator = ChildCoordinator(store, policy=policy)
            await coordinator.spawn(
                ChildSpawnRequest(
                    parent_task_id="parent",
                    namespace=NAMESPACE,
                    session_id=SESSION,
                    spawn_identity="spawn:1",
                    child_task_id="child",
                    child_run_id="run-child",
                    initial_input=InputContent(kind=InputKind.INITIAL, text="child prompt"),
                    profile="researcher",
                    workspace_ref=SECRET_WORKSPACE,
                    budget_amount=2.0,
                )
            )
            await store.claim_task("child", holder="w1", ttl_seconds=300.0)
            await coordinator.complete_child(
                "child",
                outcome=ChildOutcome.COMPLETED,
                reason="done",
                result={"secret": SECRET_RESULT},
                usage={"unit": "model_requests", "amount": 1.0},
            )
        finally:
            store.close()

    run(main_())


def _poison_pill(path: Path) -> str:
    """Write a sentinel that must not change if the reader only reads."""
    store = SqliteControlStore(path)

    async def grab() -> str:
        try:
            task = await store.read_task("parent")
            claim = await store.read_claim("parent")
            outbox = await store.read_outbox(task_id="parent", pending_only=False)
            inputs = await store.read_inputs("parent")
            return json.dumps(
                {
                    "task_updated_at": task.updated_at,
                    "claim_expires_at": None if claim is None else claim.expires_at,
                    "claim_fence": None if claim is None else claim.fencing_token,
                    "outbox": [(entry.outbox_id, entry.delivered_at, entry.attempts) for entry in outbox],
                    "inputs": [(record.input_id, record.state.value, record.applied_run_revision) for record in inputs],
                },
                sort_keys=True,
            )
        finally:
            store.close()

    return run(grab())


def test_the_reader_reports_the_linked_control_facts(tmp_path: Path) -> None:
    path = tmp_path / "control.db"
    _seed(path)
    reader = SqliteControlStore(path, read_only=True)
    try:
        report = run(
            TaskInspector(control=reader).inspect_task(
                "parent", namespace=NAMESPACE, session_id=SESSION
            )
        )
    finally:
        reader.close()
    assert report.status == TaskStatus.RUNNING.value
    assert report.current_attempt_id == "parent-a1"
    assert report.claim is not None and report.claim.fencing_token == 1 and report.claim.live
    # Inputs: the applied one names the run revision that took it in; the initial one is applied
    # by the task's own creation.
    applied = {item.input_id: item for item in report.inputs}
    assert set(applied) >= {"initial", "steer-1"}
    assert applied["steer-1"].state == "applied"
    assert applied["steer-1"].applied_run_revision == 1
    # Children, budget and notices are linked rather than inferred.
    assert [item.child_task_id for item in report.children] == ["child"]
    assert report.children[0].state == "completed"
    assert report.children[0].child_status == "completed"
    assert report.children[0].child_accepted is True
    assert [item.reservation_id for item in report.budget] == ["grant:spawn:1"]
    assert report.budget[0].state == "settled"
    kinds = sorted(item.kind for item in report.outbox)
    assert kinds == ["child_completed", "child_spawned"]
    assert report.notifications_pending == 2


def test_the_reader_changes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "control.db"
    _seed(path)
    before = _poison_pill(path)
    reader = SqliteControlStore(path, read_only=True)
    try:
        inspector = TaskInspector(control=reader)
        run(inspector.inspect_task("parent", namespace=NAMESPACE, session_id=SESSION))
        run(inspector.inspect_task("child", namespace=NAMESPACE, session_id=SESSION))
    finally:
        reader.close()
    assert _poison_pill(path) == before, (
        "the reader neither renewed a claim, delivered a notice nor consumed an input"
    )


def test_the_inspector_refuses_a_writable_store(tmp_path: Path) -> None:
    path = tmp_path / "control.db"
    _seed(path)
    store = SqliteControlStore(path)
    try:
        with pytest.raises(ConfigurationError):
            TaskInspector(control=store)
    finally:
        store.close()


def test_a_task_id_is_not_permission_to_read_another_tenant(tmp_path: Path) -> None:
    path = tmp_path / "control.db"
    _seed(path)
    reader = SqliteControlStore(path, read_only=True)
    try:
        inspector = TaskInspector(control=reader)
        with pytest.raises(ContractError) as wrong_namespace:
            run(inspector.inspect_task("parent", namespace="project-b", session_id=SESSION))
        assert "namespace" in str(wrong_namespace.value)
        with pytest.raises(ContractError) as wrong_session:
            run(inspector.inspect_task("parent", namespace=NAMESPACE, session_id="s2"))
        assert "session" in str(wrong_session.value)
    finally:
        reader.close()


def test_the_public_output_keeps_raw_text_out(tmp_path: Path) -> None:
    path = tmp_path / "control.db"
    _seed(path)
    reader = SqliteControlStore(path, read_only=True)
    try:
        inspector = TaskInspector(control=reader)
        report = run(
            inspector.inspect_task("parent", namespace=NAMESPACE, session_id=SESSION)
        )
        child_report = run(
            inspector.inspect_task("child", namespace=NAMESPACE, session_id=SESSION)
        )
    finally:
        reader.close()
    payload = task_report_to_json(report)
    markdown = task_report_to_markdown(report)
    child_payload = task_report_to_json(child_report)
    for secret in (SECRET_PROMPT, SECRET_RESULT, SECRET_WORKSPACE, "another secret"):
        assert secret not in payload, secret
        assert secret not in markdown, secret
        assert secret not in child_payload, secret
    # What replaces them is a digest, a tail and an enum — enough to link, not to leak.
    assert "content_digest" in payload
    assert child_report.workspace_ref_tail == "child"
    assert child_report.workspace_ref_digest.startswith("sha256:")
    assert report.children[0].child_result_digest.startswith("sha256:")
    assert json.loads(payload)["schema_version"] == report.schema_version


def test_a_terminal_task_with_pending_notices_says_so(tmp_path: Path) -> None:
    path = tmp_path / "control.db"
    _seed(path)

    async def finish_parent() -> None:
        store = SqliteControlStore(path)
        try:
            parent = await store.read_task("parent")
            await store.save_task(
                parent.transition(TaskStatus.COMPLETED, reason="work_completed", at=time.time()),
                expected_status=parent.status,
            )
        finally:
            store.close()

    run(finish_parent())
    reader = SqliteControlStore(path, read_only=True)
    try:
        report = run(
            TaskInspector(control=reader).inspect_task(
                "parent", namespace=NAMESPACE, session_id=SESSION
            )
        )
    finally:
        reader.close()
    assert report.status == "completed"
    assert any("still pending" in note for note in report.integrity)


def test_the_task_cli_reports_the_same_facts(tmp_path: Path, capsys: Any) -> None:
    path = tmp_path / "control.db"
    _seed(path)
    code = main(
        [
            "task",
            "--control",
            str(path),
            "--namespace",
            NAMESPACE,
            "--session",
            SESSION,
            "--task-id",
            "parent",
            "--format",
            "json",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    report = json.loads(out)
    assert report["task_id"] == "parent"
    assert SECRET_PROMPT not in out and SECRET_RESULT not in out


def test_the_task_cli_refuses_a_missing_store(tmp_path: Path, capsys: Any) -> None:
    code = main(
        [
            "task",
            "--control",
            str(tmp_path / "missing.db"),
            "--namespace",
            NAMESPACE,
            "--session",
            SESSION,
            "--task-id",
            "parent",
        ]
    )
    assert code == 2
    assert "refusing to create" in capsys.readouterr().err
