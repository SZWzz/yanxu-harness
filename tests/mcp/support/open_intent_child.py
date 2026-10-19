#!/usr/bin/env python3
"""Child process for AC21: record a real MCP intent, then die before dispatch.

The gateway admits through a durable SQLite ledger; the subclass exits *after* that
transaction commits and *before* the handler runs. A second process then has to see
an open row (not "absent") and no remote ``tools/call``.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from yanxu_harness.integrations.mcp import (
    McpClientManager,
    McpServerConfig,
    McpToolAdapter,
    McpToolBinding,
    McpTransport,
    discover_catalog,
)
from yanxu_harness.runtime import Principal, RunContext, RunIdentity
from yanxu_harness.storage import SqliteExecutionBackend
from yanxu_harness.tools import (
    ConservativePolicy,
    GatewayBindings,
    InMemoryApprovalStore,
    ToolEffect,
    ToolGateway,
    ToolRegistry,
    core_tool,
)

HERE = Path(__file__).resolve().parent


class ExitAfterAdmit(SqliteExecutionBackend):
    """Commit the intent, then die exactly in the window AC21 is about."""

    async def admit(self, intent):  # type: ignore[override]
        await super().admit(intent)
        sys.stdout.flush()
        os._exit(9)


def _config(directory: Path) -> McpServerConfig:
    return McpServerConfig(
        server_id="fixture",
        transport=McpTransport.STDIO,
        protocol="2025-11-25",
        authorization_scope="scope-a",
        command=sys.executable,
        args=(str(HERE / "server.py"), "--transport", "stdio"),
        cwd=str(HERE),
        env={
            "MCP_FIXTURE_FACTS": str(directory / "facts.jsonl"),
            "MCP_FIXTURE_STATE": str(directory / "state"),
        },
    )


def _context() -> RunContext:
    return RunContext(
        identity=RunIdentity(
            namespace="project-a", session_id="s1", run_id="r1", attempt_id="a1"
        ),
        principal=Principal(subject="author"),
    )


async def main(directory: Path) -> None:
    manager = McpClientManager()
    await manager.__aenter__()
    config = _config(directory)
    connection = await manager.connect(config)
    snapshot = await discover_catalog(
        manager,
        connection,
        config=config,
        bindings={
            "echo": McpToolBinding("echo", effects=frozenset({ToolEffect.READ}))
        },
    )
    adapter = McpToolAdapter(
        manager=manager, connection=connection, config=config, snapshot=snapshot
    )
    definition = adapter.definitions()[0]
    backend = ExitAfterAdmit(directory / "operations.db")
    gateway = ToolGateway(
        registry=ToolRegistry([definition]),
        policy=ConservativePolicy(),
        approvals=InMemoryApprovalStore(),
        backend=backend,
    )
    await core_tool(
        definition, GatewayBindings(gateway=gateway, context=_context())
    ).execute("call-1", {"text": "must not be sent"})


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1])))
