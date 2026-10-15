"""The recorded tool batch: which calls a suspended run stopped in, and how each ended.

A run that pauses must leave behind enough to answer three questions without
guessing: *which* calls were declared, *which* of them are settled, and *which*
still need a decision or evidence. The gateway already knows the truth for each
individual operation (intent, receipt, approval binding); this module records the
run's own view of the batch so a restarted process can rebuild the transcript and
only dispatch what is provably not done.

The classification has exactly four outcomes, and the distinction between the last
three is the whole point:

``settled``
    the call has a receipt (executed, replayed, denied, invalid args, failed,
    cancelled). It has a model-visible result and does not block a resume.
``awaiting``
    a human has to decide. Nothing was written, and the call must not be reported
    to the model as if it had been.
``uncertain``
    the outcome could not be proven. It is neither settled nor replayable: a resume
    must not dispatch it again, and it must not be reported as a success.
``unstarted``
    the batch paused before reaching this call (a sequential batch that stopped on
    an earlier waiting call). Replayable, in source order.

:class:`CallOutcome` and :func:`classify_status` themselves live in
:mod:`yanxu_harness.core.outcome` and are re-exported here: the loop is what has to
decide whether a batch lets it ask the model again, so the classification belongs to
core, and one import path must not become a second vocabulary.

An *interaction* is what the run is waiting for: an approval decision on one call,
or business evidence about one unproven operation. ``Interaction`` is a neutral
record — it names the operation and the binding key, not a client-supplied
permission.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Mapping, Sequence

from ..core.outcome import CallOutcome, classify_status
from ..core.types import ToolCall, ToolResultMessage
from ..errors import ContractError
from ..tools.gateway import InvocationStatus

__all__ = [
    "BatchCall",
    "CallOutcome",
    "Interaction",
    "InteractionKind",
    "RunBatch",
    "classify_status",
]


class InteractionKind(str, Enum):
    """Why a run is waiting."""

    #: A human must approve or decline one call.
    APPROVAL = "approval"
    #: An operation's outcome is unproven and needs business evidence.
    RECONCILIATION = "reconciliation"


@dataclass(frozen=True)
class BatchCall:
    """One declared tool call of a recorded batch."""

    call_id: str
    tool_name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    #: The operation id the gateway used. Recorded explicitly so a resume dispatches
    #: the *same* operation and the gateway replays its receipt instead of executing
    #: a second effect. Derived the same way as ``ToolInvocation.operation_key`` when
    #: the gateway never got far enough to report one.
    operation_id: str = ""
    status: str = ""
    outcome: CallOutcome = CallOutcome.UNSTARTED
    effect_state: str | None = None
    approval_binding_key: str | None = None
    interaction_id: str | None = None
    #: The settled result, as ``core.types.message_to_json`` output. A resume reuses
    #: this instead of calling the gateway again for a call that is already settled.
    result: Mapping[str, Any] | None = None
    reason: str = ""
    #: Business evidence that settled an operation the gateway could not prove.
    #: Required when an ``unknown`` status is classified as settled: without it the
    #: classification would be a guess, and a guess about a side effect is exactly
    #: what must not happen.
    resolution: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.call_id or not self.tool_name:
            raise ContractError("a recorded batch call needs a call id and a tool name")
        if not isinstance(self.outcome, CallOutcome):
            raise ContractError(f"batch call outcome must be a CallOutcome, got {self.outcome!r}")
        if self.outcome is CallOutcome.SETTLED and self.result is None:
            raise ContractError(
                f"{self.call_id}: a settled call must carry the result the model will see"
            )
        if self.outcome is CallOutcome.AWAITING and not self.approval_binding_key:
            raise ContractError(
                f"{self.call_id}: a waiting call must name the approval binding it waits on"
            )
        if self.status == "unknown" and self.outcome is not CallOutcome.UNCERTAIN:
            if self.resolution is None:
                raise ContractError(
                    f"{self.call_id}: an unproven operation cannot be classified settled "
                    "without recorded business evidence"
                )
        if self.resolution is not None:
            state = str(dict(self.resolution).get("state") or "")
            if state not in {"succeeded", "failed"}:
                raise ContractError(
                    f"{self.call_id}: reconciliation evidence must name state='succeeded' or "
                    f"'failed', got {state!r}"
                )

    def to_record(self) -> Mapping[str, Any]:
        return {
            "call_id": self.call_id,
            "tool_name": self.tool_name,
            "arguments": dict(self.arguments),
            "operation_id": self.operation_id,
            "status": self.status,
            "outcome": self.outcome.value,
            "effect_state": self.effect_state,
            "approval_binding_key": self.approval_binding_key,
            "interaction_id": self.interaction_id,
            "result": None if self.result is None else dict(self.result),
            "reason": self.reason,
            "resolution": None if self.resolution is None else dict(self.resolution),
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "BatchCall":
        return cls(
            call_id=str(record["call_id"]),
            tool_name=str(record["tool_name"]),
            arguments=dict(record.get("arguments") or {}),
            operation_id=str(record.get("operation_id") or ""),
            status=str(record.get("status") or ""),
            outcome=CallOutcome(str(record.get("outcome") or CallOutcome.UNSTARTED.value)),
            effect_state=record.get("effect_state"),
            approval_binding_key=record.get("approval_binding_key"),
            interaction_id=record.get("interaction_id"),
            result=record.get("result"),
            reason=str(record.get("reason") or ""),
            resolution=record.get("resolution"),
        )


@dataclass(frozen=True)
class Interaction:
    """One question the run is waiting for an answer to."""

    interaction_id: str
    kind: InteractionKind
    call_id: str
    tool_name: str
    operation_id: str = ""
    approval_binding_key: str | None = None
    args_hash: str = ""
    target_version: str | None = None
    preview: Mapping[str, Any] | None = None
    expires_at: float | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        for name in ("interaction_id", "call_id", "tool_name"):
            if not getattr(self, name):
                raise ContractError(f"an interaction requires a non-empty {name}")
        if not isinstance(self.kind, InteractionKind):
            raise ContractError(f"interaction kind must be an InteractionKind, got {self.kind!r}")
        if self.kind is InteractionKind.APPROVAL and not self.approval_binding_key:
            raise ContractError("an approval interaction must name the binding it asks about")

    def expired(self, *, now: float | None = None) -> bool:
        if self.expires_at is None:
            return False
        return (time.time() if now is None else now) > self.expires_at

    def to_record(self) -> Mapping[str, Any]:
        return {
            "interaction_id": self.interaction_id,
            "kind": self.kind.value,
            "call_id": self.call_id,
            "tool_name": self.tool_name,
            "operation_id": self.operation_id,
            "approval_binding_key": self.approval_binding_key,
            "args_hash": self.args_hash,
            "target_version": self.target_version,
            "preview": None if self.preview is None else dict(self.preview),
            "expires_at": self.expires_at,
            "detail": self.detail,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "Interaction":
        return cls(
            interaction_id=str(record["interaction_id"]),
            kind=InteractionKind(str(record["kind"])),
            call_id=str(record["call_id"]),
            tool_name=str(record["tool_name"]),
            operation_id=str(record.get("operation_id") or ""),
            approval_binding_key=record.get("approval_binding_key"),
            args_hash=str(record.get("args_hash") or ""),
            target_version=record.get("target_version"),
            preview=record.get("preview"),
            expires_at=record.get("expires_at"),
            detail=str(record.get("detail") or ""),
        )


@dataclass(frozen=True)
class RunBatch:
    """The batch a run stopped in, with the identity it was declared under."""

    calls: tuple[BatchCall, ...] = ()
    interactions: tuple[Interaction, ...] = ()
    attempt_id: str = ""
    config_fingerprint: str = ""
    tools_fingerprint: str = ""
    instructions_fingerprint: str = ""
    policy_fingerprint: str = ""
    exposure_manifest: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "calls", tuple(self.calls))
        object.__setattr__(self, "interactions", tuple(self.interactions))

    # -- queries --------------------------------------------------------

    def __bool__(self) -> bool:
        return bool(self.calls)

    @property
    def waiting(self) -> tuple[BatchCall, ...]:
        return tuple(call for call in self.calls if call.outcome is CallOutcome.AWAITING)

    @property
    def uncertain(self) -> tuple[BatchCall, ...]:
        return tuple(call for call in self.calls if call.outcome is CallOutcome.UNCERTAIN)

    @property
    def unstarted(self) -> tuple[BatchCall, ...]:
        return tuple(call for call in self.calls if call.outcome is CallOutcome.UNSTARTED)

    @property
    def settled(self) -> tuple[BatchCall, ...]:
        return tuple(call for call in self.calls if call.outcome is CallOutcome.SETTLED)

    def blocking(self) -> tuple[BatchCall, ...]:
        """Calls that stop the run from asking the model anything else."""
        return tuple(
            call for call in self.calls if call.outcome in {CallOutcome.AWAITING, CallOutcome.UNCERTAIN}
        )

    def call(self, call_id: str) -> BatchCall | None:
        return next((call for call in self.calls if call.call_id == call_id), None)

    def operation_id_for(self, call_id: str, *, run_id: str) -> str:
        """The stable operation id for one call, recorded or derived."""
        recorded = self.call(call_id)
        if recorded is not None and recorded.operation_id:
            return recorded.operation_id
        return f"{run_id}:{call_id}"

    def interaction_for(self, call_id: str) -> Interaction | None:
        return next(
            (item for item in self.interactions if item.call_id == call_id),
            None,
        )

    def replace_call(self, updated: BatchCall) -> "RunBatch":
        """Return a batch with one call replaced, keeping the declared order."""
        if self.call(updated.call_id) is None:
            raise ContractError(f"{updated.call_id} is not part of this batch")
        return replace(
            self,
            calls=tuple(updated if call.call_id == updated.call_id else call for call in self.calls),
        )

    def reconciliation_interactions(self) -> tuple[Interaction, ...]:
        return tuple(
            item for item in self.interactions if item.kind is InteractionKind.RECONCILIATION
        )

    # -- serialization --------------------------------------------------

    def to_record(self) -> Mapping[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "calls": [call.to_record() for call in self.calls],
            "interactions": [item.to_record() for item in self.interactions],
            "config_fingerprint": self.config_fingerprint,
            "tools_fingerprint": self.tools_fingerprint,
            "instructions_fingerprint": self.instructions_fingerprint,
            "policy_fingerprint": self.policy_fingerprint,
            "exposure_manifest": None if self.exposure_manifest is None else dict(self.exposure_manifest),
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "RunBatch":
        if not isinstance(record, Mapping):
            raise ContractError(f"a batch record must be a mapping, got {type(record).__name__}")
        return cls(
            calls=tuple(BatchCall.from_record(item) for item in (record.get("calls") or ())),
            interactions=tuple(
                Interaction.from_record(item) for item in (record.get("interactions") or ())
            ),
            attempt_id=str(record.get("attempt_id") or ""),
            config_fingerprint=str(record.get("config_fingerprint") or ""),
            tools_fingerprint=str(record.get("tools_fingerprint") or ""),
            instructions_fingerprint=str(record.get("instructions_fingerprint") or ""),
            policy_fingerprint=str(record.get("policy_fingerprint") or ""),
            exposure_manifest=record.get("exposure_manifest"),
        )


def record_batch(
    assistant_message: Any,
    tool_results: Sequence[ToolResultMessage],
    *,
    run_id: str,
    attempt_id: str = "",
    fingerprints: Mapping[str, str] | None = None,
    exposure_manifest: Mapping[str, Any] | None = None,
) -> RunBatch:
    """Build a :class:`RunBatch` from one settled batch.

    The declared calls come from the assistant message (so a call the loop never
    reached is still recorded, as ``unstarted``); the outcomes come from each
    result's machine-readable ``details``, which the gateway wrote. A result whose
    details are missing is **refused**: without a status there is no way to tell
    "executed" from "probably executed", and that guess is the defect this whole
    module exists to prevent.
    """
    declared = [block for block in getattr(assistant_message, "content", []) if isinstance(block, ToolCall)]
    results = {result.tool_call_id: result for result in tool_results}
    calls: list[BatchCall] = []
    interactions: list[Interaction] = []

    for block in declared:
        details = results.get(block.id).details if block.id in results else None
        if block.id in results and not isinstance(details, Mapping):
            raise ContractError(
                f"{block.id}: the tool result carries no machine-readable details, so the run "
                "cannot tell an executed call from an unproven one"
            )
        details = dict(details or {})
        raw_status = details.get("status")
        if block.id in results:
            if raw_status is None:
                raise ContractError(f"{block.id}: the tool result has no recorded status")
            outcome = classify_status(str(raw_status))
        else:
            outcome = CallOutcome.UNSTARTED
            raw_status = ""
        approval = details.get("approval") if isinstance(details.get("approval"), Mapping) else {}
        binding_key = approval.get("binding_key") if approval else None
        call = BatchCall(
            call_id=block.id,
            tool_name=block.name,
            arguments=dict(block.arguments),
            operation_id=str(details.get("operation_id") or f"{run_id}:{block.id}"),
            status=str(raw_status or ""),
            outcome=outcome,
            effect_state=details.get("effect_state"),
            approval_binding_key=binding_key,
            result=_result_record(results[block.id]) if block.id in results else None,
            reason=str(details.get("reason") or ""),
        )
        if outcome is CallOutcome.AWAITING:
            interaction = Interaction(
                interaction_id=f"{call.operation_id}:approval",
                kind=InteractionKind.APPROVAL,
                call_id=call.call_id,
                tool_name=call.tool_name,
                operation_id=call.operation_id,
                approval_binding_key=binding_key,
                args_hash=str(details.get("args_hash") or ""),
                preview=details.get("preview") if isinstance(details.get("preview"), Mapping) else None,
                expires_at=approval.get("expires_at") if approval else None,
                detail=call.reason,
            )
            call = replace(call, interaction_id=interaction.interaction_id)
            interactions.append(interaction)
        elif outcome is CallOutcome.UNCERTAIN:
            interaction = Interaction(
                interaction_id=f"{call.operation_id}:reconciliation",
                kind=InteractionKind.RECONCILIATION,
                call_id=call.call_id,
                tool_name=call.tool_name,
                operation_id=call.operation_id,
                args_hash=str(details.get("args_hash") or ""),
                detail=call.reason or "the outcome could not be proven",
            )
            call = replace(call, interaction_id=interaction.interaction_id)
            interactions.append(interaction)
        calls.append(call)

    values = dict(fingerprints or {})
    return RunBatch(
        calls=tuple(calls),
        interactions=tuple(interactions),
        attempt_id=attempt_id,
        config_fingerprint=values.get("config", ""),
        tools_fingerprint=values.get("tools", ""),
        instructions_fingerprint=values.get("instructions", ""),
        policy_fingerprint=values.get("policy", ""),
        exposure_manifest=exposure_manifest,
    )


def _result_record(result: ToolResultMessage) -> Mapping[str, Any]:
    from ..core.types import message_to_json

    return message_to_json(result)
