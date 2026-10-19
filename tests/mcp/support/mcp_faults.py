"""A real MCP deployment that a parent test and a child process can both build.

The failure windows in the independent review are about processes: a client that dies
after the intent, after a receipt, or after a promotion. Each child therefore has to
construct *the same* deployment (same server id, tool schema, description, system
prompt and run identity) as the process that recovers it, or the harness's arrangement
check would be comparing two different deployments.

This module is imported by the fault child script, not by the parent test, so the
fixture server paths and the package source are resolved once, explicitly.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from yanxu_harness.core.types import AssistantMessage, StopReason, TextContent, ToolCall
from yanxu_harness.integrations.mcp import (
    McpClientManager,
    McpServerConfig,
    McpToolAdapter,
    McpToolBinding,
    McpTransport,
    discover_catalog,
    local_alias,
)
from yanxu_harness.runtime import (
    DefaultHarness,
    Principal,
    RunContext,
    RunIdentity,
)
from yanxu_harness.storage import SqliteExecutionBackend, SqlitePersistence
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
RUN_ID = "r1"
NAMESPACE = "project-a"
SESSION_ID = "s1"
ALIAS_ECHO = local_alias("fixture", "echo")
ALIAS_BUMP = local_alias("fixture", "bump_then_die")


@dataclass(frozen=True)
class Paths:
    directory: Path

    @property
    def facts(self) -> Path:
        return self.directory / "facts.jsonl"

    @property
    def state(self) -> Path:
        return self.directory / "state"

    @property
    def runs(self) -> Path:
        return self.directory / "runs.db"

    @property
    def operations(self) -> Path:
        return self.directory / "operations.db"

    @property
    def counter(self) -> Path:
        return self.state / "k"


def config(paths: Paths) -> McpServerConfig:
    return McpServerConfig(
        server_id="fixture",
        transport=McpTransport.STDIO,
        protocol="2025-11-25",
        authorization_scope="scope-a",
        command=sys.executable,
        args=(str(HERE / "server.py"), "--transport", "stdio"),
        cwd=str(HERE),
        env={
            "MCP_FIXTURE_FACTS": str(paths.facts),
            "MCP_FIXTURE_STATE": str(paths.state),
        },
    )


def context(attempt: str = "a1") -> RunContext:
    return RunContext(
        identity=RunIdentity(
            namespace=NAMESPACE, session_id=SESSION_ID, run_id=RUN_ID, attempt_id=attempt
        ),
        principal=Principal(subject="author"),
    )


def tool_call(call_id: str, name: str, arguments: Mapping[str, Any]) -> AssistantMessage:
    return AssistantMessage(
        content=[ToolCall(id=call_id, name=name, arguments=dict(arguments))],
        stop_reason=StopReason.TOOL_USE,
    )


def batch_call(call_ids_and_names: list[tuple[str, str, Mapping[str, Any]]]) -> AssistantMessage:
    return AssistantMessage(
        content=[
            ToolCall(id=call_id, name=name, arguments=dict(arguments))
            for call_id, name, arguments in call_ids_and_names
        ],
        stop_reason=StopReason.TOOL_USE,
    )


def final(text: str = "done") -> AssistantMessage:
    return AssistantMessage(content=[TextContent(text=text)], stop_reason=StopReason.STOP)


class ScriptedStream:
    """One scripted provider; the last message repeats so a loop is visible."""

    def __init__(self, *messages: AssistantMessage) -> None:
        if not messages:
            raise ValueError("a stream needs at least one message")
        self.messages = list(messages)
        self.calls = 0

    async def __call__(self, model: Any, model_context: Any, options: Any = None):
        self.calls += 1
        self._final = self.messages[min(self.calls - 1, len(self.messages) - 1)]
        return self

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def result(self) -> AssistantMessage:
        return self._final


async def build(
    paths: Paths,
    stream: Any,
    *,
    bindings: Mapping[str, McpToolBinding],
    attempt: str = "a1",
    persistence: Any = None,
    ledger: Any = None,
    policy: Any = None,
    clock: Any = None,
) -> dict[str, Any]:
    """Build one real MCP deployment. The caller owns closing the returned manager/store."""
    manager = McpClientManager()
    await manager.__aenter__()
    cfg = config(paths)
    connection = await manager.connect(cfg)
    snapshot = await discover_catalog(manager, connection, config=cfg, bindings=bindings)
    adapter = McpToolAdapter(
        manager=manager, connection=connection, config=cfg, snapshot=snapshot
    )
    definitions = adapter.definitions()
    ledger = ledger if ledger is not None else SqliteExecutionBackend(paths.operations)
    approvals = InMemoryApprovalStore()
    gateway = ToolGateway(
        registry=ToolRegistry(list(definitions)),
        policy=policy or ConservativePolicy(auto_write=True),
        approvals=approvals,
        backend=ledger,
    )
    specs = [
        core_tool(
            definition,
            GatewayBindings(gateway=gateway, context=lambda: context(attempt)),
        )
        for definition in definitions
    ]
    store = persistence if persistence is not None else SqlitePersistence(paths.runs)
    harness = DefaultHarness(
        stream_fn=stream,
        tools=specs,
        persistence=store,
        system_prompt="be brief",
        clock=clock if clock is not None else time.time,
    )
    return {
        "manager": manager,
        "connection": connection,
        "config": cfg,
        "snapshot": snapshot,
        "adapter": adapter,
        "definitions": definitions,
        "gateway": gateway,
        "ledger": ledger,
        "store": store,
        "harness": harness,
        "approvals": approvals,
    }


def definitions_by_remote(deploy: Mapping[str, Any]) -> dict[str, Any]:
    return {entry.remote_name: entry for entry in deploy["snapshot"].entries}
