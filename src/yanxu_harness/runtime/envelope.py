"""Event envelopes and the fence that keeps a stale attempt out of a live run.

Two vocabularies already exist and both stay:

* **core events** (`yanxu_harness.core.events`) are in-memory progress frames —
  ``agent_start``, ``message_update``, the provider's own delta inside
  ``message_update``. They belong to one loop invocation.
* **durable events** (`yanxu_harness.events.Event`) are committed facts with an
  append-only sequence.

What neither carried was *which run and which attempt* an event belongs to, so a
callback that arrived after a resume could not be told from a current one. This
module adds the envelope both paths share — namespace, run, attempt, sequence,
schema version — and the fence that decides whether a late arrival is still
current.

The rule it enforces is the one the plan states: **validate before changing
anything.** An event from another attempt (or another namespace) is refused with a
reason, and no state is touched; only then does anything downstream see it.
Deliberately, the fence is not a permission system — it answers one question, and a
host that wants authorisation checks uses the run context for that.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from ..errors import ContractError
from ..versioning import SUPPORTED_EVENT_VERSIONS, require_version

__all__ = ["EVENT_ENVELOPE_SCHEMA_VERSION", "EventEnvelope", "EventFence", "FenceDecision"]

#: Envelope schema version. Independent of the event vocabulary's version.
EVENT_ENVELOPE_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class EventEnvelope:
    """One event, tagged with the run identity and its position in the stream."""

    namespace: str
    run_id: str
    attempt_id: str
    kind: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    sequence: int = 0
    schema_version: int = EVENT_ENVELOPE_SCHEMA_VERSION
    #: ``True`` once the event is committed to the store. A transient core event is
    #: ``False`` and may never be replayed as if it were a durable fact.
    durable: bool = False

    def __post_init__(self) -> None:
        require_version("event envelope", self.schema_version, SUPPORTED_EVENT_VERSIONS)
        for name in ("namespace", "run_id", "attempt_id", "kind"):
            if not getattr(self, name):
                raise ContractError(f"an event envelope requires a non-empty {name}")
        if self.sequence < 0:
            raise ContractError(f"event sequence must be >= 0, got {self.sequence}")
        object.__setattr__(self, "payload", dict(self.payload or {}))

    def to_record(self) -> Mapping[str, Any]:
        return {
            "schema_version": self.schema_version,
            "namespace": self.namespace,
            "run_id": self.run_id,
            "attempt_id": self.attempt_id,
            "sequence": self.sequence,
            "kind": self.kind,
            "payload": dict(self.payload),
            "durable": self.durable,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "EventEnvelope":
        if not isinstance(record, Mapping):
            raise ContractError(f"an event envelope must be a mapping, got {type(record).__name__}")
        if "schema_version" not in record:
            raise ContractError(
                "an event envelope without a schema_version cannot be interpreted; refusing "
                "instead of guessing"
            )
        return cls(
            namespace=str(record.get("namespace") or ""),
            run_id=str(record.get("run_id") or ""),
            attempt_id=str(record.get("attempt_id") or ""),
            kind=str(record.get("kind") or ""),
            payload=dict(record.get("payload") or {}),
            sequence=int(record.get("sequence") or 0),
            schema_version=int(record["schema_version"]),
            durable=bool(record.get("durable", False)),
        )

    def for_attempt(self, attempt_id: str, *, sequence: int | None = None) -> "EventEnvelope":
        """Retag this event for a new attempt (used when a resume re-enters a run)."""
        if not attempt_id:
            raise ContractError("attempt_id must be non-empty")
        return EventEnvelope(
            namespace=self.namespace,
            run_id=self.run_id,
            attempt_id=attempt_id,
            kind=self.kind,
            payload=dict(self.payload),
            sequence=self.sequence if sequence is None else sequence,
            schema_version=self.schema_version,
            durable=self.durable,
        )


@dataclass(frozen=True)
class FenceDecision:
    """Whether an envelope may proceed, and why not when it may not."""

    accepted: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.accepted


class EventFence:
    """Decides whether an event belongs to the run and attempt being driven now.

    An instance describes one drive: the identity it serves and the last sequence it
    saw. A late event from an older attempt, an event from another namespace or run,
    an out-of-order sequence, and an event whose envelope predates a resume are all
    refused — with a reason, so a host can log why it dropped something instead of
    wondering where its progress updates went.
    """

    def __init__(self, *, namespace: str, run_id: str, attempt_id: str) -> None:
        for name, value in (("namespace", namespace), ("run_id", run_id), ("attempt_id", attempt_id)):
            if not value:
                raise ContractError(f"an event fence requires a non-empty {name}")
        self.namespace = namespace
        self.run_id = run_id
        self.attempt_id = attempt_id
        self._last_sequence = -1
        self._refusals: list[str] = []

    @classmethod
    def for_run(cls, run: Any, attempt_id: str | None = None) -> "EventFence":
        """Build a fence from a record or an identity."""
        identity = getattr(run, "identity", None) or run
        return cls(
            namespace=getattr(identity, "namespace", None) or getattr(run, "namespace", ""),
            run_id=getattr(identity, "run_id", None) or getattr(run, "run_id", ""),
            attempt_id=attempt_id or getattr(identity, "attempt_id", None) or getattr(run, "attempt_id", ""),
        )

    @property
    def last_sequence(self) -> int:
        return self._last_sequence

    @property
    def refusals(self) -> tuple[str, ...]:
        """Reasons this fence dropped something, in order. Never silently empty."""
        return tuple(self._refusals)

    def check(self, envelope: EventEnvelope) -> FenceDecision:
        """Decide without side effects; call :meth:`accept` to also advance."""
        if envelope.namespace != self.namespace:
            return FenceDecision(
                False,
                f"event is for namespace {envelope.namespace!r}, this drive serves {self.namespace!r}",
            )
        if envelope.run_id != self.run_id:
            return FenceDecision(
                False, f"event is for run {envelope.run_id!r}, this drive serves {self.run_id!r}"
            )
        if envelope.attempt_id != self.attempt_id:
            return FenceDecision(
                False,
                f"event is from attempt {envelope.attempt_id!r}, the current attempt is "
                f"{self.attempt_id!r} (a stale callback must not change this run)",
            )
        if envelope.sequence <= self._last_sequence:
            return FenceDecision(
                False,
                f"event sequence {envelope.sequence} is not after {self._last_sequence}; "
                "an out-of-order delivery must not be applied twice",
            )
        return FenceDecision(True, "")

    def accept(self, envelope: EventEnvelope) -> FenceDecision:
        """Check and, when accepted, advance the sequence. The only state change."""
        decision = self.check(envelope)
        if decision.accepted:
            self._last_sequence = envelope.sequence
        else:
            self._refusals.append(decision.reason)
        return decision

    def admits(self, envelope: EventEnvelope) -> bool:
        """``accept`` without recording a refusal (for a dry-run check)."""
        return self.check(envelope).accepted
