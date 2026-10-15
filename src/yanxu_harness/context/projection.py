"""C2: a dependency-complete, deterministic projection of a transcript.

The projection answers one question — *what is the smallest request that still means
the same thing?* — and it answers it without a model, without guessing importance, and
without editing the transcript it was given.

Three rules come from the plan and shape every decision here:

* **A fragment is the unit.** An assistant turn that declares tool calls and the results
  answering those calls are one indivisible unit, associated by ``tool_call_id`` within
  that turn's own scope (ids are not assumed to be unique across a run or a history).
  Selection never cuts a fragment in half, so a request view can never contain a lone
  ``toolResult`` or an unexplained call.
* **The effective instruction and tool state is reconstructed, not merely kept.** When
  older fragments are dropped, their system/tool updates are replayed into one baseline
  message placed at the window start, and the result is *verified* to be equivalent to
  the full transcript's effective state before it is returned. A revoked tool therefore
  cannot come back to life by being dropped.
* **Same inputs, same projection.** The result is a pure function of the transcript,
  scope, source head, capacity, estimator and converter — no clock, no randomness, no
  iteration over a dict whose order can drift — and it carries the fingerprints and the
  reasons needed to explain itself.

What it deliberately does not do: it never truncates a tool's arguments or a result's
body (that is C4's bounded view, with a real artifact reference), and it never fabricates
a tool result to make the protocol look complete. When the mandatory minimum does not fit
it refuses with :class:`~yanxu_harness.errors.BudgetExceeded` instead of sending
something smaller that means something else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from ..core.messages import (
    get_current_system_message,
    get_current_system_prompt,
    get_current_tools,
)
from ..core.types import SystemMessage, declaration_to_json, message_to_json
from ..errors import BudgetExceeded, ContractError
from ..versioning import fingerprint
from .measurement import MeasurementInput, TokenEstimate, TokenEstimator

__all__ = [
    "DroppedFragment",
    "Fragment",
    "FragmentProjector",
    "ProjectionResult",
    "message_fragments",
    "visible_text",
    "system_state",
]

#: Fragment kinds, in the vocabulary the reasons and the tests use.
INSTRUCTIONS = "instructions"
TOOL_CALL = "tool-call"
TOOL_ROUNDTRIP = "tool-roundtrip"
USER_TURN = "user-turn"
ASSISTANT_TURN = "assistant-turn"
ORPHAN_RESULT = "orphan-result"


# -- reading a transcript whose messages may be dataclasses or plain payloads -------


def _role(message: Any) -> str:
    if isinstance(message, Mapping):
        return str(message.get("role") or "")
    return str(getattr(message, "role", "") or "")


def _tool_call_ids(message: Any) -> tuple[str, ...]:
    """The ids this message declares. Only an assistant turn declares calls."""
    if isinstance(message, Mapping):
        calls = message.get("tool_calls") or ()
        return tuple(str(call.get("id") or "") for call in calls if isinstance(call, Mapping))
    found: list[str] = []
    for block in getattr(message, "content", None) or ():
        if getattr(block, "type", None) == "toolCall":
            found.append(str(getattr(block, "id", "") or ""))
    return tuple(found)


def _result_call_id(message: Any) -> str | None:
    if _role(message) not in ("toolResult", "tool"):
        return None
    value = message.get("tool_call_id") if isinstance(message, Mapping) else getattr(message, "tool_call_id", None)
    return str(value) if value else None


def visible_text(message: Any) -> str:
    """The human-visible text of one message, for size hints and reasons."""
    if isinstance(message, Mapping):
        content = message.get("content")
        if isinstance(content, str):
            return content
        parts: list[str] = []
        for block in content or ():
            if isinstance(block, Mapping) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "".join(parts)
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    parts = []
    for block in content or ():
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


@dataclass(frozen=True)
class Fragment:
    """One indivisible unit of a transcript.

    ``messages`` holds the original objects, in order; ``positions`` are their indices in
    the source transcript, which is what lets a refusal name what it refused to cut.
    """

    index: int
    kind: str
    messages: tuple[Any, ...]
    positions: tuple[int, ...]
    declared_call_ids: tuple[str, ...] = ()
    answered_call_ids: tuple[str, ...] = ()

    @property
    def open_call_ids(self) -> tuple[str, ...]:
        """Declared calls whose results are not part of this fragment."""
        answered = set(self.answered_call_ids)
        return tuple(call_id for call_id in self.declared_call_ids if call_id not in answered)

    @property
    def size_hint(self) -> int:
        """Characters of visible text; a cheap hint, never a budget decision."""
        return sum(len(visible_text(message)) for message in self.messages)

    def fingerprint(self) -> str:
        return fingerprint({"messages": [_fingerprintable(message) for message in self.messages]})


def _fingerprintable(message: Any) -> Any:
    if isinstance(message, Mapping):
        return dict(message)
    return message_to_json(message)


def message_fragments(messages: Sequence[Any]) -> tuple[Fragment, ...]:
    """Group a transcript into complete fragments, preserving order.

    Contiguous system messages form one ``instructions`` fragment (their updates are
    replayed together). An assistant turn that declares calls absorbs the results that
    answer those calls — matched by id within that turn — and becomes a
    ``tool-roundtrip`` once every declared call is answered. A result with no declaring
    turn is kept where it is as an ``orphan-result``: inventing a caller would repeat the
    mistake the transcript already made.
    """
    groups: list[list[Any]] = []
    positions: list[list[int]] = []
    kinds: list[str] = []
    declared_ids: list[set[str]] = []
    open_calls: dict[str, int] = {}

    for position, message in enumerate(messages):
        role = _role(message)
        if role == "system" and kinds and kinds[-1] == INSTRUCTIONS:
            groups[-1].append(message)
            positions[-1].append(position)
            continue
        result_id = _result_call_id(message)
        if result_id is not None:
            target = open_calls.pop(result_id, None)
            if target is not None:
                groups[target].append(message)
                positions[target].append(position)
                continue
            if groups and result_id in declared_ids[-1]:
                # A second answer to a call the previous turn already declared: it belongs
                # to that turn, not to a fragment of its own.
                groups[-1].append(message)
                positions[-1].append(position)
                continue
            # A result nobody declared cannot be attached to a call: inventing a caller
            # would repeat the mistake the transcript already made.
            groups.append([message])
            positions.append([position])
            kinds.append(ORPHAN_RESULT)
            declared_ids.append(set())
            continue
        call_ids = _tool_call_ids(message)
        groups.append([message])
        positions.append([position])
        declared_ids.append(set(call_ids))
        if call_ids:
            kinds.append(TOOL_CALL)
            for call_id in call_ids:
                open_calls[call_id] = len(groups) - 1
        elif role == "user":
            kinds.append(USER_TURN)
        elif role == "system":
            kinds.append(INSTRUCTIONS)
        else:
            kinds.append(ASSISTANT_TURN)

    fragments: list[Fragment] = []
    for index, group in enumerate(groups):
        call_ids = tuple(
            call_id for message in group for call_id in _tool_call_ids(message)
        )
        answered = tuple(
            call_id for message in group if (call_id := _result_call_id(message)) is not None
        )
        kind = kinds[index]
        if kind == TOOL_CALL and call_ids and set(call_ids) <= set(answered):
            kind = TOOL_ROUNDTRIP
        fragments.append(
            Fragment(
                index=index,
                kind=kind,
                messages=tuple(group),
                positions=tuple(positions[index]),
                declared_call_ids=call_ids,
                answered_call_ids=answered,
            )
        )
    return tuple(fragments)


# -- equivalence of the effective system/tool state --------------------------------


def system_state(messages: Sequence[Any]) -> Mapping[str, Any]:
    """The effective instruction/tool state of a transcript, in plain data.

    This is the thing a projection must preserve. It is compared as data (not as
    objects) so the check cannot pass because two objects happen to be the same instance.
    """
    current = get_current_system_message(messages)
    return {
        "prompt": get_current_system_prompt(messages),
        "sections": dict(current.sections or {}) if current is not None else {},
        "tools": [declaration_to_json(tool) for tool in get_current_tools(messages)],
    }


def _baseline_message(dropped: Sequence[Any]) -> SystemMessage | None:
    """One message that replays everything the dropped fragments contributed to state."""
    current = get_current_system_message(dropped)
    if current is None:
        return None
    return SystemMessage(
        content=current.content,
        sections=dict(current.sections) if current.sections else None,
        tools_added=list(current.tools_added) if current.tools_added else None,
        timestamp=current.timestamp,
    )


def _tools_equal(left: Sequence[Any], right: Sequence[Any]) -> bool:
    """Compare tool declarations as data.

    Both sides are ``declaration_to_json`` output, so the comparison goes through the
    same canonical JSON as every other fingerprint here: a tuple and a list in a schema
    are the same declaration, and a reordered key is not a difference.
    """
    return fingerprint(list(left)) == fingerprint(list(right))


@dataclass(frozen=True)
class DroppedFragment:
    """A fragment left out of the projection, with the reason it was dropped."""

    index: int
    kind: str
    positions: tuple[int, ...]
    reason: str


@dataclass(frozen=True)
class ProjectionResult:
    """The projected request view plus everything needed to judge or explain it."""

    scope: str
    source_head: str
    messages: tuple[Any, ...]
    kept_fragments: tuple[int, ...]
    dropped: tuple[DroppedFragment, ...]
    baseline_synthesized: bool
    estimate: TokenEstimate
    capacity: int | None
    fits: bool | None
    request_fingerprint: str
    source_fingerprint: str
    effective_state_fingerprint: str
    policy_version: str = "1"
    open_call_ids: tuple[str, ...] = ()

    @property
    def removed_messages(self) -> int:
        return sum(len(fragment.positions) for fragment in self.dropped)

    def is_stale(self, source_head: str) -> bool:
        return self.source_head != source_head

    def as_dict(self) -> Mapping[str, Any]:
        return {
            "scope": self.scope,
            "source_head": self.source_head,
            "source_fingerprint": self.source_fingerprint,
            "kept_fragments": list(self.kept_fragments),
            "dropped": [
                {
                    "index": fragment.index,
                    "kind": fragment.kind,
                    "positions": list(fragment.positions),
                    "reason": fragment.reason,
                }
                for fragment in self.dropped
            ],
            "baseline_synthesized": self.baseline_synthesized,
            "capacity": self.capacity,
            "fits": self.fits,
            "request_fingerprint": self.request_fingerprint,
            "effective_state_fingerprint": self.effective_state_fingerprint,
            "policy_version": self.policy_version,
            "open_call_ids": list(self.open_call_ids),
            "estimate": self.estimate.as_dict(),
        }


@dataclass
class FragmentProjector:
    """Project a transcript into a request view with complete fragments.

    ``convert`` is the caller's real conversion into the measurement structure (the same
    one the provider will send), and ``estimator`` is the C1 port. Both are explicit
    injectables on purpose: the projection is measured with the host's own numbers, not
    with a second, private approximation of them.
    """

    estimator: TokenEstimator
    convert: Callable[[Sequence[Any]], MeasurementInput]
    policy_version: str = "1"
    #: Upper bound on cached per-fragment measurements. A cache without a bound is a leak
    #: in a long-lived process, and this one is only ever an optimisation.
    cache_limit: int = 256
    _cache: dict[str, TokenEstimate] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.estimator is None or self.convert is None:
            raise ContractError("a projector needs an estimator and a real converter")
        if self.cache_limit <= 0:
            raise ContractError("cache_limit must be positive")

    # -- the decision ---------------------------------------------------

    def plan(
        self,
        messages: Sequence[Any],
        *,
        scope: str,
        source_head: str,
        capacity: int | None,
        mandatory: Sequence[Any] = (),
    ) -> ProjectionResult:
        """Choose the smallest suffix of complete fragments that still means the same.

        The window is always a *suffix*: dropping a fragment from the middle would make
        the replayed baseline apply state updates in an order the transcript never had.
        """
        if not scope or not source_head:
            raise ContractError("a projection needs a scope and the source head it came from")
        if capacity is not None and capacity < 0:
            raise ContractError("capacity must not be negative")

        ordered = tuple(messages)
        source_fingerprint = fingerprint(
            {"messages": [_fingerprintable(message) for message in ordered]}
        )
        fragments = message_fragments(ordered)
        if not fragments:
            empty = self._measure(MeasurementInput(), "empty")
            return self._result(
                scope=scope,
                source_head=source_head,
                messages=(),
                kept=(),
                dropped=(),
                baseline=None,
                estimate=empty,
                capacity=capacity,
                source_fingerprint=source_fingerprint,
                original=ordered,
            )

        required = self._required(fragments, mandatory)
        first = self._starting_window(fragments, capacity)
        earliest_required = next((i for i, flag in enumerate(required) if flag), None)
        if earliest_required is not None and earliest_required < first:
            # Keeping the host's mandatory message means keeping everything newer than it.
            first = earliest_required

        extra_drops: set[int] = set()
        while True:
            projected, baseline = self._materialize(fragments, first)
            estimate = self._measure_input(projected)
            if capacity is None or estimate.tokens is None or estimate.tokens <= capacity:
                break
            if first >= len(fragments):
                raise BudgetExceeded(
                    "context",
                    f"the projection needs {estimate.tokens} tokens but the capacity is {capacity}; "
                    "there is nothing left to drop",
                )
            if required[first]:
                raise BudgetExceeded(
                    "context",
                    f"the projection needs {estimate.tokens} tokens but the capacity is {capacity}; "
                    f"the fragment at positions {list(fragments[first].positions)} is required "
                    "(the current turn, a host-mandated constraint, or both), so it cannot be dropped",
                )
            extra_drops.add(first)
            first += 1

        return self._result(
            scope=scope,
            source_head=source_head,
            messages=projected,
            kept=tuple(fragment.index for fragment in fragments[first:]),
            dropped=tuple(
                DroppedFragment(
                    index=fragment.index,
                    kind=fragment.kind,
                    positions=fragment.positions,
                    reason=(
                        "dropped to fit the measured capacity"
                        if index in extra_drops
                        else "not needed by the initial window"
                    ),
                )
                for index, fragment in enumerate(fragments[:first])
            ),
            baseline=baseline,
            estimate=estimate,
            capacity=capacity,
            source_fingerprint=source_fingerprint,
            original=ordered,
        )

    # -- pieces ---------------------------------------------------------

    def _required(self, fragments: Sequence[Fragment], mandatory: Sequence[Any]) -> tuple[bool, ...]:
        """Flags for the two kinds of fragment that may not be dropped.

        1. The newest fragment: it carries the current user input, or the tool round trip
           the model is waiting on. A view without it is not a smaller request, it is a
           different one.
        2. Any fragment the host marked as mandatory (a task constraint it wants kept).
        """
        flags = [False] * len(fragments)
        if fragments:
            flags[-1] = True
        if mandatory:
            wanted = tuple(mandatory)
            for index, fragment in enumerate(fragments):
                if flags[index]:
                    continue
                for message in fragment.messages:
                    if any(message is item or message == item for item in wanted):
                        flags[index] = True
                        break
        return tuple(flags)

    def _starting_window(self, fragments: Sequence[Fragment], capacity: int | None) -> int:
        """A cheap first guess: the newest fragments whose hinted sizes reach capacity.

        Only a starting point — the real measurement below decides — but it keeps the
        common case at one honest measurement instead of one per candidate window.
        """
        if capacity is None:
            return 0
        used = 0
        first = len(fragments)
        for index in range(len(fragments) - 1, -1, -1):
            tokens = self._fragment_tokens(fragments[index])
            size = tokens.tokens if tokens.tokens is not None else capacity + 1
            if used + size > capacity and first < len(fragments):
                break
            used += size
            first = index
        return first

    def _materialize(
        self, fragments: Sequence[Fragment], first: int
    ) -> tuple[tuple[Any, ...], SystemMessage | None]:
        kept = [message for fragment in fragments[first:] for message in fragment.messages]
        dropped = [message for fragment in fragments[:first] for message in fragment.messages]
        baseline = _baseline_message(dropped)
        projected = tuple(([baseline] if baseline is not None else []) + kept)
        return projected, baseline

    def _measure_input(self, messages: Sequence[Any]) -> TokenEstimate:
        key = fingerprint(
            {
                "messages": [_fingerprintable(message) for message in messages],
                "system_state": system_state(messages),
            }
        )
        return self._measure(self.convert(messages), key)

    def _fragment_tokens(self, fragment: Fragment) -> TokenEstimate:
        return self._measure(self.convert(fragment.messages), f"fragment:{fragment.fingerprint()}")

    def _measure(self, request: MeasurementInput, key: str) -> TokenEstimate:
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        estimate = self.estimator.measure(request)
        if len(self._cache) >= self.cache_limit:
            # Oldest insertion first: dict preserves order, and a plain pop is enough.
            self._cache.pop(next(iter(self._cache)), None)
        self._cache[key] = estimate
        return estimate

    def _result(
        self,
        *,
        scope: str,
        source_head: str,
        messages: Sequence[Any],
        kept: Sequence[int],
        dropped: Sequence[DroppedFragment],
        baseline: SystemMessage | None,
        estimate: TokenEstimate,
        capacity: int | None,
        source_fingerprint: str,
        original: Sequence[Any],
    ) -> ProjectionResult:
        # The transcript is the authority: the projection must not have touched it, and
        # the effective instruction/tool state must be the same one it had.
        after = fingerprint({"messages": [_fingerprintable(message) for message in original]})
        if after != source_fingerprint:
            raise ContractError("the projection modified the transcript it was given")
        before_state = system_state(original)
        after_state = system_state(messages)
        if (
            after_state["prompt"] != before_state["prompt"]
            or after_state["sections"] != before_state["sections"]
            or not _tools_equal(after_state["tools"], before_state["tools"])
        ):
            raise ContractError(
                "the projection is not equivalent to the transcript: the effective system "
                f"prompt or tool set would change ({before_state['tools']!r} -> "
                f"{after_state['tools']!r})"
            )
        fits = None
        if capacity is not None and estimate.tokens is not None:
            fits = estimate.tokens <= capacity
        return ProjectionResult(
            scope=scope,
            source_head=source_head,
            messages=tuple(messages),
            kept_fragments=tuple(kept),
            dropped=tuple(dropped),
            baseline_synthesized=baseline is not None,
            estimate=estimate,
            capacity=capacity,
            fits=fits,
            request_fingerprint=fingerprint(
                {"messages": [_fingerprintable(message) for message in messages]}
            ),
            source_fingerprint=source_fingerprint,
            effective_state_fingerprint=fingerprint(after_state),
            policy_version=self.policy_version,
            open_call_ids=tuple(
                call_id
                for fragment in message_fragments(messages)
                for call_id in fragment.open_call_ids
            ),
        )
