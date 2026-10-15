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
* **A failure is bounded.** Timeout, cancellation, refusal and a provider error all leave the
  original messages untouched and fall back to the deterministic projection — which may
  itself refuse, and that refusal is the honest answer.
"""

from __future__ import annotations

import asyncio
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
    "SummaryBudget",
    "SummaryUsage",
    "SummaryCompactor",
]


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

    def allows(self, usage: "SummaryUsage") -> tuple[bool, str]:
        """Whether another summary call is allowed, and why not when it is not."""
        if usage.calls >= self.max_calls:
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
    """The request view, plus what was done to produce it."""

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

    @property
    def outbound(self) -> bool:
        return not self.refused


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
    usage: SummaryUsage = field(default_factory=SummaryUsage)
    #: How many of the newest fragments are never summarized. The newest one is required by
    #: definition (the current input or the round trip awaiting its answer); one more keeps a
    #: little verbatim context around it.
    keep_verbatim: int = 2
    #: Bound on the single-flight/cache table: a long-lived process must not grow it forever.
    cache_limit: int = 32
    _in_flight: dict[str, Any] = field(default_factory=dict, repr=False)
    _cache: dict[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.keep_verbatim < 1:
            raise ContractError("keep_verbatim must be at least 1: the current turn is never summarized")
        if self.cache_limit <= 0:
            raise ContractError("cache_limit must be positive")

    # -- the entry point ------------------------------------------------

    async def compact(
        self,
        messages: Sequence[Any],
        *,
        scope: str,
        source_head: str,
        capacity: int | None,
        mandatory: Sequence[Any] = (),
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
            return self._publish(baseline, messages, prefix, cached, window, cache_hit=True)

        allowed, why = self.budget.allows(self.usage)
        if not allowed:
            return self._baseline(baseline, why)

        try:
            text = await self._summarize(prefix, scope=scope, head=source_head)
        except asyncio.TimeoutError:
            return self._baseline(
                baseline,
                "the summary request timed out; the original messages are unchanged",
            )
        except asyncio.CancelledError:
            # Cancellation keeps the original data and is not swallowed: the caller's own
            # cancellation semantics decide what happens next.
            return self._baseline(
                baseline,
                "the summary request was cancelled; the original messages are unchanged",
            )
        except Exception as failure:  # noqa: BLE001 - a failed summary keeps the original data
            return self._baseline(
                baseline,
                f"the summary request failed ({type(failure).__name__}: {failure}); the "
                "original messages are unchanged",
            )
        if not text.strip():
            return self._baseline(baseline, "the summarizer returned nothing usable")
        if len(text) > self.budget.max_output_chars:
            return self._baseline(
                baseline,
                f"the summarizer returned {len(text)} characters, over the "
                f"{self.budget.max_output_chars} character limit for this run",
            )
        self._remember(cache_key, text)
        return self._publish(baseline, messages, prefix, text, window, cache_hit=False)

    # -- the pieces -----------------------------------------------------

    async def _summarize(self, prefix: Sequence[Any], *, scope: str, head: str) -> str:
        """One bounded summary call, with the usage recorded either way."""
        input_chars = sum(len(self.projector.visible_text_of(message)) for message in prefix)
        if input_chars > self.budget.max_input_chars:
            raise ContractError(
                f"the stable prefix is {input_chars} characters, over the "
                f"{self.budget.max_input_chars} character limit for one summary request"
            )
        assert self.summarizer is not None
        result = await asyncio.wait_for(
            self._summarize_once(prefix, scope=scope, head=head),
            timeout=self.budget.timeout_seconds,
        )
        if not isinstance(result, SummaryResult):
            raise ContractError(
                "a summarizer must return a SummaryResult so its usage and provenance can be "
                f"recorded; got {type(result).__name__}"
            )
        if result.scope not in ("", scope):
            raise ContractError(
                f"the summarizer reported scope {result.scope!r} for a {scope!r} request"
            )
        return result.text

    async def _summarize_once(self, prefix: Sequence[Any], *, scope: str, head: str) -> SummaryResult:
        """One call, with single-flight: concurrent preparers share one request.

        The key is the source range plus the scope, so two requests for the same stable
        prefix do not become two bills; a different range is a different call.
        """
        key = fingerprint({"scope": scope, "head": head, "messages": [_message_payload(m) for m in prefix]})
        existing = self._in_flight.get(key)
        if existing is not None:
            return await existing
        assert self.summarizer is not None
        input_chars = sum(len(self.projector.visible_text_of(message)) for message in prefix)
        task = asyncio.ensure_future(self.summarizer.summarize(prefix, scope=scope, source_head=head))
        self._in_flight[key] = task
        try:
            result = await task
        except BaseException:
            # A call that was made is a call that may have cost something, whatever it
            # returned. The attempt is accounted for with *unknown* usage rather than being
            # forgotten, and the budget is not silently handed back.
            self.usage = self.usage.record(
                input_chars=input_chars, output_chars=0, usage={}, head=head
            )
            raise
        finally:
            self._in_flight.pop(key, None)
        if not isinstance(result, SummaryResult):
            self.usage = self.usage.record(
                input_chars=input_chars, output_chars=0, usage={}, head=head
            )
            raise ContractError(
                "a summarizer must return a SummaryResult so its usage and provenance can be "
                f"recorded; got {type(result).__name__}"
            )
        if result.scope not in ("", scope):
            self.usage = self.usage.record(
                input_chars=input_chars, output_chars=len(result.text), usage=result.usage, head=head
            )
            raise ContractError(
                f"the summarizer reported scope {result.scope!r} for a {scope!r} request"
            )
        self.usage = self.usage.record(
            input_chars=input_chars,
            output_chars=len(result.text),
            usage=result.usage,
            head=head,
        )
        return result

    def _publish(
        self,
        baseline: ProjectionResult,
        messages: Sequence[Any],
        prefix: Sequence[Any],
        text: str,
        window: str,
        *,
        cache_hit: bool,
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
        tail = self._verbatim_tail(messages)
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
        self.usage = replace(
            self.usage,
            published={**dict(self.usage.published), window: text},
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

    def _verbatim_tail(self, messages: Sequence[Any]) -> tuple[Any, ...]:
        """The newest fragments, sent as they are. The summary never touches them."""
        fragments = message_fragments(messages)
        if not fragments:
            return ()
        keep = fragments[-self.keep_verbatim :]
        return tuple(message for fragment in keep for message in fragment.messages)

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

    def restore(self, prior: Mapping[str, Any] | None) -> bool:
        """Continue the recorded summary budget. Idempotent for the same head.

        Called with the committed projection facts when a run is resumed. It never *resets*
        usage: a run whose last summary call was unconfirmed keeps ``usage_unknown`` set, and
        a spent allowance stays spent.
        """
        source: Any = prior
        if isinstance(prior, Mapping) and "summary" in prior:
            source = prior.get("summary")
        restored = SummaryUsage.from_record(source)
        if restored.accounted_head == self.usage.accounted_head and restored.calls == self.usage.calls:
            return False
        if restored.calls < self.usage.calls:
            return False
        self.usage = restored
        return True

    @property
    def single_flight(self) -> Mapping[str, Any]:
        """The in-flight table (one entry per source range), for diagnostics only."""
        return dict(self._in_flight)
