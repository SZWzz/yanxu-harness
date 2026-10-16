"""Durable input, task orchestration and recoverable parent/child work.

The package builds the *control plane* around the existing Core — one Runner, one reducer, one
Gateway ledger — and never a second agent loop (plan §3):

* :mod:`yanxu_harness.tasks.types` is the frozen vocabulary: task status and its transition table,
  input kinds and their order, admission results and refusal codes, claims, children, budget
  reservations and the control outbox. Pure data and pure decisions;
* :mod:`yanxu_harness.tasks.ports` is the capability a store must provide for persistent tasks —
  and the check that refuses to start when the capability is missing, instead of degrading into
  two independent writes.

What this package is not: a web platform, a scheduler product, a general DAG language or a
sandbox. A local reference worker runs when the host asks it to; nothing here starts a background
thread on import, contacts a network or reads a credential.
"""

from __future__ import annotations

from .ports import (
    COORDINATION_METHODS,
    CoordinationStore,
    ExecutionRight,
    coordination_capability,
    is_coordination_store,
    require_coordination,
)
from .inbox import MemoryControlStore, SqliteControlStore
from .types import (
    TASK_SCHEMA_VERSION,
    AdmissionOutcome,
    AdmissionResult,
    BacklogLimits,
    BudgetRefused,
    BudgetReservation,
    BudgetState,
    BudgetUnit,
    ChildRelation,
    ChildSpawnState,
    ClaimLost,
    ControlOutboxEntry,
    DispatchClaim,
    InputAdmission,
    InputContent,
    InputKind,
    InputRecord,
    InputSelection,
    InputState,
    RefusalCode,
    ReservationState,
    TaskAdmission,
    TaskRecord,
    TaskStatus,
    assert_transition,
    can_transition,
    decide_admission,
    select_inputs,
    transition_table,
)

__all__ = [
    "COORDINATION_METHODS",
    "CoordinationStore",
    "ExecutionRight",
    "AdmissionOutcome",
    "AdmissionResult",
    "BacklogLimits",
    "BudgetRefused",
    "ClaimLost",
    "MemoryControlStore",
    "SqliteControlStore",
    "BudgetReservation",
    "BudgetState",
    "BudgetUnit",
    "ChildRelation",
    "ChildSpawnState",
    "ControlOutboxEntry",
    "DispatchClaim",
    "InputAdmission",
    "InputContent",
    "InputKind",
    "InputRecord",
    "InputSelection",
    "InputState",
    "RefusalCode",
    "ReservationState",
    "TASK_SCHEMA_VERSION",
    "TaskAdmission",
    "TaskRecord",
    "TaskStatus",
    "assert_transition",
    "can_transition",
    "coordination_capability",
    "decide_admission",
    "is_coordination_store",
    "require_coordination",
    "select_inputs",
    "transition_table",
]
