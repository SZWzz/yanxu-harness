"""Example 12 — MCP over loopback Streamable HTTP, and the unknown window.

    python examples/12_mcp_http_recovery.py

The server here is again a real one (SDK lowlevel server, Streamable HTTP, loopback),
but this time the bound tool writes its business record and then exits **without
answering** — the exact window the operation ledger exists for. The example shows the
whole recovery story with no model and no credentials:

* the call is UNKNOWN, not FAILED (the request was dispatched);
* the remote business file proves what actually happened;
* the ledger is promoted with that evidence, and the promoted receipt is reused
  instead of being re-dispatched.

Requires the optional extra: `pip install 'yanxu-harness[mcp]'`.
"""

from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
import tempfile
import time
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
    EffectState,
    GatewayBindings,
    InMemoryApprovalStore,
    ToolEffect,
    ToolGateway,
    ToolInvocation,
    ToolRegistry,
    core_tool,
    result_status,
)

SERVER_SOURCE = '''
"""A real Streamable HTTP MCP server whose write tool dies after writing."""
import json
import os
import sys

import mcp_types as types
from mcp.server.lowlevel import Server
from mcp.server.models import InitializationOptions

STATE = os.environ["EXAMPLE_STATE"]
TOOL = types.Tool(
    name="bump_local",
    description="Write the business record, then exit without answering.",
    input_schema={
        "type": "object",
        "properties": {"key": {"type": "string"}},
        "required": ["key"],
        "additionalProperties": False,
    },
)


async def on_list_tools(ctx, params):
    return types.ListToolsResult(tools=[TOOL])


async def on_call_tool(ctx, params):
    key = str((params.arguments or {}).get("key", "default"))
    path = os.path.join(STATE, key)
    value = int(open(path).read()) if os.path.exists(path) else 0
    open(path, "w").write(str(value + 1))
    sys.stderr.flush()
    os._exit(7)  # the effect happened; the answer never does


def main() -> None:
    import uvicorn

    server = Server("example-http", version="1.0.0", on_list_tools=on_list_tools, on_call_tool=on_call_tool)
    app = server.streamable_http_app(streamable_http_path="/mcp", host="127.0.0.1")
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")


main()
'''


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_port(port: int, *, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.25)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.02)
    raise TimeoutError(f"the example server did not listen on {port}")


def context() -> RunContext:
    return RunContext(
        identity=RunIdentity(namespace="example", session_id="s1", run_id="r1", attempt_id="a1"),
        principal=Principal(subject="developer", permissions=frozenset({"example.write"})),
    )


async def main() -> int:
    with tempfile.TemporaryDirectory() as work:
        workdir = Path(work)
        server_script = workdir / "example_http_server.py"
        server_script.write_text(SERVER_SOURCE, encoding="utf-8")
        port = free_port()
        env = {**dict(__import__("os").environ), "EXAMPLE_STATE": str(workdir), "PYTHONIOENCODING": "utf-8"}
        process = subprocess.Popen(
            [sys.executable, str(server_script), str(port)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        backend: SqliteExecutionBackend | None = None
        try:
            wait_for_port(port)
            config = McpServerConfig(
                server_id="example-http",
                transport=McpTransport.STREAMABLE_HTTP,
                protocol="2025-11-25",
                authorization_scope="example-scope",
                url=f"http://127.0.0.1:{port}/mcp",
                allow_loopback_http=True,
            )
            binding = McpToolBinding(
                "bump_local",
                effects=frozenset({ToolEffect.WRITE}),
                preview=lambda _ctx, args: {"key": args.get("key"), "effect": "increment"},
            )
            ledger_path = workdir / "operations.sqlite"

            async with McpClientManager() as manager:
                connection = await manager.connect(config)
                snapshot = await discover_catalog(
                    manager, connection, config=config, bindings={"bump_local": binding}
                )
                adapter = McpToolAdapter(
                    manager=manager, connection=connection, config=config, snapshot=snapshot
                )
                backend = SqliteExecutionBackend(ledger_path)
                gateway = ToolGateway(
                    registry=ToolRegistry(list(adapter.definitions())),
                    policy=ConservativePolicy(auto_write=True),
                    approvals=InMemoryApprovalStore(),
                    backend=backend,
                )
                print(
                    f"mcp-example-http: transport={config.transport.value} "
                    f"protocol={connection.protocol} tools={len(snapshot.entries)}"
                )
                projected = await core_tool(
                    adapter.definitions()[0], GatewayBindings(gateway=gateway, context=context())
                ).execute("call-1", {"key": "business"})
                print(
                    f"mcp-example-http: dispatched call status="
                    f"{result_status(projected).value} business_file={int((workdir / 'business').read_text())}"
                )

            entry = snapshot.entries[0]
            invocation = ToolInvocation(
                call_id="call-1",
                namespace="example",
                tool_name=entry.alias,
                arguments={"key": "business"},
                session_id="s1",
                run_id="r1",
            )
            reviewed = await gateway.review(invocation, context())
            print(f"mcp-example-http: ledger review={reviewed.status.value if reviewed else 'absent'}")

            promoted = await gateway.promote(
                invocation,
                {
                    "state": "succeeded",
                    "source": "remote business file",
                    "detail": "the counter file shows exactly one increment",
                },
            )
            print(
                f"mcp-example-http: promoted state="
                f"{promoted.state.value if promoted else 'unsupported'}"
            )
            replayed = await gateway.review(invocation, context())
            print(
                f"mcp-example-http: after promotion review="
                f"{replayed.status.value if replayed else 'absent'} "
                f"receipt={replayed.receipt.state.value if replayed and replayed.receipt else '?'}"
            )
            rows = backend.all_operations()
            print(f"mcp-example-http: ledger rows={len(rows)} state={rows[0]['state']}")
            print("mcp-example-http: ok")
        finally:
            if backend is not None:
                backend.close()
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover - the server refused to die
                process.kill()
                process.wait(timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
