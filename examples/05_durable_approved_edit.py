"""Example 5 — a durable, approval-bound edit through the public API only.

Run it with no arguments and it performs the whole chain itself, in real
subprocesses, in a temporary directory:

    python examples/05_durable_approved_edit.py

The chain it demonstrates is the one the package exists for:

```text
task → the model asks for a tool call → the gateway previews the real change
     → the run suspends and the pause is persisted → the process exits
     → a new process reads the status → the operator approves (trusted local entry)
     → a new process resumes → only the unfinished call runs → the model receives
       the real result and answers
```

Every step above is a **separate process** (`start`, `status`, `approve`, `resume`),
so the persistence claim is tested rather than asserted: nothing is carried in
memory between the steps.

What is real and what is simulated:

* **Real**: the model loop, the provider→core adapter, the tool gateway (policy,
  approval binding, intent, receipt) and all three ledgers (run state, operations,
  approvals) — plus the business store, whose row version is checked with a
  conditional `UPDATE` so an approval for an older version cannot overwrite newer
  data.
* **Simulated, and clearly labelled**: the model. `ScriptedEditModel` implements the
  public `Provider` protocol and decides what to answer from the request it
  receives — including echoing the tool result it was given, which is why the final
  answer proves the result travelled through the loop. It is not an OpenAI mock and
  it does not call anything.

Manual operation (the same commands the demo runs; `--state DIR` holds every
ledger):

```bash
python examples/05_durable_approved_edit.py --state /tmp/edit start --task "更新欢迎语"
python examples/05_durable_approved_edit.py --state /tmp/edit status --run <run_id>
python examples/05_durable_approved_edit.py --state /tmp/edit approve --approval <binding_key> --actor you
python examples/05_durable_approved_edit.py --state /tmp/edit resume --run <run_id>
```

`approve` is a **trusted local operator entry**: it writes the decision into the
approval ledger the gateway reads. It is not a web endpoint and this package does
not authenticate users — a host that exposes approvals over HTTP must do its own
authorisation in front of it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, AsyncIterator, Mapping, Sequence

from yanxu_harness.core.types import TextContent
from yanxu_harness.providers import CoreStreamBridge
from yanxu_harness.providers.protocol import (
    Provider,
    ProviderEvent,
    ProviderEventKind,
    ProviderRequest,
)
from yanxu_harness.runtime import (
    DefaultHarness,
    Principal,
    RunContext,
    RunIdentity,
    RunOutcome,
    RunRequest,
)
from yanxu_harness.storage import (
    SqliteApprovalStore,
    SqliteExecutionBackend,
    SqlitePersistence,
)
from yanxu_harness.tools import (
    ApprovalStatus,
    ConservativePolicy,
    GatewayBindings,
    ToolDefinition,
    ToolEffect,
    ToolGateway,
    ToolRejected,
    ToolRegistry,
    core_tool,
)

NOTE_PATH = "notes/welcome.md"
RUN_ID = "edit-1"
NAMESPACE = "example-project"
SESSION = "session-1"

_BUSINESS_SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    path TEXT PRIMARY KEY,
    text TEXT NOT NULL,
    version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS write_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt TEXT NOT NULL
);
"""


# --- the business store: a versioned record updated with a conditional write ---


class NoteStore:
    """A tiny business store whose only write is a compare-and-set.

    The version check is the *database's*: `UPDATE ... WHERE version = ?` either
    changes one row or none, and "none" is reported as a refusal. A handler that
    merely compared the model's `expected_version` against a value it read earlier
    would be checking a memory, not the data.
    """

    def __init__(self, path: Path) -> None:
        self._connection = sqlite3.connect(str(path), check_same_thread=False)
        self._connection.executescript(_BUSINESS_SCHEMA)
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def ensure(self, path: str, text: str) -> None:
        self._connection.execute(
            "INSERT OR IGNORE INTO notes(path, text, version) VALUES (?, ?, 1)", (path, text)
        )
        self._connection.commit()

    def read(self, path: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT path, text, version FROM notes WHERE path = ?", (path,)
        ).fetchone()
        return None if row is None else {"path": row[0], "text": row[1], "version": int(row[2])}

    def apply(self, path: str, expected_version: int, text: str) -> dict[str, Any]:
        """Write only if the stored version is still `expected_version`."""
        self.record_attempt()
        cursor = self._connection.execute(
            "UPDATE notes SET text = ?, version = version + 1 WHERE path = ? AND version = ?",
            (text, path, expected_version),
        )
        self._connection.commit()
        if cursor.rowcount != 1:
            current = self.read(path)
            raise ToolRejected(
                f"{path}: version {expected_version} is no longer current"
                + (f" (it is {current['version']})" if current else " (the note is gone)")
                + "; the edit was not applied",
                details={"expected_version": expected_version, "current": current},
            )
        return self.read(path) or {}

    def record_attempt(self) -> None:
        """Count handler invocations, so "ran exactly once" is evidence."""
        self._connection.execute("INSERT INTO write_attempts(attempt) VALUES ('write')")
        self._connection.commit()

    def attempts(self) -> int:
        return int(self._connection.execute("SELECT COUNT(*) FROM write_attempts").fetchone()[0])

    def update_out_of_band(self, path: str, text: str) -> dict[str, Any]:
        """Stand in for "someone else changed the note while the run waited"."""
        self._connection.execute("UPDATE notes SET text = ?, version = version + 1 WHERE path = ?", (text, path))
        self._connection.commit()
        return self.read(path) or {}


# --- the tool: preview the real change, write with a conditional update --------


class EditTools:
    def __init__(self, store: NoteStore) -> None:
        self.store = store

    def definition(self) -> ToolDefinition:
        def validator(arguments: Mapping[str, Any]) -> dict[str, Any]:
            if not isinstance(arguments.get("text"), str) or not arguments["text"]:
                raise ValueError("text must be a non-empty string")
            if not isinstance(arguments.get("expected_version"), int):
                raise ValueError("expected_version must be an integer")
            return {"text": arguments["text"], "expected_version": arguments["expected_version"]}

        def preview(_context: Any, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
            # The approver must see the *real* current value and the proposed one,
            # not what the model claimed the current value was.
            current = self.store.read(NOTE_PATH) or {}
            return {
                "path": NOTE_PATH,
                "current_text": current.get("text"),
                "current_version": current.get("version"),
                "proposed_text": arguments["text"],
                "expected_version": arguments["expected_version"],
            }

        def handler(_context: Any, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
            # Re-checked here, at the moment of the write: the approval was bound to
            # a version, and if the row moved the edit is refused rather than
            # overwriting newer data.
            current = self.store.read(NOTE_PATH) or {}
            if current.get("version") != arguments["expected_version"]:
                raise ToolRejected(
                    f"{NOTE_PATH}: approved for version {arguments['expected_version']}, "
                    f"but the note is now at version {current.get('version')}",
                    details={"approved_version": arguments["expected_version"], "current": current},
                )
            written = self.store.apply(NOTE_PATH, arguments["expected_version"], arguments["text"])
            return {
                "content": [
                    TextContent(
                        text=f"{NOTE_PATH} is now version {written['version']}: {written['text']}"
                    )
                ],
                "details": {"version": written["version"], "path": NOTE_PATH},
            }

        return ToolDefinition(
            "notes.edit",
            "replace the text of a note, if it is still the version the caller expects",
            effects=frozenset({ToolEffect.WRITE}),
            args_validator=validator,
            handler=handler,
            preview=preview,
        )


def target_version(store: NoteStore, _call_id: str, _arguments: Mapping[str, Any]) -> str | None:
    """The server-side version an approval binds to."""
    current = store.read(NOTE_PATH)
    return None if current is None else str(current["version"])


# --- the simulated model: a real Provider, deciding from the request -----------


class ScriptedEditModel(Provider):
    """A deterministic model that implements the public ``Provider`` protocol.

    It reads the request it is given: with no tool result yet it asks for the edit;
    once the transcript carries the tool's result it echoes that result in its final
    answer. That is why the demo's final text proves the result came back through the
    loop instead of being printed by the example.
    """

    name = "scripted-edit-model"

    def __init__(self, text: str, *, expected_version: int) -> None:
        self.text = text
        self.expected_version = expected_version

    async def stream(
        self, request: ProviderRequest, cancellation: Any = None
    ) -> AsyncIterator[ProviderEvent]:
        results = [
            message for message in request.messages if message.get("role") == "tool"
        ]
        if results:
            observed = str(results[-1].get("content") or "")
            yield ProviderEvent(
                kind=ProviderEventKind.TEXT_DELTA,
                sequence=0,
                text=f"编辑完成。工具报告：{observed}",
            )
            yield ProviderEvent(kind=ProviderEventKind.TERMINAL, sequence=1, stop_reason="stop")
            return
        yield ProviderEvent(
            kind=ProviderEventKind.TOOL_CALL,
            sequence=0,
            tool_call_id="call-1",
            tool_name="notes.edit",
            arguments_json=json.dumps(
                {"text": self.text, "expected_version": self.expected_version}
            ),
        )
        yield ProviderEvent(kind=ProviderEventKind.TERMINAL, sequence=1, stop_reason="tool_calls")


# --- wiring -------------------------------------------------------------------


class Deployment:
    """Every component of the example, built from one state directory."""

    def __init__(
        self,
        state: Path,
        *,
        text: str,
        expected_version: int,
        attempt: str,
        run_id: str = RUN_ID,
    ) -> None:
        self.run_id = run_id
        self.state = state
        state.mkdir(parents=True, exist_ok=True)
        self.store = NoteStore(state / "notes.db")
        self.store.ensure(NOTE_PATH, "你好，世界。")
        self.tools = EditTools(self.store)
        self.approvals = SqliteApprovalStore(state / "approvals.db")
        self.gateway = ToolGateway(
            registry=ToolRegistry([self.tools.definition()]),
            # A write needs an approval: that is what makes the run pause.
            policy=ConservativePolicy(),
            approvals=self.approvals,
            backend=SqliteExecutionBackend(state / "operations.db"),
        )
        self.bindings = GatewayBindings(
            gateway=self.gateway,
            context=lambda: self.context(attempt),
            target_version=lambda call_id, arguments: target_version(self.store, call_id, arguments),
        )
        self.harness = DefaultHarness(
            stream_fn=CoreStreamBridge(ScriptedEditModel(text, expected_version=expected_version)),
            tools=[core_tool(self.tools.definition(), self.bindings)],
            persistence=SqlitePersistence(state / "runs.db"),
            system_prompt="You edit one note. Use the tool; do not describe the edit.",
            # The host names the policy it configured, so a resume can tell whether
            # the arrangement an approval was granted for still holds.
            fingerprints={"policy": "example-policy-1"},
        )

    def context(self, attempt: str) -> RunContext:
        return RunContext(
            identity=RunIdentity(
                namespace=NAMESPACE, session_id=SESSION, run_id=self.run_id, attempt_id=attempt
            ),
            principal=Principal(subject="local-operator"),
        )


def _outcome_payload(outcome: RunOutcome, store: NoteStore) -> dict[str, Any]:
    return {
        "run_id": outcome.run_id,
        "status": outcome.status.value,
        "stop_code": outcome.stop_code,
        "turns_used": outcome.turns_used,
        "final_text": outcome.final_text,
        "pending_interaction": outcome.pending_interaction,
        "error_code": outcome.error_code,
        "note": store.read(NOTE_PATH),
        "attempts": store.attempts(),
    }


async def _start(state: Path, task: str, run_id: str = RUN_ID) -> dict[str, Any]:
    deployment = Deployment(state, text=task, expected_version=1, attempt="a1", run_id=run_id)
    outcome = await deployment.harness.start(
        RunRequest(prompt=task), deployment.context("a1")
    )
    payload = _outcome_payload(outcome, deployment.store)
    payload["open_approvals"] = [item.key for item in deployment.approvals.pending()]
    deployment.store.close()
    deployment.approvals.close()
    return payload


async def _status(state: Path, run_id: str) -> dict[str, Any]:
    deployment = Deployment(state, text="", expected_version=1, attempt="a1", run_id=run_id)
    try:
        run_state = await deployment.harness.state(run_id, context=deployment.context("a1"))
        opened = [item.key for item in deployment.approvals.pending()]
        note = deployment.store.read(NOTE_PATH)
    finally:
        deployment.store.close()
        deployment.approvals.close()
    return {
        "run_id": run_state.run_id,
        "status": run_state.status.value,
        "attempt": run_state.attempt_id,
        "turns_used": run_state.turns_used,
        "stop_code": run_state.stop_code,
        "pending_interaction": run_state.pending_interaction,
        "final_text": run_state.final_text,
        "note": note,
        "open_approvals": opened,
    }


async def _approve(state: Path, binding_key: str, actor: str) -> dict[str, Any]:
    store = SqliteApprovalStore(state / "approvals.db")
    notes = NoteStore(state / "notes.db")
    try:
        approval = await store.decide(binding_key, ApprovalStatus.APPROVED, decided_by=actor)
        payload = {
            "approval": approval.key,
            "status": approval.status.value,
            "decided_by": approval.decided_by,
            "note": notes.read(NOTE_PATH),
        }
    finally:
        store.close()
        notes.close()
    return payload


async def _resume(state: Path, run_id: str, attempt: str) -> dict[str, Any]:
    deployment = Deployment(state, text="", expected_version=1, attempt=attempt, run_id=run_id)
    try:
        outcome = await deployment.harness.resume(run_id, {}, deployment.context(attempt))
        payload = _outcome_payload(outcome, deployment.store)
        payload["attempt"] = attempt
    finally:
        deployment.store.close()
        deployment.approvals.close()
    return payload


# --- the self-contained demo --------------------------------------------------


async def _someone_else_edits(state: Path, text: str) -> dict[str, Any]:
    """A different writer changes the note while the run waits for its decision."""
    store = NoteStore(state / "notes.db")
    try:
        return store.update_out_of_band(NOTE_PATH, text)
    finally:
        store.close()


def _run_command(state: Path, *args: str) -> dict[str, Any]:
    """Run one step in a real child process and read its JSON answer."""
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--state", str(state), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if completed.returncode != 0 or not lines:
        raise SystemExit(
            f"step {args!r} failed (exit {completed.returncode})\n"
            f"stdout:\n{completed.stdout[-2000:]}\nstderr:\n{completed.stderr[-2000:]}"
        )
    return json.loads(lines[-1])


def _scenario_happy(state: Path) -> None:
    """The chain: start → pause → status → approve → resume → refuse a repeat."""
    started = _run_command(state, "start", "--task", "你好，持久的世界。")
    print(f"1. start   → {started['status']} (run {started['run_id']}, attempt a1)")
    interaction = started["pending_interaction"] or {}
    preview = interaction.get("preview") or {}
    print(
        "   preview  → "
        f"version {preview.get('current_version')}: {preview.get('current_text')!r} → "
        f"{preview.get('proposed_text')!r}"
    )
    assert started["status"] == "suspended", started
    assert interaction.get("kind") == "approval", interaction
    assert started["note"]["version"] == 1, "the pause must not have written anything"
    assert started["note"]["text"] == "你好，世界。"
    assert started["attempts"] == 0, "no handler may run before the decision"
    assert started["open_approvals"] == [interaction["approval_binding_key"]], (
        "the pause left exactly one open approval"
    )

    binding_key = interaction["approval_binding_key"]
    statused = _run_command(state, "status", "--run", started["run_id"])
    print(f"2. status  → {statused['status']} (attempt {statused['attempt']}) — read by another process")
    assert statused["status"] == "suspended"
    assert statused["pending_interaction"]["approval_binding_key"] == binding_key

    approved = _run_command(state, "approve", "--approval", binding_key, "--actor", "local-operator")
    print(f"3. approve → {approved['status']} by {approved['decided_by']}")
    assert approved["status"] == "approved"
    assert approved["note"]["version"] == 1, "approving writes nothing on its own"

    resumed = _run_command(state, "resume", "--run", started["run_id"])
    print(f"4. resume  → {resumed['status']} (attempt {resumed['attempt']})")
    print(f"   answer   → {resumed['final_text']}")
    assert resumed["status"] == "completed", resumed
    assert resumed["attempt"] == "a2", "the resume advanced the attempt"
    assert resumed["note"]["version"] == 2, "the write happened exactly once"
    assert resumed["note"]["text"] == "你好，持久的世界。"
    assert resumed["attempts"] == 1, "the handler ran exactly once, in the resume process"
    assert "工具报告" in resumed["final_text"], "the model saw the tool's real result"
    assert NOTE_PATH in resumed["final_text"]

    repeated = _run_command(state, "resume", "--run", started["run_id"])
    print(f"5. resume again → refused: {repeated.get('error', repeated.get('status'))}")
    assert repeated.get("refused") is True, repeated
    assert repeated["note"]["version"] == 2, "a repeated resume writes nothing"


def _scenario_stale_approval(state: Path) -> None:
    """The row moves while the run waits: the stale approval must not overwrite it."""
    started = _run_command(state, "start", "--task", "这是过期的编辑。", "--run", "edit-2")
    assert started["status"] == "suspended", started
    binding_key = started["pending_interaction"]["approval_binding_key"]
    approved = _run_command(state, "approve", "--approval", binding_key, "--actor", "local-operator")
    assert approved["status"] == "approved"

    # A second writer changes the note after the approval was granted.
    elsewhere = _run_command(state, "edit-elsewhere", "--text", "另一个人写的。")
    print(f"6. stale   → note is now version {elsewhere['version']}: {elsewhere['text']!r}")
    assert elsewhere["version"] == 2

    again = _run_command(state, "resume", "--run", "edit-2")
    print(f"7. resume  → {again['status']} (the approval no longer applies)")
    print(f"   reason   → {again.get('pending_interaction', {}).get('detail', '')}")
    assert again["status"] == "suspended", again
    assert again["note"]["version"] == 2, "the stale approval did not overwrite the newer text"
    assert again["note"]["text"] == "另一个人写的。"
    fresh_binding = again["pending_interaction"]["approval_binding_key"]
    # The *key* stays the same on purpose: it identifies the call (namespace + call id
    # + tool), which is what lets the ledger explain that the operator already
    # answered — a different question. What changed are the bound values.
    assert fresh_binding == binding_key, "the key identifies the call, not the binding"
    assert again["pending_interaction"]["preview"]["current_version"] == 2, (
        "the operator is shown the *current* value, not the value from the pause"
    )
    approvals = SqliteApprovalStore(state / "approvals.db")
    try:
        history = approvals.history(binding_key)
    finally:
        approvals.close()
    assert [entry["status"] for entry in history] == ["approved"], (
        f"the earlier decision is kept as history: {history}"
    )

    # Approving the *new* binding is not enough either: the model still asked for
    # version 1 and the note is at version 2, so the handler's conditional write
    # refuses and the model is told why. Nothing overwrites the newer text.
    _run_command(state, "approve", "--approval", fresh_binding, "--actor", "local-operator")
    final = _run_command(state, "resume", "--run", "edit-2")
    print(f"8. resume  → {final['status']} (the write was refused, the newer text stands)")
    print(f"   answer   → {final['final_text']}")
    assert final["status"] == "completed", final
    assert final["note"]["version"] == 2, "an edit approved for an older version must not write"
    assert final["note"]["text"] == "另一个人写的。"
    assert "denied" in final["final_text"], "the model is told why the edit was refused"
    assert "version 2" in final["final_text"]


def _scenario_business_cas(directory: Path) -> None:
    """The business store's own compare-and-set, called directly.

    The handler re-checks the version before writing; this shows the mechanism it
    relies on, with the real store rather than a mock: a write for an older version
    changes nothing and says why.
    """
    directory.mkdir(parents=True, exist_ok=True)
    store = NoteStore(directory / "cas-notes.db")
    try:
        store.ensure(NOTE_PATH, "第一版。")
        moved = store.update_out_of_band(NOTE_PATH, "第二版。")
        assert moved["version"] == 2
        try:
            store.apply(NOTE_PATH, expected_version=1, text="基于第一版的写入。")
        except ToolRejected as exc:
            print(f"9. cas     → refused: {exc}")
        else:  # pragma: no cover - a passing CAS would be the defect
            raise AssertionError("a stale version must not overwrite newer data")
        current = store.read(NOTE_PATH)
        assert current == {"path": NOTE_PATH, "text": "第二版。", "version": 2}, current
        assert store.attempts() == 1, "the handler attempted exactly one write"
    finally:
        store.close()


def demo() -> int:
    """The whole chain, in child processes, plus the assertions that prove it."""
    with tempfile.TemporaryDirectory(prefix="yanxu-edit-") as directory:
        state = Path(directory)
        print(f"state directory: {state}")
        # Each scenario gets its own ledgers, so neither depends on the other's note.
        _scenario_happy(state / "happy")
        _scenario_stale_approval(state / "stale")
        _scenario_business_cas(state)

        # The ledger is the third witness: one operation, one receipt.
        settled: dict[str, int] = {}
        for name in ("happy", "stale"):
            backend = SqliteExecutionBackend(state / name / "operations.db")
            for state_name, count in backend.counts_by_state().items():
                settled[state_name] = settled.get(state_name, 0) + count
            unsettled = backend.unsettled()
            backend.close()
            assert unsettled == (), f"{name}: an operation was left unsettled"
        print(f"10. receipts → {settled}, unsettled: 0")
        assert settled.get("succeeded") == 1, settled  # only the plain approval wrote
        assert settled.get("rejected") == 1, settled  # the stale-version edit was refused

        print()
        print("OK: an approved edit survived its processes, ran exactly once, and the model saw")
        print("    its result; an approval for a version that had moved was refused instead of")
        print("    overwriting the newer text.")
        print("    (The model here is a deterministic Provider double, clearly not a real model;")
        print("     see examples/optional/real_model_edit.py for the real-provider variant.)")
    return 0


# --- CLI ----------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--state", help="directory holding the four ledgers (created if absent)")
    sub = parser.add_subparsers(dest="command")

    start = sub.add_parser("start", help="start the run; it suspends waiting for approval")
    start.add_argument("--task", required=True, help="the new note text the model should write")
    start.add_argument("--run", default=RUN_ID, help="run id to use")

    status = sub.add_parser("status", help="print the persisted state of a run")
    status.add_argument("--run", required=True, help="run id")

    approve = sub.add_parser("approve", help="record the operator's decision (trusted local entry)")
    approve.add_argument("--approval", required=True, help="approval binding key")
    approve.add_argument("--actor", default="local-operator", help="who decided")

    other = sub.add_parser("edit-elsewhere", help="change the note outside the run (a second writer)")
    other.add_argument("--text", required=True, help="the other writer's new text")

    resume = sub.add_parser("resume", help="continue a suspended run under a new attempt")
    resume.add_argument("--run", required=True, help="run id")
    resume.add_argument("--attempt", default="a2", help="attempt id for this continuation")

    args = parser.parse_args(argv)

    if args.command is None:
        return demo()

    if not args.state:
        parser.error("--state DIR is required for the subcommands")
    state = Path(args.state).expanduser()

    if args.command == "start":
        result = asyncio.run(_start(state, args.task, args.run))
    elif args.command == "status":
        result = asyncio.run(_status(state, args.run))
    elif args.command == "approve":
        result = asyncio.run(_approve(state, args.approval, args.actor))
    elif args.command == "edit-elsewhere":
        result = asyncio.run(_someone_else_edits(state, args.text))
    else:
        try:
            result = asyncio.run(_resume(state, args.run, args.attempt))
        except Exception as exc:  # noqa: BLE001 - a refused resume is a result here
            # The demo asserts on this shape: refusing to resume a finished run is
            # the correct answer, and it is reported as data rather than a traceback.
            notes = NoteStore(state / "notes.db")
            try:
                result = {
                    "refused": True,
                    "error": f"{type(exc).__name__}: {exc}",
                    "note": notes.read(NOTE_PATH),
                }
            finally:
                notes.close()
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
