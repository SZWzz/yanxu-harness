"""C5: a bounded summary of stable history — optional, budgeted, and never a new truth.

A summary is the most dangerous thing in this package, because it is the only step that
*replaces* text the model would otherwise read with text a model wrote. Everything here is
shaped by that:

* **It is off unless a host injects a summarizer.** With none configured, no auxiliary
  request is made and the deterministic projection is the answer. A test asserts the call
  count is zero, because "we only call the model when needed" is a claim that has to be
  checkable.
* **It only ever summarizes a stable prefix.** The newest fragments (the current input and
  the tool round trip the model is waiting on) are never summarized: they are sent as they
  are, or the request is refused.
* **A publication must still describe the same head.** The summary is produced for a source
  range with a fingerprint and a base projection, and
  :func:`~yanxu_harness.context.policy.validate_publication` refuses it when the scope, the
  head or the lease moved. A refused publication is *not* an error the caller can ignore: it
  falls back to the deterministic projection, so a late summary can never overwrite newer
  input.
* **The budget is durable, not per-process.** Calls, characters and usage are recorded as
  plain data and committed with the projection facts; a resume continues from the recorded
  numbers instead of getting a fresh allowance, and an unconfirmed cost stays *unknown*
  rather than becoming zero.
* **The budget belongs to one logical run.** The compactor is deployment-level and serves
  several runs at once, so the numbers live in a per-run state object keyed by the same
  scope the projection is isolated by. Two independent runs cannot spend each other's
  allowance, and a result carries an immutable snapshot of the usage it was prepared
  against rather than reading "the last usage the compactor saw".
* **A failure is bounded.** Timeout, refusal and a provider error all leave the original
  messages untouched and fall back to the deterministic projection — which may itself
  refuse, and that refusal is the honest answer. External *cancellation* is different: it is
  a control signal, so it keeps propagating after the auxiliary task has been collected and
  its cost accounted for, and it never becomes a "successful" summary.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Sequence

from ..errors import ContractError
from ..versioning import fingerprint
from .measurement import RequestBudget, TokenEstimate
from .policy import SummaryArtifact, SummaryResult, Summarizer, validate_publication
from .projection import (
    INSTRUCTIONS,
    FragmentProjector,
    ProjectionResult,
    message_fragments,
)

__all__ = [
    "CompactionResult",
    "SUMMARY_FACTS_VERSION",
    "SummaryBudget",
    "SummaryUsage",
    "SummaryCompactor",
]

#: Bumped when the shape of the committed ``context_state["summary"]`` facts changes in a
#: way an older reader could misread. A record *without* the field is an older run whose
#: numbers are still valid: absence means "no identity was recorded", not "version zero".
SUMMARY_FACTS_VERSION = 2

#: How long a call that no waiter needs any more is given to notice its cancellation. It is
#: bounded on purpose: a third-party summarizer that refuses to be cancelled cannot be
#: forced, and the run must not be held hostage by one.
_CLEANUP_GRACE_SECONDS = 1.0

#: The slot a compactor used directly (without a controller) restores its prior into. The
#: first ``compact`` that names a scope adopts it, which is how the older single-run API keeps
#: working without silently sharing numbers between runs.
_LEGACY_SCOPE = "\x00legacy"


@dataclass(frozen=True)
class SummaryBudget:
    """How much auxiliary work one logical run may spend on summaries.

    Deliberately *absolute* per run, like the turn budget: reaching the cap means no more
    summaries for this run, not "start again on the next attempt". A resumed attempt reads
    the spent numbers from the committed projection facts, so pausing cannot buy a fresh
    allowance.
    """

    max_calls: int = 1
    max_input_chars: int = 40_000
    max_output_chars: int = 4_000
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.max_calls < 0:
            raise ContractError("max_calls must not be negative")
        if self.max_input_chars <= 0 or self.max_output_chars <= 0:
            raise ContractError("the summary character limits must be positive")
        if self.timeout_seconds <= 0:
            raise ContractError("timeout_seconds must be positive")

    def allows(self, usage: "SummaryUsage", *, reserved: int = 0) -> tuple[bool, str]:
        """Whether another summary call is allowed, and why not when it is not.

        ``reserved`` counts calls that have already been admitted but whose cost has not
        settled yet. It is part of the check on purpose: two concurrent preparers of the same
        run must not both see the last unspent call.
        """
        if usage.calls + max(0, reserved) >= self.max_calls:
            return False, (
                f"the summary budget of {self.max_calls} call(s) for this run is spent"
            )
        if usage.input_chars >= self.max_input_chars:
            return False, "the summary input budget for this run is spent"
        if usage.output_chars >= self.max_output_chars:
            return False, "the summary output budget for this run is spent"
        return True, ""


@dataclass(frozen=True)
class SummaryUsage:
    """What the auxuliary path has spent so far. Plain data, committed with the facts."""

    calls: int = 0
    input_chars: int = 0
    output_chars: int = 0
    #: Whatever the host's summarizer reported, recorded separately from the main model.
    model_usage: Mapping[str, Any] = field(default_factory=dict)
    #: True when at least one call's cost was not confirmed. Never rendered as zero.
    usage_unknown: bool = False
    #: The head the last accounted call was made against, so restoring is idempotent.
    accounted_head: str = ""
    #: The summary texts that were published, by source range fingerprint.
    published: Mapping[str, str] = field(default_factory=dict)

    def record(
        self,
        *,
        input_chars: int,
        output_chars: int,
        usage: Mapping[str, Any],
        head: str,
        published_range: str = "",
        published_text: str = "",
    ) -> "SummaryUsage":
        published = dict(self.published)
        if published_range:
            published[published_range] = published_text
        # A summarizer that reports nothing is *unknown*, not free: the request happened.
        missing = not usage or not any(
            usage.get(key) is not None
            for key in ("input_tokens", "output_tokens", "total_tokens", "input", "output")
        )
        return SummaryUsage(
            calls=self.calls + 1,
            input_chars=self.input_chars + max(0, input_chars),
            output_chars=self.output_chars + max(0, output_chars),
            model_usage=dict(usage or {}),
            usage_unknown=self.usage_unknown or missing,
            accounted_head=head,
            published=published,
        )

    def as_dict(self) -> Mapping[str, Any]:
        return {
            "calls": self.calls,
            "input_chars": self.input_chars,
            "output_chars": self.output_chars,
            "model_usage": dict(self.model_usage),
            "usage_unknown": self.usage_unknown,
            "accounted_head": self.accounted_head,
            "published_ranges": sorted(self.published),
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any] | None) -> "SummaryUsage":
        """Read recorded usage back, so a resume continues instead of restarting.

        A record without the fields is an older run with no summary usage: zero calls is
        then *true*, not a reset, because nothing was ever recorded as spent.
        """
        if not record:
            return cls()
        return cls(
            calls=int(record.get("calls") or 0),
            input_chars=int(record.get("input_chars") or 0),
            output_chars=int(record.get("output_chars") or 0),
            model_usage=dict(record.get("model_usage") or {}),
            usage_unknown=bool(record.get("usage_unknown")),
            accounted_head=str(record.get("accounted_head") or ""),
            published=dict(record.get("published") or {}),
        )


def _message_payload(message: Any) -> Any:
    """A fingerprintable form of one message, for the summary cache key."""
    from ..core.types import message_to_json

    if isinstance(message, Mapping):
        return dict(message)
    return message_to_json(message)


def _replayed_baseline(messages: Sequence[Any]) -> Any:
    """One system message replaying the effective state of the messages it replaces."""
    from ..core.messages import get_current_system_message
    from ..core.types import SystemMessage

    current = get_current_system_message(messages)
    if current is None:
        return None
    return SystemMessage(
        content=current.content,
        sections=dict(current.sections) if current.sections else None,
        tools_added=list(current.tools_added) if current.tools_added else None,
        timestamp=current.timestamp,
    )


@dataclass(frozen=True)
class CompactionResult:
    """The request view, plus what was done to produce it.

    ``summary_usage`` is an immutable snapshot of the run's accounting as of *this*
    preparation. It travels with the result so the committed facts, the scope and the
    measurement all come from the same moment instead of from "the last usage the shared
    compactor happened to hold".
    """

    projection: ProjectionResult
    messages: tuple[Any, ...]
    estimate: TokenEstimate
    fits: bool | None
    used_summary: bool = False
    summary_text: str = ""
    summary_range: str = ""
    cache_hit: bool = False
    fallback: str = ""
    refused: str = ""
    summary_usage: "SummaryUsage | None" = None

    @property
    def outbound(self) -> bool:
        return not self.refused


@dataclass
class _SharedCall:
    """One auxiliary request, shared by the preparers waiting on the same source range."""

    key: str
    input_chars: int
    head: str
    task: "asyncio.Task[Any]"
    waiters: int = 1


@dataclass
class _SummaryRun:
    """One logical run's summary state: spent, reserved, and in flight.

    A deployment-level compactor serves several runs at once, and a budget shared between
    them is not a budget. The run is identified by the same scope the projection is isolated
    by — ``namespace/session/run`` — so a new attempt of the same run continues its own
    numbers while a different run starts its own.
    """

    scope: str
    usage: SummaryUsage = field(default_factory=SummaryUsage)
    #: Calls admitted but not settled. Counted by the budget check, so two preparers of the
    #: same run cannot both spend the last call.
    reserved_calls: int = 0
    in_flight: dict[str, _SharedCall] = field(default_factory=dict)


@dataclass
class SummaryCompactor:
    """Summarize a stable prefix when the deterministic projection does not fit.

    ``summarizer`` is the host's adapter over its own model — the package ships no default,
    because "call a model to shrink the prompt" is a deployment decision with a cost and a
    data-flow consequence. The compactor itself is deterministic: same inputs, same choice of
    what to summarize, and every branch is recorded in the result.
    """

    projector: FragmentProjector
    summarizer: Summarizer | None = None
    budget: SummaryBudget = field(default_factory=SummaryBudget)
    #: How many of the newest fragments are never summarized. The newest one is required by
    #: definition (the current input or the round trip awaiting its answer); one more keeps a
    #: little verbatim context around it.
    keep_verbatim: int = 2
    #: Bound on the single-flight/cache table: a long-lived process must not grow it forever.
    cache_limit: int = 32
    #: Bound on the per-run state table. Eviction only touches a run that has nothing in
    #: flight, and never refunds anything: a re-entering run restores its recorded numbers.
    run_limit: int = 64
    _cache: dict[str, str] = field(default_factory=dict, repr=False)
    _runs: dict[str, _SummaryRun] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.keep_verbatim < 1:
            raise ContractError("keep_verbatim must be at least 1: the current turn is never summarized")
        if self.cache_limit <= 0:
            raise ContractError("cache_limit must be positive")
        if self.run_limit <= 0:
            raise ContractError("run_limit must be positive")

    # -- run state ------------------------------------------------------

    @property
    def usage(self) -> SummaryUsage:
        """The single logical run's usage, for the older single-run API.

        A compactor that already serves more than one run has no "the" usage to return;
        ``usage_for(scope)`` is then the honest call, and guessing one would be exactly the
        cross-run accounting this type exists to prevent.
        """
        states = list(self._runs.values())
        if not states:
            return SummaryUsage()
        if len(states) == 1:
            return states[0].usage
        raise ContractError(
            "this compactor serves several logical runs; read usage_for(scope) rather than a "
            "single usage"
        )

    def usage_for(self, scope: str) -> SummaryUsage | None:
        """The settled usage of one logical run, or ``None`` when it has no state yet."""
        state = self._runs.get(scope)
        return None if state is None else state.usage

    def reported_usage_for(self, scope: str) -> SummaryUsage | None:
        """One run's usage as it should be read *now*: settled spend plus calls in flight.

        A reserved call may already have reached the provider, so it must not read as "not
        called". Its cost is unconfirmed until it settles, which is ``usage_unknown`` rather
        than zero.
        """
        state = self._runs.get(scope)
        return None if state is None else self._reported_usage(state)

    def release_run(self, scope: str) -> bool:
        """Drop one run's in-memory state once its facts are durable elsewhere.

        Releasing frees memory; it never refunds. A run that re-enters restores the numbers
        it recorded through :meth:`restore`, so forgetting them here cannot buy a fresh
        allowance. A run that still has a call in flight is kept: its cost is not settled.
        """
        state = self._runs.get(scope)
        if state is None:
            return False
        if state.in_flight or state.reserved_calls:
            return False
        del self._runs[scope]
        return True

    def _state_for(self, scope: str) -> _SummaryRun:
        state = self._runs.get(scope)
        if state is not None:
            return state
        if scope != _LEGACY_SCOPE:
            # A compactor used directly restores its prior without naming a scope; the first
            # compact() adopts that run rather than starting a second one.
            legacy = self._runs.pop(_LEGACY_SCOPE, None)
            if legacy is not None and not self._runs:
                legacy.scope = scope
                self._runs[scope] = legacy
                return legacy
            if legacy is not None:
                self._runs[_LEGACY_SCOPE] = legacy
        self._prune()
        state = _SummaryRun(scope=scope)
        self._runs[scope] = state
        return state

    def _prune(self) -> None:
        """Bound the run table without ever dropping a call that is still being accounted."""
        if len(self._runs) < self.run_limit:
            return
        for key, state in list(self._runs.items()):
            if key == _LEGACY_SCOPE or state.in_flight or state.reserved_calls:
                continue
            del self._runs[key]
            return

    # -- the entry point ------------------------------------------------

    async def compact(
        self,
        messages: Sequence[Any],
        *,
        scope: str,
        source_head: str,
        capacity: int | None,
        mandatory: Sequence[Any] = (),
        dispatch_budget: Any | None = None,
        dispatch_context: Mapping[str, Any] | None = None,
    ) -> CompactionResult:
        """Return the best view this compactor can honestly build for one request.

        The order is the plan's order, and each step is a reason *not* to call a model:

        1. the deterministic projection is computed first, always;
        2. if nothing had to be dropped, it already fits and there is nothing to retain, so
           the answer is the baseline — no auxiliary request (this is what makes "summaries
           are off unless they are needed" checkable);
        3. otherwise a stable prefix exists, the budget is consulted, the cache is consulted;
        4. only then is the summarizer called, and whatever it does — success, failure,
           timeout, a late publication — is recorded in the result.
        """
        state = self._state_for(scope)
        result = await self._compact(
            state,
            messages,
            scope=scope,
            source_head=source_head,
            capacity=capacity,
            mandatory=mandatory,
            dispatch_budget=dispatch_budget,
            dispatch_context=dispatch_context,
        )
        # Bind the accounting to this preparation: the facts committed with a view must
        # describe this run at this moment, not whatever the shared compactor saw last.
        return replace(result, summary_usage=self._reported_usage(state))

    async def _compact(
        self,
        state: _SummaryRun,
        messages: Sequence[Any],
        *,
        scope: str,
        source_head: str,
        capacity: int | None,
        mandatory: Sequence[Any],
        dispatch_budget: Any | None,
        dispatch_context: Mapping[str, Any] | None,
    ) -> CompactionResult:
        baseline = self.projector.plan(
            messages,
            scope=scope,
            source_head=source_head,
            capacity=capacity,
            mandatory=mandatory,
        )
        if not baseline.dropped:
            # Everything the request needs is already there: an auxiliary model call here
            # would spend money to replace history that is not being lost.
            return CompactionResult(
                projection=baseline,
                messages=baseline.messages,
                estimate=baseline.estimate,
                fits=baseline.fits,
            )
        if self.summarizer is None:
            return self._baseline(
                baseline, "no summarizer is configured for this run"
            )

        fragments = message_fragments(messages)
        required = self._required_flags(fragments, mandatory)
        first_required = next((index for index, flag in enumerate(required) if flag), len(fragments))
        cut = min(first_required, max(0, len(fragments) - self.keep_verbatim))
        prefix = [message for fragment in fragments[:cut] for message in fragment.messages]
        if not prefix:
            return self._baseline(
                baseline,
                "everything outside the verbatim tail is required, so nothing may be "
                "summarized away",
            )

        window = fingerprint({"messages": [_message_payload(message) for message in prefix]})
        # The cache key includes the scope. The summary text is derived only from the
        # messages, so an identical prefix would in fact produce the same text — but the plan
        # requires that a cache never crosses a namespace boundary, and a key that cannot
        # express the boundary is a bug waiting for a host to reuse a controller across two
        # deployments.
        cache_key = fingerprint({"scope": scope, "window": window})
        # The cache is consulted *before* the budget: reusing a summary that was already paid
        # for costs nothing, so a spent allowance must not refuse it.
        cached = self._cache.get(cache_key)
        if cached is not None:
            return self._publish(
                baseline,
                messages,
                prefix,
                cached,
                window,
                cache_hit=True,
                state=state,
                mandatory=mandatory,
            )

        input_chars = sum(len(self.projector.visible_text_of(message)) for message in prefix)
        if input_chars > self.budget.max_input_chars:
            return self._baseline(
                baseline,
                "the summary request failed validation: the stable prefix is "
                f"{input_chars} characters, over the {self.budget.max_input_chars} character "
                "limit for one summary request",
            )

        # Admission and reservation happen in one synchronous step, before the first await
        # that could let another preparer of the same run read the same unspent allowance.
        call, refusal = self._admit(
            state,
            cache_key,
            prefix=prefix,
            scope=scope,
            head=source_head,
            input_chars=input_chars,
            dispatch_budget=dispatch_budget,
            dispatch_context=dispatch_context,
        )
        if refusal is not None:
            return self._baseline(baseline, refusal)
        assert call is not None
        try:
            summary = await self._await_call(call, timeout=self.budget.timeout_seconds)
        except asyncio.TimeoutError:
            return self._baseline(
                baseline,
                "the summary request timed out; the original messages are unchanged",
            )
        except asyncio.CancelledError:
            # Cancellation keeps the original data and is *not* swallowed: the auxiliary task
            # has been collected and accounted for, and the caller's own cancellation
            # semantics decide what happens next.
            raise
        except Exception as failure:  # noqa: BLE001 - a failed summary keeps the original data
            return self._baseline(
                baseline,
                f"the summary request failed ({type(failure).__name__}: {failure}); the "
                "original messages are unchanged",
            )
        text = summary.text
        if not text.strip():
            return self._baseline(baseline, "the summarizer returned nothing usable")
        if len(text) > self.budget.max_output_chars:
            return self._baseline(
                baseline,
                f"the summarizer returned {len(text)} characters, over the "
                f"{self.budget.max_output_chars} character limit for this run",
            )
        self._remember(cache_key, text)
        return self._publish(
            baseline,
            messages,
            prefix,
            text,
            window,
            cache_hit=False,
            state=state,
            mandatory=mandatory,
        )

    # -- the auxiliary call ---------------------------------------------

    def _admit(
        self,
        state: _SummaryRun,
        key: str,
        *,
        prefix: Sequence[Any],
        scope: str,
        head: str,
        input_chars: int,
        dispatch_budget: Any | None,
        dispatch_context: Mapping[str, Any] | None,
    ) -> tuple["_SharedCall | None", "str | None"]:
        """Admit one call, reserving it before any external await.

        Synchronous on purpose: asyncio runs one coroutine at a time, so checking the budget
        and reserving the call in the same uninterrupted step is what makes ``max_calls=1``
        mean one call even when two preparers of the same run race. A request for a source
        range that is already in flight *joins* that call instead of starting a second one.
        """
        existing = state.in_flight.get(key)
        if existing is not None:
            existing.waiters += 1
            return existing, None
        allowed, why = self.budget.allows(state.usage, reserved=state.reserved_calls)
        if not allowed:
            return None, why
        assert self.summarizer is not None
        task = asyncio.ensure_future(
            self._run_shared(
                state,
                key,
                prefix,
                scope=scope,
                head=head,
                input_chars=input_chars,
                dispatch_budget=dispatch_budget,
                dispatch_context=dispatch_context,
            )
        )
        call = _SharedCall(key=key, input_chars=input_chars, head=head, task=task)
        state.in_flight[key] = call
        state.reserved_calls += 1
        return call, None

    async def _run_shared(
        self,
        state: _SummaryRun,
        key: str,
        prefix: Sequence[Any],
        *,
        scope: str,
        head: str,
        input_chars: int,
        dispatch_budget: Any | None,
        dispatch_context: Mapping[str, Any] | None,
    ) -> SummaryResult:
        """Perform one auxiliary request and settle it exactly once.

        Settlement lives here, not in the waiter, because several preparers can share one
        call: the call is accounted for once, by whoever observed it finish, and a call that
        was made never gets its allowance back — cancellation, timeout and provider failure
        included.
        """
        assert self.summarizer is not None
        reservation: Any = None
        dispatch_started = False
        try:
            if dispatch_budget is not None:
                reserve = getattr(dispatch_budget, "reserve_auxiliary", None)
                if not callable(reserve):
                    raise ContractError(
                        "summary model dispatch budget must expose reserve_auxiliary"
                    )
                reservation = reserve(
                    {**dict(dispatch_context or {}), "summary_key": key},
                    kind="summary",
                )
                if hasattr(reservation, "__await__"):
                    reservation = await reservation
            dispatch_started = True
            result = await self.summarizer.summarize(prefix, scope=scope, source_head=head)
        except BaseException:
            # A call that was made is a call that may have cost something, whatever it
            # returned. The attempt is accounted for with *unknown* usage rather than being
            # forgotten, and the budget is not silently handed back.
            if not dispatch_started:
                self._settle(
                    state,
                    key,
                    input_chars=input_chars,
                    output_chars=0,
                    usage={},
                    head=head,
                    counted=False,
                )
            else:
                if dispatch_budget is not None and reservation is not None:
                    settle = getattr(dispatch_budget, "settle_auxiliary", None)
                    if callable(settle):
                        try:
                            outcome = settle(
                                reservation,
                                usage=None,
                                detail="summary model dispatch ended without a result",
                            )
                            if hasattr(outcome, "__await__"):
                                await outcome
                        except BaseException:
                            # The reservation remains reserved if the settlement reply was
                            # lost; recovery will keep its full coverage.
                            pass
                self._settle(
                    state, key, input_chars=input_chars, output_chars=0, usage={}, head=head
                )
            raise

        async def settle_budget(usage: Mapping[str, Any]) -> None:
            if dispatch_budget is None or reservation is None:
                return
            settle = getattr(dispatch_budget, "settle_auxiliary", None)
            if not callable(settle):
                raise ContractError("summary model dispatch budget must expose settle_auxiliary")
            outcome = settle(reservation, usage=usage)
            if hasattr(outcome, "__await__"):
                await outcome

        if not isinstance(result, SummaryResult):
            try:
                await settle_budget({})
            finally:
                self._settle(
                    state, key, input_chars=input_chars, output_chars=0, usage={}, head=head
                )
            raise ContractError(
                "a summarizer must return a SummaryResult so its usage and provenance can be "
                f"recorded; got {type(result).__name__}"
            )
        if result.scope not in ("", scope):
            try:
                await settle_budget(result.usage)
            finally:
                self._settle(
                    state,
                    key,
                    input_chars=input_chars,
                    output_chars=len(result.text),
                    usage=result.usage,
                    head=head,
                )
            raise ContractError(
                f"the summarizer reported scope {result.scope!r} for a {scope!r} request"
            )
        try:
            await settle_budget(result.usage)
        finally:
            self._settle(
                state,
                key,
                input_chars=input_chars,
                output_chars=len(result.text),
                usage=result.usage,
                head=head,
            )
        return result

    def _settle(
        self,
        state: _SummaryRun,
        key: str,
        *,
        input_chars: int,
        output_chars: int,
        usage: Mapping[str, Any],
        head: str,
        counted: bool = True,
    ) -> None:
        """Move one call from reserved to spent. Synchronous, so it cannot be interrupted."""
        call = state.in_flight.pop(key, None)
        if call is not None:
            state.reserved_calls = max(0, state.reserved_calls - 1)
        if not counted:
            return
        state.usage = state.usage.record(
            input_chars=input_chars, output_chars=output_chars, usage=usage, head=head
        )

    async def _await_call(self, call: _SharedCall, *, timeout: float) -> SummaryResult:
        """Wait for a shared call without letting one waiter's exit kill the others.

        The task is shielded from a waiter's own cancellation; the last waiter to leave
        cancels and collects it, so a cancelled run does not leave an auxiliary request
        running in the background.
        """
        try:
            return await asyncio.wait_for(asyncio.shield(call.task), timeout=timeout)
        finally:
            call.waiters -= 1
            if call.waiters <= 0:
                await self._reap(call)

    async def _reap(self, call: _SharedCall) -> None:
        """Cancel and collect a shared call that no waiter needs any more.

        Bounded on purpose: the cancellation is requested, and the task is awaited only long
        enough for its own accounting to run and to avoid a dangling task. A summarizer that
        refuses to be cancelled is not something this build can force, and it is not awaited
        forever.
        """
        task = call.task
        if task.done():
            with contextlib.suppress(BaseException):
                task.exception()
            return
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=_CLEANUP_GRACE_SECONDS)
        except BaseException:
            # Including this waiter's own cancellation: the point was to *request* the stop,
            # not to make the run's exit depend on the third party acknowledging it.
            pass

    def _reported_usage(self, state: _SummaryRun) -> SummaryUsage:
        """One run's usage including calls still in flight, for the facts it commits."""
        if not state.reserved_calls:
            return state.usage
        return replace(
            state.usage,
            calls=state.usage.calls + state.reserved_calls,
            usage_unknown=True,
        )

    # -- publication ----------------------------------------------------

    def _publish(
        self,
        baseline: ProjectionResult,
        messages: Sequence[Any],
        prefix: Sequence[Any],
        text: str,
        window: str,
        *,
        cache_hit: bool,
        state: "_SummaryRun | None" = None,
        mandatory: Sequence[Any] = (),
    ) -> CompactionResult:
        """Validate the publication and build the view the summary replaces the prefix with."""
        artifact = SummaryArtifact(
            text=text,
            scope=baseline.scope,
            source_head=baseline.source_head,
            base_fingerprint=window,
        )
        allowed, why = validate_publication(
            artifact, scope=baseline.scope, source_head=baseline.source_head
        )
        if not allowed:
            return self._baseline(baseline, f"the summary was refused for publication: {why}")
        if baseline.source_fingerprint != self.projector.fingerprint_messages(messages):
            return self._baseline(
                baseline,
                "the transcript moved while the summary was being produced; refusing to publish "
                "it over newer input",
            )
        tail = self._verbatim_tail(messages, mandatory=mandatory)
        tail_positions = {id(message) for message in tail}
        replaced = [message for message in messages if id(message) not in tail_positions]
        baseline_message = _replayed_baseline(replaced)
        combined = tuple(
            ([baseline_message] if baseline_message is not None else [])
            + [self._summary_message(text)]
            + list(tail)
        )
        projected, _ = self.projector.substitute(
            messages,
            combined,
            scope=baseline.scope,
            source_head=baseline.source_head,
            capacity=baseline.capacity,
            baseline_synthesized=baseline_message is not None,
        )
        if projected.fits is False:
            return self._baseline(
                baseline,
                f"the summarized view still needs {projected.estimate.tokens} tokens of "
                f"{projected.capacity} capacity",
            )
        state = self._state_for(baseline.scope) if state is None else state
        state.usage = replace(
            state.usage,
            published={**dict(state.usage.published), window: text},
        )
        return CompactionResult(
            projection=projected,
            messages=projected.messages,
            estimate=projected.estimate,
            fits=projected.fits,
            used_summary=True,
            summary_text=text,
            summary_range=window,
            cache_hit=cache_hit,
        )

    def _verbatim_tail(
        self, messages: Sequence[Any], *, mandatory: Sequence[Any] = ()
    ) -> tuple[Any, ...]:
        """Keep the newest fragments and every host-mandated fragment verbatim."""
        fragments = message_fragments(messages)
        if not fragments:
            return ()
        keep_indexes = set(range(max(0, len(fragments) - self.keep_verbatim), len(fragments)))
        keep_indexes.update(
            index
            for index, required in enumerate(self._required_flags(fragments, mandatory))
            if required
        )
        return tuple(
            message
            for index, fragment in enumerate(fragments)
            if index in keep_indexes
            for message in fragment.messages
        )

    def _summary_message(self, text: str) -> Any:
        """The summary travels as *material*, never as a system message.

        A summary is model-written text about the conversation. If it were a system message
        it would enter the effective system prompt, and instruction-like text inside it would
        gain system authority — which the plan forbids (section 3.4). As a user message it is
        what it is: something to read, with no more standing than any other content, and the
        effective instruction/tool state stays exactly what the transcript says it is.
        """
        from ..core.types import UserMessage

        return UserMessage(
            content=(
                "[summary of earlier conversation, produced by a separate model call and "
                "validated against this transcript's head; the original messages are still "
                "stored and this text grants nothing]\n" + text
            )
        )

    def _baseline(self, baseline: ProjectionResult, reason: str) -> CompactionResult:
        """The deterministic answer plus why no summary was used instead.

        A fallback is not a failure of the run: the deterministic projection is a complete
        answer (possibly a refusal, when even the minimum does not fit). What matters is that
        the reason is recorded rather than the original data being distorted to avoid it.
        """
        return CompactionResult(
            projection=baseline,
            messages=baseline.messages,
            estimate=baseline.estimate,
            fits=baseline.fits,
            fallback=reason,
            refused="" if baseline.fits is not False else reason,
        )

    def _required_flags(
        self, fragments: Sequence[Any], mandatory: Sequence[Any]
    ) -> tuple[bool, ...]:
        """Which fragments must be sent verbatim: the newest one, plus host-mandated ones."""
        flags = [False] * len(fragments)
        if fragments:
            flags[-1] = True
        if mandatory:
            wanted = tuple(mandatory)
            for index, fragment in enumerate(fragments):
                for message in fragment.messages:
                    if any(message is item or message == item for item in wanted):
                        flags[index] = True
                        break
        return tuple(flags)

    def _remember(self, window: str, text: str) -> None:
        if len(self._cache) >= self.cache_limit and window not in self._cache:
            self._cache.pop(next(iter(self._cache)), None)
        self._cache[window] = text

    # -- durable accounting ---------------------------------------------

    def restore(self, prior: Mapping[str, Any] | None, *, scope: str | None = None) -> bool:
        """Continue one run's recorded summary budget. Idempotent for the same head.

        Called with the committed projection facts when a run is resumed. It never *resets*
        usage within that run: a run whose last summary call was unconfirmed keeps
        ``usage_unknown`` set, and a spent allowance stays spent. The numbers of a *different*
        run are not comparable, so a smaller count only refuses to rewind the same run rather
        than becoming a global maximum.

        ``scope`` names the run the facts belong to. Without one, the facts are bound to the
        compactor's single unnamed run, which the first ``compact`` adopts — the older
        single-run API. A compactor that already serves named runs refuses that ambiguous call
        instead of guessing which run the facts describe, and facts that carry an explicit
        identity are checked against the scope they are restored into.
        """
        source: Any = prior
        if isinstance(prior, Mapping) and "summary" in prior:
            source = prior.get("summary")
        recorded_scope = source.get("scope") if isinstance(source, Mapping) else None
        if isinstance(source, Mapping):
            version = source.get("summary_schema_version")
            if version is not None and int(version) > SUMMARY_FACTS_VERSION:
                raise ContractError(
                    f"the recorded summary facts are version {version}, newer than this build "
                    f"understands ({SUMMARY_FACTS_VERSION}); refusing to interpret them"
                )
        if scope is None:
            # The older single-run entry point: the facts are bound to the compactor's one
            # unnamed run, and the first ``compact`` adopts it. More than one named run and
            # there is no honest answer to "which one?", so it refuses instead of guessing.
            named = [key for key in self._runs if key != _LEGACY_SCOPE]
            if len(named) > 1:
                raise ContractError(
                    "this compactor serves several named runs, so a restore without a scope "
                    "cannot say which one the facts belong to; pass scope=..."
                )
            if recorded_scope:
                target = str(recorded_scope)
                if named and named[0] != target:
                    raise ContractError(
                        f"the recorded summary facts belong to run {target!r}, but this "
                        f"compactor serves {named[0]!r}; pass scope=... instead of guessing"
                    )
            else:
                target = named[0] if named else _LEGACY_SCOPE
        else:
            if recorded_scope and recorded_scope != scope:
                raise ContractError(
                    f"the recorded summary facts belong to run {recorded_scope!r}, not to "
                    f"{scope!r}; refusing to move them between runs"
                )
            target = scope
        state = self._state_for(target)
        restored = SummaryUsage.from_record(source)
        if restored.accounted_head == state.usage.accounted_head and restored.calls == state.usage.calls:
            return False
        if restored.calls < state.usage.calls:
            # An older prior must not rewind what this active run has already spent.
            return False
        state.usage = restored
        return True

    @property
    def single_flight(self) -> Mapping[str, Any]:
        """The in-flight table (one entry per source range), for diagnostics only."""
        return {
            key: call.task
            for state in self._runs.values()
            for key, call in state.in_flight.items()
        }
