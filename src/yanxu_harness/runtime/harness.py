"""The ``Harness`` port and the durable run record: lifecycle, terminal states, budgets.

The lifecycle contract, in one place:

* :class:`RunStatus` distinguishes *how* a run ended — and that it is still
  running. ``RUNNING`` is the only active phase; :class:`RunOutcome` refuses it,
  so an active run can never be handed back as if it had finished.
* A run that stopped because the turn budget ran out is ``usage_limited``, not
  ``completed``: an unfinished task must never look finished. ``unknown`` is not a
  status either — a provider failure is ``failed``, an unprovable external effect
  is reported by the tool layer.
* A suspended run is not terminal. It is resumed with an identity that keeps the
  original namespace/session/run and issues a new attempt.
* ``max_turns`` accepts ``None`` (unbounded) or a positive integer. ``0``,
  negative numbers, booleans and other types are refused *before* any model
  request, tool effect or lifecycle event — never silently treated as "no limit".

The status vocabulary itself lives in :mod:`yanxu_harness.core.outcome`, because
the model loop is what produces it; this module re-exports it so
``yanxu_harness.runtime.RunStatus`` is the same object as
``yanxu_harness.core.RunStatus``.

:class:`RunRecord` is the durable "what happened to this run" document a
:class:`Harness` implementation writes. It carries a schema version because it
outlives the process that wrote it, and a reader refuses a future version instead
of reconstructing a partial run from the fields it happens to recognise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from ..core.outcome import (
    MAX_TURNS_STOP_CODE,
    RUN_STATUS_TRANSITIONS,
    TERMINAL_STATUSES,
    RunStatus,
    assert_transition,
    can_transition,
    is_terminal_status,
)
from ..errors import ConfigurationError, ContractError
from ..versioning import SUPPORTED_RUN_RECORD_VERSIONS, require_version
from .context import RunContext

__all__ = [
    "Harness",
    "MAX_TURNS_ERROR",
    "MAX_TURNS_STOP_CODE",
    "RUN_RECORD_SCHEMA_VERSION",
    "RUN_STATUS_TRANSITIONS",
    "RunOutcome",
    "RunRecord",
    "RunRequest",
    "RunState",
    "RunStatus",
    "TERMINAL_STATUSES",
    "assert_transition",
    "can_transition",
    "is_terminal_status",
    "validate_max_concurrent_tools",
    "validate_max_turns",
]

MAX_TURNS_ERROR = "invalid_max_turns"

#: Schema version written for new run records. Readers accept every version in
#: :data:`yanxu_harness.versioning.SUPPORTED_RUN_RECORD_VERSIONS`.
RUN_RECORD_SCHEMA_VERSION = 1


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


def validate_max_concurrent_tools(value: object) -> int | None:
    """Return ``None`` or a positive int, or raise :class:`ConfigurationError`.

    Same shape as :func:`validate_max_turns`, including the ``bool`` refusal: a
    concurrency limit is a configuration decision and a flag must not silently mean
    "one at a time".
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(
            f"max_concurrent_tools must be None or a positive integer, got {value!r}"
        )
    if value <= 0:
        raise ConfigurationError(
            f"max_concurrent_tools must be positive, got {value!r}; "
            "use None for unbounded concurrency"
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
    """What the host gets back from one invocation.

    ``stop_code`` is the machine-readable reason the loop stopped (see
    :class:`~yanxu_harness.core.outcome.LoopStop`). It is deliberately separate
    from ``status``: a host hook that stopped the loop and a model that finished
    its answer are both ``completed``, and a caller should not have to infer which
    one happened from the text.
    """

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
    stop_code: str = ""
    #: What is still outstanding, from the committed execution state: blocking calls,
    #: calls that provably never ran (replayable), and whether a model request was in
    #: flight. Empty when the run carries no execution snapshot (a ``v1`` record) or
    #: nothing is outstanding. This is a projection, not a second source of truth.
    pending_work: Mapping[str, Any] = field(default_factory=dict)
    #: The projection facts behind the request view this run last prepared, or ``None``
    #: when no preparation is configured. A projection is derived: it explains the request
    #: that was (or was about to be) sent and grants no execution right.
    context_state: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, RunStatus):
            raise ContractError(f"run outcome status must be a RunStatus, got {self.status!r}")
        if self.status is RunStatus.RUNNING:
            raise ContractError(
                "a running run has no outcome yet; read state() while it is active"
            )
        if self.status is RunStatus.SUSPENDED and self.pending_interaction is None:
            raise ContractError("a suspended run must carry the pending interaction it waits for")
        if self.status is RunStatus.USAGE_LIMITED and not self.error_code:
            raise ContractError("a usage-limited run must carry a machine-readable error_code")
        object.__setattr__(self, "pending_work", dict(self.pending_work or {}))


@dataclass(frozen=True)
class RunState:
    """The durable state a host can read back at any time, including while running."""

    run_id: str
    status: RunStatus
    turns_used: int = 0
    attempt_id: str = ""
    pending_interaction: Mapping[str, Any] | None = None
    updated_at: float = 0.0
    stop_code: str = ""
    stop_reason: str = ""
    error_code: str | None = None
    detail: str = ""
    final_text: str = ""
    #: The same ``pending_work`` projection :class:`RunOutcome` carries.
    pending_work: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.status, RunStatus):
            raise ContractError(f"run state status must be a RunStatus, got {self.status!r}")
        object.__setattr__(self, "pending_work", dict(self.pending_work or {}))


@dataclass(frozen=True)
class RunRecord:
    """The durable record of one run. Exactly one row per run id."""

    run_id: str
    namespace: str
    session_id: str
    status: RunStatus
    attempt_id: str = ""
    turns_used: int = 0
    stop_code: str = ""
    #: The model's own stop reason for the last assistant message (raw, unmapped).
    stop_reason: str = ""
    error_code: str | None = None
    detail: str = ""
    final_text: str = ""
    usage: Mapping[str, Any] = field(default_factory=dict)
    pending_interaction: Mapping[str, Any] | None = None
    #: The committed transcript, serialized with ``core.types.message_to_json``.
    messages: tuple[Mapping[str, Any], ...] = ()
    #: The tool batch a suspension stopped in, with each call's classification and
    #: the interactions waiting for an answer. ``None`` when the run never stopped
    #: inside a batch. A suspended record without it cannot be resumed, and is
    #: refused rather than reconstructed from the transcript.
    batch: Mapping[str, Any] | None = None
    labels: Mapping[str, Any] = field(default_factory=dict)
    #: Fingerprint of the request this run was started with. A second submission
    #: with a different fingerprint is refused instead of overwriting the record.
    request_fingerprint: str = ""
    #: A cancellation that arrived while no local driver could act on it. It is
    #: recorded rather than guessed at; the next driver of this run must observe it.
    cancel_requested: Mapping[str, Any] | None = None
    #: The explicit execution state (``core.execution.state_to_record``) as of the last
    #: commit, so a restarted process can read where the run was instead of inferring it.
    #: Absent for records written before this field existed (``v1`` without a snapshot),
    #: which is a valid state: the run then recovers from ``messages``/``batch`` alone.
    #: The snapshot's own ``schema_version`` is refused when unknown.
    execution: Mapping[str, Any] | None = None
    #: The projection facts behind the request view this run last prepared
    #: (``context/controller.py``), committed in the same transaction as the state they
    #: describe. Absent is a valid state and not an error: a run with no configured
    #: preparation has none, and a record written before this field existed reads as none.
    #: It carries its own ``context_schema_version``, refused when unknown.
    context_state: Mapping[str, Any] | None = None
    updated_at: float = 0.0
    schema_version: int = RUN_RECORD_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_version("run record", self.schema_version, SUPPORTED_RUN_RECORD_VERSIONS)
        if not isinstance(self.status, RunStatus):
            raise ContractError(f"run record status must be a RunStatus, got {self.status!r}")
        for name in ("run_id", "namespace"):
            if not getattr(self, name):
                raise ContractError(f"run record requires a non-empty {name}")
        if self.status is RunStatus.SUSPENDED and self.pending_interaction is None:
            raise ContractError("a suspended run record must carry the pending interaction")
        if self.status is RunStatus.USAGE_LIMITED and not self.error_code:
            raise ContractError("a usage-limited run record must carry a machine-readable error_code")
        object.__setattr__(self, "messages", tuple(dict(item) for item in self.messages))
        object.__setattr__(self, "usage", dict(self.usage or {}))
        object.__setattr__(self, "labels", dict(self.labels or {}))
        if self.batch is not None:
            # The batch view is validated by `RunBatch.from_record` when a resume
            # reads it; storing it as a plain mapping keeps the record JSON-shaped.
            object.__setattr__(self, "batch", dict(self.batch))
        if self.pending_interaction is not None:
            object.__setattr__(self, "pending_interaction", dict(self.pending_interaction))
        if self.cancel_requested is not None:
            object.__setattr__(self, "cancel_requested", dict(self.cancel_requested))
        if self.execution is not None:
            object.__setattr__(self, "execution", dict(self.execution))
        if self.context_state is not None:
            from ..core.execution import context_state_from_record

            object.__setattr__(
                self, "context_state", dict(context_state_from_record(self.context_state) or {})
            )

    # -- reading --------------------------------------------------------

    def to_state(self) -> RunState:
        """The read-only view a host sees through ``Harness.state``."""
        return RunState(
            run_id=self.run_id,
            status=self.status,
            turns_used=self.turns_used,
            attempt_id=self.attempt_id,
            pending_interaction=self.pending_interaction,
            updated_at=self.updated_at,
            stop_code=self.stop_code,
            stop_reason=self.stop_reason,
            error_code=self.error_code,
            detail=self.detail,
            final_text=self.final_text,
            pending_work=self.pending_work(),
        )

    def pending_work(self) -> Mapping[str, Any]:
        """The outstanding work of the committed execution snapshot, if any.

        Read from the snapshot rather than recomputed here: the reducer is what
        classifies a call, and a second classification in the harness is how the two
        would drift. A record without a snapshot reports nothing outstanding, which is
        the same answer the pre-snapshot code gave.
        """
        if not self.execution:
            return {}
        work = self.execution.get("pending_work")
        return dict(work) if isinstance(work, Mapping) else {}

    @property
    def terminal(self) -> bool:
        return is_terminal_status(self.status)

    # -- serialization --------------------------------------------------

    def to_record(self) -> Mapping[str, Any]:
        """A JSON-ready record. Keys are stable and the schema version is included."""
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "namespace": self.namespace,
            "session_id": self.session_id,
            "attempt_id": self.attempt_id,
            "status": self.status.value,
            "turns_used": self.turns_used,
            "stop_code": self.stop_code,
            "stop_reason": self.stop_reason,
            "error_code": self.error_code,
            "detail": self.detail,
            "final_text": self.final_text,
            "usage": dict(self.usage),
            "pending_interaction": None if self.pending_interaction is None else dict(self.pending_interaction),
            "messages": [dict(item) for item in self.messages],
            "batch": None if self.batch is None else dict(self.batch),
            "labels": dict(self.labels),
            "request_fingerprint": self.request_fingerprint,
            "cancel_requested": None if self.cancel_requested is None else dict(self.cancel_requested),
            "execution": None if self.execution is None else dict(self.execution),
            "context_state": None if self.context_state is None else dict(self.context_state),
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "RunRecord":
        """Rebuild a record, refusing a version this build cannot prove safe.

        Unknown *extra* keys are ignored on purpose (a newer writer may add an
        optional field); a missing required key is a hard failure, and a future
        ``schema_version`` is refused before anything is interpreted.
        """
        if not isinstance(record, Mapping):
            raise ContractError(f"a run record must be a mapping, got {type(record).__name__}")
        if "schema_version" not in record:
            raise ContractError(
                "a run record without a schema_version cannot be migrated; refusing instead of guessing"
            )
        version = require_version(
            "run record", record["schema_version"], SUPPORTED_RUN_RECORD_VERSIONS
        )
        for required in ("run_id", "namespace", "status"):
            if not record.get(required):
                raise ContractError(f"run record is missing {required!r}")
        try:
            status = RunStatus(str(record["status"]))
        except ValueError as exc:
            raise ContractError(f"run record status {record['status']!r} is not a known RunStatus") from exc
        return cls(
            run_id=str(record["run_id"]),
            namespace=str(record["namespace"]),
            session_id=str(record.get("session_id") or ""),
            status=status,
            attempt_id=str(record.get("attempt_id") or ""),
            turns_used=int(record.get("turns_used") or 0),
            stop_code=str(record.get("stop_code") or ""),
            stop_reason=str(record.get("stop_reason") or ""),
            error_code=record.get("error_code"),
            detail=str(record.get("detail") or ""),
            final_text=str(record.get("final_text") or ""),
            usage=dict(record.get("usage") or {}),
            pending_interaction=record.get("pending_interaction"),
            messages=tuple(record.get("messages") or ()),
            batch=record.get("batch"),
            labels=dict(record.get("labels") or {}),
            request_fingerprint=str(record.get("request_fingerprint") or ""),
            cancel_requested=record.get("cancel_requested"),
            execution=record.get("execution"),
            context_state=record.get("context_state"),
            updated_at=float(record.get("updated_at") or 0.0),
            schema_version=version,
        )


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
