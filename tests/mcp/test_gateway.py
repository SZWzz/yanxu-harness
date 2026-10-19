"""MCP05：core_tool/Gateway 真实执行、审批、结果 codec、错误与期限。

用真实的 ``ToolGateway`` + SQLite 账本 + 真实 MCP 子进程：状态不是猜的，
远端业务计数器、服务端 facts、账本行三者要对得上。最后一条测试跑真的
``DefaultHarness``，证明工具结果确实进入了**下一个模型请求**。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from support import harness

if not harness.sdk_available():  # pragma: no cover - exercised by the base gate
    pytest.skip(
        "the MCP extra is not installed; the MCP gate runs this suite with the pinned extra",
        allow_module_level=True,
    )

import anyio  # noqa: E402

from yanxu_harness.core.types import AssistantMessage, StopReason, TextContent, ToolCall, Usage  # noqa: E402
from yanxu_harness.integrations.mcp import (  # noqa: E402
    McpClientManager,
    McpLimits,
    McpToolAdapter,
    McpToolBinding,
    discover_catalog,
)
from yanxu_harness.runtime import (  # noqa: E402
    Cancellation,
    DefaultHarness,
    Principal,
    RunContext,
    RunIdentity,
    RunRequest,
    RunStatus,
)
from yanxu_harness.tools import (  # noqa: E402
    ApprovalStatus,
    ConservativePolicy,
    EffectState,
    GatewayBindings,
    InMemoryApprovalStore,
    InvocationStatus,
    ToolEffect,
    ToolGateway,
    ToolRegistry,
    core_tool,
    result_status,
    suspension_reason,
)
from yanxu_harness.storage import InMemoryPersistence, SqliteExecutionBackend  # noqa: E402
from support.harness import counter, facts_of, mcp_stdio_config  # noqa: E402

READ = McpToolBinding("echo", effects=frozenset({ToolEffect.READ}))
FAIL = McpToolBinding("explode", effects=frozenset({ToolEffect.READ}))
IMAGE = McpToolBinding("image", effects=frozenset({ToolEffect.READ}))
EMPTY = McpToolBinding("empty", effects=frozenset({ToolEffect.READ}))


def write_binding(name: str = "bump") -> McpToolBinding:
    return McpToolBinding(
        name,
        effects=frozenset({ToolEffect.WRITE}),
        preview=lambda _ctx, args: {"key": args.get("key"), "effect": "increment"},
    )


def context(*, run_id: str = "r1", permissions: frozenset[str] | None = None) -> RunContext:
    return RunContext(
        identity=RunIdentity(namespace="project-a", session_id="s1", run_id=run_id, attempt_id="a1"),
        principal=Principal(subject="author", permissions=permissions or frozenset({"mcp.write"})),
    )


async def deployment(
    tmp_path,
    bindings,
    *,
    policy=None,
    limits=None,
    ledger: str | None = "ledger.sqlite",
    approvals=None,
    extra_env=None,
):
    """Wire one real deployment: MCP server + catalog + adapter + gateway + SQLite."""
    facts, state = tmp_path / "facts.jsonl", tmp_path / "state"
    manager = McpClientManager()
    await manager.__aenter__()
    config = mcp_stdio_config(facts, state, limits=limits, extra_env=extra_env)
    connection = await manager.connect(config)
    snapshot = await discover_catalog(manager, connection, config=config, bindings=bindings)
    adapter = McpToolAdapter(manager=manager, connection=connection, config=config, snapshot=snapshot)
    definitions = adapter.definitions()
    backend = SqliteExecutionBackend(tmp_path / ledger) if ledger else None
    gateway = ToolGateway(
        registry=ToolRegistry(list(definitions)),
        policy=policy or ConservativePolicy(auto_write=True),
        approvals=approvals or InMemoryApprovalStore(),
        backend=backend,
    )
    return {
        "manager": manager,
        "connection": connection,
        "config": config,
        "snapshot": snapshot,
        "adapter": adapter,
        "definitions": definitions,
        "backend": backend,
        "gateway": gateway,
        "facts": facts,
        "state": state,
    }


def spec_for(deployment_map, ctx, *, label="mcp"):
    """A core tool spec bound to this deployment's gateway and context."""
    bindings = GatewayBindings(gateway=deployment_map["gateway"], context=ctx)
    return core_tool(deployment_map["definitions"][0], bindings, label=label)


# -- success, failure, unsupported ---------------------------------------


async def test_a_read_tool_settles_once_in_the_ledger_and_reaches_the_model(tmp_path) -> None:
    deploy = await deployment(tmp_path, {"echo": READ})
    try:
        spec = spec_for(deploy, context())
        projected = await spec.execute("call-1", {"text": "hello"})
        assert projected["is_error"] is False
        assert result_status(projected) is InvocationStatus.EXECUTED
        assert projected["content"][0].text == "hello"
        details = projected["details"]
        assert details["effect_state"] == "succeeded"
        assert details["receipt_details"]["remote_tool"] == "echo"
        assert details["receipt_details"]["transport"] == "stdio"
        assert details["receipt_details"]["dispatched"] is True
    finally:
        await deploy["manager"].__aexit__(None, None, None)

    assert len(facts_of(deploy["facts"], "server.call_tool.start")) == 1
    # The durable row survives a reopen and carries the same outcome.
    fresh = SqliteExecutionBackend(tmp_path / "ledger.sqlite")
    rows = fresh.all_operations()
    assert [row["state"] for row in rows] == ["succeeded"]
    assert "hello" in str(rows[0]["result"])


async def test_a_terminal_remote_error_is_failed_and_says_so(tmp_path) -> None:
    deploy = await deployment(tmp_path, {"explode": FAIL})
    try:
        projected = await spec_for(deploy, context()).execute("call-1", {})
        assert projected["is_error"] is True
        assert result_status(projected) is InvocationStatus.FAILED
        assert "remote failure: exploded" in projected["content"][0].text
        receipt_details = projected["details"]["receipt_details"]
        assert receipt_details["remote_is_error"] is True
        assert "exploded" in receipt_details["error_text"]
    finally:
        await deploy["manager"].__aexit__(None, None, None)

    assert len(facts_of(deploy["facts"], "server.call_tool.start")) == 1, "no retry of a terminal error"
    fresh = SqliteExecutionBackend(tmp_path / "ledger.sqlite")
    assert [row["state"] for row in fresh.all_operations()] == ["failed"]


async def test_an_unsupported_media_answer_is_explicit_never_an_empty_success(tmp_path) -> None:
    deploy = await deployment(tmp_path, {"image": IMAGE})
    try:
        projected = await spec_for(deploy, context()).execute("call-1", {})
        assert projected["is_error"] is True
        assert result_status(projected) is InvocationStatus.FAILED
        assert "cannot represent" in projected["content"][0].text
        assert projected["details"]["receipt_details"]["unsupported_kind"] == "media_or_shape"
    finally:
        await deploy["manager"].__aexit__(None, None, None)

    assert len(facts_of(deploy["facts"], "server.call_tool.start")) == 1


async def test_an_empty_success_is_still_a_real_success(tmp_path) -> None:
    deploy = await deployment(tmp_path, {"empty": EMPTY})
    try:
        projected = await spec_for(deploy, context()).execute("call-1", {})
        assert projected["is_error"] is False
        assert result_status(projected) is InvocationStatus.EXECUTED
        assert isinstance(projected["content"], list)
    finally:
        await deploy["manager"].__aexit__(None, None, None)


# -- approval and refusal ------------------------------------------------


async def test_a_write_tool_waits_for_approval_then_runs_exactly_once(tmp_path) -> None:
    approvals = InMemoryApprovalStore()
    deploy = await deployment(
        tmp_path, {"bump": write_binding()}, policy=ConservativePolicy(), approvals=approvals
    )
    try:
        spec = spec_for(deploy, context())
        first = await spec.execute("call-1", {"key": "approved"})
        assert first["is_error"] is True
        assert result_status(first) is InvocationStatus.AWAITING_APPROVAL
        assert suspension_reason(first) == "awaiting_approval"
        assert facts_of(deploy["facts"], "server.call_tool.start") == [], "nothing may be sent before approval"

        await approvals.decide(first["details"]["approval"]["binding_key"], ApprovalStatus.APPROVED, "author")
        second = await spec.execute("call-1", {"key": "approved"})
        assert result_status(second) is InvocationStatus.EXECUTED
    finally:
        await deploy["manager"].__aexit__(None, None, None)

    assert len(facts_of(deploy["facts"], "server.call_tool.start")) == 1
    assert counter(deploy["state"], "approved") == 1


# -- unknown outcome, cancellation ---------------------------------------


async def test_a_timeout_after_dispatch_is_unknown_and_is_not_replayed(tmp_path) -> None:
    limits = McpLimits(call_timeout=0.5, connect_timeout=10.0)
    deploy = await deployment(tmp_path, {"slow": McpToolBinding("slow", effects=frozenset({ToolEffect.READ}))}, limits=limits)
    try:
        projected = await spec_for(deploy, context()).execute("call-1", {"seconds": 5})
        assert result_status(projected) is InvocationStatus.UNKNOWN
        assert suspension_reason(projected) == "unknown"
        assert projected["is_error"] is True
        assert "could not be proven" in projected["content"][0].text
        assert projected["details"]["receipt_details"]["dispatched"] is True
    finally:
        await deploy["manager"].__aexit__(None, None, None)

    # The server did receive it: that is exactly why the outcome is unknown, not failed.
    assert len(facts_of(deploy["facts"], "server.call_tool.start")) == 1


async def test_cancellation_before_dispatch_is_a_control_signal_with_zero_calls(tmp_path) -> None:
    deploy = await deployment(tmp_path, {"echo": READ})
    try:
        cancellation = Cancellation()
        cancellation.cancel("author stopped the run")
        ctx = RunContext(
            identity=RunIdentity(namespace="project-a", session_id="s1", run_id="r1", attempt_id="a1"),
            principal=Principal(subject="author", permissions=frozenset({"mcp.read"})),
            cancellation=cancellation,
        )
        projected = await spec_for(deploy, ctx).execute("call-1", {"text": "should not happen"})
        assert result_status(projected) is InvocationStatus.CANCELLED
    finally:
        await deploy["manager"].__aexit__(None, None, None)

    assert facts_of(deploy["facts"], "server.call_tool.start") == []


async def test_a_dead_connection_is_reported_as_not_sent(tmp_path) -> None:
    deploy = await deployment(
        tmp_path,
        {"die": McpToolBinding("die", effects=frozenset({ToolEffect.WRITE}), preview=lambda _c, _a: {"k": 1}),
         "echo": READ},
    )
    try:
        # Kill the server with a real call first, then try to use the same connection.
        dead_dir = tmp_path / "dead"
        dead_dir.mkdir()
        dead = await deployment(
            dead_dir,
            {"die": McpToolBinding("die", effects=frozenset({ToolEffect.WRITE}), preview=lambda _c, _a: {"k": 1})},
        )
        try:
            await spec_for(dead, context()).execute("call-1", {})
        except Exception:  # noqa: BLE001 - the point is the crash, not its type
            pass

        adapter = deploy["adapter"]
        # Simulate the same observable state: the manager knows the connection died.
        await deploy["manager"].close_connection(deploy["connection"])
        result = await adapter.invoke(deploy["snapshot"].entry("echo"), context(), {"text": "x"})
        assert result.state is EffectState.FAILED
        assert result.details["not_sent"] is True
        await dead["manager"].__aexit__(None, None, None)
    finally:
        await deploy["manager"].__aexit__(None, None, None)

    assert facts_of(deploy["facts"], "server.call_tool.start") == []


# -- the whole loop ------------------------------------------------------


async def test_the_result_enters_the_next_model_request(tmp_path) -> None:
    """A real DefaultHarness run: tool call → remote answer → next model request."""
    deploy = await deployment(tmp_path, {"echo": READ})
    seen: list[tuple[str, ...]] = []

    class Script:
        """Turn 1 asks for the tool; turn 2 answers with text."""

        def __init__(self) -> None:
            self.turns = [
                AssistantMessage(
                    content=[ToolCall(id="call-1", name=deploy["definitions"][0].name, arguments={"text": "written"})],
                    stop_reason=StopReason.TOOL_USE,
                ),
                AssistantMessage(content=[TextContent(text="done")], stop_reason=StopReason.STOP),
            ]
            self.calls = 0
            self.seen: list[str] = []

        async def __call__(self, model, context, options=None):
            await asyncio.sleep(0)
            self.calls += 1
            self.seen.append(repr(context))
            self._final = self.turns[min(self.calls - 1, len(self.turns) - 1)]
            return self

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def result(self):
            return self._final

    provider = Script()
    store = InMemoryPersistence()
    harness = DefaultHarness(
        stream_fn=provider,
        tools=[spec_for(deploy, context())],
        persistence=store,
        system_prompt="be brief",
    )
    try:
        outcome = await harness.start(RunRequest(prompt="write it"), context())
        assert outcome.status is RunStatus.COMPLETED
    finally:
        await deploy["manager"].__aexit__(None, None, None)

    assert provider.calls == 2, "the tool result must produce a second model request"
    assert len(facts_of(deploy["facts"], "server.call_tool.start")) == 1
    assert counter(deploy["state"], "call-1") == 0  # echo does not bump; the call fact is the proof
    # The second model request really carried the remote text.
    assert len(provider.seen) == 2
    assert "written" in provider.seen[1], "the remote result must reach the next model request"


def _tool_blocks(provider) -> list:
    """Deprecated placeholder kept out of the assertions above."""
    return []
