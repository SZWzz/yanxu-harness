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
from .artifacts import (
    ArtifactRef,
    ArtifactStore,
    BoundedToolOutput,
    InMemoryArtifactStore,
    ToolOutputPolicy,
    bounded_tool_output,
    read_artifact,
)
from .compaction import (
    SUMMARY_FACTS_VERSION,
    CompactionResult,
    SummaryBudget,
    SummaryCompactor,
    SummaryUsage,
)
from .controller import CONTEXT_STATE_SCHEMA_VERSION, ContextController, PreparationOutcome
from .notes import INFERRED_SOURCES, NoteLedger, NoteSource, TaskNote, TaskNotes
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
from .step_context import (
    StepContextLimits,
    StepContextManifest,
    StepContextRefused,
    StepMaterialDecision,
    StepMaterialDisposition,
    StepMaterialKind,
    StepMaterialRef,
    StepMaterialSource,
    StepMaterialTrust,
    StepMaterialValidity,
)
from .step_assembler import (
    StepContextAssembler,
    StepMaterialResolution,
    StepMaterialResolutionRequest,
    StepMaterialResolver,
    StepMaterialSourceVersion,
)
from .step_preparation import StepContextRuntime, StepContextSelection
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
    "INFERRED_SOURCES",
    "ArtifactRef",
    "ArtifactStore",
    "BoundedToolOutput",
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
    "InMemoryArtifactStore",
    "InMemoryTranscriptArchive",
    "MeasurementInput",
    "NoteLedger",
    "NoteSource",
    "ProjectionResult",
    "ProjectionSnapshot",
    "PreparationOutcome",
    "RequestBudget",
    "CompactionResult",
    "SummaryArtifact",
    "SUMMARY_FACTS_VERSION",
    "SummaryBudget",
    "SummaryCompactor",
    "SummaryUsage",
    "StepContextLimits",
    "StepContextManifest",
    "StepContextRefused",
    "StepContextAssembler",
    "StepContextRuntime",
    "StepContextSelection",
    "StepMaterialDecision",
    "StepMaterialDisposition",
    "StepMaterialKind",
    "StepMaterialRef",
    "StepMaterialResolution",
    "StepMaterialResolutionRequest",
    "StepMaterialResolver",
    "StepMaterialSource",
    "StepMaterialSourceVersion",
    "StepMaterialTrust",
    "StepMaterialValidity",
    "SummaryResult",
    "Summarizer",
    "TaskNote",
    "TaskNotes",
    "TokenEstimate",
    "TokenEstimator",
    "ToolOutputPolicy",
    "TranscriptArchive",
    "TranscriptEntry",
    "UnknownPolicy",
    "WindowedProjector",
    "bounded_tool_output",
    "message_fragments",
    "read_artifact",
    "payload_budget_gate",
    "system_state",
    "transcript_fingerprint",
    "validate_publication",
    "visible_text",
]
