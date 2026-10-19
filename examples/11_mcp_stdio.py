"""Example 11 — MCP over stdio, through the gateway, with no model and no credentials.

    python examples/11_mcp_stdio.py

The example starts a **real MCP server** (the SDK's own lowlevel server, written to a
temporary file and launched as a child process), discovers its tools, freezes a catalog
with an explicit host binding, and runs one read and one write call through the real
`ToolGateway` and a real SQLite ledger. Nothing here needs a model: the point is the
MCP boundary — validation, approval-free read, one admission per remote call, a receipt
in the single operation ledger.

Requires the optional extra: `pip install 'yanxu-harness[mcp]'`.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
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
    result_status,
)

SERVER_SOURCE = '''
"""A tiny real MCP server: echo (read) and bump (write to a file)."""
import json
import os
import sys

import anyio
import mcp_types as types
from mcp.server.lowlevel import Server
from mcp.server.models import InitializationOptions

STATE = os.environ["EXAMPLE_STATE"]


def bump(key: str) -> int:
    path = os.path.join(STATE, key)
    value = int(open(path).read()) if os.path.exists(path) else 0
    value += 1
    open(path, "w").write(str(value))
    return value


TOOLS = [
    types.Tool(
        name="echo",
        description="Echo the given text back.",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        annotations=types.ToolAnnotations(read_only_hint=True, idempotent_hint=True),
    ),
    types.Tool(
        name="bump",
        description="Increment a counter file.",
        input_schema={
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
            "additionalProperties": False,
        },
    ),
]


async def on_list_tools(ctx, params):
    return types.ListToolsResult(tools=TOOLS)


async def on_call_tool(ctx, params):
    args = params.arguments or {}
    if params.name == "echo":
        return types.CallToolResult(content=[types.TextContent(type="text", text=str(args["text"]))])
    if params.name == "bump":
        count = bump(str(args["key"]))
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"bumped {count}")],
            structured_content={"count": count},
        )
    return types.CallToolResult(content=[types.TextContent(type="text", text="unknown tool")], is_error=True)


async def main() -> None:
    server = Server("example-mcp", version="1.0.0", on_list_tools=on_list_tools, on_call_tool=on_call_tool)
    from mcp.server.stdio import stdio_server

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="example-mcp",
                server_version="1.0.0",
                capabilities=types.ServerCapabilities(tools=types.ToolsCapability(list_changed=False)),
            ),
        )


anyio.run(main)
'''


def context() -> RunContext:
    return RunContext(
        identity=RunIdentity(namespace="example", session_id="s1", run_id="r1", attempt_id="a1"),
        principal=Principal(subject="developer", permissions=frozenset({"example.write"})),
    )


async def main() -> int:
    with tempfile.TemporaryDirectory() as work:
        workdir = Path(work)
        server_script = workdir / "example_mcp_server.py"
        server_script.write_text(SERVER_SOURCE, encoding="utf-8")

        config = McpServerConfig(
            server_id="example",
            transport=McpTransport.STDIO,
            protocol="2025-11-25",
            authorization_scope="example-scope",
            command=sys.executable,
            args=(str(server_script),),
            env={"EXAMPLE_STATE": str(workdir)},
        )
        bindings = {
            "echo": McpToolBinding("echo", effects=frozenset({ToolEffect.READ})),
            "bump": McpToolBinding(
                "bump",
                effects=frozenset({ToolEffect.WRITE}),
                preview=lambda _ctx, args: {"key": args.get("key"), "effect": "increment"},
            ),
        }

        async with McpClientManager() as manager:
            connection = await manager.connect(config)
            snapshot = await discover_catalog(manager, connection, config=config, bindings=bindings)
            adapter = McpToolAdapter(
                manager=manager, connection=connection, config=config, snapshot=snapshot
            )
            definitions = adapter.definitions()
            gateway = ToolGateway(
                registry=ToolRegistry(list(definitions)),
                policy=ConservativePolicy(auto_write=True),
                approvals=InMemoryApprovalStore(),
                backend=SqliteExecutionBackend(workdir / "operations.sqlite"),
            )
            print(
                f"mcp-example: transport={config.transport.value} protocol={connection.protocol} "
                f"tools={len(snapshot.entries)} excluded={len(snapshot.exclusions)}"
            )
            for definition in definitions:
                entry = snapshot.entry(definition.name)
                spec = core_tool(definition, GatewayBindings(gateway=gateway, context=context()))
                arguments = (
                    {"text": "hello from MCP"} if entry.remote_name == "echo" else {"key": "example"}
                )
                projected = await spec.execute(f"call-{entry.remote_name}", arguments)
                status = result_status(projected)
                print(
                    f"mcp-example: call {entry.remote_name} "
                    f"status={status.value if status else '?'} is_error={projected['is_error']} "
                    f"content={projected['content'][0].text!r}"
                )

        print(f"mcp-example: counter={int((workdir / 'example').read_text())}")
        print("mcp-example: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
