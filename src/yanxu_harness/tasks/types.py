"""The durable task contract: identity, status, inputs, children and budgets.

This module is the **frozen vocabulary** the orchestrator is built from. It is deliberately just
data and pure decisions: no store, no clock, no I/O, no model, no worker. Everything here can be
decided in a unit test, which is what makes the transactional parts in
:mod:`yanxu_harness.tasks.ports` and :mod:`yanxu_harness.tasks.inbox` checkable against a fixed
contract instead of against whatever the implementation happened to do.

Six objects, each with one owner (plan §5.1):

* **namespace** — the tenant/project isolation boundary, established by the host;
* **session** — a conversation grouping. It is *not* a container that shares task state or
  permissions: two tasks in one session do not share a budget, a workspace or an authority;
* **task** — one unit of work that can be queued, waited on, cancelled and verified. It is bound
  to exactly one logical ``run`` for its whole life;
* **run** — the existing Core execution, the authority on what happened. A worker restart does not
  change the run id, and the task never holds a second copy of the run's status;
* **attempt** — one claim of the execution right. A new claim gets a new id; the logical task and
  its budget do not reset;
* **input** — one admitted, ordered input with a stable id, a content digest, a target and a
  consumption fact;
* **child relation** — parent task, stable spawn identity, child task, authorization and budget
  reservation.

Two rules the rest of the package leans on, stated here once:

* **a status is not evidence.** ``TaskStatus.COMPLETED`` says the task's work is settled by the
  facts the control store holds; it never says a tool side effect happened. Side effects are
  Gateway receipts (:mod:`yanxu_harness.tools`), and a message stored, a coroutine cancelled or a
  worker stopped prove nothing about them;
* **terminal means terminal.** No task is revived by a late input, an old callback or a child
  result that arrives after the parent ended: the host creates a new task and a new run.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Mapping, Sequence

from ..errors import ContractError, HarnessError, VersionRefused
from ..versioning import fingerprint

__all__ = [
    "ADMISSION_OUTCOMES",
    "BudgetRefused",
    "ClaimLost",
    "InboxMoved",
    "PendingInputsRemain",
    "BACKLOG_LIMITS",
    "CHILD_SPAWN_STATES",
    "COORDINATION_METHODS",
    "COORDINATION_READ_METHODS",
    "CONVERSATION_KINDS",
    "BacklogLimits",
    "BudgetReservation",
    "BudgetState",
    "BudgetUnit",
    "ChildOutcome",
    "ChildRefused",
    "ChildRelation",
    "ChildResult",
    "ChildSpawnState",
    "OUTBOX_CHILD_COMPLETED",
    "OUTBOX_CHILD_SPAWNED",
    "SPAWN_REFUSALS",
    "SpawnRefusal",
    "ControlOutboxEntry",
    "DispatchClaim",
    "INPUT_LIMITS",
    "INPUT_ORDER",
    "InputAdmission",
    "InputContent",
    "InputKind",
    "InputRead",
    "InputRecord",
    "InputSelection",
    "InputState",
    "AdmissionOutcome",
    "AdmissionResult",
    "RefusalCode",
    "ReservationState",
    "TASK_SCHEMA_VERSION",
    "TaskAdmission",
    "TASK_TREE_LIMITS",
    "TaskRecord",
    "TaskStatus",
    "TaskTreeLimits",
    "assert_transition",
    "can_transition",
    "decide_admission",
    "select_inputs",
    "TERMINAL_TASK_STATUSES",
    "transition_reasons",
]

#: Version of every record in this module. A reader refuses an unknown future version rather than
#: reconstructing a task from the fields it happens to recognise.
TASK_SCHEMA_VERSION = 1
SUPPORTED_TASK_SCHEMA_VERSIONS = (1,)


class ClaimLost(HarnessError):
    """The execution right this caller held is no longer the current one.

    Raised inside the transaction that was about to commit: another worker has claimed the task
    (a bigger fencing token), the claim expired, or it was released. The caller stops dispatching
    and does not write — a late commit from a claim someone else now holds is exactly what the
    fence exists to refuse. It says nothing about whether an external tool is still running.
    """

    code = "claim_lost"

    def __init__(self, task_id: str, detail: str = "") -> None:
        super().__init__(f"the execution right for task {task_id!r} was lost" + (f": {detail}" if detail else ""))
        self.task_id = task_id
        self.detail = detail


class InboxMoved(HarnessError):
    """The inbox changed while a run was deciding to finish.

    Raised inside the terminal transaction: an input was accepted after the run observed revision
    *N* and before it committed its end. Finishing anyway would drop an accepted input, so the
    commit is refused and the caller re-enters the polling boundary — the run continues instead of
    ending over work it was told about.
    """

    code = "inbox_moved"

    def __init__(self, observed: int, expected: int) -> None:
        super().__init__(
            f"the inbox is at revision {observed}, not the {expected} the finishing run observed; "
            "an input arrived while the run was deciding to stop"
        )
        self.observed = observed
        self.expected = expected


class PendingInputsRemain(HarnessError):
    """A terminal commit was refused because unprocessed conversation inputs remain.

    Raised inside the final write transaction (plan §5.2): a run that decides to finish while an
    accepted conversation input is still ``pending``/``claimed`` must re-enter the polling
    boundary and integrate it, instead of leaving an accepted input in a finished run. It is a
    *refusal of the finish*, not a failure of the run.
    """

    code = "pending_inputs_remain"

    def __init__(self, task_id: str, input_ids: Sequence[str] = ()) -> None:
        super().__init__(
            f"task {task_id!r} still holds unprocessed conversation inputs "
            f"{list(input_ids)}; the run must return to the polling boundary instead of "
            "finishing over them"
        )
        self.task_id = task_id
        self.input_ids = tuple(input_ids)


class BudgetRefused(HarnessError):
    """A reservation would exceed the allowance, so it was not granted.

    Raised *before* the dispatch window opens: the caller records the refusal and stops
    dispatching rather than spending an allowance the root never granted. ``available`` and
    ``requested`` are the numbers that decided it, so a report can show the arithmetic.
    """

    code = "budget_refused"

    def __init__(
        self,
        root_task_id: str,
        unit: str,
        requested: float,
        available: float | None,
        detail: str = "",
    ) -> None:
        super().__init__(
            f"reserving {requested} {unit} for {root_task_id!r} was refused"
            + (f" (available {available})" if available is not None else " (no limit declared)")
            + (f": {detail}" if detail else "")
        )
        self.root_task_id = root_task_id
        self.unit = unit
        self.requested = requested
        self.available = available
        self.detail = detail


class SpawnRefusal(str, Enum):
    """Why a spawn was refused. Every refusal names one of these.

    A refusal is a *recorded* decision, raised before the relation, the child task, the grant or
    the notice are written: the whole point is that nothing is created. A caller that catches one
    and reports success anyway is the failure mode this vocabulary exists to make visible.
    """

    UNKNOWN_PARENT = "unknown_parent"
    PARENT_TERMINAL = "parent_terminal"
    CANCELLED_FIRST = "cancelled_first"
    NOT_PERMITTED = "not_permitted"
    DEPTH_EXCEEDED = "depth_exceeded"
    TREE_FULL = "tree_full"
    IDENTITY_MISMATCH = "identity_mismatch"
    CONFIG_CONFLICT = "config_conflict"
    BUDGET_REFUSED = "budget_refused"
    UNKNOWN_CHILD = "unknown_child"
    NOT_A_CHILD = "not_a_child"


SPAWN_REFUSALS = tuple(item.value for item in SpawnRefusal)


class ChildRefused(HarnessError):
    """A spawn, wait, completion or cancellation the control plane refused.

    ``code`` is one of :class:`SpawnRefusal`. Refusals are expected outcomes — a parent that may
    not delegate, a child outside its own tree, a grant that would exceed the root — so they are
    raised *before* any transaction writes and carry enough detail to report the arithmetic.
    """

    code = "child_refused"

    def __init__(self, refusal: SpawnRefusal | str, detail: str = "") -> None:
        value = refusal.value if isinstance(refusal, SpawnRefusal) else str(refusal)
        super().__init__(f"the child operation was refused ({value})" + (f": {detail}" if detail else ""))
        self.refusal = value
        self.detail = detail


def _require_version(what: str, version: Any) -> int:
    version = 1 if version is None else int(version)
    if version not in SUPPORTED_TASK_SCHEMA_VERSIONS:
        raise VersionRefused(what, version, SUPPORTED_TASK_SCHEMA_VERSIONS)
    return version


# --------------------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------------------


class TaskStatus(str, Enum):
    """Where a task stands in the *control* store — not where its run stands.

    ``QUEUED`` and ``WAITING`` exist so that "the scheduler has not picked this up yet" and "this
    is blocked on a fact outside the run" never have to be expressed as a run status. A queued
    task has no run activity to describe, and a waiting task is not a model that failed.
    """

    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    USAGE_LIMITED = "usage_limited"

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_TASK_STATUSES

    @property
    def holds_execution_slot(self) -> bool:
        """Does a worker occupy a concurrency slot while the task is in this state?"""
        return self is TaskStatus.RUNNING


TERMINAL_TASK_STATUSES = frozenset(
    {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.USAGE_LIMITED,
    }
)

#: The reason codes a transition may carry. Every edge names the reasons it accepts, so "why did
#: this task change state" is answerable from the record instead of from a log line.
REASON_ADMITTED = "admitted"
REASON_CLAIMED = "execution_right_claimed"
REASON_RIGHT_LOST = "execution_right_lost"
REASON_AWAITING = "awaiting_external_fact"
REASON_RESUMED = "external_fact_persisted"
REASON_CANCEL_REQUESTED = "cancel_requested"
REASON_COMPLETED = "work_completed"
REASON_FAILED = "work_failed"
REASON_CANCELLED = "cancellation_settled"
REASON_USAGE_LIMITED = "budget_exhausted"

#: The transition table: ``(from, to) -> allowed reasons``. Symmetry is not assumed; a missing
#: pair is refused, and so is a pair present with the wrong reason.
_TASK_TRANSITIONS: Mapping[tuple[TaskStatus, TaskStatus], frozenset[str]] = {
    (TaskStatus.QUEUED, TaskStatus.RUNNING): frozenset({REASON_CLAIMED}),
    (TaskStatus.QUEUED, TaskStatus.CANCELLING): frozenset({REASON_CANCEL_REQUESTED}),
    (TaskStatus.QUEUED, TaskStatus.FAILED): frozenset({REASON_FAILED}),
    (TaskStatus.QUEUED, TaskStatus.USAGE_LIMITED): frozenset({REASON_USAGE_LIMITED}),
    # A worker that loses its claim, or hands the task back, returns it to the queue: another
    # attempt (with a new attempt id, same logical task and budget) may take it.
    (TaskStatus.RUNNING, TaskStatus.QUEUED): frozenset({REASON_RIGHT_LOST}),
    (TaskStatus.RUNNING, TaskStatus.WAITING): frozenset({REASON_AWAITING}),
    (TaskStatus.RUNNING, TaskStatus.CANCELLING): frozenset({REASON_CANCEL_REQUESTED}),
    (TaskStatus.RUNNING, TaskStatus.COMPLETED): frozenset({REASON_COMPLETED}),
    (TaskStatus.RUNNING, TaskStatus.FAILED): frozenset({REASON_FAILED}),
    (TaskStatus.RUNNING, TaskStatus.CANCELLED): frozenset({REASON_CANCELLED}),
    (TaskStatus.RUNNING, TaskStatus.USAGE_LIMITED): frozenset({REASON_USAGE_LIMITED}),
    (TaskStatus.WAITING, TaskStatus.QUEUED): frozenset({REASON_RESUMED}),
    (TaskStatus.WAITING, TaskStatus.CANCELLING): frozenset({REASON_CANCEL_REQUESTED}),
    (TaskStatus.WAITING, TaskStatus.FAILED): frozenset({REASON_FAILED}),
    # Cancellation is a request, not a verdict. If the run finished first, or needs to publish
    # the facts it already has, the task settles as whatever the facts say and the request stays
    # in the record.
    (TaskStatus.CANCELLING, TaskStatus.CANCELLED): frozenset({REASON_CANCELLED}),
    (TaskStatus.CANCELLING, TaskStatus.COMPLETED): frozenset({REASON_COMPLETED}),
    (TaskStatus.CANCELLING, TaskStatus.FAILED): frozenset({REASON_FAILED}),
    (TaskStatus.CANCELLING, TaskStatus.USAGE_LIMITED): frozenset({REASON_USAGE_LIMITED}),
    (TaskStatus.CANCELLING, TaskStatus.WAITING): frozenset({REASON_AWAITING}),
}


def can_transition(start: TaskStatus, end: TaskStatus, reason: str) -> bool:
    """May a task move from ``start`` to ``end`` for ``reason``? Pure, total, no I/O."""
    return reason in _TASK_TRANSITIONS.get((start, end), frozenset())


def transition_reasons(start: TaskStatus, end: TaskStatus) -> frozenset[str]:
    return _TASK_TRANSITIONS.get((start, end), frozenset())


def assert_transition(start: TaskStatus, end: TaskStatus, reason: str) -> None:
    """Refuse an illegal transition, naming the edge and the reason that was offered."""
    if start is end:
        # Staying put is not a transition: a lease renewal or an idempotent re-commit must not
        # be forced through the table.
        return
    if start.is_terminal:
        raise ContractError(
            f"a task in {start.value} is terminal; it is not revived as {end.value} "
            f"(reason {reason!r}). New work is a new task, not a resumed one"
        )
    allowed = _TASK_TRANSITIONS.get((start, end))
    if allowed is None:
        raise ContractError(
            f"no transition from {start.value} to {end.value} exists in the task table"
        )
    if reason not in allowed:
        raise ContractError(
            f"the {start.value} -> {end.value} transition accepts reasons "
            f"{sorted(allowed)}, not {reason!r}"
        )


def transition_table() -> Mapping[tuple[str, str], list[str]]:
    """The table as plain data, for documentation and for a test that pins it."""
    return {
        (start.value, end.value): sorted(reasons)
        for (start, end), reasons in sorted(
            _TASK_TRANSITIONS.items(), key=lambda item: (item[0][0].value, item[0][1].value)
        )
    }


# --------------------------------------------------------------------------------------
# inputs
# --------------------------------------------------------------------------------------


class InputKind(str, Enum):
    """What an admitted input *is*, decided by the host, never by a model's text."""

    INITIAL = "initial"
    STEERING = "steering"
    FOLLOW_UP = "follow_up"
    CANCEL_REQUEST = "cancel_request"
    APPROVAL_DECISION = "approval_decision"
    CHILD_COMPLETED = "child_completed"

    @property
    def is_control(self) -> bool:
        """Control entries change authority or lifecycle; they are not conversation text.

        A model may *ask* for something, but it cannot type its way into cancelling work,
        approving an operation or declaring a child finished.
        """
        return self in TRUSTED_CONTROL_KINDS


#: Entries a trusted host or an internal fact may submit.
TRUSTED_CONTROL_KINDS = frozenset(
    {InputKind.CANCEL_REQUEST, InputKind.APPROVAL_DECISION, InputKind.CHILD_COMPLETED}
)

#: Entries a model's own output may be projected into. Note that steering and follow-up are
#: conversation, not control: they cannot approve, cancel or complete anything.
MODEL_PROPOSABLE_KINDS = frozenset({InputKind.STEERING, InputKind.FOLLOW_UP})

#: Where each kind sits in the run's polling loop, and how much of it is taken at once.
#: ``INITIAL`` opens the run; ``STEERING`` is what the model reads before its next request (all of
#: it, in order); ``FOLLOW_UP`` is taken one at a time so a backlog of follow-ups becomes several
#: turns rather than one enormous prompt. ``CANCEL_REQUEST`` is not polled as conversation: it is
#: a control entry the runner checks before dispatch.
INPUT_ORDER: Mapping[InputKind, str] = {
    InputKind.INITIAL: "one-at-a-time",
    InputKind.STEERING: "all",
    InputKind.FOLLOW_UP: "one-at-a-time",
    InputKind.CANCEL_REQUEST: "control",
    InputKind.APPROVAL_DECISION: "control",
    InputKind.CHILD_COMPLETED: "control",
}

#: The kinds a poll site may hand to the model. ``INITIAL`` opens the run rather than being polled
#: as conversation, and the ``INPUT_ORDER == "control"`` entries change authority or lifecycle, so
#: neither is ever read as text. A pending-input query must use exactly this filter: a control
#: entry must not hold the run open, and it must not be dropped by being mistaken for one.
CONVERSATION_KINDS: tuple[InputKind, ...] = tuple(
    kind for kind in InputKind if kind is not InputKind.INITIAL and INPUT_ORDER[kind] != "control"
)


class InputState(str, Enum):
    """How far one input got.

    ``CLAIMED`` is a temporary consumption right, not an application: a worker that dies between
    claiming and committing leaves the input claimable again. ``APPLIED`` must name the run
    revision that took it into the durable execution state, because "it was taken out of the
    queue" is not evidence that it reached the run.
    """

    PENDING = "pending"
    CLAIMED = "claimed"
    APPLIED = "applied"
    CANCELLED = "cancelled"
    REFUSED = "refused"


class AdmissionOutcome(str, Enum):
    """The only three answers an admission may give."""

    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    REFUSED = "refused"


ADMISSION_OUTCOMES = tuple(item.value for item in AdmissionOutcome)


class RefusalCode(str, Enum):
    """Why an input was refused. Every refusal names one of these."""

    TASK_TERMINAL = "task_terminal"
    #: The task is not terminal yet, but the run it is bound to already is. This is the "run
    #: finished, task not settled" window: admitting here would queue work no reader will ever
    #: reach, so the input is refused instead of accepted-then-dropped.
    RUN_TERMINAL = "run_terminal"
    CANCELLED_FIRST = "cancelled_first"
    CONTENT_CONFLICT = "content_conflict"
    BACKLOG_FULL = "backlog_full"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    MODEL_CANNOT_CONTROL = "model_cannot_control"
    IDENTITY_MISMATCH = "identity_mismatch"
    UNKNOWN_TARGET = "unknown_target"
    NOT_PERMITTED = "not_permitted"


@dataclass(frozen=True)
class InputContent:
    """The semantically meaningful part of an input, and its digest.

    The digest covers the *kind*, the target and the payload's semantic fields — not its encoding,
    not a timestamp and not a sequence number. Two admissions with the same identity and the same
    content are the same request; the same identity with different content is a conflict the
    caller must resolve, never a silent overwrite.
    """

    kind: InputKind
    text: str = ""
    #: A control entry's own fields. Kept separate from conversation text on purpose: a payload
    #: that could be either is a payload whose authority is ambiguous.
    decision: str = ""
    child_id: str = ""
    parameters: Mapping[str, Any] = field(default_factory=dict)

    def to_record(self) -> Mapping[str, Any]:
        return {
            "kind": self.kind.value,
            "text": self.text,
            "decision": self.decision,
            "child_id": self.child_id,
            "parameters": {
                str(key): self.parameters[key] for key in sorted(self.parameters)
            },
        }

    @property
    def digest(self) -> str:
        return fingerprint(self.to_record())

    @property
    def size_bytes(self) -> int:
        """The payload size used for byte-bounded backlogs."""
        import json

        return len(json.dumps(self.to_record(), ensure_ascii=False).encode("utf-8"))


@dataclass(frozen=True)
class InputAdmission:
    """One request to admit an input. The store answers :class:`AdmissionResult`.

    ``input_id`` is the idempotency key together with the task identity: the caller keeps it
    stable across a retry, and a retry must not consume capacity twice.
    """

    namespace: str
    session_id: str
    task_id: str
    input_id: str
    content: InputContent
    #: ``host`` for a host-submitted entry, ``internal`` for a fact this build produced (a child's
    #: completion), ``model`` for text projected from model output. The source bounds what may be
    #: admitted at all.
    source: str = "host"
    #: When set, the admission must be refused unless the target run is still at or before this
    #: revision — how a caller prevents an input from slipping in after a terminal commit it has
    #: already observed.
    observed_run_revision: int | None = None

    def __post_init__(self) -> None:
        for name in ("namespace", "session_id", "task_id", "input_id"):
            if not getattr(self, name):
                raise ContractError(f"an input admission requires a non-empty {name}")
        if self.source not in {"host", "internal", "model"}:
            raise ContractError(f"unknown input source {self.source!r}")
        if self.source == "model" and self.content.kind.is_control:
            raise ContractError(
                f"a model-sourced input cannot be {self.content.kind.value}: control entries "
                "change authority and lifecycle, and are submitted by the host or by an internal "
                "fact"
            )


@dataclass(frozen=True)
class InputRecord:
    """One admitted input, as the control store keeps it."""

    namespace: str
    session_id: str
    task_id: str
    input_id: str
    kind: InputKind
    sequence: int
    content_digest: str
    content: InputContent
    state: InputState = InputState.PENDING
    source: str = "host"
    attempts: int = 0
    claimed_by: str = ""
    claim_expires_at: float | None = None
    applied_run_revision: int | None = None
    applied_event_sequence: int | None = None
    refusal_code: str = ""
    created_at: float = 0.0
    schema_version: int = TASK_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in ("namespace", "session_id", "task_id", "input_id", "content_digest"):
            if not getattr(self, name):
                raise ContractError(f"an input record requires a non-empty {name}")
        if self.sequence < 0:
            raise ContractError("an admitted input's sequence cannot be negative")
        if self.state is InputState.APPLIED and self.applied_run_revision is None:
            raise ContractError(
                "an applied input must name the run revision that took it into the durable "
                "state; being taken out of the queue is not application"
            )
        if self.state is InputState.REFUSED and not self.refusal_code:
            raise ContractError("a refused input must say why it was refused")
        if self.state is InputState.CLAIMED and (not self.claimed_by or self.claim_expires_at is None):
            raise ContractError("a claimed input must name its holder and its claim expiry")
        if self.state is not InputState.CLAIMED and self.claimed_by:
            raise ContractError(
                f"an input in {self.state.value} cannot still be claimed by {self.claimed_by!r}"
            )
        if self.state in {InputState.PENDING, InputState.CANCELLED} and self.applied_run_revision is not None:
            raise ContractError(
                f"an input in {self.state.value} cannot carry an applied revision"
            )

    def to_record(self) -> Mapping[str, Any]:
        return {
            "schema_version": self.schema_version,
            "namespace": self.namespace,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "input_id": self.input_id,
            "kind": self.kind.value,
            "sequence": self.sequence,
            "content_digest": self.content_digest,
            "content": self.content.to_record(),
            "state": self.state.value,
            "source": self.source,
            "attempts": self.attempts,
            "claimed_by": self.claimed_by,
            "claim_expires_at": self.claim_expires_at,
            "applied_run_revision": self.applied_run_revision,
            "applied_event_sequence": self.applied_event_sequence,
            "refusal_code": self.refusal_code,
            "created_at": self.created_at,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "InputRecord":
        _require_version("input record", record.get("schema_version"))
        content = dict(record.get("content") or {})
        return cls(
            namespace=str(record.get("namespace") or ""),
            session_id=str(record.get("session_id") or ""),
            task_id=str(record.get("task_id") or ""),
            input_id=str(record.get("input_id") or ""),
            kind=InputKind(str(record.get("kind") or InputKind.STEERING.value)),
            sequence=int(record.get("sequence") or 0),
            content_digest=str(record.get("content_digest") or ""),
            content=InputContent(
                kind=InputKind(str(content.get("kind") or record.get("kind") or "steering")),
                text=str(content.get("text") or ""),
                decision=str(content.get("decision") or ""),
                child_id=str(content.get("child_id") or ""),
                parameters=dict(content.get("parameters") or {}),
            ),
            state=InputState(str(record.get("state") or InputState.PENDING.value)),
            source=str(record.get("source") or "host"),
            attempts=int(record.get("attempts") or 0),
            claimed_by=str(record.get("claimed_by") or ""),
            claim_expires_at=record.get("claim_expires_at"),
            applied_run_revision=record.get("applied_run_revision"),
            applied_event_sequence=record.get("applied_event_sequence"),
            refusal_code=str(record.get("refusal_code") or ""),
            created_at=float(record.get("created_at") or 0.0),
            schema_version=int(record.get("schema_version") or TASK_SCHEMA_VERSION),
        )

    def claim(self, holder: str, expires_at: float, *, now: float | None = None) -> "InputRecord":
        """Take the temporary consumption right. Only a claimable input may be claimed.

        An expired claim is claimable again — a worker that died between claiming and committing
        must not strand the input — and that is *not* evidence about whether the run applied it:
        the run's own snapshot decides that (plan §7.2).
        """
        if self.state in {InputState.APPLIED, InputState.REFUSED, InputState.CANCELLED}:
            raise ContractError(
                f"an input in {self.state.value} cannot be claimed again"
            )
        if self.state is InputState.CLAIMED:
            if now is not None and self.claim_expires_at is not None and self.claim_expires_at > now:
                raise ContractError(
                    f"input {self.input_id!r} is claimed by {self.claimed_by!r} until "
                    f"{self.claim_expires_at}"
                )
        return replace(
            self,
            state=InputState.CLAIMED,
            claimed_by=holder,
            claim_expires_at=expires_at,
            attempts=self.attempts + 1,
        )

    def apply(self, *, run_revision: int, event_sequence: int | None = None) -> "InputRecord":
        if self.state is InputState.APPLIED:
            raise ContractError(f"input {self.input_id!r} was already applied")
        return replace(
            self,
            state=InputState.APPLIED,
            applied_run_revision=run_revision,
            applied_event_sequence=event_sequence,
            claimed_by="",
            claim_expires_at=None,
        )

    def cancel(self) -> "InputRecord":
        if self.state is InputState.APPLIED:
            raise ContractError("an applied input cannot be cancelled: it is already in the run")
        return replace(self, state=InputState.CANCELLED, claimed_by="", claim_expires_at=None)

    def refuse(self, code: RefusalCode | str) -> "InputRecord":
        value = code.value if isinstance(code, RefusalCode) else str(code)
        return replace(
            self,
            state=InputState.REFUSED,
            refusal_code=value,
            claimed_by="",
            claim_expires_at=None,
        )


@dataclass(frozen=True)
class AdmissionResult:
    """The store's answer to one :class:`InputAdmission`."""

    outcome: AdmissionOutcome
    record: InputRecord | None = None
    refusal_code: str = ""
    #: For a duplicate: the record that already exists, so the caller can read the original
    #: acceptance instead of re-arguing about it.
    duplicate_of: str = ""

    def __post_init__(self) -> None:
        if self.outcome is AdmissionOutcome.REFUSED and not self.refusal_code:
            raise ContractError("a refused admission must carry a refusal code")
        if self.outcome is not AdmissionOutcome.REFUSED and self.refusal_code:
            raise ContractError("only a refused admission carries a refusal code")
        if self.outcome is AdmissionOutcome.ACCEPTED and self.record is None:
            raise ContractError("an accepted admission must carry the record it accepted")
        if self.outcome is AdmissionOutcome.DUPLICATE and not self.duplicate_of:
            raise ContractError("a duplicate admission must point at the acceptance it repeats")

    @property
    def accepted(self) -> bool:
        """True only for a *new* acceptance: a duplicate is not a second acceptance."""
        return self.outcome is AdmissionOutcome.ACCEPTED

    @property
    def applied(self) -> bool:
        """False by construction: acceptance is reliability, not application (plan §5.2)."""
        return False

    def to_record(self) -> Mapping[str, Any]:
        return {
            "outcome": self.outcome.value,
            "input_id": None if self.record is None else self.record.input_id,
            "sequence": None if self.record is None else self.record.sequence,
            "state": None if self.record is None else self.record.state.value,
            "refusal_code": self.refusal_code,
            "duplicate_of": self.duplicate_of,
        }


@dataclass(frozen=True)
class BacklogLimits:
    """How much un-consumed input one control store may hold.

    Bounded by count *and* by bytes: a hundred one-megabyte follow-ups are not the same backlog as
    a hundred one-line ones. Reaching a limit refuses the new admission; it never discards an
    input that was already accepted.
    """

    per_task: int = 64
    per_namespace: int = 512
    total: int = 4096
    max_payload_bytes: int = 1_048_576
    max_task_bytes: int = 8_388_608

    def __post_init__(self) -> None:
        for name in ("per_task", "per_namespace", "total", "max_payload_bytes", "max_task_bytes"):
            if getattr(self, name) <= 0:
                raise ContractError(f"backlog limit {name} must be positive")


#: The limits this build admits against.
BACKLOG_LIMITS = BacklogLimits()


@dataclass(frozen=True)
class TaskTreeLimits:
    """How large and how deep one task tree may grow.

    The first version is deliberately shallow — a root and its children — because a deeper tree
    needs the cancellation, budget and wait semantics of every level to be settled first. The
    bounds are data so a host can tighten them; they are never implied by "the scheduler is
    small".
    """

    max_tasks_per_root: int = 8
    max_depth: int = 1

    def __post_init__(self) -> None:
        if self.max_tasks_per_root < 1:
            raise ContractError("a task tree needs room for at least its root")
        if self.max_depth < 0:
            raise ContractError("a task tree's depth cannot be negative")

    def allows_depth(self, depth: int) -> bool:
        return 0 <= depth <= self.max_depth


#: The tree bounds this build enforces unless a host tightens them.
TASK_TREE_LIMITS = TaskTreeLimits()

#: Per-kind input limits. Today only the payload bound is per kind; the table is here so a kind
#: that later needs its own bound has one place to state it.
INPUT_LIMITS: Mapping[InputKind, Mapping[str, int]] = {
    kind: {"max_payload_bytes": BACKLOG_LIMITS.max_payload_bytes} for kind in InputKind
}


def decide_admission(
    *,
    existing: InputRecord | None,
    incoming: InputAdmission,
    task_status: TaskStatus,
    cancel_requested: bool = False,
    pending_count: int = 0,
    pending_bytes: int = 0,
    limits: BacklogLimits | None = None,
    run_terminal: bool = False,
) -> AdmissionResult:
    """The admission decision, as a pure function.

    The store runs this **inside its transaction**, after re-reading the duplicate record, the task
    status and the backlog counters (plan §5.2/§6.1): a decision made from values read before the
    transaction would be a decision about a moment that has passed.
    """
    limits = limits or BACKLOG_LIMITS
    if incoming.content.kind.is_control and incoming.source == "model":
        return AdmissionResult(
            AdmissionOutcome.REFUSED, refusal_code=RefusalCode.MODEL_CANNOT_CONTROL.value
        )
    if task_status.is_terminal:
        # An accepted-then-dropped input is worse than a refusal: the caller would believe the
        # work is queued. Terminal tasks refuse new input; only the host creates new work.
        return AdmissionResult(
            AdmissionOutcome.REFUSED, refusal_code=RefusalCode.TASK_TERMINAL.value
        )
    # The idempotency key is (task, input_id). The same key is a retry unless its content differs.
    # This is checked *before* the run-terminal refusal so a duplicate of an input that was
    # already admitted keeps its idempotent answer even after the run finished (plan §5.2).
    if existing is not None:
        if existing.content_digest == incoming.content.digest:
            return AdmissionResult(
                AdmissionOutcome.DUPLICATE, record=existing, duplicate_of=existing.input_id
            )
        return AdmissionResult(
            AdmissionOutcome.REFUSED, refusal_code=RefusalCode.CONTENT_CONFLICT.value
        )
    if run_terminal:
        # The task row is not terminal yet, but the run it is bound to already is: this is the
        # "run finished, task not settled" window, and admitting here would queue work no reader
        # can ever reach. Only the host creates new work, by admitting a new task (plan §5.2).
        return AdmissionResult(
            AdmissionOutcome.REFUSED, refusal_code=RefusalCode.RUN_TERMINAL.value
        )
    if incoming.observed_run_revision is not None and incoming.observed_run_revision < 0:
        return AdmissionResult(
            AdmissionOutcome.REFUSED, refusal_code=RefusalCode.NOT_PERMITTED.value
        )
    # A cancel request and a new steering input are different races: the cancel wins for the
    # *lifecycle*, while an input that is accepted before the cancel stays visible as a cancelled
    # fact (plan §5.2). Conversation text after a cancel request is refused: the run is ending.
    if cancel_requested and not incoming.content.kind.is_control:
        return AdmissionResult(
            AdmissionOutcome.REFUSED, refusal_code=RefusalCode.CANCELLED_FIRST.value
        )
    if incoming.content.size_bytes > limits.max_payload_bytes:
        return AdmissionResult(
            AdmissionOutcome.REFUSED, refusal_code=RefusalCode.PAYLOAD_TOO_LARGE.value
        )
    if pending_count >= limits.per_task or pending_bytes + incoming.content.size_bytes > limits.max_task_bytes:
        return AdmissionResult(
            AdmissionOutcome.REFUSED, refusal_code=RefusalCode.BACKLOG_FULL.value
        )
    return AdmissionResult(
        AdmissionOutcome.ACCEPTED,
        record=InputRecord(
            namespace=incoming.namespace,
            session_id=incoming.session_id,
            task_id=incoming.task_id,
            input_id=incoming.input_id,
            kind=incoming.content.kind,
            sequence=0,  # assigned by the store inside the transaction
            content_digest=incoming.content.digest,
            content=incoming.content,
            source=incoming.source,
        ),
    )


@dataclass(frozen=True)
class InputSelection:
    """Which inputs the runner may take in one poll, and why the rest wait."""

    taken: tuple[InputRecord, ...]
    waiting: tuple[InputRecord, ...]
    reason: str = ""


@dataclass(frozen=True)
class InputRead:
    """One bounded read of the inputs a poll site may still take.

    ``candidates`` are the records the read selected, in sequence order; ``revision`` is the inbox
    revision observed **in the same read**, so the caller can compare it against the revision the
    run later records. Reading is not consuming: a candidate stays ``pending``/``claimed`` until
    the transaction that integrates it commits (plan §7.2).
    """

    candidates: tuple[InputRecord, ...] = ()
    revision: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidates", tuple(self.candidates))


def select_inputs(
    records: Sequence[InputRecord],
    *,
    limit: int | None = None,
) -> InputSelection:
    """Pick the inputs one poll takes, in sequence order, honouring the per-kind order table.

    ``all`` kinds are taken whole (steering is read before the next request), ``one-at-a-time``
    kinds one per poll (a follow-up backlog becomes several turns), and control kinds are never
    taken as conversation. Selection is by sequence so the order inputs were accepted in is the
    order they are applied in.
    """
    claimable = [
        record
        for record in records
        if record.state in {InputState.PENDING, InputState.CLAIMED}
        and INPUT_ORDER[record.kind] != "control"
    ]
    claimable.sort(key=lambda record: record.sequence)
    if not claimable:
        return InputSelection((), (), "no claimable conversation input")
    first = claimable[0]
    order = INPUT_ORDER[first.kind]
    if order == "all":
        taken = [record for record in claimable if record.kind is first.kind]
    else:
        taken = [first]
    budget = len(taken) if limit is None else min(limit, len(taken))
    taken = taken[:budget]
    rest = [record for record in claimable if record not in taken]
    return InputSelection(tuple(taken), tuple(rest))


# --------------------------------------------------------------------------------------
# tasks, claims, children, budgets, outbox
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskAdmission:
    """A host's request to create a task and fix its run id.

    Repeated submission of the same ``task_id`` with the same content returns the same record: a
    host that retries a submit must not create a second task, a second run or a second budget.
    """

    namespace: str
    session_id: str
    task_id: str
    run_id: str
    initial_input: InputContent | None = None
    priority: int = 0
    root_task_id: str = ""
    parent_task_id: str = ""
    depth: int = 0
    profile: str = ""
    workspace_ref: str = ""
    created_at: float = 0.0
    #: The task's absolute deadline (wall clock), persisted as an instant rather than a duration:
    #: after a restart the remaining budget is computed *in this process* and converted to a
    #: monotonic timer. Subtracting monotonic readings across processes is meaningless, and a
    #: wall-clock difference is not a duration.
    deadline_at: float | None = None

    def __post_init__(self) -> None:
        for name in ("namespace", "session_id", "task_id", "run_id"):
            if not getattr(self, name):
                raise ContractError(f"a task admission requires a non-empty {name}")
        if self.depth < 0:
            raise ContractError("a task's depth cannot be negative")
        if self.depth > 0 and not self.parent_task_id:
            raise ContractError("a task below the root must name its parent")
        if self.parent_task_id and self.depth == 0:
            raise ContractError("a task with a parent cannot be at depth 0")
        if self.initial_input is not None and self.initial_input.kind is not InputKind.INITIAL:
            raise ContractError("the initial input's kind must be `initial`")

    @property
    def identity(self) -> Mapping[str, Any]:
        return {
            "namespace": self.namespace,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "root_task_id": self.root_task_id or self.task_id,
            "parent_task_id": self.parent_task_id,
            "depth": self.depth,
        }


@dataclass(frozen=True)
class TaskRecord:
    """One task, as the control store keeps it.

    Note what is **not** here: the run's status. The run is the authority on its own state, and a
    task that cached it would be a second source of truth that can disagree — the failure mode
    "the scheduler thinks the run finished" comes from exactly that. A reader that needs the run
    status reads the run.
    """

    namespace: str
    session_id: str
    task_id: str
    run_id: str
    status: TaskStatus = TaskStatus.QUEUED
    reason: str = REASON_ADMITTED
    stop_code: str = ""
    priority: int = 0
    root_task_id: str = ""
    parent_task_id: str = ""
    depth: int = 0
    profile: str = ""
    workspace_ref: str = ""
    attempt_count: int = 0
    current_attempt_id: str = ""
    cancel_requested_at: float | None = None
    waiting_for: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    #: The task's absolute deadline (see ``TaskAdmission.deadline_at``).
    deadline_at: float | None = None
    schema_version: int = TASK_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in ("namespace", "session_id", "task_id", "run_id"):
            if not getattr(self, name):
                raise ContractError(f"a task record requires a non-empty {name}")
        if self.depth < 0:
            raise ContractError("a task's depth cannot be negative")
        if self.root_task_id and self.root_task_id != self.task_id and not self.parent_task_id:
            raise ContractError("a non-root task must name its parent")
        if self.status.is_terminal and not self.reason:
            raise ContractError("a terminal task must say why it ended")
        if self.status is TaskStatus.WAITING and not self.waiting_for:
            raise ContractError("a waiting task must name the fact it waits for")
        if self.status is TaskStatus.CANCELLING and self.cancel_requested_at is None:
            raise ContractError("a cancelling task must record when cancellation was requested")
        if self.status is TaskStatus.RUNNING and not self.current_attempt_id:
            raise ContractError("a running task must name the attempt that holds the right")

    @property
    def is_terminal(self) -> bool:
        return self.status.is_terminal

    def remaining_seconds(self, now: float) -> float | None:
        """How long this task still has, or ``None`` when it has no deadline."""
        if self.deadline_at is None:
            return None
        return max(0.0, float(self.deadline_at) - float(now))

    def past_deadline(self, now: float) -> bool:
        remaining = self.remaining_seconds(now)
        return remaining is not None and remaining <= 0.0

    def transition(
        self,
        status: TaskStatus,
        *,
        reason: str,
        at: float,
        stop_code: str = "",
        waiting_for: str = "",
        attempt_id: str = "",
        cancel_requested_at: float | None = None,
    ) -> "TaskRecord":
        """A checked transition; the store persists the result inside its transaction."""
        assert_transition(self.status, status, reason)
        return replace(
            self,
            status=status,
            reason=reason,
            stop_code=stop_code or self.stop_code,
            waiting_for=waiting_for if status is TaskStatus.WAITING else "",
            # The attempt that held the right is kept on terminal records too: "which claim
            # finished this" is a fact a reader needs, not a field to clear on the way out.
            current_attempt_id=attempt_id or self.current_attempt_id,
            cancel_requested_at=(
                cancel_requested_at
                if cancel_requested_at is not None
                else self.cancel_requested_at
            ),
            updated_at=at,
        )

    def request_cancel(self, *, at: float) -> "TaskRecord":
        """Record a cancellation request. Idempotent: repeating it changes nothing."""
        if self.status.is_terminal:
            raise ContractError(
                f"task {self.task_id!r} is {self.status.value}; a cancellation of a finished task "
                "is not a transition, and the host should read the outcome instead"
            )
        if self.cancel_requested_at is None:
            return replace(self, cancel_requested_at=at, updated_at=at)
        return self

    def to_record(self) -> Mapping[str, Any]:
        return {
            "schema_version": self.schema_version,
            "namespace": self.namespace,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "status": self.status.value,
            "reason": self.reason,
            "stop_code": self.stop_code,
            "priority": self.priority,
            "root_task_id": self.root_task_id or self.task_id,
            "parent_task_id": self.parent_task_id,
            "depth": self.depth,
            "profile": self.profile,
            "workspace_ref": self.workspace_ref,
            "attempt_count": self.attempt_count,
            "current_attempt_id": self.current_attempt_id,
            "cancel_requested_at": self.cancel_requested_at,
            "waiting_for": self.waiting_for,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "deadline_at": self.deadline_at,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "TaskRecord":
        _require_version("task record", record.get("schema_version"))
        return cls(
            namespace=str(record.get("namespace") or ""),
            session_id=str(record.get("session_id") or ""),
            task_id=str(record.get("task_id") or ""),
            run_id=str(record.get("run_id") or ""),
            status=TaskStatus(str(record.get("status") or TaskStatus.QUEUED.value)),
            reason=str(record.get("reason") or ""),
            stop_code=str(record.get("stop_code") or ""),
            priority=int(record.get("priority") or 0),
            root_task_id=str(record.get("root_task_id") or ""),
            parent_task_id=str(record.get("parent_task_id") or ""),
            depth=int(record.get("depth") or 0),
            profile=str(record.get("profile") or ""),
            workspace_ref=str(record.get("workspace_ref") or ""),
            attempt_count=int(record.get("attempt_count") or 0),
            current_attempt_id=str(record.get("current_attempt_id") or ""),
            cancel_requested_at=record.get("cancel_requested_at"),
            waiting_for=str(record.get("waiting_for") or ""),
            created_at=float(record.get("created_at") or 0.0),
            updated_at=float(record.get("updated_at") or 0.0),
            deadline_at=record.get("deadline_at"),
            schema_version=int(record.get("schema_version") or TASK_SCHEMA_VERSION),
        )


@dataclass(frozen=True)
class DispatchClaim:
    """The scheduler's execution right over one task: bounded, fenced, renewable.

    The claim is *scheduling* fencing and does not replace the run's own lease. Both are held in a
    fixed order and the run's lease decides who may commit; a stale claim can never commit a run.
    """

    task_id: str
    holder: str
    attempt_id: str
    fencing_token: int
    claimed_at: float
    expires_at: float
    namespace: str = ""
    schema_version: int = TASK_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in ("task_id", "holder", "attempt_id"):
            if not getattr(self, name):
                raise ContractError(f"a dispatch claim requires a non-empty {name}")
        if self.fencing_token < 1:
            raise ContractError("a fencing token starts at 1 and only ever grows")
        if self.expires_at <= self.claimed_at:
            raise ContractError("a dispatch claim must expire after it was taken")

    def expired_at(self, now: float) -> bool:
        """Expiry is decided against a clock the *caller* reads; this record holds no clock."""
        return now >= self.expires_at

    def holds(self, *, holder: str, attempt_id: str) -> bool:
        return self.holder == holder and self.attempt_id == attempt_id

    def to_record(self) -> Mapping[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "namespace": self.namespace,
            "holder": self.holder,
            "attempt_id": self.attempt_id,
            "fencing_token": self.fencing_token,
            "claimed_at": self.claimed_at,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "DispatchClaim":
        _require_version("dispatch claim", record.get("schema_version"))
        return cls(
            task_id=str(record.get("task_id") or ""),
            holder=str(record.get("holder") or ""),
            attempt_id=str(record.get("attempt_id") or ""),
            fencing_token=int(record.get("fencing_token") or 0),
            claimed_at=float(record.get("claimed_at") or 0.0),
            expires_at=float(record.get("expires_at") or 0.0),
            namespace=str(record.get("namespace") or ""),
            schema_version=int(record.get("schema_version") or TASK_SCHEMA_VERSION),
        )


class ChildSpawnState(str, Enum):
    """How far a spawn got. ``REGISTERED`` is a control-store fact, not a tool receipt.

    ``COMPLETED`` / ``FAILED`` / ``CANCELLED`` describe how the *relation* ended — the child
    reached a terminal committed fact — and are never a claim about the spawn tool call.
    """

    REQUESTED = "requested"
    REGISTERED = "registered"
    REJECTED = "rejected"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


CHILD_SPAWN_STATES = tuple(item.value for item in ChildSpawnState)

#: The outbox kinds this build writes. Stable strings so a reader can route a notice without
#: interpreting free text, and so a redelivery is recognisable as the same kind of fact.
OUTBOX_CHILD_SPAWNED = "child_spawned"
OUTBOX_CHILD_COMPLETED = "child_completed"


class ChildOutcome(str, Enum):
    """How one child ended, as its own committed outcome — not the parent's interpretation.

    The four values mirror the terminal task statuses; keeping them a separate enum means a child
    result cannot be confused with a task record's status, and a reader that receives one cannot
    mistake it for the child's whole state.
    """

    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    USAGE_LIMITED = "usage_limited"

    @property
    def spawn_state(self) -> "ChildSpawnState":
        """Which relation state this outcome settles the spawn into."""
        if self is ChildOutcome.COMPLETED:
            return ChildSpawnState.COMPLETED
        if self is ChildOutcome.CANCELLED:
            return ChildSpawnState.CANCELLED
        return ChildSpawnState.FAILED


CHILD_OUTCOMES = tuple(item.value for item in ChildOutcome)


@dataclass(frozen=True)
class ChildRelation:
    """The durable parent-child relation, created once per stable spawn identity.

    The relation references the Gateway's ``operation_id`` for the spawn; it does not record
    whether the spawn *succeeded* — that is the receipt's fact. When the control store and the
    receipt store are different databases, a crash between them is settled by looking up this
    relation by spawn identity, which is why the identity has to be stable across a retry.
    """

    parent_task_id: str
    child_task_id: str
    spawn_identity: str
    namespace: str = ""
    session_id: str = ""
    state: ChildSpawnState = ChildSpawnState.REGISTERED
    operation_id: str = ""
    budget_reservation_id: str = ""
    authorization_digest: str = ""
    #: The full spawn semantics' digest, and the versioned spec it was taken from. An empty
    #: value marks a relation written before spawn semantics were recorded (legacy): a resubmit
    #: cannot be proven to describe the same child, so it is refused rather than re-used.
    spawn_digest: str = ""
    spawn_spec: Mapping[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    schema_version: int = TASK_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in ("parent_task_id", "child_task_id", "spawn_identity"):
            if not getattr(self, name):
                raise ContractError(f"a child relation requires a non-empty {name}")
        if self.parent_task_id == self.child_task_id:
            raise ContractError("a task cannot be its own child")
        object.__setattr__(self, "spawn_spec", dict(self.spawn_spec or {}))

    def to_record(self) -> Mapping[str, Any]:
        return {
            "schema_version": self.schema_version,
            "parent_task_id": self.parent_task_id,
            "child_task_id": self.child_task_id,
            "spawn_identity": self.spawn_identity,
            "namespace": self.namespace,
            "session_id": self.session_id,
            "state": self.state.value,
            "operation_id": self.operation_id,
            "budget_reservation_id": self.budget_reservation_id,
            "authorization_digest": self.authorization_digest,
            "spawn_digest": self.spawn_digest,
            "spawn_spec": dict(self.spawn_spec),
            "created_at": self.created_at,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "ChildRelation":
        _require_version("child relation", record.get("schema_version"))
        return cls(
            parent_task_id=str(record.get("parent_task_id") or ""),
            child_task_id=str(record.get("child_task_id") or ""),
            spawn_identity=str(record.get("spawn_identity") or ""),
            namespace=str(record.get("namespace") or ""),
            session_id=str(record.get("session_id") or ""),
            state=ChildSpawnState(str(record.get("state") or ChildSpawnState.REGISTERED.value)),
            operation_id=str(record.get("operation_id") or ""),
            budget_reservation_id=str(record.get("budget_reservation_id") or ""),
            authorization_digest=str(record.get("authorization_digest") or ""),
            spawn_digest=str(record.get("spawn_digest") or ""),
            spawn_spec=dict(record.get("spawn_spec") or {}),
            created_at=float(record.get("created_at") or 0.0),
            schema_version=int(record.get("schema_version") or TASK_SCHEMA_VERSION),
        )


@dataclass(frozen=True)
class ChildResult:
    """The committed outcome of one child, as an **untrusted fact** the parent may read.

    This is task data, not authority (plan §10.3): it enters the parent's context as a message and
    is never parsed for permissions, tool grants, budget or lifecycle decisions. ``accepted`` is
    the host verifier's verdict on the structured result — a schema-valid but business-invalid
    result must be visible here rather than silently trusted, so the parent can choose what to do
    about it without the control plane deciding for it.

    The result is reconstructed by re-reading the committed child record and its completion
    notice, never from an in-process future: a process that died between the child's commit and
    the parent's wake leaves the notice in the outbox, and a redelivery rebuilds the same value.
    """

    parent_task_id: str
    child_task_id: str
    spawn_identity: str
    outcome: ChildOutcome
    reason: str = ""
    result: Mapping[str, Any] = field(default_factory=dict)
    accepted: bool = True
    acceptance_detail: str = ""
    usage: Mapping[str, Any] = field(default_factory=dict)
    run_id: str = ""
    attempt_id: str = ""
    created_at: float = 0.0
    schema_version: int = TASK_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in ("parent_task_id", "child_task_id", "spawn_identity"):
            if not getattr(self, name):
                raise ContractError(f"a child result requires a non-empty {name}")
        if not self.accepted and not self.acceptance_detail:
            raise ContractError(
                "a rejected child result must say which acceptance check failed; a refusal "
                "without a reason cannot be acted on"
            )

    def to_record(self) -> Mapping[str, Any]:
        return {
            "schema_version": self.schema_version,
            "parent_task_id": self.parent_task_id,
            "child_task_id": self.child_task_id,
            "spawn_identity": self.spawn_identity,
            "outcome": self.outcome.value,
            "reason": self.reason,
            "result": {str(key): self.result[key] for key in sorted(self.result)},
            "accepted": self.accepted,
            "acceptance_detail": self.acceptance_detail,
            "usage": {str(key): self.usage[key] for key in sorted(self.usage)},
            "run_id": self.run_id,
            "attempt_id": self.attempt_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "ChildResult":
        _require_version("child result", record.get("schema_version"))
        return cls(
            parent_task_id=str(record.get("parent_task_id") or ""),
            child_task_id=str(record.get("child_task_id") or ""),
            spawn_identity=str(record.get("spawn_identity") or ""),
            outcome=ChildOutcome(str(record.get("outcome") or ChildOutcome.COMPLETED.value)),
            reason=str(record.get("reason") or ""),
            result=dict(record.get("result") or {}),
            accepted=bool(record.get("accepted", True)),
            acceptance_detail=str(record.get("acceptance_detail") or ""),
            usage=dict(record.get("usage") or {}),
            run_id=str(record.get("run_id") or ""),
            attempt_id=str(record.get("attempt_id") or ""),
            created_at=float(record.get("created_at") or 0.0),
            schema_version=int(record.get("schema_version") or TASK_SCHEMA_VERSION),
        )


class BudgetUnit(str, Enum):
    """What a reservation is denominated in. Kept explicit because they are not interchangeable."""

    TURNS = "turns"
    MODEL_REQUESTS = "model_requests"
    DISPATCHES = "dispatches"
    CONCURRENCY = "concurrency"
    DEPTH = "depth"
    #: Legacy wall-clock unit. New immutable policy snapshots use integer milliseconds.
    SECONDS = "seconds"
    DURATION_MILLISECONDS = "duration_milliseconds"
    #: Legacy direction-agnostic token unit. New policy snapshots use input/output separately.
    TOKENS = "tokens"
    INPUT_TOKENS = "input_tokens"
    OUTPUT_TOKENS = "output_tokens"
    COST_MICROS = "cost_micros"


class ReservationState(str, Enum):
    """A reservation's life. ``UNKNOWN`` is a real state, not a failure to decide.

    A request that has entered the dispatch window without a final usage figure keeps its
    reservation as ``UNKNOWN``: refunding it would make the budget bigger than it is, and
    settling it at zero would spend an allowance nobody can prove was not consumed (plan §9).
    """

    RESERVED = "reserved"
    SETTLED = "settled"
    RELEASED = "released"
    UNKNOWN = "unknown"


BudgetState = ReservationState


@dataclass(frozen=True)
class BudgetReservation:
    """One reservation against a root task's allowance.

    ``dispatch_identity`` is the idempotency key: the same stable dispatch identity can be
    reserved once, settled once, and replayed any number of times without changing the numbers.
    It is *not* the logical model action id alone — a re-issued request after a recovery can cost
    real money again, and two requests are two dispatches even when they answer the same action.
    """

    root_task_id: str
    task_id: str
    unit: BudgetUnit
    amount: float
    reservation_id: str
    dispatch_identity: str
    state: ReservationState = ReservationState.RESERVED
    settled_amount: float | None = None
    estimated: bool = False
    note: str = ""
    created_at: float = 0.0
    settled_at: float | None = None
    schema_version: int = TASK_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in ("root_task_id", "task_id", "reservation_id", "dispatch_identity"):
            if not getattr(self, name):
                raise ContractError(f"a budget reservation requires a non-empty {name}")
        if self.amount < 0:
            raise ContractError("a reservation cannot be negative")
        if self.state is ReservationState.SETTLED and self.settled_amount is None:
            raise ContractError("a settled reservation must state what it settled to")
        if self.state in {ReservationState.RESERVED, ReservationState.RELEASED, ReservationState.UNKNOWN} and self.settled_amount is not None:
            raise ContractError(
                f"a reservation in {self.state.value} cannot carry a settled amount"
            )
        if self.state is ReservationState.UNKNOWN and not self.note:
            raise ContractError("an unknown reservation must say what is unknown about it")

    def settle(self, amount: float, *, at: float, estimated: bool = False) -> "BudgetReservation":
        if self.state is ReservationState.SETTLED:
            raise ContractError(f"reservation {self.reservation_id!r} was already settled")
        if amount < 0:
            raise ContractError("a settlement cannot be negative")
        return replace(
            self,
            state=ReservationState.SETTLED,
            settled_amount=amount,
            estimated=estimated,
            settled_at=at,
        )

    def release(self, *, at: float, note: str = "") -> "BudgetReservation":
        """Give the allowance back — only for work that provably never started."""
        if self.state is ReservationState.SETTLED:
            raise ContractError(
                f"reservation {self.reservation_id!r} was settled; refunding it would make the "
                "available allowance larger than the root ever granted"
            )
        return replace(
            self, state=ReservationState.RELEASED, settled_amount=None, settled_at=at, note=note
        )

    def mark_unknown(self, *, note: str) -> "BudgetReservation":
        if self.state is ReservationState.SETTLED:
            raise ContractError("a settled reservation is not made unknown again")
        return replace(self, state=ReservationState.UNKNOWN, settled_amount=None, note=note)

    def to_record(self) -> Mapping[str, Any]:
        return {
            "schema_version": self.schema_version,
            "root_task_id": self.root_task_id,
            "task_id": self.task_id,
            "unit": self.unit.value,
            "amount": self.amount,
            "reservation_id": self.reservation_id,
            "dispatch_identity": self.dispatch_identity,
            "state": self.state.value,
            "settled_amount": self.settled_amount,
            "estimated": self.estimated,
            "note": self.note,
            "created_at": self.created_at,
            "settled_at": self.settled_at,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "BudgetReservation":
        _require_version("budget reservation", record.get("schema_version"))
        return cls(
            root_task_id=str(record.get("root_task_id") or ""),
            task_id=str(record.get("task_id") or ""),
            unit=BudgetUnit(str(record.get("unit") or BudgetUnit.MODEL_REQUESTS.value)),
            amount=float(record.get("amount") or 0.0),
            reservation_id=str(record.get("reservation_id") or ""),
            dispatch_identity=str(record.get("dispatch_identity") or ""),
            state=ReservationState(str(record.get("state") or ReservationState.RESERVED.value)),
            settled_amount=record.get("settled_amount"),
            estimated=bool(record.get("estimated")),
            note=str(record.get("note") or ""),
            created_at=float(record.get("created_at") or 0.0),
            settled_at=record.get("settled_at"),
            schema_version=int(record.get("schema_version") or TASK_SCHEMA_VERSION),
        )


@dataclass(frozen=True)
class ControlOutboxEntry:
    """A durable notification owed to a task (a child's completion, a decision).

    Written in the same transaction as the fact it announces, delivered later, and redelivered
    freely: the receiving input is deduplicated by its stable id, so a duplicate delivery is a
    duplicate admission rather than a second application. The outbox is not a queue of live
    futures — a process that dies leaves the entry, not a lost callback.
    """

    outbox_id: str
    kind: str
    target_task_id: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    namespace: str = ""
    created_at: float = 0.0
    delivered_at: float | None = None
    attempts: int = 0
    last_error: str = ""
    schema_version: int = TASK_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in ("outbox_id", "kind", "target_task_id"):
            if not getattr(self, name):
                raise ContractError(f"an outbox entry requires a non-empty {name}")

    @property
    def pending(self) -> bool:
        return self.delivered_at is None

    def delivered(self, *, at: float) -> "ControlOutboxEntry":
        return replace(self, delivered_at=at, attempts=self.attempts + 1, last_error="")

    def failed(self, *, error: str) -> "ControlOutboxEntry":
        return replace(self, attempts=self.attempts + 1, last_error=error)

    def to_record(self) -> Mapping[str, Any]:
        return {
            "schema_version": self.schema_version,
            "outbox_id": self.outbox_id,
            "kind": self.kind,
            "target_task_id": self.target_task_id,
            "namespace": self.namespace,
            "payload": {str(key): self.payload[key] for key in sorted(self.payload)},
            "created_at": self.created_at,
            "delivered_at": self.delivered_at,
            "attempts": self.attempts,
            "last_error": self.last_error,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "ControlOutboxEntry":
        _require_version("control outbox entry", record.get("schema_version"))
        return cls(
            outbox_id=str(record.get("outbox_id") or ""),
            kind=str(record.get("kind") or ""),
            target_task_id=str(record.get("target_task_id") or ""),
            payload=dict(record.get("payload") or {}),
            namespace=str(record.get("namespace") or ""),
            created_at=float(record.get("created_at") or 0.0),
            delivered_at=record.get("delivered_at"),
            attempts=int(record.get("attempts") or 0),
            last_error=str(record.get("last_error") or ""),
            schema_version=int(record.get("schema_version") or TASK_SCHEMA_VERSION),
        )


#: The coordination operations a persistent-task deployment must provide. A store that offers
#: only the single-run API is fine — as long as persistent tasks are not enabled on it, which
#: :func:`yanxu_harness.tasks.ports.require_coordination` refuses before anything is dispatched.
COORDINATION_METHODS: tuple[str, ...] = (
    "admit_task",
    "admit_input",
    "claim_task",
    "commit_run_transition_with_inputs",
    "reserve_budget",
    "settle_budget",
    "register_child",
)

#: The *read* half of the coordination port that the persistent input reader needs. It is kept
#: separate from :data:`COORDINATION_METHODS` because a store may be able to commit atomically and
#: still lack the bounded pending-input query; such a store is refused before admission rather
#: than allowed to fall back to scanning applied history, which is the defect RF1 removes.
COORDINATION_READ_METHODS: tuple[str, ...] = (
    "poll_pending_inputs",
)


def task_record_digest(record: TaskRecord) -> str:
    """A digest over the record's durable content, for a test that pins the shape."""
    return fingerprint(record.to_record())
