"""EXT-AC19/20/22: owner-bound lifecycle, bounded rollback and resource isolation."""

from __future__ import annotations

import asyncio
import threading

import pytest

from yanxu_harness.extensions import (
    ExtensionContribution,
    ExtensionOpenFailed,
    ExtensionOwnerMismatch,
    ExtensionState,
    open_extensions,
    plan_extensions,
)
from yanxu_harness.integrations.mcp import McpServerConfig, McpTransport, SUPPORTED_PROTOCOLS
from yanxu_harness.extensions.mcp import McpExtensionFactory

from ext_support import RecordingFactory, binding, manifest, read_tool


class OrderedFactory(RecordingFactory):
    def __init__(self, name: str, order: list[str], *, fail_close: bool = False, **kwargs) -> None:
        super().__init__(**kwargs)
        self.name = name
        self.order = order
        self.fail_close = fail_close

    async def open(self, context):
        self.resources += 1
        self.opened += 1
        return self.contribution or ExtensionContribution()

    async def close(self) -> None:
        self.order.append(self.name)
        self.closed += 1
        self.resources = 0
        if self.fail_close:
            raise RuntimeError(f"{self.name} close failed")


class SlowFactory(RecordingFactory):
    async def open(self, context):
        self.resources += 1
        self.opened += 1
        await asyncio.sleep(30)
        return ExtensionContribution()

    async def close(self) -> None:
        self.closed += 1
        self.resources = 0


def simple_plan(factory, *, extension_id: str = "example.reference", tool: str | None = None):
    tools = (tool,) if tool else ()
    return plan_extensions(
        manifests=[
            manifest(
                extension_id=extension_id,
                tools=tools,
                skills=(),
                capabilities=("tools.v1",),
            )
        ],
        factories={extension_id: factory},
        bindings=[
            binding(
                extension_id=extension_id,
                tools=tools,
                skills=(),
                capabilities=("tools.v1",),
            )
        ],
    )


async def test_the_owner_task_can_close_and_close_is_idempotent() -> None:
    factory = RecordingFactory(ExtensionContribution())
    plan = simple_plan(factory)
    deployment = await open_extensions(plan).open()
    assert deployment.state is ExtensionState.READY

    first = await deployment.aclose()
    second = await deployment.aclose()

    assert first.released == ("example.reference",)
    assert second is first
    assert factory.closed == 1, "resources are released once, not once per close call"
    assert deployment.state is ExtensionState.CLOSED


async def test_another_task_cannot_close_or_reopen_the_deployment() -> None:
    factory = RecordingFactory(ExtensionContribution())
    deployment = await open_extensions(simple_plan(factory)).open()

    async def other_task() -> None:
        with pytest.raises(ExtensionOwnerMismatch):
            await deployment.aclose()

    await asyncio.create_task(other_task())
    assert deployment.state is ExtensionState.READY, "a refused close must not break the owner"

    await deployment.aclose()
    assert factory.closed == 1


def test_another_event_loop_cannot_close_the_deployment() -> None:
    ready = threading.Event()
    release = threading.Event()
    outcome: dict[str, object] = {}

    def owner() -> None:
        async def body() -> None:
            deployment = await open_extensions(simple_plan(RecordingFactory())).open()
            outcome["deployment"] = deployment
            ready.set()
            await asyncio.get_running_loop().run_in_executor(None, release.wait)
            await deployment.aclose()

        asyncio.run(body())

    thread = threading.Thread(target=owner)
    thread.start()
    try:
        assert ready.wait(timeout=10)
        deployment = outcome["deployment"]

        async def foreign() -> None:
            with pytest.raises(ExtensionOwnerMismatch):
                await deployment.aclose()

        asyncio.run(foreign())
        assert deployment.state is ExtensionState.READY
    finally:
        release.set()
        thread.join(timeout=10)

    assert not thread.is_alive()
    assert outcome["deployment"].state is ExtensionState.CLOSED


async def test_startup_timeout_and_cancellation_release_the_partial_resource() -> None:
    timeout_factory = SlowFactory()
    plan = simple_plan(timeout_factory)
    with pytest.raises(ExtensionOpenFailed, match="TimeoutError|timed out"):
        async with open_extensions(plan, startup_timeout_seconds=0.01):
            pass
    assert timeout_factory.closed == 1
    assert timeout_factory.resources == 0

    cancelled = SlowFactory()
    deployment = open_extensions(simple_plan(cancelled))
    task = asyncio.create_task(deployment.open())
    while cancelled.opened == 0:
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.closed == 1
    assert cancelled.resources == 0
    assert deployment.state is ExtensionState.FAILED


async def test_close_is_reverse_bounded_and_continues_after_one_failure() -> None:
    order: list[str] = []
    first = OrderedFactory(
        "example.one", order, contribution=ExtensionContribution(tools=(read_tool("example.one.read"),))
    )
    second = OrderedFactory(
        "example.two", order, fail_close=True,
        contribution=ExtensionContribution(tools=(read_tool("example.two.read"),)),
    )
    plan = plan_extensions(
        manifests=[
            manifest(
                extension_id="example.one",
                tools=("example.one.read",),
                skills=(),
                capabilities=("tools.v1",),
            ),
            manifest(
                extension_id="example.two",
                tools=("example.two.read",),
                skills=(),
                capabilities=("tools.v1",),
            ),
        ],
        factories={"example.one": first, "example.two": second},
        bindings=[
            binding(
                extension_id="example.one",
                tools=("example.one.read",),
                skills=(),
                capabilities=("tools.v1",),
            ),
            binding(
                extension_id="example.two",
                tools=("example.two.read",),
                skills=(),
                capabilities=("tools.v1",),
            ),
        ],
    )
    async with open_extensions(plan) as deployment:
        pass

    report = deployment.close_report
    assert order == ["example.two", "example.one"], "close is reverse of open"
    assert report.failed and "example.two" in report.failed[0]
    assert "example.one" in report.released
    assert first.resources == 0 and second.resources == 0


async def test_closing_one_namespace_does_not_affect_another() -> None:
    left_factory = RecordingFactory(ExtensionContribution())
    right_factory = RecordingFactory(ExtensionContribution())
    left_factory.contribution = ExtensionContribution(tools=(read_tool("example.left.read"),))
    right_factory.contribution = ExtensionContribution(tools=(read_tool("example.right.read"),))
    left_plan = simple_plan(left_factory, extension_id="example.left", tool="example.left.read")
    right_plan = simple_plan(right_factory, extension_id="example.right", tool="example.right.read")
    left = await open_extensions(left_plan).open()
    right = await open_extensions(right_plan).open()

    await left.aclose()
    assert left.state is ExtensionState.CLOSED
    assert right.state is ExtensionState.READY
    assert right.registry.names() == ("example.right.read",)
    await right.aclose()
    assert left_factory.closed == 1 and right_factory.closed == 1


async def test_a_borrowed_mcp_manager_is_not_closed_by_the_deployment() -> None:
    class Manager:
        def __init__(self) -> None:
            self.closed = 0

        async def __aexit__(self, *exc):
            self.closed += 1

    class Adapter:
        def definitions(self):
            return (read_tool("example.reference.lookup"),)

    manager = Manager()
    config = McpServerConfig(
        server_id="fixture",
        transport=McpTransport.STDIO,
        protocol=SUPPORTED_PROTOCOLS[0],
        authorization_scope="authorized",
        command="python3",
        config_revision="cfg-1",
    )
    factory = McpExtensionFactory(
        config=config,
        bindings={},
        manager=manager,
        owned=False,
        adapter=Adapter(),
        borrowed_namespace="project-a",
    )
    plan = plan_extensions(
        manifests=[
            manifest(
                tools=("example.reference.lookup",),
                skills=(),
                capabilities=("mcp.tools.v1", "tools.v1"),
            )
        ],
        factories={"example.reference": factory},
        bindings=[
            binding(
                tools=("example.reference.lookup",),
                skills=(),
                capabilities=("mcp.tools.v1", "tools.v1"),
            )
        ],
    )
    async with open_extensions(plan) as deployment:
        assert deployment.registry.names() == ("example.reference.lookup",)
    assert manager.closed == 0, "a borrowed manager belongs to the host, not the deployment"
