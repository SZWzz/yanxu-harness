"""The tool registry: one place where definitions live and dependencies resolve.

Registration is where a broken contract has to fail, because everything after it
assumes the contract holds:

* a duplicate name is refused (two definitions for one name would make the
  model-facing catalog and the executor disagree),
* an executable tool without a validator is refused (already enforced by
  :class:`~yanxu_harness.tools.definition.ToolDefinition`, re-checked here so a
  registry cannot be fed by a duck-typed stand-in),
* a dependency on an unregistered tool is refused at resolution time instead of
  failing mid-run,
* dependency cycles are refused as soon as they appear.

The registry knows nothing about deployment, approval or execution. Knowing a
tool exists is not being allowed to call it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from ..errors import ContractError
from .definition import ToolDefinition

__all__ = ["ToolRegistry"]


class ToolRegistry:
    """An ordered, dependency-checked set of tool definitions."""

    def __init__(self, definitions: Iterable[ToolDefinition] = ()) -> None:
        self._definitions: dict[str, ToolDefinition] = {}
        for definition in definitions:
            self.register(definition)

    # -- registration ---------------------------------------------------

    def register(self, definition: ToolDefinition) -> ToolDefinition:
        """Add one definition, refusing anything that would break an invariant."""
        if not isinstance(definition, ToolDefinition):
            raise ContractError(
                f"registry accepts ToolDefinition, got {type(definition).__name__}"
            )
        if definition.name in self._definitions:
            raise ContractError(
                f"{definition.name}: already registered; a tool has exactly one definition"
            )
        if definition.executable and definition.args_validator is None:
            raise ContractError(
                f"{definition.name}: refusing an executable tool without an argument validator"
            )
        self._definitions[definition.name] = definition
        try:
            self._assert_acyclic()
        except ContractError:
            del self._definitions[definition.name]
            raise
        return definition

    def extend(self, definitions: Iterable[ToolDefinition]) -> None:
        for definition in definitions:
            self.register(definition)

    def replace(self, definition: ToolDefinition) -> ToolDefinition:
        """Replace an existing definition (used by host decorators/wrappers)."""
        if definition.name not in self._definitions:
            raise ContractError(f"{definition.name}: not registered")
        previous = self._definitions[definition.name]
        del self._definitions[definition.name]
        try:
            return self.register(definition)
        except ContractError:
            self._definitions[previous.name] = previous
            raise

    # -- lookups --------------------------------------------------------

    def get(self, name: str) -> ToolDefinition:
        try:
            return self._definitions[name]
        except KeyError:
            raise ContractError(f"{name}: not a registered tool") from None

    def maybe_get(self, name: str) -> ToolDefinition | None:
        return self._definitions.get(name)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._definitions

    def __len__(self) -> int:
        return len(self._definitions)

    def names(self) -> tuple[str, ...]:
        """Registered names in a stable order."""
        return tuple(sorted(self._definitions))

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(self._definitions[name] for name in self.names())

    def writes(self) -> tuple[ToolDefinition, ...]:
        return tuple(d for d in self.definitions() if d.writes)

    # -- dependencies ---------------------------------------------------

    def resolve_dependencies(self, name: str) -> tuple[ToolDefinition, ...]:
        """Return the transitive dependency closure of ``name``, dependencies first."""
        return self._closure({name}, ordered=True)

    def _closure(self, roots: Iterable[str], *, ordered: bool = False) -> tuple[ToolDefinition, ...]:
        seen: dict[str, ToolDefinition] = {}
        order: list[str] = []

        def visit(tool_name: str, trail: tuple[str, ...]) -> None:
            if tool_name in seen:
                return
            if tool_name in trail:
                cycle = " -> ".join((*trail, tool_name))
                raise ContractError(f"tool dependency cycle: {cycle}")
            definition = self._definitions.get(tool_name)
            if definition is None:
                raise ContractError(
                    f"{trail[-1] if trail else tool_name}: requires unregistered tool {tool_name!r}"
                )
            for dependency in sorted(definition.requires):
                visit(dependency, (*trail, tool_name))
            seen[tool_name] = definition
            order.append(tool_name)

        for root in roots:
            visit(root, ())
        if ordered:
            return tuple(seen[name] for name in order)
        return tuple(seen[name] for name in sorted(seen))

    def _assert_acyclic(self) -> None:
        """Refuse a cycle among *registered* tools, whatever order they arrived in.

        Unregistered dependencies are not an error here: a registry is often
        filled in dependency order and a definition may legitimately reference a
        tool that arrives later. A missing dependency is reported by
        :meth:`resolve_dependencies` (and by :meth:`missing_dependencies`) instead
        of blocking registration.
        """
        for name in self.names():
            self._visit(name, ())

    def _visit(self, name: str, trail: tuple[str, ...]) -> None:
        if name in trail:
            raise ContractError(f"tool dependency cycle: {' -> '.join((*trail, name))}")
        definition = self._definitions.get(name)
        if definition is None:
            return
        for dependency in sorted(definition.requires):
            self._visit(dependency, (*trail, name))

    def missing_dependencies(self) -> dict[str, tuple[str, ...]]:
        """Registered tools whose declared dependencies are not registered yet."""
        missing: dict[str, tuple[str, ...]] = {}
        for name in self.names():
            absent = tuple(sorted(d for d in self._definitions[name].requires if d not in self._definitions))
            if absent:
                missing[name] = absent
        return missing


@dataclass(frozen=True)
class ToolCatalogEntry:
    """The compact, model-facing entry for one tool."""

    name: str
    stable_id: str
    description: str
    effects: tuple[str, ...]
    requires: tuple[str, ...]
    args_schema: Mapping[str, object] | None

    @classmethod
    def of(cls, definition: ToolDefinition) -> "ToolCatalogEntry":
        return cls(
            name=definition.name,
            stable_id=definition.stable_id(),
            description=definition.description,
            effects=tuple(sorted(effect.value for effect in definition.effects)),
            requires=tuple(sorted(definition.requires)),
            args_schema=definition.schema(),
        )


def catalog(registry: ToolRegistry, visible: Sequence[str] | None = None) -> tuple[ToolCatalogEntry, ...]:
    """Build model-facing entries for ``visible`` tool names, in stable id order."""
    names = registry.names() if visible is None else tuple(sorted(set(visible)))
    return tuple(ToolCatalogEntry.of(registry.get(name)) for name in names)
