"""Context: the raw transcript archive, the model projection, and the budget.

The archive is append-only and the projection is derived. Anything that looks
like "compaction" here produces a projection; nothing edits the archive.

Budgets are measured over the *whole* request (:mod:`yanxu_harness.context.measurement`)
and an unknown window, estimate or output reserve stays unknown instead of becoming a
percentage.
"""

from .archive import InMemoryTranscriptArchive, ProjectionSnapshot, WindowedProjector
from .measurement import (
    BudgetReport,
    HeuristicTokenEstimator,
    MeasurementInput,
    RequestBudget,
    TokenEstimate,
    TokenEstimator,
    UnknownPolicy,
    payload_budget_gate,
)
from .policy import (
    ContextAssessment,
    ContextPolicy,
    ContextProjector,
    ContextWindow,
    SummaryArtifact,
    SummaryResult,
    Summarizer,
    TranscriptArchive,
    TranscriptEntry,
    validate_publication,
)

__all__ = [
    "BudgetReport",
    "ContextAssessment",
    "ContextPolicy",
    "ContextProjector",
    "ContextWindow",
    "HeuristicTokenEstimator",
    "InMemoryTranscriptArchive",
    "MeasurementInput",
    "ProjectionSnapshot",
    "RequestBudget",
    "SummaryArtifact",
    "SummaryResult",
    "Summarizer",
    "TokenEstimate",
    "TokenEstimator",
    "TranscriptArchive",
    "TranscriptEntry",
    "UnknownPolicy",
    "WindowedProjector",
    "payload_budget_gate",
    "validate_publication",
]
