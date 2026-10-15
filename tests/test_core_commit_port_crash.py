"""S4/S6: the commit port proven across a real process boundary.

The claim under test is "the model's final response is durable before its tool calls are
dispatched". It cannot be shown by an exception double or a sleep: it needs a process
that actually dies *inside* a tool handler (``tests/fault_windows.py``'s
``batch-dispatch-die``), a commit log that was fsynced before that death, and an
effect log that proves the handler really ran. This file lives next to ``tests/``
(rather than under ``tests/core/``) because it spawns children through
``tests/child_process.py``, which is a root-level helper.
"""

from __future__ import annotations

import json
from pathlib import Path

from child_process import run_child
from yanxu_harness.core.execution import Phase, pending_work_of, state_from_record

# --- a real crash inside the batch, and what the next process can read -----------------


def test_a_crash_inside_a_tool_leaves_the_declared_call_committed_and_the_effect_unproven(
    tmp_path: Path,
) -> None:
    """The window the commit port exists for: effect happened, no receipt recorded.

    The child dies *inside* the handler with ``os._exit``, after the commit port has
    fsynced the state that names the call. So the next process reads a state that says
    "this call was declared, its outcome is unknown", and it also finds the external
    trace — which is exactly why the answer must be "check the ledger", not "replay".
    """
    commit_log = tmp_path / "commits.jsonl"
    effect_log = tmp_path / "effects.log"
    unused = tmp_path / "unused.log"

    result = run_child(
        ["batch-dispatch-die", str(commit_log), str(effect_log), str(unused)],
        timeout=60,
    )
    assert result.returncode == 9, (result.returncode, result.stderr[-2000:])
    assert effect_log.exists(), "the child must have produced its external trace before dying"

    lines = [json.loads(line) for line in commit_log.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert lines, "the commit port must have written at least one record before the crash"
    last = lines[-1]
    assert last["phase"] == Phase.RUNNING_BATCH.value, last["phase"]
    assert last["pending_work"]["batch_outcome_unknown"] is True
    assert [item["call_id"] for item in last["pending_work"]["declared"]] == ["c1"]
    # The declaration is durable (identity, order, argument digest) while no outcome is
    # claimed for the call: it is `declared`, not `observed`, and carries no result. That
    # is what lets a recovery process ask the ledger about this call instead of guessing.
    assert [
        (call["call_id"], call["outcome"], call["progress"], call["result"])
        for call in last["batch"]
    ] == [("c1", "unstarted", "declared", None)], (
        "no outcome may be claimed for a call that was still running"
    )
    assert last["last_tool_results"] == []

    # A fresh process rebuilds the state from the committed bytes and can still answer
    # "may I dispatch?" without the transcript's help.
    rebuilt = state_from_record(last)
    assert rebuilt.phase is Phase.RUNNING_BATCH
    work = pending_work_of(rebuilt)
    assert work["replayable"] == []
    assert [item["call_id"] for item in work["declared"]] == ["c1"]


