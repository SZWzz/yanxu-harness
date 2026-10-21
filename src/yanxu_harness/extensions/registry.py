"""A read-only view of the definitions a deployment published.

``ToolGateway`` only needs ``get``; hosts and diagnostics need the same read
surface :class:`~yanxu_harness.tools.registry.ToolRegistry` offers. This view
deliberately exposes no ``register``/``replace``: once a deployment is READY, a
caller holding the registry cannot add a tool behind the frozen identity.
"""

from __future__ import annotations

from typing import Iterable, Sequence

from ..tools.definition import ToolDefinition
from ..tools.registry import ToolCatalogEntry, ToolRegistry

__all__ = ["FrozenToolRegistry"]


class FrozenToolRegistry:
    """The immutable tool set of one assembly snapshot."""

    def __init__(self, definitions: Iterable[ToolDefinition]) -> None:
        # ToolRegistry performs the authoritative duplicate/cycle/dependency
        # checks; this wrapper keeps its storage private and exposes reads only.
        self._registry = ToolRegistry(tuple(definitions))

    def get(self, name: str) -> ToolDefinition:
        return self._registry.get(name)

    def maybe_get(self, name: str) -> ToolDefinition | None:
        return self._registry.maybe_get(name)

    def __contains__(self, name: object) -> bool:
        return name in self._registry

    def __len__(self) -> int:
        return len(self._registry)

    def names(self) -> tuple[str, ...]:
        return self._registry.names()

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return self._registry.definitions()

    def writes(self) -> tuple[ToolDefinition, ...]:
        return self._registry.writes()

    def resolve_dependencies(self, name: str) -> tuple[ToolDefinition, ...]:
        return self._registry.resolve_dependencies(name)

    def missing_dependencies(self) -> dict[str, tuple[str, ...]]:
        return self._registry.missing_dependencies()

    def catalog(self, visible: Sequence[str] | None = None) -> tuple[ToolCatalogEntry, ...]:
        names = self.names() if visible is None else tuple(sorted(set(visible)))
        return tuple(ToolCatalogEntry.of(self.get(name)) for name in names)
