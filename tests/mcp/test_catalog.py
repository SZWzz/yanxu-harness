"""MCP04 的目录层：分页、别名、冻结版本与漂移判定。

漂移拒绝必须发生在任何远端调用之前，所以每条「拒绝」断言都会同时看服务端 facts
里的调用计数：目录变化不能让原 Run 用新定义继续跑旧审批。
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
    McpClientManager,
    McpConfigurationError,
    McpDriftRefusal,
    McpProtocolError,
    McpSchemaError,
    McpToolBinding,
    build_definitions,
    build_snapshot,
    check_binding,
    discover_catalog,
    freeze_binding,
    identity_shaped_fields,
    local_alias,
)
from yanxu_harness.integrations.mcp.errors import McpInvalidArguments  # noqa: E402
from yanxu_harness.tools import ToolArgsInvalid, ToolEffect  # noqa: E402
from support.harness import facts_of, mcp_stdio_config  # noqa: E402

READ = McpToolBinding("echo", effects=frozenset({ToolEffect.READ}))
WRITE = McpToolBinding(
    "bump",
    effects=frozenset({ToolEffect.WRITE}),
    preview=lambda _ctx, args: {"key": args.get("key"), "effect": "increment"},
    policy_revision="7",
)


def test_local_aliases_are_stable_lowercase_and_distinct() -> None:
    first = local_alias("fixture", "CamelCaseTool")
    second = local_alias("fixture", "camelcasetool")
    assert first == local_alias("fixture", "CamelCaseTool"), "aliases must be stable"
    assert first != second, "case folding must not merge two different remote names"
    assert first.startswith("mcp.fixture.camelcasetool.")
    for alias in (first, second, local_alias("fixture", "long." + "a" * 120 + "x")):
        assert len(alias) <= 64
        import re

        assert re.match(r"^[a-z0-9][a-z0-9_.-]{0,119}$", alias), alias
    long_x = local_alias("fixture", "long." + "a" * 120 + "x")
    long_y = local_alias("fixture", "long." + "a" * 120 + "y")
    assert long_x != long_y, "truncation must not collapse two long names"


async def test_an_incomplete_assembly_of_a_bound_tool_is_a_failure(tmp_path) -> None:
    facts, state = tmp_path / "facts.jsonl", tmp_path / "state"
    bindings = {"remote_ref": McpToolBinding("remote_ref", effects=frozenset({ToolEffect.READ}))}
    async with McpClientManager() as manager:
        config = mcp_stdio_config(facts, state, extra_env={"MCP_FIXTURE_EDGE_TOOLS": "1"})
        connection = await manager.connect(config)
        with pytest.raises(McpSchemaError) as caught:
            await discover_catalog(manager, connection, config=config, bindings=bindings)
    assert "remote $ref" in str(caught.value)
    assert facts_of(facts, "server.call_tool.start") == []


async def test_the_catalog_contains_only_host_bound_tools(tmp_path) -> None:
    facts, state = tmp_path / "facts.jsonl", tmp_path / "state"
    bindings = {"echo": READ, "bump": WRITE}
    async with McpClientManager() as manager:
        config = mcp_stdio_config(facts, state)
        connection = await manager.connect(config)
        snapshot = await discover_catalog(manager, connection, config=config, bindings=bindings)

    assert snapshot.complete is True
    assert [entry.remote_name for entry in snapshot.entries] == ["echo", "bump"]
    assert {item.remote_name for item in snapshot.exclusions} == {
        "explode",
        "headerbound",
        "structured_only",
        "empty",
        "big",
        "slow",
        "hold",
        "die",
        "noisy",
        "image",
        "bump_then_die",
    }
    assert snapshot.entry("echo") is not None
    assert snapshot.entry(local_alias("fixture", "echo")).remote_name == "echo"
    # The public summary never carries schemas or descriptions.
    described = str(snapshot.describe())
    assert "Echo the given text back" not in described


async def test_pagination_is_bounded_and_complete(tmp_path) -> None:
    facts, state = tmp_path / "facts.jsonl", tmp_path / "state"
    bindings = {"echo": READ}
    async with McpClientManager() as manager:
        config = mcp_stdio_config(facts, state, extra_env={"MCP_FIXTURE_PAGE_SIZE": "3"})
        connection = await manager.connect(config)
        directory = await manager.list_tools(connection)
        snapshot = await discover_catalog(manager, connection, config=config, bindings=bindings)

    assert directory.pages_read > 1
    assert len(directory.descriptors) == 13
    assert snapshot.pages_read > 1 and snapshot.complete is True


async def test_a_repeated_cursor_is_refused_instead_of_looping(tmp_path) -> None:
    facts, state = tmp_path / "facts.jsonl", tmp_path / "state"
    async with McpClientManager() as manager:
        config = mcp_stdio_config(
            facts, state, extra_env={"MCP_FIXTURE_CURSOR_CYCLE": "1", "MCP_FIXTURE_PAGE_SIZE": "1"}
        )
        connection = await manager.connect(config)
        with pytest.raises(McpProtocolError) as caught:
            await manager.list_tools(connection)
    assert "cursor" in str(caught.value)


async def test_the_definition_version_covers_schema_and_policy(tmp_path) -> None:
    facts, state = tmp_path / "facts.jsonl", tmp_path / "state"
    async with McpClientManager() as manager:
        config = mcp_stdio_config(facts, state)
        connection = await manager.connect(config)
        first = await discover_catalog(manager, connection, config=config, bindings={"bump": WRITE})
        second = await discover_catalog(manager, connection, config=config, bindings={"bump": WRITE})
        other_policy = McpToolBinding(
            "bump",
            effects=frozenset({ToolEffect.WRITE}),
            preview=WRITE.preview,
            policy_revision="8",
        )
        third = await discover_catalog(manager, connection, config=config, bindings={"bump": other_policy})

    assert first.entry("bump").version == second.entry("bump").version
    assert first.entry("bump").version != third.entry("bump").version
    # Effects are part of the version too.
    read_binding = McpToolBinding("bump", effects=frozenset({ToolEffect.READ}))
    fourth = build_snapshot(
        [descriptor for descriptor in (await _descriptors(facts, state)) if descriptor.name == "bump"],
        config=mcp_stdio_config(tmp_path / "f2.jsonl", tmp_path / "s2"),
        bindings={"bump": read_binding},
    )
    assert fourth.entry("bump").version != first.entry("bump").version


async def _descriptors(facts, state):
    async with McpClientManager() as manager:
        config = mcp_stdio_config(facts, state)
        connection = await manager.connect(config)
        return (await manager.list_tools(connection)).descriptors


async def test_drift_refuses_before_any_remote_call(tmp_path) -> None:
    facts, state = tmp_path / "facts.jsonl", tmp_path / "state"
    async with McpClientManager() as manager:
        config = mcp_stdio_config(facts, state)
        connection = await manager.connect(config)
        snapshot = await discover_catalog(manager, connection, config=config, bindings={"echo": READ})
        directory = await manager.list_tools(connection)
        frozen = freeze_binding(snapshot.entry("echo"), snapshot)

        live = check_binding(
            frozen,
            snapshot=snapshot,
            bindings={"echo": READ},
            protocol=config.protocol,
            config_revision=config.config_revision,
            authorization_scope=config.authorization_scope,
        )
        assert live.ok

        missing = check_binding(
            frozen,
            snapshot=build_snapshot([], config=config, bindings={}),
            bindings={"echo": READ},
            protocol=config.protocol,
            config_revision=config.config_revision,
            authorization_scope=config.authorization_scope,
        )
        assert not missing.ok and missing.code == "missing"
        with pytest.raises(McpDriftRefusal):
            missing.raise_for_refusal()

        revoked = check_binding(
            frozen,
            snapshot=snapshot,
            bindings={},
            protocol=config.protocol,
            config_revision=config.config_revision,
            authorization_scope=config.authorization_scope,
        )
        assert not revoked.ok and revoked.code == "revoked"

        # Recovery re-freezes the catalog from the *current* deployment bindings, so a
        # changed policy shows up as a changed version in the new snapshot.
        moved = McpToolBinding("echo", effects=frozenset({ToolEffect.READ}), policy_revision="2")
        changed_snapshot = build_snapshot(directory.descriptors, config=config, bindings={"echo": moved})
        changed = check_binding(
            frozen,
            snapshot=changed_snapshot,
            bindings={"echo": moved},
            protocol=config.protocol,
            config_revision=config.config_revision,
            authorization_scope=config.authorization_scope,
        )
        assert not changed.ok and changed.code in {"changed", "policy_changed"}

        protocol_moved = check_binding(
            frozen,
            snapshot=snapshot,
            bindings={"echo": READ},
            protocol="2026-07-28",
            config_revision=config.config_revision,
            authorization_scope=config.authorization_scope,
        )
        assert not protocol_moved.ok and protocol_moved.code == "protocol_changed"

        scope_moved = check_binding(
            frozen,
            snapshot=snapshot,
            bindings={"echo": READ},
            protocol=config.protocol,
            config_revision=config.config_revision,
            authorization_scope="scope-b",
        )
        assert not scope_moved.ok and scope_moved.code == "authorization_revoked"

    assert facts_of(facts, "server.call_tool.start") == [], "drift checks never dispatch"


async def test_invalid_arguments_never_reach_the_server(tmp_path) -> None:
    facts, state = tmp_path / "facts.jsonl", tmp_path / "state"
    async with McpClientManager() as manager:
        config = mcp_stdio_config(facts, state)
        connection = await manager.connect(config)
        snapshot = await discover_catalog(manager, connection, config=config, bindings={"echo": READ})
        definitions = build_definitions(snapshot, lambda entry: (lambda _c, _a: None))
        definition = definitions[0]
        # A definition normalises every validator failure into the harness's own
        # invalid-arguments error, and the remote schema is still the authority.
        with pytest.raises(ToolArgsInvalid):
            definition.validate({})  # "text" is required by the remote schema
        with pytest.raises(ToolArgsInvalid):
            definition.validate({"text": "ok", "unexpected": 1})
        with pytest.raises(ToolArgsInvalid):
            definition.validate({"text": 5})
        assert McpInvalidArguments is not None
        definition.validate({"text": "ok"})

    assert facts_of(facts, "server.call_tool.start") == []


async def test_a_write_binding_without_a_preview_is_refused() -> None:
    with pytest.raises(McpConfigurationError):
        McpToolBinding("bump", effects=frozenset({ToolEffect.WRITE}))


async def test_definitions_are_built_only_for_the_delegated_subset(tmp_path) -> None:
    facts, state = tmp_path / "facts.jsonl", tmp_path / "state"
    async with McpClientManager() as manager:
        config = mcp_stdio_config(facts, state)
        connection = await manager.connect(config)
        snapshot = await discover_catalog(manager, connection, config=config, bindings={"echo": READ, "bump": WRITE})
        subset = build_definitions(snapshot, lambda entry: (lambda _c, _a: None), only=["bump"])
        assert [definition.name for definition in subset] == [local_alias("fixture", "bump")]
        with pytest.raises(McpConfigurationError):
            build_definitions(snapshot, lambda entry: (lambda _c, _a: None), only=["explode"])


def test_identity_shaped_schema_fields_are_reported() -> None:
    found = identity_shaped_fields(
        {
            "type": "object",
            "properties": {
                "namespace": {"type": "string"},
                "api_key": {"type": "string"},
                "documents": {"type": "array", "items": {"type": "object", "properties": {"token": {}}}},
                "benign": {"type": "string"},
            },
        }
    )
    assert found == ("api_key", "namespace", "token")


async def test_a_write_definition_carries_a_synchronous_local_preview(tmp_path) -> None:
    facts, state = tmp_path / "facts.jsonl", tmp_path / "state"
    async with McpClientManager() as manager:
        config = mcp_stdio_config(facts, state)
        connection = await manager.connect(config)
        snapshot = await discover_catalog(manager, connection, config=config, bindings={"bump": WRITE})
        definition = build_definitions(snapshot, lambda entry: (lambda _c, _a: None))[0]
    preview = definition.preview_for(None, {"key": "k"})
    assert preview["remote_tool"] == "bump"
    assert preview["arguments"] == {"key": "k"}
    assert preview["host_preview"] == {"key": "k", "effect": "increment"}
