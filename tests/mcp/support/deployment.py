"""A reusable real deployment for the MCP recovery and diagnostics tests.

Same shape as the gateway tests: a real MCP subprocess, a real catalog, the real
gateway and a real SQLite ledger. Kept in one place so the fault-window tests cannot
drift into using a fake ledger by accident.
"""

from __future__ import annotations

from typing import Any, Mapping

from yanxu_harness.integrations.mcp import (
    McpClientManager,
    McpToolAdapter,
    discover_catalog,
)
from yanxu_harness.storage import SqliteExecutionBackend
from yanxu_harness.tools import (
    ConservativePolicy,
    InMemoryApprovalStore,
    ToolGateway,
    ToolRegistry,
)

from support.harness import mcp_stdio_config


async def build_deployment(
    tmp_path,
    bindings: Mapping[str, Any],
    *,
    policy=None,
    limits=None,
    approvals=None,
    extra_env: dict[str, str] | None = None,
    ledger: str = "ledger.sqlite",
    authorization_scope: str = "scope-a",
    run_id: str = "r1",
):
    facts, state = tmp_path / "facts.jsonl", tmp_path / "state"
    manager = McpClientManager()
    await manager.__aenter__()
    config = mcp_stdio_config(
        facts,
        state,
        limits=limits,
        extra_env=extra_env,
        authorization_scope=authorization_scope,
    )
    connection = await manager.connect(config)
    snapshot = await discover_catalog(manager, connection, config=config, bindings=bindings)
    adapter = McpToolAdapter(manager=manager, connection=connection, config=config, snapshot=snapshot)
    definitions = adapter.definitions()
    backend = SqliteExecutionBackend(tmp_path / ledger)
    approvals = approvals or InMemoryApprovalStore()
    gateway = ToolGateway(
        registry=ToolRegistry(list(definitions)),
        policy=policy or ConservativePolicy(auto_write=True),
        approvals=approvals,
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
        "run_id": run_id,
    }
