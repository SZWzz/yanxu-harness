"""MCP02：manager、stdio 生命周期、期限、隔离与有界资源。

每条断言都对着真实子进程或真实管道：进程真的是子进程，stderr 真的是另一根管道，
帧上限真的是字节数。服务端自述、工具自称和「它应该已经退出了」都不是证据。
"""

from __future__ import annotations

import asyncio
import os

import pytest

from support import harness

if not harness.sdk_available():  # pragma: no cover - exercised by the base gate
    pytest.skip(
        "the MCP extra is not installed; the MCP gate runs this suite with the pinned extra",
        allow_module_level=True,
    )

import anyio  # noqa: E402

from yanxu_harness.integrations.mcp import (  # noqa: E402
    McpCallFailure,
    McpClientManager,
    McpConfigurationError,
    McpConnectError,
    McpFrameTooLarge,
    McpLimits,
    McpProtocolError,
)
from yanxu_harness.integrations.mcp.transports import effective_environment  # noqa: E402
from support.harness import (  # noqa: E402
    counter_log,
    facts_of,
    mcp_stdio_config,
    raw_stdio_config,
)


def pids(facts) -> list[int]:
    return [int(row["pid"]) for row in facts_of(facts, "server.start")]


def process_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        open_process.restype = wintypes.HANDLE
        handle = open_process(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            error = ctypes.get_last_error()
            if error in {5, 87, 1168}:  # access denied means alive; invalid/missing means gone
                return error == 5
            raise ctypes.WinError(error)
        try:
            exit_code = wintypes.DWORD()
            get_exit_code = kernel32.GetExitCodeProcess
            get_exit_code.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
            get_exit_code.restype = wintypes.BOOL
            if not get_exit_code(handle, ctypes.byref(exit_code)):
                raise ctypes.WinError(ctypes.get_last_error())
            return exit_code.value == 259  # STILL_ACTIVE
        finally:
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = (wintypes.HANDLE,)
            close_handle.restype = wintypes.BOOL
            close_handle(handle)

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - would mean another user's process
        return True
    return True


def test_stdio_environment_keeps_required_windows_variables_without_leaking_secrets(
    monkeypatch, tmp_path
) -> None:
    """A minimal stdio environment still starts child processes on Windows."""
    facts = tmp_path / "facts.jsonl"
    state = tmp_path / "state"
    names = ("SystemRoot", "WINDIR", "TEMP", "TMP")
    for name in names:
        monkeypatch.setenv(name, f"value-for-{name}")
    monkeypatch.setenv("OPENAI_API_KEY", "secret-sentinel")
    monkeypatch.setenv("UNRELATED_TEST_VALUE", "do-not-inherit")

    environment = effective_environment(mcp_stdio_config(facts, state))

    assert {name: environment[name] for name in names} == {
        name: f"value-for-{name}" for name in names
    }
    assert "OPENAI_API_KEY" not in environment
    assert "UNRELATED_TEST_VALUE" not in environment


async def wait_until(predicate, *, timeout: float = 5.0) -> bool:
    """Bounded wait for an observable state change, with a recorded reason."""
    deadline = anyio.current_time() + timeout
    while anyio.current_time() < deadline:
        if predicate():
            return True
        await anyio.sleep(0.02)
    return predicate()


async def test_stdio_connects_lists_and_calls_under_the_pinned_revision(tmp_path) -> None:
    facts, state = tmp_path / "facts.jsonl", tmp_path / "state"
    async with McpClientManager() as manager:
        connection = await manager.connect(mcp_stdio_config(facts, state))
        tools = await manager.list_tools(connection)
        names = [tool.name for tool in tools.descriptors]
        assert "echo" in names and "bump" in names
        assert connection.generation == 1
        assert connection.protocol == "2025-11-25"

        answer = await manager.call_tool(connection, "bump", {"key": "stdio"})
        assert answer.is_error is False
        assert harness.counter(state, "stdio") == 1
        assert any(text == "bumped 1" for text in answer.text)
        assert answer.structured == {"count": 1}

    assert harness.facts_of(facts, "server.start")[0]["transport"] == "stdio"
    assert [row["protocol"] for row in facts_of(facts, "server.list_tools")] == ["2025-11-25"]


async def test_stdio_modern_revision_is_negotiated_not_assumed(tmp_path) -> None:
    facts, state = tmp_path / "facts.jsonl", tmp_path / "state"
    async with McpClientManager() as manager:
        connection = await manager.connect(
            mcp_stdio_config(facts, state, protocol="2026-07-28", server_id="fixture-modern")
        )
        assert connection.protocol == "2026-07-28"
        tools = await manager.list_tools(connection)
        assert [tool.name for tool in tools.descriptors][:3] == ["echo", "bump", "explode"]
        assert harness.facts_of(facts, "server.list_tools")[0]["protocol"] == "2026-07-28"


async def test_a_counter_offered_revision_is_refused_before_any_call(tmp_path) -> None:
    facts = tmp_path / "facts.jsonl"
    async with McpClientManager() as manager:
        with pytest.raises(McpProtocolError) as caught:
            await manager.connect(raw_stdio_config(facts, mode="counter-offer"))
    assert "2025-06-18" in str(caught.value)
    assert facts_of(facts, "server.call_tool.start") == []


async def test_an_oversize_frame_fails_the_connection_instead_of_being_truncated(tmp_path) -> None:
    facts = tmp_path / "facts.jsonl"
    limits = McpLimits(max_message_bytes=4096)
    async with McpClientManager() as manager:
        connection = await manager.connect(
            raw_stdio_config(facts, mode="oversize", limits=limits, server_id="raw-oversize")
        )
        with pytest.raises(McpFrameTooLarge):
            await manager.list_tools(connection)
        # The connection is not quietly reusable: it observed a frame it skipped.
        assert await wait_until(lambda: manager.connection_state(connection) != "live")


async def test_a_garbage_line_is_reported_not_silently_dropped(tmp_path) -> None:
    facts = tmp_path / "facts.jsonl"
    async with McpClientManager() as manager:
        connection = await manager.connect(raw_stdio_config(facts, mode="garbage", server_id="raw-garbage"))
        with pytest.raises(McpProtocolError) as caught:
            await manager.list_tools(connection)
        assert "unparseable" in str(caught.value)


async def test_stderr_is_bounded_and_does_not_disturb_the_protocol(tmp_path) -> None:
    facts, state = tmp_path / "facts.jsonl", tmp_path / "state"
    limits = McpLimits(max_stderr_bytes=1024)
    async with McpClientManager() as manager:
        connection = await manager.connect(mcp_stdio_config(facts, state, limits=limits))
        await manager.call_tool(connection, "noisy", {"bytes": 64 * 1024})
        answer = await manager.call_tool(connection, "echo", {"text": "still speaking"})
        assert answer.text == ("still speaking",)
        tail = manager.stderr_tail(connection)
    assert len(tail.encode("utf-8")) <= 1024
    assert "bumped" not in tail  # protocol output is not on the stderr pipe


async def test_a_server_that_dies_mid_call_is_an_unknown_effect_not_a_failure(tmp_path) -> None:
    facts, state = tmp_path / "facts.jsonl", tmp_path / "state"
    async with McpClientManager() as manager:
        connection = await manager.connect(mcp_stdio_config(facts, state))
        with pytest.raises(McpCallFailure) as caught:
            await manager.call_tool(connection, "die", {})
        assert caught.value.may_have_reached_server is True
        assert await wait_until(lambda: manager.connection_state(connection) != "live")


async def test_concurrency_slots_bound_real_overlap(tmp_path) -> None:
    facts, state = tmp_path / "facts.jsonl", tmp_path / "state"
    limits = McpLimits(max_concurrent_calls=1, max_scope_calls=4, call_timeout=10.0)
    async with McpClientManager() as manager:
        connection = await manager.connect(mcp_stdio_config(facts, state, limits=limits))
        await asyncio.gather(
            manager.call_tool(connection, "hold", {"seconds": 0.2}),
            manager.call_tool(connection, "hold", {"seconds": 0.2}),
            manager.call_tool(connection, "hold", {"seconds": 0.2}),
        )
    peak = int((state / "peak").read_text(encoding="utf-8"))
    assert peak == 1, "one slot means one server-side execution at a time"
    assert len(facts_of(facts, "server.hold.enter")) == 3


async def test_a_call_deadline_includes_waiting_for_a_slot(tmp_path) -> None:
    facts, state = tmp_path / "facts.jsonl", tmp_path / "state"
    limits = McpLimits(max_concurrent_calls=1, call_timeout=30.0)
    async with McpClientManager() as manager:
        connection = await manager.connect(mcp_stdio_config(facts, state, limits=limits))
        first = asyncio.create_task(manager.call_tool(connection, "hold", {"seconds": 0.4}))
        await anyio.sleep(0.05)
        with pytest.raises(McpCallFailure) as caught:
            await manager.call_tool(connection, "hold", {"seconds": 0.4}, timeout_seconds=0.15)
        assert caught.value.may_have_reached_server is False, "a queued call never reached the server"
        await first
    # Only the first call was ever dispatched.
    assert len(facts_of(facts, "server.hold.enter")) == 1


async def test_two_authorization_scopes_never_share_a_process(tmp_path) -> None:
    facts, state = tmp_path / "facts.jsonl", tmp_path / "state"
    async with McpClientManager() as manager:
        first = await manager.connect(mcp_stdio_config(facts, state, authorization_scope="scope-a"))
        second = await manager.connect(mcp_stdio_config(facts, state, authorization_scope="scope-b"))
        assert first.connection_id != second.connection_id
        await manager.call_tool(first, "bump", {"key": "a"})
        await manager.call_tool(second, "bump", {"key": "b"})
        assert manager.connection_state(first) == "live"
        assert manager.connection_state(second) == "live"
    assert len(pids(facts)) == 2
    assert harness.counter(state, "a") == 1 and harness.counter(state, "b") == 1


async def test_the_same_scope_reuses_one_connection(tmp_path) -> None:
    facts, state = tmp_path / "facts.jsonl", tmp_path / "state"
    async with McpClientManager() as manager:
        first = await manager.connect(mcp_stdio_config(facts, state))
        second = await manager.connect(mcp_stdio_config(facts, state))
        assert first.connection_id == second.connection_id
        await manager.call_tool(first, "bump", {"key": "reuse"})
    assert len(pids(facts)) == 1
    assert counter_log(state, "reuse") == [1]


async def test_closing_the_manager_leaves_no_child_process(tmp_path) -> None:
    facts, state = tmp_path / "facts.jsonl", tmp_path / "state"
    async with McpClientManager() as manager:
        connection = await manager.connect(mcp_stdio_config(facts, state))
        await manager.call_tool(connection, "bump", {"key": "closed"})
    for pid in pids(facts):
        assert not process_alive(pid), f"child {pid} outlived the manager"


async def test_closing_one_connection_does_not_disturb_another(tmp_path) -> None:
    facts, state = tmp_path / "facts.jsonl", tmp_path / "state"
    async with McpClientManager() as manager:
        doomed = await manager.connect(mcp_stdio_config(facts, state, authorization_scope="doomed"))
        survivor = await manager.connect(mcp_stdio_config(facts, state, authorization_scope="survivor"))
        await manager.close_connection(doomed)
        assert manager.connection_state(doomed) == "absent"
        assert manager.connection_state(survivor) == "live"
        answer = await manager.call_tool(survivor, "echo", {"text": "alive"})
        assert answer.text == ("alive",)
    assert len(pids(facts)) == 2


def test_an_unknown_protocol_is_refused_before_anything_starts(tmp_path) -> None:
    facts = tmp_path / "facts.jsonl"
    with pytest.raises(McpConfigurationError) as caught:
        mcp_stdio_config(facts, tmp_path / "state", protocol="2024-11-05")
    assert "pinned" in str(caught.value)
    assert facts_of(facts, "server.start") == []


async def test_a_missing_executable_is_a_configuration_error(tmp_path) -> None:
    config = mcp_stdio_config(tmp_path / "facts.jsonl", tmp_path / "state", command="/nonexistent/mcp-server")
    async with McpClientManager() as manager:
        with pytest.raises(McpConfigurationError) as caught:
            await manager.connect(config)
    assert "/nonexistent/mcp-server" in str(caught.value)


async def test_a_closed_manager_refuses_new_calls(tmp_path) -> None:
    facts, state = tmp_path / "facts.jsonl", tmp_path / "state"
    manager = McpClientManager()
    async with manager:
        connection = await manager.connect(mcp_stdio_config(facts, state))
    from yanxu_harness.integrations.mcp import McpClosed

    with pytest.raises(McpClosed):
        await manager.call_tool(connection, "echo", {"text": "nope"})
