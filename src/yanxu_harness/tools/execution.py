"""Layer 4, part two: the execution backend and the effect ledger.

One call has one authority. This module defines the two mutually exclusive
ownership modes a host may choose, and the receipt vocabulary both of them must
produce:

* **generic mode** — :class:`ToolExecutionBackend` from this package records the
  intent, invokes the handler and settles the ledger.
* **delegated mode** — the host's own gateway keeps owning the ledger and the
  approvals; the harness calls it and records the receipt it returns. The host
  must not let the harness create a *second* intent or a *second* approval for
  the same call, which is why the port has no "record an approval" method: the
  gateway that owns the ledger owns the decision it needs.

The states are the point of the module. ``unknown`` is a first-class outcome: an
external effect that may or may not have happened. It is never retried
automatically (:func:`should_retry`), never reported as success, and needs
business evidence to be reconciled.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Protocol, runtime_checkable

from ..errors import (
    ContractError,
    OutcomeUnknown,
)
from .definition import ToolDefinition
from .outcomes import (
    ToolArgsInvalid,
    ToolCancelled,
    ToolDeferred,
    ToolRejected,
)

__all__ = [
    "AdmissionDecision",
    "AdmissionResult",
    "EffectState",
    "ObservedOperation",
    "OperationObservation",
    "RecoverableLedger",
    "ToolArgsInvalid",
    "ToolCancelled",
    "ToolDeferred",
    "ToolExecutionBackend",
    "ToolIntent",
    "ToolReceipt",
    "ToolRejected",
    "classify_failure",
    "is_settled",
    "needs_reconciliation",
    "should_retry",
]


class EffectState(str, Enum):
    """The lifecycle of one tool call's side effect."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    #: The arguments were rejected before any effect. Distinct from `failed` on
    #: purpose: the host vocabulary already separates "fix the arguments and try
    #: again" from "the tool failed", and collapsing them loses the one thing the
    #: model needs to do next.
    INVALID_ARGS = "invalid_args"
    UNKNOWN = "unknown"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    #: The tool (or the gateway) decided to wait for a human. Not a failure and
    #: not settled: nothing may proceed until a decision exists.
    DEFERRED = "deferred"

    @property
    def settled(self) -> bool:
        return self in _SETTLED


_SETTLED = frozenset(
    {
        EffectState.SUCCEEDED,
        EffectState.FAILED,
        EffectState.INVALID_ARGS,
        EffectState.UNKNOWN,
        EffectState.REJECTED,
        EffectState.CANCELLED,
    }
)


def is_settled(state: EffectState) -> bool:
    return state.settled


def needs_reconciliation(state: EffectState) -> bool:
    """True when the outcome cannot be proven without business evidence."""
    return state is EffectState.UNKNOWN


def should_retry(state: EffectState, *, idempotent: bool = False) -> bool:
    """Whether the harness may repeat the call on its own.

    ``unknown`` is never retried: a second call could double the external
    effect, and the plan forbids promoting an unproven outcome to a proven one.
    ``failed`` may be retried only when the tool declared itself idempotent.
    """
    if state in {
        EffectState.UNKNOWN,
        EffectState.RUNNING,
        EffectState.SUCCEEDED,
        EffectState.REJECTED,
        EffectState.DEFERRED,
        EffectState.INVALID_ARGS,
    }:
        return False
    if state is EffectState.CANCELLED:
        return False
    if state is EffectState.FAILED:
        return idempotent
    return state is EffectState.QUEUED


@dataclass(frozen=True)
class ToolIntent:
    """The durable record written *before* a handler runs."""

    operation_id: str
    namespace: str
    tool_name: str
    tool_version: str
    args_hash: str
    session_id: str = ""
    run_id: str = ""
    call_id: str = ""
    idempotency_key: str | None = None
    #: The model action (batch) this call was declared in. It is part of the *logical*
    #: call identity, not of the execution right: two turns of one run may reuse a
    #: tool-call id, and the ledger must not let the second one read the first one's
    #: receipt. Empty for a caller with no such concept (and for rows written before it
    #: existed), which stays readable and is never treated as a conflict.
    model_action_id: str = ""
    #: What the model actually asked for, before any normalization. A host
    #: gateway hashes *this* (its own argument fingerprint is computed over the
    #: raw request), so a backend that only received the validated model dump
    #: would disagree with the host about what was approved.
    raw_arguments: Mapping[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        for name in ("operation_id", "namespace", "tool_name", "tool_version", "args_hash"):
            if not getattr(self, name):
                raise ContractError(f"tool intent requires a non-empty {name}")


@dataclass(frozen=True)
class ToolReceipt:
    """What actually happened, as far as the harness can prove it."""

    operation_id: str
    state: EffectState
    result: Any = None
    detail: str = ""
    external_id: str | None = None
    #: Structured detail a tool attached (for example the target it refused).
    details: Mapping[str, Any] = field(default_factory=dict)
    at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not isinstance(self.state, EffectState):
            raise ContractError(f"receipt state must be an EffectState, got {self.state!r}")
        if self.state is EffectState.SUCCEEDED and self.result is None:
            raise ContractError(
                f"{self.operation_id}: a succeeded receipt must carry a result "
                "(an empty result is still a result)"
            )

    @property
    def settled(self) -> bool:
        return self.state.settled

    @property
    def needs_reconciliation(self) -> bool:
        return needs_reconciliation(self.state)

    def raise_for_state(self) -> None:
        """Turn a non-success receipt into the matching neutral tool outcome."""
        if self.state is EffectState.UNKNOWN:
            raise OutcomeUnknown(self.operation_id, self.detail)
        if self.state is EffectState.REJECTED:
            raise ToolRejected(self.detail or "rejected", details=dict(self.details))
        if self.state is EffectState.CANCELLED:
            raise ToolCancelled(self.detail or "cancelled")
        if self.state is EffectState.DEFERRED:
            raise ToolDeferred(self.detail or "deferred", metadata=dict(self.details))
        if self.state is EffectState.INVALID_ARGS:
            raise ToolArgsInvalid(self.detail or "invalid arguments")
        if self.state is EffectState.FAILED:
            raise ToolRejected(self.detail or "tool failed", details=dict(self.details))


def classify_failure(exc: BaseException, *, idempotent: bool = False) -> tuple[EffectState, str]:
    """Map a handler/provider exception onto an explicit effect state.

    Only the cases the harness can actually prove are classified as ``failed``.
    Anything that could have reached the outside world — a timeout, a dropped
    connection, an unexpected exception raised after the request was sent — is
    ``unknown``. Guessing here is how a double write happens.

    Argument validation normally happens *before* an intent exists (the gateway
    reports ``INVALID_ARGS`` and records nothing). When a ``ToolArgsInvalid``
    reaches here it came from a handler or from the *host's* gateway — either way
    it says "fix the arguments and try again", which is its own state
    (``INVALID_ARGS``) rather than a generic failure.
    """
    if isinstance(exc, ToolRejected):
        return EffectState.REJECTED, str(exc)
    if isinstance(exc, ToolDeferred):
        return EffectState.DEFERRED, str(exc)
    if isinstance(exc, ToolCancelled):
        return EffectState.CANCELLED, str(exc)
    if isinstance(exc, ToolArgsInvalid):
        return EffectState.INVALID_ARGS, str(exc)
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return EffectState.UNKNOWN, f"timed out before the outcome could be proven: {exc}"
    if isinstance(exc, (ConnectionError, OSError)):
        return EffectState.UNKNOWN, f"transport failed without a receipt: {exc}"
    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
        raise exc
    return EffectState.UNKNOWN, f"{type(exc).__name__}: {exc}"


class OperationObservation(str, Enum):
    """What the authoritative ledger says about one operation, and nothing more.

    The four answers are deliberately not two: "the ledger says there is no intent" and
    "the ledger cannot answer" lead to opposite decisions, and collapsing them is how a
    recovery process talks itself into re-running an effect.
    """

    #: The authoritative ledger confirms it holds no intent for this binding. Only a
    #: *candidate* for execution: atomic admission, approval and the execution right are
    #: still checked separately.
    ABSENT = "absent"
    #: An intent exists and no receipt can prove the effect either way. ``uncertain``:
    #: never dispatched automatically again.
    OPEN = "open"
    #: A receipt exists, with its own identity binding and effect state. ``unknown``
    #: receipts are still uncertain.
    RECEIPT = "receipt"
    #: The query failed, or this ledger does not offer the capability. Blocks
    #: conservatively and says why; never treated as ``absent``.
    UNAVAILABLE = "unavailable"

    @property
    def proves_absence(self) -> bool:
        return self is OperationObservation.ABSENT

    @property
    def may_have_run(self) -> bool:
        return self in {OperationObservation.OPEN, OperationObservation.RECEIPT}


@dataclass(frozen=True)
class ObservedOperation:
    """One operation's ledger state, as the recovery path reads it."""

    observation: OperationObservation
    operation_id: str
    receipt: ToolReceipt | None = None
    intent: Mapping[str, Any] | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.observation, OperationObservation):
            raise ContractError(
                f"an observation must be an OperationObservation, got {self.observation!r}"
            )
        if self.observation is OperationObservation.RECEIPT and self.receipt is None:
            raise ContractError("a receipt observation must carry the receipt it read")

    @property
    def reconciled(self) -> bool:
        """True when the ledger proves an effect that no longer needs a decision."""
        return (
            self.observation is OperationObservation.RECEIPT
            and self.receipt is not None
            and not self.receipt.needs_reconciliation
        )


class AdmissionDecision(str, Enum):
    """The outcome of one *atomic* admission attempt.

    Distinguishable on purpose: a caller that cannot tell "you may run" from "something
    is already there" is the caller that runs an effect twice.
    """

    #: This call obtained the right to run. It is the only decision that may execute.
    ADMITTED = "admitted"
    #: An intent for this binding is already open and unsettled. It must not run again.
    EXISTING_OPEN = "existing_open"
    #: A receipt already exists for this binding.
    EXISTING_RECEIPT = "existing_receipt"
    #: The operation id is bound to a *different* call (arguments, tool version, run or
    #: model action). Refused: reusing it would either duplicate an effect or read one
    #: that belongs to another call.
    BINDING_CONFLICT = "binding_conflict"

    @property
    def may_run(self) -> bool:
        return self is AdmissionDecision.ADMITTED


@dataclass(frozen=True)
class AdmissionResult:
    """What atomic admission decided, with whatever the ledger already knew."""

    decision: AdmissionDecision
    operation_id: str
    receipt: ToolReceipt | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.decision, AdmissionDecision):
            raise ContractError(f"admission must be an AdmissionDecision, got {self.decision!r}")
        if self.decision is AdmissionDecision.EXISTING_RECEIPT and self.receipt is None:
            raise ContractError("an existing-receipt admission must carry the receipt")


@runtime_checkable
class RecoverableLedger(Protocol):
    """The optional read/admission capability an authoritative ledger may implement.

    Deliberately separate from :class:`ToolExecutionBackend` so an adapter written before
    this capability existed keeps working on the normal execution path: the gateway
    prefers the strong contract when the backend offers it, and falls back to the legacy
    ``begin`` when it does not. What the fallback must never do is *claim* the strong
    contract: a host without it simply has no automatic recovery guarantee.
    """

    async def observe(self, intent: ToolIntent) -> ObservedOperation:
        """Read one operation's authoritative state. Never writes."""

    async def admit(self, intent: ToolIntent) -> AdmissionResult:
        """Atomically record-or-read the intent and decide whether this call may run."""


@runtime_checkable
class ToolExecutionBackend(Protocol):
    """The single owner of intent, execution and receipt for one tool call.

    Implementations advertise which mode they are in with :attr:`ownership`:

    ``"harness"``
        this backend owns the ledger (the package's generic backend),
    ``"host"``
        the host's gateway owns the ledger and this backend delegates to it
        unchanged, returning the host's receipt.
    """

    @property
    def ownership(self) -> str:
        """``"harness"`` or ``"host"``."""

    @property
    def durable(self) -> bool:
        """True when the ledger survives a process restart."""

    async def begin(self, intent: ToolIntent) -> ToolReceipt | None:
        """Record the intent, or return the receipt already recorded for it."""

    async def execute(
        self,
        intent: ToolIntent,
        definition: ToolDefinition,
        arguments: Any,
        context: Any,
    ) -> ToolReceipt:
        """Run one validated call and return its receipt."""

    async def settle(self, receipt: ToolReceipt) -> None:
        """Persist the receipt (and the events that describe it)."""
