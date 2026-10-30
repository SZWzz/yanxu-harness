"""A model-backed decision router, with the host supplying the model call.

The package ships no network client for decisions on purpose: the host already
has one, already knows its own credentials, and already owns the policy about
which requests are allowed to leave the machine. What the package supplies is the
part that is easy to get wrong — the budget, the cache, the validation and the
failure states.

Rules this router enforces, each of which has a test:

* **At most ``max_calls_per_run`` host calls**, counted across every request the
  router serves. A decision model is an optimisation; blowing the budget must
  degrade the decision, not the task.
* **The input is bounded and declared.** The serialized payload is measured; too
  large means *no dispatch at all*, not a truncated payload (a truncated
  candidate list would let the model "choose nothing" for a reason nobody sees).
* **Failure never looks like a choice.** Timeouts, cancellations, unparseable
  output and budget stops are `degraded`/`cancelled`/`budget_exceeded` with a
  reason. Only a validated suggestion is `ok`.
* **Nothing is cached unless it is a stable fact.** ``ok`` and ``absent`` are
  cached under a key that includes the scope, the candidate versions, the policy
  version, the decision version and the task; cancellations, budget stops and
  degraded results never are.
* **One call, separately validated outputs.** :meth:`ModelDecisionRouter.route_many`
  asks once for several kinds and then validates each kind's ids against its own
  candidate set.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence

from ..errors import BudgetExceeded, ContractError
from ..tasks.types import BudgetRefused
from ..versioning import canonical_json, fingerprint
from .router import (
    DecisionKind,
    DecisionOutcome,
    DecisionRequest,
    DecisionStatus,
    DecisionSuggestion,
    DecisionUsage,
    validate_suggestions,
)

__all__ = [
    "DecisionBudget",
    "DecisionCache",
    "DecisionModelCall",
    "ModelDecisionRouter",
    "build_payload",
]


class DecisionModelCall(Protocol):
    """The host's model call. Receives a bounded payload, returns a mapping."""

    def __call__(self, payload: Mapping[str, Any]) -> Awaitable[Mapping[str, Any]]:
        """Return ``{"suggestions": [...]}`` or raise. Must not retry on its own."""


@dataclass(frozen=True)
class DecisionBudget:
    """Bounds on the decision model. Defaults are deliberately tight."""

    max_calls_per_run: int = 1
    max_seconds: float = 10.0
    max_input_chars: int = 20000
    max_output_chars: int = 8000

    def __post_init__(self) -> None:
        for name in ("max_calls_per_run", "max_input_chars", "max_output_chars"):
            if getattr(self, name) <= 0:
                raise ContractError(f"{name} must be positive")
        if self.max_seconds <= 0:
            raise ContractError("max_seconds must be positive")


@dataclass
class DecisionCache:
    """A volatile decision cache. A hit costs nothing and is marked as such.

    ``ttl_seconds`` makes the validity period **explicit**: an entry older than
    it is treated as a miss and dropped, so a long-lived host cache cannot keep
    serving a decision past the moment it was measured. ``None`` means "for the
    lifetime of this cache object", which is what the in-process reference uses.

    The cache stores whatever the caller puts in it. *Which* values are worth
    caching is the caller's policy, not the container's: ``ModelDecisionRouter``
    only stores stable facts (see ``_cacheable``), while a host that reuses this
    type for its own audited outcome decides for itself.
    """

    ttl_seconds: float | None = None
    clock: Callable[[], float] = time.monotonic
    _entries: dict[str, tuple[float, Any]] = field(default_factory=dict)
    hits: int = 0
    misses: int = 0

    def __post_init__(self) -> None:
        if self.ttl_seconds is not None and self.ttl_seconds <= 0:
            raise ContractError("ttl_seconds must be positive when set")

    @staticmethod
    def key_for(
        request: DecisionRequest,
        *,
        decision_version: str,
        extra: Mapping[str, Any] | None = None,
    ) -> str:
        """Bind the cache entry to everything that could make the answer wrong."""
        return fingerprint(
            {
                "task": request.task,
                "kind": request.kind.value,
                "scope": request.scope,
                "policy_version": request.policy_version,
                "decision_version": decision_version,
                "max_suggestions": request.max_suggestions,
                "truncated": bool(request.truncated),
                "candidates": [
                    [candidate.candidate_id, candidate.version, candidate.kind.value]
                    for candidate in request.candidates
                ],
                "extra": dict(extra or {}),
            }
        )

    def get(self, key: str) -> Any | None:
        entry = self._entries.get(key)
        if entry is None:
            self.misses += 1
            return None
        stored_at, value = entry
        if self.ttl_seconds is not None and self.clock() - stored_at > self.ttl_seconds:
            del self._entries[key]
            self.misses += 1
            return None
        self.hits += 1
        return value

    def put(self, key: str, value: Any) -> None:
        """Store a value. The caller decides whether it is worth caching."""
        self._entries[key] = (self.clock(), value)

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)


def build_payload(requests: Sequence[DecisionRequest]) -> Mapping[str, Any]:
    """Build the whole request the host's model call receives.

    It contains the task text and bounded candidate metadata — no transcript, no
    skill bodies, no credentials, no private project text. Tools and skills may
    share one payload; the caller does not have to know that.
    """
    if not requests:
        raise ContractError("a decision payload needs at least one request")
    return {
        "requests": [
            {
                "kind": request.kind.value,
                "task": request.task,
                "scope": request.scope,
                "max_suggestions": request.max_suggestions,
                "candidates": [
                    {
                        "id": candidate.candidate_id,
                        "version": candidate.version,
                        "description": candidate.description,
                    }
                    for candidate in request.candidates
                ],
            }
            for request in requests
        ]
    }


class ModelDecisionRouter:
    """A decision router backed by a host-supplied model call."""

    def __init__(
        self,
        call: DecisionModelCall,
        *,
        budget: DecisionBudget | None = None,
        cache: DecisionCache | None = None,
        policy_version: str = "",
        decision_version: str = "model/1",
        clock: Callable[[], float] = time.monotonic,
        dispatch_budget: Any | None = None,
        dispatch_context: Callable[[Sequence[DecisionRequest]], Mapping[str, Any]] | None = None,
        usage_extractor: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    ) -> None:
        if dispatch_budget is not None and not callable(
            getattr(dispatch_budget, "reserve_auxiliary", None)
        ):
            raise ContractError("decision dispatch_budget must expose reserve_auxiliary")
        if dispatch_budget is not None and not callable(
            getattr(dispatch_budget, "settle_auxiliary", None)
        ):
            raise ContractError("decision dispatch_budget must expose settle_auxiliary")
        if dispatch_budget is not None and not callable(dispatch_context):
            raise ContractError(
                "decision dispatch_context is required when typed dispatch budgeting is enabled"
            )
        self._call = call
        self._budget = budget or DecisionBudget()
        # Not `cache or ...`: `DecisionCache.__len__` makes an *empty* cache
        # falsy, so a caller-supplied cache would be silently discarded.
        self.cache = cache if cache is not None else DecisionCache()
        self._policy_version = policy_version
        self._decision_version = decision_version
        self._clock = clock
        self._dispatch_budget = dispatch_budget
        self._dispatch_context = dispatch_context
        self._usage_extractor = usage_extractor
        self.calls = 0

    @property
    def decision_version(self) -> str:
        return self._decision_version

    @property
    def usage(self) -> DecisionUsage:
        return DecisionUsage(calls=self.calls)

    # -- the port -------------------------------------------------------

    async def route(
        self, request: DecisionRequest, cancellation: Any = None
    ) -> DecisionOutcome:
        """Ask once for one kind."""
        outcomes = await self.route_many([request], cancellation=cancellation)
        return outcomes[0]

    async def route_many(
        self, requests: Sequence[DecisionRequest], cancellation: Any = None
    ) -> tuple[DecisionOutcome, ...]:
        """Ask once for several kinds, then validate each kind's answer separately."""
        requests = tuple(requests)
        if not requests:
            raise ContractError("a decision needs at least one request")
        bounded = [
            request
            if request.policy_version
            else DecisionRequest(
                task=request.task,
                candidates=request.candidates,
                kind=request.kind,
                max_suggestions=request.max_suggestions,
                scope=request.scope,
                policy_version=self._policy_version,
                truncated=request.truncated,
            )
            for request in requests
        ]

        keys = [
            DecisionCache.key_for(request, decision_version=self._decision_version)
            for request in bounded
        ]

        # Cancellation is checked *before* the cache: a cached answer only says
        # what the model said earlier, never that this run may still use it.
        if cancellation is not None and getattr(cancellation, "cancelled", False):
            return tuple(
                _failed(request, DecisionStatus.CANCELLED, "run cancelled")
                for request in bounded
            )

        if len(set(keys)) == 1:
            cached = self.cache.get(keys[0])
            if cached is not None:
                return tuple(
                    _replayed(cached, request) for request in bounded
                )

        if self.calls >= self._budget.max_calls_per_run:
            return tuple(
                _failed(
                    request,
                    DecisionStatus.BUDGET_EXCEEDED,
                    f"decision call budget of {self._budget.max_calls_per_run} is used",
                )
                for request in bounded
            )

        payload = build_payload(bounded)
        serialized = canonical_json(payload)
        if len(serialized) > self._budget.max_input_chars:
            # Refuse as a whole: a truncated candidate list would let the model
            # "choose nothing" for a reason the caller cannot see.
            return tuple(
                _failed(
                    request,
                    DecisionStatus.BUDGET_EXCEEDED,
                    f"decision payload is {len(serialized)} chars, limit is {self._budget.max_input_chars}",
                )
                for request in bounded
            )

        reservation = None
        if self._dispatch_budget is not None:
            assert self._dispatch_context is not None
            dispatch_context = self._dispatch_context(tuple(bounded))
            if not isinstance(dispatch_context, Mapping):
                raise ContractError("decision dispatch_context must return a mapping")
            try:
                reservation = await self._dispatch_budget.reserve_auxiliary(
                    {
                        **dict(dispatch_context),
                        "decision_payload_digest": fingerprint(payload),
                    },
                    kind="decision",
                )
            except BudgetRefused as exc:
                return tuple(
                    _failed(request, DecisionStatus.BUDGET_EXCEEDED, str(exc))
                    for request in bounded
                )
            if cancellation is not None and getattr(cancellation, "cancelled", False):
                await self._dispatch_budget.settle_auxiliary(
                    reservation,
                    usage={},
                    dispatched=False,
                    detail="decision was cancelled before the host model call",
                )
                return tuple(
                    _failed(request, DecisionStatus.CANCELLED, "run cancelled")
                    for request in bounded
                )

        started = self._clock()
        self.calls += 1
        try:
            raw = await self._call(payload)
        except BudgetExceeded as exc:
            if self._dispatch_budget is not None and reservation is not None:
                try:
                    await self._dispatch_budget.settle_auxiliary(
                        reservation,
                        usage=None,
                        detail="decision model call ended without a response",
                    )
                except Exception:
                    pass
            return tuple(
                _failed(request, DecisionStatus.BUDGET_EXCEEDED, str(exc)) for request in bounded
            )
        except asyncio.CancelledError:
            if self._dispatch_budget is not None and reservation is not None:
                try:
                    await self._dispatch_budget.settle_auxiliary(
                        reservation,
                        usage=None,
                        detail="decision model call was cancelled after entering the host call",
                    )
                except Exception:
                    pass
            raise
        except Exception as exc:  # noqa: BLE001 - the host call is host code
            if self._dispatch_budget is not None and reservation is not None:
                try:
                    await self._dispatch_budget.settle_auxiliary(
                        reservation,
                        usage=None,
                        detail="decision model call ended without a response",
                    )
                except Exception:
                    pass
            return tuple(
                _failed(request, DecisionStatus.DEGRADED, f"{type(exc).__name__}: {exc}")
                for request in bounded
            )
        if self._dispatch_budget is not None and reservation is not None:
            usage = {} if self._usage_extractor is None else self._usage_extractor(raw)
            if not isinstance(usage, Mapping):
                raise ContractError("decision usage_extractor must return a mapping")
            try:
                await self._dispatch_budget.settle_auxiliary(reservation, usage=usage)
            except Exception:
                # The host response exists, but a failed settlement reply must keep its
                # durable reservation covered. The decision result is still validated below.
                pass
        elapsed = self._clock() - started
        if elapsed > self._budget.max_seconds:
            return tuple(
                _failed(
                    request,
                    DecisionStatus.DEGRADED,
                    f"decision call took {elapsed:.1f}s, budget is {self._budget.max_seconds:.1f}s",
                )
                for request in bounded
            )

        usage = DecisionUsage(calls=1, duration_ms=elapsed * 1000.0)
        outcomes = self._interpret(bounded, raw, usage)
        for key, outcome in zip(keys, outcomes):
            if _cacheable(outcome):
                self.cache.put(key, outcome)
        return outcomes

    # -- interpretation -------------------------------------------------

    def _interpret(
        self,
        requests: tuple[DecisionRequest, ...],
        raw: Mapping[str, Any],
        usage: DecisionUsage,
    ) -> tuple[DecisionOutcome, ...]:
        if not isinstance(raw, Mapping):
            return tuple(
                _failed(
                    request,
                    DecisionStatus.DEGRADED,
                    f"decision model returned {type(raw).__name__}, not an object",
                )
                for request in requests
            )
        serialized = canonical_json(raw)
        if len(serialized) > self._budget.max_output_chars:
            return tuple(
                _failed(
                    request,
                    DecisionStatus.DEGRADED,
                    f"decision output is {len(serialized)} chars, limit is {self._budget.max_output_chars}",
                )
                for request in requests
            )
        suggestions = raw.get("suggestions")
        if suggestions is None:
            return tuple(
                _failed(request, DecisionStatus.DEGRADED, "decision output has no 'suggestions' key")
                for request in requests
            )
        if not isinstance(suggestions, Sequence) or isinstance(suggestions, (str, bytes)):
            return tuple(
                _failed(request, DecisionStatus.DEGRADED, "'suggestions' must be a list")
                for request in requests
            )

        by_kind: dict[DecisionKind, list[DecisionSuggestion]] = {}
        for index, item in enumerate(suggestions):
            if not isinstance(item, Mapping):
                continue
            kind_value = str(item.get("kind") or requests[0].kind.value)
            try:
                kind = DecisionKind(kind_value)
            except ValueError:
                continue
            candidate_id = str(item.get("candidate_id") or item.get("id") or "")
            if not candidate_id:
                continue
            score = item.get("score")
            by_kind.setdefault(kind, []).append(
                DecisionSuggestion(
                    candidate_id=candidate_id,
                    kind=kind,
                    rank=int(item.get("rank", index)),
                    score=float(score) if isinstance(score, (int, float)) else None,
                    reason=str(item.get("reason") or ""),
                )
            )

        outcomes: list[DecisionOutcome] = []
        for request in requests:
            chosen = tuple(sorted(by_kind.get(request.kind, ()), key=lambda item: (item.rank, item.candidate_id)))
            if not chosen:
                outcomes.append(
                    DecisionOutcome(
                        status=DecisionStatus.ABSENT,
                        usage=usage,
                        decision_version=self._decision_version,
                    )
                )
                continue
            outcomes.append(
                validate_suggestions(
                    DecisionOutcome(
                        status=DecisionStatus.OK,
                        suggestions=chosen,
                        usage=usage,
                        decision_version=self._decision_version,
                    ),
                    request,
                )
            )
        return tuple(outcomes)


def _cacheable(outcome: DecisionOutcome) -> bool:
    """Only a stable fact is cacheable.

    A cancellation or a budget stop is a property of *this* moment, and caching
    it would poison the next attempt. ``absent`` (the model explicitly chose
    nothing) is as stable as ``ok``.
    """
    return outcome.status in {DecisionStatus.OK, DecisionStatus.ABSENT}


def _failed(request: DecisionRequest, status: DecisionStatus, reason: str) -> DecisionOutcome:
    return DecisionOutcome(status=status, failure=reason, decision_version="model/1")


def _replayed(outcome: DecisionOutcome, request: DecisionRequest) -> DecisionOutcome:
    """Return a cached decision without counting it as a call."""
    return DecisionOutcome(
        status=outcome.status,
        suggestions=outcome.suggestions,
        failure=outcome.failure,
        rejected=outcome.rejected,
        usage=DecisionUsage(calls=0),
        decision_version=outcome.decision_version,
    )
