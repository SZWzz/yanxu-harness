"""Q2: consuming a durable inbox inside the same Core, atomically.

The four windows the plan names (I-03/I-04) are what these tests are about:

* **accepted, not claimed** — the input is in the control store and the run has not read it;
* **read, not committed** — the reducer integrated it and the transaction that would mark it
  applies has not committed: the input must still be claimable and the conversation must not
  contain it;
* **applied, not dispatched** — the transaction committed, so a retry must not append it twice;
* **empty read, then terminal** — an input accepted while the run is finishing must not be dropped
  by the finish, and the finish must not be reached by an input arriving in the last window.

Everything runs against a real ``SqliteControlStore`` (which is also the run store, so the commit
really is one transaction), a real scripted model and a real tool whose effects are counted in a
file.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from durable_edit import (
    DeploymentPaths,
    ScriptedStream,
    final_message,
    run_context,
    tool_call_message,
    write_definition,
)

from yanxu_harness.core.execution import ExecutionState, InputPoll, state_from_record
from yanxu_harness.errors import ConfigurationError
from yanxu_harness.runtime import DefaultHarness, RunRequest, RunIdentity
from yanxu_harness.runtime.context import Principal, RunContext
from yanxu_harness.storage import (
    SqliteApprovalStore,
    SqliteExecutionBackend,
    SqlitePersistence,
)
from yanxu_harness.tasks import (
    AdmissionOutcome,
    InputAdmission,
    InputContent,
    InputKind,
    InputState,
    InboxMoved,
    SqliteControlStore,
    TaskAdmission,
    TaskStatus,
)
from yanxu_harness.tasks.inputs import PersistentInputReader
from yanxu_harness.tools import (
    ConservativePolicy,
    GatewayBindings,
    ToolGateway,
    ToolRegistry,
    core_tool,
)

NAMESPACE = "project-a"
SESSION = "s1"
TASK = "t1"
RUN = "r1"


def run(coro: Any, *, timeout: float = 60.0) -> Any:
    async def guarded() -> Any:
        return await asyncio.wait_for(coro, timeout=timeout)

    return asyncio.run(guarded())


def _identity(attempt: str = "a1", run_id: str = RUN) -> RunIdentity:
    return RunIdentity(
        namespace=NAMESPACE, session_id=SESSION, run_id=run_id, attempt_id=attempt
    )


def _context(attempt: str = "a1", run_id: str = RUN) -> RunContext:
    return RunContext(identity=_identity(attempt, run_id), principal=Principal(subject="host"))


class Deployment:
    """A control-store-backed deployment: one SQLite file for the run and the inbox."""

    def __init__(
        self,
        paths: DeploymentPaths,
        stream: Any,
        *,
        auto_write: bool = True,
        clock: Any = None,
        lease_ttl_seconds: float = 300.0,
    ) -> None:
        self.paths = paths
        self.stream = stream
        store = SqliteControlStore(paths.runs)
        definition = write_definition(paths.counter)
        gateway = ToolGateway(
            registry=ToolRegistry([definition]),
            policy=ConservativePolicy(auto_write=auto_write),
            approvals=SqliteApprovalStore(paths.approvals),
            backend=SqliteExecutionBackend(paths.operations),
        )
        bindings = GatewayBindings(gateway=gateway, context=lambda: run_context("a1"))
        self.store = store
        self.gateway = gateway
        self.harness = DefaultHarness(
            stream_fn=stream,
            tools=[core_tool(definition, bindings)],
            persistence=store,
            control=store,
            task_id=TASK,
            system_prompt="be brief",
            clock=clock if clock is not None else time.time,
            lease_ttl_seconds=lease_ttl_seconds,
        )

    def close(self) -> None:
        self.store.close()


def _deploy(tmp_path: Path, *script: Any, initial: str = "do the thing") -> Deployment:
    paths = DeploymentPaths.under(tmp_path / "state")
    deployment = Deployment(paths, ScriptedStream(*script))
    run(
        deployment.store.admit_task(
            TaskAdmission(
                namespace=NAMESPACE,
                session_id=SESSION,
                task_id=TASK,
                run_id=RUN,
                initial_input=InputContent(kind=InputKind.INITIAL, text=initial),
            )
        )
    )
    return deployment


def _admit(deployment: Deployment, input_id: str, kind: InputKind, text: str) -> Any:
    return run(
        deployment.store.admit_input(
            InputAdmission(
                namespace=NAMESPACE,
                session_id=SESSION,
                task_id=TASK,
                input_id=input_id,
                content=InputContent(kind=kind, text=text),
            )
        )
    )


def _inputs(deployment: Deployment) -> dict[str, Any]:
    return {record.input_id: record for record in run(deployment.store.read_inputs(TASK))}


def _texts(messages: Any) -> list[str]:
    """The user-visible text of a transcript, from message objects or stored records."""
    out: list[str] = []
    for message in messages or ():
        role = getattr(message, "role", None)
        content = getattr(message, "content", None)
        if role is None and isinstance(message, dict):
            role = message.get("role")
            content = message.get("content")
        if role == "user":
            out.append(str(content))
    return out


def _stored_record(deployment: Deployment, run_id: str = RUN) -> Any:
    loaded = run(deployment.store.load_run(run_id))
    return None if loaded is None else loaded.state


# -- accepted, not claimed ----------------------------------------------------------


def test_the_initial_input_is_confirmed_by_the_transaction_that_creates_the_run(
    tmp_path: Path,
) -> None:
    deployment = _deploy(tmp_path, final_message("done"))
    try:
        outcome = run(deployment.harness.start(RunRequest(prompt="do the thing"), _context()))
        assert outcome.status.value == "completed"
        initial = _inputs(deployment)["initial"]
        assert initial.state is InputState.APPLIED
        assert initial.applied_run_revision == 1, (
            "the opening instruction is applied by the run's first transaction, so 'the run "
            "exists' and 'its initial input was applied' cannot be separated by a crash"
        )
    finally:
        deployment.close()


def test_an_input_that_arrives_while_the_run_decides_to_stop_continues_it(tmp_path: Path) -> None:
    deployment = _deploy(tmp_path, final_message("first"), final_message("second"))
    original = deployment.store.commit_run_transition_with_inputs
    injected: list[bool] = []

    async def hooked(run_id: str, state: Any, events: Any = (), **kwargs: Any) -> Any:
        # The input lands in the last window: after the run's own check and before the terminal
        # transaction commits. The transaction must refuse to finish over it.
        if kwargs.get("require_inbox_revision") is not None and not injected:
            injected.append(True)
            await deployment.store.admit_input(
                InputAdmission(
                    namespace=NAMESPACE,
                    session_id=SESSION,
                    task_id=TASK,
                    input_id="late",
                    content=InputContent(kind=InputKind.STEERING, text="one more thing"),
                )
            )
        return await original(run_id, state, events, **kwargs)

    deployment.store.commit_run_transition_with_inputs = hooked  # type: ignore[assignment]
    try:
        outcome = run(deployment.harness.start(RunRequest(prompt="do the thing"), _context()))
        assert outcome.status.value == "completed"
        assert injected, "the input really did arrive inside the terminal window"
        assert deployment.stream.calls >= 2, (
            "the run re-entered the polling boundary instead of finishing over the input"
        )
        late = _inputs(deployment)["late"]
        assert late.state is InputState.APPLIED
        record = _stored_record(deployment)
        assert _texts(record.get("messages")).count("one more thing") == 1
    finally:
        deployment.close()


def test_a_control_input_is_never_read_as_conversation(tmp_path: Path) -> None:
    deployment = _deploy(tmp_path, final_message("done"))
    try:
        _admit(deployment, "cancel-1", InputKind.CANCEL_REQUEST, "please stop")
        outcome = run(deployment.harness.start(RunRequest(prompt="do the thing"), _context()))
        assert outcome.status.value == "completed", (
            "a cancel request is a control entry the boundaries own; the reader must not hand it "
            "to the model as text, and reading it does not cancel anything by itself"
        )
        record = _stored_record(deployment)
        assert "please stop" not in _texts(record.get("messages"))
        assert _inputs(deployment)["cancel-1"].state is InputState.PENDING
    finally:
        deployment.close()


# -- read, not committed ------------------------------------------------------------


def test_a_steering_input_reaches_the_conversation_exactly_once(tmp_path: Path) -> None:
    deployment = _deploy(tmp_path, final_message("done"))
    try:
        _admit(deployment, "s1", InputKind.STEERING, "keep going")
        outcome = run(deployment.harness.start(RunRequest(prompt="do the thing"), _context()))
        assert outcome.status.value == "completed"
        record = _stored_record(deployment)
        assert _texts(record.get("messages")).count("keep going") == 1
        stored = _inputs(deployment)["s1"]
        assert stored.state is InputState.APPLIED
        assert stored.applied_run_revision is not None
        snapshot = state_from_record(dict(record.get("execution") or {}))
        assert snapshot.input_watermark >= stored.sequence
        assert snapshot.input_refs == (), (
            "the confirmation set is emptied once the transaction that applied it committed, so "
            "a recovery reading this record does not try to apply it again"
        )
    finally:
        deployment.close()


def test_the_input_is_confirmed_in_the_transaction_that_writes_the_state(tmp_path: Path) -> None:
    """The applied mark and the state that carries the input become visible together."""
    deployment = _deploy(tmp_path, final_message("done"))
    seen: list[tuple[int, str]] = []
    original = deployment.store.commit_run_transition_with_inputs

    async def watched(run_id: str, state: Any, events: Any = (), **kwargs: Any) -> Any:
        applied = list(kwargs.get("applied_inputs") or ())
        version, written = await original(run_id, state, events, **kwargs)
        if applied:
            # Inside the same call: the run is at the new version *and* the inputs it named are
            # applied at that version. A reader can never see one without the other.
            records = await deployment.store.read_inputs(TASK)
            by_id = {record.input_id: record for record in records}
            for reference in applied:
                input_id = str(
                    reference["input_id"] if isinstance(reference, dict) else reference.input_id
                )
                seen.append((version, by_id[input_id].state.value))
        return version, written

    deployment.store.commit_run_transition_with_inputs = watched  # type: ignore[assignment]
    try:
        _admit(deployment, "s1", InputKind.STEERING, "keep going")
        run(deployment.harness.start(RunRequest(prompt="do the thing"), _context()))
        assert seen, "the commit carried the input it integrated"
        assert all(state == "applied" for _version, state in seen), seen
    finally:
        deployment.close()


def test_resubmitting_the_same_input_is_a_duplicate_and_changes_nothing(tmp_path: Path) -> None:
    deployment = _deploy(tmp_path, tool_call_message("hello"), final_message("done"))
    try:
        first = _admit(deployment, "s1", InputKind.STEERING, "keep going")
        assert first.outcome is AdmissionOutcome.ACCEPTED
        # A second admission of the same id and content is a duplicate, not a second input.
        assert _admit(deployment, "s1", InputKind.STEERING, "keep going").outcome is (
            AdmissionOutcome.DUPLICATE
        )
        run(deployment.harness.start(RunRequest(prompt="do the thing"), _context()))
        record = _stored_record(deployment)
        transcript = record.get("messages") or (record.get("execution") or {}).get("messages") or ()
        assert _texts(transcript).count("keep going") == 1
        # The tool call appears once as a call and once as its result, and the effect file has
        # exactly one line: re-submitting the input re-executed nothing.
        assert json.dumps(transcript).count('"call-1"') == 2
        effects = deployment.paths.counter.read_text(encoding="utf-8").splitlines()
        assert len(effects) == 1, (
            f"a re-submitted input must not make the tool run a second time: {effects}"
        )
    finally:
        deployment.close()


def test_a_crash_before_the_commit_leaves_the_input_claimable(tmp_path: Path) -> None:
    """A real child process dies in the window between the read and the commit."""
    paths = DeploymentPaths.under(tmp_path / "state")
    store = SqliteControlStore(paths.runs)
    try:
        run(
            store.admit_task(
                TaskAdmission(
                    namespace=NAMESPACE,
                    session_id=SESSION,
                    task_id=TASK,
                    run_id=RUN,
                    initial_input=InputContent(kind=InputKind.INITIAL, text="do the thing"),
                )
            )
        )
    finally:
        store.close()

    code = (
        "import os, sys\n"
        "from pathlib import Path\n"
        "from yanxu_harness.tasks import SqliteControlStore\n"
        "from yanxu_harness.tasks.inputs import PersistentInputReader\n"
        "import test_tasks_input_flow as flow\n"
        "paths = flow.DeploymentPaths.under(Path(sys.argv[1]))\n"
        "deployment = flow.Deployment(paths, flow.ScriptedStream(flow.final_message('done')))\n"
        "original = deployment.store._commit_run_transition_with_inputs\n"
        "def dying(*args, **kwargs):\n"
        "    if kwargs.get('applied_inputs'):\n"
        "        os._exit(9)\n"
        "    return original(*args, **kwargs)\n"
        "deployment.store._commit_run_transition_with_inputs = dying\n"
        "flow.run(deployment.harness.start(flow.RunRequest(prompt='do the thing'), flow._context()))\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(Path.cwd() / "src"), str(Path.cwd() / "tests"), env.get("PYTHONPATH", "")]
    )
    completed = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path / "state")],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        check=False,
    )
    assert completed.returncode == 9, completed.stderr

    store = SqliteControlStore(paths.runs)
    try:
        records = {record.input_id: record for record in run(store.read_inputs(TASK))}
        assert records["initial"].state is InputState.PENDING, (
            "the input is still claimable: nothing was confirmed, because nothing committed"
        )
        assert run(store.load_run(RUN)) is None, (
            "and no run state exists either — the read and the commit are one fact"
        )
        # A fresh attempt integrates it exactly once.
        deployment = Deployment(
            paths,
            ScriptedStream(final_message("done")),
            lease_ttl_seconds=0.2,
            clock=lambda: time.time() + 400.0,
        )
        outcome = run(deployment.harness.start(RunRequest(prompt="do the thing"), _context()))
        assert outcome.status.value == "completed"
        assert _inputs(deployment)["initial"].state is InputState.APPLIED
        deployment.close()
    finally:
        store.close()


def test_applying_an_input_twice_is_refused_rather_than_appended(tmp_path: Path) -> None:
    deployment = _deploy(tmp_path, final_message("done"))
    try:
        _admit(deployment, "s1", InputKind.STEERING, "keep going")
        run(deployment.harness.start(RunRequest(prompt="do the thing"), _context()))
        loaded = run(deployment.store.load_run(RUN))
        assert loaded is not None
        with pytest.raises(Exception) as error:
            run(
                deployment.store.commit_run_transition_with_inputs(
                    RUN,
                    {"phase": "collecting"},
                    expected_version=loaded.version,
                    applied_inputs=[{"task_id": TASK, "input_id": "s1", "sequence": 1}],
                )
            )
        assert "already applied" in str(error.value)
    finally:
        deployment.close()


# -- configuration and the reader ---------------------------------------------------


def test_two_input_sources_for_one_channel_are_refused(tmp_path: Path) -> None:
    paths = DeploymentPaths.under(tmp_path / "state")
    store = SqliteControlStore(paths.runs)
    try:
        run(
            store.admit_task(
                TaskAdmission(
                    namespace=NAMESPACE,
                    session_id=SESSION,
                    task_id=TASK,
                    run_id=RUN,
                    initial_input=InputContent(kind=InputKind.INITIAL, text="do the thing"),
                )
            )
        )
        harness = DefaultHarness(
            stream_fn=ScriptedStream(final_message("done")),
            persistence=store,
            control=store,
            task_id=TASK,
            get_steering_messages=lambda: [],
        )
        with pytest.raises(ConfigurationError):
            run(harness.start(RunRequest(prompt="do the thing"), _context()))
    finally:
        store.close()


def test_a_control_store_without_a_task_is_refused(tmp_path: Path) -> None:
    paths = DeploymentPaths.under(tmp_path / "state")
    store = SqliteControlStore(paths.runs)
    try:
        with pytest.raises(ConfigurationError):
            DefaultHarness(stream_fn=ScriptedStream(final_message("x")), persistence=store, control=store)
    finally:
        store.close()


def test_the_reader_hands_over_candidates_without_confirming_them(tmp_path: Path) -> None:
    deployment = _deploy(tmp_path, final_message("done"))
    try:
        _admit(deployment, "s1", InputKind.STEERING, "keep going")
        reader = PersistentInputReader(deployment.store, TASK)
        poll = run(reader())
        assert isinstance(poll, InputPoll)
        assert [reference["input_id"] for reference in poll.input_refs] == ["s1"]
        assert poll.inbox_revision == 2
        # Reading did not confirm anything: the input is still pending and the run has no record.
        assert _inputs(deployment)["s1"].state is InputState.PENDING
    finally:
        deployment.close()


def test_the_inbox_revision_guard_refuses_a_finish_over_a_new_input(tmp_path: Path) -> None:
    deployment = _deploy(tmp_path, final_message("done"))
    try:
        _admit(deployment, "s1", InputKind.STEERING, "keep going")
        run(deployment.harness.start(RunRequest(prompt="do the thing"), _context()))
        # A commit that claims the revision it observed before the input existed is refused.
        loaded = run(deployment.store.load_run(RUN))
        assert loaded is not None
        with pytest.raises(InboxMoved):
            run(
                deployment.store.commit_run_transition_with_inputs(
                    RUN,
                    {"phase": "collecting"},
                    expected_version=loaded.version,
                    require_inbox_revision=1,
                    task_id=TASK,
                )
            )
    finally:
        deployment.close()
