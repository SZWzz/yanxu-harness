"""MCP01 的中立执行结果与 operation context。

这组测试盯住四件事：

1. **普通返回值仍然成功**：既有 handler 语义一个字都不变，远端错误只能通过中立类型表达。
2. **失败不是成功回执**：`ToolExecutionResult(state=FAILED)` 必须变成 FAILED receipt、
   `is_error=True`、模型能看到 detail；`UNKNOWN` 必须挂起而不是重放。
3. **效果未知不外泄为崩溃**：远端结果无法表示（不可存储、超限）时按 mutating 规则归类，
   不允许在 settle 时才炸掉一次已经发生的写入。
4. **operation context 是权威的**：handler 看到的是 Gateway 派生出来的 operation id，
   其余身份（namespace/session/run/attempt、Principal、取消、services）原样继承；
   host-owned ledger 不会多记一份 intent 或 receipt。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from yanxu_harness.errors import ContractError
from yanxu_harness.runtime import Cancellation, Principal, RunContext, RunIdentity
from yanxu_harness.storage import SqliteExecutionBackend
from yanxu_harness.testing import RecordingExecutionBackend
from yanxu_harness.tools import (
    ConservativePolicy,
    EffectState,
    InMemoryApprovalStore,
    InvocationStatus,
    ToolDefinition,
    ToolEffect,
    ToolGateway,
    ToolInvocation,
    ToolRegistry,
    core_tool,
    GatewayBindings,
    result_status,
    suspension_reason,
    tool_result_from_invocation,
)
from yanxu_harness.tools.execution import ToolExecutionResult, receipt_from_handler_result


def run(coro):
    return asyncio.run(coro)


def validator(arguments: dict) -> dict:
    if not isinstance(arguments.get("path"), str) or not arguments["path"]:
        raise ValueError("path must be a non-empty string")
    return {"path": arguments["path"]}


def context(namespace: str = "project-a") -> RunContext:
    return RunContext(
        identity=RunIdentity(namespace=namespace, session_id="s1", run_id="r1", attempt_id="a1"),
        principal=Principal(subject="author", permissions=frozenset({"story.write"})),
        services={"store": "host-owned"},
        labels={"model": "scripted"},
    )


def tool(handler, *, effects=frozenset({ToolEffect.READ}), name="notes.read") -> ToolDefinition:
    kwargs = {}
    if ToolEffect.WRITE in effects:
        kwargs["preview"] = lambda _c, a: {"path": a["path"], "action": "replace"}
    return ToolDefinition(
        name,
        "A tool with a handler",
        effects=effects,
        args_validator=validator,
        handler=handler,
        **kwargs,
    )


def gateway(definitions, backend=None) -> ToolGateway:
    return ToolGateway(
        registry=ToolRegistry(definitions),
        policy=ConservativePolicy(auto_write=True),
        approvals=InMemoryApprovalStore(),
        backend=backend or RecordingExecutionBackend(),
    )


def invocation(tool_name: str = "notes.read", *, call_id: str = "call-1") -> ToolInvocation:
    return ToolInvocation(
        call_id=call_id,
        namespace="project-a",
        tool_name=tool_name,
        arguments={"path": "a.md"},
        run_id="r1",
    )


# -- 1. plain values keep their meaning ---------------------------------


def test_a_plain_return_value_is_still_a_success_with_the_same_result() -> None:
    async def scenario() -> None:
        backend = RecordingExecutionBackend()
        gw = gateway([tool(lambda _c, a: {"wrote": a["path"]})], backend)
        result = await gw.invoke(invocation(), context())
        assert result.status is InvocationStatus.EXECUTED
        assert result.receipt is not None
        assert result.receipt.state is EffectState.SUCCEEDED
        assert result.receipt.result == {"wrote": "a.md"}

    run(scenario())


def test_a_plain_mapping_that_merely_says_iserror_is_still_not_a_failure() -> None:
    """既有语义的原样证据：普通字典里的 `isError` 不构成失败凭据。

    MCP 适配器因此必须走中立类型；这条断言防止有人"顺手"把它改成失败，
    也防止适配器依赖它来判定远端错误。
    """
    async def scenario() -> None:
        gw = gateway([tool(lambda _c, a: {"isError": True, "content": "boom"})])
        result = await gw.invoke(invocation(), context())
        assert result.status is InvocationStatus.EXECUTED
        assert result.receipt is not None and result.receipt.state is EffectState.SUCCEEDED

    run(scenario())


# -- 2. the neutral result type -----------------------------------------


def test_a_neutral_failure_is_a_failed_receipt_and_reaches_the_model_as_one() -> None:
    async def scenario() -> None:
        backend = RecordingExecutionBackend()

        async def handler(_context, _arguments):
            return ToolExecutionResult(
                state=EffectState.FAILED,
                detail="remote failure: exploded",
                details={"remote_class": "terminal_error"},
            )

        gw = gateway([tool(handler)], backend)
        result = await gw.invoke(invocation(), context())
        assert result.status is InvocationStatus.FAILED
        assert result.receipt is not None
        assert result.receipt.state is EffectState.FAILED
        assert "exploded" in result.receipt.detail
        assert result.receipt.details["remote_class"] == "terminal_error"

        spec = core_tool(tool(handler), GatewayBindings(gateway=gw, context=context()))
        projected = await spec.execute("call-1", {"path": "a.md"})
        assert projected["is_error"] is True
        assert result_status(projected) is InvocationStatus.FAILED
        assert "exploded" in projected["content"][0].text

    run(scenario())


def test_a_neutral_unknown_suspends_and_is_never_a_success() -> None:
    async def scenario() -> None:
        gw = gateway(
            [
                tool(
                    lambda _c, _a: ToolExecutionResult(
                        state=EffectState.UNKNOWN, detail="connection dropped after send"
                    )
                )
            ]
        )
        result = await gw.invoke(invocation(), context())
        assert result.status is InvocationStatus.UNKNOWN
        assert result.receipt is not None and result.receipt.needs_reconciliation

        spec = core_tool(
            tool(lambda _c, _a: ToolExecutionResult(EffectState.UNKNOWN, detail="dropped")),
            GatewayBindings(gateway=gw, context=context()),
        )
        projected = await spec.execute("call-1", {"path": "a.md"})
        assert projected["is_error"] is True
        assert suspension_reason(projected) == "unknown"

    run(scenario())


def test_a_neutral_success_carries_structured_and_empty_results() -> None:
    async def scenario() -> None:
        structured = lambda _c, _a: ToolExecutionResult(  # noqa: E731
            state=EffectState.SUCCEEDED, result={"content": [{"type": "text", "text": "ok"}]}, external_id="tx-7"
        )
        empty = lambda _c, _a: ToolExecutionResult(state=EffectState.SUCCEEDED, result={"content": []})  # noqa: E731
        gw = gateway([tool(structured, name="notes.read"), tool(empty, name="notes.empty")])

        first = await gw.invoke(invocation("notes.read"), context())
        assert first.status is InvocationStatus.EXECUTED
        assert first.receipt is not None
        assert first.receipt.external_id == "tx-7"
        assert first.receipt.result == {"content": [{"type": "text", "text": "ok"}]}

        second = await gw.invoke(invocation("notes.empty", call_id="call-2"), context())
        assert second.status is InvocationStatus.EXECUTED
        assert second.receipt is not None and second.receipt.result == {"content": []}

    run(scenario())


def test_the_neutral_result_refuses_states_outside_the_first_release() -> None:
    for state in (
        EffectState.RUNNING,
        EffectState.QUEUED,
        EffectState.REJECTED,
        EffectState.CANCELLED,
        EffectState.DEFERRED,
        EffectState.INVALID_ARGS,
    ):
        with pytest.raises(ContractError):
            ToolExecutionResult(state=state)
    with pytest.raises(ContractError):
        ToolExecutionResult(state=EffectState.SUCCEEDED, result=None)
    with pytest.raises(ContractError):
        ToolExecutionResult(state=EffectState.FAILED, detail="", details={"k": "v"})
    with pytest.raises(ContractError):
        ToolExecutionResult(state=EffectState.FAILED, detail="d", details={1: "not a string key"})


def test_the_neutral_result_details_are_bounded() -> None:
    with pytest.raises(ContractError):
        ToolExecutionResult(
            state=EffectState.FAILED, detail="d", details={f"k{i}": "v" for i in range(100)}
        )
    with pytest.raises(ContractError):
        ToolExecutionResult(state=EffectState.FAILED, detail="d", details={"k": "x" * 100_000})


def test_an_unrepresentable_success_is_unknown_for_a_mutating_tool_and_failed_for_a_read() -> None:
    class Nothing:
        pass

    mutating = receipt_from_handler_result(
        "op-1", ToolExecutionResult(state=EffectState.SUCCEEDED, result=Nothing()), mutating=True
    )
    assert mutating.state is EffectState.UNKNOWN
    assert "not storable" in mutating.detail or "cannot be represented" in mutating.detail

    read = receipt_from_handler_result(
        "op-2", ToolExecutionResult(state=EffectState.SUCCEEDED, result=Nothing()), mutating=False
    )
    assert read.state is EffectState.FAILED


# -- 3. the authoritative operation context ------------------------------


def test_the_handler_sees_the_gateway_derived_operation_id_and_nothing_else_changes() -> None:
    async def scenario() -> None:
        seen: dict = {}

        def handler(ctx, arguments):
            seen["identity"] = ctx.identity
            seen["principal"] = ctx.principal
            seen["cancellation"] = ctx.cancellation
            seen["services"] = ctx.services
            seen["labels"] = ctx.labels
            seen["tool_call_id"] = ctx.tool_call_id
            return {"ok": True}

        gw = gateway([tool(handler)])
        result = await gw.invoke(invocation(), context().for_tool_call("call-1"))
        assert result.receipt is not None
        assert seen["identity"].operation_id == result.receipt.operation_id
        assert seen["identity"].namespace == "project-a"
        assert seen["identity"].session_id == "s1"
        assert seen["identity"].run_id == "r1"
        assert seen["identity"].attempt_id == "a1"
        assert seen["principal"].subject == "author"
        assert seen["principal"].permissions == frozenset({"story.write"})
        assert seen["cancellation"] is not None
        assert seen["services"] == {"store": "host-owned"}
        assert seen["labels"] == {"model": "scripted"}
        assert seen["tool_call_id"] == "call-1"

    run(scenario())


def test_a_host_owned_context_shape_is_passed_through_untouched() -> None:
    """宿主自定义 context 不认识 operation id，就不能被强行改写。"""

    class HostContext:
        def __init__(self) -> None:
            self.identity = type("I", (), {"namespace": "project-a", "operation_id": None})()

    async def scenario() -> None:
        seen: list[object] = []

        def handler(ctx, _arguments):
            seen.append(ctx)
            return {"ok": True}

        gw = gateway([tool(handler)])
        host_context = HostContext()
        await gw.invoke(invocation(), host_context)
        assert seen[0] is host_context
        assert host_context.identity.operation_id is None

    run(scenario())


def test_a_host_owned_backend_records_exactly_one_intent_and_one_receipt() -> None:
    async def scenario() -> None:
        backend = RecordingExecutionBackend(ownership="host")
        gw = gateway([tool(lambda _c, _a: {"ok": True})], backend)
        result = await gw.invoke(invocation(), context())
        assert result.status is InvocationStatus.EXECUTED
        assert len(backend.intents) == 1
        assert len(backend.receipts) == 1
        assert len(backend.settled) == 1

    run(scenario())


# -- 4. the same normalisation on the durable ledger ---------------------


def test_the_sqlite_backend_stores_and_reads_back_a_neutral_result(tmp_path) -> None:
    async def scenario() -> None:
        path = tmp_path / "operations.sqlite"
        backend = SqliteExecutionBackend(path)

        async def handler(_context, _arguments):
            return ToolExecutionResult(
                state=EffectState.SUCCEEDED,
                result={"content": [{"type": "text", "text": "written"}]},
                detail="stored by the remote service",
                external_id="remote-42",
                details={"remote_tool": "bump"},
            )

        gw = gateway([tool(handler)], backend)
        result = await gw.invoke(invocation(), context())
        assert result.status is InvocationStatus.EXECUTED
        operation_id = result.operation_id

        fresh = SqliteExecutionBackend(path)
        receipt = fresh.receipt(operation_id)
        assert receipt is not None
        assert receipt.state is EffectState.SUCCEEDED
        assert receipt.result == {"content": [{"type": "text", "text": "written"}]}
        assert receipt.external_id == "remote-42"
        assert receipt.details["remote_tool"] == "bump"

    run(scenario())


def test_the_sqlite_backend_records_a_neutral_failure_without_inventing_success(tmp_path) -> None:
    async def scenario() -> None:
        backend = SqliteExecutionBackend(tmp_path / "operations.sqlite")

        async def handler(_context, _arguments):
            return ToolExecutionResult(state=EffectState.UNKNOWN, detail="frames lost after send")

        gw = gateway([tool(handler)], backend)
        result = await gw.invoke(invocation(), context())
        assert result.status is InvocationStatus.UNKNOWN
        receipt = backend.receipt(result.operation_id)
        assert receipt is not None and receipt.state is EffectState.UNKNOWN
        assert receipt.needs_reconciliation

    run(scenario())


def test_the_memory_and_sqlite_backends_agree_on_the_neutral_mapping(tmp_path) -> None:
    value = ToolExecutionResult(state=EffectState.FAILED, detail="remote said no", details={"code": "denied"})
    from_memory = receipt_from_handler_result("op-1", value, mutating=False)
    from_disk = receipt_from_handler_result("op-1", value, mutating=False)
    assert (from_memory.state, from_memory.detail, dict(from_memory.details)) == (
        from_disk.state,
        from_disk.detail,
        dict(from_disk.details),
    )
