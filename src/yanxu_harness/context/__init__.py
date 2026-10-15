"""Context: the raw transcript archive, the model projection, and the budget.

The archive is append-only and the projection is derived. Anything that looks
like "compaction" here produces a projection; nothing edits the archive.

Budgets are measured over the *whole* request (:mod:`yanxu_harness.context.measurement`)
and an unknown window, estimate or output reserve stays unknown instead of becoming a
percentage. A request view is projected segment by segment
(:mod:`yanxu_harness.context.projection`): the transcript is never edited, a tool call
and its results are one unit, and the effective instruction/tool state is replayed and
verified rather than assumed.
"""

from .archive import InMemoryTranscriptArchive, ProjectionSnapshot, WindowedProjector
from .controller import CONTEXT_STATE_SCHEMA_VERSION, ContextController, PreparationOutcome
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
from .projection import (
    DroppedFragment,
    Fragment,
    FragmentProjector,
    ProjectionResult,
    message_fragments,
    system_state,
    transcript_fingerprint,
    visible_text,
)

__all__ = [
    "BudgetReport",
    "CONTEXT_STATE_SCHEMA_VERSION",
    "ContextController",
    "ContextAssessment",
    "ContextPolicy",
    "ContextProjector",
    "ContextWindow",
    "DroppedFragment",
    "Fragment",
    "FragmentProjector",
    "HeuristicTokenEstimator",
    "InMemoryTranscriptArchive",
    "MeasurementInput",
    "ProjectionResult",
    "ProjectionSnapshot",
    "PreparationOutcome",
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
    "message_fragments",
    "payload_budget_gate",
    "system_state",
    "transcript_fingerprint",
    "validate_publication",
    "visible_text",
]
