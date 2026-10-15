"""The ``Harness`` port: run lifecycle, terminal states and turn budgets.

This module declares the lifecycle contract before the loop implementation
exists (that comes with the core migration). The contract is:

* :class:`RunStatus` distinguishes *how* a run ended. ``unknown`` is not a
  status: a provider failure is ``failed``, an unprovable external effect is
  reported by the tool layer, and a budget stop is ``usage_limited``.
* A run that stopped because the turn budget ran out is ``usage_limited``, not
  ``completed``: an unfinished task must never look finished.
* A suspended run is not terminal. It is resumed with an identity that keeps the
  original namespace/session/run and issues a new attempt.
* ``max_turns`` accepts ``None`` (unbounded) or a positive integer. ``0``,
  negative numbers, booleans and other types are refused *before* any model
  request, tool effect or lifecycle event — never silently treated as "no limit".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from ..errors import ConfigurationError, ContractError
from .context import RunContext

__all__ = [
    "Harness",
    "RunOutcome",
    "RunRequest",
    "RunState",
    "RunStatus",
    "TERMINAL_STATUSES",
    "is_terminal_status",
    "validate_max_turns",
]


class RunStatus(str, Enum):
    """How a run invocation ended."""

    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUSPENDED = "suspended"
    USAGE_LIMITED = "usage_limited"
    REJECTED = "rejected"


#: Statuses after which no further progress can happen without a new invocation.
TERMINAL_STATUSES = frozenset(
    {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.USAGE_LIMITED, RunStatus.REJECTED}
)


def is_terminal_status(status: RunStatus | str) -> bool:
    return RunStatus(status) in TERMINAL_STATUSES


MAX_TURNS_ERROR = "invalid_max_turns"


def validate_max_turns(value: object) -> int | None:
    """Return ``None`` or a positive int, or raise :class:`ConfigurationError`.

    ``bool`` is rejected explicitly: in Python ``True`` is an ``int``, and a
    caller that accidentally passes a flag would otherwise get a one-turn run.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(
            f"{MAX_TURNS_ERROR}: max_turns must be None or a positive integer, got {value!r}"
        )
    if value <= 0:
        raise ConfigurationError(
            f"{MAX_TURNS_ERROR}: max_turns must be positive, got {value!r}; "
            "use None for an unbounded run"
        )
    return value


@dataclass(frozen=True)
class RunRequest:
    """One model invocation the host asks for."""

    prompt: str = ""
    messages: Sequence[Any] = ()
    max_turns: int | None = None
    labels: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        validate_max_turns(self.max_turns)


@dataclass(frozen=True)
class RunOutcome:
    """What the host gets back from one invocation."""

    status: RunStatus
    run_id: str
    final_text: str = ""
    usage: Mapping[str, Any] = field(default_factory=dict)
    pending_interaction: Mapping[str, Any] | None = None
    error_code: str | None = None
    detail: str = ""
    #: Logical assistant turns actually started, so a caller can tell
    #: ``usage_limited`` apart from a normal stop without guessing.
    turns_used: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.status, RunStatus):
            raise ContractError(f"run outcome status must be a RunStatus, got {self.status!r}")
        if self.status is RunStatus.SUSPENDED and self.pending_interaction is None:
            raise ContractError("a suspended run must carry the pending interaction it waits for")
        if self.status is RunStatus.USAGE_LIMITED and not self.error_code:
            raise ContractError("a usage-limited run must carry a machine-readable error_code")


@dataclass(frozen=True)
class RunState:
    """The durable state a host can read back at any time."""

    run_id: str
    status: RunStatus
    turns_used: int = 0
    attempt_id: str = ""
    pending_interaction: Mapping[str, Any] | None = None
    updated_at: float = 0.0


@runtime_checkable
class Harness(Protocol):
    """The only supported entry point for running a model loop."""

    async def start(self, request: RunRequest, context: RunContext) -> RunOutcome:
        """Run one invocation to a terminal state or a suspension."""

    async def resume(self, run_id: str, resolution: Mapping[str, Any], context: RunContext) -> RunOutcome:
        """Continue a suspended run under a new attempt id."""

    async def cancel(self, run_id: str, reason: str = "") -> None:
        """Ask the run to stop. Idempotent; a finished run is unaffected."""

    async def state(self, run_id: str) -> RunState:
        """Read the durable state of a run."""
