"""The gateway: order, refusal, replay and the guarantee that waiting ≠ success.

Every test here asserts something a *second* implementation would get wrong:
that visibility is checked before validation, that refusal leaves no ledger
entry, that a drifted approval re-asks instead of executing, and that an
already-settled call is replayed instead of run twice — which is the whole point
of having a gateway at all.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace

import pytest

from yanxu_harness.errors import ContractError
from yanxu_harness.runtime import Cancellation, Principal, RunContext, RunIdentity
from yanxu_harness.tools import (
    ApprovalStatus,
    ConservativePolicy,
    EffectState,
    InMemoryApprovalStore,
    InvocationStatus,
    PolicyDecision,
    PolicyKind,
    ToolDefinition,
    ToolEffect,
    ToolGateway,
    ToolInvocation,
    ToolRegistry,
)
from yanxu_harness.testing import RecordingExecutionBackend, failing_handler


def run(coro):
    return asyncio.run(coro)


def context(namespace: str = "project-a") -> RunContext:
    return RunContext(
        identity=RunIdentity(namespace=namespace, session_id="s1", run_id="r1", attempt_id="a1"),
        principal=Principal(subject="author", permissions=frozenset({"story.write"})),
    )


def validator(arguments: dict) -> dict:
    if not isinstance(arguments.get("path"), str) or not arguments["path"]:
        raise ValueError("path must be a non-empty string")
    return {"path": arguments["path"]}


class Recorder:
    """A handler that records every call, so "ran once" is provable."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, _context, arguments):
        self.calls.append(arguments)
        return {"wrote": arguments["path"]}


def read_tool(handler=None) -> ToolDefinition:
    return ToolDefinition(
        "notes.read", "Read a note", args_validator=validator, handler=handler or Recorder()
    )


def write_tool(handler=None) -> ToolDefinition:
    return ToolDefinition(
        "notes.write",
        "Write a note",
        effects=frozenset({ToolEffect.WRITE}),
        args_validator=validator,
        handler=handler or Recorder(),
        preview=lambda _c, a: {"path": a["path"], "action": "replace"},
    )


class Only:
    """A visibility policy that exposes exactly the named tools."""

    def __init__(self, *names: str) -> None:
        self.names = set(names)

    def exposed(self, definition: ToolDefinition, context) -> bool:
        return definition.name in self.names


def gateway(*, definitions, policy=None, visibility=None, backend=None, approvals=None) -> ToolGateway:
    return ToolGateway(
        registry=ToolRegistry(definitions),
        policy=policy or ConservativePolicy(auto_write=True),
        approvals=approvals or InMemoryApprovalStore(),
        backend=backend or RecordingExecutionBackend(),
        visibility=visibility,
    )


def invocation(tool_name: str = "notes.read", **overrides) -> ToolInvocation:
    values = {
        "call_id": "call-1",
        "namespace": "project-a",
        "tool_name": tool_name,
        "arguments": {"path": "a.md"},
        "run_id": "r1",
    }
    values.update(overrides)
    return ToolInvocation(**values)


# -- the happy path -----------------------------------------------------


def test_a_read_tool_runs_once_and_reports_its_receipt() -> None:
    async def scenario() -> None:
        handler = Recorder()
        backend = RecordingExecutionBackend()
        gw = gateway(definitions=[read_tool(handler)], backend=backend)
        result = await gw.invoke(invocation(), context())
        assert result.status is InvocationStatus.EXECUTED
        assert result.status.succeeded
        assert result.receipt is not None and result.receipt.state is EffectState.SUCCEEDED
        assert result.receipt.result == {"wrote": "a.md"}
        assert handler.calls == [{"path": "a.md"}]
        # The intent was recorded before execution and settled afterwards.
        assert len(backend.intents) == 1
        assert len(backend.settled) == 1
        assert result.args_hash.startswith("sha256:")

    run(scenario())


def test_execution_binding_refusal_settles_a_rejected_receipt_without_running_handler() -> None:
    from yanxu_harness.plans import ExecutionBindingRefused

    async def scenario() -> None:
        handler = Recorder()
        backend = RecordingExecutionBackend()
        gw = gateway(definitions=[read_tool(handler)], backend=backend)

        class RefusingDispatch:
            async def check_tool_dispatch(self):
                raise ExecutionBindingRefused("deployment_revoked", "deployment was revoked")

        active = replace(context(), execution_binding=RefusingDispatch())
        result = await gw.invoke(invocation(), active)

        assert result.status is InvocationStatus.DENIED
        assert result.receipt is not None and result.receipt.state is EffectState.REJECTED
        assert result.receipt.details["reason_code"] == "deployment_revoked"
        assert handler.calls == []
        assert len(backend.intents) == 1
        assert len(backend.settled) == 1
        assert backend.settled[0].state is EffectState.REJECTED

    run(scenario())


def test_the_gateway_reports_whether_the_host_or_the_harness_owns_the_ledger() -> None:
    gw = gateway(definitions=[read_tool()])
    assert gw.backend_ownership == "harness"


# -- layer order --------------------------------------------------------


def test_visibility_is_checked_before_arguments_are_validated() -> None:
    """A hidden tool must not be usable as a schema oracle."""

    async def scenario() -> None:
        gw = gateway(definitions=[write_tool()], visibility=Only("other.tool"))
        result = await gw.invoke(invocation("notes.write", arguments={}), context())
        assert result.status is InvocationStatus.DENIED
        assert "not exposed" in result.reason

    run(scenario())


def test_invalid_arguments_leave_no_ledger_entry() -> None:
    async def scenario() -> None:
        handler = Recorder()
        backend = RecordingExecutionBackend()
        gw = gateway(definitions=[read_tool(handler)], backend=backend)
        result = await gw.invoke(invocation(arguments={"path": ""}), context())
        assert result.status is InvocationStatus.INVALID_ARGS
        assert "path must be a non-empty string" in result.reason
        assert handler.calls == []
        assert backend.intents == []

    run(scenario())


def test_a_policy_denial_leaves_no_ledger_entry() -> None:
    class DenyAll:
        policy_version = "test/1"

        def decide(self, *, definition, context, exposed):
            return PolicyDecision(PolicyKind.DENY, "this deployment refuses writes", self.policy_version)

    async def scenario() -> None:
        handler = Recorder()
        backend = RecordingExecutionBackend()
        gw = gateway(definitions=[write_tool(handler)], policy=DenyAll(), backend=backend)
        result = await gw.invoke(invocation("notes.write"), context())
        assert result.status is InvocationStatus.DENIED
        assert result.reason == "this deployment refuses writes"
        assert handler.calls == [] and backend.intents == []

    run(scenario())


# -- approvals ----------------------------------------------------------


def test_a_write_waits_and_records_a_pending_approval_without_running() -> None:
    async def scenario() -> None:
        handler = Recorder()
        backend = RecordingExecutionBackend()
        approvals = InMemoryApprovalStore()
        gw = gateway(
            definitions=[write_tool(handler)],
            policy=ConservativePolicy(),
            backend=backend,
            approvals=approvals,
        )
        result = await gw.invoke(invocation("notes.write"), context())
        assert result.status is InvocationStatus.AWAITING_APPROVAL
        assert not result.status.succeeded
        assert result.receipt is None
        assert result.approval is not None and result.approval.is_open()
        assert result.preview == {"path": "a.md", "action": "replace"}
        # Nothing ran and nothing was journalled.
        assert handler.calls == [] and backend.intents == []
        assert len(approvals.open_approvals()) == 1

    run(scenario())


def test_an_approved_call_executes_exactly_once() -> None:
    async def scenario() -> None:
        handler = Recorder()
        backend = RecordingExecutionBackend()
        approvals = InMemoryApprovalStore()
        gw = gateway(
            definitions=[write_tool(handler)],
            policy=ConservativePolicy(),
            backend=backend,
            approvals=approvals,
        )
        waiting = await gw.invoke(invocation("notes.write"), context())
        await approvals.decide(waiting.approval.key, ApprovalStatus.APPROVED, "author")
        executed = await gw.invoke(invocation("notes.write"), context())
        assert executed.status is InvocationStatus.EXECUTED
        assert handler.calls == [{"path": "a.md"}]
        assert len(backend.intents) == 1
        # Re-invoking the same call replays the settled receipt instead of writing twice.
        replay = await gw.invoke(invocation("notes.write"), context())
        assert replay.status is InvocationStatus.REPLAYED
        assert handler.calls == [{"path": "a.md"}]

    run(scenario())


def test_an_approval_stops_applying_when_the_arguments_change() -> None:
    async def scenario() -> None:
        handler = Recorder()
        backend = RecordingExecutionBackend()
        approvals = InMemoryApprovalStore()
        gw = gateway(
            definitions=[write_tool(handler)],
            policy=ConservativePolicy(),
            backend=backend,
            approvals=approvals,
        )
        waiting = await gw.invoke(invocation("notes.write"), context())
        await approvals.decide(waiting.approval.key, ApprovalStatus.APPROVED, "author")
        # Same call, different arguments: the approval was for the old ones.
        changed = await gw.invoke(invocation("notes.write", arguments={"path": "b.md"}), context())
        assert changed.status is InvocationStatus.AWAITING_APPROVAL
        assert "arguments changed after approval" in changed.reason
        assert handler.calls == []
        # It is the *same call*, so the same approval key — what changed is the
        # binding. Keeping one key per call is what lets the gateway name the drift
        # instead of pretending this is a brand-new question.
        assert changed.approval.key == waiting.approval.key
        assert changed.approval.binding.args_hash != waiting.approval.binding.args_hash
        assert changed.args_hash != waiting.args_hash
        # The earlier decision is preserved as history rather than overwritten.
        assert [record.status.value for record in approvals.history] == ["approved"]
        assert approvals.history[0].binding.args_hash == waiting.approval.binding.args_hash
        # Approving the *new* binding makes the call run.
        await approvals.decide(changed.approval.key, ApprovalStatus.APPROVED, "author")
        executed = await gw.invoke(invocation("notes.write", arguments={"path": "b.md"}), context())
        assert executed.status is InvocationStatus.EXECUTED
        assert handler.calls == [{"path": "b.md"}]

    run(scenario())


def test_an_approval_stops_applying_when_the_target_version_changes() -> None:
    async def scenario() -> None:
        approvals = InMemoryApprovalStore()
        gw = gateway(
            definitions=[write_tool()],
            policy=ConservativePolicy(),
            approvals=approvals,
        )
        waiting = await gw.invoke(invocation("notes.write", target_version="v3"), context())
        await approvals.decide(waiting.approval.key, ApprovalStatus.APPROVED, "author")
        moved = await gw.invoke(invocation("notes.write", target_version="v4"), context())
        assert moved.status is InvocationStatus.AWAITING_APPROVAL

    run(scenario())


@pytest.mark.parametrize(
    ("drift", "expected_reason"),
    [
        ("preview", "preview changed after approval"),
        ("preview_removed", "preview changed after approval"),
        ("target_version", "target version changed after approval"),
        ("tool_version", "tool contract version changed after approval"),
        ("policy_version", "policy version changed after approval"),
    ],
)
def test_gateway_reasks_on_approval_binding_drift_before_running_the_handler(
    drift: str, expected_reason: str
) -> None:
    async def scenario() -> None:
        handler = Recorder()
        backend = RecordingExecutionBackend()
        approvals = InMemoryApprovalStore()
        preview_state = {"value": {"resource_ref": "a.md", "change_type": "replace"}}

        class RequireVersionedApproval:
            policy_version = "policy/1"

            def decide(self, *, definition, context, exposed):
                del definition, context, exposed
                return PolicyDecision(
                    PolicyKind.REQUIRE_APPROVAL, "approval required", self.policy_version
                )

        policy = RequireVersionedApproval()
        definition = write_tool(handler).with_overrides(
            preview=lambda _context, _arguments: preview_state["value"]
        )
        gw = gateway(
            definitions=[definition],
            policy=policy,
            backend=backend,
            approvals=approvals,
        )
        original = await gw.invoke(
            invocation("notes.write", target_version="resource-v1"), context()
        )
        assert original.status is InvocationStatus.AWAITING_APPROVAL
        await approvals.decide(original.approval.key, ApprovalStatus.APPROVED, "author")

        changed_invocation = invocation("notes.write", target_version="resource-v1")
        if drift == "preview":
            preview_state["value"] = {"resource_ref": "a.md", "change_type": "delete"}
        elif drift == "preview_removed":
            preview_state["value"] = None
        elif drift == "target_version":
            changed_invocation = invocation("notes.write", target_version="resource-v2")
        elif drift == "tool_version":
            changed_definition = definition.with_overrides(version="2")
            gw = gateway(
                definitions=[changed_definition],
                policy=policy,
                backend=backend,
                approvals=approvals,
            )
        elif drift == "policy_version":
            policy.policy_version = "policy/2"
        else:  # pragma: no cover - the parameter list is the contract
            raise AssertionError(drift)

        changed = await gw.invoke(changed_invocation, context())

        assert changed.status is InvocationStatus.AWAITING_APPROVAL
        assert expected_reason in changed.reason
        assert handler.calls == []
        assert backend.intents == []

    run(scenario())


def test_gateway_deployment_drift_refuses_a_new_call_before_running_the_handler() -> None:
    async def scenario() -> None:
        handler = Recorder()
        backend = RecordingExecutionBackend()
        deployment = {"revision": "deployment-v1"}

        def check_deployment(_facts):
            return None if deployment["revision"] == "deployment-v1" else "deployment revision changed"

        definition = write_tool(handler).with_overrides(dispatch_guard=check_deployment)
        gw = gateway(
            definitions=[definition],
            policy=ConservativePolicy(auto_write=True),
            backend=backend,
        )
        first = await gw.invoke(invocation("notes.write", call_id="call-1"), context())
        assert first.status is InvocationStatus.EXECUTED
        assert handler.calls == [{"path": "a.md"}]

        deployment["revision"] = "deployment-v2"
        refused = await gw.invoke(invocation("notes.write", call_id="call-2"), context())

        assert refused.status is InvocationStatus.DENIED
        assert "deployment revision changed" in refused.reason
        assert handler.calls == [{"path": "a.md"}]
        assert len(backend.intents) == 1

    run(scenario())


def test_an_expired_approval_does_not_apply() -> None:
    async def scenario() -> None:
        handler = Recorder()
        approvals = InMemoryApprovalStore()
        gw = ToolGateway(
            registry=ToolRegistry([write_tool(handler)]),
            policy=ConservativePolicy(),
            approvals=approvals,
            backend=RecordingExecutionBackend(),
            clock=lambda: time.time() + 3600,
        )
        waiting = await gw.invoke(invocation("notes.write"), context())
        # Approve a binding that expires within the hour, then read the clock an
        # hour ahead: the approval exists but no longer applies.
        expiring = replace(waiting.approval.binding, expires_at=time.time() + 10)
        await approvals.decide(waiting.approval.key, ApprovalStatus.APPROVED, "author")
        await approvals.supersede(expiring)
        await approvals.decide(waiting.approval.key, ApprovalStatus.APPROVED, "author")
        result = await gw.invoke(invocation("notes.write"), context())
        assert result.status is InvocationStatus.AWAITING_APPROVAL
        assert "expired" in result.reason
        assert handler.calls == []

    run(scenario())


def test_a_denied_call_stays_denied() -> None:
    async def scenario() -> None:
        handler = Recorder()
        approvals = InMemoryApprovalStore()
        gw = gateway(
            definitions=[write_tool(handler)],
            policy=ConservativePolicy(),
            approvals=approvals,
        )
        waiting = await gw.invoke(invocation("notes.write"), context())
        await approvals.decide(waiting.approval.key, ApprovalStatus.DENIED, "author", "not this one")
        denied = await gw.invoke(invocation("notes.write"), context())
        assert denied.status is InvocationStatus.DENIED
        assert denied.reason == "not this one"
        assert handler.calls == []
        # Repeating the same call does not silently re-ask the author.
        again = await gw.invoke(invocation("notes.write"), context())
        assert again.status is InvocationStatus.DENIED

    run(scenario())


def test_the_gateway_cannot_write_an_approval_itself() -> None:
    """Only the decision path records approvals; the gateway reads them."""
    methods = {name for name in dir(ToolGateway) if not name.startswith("_")}
    assert "approve" not in methods
    assert "decide" not in methods
    assert not any("approve" in name for name in methods)


# -- cancellation and namespace -----------------------------------------


def test_a_cancelled_run_never_records_an_intent() -> None:
    async def scenario() -> None:
        handler = Recorder()
        backend = RecordingExecutionBackend()
        gw = gateway(definitions=[read_tool(handler)], backend=backend)
        ctx = context()
        ctx.cancellation.cancel("author stopped")
        result = await gw.invoke(invocation(), ctx)
        assert result.status is InvocationStatus.CANCELLED
        assert handler.calls == [] and backend.intents == []

    run(scenario())


def test_a_namespace_mismatch_is_a_bug_not_a_denial() -> None:
    async def scenario() -> None:
        gw = gateway(definitions=[read_tool()])
        with pytest.raises(ContractError, match="namespace"):
            await gw.invoke(invocation(namespace="project-b"), context("project-a"))

    run(scenario())


# -- unknown ------------------------------------------------------------


def test_an_unprovable_outcome_is_reported_as_unknown_and_never_rerun() -> None:
    async def scenario() -> None:
        handler = Recorder()
        backend = RecordingExecutionBackend()
        gw = gateway(
            definitions=[write_tool(failing_handler(TimeoutError("no receipt")))],
            backend=backend,
        )
        # A read-shaped write would need approval, so use an auto-write policy.
        result = await gw.invoke(invocation("notes.write"), context())
        assert result.status is InvocationStatus.UNKNOWN
        assert result.unknown and result.needs_reconciliation
        assert not result.status.succeeded
        assert handler is not None
        # Re-invoking replays the unknown receipt rather than trying again.
        again = await gw.invoke(invocation("notes.write"), context())
        assert again.status is InvocationStatus.UNKNOWN
        assert len(backend.receipts) == 1

    run(scenario())


def test_raise_for_status_turns_refusals_into_neutral_outcomes() -> None:
    from yanxu_harness.tools.outcomes import ToolArgsInvalid, ToolCancelled, ToolRejected

    async def scenario() -> None:
        gw = gateway(definitions=[read_tool()])
        denied = await gw.invoke(invocation(arguments={}), context())
        with pytest.raises(ToolArgsInvalid):
            denied.raise_for_status()
        cancelled = await gw.invoke(invocation(), context())
        assert cancelled.status is InvocationStatus.EXECUTED
        assert cancelled.raise_for_status() is cancelled

        hidden = gateway(definitions=[read_tool()], visibility=Only("other"))
        refused = await hidden.invoke(invocation(), context())
        with pytest.raises(ToolRejected):
            refused.raise_for_status()

        ctx = context()
        ctx.cancellation.cancel("stop")
        stopped = await gateway(definitions=[read_tool()]).invoke(invocation(), ctx)
        with pytest.raises(ToolCancelled):
            stopped.raise_for_status()

    run(scenario())


# -- what a backend's receipt state means to the caller -----------------


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (EffectState.SUCCEEDED, InvocationStatus.EXECUTED),
        (EffectState.UNKNOWN, InvocationStatus.UNKNOWN),
        (EffectState.DEFERRED, InvocationStatus.DEFERRED),
        (EffectState.REJECTED, InvocationStatus.DENIED),
        (EffectState.FAILED, InvocationStatus.FAILED),
        (EffectState.CANCELLED, InvocationStatus.CANCELLED),
    ],
)
def test_a_receipt_state_is_reported_as_its_own_status(state: EffectState, expected) -> None:
    """A backend that says "deferred" must not be reported as "executed".

    This is the delegated mode's whole point: the host gateway decides a call is
    waiting for the author, and the harness has to say so rather than claim the
    tool ran.
    """

    async def scenario() -> None:
        backend = RecordingExecutionBackend(state=state)
        handler = Recorder()
        gateway_ = gateway(definitions=[read_tool(handler)], backend=backend)
        result = await gateway_.invoke(invocation(), context())
        assert result.status is expected
        assert result.receipt is not None
        if expected is not InvocationStatus.EXECUTED:
            assert not result.status.succeeded, "only a proven success counts as success"
        if result.status is InvocationStatus.DENIED:
            assert result.reason, "a denied call must explain itself even without a detail"

    run(scenario())


def test_a_deferred_receipt_is_not_a_failure_and_not_a_success() -> None:
    async def scenario() -> None:
        backend = RecordingExecutionBackend(state=EffectState.DEFERRED)
        gateway_ = gateway(definitions=[read_tool()], backend=backend)
        result = await gateway_.invoke(invocation(), context())
        assert result.status is InvocationStatus.DEFERRED
        assert not result.status.succeeded
        assert not result.unknown
        # Raising for it would erase the distinction the host just made.
        assert result.raise_for_status() is result

    run(scenario())


def test_an_unsettled_state_from_a_backend_is_a_contract_violation() -> None:
    async def scenario() -> None:
        from yanxu_harness.errors import ContractError

        backend = RecordingExecutionBackend(state=EffectState.RUNNING)
        gateway_ = gateway(definitions=[read_tool()], backend=backend)
        with pytest.raises(ContractError, match="settled"):
            await gateway_.invoke(invocation(), context())

    run(scenario())
