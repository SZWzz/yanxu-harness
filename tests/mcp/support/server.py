"""真实 MCP 服务夹具：一个低层 MCP server，同时服务两代协议。

这个模块是**被测试调用的服务端**，不是被测代码。它必须：

* 说真实的 MCP 协议（用 SDK 自己的 lowlevel server 和传输），不是替身；
* 把每一个收到的请求写进 facts JSONL，便于「协议请求计数」与「业务执行计数」分开断言；
* 把副作用写进真实文件，便于断线重连后核对远端业务事实；
* 两种传输共用同一套工具：`--transport stdio` 与 `--transport http --port N`。

facts 文件路径由 `MCP_FIXTURE_FACTS` 给出，业务状态目录由 `MCP_FIXTURE_STATE` 给出。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

import anyio
import mcp_types as types
from mcp.server.lowlevel import Server
from mcp.server.models import InitializationOptions

FACTS_ENV = "MCP_FIXTURE_FACTS"
STATE_ENV = "MCP_FIXTURE_STATE"

_FACTS_LOCK = threading.Lock()


def facts_path() -> Path:
    return Path(os.environ[FACTS_ENV])


def state_dir() -> Path:
    path = Path(os.environ[STATE_ENV])
    path.mkdir(parents=True, exist_ok=True)
    return path


def fact(kind: str, **payload: Any) -> None:
    """Append one fact line. Locked: HTTP handlers run on many threads."""
    line = json.dumps({"kind": kind, **payload}, sort_keys=True, ensure_ascii=False)
    with _FACTS_LOCK:
        with facts_path().open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def read_counter(name: str) -> int:
    path = state_dir() / name
    if not path.exists():
        return 0
    return int(path.read_text(encoding="utf-8").strip() or "0")


def bump_counter(name: str) -> int:
    value = read_counter(name) + 1
    (state_dir() / name).write_text(str(value), encoding="utf-8")
    # A second file makes "the side effect happened" checkable even when the
    # caller only knows the counter's final value.
    with (state_dir() / f"{name}.log").open("a", encoding="utf-8") as handle:
        handle.write(f"{value}\n")
    return value


HEADERBOUND_SCHEMA = {
    "type": "object",
    "properties": {
        "tenant": {"type": "string", "x-mcp-header": "Tenant"},
        "note": {"type": "string"},
    },
    "required": ["tenant"],
    "additionalProperties": False,
}

TOOLS: dict[str, types.Tool] = {
    "echo": types.Tool(
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
    "bump": types.Tool(
        name="bump",
        description="Increment a persisted counter; a real side effect.",
        input_schema={
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
            "additionalProperties": False,
        },
    ),
    "explode": types.Tool(
        name="explode",
        description="Return a complete terminal remote error.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        annotations=types.ToolAnnotations(read_only_hint=True),
    ),
    "headerbound": types.Tool(
        name="headerbound",
        description="A tool whose tenant argument is mirrored into an Mcp-Param header.",
        input_schema=HEADERBOUND_SCHEMA,
        annotations=types.ToolAnnotations(read_only_hint=True),
    ),
    "structured_only": types.Tool(
        name="structured_only",
        description="Return structured content with no text content.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        annotations=types.ToolAnnotations(read_only_hint=True),
    ),
    "empty": types.Tool(
        name="empty",
        description="A legal success with no content at all.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        annotations=types.ToolAnnotations(read_only_hint=True),
    ),
    "big": types.Tool(
        name="big",
        description="Return a large text body (for size-limit tests).",
        input_schema={
            "type": "object",
            "properties": {"chars": {"type": "integer"}},
            "required": ["chars"],
            "additionalProperties": False,
        },
        annotations=types.ToolAnnotations(read_only_hint=True),
    ),
    "slow": types.Tool(
        name="slow",
        description="Sleep, then answer (for deadline/cancellation tests).",
        input_schema={
            "type": "object",
            "properties": {"seconds": {"type": "number"}},
            "required": ["seconds"],
            "additionalProperties": False,
        },
        annotations=types.ToolAnnotations(read_only_hint=True),
    ),
}

TOOL_LIST: list[types.Tool] = list(TOOLS.values())


async def on_list_tools(ctx, params):
    cursor = getattr(params, "cursor", None) if params is not None else None
    fact("server.list_tools", protocol=ctx.protocol_version, cursor=cursor)
    return types.ListToolsResult(tools=TOOL_LIST)


def get_input_schema(name: str):
    tool = TOOLS.get(name)
    return tool.input_schema if tool is not None else None


async def on_call_tool(ctx, params):
    name = params.name
    args = dict(params.arguments or {})
    protocol = ctx.protocol_version
    fact("server.call_tool.start", name=name, arguments=args, protocol=protocol)
    try:
        result = await _dispatch(name, args)
    except Exception as exc:  # noqa: BLE001 - a fixture crash is a fact, not a traceback
        fact("server.call_tool.raised", name=name, error=type(exc).__name__)
        raise
    fact("server.call_tool.end", name=name, is_error=bool(result.is_error) if hasattr(result, "is_error") else None)
    return result


async def _dispatch(name: str, args: dict[str, Any]) -> types.CallToolResult:
    if name == "echo":
        return types.CallToolResult(content=[types.TextContent(type="text", text=str(args.get("text", "")))])
    if name == "bump":
        count = bump_counter(str(args.get("key", "default")))
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"bumped {count}")],
            structured_content={"count": count},
        )
    if name == "explode":
        return types.CallToolResult(
            content=[types.TextContent(type="text", text="remote failure: exploded")],
            is_error=True,
        )
    if name == "headerbound":
        return types.CallToolResult(content=[types.TextContent(type="text", text=f"tenant={args.get('tenant')}")])
    if name == "structured_only":
        return types.CallToolResult(structured_content={"ok": True, "values": [1, 2, 3]})
    if name == "empty":
        return types.CallToolResult(content=[])
    if name == "big":
        return types.CallToolResult(content=[types.TextContent(type="text", text="x" * int(args.get("chars", 0)))])
    if name == "slow":
        await anyio.sleep(float(args.get("seconds", 0)))
        return types.CallToolResult(content=[types.TextContent(type="text", text="slept")])
    return types.CallToolResult(content=[types.TextContent(type="text", text=f"unknown tool {name}")], is_error=True)


def build_server() -> Server[Any]:
    fact("server.build", pid=os.getpid(), python=sys.executable)
    return Server(
        "yanxu-mcp-fixture",
        version="1.0.0",
        instructions="Fixture server for the yanxu-harness MCP client tests.",
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
        get_tool_input_schema=get_input_schema,
    )


async def run_stdio() -> None:
    from mcp.server.stdio import stdio_server

    server = build_server()
    fact("server.start", transport="stdio", pid=os.getpid())
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="yanxu-mcp-fixture",
                server_version="1.0.0",
                capabilities=types.ServerCapabilities(tools=types.ToolsCapability(list_changed=False)),
            ),
        )
    fact("server.stop", transport="stdio", pid=os.getpid())


def run_http(host: str, port: int) -> None:
    import uvicorn

    server = build_server()
    app = server.streamable_http_app(streamable_http_path="/mcp", host=host, json_response=False)
    fact("server.start", transport="http", pid=os.getpid(), host=host, port=port)
    uvicorn.run(app, host=host, port=port, log_level="warning")
    fact("server.stop", transport="http", pid=os.getpid())


def main(argv: list[str]) -> int:
    transport = "stdio"
    host, port = "127.0.0.1", 0
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--transport":
            transport = argv[index + 1]
            index += 2
        elif arg == "--host":
            host = argv[index + 1]
            index += 2
        elif arg == "--port":
            port = int(argv[index + 1])
            index += 2
        else:  # pragma: no cover - fixture misuse
            print(f"unknown argument: {arg}", file=sys.stderr)
            return 2
    if transport == "stdio":
        anyio.run(run_stdio)
    elif transport == "http":
        run_http(host, port)
    else:  # pragma: no cover - fixture misuse
        print(f"unknown transport: {transport}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
