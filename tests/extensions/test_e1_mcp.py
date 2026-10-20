"""EXT-AC17/18 (assembly half): MCP is explicit, lazy and ownership-checked."""

from __future__ import annotations

import pytest

from yanxu_harness.extensions import (
    ExtensionContribution,
    ExtensionOpenFailed,
    ExtensionRefused,
    open_extensions,
    plan_extensions,
    mcp_tool_factory,
)
from yanxu_harness.integrations.mcp import (
    McpServerConfig,
    McpTransport,
    SUPPORTED_PROTOCOLS,
    sdk_available,
)

from ext_support import RecordingFactory, binding, manifest, read_tool


def _config() -> McpServerConfig:
    return McpServerConfig(
        server_id="fixture",
        transport=McpTransport.STDIO,
        protocol=SUPPORTED_PROTOCOLS[0],
        authorization_scope="authorized",
        command="python3",
        config_revision="cfg-1",
    )


def _mcp_plan(factory):
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
    return plan


async def test_without_the_extra_the_failure_happens_only_when_the_pack_is_opened() -> None:
    if sdk_available():
        pytest.skip("this check belongs to the base environment without the MCP extra")
    factory = mcp_tool_factory(config=_config(), bindings={})
    # Building the factory and the plan performs no import, process or network work.
    plan = _mcp_plan(factory)

    with pytest.raises(ExtensionOpenFailed, match=r"yanxu-harness\[mcp\]"):
        async with open_extensions(plan):
            pass


async def test_a_local_pack_still_opens_when_no_mcp_pack_is_bound() -> None:
    plan = plan_extensions(
        manifests=[manifest(tools=("example.reference.lookup",), skills=())],
        factories={
            "example.reference": RecordingFactory(
                ExtensionContribution(tools=(read_tool(),))
            )
        },
        bindings=[binding(tools=("example.reference.lookup",), skills=())],
    )
    async with open_extensions(plan) as deployment:
        assert deployment.registry.names() == ("example.reference.lookup",)


class FakeManager:
    def __init__(self) -> None:
        self.calls = 0

    async def connect(self, config):
        self.calls += 1
        raise AssertionError("a borrowed manager must not be used before ownership is proven")


async def test_a_borrowed_manager_must_declare_ownership_and_namespace() -> None:
    with pytest.raises(ExtensionRefused, match="ownership"):
        mcp_tool_factory(config=_config(), bindings={}, manager=FakeManager())

    with pytest.raises(ExtensionRefused, match="namespace"):
        mcp_tool_factory(
            config=_config(),
            bindings={},
            manager=FakeManager(),
            owned=False,
        )

    borrowed = FakeManager()
    plan = _mcp_plan(
        mcp_tool_factory(
            config=_config(),
            bindings={},
            manager=borrowed,
            owned=False,
            borrowed_namespace="project-b",
        )
    )
    with pytest.raises(ExtensionRefused, match="namespace"):
        async with open_extensions(plan):
            pass
    assert borrowed.calls == 0


class FakeAdapter:
    def __init__(self, definition) -> None:
        self._definition = definition
        self.calls = 0

    def definitions(self):
        self.calls += 1
        return (self._definition,)


async def test_an_adapter_guard_is_combined_not_overwritten() -> None:
    guard_calls: list[dict] = []

    def catalog_guard(payload):
        guard_calls.append(dict(payload))
        return "catalog drift" if payload.get("call_id") == "drift" else None

    tool = read_tool()
    from dataclasses import replace

    tool = replace(tool, dispatch_guard=catalog_guard)
    adapter = FakeAdapter(tool)
    from yanxu_harness.extensions.mcp import McpExtensionFactory

    plan = _mcp_plan(McpExtensionFactory.from_adapter(adapter))
    async with open_extensions(plan) as deployment:
        definition = deployment.registry.get("example.reference.lookup")
        assert adapter.calls == 1
        assert definition.dispatch_guard is not None
        assert definition.dispatch_guard({"call_id": "ok"}) is None
        assert guard_calls
        assert definition.dispatch_guard({"call_id": "drift"}) == "catalog drift"
        deployment.invalidate("revoked")
        assert "deployment invalidated" in (definition.dispatch_guard({"call_id": "ok"}) or "")
