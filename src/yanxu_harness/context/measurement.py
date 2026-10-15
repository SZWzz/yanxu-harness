"""Measure a *complete* request, and decide the budget honestly.

The archive/projection split in :mod:`yanxu_harness.context.policy` answers "what is
in the request". This module answers the next question: **does this request fit the
window, and how do we know?**

Three rules shape the code:

* **The whole request is measured**, not just the chat text: the system prompt, every
  tool schema, the messages, the per-message protocol overhead and the output
  configuration. A tool schema is usually the largest part of a request, and a
  measurement that ignores it reports a capacity nobody can rely on.
* **Unknown is a value.** An unknown window, an unknown estimate or an unknown output
  reserve is reported as ``None`` with a reason. It is never turned into a percentage,
  into zero, or into "infinite". The host decides, explicitly, whether an unknown is
  allowed to proceed (``UnknownPolicy.PROCEED``) or must refuse
  (``UnknownPolicy.REFUSE``).
* **Heuristics say they are heuristics.** The default estimator is the standard
  library counting characters, labelled as such. A tokenizer that is exact for text
  fragments is still not exact for a provider's billing of a whole request, so no
  estimator here claims "the provider will never overflow" — only "this is what we
  measured, this way, over these parts".

The final serialized bytes are a *separate* check with its own limit: see
:func:`payload_budget_gate`, which plugs into the provider's existing
``PayloadGate`` seam and runs on the bytes that would actually be sent.

Two things this module deliberately does **not** claim. A heuristic plus a safety margin
lowers the risk of an overflow; it does not promise the provider will never answer
``context_overflow``. And that provider failure keeps its own kind: a local budget
refusal and a provider-side overflow are different facts about different measurements,
so neither is rewritten into the other. The request's own output cap is resolved before
the request reaches here (``to_provider_request`` reads the model's declaration and the
options), so an absent cap means "nothing declared it" — which the report says out loud
rather than reading as zero.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable

from ..errors import BudgetExceeded, ContractError
from .policy import ContextWindow

__all__ = [
    "BudgetReport",
    "HeuristicTokenEstimator",
    "MeasurementInput",
    "RequestBudget",
    "TokenEstimate",
    "TokenEstimator",
    "UnknownPolicy",
    "payload_budget_gate",
]


class UnknownPolicy(str, Enum):
    """What a host wants done when a number is genuinely unknown."""

    #: Report the unknown and continue. The report still says ``fits is None``.
    PROCEED = "proceed"
    #: Refuse before dispatching. Nothing is sent when the request cannot be shown to fit.
    REFUSE = "refuse"


@dataclass(frozen=True)
class TokenEstimate:
    """How many tokens a request needs, how that was measured, and what is missing.

    ``tokens is None`` means "no honest number exists" — an unmeasurable part (an
    image, a custom block a host added) or an estimator that cannot see the request.
    ``parts`` holds the counts of the parts that *were* measured, so a host can see
    where the request's size comes from. ``unmeasured`` names what was skipped; a
    non-empty ``unmeasured`` always means ``tokens is None``, because a total that
    silently omits an image is worse than admitting the gap.
    """

    tokens: int | None
    method: str
    estimator_id: str = "unknown"
    estimator_version: str = "1"
    parts: Mapping[str, int] = field(default_factory=dict)
    unmeasured: tuple[str, ...] = ()
    exact: bool = False
    reason: str = ""

    def __post_init__(self) -> None:
        if self.tokens is not None and self.tokens < 0:
            raise ContractError("a token estimate must not be negative")
        if self.unmeasured and self.tokens is not None:
            raise ContractError(
                "an estimate with unmeasured parts must not report a total: the total "
                "would silently omit them"
            )
        if self.tokens is None and not self.reason and not self.unmeasured:
            raise ContractError("an unknown estimate must say why it is unknown")

    @property
    def known(self) -> bool:
        return self.tokens is not None

    def as_dict(self) -> Mapping[str, Any]:
        return {
            "tokens": self.tokens,
            "method": self.method,
            "estimator_id": self.estimator_id,
            "estimator_version": self.estimator_version,
            "parts": dict(self.parts),
            "unmeasured": list(self.unmeasured),
            "exact": self.exact,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class MeasurementInput:
    """The request as the transport can express it: exactly what is measured.

    Deliberately not tied to a provider class: these five fields are what every
    chat-completions-shaped request already has, and :meth:`from_request` reads them
    from the real request object, so the measurement and the sent payload are built
    from the same values rather than from a reconstruction of them.
    """

    system: str = ""
    messages: Sequence[Mapping[str, Any]] = ()
    tools: Sequence[Any] = ()
    max_output_tokens: int | None = None
    model: str = ""

    @classmethod
    def from_request(cls, request: Any) -> "MeasurementInput":
        return cls(
            system=str(getattr(request, "system", "") or ""),
            messages=tuple(getattr(request, "messages", ()) or ()),
            tools=tuple(getattr(request, "tools", ()) or ()),
            max_output_tokens=_optional_positive_int(getattr(request, "max_output_tokens", None)),
            model=str(getattr(request, "model", "") or ""),
        )


def _optional_positive_int(value: Any) -> int | None:
    if type(value) is int and value > 0:
        return value
    return None


@runtime_checkable
class TokenEstimator(Protocol):
    """Turn a complete request into a :class:`TokenEstimate`.

    Implementations must not read the environment, call a network, or claim exactness
    they cannot support; an estimator that cannot measure a part reports it in
    ``unmeasured`` instead of counting it as zero.
    """

    estimator_id: str
    estimator_version: str
    method: str

    def measure(self, request: MeasurementInput) -> TokenEstimate:
        """Measure ``request``. Pure and synchronous; no I/O."""


@dataclass(frozen=True)
class HeuristicTokenEstimator:
    """The default estimator: the standard library counting characters.

    Conservative and explicitly *not* exact. ``chars_per_token`` is a rough average
    for Latin prose; CJK text, code, JSON schemas and base64 image payloads all have
    different ratios, which is why every estimate from this class carries
    ``exact=False`` and ``method="heuristic-chars-per-token"``. A host that needs
    better numbers injects its own estimator; nothing here pretends the ratio is a
    provider fact.
    """

    chars_per_token: int = 4
    per_message_overhead: int = 4
    per_tool_overhead: int = 8
    estimator_id: str = "stdlib-heuristic"
    estimator_version: str = "1"
    method: str = "heuristic-chars-per-token"

    def __post_init__(self) -> None:
        if self.chars_per_token <= 0:
            raise ContractError("chars_per_token must be positive")
        if self.per_message_overhead < 0 or self.per_tool_overhead < 0:
            raise ContractError("protocol overhead must not be negative")

    def measure(self, request: MeasurementInput) -> TokenEstimate:
        parts: dict[str, int] = {}
        unmeasured: list[str] = []
        parts["system"] = self._text(request.system)
        tool_tokens = 0
        for index, tool in enumerate(request.tools):
            payload = _tool_payload(tool)
            if payload is None:
                unmeasured.append(f"tools[{index}]")
                continue
            tool_tokens += self._text(payload) + self.per_tool_overhead
        parts["tools"] = tool_tokens
        message_tokens = 0
        for index, message in enumerate(request.messages):
            size, missing = self._message(message)
            if missing:
                unmeasured.append(f"messages[{index}]: {missing}")
                continue
            message_tokens += size + self.per_message_overhead
        parts["messages"] = message_tokens
        parts["protocol"] = self.per_message_overhead * len(request.messages) + self.per_tool_overhead * len(request.tools)
        if unmeasured:
            return TokenEstimate(
                tokens=None,
                method=self.method,
                estimator_id=self.estimator_id,
                estimator_version=self.estimator_version,
                parts=parts,
                unmeasured=tuple(unmeasured),
                exact=False,
                reason=(
                    "this estimator cannot count every part of the request and refuses "
                    "to report a total that omits them"
                ),
            )
        return TokenEstimate(
            tokens=sum(parts.values()),
            method=self.method,
            estimator_id=self.estimator_id,
            estimator_version=self.estimator_version,
            parts=parts,
            exact=False,
            reason="heuristic character count; not a provider billing figure",
        )

    # -- parts ----------------------------------------------------------

    def _text(self, text: str) -> int:
        if not text:
            return 0
        # Ceiling division: a ten-character fragment is not zero tokens.
        return -(-len(text) // self.chars_per_token)

    def _message(self, message: Mapping[str, Any]) -> tuple[int, str]:
        content = message.get("content")
        size = 0
        if isinstance(content, str):
            size += self._text(content)
        elif isinstance(content, Sequence):
            for block in content:
                kind = block.get("type") if isinstance(block, Mapping) else None
                if kind == "text":
                    size += self._text(str(block.get("text", "")))
                elif kind == "image_url":
                    return 0, "image_url"
                else:
                    return 0, f"block {kind!r}"
        elif content is not None:
            return 0, f"content {type(content).__name__}"
        for call in message.get("tool_calls") or ():
            function = call.get("function") or {}
            size += self._text(json.dumps(function, ensure_ascii=False, default=str))
        # Continuation material is re-sent only when the host asked for it, but when it
        # is sent it is part of the request and must be counted like anything else.
        for key in ("reasoning_content", "signature"):
            value = message.get(key)
            if isinstance(value, str):
                size += self._text(value)
        return size, ""


def _tool_payload(tool: Any) -> str | None:
    """The wire JSON of one tool declaration, or ``None`` when it is not expressible."""
    if isinstance(tool, Mapping):
        name = tool.get("name")
        description = tool.get("description")
        parameters = tool.get("parameters")
        strict = tool.get("strict")
    else:
        name = getattr(tool, "name", None)
        description = getattr(tool, "description", None)
        parameters = getattr(tool, "parameters", None)
        strict = getattr(tool, "strict", None)
    if not isinstance(name, str) or not name:
        return None
    return json.dumps(
        {"name": name, "description": description, "parameters": parameters, "strict": strict},
        ensure_ascii=False,
        default=str,
    )


@dataclass(frozen=True)
class BudgetReport:
    """The measured request, the capacity it was measured against, and the verdict."""

    fits: bool | None
    capacity: int | None
    estimated_input_tokens: int | None
    output_reserve: int
    output_reserve_source: str
    safety_margin: int
    payload_bytes: int | None
    payload_limit: int | None
    fits_payload: bool | None
    reason: str
    estimate: TokenEstimate

    @property
    def known(self) -> bool:
        return self.fits is not None

    def as_dict(self) -> Mapping[str, Any]:
        return {
            "fits": self.fits,
            "capacity": self.capacity,
            "estimated_input_tokens": self.estimated_input_tokens,
            "output_reserve": self.output_reserve,
            "output_reserve_source": self.output_reserve_source,
            "safety_margin": self.safety_margin,
            "payload_bytes": self.payload_bytes,
            "payload_limit": self.payload_limit,
            "fits_payload": self.fits_payload,
            "reason": self.reason,
            "estimate": self.estimate.as_dict(),
        }


@dataclass(frozen=True)
class RequestBudget:
    """The window plus the two independent limits, and what to do with an unknown.

    ``capacity`` is ``max(0, limit - reserve - safety_margin)`` where ``reserve`` is
    the effective output reservation — the larger of the policy's reserve and the
    request's own ``max_output_tokens`` — deducted **once**. The byte limit is checked
    against the serialized payload and is not derived from the token estimate.
    """

    window: ContextWindow
    max_payload_bytes: int | None = None
    on_unknown: UnknownPolicy = UnknownPolicy.PROCEED

    def __post_init__(self) -> None:
        if self.max_payload_bytes is not None and self.max_payload_bytes <= 0:
            raise ContractError("max_payload_bytes must be positive when declared")
        if not isinstance(self.on_unknown, UnknownPolicy):
            raise ContractError("on_unknown must be an UnknownPolicy")

    # -- the decision ---------------------------------------------------

    def effective_reserve(self, request_max_output_tokens: int | None) -> tuple[int, str]:
        """The output reservation to deduct, and where it came from.

        A request that declares its own cap is authoritative; otherwise the policy's
        reserve applies. A policy reserve of zero with no declared cap means *the
        reserve is unknown* — not that the model will produce nothing — so the source
        says so and a strict host refuses instead of assuming full capacity.
        """
        declared = _optional_positive_int(request_max_output_tokens)
        if declared is not None:
            return max(self.window.output_reserve, declared), "request"
        if self.window.output_reserve > 0:
            return self.window.output_reserve, "policy"
        return 0, "unknown"

    def effective_window(self, request_max_output_tokens: int | None) -> tuple[ContextWindow, str]:
        """The window with the effective output reservation, and where it came from."""
        reserve, source = self.effective_reserve(request_max_output_tokens)
        if reserve == self.window.output_reserve:
            return self.window, source
        return replace(self.window, output_reserve=reserve), source

    def capacity_for(self, request_max_output_tokens: int | None) -> tuple[int | None, int, str]:
        """The capacity, using the one formula that already exists on the window.

        ``ContextWindow.request_capacity`` is ``max(0, limit - reserve - margin)`` and
        ``None`` when the limit is unknown; going through it keeps this module and the
        pre-existing budget type from drifting into two different formulas.
        """
        window, source = self.effective_window(request_max_output_tokens)
        return window.request_capacity, window.output_reserve, source

    def assess(
        self,
        estimate: TokenEstimate,
        *,
        payload_bytes: int | None = None,
        request_max_output_tokens: int | None = None,
    ) -> BudgetReport:
        """Measure against both limits, and keep a decisive answer decisive.

        A *hard* failure (the tokens exceed the capacity, or the bytes exceed the byte
        limit) is ``False`` even when another input is unknown: adding an unknown
        reserve can only shrink the capacity, so a request that is already too big
        stays too big. Otherwise, if any input is unknown (the window, the estimate,
        the output reserve), the verdict is ``None`` — never ``True`` on a guess.
        """
        capacity, reserve, source = self.capacity_for(request_max_output_tokens)
        fits_payload: bool | None = None
        if self.max_payload_bytes is not None and payload_bytes is not None:
            fits_payload = payload_bytes <= self.max_payload_bytes

        failures: list[str] = []
        unknowns: list[str] = []
        if capacity is None:
            unknowns.append("model context window is unknown; no percentage is invented")
        else:
            if estimate.tokens is None:
                unknowns.append(estimate.reason or "the request could not be measured")
            elif estimate.tokens > capacity:
                failures.append(
                    f"the request needs {estimate.tokens} tokens but the capacity is {capacity}"
                )
            if source == "unknown":
                unknowns.append(
                    "the output reserve is unknown (no request cap and no policy reserve), so "
                    "the capacity is only an upper bound"
                )
        if fits_payload is False:
            failures.append(
                f"the payload is {payload_bytes} bytes, over the {self.max_payload_bytes} byte limit"
            )
        if failures:
            fits: bool | None = False
        elif unknowns:
            fits = None
        else:
            fits = True
        reasons = failures + unknowns
        if not reasons:
            reasons.append("the request fits the measured capacity")
        return BudgetReport(
            fits=fits,
            capacity=capacity,
            estimated_input_tokens=estimate.tokens,
            output_reserve=reserve,
            output_reserve_source=source,
            safety_margin=self.window.safety_margin,
            payload_bytes=payload_bytes,
            payload_limit=self.max_payload_bytes,
            fits_payload=fits_payload,
            reason="; ".join(reasons),
            estimate=estimate,
        )

    def require(
        self,
        estimate: TokenEstimate,
        *,
        payload_bytes: int | None = None,
        request_max_output_tokens: int | None = None,
    ) -> BudgetReport:
        """Assess and raise :class:`BudgetExceeded` when the request must not be sent.

        An unknown refuses only when the host asked for that; an unknown window is not
        silently "infinite", it is the host's explicit choice.
        """
        report = self.assess(
            estimate,
            payload_bytes=payload_bytes,
            request_max_output_tokens=request_max_output_tokens,
        )
        if report.fits is False:
            raise BudgetExceeded("context", report.reason)
        if report.fits is None and self.on_unknown is UnknownPolicy.REFUSE:
            raise BudgetExceeded("context", report.reason)
        return report


def payload_budget_gate(
    budget: RequestBudget,
    estimator: TokenEstimator,
    *,
    on_report: Callable[[BudgetReport], None] | None = None,
) -> Callable[[bytes, Any], None]:
    """A ``PayloadGate``: measure the request, check the bytes, refuse or send.

    The returned callable has the shape the provider already expects
    (``(body, request) -> None``) and is invoked on the *final* serialized body,
    immediately before the transport call, so refusing here means zero transport
    calls. It performs no I/O and starts no event loop, which is what keeps the async
    part of preparation (a summary request) out of the provider's worker thread.
    """

    def gate(body: bytes, request: Any) -> None:
        report = budget.require(
            estimator.measure(MeasurementInput.from_request(request)),
            payload_bytes=len(body),
            request_max_output_tokens=getattr(request, "max_output_tokens", None),
        )
        if on_report is not None:
            on_report(report)

    return gate
