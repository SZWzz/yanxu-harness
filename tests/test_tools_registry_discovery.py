"""Registration invariants, dependency resolution, and discovery timing.

The discovery tests encode the rule that matters most in practice: asking for a
capability in one batch never grants it in that same batch, and an oversized
request is refused *as a whole* instead of being silently truncated.
"""

from __future__ import annotations

import pytest

from yanxu_harness.errors import ContractError
from yanxu_harness.tools import (
    CapabilityCatalog,
    CapabilityRequest,
    DiscoveryOutcome,
    ToolDefinition,
    ToolRegistry,
    catalog,
)


def validator(arguments: dict) -> dict:
    return {"value": arguments.get("value", "")}


def handler(_context, arguments):
    return arguments


def tool(name: str, *, requires: tuple[str, ...] = ()) -> ToolDefinition:
    return ToolDefinition(
        name, f"{name} description", args_validator=validator, handler=handler, requires=frozenset(requires)
    )


def test_registering_the_same_name_twice_is_refused() -> None:
    registry = ToolRegistry([tool("notes.read")])
    with pytest.raises(ContractError, match="already registered"):
        registry.register(tool("notes.read"))


def test_a_registry_refuses_a_duck_typed_definition() -> None:
    registry = ToolRegistry()
    with pytest.raises(ContractError, match="ToolDefinition"):
        registry.register({"name": "fake"})  # type: ignore[arg-type]


def test_a_cycle_is_refused_whenever_it_appears() -> None:
    registry = ToolRegistry()
    registry.register(tool("a", requires=("b",)))
    # The cycle is only complete once both are present, so registration order
    # must not let it through: the second registration is the one that fails.
    with pytest.raises(ContractError, match="cycle"):
        registry.register(tool("b", requires=("a",)))
    assert registry.names() == ("a",)
    assert registry.missing_dependencies() == {"a": ("b",)}
    with pytest.raises(ContractError, match="cycle"):
        ToolRegistry([tool("a", requires=("b",)), tool("b", requires=("a",))])


def test_a_plain_cycle_among_three_tools_is_refused() -> None:
    with pytest.raises(ContractError, match="cycle"):
        ToolRegistry(
            [
                tool("a", requires=("b",)),
                tool("b", requires=("c",)),
                tool("c", requires=("a",)),
            ]
        )


def test_an_unregistered_dependency_is_reported_not_ignored() -> None:
    registry = ToolRegistry([tool("a", requires=("missing.tool",))])
    assert registry.missing_dependencies() == {"a": ("missing.tool",)}
    with pytest.raises(ContractError, match="requires unregistered tool"):
        registry.resolve_dependencies("a")


def test_the_dependency_closure_puts_dependencies_first() -> None:
    registry = ToolRegistry(
        [
            tool("c"),
            tool("b", requires=("c",)),
            tool("a", requires=("b",)),
        ]
    )
    assert [definition.name for definition in registry.resolve_dependencies("a")] == ["c", "b", "a"]


def test_lookups_and_listings_are_stable() -> None:
    registry = ToolRegistry([tool("b"), tool("a")])
    assert registry.names() == ("a", "b")
    assert "a" in registry
    with pytest.raises(ContractError, match="not a registered tool"):
        registry.get("missing")
    assert registry.maybe_get("missing") is None


def test_replace_restores_the_previous_definition_when_the_new_one_is_broken() -> None:
    registry = ToolRegistry([tool("a"), tool("b", requires=("a",))])
    with pytest.raises(ContractError):
        registry.replace(tool("a", requires=("b",)))
    assert registry.get("a").requires == frozenset()


def _catalog(names: tuple[str, ...], *, capacity: int, declared: tuple[str, ...] | None = None) -> CapabilityCatalog:
    registry = ToolRegistry([tool(name) for name in names])
    return CapabilityCatalog(catalog(registry), capacity=capacity, declared=declared)


def test_a_discovered_capability_only_takes_effect_in_the_next_batch() -> None:
    catalog_ = _catalog(("a", "b", "c"), capacity=8, declared=("a",))
    result = catalog_.request(CapabilityRequest(call_id="call-1", ids=("b",)))
    assert result.outcome is DiscoveryOutcome.QUEUED
    assert result.active == ("a",)
    assert result.queued == ("b",)
    assert catalog_.declared_ids() == ("a",)
    assert catalog_.promote() == ("a", "b")


def test_an_unknown_capability_id_is_rejected_with_a_reason() -> None:
    catalog_ = _catalog(("a",), capacity=8)
    result = catalog_.request(CapabilityRequest(call_id="call-1", ids=("a", "sql.eval")))
    assert result.outcome is DiscoveryOutcome.UNKNOWN_IDS
    assert result.rejected == {"sql.eval": "unknown capability id"}
    assert result.queued == ()


def test_a_hidden_capability_is_reported_as_not_exposed() -> None:
    catalog_ = _catalog(("a", "b"), capacity=8)
    catalog_.hide("b")
    result = catalog_.request(CapabilityRequest(call_id="call-1", ids=("b",)))
    assert result.rejected == {"b": "not exposed by this deployment"}


def test_an_oversized_request_is_refused_whole_not_truncated() -> None:
    names = tuple(f"tool.{index}" for index in range(10))
    catalog_ = _catalog(names, capacity=4)
    result = catalog_.request(CapabilityRequest(call_id="call-1", ids=names))
    assert result.outcome is DiscoveryOutcome.OVER_CAPACITY
    assert result.queued == ()
    assert "capacity" in result.rejected
    assert "not truncated" in result.rejected["capacity"]
    # Nothing was silently activated either.
    assert len(catalog_.declared_ids()) == 10


def test_an_empty_request_is_not_an_error() -> None:
    catalog_ = _catalog(("a",), capacity=8)
    result = catalog_.request(CapabilityRequest(call_id="call-1"))
    assert result.outcome is DiscoveryOutcome.EMPTY
    assert result.ok


def test_the_catalog_keeps_every_id_discoverable_even_when_undeclared() -> None:
    """A capability missing from the first batch is still discoverable later."""
    catalog_ = _catalog(("a", "b", "c"), capacity=8, declared=("a",))
    assert catalog_.ids() == ("a", "b", "c")
    assert catalog_.declared_ids() == ("a",)
    assert catalog_.request(CapabilityRequest(call_id="c1", ids=("c",))).queued == ("c",)


def test_a_capacity_limit_must_be_positive() -> None:
    with pytest.raises(ContractError):
        _catalog(("a",), capacity=0)
