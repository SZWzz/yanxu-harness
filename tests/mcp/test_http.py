"""MCP03：Streamable HTTP、预置凭据、两代协议、one-shot 与断流。

计数不靠客户端自述：fixture 的 HTTP 中间件把每个请求的 **方法、路径、JSON-RPC
method 和状态码**写进 facts，所以「一次准入一次 tools/call」是在 HTTP 层数出来的。
凭据用哨兵字符串扫：错误文本、公开描述、facts、stderr 尾巴里都不许出现。
"""

from __future__ import annotations

import pytest

from support import harness

if not harness.sdk_available():  # pragma: no cover - exercised by the base gate
    pytest.skip(
        "the MCP extra is not installed; the MCP gate runs this suite with the pinned extra",
        allow_module_level=True,
    )

from yanxu_harness.integrations.mcp import (  # noqa: E402
    McpCallFailure,
    McpClientManager,
    McpError,
)
from support.harness import (  # noqa: E402
    counter,
    facts_of,
    http_server,
    mcp_http_config,
    raw_http_server,
)

SECRET = "secret-sentinel-2f6c1a"


def http_calls(facts, method: str) -> list[dict]:
    return [row for row in facts_of(facts, "server.http") if row.get("rpc_method") == method]


async def test_http_legacy_round_trip_sends_one_post_per_tool_call(tmp_path) -> None:
    facts, state = tmp_path / "facts.jsonl", tmp_path / "state"
    with http_server(facts, state) as url:
        async with McpClientManager() as manager:
            connection = await manager.connect(mcp_http_config(url))
            tools = await manager.list_tools(connection)
            assert "bump" in [tool.name for tool in tools.descriptors]
            answer = await manager.call_tool(connection, "bump", {"key": "http-legacy"})
            assert answer.structured == {"count": 1}

    assert len(http_calls(facts, "tools/call")) == 1
    assert len(facts_of(facts, "server.call_tool.start")) == 1
    assert counter(state, "http-legacy") == 1


async def test_http_modern_round_trip_sends_one_post_per_tool_call(tmp_path) -> None:
    facts, state = tmp_path / "facts.jsonl", tmp_path / "state"
    with http_server(facts, state) as url:
        async with McpClientManager() as manager:
            connection = await manager.connect(
                mcp_http_config(url, protocol="2026-07-28", server_id="fixture-http-modern")
            )
            assert connection.protocol == "2026-07-28"
            await manager.list_tools(connection)
            answer = await manager.call_tool(connection, "bump", {"key": "http-modern"})
            assert answer.structured == {"count": 1}

    assert len(http_calls(facts, "tools/call")) == 1
    assert len(facts_of(facts, "server.call_tool.start")) == 1
    assert counter(state, "http-modern") == 1


async def test_headers_carry_the_credential_and_nothing_leaks_it(tmp_path) -> None:
    facts = tmp_path / "facts.jsonl"
    with raw_http_server(facts, mode="auth", extra_env={"MCP_RAW_EXPECTED_TOKEN": SECRET}) as (url, _port):
        async with McpClientManager() as manager:
            config = mcp_http_config(url, headers={"Authorization": f"Bearer {SECRET}"})
            connection = await manager.connect(config)
            assert manager.connection_state(connection) == "live"
            await manager.list_tools(connection)
            public = str(config.describe())
            stderr_tail = manager.stderr_tail(connection)

    # The credential really went out...
    assert facts_of(facts, "raw.http")[0]["has_authorization"] is True
    # ...and is not in the public description, the facts, or a child's stderr.
    assert SECRET not in public
    assert SECRET not in facts.read_text(encoding="utf-8")
    assert SECRET not in stderr_tail


async def test_a_rejected_credential_never_appears_in_the_error(tmp_path) -> None:
    facts = tmp_path / "facts.jsonl"
    with raw_http_server(facts, mode="auth", extra_env={"MCP_RAW_EXPECTED_TOKEN": SECRET}) as (url, _port):
        async with McpClientManager() as manager:
            config = mcp_http_config(url, headers={"Authorization": "Bearer nope"})
            with pytest.raises(McpError) as caught:
                await manager.connect(config)
    assert SECRET not in str(caught.value)
    assert "nope" not in str(caught.value)
    assert len(facts_of(facts, "raw.http")) == 1, "one rejected attempt must not be retried"


@pytest.mark.parametrize("mode", ["forbidden", "notfound", "server_error"])
async def test_http_error_statuses_are_reported_without_a_second_request(tmp_path, mode) -> None:
    facts = tmp_path / "facts.jsonl"
    with raw_http_server(facts, mode=mode) as (url, _port):
        async with McpClientManager() as manager:
            with pytest.raises(McpError):
                await manager.connect(mcp_http_config(url, server_id=f"raw-{mode}"))
    assert len(facts_of(facts, "raw.http")) == 1


async def test_a_cross_origin_redirect_is_refused_and_not_followed(tmp_path) -> None:
    facts = tmp_path / "facts.jsonl"
    target_facts = tmp_path / "target.jsonl"
    with raw_http_server(target_facts, mode="server_error") as (target_url, _target_port):
        with raw_http_server(
            facts, mode="redirect", extra_env={"MCP_RAW_REDIRECT_TO": target_url}
        ) as (url, _port):
            async with McpClientManager() as manager:
                with pytest.raises(McpError):
                    await manager.connect(mcp_http_config(url, server_id="raw-redirect"))
    assert facts_of(target_facts, "raw.http") == [], "the redirect target must receive nothing"
    assert len(facts_of(facts, "raw.http")) == 1


async def test_a_truncated_answer_is_not_resent(tmp_path) -> None:
    facts = tmp_path / "facts.jsonl"
    with raw_http_server(facts, mode="truncate") as (url, _port):
        async with McpClientManager() as manager:
            connection = await manager.connect(mcp_http_config(url, server_id="raw-truncate"))
            with pytest.raises(Exception) as caught:
                await manager.call_tool(connection, "anything", {})
    assert len([row for row in facts_of(facts, "raw.http") if row.get("rpc_method") == "tools/call"]) == 1, (
        "initialize, the initialized notification and exactly one tools/call"
    )
    assert [row["rpc_method"] for row in facts_of(facts, "raw.http")] == [
        "initialize",
        "notifications/initialized",
        "tools/call",
    ]
    assert not isinstance(caught.value, AssertionError)


async def test_a_header_mismatch_does_not_trigger_a_resend(tmp_path) -> None:
    """The 2026-07-28 header-parameter rejection must stay one request.

    The SDK's high-level `Client.call_tool` refetches the listing and resends after
    HEADER_MISMATCH (MCP00 ADR); this adapter uses `ClientSession`, so the tool call
    appears exactly once even when the server rejects it for missing `Mcp-Param-*`
    headers.
    """
    facts, state = tmp_path / "facts.jsonl", tmp_path / "state"
    with http_server(facts, state) as url:
        async with McpClientManager() as manager:
            connection = await manager.connect(
                mcp_http_config(url, protocol="2026-07-28", server_id="fixture-http-header")
            )
            # Deliberately *not* listing tools: the session has no arg→header map, so
            # the server rejects this call with HEADER_MISMATCH.
            with pytest.raises(McpCallFailure) as caught:
                await manager.call_tool(connection, "headerbound", {"tenant": "acme"})
            assert caught.value.may_have_reached_server is True

    assert len(http_calls(facts, "tools/call")) == 1
    assert facts_of(facts, "server.call_tool.start") == []
    assert http_calls(facts, "tools/list") == [], "a rejected call must not refetch the listing"
