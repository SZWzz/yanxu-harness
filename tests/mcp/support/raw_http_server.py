"""一个故意不好好说话的 Streamable HTTP 对端，用来测 HTTP 边界。

模式：

``auth``
    没有 ``Authorization: Bearer <expected>`` 就回 401；带了就回一个真实的
    ``initialize`` 结果（2025-11-25）。用来证明凭据真的上了线，而且不泄露。
``forbidden`` / ``notfound`` / ``server_error``
    对所有请求回 403 / 404 / 500，并数下到底收到了几个请求。
``redirect``
    对每个请求回 307，``Location`` 指向 ``MCP_RAW_REDIRECT_TO``；测试会验证目标
    origin 收到 0 个请求（跨 origin 重定向必须被拒绝，而不是跟过去）。
``truncate``
    回 200 + event-stream 然后立刻结束连接，不回任何 JSON-RPC 结果：这就是
    「请求可能已经到了、回包丢了」的场景，客户端不得自动重发。

每个请求都写一条 facts（方法、路径、rpc method、状态），计数不靠客户端自述。
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

FACTS_ENV = "MCP_FIXTURE_FACTS"
LIMIT = 200 * 1024


def fact(kind: str, **payload: Any) -> None:
    path = os.environ.get(FACTS_ENV)
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"kind": kind, **payload}, sort_keys=True) + "\n")


def mode() -> str:
    return os.environ.get("MCP_RAW_HTTP_MODE", "auth")


async def endpoint(request: Request) -> Response:
    body = await request.body()
    rpc_method = None
    request_id: Any = None
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict):
            rpc_method = parsed.get("method")
            request_id = parsed.get("id")
    except ValueError:
        rpc_method = "<unparsed>"
    fact(
        "raw.http",
        method=request.method,
        path=request.url.path,
        rpc_method=rpc_method,
        has_authorization="authorization" in request.headers,
        api_key=request.headers.get("x-api-key"),
    )
    current = mode()

    if current == "auth":
        expected = os.environ.get("MCP_RAW_EXPECTED_TOKEN", "token-expected")
        if request.headers.get("authorization") != f"Bearer {expected}":
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if rpc_method == "initialize":
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": "raw-http", "version": "1.0.0"},
                    },
                }
            )
        if rpc_method == "tools/list":
            return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": {"tools": []}})
        return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": {}})

    if current == "forbidden":
        return JSONResponse({"error": "forbidden"}, status_code=403)
    if current == "notfound":
        return JSONResponse({"error": "not found"}, status_code=404)
    if current == "server_error":
        return JSONResponse({"error": "boom"}, status_code=500)
    if current == "redirect":
        target = os.environ.get("MCP_RAW_REDIRECT_TO", "http://127.0.0.1:1/mcp")
        return Response(status_code=307, headers={"Location": target})
    if current == "truncate":
        # ``initialize`` and ``tools/list`` are answered so a connection can be
        # established; a ``tools/call`` gets a 200 that ends without a JSON-RPC
        # answer: the request reached the server, the answer did not come back.
        if rpc_method == "initialize":
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": "raw-http", "version": "1.0.0"},
                    },
                }
            )
        if rpc_method == "tools/list":
            return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": {"tools": []}})
        return Response(content=b"", media_type="text/event-stream", status_code=200)
    return JSONResponse({"error": f"unknown mode {current}"}, status_code=500)


def main() -> int:
    host = os.environ.get("MCP_RAW_HOST", "127.0.0.1")
    port = int(os.environ.get("MCP_RAW_PORT", "0"))
    fact("raw.http.start", pid=os.getpid(), host=host, port=port, mode=mode())
    app = Starlette(routes=[Route("/mcp", endpoint, methods=["GET", "POST", "DELETE"])])
    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
