"""MCP00 合同：锁定 SDK 事实、两代协议、以及「一次准入最多一次 tools/call」。

这个文件先于适配器存在。它证明的是**被依赖的运行时事实**，不是适配器的自述：

* 固定的 `mcp==2.3.0` 到底认识哪些协议版本、哪一代是哪一代；
* 真实子进程 stdio 服务上两代各自怎么协商、怎么调用；
* 计数分两层：客户端出站 `tools/call`（t​ap 出站流）与服务端业务执行（facts + 业务文件）；
* 高层的 `Client.call_tool` 在 2026-07-28 上会因 HEADER_MISMATCH 重发同一个业务调用 ——
  这是本适配器拒绝高层入口、改用公开低层 `ClientSession` 的直接证据。
"""

from __future__ import annotations

import json

import pytest

from support import harness

if not harness.sdk_available():  # pragma: no cover - exercised by the base gate
    pytest.skip(
        "the MCP extra is not installed; the MCP gate runs this suite with the pinned extra",
        allow_module_level=True,
    )

import mcp_types.version as protocol_versions  # noqa: E402
from mcp import ClientSession  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

from support.harness import (  # noqa: E402
    RecordingTransport,
    RecordingWriteStream,
    counter,
    counter_log,
    facts_of,
    http_server,
    stdio_params,
)


def test_the_pinned_sdk_declares_exactly_the_two_verified_revisions() -> None:
    """ADR 里写的两个版本必须真的是 SDK 的两个「最新」，而不是随手写的字符串。"""
    assert protocol_versions.LATEST_HANDSHAKE_VERSION == "2025-11-25"
    assert protocol_versions.MODERN_PROTOCOL_VERSIONS == ("2026-07-28",)
    assert protocol_versions.LATEST_PROTOCOL_VERSION == "2026-07-28"
    # 未知版本必须能被识别出来：`is_version_at_least` 对不认识的字符串保守返回 False。
    assert protocol_versions.is_version_at_least("2099-01-01", "2026-07-28") is False


async def test_stdio_legacy_handshake_calls_exactly_once(tmp_path) -> None:
    facts = tmp_path / "facts.jsonl"
    state = tmp_path / "state"
    sent: list[str] = []

    async with stdio_client(stdio_params(facts, state)) as (read_stream, write_stream):
        async with ClientSession(read_stream, RecordingWriteStream(write_stream, sent)) as session:
            result = await session.initialize()
            assert session.protocol_version == "2025-11-25"
            assert result.server_info.name == "yanxu-mcp-fixture"
            await session.list_tools()

            answer = await session.call_tool("bump", {"key": "legacy"})
            assert answer.is_error is False
            assert answer.structured_content == {"count": 1}

    assert sent.count("tools/call") == 1
    assert len(facts_of(facts, "server.call_tool.start")) == 1
    assert counter(state, "legacy") == 1
    assert counter_log(state, "legacy") == [1]


async def test_stdio_modern_discover_calls_exactly_once(tmp_path) -> None:
    facts = tmp_path / "facts.jsonl"
    state = tmp_path / "state"
    sent: list[str] = []

    async with stdio_client(stdio_params(facts, state)) as (read_stream, write_stream):
        async with ClientSession(read_stream, RecordingWriteStream(write_stream, sent)) as session:
            discovered = await session.discover()
            assert session.protocol_version == "2026-07-28"
            assert discovered.supported_versions == ["2026-07-28"]
            await session.list_tools()

            answer = await session.call_tool("bump", {"key": "modern"})
            assert answer.is_error is False
            assert answer.structured_content == {"count": 1}

    assert sent.count("tools/call") == 1
    assert len(facts_of(facts, "server.call_tool.start")) == 1
    assert counter(state, "modern") == 1


async def test_a_terminal_remote_error_is_an_error_result_not_an_exception(tmp_path) -> None:
    facts = tmp_path / "facts.jsonl"
    state = tmp_path / "state"

    async with stdio_client(stdio_params(facts, state)) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            await session.list_tools()
            answer = await session.call_tool("explode", {})

    assert answer.is_error is True
    assert answer.content[0].text == "remote failure: exploded"


async def test_high_level_client_resends_the_same_call_after_header_mismatch(tmp_path) -> None:
    """SDK 的高层入口会重发业务调用；低层入口不会。适配器只能用低层入口。

    2026-07-28 的 Streamable HTTP 会把 `x-mcp-header` 参数镜像成 `Mcp-Param-*` 头，
    服务端在未列工具/定义变化时回 `HEADER_MISMATCH`（-32020）。SDK 用自己的
    `tools/list` 重新取一次，然后**再发一次同样的 tools/call**——对写工具就是重复执行。
    """
    from mcp import Client

    facts = tmp_path / "facts.jsonl"
    state = tmp_path / "state"
    high_level: list[str] = []

    with http_server(facts, state) as url:
        client = Client(RecordingTransport(url, high_level), mode="2026-07-28", read_timeout_seconds=30.0)
        async with client:
            result = await client.call_tool("headerbound", {"tenant": "acme"})
        assert result.content[0].text == "tenant=acme"

    # 第一次被 -32020 拒绝、重取目录、原样重发：业务调用在协议上出现两次。
    assert high_level.count("tools/call") == 2
    assert [name for name in high_level if name in {"tools/call", "tools/list"}] == [
        "tools/call",
        "tools/list",
        "tools/call",
    ]
    starts = facts_of(facts, "server.call_tool.start")
    assert len(starts) == 1, "the fixture only executes the call the server accepted"
    assert json.loads(json.dumps(starts[0]["arguments"])) == {"tenant": "acme"}
