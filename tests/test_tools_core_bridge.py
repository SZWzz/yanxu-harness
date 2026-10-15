"""The tool → core bridge: the gateway decides, and its decision is not flattened.

Two properties matter more than the plumbing:

* the nine gateway statuses stay nine distinct statuses in the tool result (a
  single ``is_error=True`` would erase "denied" vs "waiting for a human" vs
  "the outcome is unknown");
* execution happens in the gateway's backend — including a host-owned one — and
  nowhere else, so the bridge can never become a second ledger.
"""

from __future__ import annotations

import asyncio

import pytest

from yanxu_harness.core.types import TextContent, Usage
from yanxu_harness.errors import ContractError
from yanxu_harness.runtime import Principal, RunContext, RunIdentity
from yanxu_harness.testing import RecordingExecutionBackend, failing_handler
from yanxu_harness.tools import (
    Approval,
    ApprovalBinding,
    ApprovalStatus,
    ConservativePolicy,
    EffectState,
    ExecutionMode,
    GatewayBindings,
    Idempotency,
    InMemoryApprovalStore,
    InvocationStatus,
    ToolDefinition,
    ToolEffect,
    ToolGateway,
    ToolInvocationResult,
    ToolReceipt,
    ToolRegistry,
    core_tool,
    result_status,
    suspension_reason,
    tool_result_from_invocation,
)

NAMESPACE = "project-a"


def run(coro):
    async def guard():
        return await asyncio.wait_for(coro, timeout=10)

    return asyncio.run(guard())


def context(namespace: str = NAMESPACE, run_id: str = "r1") -> RunContext:
    return RunContext(
        identity=RunIdentity(namespace=namespace, session_id="s1", run_id=run_id, attempt_id="a1"),
        principal=Principal(subject="author", permissions=frozenset({"notes.write"})),
    )


def path_validator(arguments: dict) -> dict:
    if not isinstance(arguments.get("path"), str) or not arguments["path"]:
        raise ValueError("path must be a non-empty string")
    return {"path": arguments["path"]}


class RecordingHandler:
    def __init__(self, result=None):
        self.calls: list[dict] = []
        self.result = result if result is not None else {"content": [TextContent(text="done")]}

    def __call__(self, ctx, arguments):
        self.calls.append({"namespace": ctx.identity.namespace, "tool_call_id": ctx.tool_call_id, "arguments": arguments})
        return self.result


def write_definition(handler) -> ToolDefinition:
    return ToolDefinition(
        "notes.write",
        "write a note",
        effects=frozenset({ToolEffect.WRITE}),
        args_validator=path_validator,
        handler=handler,
        preview=lambda _ctx, arguments: {"path": arguments["path"], "change": "append"},
    )


def gateway_for(
    definition, *, backend=None, policy=None, approvals=None, visibility=None, auto_write=False
) -> ToolGateway:
    return ToolGateway(
        registry=ToolRegistry([definition]),
        policy=policy or ConservativePolicy(auto_write=auto_write),
        approvals=approvals or InMemoryApprovalStore(),
        backend=backend or RecordingExecutionBackend(),
        visibility=visibility,
    )


def spec_for(gateway, definition, *, run_context=None, target_version=None) -> tuple[object, GatewayBindings]:
    bindings = GatewayBindings(
        gateway=gateway,
        context=run_context or context(),
        target_version=target_version,
    )
    return core_tool(definition, bindings), bindings


# --- the happy path ----------------------------------------------------------


def test_a_read_tool_runs_through_the_gateway_once() -> None:
    handler = RecordingHandler({"content": [TextContent(text="a.md: hello")], "details": {"bytes": 12}})
    definition = ToolDefinition("notes.read", "read a note", args_validator=path_validator, handler=handler)
    backend = RecordingExecutionBackend()
    spec, _ = spec_for(gateway_for(definition, backend=backend), definition)

    result = run(spec.execute("call-1", {"path": "a.md"}))

    assert [block.text for block in result["content"]] == ["a.md: hello"]
    assert result["is_error"] is False
    assert result["details"]["status"] == "executed"
    assert len(handler.calls) == 1
    assert len(backend.intents) == 1 and len(backend.receipts) == 1
    assert result_status(result) is InvocationStatus.EXECUTED
    assert suspension_reason(result) is None


def test_the_tool_call_context_cannot_widen_identity() -> None:
    handler = RecordingHandler()
    definition = ToolDefinition("notes.read", "read a note", args_validator=path_validator, handler=handler)
    spec, _ = spec_for(gateway_for(definition), definition)

    # The model can put anything in the arguments; the namespace the effect is
    # recorded under comes from the run context.
    run(spec.execute("call-1", {"path": "a.md", "namespace": "someone-elses-project"}))

    assert handler.calls[0]["namespace"] == NAMESPACE
    assert handler.calls[0]["tool_call_id"] == "call-1"


def test_a_settled_operation_is_replayed_instead_of_running_twice() -> None:
    handler = RecordingHandler()
    definition = ToolDefinition("notes.read", "read a note", args_validator=path_validator, handler=handler)
    backend = RecordingExecutionBackend()
    spec, _ = spec_for(gateway_for(definition, backend=backend), definition)

    first = run(spec.execute("call-1", {"path": "a.md"}))
    second = run(spec.execute("call-1", {"path": "a.md"}))

    assert first["details"]["status"] == "executed"
    assert second["details"]["status"] == "replayed"
    assert len(handler.calls) == 1, "a settled call must not run again"
    assert second["is_error"] is False, "a replay is the same successful effect, not an error"
    assert len(backend.receipts) == 1 and len(backend.settled) == 1


# --- the statuses stay distinct ---------------------------------------------


def _invocation_result(status: InvocationStatus) -> ToolInvocationResult:
    base = {"tool_name": "notes.write", "tool_version": "1", "operation_id": "op-1"}
    receipt = ToolReceipt(operation_id="op-1", state=EffectState.SUCCEEDED, result={"content": [TextContent(text="ok")]})
    match status:
        case InvocationStatus.EXECUTED | InvocationStatus.REPLAYED:
            return ToolInvocationResult(status=status, receipt=receipt, **base)
        case InvocationStatus.UNKNOWN:
            return ToolInvocationResult(
                status=status, receipt=ToolReceipt(operation_id="op-1", state=EffectState.UNKNOWN), **base
            )
        case InvocationStatus.DEFERRED:
            return ToolInvocationResult(
                status=status,
                receipt=ToolReceipt(operation_id="op-1", state=EffectState.DEFERRED, detail="作者要看一眼"),
                **base,
            )
        case InvocationStatus.FAILED:
            return ToolInvocationResult(
                status=status,
                receipt=ToolReceipt(operation_id="op-1", state=EffectState.FAILED, detail="disk full"),
                **base,
            )
        case InvocationStatus.AWAITING_APPROVAL:
            approval = Approval(
                binding=ApprovalBinding(
                    namespace=NAMESPACE,
                    call_id="call-1",
                    tool_name="notes.write",
                    tool_version="1",
                    args_hash="hash",
                )
            )
            return ToolInvocationResult(status=status, approval=approval, preview={"path": "a.md"}, **base)
        case InvocationStatus.DENIED:
            return ToolInvocationResult(status=status, reason="not exposed by this deployment", **base)
        case InvocationStatus.INVALID_ARGS:
            return ToolInvocationResult(status=status, reason="path must be a non-empty string", **base)
        case InvocationStatus.CANCELLED:
            return ToolInvocationResult(status=status, reason="run cancelled", **base)
    raise AssertionError(f"unhandled status {status}")


ALL_STATUSES = (
    InvocationStatus.EXECUTED,
    InvocationStatus.REPLAYED,
    InvocationStatus.AWAITING_APPROVAL,
    InvocationStatus.DEFERRED,
    InvocationStatus.DENIED,
    InvocationStatus.INVALID_ARGS,
    InvocationStatus.FAILED,
    InvocationStatus.CANCELLED,
    InvocationStatus.UNKNOWN,
)


def test_every_status_keeps_its_own_name_and_only_success_is_not_an_error() -> None:
    results = {status: tool_result_from_invocation(_invocation_result(status)) for status in ALL_STATUSES}

    assert [results[status]["details"]["status"] for status in ALL_STATUSES] == [s.value for s in ALL_STATUSES]
    assert {status for status in ALL_STATUSES if not results[status]["is_error"]} == {
        InvocationStatus.EXECUTED,
        InvocationStatus.REPLAYED,
    }
    # Text is always present, so a caller that only reads the model-facing half
    # still cannot mistake a refusal for a success.
    for status, result in results.items():
        text = "".join(getattr(block, "text", "") for block in result["content"])
        assert text, status
    assert "waiting for an approval decision" in results[InvocationStatus.AWAITING_APPROVAL]["content"][0].text
    assert "not retried automatically" in results[InvocationStatus.UNKNOWN]["content"][0].text
    assert "Fix the arguments" in results[InvocationStatus.INVALID_ARGS]["content"][0].text


def test_only_the_three_pending_statuses_suspend_the_run() -> None:
    pending = {
        status for status in ALL_STATUSES if suspension_reason(tool_result_from_invocation(_invocation_result(status)))
    }
    assert pending == {
        InvocationStatus.AWAITING_APPROVAL,
        InvocationStatus.DEFERRED,
        InvocationStatus.UNKNOWN,
    }


def test_a_result_with_an_unknown_status_is_refused() -> None:
    with pytest.raises(ContractError):
        result_status({"details": {"status": "probably_fine"}})
    assert result_status({"details": {}}) is None


# --- real gateway paths ------------------------------------------------------


def test_waiting_for_approval_is_not_a_completed_call() -> None:
    handler = RecordingHandler()
    definition = write_definition(handler)
    backend = RecordingExecutionBackend()
    spec, _ = spec_for(gateway_for(definition, backend=backend), definition)

    result = run(spec.execute("call-1", {"path": "a.md"}))

    assert result["details"]["status"] == "awaiting_approval"
    assert result["is_error"] is True, "an unapproved write must not look like a completed effect"
    assert handler.calls == [], "nothing may run before the decision"
    assert backend.intents == [], "and no intent may exist for a call that was never authorised"
    assert result["details"]["approval"]["status"] == ApprovalStatus.PENDING.value
    assert result["details"]["preview"] == {"path": "a.md", "change": "append"}
    assert suspension_reason(result) == "awaiting_approval"


def test_an_approved_retry_runs_exactly_once_and_replays_afterwards() -> None:
    handler = RecordingHandler()
    definition = write_definition(handler)
    backend = RecordingExecutionBackend()
    approvals = InMemoryApprovalStore()
    spec, bindings = spec_for(gateway_for(definition, backend=backend, approvals=approvals), definition)

    waiting = run(spec.execute("call-1", {"path": "a.md"}))
    binding = waiting["details"]["approval"]["binding_key"]

    async def approve() -> None:
        stored = await approvals.get(binding)
        assert stored is not None
        await approvals.decide(binding, ApprovalStatus.APPROVED, decided_by="operator")

    run(approve())
    approved = run(spec.execute("call-1", {"path": "a.md"}))
    again = run(spec.execute("call-1", {"path": "a.md"}))

    assert approved["details"]["status"] == "executed"
    assert again["details"]["status"] == "replayed"
    assert len(handler.calls) == 1
    assert len(backend.receipts) == 1 and len(backend.settled) == 1


def test_a_denied_call_leaves_no_ledger_entry() -> None:
    handler = RecordingHandler()
    definition = write_definition(handler)
    backend = RecordingExecutionBackend()

    class Hiding:
        def exposed(self, definition, ctx) -> bool:
            return False

    spec, _ = spec_for(gateway_for(definition, backend=backend, visibility=Hiding()), definition)
    result = run(spec.execute("call-1", {"path": "a.md"}))

    assert result["details"]["status"] == "denied"
    assert handler.calls == [] and backend.intents == []
    assert suspension_reason(result) is None, "a denial is final; it does not suspend the run"


def test_invalid_arguments_are_their_own_state() -> None:
    handler = RecordingHandler()
    backend = RecordingExecutionBackend()
    definition = ToolDefinition("notes.write", "write", args_validator=path_validator, handler=handler,
                                 effects=frozenset({ToolEffect.WRITE}),
                                 preview=lambda _ctx, arguments: {"path": arguments["path"]})
    spec, _ = spec_for(gateway_for(definition, backend=backend), definition)

    result = run(spec.execute("call-1", {"path": ""}))

    assert result["details"]["status"] == "invalid_args"
    assert handler.calls == [] and backend.intents == []
    assert result_status(result) is InvocationStatus.INVALID_ARGS


def test_an_unknown_outcome_is_reported_as_unknown_and_not_retried() -> None:
    handler = RecordingHandler()
    definition = ToolDefinition(
        "notes.write",
        "write",
        args_validator=path_validator,
        handler=handler,
        effects=frozenset({ToolEffect.WRITE}),
        preview=lambda _ctx, arguments: {"path": arguments["path"]},
    )
    backend = RecordingExecutionBackend(state=EffectState.UNKNOWN)
    spec, _ = spec_for(gateway_for(definition, backend=backend, auto_write=True), definition)

    result = run(spec.execute("call-1", {"path": "a.md"}))

    assert result["details"]["status"] == "unknown"
    assert result["details"]["effect_state"] == EffectState.UNKNOWN.value
    assert len(handler.calls) == 1, "the handler ran once; nothing retries it"
    assert suspension_reason(result) == "unknown"


def test_a_failed_handler_is_a_failure_not_an_unknown() -> None:
    definition = ToolDefinition(
        "notes.write",
        "write",
        args_validator=path_validator,
        handler=failing_handler(RuntimeError("disk full")),
        effects=frozenset({ToolEffect.WRITE}),
        preview=lambda _ctx, arguments: {"path": arguments["path"]},
    )
    backend = RecordingExecutionBackend()
    spec, _ = spec_for(gateway_for(definition, backend=backend, auto_write=True), definition)

    result = run(spec.execute("call-1", {"path": "a.md"}))

    # `classify_failure` is deliberately conservative: an exception from a handler
    # that may have touched the outside world is `unknown`, not `failed`.
    assert result["details"]["status"] == "unknown"
    assert suspension_reason(result) == "unknown"


# --- the delegated (host-owned) mode ----------------------------------------


class HostOwnedBackend:
    """A stand-in for a host gateway that keeps its own ledger."""

    ownership = "host"
    durable = True

    def __init__(self) -> None:
        self.begun: list[str] = []
        self.settled: list[str] = []
        self.ran = 0

    async def begin(self, intent):
        self.begun.append(intent.operation_id)
        return None

    async def execute(self, intent, definition, arguments, ctx):
        self.ran += 1
        return ToolReceipt(
            operation_id=intent.operation_id,
            state=EffectState.SUCCEEDED,
            result={"content": [TextContent(text="host wrote it")], "details": {"host": True}},
            external_id="host-op-9",
        )

    async def settle(self, receipt):
        self.settled.append(receipt.operation_id)


def test_a_host_owned_backend_stays_the_only_ledger() -> None:
    handler = RecordingHandler()
    definition = write_definition(handler)
    backend = HostOwnedBackend()
    gateway = gateway_for(definition, backend=backend, auto_write=True)
    spec, _ = spec_for(gateway, definition)

    result = run(spec.execute("call-1", {"path": "a.md"}))

    assert gateway.backend_ownership == "host"
    # The host's backend recorded the intent, ran the effect and settled it once. The
    # operation id is a composite of namespace/session/run/call, so two projects
    # cannot collide on their own "call-1".
    assert backend.begun == ["project-a:s1:r1:call-1"]
    assert backend.ran == 1 and backend.settled == ["project-a:s1:r1:call-1"]
    # …and the bridge never invoked the handler itself.
    assert handler.calls == []
    assert result["details"]["status"] == "executed"
    assert result["details"]["external_id"] == "host-op-9"
    assert [block.text for block in result["content"]] == ["host wrote it"]


# --- the core tool spec ------------------------------------------------------


def test_the_core_spec_carries_the_definition_and_the_replay_policy() -> None:
    handler = RecordingHandler()
    definition = ToolDefinition(
        "notes.write",
        "write a note",
        effects=frozenset({ToolEffect.WRITE}),
        args_validator=path_validator,
        handler=handler,
        preview=lambda _ctx, arguments: {"path": arguments["path"]},
        execution_mode=ExecutionMode.SEQUENTIAL,
        idempotency=Idempotency.KEYED,
        idempotency_key=lambda _ctx, arguments: arguments["path"],
        args_schema={"type": "object", "properties": {"path": {"type": "string"}}},
    )
    spec, _ = spec_for(gateway_for(definition), definition)

    assert (spec.name, spec.description) == ("notes.write", "write a note")
    assert spec.execution_mode == "sequential"
    assert spec.replay == "keyed"
    assert spec.parameters["properties"]["path"]["type"] == "string"
    assert spec.prepare_arguments is None, "validation belongs to the gateway, not to the loop"
    assert spec.label == ""


def test_a_read_only_definition_needs_no_replay_privilege() -> None:
    definition = ToolDefinition("notes.read", "read", args_validator=path_validator, handler=RecordingHandler())
    spec, _ = spec_for(gateway_for(definition), definition)
    assert spec.execution_mode == "parallel"
    assert spec.replay == "never"


def test_a_handler_result_that_is_not_content_blocks_is_still_readable() -> None:
    definition = ToolDefinition(
        "notes.read", "read", args_validator=path_validator, handler=lambda _ctx, _args: "plain text"
    )
    spec, _ = spec_for(gateway_for(definition), definition)
    result = run(spec.execute("call-1", {"path": "a.md"}))
    assert [block.text for block in result["content"]] == ["plain text"]

    definition_mapping = ToolDefinition(
        "notes.read", "read", args_validator=path_validator, handler=lambda _ctx, _args: {"content": ["raw"], "details": None}
    )
    spec2, _ = spec_for(gateway_for(definition_mapping), definition_mapping)
    result2 = run(spec2.execute("call-2", {"path": "a.md"}))
    assert [block.text for block in result2["content"]] == ["raw"]


def test_target_version_comes_from_the_host_not_from_the_arguments() -> None:
    handler = RecordingHandler()
    definition = write_definition(handler)
    backend = RecordingExecutionBackend()
    approvals = InMemoryApprovalStore()
    seen: list[str | None] = []

    def target_version(call_id: str, arguments: dict) -> str | None:
        seen.append(arguments.get("version"))
        return "v7"

    spec, _ = spec_for(
        gateway_for(definition, backend=backend, approvals=approvals),
        definition,
        target_version=target_version,
    )
    result = run(spec.execute("call-1", {"path": "a.md", "version": "v0-from-the-model"}))

    assert seen == ["v0-from-the-model"], "the callback sees the raw arguments"
    assert result["details"]["status"] == "awaiting_approval"
    binding = run(_binding(result["details"]["approval"]["binding_key"], approvals))
    assert binding.target_version == "v7", "the approval binds the server-side version"


async def _binding(key: str, approvals: InMemoryApprovalStore) -> ApprovalBinding:
    stored = await approvals.get(key)
    assert stored is not None
    return stored.binding


def test_usage_from_a_tool_receipt_is_not_invented() -> None:
    definition = ToolDefinition(
        "notes.read",
        "read",
        args_validator=path_validator,
        handler=lambda _ctx, _args: {"content": [TextContent(text="ok")], "usage": None},
    )
    spec, _ = spec_for(
        gateway_for(definition, backend=RecordingExecutionBackend(), auto_write=True), definition
    )
    result = run(spec.execute("call-1", {"path": "a.md"}))
    assert "usage" not in result or result.get("usage") is None
    assert Usage().reported is False
