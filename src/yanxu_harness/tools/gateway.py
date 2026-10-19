"""The single gateway: the only path from a model tool call to a side effect.

Everything else in this package describes *what* a tool is. This module is where
a call actually happens, in a fixed order, with each layer able to stop it:

```text
deployment visibility  →  argument validation  →  policy  →  approval  →  intent
                                                                          ↓
                              receipt  ←  settle  ←  execute  ←  cancel check
```

The order is not cosmetic. Visibility is checked before validation so a hidden
tool's schema cannot be probed by feeding it arguments. Approval is checked
before the intent is recorded, so a refused call leaves no ledger trace. And the
intent is recorded before the handler runs, so a crash mid-execution leaves a
provable "may have happened" record rather than silence.

Two invariants the return type enforces:

* **Waiting is a state, not a success.** ``AWAITING_APPROVAL`` carries the binding
  that is waiting, and no receipt.
* **Unproven is unproven.** An ``UNKNOWN`` receipt is reported as ``UNKNOWN``; it
  is never retried here, and there is no code path that turns it into
  ``EXECUTED``.

There is deliberately no method here that records an approval. The gateway reads
approvals; only the host's own decision path (or
:class:`~yanxu_harness.tools.policy.ApprovalStore`) writes them.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Protocol, runtime_checkable

from ..errors import ContractError
from ..runtime.context import RunContext
from ..versioning import fingerprint
from .definition import ToolDefinition
from .execution import (
    AdmissionDecision,
    EffectState,
    ObservedOperation,
    OperationObservation,
    RecoverableLedger,
    ToolExecutionBackend,
    ToolIntent,
    ToolReceipt,
    needs_reconciliation,
)
from .outcomes import ToolCancelled, ToolRejected
from .policy import (
    POLICY_VERSION,
    Approval,
    ApprovalBinding,
    ApprovalStatus,
    ApprovalStore,
    AuthorizationPolicy,
    PolicyKind,
    check_binding,
    preview_hash,
)

__all__ = [
    "ToolGateway",
    "ToolInvocation",
    "ToolInvocationResult",
    "InvocationStatus",
    "VisibilityPolicy",
]


class InvocationStatus(str, Enum):
    """How one gateway call ended."""

    EXECUTED = "executed"
    REPLAYED = "replayed"
    AWAITING_APPROVAL = "awaiting_approval"
    #: The backend decided to wait for a human (delegated mode: the host gateway
    #: raised its own deferred outcome). Nothing ran, and this is not a failure.
    DEFERRED = "deferred"
    DENIED = "denied"
    INVALID_ARGS = "invalid_args"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"

    @property
    def succeeded(self) -> bool:
        """True only when the handler ran *in this call* and the effect is proven."""
        return self is InvocationStatus.EXECUTED

    @property
    def ran(self) -> bool:
        """True when the handler may have run (executed, replayed or unknown)."""
        return self in {
            InvocationStatus.EXECUTED,
            InvocationStatus.REPLAYED,
            InvocationStatus.UNKNOWN,
        }

    @property
    def waiting(self) -> bool:
        """True when a human has to decide before this call can proceed."""
        return self in {InvocationStatus.AWAITING_APPROVAL, InvocationStatus.DEFERRED}


@runtime_checkable
class VisibilityPolicy(Protocol):
    """Layer 2: does this deployment offer this tool to a model at all?"""

    def exposed(self, definition: ToolDefinition, context: Any) -> bool:
        """Return whether the tool may be declared and called here."""


@dataclass(frozen=True)
class ToolInvocation:
    """One model-requested tool call, as the caller knows it.

    ``arguments`` are the *raw*, untrusted arguments. ``target_version`` is the
    server-side version of the object the call would change; a model may repeat
    it in the arguments but never establishes it.
    """

    call_id: str
    namespace: str
    tool_name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    target_version: str | None = None
    session_id: str = ""
    run_id: str = ""
    operation_id: str | None = None
    #: The model action (batch) that declared this call. It is part of the logical call
    #: reference the ledger binds to, so two turns of one run that reuse a tool-call id
    #: cannot read each other's receipt. Empty when the caller has no such concept.
    model_action_id: str = ""

    def __post_init__(self) -> None:
        for name in ("call_id", "namespace", "tool_name"):
            if not getattr(self, name):
                raise ContractError(f"tool invocation requires a non-empty {name}")

    def logical_ref(self) -> str:
        """A versioned fingerprint of the *logical call*, separators and all.

        ``namespace``/``session``/``run``/``model action``/``tool-call id`` are joined by a
        structured encoding rather than a delimiter, because a call id containing the
        delimiter would otherwise let two different calls share one identity.
        """
        from ..versioning import fingerprint

        return fingerprint(
            {
                "ref_version": 1,
                "namespace": self.namespace,
                "session_id": self.session_id,
                "run_id": self.run_id,
                "model_action_id": self.model_action_id,
                "tool_call_id": self.call_id,
            }
        )

    def operation_key(self) -> str:
        """Stable operation id: the caller's when given, else derived from the call.

        The derived key is a **composite** of namespace, session, run, model action and
        call id — not the call id alone and not ``run:call``. Two projects may each have a
        run ``r1`` with a ``call-1``, and a key that ignored the namespace would let one
        read the other's receipt (and "replay" it as if it were its own effect). The model
        action is in the key for the same reason: one run may reuse a tool-call id in a
        later turn, and those are two different calls, not one call to replay.
        """
        if self.operation_id:
            return self.operation_id
        scope = ":".join(
            part
            for part in (
                self.namespace,
                self.session_id,
                self.run_id,
                self.model_action_id,
                self.call_id,
            )
            if part
        )
        return scope


@dataclass(frozen=True)
class ToolInvocationResult:
    """What the gateway did, in a shape a host can persist and re-read."""

    status: InvocationStatus
    tool_name: str
    tool_version: str
    operation_id: str
    receipt: ToolReceipt | None = None
    approval: Approval | None = None
    preview: Mapping[str, Any] | None = None
    args_hash: str = ""
    reason: str = ""
    #: The model action (batch) the call was declared in. It travels with the result so a
    #: recorded batch can re-derive the *same* operation id on a resume.
    model_action_id: str = ""

    def __post_init__(self) -> None:
        if self.status is InvocationStatus.AWAITING_APPROVAL:
            if self.approval is None:
                raise ContractError("awaiting_approval must carry the approval that is waiting")
            if self.receipt is not None:
                raise ContractError("a call awaiting approval cannot already have a receipt")
        if self.status in {
            InvocationStatus.EXECUTED,
            InvocationStatus.REPLAYED,
            InvocationStatus.UNKNOWN,
            InvocationStatus.DEFERRED,
            InvocationStatus.FAILED,
        }:
            if self.receipt is None:
                raise ContractError(f"{self.status.value} must carry its receipt")
        if self.status is InvocationStatus.DENIED and not self.reason:
            raise ContractError("a denied call must explain itself")

    @property
    def unknown(self) -> bool:
        return self.status is InvocationStatus.UNKNOWN

    @property
    def needs_reconciliation(self) -> bool:
        return self.receipt is not None and needs_reconciliation(self.receipt.state)

    def raise_for_status(self) -> "ToolInvocationResult":
        """Turn a refusal into the matching neutral tool outcome, or return ``self``."""
        if self.status is InvocationStatus.DENIED:
            raise ToolRejected(
                f"{self.tool_name}: {self.reason}",
                details={"tool": self.tool_name, "reason": self.reason, "call": self.operation_id},
            )
        if self.status is InvocationStatus.CANCELLED:
            raise ToolCancelled(self.reason or "cancelled before the tool ran")
        if self.status is InvocationStatus.INVALID_ARGS:
            from .outcomes import ToolArgsInvalid

            raise ToolArgsInvalid(self.reason or "invalid arguments")
        if self.status is InvocationStatus.FAILED:
            raise ToolRejected(self.reason or f"{self.tool_name} failed")
        if self.status is InvocationStatus.DEFERRED:
            # Waiting for a human is not an error: raising would erase the
            # distinction the host gateway just made.
            return self
        if self.receipt is not None and self.receipt.state in {
            EffectState.FAILED,
            EffectState.REJECTED,
            EffectState.CANCELLED,
            EffectState.DEFERRED,
        }:
            self.receipt.raise_for_state()
        return self


class ToolGateway:
    """One gateway per deployment, owning the whole call path."""

    def __init__(
        self,
        *,
        registry: Any,
        policy: AuthorizationPolicy,
        approvals: ApprovalStore,
        backend: ToolExecutionBackend,
        visibility: VisibilityPolicy | None = None,
        policy_version: str = POLICY_VERSION,
        clock: Any = time.time,
    ) -> None:
        self._registry = registry
        self._policy = policy
        self._approvals = approvals
        self._backend = backend
        self._visibility = visibility
        self._policy_version = policy_version
        self._clock = clock

    # -- introspection --------------------------------------------------

    @property
    def backend_ownership(self) -> str:
        """``"harness"`` or ``"host"`` — which side owns the ledger for this gateway."""
        return self._backend.ownership

    @property
    def ledger_recoverable(self) -> bool:
        """Whether this gateway's ledger can be asked about a call without running it.

        A host that wants the automatic recovery guarantee can check this before relying
        on it; a deployment without it keeps working and simply has no such guarantee.
        """
        return isinstance(self._backend, RecoverableLedger)

    async def promote(
        self, invocation: ToolInvocation, evidence: Mapping[str, Any]
    ) -> ToolReceipt | None:
        """Record a *proven* convergence of an unproven operation, in the ledger.

        The Run's view of a reconciled call is rebuilt from the argument, but the
        authoritative record of *why* it moved belongs to the ledger owner. ``None`` means
        this ledger does not implement the promotion capability; the caller then settles
        the run alone, which is the previous behaviour.
        """
        if not isinstance(self._backend, RecoverableLedger):
            return None
        promote = getattr(self._backend, "promote", None)
        if not callable(promote):
            return None
        state = str(dict(evidence).get("state") or "")
        if state not in {"succeeded", "failed"}:
            raise ContractError(
                f"promotion evidence must name state='succeeded' or 'failed', got {state!r}"
            )
        source = str(dict(evidence).get("source") or "")
        if not source:
            raise ContractError("promotion evidence must name the source it came from")
        definition = self._registry.get(invocation.tool_name)
        operation_id = invocation.operation_key()
        try:
            validated = definition.validate(invocation.arguments)
        except Exception as exc:  # noqa: BLE001
            raise ContractError(
                f"{operation_id}: cannot promote an operation whose arguments do not validate: {exc}"
            ) from exc
        args_hash = fingerprint(_args_fingerprint_payload(definition, validated))
        intent = ToolIntent(
            operation_id=operation_id,
            namespace=invocation.namespace,
            tool_name=definition.name,
            tool_version=definition.version,
            args_hash=args_hash,
            session_id=invocation.session_id,
            run_id=invocation.run_id,
            call_id=invocation.call_id,
            model_action_id=invocation.model_action_id,
            raw_arguments=dict(invocation.arguments),
        )
        receipt = _promotion_receipt(operation_id, state, evidence)
        return await promote(receipt, evidence=dict(evidence))

    async def review(self, invocation: ToolInvocation, context: Any) -> ToolInvocationResult | None:
        """Classify one call against the authoritative ledger, running nothing.

        ``None`` means the ledger *proves* there is no intent for this binding, so the
        caller may dispatch it — through :meth:`invoke`, which admits it atomically. Any
        other answer is a result the caller must record instead of running the tool:

        * a confirmed receipt replays its recorded outcome;
        * an open intent is ``unknown`` (the effect may have happened);
        * an unavailable answer is ``unknown`` too — "I could not ask" is not "nothing
          happened", and treating it as absence is how an effect gets duplicated.

        This is a *read*: it validates arguments and hashes them the same way
        :meth:`invoke` does, so the query uses the exact binding the execution would.
        """
        definition = self._registry.get(invocation.tool_name)
        operation_id = invocation.operation_key()
        base = {
            "tool_name": definition.name,
            "tool_version": definition.version,
            "operation_id": operation_id,
            "model_action_id": invocation.model_action_id,
        }
        if not isinstance(self._backend, RecoverableLedger):
            return ToolInvocationResult(
                status=InvocationStatus.UNKNOWN,
                receipt=ToolReceipt(
                    operation_id=operation_id,
                    state=EffectState.UNKNOWN,
                    detail="this ledger offers no recovery capability, so the outcome cannot be read",
                ),
                reason=(
                    "the ledger behind this tool cannot be queried without running the call; "
                    "recovery blocks conservatively instead of assuming nothing happened"
                ),
                **base,
            )
        try:
            validated = definition.validate(invocation.arguments)
        except Exception as exc:  # noqa: BLE001 - the same refusal `invoke` would report
            return ToolInvocationResult(
                status=InvocationStatus.INVALID_ARGS, reason=str(exc), **base
            )
        args_hash = fingerprint(_args_fingerprint_payload(definition, validated))
        intent = ToolIntent(
            operation_id=operation_id,
            namespace=invocation.namespace,
            tool_name=definition.name,
            tool_version=definition.version,
            args_hash=args_hash,
            session_id=invocation.session_id,
            run_id=invocation.run_id,
            call_id=invocation.call_id,
            model_action_id=invocation.model_action_id,
            raw_arguments=dict(invocation.arguments),
        )
        observed = await self._backend.observe(intent)
        if observed.observation.proves_absence:
            return None
        if observed.observation is OperationObservation.RECEIPT:
            receipt = observed.receipt
            assert receipt is not None
            return ToolInvocationResult(
                status=_status_for_receipt(receipt, replayed=True),
                receipt=receipt,
                args_hash=args_hash,
                reason=f"the ledger already holds {receipt.state.value}",
                **base,
            )
        return ToolInvocationResult(
            status=InvocationStatus.UNKNOWN,
            receipt=ToolReceipt(
                operation_id=operation_id,
                state=EffectState.UNKNOWN,
                detail=observed.detail or "the ledger cannot prove the outcome",
            ),
            args_hash=args_hash,
            reason=observed.detail or "the ledger cannot prove the outcome",
            **base,
        )

    # -- the path -------------------------------------------------------

    async def invoke(self, invocation: ToolInvocation, context: Any) -> ToolInvocationResult:
        """Run one call through every layer, or report the state it stopped at."""
        definition = self._registry.get(invocation.tool_name)
        operation_id = invocation.operation_key()
        base = {
            "tool_name": definition.name,
            "tool_version": definition.version,
            "operation_id": operation_id,
            "model_action_id": invocation.model_action_id,
        }

        # A caller that disagrees with the run context about the namespace is a
        # bug, not a denial: it would write into another project's ledger.
        identity = getattr(context, "identity", None)
        if identity is not None and identity.namespace != invocation.namespace:
            raise ContractError(
                f"{definition.name}: invocation namespace {invocation.namespace!r} does not match "
                f"the run context namespace {identity.namespace!r}"
            )

        # Layer 2 — deployment visibility, before argument validation.
        if self._visibility is not None and not self._visibility.exposed(definition, context):
            return ToolInvocationResult(
                status=InvocationStatus.DENIED,
                reason="not exposed by this deployment",
                **base,
            )

        # Layer 1 — validated arguments.
        try:
            validated = definition.validate(invocation.arguments)
        except Exception as exc:  # noqa: BLE001 - ToolArgsInvalid and host validators alike
            return ToolInvocationResult(
                status=InvocationStatus.INVALID_ARGS,
                reason=str(exc),
                **base,
            )

        preview = definition.preview_for(context, validated) if definition.preview else None
        args_hash = fingerprint(_args_fingerprint_payload(definition, validated))

        # Cancellation is checked before the intent is recorded: a cancelled call
        # must leave no ledger trace at all.
        if _is_cancelled(context):
            return ToolInvocationResult(status=InvocationStatus.CANCELLED, reason="run cancelled", **base)

        # Layer 2b — policy.
        decision = self._policy.decide(definition=definition, context=context, exposed=True)
        if decision.kind is PolicyKind.DENY:
            return ToolInvocationResult(
                status=InvocationStatus.DENIED, reason=decision.reason or "policy denied", **base
            )
        if decision.kind is PolicyKind.REQUIRE_APPROVAL:
            pending = await self._require_approval(
                definition, invocation, args_hash, preview, decision.policy_version, operation_id, base
            )
            if isinstance(pending, ToolInvocationResult):
                return pending

        # Layer 4 — intent, then execute, then settle.
        intent = ToolIntent(
            operation_id=operation_id,
            namespace=invocation.namespace,
            tool_name=definition.name,
            tool_version=definition.version,
            args_hash=args_hash,
            session_id=invocation.session_id,
            run_id=invocation.run_id,
            call_id=invocation.call_id,
            # The batch reference travels into the ledger, so the binding check can tell
            # two turns that reuse a tool-call id apart instead of replaying one into the
            # other.
            model_action_id=invocation.model_action_id,
            idempotency_key=_idempotency_key(definition, context, validated),
            raw_arguments=dict(invocation.arguments),
        )
        admitted = await self._admit(intent)
        if admitted is not None:
            return admitted
        if isinstance(self._backend, RecoverableLedger):
            # The strong contract already recorded the intent atomically; the legacy
            # `begin` must not be called a second time for the same call.
            pass
        else:
            existing = await self._backend.begin(intent)
            if existing is not None:
                return ToolInvocationResult(
                    status=_status_for_receipt(existing, replayed=True),
                    receipt=existing,
                    args_hash=args_hash,
                    preview=preview,
                    reason=f"already settled as {existing.state.value}",
                    **base,
                )

        if _is_cancelled(context):
            return ToolInvocationResult(status=InvocationStatus.CANCELLED, reason="run cancelled", **base)

        receipt = await self._backend.execute(
            intent, definition, validated, _context_for_operation(context, operation_id)
        )
        await self._backend.settle(receipt)
        return ToolInvocationResult(
            status=_status_for_receipt(receipt),
            receipt=receipt,
            args_hash=args_hash,
            preview=preview,
            reason=receipt.detail or _state_reason(receipt.state),
            **base,
        )

    async def _admit(self, intent: ToolIntent) -> ToolInvocationResult | None:
        """Use the ledger's atomic admission when it offers one.

        Returns a result when the call must *not* run here (already open, already
        settled, or bound to a different call), and ``None`` when this call obtained the
        right to run. A backend without the capability falls through to the legacy
        ``begin`` — its caller keeps the previous behaviour, which is exactly why the
        capability is advertised explicitly instead of assumed.
        """
        if not isinstance(self._backend, RecoverableLedger):
            return None
        admission = await self._backend.admit(intent)
        base = {
            "tool_name": intent.tool_name,
            "tool_version": intent.tool_version,
            "operation_id": admission.operation_id or intent.operation_id,
        }
        if admission.decision is AdmissionDecision.BINDING_CONFLICT:
            raise ContractError(
                f"{intent.operation_id}: this operation id is already bound to a different "
                f"call ({admission.detail or 'binding mismatch'}); refusing to run it here, "
                "because reusing the id would either duplicate an effect or read one that "
                "belongs to another call"
            )
        if admission.decision is AdmissionDecision.EXISTING_RECEIPT:
            receipt = admission.receipt
            assert receipt is not None
            return ToolInvocationResult(
                status=_status_for_receipt(receipt, replayed=True),
                receipt=receipt,
                reason=f"already settled as {receipt.state.value}",
                **base,
            )
        if admission.decision is AdmissionDecision.EXISTING_OPEN:
            # An intent exists with no receipt: the effect may or may not have happened.
            # It is reported as unproven, never as a new admission, so the handler does
            # not run a second time.
            return ToolInvocationResult(
                status=InvocationStatus.UNKNOWN,
                receipt=ToolReceipt(
                    operation_id=intent.operation_id,
                    state=EffectState.UNKNOWN,
                    detail=(
                        "an intent for this operation is already open and no receipt can "
                        "prove the effect; it is not run again automatically"
                    ),
                ),
                reason=admission.detail or "an intent for this operation is already open",
                **base,
            )
        return None

    # -- approval helpers ----------------------------------------------

    async def _require_approval(
        self,
        definition: ToolDefinition,
        invocation: ToolInvocation,
        args_hash: str,
        preview: Mapping[str, Any] | None,
        policy_version: str,
        operation_id: str,
        base: Mapping[str, Any],
    ) -> ToolInvocationResult | None:
        """Return a waiting/denied result, or ``None`` when a valid approval exists.

        A drifted approval is not an error: it means the author approved something
        else. The gateway records a fresh pending approval and reports the drift
        reason, so the host can ask again with the exact new binding.
        """
        binding = ApprovalBinding(
            namespace=invocation.namespace,
            call_id=invocation.call_id,
            tool_name=definition.name,
            tool_version=definition.version,
            args_hash=args_hash,
            target_version=invocation.target_version,
            preview_hash=preview_hash(preview),
            policy_version=policy_version,
        )
        existing = await self._approvals.get(binding.key())
        if existing is not None:
            valid, reason = check_binding(
                existing.binding,
                args_hash=args_hash,
                target_version=invocation.target_version,
                tool_version=definition.version,
                preview_hash=preview_hash(preview),
                policy_version=policy_version,
                now=self._clock(),
            )
            if valid:
                if existing.is_approved():
                    return None
                if existing.is_open():
                    return ToolInvocationResult(
                        status=InvocationStatus.AWAITING_APPROVAL,
                        approval=existing,
                        args_hash=args_hash,
                        preview=preview,
                        reason="waiting for the author's decision",
                        **base,
                    )
                return ToolInvocationResult(
                    status=InvocationStatus.DENIED,
                    approval=existing,
                    args_hash=args_hash,
                    preview=preview,
                    reason=existing.note or "the author declined this call",
                    **base,
                )
            # The bound values moved (or the question expired). Ask again for the
            # same call: the earlier decision was about something else, and it is
            # kept as history rather than silently reused or overwritten.
            fresh = await self._approvals.supersede(binding)
            return ToolInvocationResult(
                status=InvocationStatus.AWAITING_APPROVAL,
                approval=fresh,
                args_hash=args_hash,
                preview=preview,
                reason=reason or "the approval no longer applies",
                **base,
            )
        pending = await self._approvals.put(Approval(binding=binding, status=ApprovalStatus.PENDING))
        return ToolInvocationResult(
            status=InvocationStatus.AWAITING_APPROVAL,
            approval=pending,
            args_hash=args_hash,
            preview=preview,
            reason="no approval recorded for this call yet",
            **base,
        )


#: A refusal must be able to explain itself even when the backend did not bother
#: to fill in a detail. "Denied, because" with an empty clause is how an author
#: ends up with no idea what to change.
_STATE_REASONS: dict[EffectState, str] = {
    EffectState.REJECTED: "the tool refused this call",
    EffectState.FAILED: "the tool failed",
    EffectState.INVALID_ARGS: "the arguments were rejected",
    EffectState.CANCELLED: "the run was cancelled",
    EffectState.DEFERRED: "waiting for a human decision",
    EffectState.UNKNOWN: "the outcome could not be proven",
}


def _context_for_operation(context: Any, operation_id: str) -> Any:
    """Stamp the gateway's authoritative operation id onto a run context.

    Only a :class:`~yanxu_harness.runtime.context.RunContext` is ours to derive:
    a host with its own context object keeps whatever it passed in, byte for byte,
    because silently rebuilding an unknown type is how identity gets lost.
    """
    if isinstance(context, RunContext):
        return context.for_operation(operation_id)
    return context


def _promotion_receipt(
    operation_id: str, state: str, evidence: Mapping[str, Any]
) -> ToolReceipt:
    detail = str(dict(evidence).get("detail") or "")
    if state == "succeeded":
        return ToolReceipt(
            operation_id=operation_id,
            state=EffectState.SUCCEEDED,
            result={"content": [{"type": "text", "text": detail or "confirmed by business evidence"}]},
            detail=detail or "the effect was confirmed by business evidence",
            details={"reconciled": True, "source": str(dict(evidence).get("source") or "")},
        )
    return ToolReceipt(
        operation_id=operation_id,
        state=EffectState.FAILED,
        detail=detail or "business evidence confirmed the effect did not succeed",
        details={"reconciled": True, "source": str(dict(evidence).get("source") or "")},
    )


def _state_reason(state: EffectState) -> str:
    return _STATE_REASONS.get(state, f"receipt state {state.value}")


def _status_for_receipt(receipt: ToolReceipt, *, replayed: bool = False) -> InvocationStatus:
    """Translate one receipt state into the caller-facing status.

    The mapping is explicit rather than a default-to-success, because a backend
    that says "deferred" or "failed" and is reported as "executed" is the worst
    possible bug in this package: it turns a call the host refused to finish into
    a call the caller believes happened.
    """
    match receipt.state:
        case EffectState.SUCCEEDED:
            return InvocationStatus.REPLAYED if replayed else InvocationStatus.EXECUTED
        case EffectState.UNKNOWN:
            return InvocationStatus.UNKNOWN
        case EffectState.DEFERRED:
            return InvocationStatus.DEFERRED
        case EffectState.REJECTED:
            return InvocationStatus.DENIED
        case EffectState.INVALID_ARGS:
            return InvocationStatus.INVALID_ARGS
        case EffectState.FAILED:
            return InvocationStatus.FAILED
        case EffectState.CANCELLED:
            return InvocationStatus.CANCELLED
        case _:
            raise ContractError(
                f"a backend must return a settled receipt; got {receipt.state.value!r} "
                f"for operation {receipt.operation_id!r}"
            )


def _args_fingerprint_payload(definition: ToolDefinition, validated: Any) -> Any:
    """What an approval binds to: the tool contract plus the validated arguments."""
    return {
        "tool": definition.stable_id(),
        "args": _jsonable(validated),
    }


def _jsonable(value: Any) -> Any:
    """Coerce a validated argument model into a fingerprintable structure.

    An argument shape this cannot fingerprint is a *contract* problem: an
    approval over unhashable arguments would not bind to anything, so it is
    refused here rather than silently bound to a meaningless digest.
    """
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [_jsonable(item) for item in sorted(value, key=repr)]
    dataclass_fields = getattr(value, "__dataclass_fields__", None)
    if dataclass_fields:
        return {name: _jsonable(getattr(value, name)) for name in sorted(dataclass_fields)}
    raise ContractError(
        f"validated arguments of type {type(value).__name__} are not fingerprintable; "
        "an approval cannot bind to them"
    )


def _idempotency_key(
    definition: ToolDefinition, context: Any, validated: Any
) -> str | None:
    key = definition.idempotency_key
    if key is None:
        return None
    return str(key(context, validated))


def _is_cancelled(context: Any) -> bool:
    token = getattr(context, "cancellation", None)
    if token is None:
        return False
    cancelled = getattr(token, "cancelled", None)
    return bool(cancelled() if callable(cancelled) else cancelled)
