"""MCP-specific fault windows for AC22/25/26, run in real child processes.

These tests close the evidence gap in the independent review: the delivery report
cited generic harness fault-window tests, but nothing proved that the *MCP* adapter,
ledger and receipts are wired into the same recovery windows. Each scenario dies in
a child process at a precise commit boundary, then a new process recovers the run.
No sleep guesses the window: the child exits inside the commit/settle call itself.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from support import harness

if not harness.sdk_available():  # pragma: no cover - exercised by the base gate
    pytest.skip(
        "the MCP extra is not installed; the MCP gate runs this suite with the pinned extra",
        allow_module_level=True,
    )

from package_paths import source_root  # noqa: E402
from yanxu_harness.runtime.harness import RunRecord  # noqa: E402
from yanxu_harness.storage import SqliteExecutionBackend, SqlitePersistence  # noqa: E402
from support.harness import counter, facts_of  # noqa: E402

CHILD = harness.SUPPORT_DIR / "mcp_fault_child.py"
RUN_ID = "r1"


def _run(mode: str, directory: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    parts = [str(source_root()), str(harness.SUPPORT_DIR)]
    existing = env.get("PYTHONPATH", "")
    if existing:
        parts.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return subprocess.run(
        [sys.executable, str(CHILD), mode, str(directory)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=90,
        env=env,
        check=False,
    )


def child_json(result: subprocess.CompletedProcess[str]) -> dict:
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines, f"child produced no output (exit {result.returncode})\n{result.stderr[-2000:]}"
    return json.loads(lines[-1])


def _record(directory: Path) -> RunRecord:
    async def read() -> RunRecord:
        store = SqlitePersistence(directory / "runs.db", read_only=True)
        try:
            stored = await store.load_run(RUN_ID)
        finally:
            store.close()
        assert stored is not None
        return RunRecord.from_record(stored.state)

    return asyncio.run(read())


def _states(directory: Path) -> dict[str, str]:
    backend = SqliteExecutionBackend(directory / "operations.db")
    try:
        return {row["operation_id"]: row["state"] for row in backend.all_operations()}
    finally:
        backend.close()


def _call_names(directory: Path) -> list[str]:
    return [
        str(row.get("name"))
        for row in facts_of(directory / "facts.jsonl", "server.call_tool.start")
    ]


# -- AC22: receipt committed, the Run has not recorded the result ---------------


def test_ac22_a_settled_mcp_receipt_survives_a_run_commit_crash(tmp_path) -> None:
    directory = tmp_path / "state"
    directory.mkdir()
    crashed = _run("settled-commit-die", directory)
    assert crashed.returncode == 9, (crashed.returncode, crashed.stderr[-2000:])

    assert _call_names(directory) == ["echo"], "the remote call happened exactly once"
    assert _states(directory) == {"project-a:s1:r1:request:1:c1": "succeeded"}
    record = _record(directory)
    assert record.status.value == "running"
    assert not [item for item in record.messages if item.get("role") == "toolResult"], (
        "the crash must leave the transcript without the tool result"
    )

    resumed = _run("resume-echo", directory)
    assert resumed.returncode == 0, resumed.stderr[-2000:]
    assert child_json(resumed)["status"] == "completed"
    assert _call_names(directory) == ["echo"], "a confirmed receipt is never dispatched again"
    assert _states(directory) == {"project-a:s1:r1:request:1:c1": "succeeded"}
    assert _record(directory).status.value == "completed"


# -- AC25: promote, then die before the Run is updated -------------------------


def test_ac25_a_promoted_mcp_receipt_is_reused_after_a_process_dies(tmp_path) -> None:
    directory = tmp_path / "state"
    directory.mkdir()
    paused = _run("unknown-pause", directory)
    assert paused.returncode == 0, paused.stderr[-2000:]
    assert child_json(paused)["status"] == "suspended"
    assert _states(directory) == {"project-a:s1:r1:request:1:c1": "unknown"}
    assert _record(directory).status.value == "suspended"

    promoted = _run("promote-die", directory)
    assert promoted.returncode == 9, (promoted.returncode, promoted.stderr[-2000:])
    assert _states(directory) == {"project-a:s1:r1:request:1:c1": "succeeded"}
    still_paused = _record(directory)
    assert still_paused.status.value == "suspended", (
        "the promotion is durable, but the Run was not updated before the crash"
    )
    assert _call_names(directory) == ["bump_then_die"]

    resumed = _run("resume", directory)
    assert resumed.returncode == 0, resumed.stderr[-2000:]
    assert child_json(resumed)["status"] == "completed"
    assert _call_names(directory) == ["bump_then_die"], (
        "the promoted operation must not be dispatched a second time"
    )
    assert _states(directory) == {"project-a:s1:r1:request:1:c1": "succeeded"}
    assert counter(directory / "state", "k") == 1, "the business effect happened exactly once"


# -- AC26: A settled, B unknown, C never sent; barrier, evidence, then C once ---


def test_ac26_an_mcp_partial_batch_barriers_then_runs_c_once_after_evidence(tmp_path) -> None:
    directory = tmp_path / "state"
    directory.mkdir()
    crashed = _run("batch-unknown-pause", directory)
    assert crashed.returncode == 9, (crashed.returncode, crashed.stderr[-2000:])
    assert _call_names(directory) == ["echo", "bump_then_die"], (
        "A ran, B ran, and C was never sent"
    )
    states = _states(directory)
    assert states == {
        "project-a:s1:r1:request:1:c1": "succeeded",
        "project-a:s1:r1:request:1:c2": "unknown",
    }
    assert "project-a:s1:r1:request:1:c3" not in states, "C has no intent at all"
    assert _record(directory).status.value == "running"

    blocked = _run("batch-resume-block", directory)
    assert blocked.returncode == 0, blocked.stderr[-2000:]
    payload = child_json(blocked)
    assert payload["status"] == "suspended"
    assert payload["pending"]["kind"] == "reconciliation"
    assert payload["pending"]["call_id"] == "c2"
    assert _call_names(directory) == ["echo", "bump_then_die"], (
        "the unproven B is a barrier: C must not be dispatched"
    )
    assert _record(directory).status.value == "suspended"

    recovered = _run("batch-promote-resume", directory)
    assert recovered.returncode == 0, recovered.stderr[-2000:]
    assert child_json(recovered)["status"] == "completed"
    assert _call_names(directory) == ["echo", "bump_then_die", "echo"], (
        "after evidence, C runs exactly once and A/B are not repeated"
    )
    assert _states(directory) == {
        "project-a:s1:r1:request:1:c1": "succeeded",
        "project-a:s1:r1:request:1:c2": "succeeded",
        "project-a:s1:r1:request:1:c3": "succeeded",
    }
    assert _record(directory).status.value == "completed"
