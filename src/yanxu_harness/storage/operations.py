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

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ..errors import ContractError
from ..tools.definition import ToolDefinition
from ..tools.execution import EffectState, ToolIntent, ToolReceipt
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

    def __init__(self, path: str | Path, *, timeout: float = 5.0) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            str(self.path), timeout=timeout, isolation_level=None, check_same_thread=False
        )
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.executescript(_SCHEMA)

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

    # -- the port -------------------------------------------------------

    async def begin(self, intent: ToolIntent) -> ToolReceipt | None:
        """Record the intent. Returns the existing receipt when there already is one.

        Replaying a settled operation must not run the handler again, so the
        receipt is returned instead of a second intent being created. A row that
        is still open is returned as ``None`` — the caller (the gateway) will run
        the handler, and the fault-window tests are precisely about the case where
        that row never gets settled.
        """
        with self._lock:
            existing = self._row(intent.operation_id)
            if existing is not None:
                return self._receipt_from_row(intent.operation_id, existing)
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute(
                    "INSERT INTO operations(operation_id, namespace, tool_name, tool_version, "
                    "args_hash, call_id, session_id, run_id, idempotency_key, state, intent_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                        EffectState.RUNNING.value,
                        intent.created_at,
                    ),
                )
            except sqlite3.IntegrityError:
                # A concurrent writer recorded the same intent first. That is not
                # an error: the intent exists exactly once, which is the point.
                self._connection.execute("ROLLBACK")
                return self._receipt_from_row(intent.operation_id, self._row(intent.operation_id))
            self._connection.execute("COMMIT")
            return None

    async def execute(
        self,
        intent: ToolIntent,
        definition: ToolDefinition,
        arguments: Any,
        context: Any,
    ) -> ToolReceipt:
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
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
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
                self._connection.execute("ROLLBACK")
                raise
            self._connection.execute("COMMIT")

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
            "SELECT operation_id, state, tool_name, args_hash, result, detail FROM operations "
            "ORDER BY intent_at"
        ).fetchall()
        return tuple(
            {
                "operation_id": row[0],
                "state": row[1],
                "tool_name": row[2],
                "args_hash": row[3],
                "result": None if row[4] is None else json.loads(row[4]),
                "detail": row[5],
            }
            for row in rows
        )

    def counts_by_state(self) -> Mapping[str, int]:
        rows = self._connection.execute(
            "SELECT state, COUNT(*) FROM operations GROUP BY state"
        ).fetchall()
        return {row[0]: int(row[1]) for row in rows}

    # -- internals ------------------------------------------------------

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
    for attribute in ("model_dump", "to_json"):
        method = getattr(value, attribute, None)
        if callable(method):
            try:
                return method(mode="json")
            except TypeError:
                return method()
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=repr)
    raise TypeError(f"result of type {type(value).__name__} is not storable")
