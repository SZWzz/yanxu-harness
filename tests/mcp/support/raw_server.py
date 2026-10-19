"""一个故意不合规的 stdio 对端，用来测边界而不是测正常路径。

模式：

``counter-offer``
    对 ``initialize`` 回一个**不同**的协议版本（2025-06-18）。适配器必须拒绝，
    而不是「设置过的字符串就是支持」。
``oversize``
    正常握手，然后对任何请求回一行超过上限的帧；适配器必须在解码前按字节拒绝，
    并让连接失效。
``garbage``
    正常握手，然后回一行不是 JSON 的东西；解析失败要作为协议错误出现，不是静默丢帧。
``exit-after-initialize``
    握手成功后立刻退出，用来测「连上过又断了」的连接状态。

握手本身用真的 JSON-RPC：这仍然是一个真实进程、真实管道，只是不装 SDK。
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

FACTS_ENV = "MCP_FIXTURE_FACTS"


def fact(kind: str, **payload: Any) -> None:
    path = os.environ.get(FACTS_ENV)
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"kind": kind, **payload}, sort_keys=True) + "\n")


def send(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def result(request_id: Any, result: dict[str, Any]) -> None:
    send({"jsonrpc": "2.0", "id": request_id, "result": result})


def main(argv: list[str]) -> int:
    mode = argv[1] if len(argv) > 1 else "counter-offer"
    fact("raw.start", mode=mode, pid=os.getpid())
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        method = message.get("method")
        request_id = message.get("id")
        if method == "initialize":
            version = "2025-06-18" if mode == "counter-offer" else "2025-11-25"
            result(
                request_id,
                {
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "raw-fixture", "version": "1.0.0"},
                },
            )
            if mode == "exit-after-initialize":
                fact("raw.exit", pid=os.getpid())
                return 0
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            if mode == "oversize":
                reject = os.environ.get("MCP_RAW_OVERSIZE_MULTIPLIER", "4")
                size = int(os.environ.get("MCP_RAW_LIMIT", "4096")) * int(reject)
                sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": {"tools": []}}) + " ")
                sys.stdout.write("x" * size + "\n")
                sys.stdout.flush()
            elif mode == "garbage":
                sys.stdout.write("this is not json-rpc\n")
                sys.stdout.flush()
            else:
                result(request_id, {"tools": []})
        else:
            send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": f"no such method {method}"},
                }
            )
    fact("raw.stdin_closed", mode=mode)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
