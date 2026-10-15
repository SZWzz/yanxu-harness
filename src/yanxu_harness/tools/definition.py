"""Tool definitions: what a tool is, and what it must declare.

A definition is a *contract*, not a callable. It states the effects the tool can
have, how it may run (parallel or sequential), how its arguments are validated,
what its result limit is, and which other tools it depends on. The rules
enforced here come from the four-layer tool model:

1. **Definition and registration.** A stable name and version, an argument
   validator, the declared effects, the execution mode, a result limit.
2. **Deployment and visibility.** Handled by the deployment policy, not here: a
   definition existing says nothing about it being offered to a model.
3. **Declaration and discovery.** Handled by the capability catalog.
4. **Authorization and execution.** Every side effect goes through the gateway;
   there is no "call the handler directly" path in this package.

Two constructors matter in practice:

``ToolDefinition``
    A normal tool. ``args_validator`` is mandatory whenever a ``handler`` is
    present: a validator is the only authority on whether arguments are
    acceptable, and a schema alone is not a validator.
``ToolDefinition.read_only``
    A convenience for read tools.

A write-effect definition must also declare a ``preview``. A preview is what an
author approves; approving "a write" without a concrete, hashable description of
the target change is exactly the failure mode approvals exist to prevent.
"""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Awaitable, Callable, FrozenSet, Iterable, Mapping

from ..errors import ContractError
from .outcomes import ToolArgsInvalid

__all__ = [
    "ArgsValidator",
    "ExecutionMode",
    "Idempotency",
    "ToolDefinition",
    "ToolEffect",
    "ToolHandler",
    "ToolPreview",
]

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,119}$")

#: ``(arguments) -> validated arguments``. Raises :class:`ToolArgsInvalid`.
ArgsValidator = Callable[[Mapping[str, Any]], Any]
ToolHandler = Callable[[Any, Any], Any]
ToolPreview = Callable[[Any, Any], Mapping[str, Any]]


class ToolEffect(str, Enum):
    """What a tool can do to the world.

    Framework-neutral on purpose: a novel tool and a spreadsheet tool use the
    same vocabulary, and the generic package contains no domain enumeration.
    """

    READ = "read"
    PROPOSE = "propose"
    WRITE = "write"


class ExecutionMode(str, Enum):
    """Whether the tool may run beside its batch siblings."""

    PARALLEL = "parallel"
    SEQUENTIAL = "sequential"


class Idempotency(str, Enum):
    """Whether repeating one call with the same arguments is safe."""

    NONE = "none"
    KEYED = "keyed"


@dataclass(frozen=True)
class ToolDefinition:
    """One tool's declared contract."""

    name: str
    description: str
    version: str = "1"
    effects: FrozenSet[ToolEffect] = frozenset({ToolEffect.READ})
    args_validator: ArgsValidator | None = None
    handler: ToolHandler | None = None
    preview: ToolPreview | None = None
    execution_mode: ExecutionMode = ExecutionMode.PARALLEL
    idempotency: Idempotency = Idempotency.NONE
    idempotency_key: Callable[[Any, Any], str] | None = None
    requires: FrozenSet[str] = frozenset()
    args_schema: Mapping[str, Any] | Callable[[], Mapping[str, Any]] | None = None
    result_char_limit: int = 8000
    #: Deployment capability this tool belongs to. ``None`` means the tool is
    #: only reachable when a deployment names it explicitly.
    capability: str | None = None

    def __post_init__(self) -> None:
        if not _NAME_RE.match(self.name or ""):
            raise ContractError(
                f"tool name {self.name!r} is not a stable id "
                "(lowercase letters, digits, dot, dash, underscore; max 120 chars)"
            )
        if not self.description:
            raise ContractError(f"{self.name}: a tool must describe itself")
        if not self.version:
            raise ContractError(f"{self.name}: a tool must declare a version")
        object.__setattr__(self, "effects", frozenset(self.effects))
        if not self.effects:
            raise ContractError(f"{self.name}: effects must not be empty")
        if self.handler is not None and self.args_validator is None:
            raise ContractError(
                f"{self.name}: an executable tool must bind an argument validator; "
                "a JSON schema alone is not a validator"
            )
        if self.handler is None and self.args_validator is not None:
            raise ContractError(f"{self.name}: an argument validator without a handler is dead weight")
        if ToolEffect.WRITE in self.effects and self.handler is not None and self.preview is None:
            raise ContractError(
                f"{self.name}: a write tool must declare a preview so an approval can bind to it"
            )
        if self.idempotency is Idempotency.KEYED and self.idempotency_key is None:
            raise ContractError(f"{self.name}: idempotency=keyed requires an idempotency_key callable")
        if self.idempotency is Idempotency.NONE and self.idempotency_key is not None:
            raise ContractError(
                f"{self.name}: an idempotency_key without idempotency=keyed would not be used"
            )
        if self.result_char_limit <= 0:
            raise ContractError(f"{self.name}: result_char_limit must be positive")
        object.__setattr__(self, "requires", frozenset(self.requires))
        if self.name in self.requires:
            raise ContractError(f"{self.name}: a tool must not require itself")

    # -- classification -------------------------------------------------

    @property
    def executable(self) -> bool:
        return self.handler is not None

    @property
    def writes(self) -> bool:
        """True when the tool may change authoritative state."""
        return ToolEffect.WRITE in self.effects

    @property
    def mutating(self) -> bool:
        """True when the tool may change anything, including proposed state."""
        return bool(self.effects & {ToolEffect.WRITE, ToolEffect.PROPOSE})

    @property
    def runs_in_parallel(self) -> bool:
        return self.execution_mode is ExecutionMode.PARALLEL

    # -- schema and validation ------------------------------------------

    def schema(self) -> Mapping[str, Any] | None:
        """Return the JSON schema the model sees, or ``None`` when none is declared.

        When ``args_schema`` is callable it is called (that is how a lazy
        Pydantic-backed schema avoids importing the extra at module import).
        """
        schema = self.args_schema
        if schema is None:
            model = getattr(self.args_validator, "__self__", None)
            generator = getattr(model, "model_json_schema", None)
            if callable(generator):
                return generator()
            return None
        if callable(schema):
            return schema()
        return schema

    def validate(self, arguments: Mapping[str, Any]) -> Any:
        """Run the declared validator, normalising every failure to the harness error."""
        if self.args_validator is None:
            raise ToolArgsInvalid(f"{self.name} does not accept call arguments")
        if not isinstance(arguments, Mapping):
            raise ToolArgsInvalid(f"{self.name}: arguments must be a mapping")
        try:
            return self.args_validator(arguments)
        except ToolArgsInvalid:
            raise
        except Exception as exc:  # noqa: BLE001 - the validator is host code
            raise ToolArgsInvalid(f"{self.name}: {exc}") from exc

    def with_overrides(self, **changes: Any) -> "ToolDefinition":
        """Return a copy with overridden fields (used by deployment wrappers)."""
        return replace(self, **changes)

    async def call(self, context: Any, arguments: Any) -> Any:
        """Invoke the handler, awaiting it when needed.

        This is the *only* place a handler is invoked, and it is called by the
        execution backend, never by the model loop directly.
        """
        if self.handler is None:
            raise ContractError(f"{self.name} has no handler")
        result = self.handler(context, arguments)
        if inspect.isawaitable(result):
            return await result
        return result

    def preview_for(self, context: Any, arguments: Any) -> Mapping[str, Any] | None:
        """Return the approval preview, awaiting a coroutine preview if needed."""
        if self.preview is None:
            return None
        preview = self.preview(context, arguments)
        if inspect.isawaitable(preview):
            # Close it instead of leaking an un-awaited coroutine warning.
            close = getattr(preview, "close", None)
            if callable(close):
                close()
            raise ContractError(
                f"{self.name}: preview must be synchronous so approvals bind before any await"
            )
        return preview

    def stable_id(self) -> str:
        """``name@version`` — what a decision or an approval may reference."""
        return f"{self.name}@{self.version}"


def read_only(
    name: str,
    description: str,
    validator: ArgsValidator,
    handler: ToolHandler,
    **kwargs: Any,
) -> ToolDefinition:
    """Build a read-only definition without repeating the effect declaration."""
    kwargs.setdefault("effects", frozenset({ToolEffect.READ}))
    return ToolDefinition(name, description, args_validator=validator, handler=handler, **kwargs)
