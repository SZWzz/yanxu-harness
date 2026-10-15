"""Persistence ports and the two reference stores.

* :class:`~yanxu_harness.storage.memory.InMemoryPersistence` — volatile, and it
  says so; a test double and a single-process store.
* :class:`~yanxu_harness.storage.sqlite.SqlitePersistence` — durable, transactional,
  with real compare-and-set and fenced leases.
* :class:`~yanxu_harness.storage.operations.SqliteExecutionBackend` — a durable
  intent/receipt ledger, so an operation that died mid-flight can be detected.

The SQLite modules import the standard library's ``sqlite3`` only, so a host that
never constructs them pays nothing for them.
"""

from .approvals import SqliteApprovalStore
from .memory import InMemoryPersistence
from .operations import SqliteExecutionBackend, UnsettledOperation
from .ports import (
    Durability,
    Lease,
    Persistence,
    StorageConflict,
    StoredEvent,
    UnitOfWork,
    VersionedRun,
    lease_is_valid,
)
from .sqlite import SCHEMA_VERSION, SqlitePersistence

__all__ = [
    "Durability",
    "InMemoryPersistence",
    "Lease",
    "Persistence",
    "SCHEMA_VERSION",
    "SqliteApprovalStore",
    "SqliteExecutionBackend",
    "SqlitePersistence",
    "StorageConflict",
    "StoredEvent",
    "UnitOfWork",
    "UnsettledOperation",
    "VersionedRun",
    "lease_is_valid",
]
