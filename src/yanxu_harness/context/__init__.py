"""Context: the raw transcript archive, the model projection, and the budget.

The archive is append-only and the projection is derived. Anything that looks
like "compaction" here produces a projection; nothing edits the archive.
"""

from .archive import InMemoryTranscriptArchive, ProjectionSnapshot, WindowedProjector
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
    "ContextAssessment",
    "ContextPolicy",
    "ContextProjector",
    "ContextWindow",
    "InMemoryTranscriptArchive",
    "ProjectionSnapshot",
    "SummaryArtifact",
    "SummaryResult",
    "Summarizer",
    "TranscriptArchive",
    "TranscriptEntry",
    "WindowedProjector",
    "validate_publication",
]
