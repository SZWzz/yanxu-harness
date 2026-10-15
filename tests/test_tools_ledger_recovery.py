"""T1: the operation ledger's read capability and its atomic admission.

Two facts are under test, and they are the reason this capability is separate from the
execution backend:

* ``observe`` answers four *different* questions (absent / open / receipt / unavailable).
  Collapsing "the ledger says there is nothing" into "the ledger cannot answer" is how a
  recovery process talks itself into re-running an effect.
* ``admit`` decides and records in **one transaction**, so two processes racing for the
  same operation cannot both be told "you may run".
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from yanxu_harness.errors import ContractError
from yanxu_harness.runtime import Principal, RunContext, RunIdentity
from yanxu_harness.storage import SqliteExecutionBackend
from yanxu_harness.testing import RecordingExecutionBackend
from yanxu_harness.tools import (
    AdmissionDecision,
    ConservativePolicy,
    EffectState,
    InMemoryApprovalStore,
    InvocationStatus,
    OperationObservation,
    ToolDefinition,
    ToolGateway,
    ToolIntent,
    ToolInvocation,
    ToolReceipt,
    ToolRegistry,
)
from yanxu_harness.tools.execution import RecoverableLedger


def run(coro):
    return asyncio.run(coro)


def context() -> RunContext:
    return RunContext(
        identity=RunIdentity(namespace="project-a", session_id="s1", run_id="r1", attempt_id="a1"),
        principal=Principal(subject="author"),
    )


def intent(
    *,
    operation_id: str = "project-a:s1:r1:call-1",
    call_id: str = "call-1",
    args_hash: str = "sha256:args",
    model_action_id: str = "request:1",
    namespace: str = "project-a",
) -> ToolIntent:
    return ToolIntent(
        operation_id=operation_id,
        namespace=namespace,
        tool_name="notes.write",
        tool_version="1",
        args_hash=args_hash,
        session_id="s1",
        run_id="r1",
        call_id=call_id,
        model_action_id=model_action_id,
    )


# --- observe: four answers, not two ----------------------------------------------------


def test_observe_distinguishes_absent_open_receipt_and_unavailable(tmp_path: Path) -> None:
    backend = SqliteExecutionBackend(tmp_path / "operations.db")
    try:
        missing = run(backend.observe(intent()))
        assert missing.observation is OperationObservation.ABSENT
        assert missing.receipt is None
        assert missing.observation.proves_absence is True

        # An intent with no receipt: may have run, cannot be proven either way.
        run(backend.begin(intent()))
        opened = run(backend.observe(intent()))
        assert opened.observation is OperationObservation.OPEN
        assert opened.observation.may_have_run is True
        assert opened.observation.proves_absence is False
        assert opened.intent is not None and opened.intent["tool_name"] == "notes.write"

        # A receipt is its own answer, with the effect state it recorded.
        run(backend.settle(ToolReceipt(operation_id=intent().operation_id, state=EffectState.SUCCEEDED, result={"ok": True})))
        settled = run(backend.observe(intent()))
        assert settled.observation is OperationObservation.RECEIPT
        assert settled.receipt is not None and settled.receipt.state is EffectState.SUCCEEDED
        assert settled.reconciled is True

        # An unknown receipt is still uncertain, not reconciled.
        unknown_intent = intent(operation_id="project-a:s1:r1:call-2", call_id="call-2")
        run(backend.begin(unknown_intent))
        run(backend.settle(ToolReceipt(operation_id=unknown_intent.operation_id, state=EffectState.UNKNOWN, detail="no receipt")))
        unknown = run(backend.observe(unknown_intent))
        assert unknown.observation is OperationObservation.RECEIPT
        assert unknown.reconciled is False, "an unknown receipt still needs evidence"

        # A binding disagreement is not absence: it is a refusal to interpret.
        conflicted = run(backend.observe(intent(args_hash="sha256:other")))
        assert conflicted.observation is OperationObservation.UNAVAILABLE
        assert conflicted.observation.proves_absence is False
        assert "args_hash" in conflicted.detail

        # A backend without the capability cannot be asked at all; the marker is explicit.
        assert isinstance(backend, RecoverableLedger)
        assert not isinstance(RecordingExecutionBackend(), RecoverableLedger)
    finally:
        backend.close()


def test_a_legacy_row_with_no_model_action_is_not_a_conflict(tmp_path: Path) -> None:
    """A row written before the column existed is unprovable, not wrong."""
    path = tmp_path / "operations.db"
    backend = SqliteExecutionBackend(path)
    try:
        import sqlite3

        connection = sqlite3.connect(path)
        connection.execute(
            "INSERT INTO operations(operation_id, namespace, tool_name, tool_version, args_hash, "
            "call_id, session_id, run_id, state, intent_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "project-a:s1:r1:legacy",
                "project-a",
                "notes.write",
                "1",
                "sha256:args",
                "legacy",
                "s1",
                "r1",
                EffectState.RUNNING.value,
                0.0,
            ),
        )
        connection.commit()
        connection.close()

        legacy = run(
            backend.observe(
                intent(operation_id="project-a:s1:r1:legacy", call_id="legacy")
            )
        )
        assert legacy.observation is OperationObservation.OPEN, (
            "the empty model_action_id is 'this build did not record it', not a mismatch"
        )
    finally:
        backend.close()


# --- admit: one atomic decision --------------------------------------------------------


def test_admit_distinguishes_the_four_decisions(tmp_path: Path) -> None:
    backend = SqliteExecutionBackend(tmp_path / "operations.db")
    try:
        first = run(backend.admit(intent()))
        assert first.decision is AdmissionDecision.ADMITTED
        assert first.decision.may_run is True

        open_again = run(backend.admit(intent()))
        assert open_again.decision is AdmissionDecision.EXISTING_OPEN
        assert open_again.decision.may_run is False

        conflict = run(backend.admit(intent(args_hash="sha256:changed")))
        assert conflict.decision is AdmissionDecision.BINDING_CONFLICT
        assert "args_hash" in conflict.detail

        run(
            backend.settle(
                ToolReceipt(
                    operation_id=intent().operation_id, state=EffectState.SUCCEEDED, result={"ok": True}
                )
            )
        )
        existing = run(backend.admit(intent()))
        assert existing.decision is AdmissionDecision.EXISTING_RECEIPT
        assert existing.receipt is not None and existing.receipt.state is EffectState.SUCCEEDED
    finally:
        backend.close()


def test_two_connections_racing_the_same_operation_admit_exactly_once(tmp_path: Path) -> None:
    path = tmp_path / "operations.db"
    first = SqliteExecutionBackend(path)
    second = SqliteExecutionBackend(path)
    try:
        async def race():
            return await asyncio.gather(first.admit(intent()), second.admit(intent()))

        outcome = run(race())
        decisions = [item.decision for item in outcome]
        assert decisions.count(AdmissionDecision.ADMITTED) == 1, (
            "two workers must not both be told they may run the same operation"
        )
        assert decisions.count(AdmissionDecision.EXISTING_OPEN) == 1
    finally:
        first.close()
        second.close()


def test_a_different_model_action_with_the_same_call_id_is_refused(tmp_path: Path) -> None:
    """Same run, same tool-call id, different turn: not the same call."""
    backend = SqliteExecutionBackend(tmp_path / "operations.db")
    try:
        assert run(backend.admit(intent())).decision is AdmissionDecision.ADMITTED
        other_turn = run(backend.admit(intent(model_action_id="request:2")))
        assert other_turn.decision is AdmissionDecision.BINDING_CONFLICT
        assert "model_action_id" in other_turn.detail
    finally:
        backend.close()


# --- settle and promote ----------------------------------------------------------------


def test_settle_is_idempotent_and_refuses_to_overwrite(tmp_path: Path) -> None:
    backend = SqliteExecutionBackend(tmp_path / "operations.db")
    try:
        run(backend.begin(intent()))
        success = ToolReceipt(
            operation_id=intent().operation_id, state=EffectState.SUCCEEDED, result={"ok": True}
        )
        run(backend.settle(success))
        run(backend.settle(success))  # the same fact again is harmless
        assert run(backend.observe(intent())).receipt.state is EffectState.SUCCEEDED

        with pytest.raises(ContractError, match="already settled"):
            run(
                backend.settle(
                    ToolReceipt(
                        operation_id=intent().operation_id,
                        state=EffectState.UNKNOWN,
                        detail="a late callback must not erase a confirmed success",
                    )
                )
            )
        assert run(backend.observe(intent())).receipt.state is EffectState.SUCCEEDED
    finally:
        backend.close()


def test_promote_only_moves_unknown_and_keeps_its_evidence(tmp_path: Path) -> None:
    backend = SqliteExecutionBackend(tmp_path / "operations.db")
    try:
        run(backend.begin(intent()))
        run(
            backend.settle(
                ToolReceipt(operation_id=intent().operation_id, state=EffectState.UNKNOWN, detail="unproven")
            )
        )
        with pytest.raises(ContractError, match="needs evidence that names its source"):
            run(
                backend.promote(
                    ToolReceipt(operation_id=intent().operation_id, state=EffectState.SUCCEEDED, result={}),
                    evidence={},
                )
            )
        promoted = run(
            backend.promote(
                ToolReceipt(operation_id=intent().operation_id, state=EffectState.SUCCEEDED, result={"ok": True}),
                evidence={"source": "business store", "detail": "the note contains the text"},
            )
        )
        assert promoted.state is EffectState.SUCCEEDED
        observed = run(backend.observe(intent()))
        assert observed.receipt.state is EffectState.SUCCEEDED
        assert observed.receipt.details["reconciliation"]["source"] == "business store"

        with pytest.raises(ContractError, match="only an unknown operation may be promoted"):
            run(
                backend.promote(
                    ToolReceipt(operation_id=intent().operation_id, state=EffectState.FAILED),
                    evidence={"source": "late caller"},
                )
            )
    finally:
        backend.close()


# --- the gateway uses the strong contract when it exists -------------------------------


class CountingHandler:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, _context, arguments):
        self.calls += 1
        return {"wrote": arguments["path"]}


def definition(handler) -> ToolDefinition:
    return ToolDefinition(
        "notes.read",
        "read a note",
        args_validator=lambda arguments: {"path": arguments["path"]},
        handler=handler,
    )


def invocation(call_id: str = "call-1") -> ToolInvocation:
    return ToolInvocation(
        call_id=call_id,
        namespace="project-a",
        tool_name="notes.read",
        arguments={"path": "a.md"},
        session_id="s1",
        run_id="r1",
        model_action_id="request:1",
    )


def test_the_gateway_replays_an_existing_receipt_instead_of_running_twice(tmp_path: Path) -> None:
    handler = CountingHandler()
    backend = SqliteExecutionBackend(tmp_path / "operations.db")
    gateway = ToolGateway(
        registry=ToolRegistry([definition(handler)]),
        policy=ConservativePolicy(),
        approvals=InMemoryApprovalStore(),
        backend=backend,
    )
    try:
        first = run(gateway.invoke(invocation(), context()))
        assert first.status is InvocationStatus.EXECUTED
        second = run(gateway.invoke(invocation(), context()))
        assert second.status is InvocationStatus.REPLAYED
        assert handler.calls == 1, "a settled operation is replayed, never run again"
    finally:
        backend.close()


class DiesBeforeSettling:
    """The crash window: the intent is committed, the handler runs, the receipt never is."""

    def __init__(self, inner: SqliteExecutionBackend) -> None:
        self._inner = inner
        self.ownership = inner.ownership
        self.durable = inner.durable

    async def observe(self, tool_intent):
        return await self._inner.observe(tool_intent)

    async def admit(self, tool_intent):
        return await self._inner.admit(tool_intent)

    async def begin(self, tool_intent):
        return await self._inner.begin(tool_intent)

    async def execute(self, tool_intent, tool_definition, arguments, tool_context):
        return await self._inner.execute(tool_intent, tool_definition, arguments, tool_context)

    async def settle(self, receipt):
        # The process "died" here: the row stays open and unproven.
        return None


def test_the_gateway_reports_an_open_operation_as_unproven_without_running_it(
    tmp_path: Path,
) -> None:
    handler = CountingHandler()
    inner = SqliteExecutionBackend(tmp_path / "operations.db")
    gateway = ToolGateway(
        registry=ToolRegistry([definition(handler)]),
        policy=ConservativePolicy(),
        approvals=InMemoryApprovalStore(),
        backend=DiesBeforeSettling(inner),
    )
    try:
        first = run(gateway.invoke(invocation(), context()))
        assert first.status is InvocationStatus.EXECUTED
        assert handler.calls == 1

        second = run(gateway.invoke(invocation(), context()))
        assert second.status is InvocationStatus.UNKNOWN, (
            "an intent with no receipt may have run; it is reported as unproven"
        )
        assert second.receipt is not None and second.receipt.state is EffectState.UNKNOWN
        assert handler.calls == 1, "the handler must not run a second time"
    finally:
        inner.close()


def test_the_gateway_calls_begin_only_when_the_ledger_lacks_the_capability() -> None:
    handler = CountingHandler()
    backend = RecordingExecutionBackend()
    gateway = ToolGateway(
        registry=ToolRegistry([definition(handler)]),
        policy=ConservativePolicy(),
        approvals=InMemoryApprovalStore(),
        backend=backend,
    )
    first = run(gateway.invoke(invocation(), context()))
    assert first.status is InvocationStatus.EXECUTED
    second = run(gateway.invoke(invocation(), context()))
    assert second.status is InvocationStatus.REPLAYED
    assert handler.calls == 1
    # The legacy port was used: `begin` recorded the intent (twice, because the legacy
    # path re-reads each time — the id is stable either way).
    assert {item.operation_id for item in backend.intents} == {"project-a:s1:r1:call-1"}


def test_the_logical_reference_binds_the_model_action() -> None:
    base = invocation()
    same = invocation()
    other_turn = ToolInvocation(
        call_id="call-1",
        namespace="project-a",
        tool_name="notes.read",
        arguments={"path": "a.md"},
        session_id="s1",
        run_id="r1",
        model_action_id="request:2",
    )
    assert base.logical_ref() == same.logical_ref()
    assert base.logical_ref() != other_turn.logical_ref()
    assert base.logical_ref().startswith("sha256:")
