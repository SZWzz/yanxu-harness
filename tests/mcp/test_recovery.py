"""MCP06：真实故障窗口、unknown 屏障、回执复用与业务证据提升。

每条断言都对着真实产物：远端业务文件（写发生在回包之前）、SQLite 里的行、服务端
facts 计数，以及**另一个进程**读同一份账本时的结论。没有任何一条用「应该」代替证据。
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from support import harness

if not harness.sdk_available():  # pragma: no cover - exercised by the base gate
    pytest.skip(
        "the MCP extra is not installed; the MCP gate runs this suite with the pinned extra",
        allow_module_level=True,
    )

from yanxu_harness.errors import ContractError  # noqa: E402
from yanxu_harness.integrations.mcp import McpToolBinding  # noqa: E402
from yanxu_harness.runtime import Principal, RunContext, RunIdentity  # noqa: E402
from yanxu_harness.storage import SqliteExecutionBackend  # noqa: E402
from yanxu_harness.tools import (  # noqa: E402
    EffectState,
    GatewayBindings,
    InvocationStatus,
    ToolEffect,
    ToolInvocation,
    core_tool,
    result_status,
    suspension_reason,
)
from support.deployment import build_deployment  # noqa: E402
from support.harness import counter, facts_of  # noqa: E402

WRITE = McpToolBinding(
    "bump_then_die",
    effects=frozenset({ToolEffect.WRITE}),
    preview=lambda _ctx, args: {"key": args.get("key"), "effect": "increment"},
)
READ = McpToolBinding("echo", effects=frozenset({ToolEffect.READ}))


def ctx(run_id: str = "r1") -> RunContext:
    return RunContext(
        identity=RunIdentity(namespace="project-a", session_id="s1", run_id=run_id, attempt_id="a1"),
        principal=Principal(subject="author", permissions=frozenset({"mcp.write"})),
    )


def invocation_for(deploy, call_id: str = "call-1", arguments=None) -> ToolInvocation:
    entry = deploy["snapshot"].entries[0]
    return ToolInvocation(
        call_id=call_id,
        namespace="project-a",
        tool_name=entry.alias,
        arguments=arguments if arguments is not None else {"key": "recovered"},
        # Same logical call as the one that was admitted: session and run are part of
        # the operation key, so a review that dropped them would be asking about a
        # different operation (and would correctly answer "absent").
        session_id="s1",
        run_id="r1",
    )


async def test_the_unknown_window_is_real_and_stays_a_barrier(tmp_path) -> None:
    deploy = await build_deployment(tmp_path, {"bump_then_die": WRITE})
    try:
        projected = await core_tool(
            deploy["definitions"][0], GatewayBindings(gateway=deploy["gateway"], context=ctx())
        ).execute("call-1", {"key": "recovered"})
        assert result_status(projected) is InvocationStatus.UNKNOWN
        assert suspension_reason(projected) == "unknown"

        # The business effect happened before the answer was lost.
        assert counter(deploy["state"], "recovered") == 1
        assert len(facts_of(deploy["facts"], "server.business_written")) == 1

        # A review of the same binding says "may have run", not "absent".
        reviewed = await deploy["gateway"].review(invocation_for(deploy), ctx())
        assert reviewed is not None and reviewed.status is InvocationStatus.UNKNOWN

        # And a second admission attempt does not dispatch again.
        again = await deploy["gateway"].invoke(invocation_for(deploy), ctx())
        assert again.status is InvocationStatus.UNKNOWN
    finally:
        await deploy["manager"].__aexit__(None, None, None)

    assert len(facts_of(deploy["facts"], "server.call_tool.start")) == 1, "the barrier must not replay"
    row = deploy["backend"].all_operations()[0]
    assert row["state"] == "unknown"
    assert row["result"] is None


async def test_business_evidence_promotes_the_unknown_and_is_idempotent(tmp_path) -> None:
    deploy = await build_deployment(tmp_path, {"bump_then_die": WRITE})
    try:
        await deploy["gateway"].invoke(invocation_for(deploy), ctx())
        evidence = {
            "state": "succeeded",
            "source": "remote business record",
            "detail": "the counter file shows the increment",
        }
        promoted = await deploy["gateway"].promote(invocation_for(deploy), evidence)
        assert promoted is not None and promoted.state is EffectState.SUCCEEDED
        assert promoted.details.get("reconciled") is True

        # Promoting the same fact again is refused by the ledger: promotion is the one
        # write that turns "unproven" into "proven", and a settled row is not reopened.
        with pytest.raises(ContractError):
            await deploy["gateway"].promote(invocation_for(deploy), evidence)

        reviewed = await deploy["gateway"].review(invocation_for(deploy), ctx())
        assert reviewed is not None and reviewed.status is InvocationStatus.REPLAYED
        assert reviewed.receipt is not None and reviewed.receipt.state is EffectState.SUCCEEDED
    finally:
        await deploy["manager"].__aexit__(None, None, None)

    assert len(facts_of(deploy["facts"], "server.call_tool.start")) == 1, "promotion never re-dispatches"
    assert [row["state"] for row in deploy["backend"].all_operations()] == ["succeeded"]


async def test_a_second_process_reads_the_unknown_as_unknown_not_absent(tmp_path) -> None:
    deploy = await build_deployment(tmp_path, {"bump_then_die": WRITE})
    try:
        await deploy["gateway"].invoke(invocation_for(deploy), ctx())
        operation_id = deploy["backend"].all_operations()[0]["operation_id"]
    finally:
        await deploy["manager"].__aexit__(None, None, None)

    script = (
        "import json, sys;"
        "sys.path.insert(0, %r);"
        "from yanxu_harness.storage import SqliteExecutionBackend;"
        "reader = SqliteExecutionBackend(%r, read_only=True);"
        "receipt = reader.receipt(%r);"
        "print(json.dumps({'state': receipt.state.value if receipt else None}))"
    ) % (str(harness.SUPPORT_DIR.parents[1] / "src"), str(tmp_path / "ledger.sqlite"), operation_id)
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-500:]
    assert '"unknown"' in result.stdout, result.stdout


async def test_a_settled_receipt_is_reused_without_running_the_tool_again(tmp_path) -> None:
    deploy = await build_deployment(tmp_path, {"echo": READ})
    try:
        spec = core_tool(deploy["definitions"][0], GatewayBindings(gateway=deploy["gateway"], context=ctx()))
        first = await spec.execute("call-1", {"text": "hello"})
        assert result_status(first) is InvocationStatus.EXECUTED

        # A recovered driver asks the ledger instead of the server, twice.
        for _ in range(2):
            reviewed = await deploy["gateway"].review(
                invocation_for(deploy, arguments={"text": "hello"}), ctx()
            )
            assert reviewed is not None and reviewed.status is InvocationStatus.REPLAYED
            assert reviewed.receipt is not None
    finally:
        await deploy["manager"].__aexit__(None, None, None)

    assert len(facts_of(deploy["facts"], "server.call_tool.start")) == 1


async def test_promotion_evidence_must_name_a_state_and_a_source(tmp_path) -> None:
    deploy = await build_deployment(tmp_path, {"bump_then_die": WRITE})
    try:
        await deploy["gateway"].invoke(invocation_for(deploy), ctx())
        with pytest.raises(ContractError):
            await deploy["gateway"].promote(invocation_for(deploy), {"source": "somewhere"})
        with pytest.raises(ContractError):
            await deploy["gateway"].promote(invocation_for(deploy), {"state": "succeeded"})
        with pytest.raises(ContractError):
            await deploy["gateway"].promote(invocation_for(deploy), {"state": "maybe", "source": "x"})
    finally:
        await deploy["manager"].__aexit__(None, None, None)

    assert len(facts_of(deploy["facts"], "server.call_tool.start")) == 1


async def test_the_receipt_keeps_the_frozen_definition_identity(tmp_path) -> None:
    deploy = await build_deployment(tmp_path, {"echo": READ})
    entry = deploy["snapshot"].entries[0]
    try:
        await core_tool(
            deploy["definitions"][0], GatewayBindings(gateway=deploy["gateway"], context=ctx())
        ).execute("call-1", {"text": "identity"})
    finally:
        await deploy["manager"].__aexit__(None, None, None)

    details = deploy["backend"].all_operations()[0]
    operation_id = details["operation_id"]
    receipt = deploy["backend"].receipt(operation_id)
    assert receipt is not None
    details = dict(receipt.details)
    assert details["alias"] == entry.alias
    assert details["tool_version"] == entry.version
    assert details["remote_tool"] == "echo"
    assert details["protocol"] == "2025-11-25"
    assert details["transport"] == "stdio"
    assert details["dispatched"] is True
    assert os.path.exists(tmp_path / "ledger.sqlite")
