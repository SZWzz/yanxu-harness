"""Read-only run diagnostics: explain what a run did, from the facts about it.

The package is deliberately separate from the runtime and the tools layer:

* it **reads** — no ``start``/``resume``/``cancel``, no lease, no tool invocation, no
  reconciler, no reducer advance, no side-effecting host hook;
* it **never becomes a second source of truth** — an operation's state is reported as a
  reference to the ledger that owns it, and a report that cannot ask the ledger says
  ``unsupported`` instead of inferring;
* it is **importable without anything optional** — the base install is standard library
  only, and no observation SDK, credential or pytest import appears on this path.

``types`` holds the versioned report objects. The readers, projections and renderers that
build them live beside it and import from here, so a host can depend on the shapes without
depending on how a particular store is read.
"""

from __future__ import annotations

from .projection import project_report
from .reader import DiagnosticFacts, RunInspector
from .render import to_json, to_markdown
from .tasks import (
    BudgetFact,
    ChildFact,
    ClaimFact,
    InputFact,
    OutboxFact,
    TaskDiagnosticReport,
    TaskInspector,
    task_report_to_json,
    task_report_to_markdown,
)
from .types import (
    CallDiagnostic,
    DIAGNOSTIC_SCHEMA_VERSION,
    Evidence,
    ReadBoundary,
    RunDiagnosticReport,
    SourceKind,
    SpanObservation,
    SUPPORTED_DIAGNOSTIC_SCHEMA_VERSIONS,
    TraceEntry,
    UsageObservation,
    UsageQuantity,
    count_sources,
)

__all__ = [
    "BudgetFact",
    "CallDiagnostic",
    "ChildFact",
    "ClaimFact",
    "DiagnosticFacts",
    "InputFact",
    "OutboxFact",
    "RunInspector",
    "TaskDiagnosticReport",
    "TaskInspector",
    "project_report",
    "task_report_to_json",
    "task_report_to_markdown",
    "to_json",
    "to_markdown",
    "count_sources",
    "DIAGNOSTIC_SCHEMA_VERSION",
    "Evidence",
    "ReadBoundary",
    "RunDiagnosticReport",
    "SourceKind",
    "SpanObservation",
    "SUPPORTED_DIAGNOSTIC_SCHEMA_VERSIONS",
    "TraceEntry",
    "UsageObservation",
    "UsageQuantity",
]
