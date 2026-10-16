"""F01–F10: the durable commit boundaries of the real ``DefaultHarness`` path.

Every claim here is about the *wired* harness, not about the core commit port in
isolation: the deployment is the real ``DefaultHarness`` over ``SqlitePersistence``
with tools that go through ``core_tool -> ToolGateway`` and a real execution ledger.
Where the claim is about a process that died, the child is started through
``tests/child_process.py`` and ``tests/fault_windows.py``; the parent only ever reads
files and the three SQLite ledgers, never the child's memory.

The contract being verified (plan §3), row by row:

* before the model request: the request phase, the context and the logical budget;
* after the full model response, before the tool dispatch: the assistant message, its
  complete tool-call declarations, the phase and the turn count;
* after a settled batch, before the next request: the real results, in message order,
  with the batch's outcome or a reference that can be checked;
* before a durable pause: one consistent execution/batch/interaction/message state;
* before a final verdict: the terminal status, its reason and its single event.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest
from child_process import child_json, run_child
from durable_edit import (
    TOOL_NAME,
    DeploymentPaths,
    ScriptedStream,
    build,
    final_message,
    run_context,
    tool_call_message,
    write_calls,
    write_definition,
)

from yanxu_harness.core.execution import (
    Phase,
    pending_work_of,
    state_from_record,
)
from yanxu_harness.core.messages import content_text
from yanxu_harness.core.outcome import CallOutcome, LoopStop, RunStatus
from yanxu_harness.core.types import AssistantMessage, StopReason, TextContent, ToolCall
from yanxu_harness.errors import ContractError, VersionRefused
from yanxu_harness.runtime import DefaultHarness, RunRequest
from yanxu_harness.runtime.batch import RunBatch
from yanxu_harness.runtime.harness import RunRecord
from yanxu_harness.storage import (
    SqliteApprovalStore,
    SqliteExecutionBackend,
    SqlitePersistence,
)
from yanxu_harness.storage.ports import StorageConflict
from yanxu_harness.tools import (
    ApprovalStatus,
    ConservativePolicy,
    GatewayBindings,
    ToolDefinition,
    ToolEffect,
    ToolGateway,
    ToolRegistry,
    core_tool,
)

RUN_ID = "r1"
#: Past the default lease TTL, so a new worker can take over from a dead one.
LEASE_PAST = 400.0

RUN_ID = "r1"


def run(coro):
    async def guard():
        return await asyncio.wait_for(coro, timeout=30)

    return asyncio.run(guard())


def read_record(path: Path, run_id: str = RUN_ID) -> RunRecord:
    """Read the run through a *separate* connection, the way a new process would."""
    store = SqlitePersistence(path)
    try:
        stored = run(store.load_run(run_id))
    finally:
        store.close()
    assert stored is not None, "the run must be readable from another connection"
    return RunRecord.from_record(stored.state)


def read_events(path: Path) -> list[str]:
    store = SqlitePersistence(path)
    try:
        return [event.kind for event in run(store.read_events())]
    finally:
        store.close()


def ledger_operations(path: Path) -> tuple[dict, ...]:
    backend = SqliteExecutionBackend(path)
    try:
        return tuple(dict(item) for item in backend.all_operations())
    finally:
        backend.close()


def declared_calls(record: RunRecord) -> list[str]:
    """The tool-call ids the committed transcript's last assistant message declares."""
    for message in reversed(record.messages):
        if message.get("role") != "assistant":
            continue
        return [
            str(block.get("id"))
            for block in message.get("content") or ()
            if isinstance(block, dict) and block.get("type") == "toolCall"
        ]
    return []


def roles(record: RunRecord) -> list[str]:
    return [str(message.get("role")) for message in record.messages]


def tool_call_ids(record: RunRecord) -> list[str]:
    return [
        str(message.get("tool_call_id"))
        for message in record.messages
        if message.get("role") == "toolResult"
    ]


def calling(call_id: str, text: str = "hello") -> AssistantMessage:
    """One assistant turn declaring one write call, with a caller-chosen call id."""
    return AssistantMessage(
        content=[ToolCall(id=call_id, name=TOOL_NAME, arguments={"path": "a.md", "text": text})],
        stop_reason=StopReason.TOOL_USE,
    )


def approve(paths: DeploymentPaths, binding_key: str) -> None:
    """Write the operator's decision through the trusted approval ledger."""

    async def decide() -> None:
        store = SqliteApprovalStore(paths.approvals)
        try:
            await store.decide(binding_key, ApprovalStatus.APPROVED, decided_by="operator")
        finally:
            store.close()

    run(decide())


def rebuild(
    paths: DeploymentPaths,
    stream: object | None = None,
    *,
    attempt: str = "a2",
    auto_write: bool = True,
) -> DefaultHarness:
    """A fresh deployment over the same stores, whose clock is past a dead lease.

    Taking over a run whose driver died is exactly what the future clock is for: the
    lease fence is real, and this worker simply arrives after it expired. No production
    code was changed to make the fence easier to pass.
    """
    harness, _gateway = build(
        paths,
        stream if stream is not None else ScriptedStream(final_message("unused")),
        attempt=attempt,
        auto_write=auto_write,
        clock=lambda: time.time() + LEASE_PAST,
    )
    return harness


# --- F01: the declared call is durable before the handler runs -------------------------


def test_f01_a_dying_handler_finds_its_call_declared_and_its_outcome_unclaimed(
    tmp_path: Path,
) -> None:
    """The window the whole fix is about, on the real harness path.

    The child writes its effect and dies with ``os._exit`` *inside* the handler. The
    parent then reads the run store, the operations ledger and the effect log. What the
    record may say is bounded: the response is committed and the call is declared, and
    nothing may claim the call was settled or that it may be blindly replayed.
    """
    directory = tmp_path / "state"
    result = run_child(
        ["harness-effect-then-die", str(directory), "", ""], timeout=60
    )
    assert result.returncode == 9, (result.returncode, result.stderr[-2000:])

    paths = DeploymentPaths.under(directory)
    assert write_calls(paths.counter) == 1, "the external effect really happened"

    record = read_record(paths.runs)
    assert record.status is RunStatus.RUNNING
    assert record.execution, "the execution snapshot must be in the run store, not only in memory"
    snapshot = state_from_record(record.execution)
    assert snapshot.phase is Phase.RUNNING_BATCH
    assert snapshot.turns_used == 1
    assert snapshot.last_assistant is not None
    assert declared_calls(record) == ["call-1"]
    assert [block.id for block in snapshot.last_assistant.content if block.type == "toolCall"] == [
        "call-1"
    ]

    work = pending_work_of(snapshot)
    assert work["batch_outcome_unknown"] is True
    assert [item["call_id"] for item in work["declared"]] == ["call-1"]
    assert work["replayable"] == [], "an in-flight call is not proven un-run"
    assert work["blocking"] == [], "nothing is waiting for a decision here"
    assert record.batch is None, "no batch outcome was recorded for an unfinished dispatch"
    assert tool_call_ids(record) == [], "no result may be claimed for the call"

    operations = ledger_operations(paths.operations)
    assert [item["state"] for item in operations] == ["running"], (
        "the gateway recorded the intent; it did not settle it, and the run record must "
        "not pretend otherwise"
    )


# --- F02: a failed boundary commit stops the dispatch ----------------------------------


class FailingBoundaryPersistence(SqlitePersistence):
    """A real SQLite store whose tool-boundary commit fails exactly once.

    The failure is injected *at the port*, not by replacing the handler: the handshake
    line written just before the injected failure records that the driver reached the
    boundary port, and at that moment the test checks the effect file is still absent —
    which is what makes "the handler never ran" evidence rather than an assumption.
    """

    def __init__(self, path: Path, *, handshake: Path, effect: Path) -> None:
        super().__init__(path)
        self.handshake = handshake
        self.effect = effect
        self.reached: list[str] = []

    async def commit_transition(
        self,
        run_id: str,
        state,
        *,
        expected_version,
        lease=None,
        events=(),
    ):
        phase = (dict(state).get("execution") or {}).get("phase")
        if phase == Phase.RUNNING_BATCH.value and not self.reached:
            self.reached.append(phase)
            assert not self.effect.exists(), (
                "the handler ran before its dispatch boundary was committed"
            )
            with self.handshake.open("a", encoding="utf-8") as handle:
                handle.write(f"port:{phase}\n")
                handle.flush()
                os.fsync(handle.fileno())
            raise StorageConflict(run_id, "injected failure at the tool boundary")
        return await super().commit_transition(
            run_id,
            state,
            expected_version=expected_version,
            lease=lease,
            events=events,
        )


def test_f02_a_failed_boundary_commit_dispatches_nothing(tmp_path: Path) -> None:
    """The commit is the gate: if it did not commit, the tool must not be called."""
    directory = tmp_path / "state"
    paths = DeploymentPaths.under(directory)
    handshake = directory / "boundary-handshake.log"
    stream = ScriptedStream(tool_call_message("hello"), final_message("done"))
    harness, _gateway = build(paths, stream, attempt="a1", auto_write=True)
    failing = FailingBoundaryPersistence(
        paths.runs, handshake=handshake, effect=paths.counter
    )
    harness._persistence = failing  # noqa: SLF001 - the injected store is the point
    try:
        with pytest.raises(StorageConflict) as excinfo:
            run(harness.start(RunRequest(prompt="write it"), run_context("a1")))
    finally:
        failing.close()

    # The handshake proves the port was reached and the handler was not entered there.
    assert handshake.read_text(encoding="utf-8") == f"port:{Phase.RUNNING_BATCH.value}\n"
    assert failing.reached == [Phase.RUNNING_BATCH.value], "the boundary really was attempted"
    assert excinfo.value.code == "storage_conflict"
    assert "tool boundary" in str(excinfo.value)
    assert stream.calls == 1, "no further model request may be made after a failed commit"
    assert write_calls(paths.counter) == 0, "the handler must not have run"
    assert not paths.counter.exists()
    assert ledger_operations(paths.operations) == (), "no gateway operation may have started"

    # The run was accepted and is readable; it sits where the last commit landed, which
    # is the request, not the dispatch: the model's response never became authoritative.
    record = read_record(paths.runs)
    assert record.status is RunStatus.RUNNING
    snapshot = state_from_record(record.execution)
    assert snapshot.phase is Phase.REQUESTING_MODEL
    assert snapshot.turns_used == 1
    assert snapshot.last_assistant is None
    assert pending_work_of(snapshot)["batch_outcome_unknown"] is False
    assert "call-1" not in declared_calls(record)
    assert read_events(paths.runs) == ["run_started"]

    # How this window is handled: recovery names the request window and refuses to
    # pretend there is anything to replay. No response was committed, no call was
    # declared, and the batch-less suspension is not automatically resumable.
    recovered = run(rebuild(paths).resume(RUN_ID, {}, run_context("a2")))
    assert recovered.status is RunStatus.SUSPENDED
    detail = str((recovered.pending_interaction or {}).get("detail"))
    assert "model request" in detail
    assert "not resume" in detail or "no recorded batch" in detail
    assert write_calls(paths.counter) == 0
    with pytest.raises(ContractError, match="no recorded tool batch"):
        run(rebuild(paths, attempt="a3").resume(RUN_ID, {}, run_context("a3")))


# --- F03: the request phase is durable before the provider is called -------------------


class ReadingStream:
    """Reads the run store from its *own* connection as the request starts.

    Nothing about the reading harness is shared with the one under test: a separate
    ``SqlitePersistence`` is a separate connection, which is what makes "the request
    phase was committed before the provider saw the request" a fact about the store
    rather than about a dict in memory.
    """

    def __init__(self, runs: Path, message: AssistantMessage) -> None:
        self.runs = runs
        self.message = message
        self.calls = 0
        self.observed: RunRecord | None = None

    async def __call__(self, model, context, options=None):
        self.calls += 1
        store = SqlitePersistence(self.runs)
        try:
            stored = await store.load_run(RUN_ID)
        finally:
            store.close()
        if stored is not None:
            self.observed = RunRecord.from_record(stored.state)
        return self

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def result(self) -> AssistantMessage:
        return self.message


def test_f03_the_request_phase_is_persisted_before_the_provider_sees_the_request(
    tmp_path: Path,
) -> None:
    paths = DeploymentPaths.under(tmp_path / "state")
    stream = ReadingStream(paths.runs, final_message("done"))
    harness, _gateway = build(paths, stream, attempt="a1")

    outcome = run(harness.start(RunRequest(prompt="write it"), run_context("a1")))
    assert outcome.status is RunStatus.COMPLETED

    record = stream.observed
    assert record is not None, "the run must be readable from another connection at request time"
    assert record.status is RunStatus.RUNNING
    snapshot = state_from_record(record.execution)
    assert snapshot.phase is Phase.REQUESTING_MODEL
    assert snapshot.turns_used == 1
    assert snapshot.model_action_id == "request:1"
    work = pending_work_of(snapshot)
    assert work["model_request_outstanding"] is True
    assert work["batch_outcome_unknown"] is False
    # The context the request is built from is durable at the same moment.
    assert roles(record) == ["system", "user"]


# --- F04: the boundary commit succeeded, the handler died before its effect ------------


def test_f04_a_handler_that_dies_before_its_effect_leaves_the_call_declared_and_unproven(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "state"
    result = run_child(["harness-noop-then-die", str(directory), "", ""], timeout=60)
    assert result.returncode == 9, (result.returncode, result.stderr[-2000:])

    paths = DeploymentPaths.under(directory)
    assert write_calls(paths.counter) == 0 and not paths.counter.exists()

    record = read_record(paths.runs)
    snapshot = state_from_record(record.execution)
    assert snapshot.phase is Phase.RUNNING_BATCH
    work = pending_work_of(snapshot)
    assert [item["call_id"] for item in work["declared"]] == ["call-1"]
    assert work["replayable"] == [], "the snapshot never proved the call did not run"
    assert work["blocking"] == []
    assert record.batch is None

    # Only the ledger can say whether an operation reached the outside world; the run
    # record deliberately claims nothing, and the intent it left is not a receipt.
    assert [item["state"] for item in ledger_operations(paths.operations)] == ["running"]

    # Recovery of this window asks for evidence instead of replaying the call.
    recovered = run(rebuild(paths).resume(RUN_ID, {}, run_context("a2")))
    assert recovered.status is RunStatus.SUSPENDED
    assert (recovered.pending_interaction or {}).get("kind") == "reconciliation"
    assert "ledger" in str((recovered.pending_interaction or {}).get("detail"))
    assert write_calls(paths.counter) == 0, "reconciliation must not dispatch the call"
    assert [item["state"] for item in ledger_operations(paths.operations)] == ["running"]

    # The recovery suspension now carries a recorded batch that names the unproven call,
    # so another resume re-asks for evidence — it no longer refuses the record as
    # unidentifiable (the declaration partition is what makes the calls nameable), and it
    # still dispatches nothing.
    again = run(rebuild(paths, attempt="a3").resume(RUN_ID, {}, run_context("a3")))
    assert again.status is RunStatus.SUSPENDED
    assert (again.pending_interaction or {}).get("kind") == "reconciliation"
    assert write_calls(paths.counter) == 0, "an unproven call is never dispatched automatically"
    assert [item["state"] for item in ledger_operations(paths.operations)] == ["running"]


# --- F05: a settled gateway call is reused, never re-run -------------------------------


def test_f05_a_settled_call_stays_settled_and_is_not_dispatched_again(tmp_path: Path) -> None:
    directory = tmp_path / "state"
    result = run_child(["harness-settled-at-request-die", str(directory), "", ""], timeout=60)
    assert result.returncode == 9, (result.returncode, result.stderr[-2000:])

    paths = DeploymentPaths.under(directory)
    assert write_calls(paths.counter) == 1

    record = read_record(paths.runs)
    snapshot = state_from_record(record.execution)
    # The run died as the *next* request started: the settled batch is committed with
    # its real result, and the transcript the next request would have used carries it.
    assert snapshot.phase is Phase.REQUESTING_MODEL
    assert [call.call_id for call in snapshot.batch] == ["call-1"]
    assert [call.outcome for call in snapshot.batch] == [CallOutcome.SETTLED]
    assert snapshot.batch[0].result is not None
    assert tool_call_ids(record) == ["call-1"]
    work = pending_work_of(snapshot)
    assert work["blocking"] == [] and work["replayable"] == []
    assert record.batch is None, "no pause happened, so no batch record was written"

    operations = ledger_operations(paths.operations)
    assert [item["state"] for item in operations] == ["succeeded"]
    assert len(operations) == 1, "one operation, one ledger: no second bookkeeping was added"

    recovered = run(rebuild(paths).resume(RUN_ID, {}, run_context("a2")))
    assert recovered.status is RunStatus.SUSPENDED
    assert write_calls(paths.counter) == 1, "a confirmed call is never run twice"
    assert [item["state"] for item in ledger_operations(paths.operations)] == ["succeeded"]


# --- F06: the durable pause is consistent even when the driver dies right after it -----


def test_f06_a_pause_committed_before_the_driver_finishes_is_already_consistent(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "state"
    result = run_child(["approval-pause-die", str(directory), "", ""], timeout=60)
    assert result.returncode == 9, (result.returncode, result.stderr[-2000:])

    paths = DeploymentPaths.under(directory)
    assert write_calls(paths.counter) == 0, "nothing may run before the decision"

    record = read_record(paths.runs)
    assert record.status is RunStatus.SUSPENDED
    snapshot = state_from_record(record.execution)
    assert snapshot.phase is Phase.SUSPENDED
    batch = RunBatch.from_record(record.batch)
    assert [call.outcome for call in batch.calls] == [CallOutcome.AWAITING]
    interaction = record.pending_interaction
    assert interaction is not None and interaction["kind"] == "approval"
    assert interaction["approval_binding_key"] == batch.interactions[0].approval_binding_key
    # The two facts about the pause agree, in the transaction that wrote them together.
    assert {call.call_id for call in batch.blocking()} == {
        item["call_id"] for item in pending_work_of(snapshot)["blocking"]
    }
    assert tool_call_ids(record) == ["call-1"], "the placeholder result names the waiting call"
    kinds = read_events(paths.runs)
    assert kinds.count("run_suspended") == 1 and kinds.count("approval_requested") == 1
    assert kinds.count("run_finished") == 0

    # A new process continues under the existing approval contract. The dead worker's
    # lease must expire first, which is a bounded wait, not a production change.
    time.sleep(1.2)
    approve(paths, interaction["approval_binding_key"])
    resumed = child_json(run_child(["resume-approved", str(directory), "", ""]))
    assert resumed["status"] == "completed"
    assert resumed["writes"] == 1, "the approved call runs exactly once, after the decision"

    finished = read_record(paths.runs)
    assert finished.status is RunStatus.COMPLETED
    assert tool_call_ids(finished) == ["call-1"], "one result per call, no duplicate history"
    assert roles(finished).count("system") == 1 and roles(finished).count("user") == 1
    assert read_events(paths.runs).count("run_finished") == 1


# --- F07: transcript economy, repeated pauses and the logical budget -------------------


def test_f07_a_normal_multi_turn_run_writes_each_message_once(tmp_path: Path) -> None:
    paths = DeploymentPaths.under(tmp_path / "state")
    stream = ScriptedStream(calling("c1", "one"), calling("c2", "two"), final_message("done"))
    harness, _gateway = build(paths, stream, attempt="a1", auto_write=True)

    outcome = run(harness.start(RunRequest(prompt="do both"), run_context("a1")))
    assert outcome.status is RunStatus.COMPLETED
    assert outcome.turns_used == 3

    record = read_record(paths.runs)
    assert roles(record) == [
        "system",
        "user",
        "assistant",
        "toolResult",
        "assistant",
        "toolResult",
        "assistant",
    ]
    assert tool_call_ids(record) == ["c1", "c2"]
    assert roles(record).count("system") == 1 and roles(record).count("user") == 1
    assert write_calls(paths.counter) == 2
    assert read_events(paths.runs).count("run_finished") == 1


def test_f07_a_resume_replaces_the_placeholder_instead_of_appending_a_second_result(
    tmp_path: Path,
) -> None:
    paths = DeploymentPaths.under(tmp_path / "state")
    first, _gateway = build(paths, ScriptedStream(calling("call-1", "hello")), attempt="a1")
    paused = run(first.start(RunRequest(prompt="write it"), run_context("a1")))
    assert paused.status is RunStatus.SUSPENDED

    approve(paths, paused.pending_interaction["approval_binding_key"])
    second, _gateway = build(paths, ScriptedStream(final_message("done")), attempt="a2")
    outcome = run(second.resume(RUN_ID, {}, run_context("a2")))

    assert outcome.status is RunStatus.COMPLETED
    record = read_record(paths.runs)
    assert roles(record) == ["system", "user", "assistant", "toolResult", "assistant"]
    assert tool_call_ids(record) == ["call-1"], "the paused placeholder is replaced, not doubled"
    assert write_calls(paths.counter) == 1


def test_f07_a_run_may_pause_more_than_once_and_finishes_once(tmp_path: Path) -> None:
    paths = DeploymentPaths.under(tmp_path / "state")
    first, _gateway = build(
        paths,
        ScriptedStream(calling("c1", "one"), calling("c2", "two"), final_message("done")),
        attempt="a1",
    )
    paused = run(first.start(RunRequest(prompt="do both"), run_context("a1")))
    assert paused.status is RunStatus.SUSPENDED
    assert write_calls(paths.counter) == 0
    approve(paths, paused.pending_interaction["approval_binding_key"])

    second, _gateway = build(
        paths, ScriptedStream(calling("c2", "two"), final_message("done")), attempt="a2"
    )
    paused_again = run(second.resume(RUN_ID, {}, run_context("a2")))
    assert paused_again.status is RunStatus.SUSPENDED, "a second pause is a new pause"
    assert paused_again.pending_interaction["call_id"] == "c2"
    assert write_calls(paths.counter) == 1, "the first call settled once; the second still waits"
    approve(paths, paused_again.pending_interaction["approval_binding_key"])

    third, _gateway = build(paths, ScriptedStream(final_message("done")), attempt="a3")
    finished = run(third.resume(RUN_ID, {}, run_context("a3")))

    assert finished.status is RunStatus.COMPLETED
    record = read_record(paths.runs)
    assert roles(record) == [
        "system",
        "user",
        "assistant",
        "toolResult",
        "assistant",
        "toolResult",
        "assistant",
    ]
    assert tool_call_ids(record) == ["c1", "c2"]
    assert write_calls(paths.counter) == 2
    kinds = read_events(paths.runs)
    assert kinds.count("run_suspended") == 2
    assert kinds.count("run_finished") == 1, "one logical run has one final verdict"


def test_f07_a_pause_does_not_refresh_the_turn_budget(tmp_path: Path) -> None:
    paths = DeploymentPaths.under(tmp_path / "state")
    first, _gateway = build(paths, ScriptedStream(calling("call-1", "hello")), attempt="a1")
    paused = run(first.start(RunRequest(prompt="write it", max_turns=1), run_context("a1")))
    assert paused.status is RunStatus.SUSPENDED and paused.turns_used == 1

    approve(paths, paused.pending_interaction["approval_binding_key"])
    second, _gateway = build(paths, ScriptedStream(final_message("never asked")), attempt="a2")
    resumed = run(second.resume(RUN_ID, {}, run_context("a2")))

    assert resumed.status is RunStatus.USAGE_LIMITED
    assert resumed.error_code == "max_turns"
    assert resumed.turns_used == 1, "the logical count survives the pause"
    record = read_record(paths.runs)
    snapshot = state_from_record(record.execution)
    assert snapshot.max_turns == 1 and snapshot.turns_used == 1
    assert tool_call_ids(record) == ["call-1"]
    assert write_calls(paths.counter) == 1, "the approved call settles; no new request is made"


class OrderingStream:
    """Records the tool results the provider is actually handed, request by request.

    The provider input is its own fact: asserting on the store's transcript alone would
    not show that the *next request* was assembled in declaration order.
    """

    def __init__(self, *messages: AssistantMessage) -> None:
        self.messages = list(messages)
        self.calls = 0
        self.provider_results: list[list[tuple[str, str]]] = []
        self._final = messages[0]

    async def __call__(self, model, context, options=None):
        self.calls += 1
        self.provider_results.append(
            [
                (str(message.tool_call_id), content_text(message.content))
                for message in context.messages
                if getattr(message, "role", None) == "toolResult"
            ]
        )
        self._final = self.messages[min(self.calls - 1, len(self.messages) - 1)]
        return self

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def result(self) -> AssistantMessage:
        return self._final


def _record_text(message: Mapping[str, Any]) -> str:
    return "".join(
        str(block.get("text") or "")
        for block in message.get("content") or ()
        if isinstance(block, dict) and block.get("type") == "text"
    )


def transcript_results(record: RunRecord) -> list[tuple[str, str]]:
    """The persisted tool results, in transcript order, as ``(call_id, text)``."""
    return [
        (str(message.get("tool_call_id")), _record_text(message))
        for message in record.messages
        if message.get("role") == "toolResult"
    ]


def assert_results_are_ordered(
    actual: Sequence[tuple[str, str]], expected: Sequence[tuple[str, str]]
) -> None:
    """Ids in the declared order, and each result's own content under its own id."""
    assert [call_id for call_id, _ in actual] == [call_id for call_id, _ in expected], actual
    for (_, text), (_, snippet) in zip(actual, expected):
        assert snippet in text, (text, snippet)


def three_writes() -> AssistantMessage:
    return AssistantMessage(
        content=[
            ToolCall(id="c1", name=TOOL_NAME, arguments={"path": "a.md", "text": "one"}),
            ToolCall(id="c2", name=TOOL_NAME, arguments={"path": "b.md", "text": "two"}),
            ToolCall(id="c3", name=TOOL_NAME, arguments={"path": "c.md", "text": "three"}),
        ],
        stop_reason=StopReason.TOOL_USE,
    )


def test_f07_results_are_assembled_in_declaration_order_when_handlers_finish_out_of_order(
    tmp_path: Path,
) -> None:
    """One response declares [c1, c2, c3]; the handlers complete [c3, c1, c2].

    Both the persisted transcript and the next *actual* provider request must carry the
    results as [c1, c2, c3], each with its own content. The completion order is forced by
    a handshake (each handler waits on its own gate), not by a sleep.
    """
    paths = DeploymentPaths.under(tmp_path / "state")
    gates = {text: asyncio.Event() for text in ("one", "two", "three")}
    started: list[str] = []
    completed: list[str] = []

    async def gated(_context, arguments):
        text = str(arguments["text"])
        started.append(text)
        await gates[text].wait()
        with paths.counter.open("a", encoding="utf-8") as handle:
            handle.write(f"{text}\n")
        completed.append(text)
        return {"content": [TextContent(text=f"wrote {text}")], "details": {"bytes": len(text)}}

    stream = OrderingStream(three_writes(), final_message("done"))
    harness, _gateway = build(paths, stream, attempt="a1", auto_write=True, handler=gated)

    async def release_out_of_order() -> None:
        deadline = time.monotonic() + 10
        while len(started) < 3 and time.monotonic() < deadline:
            await asyncio.sleep(0.001)
        for text in ("three", "one", "two"):
            gates[text].set()
            await asyncio.sleep(0.001)

    async def scenario():
        releaser = asyncio.ensure_future(release_out_of_order())
        try:
            return await asyncio.wait_for(
                harness.start(RunRequest(prompt="do three"), run_context("a1")), 20
            )
        finally:
            await releaser

    outcome = run(scenario())
    assert outcome.status is RunStatus.COMPLETED
    assert sorted(started) == ["one", "three", "two"]
    assert completed == ["three", "one", "two"], "the handlers really completed out of order"

    declared = [("c1", "wrote one"), ("c2", "wrote two"), ("c3", "wrote three")]
    assert_results_are_ordered(transcript_results(read_record(paths.runs)), declared)
    assert stream.calls == 2
    assert_results_are_ordered(stream.provider_results[-1], declared)


def mixed_calls() -> AssistantMessage:
    """Three declared calls where only the middle one needs a decision."""
    return AssistantMessage(
        content=[
            ToolCall(id="c1", name="notes.read", arguments={"path": "a.md"}),
            ToolCall(id="c2", name=TOOL_NAME, arguments={"path": "b.md", "text": "hello"}),
            ToolCall(id="c3", name="notes.read", arguments={"path": "c.md"}),
        ],
        stop_reason=StopReason.TOOL_USE,
    )


def mixed_deployment(paths: DeploymentPaths, stream: object, *, attempt: str = "a1"):
    """A read tool and a write tool, so one batch can be partly settled and partly waiting."""
    read = ToolDefinition(
        "notes.read",
        "read a note",
        effects=frozenset({ToolEffect.READ}),
        args_validator=lambda arguments: {"path": arguments["path"]},
        handler=lambda _context, arguments: {
            "content": [TextContent(text=f"read {arguments['path']}")]
        },
    )
    write = write_definition(paths.counter)
    gateway = ToolGateway(
        registry=ToolRegistry([read, write]),
        policy=ConservativePolicy(),
        approvals=SqliteApprovalStore(paths.approvals),
        backend=SqliteExecutionBackend(paths.operations),
    )
    bindings = GatewayBindings(gateway=gateway, context=lambda: run_context(attempt))
    harness = DefaultHarness(
        stream_fn=stream,
        tools=[core_tool(read, bindings), core_tool(write, bindings)],
        persistence=SqlitePersistence(paths.runs),
        system_prompt="be brief",
    )
    return harness, gateway


def test_f07_a_partly_settled_batch_is_assembled_in_declaration_order_after_a_resume(
    tmp_path: Path,
) -> None:
    """The resume reuses the settled calls and settles the waiting one, in order."""
    paths = DeploymentPaths.under(tmp_path / "state")
    first, _gateway = mixed_deployment(
        paths, ScriptedStream(mixed_calls(), final_message("done"))
    )
    paused = run(first.start(RunRequest(prompt="do three"), run_context("a1")))
    assert paused.status is RunStatus.SUSPENDED
    assert paused.pending_interaction["call_id"] == "c2"
    assert write_calls(paths.counter) == 0

    record = read_record(paths.runs)
    batch = RunBatch.from_record(record.batch)
    assert {call.call_id: call.outcome for call in batch.calls} == {
        "c1": CallOutcome.SETTLED,
        "c2": CallOutcome.AWAITING,
        "c3": CallOutcome.SETTLED,
    }

    approve(paths, paused.pending_interaction["approval_binding_key"])
    stream = OrderingStream(final_message("done"))
    second, _gateway = mixed_deployment(paths, stream, attempt="a2")
    outcome = run(second.resume(RUN_ID, {}, run_context("a2")))

    assert outcome.status is RunStatus.COMPLETED
    assert write_calls(paths.counter) == 1
    declared = [("c1", "read a.md"), ("c2", "wrote hello"), ("c3", "read c.md")]
    assert_results_are_ordered(transcript_results(read_record(paths.runs)), declared)
    assert stream.calls == 1
    assert_results_are_ordered(stream.provider_results[-1], declared)


# --- F08: lease and CAS conflicts stop the dispatch ------------------------------------


class BoundaryHookPersistence(SqlitePersistence):
    """A real store that runs one hook after a chosen phase committed.

    The hook is how a test injects a *concurrent* fact at a precise point (a host's
    cancellation) or lets real time pass (a lease expiring) without touching production
    code: the fences being exercised are the production ones.
    """

    def __init__(self, path: Path, *, phase: str, hook) -> None:
        super().__init__(path)
        self._phase = phase
        self._hook = hook
        self.fired = False

    async def commit_transition(self, run_id, state, *, expected_version, lease=None, events=()):
        result = await super().commit_transition(
            run_id, state, expected_version=expected_version, lease=lease, events=events
        )
        phase = (dict(state).get("execution") or {}).get("phase")
        if not self.fired and phase == self._phase:
            self.fired = True
            await self._hook()
        return result


def test_f08_an_expired_lease_before_the_boundary_commit_dispatches_nothing(
    tmp_path: Path,
) -> None:
    paths = DeploymentPaths.under(tmp_path / "state")
    harness, _gateway = build(
        paths,
        ScriptedStream(calling("call-1", "hello"), final_message("done")),
        attempt="a1",
        auto_write=True,
        holder="worker-a",
        lease_ttl_seconds=0.25,
    )

    async def let_the_lease_expire() -> None:
        await asyncio.sleep(0.6)

    store = BoundaryHookPersistence(
        paths.runs, phase=Phase.REQUESTING_MODEL.value, hook=let_the_lease_expire
    )
    harness._persistence = store  # noqa: SLF001 - the injected store is the point
    try:
        with pytest.raises(StorageConflict) as excinfo:
            run(harness.start(RunRequest(prompt="write it"), run_context("a1")))
    finally:
        store.close()

    assert store.fired, "the lease really had expired by the boundary commit"
    assert "expired" in str(excinfo.value)
    assert write_calls(paths.counter) == 0, "a worker that lost its lease must not dispatch"
    assert ledger_operations(paths.operations) == ()

    record = read_record(paths.runs)
    snapshot = state_from_record(record.execution)
    assert snapshot.phase is Phase.REQUESTING_MODEL
    assert record.cancel_requested is None


def test_f08_a_concurrent_cancellation_survives_and_stops_the_dispatch(tmp_path: Path) -> None:
    paths = DeploymentPaths.under(tmp_path / "state")
    harness, _gateway = build(
        paths,
        ScriptedStream(calling("call-1", "hello"), final_message("done")),
        attempt="a1",
        auto_write=True,
        holder="worker-a",
    )
    other, _gateway = build(
        paths, ScriptedStream(final_message("unused")), attempt="a1", holder="worker-b"
    )

    # Another run in the same store must be untouched by this run's conflict.
    async def seed_other_run() -> None:
        store = SqlitePersistence(paths.runs)
        try:
            neighbour = RunRecord(
                run_id="r2",
                namespace="project-b",
                session_id="s2",
                status=RunStatus.SUSPENDED,
                attempt_id="b1",
                pending_interaction={"kind": "approval", "call_id": "other"},
                updated_at=1.0,
            )
            await store.save_run("r2", neighbour.to_record(), expected_version=None)
        finally:
            store.close()

    run(seed_other_run())

    async def cancel_concurrently() -> None:
        await other.cancel(RUN_ID, "operator stopped it")

    store = BoundaryHookPersistence(
        paths.runs, phase=Phase.REQUESTING_MODEL.value, hook=cancel_concurrently
    )
    harness._persistence = store  # noqa: SLF001 - the injected store is the point
    try:
        with pytest.raises(StorageConflict):
            run(harness.start(RunRequest(prompt="write it"), run_context("a1")))
    finally:
        store.close()

    assert write_calls(paths.counter) == 0
    assert ledger_operations(paths.operations) == ()
    record = read_record(paths.runs)
    assert record.cancel_requested is not None
    assert record.cancel_requested["reason"] == "operator stopped it", "the newer fact won"
    assert state_from_record(record.execution).phase is Phase.REQUESTING_MODEL

    neighbour = read_record(paths.runs, run_id="r2")
    assert neighbour.status is RunStatus.SUSPENDED and neighbour.cancel_requested is None

    # The recorded request is honoured on the next entry: a terminal cancellation that
    # dispatches nothing, rather than progress written over the operator's decision.
    cancelled = run(rebuild(paths).resume(RUN_ID, {}, run_context("a2")))
    assert cancelled.status is RunStatus.CANCELLED
    assert cancelled.error_code == "cancelled"
    assert write_calls(paths.counter) == 0


# --- F09: notification failure, interruption, duplicate entry ---------------------------


def test_f09_a_failed_terminal_notification_leaves_one_committed_terminal_fact(
    tmp_path: Path,
) -> None:
    paths = DeploymentPaths.under(tmp_path / "state")

    def failing(envelope) -> None:
        if getattr(envelope, "kind", "") == "run_finished":
            raise RuntimeError("the consumer is down")

    harness, _gateway = build(
        paths, ScriptedStream(final_message("done")), attempt="a1", run_listener=failing
    )
    outcome = run(harness.start(RunRequest(prompt="hi"), run_context("a1")))
    assert outcome.status is RunStatus.COMPLETED
    assert "run_finished" in harness.fanout_failures

    before = read_record(paths.runs).to_record()
    assert read_events(paths.runs).count("run_finished") == 1

    # The repair path is reading the event back out, never rolling a fact back.
    published: list[str] = []

    async def collect(envelope) -> None:
        published.append(envelope.kind)

    harness._run_listener = collect  # noqa: SLF001
    replayed = run(harness.replay_run_events(RUN_ID))
    assert "run_finished" in [event.kind for event in replayed]
    assert read_record(paths.runs).to_record() == before
    assert read_events(paths.runs).count("run_finished") == 1


def test_f09_an_interrupted_run_keeps_the_committed_history_exactly_once(tmp_path: Path) -> None:
    paths = DeploymentPaths.under(tmp_path / "state")

    class Slow:
        """A provider that never answers: the run is stopped by its own runtime budget."""

        def __init__(self) -> None:
            self.calls = 0

        async def __call__(self, model, context, options=None):
            self.calls += 1
            await asyncio.sleep(5)
            return self

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def result(self):
            return final_message("late")

    stream = Slow()
    harness, _gateway = build(paths, stream, attempt="a1", auto_write=True)
    harness._max_runtime = 0.2  # noqa: SLF001 - the run's own hard bound

    outcome = run(harness.start(RunRequest(prompt="hi"), run_context("a1")))
    assert outcome.status is RunStatus.USAGE_LIMITED
    assert outcome.error_code == "max_runtime_exceeded"

    record = read_record(paths.runs)
    # One system message and one prompt: the interrupted append did not re-add the
    # context the request-phase commit already carried.
    assert roles(record) == ["system", "user"]
    snapshot = state_from_record(record.execution)
    assert snapshot.phase is Phase.REQUESTING_MODEL
    assert pending_work_of(snapshot)["model_request_outstanding"] is True
    kinds = read_events(paths.runs)
    assert kinds.count("run_finished") == 1
    assert write_calls(paths.counter) == 0


def test_f09_a_second_entry_does_not_revive_or_rewrite_a_finished_run(tmp_path: Path) -> None:
    paths = DeploymentPaths.under(tmp_path / "state")
    harness, _gateway = build(
        paths, ScriptedStream(final_message("once")), attempt="a1", run_listener=None
    )
    first = run(harness.start(RunRequest(prompt="hi"), run_context("a1")))
    assert first.status is RunStatus.COMPLETED
    before = read_record(paths.runs).to_record()

    again = run(harness.start(RunRequest(prompt="hi"), run_context("a1")))
    assert again.status is RunStatus.COMPLETED
    assert again.final_text == first.final_text
    assert read_record(paths.runs).to_record() == before
    assert read_events(paths.runs).count("run_finished") == 1


# --- F10: legacy records and unknown formats -------------------------------------------


def _pause_then_read(paths: DeploymentPaths):
    harness, _gateway = build(paths, ScriptedStream(calling("call-1", "hello")), attempt="a1")
    paused = run(harness.start(RunRequest(prompt="write it"), run_context("a1")))
    assert paused.status is RunStatus.SUSPENDED
    store = SqlitePersistence(paths.runs)
    stored = run(store.load_run(RUN_ID))
    assert stored is not None
    return paused, store, RunRecord.from_record(stored.state), stored.version


def test_f10_a_record_without_a_snapshot_is_read_on_its_batch_alone(tmp_path: Path) -> None:
    paths = DeploymentPaths.under(tmp_path / "state")
    paused, store, legacy, version = _pause_then_read(paths)
    without = legacy.to_record()
    without["execution"] = None
    try:
        run(store.save_run(RUN_ID, RunRecord.from_record(without).to_record(), expected_version=version))
    finally:
        store.close()

    approve(paths, paused.pending_interaction["approval_binding_key"])
    second, _gateway = build(paths, ScriptedStream(final_message("done")), attempt="a2")
    outcome = run(second.resume(RUN_ID, {}, run_context("a2")))

    assert outcome.status is RunStatus.COMPLETED
    record = read_record(paths.runs)
    assert record.turns_used >= 1, "a legacy record must not restart the logical turn count"
    assert tool_call_ids(record) == ["call-1"]
    assert write_calls(paths.counter) == 1
    # The resumed drive writes snapshots again, and the last one is the verdict.
    assert state_from_record(record.execution).phase is Phase.FINISHED


def test_f10_an_unknown_execution_schema_is_refused_instead_of_guessed(tmp_path: Path) -> None:
    paths = DeploymentPaths.under(tmp_path / "state")
    paused, store, legacy, version = _pause_then_read(paths)
    assert legacy.execution
    tampered = dict(legacy.execution)
    tampered["schema_version"] = 99
    damaged = legacy.to_record()
    damaged["execution"] = tampered
    try:
        run(store.save_run(RUN_ID, RunRecord.from_record(damaged).to_record(), expected_version=version))
    finally:
        store.close()

    second, _gateway = build(paths, ScriptedStream(final_message("done")), attempt="a2")
    with pytest.raises(VersionRefused):
        run(second.resume(RUN_ID, {}, run_context("a2")))
    assert write_calls(paths.counter) == 0

    # A running record whose snapshot this build cannot read is refused too: recovery
    # must not invent a reconciliation question from bytes it does not understand.
    fresh_paths = DeploymentPaths.under(tmp_path / "other")
    broken = RunRecord(
        run_id=RUN_ID,
        namespace="project-a",
        session_id="s1",
        status=RunStatus.RUNNING,
        attempt_id="a1",
        execution={"schema_version": 99, "phase": "running_batch"},
        updated_at=1.0,
    )

    async def seed() -> None:
        written = SqlitePersistence(fresh_paths.runs)
        try:
            await written.save_run(RUN_ID, broken.to_record(), expected_version=None)
        finally:
            written.close()

    run(seed())
    recovering = rebuild(fresh_paths)
    with pytest.raises(VersionRefused):
        run(recovering.resume(RUN_ID, {}, run_context("a2")))


def test_f10_a_run_record_without_a_schema_version_is_refused() -> None:
    with pytest.raises(ContractError, match="schema_version"):
        RunRecord.from_record({"run_id": "r1", "namespace": "project-a", "status": "running"})
