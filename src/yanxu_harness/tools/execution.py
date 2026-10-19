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
import json
import time
from dataclasses import dataclass, field, fields, is_dataclass
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
    "MAX_NEUTRAL_DETAIL_CHARS",
    "MAX_NEUTRAL_DETAIL_KEYS",
    "NEUTRAL_RESULT_STATES",
    "ObservedOperation",
    "OperationObservation",
    "RecoverableLedger",
    "ToolArgsInvalid",
    "ToolCancelled",
    "ToolDeferred",
    "ToolExecutionBackend",
    "ToolExecutionResult",
    "ToolIntent",
    "ToolReceipt",
    "ToolRejected",
    "classify_failure",
    "is_settled",
    "needs_reconciliation",
    "receipt_from_handler_result",
    "should_retry",
    "storable_value",
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


# -- a handler's neutral, explicit outcome --------------------------------

#: The states a handler may declare in this release. Everything else a call can
#: end in (denied, awaiting approval, invalid arguments, cancelled) is decided by
#: the gateway *before* or *around* execution and is not a handler's verdict.
NEUTRAL_RESULT_STATES = frozenset(
    {EffectState.SUCCEEDED, EffectState.FAILED, EffectState.UNKNOWN}
)

#: Bounds on the free-form detail a neutral result may attach. They exist so an
#: adapter that forwards remote text cannot grow the ledger without limit; the
#: full remote payload belongs in the (bounded) result or in the tool's own
#: artifact store, not in receipt details.
MAX_NEUTRAL_DETAIL_KEYS = 32
MAX_NEUTRAL_DETAIL_CHARS = 4000


def storable_value(value: Any) -> Any:
    """Return a JSON-storable form of one value, or raise ``TypeError``.

    This is the single definition of "the ledger can keep this": the SQLite
    backend's ``json.dumps(default=...)`` and the neutral-result normaliser below
    both go through it, so "storable" cannot mean two different things. JSON-
    native values pass through unchanged; core content blocks (dataclasses) and
    pydantic models are converted by their own serialisation hooks rather than
    rejected; anything else raises instead of being silently stringified.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [storable_value(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): storable_value(item) for key, item in value.items()}
    for attribute in ("model_dump", "to_json"):
        method = getattr(value, attribute, None)
        if callable(method):
            try:
                return method(mode="json")
            except TypeError:
                return method()
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=repr)
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: getattr(value, field.name) for field in fields(value)}
    raise TypeError(f"result of type {type(value).__name__} is not storable")


@dataclass(frozen=True)
class ToolExecutionResult:
    """What a handler says happened, in the harness's own vocabulary.

    A handler may return any value; that stays the meaning it always had (the
    call succeeded, the value is the result). This type is the *explicit* form,
    for adapters that must not let a foreign result shape decide the ledger:

    * ``FAILED`` — the remote side reported a failure. The effect did not
      succeed as far as anyone can prove; it does **not** mean the world is
      unchanged (a partial write is still possible) and it is not retried.
    * ``UNKNOWN`` — the caller cannot prove whether the effect happened. The
      gateway reports it as an unsettled operation and stops the run.

    The type is deliberately vendor-neutral: no SDK model, socket or error
    object may appear here, and it carries no operation id — a handler cannot
    rename the call it is running under (see
    :func:`receipt_from_handler_result`).
    """

    state: EffectState
    #: Persistable payload for ``SUCCEEDED``. ``None`` is refused on purpose:
    #: an empty success is ``{}`` or ``[]``, never "no result".
    result: Any = None
    detail: str = ""
    #: Bounded, whitelisted machine-readable facts (see the module constants).
    details: Mapping[str, Any] = field(default_factory=dict)
    external_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, EffectState):
            raise ContractError(f"a neutral result state must be an EffectState, got {self.state!r}")
        if self.state not in NEUTRAL_RESULT_STATES:
            raise ContractError(
                f"a handler's neutral result may be SUCCEEDED, FAILED or UNKNOWN; "
                f"got {self.state.value!r} (the gateway decides the other states)"
            )
        if self.state is EffectState.SUCCEEDED and self.result is None:
            raise ContractError("a succeeded neutral result must carry a result (use {} for an empty one)")
        if self.state is not EffectState.SUCCEEDED and not self.detail:
            raise ContractError(f"a {self.state.value} neutral result must say what happened in detail")
        if not isinstance(self.details, Mapping):
            raise ContractError(f"neutral result details must be a mapping, got {type(self.details).__name__}")
        if len(self.details) > MAX_NEUTRAL_DETAIL_KEYS:
            raise ContractError(
                f"neutral result details carry at most {MAX_NEUTRAL_DETAIL_KEYS} keys, "
                f"got {len(self.details)}"
            )
        for key, value in self.details.items():
            if not isinstance(key, str) or not key:
                raise ContractError(f"neutral result detail keys must be non-empty strings, got {key!r}")
            if len(str(value)) > MAX_NEUTRAL_DETAIL_CHARS:
                raise ContractError(
                    f"neutral result detail {key!r} exceeds {MAX_NEUTRAL_DETAIL_CHARS} characters"
                )
        object.__setattr__(self, "details", dict(self.details))


def receipt_from_handler_result(
    operation_id: str, value: Any, *, mutating: bool = False
) -> ToolReceipt:
    """Turn whatever a handler returned into a receipt under the gateway's identity.

    Plain values keep their original meaning: the call succeeded and the value is
    its result. A :class:`ToolExecutionResult` maps onto the receipt vocabulary
    explicitly, which is what lets an adapter report a remote failure or an
    unprovable outcome without inventing a second ledger entry.

    ``mutating`` describes the *definition*, not the value: when a declared
    success cannot be made storable, a tool that may have changed something is
    ``UNKNOWN`` (the effect may have happened; the display is what failed), while
    a read-only tool is ``FAILED``. Both are decided here, before the receipt is
    persisted, instead of surfacing as a settle-time crash.
    """
    if not isinstance(value, ToolExecutionResult):
        return ToolReceipt(operation_id=operation_id, state=EffectState.SUCCEEDED, result=value)

    if value.state is EffectState.SUCCEEDED:
        try:
            payload = storable_value(value.result)
            json.dumps(payload, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError) as exc:
            state = EffectState.UNKNOWN if mutating else EffectState.FAILED
            return ToolReceipt(
                operation_id=operation_id,
                state=state,
                detail=f"the declared result could not be represented: {exc}",
                details={"result_unrepresentable": True, **dict(value.details)},
                external_id=value.external_id,
            )
        return ToolReceipt(
            operation_id=operation_id,
            state=EffectState.SUCCEEDED,
            result=payload,
            detail=value.detail,
            details=dict(value.details),
            external_id=value.external_id,
        )

    return ToolReceipt(
        operation_id=operation_id,
        state=value.state,
        detail=value.detail,
        details=dict(value.details),
        external_id=value.external_id,
    )


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
