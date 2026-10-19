#!/usr/bin/env python3
"""独立 MCP 验收探针：不依赖 pytest，也不依赖仓库里的测试夹具。

跑的是真东西：真子进程 stdio、真 loopback Streamable HTTP、真账本、真计数。
它自己写一个最小的 MCP 服务端（SDK 的 lowlevel server），把每一次 ``tools/call``
写进 facts 文件，所以「一次准入一次远端调用」是在服务端数出来的，不是客户端自述。

    python scripts/probe_mcp_acceptance.py --report /tmp/mcp-probe.json

退出码 0 表示所有场景通过；任何一条失败都会打印原因并返回 1。
需要可选 extra：``pip install 'yanxu-harness[mcp]'``。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

SERVER_SOURCE = '''
"""Probe-local MCP server: echo (read) and bump_then_die (write then exit)."""
import json
import os
import sys

import anyio
import mcp_types as types
from mcp.server.lowlevel import Server
from mcp.server.models import InitializationOptions

FACTS = os.environ["PROBE_FACTS"]
STATE = os.environ["PROBE_STATE"]


def fact(kind, **payload):
    with open(FACTS, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"kind": kind, **payload}, sort_keys=True) + "\\n")


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
        name="bump_then_die",
        description="Write the business record, then exit without answering.",
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
    fact("call", tool=params.name, protocol=ctx.protocol_version)
    if params.name == "echo":
        return types.CallToolResult(content=[types.TextContent(type="text", text=str(args.get("text", "")))])
    if params.name == "bump_then_die":
        key = str(args.get("key", "default"))
        path = os.path.join(STATE, key)
        value = int(open(path).read()) if os.path.exists(path) else 0
        open(path, "w").write(str(value + 1))
        fact("business_written", key=key, count=value + 1)
        sys.stderr.flush()
        os._exit(7)
    return types.CallToolResult(content=[types.TextContent(type="text", text="unknown")], is_error=True)


def build():
    return Server("probe", version="1.0.0", on_list_tools=on_list_tools, on_call_tool=on_call_tools)


async def on_call_tools(ctx, params):  # pragma: no cover - alias kept for readability
    return await on_call_tool(ctx, params)


def main(argv):
    transport = argv[1] if len(argv) > 1 else "stdio"
    server = Server("probe", version="1.0.0", on_list_tools=on_list_tools, on_call_tool=on_call_tool)
    if transport == "stdio":
        from mcp.server.stdio import stdio_server

        async def run():
            async with stdio_server() as (read_stream, write_stream):
                await server.run(
                    read_stream,
                    write_stream,
                    InitializationOptions(
                        server_name="probe",
                        server_version="1.0.0",
                        capabilities=types.ServerCapabilities(tools=types.ToolsCapability(list_changed=False)),
                    ),
                )

        anyio.run(run)
        return
    import uvicorn

    app = server.streamable_http_app(streamable_http_path="/mcp", host="127.0.0.1")
    uvicorn.run(app, host="127.0.0.1", port=int(argv[2]), log_level="warning")


main(sys.argv)
'''


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_port(port: int, *, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.25)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.02)
    raise TimeoutError(f"the probe server never listened on {port}")


def facts(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def calls(path: Path, tool: str) -> int:
    return len([row for row in facts(path) if row.get("kind") == "call" and row.get("tool") == tool])


class Scenario:
    def __init__(self, name: str) -> None:
        self.name = name
        self.ok = True
        self.detail: dict[str, object] = {}
        self.failures: list[str] = []

    def check(self, condition: bool, message: str) -> None:
        if not condition:
            self.ok = False
            self.failures.append(message)

    def as_dict(self) -> dict:
        return {"scenario": self.name, "ok": self.ok, "detail": self.detail, "failures": self.failures}


async def run_grid_cell(
    server_script: Path, workdir: Path, protocol: str, transport: str, report: dict
) -> Scenario:
    from yanxu_harness.integrations.mcp import (
        McpClientManager,
        McpServerConfig,
        McpTransport,
        discover_catalog,
        McpToolBinding,
    )
    from yanxu_harness.tools import ToolEffect

    scenario = Scenario(f"grid:{transport}:{protocol}")
    facts_path = workdir / f"facts-{transport}-{protocol}.jsonl"
    state = workdir / f"state-{transport}-{protocol}"
    state.mkdir(parents=True, exist_ok=True)
    environment = {
        "PROBE_FACTS": str(facts_path),
        "PROBE_STATE": str(state),
        "PYTHONIOENCODING": "utf-8",
    }
    process = None
    url = None
    if transport == "stdio":
        config = McpServerConfig(
            server_id="probe",
            transport=McpTransport.STDIO,
            protocol=protocol,
            authorization_scope="probe",
            command=sys.executable,
            args=(str(server_script), "stdio"),
            env=environment,
        )
    else:
        port = free_port()
        process = subprocess.Popen(
            [sys.executable, str(server_script), "http", str(port)],
            env={**os.environ, **environment},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        wait_for_port(port)
        url = f"http://127.0.0.1:{port}/mcp"
        config = McpServerConfig(
            server_id="probe",
            transport=McpTransport.STREAMABLE_HTTP,
            protocol=protocol,
            authorization_scope="probe",
            url=url,
            allow_loopback_http=True,
        )
    try:
        async with McpClientManager() as manager:
            connection = await manager.connect(config)
            snapshot = await discover_catalog(
                manager,
                connection,
                config=config,
                bindings={"echo": McpToolBinding("echo", effects=frozenset({ToolEffect.READ}))},
            )
            answer = await manager.call_tool(connection, "echo", {"text": f"{transport}/{protocol}"})
            scenario.detail = {
                "protocol": connection.protocol,
                "transport": connection.transport.value,
                "tools": len(snapshot.entries),
                "answer": list(answer.text),
                "server_side_calls": calls(facts_path, "echo"),
            }
            scenario.check(connection.protocol == protocol, f"negotiated {connection.protocol}")
            scenario.check(answer.text == (f"{transport}/{protocol}",), "the answer text came back")
            scenario.check(calls(facts_path, "echo") == 1, "exactly one server-side execution")
    except Exception as exc:  # noqa: BLE001 - the probe reports, it does not hide
        scenario.check(False, f"{type(exc).__name__}: {exc}")
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover
                process.kill()
                process.wait(timeout=10)
    return scenario


async def run_unknown_window(server_script: Path, workdir: Path) -> Scenario:
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
        ToolInvocation,
        ToolRegistry,
        core_tool,
        result_status,
    )

    scenario = Scenario("unknown_window")
    facts_path = workdir / "facts-unknown.jsonl"
    state = workdir / "state-unknown"
    state.mkdir(parents=True, exist_ok=True)
    config = McpServerConfig(
        server_id="probe",
        transport=McpTransport.STDIO,
        protocol="2025-11-25",
        authorization_scope="probe",
        command=sys.executable,
        args=(str(server_script), "stdio"),
        env={"PROBE_FACTS": str(facts_path), "PROBE_STATE": str(state), "PYTHONIOENCODING": "utf-8"},
    )
    binding = McpToolBinding(
        "bump_then_die",
        effects=frozenset({ToolEffect.WRITE}),
        preview=lambda _ctx, args: {"key": args.get("key")},
    )
    context = RunContext(
        identity=RunIdentity(namespace="probe", session_id="s1", run_id="r1", attempt_id="a1"),
        principal=Principal(subject="probe", permissions=frozenset({"probe.write"})),
    )
    try:
        async with McpClientManager() as manager:
            connection = await manager.connect(config)
            snapshot = await discover_catalog(manager, connection, config=config, bindings={"bump_then_die": binding})
            adapter = McpToolAdapter(manager=manager, connection=connection, config=config, snapshot=snapshot)
            backend = SqliteExecutionBackend(workdir / "probe-ledger.sqlite")
            gateway = ToolGateway(
                registry=ToolRegistry(list(adapter.definitions())),
                policy=ConservativePolicy(auto_write=True),
                approvals=InMemoryApprovalStore(),
                backend=backend,
            )
            projected = await core_tool(
                adapter.definitions()[0], GatewayBindings(gateway=gateway, context=context)
            ).execute("call-1", {"key": "probe"})
            status = result_status(projected)
            business = int((state / "probe").read_text())
            entry = snapshot.entries[0]
            invocation = ToolInvocation(
                call_id="call-1",
                namespace="probe",
                tool_name=entry.alias,
                arguments={"key": "probe"},
                session_id="s1",
                run_id="r1",
            )
            reviewed = await gateway.review(invocation, context)
            again = await gateway.invoke(invocation, context)
            scenario.detail = {
                "status": status.value if status else None,
                "business_file": business,
                "ledger_state": backend.all_operations()[0]["state"],
                "review": reviewed.status.value if reviewed else None,
                "second_attempt": again.status.value,
                "server_side_calls": calls(facts_path, "bump_then_die"),
            }
            scenario.check(status is not None and status.value == "unknown", "the call is unknown")
            scenario.check(business == 1, "the business effect happened exactly once")
            scenario.check(backend.all_operations()[0]["state"] == "unknown", "the ledger row is unknown")
            scenario.check(calls(facts_path, "bump_then_die") == 1, "no replay after the unknown")
    except Exception as exc:  # noqa: BLE001
        scenario.check(False, f"{type(exc).__name__}: {exc}")
    return scenario


def run_unknown_protocol_refused() -> Scenario:
    from yanxu_harness.integrations.mcp import McpConfigurationError, McpServerConfig, McpTransport

    scenario = Scenario("unknown_protocol_refused")
    try:
        McpServerConfig(
            server_id="probe",
            transport=McpTransport.STDIO,
            protocol="2024-11-05",
            authorization_scope="probe",
            command=sys.executable,
        )
        scenario.check(False, "an unverified revision was accepted")
    except McpConfigurationError as exc:
        scenario.detail = {"reason": str(exc)[:200]}
        scenario.check("pinned" in str(exc), "the refusal names the pinned revisions")
    return scenario


def run_import_isolation() -> Scenario:
    scenario = Scenario("import_isolation")
    script = (
        "import importlib, json, pkgutil, sys;"
        "sys.path.insert(0, %r);"
        "import yanxu_harness;"
        "[importlib.import_module(m.name) for m in pkgutil.walk_packages(yanxu_harness.__path__, 'yanxu_harness.')];"
        "print(json.dumps(sorted({n.split('.')[0] for n in sys.modules "
        "if n.split('.')[0] in {'mcp', 'mcp_types', 'anyio', 'httpx2', 'jsonschema', 'starlette', 'uvicorn'}})))"
    ) % (str((Path(__file__).resolve().parents[1] / "src")),)
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False)
    scenario.detail = {"exit": result.returncode, "leaked": result.stdout.strip()}
    scenario.check(result.returncode == 0, "the cold import succeeded")
    scenario.check(result.stdout.strip().splitlines()[-1] == "[]", "no SDK dependency was imported")
    return scenario


def run_sdk_facts() -> Scenario:
    scenario = Scenario("sdk_facts")
    try:
        import importlib.metadata as metadata

        import mcp_types.version as version

        scenario.detail = {
            "mcp": metadata.version("mcp"),
            "mcp_types": metadata.version("mcp-types"),
            "jsonschema": metadata.version("jsonschema"),
            "latest_handshake": version.LATEST_HANDSHAKE_VERSION,
            "modern": list(version.MODERN_PROTOCOL_VERSIONS),
        }
        scenario.check(metadata.version("mcp") == "2.3.0", "mcp is pinned to 2.3.0")
        scenario.check(version.LATEST_HANDSHAKE_VERSION == "2025-11-25", "handshake revision")
        scenario.check(version.MODERN_PROTOCOL_VERSIONS == ("2026-07-28",), "modern revision")
    except Exception as exc:  # noqa: BLE001
        scenario.check(False, f"{type(exc).__name__}: {exc}")
    return scenario


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", default="", help="where to write the JSON report")
    args = parser.parse_args()

    scenarios: list[Scenario] = [run_sdk_facts(), run_import_isolation(), run_unknown_protocol_refused()]
    with tempfile.TemporaryDirectory() as work:
        workdir = Path(work)
        server_script = workdir / "probe_server.py"
        server_script.write_text(SERVER_SOURCE, encoding="utf-8")
        for transport in ("stdio", "http"):
            for protocol in ("2025-11-25", "2026-07-28"):
                scenarios.append(await run_grid_cell(server_script, workdir, protocol, transport, {}))

    # A fresh server for the crash window: its process is expected to die mid-call.
    with tempfile.TemporaryDirectory() as work:
        workdir = Path(work)
        server_script = workdir / "probe_server.py"
        server_script.write_text(SERVER_SOURCE, encoding="utf-8")
        scenarios.append(await run_unknown_window(server_script, workdir))

    report = {
        "probe": "mcp_acceptance",
        "run_id": uuid.uuid4().hex,
        "python": sys.version.split()[0],
        "scenarios": [scenario.as_dict() for scenario in scenarios],
        "ok": all(scenario.ok for scenario in scenarios),
    }
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.report:
        Path(args.report).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    for scenario in scenarios:
        mark = "ok  " if scenario.ok else "FAIL"
        print(f"{mark} {scenario.name} {json.dumps(scenario.detail, sort_keys=True)}", file=sys.stderr)
        for failure in scenario.failures:
            print(f"     - {failure}", file=sys.stderr)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
