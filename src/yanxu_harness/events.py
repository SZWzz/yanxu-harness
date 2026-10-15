"""Neutral, versioned events and the ports that publish and replay them.

An event is the durable record of something that already happened. Two rules
follow from that and are enforced here rather than in documentation:

* **Public payloads are public.** Credentials, provider-internal frames,
  signatures and transport headers must never reach an event payload. A host
  that wants to keep private material keeps it in its own storage, keyed by the
  event, and never in the payload that is fanned out to clients and backups.
* **Ordering is explicit.** Events carry a monotonically increasing
  ``sequence`` per stream. Readers resume from a cursor, so a dropped fanout
  can be repaired by replay instead of by re-running the work.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from .errors import ContractError
from .versioning import SUPPORTED_EVENT_VERSIONS, require_version

__all__ = [
    "EVENT_SCHEMA_VERSION",
    "Event",
    "EventKind",
    "EventSink",
    "EventReader",
    "assert_public_payload",
    "PRIVATE_PAYLOAD_KEYS",
]

#: Schema version written for new events. Readers must accept every version in
#: :data:`yanxu_harness.versioning.SUPPORTED_EVENT_VERSIONS`.
EVENT_SCHEMA_VERSION = 1

#: Key names that never belong in a public payload. Matched case-insensitively
#: against every mapping key, at any depth.
PRIVATE_PAYLOAD_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "authorization",
        "auth_header",
        "bearer",
        "cookie",
        "credentials",
        "credential",
        "encrypted_content",
        "headers",
        "password",
        "provider_raw",
        "raw_frames",
        "raw_response",
        "reasoning_details",
        "refresh_token",
        "secret",
        "set_cookie",
        "signature",
    }
)


class EventKind(str, Enum):
    """The neutral event vocabulary.

    The host projects these into its own names; this enum is not a product
    contract and hosts must not be forced to rename their public events.
    """

    RUN_STARTED = "run_started"
    TURN_STARTED = "turn_started"
    TEXT_DELTA = "text_delta"
    THINKING_DELTA = "thinking_delta"
    TOOL_CALL_STARTED = "tool_call_started"
    TOOL_CALL_FINISHED = "tool_call_finished"
    APPROVAL_REQUESTED = "approval_requested"
    RUN_SUSPENDED = "run_suspended"
    #: A cancellation the run observed (requested while it was paused, or delivered to a
    #: live driver). Distinct from ``RUN_FINISHED``: the run ended *because* someone asked
    #: it to, and that reason is what a reader must not have to infer from a status alone.
    RUN_CANCELLED = "run_cancelled"
    USAGE_REPORTED = "usage_reported"
    RUN_FINISHED = "run_finished"
    ERROR = "error"


def _reject_private_keys(value: Any, path: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ContractError(f"{path}: event payload keys must be strings, got {key!r}")
            if key.lower() in PRIVATE_PAYLOAD_KEYS:
                raise ContractError(
                    f"{path}.{key}: private material is not allowed in a public event payload"
                )
            _reject_private_keys(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_private_keys(item, f"{path}[{index}]")
    elif isinstance(value, (str, int, float, bool)) or value is None:
        return
    else:
        raise ContractError(
            f"{path}: event payloads must be JSON-serialisable, got {type(value).__name__}"
        )


def assert_public_payload(payload: Mapping[str, Any]) -> None:
    """Raise :class:`ContractError` if ``payload`` carries private or unusable data."""
    if not isinstance(payload, Mapping):
        raise ContractError(f"event payload must be a mapping, got {type(payload).__name__}")
    _reject_private_keys(payload, "payload")


@dataclass(frozen=True)
class Event:
    """One durable event. ``payload`` is public by construction."""

    kind: EventKind
    payload: Mapping[str, Any] = field(default_factory=dict)
    sequence: int = 0
    run_id: str | None = None
    schema_version: int = EVENT_SCHEMA_VERSION
    at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        require_version("event", self.schema_version, SUPPORTED_EVENT_VERSIONS)
        if self.sequence < 0:
            raise ContractError(f"event sequence must be >= 0, got {self.sequence}")
        if not isinstance(self.kind, EventKind):
            raise ContractError(f"event kind must be an EventKind, got {self.kind!r}")
        assert_public_payload(self.payload)


@runtime_checkable
class EventSink(Protocol):
    """Publish events. Publishing must not be able to undo committed facts."""

    async def emit(self, event: Event) -> None:
        """Persist (when durable) and fan out ``event``."""


@runtime_checkable
class EventReader(Protocol):
    """Replay events from a cursor so a lost fanout can be repaired."""

    async def read_after(self, cursor: int, limit: int = 100) -> Sequence[Event]:
        """Return events with ``sequence > cursor``, oldest first."""
