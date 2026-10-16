"""Read-only task diagnostics: explain a task from the control facts about it.

The run diagnostics answer "what did this run do". This module answers the orchestration
questions the plan's §11.1 names: *why* is a task queued or waiting, which worker holds the
scheduling claim, which inputs were admitted and how far they got, which children exist and how
they ended, what was reserved and settled, and which notifications are still owed.

It is a **reader**, built like one:

* the control store must already be open read-only (``read_only=True``); the inspector refuses a
  writable store rather than promising not to write through it. It never claims or renews a lease,
  never consumes or confirms an input, never delivers an outbox entry and never advances a
  parent/child fact (plan §11.1, D-01);
* **the identity is checked.** A task id is not permission: the record's namespace and session must
  match the identity the caller presented, or the read is refused (D-02);
* **the public output is a whitelist.** Raw input text, a child result's full content, an absolute
  workspace path and any credential-shaped value never appear: the report carries digests, sizes,
  enums and bounded references instead (plan §11.1, D-02).

Facts are read from the control *tables* the coordination store owns — the same committed rows a
worker and the coordinator act on — so a report explains the durable record rather than a second
projection that can drift from it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping

from ..errors import ConfigurationError, ContractError
from ..versioning import fingerprint
from .types import DIAGNOSTIC_SCHEMA_VERSION, ReadBoundary

__all__ = [
    "BudgetFact",
    "ChildFact",
    "ClaimFact",
    "InputFact",
    "OutboxFact",
    "RecoveryFact",
    "TaskDiagnosticReport",
    "TaskInspector",
    "task_report_to_json",
    "task_report_to_markdown",
]

TASK_DIAGNOSTIC_SCHEMA_VERSION = DIAGNOSTIC_SCHEMA_VERSION


def _digest(value: Any) -> str:
    """A digest of one value, ``sha256:…``. Used wherever the value itself is not published."""
    return fingerprint({"value": value})


def _tail(path: str) -> str:
    """The last path segment, never the absolute path: a report may name a workspace, not expose it."""
    cleaned = str(path or "").rstrip("/")
    if not cleaned:
        return ""
    return cleaned.rsplit("/", 1)[-1]


# --------------------------------------------------------------------------------------
# facts
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class InputFact:
    """One admitted input, as a fact — never its text."""

    input_id: str
    kind: str
    sequence: int
    state: str
    source: str
    content_digest: str
    size_bytes: int
    refusal_code: str = ""
    attempts: int = 0
    applied_run_revision: int | None = None
    applied_event_sequence: int | None = None
    claim_expires_at: float | None = None
    created_at: float = 0.0

    def to_record(self) -> Mapping[str, Any]:
        return {
            "input_id": self.input_id,
            "kind": self.kind,
            "sequence": self.sequence,
            "state": self.state,
            "source": self.source,
            "content_digest": self.content_digest,
            "size_bytes": self.size_bytes,
            "refusal_code": self.refusal_code,
            "attempts": self.attempts,
            "applied_run_revision": self.applied_run_revision,
            "applied_event_sequence": self.applied_event_sequence,
            "claim_expires_at": self.claim_expires_at,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class ClaimFact:
    """The scheduling claim as committed. ``live`` is decided against the reader's clock."""

    holder: str
    attempt_id: str
    fencing_token: int
    claimed_at: float
    expires_at: float
    live: bool

    def to_record(self) -> Mapping[str, Any]:
        return {
            "holder": self.holder,
            "attempt_id": self.attempt_id,
            "fencing_token": self.fencing_token,
            "claimed_at": self.claimed_at,
            "expires_at": self.expires_at,
            "live": self.live,
        }


@dataclass(frozen=True)
class ChildFact:
    """One child relation plus the child's committed outcome, with the result *digested*."""

    child_task_id: str
    spawn_identity: str
    state: str
    operation_id: str = ""
    budget_reservation_id: str = ""
    authorization_digest: str = ""
    created_at: float = 0.0
    child_status: str = ""
    child_outcome: str = ""
    child_accepted: bool | None = None
    child_acceptance_detail: str = ""
    child_result_digest: str = ""
    child_usage: Mapping[str, Any] = field(default_factory=dict)

    def to_record(self) -> Mapping[str, Any]:
        return {
            "child_task_id": self.child_task_id,
            "spawn_identity": self.spawn_identity,
            "state": self.state,
            "operation_id": self.operation_id,
            "budget_reservation_id": self.budget_reservation_id,
            "authorization_digest": self.authorization_digest,
            "created_at": self.created_at,
            "child_status": self.child_status,
            "child_outcome": self.child_outcome,
            "child_accepted": self.child_accepted,
            "child_acceptance_detail": self.child_acceptance_detail,
            "child_result_digest": self.child_result_digest,
            "child_usage": {str(key): self.child_usage[key] for key in sorted(self.child_usage)},
        }


@dataclass(frozen=True)
class BudgetFact:
    """One budget reservation, with its dispatch identity digested rather than published."""

    reservation_id: str
    unit: str
    amount: float
    state: str
    dispatch_identity_digest: str
    settled_amount: float | None = None
    estimated: bool = False
    note: str = ""
    created_at: float = 0.0
    settled_at: float | None = None

    def to_record(self) -> Mapping[str, Any]:
        return {
            "reservation_id": self.reservation_id,
            "unit": self.unit,
            "amount": self.amount,
            "state": self.state,
            "dispatch_identity_digest": self.dispatch_identity_digest,
            "settled_amount": self.settled_amount,
            "estimated": self.estimated,
            "note": self.note,
            "created_at": self.created_at,
            "settled_at": self.settled_at,
        }


@dataclass(frozen=True)
class OutboxFact:
    """One notification owed or delivered, with its payload digested and routed facts kept."""

    outbox_id: str
    kind: str
    target_task_id: str
    created_at: float
    delivered_at: float | None
    attempts: int
    last_error: str = ""
    payload_digest: str = ""
    child_task_id: str = ""
    child_outcome: str = ""

    @property
    def pending(self) -> bool:
        return self.delivered_at is None

    def to_record(self) -> Mapping[str, Any]:
        return {
            "outbox_id": self.outbox_id,
            "kind": self.kind,
            "target_task_id": self.target_task_id,
            "created_at": self.created_at,
            "delivered_at": self.delivered_at,
            "attempts": self.attempts,
            "last_error": self.last_error,
            "payload_digest": self.payload_digest,
            "child_task_id": self.child_task_id,
            "child_outcome": self.child_outcome,
        }


@dataclass(frozen=True)
class RecoveryFact:
    """Why a run is waiting, whether its awaited result is ready, and its budget baseline.

    The wait itself lives on the **run** record, which is why this is read from the run state the
    control store holds. ``result_ready`` is ``None`` when the run is not waiting on a child
    result, and is the structured readiness otherwise — never a placeholder string (plan §9.2).
    """

    run_status: str = ""
    wait_kind: str = ""
    wait_ref: str = ""
    waiting_child: str = ""
    result_ready: bool | None = None
    result_detail: str = ""
    max_turns: int | None = None
    turns_used: int = 0
    baseline_digest: str = ""
    snapshot_schema_version: int | None = None

    def to_record(self) -> Mapping[str, Any]:
        return {
            "run_status": self.run_status,
            "wait_kind": self.wait_kind,
            "wait_ref": self.wait_ref,
            "waiting_child": self.waiting_child,
            "result_ready": self.result_ready,
            "result_detail": self.result_detail,
            "max_turns": self.max_turns,
            "turns_used": self.turns_used,
            "baseline_digest": self.baseline_digest,
            "snapshot_schema_version": self.snapshot_schema_version,
        }


@dataclass(frozen=True)
class TaskDiagnosticReport:
    """One task, explained from its committed control facts."""

    namespace: str
    session_id: str
    task_id: str
    run_id: str
    status: str
    reason: str = ""
    stop_code: str = ""
    priority: int = 0
    root_task_id: str = ""
    parent_task_id: str = ""
    depth: int = 0
    profile: str = ""
    workspace_ref_digest: str = ""
    workspace_ref_tail: str = ""
    attempt_count: int = 0
    current_attempt_id: str = ""
    cancel_requested_at: float | None = None
    waiting_for: str = ""
    deadline_at: float | None = None
    created_at: float = 0.0
    updated_at: float = 0.0
    inputs: tuple[InputFact, ...] = ()
    inputs_truncated: bool = False
    claim: ClaimFact | None = None
    children: tuple[ChildFact, ...] = ()
    budget: tuple[BudgetFact, ...] = ()
    outbox: tuple[OutboxFact, ...] = ()
    notifications_pending: int = 0
    recovery: RecoveryFact | None = None
    unsupported: tuple[str, ...] = ()
    integrity: tuple[str, ...] = ()
    boundary: ReadBoundary = field(default_factory=ReadBoundary)
    schema_version: int = TASK_DIAGNOSTIC_SCHEMA_VERSION

    def to_record(self) -> Mapping[str, Any]:
        return {
            "schema_version": self.schema_version,
            "namespace": self.namespace,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "status": self.status,
            "reason": self.reason,
            "stop_code": self.stop_code,
            "priority": self.priority,
            "root_task_id": self.root_task_id,
            "parent_task_id": self.parent_task_id,
            "depth": self.depth,
            "profile": self.profile,
            "workspace_ref_digest": self.workspace_ref_digest,
            "workspace_ref_tail": self.workspace_ref_tail,
            "attempt_count": self.attempt_count,
            "current_attempt_id": self.current_attempt_id,
            "cancel_requested_at": self.cancel_requested_at,
            "waiting_for": self.waiting_for,
            "deadline_at": self.deadline_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "inputs": [item.to_record() for item in self.inputs],
            "inputs_truncated": self.inputs_truncated,
            "claim": None if self.claim is None else self.claim.to_record(),
            "children": [item.to_record() for item in self.children],
            "budget": [item.to_record() for item in self.budget],
            "outbox": [item.to_record() for item in self.outbox],
            "notifications_pending": self.notifications_pending,
            "recovery": None if self.recovery is None else self.recovery.to_record(),
            "unsupported": list(self.unsupported),
            "integrity": list(self.integrity),
            "boundary": self.boundary.to_record(),
        }


# --------------------------------------------------------------------------------------
# the inspector
# --------------------------------------------------------------------------------------


class TaskInspector:
    """Read one task's control facts through a store that is open read-only."""

    def __init__(
        self,
        *,
        control: Any,
        input_limit: int = 200,
        child_limit: int = 200,
        outbox_limit: int = 200,
        clock: Any = None,
    ) -> None:
        if control is None:
            raise ContractError("a task inspector needs a control store to read")
        if not getattr(control, "read_only", False):
            raise ConfigurationError(
                "the task inspector only reads: open the control store with read_only=True so "
                "'it did not write' is a property of the connection, not a promise"
            )
        for name in (input_limit, child_limit, outbox_limit):
            if not isinstance(name, int) or name <= 0:
                raise ContractError("task inspector limits must be positive integers")
        self._control = control
        self._input_limit = input_limit
        self._child_limit = child_limit
        self._outbox_limit = outbox_limit
        self._clock = clock

    async def inspect_task(
        self, task_id: str, *, namespace: str, session_id: str, now: float | None = None
    ) -> TaskDiagnosticReport:
        """Read the task, its inputs, claim, children, budget and notices — and nothing else."""
        import time as _time

        at = (_time.time() if self._clock is None else self._clock()) if now is None else now
        task = await self._control.read_task(task_id)
        if task is None:
            raise ContractError(f"no task {task_id!r} in this control store")
        if task.namespace != namespace:
            raise ContractError(
                f"task {task_id!r} belongs to namespace {task.namespace!r}, not {namespace!r}; "
                "refusing to read it under this identity"
            )
        if task.session_id != session_id:
            raise ContractError(
                f"task {task_id!r} belongs to session {task.session_id!r}, not {session_id!r}; "
                "refusing to read it under this identity"
            )
        unsupported: list[str] = []
        integrity: list[str] = []

        inputs, inputs_truncated = await self._read_inputs(task_id)
        claim = await self._read_claim(task_id, at)
        children = await self._read_children(task)
        budget = await self._read_budget(task, unsupported)
        outbox, pending = await self._read_outbox(task_id)
        recovery, run_terminal, run_status = await self._read_recovery(task, unsupported)

        if task.status.value == "running" and claim is None:
            integrity.append(
                "the task is running but no scheduling claim is recorded: the claim expired, was "
                "released, or the record is inconsistent"
            )
        if task.status.value == "running" and claim is not None and not claim.live:
            integrity.append(
                "the task is running under an expired claim; expiry means the scheduling right "
                "lapsed, never that the external work stopped"
            )
        if pending and task.status.value in {"completed", "failed", "cancelled", "usage_limited"}:
            integrity.append(
                f"the task is terminal but {pending} notification(s) are still pending; they are "
                "redelivered rather than dropped"
            )
        terminal = task.status.value in {
            "completed",
            "failed",
            "cancelled",
            "usage_limited",
        } or run_terminal
        leftover = [
            item.input_id
            for item in inputs
            if item.state in {"pending", "claimed"} and item.kind in {"steering", "follow_up"}
        ]
        if leftover and terminal:
            integrity.append(
                f"{len(leftover)} conversation input(s) are still unapplied on a terminal task "
                f"({leftover}); they are reported as leftovers and never applied automatically — "
                "only the host creates new work"
            )

        boundary = ReadBoundary(
            cursor=None,
            high_water=None,
            next_cursor=None,
            run_revision=None,
            scanned_pages=1,
            truncated=inputs_truncated,
            scan_limit_hit=False,
            exhausted=not inputs_truncated,
            non_atomic=False,
            store_schema=_store_schema(self._control),
        )
        return TaskDiagnosticReport(
            namespace=task.namespace,
            session_id=task.session_id,
            task_id=task.task_id,
            run_id=task.run_id,
            status=task.status.value,
            reason=task.reason,
            stop_code=task.stop_code,
            priority=task.priority,
            root_task_id=task.root_task_id or task.task_id,
            parent_task_id=task.parent_task_id,
            depth=task.depth,
            profile=task.profile,
            workspace_ref_digest=_digest(task.workspace_ref) if task.workspace_ref else "",
            workspace_ref_tail=_tail(task.workspace_ref),
            attempt_count=task.attempt_count,
            current_attempt_id=task.current_attempt_id,
            cancel_requested_at=task.cancel_requested_at,
            waiting_for=task.waiting_for,
            deadline_at=task.deadline_at,
            created_at=task.created_at,
            updated_at=task.updated_at,
            inputs=inputs,
            inputs_truncated=inputs_truncated,
            claim=claim,
            children=children,
            budget=budget,
            outbox=outbox,
            notifications_pending=pending,
            recovery=recovery,
            unsupported=tuple(unsupported),
            integrity=tuple(integrity),
            boundary=boundary,
        )

    # -- reading -----------------------------------------------------------------------

    async def _read_inputs(self, task_id: str) -> tuple[tuple[InputFact, ...], bool]:
        records = list(
            await self._control.read_inputs(task_id, limit=self._input_limit + 1)
        )
        truncated = len(records) > self._input_limit
        records = records[: self._input_limit]
        return (
            tuple(
                InputFact(
                    input_id=record.input_id,
                    kind=record.kind.value,
                    sequence=record.sequence,
                    state=record.state.value,
                    source=record.source,
                    content_digest=record.content_digest,
                    size_bytes=record.content.size_bytes,
                    refusal_code=record.refusal_code,
                    attempts=record.attempts,
                    applied_run_revision=record.applied_run_revision,
                    applied_event_sequence=record.applied_event_sequence,
                    claim_expires_at=record.claim_expires_at,
                    created_at=record.created_at,
                )
                for record in records
            ),
            truncated,
        )

    async def _read_claim(self, task_id: str, at: float) -> ClaimFact | None:
        reader = getattr(self._control, "read_claim", None)
        if not callable(reader):
            return None
        claim = await reader(task_id)
        if claim is None:
            return None
        return ClaimFact(
            holder=claim.holder,
            attempt_id=claim.attempt_id,
            fencing_token=claim.fencing_token,
            claimed_at=claim.claimed_at,
            expires_at=claim.expires_at,
            live=not claim.expired_at(at),
        )

    async def _read_children(self, task: Any) -> tuple[ChildFact, ...]:
        relations = list(await self._control.read_children(task.task_id))
        relations = relations[: self._child_limit]
        completed = {}
        for entry in await self._control.read_outbox(
            task_id=task.task_id, pending_only=False, limit=self._outbox_limit
        ):
            if entry.kind != "child_completed":
                continue
            child_task_id = str(entry.payload.get("child_task_id") or "")
            if child_task_id:
                completed[child_task_id] = entry.payload
        facts: list[ChildFact] = []
        for relation in relations:
            child = await self._control.read_task(relation.child_task_id)
            payload = completed.get(relation.child_task_id, {})
            result = dict(payload.get("result") or {})
            facts.append(
                ChildFact(
                    child_task_id=relation.child_task_id,
                    spawn_identity=relation.spawn_identity,
                    state=relation.state.value,
                    operation_id=relation.operation_id,
                    budget_reservation_id=relation.budget_reservation_id,
                    authorization_digest=relation.authorization_digest,
                    created_at=relation.created_at,
                    child_status="" if child is None else child.status.value,
                    child_outcome=str(payload.get("outcome") or ""),
                    child_accepted=payload.get("accepted"),
                    child_acceptance_detail=str(payload.get("acceptance_detail") or ""),
                    child_result_digest=_digest(result) if result else "",
                    child_usage=dict(payload.get("usage") or {}),
                )
            )
        return tuple(facts)

    async def _read_budget(self, task: Any, unsupported: list[str]) -> tuple[BudgetFact, ...]:
        root = task.root_task_id or task.task_id
        reader = getattr(self._control, "read_reservations", None)
        if not callable(reader):
            unsupported.append(
                "this control store cannot list budget reservations, so the report states none"
            )
            return ()
        rows = list(await reader(root))
        return tuple(
            BudgetFact(
                reservation_id=row.reservation_id,
                unit=row.unit.value,
                amount=row.amount,
                state=row.state.value,
                dispatch_identity_digest=_digest(row.dispatch_identity),
                settled_amount=row.settled_amount,
                estimated=row.estimated,
                note=row.note,
                created_at=row.created_at,
                settled_at=row.settled_at,
            )
            for row in rows
        )

    async def _read_outbox(self, task_id: str) -> tuple[tuple[OutboxFact, ...], int]:
        entries = list(
            await self._control.read_outbox(
                task_id=task_id, pending_only=False, limit=self._outbox_limit
            )
        )
        facts: list[OutboxFact] = []
        pending = 0
        for entry in entries:
            if entry.pending:
                pending += 1
            facts.append(
                OutboxFact(
                    outbox_id=entry.outbox_id,
                    kind=entry.kind,
                    target_task_id=entry.target_task_id,
                    created_at=entry.created_at,
                    delivered_at=entry.delivered_at,
                    attempts=entry.attempts,
                    last_error=entry.last_error,
                    payload_digest=_digest(dict(entry.payload)),
                    child_task_id=str(entry.payload.get("child_task_id") or ""),
                    child_outcome=str(entry.payload.get("outcome") or ""),
                )
            )
        return tuple(facts), pending

    async def _read_recovery(
        self, task: Any, unsupported: list[str]
    ) -> tuple[RecoveryFact | None, bool, str]:
        """Read the run's wait, its readiness and its budget baseline — without advancing it."""
        from ..core.execution import state_from_record

        loader = getattr(self._control, "load_run", None)
        if not callable(loader):
            unsupported.append(
                "this control store cannot read the run record, so the report cannot say why a "
                "wait exists or whether an awaited result is ready"
            )
            return None, False, ""
        loaded = await loader(task.run_id)
        if loaded is None:
            return None, False, ""
        state = getattr(loaded, "state", loaded) or {}
        run_status = str(state.get("status") or "")
        interaction = dict(state.get("pending_interaction") or {})
        execution = state.get("execution") or {}
        arrangement: Mapping[str, Any] | None = None
        snapshot_schema_version: int | None = None
        max_turns: int | None = None
        if execution:
            try:
                snapshot = state_from_record(execution)
            except Exception:  # noqa: BLE001 - an unreadable snapshot is reported, not guessed
                unsupported.append("the run's execution snapshot cannot be interpreted")
            else:
                arrangement = dict(snapshot.arrangement) if snapshot.arrangement else None
                snapshot_schema_version = int(execution.get("schema_version") or 0) or None
                max_turns = snapshot.max_turns
        result_ready: bool | None = None
        result_detail = ""
        wait_kind = str(interaction.get("kind") or "")
        if wait_kind == "child_result" and interaction.get("child_task_id"):
            from ..tasks.children import read_child_result

            readiness = await read_child_result(
                self._control,
                parent_task_id=task.task_id,
                child_task_id=str(interaction.get("child_task_id")),
                spawn_identity=str(interaction.get("spawn_identity") or ""),
                run_store=self._control,
            )
            result_ready = readiness.state == "ready"
            result_detail = readiness.detail or readiness.state
        fact = RecoveryFact(
            run_status=run_status,
            wait_kind=wait_kind,
            wait_ref=str(interaction.get("wait_ref") or ""),
            waiting_child=str(interaction.get("child_task_id") or ""),
            result_ready=result_ready,
            result_detail=result_detail,
            max_turns=max_turns,
            turns_used=int(state.get("turns_used") or 0),
            baseline_digest=_digest(arrangement) if arrangement else "",
            snapshot_schema_version=snapshot_schema_version,
        )
        return fact, run_status in {"completed", "failed", "cancelled", "usage_limited"}, run_status


def _store_schema(control: Any) -> Mapping[str, str]:
    versions: dict[str, str] = {}
    connection = getattr(control, "_connection", None)
    if connection is None:
        return versions
    try:
        row = connection.execute(
            "SELECT value FROM control_meta WHERE key = 'control_schema_version'"
        ).fetchone()
    except Exception:  # noqa: BLE001 - a store without that table simply states less
        return versions
    if row is not None:
        versions["control_schema_version"] = str(row[0])
    return versions


# --------------------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------------------


def task_report_to_json(report: TaskDiagnosticReport) -> str:
    return json.dumps(report.to_record(), indent=2, sort_keys=True, ensure_ascii=False)


def _markdown_row(values: Any) -> str:
    return "| " + " | ".join("" if item is None else str(item) for item in values) + " |"


def task_report_to_markdown(report: TaskDiagnosticReport) -> str:
    """A view of the same facts, with the same redaction rules — no raw text anywhere."""
    lines = [
        f"# Task {report.task_id}",
        "",
        f"- status: `{report.status}` ({report.reason or 'no reason recorded'})",
        f"- run: `{report.run_id}`; namespace `{report.namespace}`, session `{report.session_id}`",
        f"- root: `{report.root_task_id}`; parent: `{report.parent_task_id or '—'}`; depth {report.depth}",
        f"- attempts: {report.attempt_count}; current attempt: `{report.current_attempt_id or '—'}`",
        f"- waiting for: `{report.waiting_for or '—'}`",
        f"- workspace: {report.workspace_ref_tail or '—'} (`{report.workspace_ref_digest or 'none'}`)",
        "",
    ]
    if report.claim is None:
        lines.append("## Scheduling claim\n\nnone recorded.")
    else:
        live = "live" if report.claim.live else "expired"
        lines.append(
            "## Scheduling claim\n\n"
            f"holder `{report.claim.holder}`, attempt `{report.claim.attempt_id}`, "
            f"fence {report.claim.fencing_token}, {live} (expires {report.claim.expires_at})."
        )
    lines += ["", "## Inputs", "", "| seq | input | kind | state | digest | applied rev |", "| --- | --- | --- | --- | --- | --- |"]
    for item in report.inputs:
        lines.append(
            _markdown_row(
                [
                    item.sequence,
                    f"`{item.input_id}`",
                    item.kind,
                    item.state,
                    item.content_digest[:19] + "…",
                    item.applied_run_revision if item.applied_run_revision is not None else "—",
                ]
            )
        )
    if not report.inputs:
        lines.append("| — | — | — | — | — | — |")
    lines += ["", "## Children", "", "| child | spawn | relation | child status | outcome | accepted |", "| --- | --- | --- | --- | --- | --- |"]
    for item in report.children:
        lines.append(
            _markdown_row(
                [
                    f"`{item.child_task_id}`",
                    f"`{item.spawn_identity[:24]}…`",
                    item.state,
                    item.child_status or "—",
                    item.child_outcome or "—",
                    "—" if item.child_accepted is None else item.child_accepted,
                ]
            )
        )
    if not report.children:
        lines.append("| — | — | — | — | — | — |")
    lines += ["", "## Budget", "", "| reservation | unit | amount | state | settled |", "| --- | --- | --- | --- | --- |"]
    for item in report.budget:
        lines.append(
            _markdown_row(
                [
                    f"`{item.reservation_id}`",
                    item.unit,
                    item.amount,
                    item.state,
                    "—" if item.settled_amount is None else item.settled_amount,
                ]
            )
        )
    if not report.budget:
        lines.append("| — | — | — | — | — |")
    lines += [
        "",
        f"## Notifications ({report.notifications_pending} pending)",
        "",
        "| notice | kind | delivered | attempts | payload |",
        "| --- | --- | --- | --- | --- |",
    ]
    for item in report.outbox:
        lines.append(
            _markdown_row(
                [
                    f"`{item.outbox_id}`",
                    item.kind,
                    "no" if item.pending else "yes",
                    item.attempts,
                    item.payload_digest[:19] + "…",
                ]
            )
        )
    if not report.outbox:
        lines.append("| — | — | — | — | — |")
    if report.unsupported or report.integrity:
        lines += ["", "## What the report cannot say", ""]
        for note in report.unsupported:
            lines.append(f"- unsupported: {note}")
        for note in report.integrity:
            lines.append(f"- integrity: {note}")
    if report.recovery is not None:
        recovery = report.recovery
        ready = (
            "—"
            if recovery.result_ready is None
            else ("ready" if recovery.result_ready else "not ready")
        )
        lines += [
            "",
            "## Recovery",
            "",
            f"- run status: `{recovery.run_status or '—'}`",
            f"- waiting on: `{recovery.wait_kind or '—'}` (`{recovery.waiting_child or '—'}`)",
            f"- awaited result: {ready}"
            + (f" ({recovery.result_detail})" if recovery.result_detail else ""),
            f"- turns: {recovery.turns_used}/{recovery.max_turns if recovery.max_turns is not None else 'unbounded'}",
            f"- arrangement baseline: `{recovery.baseline_digest or 'none'}`",
            f"- snapshot schema: {recovery.snapshot_schema_version or '—'}",
        ]
    return "\n".join(lines) + "\n"
