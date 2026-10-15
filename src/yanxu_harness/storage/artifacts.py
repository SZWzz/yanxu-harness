"""A durable artifact store: the original survives the process that referred to it.

The in-memory store in :mod:`yanxu_harness.context.artifacts` says out loud that it is
volatile, which is honest and enough *within* one run. But a bounded tool result is written
into a run record as a reference, and a record outlives the process — so a reference that
only a dead process could resolve is exactly the "unreadable reference replacing the only
body" the context plan forbids.

This store is the other half: one SQLite file, content-addressed rows, no updates and no
deletes. Three properties, each with a reason:

* **Immutable and content-addressed.** The primary key *is* the content fingerprint, and
  ``put`` is an ``INSERT OR IGNORE``: the same bytes cannot become two artifacts, and an
  existing row cannot be overwritten by different bytes (the ``WHERE`` on the read path is
  the id, so a mismatch is impossible by construction rather than by convention).
* **It never evicts.** There is no delete API. A store that could drop a blob would break a
  committed reference, and deciding which references are still live needs run-record
  knowledge this store deliberately does not have.
* **Reads are reads.** :meth:`get` returns bytes and nothing else; it does not touch the run
  store, the operation ledger or any state. Whatever authorises a read happens above it.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from ..context.artifacts import ArtifactRef
from ..errors import ContractError
from ..versioning import fingerprint
from .ports import Durability

__all__ = ["SqliteArtifactStore", "ARTIFACT_SCHEMA_VERSION"]

#: Version of this store's own table. A reader refuses a newer one instead of guessing.
ARTIFACT_SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    content BLOB NOT NULL,
    media_type TEXT NOT NULL,
    source_call_id TEXT NOT NULL DEFAULT '',
    source_tool_name TEXT NOT NULL DEFAULT '',
    byte_length INTEGER NOT NULL,
    char_length INTEGER NOT NULL,
    created_at REAL NOT NULL,
    schema_version INTEGER NOT NULL
);
"""


class SqliteArtifactStore:
    """Immutable, content-addressed originals in one SQLite file.

    Deliberately synchronous: storing an original happens during preparation, which is not
    on the event loop's critical path in the same way a store commit is, and a bounded
    ``INSERT`` into a local file is not worth a worker thread. The connection is guarded by a
    lock for the same reason the other stores guard theirs — one connection, one writer.
    """

    def __init__(self, path: str | Path, *, clock: Any = time.time) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.executescript(_SCHEMA)
        self._connection.commit()
        stored = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
        if stored > ARTIFACT_SCHEMA_VERSION:
            raise ContractError(
                f"the artifact store at {self.path} was written by a newer build "
                f"(schema {stored} > {ARTIFACT_SCHEMA_VERSION}); refusing to read it"
            )
        self._connection.execute(f"PRAGMA user_version={ARTIFACT_SCHEMA_VERSION}")
        self._connection.commit()

    # -- introspection --------------------------------------------------

    @property
    def durability(self) -> Durability:
        return Durability.DURABLE

    @property
    def count(self) -> int:
        with self._lock:
            row = self._connection.execute("SELECT COUNT(*) AS n FROM artifacts").fetchone()
        return int(row["n"])

    @property
    def stored_bytes(self) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COALESCE(SUM(byte_length), 0) AS total FROM artifacts"
            ).fetchone()
        return int(row["total"])

    # -- the port -------------------------------------------------------

    def put(
        self,
        content: bytes,
        *,
        media_type: str = "text/plain",
        source_call_id: str = "",
        source_tool_name: str = "",
    ) -> ArtifactRef:
        if not isinstance(content, (bytes, bytearray)):
            raise ContractError(
                f"an artifact store holds bytes, got {type(content).__name__}; encode text first"
            )
        blob = bytes(content)
        artifact_id = fingerprint({"bytes": blob.hex()})
        ref = ArtifactRef(
            artifact_id=artifact_id,
            byte_length=len(blob),
            media_type=media_type,
            source_call_id=source_call_id,
            source_tool_name=source_tool_name,
            char_length=len(blob.decode("utf-8", errors="replace")),
        )
        with self._lock:
            self._connection.execute(
                "INSERT OR IGNORE INTO artifacts (artifact_id, content, media_type, source_call_id,"
                " source_tool_name, byte_length, char_length, created_at, schema_version)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    artifact_id,
                    blob,
                    media_type,
                    source_call_id,
                    source_tool_name,
                    len(blob),
                    ref.char_length,
                    float(self._clock()),
                    ARTIFACT_SCHEMA_VERSION,
                ),
            )
            self._connection.commit()
            row = self._connection.execute(
                "SELECT content FROM artifacts WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
        if row is None or bytes(row["content"]) != blob:
            # Content addressing makes this unreachable; asserting it is how it stays that way.
            raise ContractError(
                f"the artifact store returned a row whose content does not match {artifact_id}"
            )
        return ref

    def get(self, artifact_id: str) -> bytes | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT content FROM artifacts WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
        return None if row is None else bytes(row["content"])

    def holds(self, artifact_id: str) -> bool:
        with self._lock:
            row = self._connection.execute(
                "SELECT 1 FROM artifacts WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
        return row is not None

    def ref(self, artifact_id: str) -> ArtifactRef | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
        if row is None:
            return None
        return ArtifactRef(
            artifact_id=str(row["artifact_id"]),
            byte_length=int(row["byte_length"]),
            media_type=str(row["media_type"]),
            source_call_id=str(row["source_call_id"]),
            source_tool_name=str(row["source_tool_name"]),
            char_length=int(row["char_length"]),
        )

    def close(self) -> None:
        with self._lock:
            self._connection.close()
