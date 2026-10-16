"""The durable execution backend: intent, execute, receipt — on disk.

``yanxu_harness.testing.RecordingExecutionBackend`` is a test double; this is the
backend a restarted process can actually trust, and it is what the fault-window
tests exercise: a child process is killed *between* phases and the parent then
re-opens the same file and asks what happened.

The state machine it records is deliberately coarse, because the honest answer
after a crash is often "we do not know":

```text
begin(intent)   → row (operation_id, args_hash, RUNNING, no receipt)
execute         → handler runs
settle(receipt) → row updated with the receipt
```

A row left in ``RUNNING`` with no receipt means the process died between the
intent and the settlement. Whether the external effect happened is *unprovable*
from this file alone, so :meth:`reconcile_targets` returns those rows for the
host to resolve with business evidence. Nothing here decides on its own that an
unsettled operation failed — that guess is exactly how a double write happens.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ..errors import ContractError
from ..tools.definition import ToolDefinition
from ..tools.execution import (
    AdmissionDecision,
    AdmissionResult,
    EffectState,
    ObservedOperation,
    OperationObservation,
    ToolIntent,
    ToolReceipt,
)
from .ports import Durability

__all__ = ["SqliteExecutionBackend", "UnsettledOperation"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS operations (
    operation_id TEXT PRIMARY KEY,
    namespace TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    tool_version TEXT NOT NULL,
    args_hash TEXT NOT NULL,
    call_id TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    run_id TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT,
    model_action_id TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL,
    result TEXT,
    detail TEXT NOT NULL DEFAULT '',
    details TEXT NOT NULL DEFAULT '{}',
    external_id TEXT,
    intent_at REAL NOT NULL,
    settled_at REAL
);
CREATE INDEX IF NOT EXISTS operations_run ON operations(run_id);
CREATE INDEX IF NOT EXISTS operations_state ON operations(state);
"""

#: The states a row can hold *before* a receipt settles it.
_OPEN_STATES = (EffectState.QUEUED.value, EffectState.RUNNING.value)

#: Columns the recovery binding check compares. Each one is part of the logical call
#: identity, so a row whose value differs from the intent describes a *different* call.
_BINDING_COLUMNS = (
    "namespace",
    "tool_name",
    "tool_version",
    "args_hash",
    "call_id",
    "session_id",
    "run_id",
    "model_action_id",
)


@dataclass(frozen=True)
class UnsettledOperation:
    """An intent with no receipt: the process died somewhere in the middle."""

    operation_id: str
    namespace: str
    tool_name: str
    tool_version: str
    args_hash: str
    state: str
    run_id: str
    intent_at: float

    @property
    def provable(self) -> bool:
        """Always ``False``: an unsettled intent proves nothing about the effect."""
        return False


class SqliteExecutionBackend:
    """A durable :class:`~yanxu_harness.tools.execution.ToolExecutionBackend`."""

    def __init__(
        self, path: str | Path, *, timeout: float = 5.0, read_only: bool = False
    ) -> None:
        """Open the ledger. ``read_only=True`` never creates, migrates or writes.

        The read-only form exists for reports: an operation's state is authoritative, and a
        reader that could migrate the table or promote a row would be able to change the
        evidence it is describing. It opens the file with ``mode=ro`` and refuses every write
        path below.
        """
        self.path = Path(path)
        self.read_only = bool(read_only)
        self._lock = threading.RLock()
        if self.read_only:
            if str(self.path) == ":memory:" or not self.path.exists():
                raise ContractError(
                    f"no operation ledger at {self.path}; a reader does not create one"
                )
            self._connection = sqlite3.connect(
                f"file:{self.path}?mode=ro",
                uri=True,
                timeout=timeout,
                isolation_level=None,
                check_same_thread=False,
            )
            self._require_tables()
            return
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            str(self.path), timeout=timeout, isolation_level=None, check_same_thread=False
        )
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.executescript(_SCHEMA)
        self._migrate()

    def _require_tables(self) -> None:
        """Refuse a file that is not a ledger this build can read."""
        rows = self._connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        names = {str(row[0]) for row in rows}
        missing = {"operations"} - names
        if missing:
            raise ContractError(
                f"{self.path} is not an operation ledger this build can read "
                f"(missing: {', '.join(sorted(missing))})"
            )
        columns = {
            row[1] for row in self._connection.execute("PRAGMA table_info(operations)").fetchall()
        }
        for required in ("operation_id", "state", "namespace", "run_id", "call_id"):
            if required not in columns:
                raise ContractError(
                    f"{self.path}: the operations table has no {required!r} column, so this "
                    "build cannot tell one operation from another"
                )

    def _migrate(self) -> None:
        """Add columns a released build did not write. Idempotent and transactional.

        An older database keeps its rows: the new column is added with a default that
        means "this build did not record it", which the binding check treats as
        unprovable rather than as a mismatch.
        """
        columns = {
            row[1] for row in self._connection.execute("PRAGMA table_info(operations)").fetchall()
        }
        if "model_action_id" not in columns:
            self._connection.execute(
                "ALTER TABLE operations ADD COLUMN model_action_id TEXT NOT NULL DEFAULT ''"
            )

    # -- declaration ----------------------------------------------------

    @property
    def ownership(self) -> str:
        """This backend owns the ledger itself; it is not delegating to a host."""
        return "harness"

    @property
    def durable(self) -> bool:
        return True

    @property
    def durability(self) -> Durability:
        return Durability.DURABLE

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "SqliteExecutionBackend":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _refuse_write(self) -> None:
        if self.read_only:
            raise ContractError(
                f"{self.path} was opened read-only: a report must not be able to change the "
                "evidence it describes"
            )

    # -- the port -------------------------------------------------------

    async def begin(self, intent: ToolIntent) -> ToolReceipt | None:
        """Record the intent. Returns the existing receipt when there already is one.

        Replaying a settled operation must not run the handler again, so the
        receipt is returned instead of a second intent being created. A row that
        is still open is returned as ``None`` — the caller (the gateway) will run
        the handler, and the fault-window tests are precisely about the case where
        that row never gets settled.
        """
        self._refuse_write()
        with self._lock:
            existing = self._row(intent.operation_id)
            if existing is not None:
                return self._receipt_from_row(intent.operation_id, existing)
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._insert_intent(intent)
            except sqlite3.IntegrityError:
                # A concurrent writer recorded the same intent first. That is not
                # an error: the intent exists exactly once, which is the point.
                self._connection.execute("ROLLBACK")
                return self._receipt_from_row(intent.operation_id, self._row(intent.operation_id))
            self._connection.execute("COMMIT")
            return None

    def _insert_intent(self, intent: ToolIntent) -> None:
        self._connection.execute(
            "INSERT INTO operations(operation_id, namespace, tool_name, tool_version, "
            "args_hash, call_id, session_id, run_id, idempotency_key, model_action_id, "
            "state, intent_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                intent.operation_id,
                intent.namespace,
                intent.tool_name,
                intent.tool_version,
                intent.args_hash,
                intent.call_id,
                intent.session_id,
                intent.run_id,
                intent.idempotency_key,
                intent.model_action_id,
                EffectState.RUNNING.value,
                intent.created_at,
            ),
        )

    # -- the recovery capability ----------------------------------------

    async def observe(self, intent: ToolIntent) -> ObservedOperation:
        """Read one operation's authoritative state, in a single consistent read.

        One row carries both the intent metadata and the receipt, so a single ``SELECT``
        is a consistent answer — there is no "read the table, then guess which row is the
        operation" step, and ``receipt() is None`` is never used as proof that no intent
        exists.
        """
        with self._lock:
            row = self._row_with_binding(intent.operation_id)
        if row is None:
            return ObservedOperation(
                observation=OperationObservation.ABSENT,
                operation_id=intent.operation_id,
                detail="the ledger holds no intent for this binding",
            )
        conflict = self._binding_conflict(intent, row)
        if conflict is not None:
            # Conservative: a disagreement about *what this operation is* is not a
            # licence to run it, and it is not proof that nothing happened either.
            return ObservedOperation(
                observation=OperationObservation.UNAVAILABLE,
                operation_id=intent.operation_id,
                detail=conflict,
            )
        metadata = self._intent_metadata(row)
        if row[0] in _OPEN_STATES:
            return ObservedOperation(
                observation=OperationObservation.OPEN,
                operation_id=intent.operation_id,
                intent=metadata,
                detail="the ledger holds an intent with no receipt, so the effect is unproven",
            )
        receipt = self._receipt_from_row(intent.operation_id, row[:6])
        if receipt is None:
            return ObservedOperation(
                observation=OperationObservation.OPEN,
                operation_id=intent.operation_id,
                intent=metadata,
                detail="the row is not in a state that can prove the effect",
            )
        return ObservedOperation(
            observation=OperationObservation.RECEIPT,
            operation_id=intent.operation_id,
            receipt=receipt,
            intent=metadata,
        )

    async def admit(self, intent: ToolIntent) -> AdmissionResult:
        """Record-or-read the intent and decide, inside one transaction.

        The check and the insert share a transaction (``BEGIN IMMEDIATE``), so two
        processes racing for the same operation cannot both be told "you may run": one
        inserts, the other reads the row and is refused. A process-local lock would not do
        — the race this exists to prevent is across processes.
        """
        self._refuse_write()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._row_with_binding(intent.operation_id)
                if row is None:
                    self._insert_intent(intent)
                    self._connection.execute("COMMIT")
                    return AdmissionResult(
                        decision=AdmissionDecision.ADMITTED,
                        operation_id=intent.operation_id,
                        detail="this call obtained the right to run",
                    )
                conflict = self._binding_conflict(intent, row)
                if conflict is not None:
                    self._connection.execute("COMMIT")
                    return AdmissionResult(
                        decision=AdmissionDecision.BINDING_CONFLICT,
                        operation_id=intent.operation_id,
                        detail=conflict,
                    )
                if row[0] in _OPEN_STATES:
                    self._connection.execute("COMMIT")
                    return AdmissionResult(
                        decision=AdmissionDecision.EXISTING_OPEN,
                        operation_id=intent.operation_id,
                        detail="the ledger holds an intent with no receipt, so the effect is unproven",
                    )
                receipt = self._receipt_from_row(intent.operation_id, row[:6])
                self._connection.execute("COMMIT")
                if receipt is None:
                    return AdmissionResult(
                        decision=AdmissionDecision.EXISTING_OPEN,
                        operation_id=intent.operation_id,
                        detail="the row is not in a state that can prove the effect",
                    )
                return AdmissionResult(
                    decision=AdmissionDecision.EXISTING_RECEIPT,
                    operation_id=intent.operation_id,
                    receipt=receipt,
                )
            except BaseException:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

    async def execute(
        self,
        intent: ToolIntent,
        definition: ToolDefinition,
        arguments: Any,
        context: Any,
    ) -> ToolReceipt:
        self._refuse_write()
        from ..tools.execution import classify_failure

        try:
            result = await definition.call(context, arguments)
        except BaseException as exc:  # noqa: BLE001 - classifying it is the point
            state, detail = classify_failure(exc)
            return ToolReceipt(
                operation_id=intent.operation_id,
                state=state,
                detail=detail,
                details=dict(getattr(exc, "details", {}) or {}),
            )
        return ToolReceipt(operation_id=intent.operation_id, state=EffectState.SUCCEEDED, result=result)

    async def settle(self, receipt: ToolReceipt) -> None:
        """Persist one receipt. Idempotent for the same fact, refusing to overwrite another.

        Re-submitting the same receipt is harmless (a retried settle after a crash is not
        a new fact). Overwriting a settled row with a different one is refused: a late
        ``unknown`` must not erase a confirmed success, and a routine settlement must not
        silently rewrite history. Promotion out of ``unknown`` needs business evidence and
        goes through :meth:`promote`.
        """
        self._refuse_write()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                current = self._connection.execute(
                    "SELECT state, result, detail, details FROM operations WHERE operation_id = ?",
                    (receipt.operation_id,),
                ).fetchone()
                if current is None:
                    raise ContractError(
                        f"{receipt.operation_id}: cannot settle an operation with no recorded intent"
                    )
                if current[0] not in _OPEN_STATES:
                    if self._same_receipt(current, receipt):
                        self._connection.execute("COMMIT")
                        return
                    raise ContractError(
                        f"{receipt.operation_id}: the operation is already settled as "
                        f"{current[0]!r}; refusing to overwrite it with {receipt.state.value!r} "
                        "(promotion out of `unknown` needs evidence and goes through promote)"
                    )
                cursor = self._connection.execute(
                    "UPDATE operations SET state = ?, result = ?, detail = ?, details = ?, "
                    "external_id = ?, settled_at = ? WHERE operation_id = ?",
                    (
                        receipt.state.value,
                        None if receipt.result is None else json.dumps(
                            receipt.result, ensure_ascii=False, sort_keys=True, default=_jsonable
                        ),
                        receipt.detail,
                        json.dumps(dict(receipt.details), ensure_ascii=False, sort_keys=True, default=_jsonable),
                        receipt.external_id,
                        receipt.at,
                        receipt.operation_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ContractError(
                        f"{receipt.operation_id}: cannot settle an operation with no recorded intent"
                    )
            except BaseException:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise
            self._connection.execute("COMMIT")

    async def promote(self, receipt: ToolReceipt, *, evidence: Mapping[str, Any]) -> ToolReceipt:
        """Promote an ``unknown`` operation to a *confirmed* state, with evidence.

        Deliberately narrow, because this is the one write that turns "we cannot prove it"
        into "we proved it":

        * only an *unproven* row may be promoted — an open intent (the process died
          mid-handler) or an ``unknown`` receipt. A confirmed state is never rewritten;
        * only ``succeeded`` or ``failed`` may be the new state, and the evidence must name
          a source;
        * the row's identity binding is re-checked, so evidence for another call cannot be
          applied here;
        * the evidence reference is stored with the receipt, so a reader can see *why* the
          state moved.
        """
        self._refuse_write()
        if receipt.state not in {EffectState.SUCCEEDED, EffectState.FAILED}:
            raise ContractError(
                f"{receipt.operation_id}: only succeeded or failed may be promoted, "
                f"got {receipt.state.value!r}"
            )
        source = str(dict(evidence).get("source") or "")
        if not source:
            raise ContractError(
                f"{receipt.operation_id}: promotion needs evidence that names its source"
            )
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._row_with_binding(receipt.operation_id)
                if row is None:
                    raise ContractError(
                        f"{receipt.operation_id}: cannot promote an operation with no recorded intent"
                    )
                current = row[0]
                promotable = {*_OPEN_STATES, EffectState.UNKNOWN.value}
                if current not in promotable:
                    raise ContractError(
                        f"{receipt.operation_id}: only an unproven operation may be promoted; "
                        f"this one is {current!r}"
                    )
                details = dict(receipt.details)
                details["reconciliation"] = dict(evidence)
                cursor = self._connection.execute(
                    "UPDATE operations SET state = ?, result = ?, detail = ?, details = ?, "
                    "external_id = ?, settled_at = ? WHERE operation_id = ?",
                    (
                        receipt.state.value,
                        None if receipt.result is None else json.dumps(
                            receipt.result, ensure_ascii=False, sort_keys=True, default=_jsonable
                        ),
                        receipt.detail,
                        json.dumps(details, ensure_ascii=False, sort_keys=True, default=_jsonable),
                        receipt.external_id,
                        receipt.at,
                        receipt.operation_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ContractError(
                        f"{receipt.operation_id}: the promotion did not update exactly one row"
                    )
            except BaseException:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise
            self._connection.execute("COMMIT")
        return dataclasses.replace(
            receipt, details={**dict(receipt.details), "reconciliation": dict(evidence)}
        )

    # -- reads ----------------------------------------------------------

    def receipt(self, operation_id: str) -> ToolReceipt | None:
        """Read one recorded receipt (``None`` when the row is still open)."""
        row = self._row(operation_id)
        return None if row is None else self._receipt_from_row(operation_id, row)

    def unsettled(self, *, run_id: str | None = None) -> tuple[UnsettledOperation, ...]:
        """Return intents with no receipt, for a host to reconcile with evidence."""
        placeholders = ", ".join("?" for _ in _OPEN_STATES)
        sql = (
            "SELECT operation_id, namespace, tool_name, tool_version, args_hash, state, run_id, intent_at "
            f"FROM operations WHERE state IN ({placeholders})"
        )
        params: list[Any] = list(_OPEN_STATES)
        if run_id is not None:
            sql += " AND run_id = ?"
            params.append(run_id)
        sql += " ORDER BY intent_at"
        rows = self._connection.execute(sql, params).fetchall()
        return tuple(UnsettledOperation(*row) for row in rows)

    def all_operations(self) -> tuple[Mapping[str, Any], ...]:
        rows = self._connection.execute(
            "SELECT operation_id, state, tool_name, args_hash, result, detail, namespace, "
            "run_id, call_id, model_action_id FROM operations ORDER BY intent_at"
        ).fetchall()
        return tuple(
            {
                "operation_id": row[0],
                "state": row[1],
                "tool_name": row[2],
                "args_hash": row[3],
                "result": None if row[4] is None else json.loads(row[4]),
                "detail": row[5],
                # The identity, so a reader can scope a report to one run and one tenant
                # instead of guessing from an operation id's shape.
                "namespace": row[6],
                "run_id": row[7],
                "call_id": row[8],
                "model_action_id": row[9],
            }
            for row in rows
        )

    def counts_by_state(self) -> Mapping[str, int]:
        rows = self._connection.execute(
            "SELECT state, COUNT(*) FROM operations GROUP BY state"
        ).fetchall()
        return {row[0]: int(row[1]) for row in rows}

    # -- internals ------------------------------------------------------

    def _row_with_binding(self, operation_id: str) -> tuple | None:
        """State, receipt fields and binding columns, in one read.

        Column order is part of this method's contract: the first six values are what
        :meth:`_receipt_from_row` expects, the rest are the binding columns in
        :data:`_BINDING_COLUMNS` order.
        """
        return self._connection.execute(
            "SELECT state, result, detail, details, external_id, settled_at, "
            + ", ".join(_BINDING_COLUMNS)
            + " FROM operations WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()

    def _binding_conflict(self, intent: ToolIntent, row: tuple) -> str | None:
        """Name the first binding field the stored row disagrees about, if any.

        A stored value that this build never recorded (empty) is not a disagreement — it
        is an unprovable field, and refusing on it would make every pre-migration row
        unusable. A disagreement on a recorded value means the operation id identifies a
        different call.
        """
        stored = dict(zip(_BINDING_COLUMNS, row[6:]))
        for column in _BINDING_COLUMNS:
            want = str(getattr(intent, column) or "")
            have = str(stored.get(column) or "")
            if not want or not have:
                continue
            if want != have:
                return f"{column} differs (ledger {have!r}, call {want!r})"
        return None

    def _intent_metadata(self, row: tuple) -> Mapping[str, Any]:
        stored = dict(zip(_BINDING_COLUMNS, row[6:]))
        return {
            "namespace": stored.get("namespace") or "",
            "tool_name": stored.get("tool_name") or "",
            "tool_version": stored.get("tool_version") or "",
            "args_hash": stored.get("args_hash") or "",
            "call_id": stored.get("call_id") or "",
            "session_id": stored.get("session_id") or "",
            "run_id": stored.get("run_id") or "",
            "model_action_id": stored.get("model_action_id") or "",
        }

    def _same_receipt(self, row: tuple, receipt: ToolReceipt) -> bool:
        """Whether a stored row already records exactly this receipt."""
        state, result, detail, details = row[0], row[1], row[2], row[3]
        if state != receipt.state.value or (detail or "") != (receipt.detail or ""):
            return False
        stored_result = None if result is None else json.loads(result)
        if stored_result != receipt.result:
            return False
        stored_details = json.loads(details or "{}")
        return stored_details == json.loads(
            json.dumps(dict(receipt.details), ensure_ascii=False, sort_keys=True, default=_jsonable)
        )

    def _row(self, operation_id: str) -> tuple | None:
        return self._connection.execute(
            "SELECT state, result, detail, details, external_id, settled_at FROM operations "
            "WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()

    def _receipt_from_row(self, operation_id: str, row: tuple | None) -> ToolReceipt | None:
        if row is None:
            return None
        state, result, detail, details, external_id, settled_at = row
        if state in _OPEN_STATES:
            return None
        return ToolReceipt(
            operation_id=operation_id,
            state=EffectState(state),
            result=None if result is None else json.loads(result),
            detail=detail,
            details=json.loads(details or "{}"),
            external_id=external_id,
            at=settled_at or time.time(),
        )


def _jsonable(value: Any) -> Any:
    """Make one result value storable, or refuse it explicitly.

    Dataclasses matter here: a tool handler legitimately returns core content
    blocks (``TextContent`` and friends), and a ledger that could not store them
    would turn "the write succeeded" into a settle-time crash. Their field names
    are the same ones :func:`yanxu_harness.core.types.block_to_json` writes, so
    reading them back with ``block_from_json`` restores the same object.
    """
    for attribute in ("model_dump", "to_json"):
        method = getattr(value, attribute, None)
        if callable(method):
            try:
                return method(mode="json")
            except TypeError:
                return method()
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=repr)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {field.name: getattr(value, field.name) for field in dataclasses.fields(value)}
    raise TypeError(f"result of type {type(value).__name__} is not storable")
