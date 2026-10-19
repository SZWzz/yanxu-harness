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
    "hold": types.Tool(
        name="hold",
        description="Hold a concurrency slot for a while, recording peak overlap.",
        input_schema={
            "type": "object",
            "properties": {"seconds": {"type": "number"}},
            "required": ["seconds"],
            "additionalProperties": False,
        },
        annotations=types.ToolAnnotations(read_only_hint=True),
    ),
    "die": types.Tool(
        name="die",
        description="Exit the server process immediately, mid-call.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    "bump_then_die": types.Tool(
        name="bump_then_die",
        description="Write the business record, then die without answering.",
        input_schema={
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
            "additionalProperties": False,
        },
    ),
    "noisy": types.Tool(
        name="noisy",
        description="Write to stderr, then answer (bounded-stderr tests).",
        input_schema={
            "type": "object",
            "properties": {"bytes": {"type": "integer"}},
            "required": ["bytes"],
            "additionalProperties": False,
        },
        annotations=types.ToolAnnotations(read_only_hint=True),
    ),
    "image": types.Tool(
        name="image",
        description="Return an image content block, which this release does not represent.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        annotations=types.ToolAnnotations(read_only_hint=True),
    ),
}

TOOL_LIST: list[types.Tool] = list(TOOLS.values())

# -- optional edge-case tools (only when MCP_FIXTURE_EDGE_TOOLS=1) -----------

PAGE_SIZE = int(os.environ.get("MCP_FIXTURE_PAGE_SIZE", "0") or 0)
CURSOR_CYCLE = os.environ.get("MCP_FIXTURE_CURSOR_CYCLE") == "1"

EDGE_TOOLS: list[types.Tool] = [
    types.Tool(
        name="nested",
        description="Nested object/array schema with required, enum, oneOf and additionalProperties.",
        input_schema={
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "target": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "minLength": 1},
                        "mode": {"enum": ["replace", "append"]},
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
                "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
                "note": {"oneOf": [{"type": "string"}, {"type": "null"}]},
            },
            "required": ["target"],
            "additionalProperties": False,
        },
    ),
    types.Tool(
        name="draft7",
        description="Draft-07 schema with a same-document $ref.",
        input_schema={
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "properties": {"point": {"$ref": "#/definitions/point"}},
            "required": ["point"],
            "definitions": {
                "point": {
                    "type": "object",
                    "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
                    "required": ["x", "y"],
                }
            },
        },
    ),
    types.Tool(
        name="remote_ref",
        description="A schema that tries to reference a remote document.",
        input_schema={
            "type": "object",
            "properties": {"value": {"$ref": "https://example.invalid/schema.json#/value"}},
        },
    ),
    types.Tool(
        name="unknown_dialect",
        description="A schema claiming an unknown dialect.",
        input_schema={
            "$schema": "https://example.invalid/dialect/schema",
            "type": "object",
            "properties": {},
        },
    ),
    types.Tool(
        name="huge_schema",
        description="A schema far above the node limit.",
        input_schema={
            "type": "object",
            "properties": {f"field_{index}": {"type": "string"} for index in range(3000)},
        },
    ),
    types.Tool(
        name="CamelCaseTool",
        description="Remote name with capitals.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    types.Tool(
        name="camelcasetool",
        description="Same lowercase spelling as CamelCaseTool.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    types.Tool(
        name="long." + "a" * 120 + "x",
        description="Very long remote name, variant x.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    types.Tool(
        name="long." + "a" * 120 + "y",
        description="Very long remote name, variant y.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
]

if os.environ.get("MCP_FIXTURE_EDGE_TOOLS") == "1":
    TOOL_LIST.extend(EDGE_TOOLS)
    for tool in EDGE_TOOLS:
        TOOLS[tool.name] = tool


async def on_list_tools(ctx, params):
    cursor = getattr(params, "cursor", None) if params is not None else None
    fact("server.list_tools", protocol=ctx.protocol_version, cursor=cursor)
    if CURSOR_CYCLE:
        return types.ListToolsResult(tools=TOOL_LIST[:1], next_cursor=str(cursor or "cycle"))
    if PAGE_SIZE <= 0:
        return types.ListToolsResult(tools=TOOL_LIST)
    start = int(cursor) if cursor else 0
    page = TOOL_LIST[start : start + PAGE_SIZE]
    next_cursor = str(start + PAGE_SIZE) if start + PAGE_SIZE < len(TOOL_LIST) else None
    return types.ListToolsResult(tools=page, next_cursor=next_cursor)


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
    if name == "hold":
        active_path = state_dir() / "active"
        peak_path = state_dir() / "peak"
        active = int(active_path.read_text(encoding="utf-8").strip() or "0") if active_path.exists() else 0
        active += 1
        active_path.write_text(str(active), encoding="utf-8")
        peak = int(peak_path.read_text(encoding="utf-8").strip() or "0") if peak_path.exists() else 0
        peak_path.write_text(str(max(active, peak)), encoding="utf-8")
        fact("server.hold.enter", active=active)
        try:
            await anyio.sleep(float(args.get("seconds", 0)))
        finally:
            current = int(active_path.read_text(encoding="utf-8").strip() or "0")
            active_path.write_text(str(max(0, current - 1)), encoding="utf-8")
            fact("server.hold.exit")
        return types.CallToolResult(content=[types.TextContent(type="text", text="held")])
    if name == "noisy":
        sys.stderr.write("e" * int(args.get("bytes", 0)))
        sys.stderr.flush()
        return types.CallToolResult(content=[types.TextContent(type="text", text="noisy")])
    if name == "image":
        return types.CallToolResult(
            content=[types.ImageContent(type="image", data="aGVsbG8=", mime_type="image/png")]
        )
    if name == "bump_then_die":
        # The exact window the unknown barrier exists for: the business effect happened
        # and the answer never came back.
        count = bump_counter(str(args.get("key", "default")))
        fact("server.business_written", key=args.get("key"), count=count)
        sys.stderr.flush()
        os._exit(7)
    if name == "die":
        # A real process exit in the middle of a call: the client must report an
        # unknown effect, never a failure it cannot prove.
        sys.stderr.flush()
        os._exit(9)
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


def _counting_asgi(app, record) -> Any:
    """Record every HTTP request this fixture server answers (protocol-level counting)."""

    async def middleware(scope, receive, send):
        if scope.get("type") != "http":
            await app(scope, receive, send)
            return
        body = bytearray()
        status = {"code": None}

        async def receive_wrapper():
            message = await receive()
            if message.get("type") == "http.request":
                body.extend(message.get("body") or b"")
            return message

        async def send_wrapper(message):
            if message.get("type") == "http.response.start":
                status["code"] = message.get("status")
            await send(message)

        await app(scope, receive_wrapper, send_wrapper)
        record(scope.get("method"), scope.get("path"), status["code"], bytes(body))

    return middleware


def run_http(host: str, port: int) -> None:
    import uvicorn

    server = build_server()
    app = server.streamable_http_app(streamable_http_path="/mcp", host=host, json_response=False)

    def record(method, path, status, body) -> None:
        import json as _json

        rpc_method = None
        try:
            parsed = _json.loads(body)
            if isinstance(parsed, dict):
                rpc_method = parsed.get("method")
        except Exception:  # noqa: BLE001 - a tap must never change behaviour
            rpc_method = "<unparsed>"
        fact("server.http", method=method, path=path, status=status, rpc_method=rpc_method)

    app = _counting_asgi(app, record)
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
