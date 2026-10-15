"""A durable approval ledger.

The approval semantics are the ones in :mod:`yanxu_harness.tools.policy`, and the
reason this exists as a *durable* store is that an approval is a fact about an
author's decision: losing it on restart means the author is asked the same
question again after they already answered it, and a denied call would look
undecided.

Two tables:

``approvals``
    the live record per call key — the thing the gateway reads,
``approval_history``
    superseded records. When a call's bound values move (or its question
    expires), the previous decision is *kept*, not overwritten: "the author
    approved a different version earlier" is exactly the audit fact a rewrite
    would destroy.

`decide` is idempotent for the same status and refuses to overwrite a decided
record with a different status, which is what makes concurrent approve/deny
submissions safe rather than last-writer-wins.
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
from ..tools.policy import (
    POLICY_VERSION,
    Approval,
    ApprovalBinding,
    ApprovalStatus,
)

__all__ = ["SqliteApprovalStore"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS approvals (
    key TEXT PRIMARY KEY,
    namespace TEXT NOT NULL,
    call_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    tool_version TEXT NOT NULL,
    args_hash TEXT NOT NULL,
    target_version TEXT,
    preview_hash TEXT,
    policy_version TEXT NOT NULL,
    expires_at REAL,
    status TEXT NOT NULL,
    decided_by TEXT NOT NULL DEFAULT '',
    decided_at REAL,
    note TEXT NOT NULL DEFAULT '',
    -- The whole binding as JSON, so a reader cannot reconstruct it differently
    -- from the columns (the columns exist for querying, this is the authority).
    binding TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS approvals_pending ON approvals(status);
CREATE TABLE IF NOT EXISTS approval_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    status TEXT NOT NULL,
    args_hash TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    binding TEXT NOT NULL,
    at REAL NOT NULL
);
"""


@dataclass
class SqliteApprovalStore:
    """A durable :class:`~yanxu_harness.tools.policy.ApprovalStore`."""

    path: Path
    timeout: float = 5.0

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            str(self.path), timeout=self.timeout, isolation_level=None, check_same_thread=False
        )
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "SqliteApprovalStore":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- port -----------------------------------------------------------

    async def get(self, key: str) -> Approval | None:
        row = self._connection.execute(
            "SELECT binding FROM approvals WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else _approval_from_row(row[0])

    async def put(self, approval: Approval) -> Approval:
        with self._lock:
            if self._connection.execute(
                "SELECT 1 FROM approvals WHERE key = ?", (approval.key,)
            ).fetchone():
                raise ContractError(
                    f"{approval.key}: an approval for this call already exists; "
                    "put() is for calls that have none (use supersede otherwise)"
                )
            self._write(approval)
        return approval

    async def supersede(self, binding: ApprovalBinding) -> Approval:
        key = binding.key()
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    "SELECT binding FROM approvals WHERE key = ?", (key,)
                ).fetchone()
                if row is not None:
                    previous = _approval_from_row(row[0])
                    if previous.binding == binding:
                        self._connection.execute("COMMIT")
                        return previous
                    self._connection.execute(
                        "INSERT INTO approval_history(key, status, args_hash, reason, binding, at) "
                        "VALUES (?, ?, ?, 'superseded', ?, ?)",
                        (key, previous.status.value, previous.binding.args_hash, row[0], time.time()),
                    )
                fresh = Approval(binding=binding, status=ApprovalStatus.PENDING)
                self._write(fresh)
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            self._connection.execute("COMMIT")
            return fresh

    async def decide(
        self, key: str, status: ApprovalStatus, decided_by: str, note: str = ""
    ) -> Approval:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    "SELECT binding FROM approvals WHERE key = ?", (key,)
                ).fetchone()
                if row is None:
                    raise ContractError(f"{key}: no approval recorded for this call")
                current = _approval_from_row(row[0])
                if current.status is status:
                    self._connection.execute("COMMIT")
                    return current
                if not current.is_open():
                    raise ContractError(
                        f"{key}: approval already decided as {current.status.value}; "
                        "refusing to overwrite it"
                    )
                decided = Approval(
                    binding=current.binding,
                    status=status,
                    decided_by=decided_by,
                    decided_at=time.time(),
                    note=note,
                )
                self._write(decided)
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            self._connection.execute("COMMIT")
            return decided

    # -- reads ----------------------------------------------------------

    def pending(self) -> tuple[Approval, ...]:
        rows = self._connection.execute(
            "SELECT binding FROM approvals WHERE status = ? ORDER BY call_id",
            (ApprovalStatus.PENDING.value,),
        ).fetchall()
        return tuple(_approval_from_row(row[0]) for row in rows)

    def history(self, key: str | None = None) -> tuple[Mapping[str, Any], ...]:
        sql = "SELECT key, status, args_hash, reason, at FROM approval_history"
        params: tuple[Any, ...] = ()
        if key is not None:
            sql += " WHERE key = ?"
            params = (key,)
        sql += " ORDER BY id"
        return tuple(
            {"key": row[0], "status": row[1], "args_hash": row[2], "reason": row[3], "at": row[4]}
            for row in self._connection.execute(sql, params).fetchall()
        )

    # -- internals ------------------------------------------------------

    def _write(self, approval: Approval) -> None:
        binding = approval.binding
        self._connection.execute(
            "INSERT INTO approvals(key, namespace, call_id, tool_name, tool_version, args_hash, "
            "target_version, preview_hash, policy_version, expires_at, status, decided_by, "
            "decided_at, note, binding) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET namespace=excluded.namespace, call_id=excluded.call_id, "
            "tool_name=excluded.tool_name, tool_version=excluded.tool_version, "
            "args_hash=excluded.args_hash, target_version=excluded.target_version, "
            "preview_hash=excluded.preview_hash, policy_version=excluded.policy_version, "
            "expires_at=excluded.expires_at, status=excluded.status, decided_by=excluded.decided_by, "
            "decided_at=excluded.decided_at, note=excluded.note, binding=excluded.binding",
            (
                binding.key(),
                binding.namespace,
                binding.call_id,
                binding.tool_name,
                binding.tool_version,
                binding.args_hash,
                binding.target_version,
                binding.preview_hash,
                binding.policy_version,
                binding.expires_at,
                approval.status.value,
                approval.decided_by,
                approval.decided_at,
                approval.note,
                _row_to_payload(approval),
            ),
        )


def _approval_from_row(raw: str) -> Approval:
    payload = json.loads(raw)
    binding = ApprovalBinding(
        namespace=payload["namespace"],
        call_id=payload["call_id"],
        tool_name=payload["tool_name"],
        tool_version=payload["tool_version"],
        args_hash=payload["args_hash"],
        target_version=payload.get("target_version"),
        preview_hash=payload.get("preview_hash"),
        policy_version=payload.get("policy_version", POLICY_VERSION),
        expires_at=payload.get("expires_at"),
    )
    return Approval(
        binding=binding,
        status=ApprovalStatus(payload.get("status", ApprovalStatus.PENDING.value)),
        decided_by=payload.get("decided_by", ""),
        decided_at=payload.get("decided_at"),
        note=payload.get("note", ""),
    )


def _row_to_payload(approval: Approval) -> str:
    binding = approval.binding
    return json.dumps(
        {
            "namespace": binding.namespace,
            "call_id": binding.call_id,
            "tool_name": binding.tool_name,
            "tool_version": binding.tool_version,
            "args_hash": binding.args_hash,
            "target_version": binding.target_version,
            "preview_hash": binding.preview_hash,
            "policy_version": binding.policy_version,
            "expires_at": binding.expires_at,
            "status": approval.status.value,
            "decided_by": approval.decided_by,
            "decided_at": approval.decided_at,
            "note": approval.note,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
