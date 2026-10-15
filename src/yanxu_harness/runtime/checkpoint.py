"""Versioned run checkpoints: what a restart needs, and what it must refuse to guess.

A checkpoint is the durable answer to "where exactly did this run stop?". It exists
because the alternative — reconstructing the position from the transcript — cannot
tell *"this tool call never ran"* apart from *"this tool call ran and we lost the
record"*. Those two lead to opposite decisions, and guessing between them is how a
write happens twice.

Three things are therefore stored separately and never merged:

``pending_tool_calls``
    the complete declaration of the batch the run stopped in,
``executed_tool_call_ids`` / ``unexecuted_tool_call_ids`` / ``uncertain_tool_call_ids``
    the partition of that batch. Together they must cover exactly the declared
    calls; a checkpoint that cannot prove the partition is refused rather than
    partially restored. The third class exists because an operation whose outcome
    could not be *proven* is neither executed nor safely replayable: collapsing it
    into "unexecuted" is how a resume writes twice, and collapsing it into
    "executed" is how a real effect silently disappears from the transcript.
``pending_interactions``
    the questions the author has not answered, with their expiry.

Resume is also **fingerprint-checked**: config, tool set, instructions, policy and
the model-visible exposure manifest. If any of them moved, the saved approval can
no longer be trusted and the run is sent back for re-confirmation instead of
quietly continuing under a different arrangement. That check is the reason this
module's failure messages name the drifted fingerprint instead of just saying "no".

The checkpoint deliberately does **not** carry the host's engine identity or brand:
a host that serialized its own `engine_id` keeps doing so in its own codec (see the
migration note in the host's `backend/agent_runtime/envelope.py`), and this type
carries a neutral ``schema_version`` instead.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Any, Mapping

from ..errors import ContractError
from ..versioning import SUPPORTED_CHECKPOINT_VERSIONS, require_version
from .context import RunIdentity
from .harness import RunStatus

__all__ = [
    "CheckpointToolCall",
    "PendingInteractionRef",
    "ResumeDecision",
    "ResumePlan",
    "RunCheckpoint",
    "plan_resume",
    "resolve_resume",
]

#: Schema version written for new checkpoints. Readers accept every version in
#: :data:`yanxu_harness.versioning.SUPPORTED_CHECKPOINT_VERSIONS`.
CHECKPOINT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class CheckpointToolCall:
    """One declared tool call of the batch a run stopped in."""

    call_id: str
    tool_name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    operation_id: str | None = None

    def __post_init__(self) -> None:
        if not self.call_id or not self.tool_name:
            raise ContractError("a checkpointed tool call needs a call id and a tool name")


@dataclass(frozen=True)
class PendingInteractionRef:
    """One unanswered question: what it is about, and when it stops being valid."""

    interaction_id: str
    call_id: str
    tool_name: str
    args_hash: str
    target_version: str | None = None
    expires_at: float | None = None

    def expired(self, *, now: float | None = None) -> bool:
        if self.expires_at is None:
            return False
        return (time.time() if now is None else now) > self.expires_at


@dataclass(frozen=True)
class ResumePlan:
    """What a resuming worker may do, and what it must not touch again."""

    replay: tuple[CheckpointToolCall, ...]
    already_done: tuple[str, ...]
    requires_author_revalidation: bool
    reason: str = ""
    #: Calls whose outcome could not be proven. Never replayed: they need business
    #: evidence first, and until that exists the honest answer is still "unknown".
    uncertain: tuple[str, ...] = ()

    @property
    def resumable(self) -> bool:
        return bool(self.replay) or self.requires_author_revalidation


@dataclass(frozen=True)
class ResumeDecision:
    """Whether a checkpoint may be resumed against the *current* configuration."""

    ok: bool
    drift: tuple[str, ...] = ()
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.ok and not self.drift and not self.reason:
            raise ContractError("a refused resume must name what drifted or why")


@dataclass(frozen=True)
class RunCheckpoint:
    """A durable snapshot of one suspended run."""

    identity: RunIdentity
    status: RunStatus
    messages: tuple[Mapping[str, Any], ...] = ()
    pending_tool_calls: tuple[CheckpointToolCall, ...] = ()
    executed_tool_call_ids: frozenset[str] = frozenset()
    unexecuted_tool_call_ids: frozenset[str] = frozenset()
    #: Operations whose outcome is unproven (the gateway recorded an intent and no
    #: receipt, or the receipt state is ``unknown``). Added after schema 1: a
    #: record written without it parses with an empty set, which is the only
    #: backwards-compatible reading of "this older writer had no such class".
    uncertain_tool_call_ids: frozenset[str] = frozenset()
    pending_interactions: tuple[PendingInteractionRef, ...] = ()
    config_fingerprint: str = ""
    tools_fingerprint: str = ""
    instructions_fingerprint: str = ""
    policy_fingerprint: str = ""
    #: The model-visible subset this batch was declared with. ``None`` means the
    #: checkpoint predates exposure tracking and is treated as "full set".
    exposure_manifest: Mapping[str, Any] | None = None
    transcript_head: str = ""
    schema_version: int = CHECKPOINT_SCHEMA_VERSION
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        require_version(
            "checkpoint", self.schema_version, SUPPORTED_CHECKPOINT_VERSIONS
        )
        if not isinstance(self.status, RunStatus):
            raise ContractError(f"checkpoint status must be a RunStatus, got {self.status!r}")
        object.__setattr__(self, "messages", tuple(dict(item) for item in self.messages))
        object.__setattr__(self, "pending_tool_calls", tuple(self.pending_tool_calls))
        object.__setattr__(self, "executed_tool_call_ids", frozenset(self.executed_tool_call_ids))
        object.__setattr__(
            self, "unexecuted_tool_call_ids", frozenset(self.unexecuted_tool_call_ids)
        )
        object.__setattr__(
            self, "uncertain_tool_call_ids", frozenset(self.uncertain_tool_call_ids)
        )
        object.__setattr__(self, "pending_interactions", tuple(self.pending_interactions))
        if self.exposure_manifest is not None and not isinstance(self.exposure_manifest, Mapping):
            raise ContractError("exposure_manifest must be a mapping or None")

        overlap = self.executed_tool_call_ids & self.unexecuted_tool_call_ids
        if overlap:
            raise ContractError(
                f"a call cannot be both executed and unexecuted: {sorted(overlap)}"
            )
        doubtful = self.uncertain_tool_call_ids & (
            self.executed_tool_call_ids | self.unexecuted_tool_call_ids
        )
        if doubtful:
            raise ContractError(
                "a call whose outcome is unproven cannot also be executed or replayable: "
                f"{sorted(doubtful)}"
            )
        declared = {call.call_id for call in self.pending_tool_calls}
        partitioned = (
            self.executed_tool_call_ids
            | self.unexecuted_tool_call_ids
            | self.uncertain_tool_call_ids
        )
        if declared != partitioned:
            raise ContractError(
                "the batch partition must cover exactly the declared calls; "
                f"declared={sorted(declared)} partitioned={sorted(partitioned)}"
            )
        if self.status is RunStatus.SUSPENDED:
            if not self.pending_interactions:
                raise ContractError(
                    "a suspended checkpoint must carry the interactions it is waiting for"
                )
            if not self.pending_tool_calls or not (
                self.unexecuted_tool_call_ids or self.uncertain_tool_call_ids
            ):
                raise ContractError(
                    "a suspended checkpoint must carry the batch it stopped in, with at least "
                    "one call still to run or still unproven — otherwise there is nothing to resume"
                )

    # -- derived state --------------------------------------------------

    @property
    def resumable(self) -> bool:
        """Only a suspended run with pending work in the same run id can be resumed."""
        return self.status is RunStatus.SUSPENDED and bool(self.pending_interactions)

    @property
    def interaction_call_ids(self) -> frozenset[str]:
        return frozenset(item.call_id for item in self.pending_interactions)

    def executed_calls(self) -> tuple[CheckpointToolCall, ...]:
        return tuple(
            call for call in self.pending_tool_calls if call.call_id in self.executed_tool_call_ids
        )

    def unexecuted_calls(self) -> tuple[CheckpointToolCall, ...]:
        return tuple(
            call for call in self.pending_tool_calls if call.call_id in self.unexecuted_tool_call_ids
        )

    def uncertain_calls(self) -> tuple[CheckpointToolCall, ...]:
        """Declared calls whose outcome is unproven. Never replay candidates."""
        return tuple(
            call for call in self.pending_tool_calls if call.call_id in self.uncertain_tool_call_ids
        )

    def expired_interactions(self, *, now: float | None = None) -> tuple[PendingInteractionRef, ...]:
        return tuple(item for item in self.pending_interactions if item.expired(now=now))

    def with_execution(self, call_id: str) -> "RunCheckpoint":
        """Mark one call executed. Idempotent, and never touches a call twice."""
        if call_id in self.executed_tool_call_ids:
            return self
        if call_id not in self.unexecuted_tool_call_ids:
            raise ContractError(f"{call_id} is not part of this checkpoint's unexecuted batch")
        return replace(
            self,
            executed_tool_call_ids=self.executed_tool_call_ids | {call_id},
            unexecuted_tool_call_ids=self.unexecuted_tool_call_ids - {call_id},
        )

    # -- serialization --------------------------------------------------

    def to_record(self) -> Mapping[str, Any]:
        """A JSON-ready record. Keys are stable; the schema version is included."""
        return {
            "schema_version": self.schema_version,
            "identity": self.identity.as_dict(),
            "status": self.status.value,
            "messages": [dict(item) for item in self.messages],
            "pending_tool_calls": [
                {
                    "call_id": call.call_id,
                    "tool_name": call.tool_name,
                    "arguments": dict(call.arguments),
                    "operation_id": call.operation_id,
                }
                for call in self.pending_tool_calls
            ],
            "executed_tool_call_ids": sorted(self.executed_tool_call_ids),
            "unexecuted_tool_call_ids": sorted(self.unexecuted_tool_call_ids),
            "uncertain_tool_call_ids": sorted(self.uncertain_tool_call_ids),
            "pending_interactions": [
                {
                    "interaction_id": item.interaction_id,
                    "call_id": item.call_id,
                    "tool_name": item.tool_name,
                    "args_hash": item.args_hash,
                    "target_version": item.target_version,
                    "expires_at": item.expires_at,
                }
                for item in self.pending_interactions
            ],
            "config_fingerprint": self.config_fingerprint,
            "tools_fingerprint": self.tools_fingerprint,
            "instructions_fingerprint": self.instructions_fingerprint,
            "policy_fingerprint": self.policy_fingerprint,
            "exposure_manifest": None if self.exposure_manifest is None else dict(self.exposure_manifest),
            "transcript_head": self.transcript_head,
            "created_at": self.created_at,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "RunCheckpoint":
        """Rebuild a checkpoint, refusing anything it cannot prove safe."""
        if not isinstance(record, Mapping):
            raise ContractError(f"a checkpoint record must be a mapping, got {type(record).__name__}")
        if "schema_version" not in record:
            raise ContractError(
                "a checkpoint without a schema_version cannot be migrated; refusing instead of guessing"
            )
        version = require_version(
            "checkpoint", record["schema_version"], SUPPORTED_CHECKPOINT_VERSIONS
        )
        try:
            identity = RunIdentity(**dict(record["identity"]))
        except (KeyError, TypeError) as exc:
            raise ContractError(f"checkpoint identity is unusable: {exc}") from exc
        return cls(
            identity=identity,
            status=RunStatus(str(record.get("status") or "")),
            messages=tuple(record.get("messages") or ()),
            pending_tool_calls=tuple(
                CheckpointToolCall(
                    call_id=str(item["call_id"]),
                    tool_name=str(item["tool_name"]),
                    arguments=dict(item.get("arguments") or {}),
                    operation_id=item.get("operation_id"),
                )
                for item in (record.get("pending_tool_calls") or ())
            ),
            executed_tool_call_ids=frozenset(record.get("executed_tool_call_ids") or ()),
            unexecuted_tool_call_ids=frozenset(record.get("unexecuted_tool_call_ids") or ()),
            uncertain_tool_call_ids=frozenset(record.get("uncertain_tool_call_ids") or ()),
            pending_interactions=tuple(
                PendingInteractionRef(
                    interaction_id=str(item["interaction_id"]),
                    call_id=str(item["call_id"]),
                    tool_name=str(item["tool_name"]),
                    args_hash=str(item["args_hash"]),
                    target_version=item.get("target_version"),
                    expires_at=item.get("expires_at"),
                )
                for item in (record.get("pending_interactions") or ())
            ),
            config_fingerprint=str(record.get("config_fingerprint") or ""),
            tools_fingerprint=str(record.get("tools_fingerprint") or ""),
            instructions_fingerprint=str(record.get("instructions_fingerprint") or ""),
            policy_fingerprint=str(record.get("policy_fingerprint") or ""),
            exposure_manifest=record.get("exposure_manifest"),
            transcript_head=str(record.get("transcript_head") or ""),
            schema_version=version,
            created_at=float(record.get("created_at") or 0.0),
        )


def resolve_resume(
    checkpoint: RunCheckpoint,
    *,
    config_fingerprint: str | None = None,
    tools_fingerprint: str | None = None,
    instructions_fingerprint: str | None = None,
    policy_fingerprint: str | None = None,
    exposure_manifest: Mapping[str, Any] | None = None,
    now: float | None = None,
) -> ResumeDecision:
    """Decide whether ``checkpoint`` may be resumed under the current arrangement.

    Any drifted fingerprint is named. A drifted *tool set* or *policy* is not a
    warning: the approval that is being resumed was granted for the old
    arrangement, so the run goes back for re-confirmation instead of continuing.
    """
    drift: list[str] = []
    for label, saved, current in (
        ("config", checkpoint.config_fingerprint, config_fingerprint),
        ("tools", checkpoint.tools_fingerprint, tools_fingerprint),
        ("instructions", checkpoint.instructions_fingerprint, instructions_fingerprint),
        ("policy", checkpoint.policy_fingerprint, policy_fingerprint),
    ):
        if current is not None and saved and saved != current:
            drift.append(label)
    if exposure_manifest is not None and checkpoint.exposure_manifest is not None:
        if dict(exposure_manifest) != dict(checkpoint.exposure_manifest):
            drift.append("exposure")
    if drift:
        return ResumeDecision(
            False, tuple(drift), f"the {', '.join(drift)} fingerprint(s) changed after the pause"
        )
    if not checkpoint.resumable:
        return ResumeDecision(False, (), f"a {checkpoint.status.value} run has nothing to resume")
    expired = checkpoint.expired_interactions(now=now)
    if expired:
        return ResumeDecision(
            False,
            ("expired",),
            f"{len(expired)} pending interaction(s) expired; the author must be asked again",
        )
    return ResumeDecision(True)


def plan_resume(
    checkpoint: RunCheckpoint,
    *,
    now: float | None = None,
    **fingerprints: Any,
) -> ResumePlan:
    """Combine the drift check with the batch partition into one plan.

    The plan never lists an already-executed call in ``replay``: that is the whole
    point of storing the partition, and a plan that ignored it would re-run a side
    effect the checkpoint proved had already happened.
    """
    decision = resolve_resume(checkpoint, now=now, **fingerprints)
    executed = tuple(sorted(checkpoint.executed_tool_call_ids))
    uncertain = tuple(sorted(checkpoint.uncertain_tool_call_ids))
    if not decision.ok:
        revalidation = bool(set(decision.drift) & {"policy", "tools", "exposure", "expired"})
        return ResumePlan(
            replay=checkpoint.unexecuted_calls(),
            already_done=executed,
            requires_author_revalidation=revalidation,
            reason=decision.reason,
            uncertain=uncertain,
        )
    waiting = checkpoint.interaction_call_ids
    replay = tuple(
        call for call in checkpoint.unexecuted_calls() if call.call_id in waiting or not waiting
    )
    return ResumePlan(
        replay=replay,
        already_done=executed,
        requires_author_revalidation=False,
        reason="",
        uncertain=uncertain,
    )
