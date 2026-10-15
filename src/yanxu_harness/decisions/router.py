"""The optional decision layer: suggestions, never authority.

A decision router is asked "given this task and these bounded candidates, which
one fits?" and answers with ids, an optional ordering and a reason. Three
implementations share one contract:

* :class:`NoopDecisionRouter` — always ``absent``. A host that configures no
  decision model still runs every task; this is the default.
* :class:`RuleDecisionRouter` — a host-supplied callable.
* a model-backed router — the host's adapter. The package does not ship a
  network client for it, and does not hard-code any brand, endpoint or story
  profile.

What a router may never do: create a tool, grant an approval, or claim a write
succeeded. There is deliberately no field on :class:`DecisionOutcome` that could
express any of those, and the input carries only bounded candidate metadata —
never the full transcript, skill bodies, credentials or private project text.

Failures are explicit. A router that times out, is cancelled, or returns
unparseable output reports ``degraded`` with a reason; it never reports ``ok``
with an empty suggestion, because "the model chose nothing" and "the model never
answered" are different facts.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence, runtime_checkable

from ..errors import BudgetExceeded, ContractError

__all__ = [
    "DecisionCandidate",
    "DecisionKind",
    "DecisionOutcome",
    "DecisionRequest",
    "DecisionRouter",
    "DecisionStatus",
    "DecisionSuggestion",
    "DecisionUsage",
    "NoopDecisionRouter",
    "RuleDecisionRouter",
    "validate_suggestions",
]


class DecisionKind(str, Enum):
    """What the router is choosing between."""

    TOOL = "tool"
    SKILL = "skill"


class DecisionStatus(str, Enum):
    """How the router ended up."""

    OK = "ok"
    ABSENT = "absent"
    DEGRADED = "degraded"
    CANCELLED = "cancelled"
    BUDGET_EXCEEDED = "budget_exceeded"


@dataclass(frozen=True)
class DecisionCandidate:
    """Bounded metadata about one candidate. No bodies, no private text."""

    candidate_id: str
    kind: DecisionKind
    description: str
    version: str = "1"

    def __post_init__(self) -> None:
        if not self.candidate_id:
            raise ContractError("a decision candidate needs an id")


@dataclass(frozen=True)
class DecisionRequest:
    """The whole input a router is allowed to see."""

    task: str
    candidates: tuple[DecisionCandidate, ...]
    kind: DecisionKind
    max_suggestions: int = 8
    scope: str = ""
    policy_version: str = ""
    #: True when ``task`` is a *bounded* view of a longer input (the host cut it
    #: before the model saw it). Two requests can then carry the exact same
    #: ``task`` while being different inputs, so this flag is part of the cache
    #: identity (see ``DecisionCache.key_for``): "the model rejected this text"
    #: must never be reused for "this text was clipped before the model saw it".
    truncated: bool = False

    def __post_init__(self) -> None:
        if self.max_suggestions <= 0:
            raise ContractError("max_suggestions must be positive")
        object.__setattr__(self, "candidates", tuple(self.candidates))

    def candidate_ids(self) -> tuple[str, ...]:
        return tuple(candidate.candidate_id for candidate in self.candidates)


@dataclass(frozen=True)
class DecisionSuggestion:
    """One recommendation, referencing a candidate the request contained."""

    candidate_id: str
    kind: DecisionKind
    rank: int
    score: float | None = None
    reason: str = ""


@dataclass(frozen=True)
class DecisionUsage:
    """What the decision call cost, billed separately from the main request."""

    calls: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    duration_ms: float | None = None

    def combine(self, other: "DecisionUsage") -> "DecisionUsage":
        return DecisionUsage(
            calls=self.calls + other.calls,
            input_tokens=_add(self.input_tokens, other.input_tokens),
            output_tokens=_add(self.output_tokens, other.output_tokens),
            duration_ms=_add(self.duration_ms, other.duration_ms),
        )


def _add(left: float | int | None, right: float | int | None) -> Any:
    if left is None:
        return right
    if right is None:
        return left
    return left + right


@dataclass(frozen=True)
class DecisionOutcome:
    """The router's answer plus everything a host needs to audit it."""

    status: DecisionStatus
    suggestions: tuple[DecisionSuggestion, ...] = ()
    failure: str | None = None
    rejected: Mapping[str, str] = field(default_factory=dict)
    usage: DecisionUsage = field(default_factory=DecisionUsage)
    decision_version: str = "1"

    def __post_init__(self) -> None:
        if self.status is DecisionStatus.OK and not self.suggestions:
            raise ContractError(
                "an ok decision must contain at least one suggestion; "
                "use absent/degraded when nothing was chosen"
            )
        if self.status in {DecisionStatus.DEGRADED, DecisionStatus.CANCELLED, DecisionStatus.BUDGET_EXCEEDED}:
            if not self.failure:
                raise ContractError(f"a {self.status.value} decision must explain itself")

    @property
    def ok(self) -> bool:
        return self.status is DecisionStatus.OK

    def ids(self) -> tuple[str, ...]:
        return tuple(suggestion.candidate_id for suggestion in self.suggestions)


def validate_suggestions(
    outcome: DecisionOutcome,
    request: DecisionRequest,
) -> DecisionOutcome:
    """Drop unknown/duplicate ids and enforce the request's suggestion limit.

    Unknown ids are *rejected with a reason* rather than silently kept: a router
    that invents ids is a router that cannot be trusted with the ones it did
    receive. The result stays ``ok`` only if something valid remains.
    """
    known = set(request.candidate_ids())
    seen: set[str] = set()
    kept: list[DecisionSuggestion] = []
    rejected = dict(outcome.rejected)
    for suggestion in sorted(outcome.suggestions, key=lambda item: (item.rank, item.candidate_id)):
        if suggestion.candidate_id not in known:
            rejected[suggestion.candidate_id] = "not in the offered candidate set"
            continue
        if suggestion.candidate_id in seen:
            rejected[suggestion.candidate_id] = "duplicate suggestion"
            continue
        seen.add(suggestion.candidate_id)
        kept.append(suggestion)
    if len(kept) > request.max_suggestions:
        for extra in kept[request.max_suggestions:]:
            rejected[extra.candidate_id] = "over the request's suggestion limit"
        kept = kept[: request.max_suggestions]
    if not kept and outcome.status is DecisionStatus.OK:
        return DecisionOutcome(
            status=DecisionStatus.DEGRADED,
            suggestions=(),
            failure="every suggested id was rejected",
            rejected=rejected,
            usage=outcome.usage,
            decision_version=outcome.decision_version,
        )
    return DecisionOutcome(
        status=outcome.status,
        suggestions=tuple(kept),
        failure=outcome.failure,
        rejected=rejected,
        usage=outcome.usage,
        decision_version=outcome.decision_version,
    )


@runtime_checkable
class DecisionRouter(Protocol):
    """Choose among bounded candidates, or say why not."""

    @property
    def decision_version(self) -> str:
        """Version of this router's decision semantics (part of cache keys)."""

    async def route(self, request: DecisionRequest) -> DecisionOutcome:
        """Return a bounded decision. Never authorize anything."""


@dataclass(frozen=True)
class NoopDecisionRouter:
    """The default: no decision model configured, everything still runs."""

    decision_version: str = "noop/1"

    async def route(self, request: DecisionRequest) -> DecisionOutcome:
        return DecisionOutcome(status=DecisionStatus.ABSENT, decision_version=self.decision_version)


@dataclass(frozen=True)
class RuleDecisionRouter:
    """A deterministic router backed by a host callable.

    The callable receives the request and returns candidate ids in order. Raising
    anything other than :class:`BudgetExceeded`/``CancelledError`` degrades the
    decision instead of failing the run: a recommendation is never a
    precondition for the task itself.
    """

    decide: Callable[[DecisionRequest], Sequence[str] | Awaitable[Sequence[str]]]
    decision_version: str = "rules/1"

    async def route(self, request: DecisionRequest) -> DecisionOutcome:
        started = time.monotonic()
        try:
            chosen = self.decide(request)
            if hasattr(chosen, "__await__"):
                chosen = await chosen  # type: ignore[assignment]
        except BudgetExceeded as exc:
            return DecisionOutcome(
                status=DecisionStatus.BUDGET_EXCEEDED,
                failure=str(exc),
                usage=_elapsed(started, 1),
                decision_version=self.decision_version,
            )
        except Exception as exc:  # noqa: BLE001 - host callable
            return DecisionOutcome(
                status=DecisionStatus.DEGRADED,
                failure=f"{type(exc).__name__}: {exc}",
                usage=_elapsed(started, 1),
                decision_version=self.decision_version,
            )
        suggestions = tuple(
            DecisionSuggestion(candidate_id=str(candidate_id), kind=request.kind, rank=index)
            for index, candidate_id in enumerate(chosen)
        )
        if not suggestions:
            return DecisionOutcome(
                status=DecisionStatus.ABSENT,
                usage=_elapsed(started, 1),
                decision_version=self.decision_version,
            )
        return validate_suggestions(
            DecisionOutcome(
                status=DecisionStatus.OK,
                suggestions=suggestions,
                usage=_elapsed(started, 1),
                decision_version=self.decision_version,
            ),
            request,
        )


def _elapsed(started: float, calls: int) -> DecisionUsage:
    return DecisionUsage(calls=calls, duration_ms=(time.monotonic() - started) * 1000.0)
