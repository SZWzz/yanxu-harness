"""Context policy: raw transcript, model projection, and honest budgets.

Three separate ideas that are easy to conflate and must not be:

**The archive** — the append-only record of what actually happened. Compaction
never rewrites it. A host can always re-read the original turns, which is what
makes a summarization bug recoverable instead of destructive.

**The projection** — the (usually smaller) view that is sent to the model. It is
derived, carries the scope and the source head it was derived from, and is
discarded when either drifts.

**The budget** — how much of the model window this request may use. The window,
the reserved output and the safety margin together decide the request capacity.
When the window is unknown, the capacity is *unknown*: this module refuses to
invent a percentage, and callers get ``None`` plus a reason to report.

A summary is a projection too. It produces no business commit, grants no
approval, and its usage is billed separately from the main request.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from ..errors import BudgetExceeded, ContractError
from ..versioning import fingerprint

__all__ = [
    "ContextAssessment",
    "ContextPolicy",
    "ContextProjector",
    "ContextWindow",
    "SummaryArtifact",
    "SummaryResult",
    "Summarizer",
    "TranscriptArchive",
    "TranscriptEntry",
    "validate_publication",
]


@dataclass(frozen=True)
class ContextWindow:
    """The model's usable window and what must be left out of it."""

    limit_tokens: int | None
    output_reserve: int = 0
    safety_margin: int = 0

    def __post_init__(self) -> None:
        if self.output_reserve < 0 or self.safety_margin < 0:
            raise ContractError("output_reserve and safety_margin must not be negative")
        if self.limit_tokens is not None and self.limit_tokens <= 0:
            raise ContractError("limit_tokens must be positive or None (unknown)")

    @property
    def known(self) -> bool:
        return self.limit_tokens is not None

    @property
    def request_capacity(self) -> int | None:
        """Tokens the request may use, or ``None`` when the window is unknown."""
        if self.limit_tokens is None:
            return None
        capacity = self.limit_tokens - self.output_reserve - self.safety_margin
        return capacity if capacity > 0 else 0


@dataclass(frozen=True)
class ContextAssessment:
    """The result of checking a request against a window."""

    capacity: int | None
    estimated_tokens: int
    fits: bool | None
    reason: str = ""

    @property
    def known(self) -> bool:
        return self.fits is not None


@dataclass(frozen=True)
class ContextPolicy:
    """Check an actual payload against a declared window."""

    window: ContextWindow

    def assess(self, estimated_tokens: int) -> ContextAssessment:
        capacity = self.window.request_capacity
        if capacity is None:
            return ContextAssessment(
                capacity=None,
                estimated_tokens=estimated_tokens,
                fits=None,
                reason="model context window is unknown; no percentage is invented",
            )
        return ContextAssessment(
            capacity=capacity,
            estimated_tokens=estimated_tokens,
            fits=estimated_tokens <= capacity,
            reason="" if estimated_tokens <= capacity else "request exceeds the request capacity",
        )

    def require(self, estimated_tokens: int) -> ContextAssessment:
        """Assess and raise :class:`BudgetExceeded` when the request cannot be sent."""
        assessment = self.assess(estimated_tokens)
        if assessment.fits is False:
            raise BudgetExceeded("context", assessment.reason)
        return assessment


@dataclass(frozen=True)
class TranscriptEntry:
    """One archived turn. The archive is append-only."""

    sequence: int
    role: str
    payload: Mapping[str, Any]
    at: float = 0.0
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if self.sequence < 0:
            raise ContractError("transcript sequence must be >= 0")


@runtime_checkable
class TranscriptArchive(Protocol):
    """Append-only raw history. Compaction never rewrites it."""

    async def append(self, entry: TranscriptEntry) -> TranscriptEntry:
        """Append one entry and return it with its assigned sequence/fingerprint."""

    async def read(self, *, after: int = -1, limit: int = 200) -> Sequence[TranscriptEntry]:
        """Read entries with ``sequence > after``, oldest first."""

    @property
    def head(self) -> str:
        """Fingerprint of the current head; projections record what they derived from."""


@runtime_checkable
class ContextProjector(Protocol):
    """Derive the model-facing view from the archive."""

    def project(
        self,
        archive: Sequence[TranscriptEntry],
        *,
        scope: str,
        source_head: str,
        capacity: int | None,
    ) -> Sequence[Mapping[str, Any]]:
        """Return the payload view. Must not mutate ``archive``."""


@dataclass(frozen=True)
class SummaryResult:
    """A summary plus the provenance needed to accept or refuse it."""

    text: str
    source_head: str
    scope: str
    usage: Mapping[str, Any] = field(default_factory=dict)
    summary_version: str = "1"
    source_fingerprint: str = ""


@dataclass(frozen=True)
class SummaryArtifact:
    """A summary that wants to be published as the new projection head."""

    text: str
    scope: str
    source_head: str
    base_fingerprint: str
    lease_token: str | None = None
    summary_version: str = "1"

    def fingerprint(self) -> str:
        return fingerprint(
            {
                "text": self.text,
                "scope": self.scope,
                "source_head": self.source_head,
                "summary_version": self.summary_version,
            }
        )


def validate_publication(
    artifact: SummaryArtifact,
    *,
    scope: str,
    source_head: str,
    lease_token: str | None = None,
) -> tuple[bool, str]:
    """Decide whether a summary may replace the active projection.

    Scope, source head and lease must all still match. A summary computed from a
    head that has since moved is stale — publishing it would silently drop the
    turns that arrived meanwhile. On refusal the active checkpoint is left
    untouched.
    """
    if artifact.scope != scope:
        return False, f"summary was produced for scope {artifact.scope!r}, not {scope!r}"
    if artifact.source_head != source_head:
        return False, "summary was produced from a different transcript head"
    if lease_token is not None and artifact.lease_token != lease_token:
        return False, "summary was produced under a lease that is no longer held"
    if not artifact.text.strip():
        return False, "refusing to publish an empty summary"
    return True, ""


@runtime_checkable
class Summarizer(Protocol):
    """Turn a projection into a summary. Produces no business effect."""

    async def summarize(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        scope: str,
        source_head: str,
    ) -> SummaryResult:
        """Summarise, reporting the usage of the summary request separately."""
