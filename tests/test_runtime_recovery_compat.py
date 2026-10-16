"""T5: what an older — or newer — record means to this build.

Three compatibility claims, each of which is a way to lose data if it is guessed at:

* a **version-1 execution snapshot** (written before per-call observation existed) is
  read conservatively: its calls are ``declared``, nothing about them is claimed, and only
  the ledger may say what happened;
* a **version this build does not know** is refused rather than interpreted;
* an **operations database written before the model-action column existed** migrates in
  place, and its rows stay readable (an unrecorded binding field is unprovable, not wrong);
* a snapshot that carries **no arrangement baseline** (schema-1/2, or a run whose
  declaration predates version 3) is *refused* by a recovery rather than filled in with
  the current deployment's values, because a check that cannot fail is not a check.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path

import pytest
from child_process import run_child
from durable_edit import (
    DeploymentPaths,
    ScriptedStream,
    build,
    build_partial,
    final_message,
    run_context,
)

from yanxu_harness.core.outcome import RunStatus
from yanxu_harness.core.execution import (
    EXECUTION_STATE_SCHEMA_VERSION,
    SUPPORTED_EXECUTION_STATE_SCHEMA_VERSIONS,
    Phase,
    pending_work_of,
    state_from_record,
    state_to_record,
)
from yanxu_harness.errors import ContractError, VersionRefused
from yanxu_harness.runtime.harness import RunRecord
from yanxu_harness.storage import SqliteExecutionBackend, SqlitePersistence
from yanxu_harness.tools import EffectState, OperationObservation, ToolIntent, ToolReceipt


def run(coro):
    return asyncio.run(coro)


def intent(operation_id: str = "project-a:s1:r1:call-1") -> ToolIntent:
    return ToolIntent(
        operation_id=operation_id,
        namespace="project-a",
        tool_name="notes.write",
        tool_version="1",
        args_hash="sha256:args",
        session_id="s1",
        run_id="r1",
        call_id="call-1",
    )


# --- execution-state versions -----------------------------------------------------------


def test_the_current_version_is_what_this_build_writes() -> None:
    assert EXECUTION_STATE_SCHEMA_VERSION == 4
    for readable in (1, 2, 3, 4):
        assert readable in SUPPORTED_EXECUTION_STATE_SCHEMA_VERSIONS
    assert EXECUTION_STATE_SCHEMA_VERSION in SUPPORTED_EXECUTION_STATE_SCHEMA_VERSIONS


def test_a_version_two_snapshot_without_a_baseline_is_refused_by_recovery(
    tmp_path: Path,
) -> None:
    """A declaration partition from before the baseline existed cannot be verified.

    The window is real: a v2 snapshot names every call and its progress, so it looks
    recoverable, but nothing in it says *which deployment* the calls were bound to. The
    only honest answers are "ask a human" or "refuse"; the one answer that must not
    happen is filling the baseline in with the current deployment and reporting the
    comparison as passed.
    """
    directory = tmp_path / "state"
    result = run_child(["partial-batch-then-die", str(directory), "", ""], timeout=60)
    assert result.returncode == 9
    paths = DeploymentPaths.under(directory)

    async def degrade() -> None:
        """Rewrite the stored snapshot the way a v2 build would have left it."""
        store = SqlitePersistence(paths.runs)
        try:
            stored = await store.load_run("r1")
            assert stored is not None
            record = RunRecord.from_record(stored.state)
            snapshot = dict(record.execution or {})
            snapshot.pop("arrangement", None)
            snapshot["schema_version"] = 2
            await store.commit_transition(
                "r1",
                replace(record, execution=snapshot).to_record(),
                expected_version=stored.version,
            )
        finally:
            store.close()

    from dataclasses import replace

    run(degrade())

    stream = ScriptedStream(final_message("done"))
    harness, _gateway = build_partial(
        paths, stream, attempt="a2", clock=lambda: time.time() + 400.0
    )
    with pytest.raises(ContractError, match="no recorded arrangement baseline"):
        run(harness.resume("r1", {}, run_context("a2")))

    assert stream.calls == 0
    if paths.counter.exists():
        assert len(paths.counter.read_text(encoding="utf-8").splitlines()) == 2, (
            "C must not run: the recovery could not prove the deployment is unchanged"
        )


def test_a_version_one_snapshot_is_read_conservatively() -> None:
    """A v1 RUNNING_BATCH record: declared calls, no per-call claim, no observation."""
    legacy = {
        "schema_version": 1,
        "phase": "running_batch",
        "messages": [],
        "new_messages": [],
        "pending": [],
        "prepared": [],
        "request_view": [],
        "context_state": None,
        "tools": [],
        "turns_used": 1,
        "max_turns": None,
        "model": None,
        "thinking_level": None,
        "last_assistant": {
            "role": "assistant",
            "content": [
                {"type": "toolCall", "id": "c1", "name": "t", "arguments": {"a": 1}},
            ],
            "stop_reason": "tool_use",
            "timestamp": 0,
        },
        "model_action_id": "request:1",
        "last_tool_results": [],
        "batch": [
            {"call_id": "c1", "tool_name": "t", "outcome": "unstarted", "status": "", "result": None},
        ],
        "has_more_tool_calls": False,
        "batch_terminated": False,
        "poll_site": None,
        "interaction": None,
        "cancel_requested": False,
        "cancel_reason": "",
        "stop": None,
        "status": None,
        "error_code": None,
        "detail": "",
        "consumed": [],
    }
    state = state_from_record(legacy)
    assert state.phase is Phase.RUNNING_BATCH
    assert state.schema_version == 1
    call = state.batch[0]
    assert call.observed is False
    assert call.progress.value == "declared", "a v1 record claims no schedule progress"
    assert call.observation_id == "" and call.result is None
    work = pending_work_of(state)
    assert work["replayable"] == [], (
        "being unstarted inside a dispatch window is not proof the call never ran"
    )
    assert [item["call_id"] for item in work["unobserved"]] == ["c1"]
    assert [item["call_id"] for item in work["declared"]] == ["c1"]

    # Re-serializing upgrades the record to the current shape without inventing facts.
    written = state_to_record(state)
    assert written["schema_version"] == 1, "the codec reports what it read"
    assert written["batch"][0]["progress"] == "declared"


def test_an_unknown_execution_state_version_is_refused() -> None:
    with pytest.raises(VersionRefused):
        state_from_record({"schema_version": EXECUTION_STATE_SCHEMA_VERSION + 1, "phase": "created"})


# --- operations ledger migration --------------------------------------------------------


OLD_SCHEMA = """
CREATE TABLE operations (
    operation_id TEXT PRIMARY KEY,
    namespace TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    tool_version TEXT NOT NULL,
    args_hash TEXT NOT NULL,
    call_id TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    run_id TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT,
    state TEXT NOT NULL,
    result TEXT,
    detail TEXT NOT NULL DEFAULT '',
    details TEXT NOT NULL DEFAULT '{}',
    external_id TEXT,
    intent_at REAL NOT NULL,
    settled_at REAL
);
"""


def test_an_operations_table_without_the_model_action_column_migrates_in_place(
    tmp_path: Path,
) -> None:
    path = tmp_path / "operations.db"
    connection = sqlite3.connect(path)
    connection.executescript(OLD_SCHEMA)
    connection.execute(
        "INSERT INTO operations(operation_id, namespace, tool_name, tool_version, args_hash, "
        "call_id, session_id, run_id, state, intent_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "project-a:s1:r1:call-1",
            "project-a",
            "notes.write",
            "1",
            "sha256:args",
            "call-1",
            "s1",
            "r1",
            EffectState.RUNNING.value,
            0.0,
        ),
    )
    connection.commit()
    connection.close()

    backend = SqliteExecutionBackend(path)
    try:
        columns = {
            row[1] for row in backend._connection.execute("PRAGMA table_info(operations)")  # noqa: SLF001
        }
        assert "model_action_id" in columns, "the migration must have added the column"
        observed = run(backend.observe(intent()))
        assert observed.observation is OperationObservation.OPEN, (
            "the pre-migration row is readable; its unrecorded model action is unprovable, "
            "not a mismatch"
        )
        # A brand new operation is admitted normally after the migration.
        other = intent("project-a:s1:r1:call-2")
        admitted = run(backend.admit(other))
        assert admitted.decision.value == "admitted"
        run(
            backend.settle(
                ToolReceipt(operation_id=other.operation_id, state=EffectState.SUCCEEDED, result={"ok": True})
            )
        )
        assert run(backend.observe(other)).receipt.state is EffectState.SUCCEEDED
    finally:
        backend.close()


def test_a_legacy_snapshot_without_a_declaration_keeps_the_conservative_orphan_path(
    tmp_path: Path,
) -> None:
    """An old RUNNING record cannot be recovered automatically, and says so."""
    paths = DeploymentPaths.under(tmp_path / "state")
    # A run row as a pre-observation build would have left it: running, with a snapshot
    # that names the call but carries no partition to classify.
    legacy = {
        "schema_version": 1,
        "phase": "running_batch",
        "messages": [],
        "new_messages": [],
        "pending": [],
        "prepared": [],
        "request_view": [],
        "context_state": None,
        "tools": [],
        "turns_used": 1,
        "max_turns": None,
        "model": None,
        "thinking_level": None,
        "last_assistant": {
            "role": "assistant",
            "content": [{"type": "toolCall", "id": "call-1", "name": "notes.write", "arguments": {"path": "a.md", "text": "hi"}}],
            "stop_reason": "tool_use",
            "timestamp": 0,
        },
        "model_action_id": "request:1",
        "last_tool_results": [],
        "batch": [],
        "has_more_tool_calls": False,
        "batch_terminated": False,
        "poll_site": None,
        "interaction": None,
        "cancel_requested": False,
        "cancel_reason": "",
        "stop": None,
        "status": None,
        "error_code": None,
        "detail": "",
        "consumed": [],
    }
    record = RunRecord(
        run_id="r1",
        namespace="project-a",
        session_id="s1",
        status=RunStatus.RUNNING,
        execution=legacy,
        messages=(),
    )

    async def write() -> None:
        store = SqlitePersistence(paths.runs)
        try:
            await store.commit_transition("r1", record.to_record(), expected_version=None)
        finally:
            store.close()

    run(write())

    harness, _gateway = build(
        paths,
        ScriptedStream(final_message("done")),
        attempt="a2",
        clock=lambda: time.time() + 400.0,
    )
    outcome = run(harness.resume("r1", {}, run_context("a2")))
    assert outcome.status.value == "suspended"
    interaction = dict(outcome.pending_interaction or {})
    assert interaction.get("kind") == "reconciliation"
    assert "Nothing is retried automatically" in str(interaction.get("detail") or "")

    # The suspension carries no batch at all, and a resume of it is refused rather than
    # reconstructed from the transcript — the pre-existing limit, kept explicit.
    with pytest.raises(ContractError, match="no recorded tool batch"):
        run(
            build(
                paths,
                ScriptedStream(final_message("done")),
                attempt="a3",
                clock=lambda: time.time() + 800.0,
            )[0].resume("r1", {}, run_context("a3"))
        )


def test_the_run_turn_budget_survives_recovery(tmp_path: Path) -> None:
    """A recovery continues the *same* logical run: it does not refresh the budget."""
    directory = tmp_path / "state"
    result = run_child(["partial-batch-then-die", str(directory), "", ""], timeout=60)
    assert result.returncode == 9
    paths = DeploymentPaths.under(directory)

    first, _gateway = build_partial(
        paths, ScriptedStream(final_message("done")), attempt="a2", clock=lambda: time.time() + 400.0
    )
    run(first.resume("r1", {}, run_context("a2")))

    async def reconciler(call, details):
        return {"proven": True, "state": "succeeded", "detail": "confirmed", "source": "store"}

    second, _gateway = build_partial(
        paths,
        ScriptedStream(final_message("finished")),
        attempt="a3",
        reconcile=reconciler,
        clock=lambda: time.time() + 800.0,
    )
    outcome = run(second.resume("r1", {}, run_context("a3")))
    assert outcome.status.value == "completed"
    assert outcome.turns_used == 2, (
        "one turn was spent by the crashed attempt; the recovery does not start over"
    )
