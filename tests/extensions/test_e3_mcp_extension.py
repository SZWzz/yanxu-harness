"""EXT-AC33: the MCP extension path keeps the real adapter's semantics."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
MCP_SUPPORT = HERE.parent / "mcp"
EXAMPLES = HERE.parents[1] / "examples" / "extensions"
for extra in (MCP_SUPPORT, EXAMPLES):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from support.harness import (  # noqa: E402
    counter,
    facts_of,
    http_server,
    mcp_http_config,
    mcp_stdio_config,
    sdk_available,
)

from yanxu_harness.extensions import (  # noqa: E402
    ContributionDeclarations,
    ExtensionBinding,
    ExtensionManifest,
    open_extensions,
    plan_extensions,
)
from yanxu_harness.extensions.mcp import mcp_tool_factory  # noqa: E402
from yanxu_harness.integrations.mcp import McpToolBinding, local_alias  # noqa: E402
from yanxu_harness.runtime import Principal, RunContext, RunIdentity  # noqa: E402
from yanxu_harness.testing import RecordingExecutionBackend  # noqa: E402
from yanxu_harness.tools import (  # noqa: E402
    ApprovalStatus,
    ConservativePolicy,
    InMemoryApprovalStore,
    InvocationStatus,
    ToolEffect,
    ToolGateway,
    ToolInvocation,
)
from yanxu_harness.tools.execution import EffectState  # noqa: E402

from mcp_reference_pack import build_mcp_reference_pack  # noqa: E402

pytestmark = pytest.mark.skipif(
    not sdk_available(), reason="the optional MCP extra is not installed"
)

PROTOCOLS = ("2025-11-25", "2026-07-28")
TRANSPORTS = ("stdio", "http")


def _context(call_suffix: str = "1") -> RunContext:
    return RunContext(
        identity=RunIdentity(
            namespace="project-a",
            session_id="s1",
            run_id=f"r-{call_suffix}",
            attempt_id="a1",
        ),
        principal=Principal(subject="mcp-extension"),
        labels={"authorization_scope": "authorized"},
    )


def _invocation(tool: str, call_id: str, **arguments) -> ToolInvocation:
    return ToolInvocation(
        call_id=call_id,
        namespace="project-a",
        tool_name=tool,
        arguments=arguments,
        run_id=f"r-{call_id}",
    )


async def _run_roundtrip(config, facts: Path, state: Path) -> None:
    pack = build_mcp_reference_pack(config=config)
    deployment = await open_extensions(pack.plan).open()
    approvals = InMemoryApprovalStore()
    try:
        gateway = ToolGateway(
            registry=deployment.registry,
            policy=ConservativePolicy(),
            approvals=approvals,
            backend=RecordingExecutionBackend(),
        )
        context = _context("roundtrip")
        echo = await gateway.invoke(
            _invocation(pack.echo, "call-echo", text="hello"), context
        )
        assert echo.status is InvocationStatus.EXECUTED, echo.reason
        assert "hello" in str(echo.receipt.result)

        bump_invocation = _invocation(pack.bump, "call-bump", key="alpha")
        waiting = await gateway.invoke(bump_invocation, context)
        assert waiting.status is InvocationStatus.AWAITING_APPROVAL
        await approvals.decide(
            waiting.approval.binding.key(), ApprovalStatus.APPROVED, "operator"
        )
        bumped = await gateway.invoke(bump_invocation, context)
        assert bumped.status is InvocationStatus.EXECUTED, bumped.reason
        assert counter(state, "alpha") == 1

        metadata = await deployment.skills.list_metadata(scope="project-a")
        assert [item.skill_id for item in metadata] == ["mcp_guidance"]
        calls = facts_of(facts, "server.call_tool.start")
        assert [item["name"] for item in calls].count("echo") == 1
        assert [item["name"] for item in calls].count("bump") == 1
    finally:
        await deployment.aclose()
        assert deployment.close_report.ok


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("transport", TRANSPORTS)
async def test_mcp_extension_roundtrip_over_two_protocols_and_two_transports(
    tmp_path: Path, protocol: str, transport: str
) -> None:
    facts = tmp_path / "facts.jsonl"
    state = tmp_path / "state"
    state.mkdir()
    if transport == "stdio":
        config = mcp_stdio_config(
            facts,
            state,
            protocol=protocol,
            authorization_scope="authorized",
            config_revision="cfg-1",
        )
        await _run_roundtrip(config, facts, state)
    else:
        with http_server(facts, state) as url:
            config = mcp_http_config(
                url,
                protocol=protocol,
                authorization_scope="authorized",
                config_revision="cfg-1",
            )
            await _run_roundtrip(config, facts, state)


async def test_mcp_errors_catalog_drift_and_unknown_keep_their_semantics(tmp_path: Path) -> None:
    facts = tmp_path / "facts.jsonl"
    state = tmp_path / "state"
    state.mkdir()
    config = mcp_stdio_config(
        facts,
        state,
        protocol="2025-11-25",
        authorization_scope="authorized",
        config_revision="cfg-1",
    )

    pack = build_mcp_reference_pack(config=config)
    deployment = await open_extensions(pack.plan).open()
    try:
        gateway = ToolGateway(
            registry=deployment.registry,
            policy=ConservativePolicy(),
            approvals=InMemoryApprovalStore(),
            backend=RecordingExecutionBackend(),
        )
        failed = await gateway.invoke(
            _invocation(pack.explode, "call-explode"), _context("explode")
        )
        assert failed.status is InvocationStatus.FAILED
        assert "exploded" in failed.reason

        # The extension's combined guard must surface the adapter's own catalog check.
        pack.factory.manager.invalidate_catalog(
            pack.factory.connection, "the fixture catalog was refreshed"
        )
        denied = await gateway.invoke(
            _invocation(pack.echo, "call-echo-drift", text="x"), _context("drift")
        )
        assert denied.status is InvocationStatus.DENIED
        assert "catalog" in denied.reason.lower() or "invalidated" in denied.reason.lower()
    finally:
        await deployment.aclose()

    # A fresh deployment with a deliberately unprovable remote write: the effect is
    # recorded by the fixture, the answer never arrives, and the result is UNKNOWN.
    config = mcp_stdio_config(
        facts,
        state,
        protocol="2025-11-25",
        server_id="fixture-unk",
        authorization_scope="authorized",
        config_revision="cfg-1",
    )
    alias = local_alias(config.server_id, "bump_then_die")
    bindings = {
        "bump_then_die": McpToolBinding(
            remote_name="bump_then_die",
            effects=frozenset({ToolEffect.WRITE}),
            preview=lambda context, arguments: {"action": "bump", "key": arguments.get("key")},
        )
    }
    manifest = ExtensionManifest(
        id="yanxu.mcp-unknown",
        version="1.0.0",
        requires_capabilities=("mcp.tools.v1", "tools.v1"),
        contributes=ContributionDeclarations(tools=(alias,), skills=()),
    )
    binding = ExtensionBinding(
        extension_id="yanxu.mcp-unknown",
        namespace="project-a",
        authorization_scope="authorized",
        config_revision="cfg-1",
        policy_revision="pol-1",
        capabilities=frozenset({"mcp.tools.v1", "tools.v1"}),
        tools=(alias,),
        skills=(),
    )
    plan = plan_extensions(
        manifests=[manifest],
        factories={
            "yanxu.mcp-unknown": mcp_tool_factory(config=config, bindings=bindings)
        },
        bindings=[binding],
    )
    unknown_deployment = await open_extensions(plan).open()
    try:
        gateway = ToolGateway(
            registry=unknown_deployment.registry,
            policy=ConservativePolicy(auto_write=True),
            approvals=InMemoryApprovalStore(),
            backend=RecordingExecutionBackend(),
        )
        result = await gateway.invoke(
            _invocation(alias, "call-unknown", key="unknown-key"), _context("unknown")
        )
        assert result.status is InvocationStatus.UNKNOWN
        assert result.receipt is not None
        assert result.receipt.state is EffectState.UNKNOWN
        assert counter(state, "unknown-key") == 1, "the remote side did write once"
    finally:
        await unknown_deployment.aclose()
