"""MCP 测试夹具的共用工具：真实子进程、客户端侧请求计数、facts 读取。

这里的每一样都刻意不依赖被测代码：测试要能独立观测「协议上发了几个
`tools/call`」和「服务端真的执行了几次」，而不是相信适配器的自述。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SUPPORT_DIR = Path(__file__).resolve().parent
SERVER_SCRIPT = SUPPORT_DIR / "server.py"
RAW_SERVER_SCRIPT = SUPPORT_DIR / "raw_server.py"
RAW_HTTP_SERVER_SCRIPT = SUPPORT_DIR / "raw_http_server.py"


def sdk_available() -> bool:
    """True only when the real MCP SDK is importable.

    `find_spec("mcp")` alone is not enough: this suite's own directory is named
    `mcp`, and a directory without `__init__.py` is a namespace package, so a
    bare probe would report the extra installed when it is not. Asking for a
    submodule cannot be satisfied by that directory.
    """
    import importlib.util

    try:
        return importlib.util.find_spec("mcp.client.session") is not None
    except (ImportError, ModuleNotFoundError):
        return False


class RecordingWriteStream:
    """按原样转发，同时记录每一条出站 JSON-RPC 请求的 method。

    这是 SDK 公开 `TransportStreams` 契约内的包装（`send` + 异步上下文协议），
    不改 SDK 私有实现，也不影响行为：记录失败只写 `<unparsed>`。
    """

    def __init__(self, inner: Any, sink: list[str]) -> None:
        self._inner = inner
        self._sink = sink

    async def send(self, item: Any, /) -> None:
        message = getattr(item, "message", None)
        method = getattr(message, "method", None)
        self._sink.append(str(method) if method is not None else "<no-method>")
        await self._inner.send(item)

    async def aclose(self) -> None:
        await self._inner.aclose()

    async def __aenter__(self):
        enter = getattr(self._inner, "__aenter__", None)
        if enter is not None:
            await enter()
        return self

    async def __aexit__(self, *exc: Any):
        exit_ = getattr(self._inner, "__aexit__", None)
        if exit_ is not None:
            return await exit_(*exc)
        return None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


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
    raise TimeoutError(f"fixture server did not listen on 127.0.0.1:{port}")


@contextmanager
def http_server(
    facts: Path,
    state: Path,
    *,
    port: int | None = None,
    extra_env: dict[str, str] | None = None,
) -> Iterator[str]:
    """Run the fixture server over Streamable HTTP in a real subprocess."""
    chosen = port if port is not None else free_port()
    env = {
        **os.environ,
        "MCP_FIXTURE_FACTS": str(facts),
        "MCP_FIXTURE_STATE": str(state),
        "PYTHONIOENCODING": "utf-8",
        **(extra_env or {}),
    }
    process = subprocess.Popen(
        [sys.executable, str(SERVER_SCRIPT), "--transport", "http", "--port", str(chosen)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        wait_for_port(chosen)
        yield f"http://127.0.0.1:{chosen}/mcp"
    finally:
        was_running = process.poll() is None
        if was_running:
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - fixture refused to die
            process.kill()
            process.wait(timeout=10)
        expected = {0, -15, -9}
        if os.name == "nt" and was_running:
            # Popen.terminate() uses TerminateProcess(exit_code=1) on Windows.
            expected.add(1)
        if process.returncode not in expected:  # pragma: no cover - fixture defect
            stderr = process.stderr.read().decode("utf-8", "replace") if process.stderr else ""
            raise AssertionError(f"fixture server exited {process.returncode}: {stderr[-800:]}")


def stdio_params(facts: Path, state: Path, *, extra_env: dict[str, str] | None = None):
    from mcp import StdioServerParameters
    from yanxu_harness.integrations.mcp import DEFAULT_INHERITED_ENV_VARS

    environment = {
        name: os.environ[name] for name in DEFAULT_INHERITED_ENV_VARS if name in os.environ
    }
    environment.update(
        {
            "MCP_FIXTURE_FACTS": str(facts),
            "MCP_FIXTURE_STATE": str(state),
            **(extra_env or {}),
        }
    )

    return StdioServerParameters(
        command=sys.executable,
        args=[str(SERVER_SCRIPT), "--transport", "stdio"],
        cwd=str(SUPPORT_DIR),
        env=environment,
    )


def mcp_stdio_config(
    facts: Path,
    state: Path,
    *,
    protocol: str = "2025-11-25",
    server_id: str = "fixture",
    authorization_scope: str = "scope-a",
    limits=None,
    extra_env: dict[str, str] | None = None,
    **overrides,
):
    """A host-side configuration for the fixture server over stdio."""
    from yanxu_harness.integrations.mcp import McpLimits, McpServerConfig, McpTransport

    values = dict(
        server_id=server_id,
        transport=McpTransport.STDIO,
        protocol=protocol,
        authorization_scope=authorization_scope,
        command=sys.executable,
        args=(str(SERVER_SCRIPT), "--transport", "stdio"),
        cwd=str(SUPPORT_DIR),
        env={
            "MCP_FIXTURE_FACTS": str(facts),
            "MCP_FIXTURE_STATE": str(state),
            **(extra_env or {}),
        },
        limits=limits or McpLimits(),
    )
    values.update(overrides)
    return McpServerConfig(**values)


def raw_stdio_config(
    facts: Path,
    *,
    mode: str,
    protocol: str = "2025-11-25",
    limits=None,
    extra_env: dict[str, str] | None = None,
    **overrides,
):
    """A host-side configuration for the deliberately non-compliant raw peer."""
    from yanxu_harness.integrations.mcp import McpLimits, McpServerConfig, McpTransport

    values = dict(
        server_id="raw",
        transport=McpTransport.STDIO,
        protocol=protocol,
        authorization_scope="scope-raw",
        command=sys.executable,
        args=(str(RAW_SERVER_SCRIPT), mode),
        cwd=str(SUPPORT_DIR),
        env={"MCP_FIXTURE_FACTS": str(facts), **(extra_env or {})},
        limits=limits or McpLimits(),
    )
    values.update(overrides)
    return McpServerConfig(**values)


def mcp_http_config(
    url: str,
    *,
    protocol: str = "2025-11-25",
    server_id: str = "fixture-http",
    authorization_scope: str = "scope-http",
    limits=None,
    headers=None,
    allow_loopback_http: bool = True,
    **overrides,
):
    """A host-side configuration for a loopback Streamable HTTP endpoint."""
    from yanxu_harness.integrations.mcp import McpLimits, McpServerConfig, McpTransport

    values = dict(
        server_id=server_id,
        transport=McpTransport.STREAMABLE_HTTP,
        protocol=protocol,
        authorization_scope=authorization_scope,
        url=url,
        headers=headers or {},
        allow_loopback_http=allow_loopback_http,
        limits=limits or McpLimits(),
    )
    values.update(overrides)
    return McpServerConfig(**values)


@contextmanager
def raw_http_server(
    facts: Path,
    *,
    mode: str,
    port: int | None = None,
    extra_env: dict[str, str] | None = None,
) -> Iterator[tuple[str, int]]:
    """Run the deliberately uncooperative HTTP peer; yields (url, port)."""
    chosen = port if port is not None else free_port()
    env = {
        **os.environ,
        "MCP_FIXTURE_FACTS": str(facts),
        "MCP_RAW_HTTP_MODE": mode,
        "MCP_RAW_PORT": str(chosen),
        "PYTHONIOENCODING": "utf-8",
        **(extra_env or {}),
    }
    process = subprocess.Popen(
        [sys.executable, str(RAW_HTTP_SERVER_SCRIPT)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        wait_for_port(chosen)
        yield f"http://127.0.0.1:{chosen}/mcp", chosen
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - fixture refused to die
            process.kill()
            process.wait(timeout=10)


def read_facts(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def facts_of(path: Path, kind: str) -> list[dict[str, Any]]:
    return [row for row in read_facts(path) if row.get("kind") == kind]


def counter(state: Path, name: str) -> int:
    path = state / name
    if not path.exists():
        return 0
    return int(path.read_text(encoding="utf-8").strip() or "0")


def counter_log(state: Path, name: str) -> list[int]:
    path = state / f"{name}.log"
    if not path.exists():
        return []
    return [int(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def recording_http_client(sink: list[str]):
    """An httpx2 client whose request hook records each POST body's JSON-RPC method."""
    import httpx2

    async def record(request: "httpx2.Request") -> None:
        try:
            parsed = json.loads(request.content)
            method = parsed.get("method") if isinstance(parsed, dict) else None
            sink.append(str(method))
        except Exception:  # noqa: BLE001 - a tap must never change behaviour
            sink.append("<unparsed>")

    return httpx2.AsyncClient(event_hooks={"request": [record]}, timeout=30.0)


class RecordingTransport:
    """A Transport that lets the high-level `Client` use a recording http client."""

    def __init__(self, url: str, sink: list[str]) -> None:
        from mcp.client.streamable_http import streamable_http_client

        self._cm = streamable_http_client(url, http_client=recording_http_client(sink))

    async def __aenter__(self):
        return await self._cm.__aenter__()

    async def __aexit__(self, *exc: Any):
        return await self._cm.__aexit__(*exc)
